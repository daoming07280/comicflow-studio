"""Paced Edge speech with cancellable backoff and verified, atomic chunk caches."""
from __future__ import annotations

import asyncio
import math
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

from .common import DATA, Cancelled, JobLock, digest, duration, executable, file_hash, read_json, write_json

SERVICE_DIR = DATA / "speech-service"
REQUEST_GAP = 5.0
RETRY_DELAYS = (15, 30, 60, 120, 240)
_mutex = threading.Lock()


class IncompleteSpeech(RuntimeError):
    pass


def spoken_chars(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def split_text(text, limit=160):
    # Keep punctuation/spacing in the request and never exceed one service text packet.
    parts, current = [], ''
    for sentence in re.findall(r'[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+', text):
        while sentence:
            room = limit - len(current)
            if len(sentence) > room and current:
                parts.append(current); current = ''; continue
            current += sentence[:room]; sentence = sentence[room:]
            if len(current) == limit:
                parts.append(current); current = ''
    if current:
        parts.append(current)
    return [p for p in parts if spoken_chars(p)]


def wait_for_service(ctx, seconds, reason):
    deadline = time.monotonic() + seconds
    while True:
        ctx.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        ctx.callback(message=f'{reason} · 约 {math.ceil(remaining)} 秒后自动继续（可停止）')
        if ctx.event.wait(min(1, remaining)):
            ctx.check()


@contextmanager
def service_slot(ctx):
    while not _mutex.acquire(timeout=.2):
        ctx.check()
    lock = None
    try:
        SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        while lock is None:
            ctx.check()
            candidate = JobLock(SERVICE_DIR)
            try:
                candidate.__enter__()
                lock = candidate
            except RuntimeError:
                wait_for_service(ctx, 1, '等待其他配音请求完成')
        state_file = SERVICE_DIR / 'pacing.json'
        state = read_json(state_file) if state_file.exists() else {}
        wait_for_service(ctx, max(0, state.get('next_request', 0) - time.time()), '配音服务冷却中')
        yield state, state_file
    finally:
        if lock:
            lock.__exit__(None, None, None)
        _mutex.release()


def retry_delay(exc, attempt, *, text_repair=False):
    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)-1)]
    # One bounded compatibility probe; repeated failures still back off normally.
    # Empty audio alone is not evidence of HTTP throttling.
    if text_repair and attempt == 0 and type(exc).__name__ == 'NoAudioReceived' and not getattr(exc, 'status', None):
        delay = REQUEST_GAP
    header = (getattr(exc, 'headers', None) or {}).get('Retry-After')
    if header:
        try:
            delay = max(delay, float(header))
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(header).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    return delay


async def stream_to_file(text, voice, rate, path, *, boundary='SentenceBoundary'):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=f'{rate * 10:+d}%', boundary=boundary, connect_timeout=10, receive_timeout=30)
    boundaries, size = [], 0
    with path.open('wb') as output:
        async for message in communicate.stream():
            if message['type'] == 'audio':
                size += len(message['data'])
                output.write(message['data'])
            elif message['type'] in {'SentenceBoundary', 'WordBoundary'}:
                boundaries.append({k: message[k] for k in ('text', 'offset', 'duration')})
    if not size or not boundaries:
        raise IncompleteSpeech('服务未返回完整音频与文本边界')
    received = spoken_chars(''.join(b['text'] for b in boundaries))
    if received != spoken_chars(text):
        raise IncompleteSpeech('服务返回的朗读内容不完整')
    return {'boundaries': boundaries, 'end': max(b['offset'] + b['duration'] for b in boundaries) / 10_000_000}


async def cancellable_stream(text, config, path, ctx):
    options = {'boundary': config['edge_boundary']} if config.get('edge_boundary') else {}
    task = asyncio.create_task(stream_to_file(text, config['voice'], config['rate'], path, **options))
    deadline = time.monotonic() + 120
    try:
        while not task.done():
            ctx.check()
            if time.monotonic() >= deadline:
                raise TimeoutError('配音请求超时')
            await asyncio.wait({task}, timeout=.2)
        ctx.check()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def verified_decode(path, ctx):
    ctx.run([executable('ffmpeg'), '-v', 'error', '-xerror', '-err_detect', 'explode', '-i', path, '-f', 'null', '-'])
    seconds = duration(path)
    if seconds <= 0:
        raise IncompleteSpeech('音频时长无效')
    return seconds


