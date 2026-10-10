"""What a re-encode keeps: every subtitle track (ASS styling, SRT, forced flags, languages, titles),
font attachments, chapters, audio tracks and tags. And what a restore puts back: the exact original."""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from recast.config import Config, Root
from recast.encode import EncodeSettings
from recast.engine import Engine
from recast.probe import probe_now

from test_automation import CAPS
from test_engine import run_until

ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sign,Fancy Font,64,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:02.50,Sign,,0,0,0,,{\\pos(640,100)\\fad(200,200)}A styled sign
"""
SRT = "1\n00:00:00,500 --> 00:00:02,500\nHola\n"
CHAPTERS = """;FFMETADATA1
title=Pilot
[CHAPTER]
TIMEBASE=1/1000
START=0
END=1500
title=Cold open
[CHAPTER]
TIMEBASE=1/1000
START=1500
END=3000
title=Part one
"""
FAST = EncodeSettings(codec="hevc", encoder="libx265", speed="ultrafast", rate_mode="crf", crf=32)


def rich_episode(path: Path, work: Path) -> None:
    """A 3 s mkv with everything a fansub/remux release carries."""
    work.mkdir(parents=True, exist_ok=True)
    (work / "signs.ass").write_text(ASS)
    (work / "es.srt").write_text(SRT)
    (work / "chapters.txt").write_text(CHAPTERS)
    (work / "Fancy.ttf").write_bytes(b"\x00\x01\x00\x00" + os.urandom(2048))  # contents aren't checked by mkv
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
        "-i", str(work / "signs.ass"), "-i", str(work / "es.srt"), "-i", str(work / "chapters.txt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s",
        "-map_metadata", "5", "-map_chapters", "5",
        "-attach", str(work / "Fancy.ttf"), "-metadata:s:t", "mimetype=application/x-truetype-font",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-c:a", "aac", "-c:s", "copy",
        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
        "-metadata:s:a:1", "title=English dub", "-disposition:a:0", "default", "-disposition:a:1", "0",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Signs & Songs",
        "-disposition:s:0", "forced", "-metadata:s:s:1", "language=spa",
        str(path)], check=True)


def streams(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-show_format", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def summary(info: dict) -> dict:
    def tag(s, k):
        return {kk.lower(): v for kk, v in (s.get("tags") or {}).items()}.get(k, "")
    by = lambda t: [s for s in info["streams"] if s["codec_type"] == t]  # noqa: E731
    return {
        "audio": [(tag(s, "language"), tag(s, "title"), s["disposition"]["default"]) for s in by("audio")],
        "subs": [(s["codec_name"], tag(s, "language"), tag(s, "title"), s["disposition"]["forced"])
                 for s in by("subtitle")],
        "fonts": [(tag(s, "filename"), tag(s, "mimetype")) for s in by("attachment")],
        "chapters": [c["tags"]["title"] for c in info["chapters"]],
        "title": {k.lower(): v for k, v in info["format"].get("tags", {}).items()}.get("title", ""),
    }


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def engine(home, lib: Path, tmp_path: Path, **kw):
    cfg = Config(roots=[Root("NAS", str(lib), remote=True)], scratch=str(tmp_path / "scratch"), ffmpeg="ffmpeg",
                 ffprobe="ffprobe", encoders=CAPS, desktop_notify=False, **kw)
    return cfg, Engine(cfg)


async def encode_and_replace(eng, cfg, src: Path, s: EncodeSettings = FAST):
    j = eng.add_single(probe_now("ffprobe", str(src)), cfg.roots[0], s, "Test fast")
    await run_until(eng, lambda: j.stage in ("awaiting", "failed"))
    assert j.stage == "awaiting" and not j.flag, (j.error, j.flag, j.log[-5:])
    eng.approve(j)
    await run_until(eng, lambda: j.stage in ("replaced", "failed") or (j.stage == "awaiting" and j.flag))
    assert j.stage == "replaced", (j.error, j.flag)
    return j


async def test_subtitles_fonts_chapters_and_tags_survive(home, tmp_path):
    lib = tmp_path / "lib"
    src = lib / "Anime" / "Show" / "Season 1" / "Show - S01E01 [x264].mkv"
    rich_episode(src, tmp_path / "work")
    before = summary(streams(src))
    assert len(before["subs"]) == 2 and before["fonts"] and len(before["chapters"]) == 2  # the fixture is right
    cfg, eng = engine(home, lib, tmp_path)
    j = await encode_and_replace(eng, cfg, src)
    after_path = Path(j.final)
    assert after_path.name == "Show - S01E01 [HEVC].mkv"
    after = summary(streams(after_path))
    assert after == before, (before, after)
    assert after["subs"][0] == ("ass", "eng", "Signs & Songs", 1)               # styled signs, still forced
    sub_text = subprocess.run(["ffmpeg", "-v", "error", "-i", str(after_path), "-map", "0:s:0", "-f", "ass", "-"],
                              capture_output=True, text=True).stdout
    assert "\\pos(640,100)" in sub_text and "Style: Sign,Fancy Font" in sub_text  # ASS styling untouched


async def test_mp4_keeps_text_subs(home, tmp_path):
    lib = tmp_path / "lib"
    src = lib / "Movies" / "Film (2020)" / "Film (2020).mkv"
    rich_episode(src, tmp_path / "work")
    cfg, eng = engine(home, lib, tmp_path)
    j = await encode_and_replace(eng, cfg, src, EncodeSettings(**{**FAST.__dict__, "container": "mp4"}))
    after = summary(streams(j.final))
    assert [s[1] for s in after["subs"]] == ["eng", "spa"] and len(after["chapters"]) == 2
    assert [a[0] for a in after["audio"]] == ["jpn", "eng"]


async def test_restore_is_byte_identical_and_survives_a_restart(home, tmp_path):
    lib = tmp_path / "lib"
    src = lib / "TV" / "Show" / "Season 1" / "Show - S01E02 [x264].mkv"
    rich_episode(src, tmp_path / "work")
    digest, size = sha(src), src.stat().st_size
    cfg, eng = engine(home, lib, tmp_path)
    j = await encode_and_replace(eng, cfg, src)
    assert not src.exists() and Path(j.orig).is_file() and ".recast-trash" in j.orig
    assert sha(j.orig) == digest                     # moved (renamed), not copied or touched
    eng.save(force=True)
    eng2 = Engine(cfg)                               # a restart later still knows how to undo it
    msg = eng2.restore(j.final)
    assert "restored" in msg and sha(src) == digest and src.stat().st_size == size
    assert not Path(j.final).exists()
    undone = list((lib / ".recast-trash").rglob("*.recast-undone.mkv"))
    assert len(undone) == 1                          # the re-encode is set aside, never left in the show
    assert not any(p.name.endswith(".mkv") for p in src.parent.iterdir() if p != src)


async def test_restore_in_keep_mode_leaves_nothing_extra_in_the_show(home, tmp_path):
    lib = tmp_path / "lib"
    src = lib / "TV" / "Show" / "Season 1" / "Show - S01E03.mkv"
    rich_episode(src, tmp_path / "work")
    digest = sha(src)
    cfg, eng = engine(home, lib, tmp_path, originals="keep")
    j = await encode_and_replace(eng, cfg, src)
    assert j.orig == str(src) + ".orig"
    eng.restore(j.final)
    assert sha(src) == digest
    assert sorted(p.name for p in src.parent.iterdir()) == [src.name]   # no .orig, no stray re-encode for Plex


async def test_restore_refuses_when_the_file_changed_since(home, tmp_path):
    """Sonarr upgraded the episode after recast replaced it: restoring would throw away the upgrade."""
    lib = tmp_path / "lib"
    src = lib / "TV" / "Show" / "Season 1" / "Show - S01E04.mkv"
    rich_episode(src, tmp_path / "work")
    cfg, eng = engine(home, lib, tmp_path)
    j = await encode_and_replace(eng, cfg, src)
    Path(j.final).write_bytes(b"a newer download" * 1000)
    try:
        eng.restore(j.final)
        raise AssertionError("restore should have refused")
    except Exception as e:  # noqa: BLE001
        assert "changed" in str(e)
    assert Path(j.final).read_bytes().startswith(b"a newer download") and Path(j.orig).exists()


async def test_purged_originals_are_recorded(home, tmp_path):
    lib = tmp_path / "lib"
    src = lib / "TV" / "Show" / "Season 1" / "Show - S01E05.mkv"
    rich_episode(src, tmp_path / "work")
    cfg, eng = engine(home, lib, tmp_path)
    j = await encode_and_replace(eng, cfg, src)
    rel = Path(j.orig).relative_to(lib / ".recast-trash")
    old = lib / ".recast-trash" / "2020-01-01"
    os.rename(lib / ".recast-trash" / rel.parts[0], old)   # pretend it was replaced years ago
    eng.history[-1]["orig"] = str(old.joinpath(*rel.parts[1:]))
    assert eng.kept_until(eng.history[-1]) < time.time()
    removed = eng.purge_trash()
    assert removed == [str(old)] and not old.exists()
    h = eng.history[-1]
    assert h["purged"] and not eng.can_restore(h)
    try:
        eng.restore(j.final)
        raise AssertionError("restore should have refused")
    except Exception as e:  # noqa: BLE001
        assert "purged" in str(e) or "no longer" in str(e)
