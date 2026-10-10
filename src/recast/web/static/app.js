"use strict";
/* recast web — a small vanilla SPA over /api/*. Everything user-supplied (file names, paths) goes in via
   textContent, never innerHTML. Live progress arrives over /api/events (server-sent events). */

// ───────────────────────────── helpers ─────────────────────────────
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "selected" || k === "disabled") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(9)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  library: '<rect x="3" y="4" width="18" height="4" rx="1"/><rect x="3" y="10" width="18" height="4" rx="1"/><rect x="3" y="16" width="18" height="4" rx="1"/>',
  queue: '<path d="M9 6h12M9 12h12M9 18h12"/><path d="M3.5 6h1M3.5 12h1M3.5 18h1"/>',
  review: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16.5 9.5"/>',
  automation: '<path d="M13 2.5L4.5 13.5h6.5l-1 8 8.5-11h-6.5z"/>',
  presets: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3 4v4.5h4.5"/><path d="M12 7.5V12l3 2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10.5 9.5v5l4-2.5z"/>',
};
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.innerHTML = ICONS[name] || "";
  return s;
}

function fsize(b) {
  if (b == null || isNaN(b)) return "—";
  const neg = b < 0; b = Math.abs(b);
  let s;
  if (b >= 1024 ** 4) s = (b / 1024 ** 4).toFixed(2) + " TiB";
  else if (b >= 1024 ** 3) s = (b / 1024 ** 3).toFixed(1) + " GiB";
  else if (b >= 1024 ** 2) s = (b / 1024 ** 2).toFixed(0) + " MiB";
  else s = (b / 1024).toFixed(0) + " KiB";
  return (neg ? "−" : "") + s;
}
const pct = (x, d = 0) => (x == null || isNaN(x) ? "—" : (x * 100).toFixed(d) + "%");
const saving = (src, out) => (src ? 1 - out / src : null);
function fdur(s) {
  s = Math.max(0, Math.round(s || 0));
  const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(ss).padStart(2, "0")}s` : `${ss}s`;
}
function ago(t) {
  if (!t) return "—";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 86400 * 7) return Math.floor(s / 86400) + "d ago";
  return new Date(t * 1000).toLocaleDateString();
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const base = (p) => (p || "").split(/[\\/]/).filter(Boolean).pop() || p || "";
const SEASON_RX = /^(season|series|staffel|saison|temporada)\s*\d+$|^s\d{1,2}$|^specials?$/i;
/** The show (or movie) a file belongs to: "…/Show/Season 01/x.mkv" → "Show". */
const parentName = (p) => {
  const a = (p || "").split(/[\\/]/).filter(Boolean);
  const dir = a[a.length - 2] || "";
  return SEASON_RX.test(dir) ? `${a[a.length - 3] || ""} · ${dir}` : dir;
};
const enc = encodeURIComponent;
const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const store = {
  get(k, d) { try { const v = localStorage.getItem("recast." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("recast." + k, JSON.stringify(v)); } catch { /* private window */ } },
};

// ───────────────────────────── api ─────────────────────────────
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  if (r.status === 401 && path !== "/api/login") { renderLogin(); throw new Error("login required"); }
  let d;
  try { d = await r.json(); } catch { throw new Error(`HTTP ${r.status}`); }
  if (!d.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
const post = (p, body = {}) => api(p, { method: "POST", body });
const put = (p, body) => api(p, { method: "PUT", body });

function toast(msg, color = "", ms = 5000, onclick) {
  const el = h("div", { class: "toast " + color, onclick: () => { el.remove(); onclick && onclick(); } }, msg);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}
/** Run an action; show its error as a toast. Returns the result, or undefined on failure. */
async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg, "green", 3000);
    return r;
  } catch (e) {
    if (e.message !== "login required") toast(e.message, "red", 8000);
  }
}

function modal({ title, body, foot, wide, onclose }) {
  const ov = h("div", { class: "overlay" });
  const close = () => { ov.remove(); document.removeEventListener("keydown", esc); onclose && onclose(); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(); });
  const m = h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
    h("div", { class: "modal-head" }, h("h2", {}, title),
      h("button", { class: "btn ghost icon", onclick: close, "aria-label": "Close" }, "✕")),
    h("div", { class: "modal-body" }, body),
    foot ? h("div", { class: "modal-foot" }, typeof foot === "function" ? foot(close) : foot) : null);
  ov.append(m);
  document.body.append(ov);
  return { close, el: m };
}
function confirmBox(title, text, ok = "OK", kind = "primary") {
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title, body: h("p", { style: "margin:0;color:var(--muted)" }, text),
      foot: (close) => [
        h("button", { class: "btn", onclick: () => close() }, "Cancel"),
        h("button", { class: "btn " + kind, onclick: () => { done = true; close(); } }, ok)],
      onclose: () => resolve(done),
    });
    $(".btn." + kind, m.el)?.focus();
  });
}

// ───────────────────────────── vocabulary ─────────────────────────────
const STAGE = {
  queued: ["waiting", ""], copying: ["copying", "blue"], ready: ["copied", ""], encoding: ["encoding", "blue"],
  paused: ["paused", "amber"], verifying: ["checking", "blue"], awaiting: ["needs you", "amber"],
  to_replace: ["replacing", "blue"], replacing: ["replacing", "blue"], replaced: ["replaced", "green"],
  kept: ["kept both", "green"], discarded: ["discarded", ""], cancelled: ["cancelled", ""],
  skipped: ["skipped", ""], failed: ["failed", "red"],
};
const RUNNING = ["copying", "ready", "encoding", "paused", "verifying", "to_replace", "replacing"];
const stageBadge = (s) => { const [t, c] = STAGE[s] || [s, ""]; return h("span", { class: "badge " + c }, t); };
const originBadge = (o) => (o === "auto" ? h("span", { class: "badge outline" }, "auto")
  : o === "review" ? h("span", { class: "badge outline" }, "you said go") : null);
const MODE = { off: ["Off", ""], dry: ["Dry run", "blue"], on: ["On", "green"] };

function codecBar(codecs, size, width = "") {
  const items = Object.entries(codecs || {}).sort((a, b) => b[1] - a[1]);
  const shades = ["var(--shade-1)", "var(--shade-2)", "var(--shade-3)", "var(--shade-4)"];
  return {
    bar: h("div", { class: "stackbar", style: width ? `width:${width};flex:none` : "" },
      items.map(([c, b], i) => h("i", { style: `width:${(b / Math.max(1, size)) * 100}%;background:${shades[Math.min(i, 3)]}`, title: `${c} ${fsize(b)}` }))),
    legend: h("div", { class: "legend" }, items.map(([c, b], i) =>
      h("span", {}, h("i", { style: `background:${shades[Math.min(i, 3)]}` }), `${c} ${fsize(b)}`))),
    top: items[0] ? items[0][0] : "",
  };
}
function bar(frac, color = "") {
  return h("div", { class: "bar " + color }, h("i", { style: `width:${Math.max(0, Math.min(1, frac || 0)) * 100}%` }));
}

// ───────────────────────────── app state / shell ─────────────────────────────
const S = { info: null, live: null, page: null, presets: null, presetsAt: 0 };
const NAV = [["dashboard", "Dashboard"], ["library", "Library"], ["queue", "Queue"], ["review", "Review"],
  ["automation", "Automation"], ["presets", "Presets"], ["history", "History"], ["settings", "Settings"]];

async function getPresets(force) {
  if (force || !S.presets || Date.now() - S.presetsAt > 30000) {
    const d = await api("/api/presets");
    S.presets = d; S.presetsAt = Date.now();
  }
  return S.presets;
}

function renderShell() {
  const nav = h("nav", { class: "nav" }, NAV.map(([id, label]) =>
    h("a", { href: "#/" + id, "data-id": id }, icon(id), h("span", { class: "label" }, label),
      h("span", { class: "count hidden", "data-count": id }))));
  const side = h("aside", { class: "side" },
    h("div", { class: "brand" }, h("div", { class: "brand-mark" }, "r"), h("span", {}, "recast")), nav,
    h("div", { class: "side-foot" },
      h("span", { class: "conn" }, h("i", { class: "dot", id: "conn-dot" }), h("span", { id: "conn-text" }, "connecting…")),
      h("span", { id: "machine", class: "trunc" })));
  const top = h("header", { class: "topbar" },
    h("a", { href: "#/automation", id: "tb-auto", class: "badge", style: "text-decoration:none" }),
    h("span", { id: "tb-auto-status", class: "muted trunc", style: "max-width:340px;font-size:13px" }),
    h("span", { class: "spacer" }),
    h("span", { class: "stat", id: "tb-encoding" }),
    h("span", { class: "stat" }, "Saved", h("b", { id: "tb-saved", class: "green" }, "—")),
    h("span", { class: "stat hide-sm" }, "Scratch", h("b", { id: "tb-scratch" }, "—")),
    h("button", { class: "btn sm", id: "tb-hold", onclick: toggleHold }, "Pause"));
  const main = h("div", { class: "main" }, top, h("div", { id: "banners" }), h("div", { class: "page", id: "page" }));
  $("#app").replaceChildren(h("div", { class: "shell" }, side, main));
  const m = S.info.machine || {};
  $("#machine").textContent = [m.host, S.info.hevc_encoder].filter(Boolean).join(" · ");
  $("#machine").title = [m.os, m.cpu, m.gpu].filter(Boolean).join(" · ");
}

async function toggleHold() {
  const hold = !(S.live && S.live.hold);
  const d = await act(() => post("/api/queue/hold", { hold }));
  if (d) toast(d.hold ? "Paused — nothing new will start; the running encode is paused too." : "Resumed", "", 4000);
}

function updateChrome(live) {
  const setCount = (id, n, color) => {
    const el = $(`[data-count="${id}"]`);
    if (!el) return;
    el.textContent = n; el.className = "count " + (color || "") + (n ? "" : " hidden");
  };
  const busy = live.active.length;
  setCount("queue", busy + live.queued, busy ? "blue" : "");
  setCount("review", live.inbox + live.review, live.inbox + live.review ? "amber" : "");
  const [label, color] = MODE[live.automation.mode] || MODE.off;
  const a = $("#tb-auto");
  if (a) {
    a.className = "badge " + color;
    a.textContent = "Automation " + label.toLowerCase();
    $("#tb-auto-status").textContent = live.automation.mode === "off" ? "" : live.automation.status;
    $("#tb-saved").textContent = fsize(live.saved);
    $("#tb-scratch").textContent = fsize(live.scratch);
    const enc = live.active.find((j) => j.stage === "encoding");
    const te = $("#tb-encoding");
    te.replaceChildren(...(enc ? [h("i", { class: "dot blue pulse" }), h("b", { class: "trunc", style: "max-width:220px" }, enc.name), pct(enc.progress)] : []));
    const hb = $("#tb-hold");
    hb.textContent = live.hold ? "Resume" : "Pause";
    hb.className = "btn sm" + (live.hold ? " go" : "");
  }
  const banners = $("#banners");
  if (banners) {
    const items = [];
    for (const r of live.offline || []) {
      items.push(h("div", { class: "banner red" }, h("i", { class: "dot red" }),
        h("span", {}, h("b", {}, "Library unreachable: "), r, " — work that needs it waits until it's back. Nothing is lost.")));
    }
    if (S.info && S.info.exposed) {
      items.push(h("div", { class: "banner amber" }, h("i", { class: "dot amber" }),
        h("span", {}, "recast is reachable from your network without a password. ", h("a", { href: "#/settings" }, "Set one in Settings"), ".")));
    }
    banners.replaceChildren(...items);
  }
}

function setConn(ok) {
  const d = $("#conn-dot"), t = $("#conn-text");
  if (!d) return;
  d.className = "dot " + (ok ? "green" : "red");
  t.textContent = ok ? "connected" : "reconnecting…";
}

let es = null;
function connect() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false);
  es.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "live") {
      const prev = S.live;
      S.live = m;
      setConn(true);
      updateChrome(m);
      if (S.page && S.page.onLive) S.page.onLive(m, prev);
    } else if (m.type === "event") onEvent(m);
  };
}

const go = (hash) => () => { location.hash = hash; };
const refreshInfo = debounce(async () => { try { S.info = { ...S.info, ...(await api("/api/state")) }; } catch { /* next tick */ } }, 1500);
function onEvent(m) {
  const j = m.job || {};
  const auto = j.origin === "auto";
  switch (m.kind) {
    case "finished": toast(`Ready to review: ${j.name}`, "amber", 9000, go("#/review")); break;
    case "flagged": toast(`Needs a look: ${j.name} — ${j.flag}`, "amber", 12000, go("#/review")); break;
    case "auto_done": if (j.stage === "awaiting") toast(`Automation: ${j.name} is waiting for you — ${j.flag || j.note}`, "amber", 12000, go("#/review")); break;
    case "failed": toast(`Failed: ${j.name} — ${j.error}`, "red", 15000, go("#/queue")); break;
    case "replaced": refreshInfo(); toast(`${auto ? "Automation replaced" : "Replaced"} ${j.name} · saved ${fsize(j.src_size - j.out_size)}`, "green", 5000); break;
    case "batch_first": toast(`First file of ${m.batch.name} is done — check it before the rest finish`, "amber", 12000, go("#/review")); break;
    case "batch_done": toast(`${m.batch.name}: every file is encoded and waiting for your OK`, "amber", 12000, go("#/review")); break;
    case "batch_replaced": toast(`${m.batch.name}: all replaced`, "green"); break;
    case "offline": toast(`Library unreachable: ${m.text}`, "red", 12000); break;
    case "online": toast(`Library is back: ${m.text}`, "green"); break;
    case "no_space": toast(`Not enough free space in scratch for ${j.name} (needs ${fsize(m.need)}, ${fsize(m.free)} free)`, "red", 15000); break;
    case "budget": toast("Scratch budget reached — approve or discard finished encodes so more can start", "amber", 12000, go("#/review")); break;
  }
  if (S.page && S.page.onEvent) S.page.onEvent(m);
}

// ───────────────────────────── router ─────────────────────────────
const PAGES = {};
async function route() {
  const [p, qs] = location.hash.replace(/^#\/?/, "").split("?");
  let id = p || "dashboard";
  if (S.info.needs_setup && id !== "setup") { location.hash = "#/setup"; return; }
  if (!PAGES[id]) id = "dashboard";
  if (S.page) { S.page.alive = false; S.page.timers.forEach(clearInterval); }
  $$(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.id === (id === "browse" ? "library" : id)));
  const el = $("#page") || h("div");  // setup draws over the whole window
  el.replaceChildren();
  const pg = {
    id, el, params: new URLSearchParams(qs || ""), timers: [], alive: true,
    every(ms, fn) { this.timers.push(setInterval(() => { if (this.alive) fn(); }, ms)); },
  };
  S.page = pg;
  window.scrollTo(0, 0);
  try { await PAGES[id](pg); } catch (e) {
    if (pg.alive && e.message !== "login required") el.append(h("div", { class: "banner red", style: "margin:0" }, e.message));
  }
}

function pageHead(title, sub, ...actions) {
  return h("div", { class: "page-head" }, h("div", { class: "grow" }, h("h1", {}, title), sub ? h("p", {}, sub) : null), ...actions);
}
function card(title, ...kids) {
  return h("div", { class: "card" }, title ? (typeof title === "string" ? h("h2", {}, title) : title) : null, ...kids);
}

// ───────────────────────────── now encoding ─────────────────────────────
function frameImg(j) {
  const img = h("img", { alt: "" });
  img.style.visibility = "hidden";
  img.onload = () => { img.style.visibility = ""; };
  img._load = (jid) => {
    const n = new Image();
    n.onload = () => { img.src = n.src; img.style.visibility = ""; };
    n.src = `/api/jobs/${jid}/frame.jpg?t=${Date.now()}`;
    img._t = Date.now();
  };
  return img;
}

function stepsFor(j) {
  const steps = (j.remote ? [["copy", "Copy"]] : []).concat([["encode", "Encode"], ["check", "Check"], ["replace", "Replace"]]);
  const cur = { queued: -1, copying: "copy", ready: "copy", encoding: "encode", paused: "encode", verifying: "check", to_replace: "replace", replacing: "replace" }[j.stage];
  const idx = steps.findIndex((s) => s[0] === cur);
  return h("div", { class: "pipeline" }, steps.map((s, i) => h("span", { class: i < idx ? "done" : i === idx ? "on" : "" }, (i < idx ? "✓ " : "") + s[1])));
}

function jobProgress(j) {
  if (j.stage === "copying") return [j.copied / Math.max(1, j.src_size), `copying from the library · ${fsize(j.copied)} of ${fsize(j.src_size)}`];
  if (j.stage === "ready") return [0, "copied — waiting for the encoder"];
  if (j.stage === "verifying") return [1, "checking the result (duration, streams, decode test)"];
  if (j.stage === "to_replace" || j.stage === "replacing") return [j.replace_done / Math.max(1, j.out_size), `writing back · ${fsize(j.replace_done)} of ${fsize(j.out_size)}`];
  const bits = [pct(j.progress), j.phase, j.fps ? `${j.fps} fps` : "", j.speed ? `${j.speed}×` : "", j.eta ? `${fdur(j.eta)} left` : ""];
  if (j.stage === "paused") bits.push("paused");
  return [j.progress, bits.filter(Boolean).join(" · ")];
}

/** Keeps one block per running job and updates it in place (so the frame doesn't flicker). */
function nowView(box, emptyFn) {
  const blocks = new Map();
  return (jobs) => {
    jobs = jobs.filter((j) => j.stage !== "queued");
    for (const [id, b] of blocks) if (!jobs.find((j) => j.id === id)) { b.el.remove(); blocks.delete(id); }
    if (!jobs.length) {  // redraw the empty state only when its text changes (keeps its links clickable)
      const node = emptyFn();
      if (box._empty !== node.textContent) { box.replaceChildren(node); box._empty = node.textContent; }
      return;
    }
    box.querySelector(".empty")?.remove();
    box._empty = null;
    for (const j of jobs) {
      let b = blocks.get(j.id);
      if (!b) {
        const img = frameImg(j);
        b = {
          img, frame: h("div", { class: "frame" }, img, h("span", { class: "frame-msg" })),
          title: h("b", { class: "trunc grow" }), origin: h("span"), sub: h("div", { class: "muted trunc", style: "font-size:12.5px" }),
          steps: h("div"), bar: bar(0, "blue"), metrics: h("div", { class: "metrics" }), sizes: h("div", { class: "metrics" }),
          cancel: h("button", { class: "btn sm danger", onclick: async () => {
            if (await confirmBox("Cancel this job?", `${j.name} — the library file is not touched.`, "Cancel job", "danger"))
              act(() => post(`/api/jobs/${j.id}/cancel`));
          } }, "Cancel"),
        };
        b.el = h("div", { class: "now" }, b.frame,
          h("div", { style: "min-width:0" }, h("div", { class: "row" }, b.title, b.origin), b.sub, b.steps, b.bar, b.metrics, b.sizes,
            h("div", { class: "row", style: "margin-top:10px" }, b.cancel)));
        blocks.set(j.id, b);
        box.append(b.el);
      }
      b.title.textContent = j.name; b.title.title = j.src;
      b.origin.replaceChildren(originBadge(j.origin) || "");
      b.sub.textContent = `${j.preset} · ${j.encoder} · ${j.codec_from} → ${j.codec_to}`;
      b.steps.replaceChildren(stepsFor(j));
      const [frac, text] = jobProgress(j);
      b.bar.firstChild.style.width = `${Math.min(1, frac) * 100}%`;
      b.metrics.textContent = text;
      const proj = j.projected || j.out_size;
      b.sizes.replaceChildren(fsize(j.src_size), " → ", h("b", {}, proj ? (j.stage === "encoding" ? "~" : "") + fsize(proj) : "…"),
        proj ? h("span", { class: "green" }, ` (−${pct(saving(j.src_size, proj))})`) : "");
      const msg = b.frame.querySelector(".frame-msg");
      const showFrame = j.has_frame && ["encoding", "paused", "verifying"].includes(j.stage);
      msg.textContent = showFrame ? "" : j.stage === "copying" ? "copying…" : j.stage === "ready" ? "next up" : j.stage.startsWith("replac") || j.stage === "to_replace" ? "writing back…" : "starting…";
      msg.style.position = showFrame ? "" : "absolute";
      if (showFrame && j.stage === "encoding" && (!b.img._t || Date.now() - b.img._t > 2000)) b.img._load(j.id);
      if (!showFrame) b.img.style.visibility = "hidden";
    }
  };
}

// ───────────────────────────── dashboard ─────────────────────────────
PAGES.dashboard = async (pg) => {
  const info = S.info;
  pg.el.append(pageHead("Dashboard", null));
  const tiles = h("div", { class: "tiles" });
  const nowBox = h("div");
  const nextBox = h("div", { class: "list" });
  const logBox = h("div", { class: "list log" });
  const chartBox = h("div");
  pg.el.append(tiles, h("div", { class: "grid cols-main" },
    h("div", { class: "stack" },
      card(h("div", { class: "card-head" }, h("h2", {}, "Now"), h("a", { href: "#/queue", class: "btn ghost sm" }, "Queue →")), nowBox),
      card(h("div", { class: "card-head" }, h("h2", {}, "Space saved per day"), h("span", { class: "muted", style: "font-size:12.5px" }, "last 30 days")), chartBox)),
    h("div", { class: "stack" },
      card(h("div", { class: "card-head" }, h("h2", {}, "Up next"), h("a", { href: "#/automation", class: "btn ghost sm" }, "Automation →")), nextBox),
      card("Activity", logBox))));

  let hist = [];
  const tile = (label, value, sub, cls = "", href) => h(href ? "a" : "div", { class: "tile", href },
    h("div", { class: "label" }, label), h("div", { class: "value " + cls }, value), h("div", { class: "sub trunc" }, sub));
  const drawTiles = () => {
    const l = S.live || { saved: 0, inbox: 0, review: 0, automation: { mode: "off", status: "" } };
    const n = hist.filter((x) => !x.restored).length;
    const [ml, mc] = MODE[l.automation.mode] || MODE.off;
    const need = l.inbox + l.review;
    tiles.replaceChildren(
      tile("Space saved", fsize(l.saved), n ? `${num(n)} files re-encoded` : "nothing replaced yet", "green", "#/history"),
      tile("Could still free", info.potential ? fsize(info.potential) : info.scanned ? "0" : "—",
        info.potential ? `with ${info.default_preset}` : info.scanned ? "nothing left worth encoding" : "scan the library to see", "", "#/library"),
      tile("Automation", ml, l.automation.mode === "off" ? "turn it on to keep the library optimized" : l.automation.status, mc, "#/automation"),
      tile("Needs you", String(need), need ? `${l.inbox} to approve · ${l.review} borderline` : "nothing waiting", need ? "amber" : "", "#/review"));
  };
  const drawNow = nowView(nowBox, () => {
    const l = S.live;
    if (l && l.automation.mode === "on") return h("div", { class: "empty" }, `Nothing running — automation is ${l.automation.status}.`);
    return h("div", { class: "empty" }, "Nothing running.", h("div", { style: "margin-top:8px" },
      h("a", { href: "#/library" }, "Pick a show in the Library"), " or ", h("a", { href: "#/automation" }, "let automation handle it"), "."));
  });
  const drawNext = (a) => {
    if (!a.queue.length) {
      nextBox.replaceChildren(h("div", { class: "empty" }, a.settings.auto_mode === "off" ? "Automation is off." : "Nothing above your threshold right now."));
      return;
    }
    nextBox.replaceChildren(...a.queue.slice(0, 6).map((p) => h("div", { class: "item" },
      h("div", { class: "grow" }, h("div", { class: "trunc" }, base(p.path)), h("div", { class: "dim trunc", style: "font-size:12px" }, parentName(p.path) + " · " + p.preset)),
      h("span", { class: "num green nowrap" }, "−" + pct(p.pct)))),
      a.queue_total > 6 ? h("div", { class: "item dim", style: "font-size:12.5px" }, `+ ${num(a.queue_total - 6)} more`) : "");
  };
  const drawLog = (log) => {
    logBox.replaceChildren(...(log.length ? log.slice(0, 12).map(logItem) : [h("div", { class: "empty" }, "No activity yet.")]));
  };

  const [a, hs] = await Promise.all([api("/api/automation"), api("/api/history")]);
  if (!pg.alive) return;
  hist = hs.history;
  drawTiles(); drawNow((S.live && S.live.active) || []); drawNext(a); drawLog(a.log);
  chartBox.append(savingsChart(hist));
  pg.onLive = (l) => { drawTiles(); drawNow(l.active); };
  pg.every(10000, async () => {
    const a2 = await api("/api/automation").catch(() => null);
    if (a2 && pg.alive) { drawNext(a2); drawLog(a2.log); }
  });
  pg.onEvent = async (m) => {
    if (m.kind === "replaced") { const h2 = await api("/api/history").catch(() => null); if (h2 && pg.alive) { hist = h2.history; drawTiles(); chartBox.replaceChildren(savingsChart(hist)); } }
  };
};

function savingsChart(hist) {
  const days = 30, now = new Date(); now.setHours(0, 0, 0, 0);
  const buckets = Array.from({ length: days }, (_, i) => ({ d: new Date(now.getTime() - (days - 1 - i) * 86400000), v: 0, n: 0 }));
  for (const x of hist) {
    if (x.restored || !x.when) continue;
    const d = new Date(x.when * 1000); d.setHours(0, 0, 0, 0);
    const i = days - 1 - Math.round((now - d) / 86400000);
    if (i >= 0 && i < days) { buckets[i].v += (x.src_size || 0) - (x.out_size || 0); buckets[i].n++; }
  }
  const max = Math.max(...buckets.map((b) => b.v));
  if (max <= 0) return h("div", { class: "empty" }, "Nothing replaced in the last 30 days.");
  const W = 600, H = 150, pad = 22, bw = (W - pad) / days;
  const ns = "http://www.w3.org/2000/svg";
  const el = (t, a) => { const e = document.createElementNS(ns, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", preserveAspectRatio: "none", role: "img", "aria-label": "Space saved per day" });
  svg.append(el("line", { x1: pad, x2: W, y1: H - 16, y2: H - 16, class: "axis" }));
  const g = el("g", { class: "bar-g" });
  buckets.forEach((b, i) => {
    if (!b.v) return;
    const bh = Math.max(2, (b.v / max) * (H - 30));
    const r = el("rect", { x: pad + i * bw + 2, y: H - 16 - bh, width: Math.max(2, bw - 4), height: bh, rx: 2 });
    const t = el("title", {}); t.textContent = `${b.d.toLocaleDateString()}: ${fsize(b.v)} saved · ${b.n} files`;
    r.append(t); g.append(r);
  });
  svg.append(g);
  const label = (x, txt, anchor) => { const t = el("text", { x, y: H - 3, "text-anchor": anchor }); t.textContent = txt; svg.append(t); };
  const fmt = (d) => d.toLocaleDateString([], { month: "short", day: "numeric" });
  label(pad, fmt(buckets[0].d), "start"); label(W, "today", "end");
  const top = el("text", { x: 0, y: 12 }); top.textContent = fsize(max); svg.append(top);
  return svg;
}

function logItem(e) {
  const name = e.path ? base(e.path) : "";
  const map = {
    encode: ["blue", e.reviewed ? "Encoding (you said go)" : "Started", `${e.preset} · est. −${pct(e.pct)}`],
    replaced: ["green", "Replaced", `saved ${fsize(e.saved)} (−${pct(e.pct)})`],
    "needs-you": ["amber", "Waiting for you", e.why || ""],
    failed: ["red", "Failed", e.error || ""],
    skip: ["", "Skipped", e.reason || ""],
    webhook: ["", `${(e.source || "").replace(/^./, (c) => c.toUpperCase())} imported`, "looking at it shortly"],
    "webhook-test": ["green", `${(e.source || "").replace(/^./, (c) => c.toUpperCase())} test webhook received`, "connection works"],
    "webhook-ignored": ["amber", "Webhook ignored", e.reason || ""],
    new: ["", "New file", e.verdict === "auto" ? `queued (est. −${pct(e.pct)})` : e.verdict === "review" ? `borderline (est. −${pct(e.pct)})` : "already efficient"],
    sweep: ["", "Library sweep", e.counts ? `${num(e.counts.auto)} to encode · ${num(e.counts.review)} borderline · ${num(e.counts.skip)} skipped` : ""],
  };
  const [color, what, detail] = map[e.action] || ["", e.action, ""];
  return h("div", { class: "item" }, h("time", {}, clock(e.t)), h("i", { class: "dot " + color, style: "margin-top:6px" }),
    h("div", { class: "grow", style: "min-width:0" }, h("div", { class: "trunc" }, h("b", { style: "font-weight:500" }, what), name ? " · " + name : ""),
      detail ? h("div", { class: "dim trunc", style: "font-size:12px" }, detail) : null));
}

// ───────────────────────────── library ─────────────────────────────
PAGES.library = async (pg) => {
  const info = S.info;
  if (!info.roots.length) { pg.el.append(h("div", { class: "empty" }, "No library folders yet. ", h("a", { href: "#/settings" }, "Add one in Settings"), ".")); return; }
  let root = store.get("root", info.roots[0].path);
  if (!info.roots.find((r) => r.path === root)) root = info.roots[0].path;
  let rank = store.get("rank", "default");
  let q = "", limit = 300, data = null, wasScanning = false;

  const rootSel = h("select", { onchange: (e) => { root = e.target.value; store.set("root", root); load(); } },
    info.roots.map((r) => h("option", { value: r.path, selected: r.path === root }, r.name)));
  const rankSel = h("select", { onchange: (e) => { rank = e.target.value; store.set("rank", rank); load(); }, title: "Rank shows by" });
  const search = h("input", { type: "search", placeholder: "Filter shows…", oninput: debounce((e) => { q = e.target.value.toLowerCase(); limit = 300; draw(); }, 120) });
  const scanBtn = h("button", { class: "btn", onclick: async () => {
    if (await act(() => post("/api/library/scan", { root, force: true }))) toast("Re-listing the library — only new or changed files get their headers read.", "", 5000);
  } }, "Rescan");
  const scanLine = h("span", { class: "muted", style: "font-size:13px" });
  const summary = h("div", { class: "muted", style: "margin:-6px 0 14px" });
  const table = h("div");
  const dupBox = h("div");
  pg.el.append(pageHead("Library", "Shows ranked by how much space re-encoding would free."),
    h("div", { class: "row wrap", style: "margin-bottom:14px" }, info.roots.length > 1 ? rootSel : null, rankSel, search, scanBtn, scanLine),
    summary, card(null, table), dupBox);

  const drawScan = (l) => {
    const s = l && l.scan;
    if (!s) { scanLine.replaceChildren(data && data.when ? `listed ${ago(data.when)}` : ""); scanBtn.disabled = false; return; }
    scanBtn.disabled = true;
    scanLine.replaceChildren(h("i", { class: "dot blue pulse" }), " ",
      s.phase === "listing" ? `Listing files… ${num(s.done)} found` : `Reading headers ${num(s.done)} / ${num(s.total)}`);
  };

  function draw() {
    if (!data) return;
    const shows = data.shows.filter((s) => !q || s.name.toLowerCase().includes(q));
    const total = data.shows.reduce((a, s) => a + s.size, 0), saves = data.shows.reduce((a, s) => a + s.saves, 0);
    summary.replaceChildren(`${num(data.shows.length)} shows · ${fsize(total)}`, saves ? h("span", {}, " · could free ", h("b", { class: "green" }, fsize(saves)), ` (${pct(saves / Math.max(1, total))})`) : "",
      ` · ranked by ${data.rank_label}`);
    if (!data.shows.length) {
      table.replaceChildren(h("div", { class: "empty" }, data.scan ? "Scanning… results appear as headers are read." : "Nothing here yet — hit Rescan."));
      return;
    }
    const rows = shows.slice(0, limit).map((s) => {
      const cb = codecBar(s.codecs, s.size, "72px");
      return h("tr", { class: "click", onclick: () => { location.hash = "#/browse?path=" + enc(s.path); } },
        h("td", { class: "name" }, h("div", { class: "trunc" }, s.name, s.dup ? h("span", { class: "badge", style: "margin-left:6px", title: "Another folder looks like the same show" }, "split") : ""),
          h("div", { class: "sub" }, `${num(s.files)} files`)),
        h("td", { class: "num" }, fsize(s.size)),
        h("td", { class: "hide-sm" }, h("div", { class: "row", style: "gap:8px" }, cb.bar, h("span", { class: "codec dim" }, cb.top))),
        h("td", { class: "hide-sm muted trunc", style: "max-width:220px" }, s.saves ? s.best : ""),
        h("td", { class: "num" }, s.saves ? h("span", { class: "green" }, "−" + fsize(s.saves)) : h("span", { class: "dim" }, "—"),
          s.saves ? h("div", { class: "sub" }, pct(s.saves / Math.max(1, s.size)) + (s.measured ? " · measured" : "")) : ""),
        h("td", { class: "num hide-sm" }, s.done ? h("span", { class: "green" }, "✓ " + s.done) : ""));
    });
    table.replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Show"), h("th", { class: "num" }, "Size"), h("th", { class: "hide-sm" }, "Codecs"),
        h("th", { class: "hide-sm" }, "Best preset"), h("th", { class: "num" }, "Could free"), h("th", { class: "num hide-sm" }, "Done"))),
      h("tbody", {}, rows))),
    shows.length > limit ? h("div", { style: "text-align:center;margin-top:10px" }, h("button", { class: "btn", onclick: () => { limit += 500; draw(); } }, `Show more (${num(shows.length - limit)})`)) : "");
    dupBox.replaceChildren(data.duplicates.length ? h("details", { class: "card", style: "margin-top:14px" },
      h("summary", {}, `${data.duplicates.length} shows are split across several folders`),
      h("div", { class: "list", style: "margin-top:10px" }, data.duplicates.map((d) => h("div", { class: "item" },
        h("b", { style: "font-weight:500;min-width:180px" }, d.title), h("span", { class: "muted trunc" }, d.folders.join("  ·  ")))))) : "");
  }

  async function load() {
    const d = await api(`/api/library/overview?root=${enc(root)}&rank=${enc(rank)}`);
    if (!pg.alive) return;
    data = d;
    const p = await getPresets();
    rankSel.replaceChildren(h("option", { value: "default" }, `Your default (${info.default_preset})`), h("option", { value: "best" }, "Best preset per show"),
      ...p.presets.map((x) => h("option", { value: x.name }, x.name)));
    rankSel.value = d.rank;
    draw(); drawScan(S.live);
  }
  await load();
  let lastLoad = Date.now();
  pg.onLive = (l) => {
    drawScan(l);
    const scanning = !!l.scan;
    if ((wasScanning && !scanning) || (scanning && Date.now() - lastLoad > 6000)) { lastLoad = Date.now(); load(); }
    wasScanning = scanning;
  };
};

// ───────────────────────────── browse ─────────────────────────────
PAGES.browse = async (pg) => {
  const path = pg.params.get("path");
  if (!path) { location.hash = "#/library"; return; }
  const d = await api("/api/library/browse?path=" + enc(path));
  if (!pg.alive) return;
  const crumbs = h("div", { class: "crumbs" }, h("a", { href: "#/library" }, "Library"),
    d.crumbs.map((c) => [h("span", {}, "/"), h("a", { href: "#/browse?path=" + enc(c.path) }, c.name)]));
  const list = h("div", { class: "list", style: "gap:1px" });
  const details = h("div", { class: "card details" });
  pg.el.append(crumbs, h("div", { class: "browse" }, card(null, list), details));
  let selected = null;
  for (const x of d.dirs) {
    list.append(h("div", { class: "entry", onclick: () => { location.hash = "#/browse?path=" + enc(x.path); } }, icon("folder"),
      h("span", { class: "trunc" }, x.name),
      h("span", { class: "meta" }, x.saves ? h("span", { class: "green" }, "−" + fsize(x.saves)) : "", x.size != null ? fsize(x.size) : "")));
  }
  for (const f of d.files) {
    const st = f.status === "done" ? h("span", { class: "badge green" }, "✓ recast") : f.status === "busy" ? h("span", { class: "badge blue" }, "in queue")
      : f.status === "review" ? h("span", { class: "badge amber" }, "borderline") : "";
    const row = h("div", { class: "entry", onclick: () => {
      if (selected) selected.classList.remove("on");
      selected = row; row.classList.add("on");
      showDetails(details, f.path, pg);
    } }, icon("file"), h("span", { class: "trunc" }, f.name),
    h("span", { class: "meta" }, st, f.codec ? h("span", { class: "codec" }, f.codec) : "", f.res || "", fsize(f.size)));
    list.append(row);
  }
  if (!d.dirs.length && !d.files.length) list.append(h("div", { class: "empty" }, "No folders or video files here."));
  showDetails(details, path, pg);
};

async function showDetails(box, path, pg) {
  box.dataset.path = path;
  box.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  let d;
  try { d = await api("/api/library/details?path=" + enc(path)); } catch (e) {
    if (box.dataset.path === path) box.replaceChildren(h("div", { class: "empty red" }, e.message));
    return;
  }
  if (!pg.alive || box.dataset.path !== path) return;
  const kids = d.kind === "folder" ? folderDetails(d) : fileDetails(d, box, pg);
  box.replaceChildren(...kids);
  if (d.partial) setTimeout(() => { if (pg.alive && box.dataset.path === path) showDetails(box, path, pg); }, 3000);
}

function gainsTable(d, isFolder, files) {
  if (!d.gains.length) return h("div", { class: "empty" }, "No video files.");
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Preset"), isFolder ? h("th", { class: "num" }, "Files") : null, h("th", { class: "num" }, "Now → after"), h("th", { class: "num" }, "Saves"), h("th", {}))),
    h("tbody", {}, d.gains.map((g) => h("tr", {},
      h("td", {}, h("div", { class: "trunc", style: "max-width:240px" }, g.preset, g.default ? h("span", { class: "badge", style: "margin-left:6px" }, "default") : ""),
        h("div", { class: "sub" }, g.pct == null ? g.why : `${g.encoder}${g.how === "measured" ? " · measured on this show" : ""}`)),
      isFolder ? h("td", { class: "num" }, g.files || "—") : null,
      h("td", { class: "num nowrap" }, g.pct == null ? "—" : `${fsize(g.src)} → ${fsize(g.out)}`),
      h("td", { class: "num" }, g.pct == null ? "" : h("span", { class: g.pct > 0.05 ? "green" : g.pct < 0 ? "red" : "muted" }, (g.pct > 0 ? "−" : "+") + pct(Math.abs(g.pct)))),
      h("td", { class: "right" }, g.pct == null ? "" : h("button", { class: "btn sm", onclick: () => encodeModal(d.path, isFolder, g.preset, files) }, "Encode")))))));
}

function folderDetails(d) {
  const cb = codecBar(d.codecs, d.size);
  const r = d.recast;
  return [
    h("div", { class: "card-head" }, h("h2", { class: "trunc" }, d.name), h("button", { class: "btn primary sm", onclick: () => encodeModal(d.path, true, d.last_used ? d.last_used.preset : null, d.files) }, "Encode…")),
    h("div", { class: "muted", style: "margin-top:4px" }, `${num(d.files)} files · ${fsize(d.size)}`, d.partial ? h("span", { class: "blue" }, ` · reading headers ${num(d.read)}/${num(d.files)}…`) : "",
      d.arr ? ` · ${d.arr.source}: ${d.arr.title}${d.arr.series_type === "anime" ? " (anime)" : ""}` : ""),
    h("div", { style: "margin-top:10px" }, cb.bar, cb.legend),
    r.files ? h("div", { class: "estimate", style: "margin-top:12px" }, h("span", { class: "green" }, `✓ recast re-encoded ${r.files} files here`), ` · ${fsize(r.before)} → ${fsize(r.after)} (−${pct(saving(r.before, r.after))})`) : "",
    d.last_used ? h("div", { class: "dim", style: "margin-top:8px;font-size:12.5px" }, `Last used here: ${d.last_used.preset}`) : "",
    h("h3", { style: "margin:16px 0 4px" }, "What each preset would do"),
    gainsTable(d, true, d.files),
  ];
}

function fileDetails(d, box, pg) {
  const m = d.media;
  const vid = [m.codec, m.width && `${m.width}×${m.height}`, m.fps && `${Number(m.fps).toFixed(3).replace(/\.?0+$/, "")} fps`, m.pix_fmt, m.hdr, m.interlaced && "interlaced"].filter(Boolean).join(" · ");
  const out = [
    h("h2", { class: "trunc", title: d.path }, d.name),
    d.episode ? h("div", { class: "muted", style: "margin-top:2px" }, `S${String(d.episode.season).padStart(2, "0")}E${String(d.episode.episode).padStart(2, "0")} · ${d.episode.title}${d.episode.quality ? " · " + d.episode.quality : ""}`) : "",
    h("div", { class: "kv", style: "margin-top:12px" },
      h("span", {}, "Video"), h("span", {}, vid),
      h("span", {}, "Bitrate"), h("span", { class: "num" }, m.vkbps ? `${num(m.vkbps)} kb/s` : "—"),
      h("span", {}, "Length"), h("span", {}, fdur(m.duration)),
      h("span", {}, "Size"), h("span", { class: "num" }, fsize(m.size)),
      h("span", {}, "Audio"), h("span", {}, m.audio.join(", ") || "none"),
      h("span", {}, "Subtitles"), h("span", { class: "trunc" }, m.subs.join(", ") || "none")),
  ];
  if (d.history) {
    const hh = d.history;
    out.push(h("div", { class: "estimate", style: "margin-top:14px" },
      h("div", {}, h("span", { class: "green" }, "✓ Re-encoded by recast"), ` with ${hh.preset}, ${ago(hh.when)}`),
      h("div", { class: "num muted", style: "margin-top:2px" }, `${fsize(hh.src_size)} → ${fsize(hh.out_size)} (−${pct(saving(hh.src_size, hh.out_size))})`),
      hh.can_restore ? h("button", { class: "btn sm", style: "margin-top:8px", onclick: async () => {
        if (!(await confirmBox("Put the original back?", "The original returns to its place and the re-encode moves to recast's trash.", "Restore original"))) return;
        const r = await act(() => post("/api/history/restore", { path: d.path }));
        if (r) { toast(r.message, "green"); history.back(); }
      } }, "Restore original") : h("div", { class: "dim", style: "font-size:12px;margin-top:4px" }, "The original is no longer in the trash.")));
  }
  if (d.review) {
    out.push(h("div", { class: "estimate", style: "margin-top:14px" },
      h("div", {}, h("span", { class: "amber" }, "Automation wants your call: "), `about −${pct(d.review.pct)} with ${d.review.preset}`),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { class: "btn sm go", onclick: async () => { if (await act(() => post("/api/automation/review/encode", { path: d.path }), "Queued")) showDetails(box, d.path, pg); } }, "Encode it"),
        h("button", { class: "btn sm", onclick: async () => { if (await act(() => post("/api/automation/review/skip", { path: d.path }))) showDetails(box, d.path, pg); } }, "Skip"))));
  }
  if (d.gains.length) out.push(h("h3", { style: "margin:16px 0 4px" }, "What each preset would do"), gainsTable(d, false, 1));
  return out;
}

// ───────────────────────────── encode dialog ─────────────────────────────
const OVERRIDES = [
  ["rate_mode", "Rate control"], ["bitrate", "Bitrate (kb/s)"], ["crf", "Quality (CRF)"], ["resolution", "Max resolution"],
  ["audio", "Audio"], ["bit_depth", "Pixel format"], ["speed", "Speed"], ["two_pass", "Two-pass"],
];

async function encodeModal(path, isFolder, presetName, files) {
  const P = await getPresets();
  let preset = P.presets.find((p) => p.name === presetName) || P.presets.find((p) => p.default) || P.presets[0];
  let overrides = {}, scope = isFolder ? "first" : "one";
  const est = h("div", { class: "estimate" }, "Estimating…");
  const presetSel = h("select", { onchange: (e) => { preset = P.presets.find((p) => p.name === e.target.value); overrides = {}; drawAdv(); estimate(); } },
    P.presets.map((p) => h("option", { value: p.name, selected: p === preset }, p.name + (p.default ? "  (default)" : ""))));
  const adv = h("div", { class: "opts" });
  const settings = () => ({ ...preset.settings, ...overrides });
  function control(key) {
    const sch = P.schema[key] || { options: [] };
    const v = settings()[key];
    const set = (val) => { overrides[key] = val; drawAdv(); estimate(); };
    if (typeof v === "boolean") return h("label", { class: "check", style: "height:32px" }, h("input", { type: "checkbox", checked: v, onchange: (e) => set(e.target.checked) }), "on");
    if (typeof v === "number") return h("input", { type: "number", value: v, step: key === "bitrate" ? 100 : 1, onchange: (e) => set(Number(e.target.value)) });
    return h("select", { onchange: (e) => set(e.target.value) }, sch.options.map((o) => h("option", { value: o, selected: String(o) === String(v) }, o)));
  }
  function drawAdv() {
    const s = settings();
    adv.replaceChildren(...OVERRIDES.filter(([k]) => !(k === "bitrate" && s.rate_mode === "crf") && !(k === "crf" && s.rate_mode !== "crf") && !(k === "two_pass" && s.rate_mode === "crf"))
      .map(([k, label]) => h("label", { class: "field" }, h("span", {}, label), control(k))));
  }
  let seq = 0;
  const estimate = debounce(async () => {
    const my = ++seq;
    est.replaceChildren("Estimating…");
    const body = { path, preset: preset.name };
    if (Object.keys(overrides).length) body.settings = settings();
    try {
      const r = await post("/api/estimate", body);
      if (my !== seq) return;
      const n = scope === "first" ? 1 : r.files;
      const src = scope === "first" && r.files ? r.src / r.files : r.src, out = scope === "first" && r.files ? r.out / r.files : r.out;
      const secs = scope === "first" && r.files ? r.seconds / r.files : r.seconds;
      const skipped = Object.entries(r.skipped || {});
      est.replaceChildren(
        r.files ? h("div", { class: "row", style: "align-items:baseline;gap:12px" },
          h("span", { class: "big " + (r.pct > 0 ? "green" : "red") }, (r.pct > 0 ? "−" : "+") + pct(Math.abs(r.pct))),
          h("span", { class: "num" }, `${fsize(src)} → ~${fsize(out)}`), r.measured ? h("span", { class: "badge" }, "measured on this show") : h("span", { class: "badge" }, "estimate"))
          : h("div", { class: "amber" }, "Nothing here needs encoding with this preset."),
        r.files ? h("div", { class: "muted", style: "margin-top:6px;font-size:13px" },
          `${n} file${n === 1 ? "" : "s"} · ~${fdur(secs)} on ${r.encoder}`, r.quality ? ` · quality: ${r.quality}` : "", r.encoder_note ? h("div", { class: "amber" }, r.encoder_note) : "") : "",
        skipped.length ? h("div", { class: "dim", style: "margin-top:6px;font-size:12.5px" }, "Skipping " + skipped.map(([why, c]) => `${c} ${why}`).join(" · ")) : "");
      start.disabled = !r.files;
      allTitle.textContent = r.files === files ? `All ${files} files` : `The ${r.files} file${r.files === 1 ? "" : "s"} that need it`;
    } catch (e) { if (my === seq) est.replaceChildren(h("span", { class: "red" }, e.message)); }
  }, 250);
  const choice = (val, title, sub) => {
    const el = h("label", { class: "choice" + (scope === val ? " on" : "") },
      h("input", { type: "radio", name: "scope", checked: scope === val, onchange: () => { scope = val; $$(".choice", m.el).forEach((c) => c.classList.toggle("on", c === el)); estimate(); } }),
      h("div", {}, title, h("small", {}, sub)));
    return el;
  };
  const allTitle = h("span", {}, `All ${files} files`);
  const start = h("button", { class: "btn primary", onclick: async () => {
    const body = { path, preset: preset.name, scope: scope === "first" ? "first" : "all" };
    if (Object.keys(overrides).length) body.settings = settings();
    start.disabled = true;
    const r = await act(() => post("/api/encode", body));
    start.disabled = false;
    if (!r) return;
    m.close();
    toast(r.batch ? `Queued ${r.queued} files — you'll approve them once, together.` : "Queued — you'll see it in Review when it's done.", "green", 5000);
    location.hash = "#/queue";
  } }, "Start");
  drawAdv();
  const m = modal({
    title: `Encode ${base(path)}`,
    body: [
      h("label", { class: "field" }, h("span", {}, "Preset"), presetSel),
      isFolder ? h("div", { class: "stack", style: "gap:8px" },
        choice("first", "Try 1 file first", "Encode one, compare it, then decide on the rest. Recommended for a new preset."),
        choice("all", allTitle, "Queued one after another; you approve the whole folder once.")) : null,
      est,
      h("details", {}, h("summary", {}, "Adjust settings for this run"), adv),
    ],
    foot: (close) => [h("button", { class: "btn", onclick: close }, "Cancel"), start],
  });
  estimate();
}

