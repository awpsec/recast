"""Grabbing video frames with ffmpeg and putting two side by side (Pillow only — no UI toolkit)."""
from __future__ import annotations

import io
import subprocess

from PIL import Image

from .ffmpeg import NO_WINDOW


def load_image(path: str) -> Image.Image | None:
    try:
        with Image.open(path) as im:
            im.load()
            return im.convert("RGB")
    except Exception:  # half-written or missing: keep showing the last good frame
        return None


def grab_frame(ffmpeg: str, path: str, t: float, width: int = 640) -> Image.Image | None:
    """Decode one frame at t seconds (blocking; call from a thread)."""
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", path,
                            "-frames:v", "1", "-map", "0:V:0", "-vf", f"scale={width}:-2", "-f", "image2pipe",
                            "-c:v", "png", "-"], capture_output=True, timeout=60, creationflags=NO_WINDOW)
        return Image.open(io.BytesIO(r.stdout)).convert("RGB") if r.stdout else None
    except Exception:
        return None



def side_by_side(a: Image.Image | None, b: Image.Image | None) -> Image.Image | None:
    """Left half of `a` next to right half of `b`, with a divider: source │ encoded."""
    if not a or not b:
        return a or b
    b = b.resize(a.size)
    w, h = a.size
    out = a.copy()
    out.paste(b.crop((w // 2, 0, w, h)), (w // 2, 0))
    bar = max(2, w // 160)
    for y in range(h):
        for x in range(w // 2 - bar // 2, w // 2 + bar - bar // 2):
            out.putpixel((x, y), (235, 235, 245))
    return out
