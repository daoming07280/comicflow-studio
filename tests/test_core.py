import io
import json
import threading
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from comicflow.common import Cancelled, Context, read_json, within, write_json
from comicflow.importers import extract_zip, import_source
from comicflow.narration import make_plan, validate_scenes
from comicflow.panels import make_panels, split_ranges
from comicflow.pipeline import configuration
from comicflow.render import audit, caption_chunks, render_video
from comicflow.speech import synthesize


def wav(path, seconds, rate=44100):
    x = (np.sin(np.arange(int(seconds * rate)) * 2 * np.pi * 220 / rate) * 6000).astype('<i2')
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(x.tobytes())


@pytest.mark.parametrize('name', ['../escape.png', 'C:/escape.png', '/escape.png', '..\\escape.png'])
def test_zip_validates_all_paths_before_extracting(tmp_path, name):
    archive = tmp_path / 'bad.cbz'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('valid.png', b'ok')
        z.writestr(name, b'bad')
    dest = tmp_path / 'out'
    with pytest.raises(ValueError):
        extract_zip(archive, dest)
    assert not dest.exists()


def test_archive_symlink_rejected(tmp_path):
    archive = tmp_path / 'bad.zip'
    item = zipfile.ZipInfo('link.png')
    item.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(item, '../secret')
    with pytest.raises(ValueError, match='符号链接'):
        extract_zip(archive, tmp_path / 'out')


def test_batch_chapter_archives_are_imported_in_order(tmp_path):
    src = tmp_path / 'comics'
    src.mkdir()
    for chapter in [10, 2]:
        data = io.BytesIO()
        Image.new('RGB', (100, 80), 'white').save(data, format='PNG')
        with zipfile.ZipFile(src / f'第{chapter}章.cbz', 'w') as z:
            z.writestr('001.png', data.getvalue())
    pages = import_source(src, tmp_path / 'job', Context(tmp_path / 'job'))
    assert [p['source'] for p in pages] == ['第2章.cbz/001.png', '第10章.cbz/001.png']


def test_import_nested_chapters_natural_order_duplicates_and_transparency(tmp_path):
    src, job = tmp_path / '漫画', tmp_path / 'job'
    for chapter, page in [('第10章', '10.png'), ('第2章', '10.png'), ('第2章', '2.png')]:
        folder = src / chapter
        folder.mkdir(parents=True, exist_ok=True)
        Image.new('RGBA', (120, 100), (0, 0, 0, 0)).save(folder / page)
    ctx = Context(job)
    pages = import_source(src, job, ctx)
    assert [p['source'] for p in pages] == ['第2章/2.png', '第2章/10.png', '第10章/10.png']
    assert len(pages) == 3 and len(ctx.warnings) == 2
    assert Image.open(job / pages[0]['file']).getpixel((50, 50)) == (255, 255, 255)


def test_gutter_split_covers_every_source_pixel():
    image = Image.new('RGB', (500, 2600), '#295276')
    d = ImageDraw.Draw(image)
    d.rectangle((0, 890, 500, 950), fill='white')
    ranges = split_ranges(image)
    assert ranges[0][1] == 920
    assert ranges[0][2] is False
    assert ranges[0][0] == 0 and ranges[-1][1] == image.height
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
    assert all(a < b for a, b, _ in ranges)


def test_continuous_action_panel_never_drops_pixels():
    ranges = split_ranges(Image.new('RGB', (400, 3000), '#39567a'))
    assert sum(b - a for a, b, _ in ranges) == 3000
    assert any(forced for _, _, forced in ranges)


@pytest.mark.parametrize('ids', [['p00002', 'p00001'], ['p00001'], ['p00001', 'p00001']])
def test_narration_rejects_wrong_picture_mapping(tmp_path, ids):
    with pytest.raises(ValueError, match='完整覆盖'):
        validate_scenes([{'panel_ids': ids, 'text': '测试'}], [{'id': 'p00001'}, {'id': 'p00002'}], tmp_path)


def test_manual_audio_cannot_escape_source(tmp_path):
    with pytest.raises(ValueError, match='超出'):
        validate_scenes([{'panel_ids': ['p00001'], 'text': '测试', 'audio': '../secret.wav'}], [{'id': 'p00001'}], tmp_path, manual=True)


