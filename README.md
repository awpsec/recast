# recast

Re-encode a Plex / Sonarr / Radarr library with ffmpeg, safely, and get your disk space back.

recast comes in two parts that share an engine but nothing else:

- **recast-server** is the always-on part. It runs in Docker next to Sonarr and Radarr, reads from
  the NAS, and works through the library over time, one file at a time. You use it from a web app
  (dashboard, library, queue, review, automation, history), and Sonarr/Radarr webhooks send it new
  downloads.
- **recast** is the terminal app, for one-off jobs on whatever machine you're sitting at, like
  pushing a big batch through a gaming PC's NVENC.

Each one has its own settings, queue and history, even on the same machine. Files either one
re-encodes are tagged, so the other never re-encodes them.

---

## recast-server (Docker)

On the server (any Linux with Docker, e.g. Debian 13):

```bash
mkdir -p ~/recast && cd ~/recast
curl -fsSLO https://raw.githubusercontent.com/awpsec/recast/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/awpsec/recast/main/.env.example -o .env
nano .env
```
```bash
docker compose up -d
```

Then open `http://<server>:8484`. In `.env`, set:

- `TV_DIR` / `MOVIES_DIR`: where the NAS share is mounted on the server. Use the same folders
  Sonarr/Radarr use (they show up inside the container as `/tv` and `/movies`).
- `SCRATCH_DIR`: a local disk with room for a few episodes. Files get copied here, encoded, checked,
  and only then written back.
- `PUID` / `PGID`: the user that owns your media (run `id` as that user to get the numbers).
  Replaced files keep that owner, so Sonarr/Radarr can still manage them.
- `TZ`: your timezone, used for working hours.

On first start the server sets itself up from these values. It tests every encoder (Quick Sync and
VA-API through `/dev/dri`, NVENC with the NVIDIA runtime, x265/SVT-AV1 otherwise), reads the
library headers once (cached after that), and ranks every show by how much space it could free.
**Automation starts Off**, so nothing is encoded until you say so. Set a password under
**Settings → Security** before you use it outside your home network.

**Updating:**

```bash
docker compose pull && docker compose up -d
```

The web app shows when a new release is out (sidebar and Settings). Your settings, queue and history
live in `CONFIG_DIR` and carry over. To pin a version, set `RECAST_TAG=0.2.0` in `.env`
(`edge` follows the main branch).

<details><summary>Notes for a Debian server with the NAS on SMB/NFS</summary>

- Mount the share on the host, e.g. in `/etc/fstab`:
  `//zeddnas/media /mnt/nas cifs credentials=/root/.smbcred,uid=1000,gid=1000,iocharset=utf8,_netdev 0 0`.
  Set `uid`/`gid` to your `PUID`/`PGID`. recast notices the share is remote and copies each file
  to scratch before encoding it, reading it once, start to finish.
- Intel iGPU: `ls /dev/dri` should list `renderD128`. The container gets access to it
  automatically. If the server has no GPU, delete the two `devices` lines in `compose.yaml`.
- Hardlinks: if your downloads are hardlinked into the library (torrents still seeding),
  re-encoding a file wouldn't free anything until the torrent is removed. Automation skips
  hardlinked files and says so.

</details>

<details><summary>Without Docker</summary>

```bash
uv tool install git+https://github.com/awpsec/recast
```
```bash
recast-server --host 0.0.0.0
```

It uses `~/.config/recast-server` (Linux), `~/Library/Application Support/recast-server` (macOS)
or `%APPDATA%\recast-server`, and walks through setup in the browser. If the terminal app is
already set up on that machine, the server copies its library folders, presets and header cache the
first time it starts. As a systemd user service (`~/.config/systemd/user/recast-server.service`,
then `systemctl --user enable --now recast-server` and `loginctl enable-linger $USER`):

```ini
[Unit]
Description=recast-server
After=network-online.target

[Service]
ExecStart=%h/.local/bin/recast-server --host 0.0.0.0
Restart=on-failure

[Install]
WantedBy=default.target
```

</details>

### Environment variables

| variable | |
|---|---|
| `RECAST_LIBRARIES` | library folders on first start, e.g. `/tv,/movies` or `TV=/tv,Films=/movies` |
| `RECAST_SCRATCH` | scratch folder (the image uses `/scratch`) |
| `RECAST_SONARR_URL`, `RECAST_SONARR_API_KEY`, `RECAST_RADARR_URL`, `RECAST_RADARR_API_KEY` | connect Sonarr/Radarr on first start |
| `RECAST_HOST`, `RECAST_PORT` | listen address (the image uses `0.0.0.0:8484`) |
| `PUID`, `PGID`, `UMASK` | who the server runs as inside the container (default `1000:1000`, `002`) |
| `RECAST_LOG` | `INFO` (default) or `DEBUG`; everything that happens is logged to `docker logs recast` |

Environment values only fill in settings that are still empty. Once you change something in the
web app, your change stays.

