"""The web daemon's API: library, encode flow, automation, webhooks, auth and path safety."""
import asyncio
from pathlib import Path

import pytest

from recast.config import Config, Root
from recast.encode import save_preset
from recast.service import Service
from recast.web.server import create_app

from test_automation import CAPS, FAST, clip


@pytest.fixture
def svc(home, tmp_path):
    lib = tmp_path / "lib"
    for e in (1, 2):
        clip(lib / "TV" / "Show" / "Season 1" / f"Show - S01E0{e}.mkv", crf=8, secs=3)
    save_preset("Test fast", "", FAST)
    cfg = Config(roots=[Root("Lib", str(lib), remote=False)], scratch=str(tmp_path / "scratch"), ffmpeg="ffmpeg",
                 ffprobe="ffprobe", encoders=CAPS, default_preset="Test fast", auto_preset="Test fast",
                 desktop_notify=False, verify_decode=False)
    cfg.save()
    s = Service(cfg)
    s.scanner.scan(str(lib))
    return s


@pytest.fixture
async def client(svc, aiohttp_client):
    return await aiohttp_client(create_app(svc))


async def ok(resp, status=200):
    data = await resp.json()
    assert resp.status == status, data
    return data


async def test_state_and_library(client, svc):
    st = await ok(await client.get("/api/state"))
    assert not st["needs_setup"] and st["roots"][0]["reachable"] and "Test fast" in st["presets"]
    root = svc.cfg.roots[0].path
    ov = await ok(await client.get("/api/library/overview", params={"root": root}))
    assert ov["shows"][0]["name"] == "Show" and ov["shows"][0]["files"] == 2 and ov["shows"][0]["saves"] > 0
    show = ov["shows"][0]["path"]
    br = await ok(await client.get("/api/library/browse", params={"path": show}))
    assert [d["name"] for d in br["dirs"]] == ["Season 1"] and br["crumbs"][0]["name"] == "Lib"
    det = await ok(await client.get("/api/library/details", params={"path": show}))
    assert det["kind"] == "folder" and det["files"] == 2 and det["gains"]
    f = str(Path(show) / "Season 1" / "Show - S01E01.mkv")
    det = await ok(await client.get("/api/library/details", params={"path": f}))
    assert det["kind"] == "file" and det["media"]["codec"] == "H.264"


async def test_paths_outside_the_library_are_refused(client, tmp_path):
    for p in ("/etc", str(tmp_path / "scratch"), str(tmp_path / "lib" / ".." / "elsewhere"), ""):
        r = await client.get("/api/library/browse", params={"path": p})
        assert r.status == 400
    r = await client.post("/api/encode", json={"path": "/etc/passwd"})
    assert r.status == 400
    r = await client.post("/api/history/restore", json={"path": "/etc/hosts"})
    assert r.status == 400


async def test_estimate_encode_and_approve(client, svc):
    folder = str(Path(svc.cfg.roots[0].path) / "TV" / "Show")
    est = await ok(await client.post("/api/estimate", json={"path": folder, "preset": "Test fast"}))
    assert est["files"] == 2 and 0 < est["pct"] < 1 and est["quality"]
    r = await ok(await client.post("/api/encode", json={"path": folder, "preset": "Test fast", "scope": "first"}))
    jid = r["job"]
    for _ in range(600):
        job = (await ok(await client.get(f"/api/jobs/{jid}")))["job"]
        if job["stage"] in ("awaiting", "failed"):
            break
        await asyncio.sleep(0.1)
    assert job["stage"] == "awaiting", job
    img = await client.get(f"/api/jobs/{jid}/compare.jpg", params={"pos": 0.5})
    assert img.status == 200 and img.content_type == "image/jpeg"
    inbox = await ok(await client.get("/api/inbox"))
    assert [i["id"] for i in inbox["items"]] == [jid]
    await ok(await client.post(f"/api/inbox/job/{jid}/approve"))
    for _ in range(200):
        if svc.engine.history:
            break
        await asyncio.sleep(0.1)
    hist = await ok(await client.get("/api/history"))
    assert hist["history"][0]["can_restore"]
    again = await ok(await client.post("/api/estimate", json={"path": folder, "preset": "Test fast"}))
    assert again["files"] == 1 and again["skipped"] == {"done by recast": 1}


