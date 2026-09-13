from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .common import DATA, EXPORTS, JOBS, ROOT, Cancelled, Context, JobLock, read_json, within, write_json
from .pipeline import configuration, run_pipeline
from .speech import DEFAULT_VOICE, VOICES, synthesize
from . import sources
from .downloads import DownloadManager

_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comicflow")
_events = {}
_futures = {}


def job_folder(job_id):
    if not job_id or len(job_id) != 12 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(404, "任务不存在")
    folder = JOBS / job_id
    if not folder.exists():
        raise HTTPException(404, "任务不存在")
    return folder


def update(folder, **changes):
    with _lock:
        path = folder / "state.json"
        state = read_json(path)
        state.update(changes)
        state["updated"] = time.time()
        write_json(path, state)


def execute(folder, event):
    try:
        if event.is_set():
            raise Cancelled()
        update(folder, status="running", message="开始处理漫画")
        result = run_pipeline(folder, event, lambda **kw: update(folder, **kw))
        update(folder, status="completed", progress=100, message="成片已完成 · 技术自审通过", **result)
    except Cancelled:
        update(folder, status="cancelled", message="已停止，可从已完成的步骤继续")
    except Exception as e:
        import traceback
        (folder / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        message = str(e)
        if len(message) > 900:
            message = message[:900] + "…（详见任务日志）"
        update(folder, status="failed", message=message)


def enqueue(folder):
    event = threading.Event()
    with _lock:
        _events[folder.name] = event
        _futures[folder.name] = _executor.submit(execute, folder, event)


@asynccontextmanager
async def lifespan(app):
    JOBS.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    for path in JOBS.glob("*/state.json"):
        state = read_json(path)
        if state["status"] in {"running", "queued", "cancelling"}:
            update(path.parent, status="interrupted", message="上次运行已中断，可点击继续")
    app.state.downloads = DownloadManager(DATA)
    yield
    for event in _events.values():
        event.set()
    app.state.downloads.close()


app = FastAPI(title="ComicFlow Studio", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_only(request: Request, call_next):
    # No browser page from another origin may start local jobs or read/upload files.
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "只允许本机工作台发起操作"}, status_code=403)
    return await call_next(request)


class JobRequest(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    title: str = "我的漫画合集"
    mode: str = "ocr"
    tts: str = "edge"
    voice: str = DEFAULT_VOICE
    rate: int = 0
    preset: str = "classic"
    fps: int = 24
    motion: bool = True
    subtitles: bool = True


class VoicePreviewRequest(BaseModel):
    voice: str = DEFAULT_VOICE
    rate: int = Field(default=0, ge=-3, le=5)


_preview_lock = threading.Lock()
PREVIEW_TEXT = "地下城的大门缓缓打开，少年握紧手中的长剑。他还不知道，这场冒险，将彻底改变自己的命运。"


@app.post("/api/voice-preview")
def voice_preview(req: VoicePreviewRequest):
    if req.voice not in VOICES:
        raise HTTPException(400, "请选择列表中的音色")
    folder = DATA / "voice-previews"
    folder.mkdir(parents=True, exist_ok=True)
    config = configuration({"voice": req.voice, "rate": req.rate})
    try:
        with _preview_lock, JobLock(folder):
            path, seconds = synthesize({"text": PREVIEW_TEXT}, folder, config, Context(folder))
        return {"url": f"/voice-previews/{path.name}", "voice": req.voice,
                "label": VOICES[req.voice], "duration": seconds, "text": PREVIEW_TEXT}
    except Exception as e:
        raise HTTPException(502, "试听配音暂时失败，请检查网络后重试。") from e


@app.get("/voice-previews/{name}")
def voice_preview_asset(name: str):
    if len(name) != 28 or not name.endswith(".wav") or any(c not in "0123456789abcdef" for c in name[:-4]):
        raise HTTPException(404, "试听不存在")
    path = DATA / "voice-previews" / "audio" / name
    if not path.is_file():
        raise HTTPException(404, "试听不存在")
    return FileResponse(path)


@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.2.0", "root": str(ROOT),
            "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
            "vision_ready": bool(os.environ.get("COMICFLOW_VISION_BASE_URL") and os.environ.get("COMICFLOW_VISION_MODEL")),
            "example": str(ROOT / "examples" / "starter"), "windows": os.name == "nt"}


@app.get("/api/jobs")
def list_jobs():
    with _lock:
        jobs = [read_json(p) for p in JOBS.glob("*/state.json")]
    return sorted(jobs, key=lambda x: x["created"], reverse=True)


@app.post("/api/shutdown")
def shutdown():
    if not hasattr(app.state, "service"):
        raise HTTPException(409, "当前运行方式不支持关闭，请结束启动进程。")
    for event in _events.values():
        event.set()
    app.state.service.should_exit = True
    return {"ok": True}


