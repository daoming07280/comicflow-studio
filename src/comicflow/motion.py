"""Subpixel camera motion with a fixed optical centre, independent of YUV crop rounding."""
from PIL import Image

from .common import executable

MOTION_VERSION = "affine-subpixel-v1"


def camera_frame(image, index, count, amount=.024):
    if count <= 1 or index == 0:
        return image.copy()
    t = min(1.0, max(0.0, index / (count - 1)))
    # Gentle start and finish avoid an abrupt velocity change at scene cuts.
    zoom = 1.0 + amount * t * t * (3.0 - 2.0 * t)
    inverse = 1.0 / zoom
    cx, cy = image.width / 2, image.height / 2
    return image.transform(image.size, Image.Transform.AFFINE,
                           (inverse, 0.0, cx * (1 - inverse),
                            0.0, inverse, cy * (1 - inverse)),
                           resample=Image.Resampling.BICUBIC)


def encode_camera(frame, target, count, fps, ctx):
    with Image.open(frame) as image:
        image = image.convert("RGB")
        w, h = image.size

        def frames():
            for index in range(count):
                ctx.check()
                with camera_frame(image, index, count) as output:
                    yield output.tobytes()

        ctx.run_input([executable("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo",
                       "-pixel_format", "rgb24", "-video_size", f"{w}x{h}",
                       "-framerate", fps, "-i", "pipe:0", "-frames:v", count,
                       "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                       "-pix_fmt", "yuv420p", "-threads", "2", target], frames(),
                      timeout=max(900, count / fps * 20))
