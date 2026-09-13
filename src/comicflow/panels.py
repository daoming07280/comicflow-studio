from __future__ import annotations

import numpy as np
from PIL import Image

from .common import write_json


def split_ranges(image, ratio=1.55):
    """Find full-width gutters; otherwise use contiguous viewports, never drop pixels."""
    w, h = image.size
    if h <= w * 1.85:
        return [(0, h, False)]
    sample = image.convert("RGB").resize((min(w, 256), h))
    pixels = np.asarray(sample, dtype=np.float32)
    white = (pixels.min(axis=2) > 243).mean(axis=1) > .985
    black = (pixels.max(axis=2) < 12).mean(axis=1) > .985
    uniform = white | black
    bands, start = [], None
    for y, blank in enumerate(uniform):
        if blank and start is None:
            start = y
        if start is not None and (not blank or y == h - 1):
            end = y if not blank else y + 1
            if end - start >= max(8, w * .013):
                bands.append((start + end) // 2)
            start = None
    ranges, top = [], 0
    target = int(w * ratio)
    while h - top > w * 1.85:
        desired = top + target
        candidates = [y for y in bands if top + w * .65 <= y <= min(top + w * 1.85, h - w * .45)]
        cut = min(candidates, key=lambda y: abs(y - desired)) if candidates else desired
        ranges.append((top, cut, not bool(candidates)))
        top = cut
    ranges.append((top, h, False))
    return ranges


def make_panels(pages, folder, ctx):
    out = folder / "panels"
    out.mkdir(exist_ok=True)
    panels = []
    for index, page in enumerate(pages):
        ctx.progress(10 + 10 * index / len(pages), f"切分长图 {index + 1}/{len(pages)}")
        with Image.open(folder / page["file"]) as image:
            for top, bottom, forced in split_ranges(image):
                pid = f"p{len(panels) + 1:05d}"
                tile = image.crop((0, top, image.width, bottom))
                # Bound processing memory and maintain good OCR detail.
                if tile.width > 1800:
                    tile.thumbnail((1800, 4000))
                tile.save(out / f"{pid}.jpg", quality=94)
                panels.append({"id": pid, "page_id": page["id"], "chapter": page["chapter"],
                               "source": page["source"], "file": f"panels/{pid}.jpg",
                               "box": [0, top, image.width, bottom], "forced_cut": forced})
                if forced:
                    ctx.warn(f"{page['source']} 没有明显分格留白，已按连续视窗切分；原图全部保留。")
    write_json(folder / "panels.json", panels)
    return panels
