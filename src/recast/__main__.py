"""recast — re-encode your media library.

    recast                 the terminal app (first launch walks through setup)
    recast --setup         re-run hardware detection / pick library + scratch
    recast web             the web app + automation daemon at http://localhost:8484
    recast web --host 0.0.0.0 --port 8484    reachable from other computers (set a password!)
"""
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(prog="recast", description="Re-encode your media library.",
                                 epilog="Run `recast web` for the browser app with Sonarr/Radarr automation.")
    ap.add_argument("command", nargs="?", choices=["web"], help="web: run the web app + automation daemon")
    ap.add_argument("--setup", action="store_true", help="re-run hardware detection and library setup")
    ap.add_argument("--host", default="127.0.0.1", help="web: address to listen on (default: this computer only)")
    ap.add_argument("--port", type=int, default=8484, help="web: port (default 8484)")
    ap.add_argument("--web", action="store_true", help=argparse.SUPPRESS)  # old spelling of `recast web`
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        from . import __version__
        print(__version__)
        return
    from .config import Config
    if a.setup:
        c = Config.load()
        c.encoders = {}
        c.save()
    if a.command == "web" or a.web:
        from .web.server import run
        run(a.host, a.port)
        return
    from .service import instance_lock, release_lock
    msg = instance_lock("terminal app")
    if msg:
        raise SystemExit(msg)
    try:
        from .ui.app import RecastApp
        RecastApp().run()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