// ───────────────────────────── queue ─────────────────────────────
PAGES.queue = async (pg) => {
  const holdBtn = h("button", { class: "btn", onclick: toggleHold });
  const clearBtn = h("button", { class: "btn", onclick: async () => { const r = await act(() => post("/api/queue/clear")); if (r) { toast(`Cleared ${r.cleared}`, "", 2500); load(); } } }, "Clear finished");
  const box = h("div", { class: "stack" });
  pg.el.append(pageHead("Queue", "One encode at a time; the next file is copied while the current one runs.", holdBtn, clearBtn), box);
  let sig = "";
  const row = (j) => {
    const live = RUNNING.includes(j.stage);
    const [frac, text] = live ? jobProgress(j) : [0, ""];
    const out = j.stage === "encoding" ? j.projected : j.out_size;
    return h("tr", { "data-id": j.id },
      h("td", { class: "name" }, h("div", { class: "row", style: "gap:6px" }, h("span", { class: "trunc" }, j.name), originBadge(j.origin)),
        h("div", { class: "sub trunc" }, j.preset + (j.error ? " · " : ""), j.error ? h("span", { class: "red" }, j.error) : "", j.flag ? h("span", { class: "amber" }, " · " + j.flag) : "", j.note && !j.flag ? h("span", { class: "amber" }, " · " + j.note) : "")),
      h("td", {}, stageBadge(j.stage)),
      h("td", { class: "hide-sm", style: "width:28%" }, live ? h("div", {}, bar(frac, "blue"), h("div", { class: "sub prog" }, text)) : j.finished ? h("span", { class: "sub nowrap" }, ago(j.finished)) : ""),
      h("td", { class: "num nowrap" }, fsize(j.src_size), out ? h("div", { class: "sub" }, (j.stage === "encoding" ? "~" : "→ ") + fsize(out) + (j.src_size ? ` (−${pct(saving(j.src_size, out))})` : "")) : ""),
      h("td", { class: "right nowrap" },
        ["queued", ...RUNNING.filter((s) => !["to_replace", "replacing"].includes(s))].includes(j.stage)
          ? h("button", { class: "btn sm ghost", onclick: async () => { if (await confirmBox("Cancel this job?", j.name + " — the library file is not touched.", "Cancel job", "danger")) { await act(() => post(`/api/jobs/${j.id}/cancel`)); load(); } } }, "Cancel") : "",
        ["failed", "cancelled"].includes(j.stage) ? h("button", { class: "btn sm", onclick: async () => { if (await act(() => post(`/api/jobs/${j.id}/retry`))) load(); } }, "Retry") : "",
        h("button", { class: "btn sm ghost", onclick: () => jobModal(j.id) }, "Details")));
  };
  const section = (title, jobs, empty) => card(h("div", { class: "card-head" }, h("h2", {}, title), h("span", { class: "muted num" }, jobs.length || "")),
    jobs.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("tbody", {}, jobs.map(row)))) : h("div", { class: "empty", style: "padding:10px" }, empty));
  async function load() {
    const d = await api("/api/jobs").catch(() => null);
    if (!d || !pg.alive) return;
    const jobs = d.jobs;
    const live = S.live || {};
    holdBtn.textContent = live.hold ? "Resume" : "Pause";
    holdBtn.className = "btn" + (live.hold ? " go" : "");
    const s2 = jobs.map((j) => j.id + j.stage + (j.error || "")).join("|");
    if (s2 === sig) { // same jobs, same stages: just move the progress bars
      for (const j of jobs) {
        if (!RUNNING.includes(j.stage)) continue;
        const tr = box.querySelector(`tr[data-id="${j.id}"]`);
        if (!tr) continue;
        const [frac, text] = jobProgress(j);
        const b = tr.querySelector(".bar > i"); if (b) b.style.width = `${Math.min(1, frac) * 100}%`;
        const p = tr.querySelector(".prog"); if (p) p.textContent = text;
      }
      return;
    }
    sig = s2;
    const running = jobs.filter((j) => RUNNING.includes(j.stage)).sort((a, b) => a.id - b.id);
    const waiting = jobs.filter((j) => j.stage === "queued").sort((a, b) => a.id - b.id);
    const needs = jobs.filter((j) => j.stage === "awaiting");
    const done = jobs.filter((j) => !RUNNING.includes(j.stage) && !["queued", "awaiting"].includes(j.stage)).slice(0, 150);
    box.replaceChildren(
      section("Running", running, live.hold ? "Paused." : "Nothing running."),
      section("Waiting", waiting, "Nothing waiting."),
      needs.length ? card(h("div", { class: "card-head" }, h("h2", {}, "Waiting for your OK"), h("a", { class: "btn sm", href: "#/review" }, "Review →")),
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("tbody", {}, needs.map(row))))) : "",
      section("Finished", done, "Nothing finished yet."));
  }
  await load();
  pg.every(1500, load);
};

