"""Automation: scoring, pacing (never floods), safety valve, webhooks, your decisions."""
import asyncio
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from recast.config import Arr, Config, EncoderCap, Root
from recast.encode import EncodeSettings, save_preset
from recast.engine import ACTIVE
from recast.service import Service

CAPS = {"libx265": EncoderCap("ok", 60), "libx264": EncoderCap("ok", 150)}
FAST = EncodeSettings(codec="hevc", encoder="libx265", speed="ultrafast", rate_mode="crf", crf=32)


def clip(path: Path, crf: int, secs: int = 4):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=24:duration={secs}",
                    "-f", "lavfi", "-i", f"sine=frequency=300:duration={secs}", "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf), "-c:a", "aac", str(path)], check=True)


@pytest.fixture
def svc(home, tmp_path):
    lib = tmp_path / "lib"
    for e in range(1, 6):  # five fat episodes: easy, clear wins
        clip(lib / "TV" / "Fat Show" / "Season 1" / f"Fat Show - S01E0{e}.mkv", crf=8)
    save_preset("Test fast", "", FAST)
    cfg = Config(roots=[Root("Lib", str(lib), remote=True)], scratch=str(tmp_path / "scratch"), ffmpeg="ffmpeg",
                 ffprobe="ffprobe", encoders=CAPS, auto_preset="Test fast", auto_mode="on",
                 auto_threshold=0.30, review_threshold=0.10, desktop_notify=False)
    s = Service(cfg)
    s.scanner.scan(str(lib))
    return s


async def drive(svc, until, timeout=180, watch=None):
    for _ in range(int(timeout / 0.1)):
        svc.engine.tick()
        svc.automation.tick()
        if watch:
            watch()
        if until():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out: {[(j.id, j.stage, j.error) for j in svc.engine.jobs.values()]}")


def test_scoring_and_rebuild(svc):
    counts = svc.automation.rebuild()
    assert counts["auto"] == 5 and counts["review"] == 0
    q = svc.automation.queue
    assert all(p["pct"] >= 0.30 for p in q)
    assert q == sorted(q, key=lambda p: -(p["size"] - p["est_out"]))   # biggest wins first
    p = svc.automation.evaluate(svc.probes.get(q[0]["path"]), auto_t=0.99, review_t=0.10)
    assert p["verdict"] == "review"
    p = svc.automation.evaluate(svc.probes.get(q[0]["path"]), auto_t=0.999, review_t=0.995)
    assert p["verdict"] == "skip" and "would save only" in p["reason"]


async def test_paces_one_at_a_time_and_replaces(svc):
    svc.automation.rebuild()
    peak = {"active": 0, "waiting": 0}

    def watch():
        auto = [j for j in svc.engine.jobs.values() if j.origin == "auto" and j.stage in ACTIVE]
        peak["active"] = max(peak["active"], len(auto))
        peak["waiting"] = max(peak["waiting"], sum(1 for j in auto if j.stage in ("queued", "copying", "ready")))
    await drive(svc, lambda: len(svc.engine.history) == 5, watch=watch)
    assert peak["active"] <= 2 and peak["waiting"] <= 1, peak       # never floods
    assert len([j for j in svc.engine.jobs.values() if j.origin == "auto"]) == 5
    assert all(Path(h["final"]).exists() for h in svc.engine.history)
    actions = [e["action"] for e in svc.automation.log]
    assert actions.count("encode") == 5 and actions.count("replaced") == 5
    svc.automation.tick()
    assert svc.automation.status.startswith("idle")


async def test_real_saving_below_threshold_goes_to_you(svc):
    svc.cfg.auto_threshold = 0.30
    svc.automation.rebuild()
    svc.automation.queue = svc.automation.queue[:1]
    svc.cfg.prefetch = False
    svc.automation.tick()
    j = next(j for j in svc.engine.jobs.values() if j.origin == "auto")
    j.auto_min_saving = 0.999                                     # the real result can't clear this
    await drive(svc, lambda: j.stage == "awaiting")
    assert "below the" in j.note and j in svc.engine.inbox()
    assert Path(j.src).exists()                                    # library untouched