`GET /api/health` needs no login (it's what Docker's healthcheck and uptime monitors use).

---

## Automation

The **Automation** page has four modes:

| mode | what happens |
|---|---|
| **Off** | nothing happens unless you start it. You encode from the Library. |
| **Dry run** | recast makes every decision and shows them, but encodes nothing. Use it to check your thresholds. |
| **Ask first** | clear wins are encoded one at a time, then wait in the Inbox for your OK before anything is replaced (at most 10 waiting by default). |
| **Automatic** | clear wins are encoded and replaced on their own. Borderline files wait in Review. |

Every file gets a score: how much space your preferred preset would save, estimated at first and
measured from your real results on that show once there are some. You set two thresholds:

| score | what happens |
|---|---|
| ≥ **replace automatically** (default 30%) | encoded, checked, and replaced on its own (in Ask first: waits for your OK) |
| between the two | listed in **Review**. Nothing is encoded until you say so. |
| < **ask me** (default 10%) | skipped, until the file changes |

As you drag the sliders, the page shows how many files land in each bucket and how much space they'd
free.

Automation won't replace a file just because it was encoded:

- An automatic encode only replaces the original if the *real* saving also clears your threshold
  and every check passes (duration, track counts, size, decode test). Otherwise it waits in Review
  with a note saying why.
- If the preset would cost a file anything besides bitrate (4K down to 1080p, HDR, Dolby Vision,
  surround sound), that file always waits in Review, however big the saving.
- Shows or folders you exclude (a checkbox in the Library details) are never touched by
  automation. You can still encode them by hand.
- Files recast already re-encoded (the `RECAST` tag, from any install) and originals you restored
  are never redone.

### Pacing

The goal is a library that improves overnight without a hammered NAS or a full scratch disk:

- One encode at a time, with at most one file copied ahead. A 10,000-file library goes through
  one file at a time instead of being queued all at once.
- Each file is read from the NAS once, start to finish, and written back once.
- Biggest savings go first. New downloads (from the webhook) jump to the front.
- Optional working hours (e.g. 01:00–08:00) for starting new files.
- Scratch budget (default 200 GB): no new work starts while encodes waiting for you use more than that.
- Library drive floor (default 50 GB free): automation pauses below it. Each replace keeps the original
  in the trash for a while, so the space only comes back when it's purged (see below).
- If the share drops off, recast waits and doesn't lose anything. It re-lists folders every 24 hours (cheap) to
  find new files. With webhooks you rarely need it.
- Series that Sonarr marks as **anime** can use their own preset (e.g. *Anime HEVC · 1800k*).

### Sonarr / Radarr

1. **Settings → Sonarr & Radarr**: enter the URL and API key, then click *Save & test*. recast maps
   their folder paths to its own automatically. It needs this for webhooks, anime detection,
   episode names and the rescan after each replace.