async function jobModal(id) {
  const d = await act(() => api(`/api/jobs/${id}`));
  if (!d) return;
  const j = d.job;
  const hasOut = ["awaiting", "to_replace", "replacing", "replaced", "kept"].includes(j.stage);
  const si = j.src_info || {}, oi = j.out_info || {};
  modal({
    title: j.name, wide: true,
    body: [
      h("div", { class: "row wrap" }, stageBadge(j.stage), originBadge(j.origin), h("span", { class: "muted" }, `${j.preset} · ${j.encoder}`)),
      j.error ? h("div", { class: "banner red", style: "margin:0" }, j.error) : "",
      j.flag ? h("div", { class: "banner amber", style: "margin:0" }, j.flag) : "",
      j.note ? h("div", { class: "banner amber", style: "margin:0" }, j.note) : "",
      hasOut && j.stage === "awaiting" ? compareView(j) : "",
      h("div", { class: "kv" },
        h("span", {}, "Source"), h("span", { class: "trunc", title: j.src }, j.src),
        h("span", {}, "Before"), h("span", { class: "num" }, `${si.codec || "?"} ${si.width || ""}×${si.height || ""} · ${num(si.vkbps)} kb/s · ${fsize(j.src_size)}`),
        hasOut ? h("span", {}, "After") : "", hasOut ? h("span", { class: "num" }, `${oi.codec || j.codec_to} ${oi.width || ""}×${oi.height || ""} · ${num(oi.vkbps || Math.round(j.kbps))} kb/s · ${fsize(j.out_size)} (−${pct(saving(j.src_size, j.out_size))})`) : "",
        h("span", {}, "Audio / subs"), h("span", {}, `${j.audio[0]} → ${hasOut ? j.audio[1] : "…"} audio · ${j.subs[0]} → ${hasOut ? j.subs[1] : "…"} subtitle tracks`),
        j.started ? h("span", {}, "Encode time") : "", j.started ? h("span", {}, fdur((j.finished || Date.now() / 1000) - j.started)) : "",
        j.result ? h("span", {}, "Result") : "", j.result ? h("span", {}, j.result) : ""),
      j.log.length ? h("details", {}, h("summary", {}, "ffmpeg log"), h("pre", { class: "lines", style: "margin-top:8px" }, j.log.join("\n"))) : "",
    ],
  });
}

