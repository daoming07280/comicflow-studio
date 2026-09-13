import asyncio
import io
import threading
import time
import wave

import edge_tts
import pytest

from comicflow import edge_speech as edge
from comicflow.common import Cancelled, Context, read_json
from comicflow.pipeline import configuration
from comicflow.speech import synthesize


def wav():
    out=io.BytesIO()
    with wave.open(out,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(b'\x01\x01'*24000)
    return out.getvalue()


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(edge,'SERVICE_DIR',tmp_path/'service')
    monkeypatch.setattr(edge,'REQUEST_GAP',0)
    monkeypatch.setattr(edge,'RETRY_DELAYS',(0,0))
    directory=tmp_path/'chunks';directory.mkdir()
    return directory,configuration({}),Context(tmp_path)


def test_long_text_splits_without_losing_spoken_characters():
    text=('少年走进了地下城。他没有回头！'*30)+'最后一句。'
    chunks=edge.split_text(text)
    assert max(map(len,chunks))<=160
    assert edge.spoken_chars(''.join(chunks))==edge.spoken_chars(text)
    assert edge.split_text('……！？')==[]


def test_no_audio_retries_with_same_words_and_voice(setup,monkeypatch):
    directory,config,ctx=setup;calls=[]
    async def stream(text,voice,rate,path):
        calls.append((text,voice))
        path.write_bytes(b'partial')
        if len(calls)==1:raise edge_tts.exceptions.NoAudioReceived('test')
        path.write_bytes(wav());return {'boundaries':[],'end':.9}
    monkeypatch.setattr(edge,'stream_to_file',stream)
    result=edge.speech_chunk('外面的人类。',directory,config,ctx)
    assert calls==[('外面的人类。',config['voice']),('外面的人类',config['voice'])]
    assert result.read_bytes()==wav() and not list(directory.glob('*.part.mp3'))
    assert read_json(result.with_suffix('.json'))['punctuation_adjusted']
    assert edge.speech_chunk('外面的人类。',directory,config,ctx)==result and len(calls)==2


def test_truncated_tail_not_published(setup,monkeypatch):
    directory,config,ctx=setup
    async def stream(text,voice,rate,path):
        path.write_bytes(wav());return {'boundaries':[],'end':3}
    monkeypatch.setattr(edge,'stream_to_file',stream)
    with pytest.raises(RuntimeError,match='不会导出缺音'):
        edge.speech_chunk('最后一句不能丢失。',directory,config,ctx)
    assert not list(directory.glob('*.mp3')) and not list(directory.glob('*.json'))


def test_stream_rejects_missing_text_boundary(setup,monkeypatch):
    directory,config,ctx=setup
    class Fake:
        def __init__(self,*a,**kw):pass
        async def stream(self):
            yield {'type':'audio','data':wav()}
            yield {'type':'SentenceBoundary','text':'第一句。','offset':0,'duration':10000000}
    monkeypatch.setattr(edge_tts,'Communicate',Fake)
    with pytest.raises(edge.IncompleteSpeech,match='内容不完整'):
        asyncio.run(edge.stream_to_file('第一句。第二句。',config['voice'],0,directory/'raw.mp3'))


def test_cancel_pending_network_request_promptly(setup,monkeypatch):
    directory,config,ctx=setup
    async def stream(*args):await asyncio.sleep(60)
    monkeypatch.setattr(edge,'stream_to_file',stream)
    timer=threading.Timer(.2,ctx.event.set);timer.start();start=time.monotonic()
    try:
        with pytest.raises(Cancelled):asyncio.run(edge.cancellable_stream('测试',config,directory/'a.mp3',ctx))
    finally:timer.cancel()
    assert time.monotonic()-start<2


def test_backoff_honors_retry_after_and_is_cancellable(setup):
    _,_,ctx=setup
    error=RuntimeError('429');error.headers={'Retry-After':'120'}
    assert edge.retry_delay(error,0)==120
    timer=threading.Timer(.15,ctx.event.set);timer.start();start=time.monotonic()
    try:
        with pytest.raises(Cancelled):edge.wait_for_service(ctx,120,'等待')
    finally:timer.cancel()
    assert time.monotonic()-start<2


def test_only_first_changed_text_probe_uses_short_wait(monkeypatch):
    monkeypatch.setattr(edge, 'REQUEST_GAP', 5)
    monkeypatch.setattr(edge, 'RETRY_DELAYS', (15, 30, 60, 120, 240))
    error = edge_tts.exceptions.NoAudioReceived('empty')
    assert edge.retry_delay(error, 0, text_repair=True) == 5
    assert edge.retry_delay(error, 0) == 15
    assert edge.retry_delay(error, 1, text_repair=True) == 30
    assert edge.retry_delay(TimeoutError(), 0, text_repair=True) == 15
    error.status = 429
    assert edge.retry_delay(error, 0, text_repair=True) == 15
    error.status = None
    error.headers = {'Retry-After': '90'}
    assert edge.retry_delay(error, 0, text_repair=True) == 90


def test_retry_reuses_good_long_text_chunks(setup,monkeypatch):
    directory,config,ctx=setup;calls=[]
    text='甲'*160+'乙'*160
    async def stream(text,voice,rate,path):
        calls.append(text);path.write_bytes(wav());return {'boundaries':[],'end':.9}
    monkeypatch.setattr(edge,'stream_to_file',stream)
    result=edge.synthesize_edge(text,directory,config,ctx)
    assert edge.duration(result)>=1.99 and len(calls)==2
    edge.synthesize_edge(text,directory,config,ctx)
    assert len(calls)==2


def test_unvoiced_sentence_recovers_with_short_clauses(setup,monkeypatch):
    directory,config,ctx=setup;calls=[]
    async def stream(text,voice,rate,path):
        calls.append(text)
        if text not in ['第一句','第二句']:raise edge_tts.exceptions.NoAudioReceived('test')
        path.write_bytes(wav());return {'boundaries':[],'end':.9}
    monkeypatch.setattr(edge,'stream_to_file',stream)
    result=edge.speech_chunk('第一句。第二句。',directory,config,ctx)
    assert len(calls)==5 and result.suffix=='.wav' and edge.duration(result)>=1.99
    assert edge.speech_chunk('第一句。第二句。',directory,config,ctx)==result and len(calls)==5


def test_legacy_cache_validated_and_corruption_repaired(setup,tmp_path):
    _,config,ctx=setup
    (tmp_path/'input.wav').write_bytes(wav())
    scene={'text':'原音频','imported_audio':'input.wav'}
    result,_=synthesize(scene,tmp_path,config,ctx)
    result.with_suffix('.checked.json').unlink()
    synthesize(scene,tmp_path,config,ctx)
    assert read_json(result.with_suffix('.checked.json'))['legacy_decode_checked']
    result.write_bytes(b'broken')
    repaired,seconds=synthesize(scene,tmp_path,config,ctx)
    assert repaired==result and seconds>1
    assert edge.verified_decode(repaired,ctx)>1