2. On the **Automation** page, copy the webhook URL for each.
3. In Sonarr/Radarr: **Settings → Connect → + → Webhook**. Paste the URL, set the method to POST,
   tick *On Import* and *On Upgrade*, and press *Test* (it shows up in recast's activity log).

New imports are looked at about 90 seconds later, scored, and encoded next if they clear your bar.
If Sonarr upgrades an episode recast already re-encoded, the upgrade counts as a new file.

---

## Replacing, and getting the original back

recast never writes over a file while it's working on it:

1. The source is copied to scratch (if it's on a share), encoded there, then checked: duration,
   audio and subtitle track counts, size, and a decode test.
2. When you (or automation) approve, the new file is copied next to the original as a hidden
   `.recast-…part` file.
3. The original is **renamed** into `<library>/.recast-trash/<date>/<same folders>/`. It's the same
   share, so this is instant and copies nothing. A `.plexignore` keeps Plex out of the trash.
4. The new file is renamed into place. `…1080p AV1.mkv` becomes `…1080p HEVC.mkv`, and Sonarr/Radarr
   get a rescan.

If anything is off (the original changed since it was encoded, a file with the new name already
exists, or there isn't enough space), recast stops and leaves everything as it was. If step 4 fails,
the original is put back.

**Restore** (History, the file's details, or `u` in the terminal app) is two renames in the other
direction. The original returns under its old name, byte for byte, and the re-encode goes to the
trash. There's no diff or patch involved: the whole original sits in the trash until it's purged.
That's why restoring is instant, and also why **the space only comes back after the purge**
(default 14 days, set under Settings). After a restore, automation leaves that file alone. Restore
refuses if Sonarr/Radarr replaced the file since (it would throw the newer download away), or if
the original was already purged. History shows which case you're in ("in trash until Oct 24",
"purged Oct 2", …).

To keep the trash from growing too large during a big first pass, set a **trash size limit**. The
oldest days are purged early (never today's), and History shows which ones went. You can also keep
originals next to the new file (`.orig`) or delete them right away (no undo).

## What's kept

Everything except the video stream is copied as-is:

- **Every subtitle track**: ASS/SSA with their styling, SRT, PGS/VobSub image subs. Languages,
  titles, and *default*/*forced* flags are kept.
- **Font attachments** (what styled anime subs need) and **chapters**.
- **Audio tracks** are copied by default. Presets can convert them, and automation never downmixes
  surround on its own.
- **Metadata** (title, language tags) carries over. recast adds a `RECAST` tag so it never
  re-encodes its own output.

The one exception is the MP4 container, which can't hold image subtitles or font attachments. Keep
`"container": "mkv"` (the default) for anything with them. The tests build a file with all of the
above and check that it comes out the same.

---

## recast (terminal app)

You need ffmpeg (with ffprobe) and [uv](https://docs.astral.sh/uv/).

**macOS**
```bash
brew install ffmpeg uv
```
**Debian / Ubuntu** (`jellyfin-ffmpeg` instead of `ffmpeg` for Quick Sync, and add yourself to the `render` group)
```bash
sudo apt install ffmpeg && curl -LsSf https://astral.sh/uv/install.sh | sh
```
**Windows (NVIDIA)**: in PowerShell, using Windows Terminal for proper colors
```bash
winget install Gyan.FFmpeg astral-sh.uv
```
then:
```bash
uv tool install git+https://github.com/awpsec/recast
```

Then run `recast`. On first launch it detects your hardware (every encoder gets a 2-second test
encode, so `auto` picks what really works on this machine), suggests the media folders on your
mounted drives and shares, and picks a scratch folder (`~/Documents/recast`). Run detection again
any time from Settings or with `recast --setup`.

**Updating:**
```bash
recast update
```
This installs the newest GitHub release with whatever installed recast (uv, pipx or pip). The app
also mentions it when a new release is out (it checks at most once a day).

What it does:

- **Find your biggest wins.** Pick a library folder. recast reads the headers once (cached), then
  ranks every show by how much space your default preset would free. `p` switches the preset,
  `e` encodes a show.
- **Browse the library** with codec, bitrate, tracks and Sonarr/Radarr info for any file or folder.
  `/` searches every show, season and episode.
- **See what every preset would do** to a file, season or show. Once you've encoded something from a
  show, the numbers are measured instead of estimated.
- **Encode one episode, look at it, then press "Approve + rest of season / whole show".** It uses
  the same settings, asks for one approval for the batch, and skips episodes that are already done.
- **Watch it happen**: fps, speed, size so far vs projected, a bitrate graph, ETA, and the frame
  ffmpeg is working on, drawn in the terminal.
- **Long runs are safe to leave.** If the NAS drops off, jobs wait. `space` pauses everything, the
  computer stays awake, and you get a desktop notification when something needs you.
- **Presets** are JSON with autocomplete as you type. Anything this machine can't run shows red.

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

---

## Presets

Presets support every field in the encode dialog, plus `extra` for raw ffmpeg flags:

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

`"encoder": "auto"` resolves per machine, hardware first. If a machine lacks a pinned encoder, recast
falls back to the best one it has and tells you. `-x265-params` in `extra` is dropped automatically
when the encoder isn't x265.

## Where things live

| | terminal app | server |
|---|---|---|
| Docker | | `/config` (your `CONFIG_DIR`) |
| Linux | `~/.config/recast` | `~/.config/recast-server` |
| macOS | `~/Library/Application Support/recast` | `~/Library/Application Support/recast-server` |
| Windows | `%APPDATA%\recast` | `%APPDATA%\recast-server` |

- `config.json`: library folders, scratch, detected encoders, Sonarr/Radarr, automation settings.
- `presets/*.json`: one file per preset. Edit them in the app or any editor.
- `state.json`: queue, approval inbox, and the history of replaced files (what restore uses).
- `probe-cache.json`: ffprobe results, so headers aren't read from the NAS again.
- `overview.json`: the last listing of each library folder.
- `automation.json`: borderline and skipped decisions, and the activity log (server).

Set `RECAST_HOME` to use a different folder.

## Releases

Bump `__version__` in `src/recast/__init__.py`, then tag:

```bash
git tag v0.3.0 && git push origin v0.3.0
```

The tag runs the tests on Linux and macOS, publishes a GitHub release with the wheel (what
`recast update` installs), and pushes `ghcr.io/awpsec/recast:0.3.0` and `:latest` (what
`compose.yaml` pulls). Every push to `main` also builds `:edge`.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
```
```bash
.venv/bin/python -m pytest
```

The tests build a small library of real clips with ffmpeg (AV1, MPEG-2 with 5.1(side) audio,
H.264, a fansub-style file with ASS + fonts + chapters) and run full encode → approve → replace →
restore cycles against it. `python scripts/demo_server.py` runs the server against a throwaway demo
library, and `scripts/demo_web.py` runs the terminal app in a browser via textual-serve. To build
the image locally, run `docker build -t recast:dev .`.