function compareView(j) {
  let side = "split", pos = 0.35;
  const img = h("img", { alt: "Original and re-encode at the same moment", loading: "lazy" });
  const tagL = h("span", { class: "tag", style: "left:8px" }, "original"), tagR = h("span", { class: "tag", style: "right:8px" }, "re-encode");
  const wait = h("span", { class: "frame-wait" }, "grabbing frames…");
  img.onload = img.onerror = () => wait.classList.add("hidden");
  const wrap = h("div", { class: "compare" }, img, tagL, tagR, wait);
  const load = () => {
    wait.classList.remove("hidden");
    img.src = `/api/jobs/${j.id}/compare.jpg?pos=${pos}&side=${side}`;
    tagL.classList.toggle("hidden", side === "out"); tagR.classList.toggle("hidden", side === "src");
    tagL.textContent = side === "split" ? "◀ original" : "original";
    tagR.textContent = side === "split" ? "re-encode ▶" : "re-encode";
  };
  const seg = h("div", { class: "seg" });
  const drawSeg = () => seg.replaceChildren(...[["split", "Split"], ["src", "Original"], ["out", "Re-encode"]].map(([v, l]) =>
    h("button", { class: side === v ? "on" : "", onclick: () => { side = v; drawSeg(); load(); } }, l)));
  drawSeg(); load();
  const slider = h("input", { type: "range", min: 2, max: 97, value: 35, onchange: (e) => { pos = e.target.value / 100; load(); }, "aria-label": "Position in the video" });
  return h("div", { style: "max-width:760px" }, wrap, h("div", { class: "row", style: "margin-top:8px" }, seg, h("span", { class: "dim", style: "font-size:12px" }, "position"), slider));
}