def speech_chunk(text, directory, config, ctx):
    key = digest([text, config['voice'], config['rate'], 'edge-verified-v1'])
    target = directory / (key + '.mp3')
    marker = target.with_suffix('.json')
    clauses = [p.strip() for p in re.split(r'[。！？!?；;，,\n]+', text) if spoken_chars(p)]
    if marker.exists():
        try:
            saved = read_json(marker)
            cached = target.with_suffix('.split.wav') if saved.get('split') else target
            if cached.exists() and saved.get('sha256') == file_hash(cached):
                return cached
            if saved.get('split_pending') and len(clauses) > 1:
                return split_recovery(text, clauses, directory, config, ctx, target, marker)
        except (KeyError, ValueError, OSError):
            pass
    temporary = target.with_suffix('.part.mp3')
    alternate = False
    for attempt in range(len(RETRY_DELAYS) + 1):
        ctx.check()
        split_requested = False
        request_text = (text.replace('。', '') if attempt >= 2 else re.sub(r'。+', '，', text).rstrip('，')) if alternate else text
        with service_slot(ctx) as (state, state_file):
            ctx.callback(message=f'正在生成配音片段 · 第 {attempt+1} 次尝试')
            try:
                metadata = asyncio.run(cancellable_stream(request_text, config, temporary, ctx))
                seconds = verified_decode(temporary, ctx)
                if seconds + .12 < metadata['end']:
                    raise IncompleteSpeech('音频在最后一句朗读完成前中断')
                temporary.replace(target)
                write_json(marker, {**metadata, 'sha256': file_hash(target), 'duration': seconds,
                                    'punctuation_adjusted': alternate, 'voice': config['voice']})
                write_json(state_file, {'next_request': time.time() + REQUEST_GAP})
                return target
            except Cancelled:
                write_json(state_file, {'next_request': time.time() + REQUEST_GAP})
                raise
            except Exception as exc:
                # Certain otherwise valid Chinese sentences reproducibly return no audio
                # with a full stop. Preserve every spoken character, adjusting pauses only.
                if type(exc).__name__ == 'NoAudioReceived':
                    alternate = True
                    split_requested = attempt >= 2 and len(clauses) > 1
                repaired = re.sub(r'。+', '，', text).rstrip('，')
                delay = retry_delay(exc, attempt, text_repair=repaired != request_text)
                write_json(state_file, {'next_request': time.time() + delay})
                log = directory / 'retries.log'
                with log.open('a', encoding='utf-8') as output:
                    output.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {key} attempt={attempt+1} {type(exc).__name__}: {str(exc)[:200]} cooldown={delay}\n')
                if attempt == len(RETRY_DELAYS) and not split_requested:
                    raise RuntimeError('在线配音经过限速和退避重试仍不可用。已完成音频和画面均已保留；稍后点击“继续制作”即可从断点恢复，不会导出缺音成片。') from exc
            finally:
                # This attempt's uncommitted file must never be reused as complete audio.
                temporary.unlink(missing_ok=True)
        # Release the shared request lock before recursively synthesizing short clauses.
        if split_requested:
            write_json(marker, {'split_pending': True})
            return split_recovery(text, clauses, directory, config, ctx, target, marker)
    raise AssertionError('unreachable')


def split_recovery(text, clauses, directory, config, ctx, target, marker):
    if spoken_chars(''.join(clauses)) != spoken_chars(text):
        raise IncompleteSpeech('短句拆分未覆盖全部原文')
    paths = [speech_chunk(part, directory, config, ctx) for part in clauses]
    listing = target.with_suffix('.split.txt')
    listing.write_text('\n'.join(f"file '{p.name}'" for p in paths) + '\n', encoding='utf-8')
    combined = target.with_suffix('.split.wav')
    temporary = target.with_suffix('.split.part.wav')
    ctx.run([executable('ffmpeg'), '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', listing.name,
             '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', temporary.name], cwd=directory)
    seconds = verified_decode(temporary, ctx)
    if seconds + .12 < sum(duration(p) for p in paths):
        raise IncompleteSpeech('短句拼接后的配音不完整')
    temporary.replace(combined)
    write_json(marker, {'split': True, 'sha256': file_hash(combined), 'duration': seconds,
                        'voice': config['voice'], 'clauses': len(clauses)})
    return combined


def synthesize_edge(text, audio_dir, config, ctx):
    directory = audio_dir / 'edge-chunks'
    directory.mkdir(exist_ok=True)
    chunks = split_text(text)
    paths = [speech_chunk(part, directory, config, ctx) for part in chunks]
    if not paths:
        raise IncompleteSpeech('没有可朗读文字')
    if len(paths) == 1:
        return paths[0]
    key = digest([text, config['voice'], config['rate'], 'joined-edge-v1'])
    listing = directory / (key + '.txt')
    listing.write_text('\n'.join(f"file '{p.name}'" for p in paths) + '\n', encoding='utf-8')
    combined = directory / (key + '.wav')
    ctx.run([executable('ffmpeg'), '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', listing.name,
             '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', combined.name], cwd=directory)
    verified_decode(combined, ctx)
    return combined
