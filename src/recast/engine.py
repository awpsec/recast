"""The job pipeline: copy → encode → verify → (approve) → replace.

One encode at a time, one file prefetched into scratch while it runs, one
write-back to the library at a time. Everything persists to state.json so the
approval inbox and queue survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from .config import Config, config_dir
from .encode import EncodeSettings, _audio_indices, _sub_indices, build_command
from .ffmpeg import NO_WINDOW, parse_progress, run
from .probe import MediaInfo, probe_now

ACTIVE = ("queued", "copying", "ready", "encoding", "paused", "verifying")
LIVE = ("encoding", "paused", "verifying")
DONE = ("awaiting", "to_replace", "replacing", "replaced", "kept")
FINAL = ("replaced", "kept", "discarded", "cancelled", "skipped", "failed")


class Cancelled(Exception):
    pass


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
        data = {"next_id": self.next_id,
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
        j = self._job(m, root, s, preset, preview)
        self.touch()
        self.save(force=True)
        return j

    def add_batch(self, folder: str, media: list[MediaInfo], root, s: EncodeSettings, preset: str,
                  skip: Callable[[MediaInfo], bool]) -> Batch:
        b = Batch(self.next_id, folder, asdict(s), preset)
        self.next_id += 1
        for m in media:
            j = self._job(m, root, s, preset, False, b.id)
            if skip(m):
                j.stage, j.result = "skipped", "already in target codec"
            b.jobs.append(j.id)
        self.batches[b.id] = b
        self.touch()
        self.save(force=True)
        return b

    # ── scheduling (called ~4x a second by the app) ──
    def tick(self) -> None:
        jobs = list(self.jobs.values())
        by = lambda st: sorted((j for j in jobs if j.stage == st), key=lambda j: (not j.preview, j.id))
        busy_copy = any(j.stage == "copying" for j in jobs)
        live = any(j.stage in LIVE for j in jobs)
        ready, queued = by("ready"), by("queued")
        if not busy_copy and (not ready or not self.cfg.prefetch and not live) and queued:
            j = queued[0]
            over = self.held_bytes() > self.cfg.max_scratch_gb * 1024**3
            if j.batch and over:
                self.on_event("budget", j)
            elif not (live and not self.cfg.prefetch):
                self._start(j, "copying", self._copy)
        if not live and ready:
            self._start(ready[0], "encoding", self._encode)
        if not any(j.stage == "replacing" for j in jobs):
            nxt = next((j for j in sorted(jobs, key=lambda j: j.id) if j.stage == "to_replace"), None)
            if nxt:
                self._start(nxt, "replacing", self._replace)
        self.save()

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
        except OSError as e:
            j.stage, j.error = "awaiting", f"replace failed: {e}"
            j.add_log(j.error)
            self.on_event("failed", j)
            self.touch()
            return
        j.stage, j.result, j.finished = ("kept" if keep_both else "replaced"), msg, time.time()
        j.add_log(msg)
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

    def _replace_files(self, j: Job, keep_both: bool) -> str:
        src = j.src
        d = os.path.dirname(src)
        stem, ext = os.path.splitext(os.path.basename(src))
        new_ext = "." + j.s.container
        final = os.path.join(d, stem + (".recast" if keep_both else "") + new_ext)
        tmp = os.path.join(d, f".recast-{j.id}{new_ext}.part")
        if j.remote or not _same_device(j.out, d):
            self._copy_file(j.out, tmp, j, "replace_done")
        else:
            shutil.move(j.out, tmp)
            j.replace_done = j.out_size
        if keep_both:
            os.replace(tmp, final)
            return f"kept both · new file {os.path.basename(final)}"
        moved_to = ""
        mode = self.cfg.originals
        if mode == "trash":
            rel = os.path.relpath(d, j.root_path)
            trash_dir = os.path.join(j.root_path, ".recast-trash", time.strftime("%Y-%m-%d"), rel)
            os.makedirs(trash_dir, exist_ok=True)
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
            raise
        if mode == "delete":
            if os.path.abspath(src) != os.path.abspath(final):
                os.remove(src)
        saved = j.info.get("size", 0) - j.out_size
        where = {"trash": f"original → .recast-trash ({self.cfg.trash_days} days)", "keep": "original kept as .orig",
                 "delete": "original deleted"}[mode]
        return f"replaced · saved {saved / 1024**2:,.0f} MiB · {where}"

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

    def toggle_pause(self, j: Job) -> bool:
        p = self._procs.get(j.id)
        if not p or p.returncode is not None or j.stage not in ("encoding", "paused"):
            return False
        pause = j.stage == "encoding"
        self._signal(p.pid, cont=not pause)
        j.stage = "paused" if pause else "encoding"
        self.touch()
        return True

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
        for jid, p in list(self._procs.items()):
            if p.returncode is None:
                j = self.jobs.get(jid)
                if j and j.stage == "paused":
                    self._signal(p.pid, cont=True)
                p.terminate()
        self.save(force=True)

    # ── housekeeping ──
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


def _same_device(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False
