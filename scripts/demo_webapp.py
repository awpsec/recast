"""Run the web app (`recast web`) against a throwaway demo library (dev only).

    python scripts/demo_webapp.py [--fresh]   # --fresh shows first-run setup
"""
import json
import os
import subprocess
import sys
from pathlib import Path

base = Path(os.environ.get("RECAST_DEMO") or (Path(__file__).resolve().parent.parent / ".demo"))
home, lib, scratch = base / "webhome", base / "lib", base / "scratch-web"
if not lib.exists():
    subprocess.run([sys.executable, str(Path(__file__).parent.parent / "tests" / "make_library.py"), str(lib),
                    "--secs", "120", "--size", "1920x1080", "--episodes", "4"], check=True,
                   stderr=subprocess.DEVNULL)
os.environ["RECAST_HOME"] = str(home)
cfg = home / "config.json"
if "--fresh" in sys.argv:
    cfg.unlink(missing_ok=True)
elif not cfg.exists() and (base / "home" / "config.json").exists():
    c = json.loads((base / "home" / "config.json").read_text())  # reuse the terminal demo's detection
    c.update(scratch=str(scratch), auto_mode="off", webhook_token="", web_password_hash="")
    for r in c.get("roots", []):
        r["remote"] = True  # pretend it's the NAS so the copy stage shows
    home.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(c, indent=2))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from recast.web.server import run  # noqa: E402
run("127.0.0.1", int(os.environ.get("PORT", 8768)))
