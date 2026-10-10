"""Updates come from GitHub releases.

The terminal app updates itself (`recast update`): it installs the release's wheel with whichever tool
installed recast (uv, pipx or pip). The server is a Docker image; it only says when a newer release
exists — `docker compose pull && docker compose up -d` does the rest.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from . import __version__
from .config import config_dir

REPO = "awpsec/recast"
API = f"https://api.github.com/repos/{REPO}/releases/latest"


def parse_version(v: str) -> tuple[int, ...]:
    out = []
    for part in v.lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def latest_release(timeout: float = 6) -> dict:
    """{"version", "tag", "url", "wheel", "notes"} for the newest release. Raises on network trouble."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"recast/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    wheel = next((a["browser_download_url"] for a in d.get("assets", []) if a.get("name", "").endswith(".whl")), "")
    tag = d.get("tag_name", "")
    return {"version": tag.lstrip("vV"), "tag": tag, "url": d.get("html_url", ""), "wheel": wheel,
            "notes": (d.get("body") or "")[:4000]}


def check(max_age: float = 86400, force: bool = False) -> dict | None:
    """The latest release if it's newer than this one, else None. Asks GitHub at most once a day."""
    f = config_dir() / "update-check.json"
    rel = None
    try:
        cached = json.loads(f.read_text())
        if not force and time.time() - cached.get("checked", 0) < max_age:
            rel = cached.get("release")
    except (OSError, ValueError):
        pass
    if rel is None:
        try:
            rel = latest_release()
        except Exception:  # noqa: BLE001 — offline, rate-limited, no releases yet: just don't nag
            return None
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"checked": time.time(), "release": rel}))
        except OSError:
            pass
    if rel and rel.get("version") and parse_version(rel["version"]) > parse_version(__version__):
        return rel
    return None


def install_command(rel: dict) -> list[str]:
    """How to install `rel` with the tool that installed this copy of recast."""
    git = f"git+https://github.com/{REPO}@{rel['tag']}"
    spec = rel.get("wheel") or f"recast @ {git}"
    prefix = sys.prefix.replace("\\", "/")
    uv = os.path.exists(os.path.join(sys.prefix, "uv-receipt.toml")) or "/uv/tools/" in prefix
    pipx = os.path.exists(os.path.join(sys.prefix, "pipx_metadata.json")) or "/pipx/venvs/" in prefix
    if uv and shutil.which("uv"):
        return ["uv", "tool", "install", "--force", spec]
    if pipx and shutil.which("pipx"):
        return ["pipx", "install", "--force", rel.get("wheel") or git]
    return [sys.executable, "-m", "pip", "install", "--upgrade", spec]


def self_update(yes: bool = False) -> int:
    """`recast update`: install the newest release. Returns a process exit code."""
    print(f"recast {__version__} · checking {REPO} for releases…")
    try:
        rel = latest_release()
    except Exception as e:  # noqa: BLE001
        print(f"couldn't reach GitHub: {e}")
        return 1
    if not rel["version"] or parse_version(rel["version"]) <= parse_version(__version__):
        print("You're on the latest release.")
        return 0
    print(f"recast {rel['version']} is out: {rel['url']}")
    cmd = install_command(rel)
    print("→ " + " ".join(cmd))
    if not yes and sys.stdin.isatty():
        if input("Install it now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 0
    if os.name != "nt":
        os.execvp(cmd[0], cmd)  # replace this process: nothing of the old install stays loaded
    return subprocess.call(cmd)
