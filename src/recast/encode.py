"""Encode settings, presets, encoder resolution and ffmpeg command building."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import EncoderCap, config_dir

if TYPE_CHECKING:
    from .probe import MediaInfo


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

    @classmethod
    def from_dict(cls, d: dict) -> "EncodeSettings":
        known = {f.name: f for f in fields(cls)}
        bad = set(d) - set(known)
        if bad:
            raise ValueError(f"unknown keys: {', '.join(sorted(bad))}")
        s = cls(**d)
        for f in fields(cls):
            want = type(getattr(cls(), f.name))
            if not isinstance(getattr(s, f.name), want):
                raise ValueError(f"“{f.name}” should be a {want.__name__}")
        return s


CODEC_LABEL = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1", "copy": "copy"}
ENCODERS = {
    "hevc": ["libx265", "hevc_videotoolbox", "hevc_nvenc", "hevc_qsv", "hevc_vaapi"],
    "h264": ["libx264", "h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_vaapi"],
    "av1": ["libsvtav1", "libaom-av1", "av1_nvenc", "av1_qsv", "av1_vaapi"],
    "copy": ["copy"],
}
ALL_ENCODERS = [e for c in ("hevc", "h264", "av1") for e in ENCODERS[c]]
# "auto" = first working encoder in this order (hardware first; that's the point of auto)
AUTO_ORDER = {
    "hevc": ["hevc_nvenc", "hevc_videotoolbox", "hevc_qsv", "hevc_vaapi", "libx265"],
    "h264": ["h264_nvenc", "h264_videotoolbox", "h264_qsv", "h264_vaapi", "libx264"],
    "av1": ["av1_nvenc", "av1_qsv", "av1_vaapi", "libsvtav1", "libaom-av1"],
}
HW = ("nvenc", "videotoolbox", "qsv", "vaapi")
SPEEDS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
SVT_PRESET = dict(zip(SPEEDS, ["12", "11", "10", "9", "8", "6", "5", "4", "2"]))
NV_PRESET = dict(zip(SPEEDS, ["p1", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p7"]))
QSV_PRESET = dict(zip(SPEEDS, ["veryfast", "veryfast", "veryfast", "faster", "fast", "medium", "slow",
                               "slower", "veryslow"]))
X26X_SPEED = dict(zip(SPEEDS, [3.3, 2.8, 2.2, 1.7, 1.4, 1.0, 0.52, 0.22, 0.1]))  # fps vs medium
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
KEEP_LOSSY = {"ac3", "eac3", "aac", "mp3", "opus", "vorbis"}  # already small: re-encoding only loses quality
MP4_BAD_AUDIO = {"truehd", "mlp", "dts", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray", "pcm_dvd", "wmav2",
                 "wmapro", "vorbis"}
# mkvmerge statistics tags describe the *source* stream; on a re-encoded stream they're lies
STAT_TAGS = ["BPS", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES", "_STATISTICS_WRITING_APP",
             "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS"]


def _clear_stats(spec: str) -> list[str]:
    out: list[str] = []
    for t in STAT_TAGS:
        out += [f"-metadata:{spec}", f"{t}=", f"-metadata:{spec}", f"{t}-eng="]
    return out


# Quality words for a CRF number. Each codec's scale is different (x264/x265 0–51, SVT-AV1 0–63),
# lower is always better here; hardware encoders get the number translated (see build_command).
QUALITY_TIERS = {
    "h264": [(17, "extreme"), (19, "very high"), (21, "high"), (24, "balanced"), (27, "low"), (99, "very low")],
    "hevc": [(17, "extreme"), (19, "very high"), (22, "high"), (25, "balanced"), (28, "low"), (99, "very low")],
    "av1": [(19, "extreme"), (24, "very high"), (29, "high"), (35, "balanced"), (41, "low"), (99, "very low")],
}
CRF_SCALE = {"h264": ("x264", 51), "hevc": ("x265", 51), "av1": ("SVT-AV1", 63)}
# video bits per pixel per frame, roughly where each word starts (HEVC); H.264 needs ~1.5x, AV1 ~0.75x
BPP_TIERS = [(0.020, "very low"), (0.034, "low"), (0.050, "balanced"), (0.085, "high"), (0.13, "very high"),
             (9.0, "extreme")]
QUALITY_STYLE = {"extreme": "bold #3fb950", "very high": "bold #3fb950", "high": "#3fb950", "balanced": "#e6e6e6",
                 "low": "#d29922", "very low": "bold #f85149"}


def crf_word(codec: str, crf: int) -> str:
    return next(w for top, w in QUALITY_TIERS.get(codec, QUALITY_TIERS["hevc"]) if crf <= top)


def bitrate_word(codec: str, kbps: int, width: int, height: int, fps: float) -> str:
    bpp = kbps * 1000 / max(1, width * height * (fps or 24))
    bpp /= {"h264": 1.5, "av1": 0.75}.get(codec, 1.0)
    return next(w for top, w in BPP_TIERS if bpp < top)


def enc_status(e: str, caps: dict[str, EncoderCap]) -> str:
    if e == "copy":
        return "ok"
    c = caps.get(e)
    return c.status if c else "missing"


def resolve_encoder(s: EncodeSettings, caps: dict[str, EncoderCap]) -> tuple[str, str]:
    """(encoder actually used on this machine, note). note is "auto", "" or a fallback warning."""
    if s.codec == "copy":
        return "copy", ""
    best = next((e for e in AUTO_ORDER[s.codec] if enc_status(e, caps) == "ok"), ENCODERS[s.codec][0])
    if s.encoder == "auto":
        return best, "auto"
    if s.encoder not in ENCODERS[s.codec]:
        return best, f"{s.encoder} doesn't make {CODEC_LABEL[s.codec]} → using {best}"
    if enc_status(s.encoder, caps) == "ok":
        return s.encoder, ""
    return best, f"{s.encoder} isn't available on this machine → using {best}"


def is_hw(enc: str) -> bool:
    return any(h in enc for h in HW)


# ───────────────────────────── command ──────────────────────────────

def _audio_indices(s: EncodeSettings, m: "MediaInfo") -> list[int]:
    langs = [l.strip().lower() for l in s.langs.split(",") if l.strip()]
    if not langs:
        return list(range(len(m.audio)))
    keep = [i for i, a in enumerate(m.audio) if a.get("lang", "und").lower() in langs]
    return keep or list(range(len(m.audio)))  # never produce a silent file


def _sub_indices(s: EncodeSettings, m: "MediaInfo") -> list[int]:
    if s.subs == "none":
        return []
    idx = [i for i, t in enumerate(m.subs) if s.subs == "copy" or t.get("forced")]
    if s.container == "mp4":  # mp4 can't carry PGS/VobSub
        idx = [i for i in idx if m.subs[i].get("codec") in TEXT_SUBS]
    return idx


def two_pass(s: EncodeSettings, enc: str) -> bool:
    return s.two_pass and s.rate_mode == "bitrate" and s.codec != "copy" and enc in ("libx264", "libx265")


def _eac3_plan(s: EncodeSettings, m: "MediaInfo", a_idx: list[int]) -> list[tuple[int, int | None]]:
    """For audio=eac3_51: (output index, kbps to encode at, or None = copy as-is)."""
    plan = []
    for n, i in enumerate(a_idx):
        t = m.audio[i]
        ch = t.get("channels", 2)
        keep = t.get("codec") in KEEP_LOSSY and ch <= 6 and not (s.container == "mp4" and t.get("codec") in MP4_BAD_AUDIO)
        plan.append((n, None if keep else (640 if ch > 2 else 224)))
    return plan


def ten_bit(s: EncodeSettings, m: "MediaInfo | None" = None) -> bool:
    """10-bit output? H.264 stays 8-bit (10-bit H.264 barely plays anywhere)."""
    if s.codec in ("h264", "copy"):
        return False
    if s.bit_depth == "source":
        return bool(m and any(x in m.pix_fmt for x in ("10", "12", "p010")))
    return s.bit_depth == "10"


def pix_fmt_name(enc: str, ten: bool) -> str:
    """The pixel format ffmpeg is actually given for this encoder."""
    if "vaapi" in enc:
        return "p010" if ten else "nv12"
    if is_hw(enc):
        return "p010le" if ten else ("nv12" if "qsv" in enc else "yuv420p")
    return "yuv420p10le" if ten else "yuv420p"


def build_command(s: EncodeSettings, m: "MediaInfo", src: str, out: str, caps: dict[str, EncoderCap],
                  preview: str | None = None, ffmpeg: str = "ffmpeg", pass_num: int | None = None,
                  passlog: str = "recast-pass") -> list[list[str]]:
    """ffmpeg argv, grouped for pretty display. Flatten with sum(groups, []).

    Two-pass: call with pass_num=1 (analysis only, null output) then pass_num=2; `passlog` is a bare
    file name and the process must run with cwd = the folder it lives in (keeps Windows drive
    colons out of -x265-params). pass_num=None shows the pass-2 command."""
    enc, _ = resolve_encoder(s, caps)
    ten = ten_bit(s, m)
    tp = two_pass(s, enc)
    if tp and pass_num is None:
        pass_num = 2
    first = tp and pass_num == 1
    g: list[list[str]] = [[ffmpeg, "-hide_banner", "-y", "-nostdin", "-loglevel", "warning"],
                          ["-progress", "pipe:1", "-nostats", "-stats_period", "0.5"]]
    if "vaapi" in enc:
        g.append(["-vaapi_device", "/dev/dri/renderD128"])
    g.append(["-i", src])
    maps = ["-map", "0:V:0"]
    if not first:
        for i in _audio_indices(s, m):
            maps += ["-map", f"0:a:{i}"]
        for i in _sub_indices(s, m):
            maps += ["-map", f"0:s:{i}"]
        if s.container == "mkv":
            maps += ["-map", "0:t?"]  # attachments (fonts for ASS subs)
        maps += ["-map_metadata", "0", "-map_chapters", "0"]
    g.append(maps)
    v: list[str]
    if s.codec == "copy":
        v = ["-c:v", "copy"]
    else:
        v = ["-c:v", enc]
        if enc in ("libx265", "libx264"):
            v += ["-preset", s.speed]
        elif enc == "libaom-av1":
            v += ["-cpu-used", "4", "-row-mt", "1"]
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
            v += ["-pix_fmt", pix_fmt_name(enc, ten)]
        if tp and enc == "libx264":
            v += ["-pass", str(pass_num), "-passlogfile", passlog]
    g.append(v)
    vf = []
    if s.codec != "copy":
        if s.deinterlace:
            vf.append("bwdif=mode=send_field")
        if s.resolution != "source" and int(s.resolution) < m.height:
            vf.append(f"scale=-2:{s.resolution}:flags=lanczos")
        if "vaapi" in enc:
            vf.append(f"format={'p010' if ten else 'nv12'},hwupload")
    if vf:
        g.append(["-vf", ",".join(vf)])
    try:
        extra = shlex.split(s.extra)
    except ValueError:
        extra = [s.extra] if s.extra else []
    x265: list[str] = []
    if "-x265-params" in extra:
        i = extra.index("-x265-params")
        if enc == "libx265":
            x265 += extra[i + 1:i + 2]
        del extra[i:i + 2]  # x265-only flags are dropped when this machine resolves to another encoder
    if s.codec != "copy" and s.hdr and m.hdr and ten:
        g.append(["-color_primaries", "bt2020", "-color_trc",
                  "arib-std-b67" if m.hdr == "HLG" else "smpte2084", "-colorspace", "bt2020nc"])
        if enc == "libx265":
            x265.insert(0, "hdr10-opt=1:repeat-headers=1")
    if tp and enc == "libx265":
        x265 += [f"pass={pass_num}", f"stats={passlog}.log"]
    if x265:
        g.append(["-x265-params", ":".join(x265)])
    if first:  # analysis pass: video only, thrown away
        if extra:
            g.append(extra)
        g.append(["-f", "null", "-"])
        if preview:
            g.append(["-map", "0:V:0", "-vf", "fps=2,scale=640:-2", "-c:v", "mjpeg", "-q:v", "5",
                      "-update", "1", "-atomic_writing", "1", "-f", "image2", preview])
        return g
    a_idx = _audio_indices(s, m)
    if s.audio == "copy":
        a = ["-c:a", "copy"]
        if s.container == "mp4":  # mp4 can't hold these; convert just those tracks
            for n, i in enumerate(a_idx):
                if m.audio[i].get("codec") in MP4_BAD_AUDIO:
                    multi = m.audio[i].get("channels", 2) > 2
                    a += [f"-c:a:{n}", "eac3" if multi else "aac", f"-b:a:{n}", "640k" if multi else "256k"]
    elif s.audio == "aac_stereo":
        a = ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]
    elif s.audio == "eac3_51":
        a = ["-c:a", "copy"]
        for n, kbps in _eac3_plan(s, m, a_idx):
            if kbps:
                a += [f"-c:a:{n}", "eac3", f"-b:a:{n}", f"{kbps}k"]
                if m.audio[a_idx[n]].get("channels", 2) > 6:
                    a += [f"-ac:a:{n}", "6"]  # 7.1 → 5.1
    else:
        a = ["-c:a", "libopus", "-af", "aformat=channel_layouts=7.1|5.1|stereo|mono"]
        for n, i in enumerate(a_idx):
            a += [f"-b:a:{n}", f"{min(8, max(1, m.audio[i].get('channels', 2))) * 64}k"]
        if any(m.audio[i].get("channels", 2) > 2 for i in a_idx):
            a += ["-mapping_family", "1"]
    subs = _sub_indices(s, m)
    if subs:
        a += ["-c:s", "mov_text" if s.container == "mp4" else "copy"]
        if s.container == "mkv":  # mkv can't hold mov_text (mp4 sources): make those srt
            for n, i in enumerate(subs):
                if m.subs[i].get("codec") == "mov_text":
                    a += [f"-c:s:{n}", "srt"]
    if s.container == "mkv":
        a += ["-c:t", "copy"]
    g.append(a)
    stale = (_clear_stats("s:v:0") if s.codec != "copy" else []) + (_clear_stats("s:a") if s.audio != "copy" else [])
    if stale:
        g.append(stale)
    if extra:
        g.append(extra)
    g.append(["-max_muxing_queue_size", "4096", out])
    if preview:
        # Second output: the frame ffmpeg is on, ~2x a second, overwritten in place (atomically).
        g.append(["-map", "0:V:0", "-vf", "fps=2,scale=640:-2", "-c:v", "mjpeg", "-q:v", "5",
                  "-update", "1", "-atomic_writing", "1", "-f", "image2", preview])
    return g


def quote(tok: str, windows: bool) -> str:
    if re.fullmatch(r"[\w@%+=:,./\\*?-]+", tok):
        return tok
    if windows:
        return '"' + tok.replace('"', '""') + '"'
    return "'" + tok.replace("'", "'\\''") + "'"


# ───────────────────────────── estimates ─────────────────────────────

def estimate(s: EncodeSettings, m: "MediaInfo", caps: dict[str, EncoderCap]) -> tuple[float, float]:
    """(video kb/s, audio kb/s) best guess for the output."""
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
            if is_hw(resolve_encoder(s, caps)[0]):
                v *= 1.2
        v = min(v, m.vkbps * 1.05) if m.vkbps else v
    idx = _audio_indices(s, m)
    if s.audio == "copy":
        a = sum(m.audio[i].get("kbps", 192) for i in idx)
    elif s.audio == "aac_stereo":
        a = 192 * len(idx)
    elif s.audio == "eac3_51":
        a = sum(kbps or m.audio[idx[n]].get("kbps", 192) for n, kbps in _eac3_plan(s, m, idx))
    else:
        a = sum(min(8, m.audio[i].get("channels", 2)) * 64 for i in idx)
    return v, a


def est_bytes(s: EncodeSettings, m: "MediaInfo", caps: dict[str, EncoderCap]) -> float:
    v, a = estimate(s, m, caps)
    return (v + a) * 1000 / 8 * m.duration


def est_fps(s: EncodeSettings, m: "MediaInfo", caps: dict[str, EncoderCap]) -> float:
    """Expected fps, scaled from the 1080p benchmark taken during detection."""
    if s.codec == "copy":
        return 3000.0
    enc, _ = resolve_encoder(s, caps)
    cap = caps.get(enc)
    fps = cap.fps if cap and cap.fps else 30.0
    if two_pass(s, enc):
        fps /= 1.8  # analysis pass is a bit quicker than the real one
    if enc in ("libx265", "libx264", "libsvtav1"):
        fps *= X26X_SPEED[s.speed]
    pixels = max(1, m.width * m.height)
    if s.resolution != "source" and int(s.resolution) < m.height:
        pixels = int(s.resolution) ** 2 * 16 / 9
    return max(0.5, fps * (1920 * 1080) / pixels)


def skip_reason(s: EncodeSettings, m: "MediaInfo", caps: dict[str, EncoderCap]) -> str:
    """Why this preset isn't worth running on this file ("" = it is). Only applies with skip_same on."""
    if not s.skip_same:
        return ""
    if CODEC_LABEL.get(s.codec) == m.codec and (not m.vkbps or m.vkbps <= estimate(s, m, caps)[0] * 1.15):
        return f"already {m.codec}"
    if m.size and est_bytes(s, m, caps) > m.size * 0.95:
        return "wouldn't shrink"  # e.g. a lean AV1 file vs an HEVC 1800k preset
    return ""


