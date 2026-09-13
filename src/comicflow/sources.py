"""Built-in comic catalogs. Only structured metadata is read; no remote scripts run.

Source research and upstream references are recorded in docs/SOURCES.md.
"""
from __future__ import annotations

import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from .common import Cancelled, natural_key


class SourceError(ValueError):
    pass


PROVIDERS = {
    "dogemanga": {"id": "dogemanga", "name": "漫画狗", "description": "中文漫画 · 简繁体", "url": "https://dogemanga.com"},
    "mangadex": {"id": "mangadex", "name": "MangaDex", "description": "多语言漫画 · 可选择译本", "url": "https://mangadex.org"},
}
LANGUAGES = {"zh": "简体中文", "zh-hk": "繁体中文", "en": "英语", "ko": "韩语", "ja": "日语",
             "pt-br": "葡萄牙语（巴西）", "pt": "葡萄牙语", "es": "西班牙语", "es-la": "西班牙语（拉美）",
             "fr": "法语", "de": "德语", "it": "意大利语", "ru": "俄语", "vi": "越南语", "th": "泰语",
             "id": "印尼语", "ms": "马来语", "pl": "波兰语", "tr": "土耳其语", "ar": "阿拉伯语",
             "hi": "印地语", "he": "希伯来语", "ka": "格鲁吉亚语", "uk": "乌克兰语", "hu": "匈牙利语",
             "ro": "罗马尼亚语", "fa": "波斯语", "cs": "捷克语", "nl": "荷兰语", "sv": "瑞典语",
             "el": "希腊语", "bn": "孟加拉语", "ta": "泰米尔语", "te": "泰卢固语", "lt": "立陶宛语",
             "kk": "哈萨克语", "tl": "菲律宾语", "sr": "塞尔维亚语", "my": "缅甸语"}
HEADERS = {"User-Agent": "ComicFlow/0.2 (local comic reader)", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}


def validate_id(provider, value):
    if provider not in PROVIDERS:
        raise SourceError("请选择内置漫画源")
    pattern = r"[A-Za-z0-9_-]{1,80}" if provider == "dogemanga" else r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SourceError("漫画或章节编号无效")
    return value


def allowed_url(provider, url):
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        raise SourceError("漫画源返回了不支持的资源地址")
    hosts = ("dogemanga.com",) if provider == "dogemanga" else ("mangadex.org", "mangadex.network")
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise SourceError("漫画源返回了未识别的图片域名，请更新源适配器")
    return url


def fetch(provider, url, *, params=None, event=None, limit=8 * 1024**2, referer=None):
    event = event or threading.Event()
    headers = {**HEADERS, "Referer": referer or PROVIDERS[provider]["url"] + "/"}
    last = None
    for attempt in range(3):
        if event.is_set():
            raise Cancelled()
        try:
            target = allowed_url(provider, url)
            query = params
            with httpx.Client(timeout=httpx.Timeout(18, connect=8), headers=headers) as client:
                for _ in range(6):
                    with client.stream("GET", target, params=query) as response:
                        if response.is_redirect:
                            target = allowed_url(provider, urljoin(str(response.url), response.headers.get("location", "")))
                            query = None
                            continue
                        if response.status_code in (401, 403):
                            raise SourceError("该漫画源当前要求登录或验证，请稍后重试或选择其他源")
                        if response.status_code == 404:
                            raise SourceError("这部漫画或章节已不可用")
                        response.raise_for_status()
                        result = bytearray()
                        for part in response.iter_bytes(64 * 1024):
                            if event.is_set():
                                raise Cancelled()
                            result.extend(part)
                            if len(result) > limit:
                                raise SourceError("单个资源超过大小限制，请分批下载")
                        return bytes(result)
                raise SourceError("漫画源重定向过多")
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            if attempt < 2 and event.wait(1 + attempt):
                raise Cancelled()
    status = getattr(getattr(last, "response", None), "status_code", None)
    raise SourceError(f"{PROVIDERS[provider]['name']}暂时连接失败" + (f"（HTTP {status}）" if status else "（网络超时或连接中断）") + "，可重试或切换漫画源") from last


def soup_at(provider, url, **kwargs):
    return BeautifulSoup(fetch(provider, url, **kwargs).decode("utf-8"), "html.parser")


def json_at(url, **kwargs):
    import json
    raw = fetch("mangadex", url, **kwargs)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SourceError("MangaDex 返回了无法识别的数据，请稍后重试") from exc
    if data.get("result") != "ok":
        raise SourceError("MangaDex 暂时无法提供目录")
    return data