def test_dry_run_creates_nothing(svc):
    svc.cfg.auto_mode = "dry"
    svc.automation.rebuild()
    for _ in range(5):
        svc.engine.tick()
        svc.automation.tick()
    assert not svc.engine.jobs and svc.automation.status.startswith("dry run — next would be")


def test_review_decisions_and_your_skips_stick(svc):
    svc.cfg.auto_threshold, svc.cfg.review_threshold = 0.999, 0.10   # everything is "borderline"
    svc.automation.reset_decisions()
    assert len(svc.automation.review) == 5 and not svc.automation.queue
    paths = sorted(svc.automation.review)
    svc.automation.skip_review(paths[0])
    jid = svc.automation.encode_review(paths[1])
    j = svc.engine.jobs[jid]
    assert j.origin == "review" and j.auto_min_saving == 0.10
    svc.automation.reset_decisions()
    assert svc.automation.skipped[paths[0]]["by_you"] and paths[0] not in svc.automation.review
    assert paths[1] not in svc.automation.review                     # it has a job now


def test_webhook_maps_path_and_jumps_the_queue(svc, monkeypatch):
    lib = Path(svc.cfg.roots[0].path)
    new = lib / "TV" / "New Show" / "Season 1" / "New Show - S01E01.mkv"
    clip(new, crf=8)
    svc.cfg.sonarr = Arr("http://sonarr", "k", [["/tv", str(lib / "TV")]])
    svc.automation.rebuild()
    payload = {"eventType": "Download", "series": {"path": "/tv/New Show", "type": "anime"},
               "episodeFile": {"relativePath": "Season 1/New Show - S01E01.mkv"}}
    assert svc.automation.webhook("sonarr", payload) == str(new)
    assert svc.scanner.series_type(str(new)) == "anime"
    svc.automation.process_pending()                                  # not due yet
    assert svc.automation.queue[0]["path"] != str(new)
    for p in svc.automation.pending:
        p["due"] = 0
    svc.automation.process_pending()
    assert svc.automation.queue[0]["path"] == str(new)                # new arrivals go first
    assert svc.automation.webhook("sonarr", {"eventType": "Test"}) is None
    outside = {"eventType": "Download", "series": {"path": "/elsewhere/X"},
               "episodeFile": {"relativePath": "a.mkv"}}
    assert svc.automation.webhook("sonarr", outside) is None


def test_quiet_hours(svc):
    a = svc.automation
    svc.cfg.auto_hours = "01:00-08:00"
    assert a.in_hours(datetime(2026, 1, 1, 3, 0)) and not a.in_hours(datetime(2026, 1, 1, 12, 0))
    svc.cfg.auto_hours = "22:00-06:00"                                # wraps past midnight
    assert a.in_hours(datetime(2026, 1, 1, 23, 30)) and a.in_hours(datetime(2026, 1, 1, 5, 0))
    assert not a.in_hours(datetime(2026, 1, 1, 12, 0))
    svc.cfg.auto_hours = ""
    assert a.in_hours(datetime(2026, 1, 1, 12, 0))


def test_preview_counts_follow_thresholds(svc):
    loose = svc.automation.preview(0.10, 0.05)
    strict = svc.automation.preview(0.999, 0.10)
    assert loose["auto"] == 5 and strict["auto"] == 0 and strict["review"] == 5
    assert loose["auto_bytes"] > 0


async def test_replaced_files_drop_out_and_upgrades_come_back(svc):
    svc.automation.rebuild()
    svc.automation.queue = svc.automation.queue[:1]
    first = svc.automation.queue[0]["path"]
    await drive(svc, lambda: len(svc.engine.history) == 1)
    final = svc.engine.history[0]["final"]
    assert svc.automation.rebuild()["done"] == 1                     # the scanner knows the new file
    assert all(p["path"] not in (first, final) for p in svc.automation.queue)
    clip(Path(final), crf=6)                                          # Sonarr upgrade: a different file, same name
    svc.scanner.scan(svc.cfg.roots[0].path, relist=True)
    svc.automation.rebuild()
    assert final in [p["path"] for p in svc.automation.queue]         # fair game again
