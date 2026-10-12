"""Things that must never go wrong in the library, however the timing falls: interrupted replaces, files
Sonarr/Radarr change mid-job, outputs that lost something, and automation redoing what you threw away."""
import json
import os
import shutil
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from recast import automation, engine
from recast.arr import ArrClient, ArrIndex
from recast.config import Arr
from recast.engine import ACTIVE, ASK_FIRST
from recast.probe import probe_now

from test_automation import FAST, drive, svc  # noqa: F401 — svc is a fixture


def episode(svc, n):  # noqa: F811
    return Path(svc.cfg.roots[0].path) / "TV" / "Fat Show" / "Season 1" / f"Fat Show - S01E0{n}.mkv"


async def encoded(svc, path, origin="manual", min_saving=-1.0, settings=FAST):  # noqa: F811
    svc.cfg.auto_mode = "off"  # only the job under test runs
    j = svc.engine.add_single(svc.probes.get(str(path)), svc.cfg.roots[0], settings, "Test fast", preview=True)
    j.origin, j.auto_min_saving = origin, min_saving
    await drive(svc, lambda: j.stage not in ACTIVE)
    return j


async def test_a_replace_cut_off_between_its_renames_is_finished(svc):  # noqa: F811
    src = episode(svc, 1)
    os.chmod(src, 0o640)
    j = await encoded(svc, src)
    assert j.stage == "awaiting", (j.error, j.flag)
    # the power went out mid-replace: new file next to it, original already in the trash, nothing recorded
    tmp = src.parent / f".recast-{j.id}.mkv.part"
    shutil.copy(j.out, tmp)
    os.replace(src, Path(engine._trash_dir(svc.cfg.roots[0].path, str(src.parent))) / src.name)
    svc.engine.approve(j)
    await drive(svc, lambda: j.stage != "to_replace" and j.id not in svc.engine._tasks)
    assert j.stage == "replaced" and "interrupted" in j.result, (j.stage, j.flag, j.result)
    assert probe_now("ffprobe", str(src)).codec == "HEVC" and not tmp.exists()
    assert svc.engine.history[-1]["orig"].endswith(src.name) and ".recast-trash" in svc.engine.history[-1]["orig"]


async def test_upgraded_while_writing_back(svc, monkeypatch):  # noqa: F811
    eng = svc.engine
    real_copy = eng._copy_file

    def copy_then_sonarr_upgrades(a, b, j, counter):
        real_copy(a, b, j, counter)
        if counter == "replace_done":
            with open(j.src, "ab") as fh:
                fh.write(b"\0" * 4096)
    monkeypatch.setattr(eng, "_copy_file", copy_then_sonarr_upgrades)

    mine = await encoded(svc, episode(svc, 1))  # yours: flagged for you, never overwritten
    eng.approve(mine)
    auto = await encoded(svc, episode(svc, 2), origin="auto", min_saving=0.0)  # automation's: dropped quietly
    await drive(svc, lambda: not eng._tasks and mine.stage != "to_replace" and auto.stage != "to_replace")
    assert mine.stage == "awaiting" and "changed since it was encoded" in mine.flag
    assert auto.stage == "discarded" and not eng.history
    assert any(e["action"] == "changed" for e in svc.automation.log)
    for n in (1, 2):
        assert probe_now("ffprobe", str(episode(svc, n))).codec == "H.264"  # both originals untouched
    assert not list(episode(svc, 1).parent.glob(".recast-*"))


async def test_replaced_file_keeps_the_original_permissions(svc):  # noqa: F811
    src = episode(svc, 3)
    os.chmod(src, 0o640)
    j = await encoded(svc, src)
    svc.engine.approve(j)
    await drive(svc, lambda: j.stage == "replaced")
    assert src.stat().st_mode & 0o777 == 0o640 and probe_now("ffprobe", str(src)).codec == "HEVC"


