"""The job pipeline: copy → encode → verify → (approve) → replace.

One encode at a time, one file prefetched into scratch while it runs, one
write-back to the library at a time. Everything persists to state.json so the
approval inbox and queue survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from .config import Config, config_dir
from .encode import (CODEC_LABEL, EncodeSettings, _audio_indices, _sub_indices, build_command, est_bytes,
                     resolve_encoder, two_pass)
from .ffmpeg import NO_WINDOW, parse_progress, run
from .probe import VIDEO_EXT, MediaInfo, probe_now

ACTIVE = ("queued", "copying", "ready", "encoding", "paused", "verifying")
LIVE = ("encoding", "paused", "verifying")
DONE = ("awaiting", "to_replace", "replacing", "replaced", "kept")
FINAL = ("replaced", "kept", "discarded", "cancelled", "skipped", "failed")
ASK_FIRST = 2.0  # auto_min_saving for automation in "ask" mode: no saving clears it, so you always approve


class Cancelled(Exception):
    pass


class NotReplaced(Exception):
    """A safety check stopped the replace; the message says why. Nothing in the library was touched."""


SEASON_RX = re.compile(r"^(season|series|staffel|saison|temporada)\s*\d+$|^s\d{1,2}$|^specials?$", re.I)
CODEC_TOKEN_RX = re.compile(r"(?<![A-Za-z0-9])(AV1|x264|x265|h\.?264|h\.?265|HEVC|AVC|XviD|DivX|VC-?1|MPEG-?2)"
                            r"(?![A-Za-z0-9])", re.I)


def show_root(path: str) -> str:
    """The show (or movie) folder a path belongs to: Season folders roll up to their parent.
    Pure string work — no filesystem access (this runs for every file in a library over SMB)."""
    d = os.path.dirname(path) if os.path.splitext(path)[1].lower() in VIDEO_EXT else path
    d = d.rstrip("/\\")
    return os.path.dirname(d) if SEASON_RX.match(os.path.basename(d)) else d


def norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def renamed_for_codec(name: str, codec: str) -> str:
    """'Show S01E01 1080p AV1.mkv' → 'Show S01E01 1080p HEVC.mkv' (last codec token only)."""
    hits = list(CODEC_TOKEN_RX.finditer(name))
    if not hits or codec == "copy":
        return name
    m = hits[-1]
    return name[:m.start()] + CODEC_LABEL[codec] + name[m.end():]


@dataclass
class Job:
    id: int
    src: str
    root: str
    root_path: str
    remote: bool
    settings: dict
    preset: str
    preview: bool = False
    batch: int | None = None
    stage: str = "queued"
    created: float = field(default_factory=time.time)
    info: dict = field(default_factory=dict)
    work_src: str = ""
    out: str = ""
    preview_jpg: str = ""
    copied: int = 0
    frame: int = 0
    fps: float = 0.0
    speed: float = 0.0
    kbps: float = 0.0
    out_size: int = 0
    out_time: float = 0.0
    started: float = 0.0
    finished: float = 0.0
    replace_done: int = 0
    flag: str = ""
    error: str = ""
    result: str = ""
    vmaf: float | None = None
    waiting_since: float = 0.0
    out_info: dict = field(default_factory=dict)
    final: str = ""  # path of the new file in the library after replace
    orig: str = ""   # where the original went (trash / .orig), for undo
    phase: str = ""  # "pass 1/2" during two-pass analysis
    origin: str = "manual"   # manual | auto (created by automation) | review (borderline, you said go)
    auto_min_saving: float = -1.0  # ≥0: replace without asking only if the real saving reaches this
    note: str = ""           # why it's waiting for you (e.g. saved less than the auto threshold)
    log: list = field(default_factory=list)
    spark: list = field(default_factory=list)

    @property
    def media(self) -> MediaInfo:
        return MediaInfo(**self.info)

    @property
    def s(self) -> EncodeSettings:
        return EncodeSettings(**self.settings)

    @property
    def name(self) -> str:
        return os.path.basename(self.src)

    @property
    def progress(self) -> float:
        m = self.info
        if m.get("duration"):
            return min(1.0, self.out_time / m["duration"])
        return min(1.0, self.frame / max(1, m.get("frames", 1)))

    @property
    def projected(self) -> float:
        return self.out_size / self.progress if self.progress > 0.03 else 0.0

    def add_log(self, line: str) -> None:
        self.log.append(line.rstrip())
        del self.log[:-60]


@dataclass
class Batch:
    id: int
    folder: str
    settings: dict
    preset: str
    jobs: list = field(default_factory=list)  # job ids
    approved: bool = False
    denied: bool = False
    created: float = field(default_factory=time.time)
    waiting_since: float = 0.0
    vmaf: float | None = None
    announced: bool = False

    @property
    def s(self) -> EncodeSettings:
        return EncodeSettings(**self.settings)


class Engine:
    def __init__(self, cfg: Config, on_event: Callable[[str, object], None] = lambda k, o: None):
        self.cfg = cfg
        self.on_event = on_event
        self.jobs: dict[int, Job] = {}
        self.batches: dict[int, Batch] = {}
        self.next_id = 1
        self.arr = None  # set by the app (ArrIndex)
        self._tasks: dict[int, asyncio.Task] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancel: set[int] = set()
        self._dirty = False
        self._last_save = 0.0
        self._space_warned: set[int] = set()
        self.hold = False            # paused by the user: start nothing new
        self.offline: set[str] = set()  # library roots that aren't reachable right now
        self._root_seen: dict[str, tuple[float, bool]] = {}
        self._awake = None
        # permanent, compact record of every replaced file: drives "measured" savings,
        # per-show preset memory and "already done" skipping. Jobs themselves are pruned.
        self.history: list[dict] = []
        self.state_file = config_dir() / "state.json"
        self.load()

    # ── persistence ──
    def load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        jf, bf = {f.name for f in fields(Job)}, {f.name for f in fields(Batch)}
        self.next_id = raw.get("next_id", 1)
        self.history = raw.get("history", [])
        cutoff = time.time() - 7 * 86400
        for d in raw.get("jobs", []):
            j = Job(**{k: v for k, v in d.items() if k in jf})
            if j.stage in FINAL and (j.finished or j.created) < cutoff:
                continue
            if j.stage in ("copying", "ready", "encoding", "paused", "verifying"):
                j.stage, j.copied, j.frame, j.out_size, j.out_time = "queued", 0, 0, 0, 0.0
                j.add_log("interrupted by restart — requeued")
            elif j.stage == "replacing":
                j.stage = "to_replace"
            if j.stage in ("awaiting", "to_replace") and j.out and not os.path.exists(j.out):
                j.stage, j.error = "failed", "encoded file is gone from scratch"
            self.jobs[j.id] = j
        for d in raw.get("batches", []):
            b = Batch(**{k: v for k, v in d.items() if k in bf})
            b.jobs = [i for i in b.jobs if i in self.jobs]
            if b.jobs:
                self.batches[b.id] = b

    def save(self, force: bool = False) -> None:
        if not force and (not self._dirty or time.time() - self._last_save < 2):
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"next_id": self.next_id, "history": self.history[-20000:],
                "jobs": [{**asdict(j), "spark": j.spark[-60:], "log": j.log[-20:]} for j in self.jobs.values()],
                "batches": [asdict(b) for b in self.batches.values()]}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.state_file)
        self._dirty, self._last_save = False, time.time()

    def touch(self) -> None:
        self._dirty = True

    # ── queries ──
    def batch_jobs(self, b: Batch, *stages: str) -> list[Job]:
        js = [self.jobs[i] for i in b.jobs if i in self.jobs]
        return [j for j in js if j.stage in stages] if stages else js

    def batch_of(self, j: Job) -> Batch | None:
        return self.batches.get(j.batch) if j.batch else None

    def inbox(self) -> list:
        """Items needing a decision: unapproved batches with finished files, single awaiting jobs, flags."""
        items: list = []
        for b in self.batches.values():
            if b.s.after == "ask" and not b.approved and not b.denied and self.batch_jobs(b, "awaiting"):
                items.append(b)
        for j in self.jobs.values():
            if j.stage != "awaiting":
                continue
            b = self.batches.get(j.batch) if j.batch else None
            if b is None or b.approved or b.s.after != "ask":
                items.append(j)
        return sorted(items, key=lambda x: x.waiting_since or x.created)

    def busy_paths(self) -> set[str]:
        """Files that already have a job in flight or waiting for a decision."""
        return {norm(j.src) for j in self.jobs.values()
                if j.stage in ACTIVE or j.stage in ("awaiting", "to_replace", "replacing")}

    def done_sizes(self) -> dict[str, int]:
        """Library file recast produced → its size then. A different size now means the file was replaced
        since (e.g. a Sonarr upgrade) and is fair game again."""
        return {norm(h["final"]): h.get("out_size", 0) for h in self.history if h.get("final") and not h.get("restored")}

    def done_paths(self) -> set[str]:
        """Library files recast itself produced (so 'encode all' never redoes them)."""
        return {norm(h["final"]) for h in self.history if h.get("final") and not h.get("restored")}

    def record_for(self, path: str) -> dict | None:
        """The history entry for a library file recast produced, if any (with `orig` located if possible)."""
        key = norm(path)
        h = next((h for h in reversed(self.history) if h.get("final") and norm(h["final"]) == key
                  and not h.get("restored")), None)
        if h and not (h.get("orig") and os.path.exists(h["orig"])):
            h["orig"] = _find_original(h["src"]) or h.get("orig", "")
        return h

    def can_restore(self, h: dict) -> bool:
        return bool(not h.get("restored") and not h.get("purged") and h.get("orig") and os.path.exists(h["orig"]))

    def kept_until(self, h: dict) -> float:
        """When the trash purge will delete this original (0 = not in the trash: kept as .orig or deleted)."""
        day = _trash_day(h.get("orig", ""))
        return day + self.cfg.trash_days * 86400 if day else 0.0

    def restore(self, path: str) -> str:
        """Undo a replace: the original goes back under its old name, the re-encode is set aside in the trash.
        Pure renames on the same share — nothing is copied. Blocking."""
        h = self.record_for(path)
        if not h:
            raise NotReplaced("recast didn't produce this file")
        orig = h.get("orig", "")
        if h.get("purged") and not (orig and os.path.exists(orig)):
            raise NotReplaced("the original was purged from the trash on "
                              + time.strftime("%Y-%m-%d", time.localtime(h["purged"])))
        if not orig or not os.path.exists(orig):
            raise NotReplaced("the original is no longer in the trash (purged or deleted)")
        final, src = h["final"], h["src"]
        try:
            st = os.stat(final)
        except FileNotFoundError:
            raise NotReplaced(f"{os.path.basename(final)} is gone from the library (moved or deleted since?)")
        if h.get("out_size") and st.st_size != h["out_size"]:
            raise NotReplaced(f"{os.path.basename(final)} changed since recast replaced it (upgraded by "
                              "Sonarr/Radarr?) — restoring would throw that file away")
        if norm(src) != norm(final) and os.path.exists(src):
            raise NotReplaced(f"{os.path.basename(src)} exists again (re-downloaded?)")
        root = next((r.path for r in self.cfg.roots if norm(src).startswith(norm(r.path) + os.sep)), "")
        if root:
            aside_dir = _trash_dir(root, os.path.dirname(src))
        elif ".recast-trash" in orig:
            aside_dir = os.path.dirname(orig)
        else:
            raise NotReplaced("this file isn't inside a library folder any more")
        stem, ext = os.path.splitext(os.path.basename(final))
        aside = os.path.join(aside_dir, f"{stem}.recast-undone{ext}")
        n = 1
        while os.path.exists(aside):
            n += 1
            aside = os.path.join(aside_dir, f"{stem}.recast-undone-{n}{ext}")
        os.replace(final, aside)  # same share: renames, no copying
        try:
            os.replace(orig, src)
        except OSError:
            os.replace(aside, final)
            raise
        h["restored"], h["undone"] = time.time(), aside
        self.touch()
        self.save(force=True)
        self.on_event("restored", h)
        return f"restored {os.path.basename(src)} · the re-encode was moved to the trash"

    def measured(self, folder: str) -> dict[str, tuple[float, int]]:
        """preset → (output/source size ratio, files) from real results under this show/folder."""
        root = norm(show_root(folder))
        acc: dict[str, list[int]] = {}
        rows = [(h["preset"], h["src_size"], h["out_size"], h["src"]) for h in self.history]
        rows += [(j.preset, j.info.get("size", 0), j.out_size, j.src) for j in self.jobs.values()
                 if j.stage in ("awaiting", "to_replace", "replacing") and j.out_size and not j.flag]
        for preset, a, b, src in rows:
            if a and b and norm(src).startswith(root + os.sep):
                t = acc.setdefault(preset, [0, 0, 0])
                t[0] += a
                t[1] += b
                t[2] += 1
        return {k: (v[1] / v[0], v[2]) for k, v in acc.items()}

    def saved_under(self, folder: str) -> tuple[int, int, int]:
        """(files, bytes before, bytes after) that recast replaced under this folder."""
        root = norm(folder)
        hs = [h for h in self.history if norm(h["src"]).startswith(root + os.sep) and h.get("out_size")
              and not h.get("restored")]
        return len(hs), sum(h["src_size"] for h in hs), sum(h["out_size"] for h in hs)

    def last_used(self, path: str) -> dict | None:
        """The most recent settings approved (or queued) for this show."""
        root = norm(show_root(path))
        cands = [h for h in self.history if norm(show_root(h["src"])) == root]
        cands += [{"preset": j.preset, "settings": j.settings, "when": j.created, "src": j.src, "pending": True}
                  for j in self.jobs.values() if j.stage not in ("discarded", "cancelled", "failed", "skipped")
                  and norm(show_root(j.src)) == root]
        return max(cands, key=lambda h: h.get("when", 0)) if cands else None

    def held_bytes(self) -> int:
        """Bytes recast is using in scratch right now (copies of sources + encoded outputs)."""
        total = 0
        for j in self.jobs.values():
            if j.stage in ("awaiting", "to_replace", "replacing", "encoding", "paused", "verifying"):
                total += j.out_size
            if j.remote and j.stage in ("copying",):
                total += j.copied
            elif j.remote and j.stage in ("ready", "encoding", "paused", "verifying"):
                total += j.info.get("size", 0)
        return total

    # ── adding work ──
    def _job(self, m: MediaInfo, root, s: EncodeSettings, preset: str, preview: bool, batch=None) -> Job:
        j = Job(self.next_id, m.path, root.name, root.path, root.remote, asdict(s), preset, preview, batch,
                info=asdict(m))
        self.next_id += 1
        self.jobs[j.id] = j
        return j

    def add_single(self, m: MediaInfo, root, s: EncodeSettings, preset: str, preview: bool = True) -> Job:
        existing = next((j for j in self.jobs.values() if norm(j.src) == norm(m.path)
                         and (j.stage in ACTIVE or j.stage in ("awaiting", "to_replace", "replacing"))), None)
        if existing:
            return existing  # never two jobs for one file
        j = self._job(m, root, s, preset, preview)
        self.touch()
        self.save(force=True)
        return j

    def add_batch(self, folder: str, media: list[MediaInfo], root, s: EncodeSettings, preset: str,
                  skip: Callable[[MediaInfo], object]) -> Batch:
        b = Batch(self.next_id, folder, asdict(s), preset)
        self.next_id += 1
        busy, done = self.busy_paths(), self.done_paths()
        for m in media:
            key = norm(m.path)
            if key in busy:
                continue  # already has its own job; leave it alone
            j = self._job(m, root, s, preset, False, b.id)
            if key in done:
                j.stage, j.result = "skipped", "already re-encoded by recast"
            elif why := skip(m):
                j.stage, j.result = "skipped", why if isinstance(why, str) else "not worth re-encoding"
            b.jobs.append(j.id)
        self.batches[b.id] = b
        self.touch()
        self.save(force=True)
        return b

    def forget_finished(self) -> list[int]:
        """Drop finished jobs from the list (history of replaced files is kept)."""
        gone = [jid for jid, j in self.jobs.items() if j.stage in FINAL and jid not in self._tasks]
        for jid in gone:
            del self.jobs[jid]
        for b in list(self.batches.values()):
            b.jobs = [i for i in b.jobs if i in self.jobs]
            if not b.jobs:
                del self.batches[b.id]
        self.touch()
        return gone

    def requeue(self, j: Job) -> bool:
        """Try a failed / cancelled job again from the start."""
        if j.stage not in ("failed", "cancelled") or norm(j.src) in self.busy_paths():
            return False
        for k in ("copied", "frame", "out_size", "replace_done"):
            setattr(j, k, 0)
        j.out_time, j.fps, j.speed, j.error, j.flag, j.spark = 0.0, 0.0, 0.0, "", "", []
        j.stage = "queued"
        j.add_log("requeued")
        self.touch()
        return True

    # ── scheduling (called ~4x a second by the app) ──
    def root_ok(self, j: Job) -> bool:
        return self.root_ok_path(j.root_path)

    def root_ok_path(self, root_path: str) -> bool:
        """Is this library folder reachable? Checked at most every 5 s per folder (a hung share
        can make stat() slow); transitions are reported once so the UI can say so."""
        now = time.monotonic()
        last = self._root_seen.get(root_path)
        if last and now - last[0] < 5:
            return last[1]
        ok = os.path.isdir(root_path)
        self._root_seen[root_path] = (now, ok)
        if ok and root_path in self.offline:
            self.offline.discard(root_path)
            self.on_event("online", root_path)
        elif not ok and root_path not in self.offline:
            self.offline.add(root_path)
            self.on_event("offline", root_path)
        return ok

    def tick(self) -> None:
        jobs = list(self.jobs.values())
        if self.hold:
            self._keep_awake(any(j.stage in ("copying", "replacing") for j in jobs))
            self.save()
            return
        by = lambda st: sorted((j for j in jobs if j.stage == st), key=lambda j: (not j.preview, j.id))
        busy_copy = any(j.stage == "copying" for j in jobs)
        live = any(j.stage in LIVE for j in jobs)
        ready, queued = by("ready"), by("queued")
        queued = [j for j in queued if self.root_ok(j)]  # cached per folder, so cheap
        if not busy_copy and (not ready or not self.cfg.prefetch and not live) and queued:
            j = queued[0]
            over = self.held_bytes() > self.cfg.max_scratch_gb * 1024**3
            if j.batch and over:
                self.on_event("budget", j)
            elif not (live and not self.cfg.prefetch):
                need = (j.info.get("size", 0) if j.remote else 0) + est_bytes(j.s, j.media, self.cfg.encoders) * 1.3
                free = _free(self.cfg.scratch)
                if free is not None and free < need + 2 * 1024**3:
                    if j.id not in self._space_warned:
                        self._space_warned.add(j.id)
                        self.on_event("no_space", (j, free, need))
                else:
                    self._space_warned.discard(j.id)
                    self._start(j, "copying", self._copy)
        if not live and ready:
            self._start(ready[0], "encoding", self._encode)
        if not any(j.stage == "replacing" for j in jobs):
            nxt = next((j for j in sorted(jobs, key=lambda j: j.id) if j.stage == "to_replace"), None)
            if nxt and self.root_ok(nxt):
                self._start(nxt, "replacing", self._replace)
        self._keep_awake(any(j.stage in ("copying", "ready", "encoding", "verifying", "replacing") for j in jobs))
        self.save()

    def _keep_awake(self, on: bool) -> None:
        """Stop the OS idle-sleeping mid-encode (caffeinate / SetThreadExecutionState / systemd-inhibit)."""
        if not self.cfg.keep_awake:
            on = False
        if on == bool(self._awake):
            return
        try:
            if sys.platform == "win32":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
                self._awake = True if on else None
            elif on:
                cmd = (["caffeinate", "-ims", "-w", str(os.getpid())] if sys.platform == "darwin" else
                       ["systemd-inhibit", "--what=sleep:idle", "--who=recast", "--why=encoding",
                        "--mode=block", "sleep", "infinity"])
                if shutil.which(cmd[0]):
                    self._awake = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self._awake.terminate()
                self._awake = None
        except Exception:  # noqa: BLE001 — staying awake is a nicety, never a failure
            self._awake = None

    def _start(self, j: Job, stage: str, fn) -> None:
        """Claim the stage *now* (so the next tick can't start it again), then run it."""
        running = self._tasks.get(j.id)
        if running and not running.done():
            return
        j.stage = stage
        t = asyncio.ensure_future(fn(j))
        self._tasks[j.id] = t
        t.add_done_callback(lambda _t, jid=j.id: self._tasks.pop(jid, None) if self._tasks.get(jid) is _t else None)

    def scratch(self, *parts: str) -> str:
        p = Path(self.cfg.scratch, *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    # ── stage: copy ──
    async def _copy(self, j: Job) -> None:
        if not j.remote:
            j.work_src, j.stage = j.src, "ready"
            j.add_log("local source — encoding in place, no copy")
            self.touch()
            return
        j.stage, j.copied = "copying", 0
        j.work_src = self.scratch("in", f"{j.id}-{j.name}")
        j.add_log(f"copy {j.src} → {j.work_src}")
        self.touch()
        try:
            await asyncio.to_thread(self._copy_file, j.src, j.work_src, j, "copied")
            j.stage = "ready"
            j.add_log(f"copied {j.copied / 1024**2:.0f} MiB to scratch")
        except Cancelled:
            pass
        except OSError as e:
            if not os.path.isdir(j.root_path):  # the share went away: wait for it, don't fail
                _rm(j.work_src + ".part")
                j.stage, j.copied = "queued", 0
                j.add_log(f"library went offline during copy — will retry ({e})")
                self._root_seen.pop(j.root_path, None)
                self.root_ok(j)
            else:
                self._fail(j, f"copy failed: {e}")
        self.touch()

    def _copy_file(self, src: str, dst: str, j: Job, counter: str) -> None:
        tmp = dst + ".part"
        setattr(j, counter, 0)
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                if j.id in self._cancel:
                    fo.close()
                    os.remove(tmp)
                    raise Cancelled
                chunk = fi.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fo.write(chunk)
                setattr(j, counter, getattr(j, counter) + len(chunk))
        os.replace(tmp, dst)

    # ── stage: encode ──
    async def _encode(self, j: Job) -> None:
        s, m = j.s, j.media
        stem = os.path.splitext(j.name)[0]
        j.out = self.scratch("out", f"{j.id}-{stem}.{s.container}")
        j.preview_jpg = self.scratch("preview", f"{j.id}.jpg")
        enc, _ = resolve_encoder(s, self.cfg.encoders)
        passes = [1, 2] if two_pass(s, enc) else [None]
        passdir = os.path.dirname(self.scratch("pass", "x"))
        passlog = f"{j.id}-pass"
        j.stage, j.started, j.spark = "encoding", time.time(), []
        self.touch()
        for n in passes:
            j.phase = f"pass {n}/2" if n else ""
            j.frame, j.out_size, j.out_time = 0, 0, 0.0
            argv = sum(build_command(s, m, j.work_src or j.src, j.out, self.cfg.encoders, j.preview_jpg,
                                     self.cfg.ffmpeg, pass_num=n, passlog=passlog), [])
            j.add_log(("$ " if not n else f"$ [pass {n}/2] ") + " ".join(argv))
            rc, err_tail = await self._run_ffmpeg(j, argv, cwd=passdir if n else None)
            if j.id in self._cancel:
                self._cancel.discard(j.id)
                self._cleanup(j)
                _rm_glob(passdir, passlog)
                return
            if rc != 0:
                _rm_glob(passdir, passlog)
                msg = next((l for l in reversed(err_tail) if "error" in l.lower() or "invalid" in l.lower()),
                           err_tail[-1] if err_tail else f"ffmpeg exited {rc}")
                return self._fail(j, msg[:200])
        _rm_glob(passdir, passlog)
        j.phase = ""
        j.out_size = os.path.getsize(j.out)
        j.stage, j.finished = "verifying", time.time()
        j.add_log("verifying output…")
        self.touch()
        await self._verify(j)

    async def _run_ffmpeg(self, j: Job, argv: list[str], cwd: str | None = None) -> tuple[int, list[str]]:
        """Run one ffmpeg, streaming -progress into the job. Returns (exit code, last stderr lines)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW, cwd=cwd)
        except OSError as e:
            return 1, [f"couldn't start ffmpeg: {e}"]
        self._procs[j.id] = proc
        if self.hold:  # paused while we were between passes / starting
            self._signal(proc.pid, cont=False)
            j.stage = "paused"
        err_tail: list[str] = []

        async def read_err():
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line and not line.startswith(("Svt[info]", "Svt[warn]", "x265 [info]")):
                    err_tail.append(line)
                    del err_tail[:-40]
                    j.add_log(line)

        async def read_progress():
            block: dict[str, str] = {}
            async for raw in proc.stdout:
                k, _, v = raw.decode(errors="replace").strip().partition("=")
                block[k] = v
                if k == "progress":
                    p = parse_progress(block)
                    j.frame, j.fps, j.speed, j.out_time = p["frame"], p["fps"], p["speed"], p["time"]
                    j.out_size = p["size"] or j.out_size
                    if p["kbps"] and j.phase != "pass 1/2":
                        j.kbps = p["kbps"]
                        j.spark.append(p["kbps"])
                        del j.spark[:-240]
                    block = {}

        await asyncio.gather(read_err(), read_progress())
        rc = await proc.wait()
        self._procs.pop(j.id, None)
        return rc, err_tail

    # ── stage: verify ──
    async def _verify(self, j: Job) -> None:
        m, s = j.media, j.s
        if j.stage != "verifying":
            return
        try:
            out = await asyncio.to_thread(probe_now, self.cfg.ffprobe, j.out)
        except Exception as e:  # noqa: BLE001
            return self._fail(j, f"couldn't read output: {e}")
        j.out_info = asdict(out)
        flags = []
        tol = max(1.0, m.duration * 0.005)
        if not out.codec_name:
            flags.append("no video stream in output")
        if m.duration and abs(out.duration - m.duration) > tol:
            flags.append(f"duration off by {abs(out.duration - m.duration):.1f} s")
        want_a = len(_audio_indices(s, m))
        if len(out.audio) != want_a:
            flags.append(f"audio tracks {want_a} → {len(out.audio)}")
        want_s = len(_sub_indices(s, m))
        if len(out.subs) != want_s:
            flags.append(f"subtitle tracks {want_s} → {len(out.subs)}")
        if j.out_size >= j.info.get("size", 0) > 0:
            flags.append("output larger than source")
        if self.cfg.verify_decode and not flags:
            bad = await asyncio.to_thread(self._decode_test, j.out)
            if bad:
                flags.append(f"decode errors: {bad}")
        j.flag = "; ".join(flags)
        j.add_log(f"verify: ⚑ {j.flag}" if j.flag else "verify passed ✓")
        self._after(j)

    def _decode_test(self, path: str) -> str:
        """Decode the first and last 20 s; full-file decode would double the time."""
        for pre in ([], ["-sseof", "-20"]):
            r = run([self.cfg.ffmpeg, "-v", "error", "-nostdin", *pre, "-i", path, "-t", "20", "-map", "0:V:0",
                     "-f", "null", "-"], timeout=300)
            # Only decoder complaints count; the null muxer moans about timestamps after a seek.
            errs = [l for l in r.stderr.splitlines() if l.strip() and not l.startswith("[null @")
                    and "monotonic" not in l]
            if r.returncode != 0 or errs:
                return (errs or ["decoder failed"])[0][:80]
        return ""

    def _after(self, j: Job) -> None:
        if j.stage != "verifying":
            return  # cancelled / denied meanwhile
        b = self.batch_of(j)
        s = j.s
        if j.auto_min_saving >= 0:  # automation: replace by itself only when the real result clearly pays off
            actual = 1 - j.out_size / max(1, j.info.get("size", 1))
            if not j.flag and actual >= j.auto_min_saving:
                j.stage = "to_replace"
            else:
                j.stage, j.waiting_since = "awaiting", time.time()
                if not j.flag and j.auto_min_saving >= ASK_FIRST:
                    j.note = f"saved {actual * 100:.0f}% — automation is set to ask before replacing"
                elif not j.flag:
                    j.note = (f"saved {actual * 100:.0f}% — below the {j.auto_min_saving * 100:.0f}% you set for "
                              "replacing automatically")
            self.touch()
            self.on_event("auto_done", j)
            self.save(force=True)
            return
        if s.after == "keep":
            j.stage = "awaiting" if j.flag else "to_replace"
            j.result = "keep"
        elif s.after == "auto" or (b and b.approved):
            j.stage = "awaiting" if j.flag else "to_replace"
        else:
            j.stage = "awaiting"
        if j.stage == "awaiting":
            j.waiting_since = time.time()
        self.touch()
        if j.stage == "awaiting" and not b:
            self.on_event("finished", j)  # single files always get the prompt (flag shown in it)
        elif j.stage == "awaiting" and j.flag and (b.approved or s.after != "ask"):
            self.on_event("flagged", j)
        elif j.stage == "awaiting" and b:
            if not b.waiting_since:
                b.waiting_since = time.time()
            if not b.announced:
                b.announced = True
                self.on_event("batch_first", b)
            if not self.batch_jobs(b, *ACTIVE):
                self.on_event("batch_done", b)
            else:
                self.on_event("changed", b)
        self.save(force=True)

    # ── stage: replace ──
    async def _replace(self, j: Job) -> None:
        j.stage, j.replace_done = "replacing", 0
        self.touch()
        keep_both = j.result == "keep"
        try:
            msg = await asyncio.to_thread(self._replace_files, j, keep_both)
        except Cancelled:
            return
        except NotReplaced as e:
            j.stage, j.flag, j.waiting_since = "awaiting", f"not replaced: {e}", time.time()
            j.add_log(j.flag)
            self.on_event("flagged", j)
            self.touch()
            return
        except OSError as e:
            if not os.path.isdir(j.root_path):  # share dropped mid write-back: the original is untouched
                j.stage = "to_replace"
                j.add_log(f"library went offline during replace — will retry ({e})")
                self._root_seen.pop(j.root_path, None)
                self.root_ok(j)
                self.touch()
                return
            j.stage, j.error = "awaiting", f"replace failed: {e}"
            j.add_log(j.error)
            self.on_event("failed", j)
            self.touch()
            return
        j.stage, j.result, j.finished = ("kept" if keep_both else "replaced"), msg, time.time()
        j.add_log(msg)
        self.history.append({"src": j.src, "final": j.final, "orig": j.orig, "preset": j.preset,
                             "settings": j.settings,
                             "src_size": j.info.get("size", 0), "out_size": j.out_size, "when": time.time()})
        if self.arr and self.cfg.rescan_after_replace:
            try:
                j.add_log(await asyncio.to_thread(self.arr.rescan, j.src))
            except Exception as e:  # noqa: BLE001
                j.add_log(f"rescan failed: {e}")
        self._cleanup(j, keep_out=False)
        b = self.batch_of(j)
        if b and not self.batch_jobs(b, *ACTIVE, "awaiting", "to_replace", "replacing"):
            self.on_event("batch_replaced", b)
        elif not b:
            self.on_event("replaced", j)
        self.save(force=True)

    def target_name(self, j: Job, keep_both: bool = False) -> str:
        stem, _ = os.path.splitext(os.path.basename(j.src))
        if self.cfg.rename_codec:
            stem = renamed_for_codec(stem, j.s.codec)
        return stem + (".recast" if keep_both else "") + "." + j.s.container

    def _replace_files(self, j: Job, keep_both: bool) -> str:
        src = j.src
        d = os.path.dirname(src)
        final = os.path.join(d, self.target_name(j, keep_both))
        # ── safety checks: anything off here leaves the library exactly as it was ──
        try:
            st = os.stat(src)
        except FileNotFoundError:
            raise NotReplaced("the original is gone from the library (moved or deleted since?)")
        if st.st_size != j.info.get("size") or (j.info.get("mtime") and abs(st.st_mtime - j.info["mtime"]) > 2):
            raise NotReplaced("the original changed since it was encoded (upgraded by Sonarr/Radarr?)")
        if norm(final) != norm(src) and os.path.exists(final):
            raise NotReplaced(f"{os.path.basename(final)} already exists next to it")
        free = _free(d)
        if free is not None and free < j.out_size + 256 * 1024**2:
            raise NotReplaced(f"not enough free space on the library drive ({free / 1024**3:.1f} GiB)")
        tmp = os.path.join(d, f".recast-{j.id}.{j.s.container}.part")
        try:
            if j.remote or not _same_device(j.out, d):
                self._copy_file(j.out, tmp, j, "replace_done")
            else:
                shutil.move(j.out, tmp)
                j.replace_done = j.out_size
        except BaseException:
            _rm(tmp)
            raise
        if keep_both:
            os.replace(tmp, final)
            j.final = final
            return f"kept both · new file {os.path.basename(final)}"
        moved_to = ""
        mode = self.cfg.originals
        if mode == "trash":
            trash_dir = _trash_dir(j.root_path, d)
            moved_to = os.path.join(trash_dir, os.path.basename(src))
            os.replace(src, moved_to)
        elif mode == "keep":
            moved_to = src + ".orig"
            os.replace(src, moved_to)
        try:
            os.replace(tmp, final)
        except OSError:
            if moved_to:  # roll back so the library is never left without the episode
                os.replace(moved_to, src)
            _rm(tmp)
            raise
        j.final, j.orig = final, moved_to
        if mode == "delete":
            if os.path.abspath(src) != os.path.abspath(final):
                os.remove(src)
        saved = j.info.get("size", 0) - j.out_size
        where = {"trash": f"original → .recast-trash ({self.cfg.trash_days} days)", "keep": "original kept as .orig",
                 "delete": "original deleted"}[mode]
        renamed = f" · now {os.path.basename(final)}" if os.path.basename(final) != os.path.basename(src) else ""
        return f"replaced · saved {saved / 1024**2:,.0f} MiB · {where}{renamed}"

    # ── controls ──
    def approve(self, item) -> None:
        if isinstance(item, Batch):
            item.approved = True
            for j in self.batch_jobs(item, "awaiting"):
                if j.flag:
                    self.on_event("flagged", j)
                else:
                    j.stage = "to_replace"
        else:
            item.stage, item.error = "to_replace", ""
        self.touch()
        self.save(force=True)

    def deny(self, item) -> None:
        jobs = self.batch_jobs(item) if isinstance(item, Batch) else [item]
        if isinstance(item, Batch):
            item.denied = True
        for j in jobs:
            if j.stage in ACTIVE:
                self.cancel(j)
            elif j.stage == "awaiting":
                j.stage, j.finished = "discarded", time.time()
                self._cleanup(j)
        self.touch()
        self.save(force=True)

    def cancel(self, j: Job) -> None:
        if j.stage not in ACTIVE:
            return
        was = j.stage
        j.stage, j.finished = "cancelled", time.time()
        self._cancel.add(j.id)
        p = self._procs.get(j.id)
        if p and p.returncode is None:
            if was == "paused":
                self._signal(p.pid, cont=True)
            p.terminate()
        elif was in ("queued", "ready"):
            self._cancel.discard(j.id)
            self._cleanup(j)
        self.touch()

    def set_hold(self, on: bool) -> None:
        """Pause everything: the running encode is frozen and nothing new starts (copies/replaces in flight finish)."""
        self.hold = on
        for j in self.jobs.values():
            p = self._procs.get(j.id)
            if p and p.returncode is None and j.stage in ("encoding", "paused"):
                self._signal(p.pid, cont=not on)
                j.stage = "paused" if on else "encoding"
        self.touch()

    @staticmethod
    def _signal(pid: int, cont: bool) -> None:
        if sys.platform == "win32":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
            (ctypes.windll.ntdll.NtResumeProcess if cont else ctypes.windll.ntdll.NtSuspendProcess)(h)
            ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, signal.SIGCONT if cont else signal.SIGSTOP)

    def _fail(self, j: Job, msg: str) -> None:
        j.stage, j.error, j.finished = "failed", msg, time.time()
        j.add_log(f"✗ {msg}")
        self._cleanup(j)
        self.touch()
        self.on_event("failed", j)

    def _cleanup(self, j: Job, keep_out: bool = False) -> None:
        paths = [j.preview_jpg]
        if j.remote and j.work_src and j.work_src != j.src:
            paths += [j.work_src, j.work_src + ".part"]
        if not keep_out and j.out:
            paths.append(j.out)
        for p in paths:
            try:
                if p:
                    os.remove(p)
            except OSError:
                pass

    async def shutdown(self) -> None:
        self._keep_awake(False)
        for jid, p in list(self._procs.items()):
            if p.returncode is None:
                j = self.jobs.get(jid)
                if j and j.stage == "paused":
                    self._signal(p.pid, cont=True)
                p.terminate()
        self.save(force=True)

    # ── housekeeping ──
    def clean_scratch(self) -> int:
        """Delete leftovers in scratch/{in,out,preview} that no live job refers to. Returns bytes freed."""
        keep = set()
        for j in self.jobs.values():
            if j.stage in ACTIVE or j.stage in ("awaiting", "to_replace", "replacing"):
                keep |= {norm(p) for p in (j.work_src, j.out, j.preview_jpg) if p}
        freed = 0
        for sub in ("in", "out", "preview"):
            d = os.path.join(self.cfg.scratch, sub)
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if e.is_file() and norm(e.path) not in keep and norm(e.path.removesuffix(".part")) not in keep:
                    try:
                        freed += e.stat().st_size
                        os.remove(e.path)
                    except OSError:
                        pass
        return freed

    def trash_folders(self) -> list[tuple[float, str, int]]:
        """[(day, folder, bytes)] for every .recast-trash/<date> folder, oldest first. Walks only the trash."""
        out = []
        for r in self.cfg.roots:
            t = os.path.join(r.path, ".recast-trash")
            try:
                entries = list(os.scandir(t))
            except OSError:
                continue
            for e in entries:
                day = _trash_day(os.path.join(e.path, "x"))
                if day and e.is_dir():
                    out.append((day, e.path, _tree_size(e.path)))
        return sorted(out)

    def purge_trash(self) -> list[str]:
        """Delete .recast-trash/<date> folders older than trash_days — and, if trash_max_gb is set, the oldest
        ones beyond it (never today's). Only touches folders recast created. History remembers what went."""
        removed = []
        cutoff = time.time() - self.cfg.trash_days * 86400
        days = self.trash_folders()
        total = sum(d[2] for d in days)
        cap = self.cfg.trash_max_gb * 1024**3 if self.cfg.trash_max_gb > 0 else 0
        today = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        for day, folder, size in days:
            if day < cutoff or (cap and total > cap and day < today):
                shutil.rmtree(folder, ignore_errors=True)
                if not os.path.exists(folder):
                    removed.append(folder)
                    total -= size
        if removed:
            now, gone = time.time(), [norm(f) + os.sep for f in removed]
            for h in self.history:
                if h.get("orig") and not h.get("purged") and any(norm(h["orig"]).startswith(g) for g in gone):
                    h["purged"] = now
            self.touch()
            self.save(force=True)
        return removed

    def trash_bytes(self) -> int:
        """Originals recast is still holding in the trash (from its own records — no disk walk)."""
        return sum(h.get("src_size", 0) for h in self.history
                   if not h.get("restored") and not h.get("purged") and _trash_day(h.get("orig", "")))


def _find_original(src: str) -> str:
    """Where an original went: <root>/.recast-trash/<date>/<same subfolders>/<name>, or <name>.orig."""
    if os.path.exists(src + ".orig"):
        return src + ".orig"
    d, name = os.path.dirname(src), os.path.basename(src)
    a = d
    for _ in range(8):
        t = os.path.join(a, ".recast-trash")
        if os.path.isdir(t):
            rel = os.path.relpath(d, a)
            hits = sorted(p for day in os.listdir(t) if os.path.isfile(p := os.path.join(t, day, rel, name)))
            if hits:
                return hits[-1]
        parent = os.path.dirname(a)
        if parent == a:
            break
        a = parent
    return ""


def _trash_dir(root: str, folder: str) -> str:
    """<root>/.recast-trash/<today>/<folder relative to root>, created, with a .plexignore at the top."""
    trash_root = os.path.join(root, ".recast-trash")
    d = os.path.join(trash_root, time.strftime("%Y-%m-%d"), os.path.relpath(folder, root))
    os.makedirs(d, exist_ok=True)
    ignore = os.path.join(trash_root, ".plexignore")
    if not os.path.exists(ignore):  # keep Plex from ever indexing the trash
        with open(ignore, "w") as fh:
            fh.write("*\n")
    return d


TRASH_DAY_RX = re.compile(r"[\\/]\.recast-trash[\\/](\d{4}-\d{2}-\d{2})[\\/]")


def _trash_day(path: str) -> float:
    """The day folder an original sits in (as a timestamp), or 0 if it isn't in a recast trash."""
    m = TRASH_DAY_RX.search(path or "")
    if not m:
        return 0.0
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
    except ValueError:
        return 0.0


def _tree_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _rm_glob(folder: str, prefix: str) -> None:
    try:
        for e in os.scandir(folder):
            if e.name.startswith(prefix):
                _rm(e.path)
    except OSError:
        pass


def _free(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _same_device(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False