def already_target(s: EncodeSettings, m: "MediaInfo", caps: dict[str, EncoderCap]) -> bool:
    return bool(skip_reason(s, m, caps))


# ───────────────────────────── presets ───────────────────────────────

def default_presets() -> dict[str, tuple[str, EncodeSettings]]:
    S = EncodeSettings
    return {
        "Anime HEVC · 1800k": (
            "x265 at 1800 kb/s, animation tune, 10-bit, same resolution, audio/subs/fonts untouched. "
            "Two-pass (Advanced) = best quality per byte at ~2x the time.",
            S(codec="hevc", encoder="libx265", resolution="source", rate_mode="bitrate", bitrate=1800,
              maxrate="3000k", bufsize="6000k", speed="medium", tune="animation", bit_depth="10",
              audio="copy", subs="copy", extra="-x265-params aq-mode=3")),
        "Live action HEVC · 3200k · 5.1": (
            "x265 at 3200 kb/s, 10-bit, 1080p max (4K is scaled down, HDR10 kept). Audio: AC3/EAC3/AAC "
            "kept as-is; TrueHD/DTS/FLAC/7.1 → EAC3 5.1 640k.",
            S(codec="hevc", encoder="libx265", resolution="1080", rate_mode="bitrate", bitrate=3200,
              maxrate="5500k", bufsize="11000k", speed="medium", tune="none", bit_depth="10",
              audio="eac3_51", subs="copy", hdr=True)),
        "HEVC 1080p · 2000k": (
            "Same streams, HEVC at 2000 kb/s, 1080p max. Encoder: best one on this machine.",
            S(codec="hevc", resolution="1080", rate_mode="bitrate", bitrate=2000)),
        "Anime → HEVC · quality": (
            "x265 CRF 20 slow + animation tune (pinned to CPU: best quality per GB).",
            S(codec="hevc", encoder="libx265", rate_mode="crf", crf=20, speed="slow", tune="animation",
              extra="-x265-params aq-mode=3:psy-rd=1.5:deblock=-1,-1")),
        "Fast hardware HEVC": (
            "NVENC / VideoToolbox / QSV, whichever this machine has. ~10x faster, ~20% bigger.",
            S(codec="hevc", rate_mode="crf", crf=24, speed="slow")),
        "4K HDR HEVC · CRF 18": (
            "Keeps 2160p + HDR10 metadata; for remuxes eating the NAS.",
            S(codec="hevc", encoder="libx265", rate_mode="crf", crf=18, speed="slow", hdr=True,
              maxrate="25M", bufsize="50M")),
        "DVD rescue (MPEG-2 → HEVC)": (
            "Deinterlace + CRF 19 for old SD sources.",
            S(codec="hevc", encoder="libx265", rate_mode="crf", crf=19, speed="slow", deinterlace=True)),
        "AV1 · smallest": (
            "AV1 at quality 30 (SVT-AV1 / NVENC AV1). Smallest files; needs newer TVs/clients.",
            S(codec="av1", rate_mode="crf", crf=30, speed="slow")),
        "H.264 max compat · 720p": (
            "For ancient TVs. MP4, stereo AAC, text subs only.",
            S(codec="h264", resolution="720", rate_mode="crf", crf=21, audio="aac_stereo",
              container="mp4", bit_depth="8")),
    }


