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


def build_command(s: EncodeSettings, m: "MediaInfo", src: str, out: str, caps: dict[str, EncoderCap],
                  preview: str | None = None, ffmpeg: str = "ffmpeg") -> list[list[str]]:
    """ffmpeg argv, grouped for pretty display. Flatten with sum(groups, [])."""
    enc, _ = resolve_encoder(s, caps)
    ten = s.bit_depth == "10" and s.codec != "h264"
    g: list[list[str]] = [[ffmpeg, "-hide_banner", "-y", "-nostdin", "-loglevel", "warning"],
                          ["-progress", "pipe:1", "-nostats", "-stats_period", "0.5"]]
    if "vaapi" in enc:
        g.append(["-vaapi_device", "/dev/dri/renderD128"])
    g.append(["-i", src])
    maps = ["-map", "0:V:0"]
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
            if is_hw(enc):
                v += ["-pix_fmt", "p010le" if ten else ("nv12" if "qsv" in enc else "yuv420p")]
            else:
                v += ["-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
        if s.two_pass and s.rate_mode == "bitrate" and enc.startswith("lib"):
            v += ["-pass", "2"]
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
    if x265:
        g.append(["-x265-params", ":".join(x265)])
    a_idx = _audio_indices(s, m)
    if s.audio == "copy":
        a = ["-c:a", "copy"]
    elif s.audio == "aac_stereo":
        a = ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]
    else:
        a = ["-c:a", "libopus", "-af", "aformat=channel_layouts=7.1|5.1|stereo|mono"]
        for n, i in enumerate(a_idx):
            a += [f"-b:a:{n}", f"{min(8, max(1, m.audio[i].get('channels', 2))) * 64}k"]
        if any(m.audio[i].get("channels", 2) > 2 for i in a_idx):
            a += ["-mapping_family", "1"]
    if _sub_indices(s, m):
        a += ["-c:s", "mov_text" if s.container == "mp4" else "copy"]
    if s.container == "mkv":
        a += ["-c:t", "copy"]
    g.append(a)
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
    if enc in ("libx265", "libx264", "libsvtav1"):
        fps *= X26X_SPEED[s.speed]
    pixels = max(1, m.width * m.height)
    if s.resolution != "source" and int(s.resolution) < m.height:
        pixels = int(s.resolution) ** 2 * 16 / 9
    return max(0.5, fps * (1920 * 1080) / pixels)


def already_target(s: EncodeSettings, m: "MediaInfo", caps: dict[str, EncoderCap]) -> bool:
    return (s.skip_same and CODEC_LABEL.get(s.codec) == m.codec
            and (not m.vkbps or m.vkbps <= estimate(s, m, caps)[0] * 1.15))


# ───────────────────────────── presets ───────────────────────────────

def default_presets() -> dict[str, tuple[str, EncodeSettings]]:
    S = EncodeSettings
    return {
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
    if not d.exists() or not any(d.glob("*.json")):
        d.mkdir(parents=True, exist_ok=True)
        for name, (desc, s) in default_presets().items():
            save_preset(name, desc, s)
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
    "bit_depth": FieldSpec(str, "10-bit avoids banding (not for H.264)", _o(("10", "recommended"), ("8", "compat"))),
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
