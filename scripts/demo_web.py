"""Serve recast in a browser against a throwaway demo library (dev only).

    python scripts/demo_web.py [--fresh]   # --fresh shows first-run setup
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

base = Path(os.environ.get("RECAST_DEMO") or (Path(__file__).resolve().parent.parent / ".demo"))
home, lib, scratch = base / "home", base / "lib", base / "scratch"
if not lib.exists():
    subprocess.run([sys.executable, str(Path(__file__).parent.parent / "tests" / "make_library.py"), str(lib),
                    "--secs", "120", "--size", "1920x1080", "--episodes", "4"], check=True,
                   stderr=subprocess.DEVNULL)
os.environ["RECAST_HOME"] = str(home)
cfg = home / "config.json"
if "--fresh" in sys.argv:
    cfg.unlink(missing_ok=True)
elif cfg.exists():
    c = json.loads(cfg.read_text())
    c["scratch"] = str(scratch)
    for r in c.get("roots", []):
        r["remote"] = True  # pretend it's the NAS so the copy stage shows
    cfg.write_text(json.dumps(c, indent=2))
from textual_serve.server import Server  # noqa: E402
Server(f"{shlex.quote(sys.executable)} -m recast", port=int(os.environ.get("PORT", 8766))).serve()
