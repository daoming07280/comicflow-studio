from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
JOBS = DATA / "jobs"
EXPORTS = ROOT / "outputs"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


class Cancelled(Exception):
    pass


class JobLock:
    """OS-owned locks are released even if the renderer or app crashes."""
    def __init__(self, folder):
        self.path = Path(folder) / "running.lock"
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.handle.close()
            raise RuntimeError("该任务已经在另一处运行，请勿重复启动。") from e
        return self

    def __exit__(self, *args):
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def natural_key(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", str(value))]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def within(base, relative):
    base = Path(base).resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ValueError("文件路径超出了项目目录")
    return target


def executable(name):
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"找不到 {name}，请先安装 FFmpeg 并加入 PATH。")
    return found


class Context:
    def __init__(self, folder, event=None, callback=None):
        self.folder = Path(folder)
        self.event = event or threading.Event()
        self.callback = callback or (lambda **kw: None)
        self.warnings = []

    def check(self):
        if self.event.is_set():
            raise Cancelled("任务已停止；已完成步骤会保留，支持继续。")

    def progress(self, percent, message):
        self.check()
        self.callback(progress=round(percent, 1), message=message)

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def run(self, args, timeout=600, cwd=None):
        self.check()
        log_dir = self.folder / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "process.log"
        with log.open("ab") as out:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            p = subprocess.Popen([str(a) for a in args], stdout=out, stderr=out,
                                 cwd=cwd, creationflags=flags)
            started = time.monotonic()
            try:
                while p.poll() is None:
                    self.check()
                    if time.monotonic() - started > timeout:
                        raise RuntimeError(f"处理超时（{timeout} 秒），请查看任务日志。")
                    time.sleep(0.15)
            except BaseException:
                p.kill()
                p.wait()
                raise
        if p.returncode:
            tail = log.read_bytes()[-2500:].decode("utf-8", errors="replace")
            raise RuntimeError(f"处理失败：{Path(args[0]).name}\n{tail}")

    def run_input(self, args, chunks, timeout=600):
        """Stream generated frames without writing a frame sequence to disk."""
        self.check()
        log_dir = self.folder / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "process.log"
        finished = threading.Event()
        timed_out = threading.Event()
        with log.open("ab") as out:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            p = subprocess.Popen([str(a) for a in args], stdin=subprocess.PIPE,
                                 stdout=out, stderr=out, creationflags=flags)
            started = time.monotonic()

            def watch():
                # A blocked pipe write must also respond to stop / timeout.
                while not finished.wait(.1):
                    expired = time.monotonic() - started > timeout
                    if expired:
                        timed_out.set()
                    if expired or self.event.is_set():
                        if p.poll() is None:
                            p.kill()
                        return

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                for chunk in chunks:
                    self.check()
                    p.stdin.write(chunk)
                p.stdin.close()
                p.wait()
                self.check()
            except (BrokenPipeError, OSError):
                self.check()
                if not timed_out.is_set() and p.poll() is None:
                    raise
            finally:
                finished.set()
                watcher.join(timeout=1)
                if p.poll() is None:
                    p.kill()
                p.wait()
                try:
                    p.stdin.close()
                except OSError:
                    pass
        if timed_out.is_set():
            raise RuntimeError(f"镜头渲染超时（{timeout} 秒），已停止编码。")
        if p.returncode:
            tail = log.read_bytes()[-2500:].decode("utf-8", errors="replace")
            raise RuntimeError(f"镜头渲染失败：\n{tail}")


def probe(path):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.run([executable("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(path)], capture_output=True, timeout=30, creationflags=flags)
    if p.returncode:
        raise ValueError(f"无法读取媒体：{Path(path).name}")
    return json.loads(p.stdout)


def duration(path):
    seconds = float(probe(path)["format"]["duration"])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("音频时长必须大于零")
    return seconds
