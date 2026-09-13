from __future__ import annotations

import html
import math
import re
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .common import digest, executable, probe, read_json, write_json
from .motion import MOTION_VERSION, encode_camera

PRESETS = {"landscape": (1280, 720), "classic": (1024, 768), "portrait": (720, 1280), "preview": (768, 576)}


def caption_chunks(text, limit=19):
    parts = re.findall(r"[^。！？!?；;，,]+[。！？!?；;，,]?", text)
    chunks = []
    for part in parts:
        part = part.strip()
        while len(part) > limit:
            chunks.append(part[:limit])
            part = part[limit:]
        if part:
            chunks.append(part)
    return chunks


def stamp(seconds, ass=False):
    units = int(round(seconds * (100 if ass else 1000)))
    base = 100 if ass else 1000
    hours, rem = divmod(units, 3600 * base)
    minutes, rem = divmod(rem, 60 * base)
    secs, frac = divmod(rem, base)
    return f"{hours}:{minutes:02d}:{secs:02d}.{frac:02d}" if ass else f"{hours:02d}:{minutes:02d}:{secs:02d},{frac:03d}"


def ass_escape(text):
    return text.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ").replace("\r", " ")


def write_captions(timeline, folder, config):
    w, h = PRESETS[config["preset"]]
    font = round(w * .04) if config["preset"] == "portrait" else round(h * .047)
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\n"
              "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Default,Microsoft YaHei,{font},&H00FFFFFF,&H000000FF,&H00101010,&H88000000,-1,0,0,0,100,100,0,0,1,2.4,1,2,28,28,{int(h*.045)},1\n"
              "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ass, srt, captions = [header], [], []
    for entry in timeline:
        chunks = caption_chunks(entry["text"], 16 if config["preset"] == "portrait" else 23)
        weights = [max(1, len(c.strip("，。！？!?；;"))) for c in chunks]
        total = sum(weights) or 1
        cursor = entry["start"]
        available = max(.01, entry["duration"] - .13)
        for chunk, weight in zip(chunks, weights):
            end = cursor + available * weight / total
            index = len(captions) + 1
            captions.append({"start": round(cursor, 3), "end": round(end, 3), "text": chunk})
            srt.append(f"{index}\n{stamp(cursor)} --> {stamp(end)}\n{chunk}\n")
            ass.append(f"Dialogue: 0,{stamp(cursor, True)},{stamp(end, True)},Default,,0,0,0,,{ass_escape(chunk)}\n")
            cursor = end
    (folder / "captions.ass").write_text("".join(ass), encoding="utf-8-sig")
    (folder / "captions.srt").write_text("\n".join(srt), encoding="utf-8-sig")
    return captions


