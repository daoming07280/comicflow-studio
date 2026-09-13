import json
import urllib.request
from pathlib import Path

root = str(Path(__file__).resolve().parents[1])
for port in range(8765, 8776):
    url = f'http://127.0.0.1:{port}'
    try:
        info = json.load(urllib.request.urlopen(url + '/api/health', timeout=1))
        if info.get('root') == root:
            urllib.request.urlopen(urllib.request.Request(url + '/api/shutdown', method='POST'), timeout=5).close()
            print('ComicFlow is stopping. Completed work is preserved.')
            break
    except Exception:
        pass
else:
    print('ComicFlow is not running.')
