"""Sonarr / Radarr: metadata for the details pane, rescans after a replace.

Paths: Sonarr might call a folder "/tv/Black Clover (2017)" while this machine
sees it as "/Volumes/media/TV/Black Clover (2017)" or "M:\\TV\\...". Each Arr
config carries a path_map of [arr_prefix, local_prefix] pairs; auto_map() can
guess them by finding a series folder name under the library roots.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from .config import Arr

WIN = sys.platform == "win32"


def _norm(p: str) -> str:
    p = p.replace("\\", "/").rstrip("/")
    return p.lower() if WIN else p


class ArrError(Exception):
    pass


class ArrClient:
    def __init__(self, kind: str, cfg: Arr):
        self.kind, self.cfg = kind, cfg

    def req(self, method: str, path: str, body: dict | None = None, timeout: float = 15):
        url = self.cfg.url.rstrip("/") + "/api/v3/" + path.lstrip("/")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"X-Api-Key": self.cfg.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raise ArrError(f"{self.kind} HTTP {e.code}" + (" (bad API key?)" if e.code == 401 else "")) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ArrError(f"can't reach {self.kind} at {self.cfg.url}: {getattr(e, 'reason', e)}") from e

    def status(self) -> dict:
        return self.req("GET", "system/status")

    def root_folders(self) -> list[str]:
        return [r["path"] for r in self.req("GET", "rootfolder")]

    def to_local(self, arr_path: str) -> str:
        n = _norm(arr_path)
        for a, local in sorted(self.cfg.path_map, key=lambda x: -len(x[0])):
            na = _norm(a)
            if n == na or n.startswith(na + "/"):
                rest = arr_path.replace("\\", "/").rstrip("/")[len(a.rstrip("/\\")):]
                return local.rstrip("/\\") + (rest.replace("/", "\\") if WIN else rest)
        return arr_path


class ArrIndex:
    """Folder → series/movie lookup for everything Sonarr/Radarr know about."""

    def __init__(self, sonarr: Arr, radarr: Arr):
        self.clients = {k: ArrClient(k, c) for k, c in (("Sonarr", sonarr), ("Radarr", radarr)) if c.enabled}
        self.items: dict[str, dict] = {}  # normalized local folder -> record
        self.episodes: dict[int, dict[str, dict]] = {}  # series id -> filename -> episode
        self.profiles: dict[str, dict[int, str]] = {}
        self.errors: dict[str, str] = {}
        self.ready = False
        self.refreshed = 0.0  # when the last refresh started
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """Blocking; run in a thread."""
        self.refreshed = time.time()
        items: dict[str, dict] = {}
        for kind, c in self.clients.items():
            try:
                self.profiles[kind] = {p["id"]: p["name"] for p in c.req("GET", "qualityprofile")}
                tags = {t["id"]: t["label"] for t in c.req("GET", "tag")}
                records = c.req("GET", "series" if kind == "Sonarr" else "movie", timeout=60)
                for r in records:
                    local = c.to_local(r["path"])
                    stats = r.get("statistics") or {}
                    rec = {"source": kind, "id": r["id"], "title": r.get("title", ""), "year": r.get("year"),
                           "status": r.get("status", ""), "monitored": r.get("monitored", False),
                           "profile": self.profiles[kind].get(r.get("qualityProfileId"), ""),
                           "path": local, "genres": list(r.get("genres") or []),
                           "tags": [tags[t] for t in r.get("tags") or [] if t in tags]}
                    if kind == "Sonarr":
                        rec["episodes"] = f"{stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)} files"
                        rec["series_type"] = r.get("seriesType", "")  # "anime" / "standard" / "daily"
                        rec["network"] = r.get("network", "")
                    else:
                        mf = r.get("movieFile") or {}
                        rec["quality"] = ((mf.get("quality") or {}).get("quality") or {}).get("name", "")
                    items[_norm(local)] = rec
                self.errors.pop(kind, None)
            except (ArrError, KeyError, TypeError) as e:
                self.errors[kind] = str(e)
        with self._lock:
            self.items = items
            self.ready = True

    def facets(self) -> dict[str, list[dict]]:
        """What preset rules can match on, with how many shows/movies have each value:
        {"genre": [{"value": "Animation", "shows": 34, "movies": 2}, …], "tag": […], "type": […], "source": […]}"""
        counts: dict[str, dict[str, list[int]]] = {"type": {}, "genre": {}, "tag": {}, "source": {}}
        with self._lock:
            records = list(self.items.values())
        for r in records:
            i = 0 if r["source"] == "Sonarr" else 1
            vals = {"source": [r["source"]], "type": [r["series_type"]] if r.get("series_type") else [],
                    "genre": r.get("genres") or [], "tag": r.get("tags") or []}
            for by, vs in vals.items():
                for v in vs:
                    counts[by].setdefault(v, [0, 0])[i] += 1
        return {by: [{"value": v, "shows": n[0], "movies": n[1]}
                     for v, n in sorted(c.items(), key=lambda x: (-sum(x[1]), x[0].lower()))]
                for by, c in counts.items()}

    def lookup(self, local_path: str) -> dict | None:
        """The series/movie whose folder contains local_path."""
        n = _norm(local_path)
        with self._lock:
            while n and "/" in n:
                if n in self.items:
                    return self.items[n]
                n = n.rsplit("/", 1)[0]
        return None

    def episode(self, rec: dict, local_file: str) -> dict | None:
        """Episode metadata for a file (fetches the series' episodes once; blocking)."""
        if rec["source"] != "Sonarr":
            return None
        sid = rec["id"]
        if sid not in self.episodes:
            try:
                eps = self.clients["Sonarr"].req("GET", f"episode?seriesId={sid}&includeEpisodeFile=true",
                                                 timeout=30)
            except ArrError:
                return None
            by_file = {}
            for e in eps:
                ef = e.get("episodeFile") or {}
                name = os.path.basename((ef.get("path") or ef.get("relativePath") or "").replace("\\", "/"))
                if name:
                    q = ((ef.get("quality") or {}).get("quality") or {}).get("name", "")
                    by_file[name.lower() if WIN else name] = {
                        "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
                        "title": e.get("title", ""), "aired": e.get("airDate", ""), "quality": q}
            self.episodes[sid] = by_file
        name = os.path.basename(local_file)
        return self.episodes[sid].get(name.lower() if WIN else name)

    def rescan(self, local_path: str) -> str:
        rec = self.lookup(local_path)
        if not rec:
            return "not managed by Sonarr/Radarr"
        c = self.clients[rec["source"]]
        body = ({"name": "RescanSeries", "seriesId": rec["id"]} if rec["source"] == "Sonarr"
                else {"name": "RescanMovie", "movieIds": [rec["id"]]})
        c.req("POST", "command", body)
        self.episodes.pop(rec["id"], None)
        return f"{rec['source']} rescan queued"


def auto_map(client: ArrClient, roots: list[str]) -> list[list[str]]:
    """Guess [arr_root, local_dir] pairs: find a known series/movie folder name under the library roots."""
    kind_path = "series" if client.kind == "Sonarr" else "movie"
    records = client.req("GET", kind_path, timeout=60)
    pairs = []
    for arr_root in client.root_folders():
        na = _norm(arr_root)
        names = [os.path.basename(r["path"].replace("\\", "/").rstrip("/")) for r in records
                 if _norm(r["path"]).startswith(na + "/")][:25]
        found = None
        for root in roots:
            cands = [root] + [e.path for e in _subdirs(root)]
            for cand in cands:
                if any(os.path.isdir(os.path.join(cand, n)) for n in names):
                    found = cand
                    break
            if found:
                break
        if found:
            pairs.append([arr_root, found])
    return pairs


def _subdirs(path: str):
    try:
        return [e for e in os.scandir(path) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return []
