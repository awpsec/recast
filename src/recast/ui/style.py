"""Shared formatting bits."""
from __future__ import annotations

from rich.text import Text
from textual.theme import Theme

# Monochrome, with colour only where it means something: green = saved/done, amber = needs you,
# red = failed/danger, blue = working/info. Same palette as the web app.
RECAST_THEME = Theme(
    name="recast", dark=True, primary="#8b8b92", secondary="#6e6e73", accent="#58a6ff", foreground="#e6e6e6",
    background="#0d0d0e", surface="#151517", panel="#1c1c1f", boost="#232327",
    success="#3fb950", warning="#d29922", error="#f85149",
)
SHADES = ["#d0d0d4", "#9a9aa1", "#6a6a71", "#45454b"]  # for breakdowns (codecs) where hue would imply meaning

BADGE = "bold #e6e6e6 on #303036"  # one neutral style: colour is reserved for meaning


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
    return Text(f" {codec} ", style=BADGE)


def pct_bar(p: float, width: int = 10) -> str:
    p = max(0.0, min(1.0, p))
    return "█" * int(p * width) + "░" * (width - int(p * width))
