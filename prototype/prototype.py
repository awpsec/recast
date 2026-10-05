#!/usr/bin/env python3
"""recast — interactive UI prototype of a TUI ffmpeg re-encoder for a Plex library.

Everything here is mocked: the library, Sonarr/Radarr metadata and the encodes
themselves (time-compressed so a 24 min episode "encodes" in ~30 s).
"""
from __future__ import annotations

import json
import math
import random
import re
import shlex
import time
from dataclasses import asdict, dataclass, field, fields

from rich.color import Color
from rich.console import Group
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import (
    Button, DataTable, Footer, Input, Label, ListItem, ListView, ProgressBar,
    Select, Sparkline, Static, Switch, TabbedContent, TabPane, TextArea, Tree,
)

random.seed(7)
GiB, MiB = 1024**3, 1024**2
COPY_SIM, ENCODE_SIM, VERIFY_SIM, REPLACE_SIM = 4.0, 30.0, 2.0, 3.0
CADENCES = [("live", 0.125), ("0.5s", 0.5), ("1s", 1.0), ("5s", 5.0)]

CODEC_STYLE = {
    "AV1": "bold #1a1b26 on #bb9af7", "HEVC": "bold #1a1b26 on #9ece6a",
    "H.264": "bold #1a1b26 on #7aa2f7", "VC-1": "bold #1a1b26 on #f7768e",
    "MPEG-2": "bold #1a1b26 on #e0af68",
}
CODEC_KEY = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1"}
MAX_SCRATCH = 200 * 1024**3

# Per-machine profiles. In the real tool these come from first-run detection:
# `ffmpeg -encoders`, then a 2 s test encode per encoder to prove the hardware works.
MACHINES = {
    "mac": dict(
        key="mac", host="chris-mbp", os="macOS 26.0", hw="Apple M3 Pro", win=False,
        ffmpeg="ffmpeg 8.0 · /opt/homebrew/bin/ffmpeg", mount="/Volumes/media", mount_how="SMB",
        local="~/Media", scratch="/Volumes/FastSSD/recast-scratch", scratch_info="1.6 TB free · NVMe",
        enc={"hevc_videotoolbox": 320, "h264_videotoolbox": 410, "libx265": 58, "libx264": 140,
             "libsvtav1": 75, "libaom-av1": 4},
        failed={}, seen="this machine"),
    "pc": dict(
        key="pc", host="chris-desktop", os="Windows 11", hw="Ryzen 7 7800X3D · RTX 4070", win=True,
        ffmpeg="ffmpeg 8.0 (gyan.dev full) · C:\\ffmpeg\\bin\\ffmpeg.exe", mount="M:",
        mount_how="SMB \\\\nas.local\\media", local="D:\\Media", scratch="D:\\recast-scratch",
        scratch_info="1.1 TB free · NVMe",
        enc={"hevc_nvenc": 540, "h264_nvenc": 620, "av1_nvenc": 480, "libx265": 72, "libx264": 170,
             "libsvtav1": 95, "libaom-av1": 5},
        failed={e: "listed, but no Intel GPU (MFX init failed)" for e in ("hevc_qsv", "h264_qsv", "av1_qsv")},
        seen="last seen 2 h ago"),
    "server": dict(
        key="server", host="plex-box", os="Ubuntu 24.04", hw="Core i5-12500 · UHD 770", win=False,
        ffmpeg="jellyfin-ffmpeg 7.1 · /usr/lib/jellyfin-ffmpeg/ffmpeg", mount="/mnt/nas/media",
        mount_how="NFS", local="/srv/media", scratch="/srv/recast-scratch",
        scratch_info="410 GB free · SATA SSD",
        enc={"hevc_qsv": 280, "h264_qsv": 340, "hevc_vaapi": 250, "h264_vaapi": 300, "libx265": 40,
             "libx264": 110, "libsvtav1": 48, "libaom-av1": 2},
        failed={"av1_qsv": "UHD 770 has no AV1 encoder (needs Arc / Core Ultra)",
                **{e: "listed, but no NVIDIA device" for e in ("hevc_nvenc", "h264_nvenc", "av1_nvenc")}},
        seen="last seen 3 d ago"),
}
MACHINE = MACHINES["mac"]
CANON_NAS, CANON_LOCAL = "/mnt/nas/media", "~/Media"


def set_machine(key: str) -> None:
    global MACHINE
    MACHINE = MACHINES[key]


def local_path(p: str) -> str:
    """Map a canonical library path to where this machine sees it."""
    if p.startswith(CANON_NAS):
        p = MACHINE["mount"] + p[len(CANON_NAS):]
    elif p.startswith(CANON_LOCAL):
        p = MACHINE["local"] + p[len(CANON_LOCAL):]
    return p.replace("/", "\\") if MACHINE["win"] else p


def scratch(*parts: str) -> str:
    return ("\\" if MACHINE["win"] else "/").join([MACHINE["scratch"], *parts])


# ───────────────────────────── formatting ──────────────────────────────

def fsize(b: float) -> str:
    if b >= 1024**4:
        return f"{b / 1024**4:.2f} TiB"
    if b >= GiB:
        return f"{b / GiB:.1f} GiB"
    return f"{b / MiB:.0f} MiB"


def fdur(s: float) -> str:
    s = int(s)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def badge(codec: str) -> Text:
    return Text(f" {codec} ", style=CODEC_STYLE.get(codec, "reverse"))


def pct_bar(p: float, width: int = 10) -> str:
    full = int(p * width)
    return "█" * full + "░" * (width - full)


# ───────────────────────────── mock library ────────────────────────────

@dataclass
class Media:
    name: str
    label: str
    title: str
    path: str
    codec: str
    profile: str
    width: int
    height: int
    fps: float
    duration: float
    vkbps: int
    akbps: int
    audio: list
    subs: list
    hdr: str = ""
    remote: bool = True
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return int((self.vkbps + self.akbps) * 1000 / 8 * self.duration)

    @property
    def frames(self) -> int:
        return int(self.duration * self.fps)

    @property
    def res(self) -> str:
        return {2160: "2160p", 1080: "1080p", 720: "720p"}.get(self.height, f"{self.height}p")


@dataclass
class Folder:
    name: str
    path: str
    children: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    remote: bool = True

    def files(self):
        for c in self.children:
            if isinstance(c, Folder):
                yield from c.files()
            else:
                yield c

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files())


def make_eps(series, base, season, n, spec, titles=(), aired=(2017, 10, 3), remote=True):
    folder = Folder(f"Season {season:02}", f"{base}/Season {season:02}", remote=remote,
                    meta={"kind": "season"})
    y, mo, d = aired
    t0 = time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1))
    for e in range(1, n + 1):
        title = titles[e - 1] if e <= len(titles) else f"Episode {e}"
        tag = f"S{season:02}E{e:03}" if n > 99 else f"S{season:02}E{e:02}"
        fname = f"{series} - {tag} - {title}.mkv"
        jitter = random.uniform(0.9, 1.1)
        folder.children.append(Media(
            name=fname, label=f"{tag} · {title}", title=f"{series} · {tag} “{title}”",
            path=f"{folder.path}/{fname}", vkbps=int(spec["vkbps"] * jitter), remote=remote,
            meta={"source": "Sonarr", "episode": title, "quality": spec["quality"],
                  "aired": time.strftime("%Y-%m-%d", time.localtime(t0 + (e - 1) * 7 * 86400)),
                  "release": spec.get("group", "")},
            **{k: v for k, v in spec.items() if k not in ("vkbps", "quality", "group")},
        ))
    return folder


def movie(name, year, spec, quality, remote=True, base="/mnt/nas/media/Movies"):
    path = f"{base}/{name} ({year})"
    fname = f"{name} ({year}) {quality}.mkv"
    m = Media(name=fname, label=fname, title=f"{name} ({year})", path=f"{path}/{fname}",
              remote=remote, meta={"source": "Radarr", "quality": quality, "year": year}, **spec)
    return Folder(f"{name} ({year})", path, [m], remote=remote,
                  meta={"source": "Radarr", "kind": "movie", "quality": quality, "monitored": True})


def build_library() -> list[Folder]:
    bc_spec = dict(codec="AV1", profile="Main 10", width=1920, height=1080, fps=23.976,
                   duration=1420, vkbps=4283, akbps=384, hdr="",
                   audio=["jpn Opus 2.0", "eng Opus 5.1"], subs=["eng ASS (full)", "eng ASS (signs)"],
                   quality="WEBDL-1080p", group="AV1-Encoder")
    bc_titles = ["Asta and Yuno", "A Boy's Vow", "To the Royal Capital of the Clover Kingdom!"]
    bc = Folder("Black Clover (2017)", "/mnt/nas/media/TV/Black Clover (2017)", [
        make_eps("Black Clover", "/mnt/nas/media/TV/Black Clover (2017)", 1, 170, bc_spec, bc_titles),
        make_eps("Black Clover", "/mnt/nas/media/TV/Black Clover (2017)", 2, 3,
                 dict(bc_spec, codec="HEVC", profile="Main 10", vkbps=1850, quality="WEBDL-1080p",
                      group="SubsPlease"), aired=(2026, 9, 6)),
    ], meta={"source": "Sonarr", "kind": "series", "status": "Continuing", "profile": "HD-1080p",
             "monitored": True, "episodes": "S01 170/170 · S02 3/12 aired",
             "note": "Sonarr now maps the first 170 eps as S01 (TVDB re-order)."})
    frieren = Folder("Frieren - Beyond Journey's End (2023)",
                     "/mnt/nas/media/TV/Frieren - Beyond Journey's End (2023)", [
        make_eps("Frieren", "/mnt/nas/media/TV/Frieren - Beyond Journey's End (2023)", 1, 28,
                 dict(codec="H.264", profile="High 10", width=1920, height=1080, fps=23.976,
                      duration=1440, vkbps=7400, akbps=256, audio=["jpn AAC 2.0", "eng AAC 2.0"],
                      subs=["eng ASS"], quality="Bluray-1080p"), aired=(2023, 9, 29)),
    ], meta={"source": "Sonarr", "kind": "series", "status": "Continuing", "profile": "HD-1080p",
             "monitored": True, "episodes": "S01 28/28"})
    atla = Folder("Avatar - The Last Airbender (2005)",
                  "/mnt/nas/media/TV/Avatar - The Last Airbender (2005)", [
        make_eps("Avatar", "/mnt/nas/media/TV/Avatar - The Last Airbender (2005)", 1, 20,
                 dict(codec="MPEG-2", profile="Main", width=720, height=480, fps=29.97,
                      duration=1380, vkbps=6200, akbps=448, audio=["eng AC3 2.0"],
                      subs=["eng VobSub"], quality="DVD"), aired=(2005, 2, 21)),
    ], meta={"source": "Sonarr", "kind": "series", "status": "Ended", "profile": "SD",
             "monitored": True, "episodes": "S01 20/20"})
    sev = Folder("Severance (2022)", "/mnt/nas/media/TV/Severance (2022)", [
        make_eps("Severance", "/mnt/nas/media/TV/Severance (2022)", 2, 10,
                 dict(codec="HEVC", profile="Main 10", width=3840, height=2160, fps=23.976,
                      duration=3300, vkbps=15800, akbps=768, hdr="Dolby Vision + HDR10",
                      audio=["eng EAC3 Atmos 5.1"], subs=["eng SRT", "eng SRT (SDH)"],
                      quality="WEBDL-2160p"), aired=(2025, 1, 17)),
    ], meta={"source": "Sonarr", "kind": "series", "status": "Continuing", "profile": "UHD",
             "monitored": True, "episodes": "S02 10/10"})
    tv = Folder("TV", "/mnt/nas/media/TV", [atla, bc, frieren, sev], meta={"kind": "category"})
    movies = Folder("Movies", "/mnt/nas/media/Movies", [
        movie("Dune Part Two", 2024, dict(codec="HEVC", profile="Main 10", width=3840, height=2160,
              fps=23.976, duration=9960, vkbps=58000, akbps=4600, hdr="HDR10",
              audio=["eng TrueHD Atmos 7.1", "eng AC3 5.1"], subs=["eng PGS", "eng PGS (forced)"]),
              "Remux-2160p"),
        movie("Paprika", 2006, dict(codec="VC-1", profile="Advanced", width=1920, height=1080,
              fps=23.976, duration=5400, vkbps=24000, akbps=1500, audio=["jpn DTS-HD MA 5.1",
              "eng DTS-HD MA 5.1"], subs=["eng PGS"]), "Remux-1080p"),
        movie("Spirited Away", 2001, dict(codec="H.264", profile="High", width=1920, height=1036,
              fps=23.976, duration=7500, vkbps=11800, akbps=1500, audio=["jpn DTS-HD MA 5.1",
              "eng DTS-HD MA 5.1"], subs=["eng PGS"]), "Bluray-1080p"),
    ], meta={"kind": "category"})
    nas = Folder("NAS · nas.local/media", "/mnt/nas/media", [movies, tv], remote=True,
                 meta={"kind": "root", "access": "remote · SMB nas.local", "free": "3.4 TB of 22 TB free"})
    local_movies = Folder("Movies", "~/Media/Movies", [
        movie("The Thing", 1982, dict(codec="H.264", profile="High", width=1920, height=1040,
              fps=23.976, duration=6540, vkbps=29000, akbps=3500, audio=["eng DTS-HD MA 5.1"],
              subs=["eng PGS"]), "Remux-1080p", remote=False, base="~/Media/Movies"),
    ], remote=False, meta={"kind": "category"})
    local = Folder("Local · ~/Media", "~/Media", [local_movies], remote=False,
                   meta={"kind": "root", "access": "local disk", "free": "612 GB free"})
    return [nas, local]


# ───────────────────────────── encode settings ─────────────────────────