def composition(panel, target, size):
    w, h = size
    with Image.open(panel) as raw:
        raw = raw.convert("RGB")
        background = ImageOps.fit(raw, size).filter(ImageFilter.GaussianBlur(w / 35))
        background = ImageEnhance.Brightness(background).enhance(.35)
        # Keep a safe border for the slow push-in and an unobstructed subtitle area.
        scale = min(w * .975 / raw.width, h * .87 / raw.height)
        raw = raw.resize((round(raw.width * scale), round(raw.height * scale)), Image.Resampling.LANCZOS)
        background.paste(raw, ((w - raw.width) // 2, max(8, (int(h * .9) - raw.height) // 2)))
        background.save(target, quality=95)


def concat_file(paths, target):
    # Our generated basenames are controlled; cwd is the job directory, avoiding drive / apostrophe escaping.
    target.write_text("\n".join("file '" + p + "'" for p in paths) + "\n", encoding="utf-8")


def render_video(plan, panels, folder, config, ctx):
    from .speech import synthesize, audio_target
    from .speech_batch import prepare_batch
    by_id = {p["id"]: p for p in panels}
    scenes = plan["scenes"]
    timeline, clips, audios = [], [], []
    size = PRESETS[config["preset"]]
    fps = config["fps"]
    work = folder / "clips"
    work.mkdir(exist_ok=True)
    frames_cursor = 0
    for i, scene in enumerate(scenes):
        ctx.progress(55 + 34 * i / len(scenes), f"配音与剪辑 {i + 1}/{len(scenes)}")
        if scene['text'] and not audio_target(scene, folder, config).exists():
            prepare_batch(scenes[i:], folder, config, ctx)
        audio, seconds = synthesize(scene, folder, config, ctx)
        frames = round(seconds * fps)
        ids = scene["panel_ids"]
        if frames < len(ids):
            raise ValueError("自备音频过短，无法完整展示对应画面")
        entry = {**scene, "start": frames_cursor / fps, "duration": frames / fps,
                 "end": (frames_cursor + frames) / fps, "audio": audio.relative_to(folder).as_posix(), "shots": []}
        for j, pid in enumerate(ids):
            count = frames // len(ids) + (1 if j < frames % len(ids) else 0)
            panel = by_id[pid]
            key = digest([panel, count, config["preset"], fps, config["motion"], "render-v4", MOTION_VERSION])
            clip = work / f"{key}.mp4"
            frame = work / f"{key}.jpg"
            if not clip.exists():
                composition(folder / panel["file"], frame, size)
                temp = clip.with_suffix(".part.mp4")
                if config["motion"]:
                    encode_camera(frame, temp, count, fps, ctx)
                else:
                    ctx.run([executable("ffmpeg"), "-v", "error", "-y", "-loop", "1", "-i", frame,
                             "-vf", f"fps={fps}", "-frames:v", count, "-an", "-c:v", "libx264", "-preset", "veryfast",
                             "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "2", temp], timeout=900)
                clip_info = probe(temp)
                if int(clip_info["streams"][0]["nb_frames"]) != count:
                    raise RuntimeError("分镜帧数校验失败")
                temp.replace(clip)
            clips.append(clip.relative_to(folder).as_posix())
            entry["shots"].append({"panel_id": pid, "start": frames_cursor / fps,
                                   "end": (frames_cursor + count) / fps, "source": panel["source"], "box": panel["box"]})
            frames_cursor += count
        timeline.append(entry)
        audios.append(audio.relative_to(folder).as_posix())
    write_json(folder / "timeline.json", timeline)
    captions = write_captions(timeline, folder, config)
    write_json(folder / "captions.json", captions)
    concat_file(clips, folder / "video-concat.txt")
    concat_file(audios, folder / "audio-concat.txt")
    ctx.progress(90, "合成连续音轨与合集画面")
    ctx.run([executable("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "audio-concat.txt",
             "-c:a", "flac", "narration.flac"], timeout=3600, cwd=folder)
    args = [executable("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "video-concat.txt",
            "-i", "narration.flac", "-map", "0:v:0", "-map", "1:a:0"]
    if config["subtitles"] and captions:
        args += ["-vf", "ass=captions.ass", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2"]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "final.part.mp4"]
    ctx.run(args, timeout=max(3600, frames_cursor / fps * 8), cwd=folder)
    (folder / "final.part.mp4").replace(folder / "final.mp4")
    return timeline


def audit(folder, timeline, panels, config, ctx):
    ctx.progress(97, "自审：完整解码、画面覆盖、音画时长与字幕边界")
    path = folder / "final.mp4"
    info = probe(path)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    expected = timeline[-1]["end"]
    actual = float(info["format"]["duration"])
    checks = {"video_and_audio_present": len(videos) == 1 and len(audios) == 1,
              "all_panels_in_order": [pid for s in timeline for pid in s["panel_ids"]] == [p["id"] for p in panels],
              "duration_matches_plan": abs(expected - actual) < .12,
              "timeline_contiguous": all(abs(a["end"] - b["start"]) < .0001 for a, b in zip(timeline, timeline[1:])),
              "resolution_matches": (videos[0]["width"], videos[0]["height"]) == PRESETS[config["preset"]] if videos else False}
    if videos and audios:
        checks["audio_video_drift_under_100ms"] = abs(float(videos[0]["duration"]) - float(audios[0]["duration"])) < .1
    captions = read_json(folder / "captions.json")
    checks["subtitle_bounds"] = all(0 <= c["start"] < c["end"] <= expected + .001 for c in captions)
    checks["subtitles_in_order"] = all(a["end"] <= b["start"] + .002 for a, b in zip(captions, captions[1:]))
    ctx.run([executable("ffmpeg"), "-v", "error", "-xerror", "-i", path, "-f", "null", "-"], timeout=max(600, expected * 3))
    checks["full_decode"] = True
    report = {"passed": all(checks.values()), "checks": checks, "duration": actual,
              "planned_duration": expected, "panels": len(panels), "scenes": len(timeline),
              "mode": config["mode"], "warnings": ctx.warnings,
              "motion_renderer": MOTION_VERSION if config["motion"] else "static",
              "voice": config["voice"], "voice_rate": config["rate"], "tts": config["tts"],
              "scope": "技术自审验证文件、时间轴与素材覆盖；不代表剧情语义或识别文字已人工审定。",
              "subtitle_timing": "分段字幕按字数在实测配音时长内分配，非逐字强制对齐"}
    write_json(folder / "audit.json", report)
    if not report["passed"]:
        raise RuntimeError("成片技术自审未通过：" + ", ".join(k for k, v in checks.items() if not v))
    return report


def review_html(folder, config, panels, timeline, report):
    by_id = {p["id"]: p for p in panels}
    rows = []
    for entry in timeline:
        images = "".join(f'<figure><img loading="lazy" src="{by_id[p]["file"]}"><figcaption>{p} · {html.escape(by_id[p]["source"])}</figcaption></figure>' for p in entry["panel_ids"])
        rows.append(f'<article><div class="frames">{images}</div><div><small>{entry["start"]:.2f}–{entry["end"]:.2f} 秒</small><p>{html.escape(entry["text"]) or "（无台词停留）"}</p><audio controls preload="none" src="{entry["audio"]}"></audio></div></article>')
    warnings = "".join(f"<li>{html.escape(w)}</li>" for w in report["warnings"])
    title = html.escape(config["title"])
    document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · 审片</title><style>body{{background:#0c1016;color:#e2e7ef;font:16px/1.7 system-ui;max-width:1080px;margin:40px auto;padding:0 24px}}h1{{font-size:30px}}small{{color:#9aa9bc}}article{{display:grid;grid-template-columns:45% 1fr;gap:24px;border-top:1px solid #2d3744;padding:24px 0}}img{{max-width:100%;max-height:330px;object-fit:contain}}figure{{margin:0}}figcaption{{font-size:12px;color:#95a1b4}}.frames{{display:flex;gap:8px}}video{{width:100%;max-height:550px;background:black}}audio{{width:100%}}li{{color:#efbc72}}a{{color:#9cdcae}}@media(max-width:700px){{article{{display:block}}}}</style><h1>{title}</h1><p>技术自审通过 · {report['panels']} 个分镜 · {report['duration']:.1f} 秒</p><p>{html.escape(report['scope'])}</p><video controls preload="metadata" src="final.mp4"></video><p><a href="captions.srt" download>下载字幕</a> · <a href="timeline.json" download>下载时间轴</a> · <a href="audit.json" download>下载自审报告</a></p><details><summary>制作说明与需留意的内容（{len(report['warnings'])}）</summary><ul>{warnings}</ul></details><h2>画面与解说逐段对照</h2>{''.join(rows)}</html>'''
    (folder / "review.html").write_text(document, encoding="utf-8")