def presets_dir() -> Path:
    return config_dir() / "presets"


def _slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip("-").lower() or "preset"


def preset_doc(name: str, desc: str, s: EncodeSettings) -> dict:
    return {"name": name, "description": desc, **asdict(s)}


def load_presets() -> dict[str, tuple[str, EncodeSettings]]:
    d = presets_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Offer each built-in preset once: new ones appear after an upgrade, deleted ones stay deleted.
    marker = d / ".offered"
    try:
        offered = set(json.loads(marker.read_text()))
    except (OSError, ValueError):
        offered = set()
        for f in d.glob("*.json"):
            try:
                offered.add(json.loads(f.read_text()).get("name", ""))
            except (OSError, ValueError):
                pass
    for name, (desc, s) in default_presets().items():
        if name not in offered:
            save_preset(name, desc, s)
            offered.add(name)
    marker.write_text(json.dumps(sorted(offered)))
    out: dict[str, tuple[str, EncodeSettings]] = {}
    for p in sorted(d.glob("*.json")):
        try:
            doc = json.loads(p.read_text())
            name = doc.pop("name", p.stem)
            desc = doc.pop("description", "")
            out[name] = (desc, EncodeSettings.from_dict(doc))
        except Exception:
            continue  # a broken file shouldn't take the app down; the editor shows errors on save
    return out