@dataclass
class EncodeSettings:
    codec: str = "hevc"
    resolution: str = "source"
    rate_mode: str = "bitrate"
    bitrate: int = 2000
    crf: int = 22
    audio: str = "copy"
    subs: str = "copy"
    container: str = "mkv"
    after: str = "ask"
    skip_same: bool = True
    encoder: str = "auto"
    speed: str = "medium"
    tune: str = "none"
    bit_depth: str = "10"
    maxrate: str = ""
    bufsize: str = ""
    two_pass: bool = False
    hdr: bool = True
    deinterlace: bool = False
    langs: str = ""
    extra: str = ""


ENCODERS = {
    "hevc": ["libx265", "hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "hevc_vaapi"],
    "h264": ["libx264", "h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_vaapi"],
    "av1": ["libsvtav1", "libaom-av1", "av1_nvenc", "av1_qsv"],
    "copy": ["copy"],
}
# "auto" picks the first working encoder in this order (hardware first: that's the point of auto).
AUTO_ORDER = {
    "hevc": ["hevc_nvenc", "hevc_videotoolbox", "hevc_qsv", "hevc_vaapi", "libx265"],
    "h264": ["h264_nvenc", "h264_videotoolbox", "h264_qsv", "h264_vaapi", "libx264"],
    "av1": ["av1_nvenc", "av1_qsv", "libsvtav1", "libaom-av1"],
    "copy": ["copy"],
}
SPEEDS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
SVT_PRESET = dict(zip(SPEEDS, ["12", "11", "10", "9", "8", "6", "5", "4", "2"]))
NV_PRESET = dict(zip(SPEEDS, ["p1", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p7"]))
QSV_PRESET = dict(zip(SPEEDS, ["veryfast", "veryfast", "veryfast", "faster", "fast", "medium", "slow",
                               "slower", "veryslow"]))
X265_FPS = dict(zip(SPEEDS, [190, 160, 130, 100, 80, 58, 30, 13, 6]))
HW = ("nvenc", "videotoolbox", "qsv", "vaapi")


def enc_status(e: str) -> str:
    if e == "copy" or e in MACHINE["enc"]:
        return "ok"
    return "failed" if e in MACHINE["failed"] else "missing"


def resolve_encoder(s: EncodeSettings) -> tuple[str, str]:
    """(encoder actually used on this machine, note)."""
    if s.codec == "copy":
        return "copy", ""
    best = next(e for e in AUTO_ORDER[s.codec] if enc_status(e) == "ok")
    if s.encoder == "auto":
        return best, "auto"
    if enc_status(s.encoder) == "ok":
        return s.encoder, ""
    return best, f"{s.encoder} isn't available on {MACHINE['host']} → using {best}"


def enc_options(codec: str) -> list[tuple[str, str]]:
    if codec == "copy":
        return [("copy", "copy")]
    best, _ = resolve_encoder(EncodeSettings(codec=codec, encoder="auto"))
    mark = {"ok": "  ✓", "failed": "  ✗ failed probe", "missing": "  ✗ not on this machine"}
    return [(f"auto → {best}", "auto")] + [(e + mark[enc_status(e)], e) for e in ENCODERS[codec]]


def default_presets() -> dict[str, dict]:
    S = EncodeSettings
    return {
        "HEVC 1080p · 2000k": dict(
            desc="Same streams, HEVC at 2000 kb/s, 1080p max. Encoder: best on whatever machine runs it.",
            s=S(codec="hevc", resolution="1080", rate_mode="bitrate", bitrate=2000)),
        "Anime → HEVC · quality": dict(
            desc="x265 CRF 20 slow + animation tune (pinned to CPU: best quality per GB).",
            s=S(codec="hevc", encoder="libx265", rate_mode="crf", crf=20, speed="slow", tune="animation",
                extra="-x265-params aq-mode=3:psy-rd=1.5:deblock=-1,-1")),
        "Fast hardware HEVC": dict(
            desc="NVENC / VideoToolbox / QSV, whichever this machine has. ~10x faster, ~20% bigger.",
            s=S(codec="hevc", encoder="auto", rate_mode="crf", crf=24, speed="slow")),
        "4K HDR HEVC · CRF 18": dict(
            desc="Keeps 2160p + HDR10 metadata; for remuxes eating the NAS.",
            s=S(codec="hevc", encoder="libx265", rate_mode="crf", crf=18, speed="slow", hdr=True,
                maxrate="25M", bufsize="50M", langs="eng")),
        "DVD rescue (MPEG-2 → HEVC)": dict(
            desc="Deinterlace + CRF 19 for old SD sources.",
            s=S(codec="hevc", encoder="libx265", rate_mode="crf", crf=19, speed="slow", deinterlace=True,
                tune="animation")),
        "H.264 max compat · 720p": dict(
            desc="For the family members on ancient TVs. MP4, stereo AAC, no subs.",
            s=S(codec="h264", resolution="720", rate_mode="crf", crf=21,
                audio="aac_stereo", subs="none", container="mp4", bit_depth="8")),
    }


def build_command(s: EncodeSettings, m: Media) -> list[list[str]]:
    enc, _ = resolve_encoder(s)
    src = scratch(m.name) if m.remote else local_path(m.path)
    out = scratch("out", f"{m.name.rsplit('.', 1)[0]}.{s.container}")
    ten = s.bit_depth == "10" and s.codec != "h264"
    g: list[list[str]] = [["ffmpeg", "-hide_banner", "-y"]]
    if "vaapi" in enc:
        g.append(["-vaapi_device", "/dev/dri/renderD128"])
    g.append(["-i", src])
    maps = ["-map", "0:v:0"]
    langs = [l.strip() for l in s.langs.split(",") if l.strip()]
    maps += sum((["-map", f"0:a:m:language:{l}?"] for l in langs), []) if langs else ["-map", "0:a?"]
    if s.subs == "copy":
        maps += ["-map", "0:s?"]
    elif s.subs == "forced":
        maps += ["-map", "0:s:disp:forced?"]
    g.append(maps)
    v: list[str] = []
    if s.codec == "copy":
        v = ["-c:v", "copy"]
    else:
        v = ["-c:v", enc]
        if enc in ("libx265", "libx264"):
            v += ["-preset", s.speed]
        elif enc == "libaom-av1":
            v += ["-cpu-used", "4"]
        elif enc == "libsvtav1":
            v += ["-preset", SVT_PRESET[s.speed]]
        elif "nvenc" in enc:
            v += ["-preset", NV_PRESET[s.speed], "-tune", "hq"]
        elif "qsv" in enc:
            v += ["-preset", QSV_PRESET[s.speed]]
        if s.rate_mode == "bitrate":
            v += ["-b:v", f"{s.bitrate}k"]
        elif "videotoolbox" in enc:
            v += ["-q:v", str(max(1, min(100, 100 - s.crf * 2)))]
        elif "nvenc" in enc:
            v += ["-rc", "vbr", "-cq", str(s.crf + 2), "-b:v", "0"]
        elif "qsv" in enc:
            v += ["-global_quality", str(s.crf + 2)]
        elif "vaapi" in enc:
            v += ["-rc_mode", "CQP", "-qp", str(s.crf + 2)]
        else:
            v += ["-crf", str(s.crf)]
        if s.maxrate:
            v += ["-maxrate", s.maxrate]
        if s.bufsize:
            v += ["-bufsize", s.bufsize]
        if s.tune != "none" and enc in ("libx265", "libx264"):
            v += ["-tune", s.tune]
        if "vaapi" not in enc:
            if any(h in enc for h in HW):
                v += ["-pix_fmt", "p010le" if ten else ("nv12" if "qsv" in enc else "yuv420p")]
            else:
                v += ["-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
        if s.two_pass and s.rate_mode == "bitrate" and enc.startswith("lib"):
            v += ["-pass", "2"]
    g.append(v)
    vf = []
    if s.deinterlace and s.codec != "copy":
        vf.append("bwdif=mode=send_field")
    if s.resolution != "source" and s.codec != "copy" and int(s.resolution) < m.height:
        vf.append(f"scale=-2:{s.resolution}:flags=lanczos")
    if "vaapi" in enc:
        vf.append(f"format={'p010' if ten else 'nv12'},hwupload")
    if vf:
        g.append(["-vf", ",".join(vf)])
    try:
        extra = shlex.split(s.extra)
    except ValueError:
        extra = [s.extra]
    x265 = []
    if "-x265-params" in extra:
        i = extra.index("-x265-params")
        if enc == "libx265":
            x265 += extra[i + 1:i + 2]
        del extra[i:i + 2]  # x265-only flags are dropped when this machine resolves to another encoder
    if s.hdr and m.hdr and enc == "libx265":
        x265.insert(0, "hdr10-opt=1:repeat-headers=1")
        g.append(["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"])
    if x265:
        g.append(["-x265-params", ":".join(x265)])
    a = {"copy": ["-c:a", "copy"], "aac_stereo": ["-c:a", "aac", "-ac", "2", "-b:a", "192k"],
         "opus": ["-c:a", "libopus", "-b:a", "96k", "-mapping_family", "1"]}[s.audio]
    if s.subs != "none":
        a += ["-c:s", "mov_text" if s.container == "mp4" else "copy"]
    g.append(a)
    if extra:
        g.append(extra)
    g.append(["-progress", "pipe:1", "-nostats", out])
    return g


def render_command(groups: list[list[str]]) -> Text:
    t = Text()
    for i, grp in enumerate(groups):
        t.append("  " if i else "")
        for j, tok in enumerate(grp):
            if j:
                t.append(" ")
            if re.fullmatch(r"[\w@%+=:,./\\*?-]+", tok):
                q = tok
            elif MACHINE["win"]:  # cmd.exe: plain double quotes, backslashes are literal
                q = '"' + tok.replace('"', '""') + '"'
            else:
                q = '"' + re.sub(r'(["\\$`])', r"\\\1", tok) + '"'
            if tok == "ffmpeg":
                t.append(q, "bold #ff9e64")
            elif tok.startswith("-") and not tok[1:2].isdigit():
                t.append(q, "#7dcfff")
            elif "/" in tok or "\\" in tok:
                t.append(q, "#9ece6a")
            else:
                t.append(q, "#c0caf5")
        if i < len(groups) - 1:
            t.append(" \\\n" if not MACHINE["win"] else " ^\n", "dim")
    return t


def estimate(s: EncodeSettings, m: Media) -> tuple[float, float]:
    """(video kbps, audio kbps) estimate for the output."""
    if s.codec == "copy":
        v = m.vkbps
    else:
        h = m.height if s.resolution == "source" else min(int(s.resolution), m.height)
        if s.rate_mode == "bitrate":
            v = s.bitrate
        else:
            base = 9500 if h > 1440 else 2600 if h > 800 else 1400 if h > 600 else 850
            eff = {"hevc": 1.0, "av1": 0.78, "h264": 1.6}[s.codec]
            crf = s.crf - 8 if s.codec == "av1" else s.crf
            v = base * eff * 2 ** ((22 - crf) / 6)
            if any(h in resolve_encoder(s)[0] for h in HW):
                v *= 1.2
        v = min(v, m.vkbps * 1.05)
    a = m.akbps if s.audio == "copy" else 192 if s.audio == "aac_stereo" else 96 * len(m.audio) * 2
    return v, a


def est_bytes(s: EncodeSettings, m: Media) -> float:
    v, a = estimate(s, m)
    return (v + a) * 1000 / 8 * m.duration


def display_fps(s: EncodeSettings, m: Media) -> float:
    """Expected fps on this machine, from the benchmark taken during detection."""
    if s.codec == "copy":
        return 2400
    enc, _ = resolve_encoder(s)
    fps = MACHINE["enc"].get(enc, 30)
    if enc in ("libx265", "libx264", "libsvtav1"):
        fps *= X265_FPS[s.speed] / X265_FPS["medium"]
    return fps * (1920 * 1080) / max(1, m.width * m.height)


def already_target(s: EncodeSettings, m: Media) -> bool:
    return s.skip_same and CODEC_KEY.get(s.codec) == m.codec and m.vkbps <= estimate(s, m)[0] * 1.15


# ───────────────────────────── jobs ────────────────────────────────────

@dataclass
class Job:
    id: int
    media: Media
    s: EncodeSettings
    preset: str
    preview: bool
    stage: str = "queued"
    copied: float = 0.0
    frame: float = 0.0
    out_bytes: float = 0.0
    fps: float = 0.0
    noise: float = 0.0
    stage_t: float = 0.0
    started: float = 0.0
    replace_p: float = 0.0
    vmaf: float | None = None
    waiting_since: str = ""
    batch: "Batch | None" = None
    flag: str = ""
    spark: list = field(default_factory=list)
    log: list = field(default_factory=list)
    _last_log: float = 0.0

    @property
    def copy_sim(self) -> float:
        return COPY_SIM if self.preview else 1.0

    @property
    def enc_sim(self) -> float:
        return ENCODE_SIM if self.preview else 5.0

    @property
    def progress(self) -> float:
        return min(1.0, self.frame / max(1, self.media.frames))

    @property
    def projected(self) -> float:
        return self.out_bytes / self.progress if self.progress > 0.02 else est_bytes(self.s, self.media)

    @property
    def video_time(self) -> float:
        return self.frame / self.media.fps


@dataclass
class Batch:
    """One approval for a whole folder: approve once, the rest auto-replace as they pass checks."""
    id: int
    folder: Folder
    s: EncodeSettings
    preset: str
    jobs: list = field(default_factory=list)
    approved: bool = False
    waiting_since: str = ""
    vmaf: float | None = None
    announced: bool = False

    def of(self, *stages):
        return [j for j in self.jobs if j.stage in stages]

    @property
    def done(self):
        return self.of("awaiting", "to_replace", "replacing", "replaced")

    @property
    def remaining(self):
        return self.of("queued", "copying", "ready", "encoding", "paused", "verifying")

    @property
    def held_bytes(self) -> float:
        return sum(j.out_bytes for j in self.of("awaiting"))


ACTIVE = ("queued", "copying", "ready", "encoding", "paused", "verifying")

STAGE_STYLE = {
    "queued": ("◌ queued", "dim"), "copying": ("⇣ copying", "#7dcfff"),
    "ready": ("◌ in scratch", "#7dcfff"), "encoding": ("● encoding", "bold #e0af68"),
    "paused": ("❚❚ paused", "#e0af68"), "verifying": ("◎ verifying", "#7aa2f7"),
    "awaiting": ("⚑ awaiting approval", "bold #bb9af7"), "replacing": ("⇡ replacing", "#7dcfff"),
    "to_replace": ("⇡ queued for replace", "#7dcfff"),
    "replaced": ("✓ replaced", "#9ece6a"), "discarded": ("✗ discarded", "dim"),
    "kept": ("✓ kept both", "#9ece6a"), "skipped": ("↷ skipped", "dim"),
    "cancelled": ("✗ cancelled", "dim #f7768e"),
}


# ───────────────────────────── frame preview ───────────────────────────

def _lerp(a, b, k):
    k = max(0.0, min(1.0, k))
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k)


def scene(x: float, y: float, W: int, H: int, t: float, reflect: bool = False):
    """Procedural stand-in for a decoded frame (sunset over water)."""
    u, v = x / W, y / H
    hz = 0.60
    if v >= hz and not reflect:
        rv = hz - (v - hz) * 1.25
        u2 = u + 0.012 * math.sin(v * 90 + t * 3)
        c = scene(u2 * W, rv * H, W, H, t, True)
        k = 0.55 + 0.1 * math.sin(v * 140 + t * 2)
        boat_u = ((t * 0.05) % 1.3) - 0.15
        if abs(u - boat_u) < 0.05 - (v - 0.70) * 0.6 and 0.66 < v < 0.70:
            return (12, 10, 22)
        return (c[0] * k * 0.8, c[1] * k * 0.9, c[2] * k + 25)
    m1 = hz - 0.12 - 0.06 * math.sin(u * 7 + t * 0.15) - 0.035 * math.sin(u * 17 + 1.3 + t * 0.2)
    m2 = hz - 0.03 - 0.05 * math.sin(u * 5 + 2 + t * 0.35) - 0.02 * math.sin(u * 23 + t * 0.5)
    if v > m2:
        return _lerp((40, 22, 55), (18, 10, 30), (v - m2) * 8)
    if v > m1:
        return _lerp((95, 55, 110), (60, 35, 85), (v - m1) * 6)
    k = v / hz
    c = _lerp((18, 22, 68), (120, 60, 140), k * 1.4)
    c = _lerp(c, (255, 130, 90), (k - 0.45) * 2.2)
    sx, sy, r = 0.68 + 0.04 * math.sin(t * 0.07), hz - 0.17, 0.12
    dx, dy = (u - sx) * W / H, v - sy
    d = math.hypot(dx, dy)
    if d < r:
        if dy > 0 and int(dy * 90) % 3 == 0:
            return c
        return _lerp((255, 236, 140), (255, 120, 100), (dy + r) / (2 * r))
    glow = max(0.0, 0.22 - (d - r)) * 2.2
    c = _lerp(c, (255, 190, 130), glow)
    cloud = math.sin(u * 9 + t * 0.4 + math.sin(v * 30)) * math.sin(v * 38)
    if 0.18 < v < 0.38 and cloud > 0.72:
        c = _lerp(c, (255, 160, 170), 0.45)
    h = (int(x) * 73856093 ^ int(y) * 19349663) & 0xFFFF
    if v < 0.3 and h < 90 and (h + int(t * 10)) % 7:
        c = _lerp(c, (255, 255, 255), 0.8)
    return c


class FramePreview(Widget):
    DEFAULT_CSS = "FramePreview { height: 1fr; width: 1fr; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.t: float | None = None
        self.split = False
        self._rows = None
        self._key = None

    def show(self, t: float | None, split: bool) -> None:
        self.t, self.split = t, split
        self._rows = None
        self.refresh()

    def _build(self):
        W, H = self.size.width, self.size.height * 2
        st = (self.t or 0) * 0.04
        rows = [[scene(x, y, W, H, st) for x in range(W)] for y in range(H)]
        if self.split:
            mid = W // 2
            for y in range(0, H - 1, 2):
                for x in range(mid + 1, W - 1, 2):
                    blk = [rows[y][x], rows[y][x + 1], rows[y + 1][x], rows[y + 1][x + 1]]
                    avg = tuple(int(sum(p[i] for p in blk) / 4) // 20 * 20 + 10 for i in range(3))
                    rows[y][x] = rows[y][x + 1] = rows[y + 1][x] = rows[y + 1][x + 1] = avg
            for y in range(H):
                rows[y][mid] = (230, 230, 240)
        self._rows = rows
        self._key = (W, H, self.t, self.split)

    def render_line(self, y: int) -> Strip:
        W = self.size.width
        if self.t is None:
            if y == self.size.height // 2:
                msg = "no active encode — pick a file in Library and press p"
                pad = max(0, (W - len(msg)) // 2)
                return Strip([Segment(" " * pad), Segment(msg, Style(color="#565f89")),
                              Segment(" " * max(0, W - pad - len(msg)))], W)
            return Strip.blank(W)
        if self._rows is None or self._key[:2] != (W, self.size.height * 2):
            self._build()
        if 2 * y + 1 >= len(self._rows):
            return Strip.blank(W)
        top, bot = self._rows[2 * y], self._rows[2 * y + 1]
        segs = [Segment("▀", Style(color=Color.from_rgb(*map(int, top[x])),
                                   bgcolor=Color.from_rgb(*map(int, bot[x])))) for x in range(W)]
        return Strip(segs, W)


# ───────────────────────────── modals ──────────────────────────────────

class EncodeDialog(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"),
                Binding("ctrl+a", "toggle_adv", "Advanced")]

    def __init__(self, target, presets: dict, preset_name: str, settings: EncodeSettings | None = None):
        super().__init__()
        self.target = target
        self.presets = presets
        self.preset_name = preset_name
        self.files = [target] if isinstance(target, Media) else list(target.files())
        self.sample = self.files[0]
        self.initial = settings or presets[preset_name]["s"]

    def compose(self) -> ComposeResult:
        s = self.initial
        sel = lambda i, opts, val: Select(opts, value=val, allow_blank=False, id=i)
        with Vertical(id="enc-box"):
            yield Static(id="enc-title")
            with VerticalScroll(id="enc-body"):
                with Horizontal(id="preset-row"):
                    yield Label("Preset", classes="lbl")
                    yield sel("f-preset", [("✎ Custom (on the fly)", "__custom__")] +
                              [(k, k) for k in self.presets], self.preset_name)
                    yield Static(id="preset-state")
                yield Static("── Basic ─────────────────────────────", classes="section")
                with Grid(classes="grid4"):
                    yield Label("Video codec", classes="lbl")
                    yield sel("f-codec", [("HEVC / H.265", "hevc"), ("H.264", "h264"),
                                          ("AV1", "av1"), ("Copy (remux only)", "copy")], s.codec)
                    yield Label("Resolution", classes="lbl")
                    yield sel("f-res", [("Same as source", "source"), ("2160p", "2160"),
                                        ("1080p", "1080"), ("720p", "720"), ("480p", "480")], s.resolution)
                    yield Label("Rate control", classes="lbl")
                    yield sel("f-rate", [("Target bitrate", "bitrate"), ("Constant quality", "crf")],
                              s.rate_mode)
                    yield Label("Bitrate kb/s", classes="lbl")
                    yield Input(str(s.bitrate), id="f-bitrate", type="integer")
                    yield Label("Audio", classes="lbl")
                    yield sel("f-audio", [("Copy all tracks", "copy"), ("AAC stereo", "aac_stereo"),
                                          ("Opus (keep channels)", "opus")], s.audio)
                    yield Label("Quality (CRF)", classes="lbl")
                    yield Input(str(s.crf), id="f-crf", type="integer")
                    yield Label("Subtitles", classes="lbl")
                    yield sel("f-subs", [("Copy all", "copy"), ("Forced only", "forced"),
                                         ("Drop", "none")], s.subs)
                    yield Label("Container", classes="lbl")
                    yield sel("f-container", [("MKV", "mkv"), ("MP4", "mp4")], s.container)
                    yield Label("After encode", classes="lbl")
                    yield sel("f-after", [("Ask once for the batch" if len(self.files) > 1
                                           else "Ask me (approval inbox)", "ask"),
                                          ("Auto-replace if checks pass", "auto"),
                                          ("Keep both files", "keep")], s.after)
                    yield Label("Skip if done", classes="lbl")
                    with Horizontal(classes="sw"):
                        yield Switch(s.skip_same, id="f-skip")
                        yield Label("skip files already in target codec", classes="hint")
                with Horizontal(id="adv-row"):
                    yield Button("▸ Advanced  ^a", id="adv-btn", variant="default")
                with Vertical(id="adv"):
                    yield Static("── Advanced ──────────────────────────", classes="section")
                    with Grid(classes="grid4"):
                        yield Label("Encoder", classes="lbl")
                        yield sel("f-encoder", enc_options(s.codec), s.encoder if s.codec != "copy" else "copy")
                        yield Label("Speed preset", classes="lbl")
                        yield sel("f-speed", [(p, p) for p in SPEEDS], s.speed)
                        yield Label("Tune", classes="lbl")
                        yield sel("f-tune", [(p, p) for p in ["none", "animation", "grain", "film",
                                                              "fastdecode"]], s.tune)
                        yield Label("Bit depth", classes="lbl")
                        yield sel("f-depth", [("10-bit", "10"), ("8-bit", "8")], s.bit_depth)
                        yield Label("Max rate", classes="lbl")
                        yield Input(s.maxrate, placeholder="e.g. 4M", id="f-maxrate")
                        yield Label("Buffer size", classes="lbl")
                        yield Input(s.bufsize, placeholder="e.g. 8M", id="f-bufsize")
                        yield Label("Two-pass", classes="lbl")
                        yield Switch(s.two_pass, id="f-twopass")
                        yield Label("HDR passthru", classes="lbl")
                        yield Switch(s.hdr, id="f-hdr")
                        yield Label("Deinterlace", classes="lbl")
                        yield Switch(s.deinterlace, id="f-deint")
                        yield Label("Audio langs", classes="lbl")
                        yield Input(s.langs, placeholder="jpn,eng (blank = all)", id="f-langs")
                    with Horizontal(id="extra-row"):
                        yield Label("Extra ffmpeg args", classes="lbl")
                        yield Input(s.extra, placeholder="-x265-params aq-mode=3  (anything goes)",
                                    id="f-extra")
                yield Static("── ffmpeg command (live) ─────────────", classes="section")
                yield Static(id="cmd")
                yield Static(id="est")
            with Horizontal(id="enc-buttons"):
                yield Button("▶ Preview 1 file", id="go-preview", variant="primary")
                if len(self.files) > 1:
                    yield Button(f"⏵⏵ Encode all {len(self.files)}", id="go-all", variant="warning")
                yield Button("Save as preset…", id="save-preset")
                yield Button("Cancel", id="cancel", variant="error")

    def on_mount(self) -> None:
        self.query_one("#adv").display = False
        t = self.target
        if isinstance(t, Media):
            head = Text.assemble(("Encode  ", "bold"), (t.title, "bold #c0caf5"), "   ",
                                 badge(t.codec), f" {t.res} · {fsize(t.size)}",
                                 ("  (remote → copies to scratch)" if t.remote else "  (local)", "dim"))
        else:
            codecs = sorted({f.codec for f in self.files})
            head = Text.assemble(("Encode  ", "bold"), (local_path(t.path), "bold #c0caf5"), "   ",
                                 f"{len(self.files)} files · {fsize(t.size)} · ", *[badge(c) for c in codecs])
        self.query_one("#enc-title", Static).update(head)
        self.update_preview()

    def read(self) -> EncodeSettings:
        q = lambda i: self.query_one(f"#{i}")
        num = lambda i, d: int(q(i).value) if q(i).value.strip().lstrip("-").isdigit() else d
        return EncodeSettings(
            codec=q("f-codec").value, resolution=q("f-res").value, rate_mode=q("f-rate").value,
            bitrate=num("f-bitrate", 2000), crf=num("f-crf", 22), audio=q("f-audio").value,
            subs=q("f-subs").value, container=q("f-container").value, after=q("f-after").value,
            skip_same=q("f-skip").value, encoder=q("f-encoder").value, speed=q("f-speed").value,
            tune=q("f-tune").value, bit_depth=q("f-depth").value, maxrate=q("f-maxrate").value,
            bufsize=q("f-bufsize").value, two_pass=q("f-twopass").value, hdr=q("f-hdr").value,
            deinterlace=q("f-deint").value, langs=q("f-langs").value, extra=q("f-extra").value)

    def load(self, s: EncodeSettings) -> None:
        q = lambda i: self.query_one(f"#{i}")
        for i, val in [("f-codec", s.codec), ("f-res", s.resolution), ("f-rate", s.rate_mode),
                       ("f-audio", s.audio), ("f-subs", s.subs), ("f-container", s.container),
                       ("f-after", s.after), ("f-speed", s.speed), ("f-tune", s.tune),
                       ("f-depth", s.bit_depth)]:
            q(i).value = val
        enc = q("f-encoder")
        enc.set_options(enc_options(s.codec))
        enc.value = s.encoder if s.codec != "copy" else "copy"
        for i, val in [("f-bitrate", str(s.bitrate)), ("f-crf", str(s.crf)), ("f-maxrate", s.maxrate),
                       ("f-bufsize", s.bufsize), ("f-langs", s.langs), ("f-extra", s.extra)]:
            q(i).value = val
        for i, val in [("f-skip", s.skip_same), ("f-twopass", s.two_pass), ("f-hdr", s.hdr),
                       ("f-deint", s.deinterlace)]:
            q(i).value = val
        if s.extra or s.encoder != "auto" or s.tune != "none" or s.maxrate:
            self.set_adv(True)

    def set_adv(self, on: bool) -> None:
        self.query_one("#adv").display = on
        self.query_one("#adv-btn", Button).label = ("▾ Advanced  ^a" if on else "▸ Advanced  ^a")

    def action_toggle_adv(self) -> None:
        self.set_adv(not self.query_one("#adv").display)

    def update_preview(self) -> None:
        try:
            s = self.read()
        except Exception:
            return
        self.query_one("#f-bitrate").disabled = s.rate_mode != "bitrate" or s.codec == "copy"
        self.query_one("#f-crf").disabled = s.rate_mode != "crf" or s.codec == "copy"
        name = self.query_one("#f-preset").value
        st = self.query_one("#preset-state", Static)
        if name == "__custom__":
            st.update(Text("on-the-fly settings", style="#e0af68"))
        elif asdict(self.presets[name]["s"]) != asdict(s):
            st.update(Text("● modified from preset", style="#e0af68"))
        else:
            st.update(Text(self.presets[name]["desc"], style="dim"))
        self.query_one("#cmd", Static).update(render_command(build_command(s, self.sample)))
        m = self.sample
        per = est_bytes(s, m)
        todo = [f for f in self.files if not already_target(s, f)]
        tot_src = sum(f.size for f in todo)
        tot_out = sum(est_bytes(s, f) for f in todo)
        e = Text()
        e.append("Estimate  ", "bold")
        e.append(f"{fsize(m.size)} → ≈{fsize(per)} ")
        e.append(f"({(per / m.size - 1) * 100:+.0f}%)", "bold #9ece6a" if per < m.size else "bold #f7768e")
        enc, note = resolve_encoder(s)
        e.append(f" for this file\n          ")
        e.append(f"{enc}", "bold #7dcfff")
        e.append(f" on {MACHINE['host']}" + (" (auto)" if note == "auto" else ""))
        e.append(f" · ~{display_fps(s, m):.0f} fps · ≈{fdur(m.frames / display_fps(s, m))} per file")
        if note and note != "auto":
            e.append(f"\n          ⚠ {note}", "#e0af68")
        if len(self.files) > 1:
            e.append(f"\n          {len(todo)}/{len(self.files)} files to encode · {fsize(tot_src)} → "
                     f"≈{fsize(tot_out)} · saves ≈{fsize(tot_src - tot_out)}", "#7dcfff")
            if len(todo) < len(self.files):
                e.append(f"  ({len(self.files) - len(todo)} skipped: already target codec)", "dim")
        self.query_one("#est", Static).update(e)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-codec":
            enc = self.query_one("#f-encoder", Select)
            cur = enc.value
            enc.set_options(enc_options(event.value))
            valid = ["copy"] if event.value == "copy" else ["auto", *ENCODERS[event.value]]
            enc.value = cur if cur in valid else valid[0]
        if event.select.id == "f-preset" and event.value != "__custom__":
            self.load(self.presets[event.value]["s"])
        self.update_preview()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.update_preview()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.update_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        preset = self.query_one("#f-preset").value
        if bid == "adv-btn":
            self.action_toggle_adv()
        elif bid == "cancel":
            self.dismiss(None)
        elif bid in ("go-preview", "go-all"):
            s = self.read()
            label = preset if preset != "__custom__" else "custom"
            if preset != "__custom__" and asdict(self.presets[preset]["s"]) != asdict(s):
                label += "*"
            files = [self.sample] if bid == "go-preview" else self.files
            self.dismiss(dict(files=files, s=s, preset=label, preview=bid == "go-preview",
                              folder=self.target if bid == "go-all" else None))
        elif bid == "save-preset":
            def saved(name):
                if name:
                    self.presets[name] = dict(desc="Saved from the encode dialog.", s=self.read())
                    sel = self.query_one("#f-preset", Select)
                    sel.set_options([("✎ Custom (on the fly)", "__custom__")] + [(k, k) for k in self.presets])
                    sel.value = name
                    self.app.notify(f"Saved preset “{name}”", title="Presets")
                    self.app.refresh_presets()
            self.app.push_screen(NamePrompt(), saved)


class NamePrompt(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="name-box"):
            yield Label("Preset name")
            yield Input(placeholder="e.g. Black Clover HEVC CRF 21", id="name-in")
            with Horizontal():
                yield Button("Save", variant="primary", id="ok")
                yield Button("Cancel", id="no")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value.strip() or None if event.button.id == "ok" else None)


def compare_table(j: Job) -> Table:
    m, s = j.media, j.s
    t = Table(box=None, padding=(0, 2), header_style="bold")
    t.add_column("")
    t.add_column("Source", style="#a9b1d6")
    t.add_column("Output", style="#c0caf5")
    t.add_column("")
    out_h = m.height if s.resolution == "source" else min(int(s.resolution), m.height)
    out_w = int(m.width * out_h / m.height) // 2 * 2
    out_kbps = j.out_bytes * 8 / 1000 / m.duration - estimate(s, m)[1]
    ok = Text("✓", style="bold #9ece6a")
    t.add_row("Codec", Text.assemble(badge(m.codec), f" {m.profile}"),
              Text.assemble(badge(CODEC_KEY.get(s.codec, m.codec)), f" {resolve_encoder(s)[0]}"), "")
    t.add_row("Resolution", f"{m.width}×{m.height}", f"{out_w}×{out_h}", "")
    t.add_row("Video bitrate", f"{m.vkbps:,} kb/s", f"{out_kbps:,.0f} kb/s", "")
    saved = 1 - j.out_bytes / m.size
    t.add_row("Size", fsize(m.size), Text(f"{fsize(j.out_bytes)}  ({-saved * 100:+.0f}%)",
              style="bold #9ece6a" if saved > 0 else "bold #f7768e"), "")
    t.add_row("Duration", fdur(m.duration), fdur(m.duration), ok)
    n_a = len(m.audio) if not s.langs else len(m.audio)
    t.add_row("Audio", f"{len(m.audio)} tracks", f"{n_a} tracks ({'copied' if s.audio == 'copy' else s.audio})", ok)
    n_s = {"copy": len(m.subs), "forced": 0, "none": 0}[s.subs]
    t.add_row("Subtitles", f"{len(m.subs)} tracks", f"{n_s} tracks", ok if n_s == len(m.subs) else Text("!", style="#e0af68"))
    t.add_row("Decode test", "", "full pass, 0 errors", ok)
    if j.flag:
        t.add_row(Text("Flag", style="bold #e0af68"), "", Text(j.flag, style="#e0af68"), Text("⚑", style="#e0af68"))
    t.add_row("VMAF", "", Text(f"{j.vmaf:.1f}" if j.vmaf else "press v to compute",
                               style="bold #9ece6a" if j.vmaf else "dim"), ok if j.vmaf else "")
    return t


class ApprovalPrompt(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss('later')", "Decide later"),
                Binding("y", "dismiss('approve')", "Approve"),
                Binding("n", "dismiss('deny')", "Deny"),
                Binding("r", "dismiss('retry')", "Retry")]

    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        j = self.job
        with Vertical(id="appr-box"):
            yield Static(Text.assemble(("⚑ Encode finished — replace in library?\n", "bold #bb9af7"),
                                       (j.media.title, "bold"), ("\n" + j.media.path, "dim")))
            yield Static(compare_table(j))
            yield Static(Text("Approve copies the new file back to the NAS, moves the original to "
                              ".recast-trash/ (14 days), and asks Sonarr to rescan.\nClosing this "
                              "keeps it in the Approvals inbox. Nothing times out.", style="dim"))
            with Horizontal(id="appr-box-btns"):
                yield Button("✓ Approve & replace  y", id="approve", variant="success")
                yield Button("✗ Deny & discard  n", id="deny", variant="error")
                yield Button("↻ Retry with…  r", id="retry", variant="warning")
                yield Button("Later  esc", id="later")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        t = Table(box=None, padding=(0, 2), show_header=False)
        t.add_column(style="bold #7dcfff")
        t.add_column()
        for k, d in [("1-5", "switch tabs (or click them)"), ("↑ ↓ ← → / mouse", "browse the library tree"),
                     ("p", "preview-encode the highlighted file (or first file of a folder)"),
                     ("e", "encode highlighted file / whole folder"),
                     ("f", "toggle frame preview"), ("s", "split view: source │ encoded"),
                     ("[  ]", "frame preview cadence (live · 0.5s · 1s · 5s)"),
                     ("space", "pause / resume the active encode"), ("c", "cancel highlighted job"),
                     ("y / n / r", "approve / deny / retry in Approvals"), ("v", "compute VMAF for approval"),
                     ("ctrl+a", "advanced settings (in encode dialog)"), ("ctrl+p", "command palette / themes"),
                     ("q", "quit")]:
            t.add_row(k, d)
        with Vertical(id="help-box"):
            yield Static(Text("recast — keys", style="bold"))
            yield Static(t)
            yield Static(Text("\nThis is a UI prototype: library, metadata and encodes are simulated.",
                              style="dim italic"))


# ───────────────────────────── list items ──────────────────────────────

class JobItem(ListItem):
    def __init__(self, job: Job):
        self.job = job
        m = job.media
        super().__init__(Label(Text.assemble(("⚑ ", "#bb9af7"), (m.label[:38], "bold"), "\n  ",
                                             badge(m.codec), " → ", badge(CODEC_KEY.get(job.s.codec, m.codec)),
                                             (f"  {job.waiting_since}", "dim"))))


class PresetItem(ListItem):
    def __init__(self, name: str, default: bool):
        self.preset_name = name
        super().__init__(Label(Text.assemble(("★ " if default else "  ", "#e0af68"), name)))


def batch_label(b: Batch) -> str:
    return " / ".join(b.folder.path.split("/")[-2:])


def batch_view(b: Batch) -> Group:
    done, rem = b.done, b.remaining
    todo = [j for j in b.jobs if j.stage not in ("skipped", "cancelled", "discarded")]
    src = sum(j.media.size for j in done)
    out = sum(j.out_bytes for j in done)
    all_src = sum(j.media.size for j in todo) or 1
    ratio = out / src if src else sum(est_bytes(b.s, j.media) for j in todo) / all_src
    flagged = [j for j in done if j.flag]
    held = b.held_bytes
    status = Text()
    if rem:
        fps = display_fps(b.s, rem[0].media)
        eta = sum(j.media.frames - j.frame for j in rem) / fps
        status.append(f"encoding {len(done)}/{len(todo)}", "bold #e0af68")
        status.append(f" · ETA ≈{fdur(eta)} on {MACHINE['host']} · ")
    else:
        status.append(f"all {len(todo)} encoded · waiting for your OK", "bold #bb9af7")
        status.append(" · ")
    status.append(f"scratch holding {fsize(held)} of {fsize(MAX_SCRATCH)}", "#f7768e" if held > MAX_SCRATCH * 0.8 else "dim")
    t = Table(box=None, padding=(0, 2), header_style="bold")
    t.add_column("")
    t.add_column("Source", style="#a9b1d6")
    t.add_column("Output", style="#c0caf5")
    dur = sum(j.media.duration for j in done) or 1
    t.add_row("Files", f"{len(done)} done", Text.assemble(f"{len(done) - len(flagged)} pass", (" · ", "dim"),
              (f"{len(flagged)} flagged", "bold #e0af68" if flagged else "dim")))
    t.add_row("Size so far", fsize(src), Text(f"{fsize(out)}  ({(ratio - 1) * 100:+.0f}%)", style="bold #9ece6a"))
    t.add_row("Whole batch", fsize(all_src), Text.assemble(f"≈{fsize(all_src * ratio)}  ",
              (f"saves ≈{fsize(all_src * (1 - ratio))}", "bold #9ece6a")))
    t.add_row("Avg bitrate", f"{src * 8 / 1000 / dur:,.0f} kb/s" if done else "—",
              f"{out * 8 / 1000 / dur:,.0f} kb/s" if done else "—")
    t.add_row("Encoder", "", f"{resolve_encoder(b.s)[0]} on {MACHINE['host']}")
    t.add_row("VMAF", "", Text(f"{b.vmaf:.1f} (10 sampled files)" if b.vmaf else "press v (samples 10 files)",
                               style="bold #9ece6a" if b.vmaf else "dim"))
    f = Table(box=None, padding=(0, 2), header_style="bold dim")
    for c in ("File", "Source", "Output", "Δ", "Check"):
        f.add_column(c)
    rows = flagged + [j for j in reversed(done) if not j.flag]
    for j in rows[:14]:
        d = j.out_bytes / j.media.size - 1
        f.add_row(j.media.label[:30], fsize(j.media.size), fsize(j.out_bytes),
                  Text(f"{d * 100:+.0f}%", style="#9ece6a" if d < 0 else "#f7768e"),
                  Text(f"⚑ {j.flag}", style="#e0af68") if j.flag else Text("✓", style="#9ece6a"))
    more = Text(f"… and {len(rows) - 14} more" if len(rows) > 14 else "", style="dim")
    n_pass = len(done) - len(flagged)
    foot = Text(f"✓ Approve batch → replaces the {n_pass} passing file{'s' * (n_pass != 1)} now"
                + (", then auto-replaces the rest as each passes checks" if rem else "")
                + ".\n  Flagged files drop into this inbox one by one; they never auto-replace.\n"
                "✗ Deny → discards the outputs and cancels what's left. Originals are never touched.",
                style="dim")
    return Group(Text.assemble(("▤ ", "#bb9af7"), (batch_label(b), "bold #c0caf5"), "  ",
                               (f"batch · {len(todo)} files · {b.preset}", "#e0af68")),
                 Text(local_path(b.folder.path), style="dim"), status, Text(""), t, Text(""), f, more,
                 Text(""), foot)


class BatchItem(ListItem):
    def __init__(self, batch: Batch):
        self.batch = batch
        self.lbl = Label(self.text())
        super().__init__(self.lbl)

    def text(self) -> Text:
        b = self.batch
        todo = [j for j in b.jobs if j.stage not in ("skipped", "cancelled", "discarded")]
        done = len(b.done)
        flagged = sum(1 for j in b.done if j.flag)
        t = Text.assemble(("▤ ", "#bb9af7"), (batch_label(b)[:38], "bold"), "\n  ")
        t.append(f"{done}/{len(todo)} ", "#e0af68" if b.remaining else "#9ece6a")
        t.append(pct_bar(done / max(1, len(todo)), 8), "#e0af68" if b.remaining else "#9ece6a")
        if flagged:
            t.append(f" ⚑{flagged}", "#e0af68")
        t.append(f"  {b.waiting_since}", "dim")
        return t

    def refresh_label(self) -> None:
        self.lbl.update(self.text())


class SetupScreen(ModalScreen):
    """First-run detection. Real version: probes ffmpeg, encoders, mounts, disks, *arr."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Skip")]

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.t0 = 0.0

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-box"):
            yield Static(Text.assemble(("◆ recast", "bold #bb9af7"), ("   first-run setup · detecting this machine",
                                                                       "bold")))
            with Horizontal(id="setup-sim"):
                yield Label("demo: pretend this is", classes="lbl2")
                yield Select([("Mac · chris-mbp", "mac"), ("PC · chris-desktop (NVIDIA)", "pc"),
                              ("Server · plex-box (Intel iGPU)", "server")],
                             value=self.key, allow_blank=False, id="setup-machine")
            yield Static(id="setup-log")
            with Horizontal(id="setup-btns"):
                yield Button("✓ Save machine profile", id="setup-save", variant="success", disabled=True)
                yield Button("↻ Re-run detection", id="setup-rerun")
                yield Button("Skip  esc", id="setup-skip")

    def on_mount(self) -> None:
        self.restart()
        self.set_interval(0.1, self.render_log)
        self.query_one("#setup-skip", Button).focus()

    def restart(self) -> None:
        self.t0 = time.monotonic()
        self.query_one("#setup-save", Button).disabled = True

    def on_select_changed(self, event: Select.Changed) -> None:
        self.key = event.value
        self.restart()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "setup-rerun":
            self.restart()
        elif bid == "setup-save":
            self.dismiss(self.key)
        elif bid == "setup-skip":
            self.dismiss(None)

    def render_log(self) -> None:
        mc = MACHINES[self.key]
        el = time.monotonic() - self.t0
        spin = "◐◓◑◒"[int(el * 8) % 4]
        out = Text()
        t = 0.0

        def step(title: str, dur: float, result) -> bool:
            nonlocal t
            start, t = t, t + dur
            if el < start:
                out.append(f"  ○ {title}\n", "dim")
                return False
            if el < t:
                out.append(f"  {spin} {title}…\n", "bold #e0af68")
                return False
            out.append("  ✓ ", "bold #9ece6a")
            out.append_text(result if isinstance(result, Text) else Text(result))
            out.append("\n")
            return True

        k = lambda s: (f"{s:<15}", "#7dcfff")
        step("Identifying machine", 0.5, Text.assemble(k("Machine"), f"{mc['host']} · {mc['os']} · {mc['hw']}"))
        step("Looking for ffmpeg", 0.5, Text.assemble(k("ffmpeg"), mc["ffmpeg"], ("  · ffprobe ✓", "dim")))
        step("Looking for shared config on the NAS", 0.6, Text.assemble(
            k("Shared config"), "nas.local/media/.recast/ ", ("· 6 presets · 2 library roots · same on every "
                                                               "machine", "dim")))
        encs = [e for c in ("hevc", "h264", "av1") for e in ENCODERS[c]]
        probe_t = 0.22 * len(encs)
        start = t
        if step("Probing encoders (2 s test encode each)", probe_t, Text.assemble(
                k("Encoders"), (f"{len(mc['enc'])} working", "bold"),
                ("  ffmpeg -f lavfi -i testsrc2=s=1920x1080:r=24 -t 2 -c:v <enc> -f null -", "dim"))) or el > start:
            shown = encs[:max(0, int((el - start) / 0.22) + 1)]
            for e in shown:
                out.append(f"      {e:<20}")
                if e in mc["enc"]:
                    out.append(f"✓ works  {mc['enc'][e]:>4} fps @1080p\n", "#9ece6a")
                elif e in mc["failed"]:
                    out.append(f"✗ {mc['failed'][e]}\n", "#f7768e")
                else:
                    out.append("– not built into this ffmpeg\n", "dim")
        step("Finding the library on this machine", 0.6, Text.assemble(
            k("NAS"), "root “NAS media” (nas.local/media) → ", (mc["mount"], "bold #9ece6a"),
            (f"  ({mc['mount_how']}) · 353.5 GiB visible", "dim")))
        step("Picking a scratch disk", 0.5, Text.assemble(
            k("Scratch"), (mc["scratch"], "bold #9ece6a"), (f"  ({mc['scratch_info']}) · fastest local disk with room",
                                                            "dim")))
        step("Contacting Sonarr / Radarr", 0.5, Text.assemble(
            k("Sonarr/Radarr"), "nas.local:8989 ✓ · nas.local:7878 ✓",
            ("  · keys from shared config · /tv → " + mc["mount"] + ("\\TV" if mc["win"] else "/TV"), "dim")))
        if el > t:
            prev = MACHINE
            set_machine(self.key)
            best = {c: resolve_encoder(EncodeSettings(codec=c, encoder="auto"))[0] for c in ("hevc", "h264", "av1")}
            set_machine(prev["key"])
            out.append("\n  Defaults on ", "bold")
            out.append(mc["host"], "bold #bb9af7")
            out.append(":  ")
            for c, e in best.items():
                out.append_text(badge(CODEC_KEY[c]))
                out.append(f" {e}   ")
            out.append("\n  Presets set to “auto” use these here; presets pinned to an encoder keep it "
                       "(and fall back if this machine lacks it).", "dim")
            self.query_one("#setup-save", Button).disabled = False
        self.query_one("#setup-log", Static).update(out)


# ───────────────────────────── app ─────────────────────────────────────

CSS = """
Screen { background: $background; }
#topbar { dock: top; height: 1; background: $panel; padding: 0 1; }
TabbedContent, TabPane { height: 1fr; }
TabPane { padding: 0; }
.pane { border: round $primary 50%; border-title-color: $accent; border-title-style: bold; }
.pane:focus-within { border: round $accent; }
#lib-tree { width: 52%; padding: 0 1; }
#lib-right { width: 1fr; }
#details { height: 1fr; padding: 0 1; }
#lib-actions, #appr-actions, #preset-actions { height: 3; padding: 0 1; }
#lib-actions Button, #appr-actions Button, #preset-actions Button, #enc-buttons Button,
#appr-box-btns Button { margin-right: 1; min-width: 10; }
#jobs { height: 11; }
#queue-bottom { height: 1fr; }
#job-detail { width: 1fr; padding: 0 1; }
#frame-panel { width: 1.25fr; }
#frame-caption { height: 1; padding: 0 1; background: $boost; }
#jd-title, #jd-pipeline { height: auto; }
#jd-pipeline { margin: 1 0; }
.pbrow { height: 1; }
.pblbl { width: 8; color: $text-muted; }
.pbrow ProgressBar { width: 1fr; }
.pbrow Bar { width: 1fr; }
.pbinfo { height: 1; color: $text-muted; padding-left: 8; margin-bottom: 1; }
#jd-stats { height: auto; margin-bottom: 1; }
#jd-spark-lbl { height: 1; color: $text-muted; }
#jd-spark { height: 3; }
#jd-log { height: 1fr; color: $text-muted; margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
#appr-list, #preset-list { width: 44; }
#appr-right, #preset-right { width: 1fr; }
#appr-scroll { height: 1fr; }
#appr-detail { height: auto; padding: 0 1; }
#machines { height: 6; margin-bottom: 1; }
#setup-box { width: 120; max-width: 98%; height: auto; max-height: 96%; border: thick $accent;
             background: $surface; padding: 1 2; }
#setup-sim { height: 3; margin: 1 0; }
#setup-sim Select { width: 44; }
.lbl2 { padding-top: 1; color: $text-muted; width: 24; }
#setup-log { height: auto; }
#setup-btns { height: 3; margin-top: 1; }
#setup-btns Button { margin-right: 1; }
#preset-help { height: auto; padding: 0 1; color: $text-muted; }
#preset-editor { height: 1fr; }
#settings-scroll { padding: 0 2; }
.set-section { margin-top: 1; color: $accent; text-style: bold; }
.set-row { height: 3; }
.set-row Label { width: 26; padding-top: 1; color: $text-muted; }
.set-row Input { width: 1fr; }
.set-row Select { width: 1fr; }
.set-row Button { margin-left: 1; }
.set-row Switch { margin-right: 2; }
#roots { height: 6; margin: 0 0 1 0; }

EncodeDialog, ApprovalPrompt, NamePrompt, HelpScreen, SetupScreen { align: center middle; background: $background 70%; }
#enc-box { width: 118; max-width: 98%; height: 96%; border: thick $accent; background: $surface; padding: 0 1; }
#enc-title { height: 2; padding-top: 1; }
#enc-body { height: 1fr; }
#preset-row { height: 3; }
#preset-row Select { width: 46; }
#preset-state { padding: 1 0 0 2; width: 1fr; }
.section { color: $accent; margin-top: 1; }
.grid4 { grid-size: 4; grid-columns: 15 1fr 15 1fr; grid-rows: 3; grid-gutter: 0 1; height: auto; }
.lbl { padding-top: 1; color: $text-muted; width: 15; }
.sw { height: 3; }
.hint { padding: 1 0 0 1; color: $text-muted; }
#adv-row { height: 3; margin-top: 1; }
#adv { height: auto; }
#extra-row { height: 3; }
#extra-row Input { width: 1fr; }
#cmd { background: $boost; padding: 1 2; height: auto; }
#est { padding: 1 0 0 0; height: auto; }
#enc-buttons { height: 3; dock: bottom; margin-bottom: 1; }
#appr-box { width: 96; max-width: 98%; height: auto; border: thick $secondary; background: $surface; padding: 1 2; }
#appr-box Static { margin-bottom: 1; }
#appr-box-btns { height: 3; }
#name-box { width: 60; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
#name-box Horizontal { height: 3; margin-top: 1; }
#help-box { width: 84; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
"""


class Recast(App):
    TITLE = "recast"
    CSS = CSS
    BINDINGS = [
        Binding("p", "preview", "Preview encode"),
        Binding("e", "encode", "Encode…"),
        Binding("f", "toggle_frame", "Frame preview"),
        Binding("s", "split", "Split compare"),
        Binding("space", "pause", "Pause"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        Binding("left_square_bracket", "cadence(-1)", "Slower", show=False),
        Binding("right_square_bracket", "cadence(1)", "Faster", show=False),
        Binding("c", "cancel_job", "Cancel job", show=False),
        Binding("y", "approve", "Approve", show=False),
        Binding("n", "deny", "Deny", show=False),
        Binding("r", "retry", "Retry", show=False),
        Binding("v", "vmaf", "VMAF", show=False),
        Binding("i", "probe", "Probe", show=False),
        *[Binding(str(i + 1), f"tab('{t}')", show=False) for i, t in
          enumerate(["tab-library", "tab-queue", "tab-approvals", "tab-presets", "tab-settings"])],
    ]

    def __init__(self, machine: str = "mac", setup: bool = True):
        super().__init__()
        set_machine(machine)
        self.run_setup = setup
        self.roots = build_library()
        self.presets = default_presets()
        self.default_preset = "HEVC 1080p · 2000k"
        self.jobs: list[Job] = []
        self.approvals: list = []  # Job | Batch
        self.next_id = 1
        self.frame_on = True
        self.split = False
        self.cadence = 2
        self.selected_job: Job | None = None
        self._frame_timer = None
        self._last_frame = 0.0
        self._seed()

    # ── seed a "previous session" so the inbox shows persistence ──
    def _seed(self):
        atla = next(f for f in self.roots[0].files() if f.codec == "MPEG-2")
        pap = next(f for f in self.roots[0].files() if f.codec == "VC-1")
        fr = next(f for f in self.roots[0].files() if f.codec == "H.264")
        for m, preset, when in [(atla, "DVD rescue (MPEG-2 → HEVC)", "yesterday 23:14"),
                                (pap, "Anime → HEVC · quality", "Sat 10:02")]:
            j = self.make_job(m, self.presets[preset]["s"], preset, True)
            j.stage, j.frame = "awaiting", m.frames
            j.out_bytes = est_bytes(j.s, m) * random.uniform(0.9, 1.05)
            j.waiting_since = f"waiting since {when}"
            self.approvals.append(j)
        # a whole-season batch that finished overnight and is waiting for one decision
        season = next(c for c in self.walk_folders() if c.path.endswith("(2023)/Season 01"))
        preset = "Anime → HEVC · quality"
        b = Batch(self.next_id, season, self.presets[preset]["s"], preset, waiting_since="since 04:12")
        for m in season.files():
            j = self.make_job(m, b.s, preset, False)
            j.batch, j.stage, j.frame = b, "awaiting", m.frames
            j.out_bytes = est_bytes(j.s, m) * random.uniform(0.85, 1.1)
            b.jobs.append(j)
        b.jobs[16].flag = "audio stream count differs (2 → 1)"
        b.announced = True
        self.approvals.append(b)

    def walk_folders(self, folders=None):
        for f in folders if folders is not None else self.roots:
            yield f
            yield from self.walk_folders([c for c in f.children if isinstance(c, Folder)])

    def make_job(self, m: Media, s: EncodeSettings, preset: str, preview: bool) -> Job:
        j = Job(self.next_id, m, s, preset, preview)
        self.next_id += 1
        self.jobs.append(j)
        return j

    # ── layout ──
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with TabbedContent(initial="tab-library", id="tabs"):
            with TabPane("① Library", id="tab-library"):
                with Horizontal():
                    yield Tree("library", id="lib-tree", classes="pane")
                    with Vertical(id="lib-right"):
                        yield Static(id="details", classes="pane")
                        with Horizontal(id="lib-actions"):
                            yield Button("▶ Preview encode  p", id="btn-preview", variant="primary")
                            yield Button("⏵⏵ Encode…  e", id="btn-encode", variant="warning")
                            yield Button("ⓘ Probe  i", id="btn-probe")
            with TabPane("② Queue", id="tab-queue"):
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True, classes="pane")
                with Horizontal(id="queue-bottom"):
                    with Vertical(id="job-detail", classes="pane"):
                        yield Static(id="jd-title")
                        yield Static(id="jd-pipeline")
                        with Horizontal(classes="pbrow"):
                            yield Label("Copy", classes="pblbl")
                            yield ProgressBar(total=100, show_eta=False, id="pb-copy")
                        yield Static(id="jd-copyinfo", classes="pbinfo")
                        with Horizontal(classes="pbrow"):
                            yield Label("Encode", classes="pblbl")
                            yield ProgressBar(total=100, show_eta=False, id="pb-enc")
                        yield Static(id="jd-encinfo", classes="pbinfo")
                        yield Static(id="jd-stats")
                        yield Static("bitrate over time", id="jd-spark-lbl")
                        yield Sparkline([0], id="jd-spark")
                        yield Static(id="jd-log")
                    with Vertical(id="frame-panel", classes="pane"):
                        yield Static(id="frame-caption")
                        yield FramePreview(id="frame")
            with TabPane("③ Approvals", id="tab-approvals"):
                with Horizontal():
                    yield ListView(id="appr-list", classes="pane")
                    with Vertical(id="appr-right"):
                        with VerticalScroll(id="appr-scroll", classes="pane"):
                            yield Static(id="appr-detail")
                        with Horizontal(id="appr-actions"):
                            yield Button("✓ Approve & replace  y", id="a-approve", variant="success")
                            yield Button("✗ Deny  n", id="a-deny", variant="error")
                            yield Button("↻ Retry…  r", id="a-retry", variant="warning")
                            yield Button("VMAF  v", id="a-vmaf")
                            yield Button("▶ mpv side-by-side", id="a-mpv")
                            yield Button("Approve all passing", id="a-all")
            with TabPane("④ Presets", id="tab-presets"):
                with Horizontal():
                    yield ListView(id="preset-list", classes="pane")
                    with Vertical(id="preset-right"):
                        yield Static(Text.assemble(
                            "Presets live in ", ("~/.config/recast/presets/*.json", "#9ece6a"),
                            ". Edit here or in $EDITOR. Every field the dialog has, plus ",
                            ("extra", "#7dcfff"), " for raw ffmpeg flags (filters, -x265-params, "
                            "-svtav1-params, stream maps…)."), id="preset-help")
                        yield TextArea.code_editor("", language="json", id="preset-editor", classes="pane")
                        with Horizontal(id="preset-actions"):
                            yield Button("Save", id="p-save", variant="primary")
                            yield Button("Duplicate", id="p-dup")
                            yield Button("★ Set default", id="p-default")
                            yield Button("Delete", id="p-del", variant="error")
            with TabPane("⑤ Settings", id="tab-settings"):
                with VerticalScroll(id="settings-scroll"):
                    yield Static("Machines", classes="set-section")
                    yield Static(Text("Each machine keeps its own encoders, mount points and scratch disk. "
                                      "Presets, library roots and Sonarr/Radarr live in the shared config on "
                                      "the NAS (nas.local/media/.recast/), so every machine sees the same ones.",
                                      style="dim"))
                    yield DataTable(id="machines", cursor_type="row")
                    with Horizontal(classes="set-row"):
                        yield Button("↻ Re-run detection on this machine", id="s-detect", variant="primary")
                    yield Static("Library roots", classes="set-section")
                    yield DataTable(id="roots", cursor_type="row")
                    with Horizontal(classes="set-row"):
                        yield Button("+ Add root", id="s-addroot")
                        yield Button("Path mapping…", id="s-map")
                    yield Static("Scratch", classes="set-section")
                    with Horizontal(classes="set-row"):
                        yield Label("Scratch directory")
                        yield Input(MACHINE["scratch"], id="s-scratch")
                    with Horizontal(classes="set-row"):
                        yield Label("Max scratch usage (GiB)")
                        yield Input("200", type="integer")
                    with Horizontal(classes="set-row"):
                        yield Label("Prefetch next file")
                        yield Switch(True)
                        yield Label("copy 1 file ahead while encoding (never more)")
                    yield Static("Integrations", classes="set-section")
                    for name, url in [("Sonarr", "http://nas.local:8989"), ("Radarr", "http://nas.local:7878")]:
                        with Horizontal(classes="set-row"):
                            yield Label(f"{name} URL / API key")
                            yield Input(url)
                            yield Input("3f9c2e7a1b8d4c6e", password=True)
                            yield Button("Test", id=f"s-test-{name.lower()}")
                    with Horizontal(classes="set-row"):
                        yield Label("After replace")
                        yield Switch(True)
                        yield Label("trigger rescan + rename in Sonarr/Radarr")
                    yield Static("Encoding", classes="set-section")
                    with Horizontal(classes="set-row"):
                        yield Label("Default preset")
                        yield Select([(k, k) for k in self.presets], value=self.default_preset,
                                     allow_blank=False, id="s-default")
                    with Horizontal(classes="set-row"):
                        yield Label("Concurrent encodes")
                        yield Select([("1", 1), ("2", 2), ("3", 3)], value=1, allow_blank=False)
                    with Horizontal(classes="set-row"):
                        yield Label("Originals on replace")
                        yield Select([("Move to .recast-trash/ (14 days)", "trash"),
                                      ("Keep alongside as .orig", "keep"), ("Delete immediately", "delete")],
                                     value="trash", allow_blank=False)
                    yield Static(id="s-encoders", classes="set-row")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        tree = self.query_one("#lib-tree", Tree)
        tree.border_title = "Library"
        tree.show_root = False
        tree.guide_depth = 3
        focus_node = None
        for root in self.roots:
            n = tree.root.add(self.folder_label(root), data=root, expand=True)
            r = self._add(n, root)
            focus_node = focus_node or r
        self.query_one("#details").border_title = "Details"
        jobs = self.query_one("#jobs", DataTable)
        jobs.border_title = "Jobs"
        for label, key, w in [("#", "id", 3), ("File", "file", 36), ("Preset", "preset", 26),
                              ("Stage", "stage", 20), ("Progress", "prog", 16), ("FPS", "fps", 5),
                              ("Size", "size", 22), ("ETA", "eta", 9)]:
            jobs.add_column(label, key=key, width=w)
        for j in self.jobs:
            self.add_job_row(j)
        self.query_one("#job-detail").border_title = "Job"
        self.query_one("#frame-panel").border_title = "Frame preview  f"
        self.query_one("#appr-list").border_title = "Awaiting approval"
        self.query_one("#appr-scroll").border_title = "Review"
        self.query_one("#preset-list").border_title = "Presets"
        self.query_one("#preset-editor").border_title = "preset.json"
        for c in ["Name", "Share", "Access", "Mounted on this machine", "Arr root"]:
            self.query_one("#roots", DataTable).add_column(c)
        for c in ["", "Host", "OS · hardware", "HEVC (auto)", "NAS mounted at", "Scratch", ""]:
            self.query_one("#machines", DataTable).add_column(c)
        self.fill_machine_views()
        self.refresh_approvals()
        self.refresh_presets()
        self.set_interval(0.2, self.tick)
        self.set_frame_timer()
        if focus_node:
            self.show_details(focus_node.data)
            self.call_after_refresh(tree.move_cursor, focus_node)
        tree.focus()
        self.tick()
        if self.run_setup:
            self.push_screen(SetupScreen(MACHINE["key"]), self.apply_machine)

    def apply_machine(self, key: str | None) -> None:
        if not key:
            return
        set_machine(key)
        self.fill_machine_views()
        node = self.query_one("#lib-tree", Tree).cursor_node
        if node and node.data is not None:
            self.show_details(node.data)
        self.notify(f"Profile saved for {MACHINE['host']}. Encodes here use "
                    f"{resolve_encoder(EncodeSettings(codec='hevc'))[0]} for HEVC.", title="✓ Machine ready")

    def fill_machine_views(self) -> None:
        mt = self.query_one("#machines", DataTable)
        mt.clear()
        cur = MACHINE["key"]
        for k, mc in MACHINES.items():
            set_machine(k)
            best = resolve_encoder(EncodeSettings(codec="hevc"))[0]
            set_machine(cur)
            here = k == cur
            mt.add_row(Text("●" if here else "○", style="#9ece6a" if here else "dim"),
                       Text(mc["host"], style="bold" if here else ""), f"{mc['os']} · {mc['hw']}",
                       Text(best, style="#7dcfff"), mc["mount"], mc["scratch"],
                       Text("this machine" if here else mc["seen"].replace("this machine", "last seen today"),
                            style="#9ece6a" if here else "dim"))
        rt = self.query_one("#roots", DataTable)
        rt.clear()
        rt.add_row("NAS media", "nas.local/media", f"remote · {MACHINE['mount_how'].split()[0]}", MACHINE["mount"],
                   "Sonarr /tv · Radarr /movies")
        rt.add_row("Local", "(per machine)", "local", MACHINE["local"], "—")
        self.query_one("#s-scratch", Input).value = MACHINE["scratch"]
        t = Text.assemble((f"Encoders on {MACHINE['host']}  ", "bold"))
        for e in sorted({e for c in ("hevc", "h264", "av1") for e in ENCODERS[c]}):
            st = enc_status(e)
            if st == "ok":
                t.append(f"{e} ✓  ", "#9ece6a")
            elif st == "failed":
                t.append(f"{e} ✗  ", "#f7768e")
        self.query_one("#s-encoders", Static).update(t)

    def _add(self, node, folder: Folder):
        focus = None
        for c in folder.children:
            if isinstance(c, Folder):
                expand = c.name in ("TV", "Black Clover (2017)")
                n = node.add(self.folder_label(c), data=c, expand=expand)
                if c.path.endswith("Black Clover (2017)/Season 01"):
                    focus = n
                focus = self._add(n, c) or focus
            else:
                node.add_leaf(self.file_label(c), data=c)
        return focus

    def folder_label(self, f: Folder) -> Text:
        files = list(f.files())
        t = Text(f.name, style="bold" if f.meta.get("kind") in ("root", "category") else "")
        t.append(f"  {fsize(f.size)}", "dim")
        codecs = sorted({x.codec for x in files})
        if f.meta.get("kind") not in ("root", "category"):
            for c in codecs:
                t.append(" ")
                t.append_text(badge(c))
        if f.meta.get("source"):
            t.append(f"  ◆{f.meta['source'][0]}", "#7dcfff")
        return t

    def file_label(self, m: Media) -> Text:
        t = Text(m.label[:34].ljust(35))
        t.append_text(badge(m.codec))
        t.append(f" {m.res:>5} {fsize(m.size):>9}", "dim")
        return t

    # ── details pane ──
    def show_details(self, data) -> None:
        d = self.query_one("#details", Static)
        preset = self.presets[self.default_preset]["s"]
        if isinstance(data, Media):
            m = data
            g = Table.grid(padding=(0, 2))
            g.add_column(style="#565f89", width=9)
            g.add_column()
            g.add_row("Video", Text.assemble(badge(m.codec), f" {m.profile} · {m.width}×{m.height} · "
                                             f"{m.fps:g} fps · {m.vkbps:,} kb/s" + (f" · {m.hdr}" if m.hdr else "")))
            g.add_row("Audio", "  ".join(f"{i + 1} {a}" for i, a in enumerate(m.audio)))
            g.add_row("Subs", "  ".join(f"{i + 1} {s}" for i, s in enumerate(m.subs)))
            g.add_row("Length", f"{fdur(m.duration)}  ·  {m.frames:,} frames")
            g.add_row("Size", fsize(m.size))
            g.add_row("Where", Text(local_path(m.path), style="#9ece6a"))
            g.add_row("Access", "NAS (remote) → copies to scratch first" if m.remote else "local disk — encodes in place")
            meta = m.meta
            src = Text()
            src.append(f"◆ {meta['source']}  ", "bold #7dcfff")
            if "episode" in meta:
                src.append(f"“{meta['episode']}” · aired {meta['aired']} · {meta['quality']}")
            else:
                src.append(f"{meta['quality']} · {meta['year']} · monitored")
            est = est_bytes(preset, m)
            e = Text.assemble(("With ", "dim"), (self.default_preset, "#e0af68"), (": ", "dim"),
                              f"{fsize(m.size)} → ≈{fsize(est)} ",
                              (f"({(est / m.size - 1) * 100:+.0f}%)", "bold #9ece6a" if est < m.size else "bold #f7768e"))
            d.update(Group(Text(m.title, style="bold #c0caf5"), src, Text(""), g, Text(""), e))
        elif isinstance(data, Folder):
            f = data
            files = list(f.files())
            total = sum(x.size for x in files) or 1
            by: dict[str, int] = {}
            for x in files:
                by[x.codec] = by.get(x.codec, 0) + x.size
            parts = [Text(f.name, style="bold #c0caf5"), Text(local_path(f.path), style="#9ece6a")]
            if f.meta.get("source"):
                mt = f.meta
                info = Text(f"◆ {mt['source']}  ", style="bold #7dcfff")
                info.append(" · ".join(str(mt[k]) for k in ("status", "profile", "episodes", "quality")
                                       if k in mt))
                parts.append(info)
                if mt.get("note"):
                    parts.append(Text(f"ⓘ {mt['note']}", style="#e0af68"))
            if f.meta.get("access"):
                parts.append(Text(f"{f.meta['access']} · {f.meta['free']}", style="dim"))
            parts.append(Text(""))
            parts.append(Text(f"{len(files)} files · {fsize(total)}", style="bold"))
            bar = Text()
            W = 50
            for c, b in sorted(by.items(), key=lambda kv: -kv[1]):
                n = max(1, round(b / total * W))
                bar.append("█" * n, CODEC_STYLE.get(c, "").split(" on ")[-1])
            parts.append(bar)
            leg = Text()
            for c, b in sorted(by.items(), key=lambda kv: -kv[1]):
                leg.append_text(badge(c))
                leg.append(f" {fsize(b)}  ")
            parts.append(leg)
            todo = [x for x in files if not already_target(preset, x)]
            est = sum(est_bytes(preset, x) for x in todo)
            src_b = sum(x.size for x in todo)
            parts.append(Text(""))
            parts.append(Text.assemble(("With ", "dim"), (self.default_preset, "#e0af68"), (": ", "dim"),
                                       f"{len(todo)} files to encode · {fsize(src_b)} → ≈{fsize(est)} · ",
                                       (f"saves ≈{fsize(src_b - est)}", "bold #9ece6a")))
            parts.append(Text("\np preview one file first · e encode the whole folder", style="dim"))
            d.update(Group(*parts))

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self.show_details(event.node.data)

    # ── actions ──
    def check_action(self, action: str, parameters) -> bool | None:
        if action == "quit":
            return True
        if len(self.screen_stack) > 1:
            return False
        return True

    def current_target(self):
        node = self.query_one("#lib-tree", Tree).cursor_node
        return node.data if node else None

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def open_encode(self, target, preview_only: bool, settings=None, preset=None, on_done=None) -> None:
        if target is None:
            return
        if preview_only and isinstance(target, Folder):
            target = next(target.files())

        def done(res):
            if not res:
                return
            if on_done:
                on_done()
            self.enqueue(res)
        self.push_screen(EncodeDialog(target, self.presets, preset or self.default_preset, settings), done)

    def enqueue(self, res: dict) -> None:
        first = None
        skipped = 0
        batch = None
        if len(res["files"]) > 1:
            batch = Batch(self.next_id, res["folder"], res["s"], res["preset"])
        for m in res["files"]:
            j = self.make_job(m, res["s"], res["preset"], res["preview"])
            j.batch = batch
            if batch:
                batch.jobs.append(j)
            if batch and already_target(res["s"], m):
                j.stage = "skipped"
                skipped += 1
            first = first or j
            self.add_job_row(j)
        self.selected_job = first
        self.action_tab("tab-queue")
        tbl = self.query_one("#jobs", DataTable)
        tbl.move_cursor(row=tbl.get_row_index(str(first.id)))
        n = len(res["files"]) - skipped
        self.notify(f"{'Preview' if res['preview'] else 'Queued'}: {n} file{'s' * (n != 1)} with {res['preset']}",
                    title="Queue")

    def action_preview(self) -> None:
        self.open_encode(self.current_target(), True)

    def action_encode(self) -> None:
        self.open_encode(self.current_target(), False)

    def action_probe(self) -> None:
        t = self.current_target()
        if isinstance(t, Media):
            self.notify(f"ffprobe -v error -show_streams -show_format \"{t.path}\"\n(cached; re-probed in 0.3s)",
                        title="Probe")

    def action_toggle_frame(self) -> None:
        self.frame_on = not self.frame_on
        self.query_one("#frame-panel").display = self.frame_on

    def action_split(self) -> None:
        self.split = not self.split
        self._last_frame = 0
        self.update_frame(force=True)

    def action_cadence(self, d: int) -> None:
        self.cadence = max(0, min(len(CADENCES) - 1, self.cadence - d))
        self.set_frame_timer()
        self.notify(f"Frame preview every {CADENCES[self.cadence][0]}", timeout=2)

    def set_frame_timer(self) -> None:
        if self._frame_timer:
            self._frame_timer.stop()
        self._frame_timer = self.set_interval(CADENCES[self.cadence][1], self.update_frame)

    def action_pause(self) -> None:
        for j in self.jobs:
            if j.stage in ("encoding", "paused"):
                j.stage = "paused" if j.stage == "encoding" else "encoding"
                self.notify("Encode paused (SIGSTOP)" if j.stage == "paused" else "Resumed", timeout=2)
                return

    def action_cancel_job(self) -> None:
        j = self.selected_job
        if j and j.stage in ("queued", "copying", "ready", "encoding", "paused"):
            j.stage = "cancelled"
            self.notify(f"Cancelled #{j.id}; scratch files removed", timeout=3)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # approvals
    def highlighted_approval(self):
        item = self.query_one("#appr-list", ListView).highlighted_child
        if isinstance(item, BatchItem):
            return item.batch
        return item.job if isinstance(item, JobItem) else None

    def decide(self, item, what: str) -> None:
        if item is None:
            return
        if what == "later":
            self.notify("Kept in Approvals inbox — it'll wait for you.", timeout=3)
            return
        if isinstance(item, Batch):
            return self.decide_batch(item, what)
        j = item
        if what == "retry":
            def drop():
                j.stage = "discarded"
                if j in self.approvals:
                    self.approvals.remove(j)
                self.refresh_approvals()
            self.open_encode(j.media, True, settings=j.s,
                             preset=j.preset.rstrip("*") if j.preset.rstrip("*") in self.presets else None,
                             on_done=drop)
            return
        if j in self.approvals:
            self.approvals.remove(j)
        if what == "approve":
            j.stage = "to_replace"
        else:
            j.stage = "discarded"
            self.notify(f"Discarded encode of {j.media.label}; original untouched.", timeout=3)
        self.refresh_approvals()

    def decide_batch(self, b: Batch, what: str) -> None:
        if what == "retry":
            def drop():
                self.decide_batch(b, "deny")
            self.open_encode(b.folder, False, settings=b.s,
                             preset=b.preset.rstrip("*") if b.preset.rstrip("*") in self.presets else None,
                             on_done=drop)
            return
        if b in self.approvals:
            self.approvals.remove(b)
        if what == "approve":
            b.approved = True
            n = 0
            for j in b.of("awaiting"):
                if j.flag:
                    self.flag_out(j)
                else:
                    j.stage = "to_replace"
                    n += 1
            self.notify(f"Replacing {n} files now" + (f"; the other {len(b.remaining)} will auto-replace as "
                        "they pass checks" if b.remaining else "") + ".", title=f"✓ Batch approved · {batch_label(b)}")
        else:
            for j in b.jobs:
                if j.stage in ACTIVE:
                    j.stage = "cancelled"
                elif j.stage == "awaiting":
                    j.stage = "discarded"
            self.notify(f"Discarded {batch_label(b)} outputs and cancelled the rest. Originals untouched.",
                        title="✗ Batch denied")
        self.refresh_approvals()

    def flag_out(self, j: Job) -> None:
        """A flagged file from an approved batch becomes its own inbox item."""
        j.waiting_since = "flagged in batch"
        if j not in self.approvals:
            self.approvals.append(j)

    def action_approve(self) -> None:
        self.decide(self.highlighted_approval(), "approve")

    def action_deny(self) -> None:
        self.decide(self.highlighted_approval(), "deny")

    def action_retry(self) -> None:
        self.decide(self.highlighted_approval(), "retry")

    def action_vmaf(self) -> None:
        item = self.highlighted_approval()
        if item:
            what = "10 sampled files" if isinstance(item, Batch) else "6 sampled 10 s segments"
            self.notify(f"Computing VMAF on {what}…", timeout=2)
            self.set_timer(2.0, lambda: (setattr(item, "vmaf", random.uniform(94.5, 97.8)),
                                         self.show_approval(item)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        j = self.highlighted_approval()
        acts = {"btn-preview": self.action_preview, "btn-encode": self.action_encode,
                "btn-probe": self.action_probe, "a-approve": self.action_approve,
                "a-deny": self.action_deny, "a-retry": self.action_retry, "a-vmaf": self.action_vmaf}
        if bid in acts:
            acts[bid]()
        elif bid == "a-mpv" and isinstance(j, Job):
            self.notify(f"mpv --lavfi-complex='[vid1][vid2]hstack[vo]' \"{j.media.name}\" "
                        f"--external-file=out/{j.media.name}", title="Would launch", timeout=6)
        elif bid == "a-mpv":
            self.notify("Pick a file from the batch list (Enter) to compare it side by side.", timeout=3)
        elif bid == "a-all":
            for a in list(self.approvals):
                if not (isinstance(a, Job) and a.flag):
                    self.decide(a, "approve")
        elif bid == "s-detect":
            self.push_screen(SetupScreen(MACHINE["key"]), self.apply_machine)
        elif bid.startswith("s-test-"):
            name = bid.split("-")[-1].title()
            self.notify(f"{name} v4.0.9 reachable · {47 if name == 'Sonarr' else 312} items",
                        title=f"✓ {name} connected")
        elif bid in ("s-addroot", "s-map"):
            self.notify("Would open a directory picker / per-machine path mapping editor.")
        elif bid.startswith("p-"):
            self.preset_action(bid)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "s-default" and event.value in self.presets:
            self.default_preset = event.value
            self.refresh_presets()

    def refresh_approvals(self) -> None:
        lv = self.query_one("#appr-list", ListView)
        lv.clear()
        for a in self.approvals:
            lv.append(BatchItem(a) if isinstance(a, Batch) else JobItem(a))
        if self.approvals:
            lv.index = 0
            self.show_approval(self.approvals[0])
        else:
            self.query_one("#appr-detail", Static).update(
                Text("\n  Inbox zero. Finished encodes that need a decision land here and stay "
                     "until you approve, deny or retry them.", style="dim"))
        n = len(self.approvals)
        try:
            self.query_one("#tabs", TabbedContent).get_tab("tab-approvals").label = \
                f"③ Approvals ({n})" if n else "③ Approvals"
        except Exception:
            pass

    def show_approval(self, item) -> None:
        if item not in self.approvals:
            return
        d = self.query_one("#appr-detail", Static)
        self.query_one("#a-approve", Button).label = ("✓ Approve batch  y" if isinstance(item, Batch)
                                                      else "✓ Approve & replace  y")
        if isinstance(item, Batch):
            d.update(batch_view(item))
            return
        j = item
        d.update(Group(
            Text(j.media.title, style="bold #c0caf5"),
            Text(f"{local_path(j.media.path)}\npreset: {j.preset} · {j.waiting_since or 'just finished'}",
                 style="dim"),
            Text(""), compare_table(j), Text(""),
            Text("✓ approve → copy back to NAS (atomic rename), original → .recast-trash/, Sonarr rescan",
                 style="dim")))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, JobItem):
            self.show_approval(event.item.job)
        elif isinstance(event.item, BatchItem):
            self.show_approval(event.item.batch)
        elif isinstance(event.item, PresetItem):
            p = self.presets[event.item.preset_name]
            doc = {"name": event.item.preset_name, "description": p["desc"], **asdict(p["s"])}
            self.query_one("#preset-editor", TextArea).load_text(json.dumps(doc, indent=2, ensure_ascii=False))

    # presets
    def refresh_presets(self) -> None:
        lv = self.query_one("#preset-list", ListView)
        idx = lv.index or 0
        lv.clear()
        for k in self.presets:
            lv.append(PresetItem(k, k == self.default_preset))
        lv.index = min(idx, len(self.presets) - 1)

    def preset_action(self, bid: str) -> None:
        lv = self.query_one("#preset-list", ListView)
        item = lv.highlighted_child
        if not isinstance(item, PresetItem):
            return
        name = item.preset_name
        if bid == "p-save":
            try:
                doc = json.loads(self.query_one("#preset-editor", TextArea).text)
                new = doc.pop("name", name)
                desc = doc.pop("description", "")
                known = {f.name for f in fields(EncodeSettings)}
                bad = set(doc) - known
                if bad:
                    raise ValueError(f"unknown keys: {', '.join(sorted(bad))}")
                s = EncodeSettings(**doc)
            except Exception as ex:
                self.notify(str(ex), title="Invalid preset", severity="error")
                return
            if new != name:
                self.presets.pop(name)
            self.presets[new] = dict(desc=desc, s=s)
            self.notify(f"Saved “{new}”", title="Presets")
        elif bid == "p-dup":
            self.presets[f"{name} copy"] = dict(desc=self.presets[name]["desc"],
                                                s=EncodeSettings(**asdict(self.presets[name]["s"])))
        elif bid == "p-default":
            self.default_preset = name
        elif bid == "p-del" and len(self.presets) > 1 and name != self.default_preset:
            self.presets.pop(name)
        self.refresh_presets()

    # ── queue table ──
    def add_job_row(self, j: Job) -> None:
        self.query_one("#jobs", DataTable).add_row(*self.job_cells(j), key=str(j.id))

    def job_cells(self, j: Job):
        m = j.media
        label, style = STAGE_STYLE[j.stage]
        stage = Text(label, style=style)
        if j.stage == "copying":
            p = j.copied / m.size
        elif j.stage == "replacing":
            p = j.replace_p
        else:
            p = j.progress
        prog = Text(f"{pct_bar(p)} {p * 100:3.0f}%", style="#e0af68" if j.stage == "encoding" else "dim")
        if j.stage in ("queued", "skipped", "cancelled"):
            prog = Text("")
        fps = f"{j.fps:.0f}" if j.stage == "encoding" else ""
        if j.out_bytes:
            saved = j.projected / m.size - 1
            size = Text.assemble(f"{fsize(j.out_bytes)} ", (f"{saved * 100:+.0f}%", "#9ece6a" if saved < 0 else "#f7768e"))
        else:
            size = Text(fsize(m.size), style="dim")
        eta = ""
        if j.stage == "encoding" and j.fps:
            eta = fdur((m.frames - j.frame) / j.fps)
        name = Text.assemble(("◉ " if j.preview else "▤ " if j.batch else "  ", "#bb9af7"), m.label[:32])
        return [str(j.id), name, Text(j.preset[:26], style="dim"), stage, prog, fps, size, eta]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "jobs" and event.row_key is not None:
            jid = int(event.row_key.value)
            self.selected_job = next((j for j in self.jobs if j.id == jid), None)
            self._last_frame = 0
            self.update_job_detail()
            self.update_frame(force=True)

    # ── simulation ──
    def tick(self) -> None:
        dt, now = 0.2, time.monotonic()
        self._ticks = getattr(self, "_ticks", 0) + 1
        live = ("encoding", "paused", "verifying")
        pending = lambda st: sorted((j for j in self.jobs if j.stage == st), key=lambda j: not j.preview)
        active = [j for j in self.jobs if j.stage in live]
        copying, ready, queued = pending("copying"), pending("ready"), pending("queued")
        held = sum(j.out_bytes for j in self.jobs if j.stage == "awaiting")
        if not copying and not ready and queued:
            j = queued[0]  # previews jump the line
            if j.batch and held > MAX_SCRATCH:
                if not getattr(self, "_budget_warned", False):
                    self._budget_warned = True
                    self.notify("Scratch budget reached — approve the batch so finished files can move back "
                                "to the NAS, then it continues.", title="❚❚ Batch waiting", timeout=10)
            else:
                j.stage = "copying" if j.media.remote else "ready"
                src, dst = local_path(j.media.path), scratch()
                j.log.append((f"$ robocopy \"{src.rsplit(chr(92), 1)[0]}\" {dst} \"{j.media.name}\" /J"
                              if MACHINE["win"] else f"$ rsync --inplace '{src}' {dst}/")
                             if j.media.remote else "local source — no copy")
        if not active and ready:
            j = ready[0]
            j.stage, j.started, j.stage_t = "encoding", now, now
            j.log.append("$ " + " ".join(sum(build_command(j.s, j.media), []))[:200] + "…")
        if not any(j.stage == "replacing" for j in self.jobs):
            nxt = next((j for j in self.jobs if j.stage == "to_replace"), None)
            if nxt:  # one write back to the NAS at a time
                nxt.stage, nxt.replace_p = "replacing", 0.0
        sel = self.selected_job
        was_live = sel is not None and sel.stage in live
        for j in self.jobs:
            self.advance(j, dt, now)
        tbl = self.query_one("#jobs", DataTable)
        if was_live and sel.batch and sel.stage not in live:
            self._follow = (sel.batch, sel)
        follow = getattr(self, "_follow", None)
        if follow and self.selected_job is follow[1]:
            cur = next((j for j in follow[0].jobs if j.stage in live), None)
            if cur:
                self._follow = None
                self.selected_job = cur
                tbl.move_cursor(row=tbl.get_row_index(str(cur.id)))
        busy = ("copying", "encoding", "verifying", "replacing", "paused", "to_replace")
        for j in self.jobs:
            if j.stage in busy or getattr(j, "_dirty", True):
                cells = self.job_cells(j)
                for key, val in zip(["id", "file", "preset", "stage", "prog", "fps", "size", "eta"], cells):
                    tbl.update_cell(str(j.id), key, val)
                j._dirty = j.stage in busy
        self.update_topbar()
        tab = self.query_one("#tabs", TabbedContent).active
        if tab == "tab-queue":
            self.update_job_detail()
        elif tab == "tab-approvals" and self._ticks % 3 == 0:
            for item in self.query(BatchItem):
                item.refresh_label()
            h = self.highlighted_approval()
            if isinstance(h, Batch):
                self.show_approval(h)

    def advance(self, j: Job, dt: float, now: float) -> None:
        m = j.media
        b = j.batch
        if j.stage == "copying":
            j.copied = min(m.size, j.copied + m.size / j.copy_sim * dt)
            if j.copied >= m.size:
                j.stage = "ready"
                j.log.append(f"copied {fsize(m.size)} to scratch")
        elif j.stage == "encoding":
            if not j.fps:
                j.fps = display_fps(j.s, m)
            j.fps = max(1, j.fps * 0.9 + display_fps(j.s, m) * random.uniform(0.85, 1.15) * 0.1)
            step = m.frames / j.enc_sim * dt
            j.frame = min(m.frames, j.frame + step)
            j.noise = max(-0.45, min(0.6, j.noise + random.uniform(-0.12, 0.12)))
            v, a = estimate(j.s, m)
            kbps = v * (1 + j.noise) + a
            j.out_bytes += kbps * 1000 / 8 * (step / m.fps)
            j.spark.append(kbps)
            j.spark = j.spark[-120:]
            if now - j._last_log > 1.0:
                j._last_log = now
                j.log.append(f"frame={int(j.frame)} fps={j.fps:.0f} q=27.0 size={j.out_bytes / 1024:.0f}kB "
                             f"time={fdur(j.video_time)} bitrate={kbps:.1f}kbits/s speed={j.fps / m.fps:.2f}x")
            if j.frame >= m.frames:
                j.stage, j.stage_t = "verifying", now
                j.log.append("verifying: duration ✓  streams ✓  decode test…")
        elif j.stage == "verifying" and now - j.stage_t > (VERIFY_SIM if j.preview else 0.6):
            if b and random.random() < 0.05:
                j.flag = random.choice(["output larger than source", "audio stream count differs (2 → 1)",
                                        "duration off by 1.4 s"])
                if j.flag.startswith("output larger"):
                    j.out_bytes = m.size * 1.06
            j.log.append(f"verify: ⚑ {j.flag}" if j.flag else "verify passed")
            if j.s.after == "keep":
                j.stage = "kept"
            elif j.s.after == "auto" or (b and b.approved):
                if j.flag:
                    j.stage = "awaiting"
                    self.flag_out(j)
                    self.refresh_approvals()
                    self.notify(f"{m.label}: {j.flag} — held for you in Approvals", title="⚑ Not auto-replaced")
                else:
                    j.stage = "to_replace"
            elif b:
                j.stage = "awaiting"
                if b not in self.approvals:
                    b.waiting_since = "since " + time.strftime("%H:%M")
                    self.approvals.append(b)
                    self.refresh_approvals()
                if not b.announced:
                    b.announced = True
                    self.notify(f"First file of {batch_label(b)} is done. Review it whenever — approving the "
                                "batch lets the rest replace automatically.", title="▤ Batch in Approvals")
                if not b.remaining:
                    self.notify(f"{batch_label(b)}: all {len(b.done)} encoded. One decision waiting in Approvals.",
                                title="▤ Batch finished")
            else:
                j.stage = "awaiting"
                j.waiting_since = "finished " + time.strftime("%H:%M")
                self.approvals.append(j)
                self.refresh_approvals()
                if j.preview and len(self.screen_stack) == 1:
                    self.push_screen(ApprovalPrompt(j), lambda r, j=j: self.decide(j, r or "later"))
                else:
                    self.notify(f"{m.label} is waiting in Approvals", title="⚑ Encode finished")
        elif j.stage == "replacing":
            j.replace_p = min(1.0, j.replace_p + dt / (REPLACE_SIM if j.preview else 1.0))
            if j.replace_p >= 1:
                j.stage = "replaced"
                j.log.append("replaced in library · original → .recast-trash/ · Sonarr RescanSeries sent")
                if not b:
                    self.notify(f"{m.label}\nsaved {fsize(m.size - j.out_bytes)} · Sonarr rescan queued",
                                title="✓ Replaced in library")
                elif not b.remaining and not b.of("awaiting", "to_replace", "replacing"):
                    saved = sum(x.media.size - x.out_bytes for x in b.of("replaced"))
                    self.notify(f"{len(b.of('replaced'))} files replaced · saved {fsize(saved)} · "
                                "Sonarr rescan queued", title=f"✓ {batch_label(b)} done")

    def update_topbar(self) -> None:
        t = Text()
        t.append("◆ recast ", "bold #bb9af7")
        t.append(" │ ", "dim")
        t.append(f"⌂ {MACHINE['host']} ", "bold #c0caf5")
        t.append(resolve_encoder(EncodeSettings(codec="hevc"))[0], "#7dcfff")
        t.append(" │ ", "dim")
        t.append(f"NAS → {MACHINE['mount']} ", "#c0caf5")
        t.append("3.4 TB free", "dim")
        t.append(" │ ", "dim")
        t.append("scratch ", "dim")
        t.append(MACHINE["scratch"], "#9ece6a")
        t.append(" │ ", "dim")
        t.append("● Sonarr ● Radarr", "#9ece6a")
        n = len(self.approvals)
        if n:
            t.append(" │ ", "dim")
            t.append(f"⚑ {n} awaiting approval", "bold #bb9af7")
        enc = next((j for j in self.jobs if j.stage in ("encoding", "paused", "copying")), None)
        if enc:
            t.append(" │ ", "dim")
            if enc.stage == "copying":
                t.append(f"⇣ copying {enc.media.label[:18]} {enc.copied / enc.media.size * 100:.0f}%", "#7dcfff")
            else:
                t.append(f"{'❚❚' if enc.stage == 'paused' else '▶'} {enc.media.label[:18]} "
                         f"{enc.progress * 100:.0f}% {enc.fps:.0f}fps", "#e0af68")
        t.append("   DEMO · simulated", "dim italic")
        self.query_one("#topbar", Static).update(t)

    def update_job_detail(self) -> None:
        j = self.selected_job
        if not j:
            return
        m = j.media
        title = Text.assemble((f"#{j.id}  ", "dim"), (m.title, "bold #c0caf5"), "  ",
                              badge(m.codec), " → ", badge(CODEC_KEY.get(j.s.codec, m.codec)),
                              (f"   {j.preset}", "#e0af68"), ("   PREVIEW" if j.preview else "", "bold #bb9af7"))
        self.query_one("#jd-title", Static).update(title)
        order = ["copying", "encoding", "verifying", "awaiting"]
        stage_idx = {"queued": -1, "copying": 0, "ready": 1, "encoding": 1, "paused": 1, "verifying": 2, "to_replace": 3,
                     "awaiting": 3, "replacing": 3, "replaced": 4, "kept": 4, "discarded": 4,
                     "skipped": -1, "cancelled": -1}[j.stage]
        names = (["Copy from NAS"] if m.remote else ["Local"]) + ["Encode", "Verify",
                                                                   "Batch approval" if j.batch else "Approve → replace"]
        spin = "◐◓◑◒"[int(time.monotonic() * 4) % 4]
        p = Text()
        for i, nm in enumerate(names):
            if i:
                p.append(" ─── ", "dim")
            if i < stage_idx:
                p.append(f"✓ {nm}", "#9ece6a")
            elif i == stage_idx:
                p.append(f"{spin} {nm}", "bold #e0af68")
            else:
                p.append(f"○ {nm}", "dim")
        self.query_one("#jd-pipeline", Static).update(p)
        cp = 100 if (not m.remote or stage_idx >= 1) else j.copied / m.size * 100
        self.query_one("#pb-copy", ProgressBar).update(progress=cp)
        rate = random.uniform(108, 116) if j.stage == "copying" else 0
        self.query_one("#jd-copyinfo", Static).update(
            f"{fsize(j.copied if j.stage == 'copying' else m.size)} / {fsize(m.size)}"
            + (f"  ·  {rate:.0f} MB/s  ·  {MACHINE['mount']} → {scratch()}" if j.stage == "copying" else "")
            if m.remote else "local file — read in place, no copy")
        self.query_one("#pb-enc", ProgressBar).update(progress=j.progress * 100)
        self.query_one("#jd-encinfo", Static).update(
            f"frame {int(j.frame):,} / {m.frames:,}  ·  {fdur(j.video_time)} / {fdur(m.duration)}")
        g = Table.grid(padding=(0, 3))
        for _ in range(4):
            g.add_column()
        k = lambda s: Text(s, style="#565f89")
        live = j.stage in ("encoding", "paused")
        cur = j.spark[-1] if j.spark else 0
        g.add_row(k("fps"), Text(f"{j.fps:.0f}" if live else "—", style="bold"),
                  k("speed"), Text(f"{j.fps / m.fps:.2f}×" if live else "—", style="bold"))
        g.add_row(k("output"), Text(fsize(j.out_bytes), style="bold #e0af68"),
                  k("projected"), Text.assemble(f"≈{fsize(j.projected)} ",
                                                (f"({(j.projected / m.size - 1) * 100:+.0f}%)", "#9ece6a")))
        g.add_row(k("source"), fsize(m.size), k("bitrate"), f"{cur:,.0f} kb/s" if live else "—")
        eta = fdur((m.frames - j.frame) / j.fps) if live and j.fps else "—"
        demo = f"  (demo {fdur((m.frames - j.frame) / (m.frames / ENCODE_SIM))})" if live else ""
        g.add_row(k("eta"), Text.assemble((eta, "bold"), (demo, "dim")), k("elapsed"),
                  fdur(time.monotonic() - j.started) if j.started else "—")
        self.query_one("#jd-stats", Static).update(g)
        self.query_one("#jd-spark", Sparkline).data = j.spark or [0]
        self.query_one("#jd-log", Static).update(Text("\n".join(j.log[-6:]), style="#565f89", overflow="ellipsis",
                                                      no_wrap=True))

    def update_frame(self, force: bool = False) -> None:
        if not self.frame_on or self.query_one("#tabs", TabbedContent).active != "tab-queue":
            return
        j = self.selected_job
        fp = self.query_one("#frame", FramePreview)
        cap = self.query_one("#frame-caption", Static)
        cad = CADENCES[self.cadence][0]
        if not j or j.frame <= 0:
            fp.show(None, False)
            cap.update(Text(f"every {cad}  ·  [ ] cadence  ·  s split  ·  f hide", style="dim"))
            return
        if j.stage not in ("encoding",) and not force and fp.t == j.video_time:
            return
        fp.show(j.video_time, self.split)
        t = Text()
        t.append(f"frame {int(j.frame):,}", "bold")
        t.append(f" · {fdur(j.video_time)} · every {cad}", "dim")
        if self.split:
            t.append("   ◀ source │ encoded ▶", "#e0af68")
        t.append("   [ ] cadence · s split", "dim")
        cap.update(t)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-queue":
            if not self.selected_job and self.jobs:
                self.selected_job = self.jobs[-1]
            self.update_job_detail()
            self.update_frame(force=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--machine", choices=list(MACHINES), default="mac", help="which demo machine profile to start as")
    ap.add_argument("--no-setup", action="store_true", help="skip the first-run detection screen")
    a = ap.parse_args()
    Recast(a.machine, setup=not a.no_setup).run()
