import sys
import threading
import time
import wave

import cv2
import numpy as np
import pytest
from PIL import Image

from comicflow.common import Cancelled, Context
from comicflow.motion import camera_frame, encode_camera
from comicflow.pipeline import configuration
from comicflow.speech import synthesize


def marker(w=640, h=480):
    yy, xx = np.indices((h, w))
    pixels = (230*np.exp(-((xx-(w/2-.5))**2+(yy-(h/2-.5))**2)/18)).astype('uint8')
    return Image.fromarray(pixels).convert('RGB')


def test_camera_single_and_first_frame_preserve_exact_image():
    image = marker(120, 100)
    assert camera_frame(image, 0, 1).tobytes() == image.tobytes()
    assert camera_frame(image, 0, 80).tobytes() == image.tobytes()
    assert camera_frame(image, 79, 80).tobytes() != image.tobytes()


def test_encoded_zoom_keeps_optical_centre_stable(tmp_path):
    source = tmp_path / 'source.png'
    marker().save(source)
    output = tmp_path / 'zoom.mp4'
    encode_camera(source, output, 120, 24, Context(tmp_path))
    capture = cv2.VideoCapture(str(output))
    centres = []
    try:
        while True:
            ok, image = capture.read()
            if not ok:
                break
            patch = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)[224:256, 304:336].astype(float)
            yy, xx = np.indices(patch.shape)
            centres.append([(xx*patch).sum()/patch.sum(), (yy*patch).sum()/patch.sum()])
    finally:
        capture.release()
    assert len(centres) == 120
    assert np.linalg.norm(np.diff(centres, axis=0), axis=1).max() < .05
    assert np.ptp(centres, axis=0).max() < .1


def test_blocked_encoder_pipe_is_cancellable(tmp_path):
    event = threading.Event()
    timer = threading.Timer(.25, event.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            Context(tmp_path, event).run_input([sys.executable, '-c', 'import time; time.sleep(30)'],
                                              [b'x' * (4 * 1024 * 1024)], timeout=10)
    finally:
        timer.cancel()
    assert time.monotonic() - start < 4


def test_audio_frame_rate_changes_invalidate_cache(tmp_path):
    with wave.open(str(tmp_path / 'input.wav'), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(44100)
        f.writeframes(b'\0\0' * 43000)
    scene = {'text': '解说', 'imported_audio': 'input.wav'}
    a, _ = synthesize(scene, tmp_path, configuration({'fps': 24}), Context(tmp_path))
    b, _ = synthesize(scene, tmp_path, configuration({'fps': 30}), Context(tmp_path))
    assert a != b
    with wave.open(str(a)) as f:
        assert f.getnframes() % 2000 == 0
    with wave.open(str(b)) as f:
        assert f.getnframes() % 1600 == 0


def test_calm_voice_is_the_default():
    config = configuration({})
    assert config['voice'] == 'zh-CN-YunyangNeural'
    assert config['rate'] == 0