def save_preset(name: str, desc: str, s: EncodeSettings, old_name: str | None = None) -> None:
    d = presets_dir()
    d.mkdir(parents=True, exist_ok=True)
    if old_name and old_name != name:
        (d / f"{_slug(old_name)}.json").unlink(missing_ok=True)
    (d / f"{_slug(name)}.json").write_text(json.dumps(preset_doc(name, desc, s), indent=2, ensure_ascii=False))


def delete_preset(name: str) -> None:
    (presets_dir() / f"{_slug(name)}.json").unlink(missing_ok=True)


# ───────────────────── schema (drives editor autocomplete) ─────────────────────

@dataclass
class Option:
    value: object
    desc: str = ""
    ok: bool = True  # False → shown red (e.g. an encoder this machine can't run)


@dataclass
class FieldSpec:
    kind: type
    doc: str
    options: Callable[[dict], list[Option]] = field(default=lambda caps: [])


def _encoder_options(caps: dict[str, EncoderCap]) -> list[Option]:
    opts = [Option("auto", "best working encoder on this machine")]
    for e in ALL_ENCODERS:
        st = enc_status(e, caps)
        c = caps.get(e)
        if st == "ok":
            opts.append(Option(e, f"✓ works here · ~{c.fps:.0f} fps @1080p" if c and c.fps else "✓ works here"))
        elif st == "failed":
            opts.append(Option(e, f"✗ {c.reason if c else 'failed'}", ok=False))
        else:
            opts.append(Option(e, "✗ not available in this ffmpeg build", ok=False))
    return opts


