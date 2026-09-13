"""Compare decoded, encoded camera motion against the previous integer-crop method."""
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from comicflow.common import Context, executable, probe, write_json
from comicflow.motion import encode_camera

ROOT = Path(__file__).resolve().parents[1]


def centre_track(path, width, height):
    cap = cv2.VideoCapture(str(path))
    points = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(float)
            cx, cy = width // 2, height // 2
            roi = gray[cy-16:cy+16, cx-16:cx+16]
            yy, xx = np.indices(roi.shape)
            points.append([float((roi*xx).sum()/roi.sum()), float((roi*yy).sum()/roi.sum())])
    finally:
        cap.release()
    points = np.asarray(points)
    return {"frames": len(points), "centre_peak_to_peak_px": float(np.ptp(points, axis=0).max()),
            "max_frame_step_px": float(np.linalg.norm(np.diff(points, axis=0), axis=1).max())}


def main():
    folder = ROOT / 'data/test-runs/motion-v2'
    folder.mkdir(parents=True, exist_ok=True)
    w, h, count, fps = 640, 480, 120, 24
    yy, xx = np.indices((h, w))
    gaussian = np.exp(-((xx-(w/2-.5))**2+(yy-(h/2-.5))**2)/18)*230
    image = Image.fromarray(gaussian.astype('uint8')).convert('RGB')
    source = folder / 'centre.png'
    image.save(source)
    ctx = Context(folder)
    old_filter = (f"scale={w*2}:{h*2},zoompan=z='1+0.024*on/{count-1}':"
                  f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={count}:s={w}x{h}:fps={fps}")
    before, after = folder / 'before.mp4', folder / 'after.mp4'
    ctx.run([executable('ffmpeg'),'-v','error','-y','-loop','1','-i',source,'-vf',old_filter,
             '-frames:v',count,'-an','-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p',before])
    encode_camera(source, after, count, fps, ctx)
    old = centre_track(before,w,h)
    new = centre_track(after,w,h)
    passed = (new['frames'] == old['frames'] == count and new['centre_peak_to_peak_px'] < .08
              and new['max_frame_step_px'] < .05 and new['centre_peak_to_peak_px'] < old['centre_peak_to_peak_px'] * .2)
    report = {'passed': passed, 'before': old, 'after': new,
              'method': '5 秒测试画面，中心固定高斯标记；对 H.264 解码后的标记质心逐帧测量。'}
    write_json(ROOT / 'docs/motion-fix-audit.json', report)
    print(report)
    assert passed, report


if __name__ == '__main__':
    main()
