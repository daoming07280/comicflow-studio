from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from .common import JOBS, Context, read_json, write_json
from .pipeline import configuration, run_pipeline
from .speech import DEFAULT_VOICE, VOICES


def main():
    parser = argparse.ArgumentParser(description="ComicFlow · 漫画自动视频制作")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动本机工作台")
    serve.add_argument("--port", type=int, default=8765)
    run = sub.add_parser("run", help="一键从漫画生成视频")
    run.add_argument("source")
    run.add_argument("--title", default="我的漫画合集")
    run.add_argument("--mode", choices=["ocr", "ai", "manual"], default="ocr")
    run.add_argument("--tts", choices=["edge", "windows"], default="edge")
    run.add_argument("--preset", choices=["classic", "landscape", "portrait", "preview"], default="classic")
    run.add_argument("--rate", type=int, default=0)
    run.add_argument("--voice", choices=list(VOICES), default=DEFAULT_VOICE)
    resume = sub.add_parser("resume", help="继续已存在的任务")
    resume.add_argument("job_id")
    download = sub.add_parser("download", help="通过 gallery-dl 下载给定公开漫画链接")
    download.add_argument("url")
    download.add_argument("--destination", required=True)
    download.add_argument("--range", default="1-500", help="页码范围，默认最多 500 张")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from .server import app
        service = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
        app.state.service = service
        service.run()
        return
    if args.command == "download":
        from urllib.parse import urlsplit
        url = urlsplit(args.url)
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            parser.error("需要普通 HTTP(S) 漫画链接")
        dest = Path(args.destination).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        Context(dest).run([sys.executable, "-m", "gallery_dl", "--ignore-config", "--no-input", "--windows-filenames",
                           "--http-timeout", "30", "--retries", "2", "--range", args.range,
                           "--destination", dest, args.url], timeout=3600)
        print(f"下载结束：{dest}；请求范围 {args.range}。请核对章节与页数后导入。")
        return
    if args.command == "run":
        config = configuration(vars(args))
        config["source"] = str(Path(args.source).resolve())
        folder = JOBS / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "config.json", config)
        import time
        state = {"id": folder.name, "title": config["title"], "source": config["source"], "status": "running", "progress": 0,
                 "message": "开始制作", "created": time.time(), "updated": time.time(), "mode": config["mode"], "preset": config["preset"]}
        write_json(folder / "state.json", state)
    else:
        from .server import job_folder
        folder = job_folder(args.job_id)
        state = read_json(folder / "state.json")
    print(f"任务编号：{folder.name}", flush=True)
    def progress(**kw):
        state.update(kw)
        state["status"] = "running"
        write_json(folder / "state.json", state)
        print(f"{kw['progress']:5.1f}%  {kw['message']}", flush=True)
    try:
        result = run_pipeline(folder, callback=progress)
        state.update(status="completed", message="成片通过技术自审", **result)
        write_json(folder / "state.json", state)
        print(f"成片目录：{result['export']}")
    except Exception as e:
        state.update(status="failed", message=str(e))
        write_json(folder / "state.json", state)
        raise


if __name__ == "__main__":
    main()