// ───────────────────────────── review ─────────────────────────────
PAGES.review = async (pg) => {
  const inboxBox = h("div", { class: "stack" });
  const borderBox = h("div");
  pg.el.append(pageHead("Review", "Encodes waiting for your OK, and files automation wasn't sure about."),
    h("div", { class: "stack" }, card(h("div", { class: "card-head" }, h("h2", {}, "Ready to replace")), inboxBox),
      card(h("div", { class: "card-head" }, h("h2", {}, "Borderline files"), h("a", { href: "#/automation", class: "btn ghost sm" }, "Thresholds →")), borderBox)));
  let counts = "";

  const jobItem = (j) => h("div", { class: "card", style: "background:var(--panel)" },
    h("div", { class: "row wrap" }, h("b", { class: "trunc grow" }, j.name), originBadge(j.origin), h("span", { class: "badge" }, j.preset)),
    h("div", { class: "num", style: "margin:6px 0 10px" }, `${fsize(j.src_size)} → `, h("b", {}, fsize(j.out_size)), " ",
      h("span", { class: j.out_size < j.src_size ? "green" : "red" }, `(${j.out_size < j.src_size ? "−" : "+"}${pct(Math.abs(saving(j.src_size, j.out_size)))})`),
      h("span", { class: "muted" }, `  ·  ${j.codec_from} ${num(j.src_info.vkbps)} kb/s → ${j.codec_to} ${num((j.out_info || {}).vkbps || Math.round(j.kbps))} kb/s`)),
    j.flag ? h("div", { class: "amber", style: "margin-bottom:8px" }, "⚠ " + j.flag) : "",
    j.note ? h("div", { class: "amber", style: "margin-bottom:8px" }, j.note) : "",
    compareView(j),
    h("div", { class: "row", style: "margin-top:12px" },
      h("button", { class: "btn go", onclick: async () => { if (await act(() => post(`/api/inbox/job/${j.id}/approve`))) { toast("Replacing — the original goes to recast's trash.", "green"); load(); } } }, j.flag ? "Replace anyway" : "Replace original"),
      h("button", { class: "btn danger", onclick: async () => { if (await act(() => post(`/api/inbox/job/${j.id}/deny`))) load(); } }, "Discard encode"),
      h("span", { class: "dim", style: "font-size:12.5px" }, `waiting ${ago(j.waiting_since)}`)));

  const batchItem = (b) => h("div", { class: "card", style: "background:var(--panel)" },
    h("div", { class: "row wrap" }, h("b", { class: "trunc grow" }, b.name), h("span", { class: "badge" }, b.preset)),
    h("div", { class: "num", style: "margin:6px 0" }, `${b.done} of ${b.files} encoded`, b.active ? ` · ${b.active} still running` : "", " · ",
      `${fsize(b.src)} → `, h("b", {}, fsize(b.out)), " ", h("span", { class: "green" }, `(−${pct(saving(b.src, b.out))})`)),
    b.flagged ? h("div", { class: "amber" }, `⚠ ${b.flagged} file${b.flagged > 1 ? "s" : ""} flagged — they'll still wait for you individually.`) : "",
    h("details", { style: "margin-top:8px" }, h("summary", {}, "Files"),
      h("div", { class: "list", style: "margin-top:6px" }, b.items.map((j) => h("div", { class: "item" },
        h("span", { class: "trunc grow" }, j.name), j.flag ? h("span", { class: "badge amber" }, "flagged") : "",
        h("span", { class: "num muted nowrap" }, `${fsize(j.src_size)} → ${fsize(j.out_size)}`),
        h("button", { class: "btn sm ghost", onclick: () => jobModal(j.id) }, "Compare"))))),
    h("div", { class: "row", style: "margin-top:12px" },
      h("button", { class: "btn go", onclick: async () => { if (await act(() => post(`/api/inbox/batch/${b.id}/approve`))) { toast("Approved — files are replaced as they finish.", "green"); load(); } } }, b.active ? "Approve all (incl. the rest)" : "Replace all"),
      h("button", { class: "btn danger", onclick: async () => { if (await confirmBox("Discard the whole batch?", "Encoded files are deleted from scratch; the library stays as it is.", "Discard", "danger") && await act(() => post(`/api/inbox/batch/${b.id}/deny`))) load(); } }, "Discard all")));

  let reason = null;  // null = every borderline file; otherwise one group
  const reasonLabel = (r, s) => r || `saving between ${pct(s.review_threshold)} and ${pct(s.auto_threshold)}`;
  async function load() {
    const [ib, a] = await Promise.all([api("/api/inbox"), api("/api/automation" + (reason == null ? "" : "?reason=" + enc(reason)))]);
    if (!pg.alive) return;
    inboxBox.replaceChildren(...(ib.items.length ? ib.items.map((it) => (it.kind === "batch" ? batchItem(it) : jobItem(it)))
      : [h("div", { class: "empty" }, "Nothing waiting for approval.")]));
    const s = a.settings;
    const groups = Object.entries(a.review_reasons || {}).sort((x, y) => y[1] - x[1]);
    const all = groups.reduce((n, g) => n + g[1], 0);
    if (reason != null && !groups.find((g) => g[0] === reason)) { reason = null; return load(); }
    if (!all) {
      borderBox.replaceChildren(h("div", { class: "empty" }, s.auto_mode === "off" ? "Automation is off — borderline files show up here when it runs." : "No borderline files."));
    } else {
      const shown = a.review_total;
      borderBox.replaceChildren(
        h("p", { class: "muted", style: "margin:6px 0 10px" }, "Automation won't touch these on its own. Encode the ones you want; skipped files stay skipped until they change."),
        h("div", { class: "row wrap", style: "margin-bottom:12px" },
          h("div", { class: "chips grow" },
            h("button", { class: "chip" + (reason == null ? " on" : ""), onclick: () => { reason = null; load(); } }, `All ${num(all)}`),
            groups.map(([r, n]) => h("button", { class: "chip" + (reason === r ? " on" : ""), onclick: () => { reason = r; load(); } }, `${reasonLabel(r, s)} · ${num(n)}`))),
          h("button", { class: "btn sm danger", onclick: async () => {
            const what = reason == null ? `all ${num(all)} borderline files` : `the ${num(shown)} files that ${reasonLabel(reason, s).replace(/^would /, "would ")}`;
            if (!(await confirmBox("Skip these?", `Skip ${what}. They stay skipped until the file changes (e.g. an upgrade).`, "Skip them", "danger"))) return;
            const r = await act(() => post("/api/automation/review/skip-all", { reason }));
            if (r) { toast(`Skipped ${num(r.skipped)}`, "", 3000); reason = null; load(); }
          } }, reason == null ? "Skip all" : `Skip these ${num(shown)}`)),
        h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", { class: "num" }, "Est. saving"), h("th", { class: "num hide-sm" }, "Now → after"), h("th", { class: "hide-sm" }, "Preset"), h("th", {}))),
          h("tbody", {}, a.review.map((p) => h("tr", {},
            h("td", { class: "name" }, h("div", { class: "trunc" }, base(p.path)),
              h("div", { class: "sub trunc" }, parentName(p.path), p.reason ? h("span", { class: "amber" }, " · " + p.reason) : "")),
            h("td", { class: "num amber" }, "−" + pct(p.pct)),
            h("td", { class: "num hide-sm nowrap" }, `${fsize(p.size)} → ${fsize(p.est_out)}`),
            h("td", { class: "hide-sm muted trunc", style: "max-width:200px" }, p.preset),
            h("td", { class: "right nowrap" },
              h("button", { class: "btn sm", onclick: async () => { if (await act(() => post("/api/automation/review/encode", { path: p.path }), "Queued")) load(); } }, "Encode"),
              h("button", { class: "btn sm ghost", onclick: async () => { if (await act(() => post("/api/automation/review/skip", { path: p.path }))) load(); } }, "Skip"))))))),
        shown > a.review.length ? h("div", { class: "dim", style: "text-align:center;margin-top:10px;font-size:12.5px" }, `Showing the ${a.review.length} biggest of ${num(shown)}.`) : "");
    }
    counts = `${(S.live || {}).inbox}|${(S.live || {}).review}`;
  }
  await load();
  pg.onLive = (l) => { if (`${l.inbox}|${l.review}` !== counts) load(); };
};

