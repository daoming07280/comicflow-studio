"""Deterministic technical fixtures, clearly labelled as a fictional test comic."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = Path('C:/Windows/Fonts/msyh.ttc')


def make():
    target = ROOT / 'examples' / 'starter' / '第01章'
    target.mkdir(parents=True, exist_ok=True)
    phrases = ['地下城的大门终于打开了。', '林舟握紧长剑，独自走进黑暗。', '石碑上亮起一行字：隐藏职业已觉醒。', '这一次，他决定改变自己的命运。']
    # Two continuous webtoon strips with a real gutter; no hidden script is supplied.
    for page in range(2):
        canvas = Image.new('RGB', (800, 1940), '#ffffff')
        d = ImageDraw.Draw(canvas)
        for row in range(2):
            y = row * 1000
            d.rectangle((0, y, 800, y + 940), fill=('#182437' if row == 0 else '#28203d'))
            for i in range(18):
                d.line((400, y + 480, (i * 197) % 800, y + (i * 151) % 720), fill='#455267', width=2)
            d.rounded_rectangle((210, y + 100, 590, y + 640), radius=180, outline='#a6d1e1', width=9)
            d.ellipse((362, y + 415, 438, y + 490), fill='#b2a58d')
            d.polygon([(367, y + 486), (430, y + 486), (470, y + 665), (330, y + 665)], fill='#647c9b')
            d.line((445, y + 580, 560, y + 425), fill='#f4dfa2', width=8)
            d.rounded_rectangle((35, y + 730, 765, y + 875), radius=20, fill='white')
            text = phrases[page * 2 + row]
            font = ImageFont.truetype(str(FONT), 34)
            if len(text) > 19:
                d.text((65, y + 758), text[:19], font=font, fill='#15191f')
                d.text((65, y + 803), text[19:], font=font, fill='#15191f')
            else:
                d.text((65, y + 775), text, font=font, fill='#15191f')
        canvas.save(target / f'{page + 1:03d}.png')
    (ROOT / 'examples' / 'README.md').write_text('starter 是程序生成的虚构流程测试图，用于验证中文识别、长图切分、配音和渲染，不代表真实韩漫美术或剧情质量。\n', encoding='utf-8')


if __name__ == '__main__':
    make()