async def test_automation_settings_and_preview(client, svc):
    r = await client.put("/api/automation", json={"auto_mode": "sometimes"})
    assert r.status == 400
    d = await ok(await client.put("/api/automation", json={"auto_mode": "dry", "auto_threshold": 0.4,
                                                          "review_threshold": 0.9}))
    assert svc.cfg.auto_mode == "dry" and svc.cfg.review_threshold == 0.4  # never above the auto bar
    assert d["counts"]["auto"] == 2
    p = await ok(await client.get("/api/automation/preview", params={"auto": 0.999, "review": 0.1}))
    assert p["auto"] == 0 and p["review"] == 2
    a = await ok(await client.get("/api/automation"))
    assert a["hooks"]["sonarr"].endswith("token=" + svc.cfg.webhook_token)
    assert len(a["queue"]) == 2 and not svc.engine.jobs   # dry run: nothing was started


async def test_webhook_needs_the_token(client, svc):
    lib = Path(svc.cfg.roots[0].path)
    payload = {"eventType": "Download", "series": {"path": str(lib / "TV" / "Show")},
               "episodeFile": {"relativePath": "Season 1/Show - S01E02.mkv"}}
    assert (await client.post("/api/hook/sonarr", json=payload)).status == 403
    assert (await client.post("/api/hook/sonarr?token=nope", json=payload)).status == 403
    d = await ok(await client.post(f"/api/hook/sonarr?token={svc.cfg.webhook_token}", json=payload))
    assert d["path"] == str(lib / "TV" / "Show" / "Season 1" / "Show - S01E02.mkv")
    assert svc.automation.pending[0]["path"] == d["path"]


async def test_password_protects_everything_but_hooks(client, svc):
    await ok(await client.post("/api/password", json={"password": "hunter2"}))
    assert svc.cfg.web_password_hash and "hunter2" not in svc.cfg.web_password_hash
    client.session.cookie_jar.clear()
    assert (await client.get("/api/state")).status == 401
    assert (await client.get("/api/library/browse", params={"path": svc.cfg.roots[0].path})).status == 401
    r = await client.get("/", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/login"
    assert (await client.get("/login")).status == 200
    assert (await client.post("/api/login", json={"password": "wrong"})).status == 401
    hook = await client.post(f"/api/hook/radarr?token={svc.cfg.webhook_token}", json={"eventType": "Test"})
    assert hook.status == 200                                  # Sonarr/Radarr can't log in; the token guards it
    await ok(await client.post("/api/login", json={"password": "hunter2"}))
    await ok(await client.get("/api/state"))
    await ok(await client.post("/api/password", json={"password": ""}))
    client.session.cookie_jar.clear()
    await ok(await client.get("/api/state"))


async def test_presets_and_settings(client, svc):
    await ok(await client.put("/api/presets", json={"name": "Mine", "description": "x",
                                                    "settings": {"codec": "hevc", "bitrate": 1500}}))
    names = [p["name"] for p in (await ok(await client.get("/api/presets")))["presets"]]
    assert "Mine" in names
    assert (await client.delete("/api/presets", params={"name": "Test fast"})).status == 400  # the default
    await ok(await client.delete("/api/presets", params={"name": "Mine"}))
    r = await client.put("/api/settings", json={"roots": [{"path": "/definitely/not/here"}]})
    assert r.status == 400
    await ok(await client.put("/api/settings", json={"trash_days": "7", "verify_decode": True}))
    assert svc.cfg.trash_days == 7 and svc.cfg.verify_decode is True
    s = await ok(await client.get("/api/settings"))
    assert s["sonarr"]["has_key"] is False and "api_key" not in s["sonarr"]   # keys never go back out


async def test_index_busts_the_asset_cache(client):
    r = await client.get("/")
    html = await r.text()
    assert "/static/app.js?v=" in html and "__V__" not in html
    assert (await client.get("/static/app.css")).status == 200