// ───────────────────────────── automation ─────────────────────────────
PAGES.automation = async (pg) => {
  const d = await api("/api/automation");
  if (!pg.alive) return;
  const s = { ...d.settings };
  const save = async (patch) => {
    Object.assign(s, patch);
    const r = await act(() => put("/api/automation", patch));
    if (r) { statusLine.textContent = r.status; }
    return r;
  };
  // mode
  const modeHelp = {
    off: "Nothing happens on its own. You encode from the Library.",
    dry: "Decides what it would do and shows it here, but encodes nothing. Good for checking your thresholds first.",
    on: "Encodes clear wins one at a time and replaces them by itself; borderline files wait in Review.",
  };
  const seg = h("div", { class: "seg" });
  const help = h("div", { class: "mode-help" });
  const statusLine = h("span", {}, d.status);
  const drawMode = () => {
    seg.replaceChildren(...["off", "dry", "on"].map((m) => h("button", { class: (s.auto_mode === m ? "on " + MODE[m][1] : ""), onclick: async () => {
      if (m === "on" && s.auto_mode !== "on" && !(await confirmBox("Turn on automation?",
        `recast will encode files whose estimated saving is at least ${pct(s.auto_threshold)} with ${s.auto_preset || d.default_preset}, one at a time, and replace each original once the real result also clears that bar and passes every check. Originals go to recast's trash first.`, "Turn on", "go"))) return;
      if (await save({ auto_mode: m })) drawMode();
    } }, MODE[m][0])));
    help.textContent = modeHelp[s.auto_mode];
  };
  drawMode();

  // presets
  const presetOpts = (sel, blank) => [blank ? h("option", { value: "" }, blank) : null, ...d.presets.map((p) => h("option", { value: p, selected: p === sel }, p))];
  const prefSel = h("select", { onchange: (e) => save({ auto_preset: e.target.value }).then(refreshPreview) }, presetOpts(s.auto_preset, `Default (${d.default_preset})`));
  const animeSel = h("select", { onchange: (e) => save({ auto_preset_anime: e.target.value }).then(refreshPreview) }, presetOpts(s.auto_preset_anime, "Same as above"));

  // thresholds
  const autoR = h("input", { type: "range", min: 5, max: 80, step: 1, value: Math.round(s.auto_threshold * 100) });
  const revR = h("input", { type: "range", min: 0, max: 80, step: 1, value: Math.round(s.review_threshold * 100) });
  const autoV = h("div", { class: "slider-val green" }), revV = h("div", { class: "slider-val amber" });
  const outcome = h("div");
  const syncSliders = (src) => {
    let a = Number(autoR.value), r = Number(revR.value);
    if (r > a) { if (src === autoR) r = a; else a = r; revR.value = r; autoR.value = a; }
    autoV.textContent = a + "%"; revV.textContent = r + "%";
    return [a / 100, r / 100];
  };
  let pseq = 0;
  const refreshPreview = debounce(async () => {
    const [a, r] = syncSliders();
    const my = ++pseq;
    const p = await api(`/api/automation/preview?auto=${a}&review=${r}`).catch(() => null);
    if (!p || my !== pseq || !pg.alive) return;
    const total = p.auto + p.review + p.skip || 1;
    outcome.replaceChildren(
      h("div", { class: "stackbar", style: "margin:14px 0 8px;height:10px" },
        h("i", { style: `width:${(p.auto / total) * 100}%;background:var(--green)` }), h("i", { style: `width:${(p.review / total) * 100}%;background:var(--amber)` }),
        h("i", { style: `width:${(p.skip / total) * 100}%;background:var(--shade-4)` })),
      h("div", { class: "outcome" }, h("i", { style: "background:var(--green)" }), h("span", {}, h("b", {}, num(p.auto)), " files encode and replace on their own"), h("span", { class: "num green" }, "frees ~" + fsize(p.auto_bytes))),
      h("div", { class: "outcome" }, h("i", { style: "background:var(--amber)" }), h("span", {}, h("b", {}, num(p.review)), " wait for you in Review"), h("span", { class: "num amber" }, "~" + fsize(p.review_bytes))),
      h("div", { class: "outcome" }, h("i", { style: "background:var(--shade-4)" }), h("span", {}, h("b", {}, num(p.skip)), " skipped — already efficient or not worth it"), h("span", {})));
  }, 200);
  for (const r of [autoR, revR]) {
    r.addEventListener("input", () => { syncSliders(r); refreshPreview(); });
    r.addEventListener("change", async () => { const [a, rv] = syncSliders(r); await save({ auto_threshold: a, review_threshold: rv }); });
  }
  syncSliders();

  // pacing
  const prefetch = h("input", { type: "checkbox", checked: s.prefetch, onchange: (e) => save({ prefetch: e.target.checked }) });
  const [hFrom, hTo] = (s.auto_hours || "01:00-08:00").split("-");
  const hoursOn = h("input", { type: "checkbox", checked: !!s.auto_hours });
  const from = h("input", { type: "time", value: hFrom }), to = h("input", { type: "time", value: hTo });
  const saveHours = () => save({ auto_hours: hoursOn.checked ? `${from.value || "01:00"}-${to.value || "08:00"}` : "" });
  [hoursOn, from, to].forEach((x) => x.addEventListener("change", saveHours));
  const sweepN = h("input", { type: "number", min: 1, value: s.sweep_hours, onchange: (e) => save({ sweep_hours: Number(e.target.value) || 24 }) });
  const sweepBtn = h("button", { class: "btn sm", onclick: async () => { if (await act(() => post("/api/automation/sweep"))) toast("Sweeping — re-listing folders; only new or changed files are read.", "", 5000); } }, "Sweep now");

  // hooks
  const copyRow = (label, url) => h("div", { class: "field", style: "margin-top:10px" }, h("span", { class: "muted", style: "font-size:12.5px;font-weight:500" }, label),
    h("div", { class: "copy" }, h("code", { title: url }, url), h("button", { class: "btn sm", onclick: async (e) => {
      try { await navigator.clipboard.writeText(url); e.target.textContent = "Copied"; setTimeout(() => { e.target.textContent = "Copy"; }, 1500); }
      catch { toast("Couldn't copy — select the text instead.", "amber"); }
    } }, "Copy")));

  const queueBox = h("div", { class: "list" }), logBox = h("div", { class: "list log" });
  const drawSide = (a) => {
    queueBox.replaceChildren(...(a.queue.length ? a.queue.slice(0, 15).map((p, i) => h("div", { class: "item" },
      h("span", { class: "num dim", style: "width:20px" }, i + 1),
      h("div", { class: "grow" }, h("div", { class: "trunc" }, base(p.path)), h("div", { class: "dim trunc", style: "font-size:12px" }, `${parentName(p.path)} · ${fsize(p.size)} · ${p.preset}`)),
      h("span", { class: "num green nowrap" }, "−" + pct(p.pct)))) : [h("div", { class: "empty" }, "Nothing above your threshold.")]),
    a.queue_total > 15 ? h("div", { class: "item dim", style: "font-size:12.5px" }, `+ ${num(a.queue_total - 15)} more, biggest savings first`) : "");
    logBox.replaceChildren(...(a.log.length ? a.log.slice(0, 40).map(logItem) : [h("div", { class: "empty" }, "No activity yet.")]));
    statusLine.textContent = a.status;
    lastSweep.textContent = a.last_sweep ? `last ${ago(a.last_sweep)}` : "never";
  };
  const lastSweep = h("span", { class: "dim", style: "font-size:12.5px" });
  const fieldRow = (k, sub, ...ctl) => h("div", { class: "field-row" }, h("div", { class: "k" }, k, sub ? h("small", {}, sub) : null), h("div", { class: "row wrap" }, ...ctl));

  pg.el.append(pageHead("Automation", "Keeps the library optimized on its own — one file at a time, so the NAS and your scratch disk never get flooded."),
    h("div", { class: "grid cols-main" },
      h("div", { class: "stack" },
        card(null, h("div", { class: "row wrap" }, seg, h("span", { class: "row", style: "gap:6px;font-size:13px" }, h("i", { class: "dot " + (s.auto_mode === "on" ? "green pulse" : "") }), statusLine)), help),
        card("What gets encoded",
          fieldRow("Preferred preset", "used for everything", prefSel),
          fieldRow("Anime preset", d.arr.sonarr ? "for series Sonarr marks as anime" : "needs Sonarr connected (Settings)", animeSel),
          h("div", { class: "field-row", style: "display:block" },
            h("div", { class: "k" }, "Replace automatically when the saving is at least"),
            h("div", { class: "slider-row" }, autoR, autoV),
            h("div", { class: "k", style: "margin-top:12px" }, "Ask me when it's at least"),
            h("div", { class: "slider-row" }, revR, revV),
            outcome,
            h("p", { class: "dim", style: "font-size:12.5px;margin:10px 0 0" },
              "The score is the estimated saving with your preset (measured from real encodes of the same show once there are some). ",
              "An automatic encode only replaces the original if the ", h("i", {}, "real"), " saving also clears the bar and every check passes; otherwise it waits in Review. ",
              "Files your preset would downscale, or strip of HDR or surround sound, always wait in Review."))),
        card("Pacing",
          fieldRow("Copy ahead", "copy the next file while the current one encodes — at most one waiting", h("label", { class: "check" }, prefetch, "on")),
          fieldRow("Working hours", "only start new files in this window", h("label", { class: "check" }, hoursOn, "only between"), from, "and", to),
          fieldRow("Look for new files", "re-lists folders (cheap); only new or changed files are read", "every", sweepN, "hours", sweepBtn, lastSweep)),
        card("Sonarr & Radarr",
          h("p", { class: "muted", style: "margin:8px 0 0" }, "Point a webhook at recast and new downloads are looked at ~90 s after import, ahead of everything else."),
          copyRow("Sonarr webhook URL", d.hooks.sonarr), copyRow("Radarr webhook URL", d.hooks.radarr),
          h("ol", { class: "steps" },
            h("li", {}, "In Sonarr/Radarr: ", h("b", {}, "Settings → Connect → + → Webhook")),
            h("li", {}, "Paste the URL, method ", h("b", {}, "POST"), ", triggers ", h("b", {}, "On Import"), " and ", h("b", {}, "On Upgrade")),
            h("li", {}, "Press ", h("b", {}, "Test"), " — it shows up in Activity here")),
          !d.arr.sonarr && !d.arr.radarr ? h("p", { class: "amber", style: "font-size:13px;margin:10px 0 0" }, "Also add their URL + API key in ", h("a", { href: "#/settings" }, "Settings"), " so recast can map their paths to yours, spot anime, and trigger a rescan after replacing.") : "")),
      h("div", { class: "stack" },
        card(h("div", { class: "card-head" }, h("h2", {}, "Up next"), h("span", { class: "muted num" }, d.queue_total ? num(d.queue_total) : "")), queueBox),
        card("Activity", logBox))));
  drawSide(d);
  refreshPreview();
  pg.every(5000, async () => { const a = await api("/api/automation").catch(() => null); if (a && pg.alive) drawSide(a); });
};

