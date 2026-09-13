"""Persistent, resumable downloads isolated from video rendering jobs."""
from __future__ import annotations

import copy
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import sources
from .common import Cancelled, JobLock, file_hash, read_json, within, write_json

MAX_FILES = 20000
MAX_BYTES = 4 * 1024**3


def safe_name(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text).strip(' .')[:65] or '章节'


class DownloadManager:
    def __init__(self, data):
        self.data = Path(data)
        self.root = self.data / "downloads"
        self.library = self.data / "library"
        self.catalog = self.data / "catalog"
        for p in (self.root, self.library, self.catalog):
            p.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comic-download")
        self.events, self.futures = {}, {}
        for p in self.root.glob("*/state.json"):
            state = read_json(p)
            if state["status"] in {"running", "queued", "cancelling"}:
                self.update(p.parent.name, status="interrupted", message="上次下载已中断，可继续下载")

    def folder(self, task_id):
        if not re.fullmatch(r"[0-9a-f]{12}", task_id or ""):
            raise sources.SourceError("下载任务不存在")
        path = self.root / task_id
        if not path.is_dir():
            raise sources.SourceError("下载任务不存在")
        return path

    def update(self, task_id, **changes):
        with self.lock:
            p = self.folder(task_id) / "state.json"
            state = read_json(p)
            state.update(changes)
            state["updated"] = time.time()
            write_json(p, state)
            return state

    def get(self, task_id):
        with self.lock:
            return read_json(self.folder(task_id) / "state.json")

    def list(self):
        with self.lock:
            return sorted((read_json(p) for p in self.root.glob("*/state.json")), key=lambda s: s["created"], reverse=True)

    def book(self, provider, book_id, refresh=False):
        sources.validate_id(provider, book_id)
        path = self.catalog / f"{provider}-{book_id}.json"
        with self.lock:
            if not refresh and path.exists() and time.time() - path.stat().st_mtime < 900:
                return read_json(path)
        book = sources.details(provider, book_id)
        with self.lock:
            write_json(path, book)
        return book

    def remember_search(self, result):
        # Covers are fetched only for book IDs returned by a catalog, never user URLs.
        with self.lock:
            for group in result["groups"]:
                for book in group["items"]:
                    sources.validate_id(book["provider"], book["id"])
                    path = self.catalog / f"{book['provider']}-{book['id']}-search.json"
                    write_json(path, book)

    def cover(self, provider, book_id):
        sources.validate_id(provider, book_id)
        with self.lock:
            paths = [self.catalog / f"{provider}-{book_id}{suffix}.json" for suffix in ("-search", "")]
            book = next((read_json(p) for p in paths if p.exists()), None)
        if not book or not book.get("cover"):
            raise sources.SourceError("没有封面")
        return sources.image_bytes(provider, {"url": book["cover"]})

    def create(self, provider, book_id, chapter_ids):
        book = self.book(provider, book_id)
        wanted = set(chapter_ids)
        if not wanted or len(wanted) != len(chapter_ids) or len(wanted) > 500:
            raise sources.SourceError("请选择 1–500 个不同章节")
        chapters = [c for c in book["chapters"] if c["id"] in wanted]
        if len(chapters) != len(wanted):
            raise sources.SourceError("所选章节不属于这部漫画，请刷新目录后重选")
        if shutil.disk_usage(self.data).free < 1024**3:
            raise sources.SourceError("可用空间不足 1 GB，请先腾出空间")
        with self.lock:
            for old in self.list():
                if old["provider"] == provider and old["book_id"] == book_id and old["chapter_ids"] == [c["id"] for c in chapters] and old["status"] in {"queued", "running", "cancelling", "completed"}:
                    return old
            task_id = uuid.uuid4().hex[:12]
            folder = self.root / task_id
            folder.mkdir()
            config = {"book": copy.deepcopy(book), "chapters": chapters}
            write_json(folder / "config.json", config)
            state = {"id": task_id, "title": book["title"], "provider": provider, "book_id": book_id,
                     "chapter_ids": [c["id"] for c in chapters], "chapters": len(chapters), "completed_chapters": 0,
                     "status": "queued", "progress": 0, "pages": 0, "total_pages": 0, "bytes": 0,
                     "source": "", "message": "等待下载", "created": time.time(), "updated": time.time()}
            write_json(folder / "state.json", state)
            self.enqueue(task_id)
            return state

    def enqueue(self, task_id):
        with self.lock:
            event = threading.Event()
            self.events[task_id] = event
            self.futures[task_id] = self.executor.submit(self.execute, task_id, event)

    def cancel(self, task_id):
        with self.lock:
            if self.get(task_id)["status"] not in {"running", "queued"}:
                raise sources.SourceError("这个下载任务当前无需停止")
            self.events[task_id].set()
            return self.update(task_id, status="cancelling", message="正在停止，已下载图片会保留")

    def resume(self, task_id):
        with self.lock:
            if self.get(task_id)["status"] not in {"failed", "cancelled", "interrupted"}:
                raise sources.SourceError("只有失败或中断的下载可以继续")
            if task_id in self.futures and not self.futures[task_id].done():
                raise sources.SourceError("下载仍在退出，请稍后重试")
            state = self.update(task_id, status="queued", message="继续下载，检查并复用已完成的图片")
            self.enqueue(task_id)
            return state

    def imported(self, task_id):
        state = self.get(task_id)
        if state["status"] != "completed":
            raise sources.SourceError("完整下载通过检查后才能导入")
        source = within(self.library / task_id, "pages")
        manifest = read_json(self.library / task_id / "manifest.json")
        for entry in manifest["files"].values():
            path = within(source, entry["file"])
            if not path.is_file() or file_hash(path) != entry["sha256"]:
                self.update(task_id, status="failed", source="", message="本地图片缺失或被改动，点击继续下载可修复")
                raise sources.SourceError("本地漫画图片缺失或被改动，请继续下载以修复")
        return {"source": str(source), "title": state["title"], "count": state["pages"], "chapters": state["chapters"]}

    def execute(self, task_id, event):
        folder = self.folder(task_id)
        try:
            with JobLock(folder):
                self._download(task_id, event)
        except Cancelled:
            self.update(task_id, status="cancelled", source="", message="已停止，继续下载会复用完整图片")
        except Exception as exc:
            (folder / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            self.update(task_id, status="failed", source="", message=str(exc)[:600] or "下载失败，请重试")

    def _download(self, task_id, event):
        def check():
            if event.is_set():
                raise Cancelled()
        check()
        config = read_json(self.folder(task_id) / "config.json")
        book, chapters = config["book"], config["chapters"]
        provider = book["provider"]
        base = self.library / task_id
        pages_root = base / "pages"
        pages_root.mkdir(parents=True, exist_ok=True)
        manifest_path = base / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {"book": book, "chapters": chapters, "files": {}, "page_counts": {}}
        plans, total_pages = [], 0
        self.update(task_id, status="running", source="", message="正在读取所选章节的图片目录", progress=0)
        for ci, chapter in enumerate(chapters):
            check()
            self.update(task_id, message=f"读取章节 {ci + 1}/{len(chapters)} · {chapter['title']}")
            pages = sources.chapter_pages(provider, chapter["id"], event)
            previous_count = manifest["page_counts"].get(chapter["id"])
            if previous_count is not None and previous_count != len(pages):
                raise sources.SourceError("源站章节页数已变化，请新建下载以避免混入旧页")
            manifest["page_counts"][chapter["id"]] = len(pages)
            total_pages += len(pages)
            if total_pages > MAX_FILES:
                raise sources.SourceError("所选章节超过 20000 页，请分批下载")
            plans.append((chapter, pages))
        write_json(manifest_path, manifest)
        count, size, completed_chapters = 0, 0, 0
        def receive(item):
            key, page, relative = item
            check()
            entry = manifest["files"].get(key)
            if entry:
                existing = within(pages_root, entry["file"])
                if existing.is_file() and file_hash(existing) == entry["sha256"]:
                    return key, entry, None, existing
            raw, ext = sources.image_bytes(provider, page, event)
            path = within(pages_root, relative + ext)
            return key, None, raw, path
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="comic-page") as pool:
            for chapter, pages in plans:
                destination = f"{chapter['order']:05d}_{safe_name(chapter['title'])}"
                # Keep the parent stable while Windows resolves paths in worker threads.
                within(pages_root, destination).mkdir(parents=True, exist_ok=True)
                for offset in range(0, len(pages), 3):
                    check()
                    batch = [(f"{chapter['id']}:{i}", pages[i], f"{destination}/{i + 1:05d}") for i in range(offset, min(offset + 3, len(pages)))]
                    pending = [pool.submit(receive, item) for item in batch]
                    for future in as_completed(pending):
                        check()
                        key, entry, raw, path = future.result()
                        added = len(raw) if raw is not None else path.stat().st_size
                        if size + added > MAX_BYTES or shutil.disk_usage(base).free < added + 256 * 1024**2:
                            raise sources.SourceError("本批下载达到 4 GB 或磁盘空间不足，请分批下载")
                        if raw is not None:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            partial = path.with_suffix(".part")
                            partial.write_bytes(raw)
                            partial.replace(path)
                            entry = {"file": path.relative_to(pages_root).as_posix(), "sha256": file_hash(path), "bytes": len(raw)}
                            previous = manifest["files"].get(key)
                            if previous and previous["file"] != entry["file"]:
                                old = within(pages_root, previous["file"])
                                if old.is_file():
                                    old.replace(base / ("replaced-" + uuid.uuid4().hex + old.suffix))
                        manifest["files"][key] = entry
                        count += 1
                        size += added
                        write_json(manifest_path, manifest)
                        self.update(task_id, pages=count, total_pages=total_pages, bytes=size,
                                    progress=round(count / total_pages * 99, 1), completed_chapters=completed_chapters,
                                    message=f"下载 {count}/{total_pages} 页 · {chapter['title']}")
                completed_chapters += 1
        check()
        if count != total_pages or len(manifest["files"]) != total_pages:
            raise sources.SourceError("下载页数不完整，未导入素材区")
        manifest["completed"] = time.time()
        write_json(manifest_path, manifest)
        self.update(task_id, status="completed", source=str(pages_root.resolve()), completed_chapters=len(chapters),
                    pages=count, total_pages=total_pages, bytes=size, progress=100,
                    message=f"已下载 {len(chapters)} 章、{count} 页 · 图片完整性检查通过")

    def close(self):
        for event in self.events.values():
            event.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
