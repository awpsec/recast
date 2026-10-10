"""Automation: keep the library optimized, one file at a time.

Every file gets a score — the estimated space saving with your preferred preset (measured from
real encodes of that show when there are any):

    score ≥ auto_threshold    → encode, and replace by itself if the *real* saving also clears it
    review ≤ score < auto     → "review" list, decided by you before anything is encoded
    score < review_threshold  → skipped (remembered until the file changes)

A file the preset would lose something on besides bitrate (4K → 1080p, HDR, surround) is never
automatic: however big the saving, it goes to review.

Pacing: automation never creates more than one running job plus one queued behind it (so the next
copy overlaps the current encode). Each file is read from the NAS once, sequentially, and written
back once. A whole library trickles through instead of flooding scratch.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime

from .config import config_dir
from .encode import EncodeSettings, est_bytes, quality_loss, skip_reason
from .engine import ACTIVE, norm, show_root


class Automation:
    def __init__(self, svc):
        self.svc = svc
        self.file = config_dir() / "automation.json"
        self.queue: list[dict] = []        # clear wins, biggest saving first (rebuilt, not persisted)
        self.review: dict[str, dict] = {}  # path → plan: borderline, waiting for you
        self.skipped: dict[str, dict] = {} # path → {reason, size, mtime, when, by_you}
        self.log: list[dict] = []          # what it did / decided, newest last
        self.pending: list[dict] = []      # files Sonarr/Radarr just imported, looked at after a short delay
        self.last_sweep = 0.0
        self.status = "off"
        self.counts: dict = {}
        self._lock = threading.RLock()
        self._dirty = False
        self.load()
        svc.subscribe(self.on_event)

    # ── persistence ──
    def load(self) -> None:
        try:
            d = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return
        self.review, self.skipped = d.get("review", {}), d.get("skipped", {})
        self.log, self.last_sweep = d.get("log", []), d.get("last_sweep", 0.0)
        self.pending = d.get("pending", [])

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        with self._lock:
            data = {"review": self.review, "skipped": self.skipped, "log": self.log[-500:],
                    "last_sweep": self.last_sweep, "pending": self.pending}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.file)
        self._dirty = False

    def note(self, action: str, path: str = "", **kw) -> None:
        with self._lock:
            self.log.append({"t": time.time(), "action": action, "path": path, **kw})
            del self.log[:-500]
            self._dirty = True

    # ── decisions ──
    def preset_for(self, path: str) -> tuple[str, EncodeSettings]:
        cfg, presets = self.svc.cfg, self.svc.presets
        anime = self.svc.scanner.series_type(path) == "anime"
        name = (cfg.auto_preset_anime if anime and cfg.auto_preset_anime else cfg.auto_preset) or cfg.default_preset
        if name not in presets:
            name = cfg.default_preset if cfg.default_preset in presets else next(iter(presets))
        return name, presets[name][1]

    def evaluate(self, m, auto_t: float | None = None, review_t: float | None = None) -> dict:
        """Score one file. Never touches the network."""
        cfg = self.svc.cfg
        auto_t = cfg.auto_threshold if auto_t is None else auto_t
        review_t = cfg.review_threshold if review_t is None else review_t
        name, s = self.preset_for(m.path)
        plan = {"path": m.path, "preset": name, "size": m.size, "est_out": float(m.size), "pct": 0.0,
                "measured": False, "codec": m.codec, "when": time.time()}
        why = skip_reason(replace(s, skip_same=True), m, cfg.encoders)
        if why:
            return {**plan, "verdict": "skip", "reason": why}
        meas = self.svc.engine.measured(m.path).get(name)
        out = m.size * meas[0] if meas else est_bytes(s, m, cfg.encoders)
        pct = 1 - out / max(1, m.size)
        verdict = "auto" if pct >= auto_t else "review" if pct >= review_t else "skip"
        reason = "" if verdict != "skip" else f"would save only {pct * 100:.0f}%"
        loss = quality_loss(s, m) if verdict == "auto" else ""
        if loss:  # a big saving that costs resolution/HDR/surround is your call, not automation's
            verdict, reason = "review", loss
        return {**plan, "est_out": out, "pct": pct, "measured": bool(meas), "verdict": verdict, "reason": reason}

    def rebuild(self) -> dict:
        """Re-score everything the scanner knows (cheap: cached headers). Keeps your own skips."""
        eng = self.svc.engine
        done, busy = eng.done_sizes(), eng.busy_paths()
        queue, counts = [], {"auto": 0, "auto_bytes": 0.0, "review": 0, "skip": 0, "done": 0, "unread": 0}
        with self._lock:
            for root, path, size, mtime, m in self.svc.scanner.files():
                k = norm(path)
                if done.get(k) == size:
                    counts["done"] += 1
                    continue
                if k in busy:
                    continue
                sk = self.skipped.get(path)
                if sk and sk.get("size") == size and int(sk.get("mtime", 0)) == int(mtime):
                    counts["skip"] += 1
                    continue
                if path in self.review:
                    counts["review"] += 1
                    continue
                if not m:
                    counts["unread"] += 1
                    continue
                p = self.evaluate(m)
                if p["verdict"] == "auto":
                    queue.append(p)
                    counts["auto"] += 1
                    counts["auto_bytes"] += p["size"] - p["est_out"]
                elif p["verdict"] == "review":
                    self.review[path] = p
                    counts["review"] += 1
                else:
                    self.skipped[path] = {"reason": p["reason"], "size": size, "mtime": mtime, "when": time.time()}
                    counts["skip"] += 1
            queue.sort(key=lambda p: -(p["size"] - p["est_out"]))  # biggest wins first
            self.queue, self.counts = queue, counts
            self._dirty = True
        return counts

    def reset_decisions(self) -> None:
        """Thresholds or presets changed: forget automatic verdicts (your own skips stay)."""
        with self._lock:
            self.skipped = {p: v for p, v in self.skipped.items() if v.get("by_you")}
            self.review = {}
        self.rebuild()

    def preview(self, auto_t: float, review_t: float) -> dict:
        """What these thresholds would do to the library right now (for the settings sliders)."""
        out = {"auto": 0, "auto_bytes": 0.0, "review": 0, "review_bytes": 0.0, "skip": 0}
        done = self.svc.engine.done_sizes()
        for _root, path, size, _mtime, m in self.svc.scanner.files():
            if not m or done.get(norm(path)) == size:
                continue
            sk = self.skipped.get(path)
            if sk and sk.get("by_you"):
                continue
            p = self.evaluate(m, auto_t, review_t)
            if p["verdict"] == "skip":
                out["skip"] += 1
            else:
                out[p["verdict"]] += 1
                out[p["verdict"] + "_bytes"] += p["size"] - p["est_out"]
        return out

    # ── pacing ──
    def in_hours(self, now: datetime | None = None) -> bool:
        h = (self.svc.cfg.auto_hours or "").strip()
        if not h:
            return True
        try:
            a, b = (datetime.strptime(x.strip(), "%H:%M").time() for x in h.split("-"))
        except ValueError:
            return True
        t = (now or datetime.now()).time()
        return a <= t < b if a <= b else (t >= a or t < b)  # windows can wrap past midnight

    def tick(self) -> None:
        """Called every couple of seconds. Starts at most one new job, and only when the pipeline has room."""
        cfg, eng = self.svc.cfg, self.svc.engine
        mode = cfg.auto_mode
        if mode == "off":
            self.status = "off"
            return
        if eng.hold:
            self.status = "paused — you paused the queue"
            return
        auto = [j for j in eng.jobs.values() if j.origin in ("auto", "review") and j.stage in ACTIVE]
        waiting = [j for j in auto if j.stage in ("queued", "copying", "ready")]
        cap = 2 if cfg.prefetch else 1  # one encoding + one copied ahead, never more
        if mode == "on" and (waiting or len(auto) >= cap):
            self.status = "working"
            return
        if not self.in_hours():
            self.status = f"waiting for {cfg.auto_hours}" if not auto else "working"
            return
        if eng.held_bytes() > cfg.max_scratch_gb * 1024**3:
            self.status = "paused — scratch is full of encodes waiting for your review"
            return
        nxt = self._next_valid()
        if not nxt:
            self.status = "idle — nothing above your threshold right now"
            return
        if mode == "dry":
            self.status = f"dry run — next would be {os.path.basename(nxt['path'])}"
            return
        root = self.svc.root_for(nxt["path"])
        m = self.svc.probes.cached(nxt["path"])
        if not root or not m:
            with self._lock:
                self.queue.pop(0)
            return
        name, s = self.preset_for(nxt["path"])
        j = eng.add_single(m, root, s, name, preview=False)
        j.origin, j.auto_min_saving = "auto", cfg.auto_threshold
        with self._lock:
            self.queue.pop(0)
        self.note("encode", nxt["path"], preset=name, pct=nxt["pct"], measured=nxt["measured"], job=j.id)
        self.status = "working"

    def _next_valid(self) -> dict | None:
        eng = self.svc.engine
        done, busy = eng.done_sizes(), eng.busy_paths()
        with self._lock:
            while self.queue:
                p = self.queue[0]
                k = norm(p["path"])
                if done.get(k) == p["size"] or k in busy:
                    self.queue.pop(0)
                    continue
                root = self.svc.root_for(p["path"])
                if root and eng.root_ok_path(root.path):
                    try:
                        st = os.stat(p["path"])
                    except OSError:
                        self.queue.pop(0)  # gone (deleted/renamed): the next sweep picks up whatever replaced it
                        continue
                    if st.st_size != p["size"]:
                        self.queue.pop(0)
                        continue
                    return p
                return None  # library offline: wait, don't throw the plan away
        return None

    # ── your decisions ──
    def encode_review(self, path: str) -> int | None:
        """You said go on a borderline file. It replaces by itself only if it saves at least the review threshold."""
        with self._lock:
            p = self.review.pop(path, None)
        if not p:
            return None
        root, m = self.svc.root_for(path), self.svc.probes.get(path)
        if not root or m.error:
            return None
        name, s = self.preset_for(path)
        j = self.svc.engine.add_single(m, root, s, name, preview=True)
        j.origin, j.auto_min_saving = "review", self.svc.cfg.review_threshold
        self.note("encode", path, preset=name, pct=p["pct"], reviewed=True, job=j.id)
        return j.id

    def skip_many(self, reason: str | None = None) -> int:
        """Skip every borderline file (or only those with this reason; "" = the plain 'saving in between' ones)."""
        with self._lock:
            paths = [p for p, v in self.review.items() if reason is None or v.get("reason", "") == reason]
        for p in paths:
            self.skip_review(p)
        return len(paths)

    def skip_review(self, path: str) -> None:
        with self._lock:
            p = self.review.pop(path, None)
            try:
                st = os.stat(path)
                self.skipped[path] = {"reason": "skipped by you", "by_you": True, "size": st.st_size,
                                      "mtime": st.st_mtime, "when": time.time()}
            except OSError:
                pass
        if p:
            self.note("skip", path, reason="skipped by you")

    # ── Sonarr / Radarr ──
    def webhook(self, source: str, payload: dict) -> str | None:
        """Sonarr/Radarr Connect → Webhook. Returns the local path we'll look at, or None."""
        event = payload.get("eventType", "")
        if event == "Test":
            self.note("webhook-test", source=source)
            return None
        if event not in ("Download", "Upgrade", "Rename", "MovieFileImported", "EpisodeFileImported"):
            return None
        if source == "sonarr":
            f, base = payload.get("episodeFile") or {}, (payload.get("series") or {}).get("path", "")
        else:
            f, base = payload.get("movieFile") or {}, (payload.get("movie") or {}).get("folderPath", "")
        remote = f.get("path") or (os.path.join(base, f.get("relativePath", "")) if base and f.get("relativePath") else "")
        if not remote:
            return None
        from .arr import ArrClient
        cfg = self.svc.cfg.sonarr if source == "sonarr" else self.svc.cfg.radarr
        local = ArrClient(source.title(), cfg).to_local(remote) if cfg.path_map else remote
        if not self.svc.in_library(local):
            self.note("webhook-ignored", local, reason="not inside a library folder (check path mapping)",
                      source=source)
            return None
        stype = (payload.get("series") or {}).get("type")
        if stype:  # Sonarr tells us anime vs standard right in the payload
            self.svc.scanner.type_hints[norm(show_root(local))] = stype
        with self._lock:
            self.pending = [p for p in self.pending if p["path"] != local]
            self.pending.append({"path": local, "due": time.time() + 90, "source": source})
            self._dirty = True
        self.note("webhook", local, source=source, event=event)
        return local

    def process_pending(self) -> None:
        """Blocking (thread): look at newly imported files once Sonarr/Radarr are done with them."""
        now = time.time()
        with self._lock:
            due = [p for p in self.pending if p["due"] <= now]
            self.pending = [p for p in self.pending if p["due"] > now]
        for item in due:
            path = item["path"]
            if not os.path.exists(path):
                continue
            m = self.svc.probes.get(path)
            root = self.svc.root_for(path)
            if m.error or not root:
                continue
            self.svc.scanner.add_info(root.path, m)
            with self._lock:
                self.skipped.pop(path, None)
                p = self.evaluate(m)
                if p["verdict"] == "auto":
                    self.queue = [q for q in self.queue if q["path"] != path]
                    self.queue.insert(0, p)  # new arrivals go first
                elif p["verdict"] == "review":
                    self.review[path] = p
                else:
                    self.skipped[path] = {"reason": p["reason"], "size": m.size, "mtime": m.mtime, "when": now}
            self.note("new", path, verdict=p["verdict"], pct=p["pct"], source=item["source"])

    def sweep(self) -> None:
        """Blocking (thread): re-list every library folder (new/changed files get their headers read) and re-score."""
        for r in self.svc.cfg.roots:
            if os.path.isdir(r.path):
                self.svc.scanner.scan(r.path, relist=True)
        self.last_sweep = time.time()
        counts = self.rebuild()
        self.note("sweep", counts=counts)
        self.save(force=True)

    def sweep_due(self) -> bool:
        return time.time() - self.last_sweep > max(1, self.svc.cfg.sweep_hours) * 3600

    # ── outcomes ──
    def on_event(self, kind: str, obj) -> None:
        if kind == "replaced":  # by anyone: it's no longer a candidate
            with self._lock:
                self.queue = [p for p in self.queue if p["path"] != obj.src]
                self.review.pop(obj.src, None)
        origin = getattr(obj, "origin", "")
        if origin not in ("auto", "review"):
            return
        if kind == "auto_done" and obj.stage == "awaiting":
            self.note("needs-you", obj.src, why=obj.flag or obj.note, job=obj.id)
        elif kind == "replaced":
            self.note("replaced", obj.final or obj.src, saved=obj.info.get("size", 0) - obj.out_size,
                      pct=1 - obj.out_size / max(1, obj.info.get("size", 1)), job=obj.id)
        elif kind == "failed":
            self.note("failed", obj.src, error=obj.error, job=obj.id)
