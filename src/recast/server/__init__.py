"""recast-server — the always-on half of recast, meant to live next to Sonarr/Radarr.

    recast-server                          http://localhost:8484, settings in .../recast-server
    recast-server --host 0.0.0.0           reachable from other computers (set a password in Settings)

It keeps its own config, queue and history, separate from the terminal app (`recast`), even on the same
machine. In Docker, everything lives in /config and first-run setup can come from the environment:

    RECAST_LIBRARIES=/tv,/movies           library folders (or "TV=/tv,Movies=/movies")
    RECAST_SCRATCH=/scratch                where copies and encodes are made
    RECAST_SONARR_URL / RECAST_SONARR_API_KEY, RECAST_RADARR_URL / RECAST_RADARR_API_KEY
    RECAST_HOST / RECAST_PORT              listen address (the image uses 0.0.0.0:8484)

Environment values only fill in what isn't set yet: anything you change in the web app sticks.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys

from ..config import APP, Arr, Config, Root, config_dir, default_scratch, is_network_path, use_server_profile

log = logging.getLogger("recast")


def main() -> None:
    ap = argparse.ArgumentParser(prog="recast-server",
                                 description="recast's server: web app + paced Sonarr/Radarr automation.")
    ap.add_argument("--host", default=os.environ.get("RECAST_HOST", "127.0.0.1"),
                    help="address to listen on (default: this computer only; 0.0.0.0 = every interface)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RECAST_PORT", "8484")), help="default 8484")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        from .. import __version__
        print(__version__)
        return
    use_server_profile()
    logging.basicConfig(level=os.environ.get("RECAST_LOG", "INFO").upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    run(a.host, a.port)


def run(host: str, port: int) -> None:
    from aiohttp import web

    from ..service import Service, instance_lock, release_lock
    from .app import create_app
    msg = instance_lock(f"server on port {port}")
    if msg:
        raise SystemExit(msg)
    try:
        import_terminal_settings()
        cfg = Config.load()
        seed_from_env(cfg)
        svc = Service(cfg)
        log.info("recast-server · config in %s", config_dir())
        if host not in ("127.0.0.1", "localhost", "::1") and not cfg.web_password_hash:
            log.warning("reachable from your network without a password — set one in Settings → Security")
        shown = "localhost" if host in ("0.0.0.0", "::") else host
        log.info("listening on http://%s:%d", shown, port)
        web.run_app(create_app(svc, host, port, auto_detect=not cfg.encoders and bool(cfg.roots)),
                    host=host, port=port, print=None, access_log=None)
    finally:
        release_lock()


def import_terminal_settings() -> bool:
    """First start on a machine that already runs the terminal app: bring over its library folders, presets,
    encoder detection and header cache (so the NAS isn't read again). Queue and history stay with the app
    that made them. Automation starts off, whatever it was."""
    if os.environ.get("RECAST_HOME"):
        return False
    mine, theirs = config_dir(), config_dir(APP)
    if (mine / "config.json").exists() or not (theirs / "config.json").exists():
        return False
    mine.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "probe-cache.json", "overview.json"):
        if (theirs / name).exists():
            shutil.copy2(theirs / name, mine / name)
    if (theirs / "presets").is_dir():
        shutil.copytree(theirs / "presets", mine / "presets", dirs_exist_ok=True)
    cfg = Config.load()
    cfg.auto_mode = "off"
    cfg.scratch = str(default_scratch())  # the terminal app's scratch is its own
    cfg.save()
    log.info("imported library folders, presets and header cache from the terminal app (%s)", theirs)
    return True


def seed_from_env(cfg: Config) -> None:
    """Headless first run (Docker): fill in what isn't configured yet from RECAST_* variables."""
    changed = False
    libs = os.environ.get("RECAST_LIBRARIES", "")
    if libs and not cfg.roots:
        for item in (x.strip() for x in libs.replace(";", ",").split(",")):
            if not item:
                continue
            name, _, path = item.rpartition("=")
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isdir(path):
                log.warning("RECAST_LIBRARIES: %s isn't a folder in here (is the volume mounted?)", path)
                continue
            cfg.roots.append(Root(name or os.path.basename(path.rstrip("/")).title() or path, path,
                                  is_network_path(path)))
            changed = True
    scratch = os.environ.get("RECAST_SCRATCH", "")
    if scratch and (not cfg.scratch or not (config_dir() / "config.json").exists()):
        os.makedirs(scratch, exist_ok=True)
        cfg.scratch, changed = scratch, True
    for kind in ("sonarr", "radarr"):
        url = os.environ.get(f"RECAST_{kind.upper()}_URL", "").strip()
        key = os.environ.get(f"RECAST_{kind.upper()}_API_KEY", "").strip()
        a: Arr = getattr(cfg, kind)
        if url and key and not a.enabled:
            a.url, a.api_key, changed = url, key, True
    if changed:
        if not cfg.scratch:
            cfg.scratch = str(default_scratch())
        cfg.save()
        log.info("configured from the environment: %s", ", ".join(r.path for r in cfg.roots) or "(no libraries)")
