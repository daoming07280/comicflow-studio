from __future__ import annotations
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = ROOT / 'vision.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if key.strip() in {'COMICFLOW_VISION_BASE_URL', 'COMICFLOW_VISION_MODEL', 'COMICFLOW_VISION_API_KEY'}:
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    for port in range(8765, 8776):
        url = f'http://127.0.0.1:{port}'
        try:
            import json
            info = json.load(urllib.request.urlopen(url + '/api/health', timeout=1))
            if info.get('root') == str(ROOT):
                webbrowser.open(url)
                return
        except Exception:
            pass
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                break
            except OSError:
                continue
    else:
        raise RuntimeError('Ports 8765-8775 are unavailable.')
    logs = ROOT / 'data' / 'server'
    logs.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with (logs / 'server.log').open('ab') as log:
        child = subprocess.Popen([sys.executable, '-m', 'comicflow.cli', 'serve', '--port', str(port)],
                                 cwd=ROOT, stdout=log, stderr=log, creationflags=flags)
    (logs / 'server.pid').write_text(str(child.pid), encoding='ascii')
    for _ in range(80):
        if child.poll() is not None:
            raise RuntimeError('Server failed. See data/server/server.log.')
        try:
            urllib.request.urlopen(url + '/api/health', timeout=1).close()
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(.25)
    child.terminate()
    raise RuntimeError('Server did not become ready. See data/server/server.log.')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(e)
        input('Press Enter to close...')
