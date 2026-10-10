"""ffprobe → MediaInfo, with an on-disk cache keyed by path + size + mtime."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field

from .config import config_dir
from .ffmpeg import run

CODEC_NAMES = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264", "vc1": "VC-1", "mpeg2video": "MPEG-2",
               "mpeg4": "MPEG-4", "vp9": "VP9", "vp8": "VP8", "msmpeg4v3": "DivX", "wmv3": "WMV",
               "prores": "ProRes", "mpeg1video": "MPEG-1"}
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".webm", ".mpg", ".mpeg",
             ".vob", ".flv"}


@dataclass
class MediaInfo:
    path: str
    size: int = 0
    mtime: float = 0.0
    duration: float = 0.0
    container: str = ""
    codec: str = "?"
    codec_name: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    vkbps: int = 0
    pix_fmt: str = ""
    hdr: str = ""  # "", HDR10, HLG, Dolby Vision
    interlaced: bool = False
    audio: list = field(default_factory=list)  # dicts: lang codec channels layout kbps title default
    subs: list = field(default_factory=list)   # dicts: lang codec forced title
    recast: str = ""  # the RECAST tag recast writes into its own outputs (any install, any machine)
    error: str = ""

    @property
    def res(self) -> str:
        h = self.height
        return "2160p" if h > 1600 else "1440p" if h > 1200 else "1080p" if h > 900 else \
            "720p" if h > 600 else f"{h}p"

    @property
    def akbps(self) -> int:
        return sum(a.get("kbps", 0) for a in self.audio)

    def audio_label(self, a: dict) -> str:
        return f"{a.get('lang', 'und')} {a.get('codec', '?')} {a.get('layout', '')}".strip()

    def sub_label(self, s: dict) -> str:
        return f"{s.get('lang', 'und')} {s.get('codec', '?')}" + (" (forced)" if s.get("forced") else "")


def _rate(s: str) -> float:
    try:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def _tag_int(st: dict, *names: str) -> int:
    tags = {k.upper(): v for k, v in (st.get("tags") or {}).items()}
    for n in names:
        for k, v in tags.items():
            if k == n or k.startswith(n + "-"):
                try:
                    return int(float(v))
                except ValueError:
                    pass
    return 0


def parse(path: str, data: dict) -> MediaInfo:
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    m = MediaInfo(path=path, size=int(fmt.get("size", 0) or 0), duration=float(fmt.get("duration", 0) or 0),
                  container=fmt.get("format_name", ""))
    m.recast = next((v for k, v in (fmt.get("tags") or {}).items() if k.upper() == "RECAST"), "")
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    if video:
        v = video[0]
        m.codec_name = v.get("codec_name", "")
        m.codec = CODEC_NAMES.get(m.codec_name, m.codec_name.upper())
        m.profile = v.get("profile", "")
        m.width, m.height = int(v.get("width", 0)), int(v.get("height", 0))
        m.fps = round(_rate(v.get("avg_frame_rate", "")) or _rate(v.get("r_frame_rate", "")), 3)
        m.pix_fmt = v.get("pix_fmt", "")
        m.interlaced = v.get("field_order", "progressive") in ("tt", "bb", "tb", "bt")
        m.vkbps = int(int(v.get("bit_rate", 0) or 0) / 1000) or _tag_int(v, "BPS") // 1000
        m.frames = int(v.get("nb_frames", 0) or 0) or _tag_int(v, "NUMBER_OF_FRAMES")
        trc = v.get("color_transfer", "")
        side = [d.get("side_data_type", "") for d in v.get("side_data_list", [])]
        if any("DOVI" in s or "Dolby Vision" in s for s in side):
            m.hdr = "Dolby Vision"
        elif trc == "smpte2084":
            m.hdr = "HDR10"
        elif trc == "arib-std-b67":
            m.hdr = "HLG"
    if not m.duration and video:
        m.duration = float(video[0].get("duration", 0) or 0)
    if not m.frames and m.duration and m.fps:
        m.frames = int(m.duration * m.fps)
    for s in streams:
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        if s.get("codec_type") == "audio":
            kbps = int(int(s.get("bit_rate", 0) or 0) / 1000) or _tag_int(s, "BPS") // 1000
            m.audio.append({"lang": tags.get("language", "und"), "codec": s.get("codec_name", "?"),
                            "channels": int(s.get("channels", 2) or 2),
                            "layout": s.get("channel_layout", ""), "kbps": kbps,
                            "title": tags.get("title", ""), "default": bool(disp.get("default"))})
        elif s.get("codec_type") == "subtitle":
            m.subs.append({"lang": tags.get("language", "und"), "codec": s.get("codec_name", "?"),
                           "forced": bool(disp.get("forced")) or "forced" in tags.get("title", "").lower(),
                           "title": tags.get("title", "")})
    for a in m.audio:  # unknown audio bitrate: assume something sane so estimates aren't silly
        a["kbps"] = a["kbps"] or (640 if a["channels"] > 2 else 192)
    if m.duration and m.size:
        # What the file can actually hold. Stream tags (mkvmerge BPS) are often copied from an
        # earlier source and can be wildly wrong, so never trust a video bitrate above this.
        total = m.size * 8 / 1000 / m.duration
        real_v = max(1, int(total - sum(a["kbps"] for a in m.audio) - 2 * len(m.subs)))
        if not m.vkbps or m.vkbps > real_v * 1.1:
            m.vkbps = real_v
    return m


class ProbeCache:
    def __init__(self, ffprobe: str):
        self.ffprobe = ffprobe
        self.file = config_dir() / "probe-cache.json"
        self._lock = threading.Lock()
        self._dirty = False
        try:
            self._data: dict = {k: v for k, v in json.loads(self.file.read_text()).items() if k.startswith("v3|")}
        except (OSError, ValueError):
            self._data = {}

    def _key(self, path: str) -> str:
        st = os.stat(path)
        return f"v3|{path}|{st.st_size}|{int(st.st_mtime)}"  # bump vN when parse() changes

    def cached(self, path: str) -> MediaInfo | None:
        try:
            raw = self._data.get(self._key(path))
        except OSError:
            return None
        return MediaInfo(**raw) if raw else None

    def cached_meta(self, path: str, size: int, mtime: float) -> MediaInfo | None:
        """Cache lookup with size/mtime we already know (no stat over the network)."""
        raw = self._data.get(f"v3|{path}|{size}|{int(mtime)}")
        return MediaInfo(**raw) if raw else None

    def get(self, path: str) -> MediaInfo:
        """Probe (blocking; call from a thread). Reads only container headers, not the whole file."""
        hit = self.cached(path)
        if hit:
            if not hit.mtime:
                hit.mtime = os.stat(path).st_mtime
            return hit
        try:
            r = run([self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                     path], timeout=60)
            m = parse(path, json.loads(r.stdout or "{}"))
            m.mtime = os.stat(path).st_mtime
            if r.returncode != 0 and not m.codec_name:
                m.error = (r.stderr.strip().splitlines() or ["ffprobe failed"])[-1][:120]
        except Exception as e:  # noqa: BLE001 — surfaced in the UI
            m = MediaInfo(path=path, error=str(e)[:120])
        if not m.error:
            with self._lock:
                self._data[self._key(path)] = asdict(m)
                self._dirty = True
        return m

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data))
            os.replace(tmp, self.file)
            self._dirty = False


def probe_now(ffprobe: str, path: str) -> MediaInfo:
    r = run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], 60)
    m = parse(path, json.loads(r.stdout or "{}"))
    try:
        m.mtime = os.stat(path).st_mtime
    except OSError:
        pass
    return m
