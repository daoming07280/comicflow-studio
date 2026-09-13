"""Exercise real queue, cancellation, and resume via the running local service."""
import json
import time
from pathlib import Path
import httpx

root = Path(__file__).resolve().parents[1]
with httpx.Client(base_url='http://127.0.0.1:8765', timeout=10) as client:
    response = client.post('/api/jobs', json={'source': str(root / 'examples/starter'), 'title': '恢复机制验收', 'preset': 'preview'})
    response.raise_for_status()
    job_id = response.json()['id']
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = client.get('/api/jobs/' + job_id).json()
        if state['progress'] >= 63 and state['status'] == 'running':
            break
        if state['status'] in {'failed', 'completed'}:
            raise RuntimeError('Did not reach the cancellation checkpoint: ' + str(state))
        time.sleep(.05)
    else:
        raise RuntimeError('No cancellation checkpoint before deadline')
    folder = root / 'data/jobs' / job_id
    cached = {str(p): p.stat().st_mtime_ns for p in (folder / 'clips').glob('*.mp4')}
    client.post(f'/api/jobs/{job_id}/cancel').raise_for_status()
    while time.monotonic() < deadline:
        state = client.get('/api/jobs/' + job_id).json()
        if state['status'] == 'cancelled':
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Cancellation timed out')
    client.post(f'/api/jobs/{job_id}/resume').raise_for_status()
    while time.monotonic() < deadline:
        state = client.get('/api/jobs/' + job_id).json()
        if state['status'] == 'completed':
            break
        if state['status'] == 'failed':
            raise RuntimeError(state['message'])
        time.sleep(.2)
    else:
        raise RuntimeError('Resume timed out')
    report = {'passed': state['status'] == 'completed' and bool(cached) and all(Path(p).stat().st_mtime_ns == m for p, m in cached.items()),
              'job_id': job_id, 'cancelled': True, 'resumed': True, 'cached_clips_reused': len(cached), 'final_audit': state['report']['passed']}
    (root / 'docs/recovery-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert report['passed']