def test_empty_text_produces_explicit_silent_warning(tmp_path):
    panels = [{'id': 'p00001'}]
    ctx = Context(tmp_path)
    plan = make_plan(panels, {'p00001': {'text': ''}}, tmp_path, configuration({}), ctx)
    assert plan['scenes'][0]['text'] == ''
    assert any('无声漫画' in w for w in ctx.warnings)


def test_ai_retries_invalid_mapping_without_fabricating_success(monkeypatch, tmp_path):
    calls = []
    def invalid(*args, **kwargs):
        calls.append(1)
        return [{'panel_ids': ['invented'], 'text': 'incorrect'}]
    monkeypatch.setattr('comicflow.narration.vision_request', invalid)
    with pytest.raises(RuntimeError, match='三次'):
        make_plan([{'id': 'p00001'}], {'p00001': {'text': ''}}, tmp_path, configuration({'mode': 'ai'}), Context(tmp_path))
    assert len(calls) == 3
    assert not (tmp_path / 'plan.json').exists()


def test_ai_draft_review_and_context_preserve_order(monkeypatch, tmp_path):
    calls = []
    panels = [{'id': f'p{i:05d}'} for i in range(1, 7)]
    def generate(batch, ocr, folder, context, review=False, draft=None):
        calls.append((len(batch), review, context))
        return [{'panel_ids': [p['id']], 'text': '复核。' if review else '草稿。'} for p in batch]
    monkeypatch.setattr('comicflow.narration.vision_request', generate)
    plan = make_plan(panels, {}, tmp_path, configuration({'mode': 'ai'}), Context(tmp_path))
    assert len(calls) == 4 and calls[2][2] == '复核。' * 5
    assert all(s['text'] == '复核。' for s in plan['scenes'])
    assert [s['id'] for s in plan['scenes']] == [f's{i:05d}' for i in range(1, 7)]


def test_caption_line_breaks_and_literal_markup():
    chunks = caption_chunks('这是一段很长的文字，需要分成合适的短句。' * 4, limit=16)
    assert all(len(c) <= 16 for c in chunks)
    from comicflow.render import ass_escape, stamp
    assert '{' not in ass_escape('{\\pos(0,0)}文字')
    assert stamp(3599.9996) == '01:00:00,000'


def test_cancel_interrupts_processing(tmp_path):
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        Context(tmp_path, event).progress(50, 'processing')


@pytest.mark.parametrize('seconds', [.336, .973, 1.417])
def test_audio_is_padded_to_exact_video_frames_and_cached(tmp_path, seconds):
    wav(tmp_path / 'input.wav', seconds)
    scene = {'text': '测试声音', 'imported_audio': 'input.wav', 'panel_ids': ['p00001']}
    config = configuration({})
    target, total = synthesize(scene, tmp_path, config, Context(tmp_path))
    with wave.open(str(target), 'rb') as f:
        assert f.getframerate() == 48000
        assert f.getnframes() == round(total * 48000)
        assert f.getnframes() % 2000 == 0
    before = target.stat().st_mtime_ns
    assert synthesize(scene, tmp_path, config, Context(tmp_path))[0] == target
    assert target.stat().st_mtime_ns == before


def test_real_ffmpeg_multishot_audio_video_subtitle_pipeline(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    panels = []
    for index, color in enumerate(['#ab4420', '#229877', '#3377aa']):
        pid = f'p{index + 1:05d}'
        Image.new('RGB', (280, 210), color).save(job / f'{pid}.jpg')
        panels.append({'id': pid, 'file': f'{pid}.jpg', 'source': f'{index}.jpg', 'box': [0, 0, 280, 210]})
    wav(job / 'voice1.wav', .973)
    wav(job / 'voice2.wav', 1.417)
    scenes = validate_scenes([
        {'panel_ids': ['p00001'], 'text': '地下城。', 'audio': 'voice1.wav'},
        {'panel_ids': ['p00002', 'p00003'], 'text': '冒险开始。', 'audio': 'voice2.wav'},
    ], panels, job, manual=True)
    config = configuration({'mode': 'manual', 'preset': 'preview', 'motion': False})
    ctx = Context(job)
    timeline = render_video({'scenes': scenes}, panels, job, config, ctx)
    report = audit(job, timeline, panels, config, ctx)
    assert report['passed'] and report['checks']['audio_video_drift_under_100ms']
    assert timeline[1]['shots'][0]['end'] == timeline[1]['shots'][1]['start']
    assert (job / 'captions.srt').read_text(encoding='utf-8-sig').count('-->') == 2
