"""Serve the recast TUI in a browser at http://localhost:8765."""
import shlex, sys
from pathlib import Path
from textual_serve.server import Server

here = Path(__file__).resolve().parent
Server(f"{shlex.quote(sys.executable)} {shlex.quote(str(here / 'prototype.py'))}", port=8765).serve()
