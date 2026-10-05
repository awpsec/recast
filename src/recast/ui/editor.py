"""Preset JSON editor with schema-aware autocomplete.

Type `"codec": "he` → a popup above the cursor lists matching values, the best
one is ghosted in grey after the cursor, Tab accepts it and writes the closing
quote plus a trailing comma when more fields follow. Typing a key name works the
same way and chains straight into its values. Values this machine can't use
(e.g. hevc_qsv on Apple Silicon) are shown and underlined in red.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual import events
from textual.widgets import Static, TextArea
from textual.widgets._text_area import TextAreaTheme

from ..config import EncoderCap
from ..encode import ALL_ENCODERS, ENCODERS, SCHEMA, EncodeSettings, Option, enc_status, resolve_encoder

FIXED = {"codec", "resolution", "rate_mode", "audio", "subs", "container", "after", "speed", "tune",
         "bit_depth", "encoder", "skip_same", "two_pass", "hdr", "deinterlace"}
MAX_ROWS = 5


@dataclass
class Ctx:
    kind: str  # "value" | "key"
    key: str
    prefix: str
    start: int  # column where the replaced token starts


def json_token(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def context_at(line: str, col: int) -> Ctx | None:
    before = line[:col]
    m = re.match(r'^(\s*)"([A-Za-z_]+)"\s*:\s*(.*)$', before)
    if m:
        key, val = m.group(2), m.group(3)
        if key not in SCHEMA:
            return None
        start = col - len(val)
        if val.startswith('"'):
            inner = val[1:]
            if '"' in inner.replace('\\"', ""):
                return None  # string already closed; cursor is past it
            return Ctx("value", key, inner, start)
        if re.fullmatch(r"[\w.+-]*", val):
            return Ctx("value", key, val, start)
        return None
    m = re.match(r'^(\s*)"?([A-Za-z_]*)$', before)
    if m and not line[col:].strip(' "'):
        return Ctx("key", "", m.group(2), len(m.group(1)))
    return None


def rank(options: list[Option], prefix: str) -> list[Option]:
    """Prefix matches, then substring, then in-order subsequence. "he" → hevc, hevc_nvenc…"""
    p = prefix.lower()
    if not p:
        return options
    scored = []
    for i, o in enumerate(options):
        t = _plain(o.value).lower()
        if t == p:
            score = 0
        elif t.startswith(p):
            score = 1
        elif p in t:
            score = 2 + t.index(p) / 100
        else:
            it = iter(t)
            if not all(ch in it for ch in p):
                continue
            score = 3
        scored.append((score, not o.ok, i, o))
    return [o for *_, o in sorted(scored, key=lambda x: x[:3])]


def _plain(v) -> str:
    return json_token(v).strip('"') if not isinstance(v, str) else v


class CompletionPopup(Static):
    DEFAULT_CSS = """
    CompletionPopup {
        overlay: screen; position: absolute; display: none; width: auto; max-width: 76; height: auto;
        background: $panel; border: round $accent; padding: 0 1;
    }
    """


class PresetEditor(TextArea):
    """TextArea that knows the preset schema."""

    def __init__(self, caps: dict[str, EncoderCap], **kw):
        super().__init__(language="json", soft_wrap=False, show_line_numbers=True, tab_behavior="indent", **kw)
        self.caps = caps
        self.items: list[Option] = []
        self.sel = 0
        self.ctx: Ctx | None = None
        self.diagnostics: list[tuple[int, str]] = []
        self._edit_cursor = None
        self.popup = CompletionPopup()

    def on_mount(self) -> None:
        base = TextAreaTheme.get_builtin_theme("vscode_dark")
        styles = dict(base.syntax_styles)
        styles["recast.invalid"] = Style(color="#f7768e", bold=True, underline=True)
        theme = TextAreaTheme("recast", syntax_styles=styles, base_style=base.base_style,
                              gutter_style=base.gutter_style, cursor_style=base.cursor_style,
                              cursor_line_style=base.cursor_line_style, selection_style=base.selection_style,
                              bracket_matching_style=base.bracket_matching_style)
        self.register_theme(theme)
        self.theme = "recast"
        self.screen.mount(self.popup)

    # ── validation → red highlights + diagnostics ──
    def _build_highlight_map(self) -> None:
        super()._build_highlight_map()
        self.diagnostics = []
        rx = re.compile(r'^(\s*)"([^"]*)"\s*:\s*(.*?)\s*,?\s*$')
        for row in range(self.document.line_count):
            line = self.document[row]
            m = rx.match(line)
            if not m:
                continue
            key, raw = m.group(2), m.group(3)
            if key not in SCHEMA:
                self._mark(row, line, m.start(2) - 1, m.end(2) + 1)
                self.diagnostics.append((row, f"unknown field “{key}”"))
                continue
            if not raw:
                continue
            try:
                val = json.loads(raw)
            except ValueError:
                continue  # half-typed; the JSON check reports it if it stays broken
            msg = self._check(key, val)
            if msg:
                self._mark(row, line, m.start(3), m.start(3) + len(raw))
                self.diagnostics.append((row, msg))
        try:
            json.loads(self.text)
        except ValueError as e:
            row = getattr(e, "lineno", 1) - 1
            typing = abs(row - self.cursor_location[0]) <= 1
            self.diagnostics.append((row, ("…" if typing else "JSON: ") + str(getattr(e, "msg", e))))

    def _mark(self, row: int, line: str, a: int, b: int) -> None:
        enc = lambda c: len(line[:c].encode())
        self._highlights[row].append((enc(a), enc(b), "recast.invalid"))

    def _check(self, key: str, val) -> str:
        spec = SCHEMA[key]
        if spec.kind is bool and not isinstance(val, bool) or \
                spec.kind is int and (not isinstance(val, int) or isinstance(val, bool)) or \
                spec.kind is str and not isinstance(val, str):
            return f"{key} should be {'true/false' if spec.kind is bool else 'a number' if spec.kind is int else 'text'}"
        if key == "encoder":
            if val != "auto" and val not in ALL_ENCODERS:
                return f"unknown encoder “{val}”"
            if val != "auto" and enc_status(val, self.caps) != "ok":
                c = self.caps.get(val)
                why = c.reason if c and c.reason else "not available here"
                codec = next((k for k in ("hevc", "h264", "av1") if val.startswith(k) or k in val), "hevc")
                fb = resolve_encoder(EncodeSettings(codec=codec), self.caps)[0]
                return f"{val}: {why} → this machine would use {fb}"
        elif key in FIXED and val not in [o.value for o in spec.options(self.caps)]:
            opts = ", ".join(_plain(o.value) for o in spec.options(self.caps))
            return f"{key} must be one of: {opts}"
        return ""

    # ── suggestions ──
    def update_suggestion(self) -> None:
        """Called by TextArea after every edit — i.e. only while typing."""
        self.refresh_completions()

    def refresh_completions(self, force: bool = False) -> None:
        row, col = self.cursor_location
        ctx = context_at(self.document[row], col) if self.has_focus or force else None
        items: list[Option] = []
        if ctx and ctx.kind == "value":
            items = rank(SCHEMA[ctx.key].options(self.caps), ctx.prefix)
            if ctx.key == "encoder":  # this preset's codec family first, then the rest
                m = re.search(r'"codec"\s*:\s*"(\w+)"', self.text)
                fam = ENCODERS.get(m.group(1), []) if m else []
                items.sort(key=lambda o: o.value not in fam and o.value != "auto")
            if len(items) == 1 and _plain(items[0].value) == ctx.prefix and not force:
                items = []  # fully typed already
        elif ctx and ctx.kind == "key" and row > 0 and not self.document[row].strip().startswith("}"):
            present = set(re.findall(r'"([A-Za-z_]+)"\s*:', self.text))
            items = rank([Option(k, s.doc) for k, s in SCHEMA.items() if k not in present], ctx.prefix)
        self.ctx, self.items, self.sel = ctx, items, 0
        self._edit_cursor = self.cursor_location
        self._render_popup()

    def hide(self) -> None:
        self.items, self.ctx = [], None
        self.suggestion = ""
        self.popup.display = False

    def _ghost(self) -> str:
        if not self.items or not self.ctx:
            return ""
        o = self.items[self.sel]
        text = _plain(o.value) if self.ctx.kind == "value" else str(o.value)
        if not text.lower().startswith(self.ctx.prefix.lower()):
            return ""
        rest = text[len(self.ctx.prefix):]
        row = self.cursor_location[0]
        if self.ctx.kind == "key":
            return rest + '": '
        is_str = SCHEMA[self.ctx.key].kind is str
        tail = self.document[row][self.cursor_location[1]:]
        if tail.strip():
            return rest
        return rest + ('"' if is_str else "") + ("," if self._needs_comma(row) else "")

    def _render_popup(self) -> None:
        if not self.items:
            self.hide()
            return
        self.suggestion = self._ghost()
        t = Text()
        shown_from = max(0, min(self.sel - MAX_ROWS + 1, len(self.items) - MAX_ROWS))
        for i, o in enumerate(self.items[shown_from:shown_from + MAX_ROWS], start=shown_from):
            sel = i == self.sel
            label = _plain(o.value) if self.ctx and self.ctx.kind == "value" else str(o.value)
            if label == "":
                label = '""'
            style = ("bold #f7768e" if not o.ok else "bold #c0caf5") + (" reverse" if sel else "")
            t.append(("▸ " if sel else "  ") + label.ljust(20), style)
            if o.desc:
                t.append("  " + o.desc[:46], "#f7768e" if not o.ok else "dim")
            t.append("\n")
        more = len(self.items) - MAX_ROWS
        t.append(f"{'+' + str(more) + ' more · ' if more > 0 else ''}tab accept · ↑↓ choose · esc", "dim italic")
        self.popup.update(t)
        self.popup.display = True
        rows = min(MAX_ROWS, len(self.items)) + 3
        x, y = self.cursor_screen_offset
        start_x = x - len(self.ctx.prefix) - 3 if self.ctx else x
        top = y - rows if y - rows >= 0 else y + 1
        self.popup.styles.offset = (max(0, start_x), max(0, top))

    def _needs_comma(self, row: int) -> bool:
        for r in range(row + 1, self.document.line_count):
            s = self.document[r].strip()
            if s:
                return not s.startswith("}")
        return False

    def accept(self) -> None:
        if not self.items or not self.ctx:
            return
        o, ctx = self.items[self.sel], self.ctx
        row, col = self.cursor_location
        line = self.document[row]
        if ctx.kind == "key":
            indent = self._indent_near(row)
            new = f'{indent}"{o.value}": '
            self.replace(new, (row, 0), (row, len(line)))
            self._comma_above(row)
            self.move_cursor((row, len(new)))
            self.refresh_completions(force=True)  # chain into the value list
            return
        rest = line[col:]
        m = re.match(r'^[^",]*"?\s*,?', rest)
        end = col + (m.end() if m else 0)
        token = json_token(o.value)
        comma = "," if self._needs_comma(row) else ""
        self.replace(token + comma, (row, ctx.start), (row, end))
        self.move_cursor((row, ctx.start + len(token) + len(comma)))
        self.hide()

    def _indent_near(self, row: int) -> str:
        for r in list(range(row - 1, 0, -1)) + list(range(row + 1, self.document.line_count)):
            m = re.match(r'^(\s+)"', self.document[r])
            if m:
                return m.group(1)
        return "  "

    def _comma_above(self, row: int) -> None:
        for r in range(row - 1, -1, -1):
            s = self.document[r].rstrip()
            if s.strip():
                if not s.endswith((",", "{", "[")):
                    self.insert(",", (r, len(s)))
                return

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+space":
            event.stop()
            event.prevent_default()
            self.refresh_completions(force=True)
            return
        if self.items and self.popup.display:
            row, col = self.cursor_location
            at_eol = not self.document[row][col:].strip(' ",')
            k = event.key
            if k == "tab" or (k == "right" and at_eol and self.suggestion):
                event.stop()
                event.prevent_default()
                self.accept()
                return
            if k in ("down", "up"):
                event.stop()
                event.prevent_default()
                self.sel = (self.sel + (1 if k == "down" else -1)) % len(self.items)
                self._render_popup()
                return
            if k == "escape":
                event.stop()
                event.prevent_default()
                self.hide()
                return
        if event.key == "enter" and not self.selected_text:
            # keep indentation like a code editor (and land straight in key suggestions)
            event.stop()
            event.prevent_default()
            row, col = self.cursor_location
            indent = re.match(r"^\s*", self.document[row]).group(0) if self.document[row].strip() not in ("{",) \
                else self._indent_near(row + 1)
            self.insert("\n" + indent)
            return
        await super()._on_key(event)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        if self.cursor_location != self._edit_cursor:
            self.hide()

    def on_blur(self, event: events.Blur) -> None:
        self.hide()

    def on_unmount(self) -> None:
        self.popup.remove()
