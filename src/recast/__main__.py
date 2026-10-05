"""recast — re-encode your media library from a TUI.

    recast            run the app (first launch walks through setup)
    recast --setup    re-run hardware detection / pick library + scratch
    recast --web      serve the TUI in a browser at http://localhost:8765
"""
from __future__ import annotations

import argparse
import shlex
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="recast", description="Re-encode your media library from a TUI.")
    ap.add_argument("--setup", action="store_true", help="re-run hardware detection and library setup")
    ap.add_argument("--web", action="store_true", help="serve in a browser (needs textual-serve)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args()
    if a.version:
        from . import __version__
        print(__version__)
        return
    if a.web:
        from textual_serve.server import Server
        Server(f"{shlex.quote(sys.executable)} -m recast" + (" --setup" if a.setup else ""), port=a.port).serve()
        return
    from .config import Config
    from .ui.app import RecastApp
    if a.setup:
        c = Config.load()
        c.encoders = {}
        c.save()
    RecastApp().run()


if __name__ == "__main__":
    main()
