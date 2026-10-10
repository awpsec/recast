"""The UI-independent core: engine, header cache, Sonarr/Radarr, library scanning and automation.

Both front ends use this: the terminal app (`recast`) and the web daemon (`recast web`).
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Callable, Iterator

from .arr import ArrIndex
from .config import Config, Root, config_dir
from .encode import load_presets
from .engine import Engine, norm, show_root
from .overview import ShowStat, list_files, load_snapshot, rank, save_snapshot
from .probe import MediaInfo, ProbeCache


class LibraryScanner:
    """Lists every video file under a library folder, reads headers for new ones (cached), ranks shows.

    `scan()` blocks — run it in a thread. It never waits on a UI: results land in `stats`/`show_stats`
    and `dirty` flips so whoever draws can pick them up."""

    def __init__(self, svc: "Service"):
        self.svc = svc
        self.state: dict | None = None          # {"root", "phase": listing|reading, "done", "total", "t0"}
        self.stats: dict[str, list[ShowStat]] = {}
        self.show_stats: dict[str, ShowStat] = {}
        self.entries: dict[str, list] = {}      # root → [(path, size, mtime)]
        self.infos: dict[str, dict[str, MediaInfo]] = {}
        self.snap_when: dict[str, float] = {}
        self.rank_by: str | None = "★"          # "★" = default preset, None = best of all, else a preset name
        self.dirty = False
        self.type_hints: dict[str, str] = {}   # show folder → "anime"/"standard" from Sonarr webhooks
        self._lock = threading.Lock()

    # ── loading / scanning ──
    def load_cached(self, root: str) -> bool:
        """Use the last listing + cached headers without touching the network. False if never scanned."""
        snap = load_snapshot(root)
        if not snap:
            return False
        entries = [tuple(e) for e in snap["files"]]
        infos = {p: m for p, size, mtime in entries if (m := self.svc.probes.cached_meta(p, size, mtime))}
        with self._lock:
            self.entries[root], self.infos[root], self.snap_when[root] = entries, infos, snap["when"]
        self.publish(root)
        return True

    def scan(self, root: str, relist: bool = False, cancelled: Callable[[], bool] = lambda: False) -> float:
        """Blocking. Returns seconds spent reading new headers (0 if everything was cached)."""
        snap = None if relist else load_snapshot(root)
        if snap:
            entries = [tuple(e) for e in snap["files"]]
            when = snap["when"]
        else:
            self.state = {"root": root, "phase": "listing", "done": 0, "total": 0}

            def listed(n):
                self.state["done"] = n
            entries = list_files(root, listed, cancelled)
            if cancelled():
                self.state = None
                return 0.0
            save_snapshot(root, entries)
            when = time.time()
        infos: dict[str, MediaInfo] = {}
        todo = []
        for p, size, mtime in entries:
            m = self.svc.probes.cached_meta(p, size, mtime)
            if m:
                infos[p] = m
            else:
                todo.append(p)
        with self._lock:
            self.entries[root], self.infos[root], self.snap_when[root] = entries, infos, when
        self.state = {"root": root, "phase": "reading", "done": 0, "total": len(todo), "t0": time.monotonic()}
        self.publish(root)
        t0 = time.monotonic()
        if todo:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            last = time.monotonic()
            with ThreadPoolExecutor(max_workers=6) as pool:  # headers only; a few at a time is kind to the NAS
                futs = {pool.submit(self.svc.probes.get, p): p for p in todo}
                for i, f in enumerate(as_completed(futs), 1):
                    if cancelled():
                        for x in futs:
                            x.cancel()
                        self.state = None
                        return 0.0
                    m = f.result()
                    if not m.error:
                        infos[futs[f]] = m
                    self.state["done"] = i
                    if time.monotonic() - last > 5:
                        last = time.monotonic()
                        self.publish(root)
                    if i % 300 == 0:
                        self.svc.probes.save()  # a quit halfway keeps what was read
            self.svc.probes.save()
        took = time.monotonic() - t0 if todo else 0.0
        self.state = None
        self.publish(root)
        return took

    def publish(self, root: str) -> None:
        """Rank (pure arithmetic on cached headers — 10k files in ~20 ms) and flag a redraw."""
        entries, infos = self.entries.get(root), self.infos.get(root, {})
        if entries is None:
            return
        svc = self.svc
        only = svc.cfg.default_preset if self.rank_by == "★" else self.rank_by
        if only is not None and only not in svc.presets:
            only = None
        stats = rank(root, entries, infos, svc.presets, svc.cfg.encoders, svc.engine.done_paths(),
                     svc.engine.measured, self.title_of(root), only=only, kind_of=self.series_type)
        with self._lock:
            self.stats[root] = stats
            self.show_stats.update({norm(st.path): st for st in stats})
        self.dirty = True

    def title_of(self, root: str) -> Callable[[str], str]:
        nroot = norm(root)

        def title(path: str) -> str:
            t = show_root(path)
            return t if norm(t).startswith(nroot) else root
        return title

    def series_type(self, path: str) -> str | None:
        hint = self.type_hints.get(norm(show_root(path)))
        if hint:
            return hint
        rec = self.svc.arr.lookup(path) if self.svc.arr else None
        return (rec or {}).get("series_type") or None

    def files(self) -> Iterator[tuple[str, str, int, float, MediaInfo | None]]:
        """(root, path, size, mtime, info or None) for every listed file in every library folder."""
        with self._lock:
            snapshot = [(r, list(self.entries.get(r, [])), dict(self.infos.get(r, {}))) for r in self.entries]
        for root, entries, infos in snapshot:
            for p, size, mtime in entries:
                yield root, p, size, mtime, infos.get(p)

    def file_replaced(self, src: str, final: str, size: int) -> None:
        """A replace renamed/shrank a file: update the listing in place (no relist, no network) and re-rank."""
        hit = None
        with self._lock:
            for root, ents in self.entries.items():
                i = next((i for i, e in enumerate(ents) if e[0] == src), None)
                if i is not None:
                    ents[i] = (final, size, time.time())
                    self.infos.get(root, {}).pop(src, None)
                    save_snapshot(root, ents, self.snap_when.get(root))  # a restart sees the new name too
                    hit = root
                    break
        if hit:
            self.publish(hit)  # "could still free" drops right away

    def add_info(self, root: str, m: MediaInfo) -> None:
        """A file we just looked at (webhook): make it part of the listing without a rescan."""
        with self._lock:
            ents = self.entries.setdefault(root, [])
            if not any(e[0] == m.path for e in ents):
                ents.append((m.path, m.size, m.mtime))
            self.infos.setdefault(root, {})[m.path] = m


class Service:
    """Everything recast does, without a UI. Subscribe to engine/automation events with `subscribe`."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load()
        self.presets = load_presets()
        self.probes = ProbeCache(self.cfg.ffprobe)
        self._listeners: list[Callable[[str, object], None]] = [self._on_engine]
        self.engine = Engine(self.cfg, self._emit)
        self.arr: ArrIndex | None = None
        self.scanner = LibraryScanner(self)
        from .automation import Automation
        self.automation = Automation(self)
        if not self.cfg.webhook_token:
            self.cfg.webhook_token = secrets.token_urlsafe(18)
            if not self.cfg.needs_setup:
                self.cfg.save()

    def _on_engine(self, kind: str, obj) -> None:
        if kind == "replaced" and getattr(obj, "final", ""):
            self.scanner.file_replaced(obj.src, obj.final, obj.out_size)

    def subscribe(self, fn: Callable[[str, object], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, kind: str, obj) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, obj)
            except Exception:  # noqa: BLE001 — one bad listener must not stop the engine
                import traceback
                traceback.print_exc()

    def reload_presets(self) -> None:
        self.presets = load_presets()

    def connect_arr(self) -> ArrIndex | None:
        """(Re)build the Sonarr/Radarr index from config. Call `self.arr.refresh()` in a thread after."""
        if self.cfg.sonarr.enabled or self.cfg.radarr.enabled:
            self.arr = ArrIndex(self.cfg.sonarr, self.cfg.radarr)
        else:
            self.arr = None
        self.engine.arr = self.arr
        return self.arr

    def root_for(self, path: str) -> Root | None:
        best = None
        for r in self.cfg.roots:
            if (norm(path) == norm(r.path) or norm(path).startswith(norm(r.path) + os.sep)) and \
                    (not best or len(r.path) > len(best.path)):
                best = r
        return best

    def in_library(self, path: str) -> bool:
        return self.root_for(path) is not None

    def saved_total(self) -> int:
        return sum(h["src_size"] - h["out_size"] for h in self.engine.history if not h.get("restored"))



def instance_lock(kind: str) -> str | None:
    """Claim this config folder for one running recast (two engines would fight over the same queue and
    scratch). Returns a message if another live process holds it."""
    p = config_dir() / "instance.lock"
    try:
        pid, other = p.read_text().split(":", 1)
        if int(pid) != os.getpid() and _alive(int(pid)):
            hint = " — open it in your browser instead" if other.startswith("web") else ""
            return f"recast is already running on this machine ({other.strip()}, pid {pid}){hint}. Stop it first."
    except (OSError, ValueError):
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{os.getpid()}:{kind}")
    return None


def release_lock() -> None:
    p = config_dir() / "instance.lock"
    try:
        if p.read_text().split(":", 1)[0] == str(os.getpid()):
            p.unlink()
    except (OSError, ValueError):
        pass


def _alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) on Windows sends CTRL_C_EVENT — never do that
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
