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
from .encode import CODEC_LABEL, EncodeSettings, _audio_indices, _sub_indices, build_command, est_bytes
from .ffmpeg import NO_WINDOW, parse_progress, run
from .probe import MediaInfo, probe_now

ACTIVE = ("queued", "copying", "ready", "encoding", "paused", "verifying")
LIVE = ("encoding", "paused", "verifying")
DONE = ("awaiting", "to_replace", "replacing", "replaced", "kept")
FINAL = ("replaced", "kept", "discarded", "cancelled", "skipped", "failed")


class Cancelled(Exception):
    pass


class NotReplaced(Exception):
    """A safety check stopped the replace; the message says why. Nothing in the library was touched."""


SEASON_RX = re.compile(r"^(season|series|staffel|saison|temporada)\s*\d+$|^s\d{1,2}$|^specials?$", re.I)
CODEC_TOKEN_RX = re.compile(r"(?<![A-Za-z0-9])(AV1|x264|x265|h\.?264|h\.?265|HEVC|AVC|XviD|DivX|VC-?1|MPEG-?2)"
                            r"(?![A-Za-z0-9])", re.I)


def show_root(path: str) -> str:
    """The show (or movie) folder a path belongs to: Season folders roll up to their parent."""
    d = path if os.path.isdir(path) or not os.path.splitext(path)[1] else os.path.dirname(path)
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

    def done_paths(self) -> set[str]:
        """Library files recast itself produced (so 'encode all' never redoes them)."""
        return {norm(h["final"]) for h in self.history if h.get("final")}

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
                  skip: Callable[[MediaInfo], bool]) -> Batch:
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
            elif skip(m):
                j.stage, j.result = "skipped", "already in target codec"
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
        """Is this job's library folder reachable? Checked at most every 5 s per folder (a hung share
        can make stat() slow); transitions are reported once so the UI can say so."""
        now = time.monotonic()
        last = self._root_seen.get(j.root_path)
        if last and now - last[0] < 5:
            return last[1]
        ok = os.path.isdir(j.root_path)
        self._root_seen[j.root_path] = (now, ok)
        if ok and j.root_path in self.offline:
            self.offline.discard(j.root_path)
            self.on_event("online", j.root_path)
        elif not ok and j.root_path not in self.offline:
            self.offline.add(j.root_path)
            self.on_event("offline", j.root_path)
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
        argv = sum(build_command(s, m, j.work_src or j.src, j.out, self.cfg.encoders, j.preview_jpg,
                                 self.cfg.ffmpeg), [])
        j.stage, j.started, j.frame, j.out_size, j.out_time, j.spark = "encoding", time.time(), 0, 0, 0.0, []
        j.add_log("$ " + " ".join(argv))
        self.touch()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        except OSError as e:
            return self._fail(j, f"couldn't start ffmpeg: {e}")
        self._procs[j.id] = proc
        err_tail: list[str] = []

        async def read_err():
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line and not line.startswith(("Svt[info]", "Svt[warn]")):
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
                    if p["kbps"]:
                        j.kbps = p["kbps"]
                        j.spark.append(p["kbps"])
                        del j.spark[:-240]
                    block = {}

        await asyncio.gather(read_err(), read_progress())
        rc = await proc.wait()
        self._procs.pop(j.id, None)
        if j.id in self._cancel:
            self._cancel.discard(j.id)
            self._cleanup(j)
            return
        if rc != 0:
            msg = next((l for l in reversed(err_tail) if "error" in l.lower() or "invalid" in l.lower()),
                       err_tail[-1] if err_tail else f"ffmpeg exited {rc}")
            return self._fail(j, msg[:200])
        j.out_size = os.path.getsize(j.out)
        j.stage, j.finished = "verifying", time.time()
        j.add_log("verifying output…")
        self.touch()
        await self._verify(j)

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
        self.history.append({"src": j.src, "final": j.final, "preset": j.preset, "settings": j.settings,
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
            rel = os.path.relpath(d, j.root_path)
            trash_root = os.path.join(j.root_path, ".recast-trash")
            trash_dir = os.path.join(trash_root, time.strftime("%Y-%m-%d"), rel)
            os.makedirs(trash_dir, exist_ok=True)
            ignore = os.path.join(trash_root, ".plexignore")
            if not os.path.exists(ignore):  # keep Plex from ever indexing the trash
                with open(ignore, "w") as fh:
                    fh.write("*\n")
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
        j.final = final
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

    def purge_trash(self) -> list[str]:
        """Delete .recast-trash/<date> folders older than trash_days. Only touches folders recast created."""
        removed = []
        cutoff = time.time() - self.cfg.trash_days * 86400
        for r in self.cfg.roots:
            t = os.path.join(r.path, ".recast-trash")
            try:
                entries = list(os.scandir(t))
            except OSError:
                continue
            for e in entries:
                try:
                    day = time.mktime(time.strptime(e.name, "%Y-%m-%d"))
                except ValueError:
                    continue
                if e.is_dir() and day < cutoff:
                    shutil.rmtree(e.path, ignore_errors=True)
                    removed.append(e.path)
        return removed


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