// ───────────────────────────── presets ─────────────────────────────
const GROUPS = [
  ["Video", ["codec", "encoder", "resolution", "rate_mode", "bitrate", "crf", "speed", "tune", "bit_depth", "two_pass", "maxrate", "bufsize", "hdr", "deinterlace"]],
  ["Audio & subtitles", ["audio", "langs", "subs"]],
  ["Output", ["container", "after", "skip_same", "extra"]],
];
const FIXED = new Set(["codec", "encoder", "resolution", "rate_mode", "audio", "subs", "container", "after", "speed", "tune", "bit_depth"]);

PAGES.presets = async (pg) => {
  const P = await getPresets(true);
  if (!pg.alive) return;
  const listBox = h("div", { class: "list preset-list", style: "gap:1px" });
  const editor = h("div", { class: "card" });
  pg.el.append(pageHead("Presets", "How files get encoded. Encoders this machine can't run are marked ✗.",
    h("button", { class: "btn", onclick: () => edit({ name: "New preset", description: "", settings: { ...(cur ? cur.settings : P.presets[0].settings) } }, true) }, "New preset")),
  h("div", { class: "grid", style: "grid-template-columns:minmax(0,280px) minmax(0,1fr)" }, card(null, listBox), editor));
  if (window.innerWidth < 900) pg.el.lastChild.style.gridTemplateColumns = "minmax(0,1fr)";
  let cur = null;
  const drawList = () => listBox.replaceChildren(...P.presets.map((p) => h("div", { class: "entry" + (cur && cur.name === p.name ? " on" : ""), onclick: () => edit(p) },
    h("div", { class: "row", style: "width:100%" }, h("b", { class: "trunc grow", style: "font-weight:500" }, p.name), p.default ? h("span", { class: "badge" }, "default") : ""),
    h("span", { class: "dim trunc", style: "font-size:12px;max-width:100%" }, p.description || p.encoder))));

  function edit(p, isNew) {
    cur = isNew ? null : p;
    drawList();
    const s = { ...p.settings };
    const name = h("input", { type: "text", value: p.name });
    const desc = h("input", { type: "text", value: p.description || "", placeholder: "what it's for" });
    const form = h("div");
    const json = h("textarea", { rows: 22, class: "hidden", spellcheck: "false" });
    let asJson = false;
    const warn = h("div", { class: "red", style: "font-size:13px" });
    const control = (k) => {
      const sch = P.schema[k] || { options: [], bad: [], doc: "" };
      const v = s[k];
      let el;
      if (typeof v === "boolean") el = h("label", { class: "check", style: "height:32px" }, h("input", { type: "checkbox", checked: v, onchange: (e) => { s[k] = e.target.checked; } }), "on");
      else if (FIXED.has(k)) {
        el = h("select", { onchange: (e) => { s[k] = e.target.value; drawWarn(); } }, sch.options.map((o) => {
          const bad = (sch.bad || []).includes(o);
          return h("option", { value: o, selected: String(o) === String(v), class: bad ? "bad" : "" }, (bad ? "✗ " : "") + o);
        }));
      } else {
        const id = "dl-" + k;
        el = h("span", { style: "display:contents" }, h("input", { type: typeof v === "number" ? "number" : "text", value: v, list: id, style: typeof v === "number" ? "" : "width:100%",
          onchange: (e) => { s[k] = typeof v === "number" ? Number(e.target.value) : e.target.value; } }),
        h("datalist", { id }, sch.options.map((o) => h("option", { value: o }))));
      }
      return h("label", { class: "field" + (k === "extra" ? " full" : "") }, h("span", {}, k.replace(/_/g, " ")), el, h("span", { class: "dim", style: "font-size:11.5px" }, sch.doc));
    };
    const drawWarn = () => {
      const bad = (P.schema.encoder.bad || []).includes(s.encoder);
      warn.textContent = bad ? `✗ ${s.encoder} doesn't work on this machine — pick "auto" or one without ✗.` : "";
    };
    const drawForm = () => {
      const seen = new Set(GROUPS.flatMap((g) => g[1]));
      const extra = Object.keys(s).filter((k) => !seen.has(k));
      form.replaceChildren(...[...GROUPS, ...(extra.length ? [["Other", extra]] : [])].map(([g, keys]) => h("div", { style: "margin-top:16px" },
        h("h3", { style: "margin-bottom:10px" }, g), h("div", { class: "form-grid" }, keys.filter((k) => k in s).map(control)))));
      drawWarn();
    };
    drawForm();
    const toggle = h("button", { class: "btn sm ghost", onclick: () => {
      if (!asJson) { json.value = JSON.stringify(s, null, 2); }
      else {
        try { Object.assign(s, JSON.parse(json.value)); } catch (e) { toast("JSON: " + e.message, "red"); return; }
        drawForm();
      }
      asJson = !asJson;
      json.classList.toggle("hidden", !asJson); form.classList.toggle("hidden", asJson);
      toggle.textContent = asJson ? "Edit as form" : "Edit as JSON";
    } }, "Edit as JSON");
    const doSave = async () => {
      if (asJson) { try { Object.assign(s, JSON.parse(json.value)); } catch (e) { toast("JSON: " + e.message, "red"); return; } }
      const r = await act(() => put("/api/presets", { name: name.value.trim(), description: desc.value, settings: s, old_name: isNew ? null : p.name }));
      if (!r) return;
      toast(r.warning ? `Saved — ${r.warning}` : "Saved", r.warning ? "amber" : "green");
      const fresh = await getPresets(true);
      Object.assign(P, fresh);
      const np = P.presets.find((x) => x.name === name.value.trim());
      if (np) edit(np);
    };
    editor.replaceChildren(
      h("div", { class: "card-head" }, h("h2", {}, isNew ? "New preset" : p.name), toggle),
      h("div", { class: "form-grid", style: "margin-top:12px" }, h("label", { class: "field" }, h("span", {}, "Name"), name), h("label", { class: "field" }, h("span", {}, "Description"), desc)),
      warn, form, json,
      h("div", { class: "row wrap", style: "margin-top:18px" },
        h("button", { class: "btn primary", onclick: doSave }, "Save"),
        !isNew && !p.default ? h("button", { class: "btn", onclick: async () => { if (await act(() => post("/api/presets/default", { name: p.name }), `${p.name} is now the default`)) { Object.assign(P, await getPresets(true)); S.info.default_preset = p.name; edit(P.presets.find((x) => x.name === p.name)); } } }, "Make default") : "",
        !isNew ? h("button", { class: "btn", onclick: () => edit({ name: p.name + " copy", description: p.description, settings: { ...s } }, true) }, "Duplicate") : "",
        h("span", { class: "spacer" }),
        !isNew && !p.default ? h("button", { class: "btn danger", onclick: async () => {
          if (!(await confirmBox(`Delete ${p.name}?`, "Files already encoded with it are not affected.", "Delete", "danger"))) return;
          if (await act(() => api("/api/presets?name=" + enc(p.name), { method: "DELETE" }))) { Object.assign(P, await getPresets(true)); edit(P.presets[0]); }
        } }, "Delete") : ""));
  }
  edit(P.presets.find((p) => p.name === pg.params.get("name")) || P.presets.find((p) => p.default) || P.presets[0]);
};

// ───────────────────────────── history ─────────────────────────────
PAGES.history = async (pg) => {
  const d = await api("/api/history");
  if (!pg.alive) return;
  const live = d.history.filter((x) => !x.restored);
  const saved = live.reduce((a, x) => a + (x.src_size - x.out_size), 0);
  let q = "";
  const box = h("div");
  const draw = () => {
    const rows = d.history.filter((x) => !q || (x.final || x.src).toLowerCase().includes(q)).slice(0, 500);
    box.replaceChildren(rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", { class: "hide-sm" }, "Preset"), h("th", { class: "num" }, "Before → after"), h("th", { class: "num" }, "Saved"), h("th", { class: "hide-sm" }, "When"), h("th", {}))),
      h("tbody", {}, rows.map((x) => h("tr", {},
        h("td", { class: "name" }, h("div", { class: "trunc", title: x.final }, base(x.final || x.src)), h("div", { class: "sub trunc" }, parentName(x.final || x.src))),
        h("td", { class: "hide-sm muted trunc", style: "max-width:200px" }, x.preset),
        h("td", { class: "num nowrap" }, `${fsize(x.src_size)} → ${fsize(x.out_size)}`),
        h("td", { class: "num" }, x.restored ? h("span", { class: "dim" }, "—") : h("span", { class: "green" }, "−" + pct(saving(x.src_size, x.out_size)))),
        h("td", { class: "hide-sm dim nowrap" }, ago(x.when)),
        h("td", { class: "right" }, x.restored ? h("span", { class: "badge" }, "restored") : x.can_restore ? h("button", { class: "btn sm ghost", onclick: async () => {
          if (!(await confirmBox("Put the original back?", `${base(x.final)} — the original returns and the re-encode moves to recast's trash.`, "Restore original"))) return;
          const r = await act(() => post("/api/history/restore", { path: x.final }));
          if (r) { toast(r.message, "green"); route(); }
        } }, "Restore") : "")))))) : h("div", { class: "empty" }, d.history.length ? "No matches." : "Nothing replaced yet."));
  };
  pg.el.append(pageHead("History", `${num(live.length)} file${live.length === 1 ? "" : "s"} re-encoded · ${fsize(saved)} saved. Originals stay in recast's trash for a while so you can undo.`,
    h("input", { type: "search", placeholder: "Filter…", oninput: debounce((e) => { q = e.target.value.toLowerCase(); draw(); }, 120) })), card(null, box));
  draw();
};

