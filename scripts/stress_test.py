"""Long-form timing regression with synthetic images and a quiet test tone."""
from pathlib import Path
import json
import wave
import numpy as np
from PIL import Image
from comicflow.common import Context, write_json
from comicflow.pipeline import configuration
from comicflow.render import render_video, audit

root = Path(__file__).resolve().parents[1]
folder = root / 'data' / 'test-runs' / 'long-form'
folder.mkdir(parents=True, exist_ok=True)
rate = 44100
samples = (np.sin(np.arange(int(41.417 * rate)) * 2 * np.pi * 220 / rate) * 500).astype('<i2')
with wave.open(str(folder / 'tone.wav'), 'wb') as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(rate)
    f.writeframes(samples.tobytes())
panels, scenes = [], []
for i in range(40):
    pid = f'p{i+1:05d}'
    Image.new('RGB', (320, 240), ((i*33)%200, 40, 60)).save(folder / f'{pid}.jpg')
    panels.append({'id': pid, 'file': f'{pid}.jpg', 'source': 'synthetic/' + pid, 'box': [0, 0, 320, 240]})
    scenes.append({'id': f's{i+1:05d}', 'panel_ids': [pid], 'text': f'长合集时间轴回归测试，第{i+1}段。', 'imported_audio': 'tone.wav'})
config = configuration({'title': '长合集时间轴回归测试', 'preset': 'preview', 'mode': 'manual', 'motion': False})
ctx = Context(folder, callback=lambda **kw: print(kw, flush=True))
timeline = render_video({'scenes': scenes}, panels, folder, config, ctx)
report = audit(folder, timeline, panels, config, ctx)
report['fixture'] = '40 张合成纯色测试图和 41.417 秒测试音重复编排，不是实际漫画剧情质量测试。'
write_json(root / 'docs' / 'long-form-audit.json', report)
print(json.dumps(report, ensure_ascii=False), flush=True)
