"""Library-wide view: which shows would free the most space, and with which preset.

A scan lists every video file once (names, sizes, mtimes → overview.json) and reads
headers for any file the probe cache doesn't know yet. After that, ranking is pure
arithmetic on cached data, so it's instant and doesn't touch the network.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .config import config_dir
from .encode import already_target, est_bytes
from .probe import VIDEO_EXT, MediaInfo


@dataclass
class ShowStat:
    path: str
    name: str
    files: int = 0
    size: int = 0
    codecs: dict = field(default_factory=dict)  # codec label → bytes
    best: str = ""          # preset that frees the most
    best_files: int = 0
    saves: float = 0.0      # bytes freed by `best`
    measured: bool = False  # best's number comes from real results on this show
    done: int = 0           # files recast already re-encoded


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def snapshot_file():
    return config_dir() / "overview.json"


def load_snapshot(root: str) -> dict | None:
    try:
        return json.loads(snapshot_file().read_text()).get(root)
    except (OSError, ValueError):
        return None


def save_snapshot(root: str, entries: list[tuple[str, int, float]]) -> None:
    p = snapshot_file()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        data = {}
    data[root] = {"when": time.time(), "files": entries}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, p)


def list_files(root: str, progress=None, cancelled=lambda: False) -> list[tuple[str, int, float]]:
    """Every video file under root as (path, size, mtime). Skips dot/@ folders (trash, Synology junk)."""
    out: list[tuple[str, int, float]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if cancelled():
            break
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "@", "#")))
        for n in filenames:
            if os.path.splitext(n)[1].lower() in VIDEO_EXT and not n.startswith("."):
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime))
        if progress:
            progress(len(out))
    return out


def rank(root: str, entries, infos: dict[str, MediaInfo], presets: dict, caps, done: set[str],
         measured_for, title_of) -> list[ShowStat]:
    """Group files by show and find, for each, the preset that saves the most bytes."""
    groups: dict[str, list[MediaInfo]] = {}
    sizes: dict[str, int] = {}
    counts: dict[str, int] = {}
    for path, size, _ in entries:
        key = title_of(path)
        sizes[key] = sizes.get(key, 0) + size
        counts[key] = counts.get(key, 0) + 1
        m = infos.get(path)
        groups.setdefault(key, [])
        if m:
            groups[key].append(m)
    stats = []
    for key, ms in groups.items():
        st = ShowStat(key, os.path.basename(key) or key, files=counts[key], size=sizes.get(key, 0))
        for m in ms:
            st.codecs[m.codec] = st.codecs.get(m.codec, 0) + m.size
        fresh = [m for m in ms if _norm(m.path) not in done]
        st.done = len(ms) - len(fresh)
        meas = measured_for(key)
        for name, (_desc, s) in presets.items():
            todo = [m for m in fresh if not already_target(s, m, caps)]
            if not todo:
                continue
            src = sum(m.size for m in todo)
            if name in meas:
                out, real = src * meas[name][0], True
            else:
                out, real = sum(est_bytes(s, m, caps) for m in todo), False
            if src - out > st.saves:
                st.best, st.best_files, st.saves, st.measured = name, len(todo), src - out, real
        stats.append(st)
    stats.sort(key=lambda x: -x.saves)
    return stats
