from __future__ import annotations

import math
import os
from pathlib import Path

from .common import ROOT, digest, duration, executable, write_json, read_json, file_hash
from .edge_speech import spoken_chars, synthesize_edge, verified_decode

DEFAULT_VOICE = "zh-CN-YunyangNeural"
VOICES = {DEFAULT_VOICE: "云扬 · 沉稳男声", "zh-CN-YunxiNeural": "云希 · 明亮男声",
          "zh-CN-YunjianNeural": "云健 · 热血男声", "zh-CN-XiaoxiaoNeural": "晓晓 · 清晰女声"}


def audio_target(scene, folder, config):
    key = digest([scene, config["voice"], config["rate"], config["tts"], config["fps"], "v4-exact-samples"])
    return folder / "audio" / f"{key}.wav"


def synthesize(scene, folder, config, ctx, *, prepared_audio=None):
    audio_dir = folder / "audio"
    audio_dir.mkdir(exist_ok=True)
    target = audio_target(scene, folder, config)
    key = target.stem
    text = scene["text"]
    engine = config["tts"]
    if target.exists():
        marker = target.with_suffix('.checked.json')
        try:
            if marker.exists() and read_json(marker).get('sha256') == file_hash(target):
                return target, duration(target)
            seconds = verified_decode(target, ctx)
            write_json(marker, {'sha256': file_hash(target), 'duration': seconds, 'legacy_decode_checked': True})
            return target, seconds
        except (RuntimeError, ValueError, OSError):
            target.replace(target.with_suffix('.invalid.wav'))
    raw = audio_dir / f"{key}-raw.wav"
    if prepared_audio is not None:
        raw = prepared_audio
    elif scene.get("imported_audio"):
        raw = folder / scene["imported_audio"]
    elif not spoken_chars(text):
        ctx.run([executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                 "anullsrc=r=48000:cl=mono", "-t", "2.4", raw])
    elif engine == "windows":
        if os.name != "nt":
            raise ValueError("离线中文语音目前需要 Windows；其他系统请选择 Edge 配音。")
        spec = audio_dir / f"{key}.json"
        write_json(spec, {"text": text, "rate": config["rate"], "output": str(raw.resolve())})
        ctx.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", ROOT / "scripts" / "speak.ps1", "-InputJson", spec.resolve()], timeout=180)
    elif engine == "edge":
        raw = synthesize_edge(text, audio_dir, config, ctx)
    else:
        raise ValueError("未知配音方式")
    seconds = duration(raw)
    # Every scene ends at a frame boundary; no progressive audio/video drift.
    total = math.ceil((seconds + (.16 if text else 0)) * config["fps"]) / config["fps"]
    temp = target.with_suffix(".part.wav")
    samples = round(total * 48000)
    filters = (f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,asetpts=N/SR/TB,"
               f"apad=whole_len={samples},atrim=end_sample={samples},asetpts=N/SR/TB")
    ctx.run([executable("ffmpeg"), "-v", "error", "-y", "-i", raw, "-af", filters,
             "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", temp])
    if abs(duration(temp) - total) > 1 / 48000:
        raise RuntimeError("配音样本数校验失败，未缓存不完整音频。")
    temp.replace(target)
    write_json(target.with_suffix('.checked.json'), {'sha256': file_hash(target), 'duration': total})
    return target, total
