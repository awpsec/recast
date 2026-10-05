# recast

A terminal app for re-encoding a Plex / Sonarr / Radarr library with ffmpeg, safely:
encode one file or a whole season, watch it happen, then approve before anything in
your library changes.

- **Browse the library** (arrows, mouse, `/` to find) and see codec, bitrate, tracks and
  Sonarr/Radarr info for any file or folder, plus what a preset would save.
- **Encode a file, or a folder** (try one file first, then "encode all").
- **Network shares are copied to a local scratch folder first**; only one file is
  prefetched ahead, so the NAS isn't hammered.
- **Live progress**: fps, speed, size so far vs projected, bitrate graph, ETA, and
  the actual frame ffmpeg is on, rendered in the terminal.
- **Nothing replaces your files until it passes checks and you approve** (or you
  choose auto-replace). Checks: duration, stream counts, size, decode test.
  A whole folder is **one approval**; approve early and the rest replace as they pass.
- **Replacing is careful**: the new file is copied next to the original as a
  temp file, the original moves to `.recast-trash/<date>/` (purged after N days),
  then the temp file is renamed into place. Sonarr/Radarr get a rescan.
- **Hardware detection on first launch**: every encoder gets a 2-second test encode,
  so `auto` picks what really works on *this* machine (NVENC, VideoToolbox, Quick
  Sync, VA-API, or x265/x264/SVT-AV1).
- **Presets** are JSON with autocomplete as you type: field names, values, Tab to
  complete (quotes and commas included), and anything this machine can't run shows red.

The approval inbox and queue persist across restarts. Nothing times out.

## Install

You need Python 3.10+, ffmpeg (with ffprobe), and [uv](https://docs.astral.sh/uv/) (or pipx).

**macOS**
```bash
brew install ffmpeg uv
```
```bash
uv tool install /path/to/recast
```

**Windows (NVIDIA)**: in PowerShell, using Windows Terminal for proper colours:
```bash
winget install Gyan.FFmpeg astral-sh.uv
```
```bash
uv tool install C:\path\to\recast
```

**Linux server (Intel iGPU / CPU)**: for Quick Sync, jellyfin-ffmpeg is the easy route;
your user needs to be in the `render` group for `/dev/dri/renderD128`:
```bash
sudo apt install jellyfin-ffmpeg7 && sudo usermod -aG render $USER
```
```bash
uv tool install /path/to/recast
```

Then run `recast`. The first launch detects your hardware, asks for your library folder
(e.g. `/Volumes/media`, `M:\`, `/mnt/nas/media`) and a scratch folder
(default `~/Documents/recast`, i.e. `C:\Users\<you>\Documents\recast`), and you're done.
Re-run detection any time from Settings or with `recast --setup`.

`recast --web` serves the same UI in a browser at http://localhost:8765 (needs
`textual-serve`, included in the `dev` extra).

## Keys

| key | |
|---|---|
| `1`–`5` | Library · Queue · Approvals · Presets · Settings (or click) |
| `/` | find a show / season / file |
| `e` | encode the highlighted file or folder |
| `f`, `[` `]` | frame preview on/off, refresh rate |
| `space`, `x` | pause/resume the active encode, cancel a job |
| `y` `n` `r` `c` | approve, deny, retry with different settings, compare frames |
| `ctrl+a` | advanced settings in the encode dialog |
| `tab` `↑↓` `ctrl+space` | accept / choose / open suggestions in the preset editor |
| `ctrl+s` | save preset |
| `?` | help |

## Where things live

| | macOS | Windows | Linux |
|---|---|---|---|
| config, presets, state | `~/Library/Application Support/recast` | `%APPDATA%\recast` | `~/.config/recast` |

- `config.json`: library folders, scratch, detected encoders, Sonarr/Radarr.
- `presets/*.json`: one file per preset; edit in the app or any editor.
- `state.json`: queue and approval inbox.
- `probe-cache.json`: ffprobe results, so headers aren't re-read from the NAS.

Set `RECAST_HOME` to use a different folder.

## Presets

Every field the encode dialog has, plus `extra` for raw ffmpeg flags:

```json
{
  "name": "Anime → HEVC · quality",
  "codec": "hevc",
  "encoder": "libx265",
  "rate_mode": "crf",
  "crf": 20,
  "speed": "slow",
  "tune": "animation",
  "extra": "-x265-params aq-mode=3:psy-rd=1.5"
}
```

`"encoder": "auto"` resolves per machine (hardware first). A pinned encoder that
a machine lacks falls back to the best available one, and the editor tells you so.
`-x265-params` in `extra` is dropped automatically when the encoder isn't x265.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
```
```bash
.venv/bin/python -m pytest
```

The tests generate a small library of real clips with ffmpeg (AV1, MPEG-2 with 5.1(side)
audio, H.264) and run complete encode → approve → replace cycles against it.
`python scripts/demo_web.py` serves the app against a throwaway demo library.
`prototype/` holds the original clickable mock-up.
