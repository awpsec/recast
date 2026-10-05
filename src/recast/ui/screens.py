"""Modal screens: setup, encode dialog, approval, compare, prompts."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Label, Select, Static, Switch

from .. import ffmpeg as ff
from ..config import Config, EncoderCap, Root, default_scratch, is_network_path, machine_summary
from ..encode import (CODEC_LABEL, ENCODERS, SPEEDS, EncodeSettings, already_target, build_command,
                      enc_status, est_bytes, est_fps, quote, resolve_encoder)
from .frames import FrameView, grab_frame, side_by_side
from .style import badge, fdur, fsize

WIN = sys.platform == "win32"


class PathSuggester(Suggester):
    """Filesystem autocomplete for path inputs (→ or Tab-to-accept ghost text)."""

    def __init__(self):
        super().__init__(use_cache=False, case_sensitive=not WIN)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        v = os.path.expanduser(value)
        d, base = os.path.split(v)
        try:
            names = sorted(e.name for e in os.scandir(d or ".") if e.is_dir() and not e.name.startswith("."))
        except OSError:
            return None
        for n in names:
            if n.lower().startswith(base.lower()) if WIN else n.startswith(base):
                return value + os.sep if n == base else value[:len(value) - len(base)] + n
        return None


# ───────────────────────────── setup ─────────────────────────────

class SetupScreen(ModalScreen):
    """Detect this machine's hardware/encoders, then pick library + scratch."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, cfg: Config, first_run: bool):
        super().__init__()
        self.cfg = cfg
        self.first_run = first_run
        self.caps: dict[str, EncoderCap] = {}
        self.lines: list[Text] = []
        self.done = False

    def compose(self) -> ComposeResult:
        root = self.cfg.roots[0] if self.cfg.roots else None
        with Vertical(id="setup-box"):
            yield Static(Text.assemble(("◆ recast", "bold #bb9af7"),
                                       ("   setup · detecting this machine", "bold")))
            with VerticalScroll(id="setup-scroll"):
                yield Static(id="setup-log")
                yield Static("── Library ─────────────────────────────────────", classes="section")
                with Horizontal(classes="row"):
                    yield Label("Library folder", classes="lbl")
                    yield Input(root.path if root else "", placeholder=self._mount_hint(), id="su-path",
                                suggester=PathSuggester())
                with Horizontal(classes="row"):
                    yield Label("Name", classes="lbl")
                    yield Input(root.name if root else "Library", id="su-name")
                    yield Label("Network share", classes="lbl2")
                    yield Switch(root.remote if root else False, id="su-remote")
                yield Static(id="su-path-info", classes="hint")
                with Horizontal(classes="row"):
                    yield Label("Scratch folder", classes="lbl")
                    yield Input(self.cfg.scratch or str(default_scratch()), id="su-scratch",
                                suggester=PathSuggester())
                yield Static(Text("Local disk where files are copied, encoded and held until you approve. "
                                  "More library folders and Sonarr/Radarr live in Settings.", style="dim"),
                             classes="hint")
            with Horizontal(id="setup-btns"):
                yield Button("✓ Save & start", id="su-save", variant="success", disabled=True)
                yield Button("↻ Re-run detection", id="su-rerun")
                yield Button("Quit" if self.first_run else "Cancel", id="su-cancel")

    def _mount_hint(self) -> str:
        if WIN:
            return "e.g. M:\\  or  \\\\nas\\media"
        return "e.g. /Volumes/media" if sys.platform == "darwin" else "e.g. /mnt/nas/media"

    def on_mount(self) -> None:
        self.detect()
        self.query_one("#su-path", Input).focus()

    def log_line(self, t: Text) -> None:
        self.lines.append(t)
        self.query_one("#setup-log", Static).update(Text("\n").join(self.lines))

    @work(thread=True, exclusive=True)
    def detect(self) -> None:
        call = self.app.call_from_thread
        call(self._reset)
        k = lambda s: (f"  {s:<14}", "#7dcfff")
        mach = machine_summary()
        call(self.log_line, Text.assemble(("  ✓", "#9ece6a"), k("Machine"),
                                          f"{mach['host']} · {mach['os']} · {mach['cpu']}"))
        if mach.get("gpu"):
            call(self.log_line, Text.assemble(("   ", ""), k("GPU"), mach["gpu"]))
        ffm, ffp = ff.find_binaries()
        if not ffm or not ffp:
            how = ("winget install Gyan.FFmpeg" if WIN else "brew install ffmpeg" if sys.platform == "darwin"
                   else "sudo apt install ffmpeg   (or jellyfin-ffmpeg for Intel QSV)")
            call(self.log_line, Text.assemble(("  ✗", "#f7768e"), k("ffmpeg"),
                                              ("not found. Install it, then Re-run detection:  ", "#f7768e"),
                                              (how, "bold")))
            return
        ver = ff.version(ffm)
        call(self.log_line, Text.assemble(("  ✓", "#9ece6a"), k("ffmpeg"), f"{ver} · {ffm}"))
        call(self.log_line, Text.assemble(("  ◐", "#e0af68"), k("Encoders"),
                                          ("2 s test encode each (1080p test pattern)…", "dim")))

        def got(name: str, cap: EncoderCap) -> None:
            line = Text(f"      {name:<20}")
            if cap.status == "ok":
                line.append(f"✓ works  {cap.fps:>5.0f} fps @1080p", "#9ece6a")
            elif cap.status == "failed":
                line.append(f"✗ {cap.reason}", "#f7768e")
            else:
                line.append("– not in this ffmpeg build", "dim")
            call(self.log_line, line)

        caps = ff.detect_encoders(ffm, got)
        best = {c: resolve_encoder(EncodeSettings(codec=c), caps)[0] for c in ("hevc", "h264", "av1")}
        summary = Text("\n  Defaults here:  ", style="bold")
        for c, e in best.items():
            summary.append_text(badge(CODEC_LABEL[c]))
            summary.append(f" {e}   ")
        call(self.log_line, summary)
        call(self._finish, ffm, ffp, ver, mach, caps)

    def _reset(self) -> None:
        self.lines = []
        self.done = False
        self.query_one("#su-save", Button).disabled = True

    def _finish(self, ffm, ffp, ver, mach, caps) -> None:
        self.cfg.ffmpeg, self.cfg.ffprobe, self.cfg.ffmpeg_version = ffm, ffp, ver
        self.cfg.machine, self.caps = mach, caps
        self.done = True
        self.query_one("#su-save", Button).disabled = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "su-path":
            p = os.path.expanduser(event.value)
            info = self.query_one("#su-path-info", Static)
            if not p:
                info.update("")
            elif os.path.isdir(p):
                net = is_network_path(p)
                self.query_one("#su-remote", Switch).value = net
                subs = [e.name for e in os.scandir(p) if e.is_dir() and not e.name.startswith(".")][:6]
                info.update(Text(f"✓ found · {'network share → files are copied to scratch first' if net else 'local disk'}"
                                 + (f" · contains {', '.join(subs)}" if subs else ""), style="#9ece6a"))
            else:
                info.update(Text("✗ folder doesn't exist (yet)", style="#f7768e"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "su-rerun":
            self.detect()
        elif bid == "su-cancel":
            self.action_close()
        elif bid == "su-save":
            path = os.path.expanduser(self.query_one("#su-path", Input).value.strip())
            scratch = os.path.expanduser(self.query_one("#su-scratch", Input).value.strip())
            if not os.path.isdir(path):
                self.app.notify("Pick an existing library folder.", severity="error")
                return
            try:
                os.makedirs(scratch, exist_ok=True)
            except OSError as e:
                self.app.notify(f"Can't create scratch folder: {e}", severity="error")
                return
            root = Root(self.query_one("#su-name", Input).value.strip() or "Library", os.path.abspath(path),
                        self.query_one("#su-remote", Switch).value)
            others = [r for r in self.cfg.roots[1:] if r.path != root.path]
            self.cfg.roots = [root, *others]
            self.cfg.scratch = os.path.abspath(scratch)
            self.cfg.encoders = self.caps
            self.cfg.detected_at = time.strftime("%Y-%m-%d %H:%M")
            self.cfg.save()
            self.dismiss(True)

    def action_close(self) -> None:
        if self.first_run:
            self.app.exit()
        else:
            self.dismiss(False)


# ───────────────────────────── encode dialog ─────────────────────────────

def encoder_options(codec: str, caps: dict[str, EncoderCap]) -> list[tuple]:
    if codec == "copy":
        return [("copy", "copy")]
    best, _ = resolve_encoder(EncodeSettings(codec=codec), caps)
    opts: list[tuple] = [(Text(f"auto → {best}"), "auto")]
    for e in ENCODERS[codec]:
        st = enc_status(e, caps)
        c = caps.get(e)
        if st == "ok":
            opts.append((Text.assemble(e, (f"  ✓ {c.fps:.0f} fps" if c and c.fps else "  ✓", "#9ece6a")), e))
        else:
            opts.append((Text(f"{e}  ✗ {'failed probe' if st == 'failed' else 'not available'}", style="#f7768e"), e))
    return opts


class EncodeDialog(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("ctrl+a", "toggle_adv", "Advanced")]

    def __init__(self, title: str, files: list, cfg: Config, presets: dict, preset_name: str,
                 settings: EncodeSettings | None = None, folder: str | None = None,
                 skipped: dict | None = None, note=None, measured: dict | None = None):
        super().__init__()
        self.title_text, self.cfg, self.presets = title, cfg, presets
        self.caps = cfg.encoders
        self.folder = folder
        self.note = note
        self.measured = measured or {}
        self.skipped = dict(skipped or {})
        self.unreadable = self.skipped.pop("__unreadable__", "")
        self.all_files = files
        self.files = [f for f in files if os.path.normcase(os.path.abspath(f.path)) not in self.skipped] or files
        self.preset_name = preset_name if preset_name in presets else next(iter(presets))
        self.initial = settings or presets[self.preset_name][1]
        # the "try 1 file first" candidate: first file that actually needs this encode
        self.sample = next((f for f in self.files if not already_target(self.initial, f, self.caps)), self.files[0])

    def compose(self) -> ComposeResult:
        s = self.initial
        sel = lambda i, opts, val: Select(opts, value=val, allow_blank=False, id=i)
        multi = len(self.files) > 1
        with Vertical(id="enc-box"):
            yield Static(id="enc-title")
            if self.note:
                yield Static(self.note, id="enc-note")
            with VerticalScroll(id="enc-body"):
                with Horizontal(id="preset-row"):
                    yield Label("Preset", classes="lbl")
                    yield sel("f-preset", [("✎ Custom (on the fly)", "__custom__")] + [(k, k) for k in self.presets],
                              self.preset_name)
                    yield Static(id="preset-state")
                yield Static("── Basic ─────────────────────────────", classes="section")
                with Grid(classes="grid4"):
                    yield Label("Video codec", classes="lbl")
                    yield sel("f-codec", [("HEVC / H.265", "hevc"), ("H.264", "h264"), ("AV1", "av1"),
                                          ("Copy (remux only)", "copy")], s.codec)
                    yield Label("Resolution", classes="lbl")
                    yield sel("f-res", [("Same as source", "source"), ("2160p", "2160"), ("1080p", "1080"),
                                        ("720p", "720"), ("480p", "480")], s.resolution)
                    yield Label("Rate control", classes="lbl")
                    yield sel("f-rate", [("Target bitrate", "bitrate"), ("Constant quality", "crf")], s.rate_mode)
                    yield Label("Bitrate kb/s", classes="lbl")
                    yield Input(str(s.bitrate), id="f-bitrate", type="integer")
                    yield Label("Audio", classes="lbl")
                    yield sel("f-audio", [("Copy all tracks", "copy"), ("AAC stereo", "aac_stereo"),
                                          ("Opus (keep channels)", "opus")], s.audio)
                    yield Label("Quality (CRF)", classes="lbl")
                    yield Input(str(s.crf), id="f-crf", type="integer")
                    yield Label("Subtitles", classes="lbl")
                    yield sel("f-subs", [("Copy all", "copy"), ("Forced only", "forced"), ("Drop", "none")], s.subs)
                    yield Label("Container", classes="lbl")
                    yield sel("f-container", [("MKV", "mkv"), ("MP4", "mp4")], s.container)
                    yield Label("After encode", classes="lbl")
                    yield sel("f-after", [("Ask once for the batch" if multi else "Ask me (approval inbox)", "ask"),
                                          ("Auto-replace if checks pass", "auto"), ("Keep both files", "keep")],
                              s.after)
                    yield Label("Skip if done", classes="lbl")
                    with Horizontal(classes="sw"):
                        yield Switch(s.skip_same, id="f-skip")
                        yield Label("skip files already in target codec", classes="hint")
                with Horizontal(id="adv-row"):
                    yield Button("▸ Advanced  ^a", id="adv-btn")
                with Vertical(id="adv"):
                    yield Static("── Advanced ──────────────────────────", classes="section")
                    with Grid(classes="grid4"):
                        yield Label("Encoder", classes="lbl")
                        yield sel("f-encoder", encoder_options(s.codec, self.caps),
                                  "copy" if s.codec == "copy" else s.encoder)
                        yield Label("Speed preset", classes="lbl")
                        yield sel("f-speed", [(p, p) for p in SPEEDS], s.speed)
                        yield Label("Tune", classes="lbl")
                        yield sel("f-tune", [(p, p) for p in ["none", "animation", "grain", "film", "fastdecode"]],
                                  s.tune)
                        yield Label("Bit depth", classes="lbl")
                        yield sel("f-depth", [("10-bit", "10"), ("8-bit", "8")], s.bit_depth)
                        yield Label("Max rate", classes="lbl")
                        yield Input(s.maxrate, placeholder="e.g. 4M", id="f-maxrate")
                        yield Label("Buffer size", classes="lbl")
                        yield Input(s.bufsize, placeholder="e.g. 8M", id="f-bufsize")
                        yield Label("Two-pass", classes="lbl")
                        yield Switch(s.two_pass, id="f-twopass")
                        yield Label("HDR passthru", classes="lbl")
                        yield Switch(s.hdr, id="f-hdr")
                        yield Label("Deinterlace", classes="lbl")
                        yield Switch(s.deinterlace, id="f-deint")
                        yield Label("Audio langs", classes="lbl")
                        yield Input(s.langs, placeholder="jpn,eng (blank = all)", id="f-langs")
                    with Horizontal(id="extra-row"):
                        yield Label("Extra ffmpeg args", classes="lbl")
                        yield Input(s.extra, placeholder="-x265-params aq-mode=3  (anything goes)", id="f-extra")
                yield Static("── ffmpeg command (live) ─────────────", classes="section")
                yield Static(id="cmd")
                yield Static(id="est")
            with Horizontal(id="enc-buttons"):
                if len(self.all_files) > 1:
                    yield Button("▶ Try 1 file first", id="go-preview", variant="primary")
                    yield Button(f"⏵⏵ Encode all {len(self.files)}", id="go-all", variant="warning")
                else:
                    yield Button("▶ Encode", id="go-preview", variant="primary")
                yield Button("Save as preset…", id="save-preset")
                yield Button("Cancel", id="cancel", variant="error")

    def on_mount(self) -> None:
        s = self.initial
        self.set_adv(bool(s.extra or s.encoder != "auto" or s.tune != "none" or s.maxrate))
        self.query_one("#enc-title", Static).update(self.title_text)
        self.update_preview()
        self.call_after_refresh(setattr, self, "_ready", True)

    def read(self) -> EncodeSettings:
        q = lambda i: self.query_one(f"#{i}")
        num = lambda i, d: int(q(i).value) if q(i).value.strip().lstrip("-").isdigit() else d
        return EncodeSettings(
            codec=q("f-codec").value, resolution=q("f-res").value, rate_mode=q("f-rate").value,
            bitrate=num("f-bitrate", 2000), crf=num("f-crf", 22), audio=q("f-audio").value, subs=q("f-subs").value,
            container=q("f-container").value, after=q("f-after").value, skip_same=q("f-skip").value,
            encoder=q("f-encoder").value, speed=q("f-speed").value, tune=q("f-tune").value,
            bit_depth=q("f-depth").value, maxrate=q("f-maxrate").value.strip(), bufsize=q("f-bufsize").value.strip(),
            two_pass=q("f-twopass").value, hdr=q("f-hdr").value, deinterlace=q("f-deint").value,
            langs=q("f-langs").value.strip(), extra=q("f-extra").value.strip())

    def load(self, s: EncodeSettings) -> None:
        q = lambda i: self.query_one(f"#{i}")
        for i, v in [("f-codec", s.codec), ("f-res", s.resolution), ("f-rate", s.rate_mode), ("f-audio", s.audio),
                     ("f-subs", s.subs), ("f-container", s.container), ("f-after", s.after), ("f-speed", s.speed),
                     ("f-tune", s.tune), ("f-depth", s.bit_depth)]:
            q(i).value = v
        enc = q("f-encoder")
        enc.set_options(encoder_options(s.codec, self.caps))
        enc.value = "copy" if s.codec == "copy" else s.encoder
        for i, v in [("f-bitrate", str(s.bitrate)), ("f-crf", str(s.crf)), ("f-maxrate", s.maxrate),
                     ("f-bufsize", s.bufsize), ("f-langs", s.langs), ("f-extra", s.extra)]:
            q(i).value = v
        for i, v in [("f-skip", s.skip_same), ("f-twopass", s.two_pass), ("f-hdr", s.hdr), ("f-deint", s.deinterlace)]:
            q(i).value = v
        if s.extra or s.encoder != "auto" or s.tune != "none" or s.maxrate:
            self.set_adv(True)

    def set_adv(self, on: bool) -> None:
        self.query_one("#adv").display = on
        self.query_one("#adv-btn", Button).label = "▾ Advanced  ^a" if on else "▸ Advanced  ^a"

    def action_toggle_adv(self) -> None:
        self.set_adv(not self.query_one("#adv").display)

    def update_preview(self) -> None:
        try:
            s = self.read()
        except Exception:
            return
        self.query_one("#f-bitrate").disabled = s.rate_mode != "bitrate" or s.codec == "copy"
        self.query_one("#f-crf").disabled = s.rate_mode != "crf" or s.codec == "copy"
        name = self.query_one("#f-preset").value
        st = self.query_one("#preset-state", Static)
        if name == "__custom__":
            st.update(Text("on-the-fly settings", style="#e0af68"))
        elif not settings_match(self.presets, name, s):
            st.update(Text("● modified from preset", style="#e0af68"))
        else:
            st.update(Text(self.presets[name][0], style="dim"))
        self.sample = next((f for f in self.files if not already_target(s, f, self.caps)), self.sample)
        m = self.sample
        src = os.path.join(self.cfg.scratch, "in", os.path.basename(m.path)) if self._remote else m.path
        out = os.path.join(self.cfg.scratch, "out", os.path.splitext(os.path.basename(m.path))[0] + "." + s.container)
        groups = build_command(s, m, src, out, self.caps, ffmpeg="ffmpeg")
        t = Text()
        hidden = [g for g in groups if g and all(x.startswith("-metadata:s:") or x.endswith("=") for x in g)]
        groups = [g for g in groups if g not in hidden]
        for i, grp in enumerate(groups):
            t.append("  " if i else "")
            for j, tok in enumerate(grp):
                t.append(" " if j else "")
                q = quote(tok, WIN)
                style = ("bold #ff9e64" if tok == "ffmpeg" else "#7dcfff" if tok.startswith("-") and not tok[1:2].isdigit()
                         else "#9ece6a" if ("/" in tok or "\\" in tok) else "#c0caf5")
                t.append(q, style)
            if i < len(groups) - 1:
                t.append(" ^\n" if WIN else " \\\n", "dim")
        if hidden:
            n = sum(len(g) for g in hidden) // 2
            t.append(f"\n  # + {n} -metadata flags that clear stale bitrate tags from the source (not shown)", "dim")
        self.query_one("#cmd", Static).update(t)
        per = est_bytes(s, m, self.caps)
        enc, note = resolve_encoder(s, self.caps)
        fps = est_fps(s, m, self.caps)
        e = Text()
        e.append("Estimate  ", "bold")
        e.append(f"{fsize(m.size)} → ≈{fsize(per)} ")
        e.append(f"({(per / max(1, m.size) - 1) * 100:+.0f}%)", "bold #9ece6a" if per < m.size else "bold #f7768e")
        e.append(f" for {os.path.basename(m.path) if len(self.all_files) > 1 else 'this file'}\n          ")
        e.append(enc, "bold #7dcfff")
        e.append(f" on {self.cfg.machine.get('host', 'this machine')}" + (" (auto)" if note == "auto" else ""))
        e.append(f" · ~{fps:.0f} fps · ≈{fdur(m.frames / fps if m.frames else m.duration)} per file")
        if note and note != "auto":
            e.append(f"\n          ⚠ {note}", "#e0af68")
        if m.interlaced and not s.deinterlace and s.codec != "copy":
            e.append("\n          ⚠ source is interlaced — turn on Deinterlace (Advanced) or use “DVD rescue”",
                     "#e0af68")
        if m.hdr == "Dolby Vision" and s.codec != "copy":
            e.append("\n          ⚠ Dolby Vision layer is dropped; the HDR10 base layer is kept", "#e0af68")
        same = name != "__custom__" and settings_match(self.presets, name, s)
        meas = self.measured.get(name) if same else None
        if meas:
            e.append(f"\n          measured on this show: {(meas[0] - 1) * 100:+.0f}% over {meas[1]} file"
                     f"{'s' * (meas[1] != 1)}", "#9ece6a")
        multi = len(self.all_files) > 1
        if multi:
            todo = [f for f in self.files if not already_target(s, f, self.caps)]
            src_b = sum(f.size for f in todo)
            out_b = src_b * meas[0] if meas else sum(est_bytes(s, f, self.caps) for f in todo)
            secs = sum((f.frames or f.duration * 24) / est_fps(s, f, self.caps) for f in todo)
            e.append(f"\n          {len(todo)} file{'s' * (len(todo) != 1)} to encode · {fsize(src_b)} → "
                     f"≈{fsize(out_b)} · saves ≈{fsize(src_b - out_b)} · ≈{fdur(secs)} total", "#7dcfff")
            why = []
            n_target = len(self.files) - len(todo)
            if n_target:
                why.append(f"{n_target} already {CODEC_LABEL.get(s.codec, s.codec)}")
            reasons: dict[str, int] = {}
            for r in self.skipped.values():
                reasons[r] = reasons.get(r, 0) + 1
            why += [f"{n} {r}" for r, n in reasons.items()]
            if self.unreadable:
                why.append(self.unreadable)
            if why:
                e.append("\n          skipping: " + " · ".join(why), "dim")
            try:
                btn = self.query_one("#go-all", Button)
                btn.label = f"⏵⏵ Encode {len(todo)} file{'s' * (len(todo) != 1)}"
                btn.disabled = not todo
            except Exception:
                pass
        self.query_one("#est", Static).update(e)

    @property
    def _remote(self) -> bool:
        return any(r.remote and self.sample.path.startswith(r.path) for r in self.cfg.roots)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "f-codec":
            enc = self.query_one("#f-encoder", Select)
            cur = enc.value
            enc.set_options(encoder_options(event.value, self.caps))
            valid = ["copy"] if event.value == "copy" else ["auto", *ENCODERS[event.value]]
            enc.value = cur if cur in valid else valid[0]
        if event.select.id == "f-preset" and event.value != "__custom__" and getattr(self, "_ready", False):
            self.load(self.presets[event.value][1])  # only when the user picks one, never during mount
        self.update_preview()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.update_preview()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.update_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        preset = self.query_one("#f-preset").value
        if bid == "adv-btn":
            self.action_toggle_adv()
        elif bid == "cancel":
            self.dismiss(None)
        elif bid in ("go-preview", "go-all"):
            s = self.read()
            label = preset if preset != "__custom__" else "custom"
            if preset != "__custom__" and not settings_match(self.presets, preset, s):
                label += "*"
            files = [self.sample] if bid == "go-preview" else self.files
            self.dismiss(dict(files=files, s=s, preset=label, preview=bid == "go-preview",
                              folder=self.folder if bid == "go-all" else None))
        elif bid == "save-preset":
            def saved(name):
                if name:
                    self.app.save_preset_from_dialog(name, self.read())
                    self.presets = self.app.presets
                    sel = self.query_one("#f-preset", Select)
                    sel.set_options([("✎ Custom (on the fly)", "__custom__")] + [(k, k) for k in self.presets])
                    sel.value = name
            self.app.push_screen(NamePrompt("Preset name", "e.g. Black Clover HEVC CRF 21"), saved)


def settings_match(presets: dict, name: str, s: EncodeSettings) -> bool:
    return name in presets and asdict(presets[name][1]) == asdict(s)


class NamePrompt(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, label: str, placeholder: str = "", value: str = "", suggest_paths: bool = False):
        super().__init__()
        self.label, self.placeholder, self.value = label, placeholder, value
        self.suggest_paths = suggest_paths

    def compose(self) -> ComposeResult:
        with Vertical(id="name-box"):
            yield Label(self.label)
            yield Input(self.value, placeholder=self.placeholder, id="name-in",
                        suggester=PathSuggester() if self.suggest_paths else None)
            with Horizontal():
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="no")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss((self.query_one(Input).value.strip() or None) if event.button.id == "ok" else None)


class ConfirmScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(False)", "No"), Binding("y", "dismiss(True)", "Yes")]

    def __init__(self, message: str, yes: str = "Yes", no: str = "Cancel"):
        super().__init__()
        self.message, self.yes, self.no = message, yes, no

    def compose(self) -> ComposeResult:
        with Vertical(id="name-box"):
            yield Static(self.message)
            with Horizontal():
                yield Button(self.yes, variant="error", id="yes")
                yield Button(self.no, id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ───────────────────────────── approval ─────────────────────────────

def compare_table(j, caps) -> Table:
    m, s = j.media, j.s
    o = j.out_info or {}
    t = Table(box=None, padding=(0, 2), header_style="bold")
    for c in ("", "Source", "Output", ""):
        t.add_column(c)
    ok = Text("✓", style="bold #9ece6a")
    warn = Text("⚑", style="bold #e0af68")
    enc = next((l.split("-c:v ", 1)[1].split()[0] for l in j.log if "-c:v " in l), resolve_encoder(s, caps)[0])
    t.add_row("Codec", Text.assemble(badge(m.codec), f" {m.profile}"),
              Text.assemble(badge(o.get("codec", "?")), f" {enc}"), "")
    t.add_row("Resolution", f"{m.width}×{m.height}", f"{o.get('width', 0)}×{o.get('height', 0)}", "")
    out_v = o.get("vkbps") or 0
    t.add_row("Video bitrate", f"{m.vkbps:,} kb/s", f"{out_v:,} kb/s", "")
    saved = 1 - j.out_size / max(1, m.size)
    t.add_row("Size", fsize(m.size), Text(f"{fsize(j.out_size)}  ({-saved * 100:+.0f}%)",
                                          style="bold #9ece6a" if saved > 0 else "bold #f7768e"), "")
    dd = abs(o.get("duration", 0) - m.duration)
    t.add_row("Duration", fdur(m.duration), fdur(o.get("duration", 0)), ok if dd <= max(1, m.duration * .005) else warn)
    t.add_row("Audio", f"{len(m.audio)} tracks", f"{len(o.get('audio', []))} tracks "
              f"({', '.join(a.get('codec', '?') for a in o.get('audio', []))})", "")
    t.add_row("Subtitles", f"{len(m.subs)} tracks", f"{len(o.get('subs', []))} tracks", "")
    t.add_row("Encode time", "", fdur(j.finished - j.started) if j.finished and j.started else "—", "")
    if j.flag:
        t.add_row(Text("Flag", style="bold #e0af68"), "", Text(j.flag, style="#e0af68"), warn)
    else:
        t.add_row("Checks", "", "duration · streams · decode test", ok)
    return t


class ApprovalPrompt(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss('later')", "Later"), Binding("y", "dismiss('approve')", "Approve"),
                Binding("n", "dismiss('deny')", "Deny"), Binding("r", "dismiss('retry')", "Retry"),
                Binding("c", "dismiss('compare')", "Compare")]

    def __init__(self, job, caps, rest: list | None = None):
        super().__init__()
        self.job, self.caps = job, caps
        self.rest = rest or []  # [(label, folder path)]: approve this one, then encode those with the same settings

    def compose(self) -> ComposeResult:
        j = self.job
        with Vertical(id="appr-box"):
            yield Static(Text.assemble(("⚑ Encode finished — replace in library?\n", "bold #bb9af7"),
                                       (os.path.basename(j.src), "bold"), ("\n" + j.src, "dim")))
            yield Static(compare_table(j, self.caps))
            yield Static(Text("Closing this keeps it in the Approvals inbox. Nothing times out.", style="dim"))
            with Horizontal(id="appr-box-btns"):
                yield Button("✓ Approve & replace  y", id="approve", variant="success")
                yield Button("✗ Deny  n", id="deny", variant="error")
                yield Button("↻ Retry…  r", id="retry", variant="warning")
                yield Button("◧ Compare  c", id="compare")
                yield Button("Later  esc", id="later")
            if self.rest and not j.flag:
                yield Static(Text("Happy with it? Approve this one and queue the rest with the same settings "
                                  "(one approval for the batch):", style="dim"), id="appr-rest-hint")
                with Horizontal(id="appr-rest-btns"):
                    for i, (label, _path) in enumerate(self.rest):
                        yield Button(f"✓ Approve + {label}  {'as'[i]}", id=f"rest{i}", variant="success")

    def on_key(self, event) -> None:
        if event.key in ("a", "s") and not self.job.flag:
            i = "as".index(event.key)
            if i < len(self.rest):
                event.stop()
                self.dismiss("rest:" + self.rest[i][1])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("rest"):
            self.dismiss("rest:" + self.rest[int(bid[4:])][1])
        else:
            self.dismiss(bid)


class CompareScreen(ModalScreen):
    """Same timestamps from source and output. space flips A/B, s toggles split, ←/→ moves."""

    BINDINGS = [Binding("escape,q", "dismiss(None)", "Close"), Binding("left", "move(-1)", "Earlier"),
                Binding("right", "move(1)", "Later"), Binding("space", "flip", "Flip A/B"),
                Binding("s", "split", "Split")]
    POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)

    def __init__(self, job, ffmpeg: str):
        super().__init__()
        self.job, self.ffmpeg = job, ffmpeg
        self.i, self.show_out, self.split = 2, False, True
        self.cache: dict[int, tuple] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="cmp-box"):
            yield Static(id="cmp-cap")
            yield FrameView("grabbing frames…", id="cmp-frame")
            yield Static(Text("←/→ position · space flip source/encoded · s split view · esc close", style="dim"))

    def on_mount(self) -> None:
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        i = self.i
        if i not in self.cache:
            t = self.job.media.duration * self.POINTS[i]
            a = grab_frame(self.ffmpeg, self.job.src if os.path.exists(self.job.src) else self.job.work_src, t)
            b = grab_frame(self.ffmpeg, self.job.out, t)
            self.cache[i] = (t, a, b)
        self.app.call_from_thread(self.show)

    def show(self) -> None:
        if self.i not in self.cache:
            return
        t, a, b = self.cache[self.i]
        cap = Text.assemble((f"{fdur(t)}", "bold"), f"  ({self.i + 1}/{len(self.POINTS)})   ")
        if self.split:
            img = side_by_side(a, b)
            cap.append("◀ source │ encoded ▶", "#e0af68")
        else:
            img = b if self.show_out else a
            cap.append("ENCODED" if self.show_out else "SOURCE", "bold #9ece6a" if self.show_out else "bold #7dcfff")
        self.query_one("#cmp-cap", Static).update(cap)
        self.query_one("#cmp-frame", FrameView).set_image(img, "couldn't decode a frame here")

    def action_move(self, d: int) -> None:
        self.i = max(0, min(len(self.POINTS) - 1, self.i + d))
        self.query_one("#cmp-frame", FrameView).set_image(None, "grabbing frames…")
        self.load()

    def action_flip(self) -> None:
        self.split, self.show_out = False, not self.show_out
        self.show()

    def action_split(self) -> None:
        self.split = not self.split
        self.show()


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        t = Table(box=None, padding=(0, 2), show_header=False)
        t.add_column(style="bold #7dcfff")
        t.add_column()
        for k, d in [("1-5", "switch tabs (or click them)"), ("↑ ↓ ← → / mouse", "browse the library"),
                     ("e", "encode the highlighted file, or a folder (try 1 file first, or all)"),
                     ("/", "find a show / season / file in the library"),
                     ("u", "restore the original of a file recast replaced (while it's in the trash)"), ("f", "toggle frame preview"),
                     ("[  ]", "frame preview refresh rate"), ("space", "pause / resume everything"),
                     ("x / r / del", "cancel · retry failed · clear finished (Queue)"),
                     ("y / n / r", "approve / deny / retry in Approvals"),
                     ("a / s", "in the approval prompt: approve + rest of season / whole show"),
                     ("c", "compare frames (source vs encoded)"), ("ctrl+a", "advanced settings (encode dialog)"),
                     ("tab / ↑↓ / ctrl+space", "accept / choose / open suggestions in the preset editor"),
                     ("ctrl+s", "save preset (in the editor)"), ("ctrl+p", "command palette / themes"),
                     ("q", "quit")]:
            t.add_row(k, d)
        with Vertical(id="help-box"):
            yield Static(Text("recast — keys", style="bold"))
            yield Static(t)