def text_of(node):
    return node.get_text(" ", strip=True) if node else ""


def md_book(item):
    a = item["attributes"]
    titles = {**a.get("title", {})}
    for alternate in a.get("altTitles", []):
        for lang, title in alternate.items():
            titles.setdefault(lang, title)
    title = next((titles[k] for k in ("zh", "zh-hk", "en", "ko-ro", "ja-ro") if titles.get(k)), next(iter(titles.values()), "未命名漫画"))
    cover = next((r.get("attributes", {}).get("fileName") for r in item.get("relationships", []) if r["type"] == "cover_art"), None)
    description = a.get("description", {})
    return {"id": item["id"], "provider": "mangadex", "title": title,
            "author": " / ".join(r.get("attributes", {}).get("name", "") for r in item.get("relationships", []) if r["type"] == "author"),
            "description": description.get("zh", description.get("en", ""))[:1200],
            "cover": f"https://uploads.mangadex.org/covers/{item['id']}/{cover}.256.jpg" if cover else "",
            "url": f"https://mangadex.org/title/{item['id']}",
            "languages": [LANGUAGES.get(x, x) for x in a.get("availableTranslatedLanguages", []) if x],
            "original_language": a.get("originalLanguage", "")}


def search_one(provider, query, page):
    if provider == "dogemanga":
        s = soup_at(provider, "https://dogemanga.com/", params={"q": query, "o": (page - 1) * 24})
        books = []
        for card in s.select(".site-card[data-manga-id]"):
            title = card.select_one(".site-card__manga-title")
            if not title:
                continue
            image = card.select_one("img.card-img-top")
            book_id = validate_id(provider, card["data-manga-id"])
            books.append({"id": book_id, "provider": provider, "title": text_of(title),
                          "author": text_of(card.select_one(".card-subtitle")),
                          "description": text_of(card.select_one(".site-card__brief"))[:1200],
                          "cover": urljoin("https://dogemanga.com/", image.get("src", "")) if image else "",
                          "url": f"https://dogemanga.com/m/{book_id}", "languages": ["中文"]})
        if not books and not s.select_one(".site-main-content"):
            raise SourceError("漫画狗页面结构已变化或正在维护，请稍后重试")
        has_next = any("下一頁" in text_of(a) for a in s.select("a[href]"))
        return books, has_next
    params = [("title", query), ("limit", 24), ("offset", (page - 1) * 24),
              ("includes[]", "cover_art"), ("includes[]", "author"),
              ("contentRating[]", "safe"), ("contentRating[]", "suggestive"), ("order[relevance]", "desc")]
    d = json_at("https://api.mangadex.org/manga", params=params)
    return [md_book(x) for x in d["data"]], d["offset"] + len(d["data"]) < min(d["total"], 10000)


def search(query, provider="all", page=1):
    query = query.strip()
    if not query or len(query) > 100 or page < 1 or page > 100:
        raise SourceError("请输入 1–100 个字的漫画名称")
    if provider != "all" and provider not in PROVIDERS:
        raise SourceError("请选择内置漫画源")
    selected = list(PROVIDERS) if provider == "all" else [provider]
    def run(p):
        try:
            items, more = search_one(p, query, page)
            return {"provider": p, "items": items, "has_next": more, "error": ""}
        except (SourceError, KeyError, TypeError, UnicodeError) as exc:
            return {"provider": p, "items": [], "has_next": False, "error": str(exc) if isinstance(exc, SourceError) else "漫画源数据格式已变化"}
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        groups = list(pool.map(run, selected))
    return {"query": query, "page": page, "groups": groups, "has_next": any(x["has_next"] for x in groups)}


