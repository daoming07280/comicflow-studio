"""Bounded speech batches, split only at verified scene boundaries."""
from __future__ import annotations

import asyncio
import time

from . import edge_speech as edge
from .common import Cancelled, digest, duration, executable, file_hash, read_json, write_json
from .speech import audio_target, synthesize


def scene_ranges(texts, boundaries, seconds):
    expected = [edge.spoken_chars(t) for t in texts]
    if not all(expected) or not boundaries:
        raise ValueError('批量配音缺少文字边界')
    if ''.join(expected) != edge.spoken_chars(''.join(b['text'] for b in boundaries)):
        raise ValueError('批量配音文字不完整')
    ends, cursor, char_count = [], 0, 0
    for text in expected:
        char_count += len(text)
        ends.append(char_count)
    cuts, scene_index, consumed, previous_end = [0.0], 0, 0, 0.0
    for index, boundary in enumerate(boundaries):
        start = boundary['offset'] / 10_000_000
        end = start + boundary['duration'] / 10_000_000
        if start < previous_end - .001 or end <= start or end > seconds + .12:
            raise ValueError('批量配音时间边界无效')
        previous_end = end
        consumed += len(edge.spoken_chars(boundary['text']))
        if consumed > ends[scene_index]:
            raise ValueError('服务将两个分镜合为一个边界，不能可靠切分')
        if consumed == ends[scene_index]:
            scene_index += 1
            if scene_index < len(texts):
                if index + 1 >= len(boundaries):
                    raise ValueError('缺少下一段配音边界')
                next_start = boundaries[index + 1]['offset'] / 10_000_000
                if next_start < end - .001:
                    raise ValueError('分镜配音边界重叠')
                cuts.append((end + next_start) / 2)
    if scene_index != len(texts):
        raise ValueError('缺少分镜配音')
    cuts.append(seconds)
    if any(b <= a for a, b in zip(cuts, cuts[1:])):
        raise ValueError('批量配音时长无效')
    return list(zip(cuts, cuts[1:]))


def candidates(scenes, folder, config):
    selected, size = [], 0
    for scene in scenes[:64]:
        if scene.get('imported_audio') or not edge.spoken_chars(scene['text']):
            continue
        if audio_target(scene, folder, config).exists():
            continue
        length = len(scene['text']) + 1
        if length > 350 or size + length > 350:
            break
        selected.append(scene)
        size += length
        if len(selected) == 8:
            break
    return selected


def prepare_batch(scenes, folder, config, ctx):
    if config['tts'] != 'edge':
        return
    selected = candidates(scenes, folder, config)
    if len(selected) < 2:
        return
    texts = [s['text'].rstrip('。！？!? \n') + '。' for s in selected]
    key = digest([texts, config['voice'], config['rate'], 'scene-batch-word-v1'])
    directory = folder / 'audio' / 'batches'
    directory.mkdir(parents=True, exist_ok=True)
    target, marker = directory / f'{key}.mp3', directory / f'{key}.json'
    failed = directory / f'{key}.fallback.json'
    if failed.exists():
        return
    ctx.callback(message=f'批量生成配音 · 一次处理 {len(selected)} 段')
    metadata = None
    if target.exists() and marker.exists():
        saved = read_json(marker)
        if saved.get('sha256') == file_hash(target):
            metadata = saved
    if metadata is None:
        temporary = target.with_suffix('.part.mp3')
        with edge.service_slot(ctx) as (_, state_file):
            try:
                metadata = asyncio.run(edge.cancellable_stream(''.join(texts), {**config, 'edge_boundary': 'WordBoundary'}, temporary, ctx))
                seconds = edge.verified_decode(temporary, ctx)
                ranges = scene_ranges(texts, metadata['boundaries'], seconds)
                temporary.replace(target)
                metadata.update(sha256=file_hash(target), duration=seconds, ranges=ranges)
                write_json(marker, metadata)
                write_json(state_file, {'next_request': time.time() + edge.REQUEST_GAP})
            except Cancelled:
                write_json(state_file, {'next_request': time.time() + edge.REQUEST_GAP})
                raise
            except Exception as exc:
                write_json(state_file, {'next_request': time.time() + edge.retry_delay(exc, 0)})
                write_json(failed, {'reason': str(exc), 'type': type(exc).__name__, 'metadata': metadata})
                ctx.callback(message='批量配音未通过完整性检查，改为逐段处理')
                return
            finally:
                temporary.unlink(missing_ok=True)
    ranges = scene_ranges(texts, metadata['boundaries'], metadata['duration'])
    for index, (scene, (start, end)) in enumerate(zip(selected, ranges)):
        ctx.check()
        raw = directory / f'{key}-{index}.wav'
        ctx.run([executable('ffmpeg'), '-v', 'error', '-y', '-i', target,
                 '-af', f'atrim=start={start:.7f}:end={end:.7f},asetpts=PTS-STARTPTS',
                 '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', raw])
        if abs(duration(raw) - (end - start)) > .025:
            raise RuntimeError('批量配音切分时长不符，未发布该片段')
        synthesize(scene, folder, config, ctx, prepared_audio=raw)
    with (directory / 'completed.log').open('a', encoding='utf-8') as log:
        log.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {key} scenes={len(selected)}\n')
