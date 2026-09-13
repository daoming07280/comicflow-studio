from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import EXPORTS, Context, JobLock, read_json, write_json
from .importers import import_source
from .narration import make_plan, recognize
from .panels import make_panels
from .render import PRESETS, audit, render_video, review_html
from .speech import DEFAULT_VOICE

DEFAULTS = {"title": "我的漫画合集", "mode": "ocr", "tts": "edge", "voice": DEFAULT_VOICE,
            "rate": 0, "preset": "classic", "fps": 24, "motion": True, "subtitles": True}


def configuration(options):
    config = {**DEFAULTS, **options}
    if config["mode"] not in {"ocr", "ai", "manual"} or config["tts"] not in {"windows", "edge"}:
        raise ValueError("未知文案或配音模式")
    if config["preset"] not in PRESETS or config["fps"] not in {24, 30}:
        raise ValueError("不支持的视频尺寸或帧率")
    if not isinstance(config["rate"], int) or not -3 <= config["rate"] <= 5:
        raise ValueError("语速必须在 -3 到 5 之间")
    config["title"] = str(config["title"]).strip()[:100] or DEFAULTS["title"]
    config["vision_model"] = os.environ.get("COMICFLOW_VISION_MODEL", "")
    return config


def run_pipeline(folder, event=None, callback=None):
    with JobLock(folder):
        return _run_pipeline(folder, event, callback)


def _run_pipeline(folder, event=None, callback=None):
    folder = Path(folder).resolve()
    config = configuration(read_json(folder / "config.json"))
    ctx = Context(folder, event, callback)
    # Use a frozen import on resume, avoiding changes to the user's source files.
    if (folder / "pages.json").exists():
        pages = read_json(folder / "pages.json")
    else:
        pages = import_source(config["source"], folder, ctx)
    if (folder / "panels.json").exists():
        panels = read_json(folder / "panels.json")
        if any(p["forced_cut"] for p in panels):
            ctx.warn("部分长图没有明显留白，已使用连续视窗切分；原图全部保留。")
    else:
        panels = make_panels(pages, folder, ctx)
    ocr = recognize(panels, folder, ctx) if config["mode"] != "manual" else {}
    plan = make_plan(panels, ocr, folder, config, ctx)
    timeline = render_video(plan, panels, folder, config, ctx)
    report = audit(folder, timeline, panels, config, ctx)
    review_html(folder, config, panels, timeline, report)
    export = EXPORTS / folder.name
    export.mkdir(parents=True, exist_ok=True)
    for name in ("final.mp4", "captions.srt", "plan.json", "timeline.json", "audit.json", "narration.flac"):
        shutil.copy2(folder / name, export / name)
    # Keep the review beside its images and audio; export the complete review as a link in the UI.
    (export / "打开审片页.url").write_text("[InternetShortcut]\nURL=" + (folder / "review.html").as_uri() + "\n", encoding="utf-8")
    ctx.progress(100, "已完成 · 成片通过技术自审")
    return {"export": str(export), "report": report}
