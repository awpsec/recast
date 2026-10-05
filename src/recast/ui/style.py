"""Shared formatting bits."""
from __future__ import annotations

from rich.text import Text

CODEC_STYLE = {
    "AV1": "bold #1a1b26 on #bb9af7", "HEVC": "bold #1a1b26 on #9ece6a", "H.264": "bold #1a1b26 on #7aa2f7",
    "VC-1": "bold #1a1b26 on #f7768e", "MPEG-2": "bold #1a1b26 on #e0af68", "MPEG-4": "bold #1a1b26 on #e0af68",
    "VP9": "bold #1a1b26 on #7dcfff", "DivX": "bold #1a1b26 on #f7768e",
}


def fsize(b: float) -> str:
    if b >= 1024**4:
        return f"{b / 1024**4:.2f} TiB"
    if b >= 1024**3:
        return f"{b / 1024**3:.1f} GiB"
    if b >= 1024**2:
        return f"{b / 1024**2:.0f} MiB"
    return f"{b / 1024:.0f} KiB"


def fdur(s: float) -> str:
    s = int(max(0, s))
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def badge(codec: str) -> Text:
    return Text(f" {codec} ", style=CODEC_STYLE.get(codec, "bold #1a1b26 on #a9b1d6"))


def pct_bar(p: float, width: int = 10) -> str:
    p = max(0.0, min(1.0, p))
    return "█" * int(p * width) + "░" * (width - int(p * width))
