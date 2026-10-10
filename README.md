# recast

Re-encode a Plex / Sonarr / Radarr library with ffmpeg, safely: encode one file or a whole
season, watch it happen, and approve before anything in your library changes. Or turn on
automation and let it keep the library optimized by itself, one file at a time.

Two front ends over the same engine:

- **`recast`** — a terminal app (keyboard + mouse).
- **`recast web`** — a browser app plus a background daemon with Sonarr/Radarr automation.

Only one of them runs per machine at a time (they share the queue and scratch folder).

### What it does

- **Find your biggest wins**: highlight a library folder and recast scans it in the
  background (headers only, cached — a 10k-file NAS library takes ~20 min once, then
  it's instant), then ranks every show by how much space your default preset would free.
  `p` switches the preset (or "best of all", which never downscales or drops surround).
  `e` on a row encodes that show; show folders in the tree get their size and potential
  saving; shows spread over several folders (leftover downloads, loose season packs)
  are flagged.
- **Browse the library** (arrows, mouse; it reopens where you left off) and see codec,
  bitrate, tracks and Sonarr/Radarr info for any file or folder. `/` is a quick-open
  that searches every show, season and episode.
- **See what every preset would do** to that file, season or show: files, size after,
  % saved. Once you've encoded something from a show, the numbers are *measured* from
  your real results instead of estimated.
- **Encode a file, or a folder.** The flow that works best: encode one episode, look at it,
  then press **"Approve + rest of season / whole show"**. Same settings, one approval for
  the batch, already-done episodes skipped. recast remembers what you used per show.
- **Library marks every file**: ✓ re-encoded by recast, ● queued/encoding, ⚑ waiting
  for you.
- **Network shares are copied to a local scratch folder first**; only one file is
  prefetched ahead, so the NAS isn't hammered.
- **Live progress**: fps, speed, size so far vs projected, bitrate graph, ETA, and
  the actual frame ffmpeg is on, rendered in the terminal.
- **Nothing replaces your files until it passes checks and you approve** (or you
  choose auto-replace). Checks: duration, stream counts, size, decode test.
  A whole folder is **one approval**; approve early and the rest replace as they pass.
- **Replacing is careful**: the new file is copied next to the original as a
  temp file, the original moves to `.recast-trash/<date>/` (purged after N days),
  then the temp file is renamed into place. `…1080p AV1.mkv` becomes `…1080p HEVC.mkv`.
  Sonarr/Radarr get a rescan. It refuses (and leaves everything alone) if the original
  changed since it was encoded, a file with the new name already exists, or there's
  no room.
- **Undo**: press `u` on any file recast replaced to put the original back while it's
  still in the trash.
- **Long runs are safe to leave**: if the NAS drops off, jobs wait instead of failing;
  `space` pauses everything; the computer is kept awake while jobs run; you get a
  desktop notification when a batch is done or something needs you.
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

Then run `recast`. The first launch detects your hardware, suggests the media folders on
your mounted drives/shares (pick one or several, e.g. `…/tv` and `…/movies`) and a scratch folder
(default `~/Documents/recast`, i.e. `C:\Users\<you>\Documents\recast`), and you're done.
Re-run detection any time from Settings or with `recast --setup`.

For the browser app, run `recast web` instead and open http://localhost:8484 — on a fresh
install it walks through the same setup in the browser.

## Web app

```bash
recast web
```

Dashboard (space saved, what could still be freed, what's encoding with a live frame, what
automation will do next), Library (shows ranked by potential saving, browse into any folder,
see what every preset would do, encode), Queue, Review (side-by-side frame compare, replace or
discard), Automation, Presets, History (undo) and Settings. Light and dark follow your system.

It listens on this computer only. To open it from another machine (e.g. it runs on the
server and you're on the couch):

```bash
recast web --host 0.0.0.0 --port 8484
```

Set a password first (Settings → Security); recast warns until you do.

## Automation

Turn it on in the web app (**Automation** page). Every file gets a score: the estimated space
saving with your preferred preset — measured from your real results on that show once there are
some. You set two numbers:

| score | what happens |
|---|---|
| ≥ **replace automatically** (default 30%) | encoded, checked, and swapped in by itself |
| between the two | listed under **Review → Borderline files**; nothing is encoded until you say so |
| < **ask me** (default 10%) | skipped (remembered until the file changes) |

The page shows, live as you drag the sliders, how many files land in each bucket and how much
space that frees. A **dry run** mode makes all the decisions and shows them without encoding
anything.

Safety valve: an automatic encode only replaces the original if the *real* saving also clears
your bar and every check passes (duration, streams, size, decode test). Otherwise it waits in
Review with a note saying why. Originals go to recast's trash, so History can undo it.

**Pacing** — the point is a library that gets better overnight without a hammered NAS or a full
scratch disk:

- One encode at a time, with at most **one** file copied ahead while it runs. Never more, so the
  first run on a 10,000-file library trickles through one by one instead of queueing everything.
- Each file is read from the NAS once, sequentially, and written back once.
- Biggest savings first. New downloads (via webhook) jump to the front.
- Optional working hours (e.g. 01:00–08:00) for starting new files.
- Stops starting new work if encodes waiting for you fill the scratch budget.
- If the share drops off, it waits; nothing is thrown away.
- Finding new files: a cheap folder re-list every N hours (default 24) — only new or changed
  files get their headers read. With webhooks you rarely need it.
- For series Sonarr marks as **anime**, a separate preset can be used (e.g. *Anime HEVC · 1800k*).

### Sonarr / Radarr

1. **Settings → Sonarr & Radarr**: URL + API key, then *Save & test*. recast maps their folder
   paths to yours automatically (e.g. `/tv` → `/Volumes/nas/tv`) — needed for webhooks, anime
   detection, episode names and a rescan after each replace.
2. **Automation** page: copy the webhook URL for each.
3. In Sonarr/Radarr: **Settings → Connect → + → Webhook**, paste the URL, method POST,
   triggers *On Import* and *On Upgrade*, press *Test* (it shows up in recast's activity log).

New imports are looked at ~90 seconds later (so Sonarr/Radarr are done with them), scored, and
encoded next if they clear your bar. An upgrade of a file recast already re-encoded counts as a
new file.

### Run it as a service

Run it as your user (that's who has the network share mounted).

**macOS** — `~/Library/LaunchAgents/com.recast.web.plist`, then
`launchctl load ~/Library/LaunchAgents/com.recast.web.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.recast.web</string>
  <key>ProgramArguments</key><array><string>/Users/YOU/.local/bin/recast</string><string>web</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

**Linux** — `~/.config/systemd/user/recast.service`, then
`systemctl --user enable --now recast` (and `loginctl enable-linger $USER` so it runs without a login):

```ini
[Unit]
Description=recast web
After=network-online.target

[Service]
ExecStart=%h/.local/bin/recast web --host 0.0.0.0
Restart=on-failure

[Install]
WantedBy=default.target
```

**Windows** — Task Scheduler → *Create Task*: trigger *At log on*, action
`C:\Users\YOU\.local\bin\recast.exe` with arguments `web`, and untick *Stop the task if it runs
longer than…*.

## Keys

| key | |
|---|---|
| `1`–`5` | Library · Queue · Approvals · Presets · Settings (or click) |
| `/` | find any show / season / episode |
| `e`, Enter, double-click | encode the highlighted episode (or folder with `e`) |
| `R` / `p` | on the library line: rescan · change the preset the ranking uses |
| `u` | restore the original of a file recast replaced |
| `f`, `[` `]` | frame preview on/off, refresh rate |
| `space` | pause / resume everything |
| `x` `r` `del` | in Queue: cancel a job, retry a failed one, clear finished |
| `y` `n` `r` `c` | approve, deny, retry with different settings, compare frames |
| `a` `s` | in the approval prompt: approve + rest of season / whole show |
| `ctrl+a` | advanced settings in the encode dialog |
| `tab` `↑↓` `ctrl+space` | accept / choose / open suggestions in the preset editor |
| `ctrl+s` | save preset |
| `?` | help (the footer always shows the keys for the current tab) |

## Where things live

| | macOS | Windows | Linux |
|---|---|---|---|
| config, presets, state | `~/Library/Application Support/recast` | `%APPDATA%\recast` | `~/.config/recast` |

- `config.json`: library folders, scratch, detected encoders, Sonarr/Radarr.
- `presets/*.json`: one file per preset; edit in the app or any editor.
- `state.json`: queue, approval inbox and the history of replaced files.
- `probe-cache.json`: ffprobe results, so headers aren't re-read from the NAS.
- `overview.json`: the last listing of each library folder.
- `automation.json`: borderline/skipped decisions and the activity log.

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
`python scripts/demo_webapp.py` runs the web app against a throwaway demo library
(`scripts/demo_web.py` does the same for the terminal app via textual-serve).
`prototype/` holds the original clickable mock-up.