@app.post("/api/jobs")
def create_job(req: JobRequest):
    try:
        values = req.model_dump()
        values["source"] = values["source"].strip().strip('"')
        config = configuration(values)
        path = Path(config["source"]).resolve()
        if not path.exists():
            raise ValueError("漫画路径不存在")
        if path == ROOT or ROOT.is_relative_to(path) or path.is_relative_to(JOBS):
            raise ValueError("请选择只包含漫画的文件夹，不能选择项目根目录或运行缓存目录。")
        if config["mode"] == "ai" and not health()["vision_ready"]:
            raise ValueError("尚未连接视觉模型；可先使用自动讲读。AI 配置说明见使用手册。")
        if shutil.disk_usage(ROOT).free < 1024 ** 3:
            raise ValueError("可用磁盘空间不足 1 GB，请腾出空间后再生成。")
        folder = JOBS / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True)
        config["source"] = str(path)
        write_json(folder / "config.json", config)
        state = {"id": folder.name, "title": config["title"], "source": str(path), "status": "queued",
                 "progress": 0, "message": "已加入制作队列", "created": time.time(), "updated": time.time(),
                 "mode": config["mode"], "preset": config["preset"]}
        write_json(folder / "state.json", state)
        enqueue(folder)
        return state
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with _lock:
        return read_json(job_folder(job_id) / "state.json")


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    folder = job_folder(job_id)
    with _lock:
        state = read_json(folder / "state.json")
        if state["status"] not in {"queued", "running"}:
            raise HTTPException(409, "当前任务无需停止")
        update(folder, status="cancelling", message="正在停止，保留已完成步骤")
        _events[job_id].set()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: str):
    folder = job_folder(job_id)
    with _lock:
        state = read_json(folder / "state.json")
        if state["status"] not in {"failed", "cancelled", "interrupted"}:
            raise HTTPException(409, "只有失败、停止或中断的任务可以继续")
        if job_id in _futures and not _futures[job_id].done():
            raise HTTPException(409, "上一次处理仍在退出，请稍后继续")
        update(folder, status="queued", message="继续处理，复用已完成步骤")
        enqueue(folder)
    return {"ok": True}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), paths: str = Form("[]")):
    allowed = {".zip", ".cbz", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".json", ".wav", ".mp3", ".m4a", ".flac"}
    staging = DATA / "imports" / uuid.uuid4().hex[:12]
    staging.mkdir(parents=True)
    total, targets = 0, set()
    try:
        names = json.loads(paths)
        if not isinstance(names, list) or len(files) > 20000 or (names and len(names) != len(files)):
            raise ValueError("上传文件列表无效")
        for i, file in enumerate(files):
            name = names[i] if names else file.filename
            if not isinstance(name, str) or not name or ":" in name or "\\" in name:
                raise ValueError("上传文件路径无效")
            path = within(staging, name)
            if path in targets or path.suffix.lower() not in allowed:
                raise ValueError("上传文件重名或格式不支持")
            targets.add(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 4 * 1024 ** 3:
                        raise ValueError("本次上传超过 4 GB，请改用本地路径导入")
                    output.write(chunk)
        if len(targets) == 1:
            only = next(iter(targets))
            if only.suffix.lower() in {".zip", ".cbz"}:
                return {"source": str(only), "count": 1}
        # A directory selection supplies one top-level directory; retain its script root.
        children = list(staging.iterdir())
        source = children[0] if len(children) == 1 and children[0].is_dir() else staging
        return {"source": str(source), "count": len(files)}
    except (ValueError, json.JSONDecodeError) as e:
        # Only remove the UUID staging directory created by this request.
        if staging.resolve().is_relative_to((DATA / "imports").resolve()):
            shutil.rmtree(staging)
        raise HTTPException(400, str(e)) from e
    finally:
        for file in files:
            await file.close()


@app.get("/jobs/{job_id}/{asset:path}")
def job_asset(job_id: str, asset: str):
    folder = job_folder(job_id)
    try:
        path = within(folder, asset)
    except ValueError:
        raise HTTPException(404, "文件不存在")
    allowed = {".html", ".mp4", ".jpg", ".wav", ".mp3", ".flac", ".json", ".srt", ".log"}
    if not path.is_file() or path.suffix.lower() not in allowed or path.name == "config.json":
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


class DownloadRequest(BaseModel):
    provider: str
    book_id: str = Field(max_length=80)
    chapter_ids: list[str] = Field(min_length=1, max_length=500)


@app.exception_handler(sources.SourceError)
async def source_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/api/sources")
def source_list():
    return list(sources.PROVIDERS.values())


@app.get("/api/catalog/search")
def catalog_search(q: str, provider: str = "all", page: int = 1):
    result = sources.search(q, provider, page)
    app.state.downloads.remember_search(result)
    return result


@app.get("/api/catalog/{provider}/{book_id}")
def catalog_details(provider: str, book_id: str, refresh: bool = False):
    return app.state.downloads.book(provider, book_id, refresh=refresh)


@app.get("/api/catalog/{provider}/{book_id}/cover")
def catalog_cover(provider: str, book_id: str):
    try:
        raw, ext = app.state.downloads.cover(provider, book_id)
        return Response(raw, media_type={".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[ext], headers={"Cache-Control": "private, max-age=3600"})
    except sources.SourceError:
        raise HTTPException(404, "封面暂不可用")


@app.get("/api/downloads")
def downloads_list():
    return app.state.downloads.list()


@app.post("/api/downloads")
def download_create(req: DownloadRequest):
    return app.state.downloads.create(req.provider, req.book_id, req.chapter_ids)


@app.get("/api/downloads/{task_id}")
def download_get(task_id: str):
    return app.state.downloads.get(task_id)


@app.post("/api/downloads/{task_id}/cancel")
def download_cancel(task_id: str):
    return app.state.downloads.cancel(task_id)


@app.post("/api/downloads/{task_id}/resume")
def download_resume(task_id: str):
    return app.state.downloads.resume(task_id)


@app.post("/api/downloads/{task_id}/import")
def download_import(task_id: str):
    return app.state.downloads.imported(task_id)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
