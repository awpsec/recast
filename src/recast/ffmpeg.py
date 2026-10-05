"""Finding ffmpeg, detecting which encoders actually work, parsing -progress output."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import EncoderCap
from .encode import ALL_ENCODERS

CANDIDATE_DIRS = [
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/usr/lib/jellyfin-ffmpeg",
    "C:\\ffmpeg\\bin", "C:\\Program Files\\ffmpeg\\bin",
    os.path.expandvars("%LOCALAPPDATA%\\Microsoft\\WinGet\\Links"),
]
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # don't flash consoles on Windows


def run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                          errors="replace")


def find_binaries() -> tuple[str, str]:
    """(ffmpeg, ffprobe) absolute paths, or "" if not found."""
    exe = ".exe" if sys.platform == "win32" else ""
    ff = shutil.which("ffmpeg") or ""
    if not ff:
        for d in CANDIDATE_DIRS:
            p = Path(d) / f"ffmpeg{exe}"
            if p.exists():
                ff = str(p)
                break
    if not ff:
        return "", ""
    probe = Path(ff).with_name(f"ffprobe{exe}")
    return ff, str(probe) if probe.exists() else (shutil.which("ffprobe") or "")


def version(ffmpeg: str) -> str:
    try:
        line = run([ffmpeg, "-hide_banner", "-version"], 10).stdout.splitlines()[0]
        return line.replace("ffmpeg version ", "").split(" Copyright")[0]
    except Exception:
        return ""


def listed_encoders(ffmpeg: str) -> set[str]:
    out = run([ffmpeg, "-hide_banner", "-encoders"], 15).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


def probe_args(enc: str) -> list[str]:
    """Extra args a test encode needs for this encoder."""
    if "vaapi" in enc:
        return ["-vaapi_device", "/dev/dri/renderD128"]
    return []


def probe_encoder(ffmpeg: str, enc: str, seconds: float = 2.0) -> EncoderCap:
    """Encode `seconds` of a 1080p test pattern and time it. Proves the hardware really works."""
    fps_in = 30
    pre = probe_args(enc)
    vf = ["-vf", "format=nv12,hwupload"] if "vaapi" in enc else []
    pix = [] if "vaapi" in enc else ["-pix_fmt", "nv12" if any(h in enc for h in ("qsv", "nvenc", "videotoolbox"))
                                     else "yuv420p"]
    speed = {"libx265": ["-preset", "medium"], "libx264": ["-preset", "medium"],
             "libsvtav1": ["-preset", "8"], "libaom-av1": ["-cpu-used", "8", "-row-mt", "1"]}.get(enc, [])
    t = 1.0 if enc == "libaom-av1" else seconds
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", *pre, "-f", "lavfi",
           "-i", f"testsrc2=size=1920x1080:rate={fps_in}", "-t", str(t), *vf, "-c:v", enc, *speed, *pix,
           "-f", "null", "-"]
    start = time.perf_counter()
    try:
        r = run(cmd, timeout=90)
    except subprocess.TimeoutExpired:
        return EncoderCap("failed", reason="test encode timed out")
    took = time.perf_counter() - start
    if r.returncode != 0:
        err = [l for l in r.stderr.strip().splitlines() if l.strip()]
        return EncoderCap("failed", reason=_short_reason(err[-1] if err else f"exit code {r.returncode}"))
    return EncoderCap("ok", fps=round(fps_in * t / max(took, 1e-3), 1))


def _short_reason(msg: str) -> str:
    m = msg.lower()
    if "no capable devices" in m or "cannot load libcuda" in m or "cuda" in m and "fail" in m:
        return "no NVIDIA GPU / driver"
    if "mfx" in m or "qsv" in m or "libmfx" in m or "vpl" in m:
        return "no usable Intel Quick Sync device"
    if "vaapi" in m or "renderd128" in m or "va_" in m:
        return "no VA-API device (/dev/dri/renderD128)"
    if "videotoolbox" in m:
        return "VideoToolbox refused this format"
    return msg[:90]


def detect_encoders(ffmpeg: str, on_result=None) -> dict[str, EncoderCap]:
    """Probe every encoder recast knows about. on_result(name, cap) is called as each finishes."""
    listed = listed_encoders(ffmpeg)
    caps: dict[str, EncoderCap] = {}
    for enc in ALL_ENCODERS:
        cap = probe_encoder(ffmpeg, enc) if enc in listed else EncoderCap("missing",
                                                                           reason="not built into this ffmpeg")
        caps[enc] = cap
        if on_result:
            on_result(enc, cap)
    return caps


def parse_progress(block: dict[str, str]) -> dict:
    """Turn one -progress key=value block into numbers."""
    def num(key: str, default: float = 0.0) -> float:
        try:
            return float(block.get(key, "").rstrip("x").replace("kbits/s", "").strip() or default)
        except ValueError:
            return default
    out_us = num("out_time_us", 0) or num("out_time_ms", 0)  # ffmpeg's out_time_ms is actually µs
    return {
        "frame": int(num("frame")),
        "fps": num("fps"),
        "kbps": num("bitrate"),
        "size": int(num("total_size")),
        "time": max(0.0, out_us / 1_000_000),
        "speed": num("speed"),
        "end": block.get("progress") == "end",
    }
