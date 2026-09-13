from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

from .common import IMAGE_EXT, file_hash, natural_key, write_json

MAX_BYTES = 4 * 1024 ** 3
MAX_FILES = 20000
MAX_PIXELS = 100_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def extract_zip(archive, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        if len(members) > MAX_FILES or sum(i.file_size for i in members) > MAX_BYTES:
            raise ValueError("压缩包超过限制：最多 20000 项、解压后 4 GB")
        # Validate the whole archive before writing a single member.
        for item in members:
            name = item.filename.replace("\\", "/")
            parts = PurePosixPath(name)
            if parts.is_absolute() or ".." in parts.parts or ":" in name or "\x00" in name:
                raise ValueError("压缩包包含不安全的路径")
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("压缩包不能包含符号链接")
            if not (destination / name).resolve().is_relative_to(destination):
                raise ValueError("压缩包路径越界")
        for item in members:
            if item.is_dir():
                continue
            target = destination / item.filename.replace("\\", "/")
            if target.suffix.lower() not in IMAGE_EXT | {".json", ".wav", ".mp3", ".m4a", ".flac"}:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(item) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)


def import_source(source, folder, ctx):
    source, folder = Path(source).resolve(), Path(folder)
    if not source.exists():
        raise ValueError("漫画路径不存在，请检查文件夹或 ZIP / CBZ 文件路径。")
    pages_dir = folder / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    if source.is_file() and source.suffix.lower() in {".zip", ".cbz"}:
        base = folder / "unpacked"
        extract_zip(source, base)
    elif source.is_dir():
        base = source
        if folder.resolve().is_relative_to(base):
            raise ValueError("输入目录包含工作缓存，不能递归导入自身；请选择独立漫画目录。")
        archives = sorted([p for p in base.rglob('*') if p.is_file() and not p.is_symlink()
                           and p.suffix.lower() in {'.zip', '.cbz'}], key=natural_key)
        if archives:
            all_files = [p for p in base.rglob('*') if p.is_file() and not p.is_symlink()
                         and p.suffix.lower() in IMAGE_EXT | {'.json', '.wav', '.mp3', '.m4a', '.flac'}]
            total = sum(p.stat().st_size for p in all_files)
            count = len(all_files)
            for archive in archives:
                with zipfile.ZipFile(archive) as z:
                    total += sum(i.file_size for i in z.infolist())
                    count += len(z.infolist())
            if total > MAX_BYTES or count > MAX_FILES:
                raise ValueError('多个章节压缩包合计超过 20000 项或解压后 4 GB，请分批导入。')
            combined = folder / 'expanded'
            combined.mkdir(parents=True, exist_ok=True)
            for item in all_files:
                dest = combined / item.relative_to(base)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
            for index, archive in enumerate(archives):
                ctx.progress(1, f'解压章节 {index + 1}/{len(archives)} · {archive.name}')
                extract_zip(archive, combined / archive.relative_to(base))
            base = combined
    elif source.suffix.lower() in IMAGE_EXT:
        base = source.parent
    else:
        raise ValueError("支持漫画文件夹、ZIP、CBZ 或图片；参考视频不作为漫画原稿直接导入。")
    files = [source] if source.is_file() and source.suffix.lower() in IMAGE_EXT else sorted(
        [p for p in base.rglob("*") if p.suffix.lower() in IMAGE_EXT and p.is_file()
         and not p.is_symlink() and not any(x.startswith(".") or x == "__MACOSX" for x in p.relative_to(base).parts)],
        key=lambda p: natural_key(p.relative_to(base).as_posix()))
    if not files:
        raise ValueError("没有找到可导入的漫画图片。压缩包内部需包含 JPG、PNG 或 WebP 等图片。")
    if len(files) > MAX_FILES or sum(p.stat().st_size for p in files) > MAX_BYTES:
        raise ValueError("本次导入超过 20000 张或 4 GB，请按章节分批导入。")
    pages, seen = [], {}
    for i, src in enumerate(files):
        ctx.progress(2 + 8 * i / len(files), f"导入漫画 {i + 1}/{len(files)} · {src.name}")
        h = file_hash(src)
        if h in seen:
            ctx.warn(f"重复图片仍按原顺序保留：{src.relative_to(base)}（与 {seen[h]} 内容相同）")
        seen[h] = str(src.relative_to(base))
        try:
            with Image.open(src) as im:
                if im.width * im.height > MAX_PIXELS:
                    raise ValueError("图片像素超过 1 亿")
                im = ImageOps.exif_transpose(im)
                rgba = im.convert("RGBA")
                canvas = Image.new("RGB", im.size, "white")
                canvas.paste(rgba, mask=rgba.getchannel("A"))
                name = f"{i + 1:05d}.jpg"
                canvas.save(pages_dir / name, quality=95)
                pages.append({"id": f"page{i + 1:05d}", "file": f"pages/{name}",
                              "source": src.relative_to(base).as_posix(),
                              "chapter": src.parent.relative_to(base).as_posix(),
                              "width": im.width, "height": im.height, "sha256": h})
        except Exception as e:
            raise ValueError(f"图片无法解码：{src.name}；{e}") from e
    # Freeze optional script/audio into the job so resume cannot silently change inputs.
    script = base / "narration.json"
    if script.exists():
        from .common import read_json, within
        payload = read_json(script)
        for scene in payload.get("scenes", []):
            if scene.get("audio"):
                audio = within(base, scene["audio"])
                if audio.suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac"} or not audio.is_file():
                    raise ValueError("自备配音文件不存在或格式不支持")
                target = folder / "imported_audio" / (file_hash(audio)[:20] + audio.suffix.lower())
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(audio, target)
                scene["audio"] = target.relative_to(folder).as_posix()
        write_json(folder / "narration.json", payload)
    write_json(folder / "pages.json", pages)
    return pages
