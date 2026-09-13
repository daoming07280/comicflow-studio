"""Measure optical-centre drift in the same reference shot before and after the fix."""
from pathlib import Path
import cv2
import numpy as np
from comicflow.common import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def track(job):
    timeline = read_json(job / 'timeline.json')
    first_duration = timeline[0]['duration']
    cap = cv2.VideoCapture(str(job / 'final.mp4'))
    fps = cap.get(cv2.CAP_PROP_FPS)
    ok, first = cap.read()
    if not ok:
        raise RuntimeError('Cannot decode reference output')
    first = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
    h,w = first.shape
    mask = np.zeros_like(first)
    mask[20:int(h*.84), 20:w-20] = 255
    points = cv2.goodFeaturesToTrack(first, 180, .02, 12, mask=mask)
    centre = np.array([w/2, h/2, 1.0])
    positions = []
    for _ in range(round(first_duration*fps)-1):
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tracked, status, errors = cv2.calcOpticalFlowPyrLK(first, gray, points, None, winSize=(31,31), maxLevel=3)
        keep = (status.ravel() == 1) & (errors.ravel() < 16)
        matrix, inliers = cv2.estimateAffinePartial2D(points[keep], tracked[keep], method=cv2.LMEDS)
        if matrix is None:
            raise RuntimeError('Not enough reliable tracks')
        positions.append(matrix @ centre)
    cap.release()
    positions = np.array(positions)
    return {'frames_measured':len(positions), 'centre_peak_to_peak_px':float(np.ptp(positions,axis=0).max()),
            'max_frame_step_px':float(np.linalg.norm(np.diff(positions,axis=0),axis=1).max())}


if __name__ == '__main__':
    before = track(ROOT / 'data/jobs/7e229bcabac1')
    after = track(ROOT / 'data/jobs/0ff570538797')
    report = {'before':before,'after':after,
              'passed':after['max_frame_step_px'] < .12 and after['centre_peak_to_peak_px'] < .15,
              'method':'参考样片第一镜头：屏蔽底部字幕，光流跟踪特征点并拟合相似变换，估计光学中心位移。包括最终二次编码。'}
    write_json(ROOT / 'docs/reference-motion-audit.json', report)
    print(report)
    assert report['passed']
