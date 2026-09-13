import json
import httpx
from comicflow.narration import vision_request


def test_vision_http_contract_keeps_images_and_text_bound(monkeypatch, tmp_path):
    monkeypatch.setenv('COMICFLOW_VISION_BASE_URL', 'http://model.test/v1')
    monkeypatch.setenv('COMICFLOW_VISION_MODEL', 'test-vision')
    monkeypatch.setenv('COMICFLOW_VISION_API_KEY', 'test-key')
    (tmp_path / 'panel.jpg').write_bytes(b'example-image-bytes')
    calls = []
    def handle(request):
        assert str(request.url) == 'http://model.test/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer test-key'
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'scenes': [{'panel_ids': ['p00001'], 'text': '测试解说'}]})}}]})
    client_class = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client_class(transport=httpx.MockTransport(handle), **kw))
    result = vision_request([{'id': 'p00001', 'file': 'panel.jpg'}], {'p00001': {'text': '画面原文'}}, tmp_path, '前文')
    assert result[0]['panel_ids'] == ['p00001']
    content = calls[0]['messages'][1]['content']
    assert 'p00001' in content[1]['text'] and '画面原文' in content[1]['text']
    assert content[2]['image_url']['url'].startswith('data:image/jpeg;base64,')


def test_os_job_lock_prevents_two_renderers(tmp_path):
    import pytest
    from comicflow.common import JobLock
    with JobLock(tmp_path):
        with pytest.raises(RuntimeError, match='另一处运行'):
            with JobLock(tmp_path):
                pass
    with JobLock(tmp_path):
        pass
