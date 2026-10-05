"""Rendering video frames in the terminal with half-block characters (works in any truecolor terminal)."""
from __future__ import annotations

import io
import subprocess

from PIL import Image
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from ..ffmpeg import NO_WINDOW


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


class FrameView(Widget):
    """Shows a PIL image scaled to fit, letterboxed, two pixels per cell."""

    DEFAULT_CSS = "FrameView { height: 1fr; width: 1fr; }"

    def __init__(self, message: str = "", **kw):
        super().__init__(**kw)
        self.image: Image.Image | None = None
        self.message = message
        self._cache: tuple | None = None
        self._rows: list = []

    def set_image(self, image: Image.Image | None, message: str = "") -> None:
        self.image = image
        self.message = message or self.message
        self._cache = None
        self.refresh()

    def _layout(self):
        W, H = self.size.width, self.size.height * 2
        key = (W, H, id(self.image))
        if self._cache == key:
            return
        self._cache = key
        self._rows = []
        if not self.image or W < 2 or H < 2:
            return
        iw, ih = self.image.size
        scale = min(W / iw, H / ih)
        w, h = max(1, int(iw * scale)), max(2, int(ih * scale)) // 2 * 2
        im = self.image.resize((w, h), Image.BILINEAR)
        px = im.load()
        ox, oy = (W - w) // 2, ((H - h) // 2) // 2 * 2
        self._rows = [(ox, oy, w, h, px)]

    def render_line(self, y: int) -> Strip:
        W = self.size.width
        self._layout()
        if not self._rows:
            if y == self.size.height // 2 and self.message:
                msg = self.message[:W]
                pad = (W - len(msg)) // 2
                return Strip([Segment(" " * pad), Segment(msg, Style(color="#565f89")),
                              Segment(" " * (W - pad - len(msg)))], W)
            return Strip.blank(W)
        ox, oy, w, h, px = self._rows[0]
        py = y * 2 - oy
        if py < 0 or py >= h:
            return Strip.blank(W)
        segs = [Segment(" " * ox)] if ox else []
        for x in range(w):
            top = px[x, py] if 0 <= py < h else (0, 0, 0)
            bot = px[x, py + 1] if 0 <= py + 1 < h else (0, 0, 0)
            segs.append(Segment("▀", Style(color=Color.from_rgb(*top), bgcolor=Color.from_rgb(*bot))))
        if W - ox - w > 0:
            segs.append(Segment(" " * (W - ox - w)))
        return Strip(segs, W)


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
