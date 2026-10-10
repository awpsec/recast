"""recast — re-encode your media library.

    recast                 the terminal app (first launch walks through setup)
    recast --setup         re-run hardware detection / pick library + scratch
    recast update          install the newest release from GitHub (uv, pipx or pip — whatever installed it)

The always-on server with the web app and Sonarr/Radarr automation is `recast-server`
(or the Docker image) — a separate install with its own settings, queue and history.
"""
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(prog="recast", description="Re-encode your media library (terminal app).",
                                 epilog="For the always-on web app + Sonarr/Radarr automation, run `recast-server`.")
    ap.add_argument("command", nargs="?", choices=["update", "web"], help="update: install the newest release")
    ap.add_argument("-y", "--yes", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--setup", action="store_true", help="re-run hardware detection and library setup")
    ap.add_argument("--version", action="store_true")
    a, _ = ap.parse_known_args()
    if a.version:
        from . import __version__
        print(__version__)
        return
    if a.command == "update":
        from .update import self_update
        raise SystemExit(self_update(a.yes))
    if a.command == "web":
        raise SystemExit("The web app is its own program now: run `recast-server` (or the Docker image) on the "
                         "machine that should do the long-running work. It keeps separate settings, so the "
                         "terminal app stays free for quick jobs.")
    from .config import Config
    if a.setup:
        c = Config.load()
        c.encoders = {}
        c.save()
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