def _o(*pairs) -> Callable[[dict], list[Option]]:
    return lambda caps: [Option(v, d) for v, d in pairs]


SCHEMA: dict[str, FieldSpec] = {
    "name": FieldSpec(str, "preset name shown in lists"),
    "description": FieldSpec(str, "one line about what it's for"),
    "codec": FieldSpec(str, "video codec", _o(("hevc", "H.265 · best size/compatibility balance"),
                                              ("h264", "H.264 · plays on anything"),
                                              ("av1", "AV1 · smallest files, newer clients only"),
                                              ("copy", "no video re-encode (remux)"))),
    "encoder": FieldSpec(str, "which encoder implementation", _encoder_options),
    "resolution": FieldSpec(str, "max output height", _o(("source", "keep source resolution"),
                                                         ("2160", "4K"), ("1080", "1080p"), ("720", "720p"),
                                                         ("480", "SD"))),
    "rate_mode": FieldSpec(str, "how size is controlled", _o(("bitrate", "target kb/s (predictable size)"),
                                                             ("crf", "constant quality (predictable look)"))),
    "bitrate": FieldSpec(int, "video kb/s when rate_mode is bitrate",
                         _o((1200, "720p-ish"), (2000, "1080p anime/TV"), (3000, "1080p live action"),
                            (6000, "1080p film"), (12000, "4K"))),
    "crf": FieldSpec(int, "quality when rate_mode is crf (lower = better/bigger)",
                     _o((18, "visually lossless"), (20, "high"), (22, "balanced"), (24, "smaller"),
                        (28, "small"))),
    "audio": FieldSpec(str, "audio handling", _o(("copy", "keep tracks as-is"),
                                                 ("eac3_51", "keep AC3/EAC3/AAC; lossless/7.1 → EAC3 5.1"),
                                                 ("aac_stereo", "AAC 2.0 192k (compat)"),
                                                 ("opus", "Opus, keeps channel count"))),
    "subs": FieldSpec(str, "subtitle handling", _o(("copy", "keep all"), ("forced", "forced only"),
                                                   ("none", "drop all"))),
    "container": FieldSpec(str, "output file type", _o(("mkv", "everything fits (recommended)"),
                                                       ("mp4", "text subs only"))),
    "after": FieldSpec(str, "what happens when an encode passes checks",
                       _o(("ask", "approval inbox (one decision per batch)"),
                          ("auto", "replace automatically if checks pass"), ("keep", "keep both files"))),
    "skip_same": FieldSpec(bool, "skip files already in the target codec at/below target bitrate",
                           _o((True, ""), (False, ""))),
    "speed": FieldSpec(str, "encoder speed preset (slower = smaller at same quality)",
                       lambda caps: [Option(s, "") for s in SPEEDS]),
    "tune": FieldSpec(str, "x264/x265 tuning", _o(("none", ""), ("animation", "anime/cartoons"),
                                                   ("grain", "keep film grain"), ("film", "live action"),
                                                   ("fastdecode", "weak playback devices"))),
    "bit_depth": FieldSpec(str, "pixel format: 10-bit (yuv420p10le) avoids banding; 8-bit (yuv420p) for old "
                                "devices; H.264 is always 8-bit",
                           _o(("10", "yuv420p10le · recommended"), ("8", "yuv420p · max compatibility"),
                              ("source", "match the source"))),
    "maxrate": FieldSpec(str, "VBV peak bitrate cap", _o(("4M", ""), ("8M", ""), ("25M", "4K"), ("", "none"))),
    "bufsize": FieldSpec(str, "VBV buffer (usually 2x maxrate)", _o(("8M", ""), ("16M", ""), ("50M", "4K"),
                                                                    ("", "none"))),
    "two_pass": FieldSpec(bool, "two-pass (bitrate mode, CPU encoders)", _o((False, ""), (True, ""))),
    "hdr": FieldSpec(bool, "carry HDR10/HLG colour info through", _o((True, ""), (False, ""))),
    "deinterlace": FieldSpec(bool, "bwdif deinterlacer for DVD/broadcast", _o((False, ""), (True, ""))),
    "langs": FieldSpec(str, "audio languages to keep, comma separated (blank = all)",
                       _o(("", "keep all"), ("jpn,eng", ""), ("eng", ""), ("eng,jpn", ""))),
    "extra": FieldSpec(str, "raw ffmpeg args appended to the command",
                       _o(("-x265-params aq-mode=3:psy-rd=1.5", "x265 anime tweaks"),
                          ("-svtav1-params tune=0:film-grain=8", "SVT-AV1 grain"),
                          ("-x264-params ref=4", "x264 tweak"),
                          ("-metadata:s:v title=recast", "tag the video stream"))),
}
