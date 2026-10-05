"""The recast TUI."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.worker import get_current_worker
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, DataTable, Footer, Input, Label, ListItem, ListView, ProgressBar, Select,
                             Sparkline, Static, Switch, TabbedContent, TabPane, Tree)

from ..arr import ArrClient, ArrError, ArrIndex, auto_map
from ..config import Config, Root, is_network_path
from ..encode import (CODEC_LABEL, EncodeSettings, already_target, delete_preset, est_bytes, load_presets, skip_reason,
                      preset_doc, resolve_encoder, save_preset)
from ..engine import ACTIVE, LIVE, Batch, Engine, Job, norm, show_root
from ..probe import VIDEO_EXT, MediaInfo, ProbeCache
from .editor import PresetEditor
from .frames import FrameView, load_image
from .screens import (ApprovalPrompt, CompareScreen, ConfirmScreen, EncodeDialog, HelpScreen, NamePrompt,
                      SetupScreen, compare_table)
from .style import CODEC_STYLE, badge, fdur, fsize, pct_bar

CADENCES = [("live", 0.25), ("0.5s", 0.5), ("1s", 1.0), ("5s", 5.0)]
STAGE = {
    "queued": ("◌ queued", "dim"), "copying": ("⇣ copying", "#7dcfff"), "ready": ("◌ in scratch", "#7dcfff"),
    "encoding": ("● encoding", "bold #e0af68"), "paused": ("❚❚ paused", "#e0af68"),
    "verifying": ("◎ verifying", "#7aa2f7"), "awaiting": ("⚑ awaiting approval", "bold #bb9af7"),
    "to_replace": ("⇡ queued for replace", "#7dcfff"), "replacing": ("⇡ replacing", "#7dcfff"),
    "replaced": ("✓ replaced", "#9ece6a"), "kept": ("✓ kept both", "#9ece6a"), "discarded": ("✗ discarded", "dim"),
    "cancelled": ("✗ cancelled", "dim"), "skipped": ("↷ skipped", "dim"), "failed": ("✗ failed", "bold #f7768e"),
}


@dataclass
class Node:
    kind: str  # dir | file
    path: str
    root: Root
    loaded: bool = False
    size: int = 0


class JobItem(ListItem):
    def __init__(self, job: Job):
        self.job = job
        m = job.info
        lbl = Text.assemble(("⚑ " if job.flag else "● ", "#e0af68" if job.flag else "#bb9af7"),
                            (job.name[:40], "bold"), "\n  ", badge(m.get("codec", "?")), " → ",
                            badge((job.out_info or {}).get("codec", CODEC_LABEL.get(job.s.codec, "?"))),
                            (f"  {_ago(job.waiting_since)}", "dim"))
        super().__init__(Label(lbl))


class BatchItem(ListItem):
    def __init__(self, batch: Batch, engine: Engine):
        self.batch, self.engine = batch, engine
        self.lbl = Label(self.text())
        super().__init__(self.lbl)

    def text(self) -> Text:
        b, e = self.batch, self.engine
        todo = [j for j in e.batch_jobs(b) if j.stage not in ("skipped", "cancelled", "discarded")]
        done = len(e.batch_jobs(b, "awaiting", "to_replace", "replacing", "replaced"))
        flagged = sum(1 for j in e.batch_jobs(b, "awaiting") if j.flag)
        active = bool(e.batch_jobs(b, *ACTIVE))
        t = Text.assemble(("▤ ", "#bb9af7"), (_short(b.folder)[:40], "bold"), "\n  ")
        t.append(f"{done}/{len(todo)} ", "#e0af68" if active else "#9ece6a")
        t.append(pct_bar(done / max(1, len(todo)), 8), "#e0af68" if active else "#9ece6a")
        if flagged:
            t.append(f" ⚑{flagged}", "#e0af68")
        t.append(f"  {_ago(b.waiting_since)}", "dim")
        return t


class PresetItem(ListItem):
    def __init__(self, name: str, default: bool):
        self.preset_name = name
        super().__init__(Label(Text.assemble(("★ " if default else "  ", "#e0af68"), name)))


def _ago(ts: float) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d / 60)} min ago"
    if d < 86400:
        return time.strftime("since %H:%M", time.localtime(ts))
    return time.strftime("since %a %H:%M", time.localtime(ts))


def _short(path: str) -> str:
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return " / ".join(parts[-2:])


class RecastApp(App):
    TITLE = "recast"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("e", "encode", "Encode…"),
        Binding("slash", "jump", "Find"),
        Binding("f", "toggle_frame", "Frame preview"),
        Binding("space", "pause", "Pause"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit_app", "Quit"),
        Binding("left_square_bracket", "cadence(-1)", "Slower", show=False),
        Binding("right_square_bracket", "cadence(1)", "Faster", show=False),
        Binding("x", "cancel_job", "Cancel job", show=False),
        Binding("y", "approve", "Approve", show=False),
        Binding("n", "deny", "Deny", show=False),
        Binding("r", "retry", "Retry", show=False),
        Binding("c", "compare", "Compare", show=False),
        Binding("ctrl+s", "save_preset", "Save preset", show=False),
        Binding("delete", "clear_finished", "Clear finished", show=False),
        Binding("u", "restore", "Restore original", show=False),
        *[Binding(str(i + 1), f"tab('{t}')", show=False) for i, t in
          enumerate(["tab-library", "tab-queue", "tab-approvals", "tab-presets", "tab-settings"])],
    ]

    def __init__(self):
        super().__init__()
        self.cfg = Config.load()
        self.presets = load_presets()
        self.engine = Engine(self.cfg, self.on_engine_event)
        self.probes = ProbeCache(self.cfg.ffprobe)
        self.arr: ArrIndex | None = None
        self.selected_job: Job | None = None
        self.frame_on = True
        self.cadence = next((i for i, (_, s) in enumerate(CADENCES) if s == self.cfg.preview_cadence), 2)
        self._frame_timer = None
        self._rows: dict[int, tuple] = {}
        self._inbox_sig: tuple = ()
        self._copy_rate: dict[int, tuple[float, int, float]] = {}
        self._tree_status: dict[str, str] = {}
        self._ticks = 0

    # ── layout ──
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with TabbedContent(initial="tab-library", id="tabs"):
            with TabPane("① Library", id="tab-library"):
                with Horizontal():
                    yield Tree("library", id="lib-tree", classes="pane")
                    with Vertical(id="lib-right"):
                        with VerticalScroll(id="details-scroll", classes="pane"):
                            yield Static(id="details")
                        with Horizontal(id="lib-actions"):
                            yield Button("▶ Encode…  e", id="btn-encode", variant="primary")
                            yield Button("↻ Refresh", id="btn-refresh")
            with TabPane("② Queue", id="tab-queue"):
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True, classes="pane")
                with Horizontal(id="queue-bottom"):
                    with Vertical(id="job-detail", classes="pane"):
                        yield Static(id="jd-title")
                        yield Static(id="jd-pipeline")
                        with Horizontal(classes="pbrow"):
                            yield Label("Copy", classes="pblbl")
                            yield ProgressBar(total=100, show_eta=False, id="pb-copy")
                        yield Static(id="jd-copyinfo", classes="pbinfo")
                        with Horizontal(classes="pbrow"):
                            yield Label("Encode", classes="pblbl")
                            yield ProgressBar(total=100, show_eta=False, id="pb-enc")
                        yield Static(id="jd-encinfo", classes="pbinfo")
                        yield Static(id="jd-stats")
                        yield Static("bitrate over time", id="jd-spark-lbl")
                        yield Sparkline([0], id="jd-spark")
                        yield Static(id="jd-log")
                    with Vertical(id="frame-panel", classes="pane"):
                        yield Static(id="frame-caption")
                        yield FrameView("no active encode — pick a file or folder in Library and press e", id="frame")
            with TabPane("③ Approvals", id="tab-approvals"):
                with Horizontal():
                    yield ListView(id="appr-list", classes="pane")
                    with Vertical(id="appr-right"):
                        with VerticalScroll(id="appr-scroll", classes="pane"):
                            yield Static(id="appr-detail")
                        with Horizontal(id="appr-actions"):
                            yield Button("✓ Approve  y", id="a-approve", variant="success")
                            yield Button("✗ Deny  n", id="a-deny", variant="error")
                            yield Button("↻ Retry…  r", id="a-retry", variant="warning")
                            yield Button("◧ Compare  c", id="a-compare")
                            yield Button("✓ + rest…", id="a-rest", variant="success")
                            yield Button("Approve all passing", id="a-all")
            with TabPane("④ Presets", id="tab-presets"):
                with Horizontal():
                    yield ListView(id="preset-list", classes="pane")
                    with Vertical(id="preset-right"):
                        yield Static(id="preset-help")
                        yield PresetEditor(self.cfg.encoders, id="preset-editor", classes="pane")
                        yield Static(id="preset-diag")
                        with Horizontal(id="preset-actions"):
                            yield Button("Save  ^s", id="p-save", variant="primary")
                            yield Button("New", id="p-new")
                            yield Button("Duplicate", id="p-dup")
                            yield Button("★ Set default", id="p-default")
                            yield Button("Delete", id="p-del", variant="error")
            with TabPane("⑤ Settings", id="tab-settings"):
                with VerticalScroll(id="settings-scroll"):
                    yield Static("This machine", classes="set-section")
                    yield Static(id="s-machine")
                    with Horizontal(classes="set-row"):
                        yield Button("↻ Re-run detection", id="s-detect", variant="primary")
                    yield Static("Library folders", classes="set-section")
                    yield DataTable(id="roots", cursor_type="row")
                    with Horizontal(classes="set-row"):
                        yield Button("+ Add folder", id="s-addroot")
                        yield Button("Toggle network share", id="s-toggleremote")
                        yield Button("Remove", id="s-delroot", variant="error")
                    yield Static("Scratch", classes="set-section")
                    with Horizontal(classes="set-row"):
                        yield Label("Scratch folder")
                        yield Input(self.cfg.scratch, id="s-scratch")
                    with Horizontal(classes="set-row"):
                        yield Label("Max scratch usage (GiB)")
                        yield Input(str(self.cfg.max_scratch_gb), id="s-maxscratch", type="integer")
                    with Horizontal(classes="set-row"):
                        yield Label("Prefetch next file")
                        yield Switch(self.cfg.prefetch, id="s-prefetch")
                        yield Label("copy 1 file ahead while encoding (never more)", classes="hint")
                    yield Static("Replacing", classes="set-section")
                    with Horizontal(classes="set-row"):
                        yield Label("Originals on replace")
                        yield Select([("Move to .recast-trash/ (undo with u until purged)", "trash"),
                                      ("Keep alongside as .orig (undo with u)", "keep"),
                                      ("Delete immediately (no undo!)", "delete")], value=self.cfg.originals,
                                     allow_blank=False, id="s-originals")
                    with Horizontal(classes="set-row"):
                        yield Label("Keep trash for (days)")
                        yield Input(str(self.cfg.trash_days), id="s-trashdays", type="integer")
                    with Horizontal(classes="set-row"):
                        yield Label("Update codec in filename")
                        yield Switch(self.cfg.rename_codec, id="s-rename")
                        yield Label("“…1080p AV1.mkv” becomes “…1080p HEVC.mkv” (never overwrites another file)",
                                    classes="hint")
                    with Horizontal(classes="set-row"):
                        yield Label("Desktop notifications")
                        yield Switch(self.cfg.desktop_notify, id="s-notify")
                        yield Label("when a batch finishes or something needs your approval", classes="hint")
                    with Horizontal(classes="set-row"):
                        yield Label("Keep computer awake")
                        yield Switch(self.cfg.keep_awake, id="s-awake")
                        yield Label("block idle sleep while copying / encoding / replacing", classes="hint")
                    with Horizontal(classes="set-row"):
                        yield Label("Decode test")
                        yield Switch(self.cfg.verify_decode, id="s-verify")
                        yield Label("decode first/last 20 s of every output before it can replace", classes="hint")
                    yield Static("Sonarr / Radarr (optional)", classes="set-section")
                    for name in ("sonarr", "radarr"):
                        a = getattr(self.cfg, name)
                        with Horizontal(classes="set-row"):
                            yield Label(f"{name.title()} URL / API key")
                            yield Input(a.url, placeholder=f"http://nas.local:{8989 if name == 'sonarr' else 7878}",
                                        id=f"s-{name}-url")
                            yield Input(a.api_key, password=True, placeholder="API key (Settings → General)",
                                        id=f"s-{name}-key")
                            yield Button("Test + map paths", id=f"s-{name}-test")
                        yield Static(id=f"s-{name}-map", classes="hint")
                    with Horizontal(classes="set-row"):
                        yield Label("After replace")
                        yield Switch(self.cfg.rescan_after_replace, id="s-rescan")
                        yield Label("ask Sonarr/Radarr to rescan the series/movie", classes="hint")
                    yield Static("Encoding", classes="set-section")
                    with Horizontal(classes="set-row"):
                        yield Label("Default preset")
                        yield Select([(k, k) for k in self.presets], value=self.cfg.default_preset
                                     if self.cfg.default_preset in self.presets else next(iter(self.presets)),
                                     allow_blank=False, id="s-default")
                    yield Static(id="s-paths", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = self.cfg.theme if self.cfg.theme in self.available_themes else "tokyo-night"
        self.query_one("#lib-tree").border_title = "Library"
        self.query_one("#details-scroll").border_title = "Details"
        jobs = self.query_one("#jobs", DataTable)
        jobs.border_title = "Jobs"
        for label, key, w in [("#", "id", 4), ("File", "file", 40), ("Preset", "preset", 24), ("Stage", "stage", 21),
                              ("Progress", "prog", 16), ("FPS", "fps", 6), ("Size", "size", 22), ("ETA", "eta", 9)]:
            jobs.add_column(label, key=key, width=w)
        self.query_one("#job-detail").border_title = "Job"
        self.query_one("#job-detail").border_subtitle = "space pause all · x cancel · r retry · del clear finished"
        self.query_one("#frame-panel").border_title = "Frame preview  f"
        self.query_one("#appr-list").border_title = "Awaiting approval"
        self.query_one("#appr-scroll").border_title = "Review"
        self.query_one("#preset-list").border_title = "Presets"
        self.query_one("#preset-editor").border_title = "preset.json"
        roots = self.query_one("#roots", DataTable)
        for c in ("Name", "Path", "Access"):
            roots.add_column(c)
        self.set_interval(0.25, self.tick)
        self.set_interval(10, self.probes.save)
        self.set_interval(15, self._save_cfg_if_moved)
        self.set_frame_timer()
        if self.cfg.needs_setup:
            self.push_screen(SetupScreen(self.cfg, first_run=True), self.after_setup)
        else:
            self.start()

    def _save_cfg_if_moved(self) -> None:
        if self.cfg.last_path != getattr(self, "_saved_last_path", None):
            self._saved_last_path = self.cfg.last_path
            self.cfg.save()

    def after_setup(self, ok: bool | None) -> None:
        if ok:
            self.notify(f"Ready. HEVC here uses {resolve_encoder(EncodeSettings(), self.cfg.encoders)[0]}.",
                        title="✓ Setup saved")
            self.start()

    def start(self) -> None:
        self.probes = ProbeCache(self.cfg.ffprobe)
        self.query_one("#preset-editor", PresetEditor).caps = self.cfg.encoders
        self.build_tree()
        if self.cfg.last_path and os.path.exists(self.cfg.last_path):
            self.run_worker(self.reveal(self.cfg.last_path), group="reveal")
        self.refresh_settings()
        self.refresh_presets()
        self.refresh_inbox(force=True)
        self.connect_arr()
        self.housekeeping()
        n = sum(1 for j in self.engine.jobs.values() if j.stage == "queued")
        if n:
            self.notify(f"Resuming {n} queued job{'s' * (n != 1)} from last time.", title="Queue")

    @work(thread=True)
    def housekeeping(self) -> None:
        freed = self.engine.clean_scratch()
        if freed > 1024**2:
            self.call_from_thread(self.notify, f"Cleaned {fsize(freed)} of leftovers from scratch.", title="Scratch")
        removed = self.engine.purge_trash()
        if removed:
            self.call_from_thread(self.notify, f"Purged {len(removed)} old trash folder(s).", title="Trash")

    def connect_arr(self) -> None:
        if self.cfg.sonarr.enabled or self.cfg.radarr.enabled:
            self.arr = ArrIndex(self.cfg.sonarr, self.cfg.radarr)
            self.engine.arr = self.arr
            self.refresh_arr()
        else:
            self.arr = self.engine.arr = None

    @work(thread=True, group="arr", exclusive=True)
    def refresh_arr(self) -> None:
        self.arr.refresh()
        for kind, err in self.arr.errors.items():
            self.call_from_thread(self.notify, err, title=f"{kind} unavailable", severity="warning")

    # ── library tree ──
    def build_tree(self) -> None:
        tree = self.query_one("#lib-tree", Tree)
        tree.clear()
        tree.show_root = False
        tree.guide_depth = 3
        for r in self.cfg.roots:
            label = Text.assemble((r.name, "bold"), (f"  {r.path}", "dim"),
                                  ("  ⇄ network" if r.remote else "  local", "#7dcfff" if r.remote else "dim"))
            n = tree.root.add(label, data=Node("dir", r.path, r), expand=False)
            if not os.path.isdir(r.path):
                n.set_label(Text.assemble((r.name, "bold"), ("  ✗ not reachable: " + r.path, "#f7768e")))
            else:
                self.load_dir(n)
                n.expand()
        tree.focus()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        d = event.node.data
        if isinstance(d, Node) and d.kind == "dir" and not d.loaded:
            self.load_dir(event.node)

    @work(thread=True)
    def load_dir(self, node) -> None:
        d: Node = node.data
        dirs, files = [], []
        try:
            for e in os.scandir(d.path):
                if e.name.startswith(".") or e.name.startswith("@"):
                    continue
                if e.is_dir():
                    dirs.append(e.path)
                elif os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                    try:
                        files.append((e.path, e.stat().st_size))
                    except OSError:
                        pass
        except OSError as ex:
            self.call_from_thread(self.notify, f"Can't read {d.path}: {ex}", severity="error")
            return
        dirs.sort(key=str.lower)
        files.sort(key=lambda f: f[0].lower())
        cached = {p: self.probes.cached(p) for p, _ in files}
        self.call_from_thread(self._fill_dir, node, dirs, files, cached)

    def _fill_dir(self, node, dirs, files, cached) -> None:
        d: Node = node.data
        if d.loaded:
            return
        d.loaded = True
        tree = self.query_one("#lib-tree", Tree)
        cur = tree.cursor_node.data.path if tree.cursor_node and isinstance(tree.cursor_node.data, Node) else None
        node.remove_children()
        for p in dirs:
            node.add(Text(os.path.basename(p)), data=Node("dir", p, d.root), allow_expand=True)
        for p, size in files:
            node.add_leaf(self.file_label(p, size, cached.get(p)), data=Node("file", p, d.root, size=size))
        if getattr(d, "refreshing", False):
            d.refreshing = False
            # keep the cursor where it was (same file, or the file that replaced it) and redraw details
            folder = norm(os.path.dirname(cur)) if cur else ""
            if cur and folder == norm(d.path):
                old = os.path.basename(cur)
                pick = max(node.children, default=None,
                           key=lambda c: len(os.path.commonprefix([old, os.path.basename(c.data.path)])))
                if pick:
                    tree.move_cursor(pick)
                    self.show_details(pick.data, pick)

    def file_label(self, path: str, size: int, m: MediaInfo | None) -> Text:
        key = norm(path)
        st = self._tree_status.get(key, "")
        mark = {"done": ("✓ ", "#9ece6a"), "busy": ("● ", "#e0af68"), "wait": ("⚑ ", "#bb9af7")}.get(st, ("  ", ""))
        name = os.path.basename(path)
        if len(name) > 42:  # keep the end: that's where SxxEyy and quality live
            name = name[:16] + "…" + name[-25:]
        t = Text.assemble(mark, name.ljust(43))
        if m:
            t.append_text(badge(m.codec))
            t.append(f" {m.res:>5}", "dim")
        t.append(f" {fsize(size):>9}", "dim")
        return t

    def current_node(self) -> Node | None:
        n = self.query_one("#lib-tree", Tree).cursor_node
        return n.data if n and isinstance(n.data, Node) else None

    def root_for(self, path: str) -> Root | None:
        best = None
        for r in self.cfg.roots:
            if os.path.normcase(path).startswith(os.path.normcase(r.path)) and (not best or len(r.path) > len(best.path)):
                best = r
        return best

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        d = event.node.data
        if isinstance(d, Node):
            self.show_details(d, event.node)
            if not getattr(self, "_revealing", False):
                self.cfg.last_path = d.path

    async def reveal(self, path: str) -> bool:
        """Expand the tree down to `path` (loading folders as needed) and put the cursor on it."""
        tree = self.query_one("#lib-tree", Tree)
        target = norm(path)
        node = next((n for n in tree.root.children if target == norm(n.data.path)
                     or target.startswith(norm(n.data.path) + os.sep)), None)
        self._revealing = True
        try:
            while node is not None:
                if norm(node.data.path) == target:
                    tree.move_cursor(node)
                    self.call_after_refresh(tree.scroll_to_node, node)
                    return True
                node.expand()
                for _ in range(200):  # folders load in a worker; wait for this one (≤10 s on a slow share)
                    if node.data.loaded:
                        break
                    await asyncio.sleep(0.05)
                node = next((c for c in node.children if target == norm(c.data.path)
                             or target.startswith(norm(c.data.path) + os.sep)), None)
            return False
        finally:
            self._revealing = False

    @work(thread=True, group="details", exclusive=True)
    def show_details(self, d: Node, tree_node=None) -> None:
        worker = get_current_worker()

        def call(fn, *a):  # a newer selection cancels this worker; never paint stale details
            if not worker.is_cancelled:
                self.call_from_thread(fn, *a)
        if d.kind == "file":
            m = self.probes.cached(d.path)
            if not m:
                call(self._set_details, Text(f"reading {os.path.basename(d.path)}…", style="dim"))
                m = self.probes.get(d.path)
                if tree_node is not None and not m.error:
                    call(tree_node.set_label, self.file_label(d.path, d.size or m.size, m))
            rec = self.arr.lookup(d.path) if self.arr else None
            ep = self.arr.episode(rec, d.path) if rec and self.arr else None
            call(self._set_details, self.file_details(m, d, rec, ep))
        else:
            rec = self.arr.lookup(d.path) if self.arr else None
            is_root = any(os.path.normcase(r.path) == os.path.normcase(d.path) for r in self.cfg.roots)
            files = self.walk(d.path, limit=0 if is_root else 3000)
            call(self._set_details, self.folder_details(d, files, rec, probed=False, is_root=is_root))
            if not is_root and len(files) <= 2000:
                todo = [p for p, _ in files if not self.probes.cached(p)]
                for i, p in enumerate(todo):
                    if worker.is_cancelled:
                        return
                    self.probes.get(p)
                    if i % 10 == 9:
                        call(self._set_details, self.folder_details(d, files, rec, probed=False,
                                                                     progress=(len(files) - len(todo) + i + 1,
                                                                               len(files))))
                call(self._set_details, self.folder_details(d, files, rec, probed=True))

    def walk(self, path: str, limit: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        if limit == 0:
            return out
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(n for n in dirnames if not n.startswith((".", "@")))
            for n in sorted(filenames):
                if os.path.splitext(n)[1].lower() in VIDEO_EXT:
                    p = os.path.join(dirpath, n)
                    try:
                        out.append((p, os.path.getsize(p)))
                    except OSError:
                        pass
                    if len(out) >= limit:
                        return out
        return out

    def _set_details(self, r) -> None:
        self.query_one("#details", Static).update(r)

    def file_details(self, m: MediaInfo, d: Node, rec, ep) -> Group:
        if m.error:
            return Group(Text(os.path.basename(d.path), style="bold"), Text(f"✗ {m.error}", style="#f7768e"))
        g = Table.grid(padding=(0, 2))
        g.add_column(style="#565f89", width=9)
        g.add_column()
        g.add_row("Video", Text.assemble(badge(m.codec), f" {m.profile} · {m.width}×{m.height} · {m.fps:g} fps · "
                                         f"{m.vkbps:,} kb/s" + (f" · {m.hdr}" if m.hdr else "")))
        g.add_row("Audio", "  ".join(f"{i + 1} {m.audio_label(a)}" for i, a in enumerate(m.audio)) or "none")
        g.add_row("Subs", "  ".join(f"{i + 1} {m.sub_label(s)}" for i, s in enumerate(m.subs)) or "none")
        g.add_row("Length", f"{fdur(m.duration)}  ·  {m.frames:,} frames" +
                  ("  ·  interlaced (use a preset with Deinterlace on)" if m.interlaced else ""))
        g.add_row("Size", fsize(m.size))
        g.add_row("Where", Text(d.path, style="#9ece6a"))
        g.add_row("Access", "network share → copied to scratch first" if d.root.remote else "local disk — read in place")
        parts = [Text(os.path.basename(d.path), style="bold #c0caf5")]
        if rec:
            t = Text(f"◆ {rec['source']}  ", style="bold #7dcfff")
            t.append(f"{rec['title']}" + (f" ({rec['year']})" if rec.get("year") else ""))
            if ep:
                t.append(f" · S{ep['season']:02}E{ep['episode']:02} “{ep['title']}” · aired {ep['aired']} · {ep['quality']}")
            elif rec.get("quality"):
                t.append(f" · {rec['quality']}")
            parts.append(t)
        parts += [Text(""), g]
        h = self.engine.record_for(m.path)
        if h:
            when = time.strftime("%b %d", time.localtime(h.get("when", 0)))
            t = Text(f"✓ re-encoded by recast on {when} with {h['preset']}: {fsize(h['src_size'])} → "
                     f"{fsize(h['out_size'])} ({(h['out_size'] / max(1, h['src_size']) - 1) * 100:+.0f}%)",
                     style="#9ece6a")
            orig = h.get("orig", "")
            if orig and os.path.exists(orig):
                t.append("\n↶ original kept in the trash — press u to put it back", style="#7dcfff")
            parts += [Text(""), t]
            return Group(*parts)
        parts += [Text(""), self.preset_gains([m], d.path)]
        parts.append(Text("\ne → encode this file", style="dim"))
        return Group(*parts)

    def preset_gains(self, infos: list[MediaInfo], path: str):
        """Every preset, what it would do to these files: measured on this show when we have real results."""
        caps = self.cfg.encoders
        done = self.engine.done_paths()
        measured = self.engine.measured(path)
        last = self.engine.last_used(path)
        last_name = last["preset"].rstrip("*") if last else None
        rows = []
        options = list(self.presets.items())
        if last and last["preset"].endswith("*") and last["preset"] in measured:
            # what you actually ran here last time (a tweaked preset) gets its own row
            options.insert(0, (last["preset"], ("", EncodeSettings(**last["settings"]))))
        for name, (_desc, s) in options:
            fresh = [m for m in infos if norm(m.path) not in done]
            todo = [m for m in fresh if not already_target(s, m, caps)]
            src = sum(m.size for m in todo)
            if not todo:
                why = "✓ done by recast" if not fresh else (skip_reason(s, fresh[0], caps) or "no saving")
                rows.append((9.0, name, s, 0, 0, 0, why))
                continue
            if name in measured:
                ratio, n = measured[name]
                out, how = src * ratio, f"measured · {n} file{'s' * (n != 1)}"
            else:
                out, how = sum(est_bytes(s, m, caps) for m in todo), "estimate"
            rows.append((out / src - 1, name, s, len(todo), src, out, how))
        rows.sort(key=lambda r: r[0])
        idle = [r for r in rows if r[0] == 9.0]
        rows = [r for r in rows if r[0] != 9.0]
        if not rows:
            why = idle[0][6] if idle else ""
            return Text(("✓ Nothing left to gain here — " + ("all done by recast" if "done" in why else
                                                               "no preset would make these files smaller")),
                        style="#9ece6a")
        t = Table(box=None, padding=(0, 1), header_style="bold dim", title_justify="left",
                  title=Text("What each preset would do", style="bold"), show_edge=False, expand=False)
        one_line = {"no_wrap": True, "overflow": "ellipsis"}
        for c, kw in (("", {"width": 2}), ("Preset", {"max_width": 26, "ratio": 3, **one_line}),
                      ("Encoder", {"style": "#7dcfff", "max_width": 14, **one_line}),
                      ("Files", {"justify": "right", **one_line}), ("After", {"justify": "right", **one_line}),
                      ("Saves", {"justify": "right", **one_line}), ("", {"style": "dim", **one_line})):
            t.add_column(c, **kw)
        best = next((r for r in rows if r[0] < 0), None)
        for pct, name, s, n, src, out, how in rows:
            mark = Text.assemble(("★" if name == self.cfg.default_preset else " ", "#e0af68"),
                                 ("◆" if name == last_name or last and name == last["preset"] else " ", "#bb9af7"))
            col = "bold #9ece6a" if pct <= -0.10 else "#e0af68" if pct < 0 else "bold #f7768e"
            t.add_row(mark, Text(name, style="bold" if best and name == best[1] else ""),
                      resolve_encoder(s, caps)[0].replace("_videotoolbox", "_vt"), str(n), f"≈{fsize(out)}",
                      Text(f"{pct * 100:+.0f}%", style=col),
                      Text(how.replace("estimate", "est."), style="#9ece6a" if how.startswith("measured") else "dim"))
        summary = Text()
        dflt = next((r for r in rows if r[1] == self.cfg.default_preset and r[0] != 9.0), None)
        for label, r in (("best", best), ("default", dflt if dflt is not best else None)):
            if r and r[0] < 0:
                summary.append(f"{label}: ", "dim")
                summary.append(f"{r[1]} saves {fsize(r[4] - r[5])}", "bold #9ece6a" if label == "best" else "#9ece6a")
                summary.append("   ")
        legend = Text("★ default  ◆ last used here  · measured = from files you've actually encoded in this show",
                      style="dim")
        parts = [t] + ([summary] if summary else [])
        if idle:
            parts.append(Text("nothing to gain from: " + ", ".join(r[1] for r in idle), style="dim"))
        return Group(*parts, legend)

    def folder_details(self, d: Node, files, rec, probed: bool, progress=None, is_root=False) -> Group:
        parts = [Text(os.path.basename(d.path.rstrip("/\\")) or d.path, style="bold #c0caf5"),
                 Text(d.path, style="#9ece6a")]
        if rec:
            info = Text(f"◆ {rec['source']}  ", style="bold #7dcfff")
            info.append(" · ".join(str(rec[k]) for k in ("title", "status", "profile", "episodes", "quality")
                                   if rec.get(k)))
            parts.append(info)
        if is_root:
            parts += [Text(""), Text("Library folder · " + ("network share (copies to scratch)" if d.root.remote
                                                              else "local disk"), style="dim"),
                      Text("Expand it and pick a show, season or movie.", style="dim")]
            return Group(*parts)
        total = sum(sz for _, sz in files) or 1
        parts += [Text(""), Text(f"{len(files)} video files · {fsize(total)}", style="bold")]
        infos = [m for m in (self.probes.cached(p) for p, _ in files) if m]
        if progress:
            parts.append(Text(f"reading headers {progress[0]}/{progress[1]}…", style="dim"))
        if infos:
            by: dict[str, int] = {}
            for m in infos:
                by[m.codec] = by.get(m.codec, 0) + m.size
            bar, leg = Text(), Text()
            for c, b in sorted(by.items(), key=lambda kv: -kv[1]):
                bar.append("█" * max(1, round(b / total * 50)), CODEC_STYLE.get(c, "").split(" on ")[-1] or "#a9b1d6")
                leg.append_text(badge(c))
                leg.append(f" {fsize(b)}  ")
            parts += [bar, leg]
            done = self.engine.done_paths()
            n_done = sum(1 for m in infos if norm(m.path) in done)
            busy = sum(1 for p, _ in files if norm(p) in self.engine.busy_paths())
            extra = []
            if n_done:
                k, before, after = self.engine.saved_under(d.path)
                extra.append(f"✓ {n_done} re-encoded by recast" +
                             (f" · saved {fsize(before - after)} ({fsize(before)} → {fsize(after)})" if k else ""))
            if busy:
                extra.append(f"● {busy} in the queue / awaiting approval")
            if extra:
                parts.append(Text("  ·  ".join(extra), style="#9ece6a"))
            partial = len(infos) < len(files)
            parts += [Text(""), self.preset_gains(infos, d.path)]
            if partial:
                parts.append(Text(f"(based on {len(infos)} of {len(files)} files so far)", style="dim italic"))
        if len(files) > 2000 and not probed:
            parts.append(Text("Large folder: headers are read when you press e.", style="dim"))
        parts.append(Text("\ne → try 1 file first, or encode the whole folder", style="dim"))
        return Group(*parts)

    # ── encode flow ──
    def action_encode(self) -> None:
        if self.query_one("#tabs", TabbedContent).active != "tab-library":
            self.action_tab("tab-library")
            self.query_one("#lib-tree").focus()
            self.notify("Pick a file, season or show, then press e.", timeout=3)
            return
        d = self.current_node()
        if d:
            self.start_encode(d.path, d.kind == "dir")

    @work(thread=True, group="encode", exclusive=True)
    def start_encode(self, path: str, is_dir: bool, settings: EncodeSettings | None = None,
                     preset: str | None = None, exclude: str | None = None) -> None:
        call = self.call_from_thread
        if any(os.path.normcase(r.path) == os.path.normcase(path) for r in self.cfg.roots):
            call(self.notify, "That's a whole library folder. Pick a show, season or movie.", severity="warning")
            return
        paths = [p for p, _ in self.walk(path, 100000)] if is_dir else [path]
        if exclude:
            paths = [p for p in paths if norm(p) != norm(exclude)]
        if not paths:
            call(self.notify, "No video files here.", severity="warning")
            return
        infos, bad = [], 0
        uncached = sum(1 for p in paths if not self.probes.cached(p))
        if uncached > 20:
            call(self.notify, f"Reading {uncached} file headers (first time only — cached after this)…",
                 title="Encode", timeout=6)
        for i, p in enumerate(paths):
            if uncached > 20 and i % 25 == 0:
                call(self._set_details, Text(f"reading headers {i}/{len(paths)}…", style="dim"))
            m = self.probes.get(p)
            if m.error:
                bad += 1
            else:
                infos.append(m)
        if not infos:
            call(self.notify, "Couldn't read any of those files with ffprobe.", severity="error")
            return
        busy, done = self.engine.busy_paths(), self.engine.done_paths()
        skipped = {norm(m.path): ("already queued / awaiting approval" if norm(m.path) in busy
                                  else "already re-encoded by recast")
                   for m in infos if norm(m.path) in busy or norm(m.path) in done}
        if bad:
            skipped["__unreadable__"] = f"{bad} unreadable file{'s' * (bad != 1)}"
        if not is_dir and skipped and norm(path) in busy:
            call(self.notify, "That file already has a job — see the Queue / Approvals tab.", severity="warning")
            return
        last = self.engine.last_used(path)
        note = None
        if settings is None and last and last["preset"].rstrip("*") in self.presets:
            preset, settings = last["preset"].rstrip("*"), EncodeSettings(**last["settings"])
            m = self.engine.measured(path).get(last["preset"])
            note = Text.assemble(("◆ Last used on this show: ", "#bb9af7"), (last["preset"], "bold"),
                                 (f" — {os.path.basename(last['src'])}", "dim"),
                                 (f"  {(m[0] - 1) * 100:+.0f}% measured" if m else "", "#9ece6a"),
                                 ("  (still in progress)" if last.get("pending") else "", "dim"))
        title = Text.assemble(("Encode  ", "bold"), (path, "bold #c0caf5"), "   ")
        if len(infos) == 1:
            m = infos[0]
            title.append_text(badge(m.codec))
            title.append(f" {m.res} · {fsize(m.size)}")
        else:
            title.append(f"{len(infos)} files · {fsize(sum(m.size for m in infos))} · ")
            for c in sorted({m.codec for m in infos}):
                title.append_text(badge(c))
                title.append(" ")
        call(self.open_dialog, title, infos, settings, preset, path if is_dir else None, None, skipped, note)

    def open_dialog(self, title, infos, settings, preset, folder, on_done, skipped=None, note=None) -> None:
        def done(res):
            if not res:
                return
            if on_done:
                on_done()
            self.enqueue(res)
        self.push_screen(EncodeDialog(title, infos, self.cfg, self.presets, preset or self.cfg.default_preset,
                                      settings, folder, skipped=skipped or {}, note=note,
                                      measured=self.engine.measured(folder or infos[0].path)), done)

    def enqueue(self, res: dict) -> None:
        files = res["files"]
        root = self.root_for(files[0].path)
        if not root:
            self.notify("That file isn't inside a library folder.", severity="error")
            return
        if len(files) == 1:
            n_before = len(self.engine.jobs)
            j = self.engine.add_single(files[0], root, res["s"], res["preset"], preview=True)
            first = j
            if len(self.engine.jobs) == n_before:
                self.notify(f"{j.name} already has a job ({j.stage}).", title="Queue", severity="warning")
            else:
                self.notify(f"Encoding {j.name} with {res['preset']}", title="Queue")
        else:
            caps = self.cfg.encoders
            b = self.engine.add_batch(res["folder"] or os.path.dirname(files[0].path), files, root, res["s"],
                                      res["preset"], lambda m: skip_reason(res["s"], m, caps))
            jobs = self.engine.batch_jobs(b)
            if not jobs or not any(j.stage == "queued" for j in jobs):
                self.notify("Nothing left to encode there — everything is done, queued or already in that codec.",
                            title="Queue", severity="warning")
                if not jobs:
                    del self.engine.batches[b.id]
                return
            first = next((j for j in jobs if j.stage == "queued"), jobs[0])
            n = sum(1 for j in jobs if j.stage == "queued")
            skipped = len(jobs) - n
            self.notify(f"Queued {n} files from {_short(b.folder)} · one approval for the batch"
                        + (f"\n{skipped} skipped (already done / in that codec)" if skipped else ""), title="Queue")
        self.sync_job_rows()
        self.selected_job = first
        self.action_tab("tab-queue")
        tbl = self.query_one("#jobs", DataTable)
        tbl.move_cursor(row=tbl.get_row_index(str(first.id)))

    # ── engine events ──
    def desktop(self, title: str, body: str) -> None:
        """OS notification for things worth walking back to the computer for."""
        if not self.cfg.desktop_notify:
            return
        import shutil
        import subprocess
        import sys
        try:
            if sys.platform == "darwin":
                esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.Popen(["osascript", "-e", f'display notification "{esc(body)}" with title "recast" '
                                  f'subtitle "{esc(title)}"'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform.startswith("linux") and shutil.which("notify-send"):
                subprocess.Popen(["notify-send", f"recast · {title}", body],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def on_engine_event(self, kind: str, obj) -> None:
        self.refresh_inbox()
        if kind == "finished":
            self.desktop("Encode finished", f"{obj.name} is ready for review")
        elif kind == "batch_done":
            self.desktop("Batch encoded", f"{_short(obj.folder)}: one approval waiting")
        elif kind == "batch_replaced":
            self.desktop("Batch done", f"{_short(obj.folder)} replaced in the library")
        elif kind in ("failed", "offline", "no_space"):
            self.desktop({"failed": "Job failed", "offline": "Library offline", "no_space": "Out of scratch space"}[kind],
                         getattr(obj, "name", str(obj if kind == "offline" else obj[0].name)))
        if kind == "finished":
            if len(self.screen_stack) == 1:
                self.prompt_for(obj)
            else:
                self.notify(f"{obj.name} is waiting in Approvals", title="⚑ Encode finished")
        elif kind == "batch_first":
            self.notify(f"First file of {_short(obj.folder)} is done. Review it whenever — approving lets the rest "
                        "replace automatically.", title="▤ Batch in Approvals")
        elif kind == "batch_done":
            self.notify(f"{_short(obj.folder)}: all encoded. One decision waiting in Approvals.", title="▤ Batch finished")
        elif kind == "flagged":
            self.notify(f"{obj.name}: {obj.flag} — held for you in Approvals", title="⚑ Not auto-replaced",
                        severity="warning")
        elif kind == "replaced":
            self.notify(f"{obj.name}\n{obj.result}", title="✓ Replaced in library")
            self.refresh_dirs({os.path.dirname(obj.src)})
        elif kind == "batch_replaced":
            js = self.engine.batch_jobs(obj, "replaced")
            saved = sum(j.info.get("size", 0) - j.out_size for j in js)
            self.notify(f"{len(js)} files replaced · saved {fsize(saved)}", title=f"✓ {_short(obj.folder)} done")
            self.refresh_dirs({os.path.dirname(j.src) for j in js})
        elif kind == "offline":
            self.notify(f"Can't reach {obj}. Jobs there wait (nothing fails) until it's back.",
                        title="⚠ Library offline", severity="warning", timeout=15)
        elif kind == "online":
            self.notify(f"{obj} is reachable again — continuing.", title="✓ Library back", timeout=6)
        elif kind == "no_space":
            j, free, need = obj
            self.notify(f"{j.name} is waiting: scratch has {fsize(free)} free, needs ≈{fsize(need)}. Free space or "
                        "approve/deny finished encodes.", title="❚❚ Not enough scratch space", severity="warning",
                        timeout=15)
        elif kind == "failed":
            self.notify(f"{obj.name}: {obj.error}", title="✗ Job failed", severity="error", timeout=12)
        elif kind == "budget":
            if not getattr(self, "_budget_warned", False):
                self._budget_warned = True
                self.notify("Scratch budget reached — approve the batch so finished files can move back to the "
                            "library, then it continues.", title="❚❚ Batch waiting", timeout=12)

    @work(thread=True, group="prompt")
    def prompt_for(self, j: Job) -> None:
        """Approval prompt for a single file, offering to carry the same settings to the rest of the show."""
        season, show = os.path.dirname(j.src), show_root(j.src)
        rest = []
        n_season = len([p for p, _ in self.walk(season, 5000)]) - 1
        if n_season > 0:
            rest.append((f"rest of {os.path.basename(season)} ({n_season})", season))
        if norm(show) != norm(season) and not any(norm(r.path) == norm(show) for r in self.cfg.roots):
            n_show = len(self.walk(show, 20000)) - 1
            if n_show > n_season:
                rest.append((f"whole show ({n_show})", show))
        self.call_from_thread(self._push_prompt, j, rest)

    def _push_prompt(self, j: Job, rest) -> None:
        if len(self.screen_stack) > 1 or j.stage != "awaiting":
            self.notify(f"{j.name} is waiting in Approvals", title="⚑ Encode finished")
            return
        self.push_screen(ApprovalPrompt(j, self.cfg.encoders, rest), lambda r, j=j: self.decide(j, r or "later"))

    def refresh_dirs(self, dirs: set[str]) -> None:
        """Re-list folders whose files recast just replaced so names/codecs/sizes are current."""
        want = {norm(d) for d in dirs}
        for node in list(self.query_one("#lib-tree", Tree)._tree_nodes.values()):
            d = node.data
            if isinstance(d, Node) and d.kind == "dir" and d.loaded and norm(d.path) in want:
                d.loaded = False
                d.refreshing = True
                self.load_dir(node)

    # ── actions ──
    def check_action(self, action: str, parameters) -> bool | None:
        if len(self.screen_stack) > 1 and action not in ("quit_app",):
            return False
        if isinstance(self.focused, (Input, PresetEditor)) and action in (
                "encode", "jump", "restore", "toggle_frame", "pause", "cancel_job", "approve", "deny", "retry", "compare",
                "cadence", "tab", "quit_app", "help"):
            return False
        return True

    def action_jump(self) -> None:
        def go(q: str | None) -> None:
            if not q:
                return
            tree = self.query_one("#lib-tree", Tree)
            ql = q.lower()
            start = tree.cursor_node
            nodes = [n for n in tree._tree_nodes.values() if isinstance(n.data, Node)]
            nodes.sort(key=lambda n: n.line if n.line >= 0 else 10**9)
            hits = [n for n in nodes if ql in os.path.basename(n.data.path).lower()]
            if not hits:
                self.notify(f"Nothing loaded matches “{q}” (expand a folder to search inside it).",
                            severity="warning")
                return
            after = [n for n in hits if start is None or n.line > start.line]
            n = (after or hits)[0]
            n.expand() if n.data.kind == "dir" else None
            p = n.parent
            while p is not None:
                p.expand()
                p = p.parent
            self.call_after_refresh(tree.move_cursor, n)
            self.call_after_refresh(tree.scroll_to_node, n)
            tree.focus()
        self.action_tab("tab-library")
        self.push_screen(NamePrompt("Find in library (show, season, file…)", "e.g. law & order"), go)

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_frame(self) -> None:
        self.frame_on = not self.frame_on
        self.query_one("#frame-panel").display = self.frame_on

    def action_cadence(self, d: int) -> None:
        self.cadence = max(0, min(len(CADENCES) - 1, self.cadence - d))
        self.cfg.preview_cadence = CADENCES[self.cadence][1]
        self.cfg.save()
        self.set_frame_timer()
        self.notify(f"Frame preview every {CADENCES[self.cadence][0]}", timeout=2)

    def set_frame_timer(self) -> None:
        if self._frame_timer:
            self._frame_timer.stop()
        self._frame_timer = self.set_interval(CADENCES[self.cadence][1], self.update_frame)

    def action_pause(self) -> None:
        self.engine.set_hold(not self.engine.hold)
        self.notify("Paused — the running encode is frozen and nothing new starts. space to resume."
                    if self.engine.hold else "Resumed", title="Queue", timeout=4)

    def action_cancel_job(self) -> None:
        j = self.selected_job
        if j and j.stage in ACTIVE:
            self.engine.cancel(j)
            self.notify(f"Cancelled {j.name}", timeout=3)

    def action_quit_app(self) -> None:
        busy = [j for j in self.engine.jobs.values() if j.stage in ("copying", "encoding", "paused", "replacing")]
        if busy:
            def go(yes):
                if yes:
                    self.call_later(self._shutdown)
            self.push_screen(ConfirmScreen(f"{busy[0].name} is still {busy[0].stage}.\nQuit anyway? It restarts "
                                           "from the beginning next launch; nothing in your library is touched.",
                                           yes="Quit", no="Keep running"), go)
        else:
            self.call_later(self._shutdown)

    async def _shutdown(self) -> None:
        self.cfg.save()
        await self.engine.shutdown()
        self.probes.save()
        self.exit()

    # approvals
    def highlighted_item(self):
        item = self.query_one("#appr-list", ListView).highlighted_child
        if isinstance(item, BatchItem):
            return item.batch
        return item.job if isinstance(item, JobItem) else None

    def decide(self, item, what: str) -> None:
        if item is None:
            return
        if what == "later":
            self.notify("Kept in the Approvals inbox — it'll wait for you.", timeout=3)
        elif what == "approve":
            self.engine.approve(item)
        elif what == "deny":
            self.engine.deny(item)
            self.notify("Discarded. Originals untouched.", timeout=3)
        elif what == "compare" and isinstance(item, Job):
            self.push_screen(CompareScreen(item, self.cfg.ffmpeg))
        elif what.startswith("rest:") and isinstance(item, Job):
            self.engine.approve(item)
            self.start_encode(what[5:], True, item.s, item.preset.rstrip("*"), exclude=item.src)
        elif what == "retry":
            if isinstance(item, Batch):
                files = [j.media for j in self.engine.batch_jobs(item)]
                title = Text.assemble(("Retry  ", "bold"), (item.folder, "bold #c0caf5"))
                self.open_dialog(title, files, item.s, item.preset.rstrip("*"), item.folder,
                                 lambda: self.engine.deny(item))
            else:
                title = Text.assemble(("Retry  ", "bold"), (item.src, "bold #c0caf5"))
                self.open_dialog(title, [item.media], item.s, item.preset.rstrip("*"), None,
                                 lambda: self.engine.deny(item))
        self.refresh_inbox()

    def action_approve(self) -> None:
        self.decide(self.highlighted_item(), "approve")

    def action_deny(self) -> None:
        self.decide(self.highlighted_item(), "deny")

    def action_retry(self) -> None:
        if self.query_one("#tabs", TabbedContent).active == "tab-queue":
            j = self.selected_job
            if j and self.engine.requeue(j):
                self.notify(f"Retrying {j.name}", timeout=3)
            elif j:
                self.notify("Only failed or cancelled jobs can be retried here.", timeout=3)
            return
        self.decide(self.highlighted_item(), "retry")

    def action_restore(self) -> None:
        d = self.current_node()
        if self.query_one("#tabs", TabbedContent).active != "tab-library" or not d or d.kind != "file":
            return
        h = self.engine.record_for(d.path)
        if not h:
            self.notify("Only files recast re-encoded can be restored.", timeout=3)
            return

        def go(yes: bool) -> None:
            if yes:
                self.restore_file(d.path)
        self.push_screen(ConfirmScreen(f"Put the original back?\n\n{os.path.basename(h['src'])} returns to the "
                                       f"library; {os.path.basename(h['final'])} is moved to the trash.",
                                       yes="↶ Restore original", no="Cancel"), go)

    @work(thread=True, group="restore")
    def restore_file(self, path: str) -> None:
        try:
            msg = self.engine.restore(path)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, str(e), title="Can't restore", severity="error")
            return
        if self.arr and self.cfg.rescan_after_replace:
            try:
                self.arr.rescan(path)
            except Exception:  # noqa: BLE001
                pass
        self.call_from_thread(self.notify, msg, title="↶ Restored")
        self.call_from_thread(self.refresh_dirs, {os.path.dirname(path)})

    def action_clear_finished(self) -> None:
        gone = self.engine.forget_finished()
        tbl = self.query_one("#jobs", DataTable)
        for jid in gone:
            if jid in self._rows:
                tbl.remove_row(str(jid))
                del self._rows[jid]
        if self.selected_job and self.selected_job.id in gone:
            self.selected_job = None
        self.notify(f"Cleared {len(gone)} finished job{'s' * (len(gone) != 1)} from the list.", timeout=3)

    def action_compare(self) -> None:
        item = self.highlighted_item()
        if isinstance(item, Batch):
            js = self.engine.batch_jobs(item, "awaiting")
            item = js[0] if js else None
        if isinstance(item, Job) and item.out and os.path.exists(item.out):
            self.push_screen(CompareScreen(item, self.cfg.ffmpeg))

    def refresh_inbox(self, force: bool = False) -> None:
        inbox = self.engine.inbox()
        sig = tuple((type(i).__name__, i.id) for i in inbox)
        n = len(inbox)
        try:
            self.query_one("#tabs", TabbedContent).get_tab("tab-approvals").label = \
                f"③ Approvals ({n})" if n else "③ Approvals"
        except Exception:
            pass
        lv = self.query_one("#appr-list", ListView)
        if sig != self._inbox_sig or force:
            self._inbox_sig = sig
            keep = self.highlighted_item()
            lv.clear()
            for i in inbox:
                lv.append(BatchItem(i, self.engine) if isinstance(i, Batch) else JobItem(i))
            if inbox:
                lv.index = inbox.index(keep) if keep in inbox else 0
            self.show_review(inbox[lv.index or 0] if inbox else None)

    def show_review(self, item) -> None:
        d = self.query_one("#appr-detail", Static)
        btn = self.query_one("#a-approve", Button)
        if item is None:
            d.update(Text("\n  Inbox zero. Finished encodes that need a decision land here and wait until you "
                          "approve, deny or retry them.", style="dim"))
            return
        self.query_one("#a-rest", Button).display = not isinstance(item, Batch)
        if isinstance(item, Batch):
            btn.label = "✓ Approve batch  y"
            d.update(self.batch_view(item))
        else:
            btn.label = "✓ Approve & replace  y"
            d.update(Group(Text(item.name, style="bold #c0caf5"),
                           Text(f"{item.src}\npreset: {item.preset} · {_ago(item.waiting_since)}", style="dim"),
                           Text(""), compare_table(item, self.cfg.encoders), Text(""),
                           Text("✓ approve → copy back, original → " + {"trash": ".recast-trash", "keep": ".orig",
                                "delete": "deleted"}[self.cfg.originals] + ", Sonarr/Radarr rescan · c compares frames",
                                style="dim")))

    def batch_view(self, b: Batch) -> Group:
        e = self.engine
        done = e.batch_jobs(b, "awaiting", "to_replace", "replacing", "replaced")
        rem = e.batch_jobs(b, *ACTIVE)
        todo = [j for j in e.batch_jobs(b) if j.stage not in ("skipped", "cancelled", "discarded")]
        src = sum(j.info.get("size", 0) for j in done)
        out = sum(j.out_size for j in done)
        all_src = sum(j.info.get("size", 0) for j in todo) or 1
        ratio = out / src if src else sum(est_bytes(b.s, j.media, self.cfg.encoders) for j in todo) / all_src
        flagged = [j for j in done if j.flag]
        held = sum(j.out_size for j in e.batch_jobs(b, "awaiting"))
        status = Text()
        live = next((j for j in rem if j.stage in LIVE), None)
        if rem:
            status.append(f"encoding {len(done)}/{len(todo)}", "bold #e0af68")
            if live and live.speed:
                left = sum(j.info.get("duration", 0) for j in rem) - live.out_time
                status.append(f" · ETA ≈{fdur(left / live.speed)}")
            status.append(" · ")
        else:
            status.append(f"all {len(todo)} encoded · waiting for your OK · ", "bold #bb9af7")
        status.append(f"scratch holding {fsize(held)} of {self.cfg.max_scratch_gb} GiB", "dim")
        t = Table(box=None, padding=(0, 2), header_style="bold")
        for c in ("", "Source", "Output"):
            t.add_column(c)
        t.add_row("Files", f"{len(done)} done", Text.assemble(f"{len(done) - len(flagged)} pass · ",
                                                              (f"{len(flagged)} flagged", "bold #e0af68" if flagged else "dim")))
        t.add_row("Size so far", fsize(src), Text(f"{fsize(out)}  ({(ratio - 1) * 100:+.0f}%)", style="bold #9ece6a"))
        t.add_row("Whole batch", fsize(all_src), Text.assemble(f"≈{fsize(all_src * ratio)}  ",
                                                               (f"saves ≈{fsize(all_src * (1 - ratio))}", "bold #9ece6a")))
        t.add_row("Encoder", "", resolve_encoder(b.s, self.cfg.encoders)[0])
        f = Table(box=None, padding=(0, 2), header_style="bold dim")
        for c in ("File", "Source", "Output", "Δ", "Check"):
            f.add_column(c)
        rows = flagged + [j for j in sorted(done, key=lambda j: -j.finished) if not j.flag]
        for j in rows[:14]:
            dd = j.out_size / max(1, j.info.get("size", 1)) - 1
            f.add_row(j.name[:36], fsize(j.info.get("size", 0)), fsize(j.out_size),
                      Text(f"{dd * 100:+.0f}%", style="#9ece6a" if dd < 0 else "#f7768e"),
                      Text(f"⚑ {j.flag}", style="#e0af68") if j.flag else Text("✓", style="#9ece6a"))
        n_pass = len(done) - len(flagged)
        foot = Text(f"✓ Approve batch → replaces the {n_pass} passing file{'s' * (n_pass != 1)} now"
                    + (", then the rest as each passes checks" if rem else "") + ".\n"
                    "  Flagged files come back here one by one; they never auto-replace.\n"
                    "✗ Deny → discards outputs and cancels what's left. Originals are never touched.  c compares frames",
                    style="dim")
        more = Text(f"… and {len(rows) - 14} more" if len(rows) > 14 else "", style="dim")
        return Group(Text.assemble(("▤ ", "#bb9af7"), (_short(b.folder), "bold #c0caf5"), "  ",
                                   (f"batch · {len(todo)} files · {b.preset}", "#e0af68")),
                     Text(b.folder, style="dim"), status, Text(""), t, Text(""), f, more, Text(""), foot)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, JobItem):
            self.show_review(event.item.job)
        elif isinstance(event.item, BatchItem):
            self.show_review(event.item.batch)
        elif isinstance(event.item, PresetItem):
            self.load_preset_doc(event.item.preset_name)

    # ── presets ──
    def refresh_presets(self, select: str | None = None) -> None:
        lv = self.query_one("#preset-list", ListView)
        idx = lv.index or 0
        names = list(self.presets)
        lv.clear()
        for k in names:
            lv.append(PresetItem(k, k == self.cfg.default_preset))
        if names:
            lv.index = names.index(select) if select in names else min(idx, len(names) - 1)
            self.load_preset_doc(names[lv.index])
        self.query_one("#preset-help", Static).update(Text.assemble(
            "Start typing a field or value — suggestions pop up, ", ("Tab", "bold #7dcfff"), " completes (with quotes "
            "and commas), ", ("↑↓", "bold #7dcfff"), " picks, ", ("ctrl+space", "bold #7dcfff"),
            " opens the list. Red = not usable on this machine. Files: ", (str(self._presets_path()), "#9ece6a")))
        sel = self.query_one("#s-default", Select)
        sel.set_options([(k, k) for k in names])
        if self.cfg.default_preset in names:
            sel.value = self.cfg.default_preset

    def _presets_path(self):
        from ..encode import presets_dir
        return presets_dir()

    def load_preset_doc(self, name: str) -> None:
        if name not in self.presets:
            return
        desc, s = self.presets[name]
        ed = self.query_one("#preset-editor", PresetEditor)
        ed.preset_name = name
        ed.load_text(json.dumps(preset_doc(name, desc, s), indent=2, ensure_ascii=False))
        ed.hide()
        self.update_diag()

    def on_text_area_changed(self, event) -> None:
        if event.text_area.id == "preset-editor":
            self.update_diag()

    def update_diag(self) -> None:
        ed = self.query_one("#preset-editor", PresetEditor)
        t = Text()
        for row, msg in ed.diagnostics[:4]:
            t.append(f"line {row + 1}: ", "dim")
            t.append(msg + "\n", "dim italic" if msg.startswith("…") else "#f7768e")
        if not ed.diagnostics:
            t.append("✓ valid", "#9ece6a")
        self.query_one("#preset-diag", Static).update(t)

    def action_save_preset(self) -> None:
        if self.query_one("#tabs", TabbedContent).active == "tab-presets":
            self.preset_action("p-save")

    def save_preset_from_dialog(self, name: str, s: EncodeSettings) -> None:
        save_preset(name, "Saved from the encode dialog.", s)
        self.presets = load_presets()
        self.refresh_presets(select=name)
        self.notify(f"Saved preset “{name}”", title="Presets")

    def preset_action(self, bid: str) -> None:
        ed = self.query_one("#preset-editor", PresetEditor)
        name = getattr(ed, "preset_name", None)
        if bid == "p-save":
            try:
                doc = json.loads(ed.text)
                new = str(doc.pop("name", name) or name).strip()
                desc = str(doc.pop("description", ""))
                s = EncodeSettings.from_dict({**asdict(EncodeSettings()), **doc})
            except Exception as ex:  # noqa: BLE001
                self.notify(str(ex), title="Can't save preset", severity="error")
                return
            save_preset(new, desc, s, old_name=name)
            if name and new != name and self.cfg.default_preset == name:
                self.cfg.default_preset = new
                self.cfg.save()
            self.presets = load_presets()
            self.refresh_presets(select=new)
            warn = [m for _, m in ed.diagnostics if "would use" in m]
            self.notify(f"Saved “{new}”" + (f"\n⚠ {warn[0]}" if warn else ""), title="Presets")
        elif bid == "p-new":
            def made(n):
                if n:
                    save_preset(n, "", EncodeSettings())
                    self.presets = load_presets()
                    self.refresh_presets(select=n)
                    self.query_one("#preset-editor").focus()
            self.push_screen(NamePrompt("New preset name"), made)
        elif bid == "p-dup" and name:
            desc, s = self.presets[name]
            save_preset(f"{name} copy", desc, s)
            self.presets = load_presets()
            self.refresh_presets(select=f"{name} copy")
        elif bid == "p-default" and name:
            self.cfg.default_preset = name
            self.cfg.save()
            self.refresh_presets(select=name)
        elif bid == "p-del" and name:
            if len(self.presets) <= 1 or name == self.cfg.default_preset:
                self.notify("Can't delete the default (or last) preset.", severity="warning")
                return
            delete_preset(name)
            self.presets = load_presets()
            self.refresh_presets()

    # ── settings ──
    def refresh_settings(self) -> None:
        mc = self.cfg.machine
        t = Text()
        t.append(f"{mc.get('host', '?')}", "bold")
        t.append(f" · {mc.get('os', '')} · {mc.get('cpu', '')}" + (f" · {mc.get('gpu')}" if mc.get("gpu") else ""), "dim")
        t.append(f"\nffmpeg {self.cfg.ffmpeg_version} · {self.cfg.ffmpeg} · detected {self.cfg.detected_at}\n", "dim")
        for name, cap in self.cfg.encoders.items():
            if cap.status == "ok":
                t.append(f"{name} ✓ {cap.fps:.0f}fps  ", "#9ece6a")
            elif cap.status == "failed":
                t.append(f"{name} ✗  ", "#f7768e")
        self.query_one("#s-machine", Static).update(t)
        rt = self.query_one("#roots", DataTable)
        rt.clear()
        for r in self.cfg.roots:
            rt.add_row(r.name, r.path, "⇄ network share (copy to scratch)" if r.remote else "local disk")
        for name in ("sonarr", "radarr"):
            a = getattr(self.cfg, name)
            self.query_one(f"#s-{name}-map", Static).update(
                Text("path map: " + "  ·  ".join(f"{x} → {y}" for x, y in a.path_map), style="dim")
                if a.path_map else "")
        from ..config import config_dir
        self.query_one("#s-paths", Static).update(Text(f"\nconfig: {config_dir()}", style="dim"))

    def on_input_changed(self, event: Input.Changed) -> None:
        i, v = event.input.id or "", event.value.strip()
        c = self.cfg
        if i == "s-scratch" and v:
            c.scratch = os.path.expanduser(v)
        elif i == "s-maxscratch" and v.isdigit():
            c.max_scratch_gb = int(v)
        elif i == "s-trashdays" and v.isdigit():
            c.trash_days = int(v)
        elif i.startswith("s-sonarr") or i.startswith("s-radarr"):
            a = getattr(c, i.split("-")[1])
            setattr(a, "url" if i.endswith("url") else "api_key", v)
        else:
            return
        c.save()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        attr = {"s-prefetch": "prefetch", "s-verify": "verify_decode", "s-rescan": "rescan_after_replace",
                "s-rename": "rename_codec", "s-awake": "keep_awake", "s-notify": "desktop_notify"}.get(
            event.switch.id or "")
        if attr:
            setattr(self.cfg, attr, event.value)
            self.cfg.save()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "s-originals":
            self.cfg.originals = event.value
        elif event.select.id == "s-default" and event.value in self.presets:
            self.cfg.default_preset = event.value
        else:
            return
        self.cfg.save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        acts = {"btn-encode": self.action_encode,
                "a-approve": self.action_approve, "a-deny": self.action_deny, "a-retry": self.action_retry,
                "a-compare": self.action_compare}
        if bid in acts:
            acts[bid]()
        elif bid == "btn-refresh":
            self.build_tree()
        elif bid == "a-rest":
            item = self.highlighted_item()
            if isinstance(item, Job):
                self.prompt_for(item)
            else:
                self.notify("That's already a batch — approving it covers the rest.", timeout=4)
        elif bid == "a-all":
            for item in self.engine.inbox():
                if not (isinstance(item, Job) and item.flag):
                    self.engine.approve(item)
            self.refresh_inbox()
        elif bid.startswith("p-"):
            self.preset_action(bid)
        elif bid == "s-detect":
            self.push_screen(SetupScreen(self.cfg, first_run=False), self._after_redetect)
        elif bid == "s-addroot":
            self.push_screen(NamePrompt("Library folder to add", "path…", suggest_paths=True), self._add_root)
        elif bid in ("s-delroot", "s-toggleremote"):
            rt = self.query_one("#roots", DataTable)
            if not self.cfg.roots or rt.cursor_row is None:
                return
            r = self.cfg.roots[rt.cursor_row]
            if bid == "s-delroot":
                self.cfg.roots.remove(r)
            else:
                r.remote = not r.remote
            self.cfg.save()
            self.refresh_settings()
            self.build_tree()
        elif bid.endswith("-test"):
            self.test_arr(bid.split("-")[1])

    def _after_redetect(self, ok) -> None:
        if ok:
            self.query_one("#preset-editor", PresetEditor).caps = self.cfg.encoders
            self.refresh_settings()
            self.build_tree()
            self.notify("Detection saved.", title="✓ Setup")

    def _add_root(self, path: str | None) -> None:
        if not path:
            return
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(p):
            self.notify(f"Not a folder: {p}", severity="error")
            return
        self.cfg.roots.append(Root(os.path.basename(p.rstrip("/\\")) or p, p, is_network_path(p)))
        self.cfg.save()
        self.refresh_settings()
        self.build_tree()

    @work(thread=True, group="arrtest")
    def test_arr(self, name: str) -> None:
        a = getattr(self.cfg, name)
        call = self.call_from_thread
        if not a.enabled:
            call(self.notify, "Fill in the URL and API key first.", severity="warning")
            return
        c = ArrClient(name.title(), a)
        try:
            st = c.status()
            pairs = auto_map(c, [r.path for r in self.cfg.roots])
        except ArrError as e:
            call(self.notify, str(e), title=f"✗ {name.title()}", severity="error")
            return
        if pairs:
            a.path_map = pairs
            self.cfg.save()
        msg = f"{name.title()} {st.get('version', '')} connected" + (
            "\n" + "\n".join(f"{x} → {y}" for x, y in pairs) if pairs else
            "\nCouldn't match its folders to your library automatically.")
        call(self.notify, msg, title=f"✓ {name.title()}", timeout=10)
        call(self.refresh_settings)
        call(self.connect_arr)

    # ── queue table / detail ──
    def sync_job_rows(self) -> None:
        tbl = self.query_one("#jobs", DataTable)
        for j in sorted(self.engine.jobs.values(), key=lambda j: j.id):
            cells = self.job_cells(j)
            key = str(j.id)
            if j.id not in self._rows:
                tbl.add_row(*cells, key=key)
            elif self._rows[j.id] != tuple(str(c) for c in cells):
                for col, val in zip(("id", "file", "preset", "stage", "prog", "fps", "size", "eta"), cells):
                    tbl.update_cell(key, col, val)
            self._rows[j.id] = tuple(str(c) for c in cells)

    def job_cells(self, j: Job):
        label, style = STAGE[j.stage]
        size = j.info.get("size", 1) or 1
        if j.stage == "copying":
            p = j.copied / size
        elif j.stage == "replacing":
            p = j.replace_done / max(1, j.out_size)
        else:
            p = j.progress
        prog = Text("") if j.stage in ("queued", "skipped", "cancelled", "failed") else \
            Text(f"{pct_bar(p)} {p * 100:3.0f}%", style="#e0af68" if j.stage == "encoding" else "dim")
        fps = f"{j.fps:.0f}" if j.stage == "encoding" else ""
        if j.out_size:
            proj = j.projected if j.stage in LIVE else j.out_size
            d = (proj or j.out_size) / size - 1
            sz = Text.assemble(f"{fsize(j.out_size)} ", (f"{d * 100:+.0f}%", "#9ece6a" if d < 0 else "#f7768e"))
        else:
            sz = Text(fsize(size), style="dim")
        eta = fdur((j.info.get("duration", 0) - j.out_time) / j.speed) if j.stage == "encoding" and j.speed else ""
        name = Text.assemble(("▤ " if j.batch else "  ", "#bb9af7"), j.name[:36])
        stage = Text(label, style=style)
        if j.flag and j.stage == "awaiting":
            stage = Text("⚑ flagged", style="bold #e0af68")
        return [str(j.id), name, Text(j.preset[:24], style="dim"), stage, prog, fps, sz, eta]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "jobs" and event.row_key is not None:
            self.selected_job = self.engine.jobs.get(int(event.row_key.value))
            self.update_job_detail()
            self.update_frame()

    def update_tree_marks(self) -> None:
        """Keep ✓ / ● / ⚑ marks on loaded files in sync with the queue (labels only, no disk access)."""
        status = {p: "done" for p in self.engine.done_paths()}
        for j in self.engine.jobs.values():
            if j.stage in ACTIVE or j.stage in ("to_replace", "replacing"):
                status[norm(j.src)] = "busy"
            elif j.stage == "awaiting":
                status[norm(j.src)] = "wait"
        if status == self._tree_status:
            return
        changed = {k for k in set(status) | set(self._tree_status) if status.get(k) != self._tree_status.get(k)}
        self._tree_status = status
        for node in self.query_one("#lib-tree", Tree)._tree_nodes.values():
            d = node.data
            if isinstance(d, Node) and d.kind == "file" and norm(d.path) in changed:
                node.set_label(self.file_label(d.path, d.size, self.probes.cached(d.path)))

    def tick(self) -> None:
        if self.cfg.needs_setup:
            return
        self._ticks += 1
        if self._ticks % 8 == 0:
            self.update_tree_marks()
        sel = self.selected_job
        was_live = sel is not None and sel.stage in LIVE
        self.engine.tick()
        self.sync_job_rows()
        if was_live and sel.batch and sel.stage not in LIVE:
            self._follow = sel
        f = getattr(self, "_follow", None)
        if f is not None and self.selected_job is f:
            b = self.engine.batch_of(f)
            cur = next((j for j in self.engine.batch_jobs(b) if j.stage in LIVE), None) if b else None
            if cur:
                self._follow = None
                self.selected_job = cur
                tbl = self.query_one("#jobs", DataTable)
                tbl.move_cursor(row=tbl.get_row_index(str(cur.id)))
        self.update_topbar()
        tab = self.query_one("#tabs", TabbedContent).active
        if tab == "tab-queue":
            self.update_job_detail()
        elif tab == "tab-approvals" and self._ticks % 4 == 0:
            self.refresh_inbox()
            for it in self.query(BatchItem):
                it.lbl.update(it.text())
            h = self.highlighted_item()
            if isinstance(h, Batch):
                self.show_review(h)

    def update_topbar(self) -> None:
        t = Text()
        t.append("◆ recast ", "bold #bb9af7")
        t.append(" │ ", "dim")
        t.append(f"{self.cfg.machine.get('host', '')} ", "bold #c0caf5")
        t.append(resolve_encoder(EncodeSettings(), self.cfg.encoders)[0], "#7dcfff")
        t.append(" │ ", "dim")
        held = self.engine.held_bytes()
        t.append("scratch ", "dim")
        t.append(fsize(held), "#f7768e" if held > self.cfg.max_scratch_gb * 1024**3 * 0.8 else "#9ece6a")
        if self.arr:
            t.append(" │ ", "dim")
            for kind in self.arr.clients:
                ok = kind not in self.arr.errors
                t.append(f"{'●' if ok else '○'} {kind} ", "#9ece6a" if ok else "#f7768e")
        if self.engine.hold:
            t.append(" │ ", "dim")
            t.append("❚❚ queue paused (space)", "bold #e0af68")
        if self.engine.offline:
            t.append(" │ ", "dim")
            t.append("⚠ library offline", "bold #f7768e")
        n = len(self.engine.inbox())
        if n:
            t.append(" │ ", "dim")
            t.append(f"⚑ {n} awaiting approval", "bold #bb9af7")
        enc = next((j for j in self.engine.jobs.values() if j.stage in ("encoding", "paused", "copying")), None)
        if enc:
            t.append(" │ ", "dim")
            if enc.stage == "copying":
                t.append(f"⇣ {enc.name[:22]} {enc.copied / max(1, enc.info.get('size', 1)) * 100:.0f}%", "#7dcfff")
            else:
                t.append(f"{'❚❚' if enc.stage == 'paused' else '▶'} {enc.name[:22]} "
                         f"{enc.phase + ' ' if enc.phase else ''}{enc.progress * 100:.0f}% {enc.fps:.0f}fps", "#e0af68")
        self.query_one("#topbar", Static).update(t)

    def update_job_detail(self) -> None:
        j = self.selected_job
        if not j:
            return
        m = j.info
        title = Text.assemble((f"#{j.id}  ", "dim"), (j.name, "bold #c0caf5"), "  ", badge(m.get("codec", "?")), " → ",
                              badge(CODEC_LABEL.get(j.s.codec, "?")), (f"   {j.preset}", "#e0af68"),
                              ("   ▤ batch" if j.batch else "", "#bb9af7"))
        self.query_one("#jd-title", Static).update(title)
        idx = {"queued": -1, "copying": 0, "ready": 1, "encoding": 1, "paused": 1, "verifying": 2, "awaiting": 3,
               "to_replace": 3, "replacing": 3, "replaced": 4, "kept": 4, "discarded": 4, "skipped": -1,
               "cancelled": -1, "failed": -1}[j.stage]
        names = ["Copy to scratch" if j.remote else "Local", f"Encode {j.phase}".strip(), "Verify",
                 "Batch approval" if j.batch else "Approve → replace"]
        spin = "◐◓◑◒"[int(time.monotonic() * 4) % 4]
        p = Text()
        for i, nm in enumerate(names):
            p.append(" ─── " if i else "", "dim")
            if i < idx:
                p.append(f"✓ {nm}", "#9ece6a")
            elif i == idx:
                p.append(f"{spin} {nm}", "bold #e0af68")
            else:
                p.append(f"○ {nm}", "dim")
        if j.stage == "failed":
            p.append(f"\n✗ {j.error}", "bold #f7768e")
        elif j.flag:
            p.append(f"\n⚑ {j.flag}", "#e0af68")
        self.query_one("#jd-pipeline", Static).update(p)
        size = m.get("size", 1) or 1
        copied = j.copied if j.stage == "copying" else (size if idx >= 1 or not j.remote else 0)
        self.query_one("#pb-copy", ProgressBar).update(progress=copied / size * 100)
        rate = ""
        if j.stage == "copying":
            now = time.monotonic()
            t0, c0, r = self._copy_rate.get(j.id, (now, j.copied, 0.0))
            if now - t0 >= 1:
                r = (j.copied - c0) / (now - t0)
                self._copy_rate[j.id] = (now, j.copied, r)
            elif j.id not in self._copy_rate:
                self._copy_rate[j.id] = (now, j.copied, 0.0)
            rate = f"  ·  {r / 1e6:.0f} MB/s" if r else ""
        self.query_one("#jd-copyinfo", Static).update(
            f"{fsize(copied)} / {fsize(size)}{rate}" if j.remote else "local file — read in place, no copy")
        self.query_one("#pb-enc", ProgressBar).update(progress=j.progress * 100)
        self.query_one("#jd-encinfo", Static).update(
            f"frame {j.frame:,} / {m.get('frames', 0):,}  ·  {fdur(j.out_time)} / {fdur(m.get('duration', 0))}")
        g = Table.grid(padding=(0, 3))
        for _ in range(4):
            g.add_column()
        k = lambda s: Text(s, style="#565f89")
        live = j.stage in ("encoding", "paused")
        proj = j.projected if live else j.out_size
        g.add_row(k("fps"), Text(f"{j.fps:.0f}" if live else "—", style="bold"),
                  k("speed"), Text(f"{j.speed:.2f}×" if live else "—", style="bold"))
        g.add_row(k("output"), Text(fsize(j.out_size), style="bold #e0af68"), k("projected"),
                  Text.assemble(f"≈{fsize(proj)} " if proj else "—",
                                (f"({(proj / size - 1) * 100:+.0f}%)" if proj else "", "#9ece6a")))
        g.add_row(k("source"), fsize(size), k("bitrate"), f"{j.kbps:,.0f} kb/s" if live and j.kbps else "—")
        eta = fdur((m.get("duration", 0) - j.out_time) / j.speed) if live and j.speed else "—"
        g.add_row(k("eta"), Text(eta, style="bold"), k("elapsed"),
                  fdur((j.finished if j.stage not in LIVE and j.finished else time.time()) - j.started)
                  if j.started else "—")
        self.query_one("#jd-stats", Static).update(g)
        self.query_one("#jd-spark", Sparkline).data = j.spark[-120:] or [0]
        self.query_one("#jd-log", Static).update(Text("\n".join(j.log[-8:]), style="#565f89", no_wrap=True,
                                                      overflow="ellipsis"))

    def update_frame(self) -> None:
        if not self.frame_on or self.query_one("#tabs", TabbedContent).active != "tab-queue":
            return
        j = self.selected_job
        fv = self.query_one("#frame", FrameView)
        cap = self.query_one("#frame-caption", Static)
        cad = CADENCES[self.cadence][0]
        if not j or not j.preview_jpg or not os.path.exists(j.preview_jpg):
            fv.set_image(None, "no frames yet — they appear once encoding starts")
            cap.update(Text(f"every {cad}  ·  [ ] refresh rate  ·  f hide", style="dim"))
            return
        if j.stage in ("encoding",) or fv.image is None or getattr(fv, "_job", None) != j.id:
            img = load_image(j.preview_jpg)
            if img:
                fv.set_image(img)
                fv._job = j.id
        t = Text()
        t.append(f"{fdur(j.out_time)}", "bold")
        t.append(f" · frame {j.frame:,} · every {cad}   [ ] refresh rate", "dim")
        cap.update(t)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-queue":
            if not self.selected_job and self.engine.jobs:
                self.selected_job = max(self.engine.jobs.values(), key=lambda j: j.id)
            self.update_job_detail()
            self.update_frame()
        elif event.pane.id == "tab-approvals":
            self.refresh_inbox(force=True)