async def test_verify_flags_lost_resolution_or_hdr(svc, monkeypatch):  # noqa: F811
    real = probe_now
    monkeypatch.setattr(engine, "probe_now", lambda ff, p: replace(real(ff, p), height=360, hdr=""))
    m = svc.probes.get(str(episode(svc, 4)))
    svc.cfg.auto_mode = "off"
    j = svc.engine.add_single(replace(m, hdr="HDR10"), svc.cfg.roots[0], FAST, "Test fast", preview=True)
    await drive(svc, lambda: j.stage not in ACTIVE)
    assert "resolution 720p → 360p" in j.flag and "HDR10 lost" in j.flag, j.flag


async def test_discarded_and_failed_encodes_are_not_redone(svc):  # noqa: F811
    a = svc.automation
    j = await encoded(svc, episode(svc, 1), origin="auto", min_saving=ASK_FIRST)
    assert j.stage == "awaiting"
    svc.engine.deny(j)
    assert a.skipped[j.src]["by_you"] and "discarded" in a.skipped[j.src]["reason"]
    j2 = svc.engine.add_single(svc.probes.get(str(episode(svc, 2))), svc.cfg.roots[0], FAST, "Test fast")
    j2.origin = "auto"
    svc.engine._fail(j2, "Error while opening encoder")
    assert a.skipped[j2.src]["reason"].startswith("last try failed")
    a.rebuild()
    queued = {p["path"] for p in a.queue}
    assert j.src not in queued and j2.src not in queued
    a.reset_decisions()  # a threshold/preset change: a failure gets another go, your discard doesn't
    queued = {p["path"] for p in a.queue}
    assert j2.src in queued and j.src not in queued


def test_an_upgrade_drops_automations_work_but_not_yours(svc):  # noqa: F811
    lib = Path(svc.cfg.roots[0].path)
    svc.cfg.sonarr = Arr("http://sonarr.invalid", "k", [["/tv", str(lib / "TV")]])
    eng = svc.engine
    ja = eng.add_single(svc.probes.get(str(episode(svc, 1))), svc.cfg.roots[0], FAST, "Test fast")
    ja.origin = "auto"
    jm = eng.add_single(svc.probes.get(str(episode(svc, 2))), svc.cfg.roots[0], FAST, "Test fast")
    payload = {"eventType": "Download", "isUpgrade": True,
               "series": {"path": "/tv/Fat Show"}, "episodeFile": {"relativePath": "Season 1/Fat Show - S01E01.mkv"},
               "deletedFiles": [{"path": "/tv/Fat Show/Season 1/Fat Show - S01E01.mkv"},
                                {"path": "/tv/Fat Show/Season 1/Fat Show - S01E02.mkv"}]}
    assert svc.automation.webhook("sonarr", payload)
    assert ja.stage == "cancelled" and jm.stage == "queued"
    assert svc.automation.last_hook["sonarr"] > time.time() - 5


def test_files_still_arriving_wait_to_settle(svc, monkeypatch):  # noqa: F811
    monkeypatch.setattr(automation, "SETTLE", 600)
    a = svc.automation
    svc.cfg.auto_mode = "dry"
    a.rebuild()
    a.tick()
    assert a.status.startswith("idle") and not a.queue
    assert {p["source"] for p in a.pending} == {"sweep"} and len(a.pending) == 5
    assert all(p["due"] > time.time() + 500 for p in a.pending)
    # recast's own output coming back through a webhook is never looked at again
    p = str(episode(svc, 1))
    svc.engine.history.append({"src": p, "final": p, "out_size": os.path.getsize(p), "src_size": 1, "preset": "x"})
    a.pending = [{"path": p, "due": 0, "source": "sonarr"}]
    a.process_pending()
    assert not a.queue and p not in a.review and not any(e["action"] == "new" for e in a.log)


# ── Sonarr/Radarr: webhook set up from recast, and a rescan that only rescans one movie ──

