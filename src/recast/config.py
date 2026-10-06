"""Where recast keeps things, and the per-install config.

Each install is standalone: one config.json (library roots, scratch, detected
encoders, Sonarr/Radarr), a presets/ folder, state.json (jobs + approval inbox)
and a probe cache. Nothing is shared between machines.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP = "recast"


def config_dir() -> Path:
    if os.environ.get("RECAST_HOME"):
        return Path(os.environ["RECAST_HOME"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


def default_scratch() -> Path:
    # Deliberately the plain ~/Documents path, not a OneDrive-redirected one:
    # multi-GB temp files should never sync anywhere.
    return Path.home() / "Documents" / APP


def is_network_path(path: str) -> bool:
    """Best-effort: is this path on a network share (so we copy to scratch first)?"""
    p = os.path.abspath(os.path.expanduser(path))
    if sys.platform == "win32":
        if p.startswith("\\\\"):
            return True
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(p)[0] + "\\") == 4  # DRIVE_REMOTE
        except Exception:
            return False
    net = {"smbfs", "nfs", "nfs4", "cifs", "smb3", "afpfs", "webdav", "fuse.sshfs", "9p"}
    best, fstype = "", ""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                # "//user@nas.local/media on /Volumes/media (smbfs, nodev, ...)"
                if " on " not in line or "(" not in line:
                    continue
                mnt = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
                typ = line.rsplit("(", 1)[1].split(",")[0].strip(" )")
                if (p == mnt or p.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, fstype = mnt, typ
        else:
            with open("/proc/mounts") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mnt = parts[1].replace("\\040", " ")
                    if (p == mnt or p.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                        best, fstype = mnt, parts[2]
    except Exception:
        return False
    return fstype in net


MEDIA_NAMES = {"tv", "tv shows", "shows", "series", "movies", "films", "anime", "kids", "documentaries",
               "docs", "media", "video", "videos", "plex", "4k", "uhd", "cartoons"}


def suggest_libraries(limit: int = 8) -> list[tuple[str, bool]]:
    """Likely media folders on mounted drives/shares: [(path, is_network)]. Quick: one listing per mount."""
    mounts: list[str] = []
    if sys.platform == "darwin":
        mounts = [os.path.join("/Volumes", n) for n in _ls("/Volumes") if n != "Macintosh HD"]
        mounts.append(os.path.expanduser("~/Movies"))
    elif sys.platform == "win32":
        mounts = [f"{c}:\\" for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{c}:\\")]
    else:
        mounts = [os.path.join("/mnt", n) for n in _ls("/mnt")] + \
                 [os.path.join("/media", u, n) for u in _ls("/media") for n in _ls(os.path.join("/media", u))] + \
                 [os.path.join("/srv", n) for n in _ls("/srv")] + [os.path.expanduser("~/Videos")]
    out: list[tuple[str, bool]] = []
    for m in mounts:
        if not os.path.isdir(m):
            continue
        subs = [os.path.join(m, n) for n in _ls(m) if n.lower() in MEDIA_NAMES and os.path.isdir(os.path.join(m, n))]
        net = is_network_path(m)
        for p in subs or ([m] if net else []):
            out.append((p, net))
    return out[:limit]


def _ls(path: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(path) if not n.startswith((".", "@", "#", "$")))
    except OSError:
        return []


@dataclass
class Root:
    name: str
    path: str
    remote: bool = False


@dataclass
class Arr:
    url: str = ""
    api_key: str = ""
    # [[path as Sonarr/Radarr sees it, path on this machine]], e.g. [["/tv", "/Volumes/media/TV"]]
    path_map: list = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)


@dataclass
class EncoderCap:
    status: str  # ok | failed | missing
    fps: float = 0.0
    reason: str = ""


@dataclass
class Config:
    roots: list = field(default_factory=list)
    scratch: str = ""
    ffmpeg: str = ""
    ffprobe: str = ""
    ffmpeg_version: str = ""
    machine: dict = field(default_factory=dict)
    encoders: dict = field(default_factory=dict)  # name -> EncoderCap
    detected_at: str = ""
    sonarr: Arr = field(default_factory=Arr)
    radarr: Arr = field(default_factory=Arr)
    default_preset: str = "HEVC 1080p · 2000k"
    max_scratch_gb: int = 200
    prefetch: bool = True
    originals: str = "trash"  # trash | keep | delete
    trash_days: int = 14
    rescan_after_replace: bool = True
    verify_decode: bool = True
    rename_codec: bool = True   # "…1080p AV1.mkv" → "…1080p HEVC.mkv" on replace
    keep_awake: bool = True     # stop idle sleep while jobs run
    desktop_notify: bool = True # OS notification when something needs you
    preview_cadence: float = 1.0
    last_path: str = ""  # where the library tree was; reopened on launch
    theme: str = "tokyo-night"

    @property
    def needs_setup(self) -> bool:
        return not (self.ffmpeg and self.encoders and self.roots and self.scratch)

    @property
    def path(self) -> Path:
        return config_dir() / "config.json"

    def save(self) -> None:
        d = asdict(self)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    @classmethod
    def load(cls) -> "Config":
        p = config_dir() / "config.json"
        if not p.exists():
            return cls(scratch=str(default_scratch()))
        raw = json.loads(p.read_text())
        known = {f.name for f in fields(cls)}
        c = cls(**{k: v for k, v in raw.items() if k in known})
        c.roots = [Root(**r) if isinstance(r, dict) else r for r in c.roots]
        c.encoders = {k: EncoderCap(**v) if isinstance(v, dict) else v for k, v in c.encoders.items()}
        c.sonarr = Arr(**c.sonarr) if isinstance(c.sonarr, dict) else c.sonarr
        c.radarr = Arr(**c.radarr) if isinstance(c.radarr, dict) else c.radarr
        return c


def machine_summary() -> dict:
    """Host / OS / CPU / GPU strings for display. Best effort, never raises."""
    def run(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout.strip()
        except Exception:
            return ""

    host = platform.node().split(".")[0]
    os_name = f"{platform.system()} {platform.release()}"
    cpu, gpu = platform.processor() or platform.machine(), ""
    if sys.platform == "darwin":
        os_name = f"macOS {platform.mac_ver()[0]}"
        cpu = run("sysctl", "-n", "machdep.cpu.brand_string") or cpu
        if platform.machine() == "arm64":
            gpu = f"{cpu} GPU (VideoToolbox)"
    elif sys.platform == "win32":
        os_name = f"Windows {platform.release()}"
        cpu = run("powershell", "-NoProfile", "-Command",
                  "(Get-CimInstance Win32_Processor).Name") or cpu
        gpu = run("powershell", "-NoProfile", "-Command",
                  "(Get-CimInstance Win32_VideoController).Name -join ', '")
    else:
        try:
            for line in open("/etc/os-release"):
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
            for line in open("/proc/cpuinfo"):
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        gpu = ", ".join(l.split(": ", 1)[-1] for l in run("lspci").splitlines()
                        if "VGA" in l or "3D controller" in l)
    nv = run("nvidia-smi", "--query-gpu=name", "--format=csv,noheader")
    if nv:
        gpu = nv.splitlines()[0]
    return {"host": host, "os": os_name, "cpu": cpu, "gpu": gpu}
