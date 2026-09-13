import io
import wave

import pytest

from comicflow import edge_speech as edge
from comicflow.common import Context, read_json
from comicflow.pipeline import configuration
from comicflow.speech import audio_target
from comicflow.speech_batch import candidates, prepare_batch, scene_ranges


def boundaries():
    return [{'text': '第一句。', 'offset': 0, 'duration': 8000000},
            {'text': '第二句。', 'offset': 12000000, 'duration': 8000000}]


def test_batch_splits_in_pause_without_losing_words():
    assert scene_ranges(['第一句', '第二句'], boundaries(), 2.4) == [(0, 1), (1, 2.4)]


@pytest.mark.parametrize('records', [
    [{'text': '第一句。第二句。', 'offset': 0, 'duration': 20000000}],
    [{'text': '第一句。', 'offset': 0, 'duration': 8000000}],
    [{'text': '第一句。', 'offset': 0, 'duration': 14000000},
     {'text': '第二句。', 'offset': 12000000, 'duration': 8000000}],
])
def test_ambiguous_missing_or_overlapping_boundaries_rejected(records):
    with pytest.raises(ValueError):
        scene_ranges(['第一句', '第二句'], records, 2.4)


def test_batch_preserves_completed_scene_and_uses_one_request(tmp_path, monkeypatch):
    config = configuration({});ctx = Context(tmp_path); calls = []
    scenes = [{'id': 'a', 'text': '第一句。'}, {'id': 'b', 'text': '第二句。'}]
    monkeypatch.setattr(edge, 'SERVICE_DIR', tmp_path / 'service')
    monkeypatch.setattr(edge, 'REQUEST_GAP', 0)
    async def stream(text, voice, rate, path, **options):
        assert options['boundary'] == 'WordBoundary'
        calls.append(text)
        with wave.open(str(path), 'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000)
            f.writeframes(b'\x01\x01' * 57600)
        return {'boundaries': boundaries(), 'end': 2}
    monkeypatch.setattr(edge, 'stream_to_file', stream)
    prepare_batch(scenes, tmp_path, config, ctx)
    assert calls == ['第一句。第二句。']
    outputs = [audio_target(s, tmp_path, config) for s in scenes]
    assert all(p.exists() and read_json(p.with_suffix('.checked.json'))['sha256'] for p in outputs)
    contents = [p.read_bytes() for p in outputs]
    prepare_batch(scenes, tmp_path, config, ctx)
    assert len(calls) == 1 and contents == [p.read_bytes() for p in outputs]
    assert candidates([{'text': ''}, {'text': 'x', 'imported_audio': 'own.wav'}], tmp_path, config) == []


def test_failed_batch_does_not_publish_audio_or_repeat(tmp_path, monkeypatch):
    config = configuration({});ctx = Context(tmp_path);calls = []
    scenes = [{'text': '第一句。'}, {'text': '第二句。'}]
    monkeypatch.setattr(edge, 'SERVICE_DIR', tmp_path / 'service')
    async def stream(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('test')
    monkeypatch.setattr(edge, 'stream_to_file', stream)
    prepare_batch(scenes, tmp_path, config, ctx)
    prepare_batch(scenes, tmp_path, config, ctx)
    assert len(calls) == 1
    assert not any(audio_target(s, tmp_path, config).exists() for s in scenes)
