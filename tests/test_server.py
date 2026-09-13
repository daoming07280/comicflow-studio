import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import comicflow.server as server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(server, 'JOBS', tmp_path / 'data' / 'jobs')
    monkeypatch.setattr(server, 'EXPORTS', tmp_path / 'outputs')
    monkeypatch.setattr(server, 'enqueue', lambda folder: None)
    with TestClient(server.app) as client:
        yield client


def png():
    b = io.BytesIO()
    Image.new('RGB', (100, 80), 'white').save(b, format='PNG')
    return b.getvalue()


def test_home_and_health(client):
    assert client.get('/').status_code == 200
    assert client.get('/api/health').json()['ok']
    assert client.get('/manual.html').status_code == 200


def test_cross_origin_cannot_start_local_job(client):
    response = client.post('/api/jobs', headers={'Origin': 'https://attacker.example'}, json={'source': 'anything'})
    assert response.status_code == 403


def test_upload_directory_keeps_chapter_paths(client):
    response = client.post('/api/upload', data={'paths': json.dumps(['Book/第2章/1.png', 'Book/第10章/1.png'])},
                           files=[('files', ('1.png', png(), 'image/png')), ('files', ('1.png', png(), 'image/png'))])
    assert response.status_code == 200
    path = Path(response.json()['source'])
    assert (path / '第2章/1.png').is_file() and (path / '第10章/1.png').is_file()


def test_upload_traversal_and_duplicate_rejected(client):
    response = client.post('/api/upload', files={'files': ('../oops.png', png(), 'image/png')})
    assert response.status_code == 400
    response = client.post('/api/upload', files=[('files', ('a.png', png(), 'image/png')), ('files', ('a.png', png(), 'image/png'))])
    assert response.status_code == 400


def test_create_bad_source_rejected_and_valid_job_persisted(client, tmp_path):
    assert client.post('/api/jobs', json={'source': str(tmp_path / 'missing')}).status_code == 400
    src = tmp_path / 'comic'
    src.mkdir()
    (src / '1.png').write_bytes(png())
    result = client.post('/api/jobs', json={'source': str(src), 'title': '<script>test</script>'})
    assert result.status_code == 200
    job_id = result.json()['id']
    assert client.get(f'/api/jobs/{job_id}').json()['status'] == 'queued'
    assert client.get(f'/jobs/{job_id}/config.json').status_code == 404
    assert client.post(f'/api/jobs/{job_id}/resume').status_code == 409


def test_missing_vision_is_clear_error(client, tmp_path, monkeypatch):
    monkeypatch.delenv('COMICFLOW_VISION_BASE_URL', raising=False)
    src = tmp_path / 'comic'
    src.mkdir()
    result = client.post('/api/jobs', json={'source': str(src), 'mode': 'ai'})
    assert result.status_code == 400 and '尚未连接' in result.json()['detail']


def test_voice_audition_and_audio_access(client, monkeypatch):
    calls = []
    def synth(scene, folder, config, ctx):
        calls.append((scene, config))
        audio = folder / 'audio' / ('a' * 24 + '.wav')
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b'RIFF-test')
        return audio, 12.0
    monkeypatch.setattr(server, 'synthesize', synth)
    response = client.post('/api/voice-preview', json={'voice': 'zh-CN-YunyangNeural', 'rate': 0})
    assert response.status_code == 200
    assert calls[0][1]['voice'] == 'zh-CN-YunyangNeural'
    assert client.get(response.json()['url']).content == b'RIFF-test'
    assert client.post('/api/voice-preview', json={'voice': 'bad-voice'}).status_code == 400
    assert client.post('/api/voice-preview', json={'rate': 100}).status_code == 422
    assert client.get('/voice-previews/config.json').status_code == 404


def test_audition_failure_is_explicit(client, monkeypatch):
    def failure(*args):
        raise RuntimeError('test connection failed')
    monkeypatch.setattr(server, 'synthesize', failure)
    response = client.post('/api/voice-preview', json={})
    assert response.status_code == 502
    assert '试听配音暂时失败' in response.json()['detail']


def test_catalog_search_download_and_import_api(client, monkeypatch):
    from comicflow import sources
    comic={'id':'sample','provider':'dogemanga','title':'测试漫画','groups':[{'id':'issue','name':'正篇','count':1}],
           'chapters':[{'id':'first','title':'第1话','group':'issue','order':1}], 'cover':''}
    monkeypatch.setattr(sources,'search',lambda *a:{'groups':[{'provider':'dogemanga','items':[comic],'has_next':False,'error':''}]})
    monkeypatch.setattr(sources,'details',lambda *a:comic)
    monkeypatch.setattr(sources,'chapter_pages',lambda *a:[{'url':'https://dogemanga.com/page'}])
    monkeypatch.setattr(sources,'image_bytes',lambda *a:(png(),'.png'))
    assert len(client.get('/api/sources').json())==2
    assert client.get('/api/catalog/search',params={'q':'测试'}).json()['groups'][0]['items'][0]['id']=='sample'
    assert client.get('/api/catalog/dogemanga/sample').json()['title']=='测试漫画'
    task=client.post('/api/downloads',json={'provider':'dogemanga','book_id':'sample','chapter_ids':['first']}).json()
    server.app.state.downloads.futures[task['id']].result(timeout=5)
    assert client.get('/api/downloads/'+task['id']).json()['status']=='completed'
    imported=client.post('/api/downloads/'+task['id']+'/import').json()
    assert Path(imported['source']).is_dir() and imported['count']==1
    assert client.post('/api/jobs',json={'source':imported['source']}).status_code==200
    assert client.post('/api/downloads',json={'provider':'dogemanga','book_id':'sample','chapter_ids':['not-member']}).status_code==400
    assert client.get('/api/catalog/unknown/sample').status_code==400
    assert client.post('/api/downloads',headers={'Origin':'https://attacker.example'},json={'provider':'dogemanga','book_id':'sample','chapter_ids':['first']}).status_code==403
