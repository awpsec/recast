"""End-to-end: real ffmpeg encodes against the generated library."""
import asyncio
import os
from pathlib import Path

from recast.config import Config, EncoderCap, Root
from recast.encode import EncodeSettings, already_target
from recast.engine import Engine
from recast.probe import probe_now

CAPS = {"libx265": EncoderCap("ok", 60), "hevc_videotoolbox": EncoderCap("ok", 300),
        "libx264": EncoderCap("ok", 150)}


def make(home, library, tmp_path, **kw):
    cfg = Config(roots=[Root("NAS", str(library), remote=True)], scratch=str(tmp_path / "scratch"),
                 ffmpeg="ffmpeg", ffprobe="ffprobe", encoders=CAPS, **kw)
    events = []
    eng = Engine(cfg, lambda k, o: events.append((k, o)))
    return cfg, eng, events


async def run_until(eng, cond, timeout=120):
    for _ in range(int(timeout / 0.1)):
        eng.tick()
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out; stages=" + str({j.id: (j.stage, j.error, j.flag) for j in eng.jobs.values()}))


async def test_preview_encode_approve_replace(home, library, tmp_path):
    cfg, eng, events = make(home, library, tmp_path)
    src = next((library / "TV" / "Black Clover (2017)" / "Season 01").glob("*E001*"))
    m = probe_now("ffprobe", str(src))
    j = eng.add_single(m, cfg.roots[0], EncodeSettings(codec="hevc"), "test")
    await run_until(eng, lambda: j.stage in ("awaiting", "failed"))
    assert j.stage == "awaiting", (j.error, j.log[-5:])
    assert not j.flag, j.flag
    assert ("finished", j) in events
    assert j.out_info["codec"] == "HEVC" and len(j.out_info["audio"]) == 2 and len(j.out_info["subs"]) == 1
    assert j.out_info["vkbps"] < 5000, j.out_info["vkbps"]  # not the source's stale BPS=99999999 tag
    import json as _json, subprocess as _sp
    tags = _json.loads(_sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream_tags",
                                "-of", "json", j.out], capture_output=True, text=True).stdout)["streams"][0]["tags"]
    assert "BPS" not in tags and "NUMBER_OF_BYTES" not in tags and tags.get("language") == "eng", tags
    assert j.frame > 150 and j.out_size > 0
    assert os.path.exists(j.out) and "hevc_videotoolbox" in " ".join(j.log)
    eng.approve(j)
    await run_until(eng, lambda: j.stage in ("replaced", "awaiting", "failed"))
    assert j.stage == "replaced", j.error
    assert probe_now("ffprobe", str(src)).codec == "HEVC"             # library file is now HEVC
    trash = list((library / ".recast-trash").rglob("*E001*"))
    assert trash and probe_now("ffprobe", str(trash[0])).codec == "AV1"  # original kept in trash
    assert not os.path.exists(j.work_src) and not os.path.exists(j.out)  # scratch cleaned
    eng.save(force=True)
    again = Engine(cfg)  # state survives a restart
    assert again.jobs[j.id].stage == "replaced"


async def test_batch_ask_once_then_auto(home, library, tmp_path):
    cfg, eng, events = make(home, library, tmp_path)
    season = library / "TV" / "Black Clover (2017)" / "Season 01"
    files = [probe_now("ffprobe", str(p)) for p in sorted(season.glob("*.mkv"))]
    s = EncodeSettings(codec="hevc", encoder="libx265", speed="ultrafast", rate_mode="crf", crf=38)
    b = eng.add_batch(str(season), files, cfg.roots[0], s, "batch", lambda m: already_target(s, m, CAPS))
    await run_until(eng, lambda: len(eng.batch_jobs(b, "awaiting")) >= 1)
    assert b in eng.inbox() and any(k == "batch_first" for k, _ in events)
    assert not any(j in eng.inbox() for j in eng.batch_jobs(b))  # one inbox item, not three
    eng.approve(b)  # approve early: the rest should replace without asking
    await run_until(eng, lambda: all(j.stage in ("replaced", "failed", "awaiting") for j in eng.batch_jobs(b)),
                    timeout=180)
    assert [j.stage for j in eng.batch_jobs(b)] == ["replaced"] * 3, [(j.stage, j.flag, j.error) for j in eng.batch_jobs(b)]
    assert any(k == "batch_replaced" for k, _ in events)
    assert eng.inbox() == []


async def test_opus_51_side_and_deinterlace(home, library, tmp_path):
    cfg, eng, _ = make(home, library, tmp_path, originals="keep")
    src = next((library / "TV" / "Avatar (2005)" / "Season 01").glob("*.mkv"))
    s = EncodeSettings(encoder="libx265", speed="ultrafast", audio="opus", deinterlace=True, rate_mode="crf")
    j = eng.add_single(probe_now("ffprobe", str(src)), cfg.roots[0], s, "dvd")
    await run_until(eng, lambda: j.stage in ("awaiting", "failed"))
    assert j.stage == "awaiting", (j.error, j.log[-6:])
    assert j.out_info["audio"][0]["codec"] == "opus" and j.out_info["audio"][0]["channels"] == 6
    eng.approve(j)
    await run_until(eng, lambda: j.stage in ("replaced", "failed", "awaiting"))
    assert Path(str(src) + ".orig").exists()


async def test_cancel_mid_encode(home, library, tmp_path):
    cfg, eng, _ = make(home, library, tmp_path)
    src = library / "Movies" / "Test Movie (2020)" / "Test Movie (2020).mkv"
    j = eng.add_single(probe_now("ffprobe", str(src)), cfg.roots[0],
                       EncodeSettings(encoder="libx265", speed="veryslow"), "slow")
    await run_until(eng, lambda: j.stage == "encoding" and j.frame > 0)
    eng.cancel(j)
    await run_until(eng, lambda: j.id not in eng._tasks)
    assert j.stage == "cancelled" and not os.path.exists(j.out) and not os.path.exists(j.work_src)
    assert probe_now("ffprobe", str(src)).codec == "H.264"


async def test_longer_clip_passes_decode_test(home, tmp_path):
    """-sseof seeks on longer files made the null muxer warn about timestamps; that's not a decode error."""
    import make_library
    lib = tmp_path / "longlib"
    make_library.episode(lib / "Show" / "S01E01.mkv", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12"],
                         size="1280x720", secs=45)
    cfg = Config(roots=[Root("NAS", str(lib), remote=False)], scratch=str(tmp_path / "scratch"),
                 ffmpeg="ffmpeg", ffprobe="ffprobe", encoders=CAPS)
    eng = Engine(cfg)
    j = eng.add_single(probe_now("ffprobe", str(lib / "Show" / "S01E01.mkv")), cfg.roots[0],
                       EncodeSettings(codec="hevc", rate_mode="crf", crf=28), "t")
    await run_until(eng, lambda: j.stage in ("awaiting", "failed"))
    assert j.stage == "awaiting" and not j.flag, (j.flag, j.error)
    assert any("verify passed" in l for l in j.log)
