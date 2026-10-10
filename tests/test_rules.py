"""Preset rules (Sonarr/Radarr series type, genre, tag → preset) and the clock working hours run on."""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from recast import automation
from recast.automation import payload_facts, rule_matches, server_clock
from recast.config import Arr, Config
from recast.encode import save_preset

from test_automation import FAST, clip, svc  # noqa: F401 — svc is a fixture

API = {
    "qualityprofile": [{"id": 1, "name": "HD-1080p"}],
    "tag": [{"id": 1, "label": "kids"}, {"id": 2, "label": "4k"}],
    "series": [{"id": 7, "title": "Fat Show", "path": "/tv/Fat Show", "seriesType": "anime", "qualityProfileId": 1,
                "genres": ["Animation", "Action"], "tags": [1], "statistics": {}}],
}


class FakeSonarr(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps(API[self.path.split("/api/v3/", 1)[1].split("?")[0]]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def sonarr():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSonarr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def anime_preset(svc):  # noqa: F811
    save_preset("Test anime", "", FAST)
    svc.reload_presets()


def test_rules_pick_presets_from_what_sonarr_knows(svc, sonarr, anime_preset):  # noqa: F811
    lib = Path(svc.cfg.roots[0].path)
    ep = str(lib / "TV" / "Fat Show" / "Season 1" / "Fat Show - S01E01.mkv")
    svc.cfg.sonarr = Arr(sonarr, "k", [["/tv", str(lib / "TV")]])
    svc.connect_arr().refresh()
    assert svc.scanner.arr_facts(ep) == {"source": "Sonarr", "type": "anime", "genres": ["Animation", "Action"],
                                         "tags": ["kids"]}                     # tag ids come back as labels
    f = svc.arr.facets()
    assert f["type"] == [{"value": "anime", "shows": 1, "movies": 0}]
    assert {"value": "kids", "shows": 1, "movies": 0} in f["tag"] and len(f["genre"]) == 2

    a = svc.automation
    assert a.preset_choice(ep) == ("Test fast", None)                           # no rules: the preferred preset
    svc.cfg.auto_rules = [{"by": "tag", "value": "4k", "preset": "Test anime"},          # doesn't match
                          {"by": "genre", "value": "animation", "preset": "Test anime"},  # case doesn't matter
                          {"by": "source", "value": "Sonarr", "preset": "Test fast"}]
    assert a.preset_choice(ep) == ("Test anime", svc.cfg.auto_rules[1])         # first match wins
    svc.cfg.auto_rules.insert(0, {"by": "type", "value": "anime", "preset": "Deleted preset"})
    assert a.preset_choice(ep)[0] == "Test anime"                               # a rule to a missing preset is skipped
    a.rebuild()
    assert a.queue and {p["preset"] for p in a.queue} == {"Test anime"}


def test_webhook_facts_count_before_the_index_knows_the_show(svc, anime_preset):  # noqa: F811
    lib = Path(svc.cfg.roots[0].path)
    new = lib / "TV" / "New Show" / "Season 1" / "New Show - S01E01.mkv"
    clip(new, crf=8)
    svc.cfg.sonarr = Arr("http://sonarr.invalid", "k", [["/tv", str(lib / "TV")]])
    svc.cfg.auto_rules = [{"by": "tag", "value": "kids", "preset": "Test anime"}]
    payload = {"eventType": "Download", "episodeFile": {"relativePath": "Season 1/New Show - S01E01.mkv"},
               "series": {"path": "/tv/New Show", "type": "standard", "genres": ["Family"], "tags": ["kids"]}}
    assert svc.automation.webhook("sonarr", payload) == str(new)
    assert svc.scanner.series_type(str(new)) == "standard"
    assert svc.automation.preset_choice(str(new))[0] == "Test anime"


def test_rule_matching_and_payloads():
    movie = {"source": "Radarr", "genres": ["Science Fiction"], "tags": ["4k"]}
    assert rule_matches({"by": "source", "value": "radarr"}, movie)
    assert rule_matches({"by": "tag", "value": "4K"}, movie)
    assert rule_matches({"by": "genre", "value": "science fiction"}, movie)
    assert not rule_matches({"by": "type", "value": "anime"}, movie)
    assert not rule_matches({"by": "genre", "value": " "}, movie) and not rule_matches({"by": "tag", "value": "4k"}, {})
    assert payload_facts("radarr", {"movie": {"genres": ["Drama"], "tags": ["4k", 3]}}) == \
        {"source": "Radarr", "genres": ["Drama"], "tags": ["4k"]}


def test_old_anime_setting_becomes_the_first_rule(home):
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"auto_preset_anime": "Anime HEVC · 1800k"}))
    c = Config.load()
    assert c.auto_rules == [{"by": "type", "value": "anime", "preset": "Anime HEVC · 1800k"}]
    c.save()
    assert "auto_preset_anime" not in json.loads((home / "config.json").read_text())
    assert Config.load().auto_rules == c.auto_rules


def test_server_clock_only_names_a_zone_that_matches(monkeypatch):
    mst = datetime(2026, 7, 1, 12, 0, tzinfo=timezone(timedelta(hours=-7), "MST"))
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(automation.os, "readlink", lambda p: "/usr/share/zoneinfo/America/Phoenix")
    c = server_clock(mst)
    assert (c["zone"], c["abbr"], c["offset"], c["now"]) == ("America/Phoenix", "MST", -420, "12:00")
    # Docker: the host's zone mounted over the image's Etc/UTC symlink. The clock is right; the name isn't.
    monkeypatch.setattr(automation.os, "readlink", lambda p: "/usr/share/zoneinfo/Etc/UTC")
    assert server_clock(mst)["zone"] == ""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert server_clock(datetime(2026, 7, 1, 12, 0, tzinfo=timezone(timedelta(hours=2), "CEST")))["zone"] == \
        "Europe/Berlin"
    assert server_clock(mst)["zone"] == ""
