import json
import shutil

import pytest

from recast.config import EncoderCap
from recast.encode import EncodeSettings, build_command, resolve_encoder, estimate, quote
from recast.ffmpeg import parse_progress
from recast.probe import MediaInfo, parse, probe_now

MAC = {"libx265": EncoderCap("ok", 60), "hevc_videotoolbox": EncoderCap("ok", 320),
       "hevc_qsv": EncoderCap("missing"), "libx264": EncoderCap("ok", 150), "libsvtav1": EncoderCap("ok", 80)}
PC = {"libx265": EncoderCap("ok", 70), "hevc_nvenc": EncoderCap("ok", 500),
      "hevc_qsv": EncoderCap("failed", reason="no Intel GPU")}


def media(**kw):
    base = dict(path="/lib/a.mkv", size=800 * 1024**2, duration=1420, codec="AV1", codec_name="av1",
                width=1920, height=1080, fps=23.976, frames=34045, vkbps=4300,
                audio=[{"lang": "jpn", "codec": "opus", "channels": 2, "kbps": 128},
                       {"lang": "eng", "codec": "opus", "channels": 6, "kbps": 256}],
                subs=[{"lang": "eng", "codec": "ass", "forced": False},
                      {"lang": "eng", "codec": "hdmv_pgs_subtitle", "forced": True}])
    base.update(kw)
    return MediaInfo(**base)


def flat(g):
    return sum(g, [])


def test_auto_resolves_per_machine():
    s = EncodeSettings(codec="hevc", encoder="auto")
    assert resolve_encoder(s, MAC) == ("hevc_videotoolbox", "auto")
    assert resolve_encoder(s, PC) == ("hevc_nvenc", "auto")
    enc, note = resolve_encoder(EncodeSettings(encoder="hevc_qsv"), MAC)
    assert enc == "hevc_videotoolbox" and "isn't available" in note


def test_command_maps_streams_and_scales():
    s = EncodeSettings(resolution="720", langs="jpn", subs="forced")
    argv = flat(build_command(s, media(), "in.mkv", "out.mkv", MAC, preview="p.jpg"))
    assert argv[argv.index("-c:v") + 1] == "hevc_videotoolbox"
    assert "scale=-2:720:flags=lanczos" in argv
    assert ["-map", "0:a:0"] == argv[argv.index("0:a:0") - 1:argv.index("0:a:0") + 1]
    assert "0:a:1" not in argv           # eng audio dropped by langs
    assert "0:s:1" in argv and "0:s:0" not in argv  # only the forced sub
    assert argv[-1] == "p.jpg" and "-atomic_writing" in argv


def test_mp4_drops_image_subs_and_x265_params_only_for_x265():
    s = EncodeSettings(container="mp4", extra="-x265-params aq-mode=3 -metadata title=x")
    argv = flat(build_command(s, media(), "in", "out.mp4", PC))
    assert "0:s:0" in argv and "0:s:1" not in argv  # PGS can't go in mp4
    assert "-x265-params" not in argv and "title=x" in argv  # nvenc here
    argv = flat(build_command(EncodeSettings(encoder="libx265", extra="-x265-params aq-mode=3"), media(), "i", "o", PC))
    assert argv[argv.index("-x265-params") + 1] == "aq-mode=3"


def test_langs_never_produce_silence():
    argv = flat(build_command(EncodeSettings(langs="fra"), media(), "i", "o", MAC))
    assert "0:a:0" in argv and "0:a:1" in argv


def test_hdr_flags():
    argv = flat(build_command(EncodeSettings(encoder="libx265"), media(hdr="HDR10"), "i", "o", MAC))
    assert "smpte2084" in argv and any("hdr10-opt=1" in a for a in argv)


def test_progress_parse():
    p = parse_progress({"frame": "120", "fps": "48.5", "bitrate": "2031.4kbits/s", "total_size": "1048576",
                        "out_time_us": "5005000", "speed": "2.01x", "progress": "continue"})
    assert p == {"frame": 120, "fps": 48.5, "kbps": 2031.4, "size": 1048576, "time": 5.005, "speed": 2.01,
                 "end": False}


def test_quote():
    assert quote("D:\\a b\\c.mkv", True) == '"D:\\a b\\c.mkv"'
    assert quote("it's.mkv", False) == "'it'\\''s.mkv'"


def test_from_dict_rejects_bad_keys_and_types():
    with pytest.raises(ValueError, match="unknown keys"):
        EncodeSettings.from_dict({"codecc": "hevc"})
    with pytest.raises(ValueError, match="crf"):
        EncodeSettings.from_dict({"crf": "22"})


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe missing")
def test_probe_real_file(library_template):
    f = next((library_template / "TV" / "Black Clover (2017)" / "Season 01").glob("*.mkv"))
    m = probe_now("ffprobe", str(f))
    assert m.codec == "AV1" and m.height == 720 and len(m.audio) == 2 and len(m.subs) == 1
    assert m.audio[0]["lang"] == "jpn" and 7.5 < m.duration < 8.5 and m.frames > 150
    side = next((library_template / "TV" / "Avatar (2005)" / "Season 01").glob("*.mkv"))
    m2 = probe_now("ffprobe", str(side))
    assert m2.codec == "MPEG-2" and m2.audio[0]["channels"] == 6