// ───────────────────────────── settings ─────────────────────────────
PAGES.settings = async (pg) => {
  const d = await api("/api/settings");
  if (!pg.alive) return;
  const st = d.settings;
  const saveKey = async (k, v) => { if (await act(() => put("/api/settings", { [k]: v }))) { st[k] = v; toast("Saved", "", 1500); } };
  const fieldRow = (k, sub, ...ctl) => h("div", { class: "field-row" }, h("div", { class: "k" }, k, sub ? h("small", {}, sub) : null), h("div", { class: "row wrap" }, ...ctl));
  const chk = (k) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: st[k], onchange: (e) => saveKey(k, e.target.checked) }), "on");

  // library folders
  let roots = d.roots.map((r) => ({ ...r }));
  const rootsBox = h("div", { class: "list" });
  const newRoot = h("input", { type: "text", placeholder: "/Volumes/nas/tv  or  \\\\nas\\media\\tv", list: "dl-sugg", style: "flex:1;min-width:220px" });
  const drawRoots = () => rootsBox.replaceChildren(...roots.map((r, i) => h("div", { class: "item" },
    h("i", { class: "dot " + (r.reachable === false ? "red" : "green"), title: r.reachable === false ? "not reachable right now" : "reachable" }),
    h("div", { class: "grow" }, h("div", {}, r.name), h("div", { class: "dim mono trunc" }, r.path)),
    r.remote ? h("span", { class: "badge" }, "network") : h("span", { class: "badge" }, "local"),
    h("button", { class: "btn sm ghost", onclick: () => { roots.splice(i, 1); drawRoots(); } }, "Remove"))),
  roots.length ? "" : h("div", { class: "empty" }, "No library folders."));
  drawRoots();
  const saveRoots = async () => {
    const r = await act(() => put("/api/settings", { roots: roots.map((x) => ({ name: x.name, path: x.path, remote: x.remote })) }));
    if (r) { toast("Library folders saved", "green"); S.info = await api("/api/state"); route(); }
  };

  // arr
  const arrCard = (kind, label) => {
    const a = d[kind];
    const url = h("input", { type: "url", value: a.url, placeholder: kind === "sonarr" ? "http://nas:8989" : "http://nas:7878", style: "flex:1;min-width:200px" });
    const key = h("input", { type: "password", placeholder: a.has_key ? "saved — type to replace" : "API key (Settings → General)", style: "flex:1;min-width:200px", autocomplete: "off" });
    const out = h("div", { style: "font-size:13px;margin-top:8px" }, a.path_map.length ? h("span", { class: "muted" }, "Paths: " + a.path_map.map(([x, y]) => `${x} → ${y}`).join(" · ")) : "");
    const test = h("button", { class: "btn", onclick: async () => {
      test.disabled = true;
      const body = { [kind]: { url: url.value.trim() } };
      if (key.value.trim()) body[kind].api_key = key.value.trim();
      if (await act(() => put("/api/settings", body))) {
        const r = await act(() => post(`/api/arr/${kind}/test`));
        if (r) {
          key.value = ""; key.placeholder = "saved — type to replace";
          out.replaceChildren(h("span", { class: "green" }, `✓ Connected · ${label} ${r.version}`),
            r.path_map.length ? h("div", { class: "muted" }, "Paths: " + r.path_map.map(([x, y]) => `${x} → ${y}`).join(" · ")) : h("div", { class: "amber" }, `Couldn't match ${label}'s folders to your library folders — webhooks need matching paths.`));
        } else out.replaceChildren(h("span", { class: "red" }, "✗ Couldn't connect — check the URL and key."));
      }
      test.disabled = false;
    } }, "Save & test");
    return h("div", { style: "margin-top:12px" }, h("div", { class: "row", style: "margin-bottom:6px" }, h("b", { style: "font-weight:500" }, label), a.url && a.has_key ? h("span", { class: "badge green" }, "configured") : ""),
      h("div", { class: "row wrap" }, url, key, test,
        a.url ? h("button", { class: "btn ghost", onclick: async () => { if (await act(() => put("/api/settings", { [kind]: { clear: true } }))) route(); } }, "Remove") : ""), out);
  };

  // security
  const pw = h("input", { type: "password", placeholder: d.protected ? "new password" : "choose a password", autocomplete: "new-password" });
  const secBox = h("div", { class: "row wrap" }, pw,
    h("button", { class: "btn", onclick: async () => {
      if (!pw.value) { toast("Type a password first", "amber"); return; }
      if (await act(() => post("/api/password", { password: pw.value }), "Password set")) { S.info = await api("/api/state"); route(); }
    } }, d.protected ? "Change password" : "Set password"),
    d.protected ? h("button", { class: "btn danger", onclick: async () => {
      if (await confirmBox("Remove the password?", "Anyone who can reach this address could use recast.", "Remove", "danger") && await act(() => post("/api/password", { password: "" }), "Password removed")) { S.info = await api("/api/state"); route(); }
    } }, "Remove") : "");

  // machine
  const encRows = Object.entries(d.encoders).sort((a, b) => (a[1].status === "ok" ? 0 : 1) - (b[1].status === "ok" ? 0 : 1));
  const detectLines = h("pre", { class: "lines hidden", style: "margin-top:10px" });
  const detectBtn = h("button", { class: "btn", onclick: async () => {
    if (!(await act(() => post("/api/setup/detect")))) return;
    detectBtn.disabled = true; detectLines.classList.remove("hidden");
    const poll = setInterval(async () => {
      const s = await api("/api/setup").catch(() => null);
      if (!s) return;
      detectLines.textContent = s.lines.join("\n");
      if (!s.running) { clearInterval(poll); detectBtn.disabled = false; toast("Detection finished", "green"); S.info = await api("/api/state"); setTimeout(route, 1200); }
    }, 700);
    pg.timers.push(poll);
  } }, "Detect again");

  pg.el.append(pageHead("Settings", `Config lives in ${d.config_dir}`),
    h("datalist", { id: "dl-sugg" }, d.suggestions.map((s) => h("option", { value: s }))),
    h("div", { class: "stack" },
      card("Library folders", rootsBox,
        h("div", { class: "row wrap", style: "margin-top:12px" }, newRoot, h("button", { class: "btn", onclick: () => {
          const p = newRoot.value.trim();
          if (!p) return;
          roots.push({ name: base(p), path: p, remote: /^(\/Volumes\/|\/mnt\/|\\\\|\/\/)/.test(p) });
          newRoot.value = ""; drawRoots();
        } }, "Add"), h("button", { class: "btn primary", onclick: saveRoots }, "Save folders"))),
      card("Scratch & replacing",
        fieldRow("Scratch folder", "local disk: files are copied here, encoded, then written back", h("input", { type: "text", value: st.scratch, style: "flex:1;min-width:240px", onchange: (e) => saveKey("scratch", e.target.value) })),
        fieldRow("Scratch budget", "stop starting new work while finished encodes waiting for you use more than this", h("input", { type: "number", min: 5, value: st.max_scratch_gb, onchange: (e) => saveKey("max_scratch_gb", Number(e.target.value)) }), "GB"),
        fieldRow("Originals", "what happens to the file being replaced",
          h("select", { onchange: (e) => saveKey("originals", e.target.value) },
            [["trash", "Move to recast's trash (undo possible)"], ["keep", "Keep next to the new file (.orig)"], ["delete", "Delete"]].map(([v, l]) => h("option", { value: v, selected: st.originals === v }, l)))),
        fieldRow("Empty the trash after", null, h("input", { type: "number", min: 1, value: st.trash_days, onchange: (e) => saveKey("trash_days", Number(e.target.value)) }), "days"),
        fieldRow("Decode test", "decode the whole output once before it can replace anything", chk("verify_decode")),
        fieldRow("Rename codec in file names", "“… AV1.mkv” → “… HEVC.mkv”", chk("rename_codec")),
        fieldRow("Rescan Sonarr/Radarr after replacing", null, chk("rescan_after_replace")),
        fieldRow("Keep the computer awake", "while jobs run", chk("keep_awake")),
        fieldRow("Default preset", "used for ranking and as the starting choice",
          h("select", { onchange: async (e) => { await saveKey("default_preset", e.target.value); S.info.default_preset = e.target.value; } },
            (await getPresets()).presets.map((p) => h("option", { value: p.name, selected: p.name === st.default_preset }, p.name))))),
      card("Sonarr & Radarr", h("p", { class: "muted", style: "margin:6px 0 0" }, "Optional. Adds show names and episode titles, spots anime, maps their paths to yours for webhooks, and triggers a rescan after a replace."),
        arrCard("sonarr", "Sonarr"), arrCard("radarr", "Radarr")),
      card("Security", h("p", { class: "muted", style: "margin:6px 0 12px" }, d.protected ? "A password is required to open recast in a browser." : "No password. Fine when recast only listens on this computer (the default); set one before exposing it to your network."), secBox),
      card("This machine",
        h("div", { class: "kv", style: "margin-top:12px" },
          h("span", {}, "Computer"), h("span", {}, [d.machine.host, d.machine.os].filter(Boolean).join(" · ")),
          h("span", {}, "CPU / GPU"), h("span", {}, [d.machine.cpu, d.machine.gpu].filter(Boolean).join(" · ")),
          h("span", {}, "ffmpeg"), h("span", { class: "mono" }, `${d.ffmpeg_version} · ${d.ffmpeg}`),
          h("span", {}, "Detected"), h("span", {}, d.detected_at || "—")),
        h("div", { class: "table-wrap", style: "margin-top:12px" }, h("table", { class: "table" }, h("tbody", {}, encRows.map(([n, c]) => h("tr", {},
          h("td", { class: "mono" }, n), h("td", {}, c.status === "ok" ? h("span", { class: "green" }, "✓ works") : h("span", { class: c.status === "failed" ? "red" : "dim" }, "✗ " + (c.reason || c.status))),
          h("td", { class: "num muted" }, c.fps ? `~${Math.round(c.fps)} fps @1080p` : "")))))),
        h("div", { style: "margin-top:12px" }, detectBtn), detectLines)));
};

// ───────────────────────────── setup ─────────────────────────────
PAGES.setup = async (pg) => {
  const d = await api("/api/setup");
  if (!pg.alive) return;
  const picked = new Set(d.roots.map((r) => r.path));
  const lines = h("pre", { class: "lines" + (d.lines.length ? "" : " hidden") }, d.lines.join("\n"));
  let detected = d.detected;
  const detectState = h("span", { class: detected ? "green" : "muted" }, detected ? "✓ done" : "");
  const detectBtn = h("button", { class: "btn " + (detected ? "" : "primary"), onclick: async () => {
    if (!(await act(() => post("/api/setup/detect")))) return;
    detectBtn.disabled = true; lines.classList.remove("hidden"); detectState.textContent = "testing each encoder for 2 s…";
    const poll = setInterval(async () => {
      const s = await api("/api/setup").catch(() => null);
      if (!s) return;
      lines.textContent = s.lines.join("\n"); lines.scrollTop = lines.scrollHeight;
      if (!s.running) { clearInterval(poll); detectBtn.disabled = false; detected = s.detected; detectState.textContent = detected ? "✓ done" : "failed"; detectState.className = detected ? "green" : "red"; detectBtn.textContent = "Detect again"; detectBtn.className = "btn"; }
    }, 600);
    pg.timers.push(poll);
  } }, detected ? "Detect again" : "Detect hardware");
  const chips = h("div", { class: "chips" });
  const drawChips = () => chips.replaceChildren(...[...new Set([...d.suggestions, ...picked])].map((p) =>
    h("button", { class: "chip" + (picked.has(p) ? " on" : ""), onclick: () => { picked.has(p) ? picked.delete(p) : picked.add(p); drawChips(); } }, (picked.has(p) ? "✓ " : "") + p)));
  drawChips();
  const manual = h("input", { type: "text", placeholder: "or type a folder path", style: "flex:1" });
  const scratch = h("input", { type: "text", value: d.scratch, style: "width:100%" });
  const finish = h("button", { class: "btn primary", onclick: async () => {
    const r = await act(() => post("/api/setup/save", { roots: [...picked], scratch: scratch.value }));
    if (!r) return;
    S.info = await api("/api/state");
    renderShell(); location.hash = "#/library"; route();
  } }, "Finish setup");
  // no sidebar during setup
  $("#app").replaceChildren(h("div", { class: "center" }, h("div", { class: "narrow stack" },
    h("div", { class: "brand", style: "padding:0" }, h("div", { class: "brand-mark" }, "r"), h("span", {}, "recast")),
    h("div", {}, h("h1", {}, "Set up this machine"), h("p", { class: "muted", style: "margin:4px 0 0" }, "Three steps. Each computer you install recast on detects its own hardware.")),
    card(h("div", { class: "card-head" }, h("h2", {}, "1 · Hardware"), detectState),
      h("p", { class: "muted", style: "margin:8px 0 10px" }, "Finds ffmpeg and tests which encoders actually work here (GPU, Quick Sync, VideoToolbox, CPU)."), detectBtn, lines),
    card("2 · Library folders", h("p", { class: "muted", style: "margin:8px 0 10px" }, "Where your shows and movies live. Network shares are fine — files are copied to this computer to encode."),
      chips, h("div", { class: "row", style: "margin-top:10px" }, manual, h("button", { class: "btn", onclick: () => { if (manual.value.trim()) { picked.add(manual.value.trim()); manual.value = ""; drawChips(); } } }, "Add"))),
    card("3 · Scratch folder", h("p", { class: "muted", style: "margin:8px 0 10px" }, "A folder on a local disk with room for a few episodes."), scratch),
    h("div", { class: "row" }, h("span", { class: "spacer" }), finish))));
};

// ───────────────────────────── login / boot ─────────────────────────────
function renderLogin() {
  if (es) { es.close(); es = null; }
  const pw = h("input", { type: "password", placeholder: "Password", autocomplete: "current-password", style: "width:100%" });
  const err = h("div", { class: "red", style: "font-size:13px;min-height:18px" });
  const submit = async (e) => {
    e.preventDefault();
    try { await api("/api/login", { method: "POST", body: { password: pw.value } }); boot(); } catch (x) { err.textContent = x.message; pw.select(); }
  };
  $("#app").replaceChildren(h("div", { class: "center" }, h("form", { class: "card narrow stack", style: "max-width:360px", onsubmit: submit },
    h("div", { class: "brand", style: "padding:0" }, h("div", { class: "brand-mark" }, "r"), h("span", {}, "recast")),
    pw, err, h("button", { class: "btn primary", type: "submit" }, "Sign in"))));
  pw.focus();
}

async function boot() {
  try { S.info = await api("/api/state"); } catch (e) {
    if (e.message !== "login required") $("#app").replaceChildren(h("div", { class: "center" }, h("div", { class: "banner red" }, "Can't reach recast: " + e.message)));
    return;
  }
  if (!S.info.needs_setup) renderShell();
  connect();
  window.onhashchange = route;
  if (location.pathname === "/login") history.replaceState(null, "", "/" + location.hash);
  await route();
  setInterval(async () => { try { const st = await api("/api/state"); const wasSetup = S.info.needs_setup; S.info = st; if (wasSetup && !st.needs_setup) { renderShell(); route(); } } catch { /* offline: SSE shows it */ } }, 60000);
}
boot();