class FakeArr(BaseHTTPRequestHandler):
    hooks: list = []
    commands: list = []
    reject = False
    api: dict = {}  # canned GETs: "series", "rootfolder", …

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")

    def do_GET(self):  # noqa: N802
        path = self.path.split("/api/v3/", 1)[1]
        if path == "notification":
            return self._send(200, FakeArr.hooks)
        if path == "notification/schema":
            return self._send(200, [{"id": 0, "implementation": "Email", "fields": []},
                                    {"id": 0, "implementation": "Webhook", "onDownload": False, "onUpgrade": False,
                                     "onGrab": False, "fields": [{"name": "url", "value": None},
                                                                 {"name": "method", "value": 1}]}])
        return self._send(200, FakeArr.api.get(path.split("?")[0], []))

    def do_POST(self):  # noqa: N802
        path, body = self.path.split("/api/v3/", 1)[1], self._body()
        if path == "command":
            FakeArr.commands.append(body)
            return self._send(201, {"id": 1})
        if FakeArr.reject:  # Sonarr tests the webhook while saving; it couldn't reach recast
            return self._send(400, [{"propertyName": "Url", "errorMessage": "Unable to send test message"}])
        FakeArr.hooks.append({**body, "id": len(FakeArr.hooks) + 1})
        return self._send(201, FakeArr.hooks[-1])

    def do_PUT(self):  # noqa: N802
        body = self._body()
        FakeArr.hooks = [body if h["id"] == body["id"] else h for h in FakeArr.hooks]
        return self._send(202, body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_arr():
    FakeArr.hooks, FakeArr.commands, FakeArr.reject, FakeArr.api = [], [], False, {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeArr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_webhook_is_added_once_and_fixed_when_the_address_changes(fake_arr):
    c = ArrClient("Sonarr", Arr(fake_arr, "k"))
    url = "http://zeddserver:8484/api/hook/sonarr?token=abc"
    assert c.install_webhook(url) == "added"
    h = FakeArr.hooks[0]
    assert h["name"] == "recast" and h["onDownload"] and h["onUpgrade"] and not h["onGrab"]
    assert {f["name"]: f["value"] for f in h["fields"]} == {"url": url, "method": 1}
    assert c.install_webhook(url) == "already set" and len(FakeArr.hooks) == 1
    assert c.install_webhook(url.replace("abc", "new")) == "updated" and len(FakeArr.hooks) == 1
    assert c.find_webhook()["fields"][0]["value"].endswith("token=new")
    FakeArr.hooks, FakeArr.reject = [], True
    with pytest.raises(Exception, match="Unable to send test message"):  # Sonarr's own reason comes through
        c.install_webhook(url)


def test_radarr_rescans_only_that_movie(fake_arr):
    idx = ArrIndex(Arr(), Arr(fake_arr, "k", [["/movies", "/m"]]))
    idx.items = {"/m/Film (2020)": {"source": "Radarr", "id": 42, "path": "/m/Film (2020)"}}
    assert idx.rescan("/m/Film (2020)/Film.mkv") == "Radarr rescan queued"
    assert FakeArr.commands == [{"name": "RescanMovie", "movieId": 42, "movieIds": [42]}]


def test_sonarr_from_the_environment_maps_its_own_paths(svc, fake_arr):  # noqa: F811
    """Docker: SONARR_URL/KEY in .env and nothing else. Sonarr's "/tv/Fat Show" is found under the library."""
    FakeArr.api = {"rootfolder": [{"path": "/tv"}],
                   "series": [{"id": 7, "title": "Fat Show", "path": "/tv/Fat Show", "seriesType": "standard",
                               "genres": [], "tags": [], "statistics": {}}]}
    svc.cfg.sonarr = Arr(fake_arr, "k")
    svc.connect_arr()
    svc.refresh_arr()
    tv = str(Path(svc.cfg.roots[0].path) / "TV")
    assert svc.cfg.sonarr.path_map == [["/tv", tv]]
    assert svc.scanner.arr_facts(str(episode(svc, 1)))["source"] == "Sonarr"


def test_saves_from_many_threads_at_once(svc):  # noqa: F811
    """The service loop, a webhook and a settings change can all save at the same moment."""
    errors = []

    def hammer():
        try:
            for _ in range(40):
                svc.automation.note("x")
                svc.automation.save(force=True)
                svc.cfg.save()
                svc.engine.save(force=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[0]
    assert len(json.loads(svc.automation.file.read_text())["log"]) == len(svc.automation.log[-500:])
    assert not list(svc.automation.file.parent.glob(".*.tmp"))
