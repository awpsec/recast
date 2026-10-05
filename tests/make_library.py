"""Build a tiny fake media library with real (short) video files for testing.

    python tests/make_library.py /path/to/lib
"""
import subprocess
from pathlib import Path

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello from recast\n\n2\n00:00:04,000 --> 00:00:06,000\nSecond line\n"


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], check=True)


def episode(path: Path, codec: list[str], size="1280x720", rate="24000/1001", secs=8, five_one=False, subs=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    srt = path.with_suffix(".srt")
    srt.write_text(SRT)
    audio = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
             "-f", "lavfi", "-i", f"sine=frequency=660:duration={secs}"]
    if five_one:
        audio = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}"]
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={secs}", *audio]
    n_audio = 1 if five_one else 2
    if subs:
        args += ["-i", str(srt)]
    args += ["-map", "0:v"] + sum((["-map", f"{i + 1}:a"] for i in range(n_audio)), [])
    if subs:
        args += ["-map", f"{n_audio + 1}:s", "-c:s", "srt" if path.suffix == ".mkv" else "mov_text",
                 "-metadata:s:s:0", "language=eng"]
    if five_one:
        args += ["-c:a", "ac3", "-b:a", "384k", "-af", "aformat=channel_layouts=5.1(side)",
                 "-metadata:s:a:0", "language=eng"]
    else:
        args += ["-c:a", "libopus", "-b:a", "96k", "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1",
                 "language=eng"]
    # mimic mkvmerge statistics tags, which recast must not carry onto re-encoded streams
    stats = ["-metadata:s:v:0", "BPS=99999999", "-metadata:s:v:0", "NUMBER_OF_BYTES=123456789",
             "-metadata:s:v:0", "language=eng"] if path.suffix == ".mkv" else []
    ff(*args, *codec, *stats, str(path))
    srt.unlink()


def main(root: Path, secs: int = 8, size: str = "1280x720", episodes: int = 3) -> None:
    show = root / "TV" / "Black Clover (2017)"
    for e in range(1, episodes + 1):
        episode(show / "Season 01" / f"Black Clover - S01E{e:03} - Episode {e}.mkv",
                ["-c:v", "libsvtav1", "-preset", "12", "-crf", "28", "-pix_fmt", "yuv420p10le"], size=size, secs=secs)
    episode(root / "TV" / "Avatar (2005)" / "Season 01" / "Avatar - S01E01 - Pilot.mkv",
            ["-c:v", "mpeg2video", "-b:v", "6M"], size="720x480", rate="30000/1001", five_one=True)
    episode(root / "Movies" / "Test Movie (2020)" / "Test Movie (2020).mkv",
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"], size="1920x1080", secs=10)
    print(f"library ready at {root}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--secs", type=int, default=8)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--episodes", type=int, default=3)
    a = ap.parse_args()
    main(Path(a.root), a.secs, a.size, a.episodes)