def details(provider, book_id, event=None):
    validate_id(provider, book_id)
    if provider == "dogemanga":
        url = f"https://dogemanga.com/m/{book_id}"
        s = soup_at(provider, url, event=event)
        title = s.select_one('meta[property="og:title"]')
        desc = s.select_one('meta[name="description"]')
        cover = s.select_one('meta[property="og:image"]')
        if not title:
            raise SourceError("漫画狗未返回有效漫画详情")
        book = {"provider": provider, "id": book_id, "title": title.get("content", "").removesuffix(" - 漫畫狗"),
                "description": desc.get("content", "")[:1200] if desc else "", "url": url,
                "cover": cover.get("content", "") if cover else "", "groups": [], "chapters": []}
        panes = s.select('[id^="site-manga__tab-pane-"]')
        groups = [p for p in panes if not p["id"].endswith("-all")] or panes
        seen = set()
        names = {"issue": "正篇", "bangaihen": "番外", "tankobon": "单行本", "all": "全部章节"}
        for pane in groups:
            key = pane["id"].removeprefix("site-manga__tab-pane-")
            rows = []
            for a in pane.select('a[href*="/p/"]'):
                cid = urlsplit(a["href"]).path.rsplit("/", 1)[-1]
                validate_id(provider, cid)
                if cid in seen:
                    continue
                seen.add(cid)
                rows.append({"id": cid, "title": text_of(a), "group": key, "language": "zh"})
            # Chapter numbers need natural order; prologues precede chapter 1.
            rows.sort(key=lambda c: (0 if any(t in c["title"] for t in ("序章", "序幕", "前言")) else 1, natural_key(c["title"])))
            if rows:
                book["groups"].append({"id": key, "name": names.get(key, key), "count": len(rows)})
                book["chapters"].extend(rows)
    else:
        item = json_at(f"https://api.mangadex.org/manga/{book_id}", params=[("includes[]", "cover_art"), ("includes[]", "author")], event=event)["data"]
        book = {**md_book(item), "groups": [], "chapters": []}
        rows, seen = [], set()
        for offset in range(0, 10000, 500):
            d = json_at(f"https://api.mangadex.org/manga/{book_id}/feed", event=event, params=[
                ("limit", 500), ("offset", offset), ("order[volume]", "asc"), ("order[chapter]", "asc"),
                ("includeExternalUrl", 0), ("contentRating[]", "safe"), ("contentRating[]", "suggestive")])
            for chapter in d["data"]:
                a = chapter["attributes"]
                if a.get("externalUrl") or not a.get("pages"):
                    continue
                lang = a["translatedLanguage"]
                key = (lang, a.get("volume"), a.get("chapter")) if a.get("chapter") is not None else (chapter["id"],)
                if key in seen:
                    continue
                seen.add(key)
                label = (f"第 {a['chapter']} 话" if a.get("chapter") is not None else "特别篇") + (" · " + a["title"] if a.get("title") else "")
                if a.get("volume"):
                    label = f"卷 {a['volume']} · " + label
                rows.append({"id": chapter["id"], "title": label, "group": lang, "language": lang, "pages": a["pages"]})
            if offset + len(d["data"]) >= d["total"] or not d["data"]:
                break
        else:
            book["notice"] = "该书目录超过 10000 条，目前仅显示前 10000 条中的可用译本。"
        languages = sorted({r["group"] for r in rows}, key=lambda x: ((["zh", "zh-hk", "en"].index(x) if x in ["zh", "zh-hk", "en"] else 3), x))
        book["groups"] = [{"id": x, "name": LANGUAGES.get(x, x), "count": sum(r["group"] == x for r in rows)} for x in languages]
        book["chapters"] = rows
    for index, chapter in enumerate(book["chapters"], 1):
        chapter["order"] = index
    if not book["chapters"]:
        book["notice"] = "当前源没有可直接下载的章节，可以尝试其他版本或漫画源。"
    return book


def chapter_pages(provider, chapter_id, event=None):
    validate_id(provider, chapter_id)
    if provider == "dogemanga":
        url = f"https://dogemanga.com/p/{chapter_id}"
        s = soup_at(provider, url, event=event)
        images = s.select("img.site-reader__image[data-page-image-url][data-page-index]")
        images.sort(key=lambda n: int(n["data-page-index"]))
        if not images:
            raise SourceError("该章节未返回漫画图片，可能已下架或需要登录")
        indices = [int(n["data-page-index"]) for n in images]
        if indices != list(range(len(images))):
            raise SourceError("章节页码不完整，已停止以免导入缺页漫画")
        return [{"url": allowed_url(provider, n["data-page-image-url"]), "referer": url} for n in images]
    data = json_at(f"https://api.mangadex.org/at-home/server/{chapter_id}", event=event)
    chapter = data["chapter"]
    if not chapter.get("data"):
        raise SourceError("该章节没有可用的漫画图片")
    return [{"url": allowed_url(provider, f"{data['baseUrl']}/data/{chapter['hash']}/{name}"), "referer": "https://mangadex.org/"} for name in chapter["data"]]


def image_bytes(provider, page, event=None):
    raw = fetch(provider, page["url"], referer=page.get("referer"), event=event, limit=30 * 1024**2)
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > 100_000_000:
                raise ValueError("图片像素过大")
            ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(im.format)
            if not ext:
                raise ValueError("图片格式不支持")
            im.verify()
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise SourceError("漫画源返回的图片无法解码，未将其当作成功下载") from exc
    return raw, ext
