import io
import threading

import pytest
from PIL import Image

from comicflow import sources
from comicflow.common import Cancelled, Context, file_hash, read_json
from comicflow.downloads import DownloadManager
from comicflow.importers import import_source


def image():
    out = io.BytesIO()
    Image.new('RGB', (80, 120), 'orange').save(out, format='PNG')
    return out.getvalue()


def book():
    return {'id': 'book', 'provider': 'dogemanga', 'title': '测试漫画', 'cover': '', 'groups': [{'id':'issue','name':'正篇','count':2}],
            'chapters': [{'id':'ch1','title':'第1话','group':'issue','order':1},
                         {'id':'ch2','title':'第2话','group':'issue','order':2}]}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, 'details', lambda *args, **kw: book())
    monkeypatch.setattr(sources, 'chapter_pages', lambda p,c,event=None: [{'url':f'https://dogemanga.com/{c}/{i}'} for i in range(3)])
    monkeypatch.setattr(sources, 'image_bytes', lambda *args: (image(), '.png'))
    m = DownloadManager(tmp_path / 'data')
    yield m
    m.close()


def finish(manager, state):
    manager.futures[state['id']].result(timeout=10)
    return manager.get(state['id'])


def test_search_preserves_other_source_when_one_fails(monkeypatch):
    def run(p, q, page):
        if p == 'mangadex': raise sources.SourceError('暂时不可用')
        return [book()], False
    monkeypatch.setattr(sources, 'search_one', run)
    groups = sources.search('test')['groups']
    assert groups[0]['items'][0]['id'] == 'book' and groups[1]['error']


def test_dogemanga_parsing_groups_order_and_pages(monkeypatch):
    html = '''<meta property="og:title" content="示例 - 漫畫狗"><div id="site-manga__tab-pane-all"><a href="/p/duplicate">不要重复</a></div>
    <div id="site-manga__tab-pane-issue"><a href="/p/b">第10话</a><a href="/p/c">第2话</a><a href="/p/a">序章</a></div>
    <div id="site-manga__tab-pane-tankobon"><a href="/p/v">第1卷</a></div>'''
    monkeypatch.setattr(sources, 'fetch', lambda *a,**kw: html.encode())
    result = sources.details('dogemanga', 'test')
    assert [c['id'] for c in result['chapters']] == ['a','c','b','v']
    assert [g['name'] for g in result['groups']] == ['正篇', '单行本']
    html = '''<img class="site-reader__image" data-page-index="1" data-page-image-url="https://dogemanga.com/images/2.jpg"><img class="site-reader__image" data-page-index="0" data-page-image-url="https://dogemanga.com/images/1.jpg">'''
    assert sources.chapter_pages('dogemanga','test')[0]['url'].endswith('1.jpg')
    html = html.replace('data-page-index="1"', 'data-page-index="2"')
    with pytest.raises(sources.SourceError, match='页码不完整'):
        sources.chapter_pages('dogemanga', 'test')


@pytest.mark.parametrize('url', ['http://dogemanga.com/a','https://127.0.0.1/a','https://dogemanga.com.attacker.example/a','https://dogemanga.com:8765/a','https://user:pass@dogemanga.com/a'])
def test_sources_reject_non_source_urls(url):
    with pytest.raises(sources.SourceError): sources.allowed_url('dogemanga',url)


def test_ids_and_image_decode_rejected(monkeypatch):
    with pytest.raises(sources.SourceError): sources.validate_id('dogemanga','../../anything')
    with pytest.raises(sources.SourceError): sources.search('   ')
    monkeypatch.setattr(sources, 'fetch', lambda *a,**kw: b'<html>access denied</html>')
    with pytest.raises(sources.SourceError, match='无法解码'):
        sources.image_bytes('dogemanga', {'url':'https://dogemanga.com/test'})


def test_download_import_and_reversed_selection_keep_reading_order(manager, tmp_path):
    task = manager.create('dogemanga','book',['ch2','ch1'])
    state = finish(manager, task)
    assert state['status'] == 'completed' and state['pages'] == 6
    assert state['completed_chapters'] == 2
    imported = manager.imported(task['id'])
    folder = tmp_path / 'render'
    folder.mkdir()
    pages = import_source(imported['source'], folder, Context(folder))
    assert len(pages) == 6 and pages[0]['chapter'].startswith('00001') and pages[3]['chapter'].startswith('00002')
    assert manager.create('dogemanga','book',['ch1','ch2'])['id'] == task['id']


def test_bad_chapters_and_partial_import_are_rejected(manager, monkeypatch):
    with pytest.raises(sources.SourceError): manager.create('dogemanga','book',['different-book-chapter'])
    with pytest.raises(sources.SourceError): manager.create('dogemanga','book',['ch1','ch1'])
    monkeypatch.setattr(manager, 'enqueue', lambda task_id: None)
    task = manager.create('dogemanga','book',['ch1'])
    with pytest.raises(sources.SourceError, match='完整下载'): manager.imported(task['id'])


def test_resume_repairs_missing_files_and_reuses_intact_ones(manager, monkeypatch):
    task=manager.create('dogemanga','book',['ch1'])
    assert finish(manager, task)['status']=='completed'
    manifest = read_json(manager.library / task['id'] / 'manifest.json')
    path = manager.library / task['id'] / 'pages' / manifest['files']['ch1:0']['file']
    path.unlink()
    with pytest.raises(sources.SourceError, match='缺失'): manager.imported(task['id'])
    calls=[]
    def receive(p, page, event): calls.append(page);return image(),'.png'
    monkeypatch.setattr(sources,'image_bytes',receive)
    manager.resume(task['id'])
    state=finish(manager,task)
    assert state['status']=='completed' and len(calls)==1 and path.exists()


def test_cancel_and_resume_download(manager, monkeypatch):
    started=threading.Event()
    def slow(p,c,event=None):
        started.set()
        event.wait(5)
        raise Cancelled()
    monkeypatch.setattr(sources,'chapter_pages',slow)
    task=manager.create('dogemanga','book',['ch1'])
    assert started.wait(3)
    manager.cancel(task['id'])
    assert finish(manager,task)['status']=='cancelled'
    monkeypatch.setattr(sources,'chapter_pages',lambda *a:[{'url':'https://dogemanga.com/page'}])
    manager.resume(task['id'])
    assert finish(manager,task)['status']=='completed'


def test_startup_marks_interrupted_and_preserves_metadata(manager, monkeypatch):
    monkeypatch.setattr(manager,'enqueue',lambda task_id:None)
    task=manager.create('dogemanga','book',['ch1'])
    other=DownloadManager(manager.data)
    try: assert other.get(task['id'])['status']=='interrupted'
    finally: other.close()


def test_mangadex_filters_external_and_deduplicates_translations(monkeypatch):
    manga={'id':'a','attributes':{'title':{'en':'Example'},'description':{}},'relationships':[]}
    def chapter(cid,**extra):return {'id':cid,'attributes':{'translatedLanguage':'en','chapter':'1','volume':'1','pages':3,**extra}}
    def data(url,**kw):
        if url.endswith('/feed'):return {'data':[chapter('a'),chapter('b'),chapter('c',translatedLanguage='zh'),chapter('d',chapter='2',externalUrl='https://example.org')], 'total':4}
        return {'data':manga}
    monkeypatch.setattr(sources,'json_at',data)
    result=sources.details('mangadex','12345678-1234-1234-1234-123456789abc')
    assert [g['id'] for g in result['groups']]==['zh','en']
    assert [c['id'] for c in result['chapters']]==['a','c']
