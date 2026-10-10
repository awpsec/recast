"""recast-server's web app: the engine + automation daemon behind a browser UI.

One process owns the queue. The browser talks JSON to /api/* and listens to /api/events
(server-sent events) for live progress. Sonarr/Radarr post to /api/hook/<kind>?token=….
Every path that comes from the browser must be inside a configured library folder.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import socket
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from ..arr import ArrClient, ArrError, auto_map
from ..config import Root, config_dir, default_scratch, is_network_path, machine_summary, suggest_libraries
from ..encode import (CODEC_LABEL, SCHEMA, EncodeSettings, already_target, bitrate_word, crf_word, delete_preset,
                      est_bytes, est_fps, resolve_encoder, save_preset, skip_reason)
from ..automation import MODES
from ..engine import ACTIVE, LIVE, Batch, Job, norm
from ..overview import duplicate_groups, pretty_title
from ..probe import VIDEO_EXT
from ..service import Service

log = logging.getLogger("recast")

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "recast_session"


# ───────────────────────────── serialisation ─────────────────────────────

def job_dict(svc: Service, j: Job, detail: bool = False) -> dict:
    m = j.info
    size = m.get("size", 0) or 0
    live = j.stage in LIVE
    eta = (m.get("duration", 0) - j.out_time) / j.speed if live and j.speed else None
    d = {"id": j.id, "name": j.name, "src": j.src, "stage": j.stage, "origin": j.origin, "preset": j.preset,
         "batch": j.batch, "progress": round(j.progress, 4), "frame": j.frame, "frames": m.get("frames", 0),
         "fps": round(j.fps, 1), "speed": round(j.speed, 2), "out_size": j.out_size,
         "projected": j.projected if live else j.out_size, "src_size": size, "copied": j.copied,
         "replace_done": j.replace_done, "out_time": j.out_time, "duration": m.get("duration", 0), "eta": eta,
         "flag": j.flag, "error": j.error, "note": j.note, "result": j.result, "phase": j.phase,
         "remote": j.remote, "started": j.started, "finished": j.finished, "waiting_since": j.waiting_since,
         "codec_from": m.get("codec", "?"), "codec_to": CODEC_LABEL.get(j.s.codec, "?"),
         "encoder": resolve_encoder(j.s, svc.cfg.encoders)[0], "kbps": j.kbps,
         "has_frame": bool(j.preview_jpg and os.path.exists(j.preview_jpg)),
         "out_info": {k: j.out_info.get(k) for k in ("codec", "width", "height", "vkbps", "duration")}
         if j.out_info else None,
         "src_info": {k: m.get(k) for k in ("codec", "width", "height", "vkbps", "duration", "hdr")},
         "audio": [len(m.get("audio", [])), len((j.out_info or {}).get("audio", []))],
         "subs": [len(m.get("subs", [])), len((j.out_info or {}).get("subs", []))]}
    if detail:
        d["log"] = j.log[-40:]
        d["spark"] = j.spark[-120:]
    return d


def batch_dict(svc: Service, b: Batch) -> dict:
    e = svc.engine
    jobs = e.batch_jobs(b)
    done = e.batch_jobs(b, "awaiting", "to_replace", "replacing", "replaced")
    todo = [j for j in jobs if j.stage not in ("skipped", "cancelled", "discarded")]
    src = sum(j.info.get("size", 0) for j in done)
    out = sum(j.out_size for j in done)
    return {"id": b.id, "folder": b.folder, "name": " / ".join(b.folder.replace("\\", "/").split("/")[-2:]),
            "preset": b.preset, "approved": b.approved, "files": len(todo), "done": len(done),
            "active": len(e.batch_jobs(b, *ACTIVE)), "flagged": sum(1 for j in done if j.flag),
            "src": src, "out": out, "waiting_since": b.waiting_since,
            "items": [job_dict(svc, j) for j in sorted(done, key=lambda j: (not j.flag, -j.finished))[:50]]}


def live_state(svc: Service) -> dict:
    e, a = svc.engine, svc.automation
    jobs = sorted(e.jobs.values(), key=lambda j: j.id)
    active = [j for j in jobs if j.stage in ACTIVE or j.stage in ("to_replace", "replacing")]
    return {"type": "live", "t": time.time(),
            "active": [job_dict(svc, j) for j in active if j.stage not in ("queued",)][:4],
            "queued": sum(1 for j in jobs if j.stage == "queued"),
            "inbox": len(e.inbox()), "review": len(a.review), "hold": e.hold, "offline": sorted(e.offline),
            "saved": svc.saved_total(), "scratch": e.held_bytes(),
            "automation": {"mode": svc.cfg.auto_mode, "status": a.status,
                           "next": os.path.basename(a.queue[0]["path"]) if a.queue else None,
                           "queue": len(a.queue)},
            "scan": svc.scanner.state, "needs_setup": svc.cfg.needs_setup}


# ───────────────────────────── helpers ─────────────────────────────

def _path_arg(request: web.Request, svc: Service, key: str = "path", body: dict | None = None) -> str:
    raw = (body or {}).get(key) if body is not None else request.query.get(key, "")
    p = os.path.abspath(os.path.expanduser(raw or ""))
    if not raw or not svc.in_library(p):
        raise web.HTTPBadRequest(text=json.dumps({"error": "that path isn't inside a library folder"}),
                                 content_type="application/json")
    return p


def _ok(data=None, **kw) -> web.Response:
    return web.json_response({"ok": True, **(data or {}), **kw}, dumps=lambda o: json.dumps(o, default=str))


def _err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def _hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()


def _check_pw(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_pw(pw, salt), stored)


def _gains(svc: Service, infos: list, path: str) -> list[dict]:
    """What every preset would do to these files (measured on this show where possible)."""
    caps = svc.cfg.encoders
    done = svc.engine.done_paths()
    measured = svc.engine.measured(path)
    rows = []
    for name, (desc, s) in svc.presets.items():
        fresh = [m for m in infos if norm(m.path) not in done]
        todo = [m for m in fresh if not already_target(s, m, caps)]
        src = sum(m.size for m in todo)
        if not todo:
            rows.append({"preset": name, "files": 0, "src": 0, "out": 0, "pct": None,
                         "why": "done by recast" if not fresh else (skip_reason(s, fresh[0], caps) or "no saving")})
            continue
        if name in measured:
            out, how = src * measured[name][0], "measured"
        else:
            out, how = sum(est_bytes(s, m, caps) for m in todo), "estimate"
        rows.append({"preset": name, "files": len(todo), "src": src, "out": out, "pct": 1 - out / max(1, src),
                     "how": how, "encoder": resolve_encoder(s, caps)[0], "default": name == svc.cfg.default_preset})
    rows.sort(key=lambda r: -(r["pct"] if r["pct"] is not None else -9))
    return rows


def _media_dict(m) -> dict:
    return {"codec": m.codec, "profile": m.profile, "width": m.width, "height": m.height, "fps": m.fps,
            "vkbps": m.vkbps, "duration": m.duration, "size": m.size, "hdr": m.hdr, "interlaced": m.interlaced,
            "pix_fmt": m.pix_fmt, "res": m.res,
            "audio": [m.audio_label(a) for a in m.audio], "subs": [m.sub_label(x) for x in m.subs]}


# ───────────────────────────── app ─────────────────────────────

def create_app(svc: Service, host: str = "127.0.0.1", port: int = 8484, auto_detect: bool = False) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware], client_max_size=4 * 1024**2)
    app["svc"], app["host"], app["port"], app["auto_detect"] = svc, host, port, auto_detect
    if not svc.cfg.webhook_token:
        svc.cfg.webhook_token = secrets.token_urlsafe(18)
        if not svc.cfg.needs_setup:
            svc.cfg.save()
    app["clients"]: set[asyncio.Queue] = set()
    app["sessions"]: set[str] = set()
    app["setup"] = {"lines": [], "running": False, "caps": None}
    app["folder_probes"] = set()
    app["rt"] = {"loop": None, "runner": None, "sweeping": False, "tasks": set(), "update": None}  # mutable after start

    def on_event(kind: str, obj) -> None:
        msg = {"type": "event", "kind": kind}
        if isinstance(obj, tuple) and obj and isinstance(obj[0], Job):  # no_space: (job, free, need)
            msg["free"], msg["need"] = obj[1], obj[2]
            obj = obj[0]
        if isinstance(obj, Job):
            msg["job"] = job_dict(svc, obj)
        elif isinstance(obj, Batch):
            msg["batch"] = {"id": obj.id, "name": batch_dict(svc, obj)["name"]}
        elif isinstance(obj, str):
            msg["text"] = obj
        loop = app["rt"]["loop"]
        if loop:
            loop.call_soon_threadsafe(_broadcast, app, msg)
    svc.subscribe(on_event)
    svc.subscribe(_log_event)

    r = app.router
    r.add_get("/", _index)
    r.add_get("/login", _index)
    r.add_static("/static/", STATIC, show_index=False)
    r.add_post("/api/login", _login)
    r.add_post("/api/logout", _logout)
    r.add_get("/api/health", _health)
    r.add_get("/api/state", _state)
    r.add_get("/api/events", _events)
    r.add_get("/api/library/overview", _overview)
    r.add_post("/api/library/scan", _scan)
    r.add_get("/api/library/browse", _browse)
    r.add_get("/api/library/details", _details)
    r.add_post("/api/estimate", _estimate)
    r.add_post("/api/encode", _encode)
    r.add_get("/api/jobs", _jobs)
    r.add_get("/api/jobs/{id}", _job)
    r.add_post("/api/jobs/{id}/{action}", _job_action)
    r.add_post("/api/queue/clear", _queue_clear)
    r.add_post("/api/queue/hold", _queue_hold)
    r.add_get("/api/jobs/{id}/frame.jpg", _frame)
    r.add_get("/api/jobs/{id}/compare.jpg", _compare)
    r.add_get("/api/inbox", _inbox)
    r.add_post("/api/inbox/{kind}/{id}/{action}", _inbox_action)
    r.add_get("/api/automation", _automation)
    r.add_put("/api/automation", _automation_put)
    r.add_get("/api/automation/preview", _automation_preview)
    r.add_post("/api/automation/review/skip-all", _skip_all)
    r.add_post("/api/automation/review/{action}", _review_action)
    r.add_post("/api/automation/sweep", _sweep)
    r.add_post("/api/automation/exclude", _exclude)
    r.add_post("/api/hook/{kind}", _hook)
    r.add_get("/api/presets", _presets)
    r.add_put("/api/presets", _preset_put)
    r.add_delete("/api/presets", _preset_delete)
    r.add_post("/api/presets/default", _preset_default)
    r.add_get("/api/settings", _settings)
    r.add_put("/api/settings", _settings_put)
    r.add_post("/api/arr/{kind}/test", _arr_test)
    r.add_post("/api/password", _password)
    r.add_get("/api/history", _history)
    r.add_post("/api/history/restore", _restore)
    r.add_get("/api/setup", _setup_get)
    r.add_post("/api/setup/detect", _setup_detect)
    r.add_post("/api/setup/save", _setup_save)
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    return app


# ── auth ──
PUBLIC = ("/login", "/api/login", "/static/", "/api/hook/", "/api/health")


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    svc: Service = request.app["svc"]
    if svc.cfg.web_password_hash and not any(request.path == p or request.path.startswith(p) for p in PUBLIC):
        if request.cookies.get(SESSION_COOKIE) not in request.app["sessions"]:
            if request.path.startswith("/api/"):
                return _err("login required", 401)
            raise web.HTTPFound("/login")
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — the browser gets a readable error, the daemon keeps going
        import traceback
        traceback.print_exc()
        return _err(f"{type(e).__name__}: {e}", 500)


async def _login(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    if not svc.cfg.web_password_hash or _check_pw(body.get("password", ""), svc.cfg.web_password_hash):
        token = secrets.token_urlsafe(24)
        request.app["sessions"].add(token)
        resp = _ok()
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", max_age=30 * 86400)
        return resp
    await asyncio.sleep(1)  # slow down guessing
    return _err("wrong password", 401)


async def _logout(request):
    request.app["sessions"].discard(request.cookies.get(SESSION_COOKIE))
    resp = _ok()
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def _password(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    pw = body.get("password", "")
    svc.cfg.web_password_hash = _hash_pw(pw) if pw else ""
    svc.cfg.save()
    request.app["sessions"].clear()
    if pw:  # keep the person who set it logged in
        token = secrets.token_urlsafe(24)
        request.app["sessions"].add(token)
        resp = _ok(protected=True)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", max_age=30 * 86400)
        return resp
    return _ok(protected=False)


# ── pages ──
async def _index(request):
    """The page shell. Asset URLs carry their mtime so an upgrade never runs stale JS/CSS from the cache."""
    v = str(int(max((STATIC / n).stat().st_mtime for n in ("app.js", "app.css"))))
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__V__", v)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


# ── live ──
def _broadcast(app, msg: dict) -> None:
    for q in list(app["clients"]):
        if q.qsize() < 200:
            q.put_nowait(msg)


async def _events(request):
    app = request.app
    svc: Service = app["svc"]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue()
    app["clients"].add(q)
    try:
        await resp.write(f"data: {json.dumps(live_state(svc), default=str)}\n\n".encode())
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"data: {json.dumps(msg, default=str)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        app["clients"].discard(q)
    return resp


def _log_event(kind: str, obj) -> None:
    """One line per thing that happened, for `docker logs` / journalctl."""
    if isinstance(obj, tuple) and obj and isinstance(obj[0], Job):
        obj = obj[0]
    if isinstance(obj, Job):
        name = obj.name
        if kind == "replaced":
            log.info("replaced  %s · %s", name, obj.result)
        elif kind == "failed":
            log.warning("failed    %s · %s", name, obj.error)
        elif kind in ("finished", "flagged") or (kind == "auto_done" and obj.stage == "awaiting"):
            log.info("needs you %s · %s", name, obj.flag or obj.note or "encoded, waiting for approval")
        elif kind == "no_space":
            log.warning("no space  %s · scratch is full", name)
    elif isinstance(obj, Batch) and kind in ("batch_done", "batch_replaced"):
        log.info("%s %s", "batch done" if kind == "batch_done" else "batch replaced", obj.name)
    elif kind == "restored" and isinstance(obj, dict):
        log.info("restored  %s", os.path.basename(obj.get("src", "")))


async def _health(request):
    """For Docker HEALTHCHECK / uptime monitors. No login, nothing sensitive."""
    svc: Service = request.app["svc"]
    from .. import __version__
    return _ok(version=__version__, setup=not svc.cfg.needs_setup,
               active=sum(1 for j in svc.engine.jobs.values() if j.stage in ACTIVE),
               automation=svc.cfg.auto_mode)


async def _state(request):
    from .. import __version__
    svc: Service = request.app["svc"]
    app = request.app
    host = app["host"]
    exposed = host not in ("127.0.0.1", "localhost", "::1") and not svc.cfg.web_password_hash
    return _ok({**live_state(svc),
                "machine": svc.cfg.machine, "ffmpeg": svc.cfg.ffmpeg_version,
                "roots": [asdict(r) | {"reachable": os.path.isdir(r.path)} for r in svc.cfg.roots],
                "presets": list(svc.presets), "default_preset": svc.cfg.default_preset,
                "hevc_encoder": resolve_encoder(EncodeSettings(), svc.cfg.encoders)[0] if svc.cfg.encoders else "",
                "protected": bool(svc.cfg.web_password_hash), "exposed": exposed,
                "version": __version__, "update": app["rt"]["update"],
                "potential": sum(s.saves for r in svc.cfg.roots for s in svc.scanner.stats.get(r.path, [])),
                "scanned": bool(svc.scanner.stats)})


# ── library ──
async def _overview(request):
    svc: Service = request.app["svc"]
    root = request.query.get("root") or (svc.cfg.roots[0].path if svc.cfg.roots else "")
    if not any(norm(r.path) == norm(root) for r in svc.cfg.roots):
        return _err("unknown library folder")
    rank_by = request.query.get("rank")
    if rank_by is not None:
        want = None if rank_by == "best" else ("★" if rank_by in ("", "default") else rank_by)
        if want != svc.scanner.rank_by:
            svc.scanner.rank_by = want
            svc.scanner.publish(root)
    if root not in svc.scanner.entries and not svc.scanner.state:
        if not await asyncio.to_thread(svc.scanner.load_cached, root):
            _spawn(request.app, asyncio.to_thread(svc.scanner.scan, root, True), "scan")
    stats = svc.scanner.stats.get(root, [])
    groups = duplicate_groups(stats)
    rb = svc.scanner.rank_by
    return _ok({
        "root": root, "reachable": os.path.isdir(root), "scan": svc.scanner.state,
        "when": svc.scanner.snap_when.get(root),
        "rank": "default" if rb == "★" else ("best" if rb is None else rb),
        "rank_label": f"{svc.cfg.default_preset} (your default)" if rb == "★" else (rb or "best of all"),
        "shows": [{"path": s.path, "name": s.name, "size": s.size, "files": s.files, "codecs": s.codecs,
                   "best": s.best, "saves": s.saves, "measured": s.measured, "done": s.done,
                   "dup": bool(s.dup_of)} for s in stats],
        "duplicates": [{"title": pretty_title(g[0].name), "folders": [x.name for x in g]} for g in groups]})


async def _scan(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    root = body.get("root", "")
    if not any(norm(r.path) == norm(root) for r in svc.cfg.roots):
        return _err("unknown library folder")
    if svc.scanner.state:
        return _err("a scan is already running")
    _spawn(request.app, asyncio.to_thread(svc.scanner.scan, root, bool(body.get("force", True))), "scan")
    return _ok()


def _list_dir(svc: Service, path: str) -> dict:
    dirs, files = [], []
    for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
        if e.name.startswith((".", "@", "#")):
            continue
        if e.is_dir():
            st = svc.scanner.show_stats.get(norm(e.path))
            dirs.append({"path": e.path, "name": e.name, "size": st.size if st else None,
                         "saves": st.saves if st else None, "files": st.files if st else None})
        elif os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
            stt = e.stat()
            m = svc.probes.cached_meta(e.path, stt.st_size, stt.st_mtime)
            files.append({"path": e.path, "name": e.name, "size": stt.st_size, "codec": m.codec if m else None,
                          "res": m.res if m else None, "vkbps": m.vkbps if m else None})
    return {"dirs": dirs, "files": files}


async def _browse(request):
    svc: Service = request.app["svc"]
    path = _path_arg(request, svc)
    if not os.path.isdir(path):
        return _err("not a folder (or the share isn't reachable)")
    data = await asyncio.to_thread(_list_dir, svc, path)
    done, busy = svc.engine.done_paths(), svc.engine.busy_paths()
    for f in data["files"]:
        k = norm(f["path"])
        f["status"] = "done" if k in done else "busy" if k in busy else \
            "review" if f["path"] in svc.automation.review else ""
    root = svc.root_for(path)
    crumbs, p = [], path
    while root and norm(p).startswith(norm(root.path)):
        crumbs.insert(0, {"path": p, "name": os.path.basename(p.rstrip("/\\")) if norm(p) != norm(root.path)
                          else root.name})
        if norm(p) == norm(root.path):
            break
        p = os.path.dirname(p)
    return _ok({"path": path, "crumbs": crumbs, **data})


def _walk(path: str, limit: int = 3000) -> list[tuple[str, int, float]]:
    out = []
    for dp, dn, fn in os.walk(path):
        dn[:] = sorted(d for d in dn if not d.startswith((".", "@", "#")))
        for n in sorted(fn):
            if os.path.splitext(n)[1].lower() in VIDEO_EXT:
                p = os.path.join(dp, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime))
                if len(out) >= limit:
                    return out
    return out


async def _details(request):
    """File: media info + what each preset would do. Folder: summary + presets (reads missing headers in the
    background; `partial` tells the page to ask again shortly)."""
    app = request.app
    svc: Service = app["svc"]
    path = _path_arg(request, svc)
    if os.path.isfile(path):
        m = await asyncio.to_thread(svc.probes.get, path)
        if m.error:
            return _err(m.error)
        h = svc.engine.record_for(path)
        rec = svc.arr.lookup(path) if svc.arr else None
        ep = await asyncio.to_thread(svc.arr.episode, rec, path) if rec and svc.arr else None
        return _ok({"kind": "file", "path": path, "name": os.path.basename(path), "media": _media_dict(m),
                    "gains": [] if h else _gains(svc, [m], path),
                    "history": h and _history_row(svc, h),
                    "review": svc.automation.review.get(path), "arr": rec, "episode": ep,
                    "excluded": svc.automation.excluded(path),
                    "remote": bool(svc.root_for(path) and svc.root_for(path).remote)})
    entries = await asyncio.to_thread(_walk, path)
    infos, missing = [], []
    for p, size, mtime in entries:
        m = svc.probes.cached_meta(p, size, mtime)
        (infos.append(m) if m else missing.append(p))
    if missing and path not in app["folder_probes"] and len(entries) < 3000:
        app["folder_probes"].add(path)

        def probe_all():
            try:
                for p in missing:
                    svc.probes.get(p)
                svc.probes.save()
            finally:
                app["folder_probes"].discard(path)
        _spawn(app, asyncio.to_thread(probe_all), "probe")
    codecs: dict[str, int] = {}
    for m in infos:
        codecs[m.codec] = codecs.get(m.codec, 0) + m.size
    k, before, after = svc.engine.saved_under(path)
    rec = svc.arr.lookup(path) if svc.arr else None
    return _ok({"kind": "folder", "path": path, "name": os.path.basename(path.rstrip("/\\")),
                "files": len(entries), "size": sum(e[1] for e in entries), "read": len(infos),
                "partial": bool(missing), "codecs": codecs, "gains": _gains(svc, infos, path),
                "recast": {"files": k, "before": before, "after": after}, "arr": rec,
                "last_used": svc.engine.last_used(path), "excluded": svc.automation.excluded(path),
                "excluded_here": any(norm(x) == norm(path) for x in svc.cfg.auto_exclude)})


# ── encoding ──
def _settings_from(body: dict, svc: Service) -> tuple[str, EncodeSettings]:
    name = body.get("preset") or svc.cfg.default_preset
    if body.get("settings"):
        s = EncodeSettings.from_dict({**asdict(EncodeSettings()), **body["settings"]})
        if name in svc.presets and asdict(svc.presets[name][1]) != asdict(s):
            name += "*"
        return name, s
    if name not in svc.presets:
        raise ValueError(f"unknown preset {name}")
    return name, svc.presets[name][1]


async def _targets(svc: Service, path: str, folder: bool):
    files = [p for p, _, _ in await asyncio.to_thread(_walk, path, 100000)] if folder else [path]
    infos = []
    for p in files:
        m = await asyncio.to_thread(svc.probes.get, p)
        if not m.error:
            infos.append(m)
    return infos


async def _estimate(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    path = _path_arg(request, svc, body=body)
    try:
        name, s = _settings_from(body, svc)
    except ValueError as e:
        return _err(str(e))
    folder = os.path.isdir(path)
    infos = await _targets(svc, path, folder)
    busy, done = svc.engine.busy_paths(), svc.engine.done_paths()
    reasons: dict[str, int] = {}
    todo = []
    for m in infos:
        k = norm(m.path)
        why = "already queued" if k in busy else "done by recast" if k in done else skip_reason(s, m, svc.cfg.encoders)
        if why and folder:
            reasons[why] = reasons.get(why, 0) + 1
        else:
            todo.append(m)
    measured = svc.engine.measured(path).get(name)
    src = sum(m.size for m in todo)
    out = src * measured[0] if measured else sum(est_bytes(s, m, svc.cfg.encoders) for m in todo)
    secs = sum((m.frames or m.duration * 24) / est_fps(s, m, svc.cfg.encoders) for m in todo)
    enc, note = resolve_encoder(s, svc.cfg.encoders)
    ref = todo[0] if todo else (infos[0] if infos else None)
    if s.codec == "copy":
        quality = ""
    elif s.rate_mode == "crf":
        quality = crf_word(s.codec, s.crf)
    else:
        quality = bitrate_word(s.codec, s.bitrate, ref.width, ref.height, ref.fps) if ref and ref.width else ""
    return _ok({"quality": quality, "preset": name, "settings": asdict(s), "files": len(todo), "src": src, "out": out,
                "pct": 1 - out / max(1, src), "measured": bool(measured), "seconds": secs, "encoder": enc,
                "encoder_note": "" if note == "auto" else note, "skipped": reasons,
                "first": todo[0].path if todo else None})


async def _encode(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    path = _path_arg(request, svc, body=body)
    try:
        name, s = _settings_from(body, svc)
    except ValueError as e:
        return _err(str(e))
    root = svc.root_for(path)
    folder = os.path.isdir(path) and body.get("scope") != "first"
    infos = await _targets(svc, path, os.path.isdir(path))
    if not infos:
        return _err("no readable video files there")
    if not folder:  # one file (or "try 1 file first" on a folder)
        busy, done = svc.engine.busy_paths(), svc.engine.done_paths()
        pick = next((m for m in infos if norm(m.path) not in busy and norm(m.path) not in done
                     and not skip_reason(s, m, svc.cfg.encoders)), None) if os.path.isdir(path) else infos[0]
        if not pick:
            return _err("nothing there needs encoding with that preset")
        j = svc.engine.add_single(pick, root, s, name, preview=True)
        return _ok(job=j.id)
    b = svc.engine.add_batch(path, infos, root, s, name, lambda m: skip_reason(s, m, svc.cfg.encoders))
    n = sum(1 for j in svc.engine.batch_jobs(b) if j.stage == "queued")
    if not n:
        svc.engine.batches.pop(b.id, None)
        return _err("nothing there needs encoding with that preset")
    return _ok(batch=b.id, queued=n)


async def _jobs(request):
    svc: Service = request.app["svc"]
    jobs = sorted(svc.engine.jobs.values(), key=lambda j: -j.id)[:500]
    return _ok(jobs=[job_dict(svc, j) for j in jobs])


async def _job(request):
    svc: Service = request.app["svc"]
    j = svc.engine.jobs.get(int(request.match_info["id"]))
    return _ok(job=job_dict(svc, j, detail=True)) if j else _err("no such job", 404)


async def _job_action(request):
    svc: Service = request.app["svc"]
    j = svc.engine.jobs.get(int(request.match_info["id"]))
    if not j:
        return _err("no such job", 404)
    act = request.match_info["action"]
    if act == "cancel":
        svc.engine.cancel(j)
    elif act == "retry":
        if not svc.engine.requeue(j):
            return _err("only failed or cancelled jobs can be retried")
    else:
        return _err("unknown action")
    return _ok()


async def _queue_clear(request):
    return _ok(cleared=len(request.app["svc"].engine.forget_finished()))


async def _queue_hold(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    svc.engine.set_hold(bool(body.get("hold")))
    return _ok(hold=svc.engine.hold)


async def _frame(request):
    svc: Service = request.app["svc"]
    j = svc.engine.jobs.get(int(request.match_info["id"]))
    if not j or not j.preview_jpg or not os.path.exists(j.preview_jpg):
        raise web.HTTPNotFound()
    return web.FileResponse(j.preview_jpg, headers={"Cache-Control": "no-store"})


async def _compare(request):
    """A frame from source and output at the same moment: side=src|out|split, pos=0..1."""
    svc: Service = request.app["svc"]
    j = svc.engine.jobs.get(int(request.match_info["id"]))
    if not j or not j.out or not os.path.exists(j.out):
        raise web.HTTPNotFound()
    from ..frames import grab_frame, side_by_side
    pos = min(0.99, max(0.0, float(request.query.get("pos", 0.5))))
    side = request.query.get("side", "split")
    t = j.info.get("duration", 0) * pos
    src = j.work_src if j.work_src and os.path.exists(j.work_src) else j.src  # local copy: no NAS read
    a = await asyncio.to_thread(grab_frame, svc.cfg.ffmpeg, src, t, 1280) if side in ("src", "split") else None
    b = await asyncio.to_thread(grab_frame, svc.cfg.ffmpeg, j.out, t, 1280) if side in ("out", "split") else None
    img = side_by_side(a, b) if side == "split" else (a or b)
    if img is None:
        raise web.HTTPNotFound()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return web.Response(body=buf.getvalue(), content_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ── approvals ──
async def _inbox(request):
    svc: Service = request.app["svc"]
    items = []
    for it in svc.engine.inbox():
        if isinstance(it, Batch):
            items.append({"kind": "batch", **batch_dict(svc, it)})
        else:
            items.append({"kind": "job", **job_dict(svc, it)})
    return _ok(items=items)


async def _inbox_action(request):
    svc: Service = request.app["svc"]
    kind, iid, act = request.match_info["kind"], int(request.match_info["id"]), request.match_info["action"]
    item = svc.engine.batches.get(iid) if kind == "batch" else svc.engine.jobs.get(iid)
    if not item:
        return _err("not found", 404)
    if act == "approve":
        svc.engine.approve(item)
    elif act == "deny":
        svc.engine.deny(item)
    else:
        return _err("unknown action")
    return _ok()


# ── automation ──
AUTO_KEYS = ("auto_mode", "auto_preset", "auto_preset_anime", "auto_threshold", "review_threshold", "auto_hours",
             "sweep_hours", "prefetch", "auto_hold_max")


def _hook_urls(request) -> dict:
    svc: Service = request.app["svc"]
    host = request.app["host"]
    if host in ("0.0.0.0", "::", ""):
        try:
            host = socket.gethostbyname(socket.gethostname())
        except OSError:
            host = socket.gethostname()
    base = f"http://{host}:{request.app['port']}/api/hook"
    return {k: f"{base}/{k}?token={svc.cfg.webhook_token}" for k in ("sonarr", "radarr")}


async def _automation(request):
    svc: Service = request.app["svc"]
    a = svc.automation
    cfg = svc.cfg
    taken = svc.engine.busy_paths() | svc.engine.done_paths()
    queue = [p for p in a.queue if norm(p["path"]) not in taken]
    reasons: dict[str, int] = {}
    for p in list(a.review.values()):
        reasons[p.get("reason", "")] = reasons.get(p.get("reason", ""), 0) + 1
    want = request.query.get("reason")
    review = [p for p in list(a.review.values()) if want is None or p.get("reason", "") == want]
    return _ok({"settings": {k: getattr(cfg, k) for k in AUTO_KEYS}, "status": a.status, "counts": a.counts,
                "queue": queue[:30], "queue_total": len(queue),
                "review": sorted(review, key=lambda p: -(p["size"] - p["est_out"]))[:300],
                "review_total": len(review), "review_reasons": reasons,
                "log": a.log[-150:][::-1], "last_sweep": a.last_sweep, "pending": a.pending,
                "hooks": _hook_urls(request), "presets": list(svc.presets), "default_preset": cfg.default_preset,
                "excluded": cfg.auto_exclude,
                "arr": {"sonarr": cfg.sonarr.enabled, "radarr": cfg.radarr.enabled}})


async def _automation_put(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    cfg = svc.cfg
    before = (cfg.auto_preset, cfg.auto_preset_anime, cfg.auto_threshold, cfg.review_threshold)
    for k in AUTO_KEYS:
        if k in body:
            v = body[k]
            if k in ("auto_threshold", "review_threshold"):
                v = min(0.95, max(0.0, float(v)))
            elif k in ("sweep_hours", "auto_hold_max"):
                v = max(1, int(v))
            elif k == "auto_mode" and v not in MODES:
                return _err("mode must be one of " + ", ".join(MODES))
            elif k == "prefetch":
                v = bool(v)
            setattr(cfg, k, v)
    if cfg.review_threshold > cfg.auto_threshold:
        cfg.review_threshold = cfg.auto_threshold
    cfg.save()
    if before != (cfg.auto_preset, cfg.auto_preset_anime, cfg.auto_threshold, cfg.review_threshold):
        await asyncio.to_thread(svc.automation.reset_decisions)
    svc.automation.tick()
    return _ok(counts=svc.automation.counts, status=svc.automation.status)


async def _automation_preview(request):
    svc: Service = request.app["svc"]
    auto_t = float(request.query.get("auto", svc.cfg.auto_threshold))
    review_t = min(auto_t, float(request.query.get("review", svc.cfg.review_threshold)))
    return _ok(await asyncio.to_thread(svc.automation.preview, auto_t, review_t))


async def _review_action(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    path = _path_arg(request, svc, body=body)
    if request.match_info["action"] == "encode":
        jid = await asyncio.to_thread(svc.automation.encode_review, path)
        return _ok(job=jid) if jid else _err("couldn't start that one")
    svc.automation.skip_review(path)
    return _ok()


async def _skip_all(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    n = await asyncio.to_thread(svc.automation.skip_many, body.get("reason"))
    return _ok(skipped=n)


async def _exclude(request):
    """Leave a show/folder out of automation (or bring it back). Manual encodes still work there."""
    svc: Service = request.app["svc"]
    body = await request.json()
    path = _path_arg(request, svc, body=body)
    await asyncio.to_thread(svc.automation.set_excluded, path, bool(body.get("on", True)))
    return _ok(excluded=svc.cfg.auto_exclude)


async def _sweep(request):
    app = request.app
    if app["rt"]["sweeping"]:
        return _err("already sweeping")
    _spawn(app, _run_sweep(app), "sweep")
    return _ok()


async def _run_sweep(app):
    app["rt"]["sweeping"] = True
    try:
        await asyncio.to_thread(app["svc"].automation.sweep)
    finally:
        app["rt"]["sweeping"] = False


async def _hook(request):
    svc: Service = request.app["svc"]
    if not hmac.compare_digest(request.query.get("token", ""), svc.cfg.webhook_token or "x"):
        return _err("bad token", 403)
    kind = request.match_info["kind"]
    if kind not in ("sonarr", "radarr"):
        return _err("unknown source", 404)
    try:
        payload = await request.json()
    except ValueError:
        return _err("expected JSON")
    path = svc.automation.webhook(kind, payload)
    return _ok(path=path)


# ── presets ──
async def _presets(request):
    svc: Service = request.app["svc"]
    schema = {k: {"doc": f.doc, "options": [o.value for o in f.options(svc.cfg.encoders)],
                  "bad": [o.value for o in f.options(svc.cfg.encoders) if not o.ok]} for k, f in SCHEMA.items()}
    return _ok(presets=[{"name": n, "description": d, "settings": asdict(s), "default": n == svc.cfg.default_preset,
                         "encoder": resolve_encoder(s, svc.cfg.encoders)[0]} for n, (d, s) in svc.presets.items()],
               schema=schema)


async def _preset_put(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return _err("a preset needs a name")
    try:
        s = EncodeSettings.from_dict({**asdict(EncodeSettings()), **(body.get("settings") or {})})
    except (ValueError, TypeError) as e:
        return _err(str(e))
    old = body.get("old_name")
    save_preset(name, body.get("description", ""), s, old_name=old)
    if old and old != name and svc.cfg.default_preset == old:
        svc.cfg.default_preset = name
        svc.cfg.save()
    svc.reload_presets()
    return _ok(warning=resolve_encoder(s, svc.cfg.encoders)[1] if s.encoder != "auto" else "")


async def _preset_delete(request):
    svc: Service = request.app["svc"]
    name = request.query.get("name", "")
    if name == svc.cfg.default_preset or len(svc.presets) <= 1:
        return _err("can't delete the default (or last) preset")
    delete_preset(name)
    svc.reload_presets()
    return _ok()


async def _preset_default(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    if body.get("name") not in svc.presets:
        return _err("unknown preset")
    svc.cfg.default_preset = body["name"]
    svc.cfg.save()
    svc.scanner.publish(svc.cfg.roots[0].path) if svc.cfg.roots else None
    return _ok()


# ── settings ──
SETTING_KEYS = ("scratch", "max_scratch_gb", "prefetch", "originals", "trash_days", "trash_max_gb", "min_free_gb",
                "verify_decode", "rename_codec", "keep_awake", "rescan_after_replace", "default_preset")


async def _settings(request):
    svc: Service = request.app["svc"]
    cfg = svc.cfg
    return _ok({"settings": {k: getattr(cfg, k) for k in SETTING_KEYS},
                "roots": [asdict(r) | {"reachable": os.path.isdir(r.path)} for r in cfg.roots],
                "sonarr": {"url": cfg.sonarr.url, "has_key": bool(cfg.sonarr.api_key), "path_map": cfg.sonarr.path_map},
                "radarr": {"url": cfg.radarr.url, "has_key": bool(cfg.radarr.api_key), "path_map": cfg.radarr.path_map},
                "machine": cfg.machine, "ffmpeg": cfg.ffmpeg, "ffmpeg_version": cfg.ffmpeg_version,
                "detected_at": cfg.detected_at,
                "encoders": {k: asdict(v) for k, v in cfg.encoders.items()},
                "protected": bool(cfg.web_password_hash), "config_dir": str(config_dir()),
                "suggestions": await asyncio.to_thread(suggest_libraries)})


async def _settings_put(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    cfg = svc.cfg
    for k in SETTING_KEYS:
        if k in body:
            setattr(cfg, k, type(getattr(cfg, k))(body[k]) if not isinstance(getattr(cfg, k), bool) else bool(body[k]))
    if "roots" in body:
        roots = []
        for r in body["roots"]:
            p = os.path.abspath(os.path.expanduser(r.get("path", "")))
            if not os.path.isdir(p):
                return _err(f"not a folder: {p}")
            roots.append(Root(r.get("name") or os.path.basename(p.rstrip("/\\")) or p, p,
                              bool(r["remote"]) if "remote" in r else is_network_path(p)))
        cfg.roots = roots
    for kind in ("sonarr", "radarr"):
        if kind in body:
            a = getattr(cfg, kind)
            a.url = body[kind].get("url", a.url).strip()
            if body[kind].get("api_key"):
                a.api_key = body[kind]["api_key"].strip()
            if body[kind].get("clear"):
                a.url, a.api_key, a.path_map = "", "", []
    cfg.save()
    if "sonarr" in body or "radarr" in body:
        if svc.connect_arr():
            _spawn(request.app, asyncio.to_thread(svc.arr.refresh), "arr")
    return _ok()


async def _arr_test(request):
    svc: Service = request.app["svc"]
    kind = request.match_info["kind"]
    if kind not in ("sonarr", "radarr"):
        return _err("unknown")
    a = getattr(svc.cfg, kind)
    if not a.enabled:
        return _err("fill in the URL and API key first")
    c = ArrClient(kind.title(), a)
    try:
        st = await asyncio.to_thread(c.status)
        pairs = await asyncio.to_thread(auto_map, c, [r.path for r in svc.cfg.roots])
    except ArrError as e:
        return _err(str(e))
    if pairs:
        a.path_map = pairs
        svc.cfg.save()
    if svc.connect_arr():
        _spawn(request.app, asyncio.to_thread(svc.arr.refresh), "arr")
    return _ok(version=st.get("version", ""), path_map=pairs)


# ── history ──
def _history_row(svc: Service, h: dict) -> dict:
    """A replaced file and where its original is: in the trash until a date, kept as .orig, purged, deleted."""
    orig = h.get("orig", "")
    if h.get("restored"):
        where = "restored"
    elif h.get("purged"):
        where = "purged"
    elif not orig:
        where = "deleted"
    elif not os.path.exists(orig):
        where = "missing"
    else:
        where = "trash" if ".recast-trash" in orig else "kept"
    return {**{k: h.get(k) for k in ("src", "final", "preset", "src_size", "out_size", "when", "restored", "purged")},
            "original": where, "kept_until": svc.engine.kept_until(h) if where == "trash" else 0,
            "can_restore": where in ("trash", "kept")}


async def _history(request):
    svc: Service = request.app["svc"]
    rows = await asyncio.to_thread(lambda: [_history_row(svc, h) for h in reversed(svc.engine.history[-1000:])])
    cfg = svc.cfg
    return _ok(history=rows, originals=cfg.originals, trash_days=cfg.trash_days, trash_max_gb=cfg.trash_max_gb,
               trash_bytes=svc.engine.trash_bytes(), saved=svc.saved_total())


async def _restore(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    path = _path_arg(request, svc, body=body)
    try:
        msg = await asyncio.to_thread(svc.engine.restore, path)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    if svc.arr and svc.cfg.rescan_after_replace:
        _spawn(request.app, asyncio.to_thread(svc.arr.rescan, path), "rescan")
    return _ok(message=msg)


# ── first-run setup ──
async def _setup_get(request):
    svc: Service = request.app["svc"]
    st = request.app["setup"]
    return _ok({"needs_setup": svc.cfg.needs_setup, "lines": st["lines"], "running": st["running"],
                "detected": st["caps"] is not None or bool(svc.cfg.encoders),
                "scratch": svc.cfg.scratch or str(default_scratch()),
                "roots": [asdict(r) for r in svc.cfg.roots],
                "suggestions": await asyncio.to_thread(suggest_libraries)})


async def _setup_detect(request):
    if request.app["setup"]["running"]:
        return _err("already detecting")
    _detect(request.app)
    return _ok()


def _detect(app) -> None:
    """Find ffmpeg and test every encoder on this machine (in a thread; progress lines in app["setup"])."""
    st = app["setup"]
    st.update(lines=[], running=True, caps=None)

    def run():
        from .. import ffmpeg as ff
        svc: Service = app["svc"]
        try:
            mach = machine_summary()
            st["lines"].append(f"Machine: {mach['host']} · {mach['os']} · {mach['cpu']}" +
                               (f" · {mach['gpu']}" if mach.get("gpu") else ""))
            ffm, ffp = ff.find_binaries()
            if not ffm or not ffp:
                st["lines"].append("✗ ffmpeg not found — install it (brew install ffmpeg / winget install "
                                   "Gyan.FFmpeg / apt install ffmpeg) and detect again")
                return
            ver = ff.version(ffm)
            st["lines"].append(f"ffmpeg {ver} · {ffm}")
            caps = ff.detect_encoders(ffm, lambda n, c: st["lines"].append(
                f"{n}: " + (f"✓ {c.fps:.0f} fps" if c.status == "ok" else f"✗ {c.reason}")))
            svc.cfg.ffmpeg, svc.cfg.ffprobe, svc.cfg.ffmpeg_version = ffm, ffp, ver
            svc.cfg.machine, svc.cfg.encoders = mach, caps
            svc.cfg.detected_at = time.strftime("%Y-%m-%d %H:%M")
            st["caps"] = caps
            svc.probes.ffprobe = ffp
            if svc.cfg.roots and svc.cfg.scratch:  # re-detect from Settings: keep it
                svc.cfg.save()
            st["lines"].append("✓ done")
        finally:
            st["running"] = False
            for line in st["lines"]:
                log.info("detect    %s", line)
    _spawn(app, asyncio.to_thread(run), "detect")


async def _setup_save(request):
    svc: Service = request.app["svc"]
    body = await request.json()
    roots = []
    for p in body.get("roots", []):
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            folder = p.rstrip("/\\")
            name = (os.path.basename(os.path.dirname(folder)) + " " + os.path.basename(folder)).strip()
            roots.append(Root(name, p, is_network_path(p)))
    if not roots:
        return _err("pick at least one existing library folder")
    if not svc.cfg.encoders:
        return _err("run hardware detection first")
    scratch = os.path.abspath(os.path.expanduser(body.get("scratch") or str(default_scratch())))
    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as e:
        return _err(f"can't create scratch folder: {e}")
    svc.cfg.roots, svc.cfg.scratch = roots, scratch
    svc.cfg.save()
    svc.probes.ffprobe = svc.cfg.ffprobe
    return _ok()  # the service loop notices setup is done and lists the library


# ───────────────────────────── the service loop ─────────────────────────────

async def _check_update(app) -> None:
    from ..update import check
    rel = await asyncio.to_thread(check)
    app["rt"]["update"] = rel and {"version": rel["version"], "url": rel["url"]}
    if rel:
        log.info("recast %s is out (%s) — docker compose pull && docker compose up -d", rel["version"], rel["url"])


def _spawn(app, coro, name: str) -> None:
    t = asyncio.ensure_future(coro)
    app["rt"]["tasks"].add(t)
    t.add_done_callback(lambda _t: app["rt"]["tasks"].discard(_t))


async def _start(app):
    app["rt"]["loop"] = asyncio.get_running_loop()
    app["rt"]["runner"] = asyncio.ensure_future(_service_loop(app))
    if app["auto_detect"]:  # headless first run (Docker): libraries came from the environment
        _detect(app)


async def _stop(app):
    app["rt"]["runner"].cancel()
    svc: Service = app["svc"]
    await svc.engine.shutdown()
    svc.probes.save()
    svc.automation.save(force=True)


async def _service_start(app):
    """Once per run, as soon as setup is complete: cached listings, scores, Sonarr/Radarr, housekeeping."""
    svc: Service = app["svc"]
    for r in svc.cfg.roots:
        await asyncio.to_thread(svc.scanner.load_cached, r.path)
    if not svc.automation.last_sweep and svc.scanner.snap_when:  # a recent listing (e.g. from the terminal
        svc.automation.last_sweep = min(svc.scanner.snap_when.values())  # app) counts: no NAS re-list now
    await asyncio.to_thread(svc.automation.rebuild)
    if svc.connect_arr():
        _spawn(app, asyncio.to_thread(svc.arr.refresh), "arr")
    if svc.automation.sweep_due():
        _spawn(app, _run_sweep(app), "sweep")
    await asyncio.to_thread(svc.engine.clean_scratch)
    await asyncio.to_thread(svc.engine.purge_trash)
    log.info("ready · %d library folder(s) · automation %s", len(svc.cfg.roots), svc.cfg.auto_mode)


async def _service_loop(app):
    svc: Service = app["svc"]
    started = False
    n = 0
    while True:
        try:
            if not started and not svc.cfg.needs_setup:
                started = True
                await _service_start(app)
            if started:
                svc.engine.tick()
                if n % 8 == 0:
                    svc.automation.tick()
                if n % 40 == 0 and svc.automation.pending:
                    await asyncio.to_thread(svc.automation.process_pending)
                if n % 2400 == 0 and n and svc.automation.sweep_due() and not app["rt"]["sweeping"]:
                    _spawn(app, _run_sweep(app), "sweep")
                if n % 120 == 0:
                    await asyncio.to_thread(svc.probes.save)
                    svc.automation.save()
                if n % 14400 == 0 and n and svc.arr:
                    _spawn(app, asyncio.to_thread(svc.arr.refresh), "arr")
                if n % 14400 == 0 and n:  # hourly (also enforces the trash size cap soon after replaces)
                    _spawn(app, asyncio.to_thread(svc.engine.purge_trash), "trash")
            if n % 345600 == 0 and not os.environ.get("RECAST_NO_UPDATE_CHECK"):  # daily: newer release?
                _spawn(app, _check_update(app), "update")
            if n % 4 == 0 and app["clients"]:
                _broadcast(app, live_state(svc))
        except Exception:  # noqa: BLE001 — keep the daemon alive; the error is printed
            import traceback
            traceback.print_exc()
        n += 1
        await asyncio.sleep(0.25)
