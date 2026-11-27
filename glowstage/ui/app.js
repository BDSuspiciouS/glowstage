"use strict";

/* ============================== helpers ============================== */

const $ = (sel, root = document) => root.querySelector(sel);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function h(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "style") node.style.cssText = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) node.append(kid);
  return node;
}

/** Replaces a node's children, skipping empty (null/false) entries. */
const fill = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));

function hexToRgb(hex) {
  let s = String(hex || "").trim().replace("#", "");
  if (s.length === 3) s = [...s].map(c => c + c).join("");
  if (!/^[0-9a-f]{6}$/i.test(s)) return null;
  return [0, 2, 4].map(i => parseInt(s.slice(i, i + 2), 16));
}
const rgbToHex = c => "#" + c.map(v => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, "0")).join("");

function hsvToRgb(hh, s, v) {
  hh = ((hh % 1) + 1) % 1;
  const i = Math.floor(hh * 6), f = hh * 6 - i;
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  const [r, g, b] = [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][i % 6];
  return [r * 255, g * 255, b * 255].map(Math.round);
}
function rgbToHsv([r, g, b]) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let hh = 0;
  if (d) {
    if (mx === r) hh = ((g - b) / d) % 6;
    else if (mx === g) hh = (b - r) / d + 2;
    else hh = (r - g) / d + 4;
    hh /= 6;
  }
  return [(hh + 1) % 1, mx ? d / mx : 0, mx];
}
function kelvinToRgb(k) {
  const t = clamp(k, 1000, 40000) / 100;
  let r, g, b;
  if (t <= 66) {
    r = 255;
    g = 99.4708025861 * Math.log(t) - 161.1195681661;
    b = t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  } else {
    r = 329.698727446 * Math.pow(t - 60, -0.1332047592);
    g = 288.1221695283 * Math.pow(t - 60, -0.0755148492);
    b = 255;
  }
  return [r, g, b].map(v => Math.round(clamp(v, 0, 255)));
}
const lerp = (a, b, t) => a.map((v, i) => v + (b[i] - v) * clamp(t, 0, 1));
const scaleRgb = (c, f) => c.map(v => v * f);

/** Calls fn at most every `ms`, always delivering the latest arguments. */
function throttle(fn, ms) {
  let last = 0, timer = null, args = null;
  return (...a) => {
    args = a;
    const wait = ms - (Date.now() - last);
    if (wait <= 0 && !timer) { last = Date.now(); fn(...args); }
    else if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); fn(...args); }, Math.max(wait, 0));
  };
}

/* ============================== backend ============================== */

/* Every call gets an answer within CALL_TIMEOUT: a call that never comes back (the app busy, the
   window reloading) must not leave the page waiting forever. Pairing and adding lights wait on
   the light itself, so they get longer. */
const CALL_TIMEOUT = 15000;
const SLOW_CALLS = { pair: 60000, pair_nanoleaf: 60000, add_device: 45000, search: 45000,
  export_settings: 3600000, pick_settings_file: 3600000 };   // (these wait for you in a file dialog)
async function callRaw(method, ...args) {
  let timer;
  try {
    const api = window.pywebview && window.pywebview.api;
    if (!api || !api[method]) return { ok: false, error: "Glowstage is still starting" };
    const late = new Promise(res => {
      timer = setTimeout(() => res({ ok: false, timeout: true, error: "Glowstage took too long to answer" }),
        SLOW_CALLS[method] || CALL_TIMEOUT);
    });
    return await Promise.race([api[method](...args), late]);
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}
async function call(method, ...args) {
  const r = await callRaw(method, ...args);
  if (!r || r.ok === false) { toast((r && r.error) || "Something went wrong", true); return undefined; }
  return r.result;
}

/* ============================== state ============================== */

const S = {
  data: null,
  page: "home",            // home | presets | show | setup
  sheetOpen: false,        // the color & effects panel on the right
  selected: null,          // null = all lights, otherwise an array of device ids
  tab: "color",            // the panel's tab
  arrange: false,          // the stage is in Arrange mode
  dragging: null,          // which slider is being dragged
  quietUntil: 0,           // ignore polls right after local edits so sliders don't jump back
  native: {},              // device id -> {effects, palettes}
  nativeBuiltFor: "",
  tiles: new Map(),
  groups: null,
  settingsSig: "",
  fxBuilt: "",
};

const visible = () => (S.data ? S.data.devices.filter(d => !d.hidden) : []);
const byId = id => S.data && S.data.devices.find(d => d.id === id);
const targetIds = () => (S.selected ? S.selected.filter(byId) : visible().map(d => d.id));
const primary = () => byId(targetIds()[0]);

const senders = new Map();
function send(ids, patch, key) {
  const list = ids || visible().map(d => d.id);
  for (const id of list) { const d = byId(id); if (d) Object.assign(d.state, patch); }
  S.quietUntil = Date.now() + 900;
  if (!senders.has(key)) senders.set(key, throttle((i, p) => call("update", i, p), 70));
  senders.get(key)(ids, patch);
  render();
}
const sendTargets = (patch, key) => send(S.selected ? targetIds() : null, patch, key);

/* One state request at a time: the next waits for the last (a forced one right after it). The app
   sends everything only when something changed since the revision we have, otherwise just the
   parts that keep changing (the show, downloads). */
let refreshing = null, refreshAgain = false;
async function refresh(force = false) {
  if (refreshing) { refreshAgain = refreshAgain || force; return refreshing; }
  refreshing = (async () => {
    const since = !force && S.data ? S.data.revision : null;
    const r = await callRaw("get_state", since);
    if (!r || r.ok === false) return;           // keep showing the last state; the next poll tries again
    const d = r.result;
    if (!force && (S.dragging || Date.now() < S.quietUntil)) return;
    if (d.same && S.data) {
      delete d.same;
      Object.assign(S.data, d);
    } else {
      delete d.same;
      S.data = d;
    }
    if (S.selected) {
      S.selected = S.selected.filter(id => S.data.devices.some(x => x.id === id && !x.hidden));
      if (!S.selected.length) S.selected = null;
    }
    render();
  })();
  try { await refreshing; } finally {
    refreshing = null;
    if (refreshAgain) { refreshAgain = false; refresh(true); }
  }
}

async function pollState() {
  try { await refresh(); } finally { setTimeout(pollState, document.hidden ? 4000 : 1000); }
}

const SVG_NS = "http://www.w3.org/2000/svg";
function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg"), use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.setAttribute("aria-hidden", "true");
  svg.append(use);
  return svg;
}

/* ============================== pages ============================== */

function go(page) {
  S.page = page;
  for (const id of ["home", "presets", "show", "setup"]) $(`#page-${id}`).hidden = id !== page;
  if (page !== "show") S.arrange = false;
  $(".main").scrollTop = 0;
  if (page === "setup") S.settingsSig = "";
  render();
}

function render() {
  if (!S.data) return;
  const p = primary();
  document.documentElement.style.setProperty("--tint", p && hexToRgb(p.state.color) ? p.state.color : "#8a6bff");
  renderChrome();
  if (S.page === "home") renderHome();
  else if (S.page === "presets") renderPresetPage();
  else if (S.page === "show") { renderShow(); renderStage(); }
  else if (S.page === "setup") renderSettings();
  if (S.sheetOpen) {
    renderTarget();
    if (S.tab === "color") renderColor();
    else if (S.tab === "effects") renderEffects();
    else renderNative();
  }
  if (S.wiz && S.wiz.open) renderWizard();
}

/* ============================== support and updates ============================== */

function openSupport() {
  const about = (S.data && S.data.about) || {};
  const links = about.donate || [];
  const give = l => h("button", { class: "btn btn-support support-link", type: "button",
    onclick: () => { call("open_link", l.url); call("support_answer", "done"); S.supportClosed = true; renderHomeNotes(); } },
    icon("heart"), l.name);
  const share = async () => {
    const url = about.repo + "/releases";
    try { await navigator.clipboard.writeText(url); toast("Download link copied"); } catch { toast(url); }
  };
  openModal({
    title: "Support Glowstage",
    body: [
      h("p", { class: "support-lead", text: "Glowstage is free, has no ads and collects nothing about you. It's made by one person, in spare time." }),
      h("p", { class: "hint", text: "If it brings your room to life, a donation keeps it going: support for more lights, better light shows, fixes." }),
      links.length ? h("div", { class: "support-links" }, links.map(give)) : null,
      h("div", { class: "support-free" },
        h("span", { class: "muted", text: "Free ways to help:" }),
        about.repo ? h("button", { class: "link-btn", type: "button", text: "Star it on GitHub", onclick: () => call("open_link", about.repo) }) : null,
        about.repo ? h("button", { class: "link-btn", type: "button", text: "Share the download link", onclick: share }) : null,
        about.repo ? h("button", { class: "link-btn", type: "button", text: "Report a bug or idea",
          onclick: () => call("open_link", about.repo + "/issues/new/choose") }) : null),
    ],
    ok: "Close",
    onOk: async () => true,
    cancel: false,
  });
}

const dismissedUpdate = () => { try { return localStorage.getItem("glowstage.update.dismissed") || ""; } catch { return ""; } };

/* Notes at the top of Home: a new version is out; once in a while, the support card. */
const AUTO_NOTES = {
  cs2: { icon: "spark", title: "Counter-Strike 2 is on", text: "Your lights play along with the match and go back when you leave it." },
  game: { icon: "spark", title: "A game is full screen", text: "Your lights changed for it by themselves and go back when you leave the game." },
  video: { icon: "spark", title: "A video is full screen", text: "Your lights changed for it by themselves and go back when the video closes." },
  music: { icon: "show", title: "Music is playing", text: "The light show started by itself. Your lights go back 30 seconds after the music stops." },
};

function renderHomeNotes() {
  const host = $("#home-notes");
  const up = S.data.update;
  const upd = S.data.updating || {};
  const busy = ["downloading", "checking", "installing"].includes(upd.state);
  const showUpdate = up && up.version && (busy || upd.state === "failed" || dismissedUpdate() !== up.version);
  if (S.data.updateResult && !S.updateToldOnce) {
    S.updateToldOnce = true;
    if (S.data.updateResult === "updated") toast(`Glowstage is updated to ${(S.data.about || {}).version || "the new version"}`);
    else toast("The update didn't work: you still have the version you had", true);
  }
  if (S.data.recovered && !S.recoveredTold) {
    S.recoveredTold = true;
    const day = new Date(S.data.recovered).toLocaleDateString("en-GB", { day: "numeric", month: "long" });
    toast(`Your settings file couldn't be read: Glowstage went back to its copy from ${day}`, true,
      { label: "Backups", run: () => showNew("setup:backups") });
  }
  const showSupport = S.data.askSupport && !S.supportClosed;
  const app = String(S.data.auto || "").startsWith("app:")
    && (S.data.appLooks || []).find(a => "app:" + a.exe === S.data.auto);
  const auto = app ? { icon: "spark", title: `${app.name} is open`, text: "Your lights changed for it by themselves and go back when you close it." }
    : AUTO_NOTES[S.data.auto];
  const sleep = S.data.sleep;
  const news = !S.newsClosed && S.data.whatsNew;
  const sig = JSON.stringify([showUpdate && up, showSupport, S.data.auto, upd.state, Math.round((upd.progress || 0) * 50), upd.message,
    sleep && sleep.until, news && news.version]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  fill(host,
    sleep ? h("div", { class: "note card", role: "status" },
      h("span", { class: "note-icon" }, icon("moon")),
      h("div", { class: "note-text" }, h("strong", { text: `Lights off at ${clockTime(sleep.until)}` }),
        h("span", { class: "muted", text: "They fade out slowly until then. A light you change yourself is left alone." })),
      h("div", { class: "note-actions" },
        h("button", { class: "btn", type: "button", text: "Cancel the timer", onclick: async () => {
          await call("cancel_sleep_timer"); toast("Sleep timer cancelled: your lights are back"); refresh(true); } }))) : null,
    auto ? h("div", { class: "note card", role: "status" },
      h("span", { class: "note-icon" }, icon(auto.icon)),
      h("div", { class: "note-text" }, h("strong", { text: auto.title }), h("span", { class: "muted", text: auto.text })),
      h("div", { class: "note-actions" },
        h("button", { class: "btn", type: "button", text: "Put my lights back", onclick: async () => {
          await call("stop_automation"); refresh(true); } }))) : null,
    news ? h("div", { class: "note card note-new", role: "status" },
      h("span", { class: "note-icon" }, icon("spark")),
      h("div", { class: "note-text" }, h("strong", { text: `New in Glowstage ${news.version}` }),
        h("ul", { class: "new-list" }, news.items.map(it => h("li", {},
          h("span", { class: "new-title", text: it.title }), h("span", { class: "muted", text: it.text }),
          h("button", { class: "link-btn", type: "button", text: "Show me", onclick: () => showNew(it.go) }))))),
      h("div", { class: "note-actions" },
        h("button", { class: "btn", type: "button", text: "Got it", onclick: () => {
          S.newsClosed = true; call("whats_new_seen"); renderHomeNotes(); } }))) : null,
    showUpdate && busy ? h("div", { class: "note card", role: "status" },
      h("span", { class: "note-icon" }, icon("spark")),
      h("div", { class: "note-text" }, h("strong", { text: `Updating to Glowstage ${up.version}` }),
        h("span", { class: "muted", text: upd.message || "Getting the new version…" }),
        h("div", { class: "progress", style: "margin:8px 0 0" }, h("span", { style: `width:${Math.round((upd.progress || 0) * 100)}%` })))) : null,
    showUpdate && !busy ? h("div", { class: "note card", role: "status" },
      h("span", { class: "note-icon" }, icon("spark")),
      h("div", { class: "note-text" }, h("strong", { text: upd.state === "failed" ? "The update didn't work" : `Glowstage ${up.version} is out` }),
        h("span", { class: "muted", text: upd.state === "failed" ? upd.message
          : upd.canInstall ? "Update with one click: Glowstage gets it from GitHub, checks it and restarts. Your lights, presets and settings stay as they are."
          : "Download it from GitHub: your lights, presets and settings stay as they are." })),
      h("div", { class: "note-actions" },
        upd.canInstall ? h("button", { class: "btn btn-primary", type: "button", text: upd.state === "failed" ? "Try again" : "Update now",
          onclick: async () => { await call("install_update"); refresh(true); } }) : null,
        h("button", { class: upd.canInstall ? "btn" : "btn btn-primary", type: "button", text: upd.canInstall ? "What's new" : "Download",
          onclick: () => call("open_link", up.url) }),
        h("button", { class: "btn btn-quiet", type: "button", text: "Not now", onclick: () => {
          try { localStorage.setItem("glowstage.update.dismissed", up.version); } catch { /* fine */ }
          host.dataset.sig = ""; renderHomeNotes();
        } }))) : null,
    showSupport ? h("div", { class: "note card note-support" },
      h("span", { class: "note-icon" }, icon("heart")),
      h("div", { class: "note-text" }, h("strong", { text: "Enjoying Glowstage?" }),
        h("span", { class: "muted", text: "It's free and made by one person. A small donation keeps the lights on." })),
      h("div", { class: "note-actions" },
        h("button", { class: "btn btn-support", type: "button", text: "Support", onclick: openSupport }),
        h("button", { class: "btn btn-quiet", type: "button", text: "Later", onclick: () => {
          S.supportClosed = true; call("support_answer", "later"); renderHomeNotes(); } }),
        h("button", { class: "link-btn", type: "button", text: "Don't ask again", onclick: () => {
          S.supportClosed = true; call("support_answer", "never"); renderHomeNotes(); } }))) : null);
}

/** "Show me" on the what's-new card: open the new thing. */
function showNew(where) {
  if (where === "sleep") return openSleepTimer();
  if (where.startsWith("setup:")) {
    go("setup");
    setTimeout(() => { const s = $("#set-" + where.slice(6)); if (s) s.scrollIntoView({ block: "start", behavior: "smooth" }); }, 60);
    return;
  }
  if (where.startsWith("effect:")) {
    $("#all-color").click();
    switchTab("effects");
    setTimeout(() => {
      const card = document.querySelector(`#fx-grid [data-fx="${where.slice(7)}"]`);
      if (card) { card.scrollIntoView({ block: "center", behavior: "smooth" }); card.focus({ preventScroll: true }); }
    }, 120);
  }
}

const clockTime = epoch => new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Home → Sleep timer: the lights that are on fade out and switch off after a while. */
function openSleepTimer() {
  const pick = async minutes => {
    const r = await callRaw("start_sleep_timer", minutes);
    if (!r || r.ok === false) return (r && r.error) || "Couldn't start the timer";
    closeModal();
    refresh(true);
    toast(`Your lights fade out and switch off in ${minutes < 60 ? minutes + " minutes" : minutes === 60 ? "an hour" : minutes / 60 + " hours"}`);
    return true;
  };
  const running = S.data.sleep;
  openModal({
    title: "Sleep timer",
    body: h("div", {},
      h("p", { class: "hint", text: running
        ? `Your lights switch off at ${clockTime(running.until)}. Pick a new time, or keep it.`
        : "The lights that are on fade out slowly, like a sunset, and switch off at the end. Next time they come on as they were." }),
      h("div", { class: "sleep-choices" }, [[15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [90, "1½ hours"]].map(([m, t]) =>
        h("button", { class: "app-pick", type: "button", onclick: async () => {
          const res = await pick(m);
          if (res !== true) { $("#modal-error").textContent = res; $("#modal-error").hidden = false; }
        } }, h("span", { text: t }), h("span", { class: "muted", text: `off at ${clockTime(Date.now() / 1000 + m * 60)}` }))))),
    ok: "Close", cancel: false, onOk: () => true,
  });
}

function renderChrome() {
  for (const b of document.querySelectorAll(".nav-item[data-page]")) {
    if (b.dataset.page === S.page) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  const about = S.data.about || {};
  $("#support").hidden = !about.repo;
  $("#version").textContent = about.version ? `Version ${about.version}` : "";
  $("#nav-live").hidden = !S.data.show;
  $("#nav-badge").hidden = !(S.data.devices.some(d => d.paired === false && !d.hidden) || (S.data.hubs || []).some(x => x.paired === false));
}

/* ============================== how a light looks ============================== */

const RAINBOW = "linear-gradient(90deg, #ff3b5c, #ffb03b, #f6ff3b, #3bff8a, #3bc8ff, #6e5bff, #ff3bd2)";
const FX_LOOK = {
  rainbow: RAINBOW, spectrum: RAINBOW, music_party: RAINBOW, music_spectrum: RAINBOW, music_ripple: RAINBOW,
  music_sparkle: RAINBOW, music_meteor: RAINBOW,
  fire: "linear-gradient(90deg, #7a1000, #ff5a00, #ffb000, #ffd25a)", music_fire: "linear-gradient(90deg, #7a1000, #ff5a00, #ffb000, #ffd25a)",
  aurora: "linear-gradient(90deg, #2bff9a, #2bd3ff, #8a4bff)", music_aurora: "linear-gradient(90deg, #2bff9a, #2bd3ff, #8a4bff)",
  screen: "linear-gradient(90deg, #3a6bff, #ff8a3a, #2bd3c4)",
  screen_sound: "linear-gradient(90deg, #fff0dc, #3a6bff 30%, #2bd3c4 70%, #ff8a3a)",
  daylight: "linear-gradient(90deg, #ff9a45, #ffe0c7, #ffb06a)",
  typing: "linear-gradient(90deg, #1b1430, #00e5ff 28%, #1b1430 48%, #7c4dff 72%, #1b1430)",
  music_organ: "linear-gradient(90deg, #ff2a2a, #2aff5a, #2a6bff)",
  music_show: "linear-gradient(90deg, #00e5ff, #2a4bff, #ff00c8, #ff0040)",
};
/** A color at full strength (brightness is the slider's job, not the fill's). */
function vivid(rgb) {
  const m = Math.max(...rgb);
  return m < 8 ? [48, 48, 54] : rgb.map(v => v * 255 / m);
}
const liveOf = d => (typeof ST !== "undefined" && ST.live[d.id]) || null;

/** The colors a light shows right now, for its card: live frames when an app effect runs. */
function lightColors(d) {
  const live = liveOf(d);
  if (live && live.length) {
    const step = Math.max(1, Math.floor(live.length / 10));
    return live.filter((_, i) => i % step === 0).map(c => rgbToHex(vivid(hexToRgb(c) || [0, 0, 0])));
  }
  return null;
}
function fillFor(d) {
  const st = d.state, live = lightColors(d);
  if (live) return live.length > 1 ? `linear-gradient(90deg, ${live.join(", ")})` : live[0];
  if (st.mode === "effect" && FX_LOOK[st.effect]) return FX_LOOK[st.effect];
  if (st.mode === "effect" || st.mode === "native") return `linear-gradient(90deg, ${st.color}, ${st.color2 || st.color})`;
  return hexToRgb(st.color) ? st.color : "#ffffff";
}
function glowColor(d) {
  const live = lightColors(d);
  if (live) return rgbToHex(vivid(average(live.map(hexToRgb))));
  return hexToRgb(d.state.color) ? d.state.color : "#8a6bff";
}

function typeIcon(d) {
  const text = `${d.name} ${d.label || ""}`.toLowerCase();
  if (d.kind === "nanoleaf") return "panels";
  if (d.kind === "qmk" || /keyboard/.test(text)) return "keyboard";
  if (d.kind === "asrock_gpu" || /\bgpu\b|graphics|radeon|geforce/.test(text)) return "gpu";
  if (d.group === "pc") return /mouse|mat\b|mousemat|pad|cloth|strip/.test(text) ? "strip" : "fan";
  if ((d.pixels || 0) <= 1) return "bulb";
  return "strip";
}

function stripMeta(d) {
  if (d.paired === false) return { text: "Not paired", warn: true };
  if (!d.online) return { text: d.error || "Offline", warn: true };
  if (!d.state.on) return { text: "Off" };
  let what = "Color";
  if (d.state.mode === "effect") {
    const fx = S.data.effects.find(e => e.id === d.state.effect);
    what = fx ? fx.name : "Effect";
  } else if (d.state.mode === "native") {
    const list = S.native[d.id] && S.native[d.id].effects;
    const fx = list && list.find(e => String(e.id) === String(d.state.native));
    what = fx ? fx.name : "Built-in effect";
  }
  return { text: `${what} · ${d.state.brightness}%` };
}

/* ============================== sliders ============================== */

function setSlider(el, v, fillCss, off) {
  el.style.setProperty("--v", (v / 100).toFixed(3));
  el.setAttribute("aria-valuenow", v);
  if (fillCss) el.style.setProperty("--fill", fillCss);
  el.classList.toggle("is-off", !!off);
}

/** A horizontal pill slider: drag, click or arrow keys. */
function bindSlider(el, key, onValue, min = 0) {
  const at = e => { const r = el.getBoundingClientRect(); return Math.round(clamp((e.clientX - r.left) / r.width, 0, 1) * 100); };
  const set = v => { v = clamp(Math.round(v), min, 100); setSlider(el, v); onValue(v); };
  el.addEventListener("pointerdown", e => {
    e.stopPropagation();
    el.setPointerCapture(e.pointerId);
    S.dragging = key;
    el.classList.add("is-dragging");
    set(at(e));
  });
  el.addEventListener("pointermove", e => { if (el.hasPointerCapture(e.pointerId)) set(at(e)); });
  const end = () => { S.dragging = null; el.classList.remove("is-dragging"); S.quietUntil = Date.now() + 900; };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("click", e => e.stopPropagation());
  el.addEventListener("keydown", e => {
    const cur = Number(el.getAttribute("aria-valuenow")) || 0;
    const step = { ArrowUp: 5, ArrowRight: 5, ArrowDown: -5, ArrowLeft: -5, PageUp: 20, PageDown: -20 }[e.key];
    if (step) { e.preventDefault(); set(cur + step); }
    if (e.key === "Home") set(min);
    if (e.key === "End") set(100);
  });
}

/* ============================== home ============================== */

function renderHome() {
  const devs = visible();
  const on = devs.filter(d => d.online && d.state.on);
  const anyOn = on.length > 0;
  $("#home-sub").textContent = devs.length
    ? `${on.length} of ${devs.length} lights on${S.data.show ? " · light show running" : ""}` : "";
  $("#home-empty").hidden = devs.length > 0;
  renderHomeNotes();
  $("#master").hidden = !devs.length;
  const power = $("#all-power");
  power.querySelector("span").textContent = anyOn ? "All off" : "All on";
  power.classList.toggle("btn-primary", !anyOn);
  const avg = devs.length ? Math.round(devs.reduce((a, d) => a + d.state.brightness, 0) / devs.length) : 100;
  if (S.dragging !== "__all") {
    const colors = on.slice(0, 8).map(glowColor);
    setSlider($("#master-slider"), avg, colors.length > 1 ? `linear-gradient(90deg, ${colors.join(", ")})` : colors[0] || "#f3f1ed", !anyOn);
  }
  $("#master-value").textContent = anyOn ? `${avg}% brightness` : "Off";
  renderQuickPresets();
  renderTiles();
}

function makeTile(id) {
  const tile = h("div", { class: "tile", "data-id": id, role: "button", tabindex: "0" });
  const power = h("button", { class: "tile-power", type: "button" }, icon("power"));
  const pair = h("button", { class: "tile-pair", type: "button", text: "Pair" });
  const slider = h("div", { class: "slider", role: "slider", tabindex: "0", "aria-valuemin": "0", "aria-valuemax": "100" },
    h("div", { class: "slider-fill" }));
  tile.append(
    h("div", { class: "tile-top" },
      h("span", { class: "tile-icon" }),
      h("div", { class: "tile-text" }, h("span", { class: "tile-name" }), h("span", { class: "tile-meta" })),
      pair, power),
    slider);
  tile.addEventListener("click", e => {
    if (e.target.closest(".tile-power, .tile-pair")) return;
    selectStrip(id, e.ctrlKey || e.shiftKey || e.metaKey);
  });
  tile.addEventListener("keydown", e => {
    if (e.target === tile && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectStrip(id, false); }
  });
  power.addEventListener("click", () => toggleStrip(id));
  pair.addEventListener("click", () => openPairDialog(byId(id)));
  bindSlider(slider, id, v => send([id], { brightness: v }, "bri:" + id));
  return tile;
}

function updateTile(node, d) {
  const st = d.state, on = st.on && d.online;
  node.classList.toggle("is-off", !st.on);
  node.classList.toggle("is-offline", !d.online);
  node.classList.toggle("is-selected", !!S.selected && S.selected.includes(d.id));
  node.style.setProperty("--c", glowColor(d));
  node.style.setProperty("--lit", on ? (0.35 + 0.65 * st.brightness / 100).toFixed(2) : 0);
  const ic = typeIcon(d), box = $(".tile-icon", node);
  if (box.dataset.icon !== ic) { box.dataset.icon = ic; fill(box, icon(ic)); }
  $(".tile-name", node).textContent = d.name;
  node.setAttribute("aria-label", `${d.name}, open color and effects`);
  const meta = stripMeta(d), metaEl = $(".tile-meta", node);
  metaEl.textContent = meta.text;
  metaEl.title = meta.text;
  metaEl.classList.toggle("is-warn", !!meta.warn);
  const needsPair = d.paired === false;
  $(".tile-pair", node).hidden = !needsPair;
  const power = $(".tile-power", node);
  power.hidden = needsPair;
  power.classList.toggle("is-on", !!st.on);
  power.setAttribute("aria-label", st.on ? `Turn ${d.name} off` : `Turn ${d.name} on`);
  power.title = st.on ? "Turn off" : "Turn on";
  const slider = $(".slider", node);
  slider.setAttribute("aria-label", `Brightness, ${d.name}`);
  if (S.dragging !== d.id) setSlider(slider, st.brightness, fillFor(d), !on);
}

function renderTiles() {
  const host = $("#light-groups");
  if (!S.groups) {
    S.groups = {};
    for (const [key, title] of [["pc", "In your PC"], ["room", "In your room"]]) {
      const grid = h("div", { class: "tiles" }), count = h("span", { class: "count" });
      const sec = h("section", { class: "light-group" }, h("div", { class: "section-head" }, h("h2", {}, title, count)), grid);
      S.groups[key] = { sec, grid, count };
      host.append(sec);
    }
  }
  const devs = visible();
  for (const [key, g] of Object.entries(S.groups)) {
    const members = devs.filter(d => (d.group === "pc" ? "pc" : "room") === key);
    g.sec.hidden = !members.length;
    g.count.textContent = members.length;
    for (const d of members) if (!S.tiles.has(d.id)) S.tiles.set(d.id, makeTile(d.id));
    const order = members.map(d => d.id);
    if ([...g.grid.children].map(n => n.dataset.id).join("|") !== order.join("|")) fill(g.grid, order.map(id => S.tiles.get(id)));
  }
  for (const [id, node] of S.tiles) if (!devs.some(d => d.id === id)) { node.remove(); S.tiles.delete(id); }
  for (const d of devs) updateTile(S.tiles.get(d.id), d);
}

/* Live colors: while app effects run, the cards follow the lights about 10 times a second. */
let tilesFrame = 0;
function liveTiles(ts) {
  requestAnimationFrame(liveTiles);
  if (S.page !== "home" || !S.data || ts - tilesFrame < (STILL.matches ? 400 : 100) || typeof ST === "undefined" || !Object.keys(ST.live).length) return;
  tilesFrame = ts;
  for (const d of visible()) {
    if (!liveOf(d)) continue;
    const node = S.tiles.get(d.id);
    if (!node) continue;
    node.style.setProperty("--c", glowColor(d));
    if (S.dragging !== d.id) $(".slider", node).style.setProperty("--fill", fillFor(d));
  }
}

function selectStrip(id, additive) {
  if (id === "__all") S.selected = null;
  else if (additive) {
    const cur = new Set(S.selected || []);
    cur.has(id) ? cur.delete(id) : cur.add(id);
    S.selected = cur.size ? [...cur] : null;
  } else S.selected = [id];
  if (id !== "__all") openSheet();
  else render();
}

function toggleStrip(id) {
  if (id === "__all") {
    const anyOn = visible().some(d => d.state.on && d.online);
    send(null, { on: !anyOn }, "on:all");
    if (anyOn) undoable("All lights are off");
    return;
  }
  const d = byId(id);
  if (!d) return;
  if (d.paired === false) return openPairDialog(d);
  send([id], { on: !d.state.on }, "on:" + id);
}

/* ============================== presets ============================== */

const isMusicPreset = p => (p.effects || []).some(e => e.startsWith("music_"));
const PRESET_PALETTES = {
  rainbow: ["#ff3b5c", "#ffb03b", "#3bff8a", "#3bc8ff", "#8a5bff"], spectrum: ["#ff3b5c", "#3bff8a", "#3bc8ff", "#ff3bd2"],
  music_party: ["#ff2f92", "#ffb03b", "#3bc8ff", "#8a5bff"], music_spectrum: ["#3bff8a", "#3bc8ff", "#ff3bd2"],
  music_ripple: ["#2b6bff", "#00e5ff", "#ff2f92"], music_sparkle: ["#ffd23b", "#ff3bd2", "#3bc8ff"],
  music_meteor: ["#ff5f3b", "#ffd23b", "#3b6bff"], screen: ["#3a6bff", "#ff8a3a", "#2bd3c4"],
  daylight: ["#ffe6cc", "#ff8a3a", "#ffc27a", "#c2541a"],
  fire: ["#ffd25a", "#ff5a00", "#8a1500"], music_fire: ["#ffd25a", "#ff5a00", "#8a1500"],
  aurora: ["#2bff9a", "#2bd3ff", "#8a4bff"], music_aurora: ["#2bff9a", "#2bd3ff", "#8a4bff"],
  music_organ: ["#ff2a2a", "#2aff5a", "#2a6bff"], music_show: ["#00e5ff", "#ff00c8", "#2a4bff", "#ff0040"],
};
const shade = (hex, f) => rgbToHex((hexToRgb(hex) || [128, 128, 128]).map(v => f >= 0 ? v + (255 - v) * f : v * (1 + f)));

/** The art on a preset's card: a soft blend of its colors, like light falling on a wall. */
function presetArt(p) {
  if (p.off) return "linear-gradient(135deg, #24252b, #141418)";
  const fx = (p.effects || []).find(e => PRESET_PALETTES[e]);
  const c = fx ? PRESET_PALETTES[fx] : (p.colors || []).filter(hexToRgb);
  if (!c.length) return "linear-gradient(135deg, #3a3b44, #1a1b20)";
  if (c.length === 1) {
    return `radial-gradient(120% 130% at 20% 0%, ${shade(c[0], 0.35)}, ${c[0]} 45%, ${shade(c[0], -0.55)} 100%)`;
  }
  const spots = [["0% 0%", 0], ["100% 10%", 1], ["30% 110%", 2], ["100% 100%", 3]];
  return spots.slice(0, Math.max(2, Math.min(4, c.length)))
    .map(([at, i]) => `radial-gradient(90% 110% at ${at}, ${c[i % c.length]} 0%, transparent 70%)`).join(", ")
    + `, linear-gradient(135deg, ${shade(c[0], -0.4)}, ${shade(c[1 % c.length], -0.4)})`;
}

/* Drag a preset onto another to give it that one's place: the tray menu, Home and hotkeys 1-9
   follow the order. (Keyboard: Move earlier / later / to the top in its menu.) */
function presetDrag(card, p) {
  const clear = () => card.classList.remove("drop-before", "drop-after");
  card.draggable = true;
  card.addEventListener("dragstart", e => {
    S.dragPreset = p.name;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", p.name);
    card.classList.add("is-dragging");
  });
  card.addEventListener("dragend", () => {
    S.dragPreset = null;
    document.querySelectorAll(".preset.is-dragging, .preset.drop-before, .preset.drop-after")
      .forEach(el => el.classList.remove("is-dragging", "drop-before", "drop-after"));
  });
  card.addEventListener("dragover", e => {
    const from = S.dragPreset;
    if (!from || from === p.name) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const names = S.data.presets.map(x => x.name);
    const later = names.indexOf(from) < names.indexOf(p.name);    // (it lands after this one)
    card.classList.toggle("drop-after", later);
    card.classList.toggle("drop-before", !later);
  });
  card.addEventListener("dragleave", e => { if (!card.contains(e.relatedTarget)) clear(); });
  card.addEventListener("drop", async e => {
    e.preventDefault();
    clear();
    const from = S.dragPreset;
    if (!from || from === p.name) return;
    const names = S.data.presets.map(x => x.name);
    const to = names.indexOf(p.name);
    const r = await callRaw("move_preset", from, to - names.indexOf(from));
    if (!r || r.ok === false) { toast((r && r.error) || "That didn't work", true); return; }
    refresh(true);
    toast(`${from} is now number ${to + 1}`);
  });
  return card;
}

function presetCard(p) {
  const active = S.data && S.data.activePreset === p.name;
  return presetDrag(h("button", {
    class: "preset" + (active ? " is-active" : ""), type: "button", "aria-pressed": active ? "true" : "false",
    title: `Apply ${p.name}` + (p.split ? " (one look for your PC, another for your room)" : ""),
    onclick: () => applyPreset(p),
  },
    h("span", { class: "preset-art", style: `background:${presetArt(p)}` }),
    active ? h("span", { class: "preset-on", text: "On" }) : null,
    h("span", { class: "preset-name", text: p.name }),
    isMusicPreset(p) ? h("span", { class: "preset-kind", title: "Moves to the music" }, icon("music")) : null,
    h("span", {
      class: "preset-x", role: "button", tabindex: "0", "aria-label": `More for ${p.name}: rename, update, move, delete`,
      title: "Rename, update, move or delete",
      onclick: e => { e.stopPropagation(); openPresetMenu(p); },
      onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); openPresetMenu(p); } },
    }, icon("more"))), p);
}

/** A preset's own menu: its name, its lights, its place in the list (tray, Home, hotkeys), delete. */
function openPresetMenu(p) {
  const names = S.data.presets.map(x => x.name), at = names.indexOf(p.name);
  const hotkeys = !!(S.data.settings || {}).hotkeys;
  const input = h("input", { class: "text-input", style: "width:100%", value: p.name, maxlength: "40", "aria-label": "Name" });
  const act = async (fn, ...args) => {
    const r = await callRaw(fn, ...args);
    if (!r || r.ok === false) { toast((r && r.error) || "That didn't work", true); return false; }
    refresh(true);
    return true;
  };
  const place = `Number ${at + 1} of ${names.length}: the tray menu and Home show presets in this order (drag a card to move it)`
    + (at < 9 ? `, and Ctrl+Alt+Shift+${at + 1} switches it on${hotkeys ? "" : " (with hotkeys on, in Setup → App)"}.` : ".");
  openModal({
    title: p.name,
    body: h("div", { class: "preset-menu" },
      h("label", { class: "field-label", text: "Name" }), input,
      h("p", { class: "hint", text: place }),
      h("div", { class: "form-actions" },
        h("button", { class: "btn", type: "button", text: "To the top", disabled: at === 0,
          title: "First on Home and in the tray, and Ctrl+Alt+Shift+1",
          onclick: async () => { if (await act("move_preset", p.name, -at)) { closeModal(); toast(`${p.name} is now number 1`); } } }),
        h("button", { class: "btn", type: "button", text: "Move earlier", disabled: at === 0,
          onclick: async () => { if (await act("move_preset", p.name, -1)) { closeModal(); toast(`${p.name} is now number ${at}`); } } }),
        h("button", { class: "btn", type: "button", text: "Move later", disabled: at === names.length - 1,
          onclick: async () => { if (await act("move_preset", p.name, 1)) { closeModal(); toast(`${p.name} is now number ${at + 2}`); } } })),
      h("div", { class: "form-actions" },
        h("button", { class: "btn", type: "button", text: "Save my lights into it", title: "It gets your lights as they are now",
          onclick: async () => { if (await act("save_preset", p.name)) { closeModal(); toast(`${p.name} now has your lights as they are`); } } }),
        h("button", { class: "btn btn-quiet", type: "button", text: "Delete…", onclick: () => { closeModal(); confirmDeletePreset(p.name); } }))),
    ok: "Save name",
    onOk: async () => {
      const name = input.value.trim();
      if (!name) return "Give the preset a name.";
      if (name === p.name) return true;
      const r = await callRaw("rename_preset", p.name, name);
      if (!r || r.ok === false) return (r && r.error) || "That didn't work";
      toast(`Renamed to ${name}`);
      refresh(true);
      return true;
    },
  });
  input.addEventListener("keydown", e => { if (e.key === "Enter") $("#modal-ok").click(); });
}

function renderQuickPresets() {
  const host = $("#quick-presets");
  const sig = JSON.stringify([S.data.presets, S.data.activePreset]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  fill(host, S.data.presets.map(presetCard));
  $("#see-presets").firstChild.textContent = `All ${S.data.presets.length} presets`;
}

function renderPresetPage() {
  const host = $("#preset-list");
  const sig = JSON.stringify([S.data.presets, S.data.activePreset]);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  const lighting = S.data.presets.filter(p => !isMusicPreset(p));
  const music = S.data.presets.filter(isMusicPreset);
  const group = (title, ic, list, extra) => [
    h("h2", { class: "preset-group-title" }, icon(ic), title, h("span", { class: "count", text: list.length })),
    h("div", { class: "preset-grid" }, list.map(presetCard), extra)];
  fill(host,
    group("Lighting", "palette", lighting, h("button", { class: "preset-add", type: "button", onclick: openSavePreset },
      icon("plus"), "Save current")),
    music.length ? group("Moves to the music", "music", music) : null);
}

function confirmDeletePreset(name) {
  openModal({
    title: `Delete “${name}”?`,
    body: h("p", { class: "hint", text: "The preset is removed from the app and the tray menu. Your lights don't change." }),
    ok: "Delete preset",
    onOk: async () => { await call("delete_preset", name); refresh(true); return true; },
  });
}

function openSavePreset() {
  const input = h("input", { class: "text-input", style: "width:100%", placeholder: "e.g. Late night", maxlength: "40" });
  openModal({
    title: "Save as preset",
    body: h("div", {}, h("p", { class: "hint", style: "margin-bottom:12px", text: "Saves the color, effect and brightness of every light as they are now." }), input),
    ok: "Save preset",
    onOk: async () => {
      if (!input.value.trim()) return "Give the preset a name.";
      const r = await callRaw("save_preset", input.value);
      if (!r.ok) return r.error;
      toast(`Saved ${input.value.trim()}`);
      refresh(true);
      return true;
    },
  });
  setTimeout(() => input.focus(), 30);
  input.addEventListener("keydown", e => { if (e.key === "Enter") $("#modal-ok").click(); });
}

/* ============================== the panel ============================== */

function openSheet() {
  S.sheetOpen = true;
  $("#sheet").hidden = false;
  render();
}
function closeSheet() {
  S.sheetOpen = false;
  S.selected = null;
  $("#sheet").hidden = true;
  render();
}

function renderTarget() {
  const ids = targetIds(), devs = ids.map(byId).filter(Boolean);
  const one = S.selected && devs.length === 1 ? devs[0] : null;
  $("#target-name").textContent = !S.selected ? "All lights" : one ? one.name : `${devs.length} lights`;
  const offline = devs.filter(d => !d.online).length;
  $("#target-meta").textContent = !S.selected
    ? `${devs.length} lights${offline ? ` · ${offline} offline` : ""}`
    : one ? [kindTitle(one), stripMeta(one).text].filter(Boolean).join(" · ") : devs.map(d => d.name).join(", ");
  const box = $("#target-icon"), ic = one ? typeIcon(one) : "presets";
  if (box.dataset.icon !== ic) { box.dataset.icon = ic; fill(box, icon(ic)); }
  $(".sheet-target").style.setProperty("--c", one ? glowColor(one) : "#f3f1ed");
  const anyOn = devs.some(d => d.state.on && d.online);
  const power = $("#sheet-power");
  power.classList.toggle("is-on", anyOn);
  power.title = anyOn ? "Turn off" : "Turn on";
  if (S.dragging !== "__sheet") {
    const avg = devs.length ? Math.round(devs.reduce((a, d) => a + d.state.brightness, 0) / devs.length) : 100;
    const cols = devs.filter(d => d.state.on && d.online).slice(0, 8).map(glowColor);
    setSlider($("#sheet-slider"), avg, cols.length > 1 ? `linear-gradient(90deg, ${cols.join(", ")})` : cols[0] || "#f3f1ed", !anyOn);
  }
}

/* ---- color tab ---- */

const SWATCHES = [
  ["Red", "#ff1a1a"], ["Amber", "#ff7a00"], ["Gold", "#ffc400"], ["Lime", "#8cff00"], ["Green", "#00e05a"],
  ["Teal", "#00e0c0"], ["Cyan", "#00c8ff"], ["Blue", "#1f4bff"], ["Violet", "#7c3dff"], ["Magenta", "#ff00c8"],
  ["Pink", "#ff5c8a"], ["Warm white", "#ffb46b"], ["Daylight", "#f2f5ff"],
];

function drawWheel() {
  const cv = $("#wheel"), ctx = cv.getContext("2d");
  const size = cv.width, R = size / 2, img = ctx.createImageData(size, size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - R + 0.5, dy = y - R + 0.5, r = Math.hypot(dx, dy) / R;
      if (r > 1.004) continue;
      const [cr, cg, cb] = hsvToRgb(Math.atan2(dy, dx) / (2 * Math.PI), Math.min(1, r), 1);
      const i = (y * size + x) * 4;
      img.data[i] = cr; img.data[i + 1] = cg; img.data[i + 2] = cb;
      img.data[i + 3] = r > 0.996 ? Math.round(255 * (1.004 - r) / 0.008) : 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}

function bindWheel() {
  const wrap = $(".wheel-wrap");
  const pick = e => {
    const r = wrap.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
    const sat = Math.min(1, Math.hypot(dx, dy) / (r.width / 2));
    sendTargets({ mode: "color", on: true, color: rgbToHex(hsvToRgb(Math.atan2(dy, dx) / (2 * Math.PI), sat, 1)) }, "color");
  };
  wrap.addEventListener("pointerdown", e => { wrap.setPointerCapture(e.pointerId); pick(e); });
  wrap.addEventListener("pointermove", e => { if (wrap.hasPointerCapture(e.pointerId)) pick(e); });
}

function bindKelvin() {
  const track = $("#kelvin");
  const stops = [];
  for (let k = 2200; k <= 6500; k += 430) stops.push(rgbToHex(kelvinToRgb(k)));
  track.style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
  const pickK = k => {
    S.kelvin = Math.round(k);
    track.setAttribute("aria-valuenow", S.kelvin);
    sendTargets({ mode: "color", on: true, color: rgbToHex(kelvinToRgb(S.kelvin)) }, "color");
  };
  const at = e => { const r = track.getBoundingClientRect(); return 2200 + clamp((e.clientX - r.left) / r.width, 0, 1) * 4300; };
  track.addEventListener("pointerdown", e => { track.setPointerCapture(e.pointerId); pickK(at(e)); });
  track.addEventListener("pointermove", e => { if (track.hasPointerCapture(e.pointerId)) pickK(at(e)); });
  track.addEventListener("keydown", e => {
    const step = { ArrowRight: 100, ArrowUp: 100, ArrowLeft: -100, ArrowDown: -100 }[e.key];
    if (step) { e.preventDefault(); pickK(clamp((S.kelvin || 4000) + step, 2200, 6500)); }
  });
}

function renderColor() {
  const p = primary();
  const color = p && hexToRgb(p.state.color) ? p.state.color : "#ffffff";
  const [hh, s] = rgbToHsv(hexToRgb(color));
  const handle = $("#wheel-handle");
  handle.style.left = `${50 + Math.cos(hh * 2 * Math.PI) * s * 50}%`;
  handle.style.top = `${50 + Math.sin(hh * 2 * Math.PI) * s * 50}%`;
  const hex = $("#hex");
  if (document.activeElement !== hex) hex.value = color.toUpperCase();
  const active = p && p.state.mode === "color" ? color.toLowerCase() : "";
  for (const b of $("#swatches").children) b.setAttribute("aria-pressed", String(b.dataset.color === active));
  const thumb = $(".kelvin-thumb");
  const showK = S.kelvin && active === rgbToHex(kelvinToRgb(S.kelvin));
  thumb.style.display = showK ? "block" : "none";
  if (showK) thumb.style.left = `${(S.kelvin - 2200) / 43}%`;
}

/* ---- synced effects tab ---- */

const rate = s => 0.03 * Math.pow(100, s / 100);
function hash01(i, seed) {
  let x = (Math.imul(i, 374761393) + Math.imul(seed, 668265263)) >>> 0;
  x = Math.imul(x ^ (x >>> 13), 1274126177) >>> 0;
  return ((x ^ (x >>> 16)) >>> 0) / 4294967295;
}
function noise(x, seed) {
  const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
  const a = hash01(i, seed), b = hash01(i + 1, seed);
  return a + (b - a) * u;
}
const FIRE = [[0, 0, 0], [120, 8, 0], [255, 50, 0], [255, 130, 10], [255, 210, 80]];
const SCREEN_DEMO = [[40, 90, 255], [255, 130, 50], [30, 200, 190], [200, 60, 120]];
/* A day in eight seconds, as glowstage/sun.py lights it: night, dawn, sunrise, day, sunset, evening. */
const SUN_DAY = [[51, 26, 0], [158, 98, 43], [194, 114, 54], [255, 224, 199], [255, 224, 199], [198, 118, 56], [158, 98, 43], [104, 59, 15]];

/* Same math as glowstage/effects.py, so the previews match what the lights do. */
const PREVIEW = {
  rainbow: (t, n, st) => Array.from({ length: n }, (_, i) => hsvToRgb(i / n - t * rate(st.speed) * 0.5, 1, 1)),
  spectrum: (t, n, st) => Array(n).fill(hsvToRgb(t * rate(st.speed) * 0.25, 1, 1)),
  breathing: (t, n, st) => {
    const ph = t * rate(st.speed) * 0.5, lvl = Math.pow((1 - Math.cos(2 * Math.PI * ph)) / 2, 1.5);
    return Array(n).fill(scaleRgb(hexToRgb(Math.floor(ph) % 2 ? st.color2 : st.color), 0.04 + 0.96 * lvl));
  },
  wave: (t, n, st) => {
    const a = hexToRgb(st.color), b = hexToRgb(st.color2), sh = t * rate(st.speed) * 0.5;
    return Array.from({ length: n }, (_, i) => lerp(a, b, (Math.sin(2 * Math.PI * (i / n - sh)) + 1) / 2));
  },
  comet: (t, n, st) => {
    const head = (t * rate(st.speed) * 0.5 % 1) * n, tail = Math.max(2, n * 0.3);
    const c = hexToRgb(st.color), bg = scaleRgb(hexToRgb(st.color2), 0.06);
    return Array.from({ length: n }, (_, i) => lerp(bg, c, Math.pow(Math.max(0, 1 - (((head - i) % n) + n) % n / tail), 2)));
  },
  twinkle: (t, n, st) => {
    const c = hexToRgb(st.color), bg = scaleRgb(hexToRgb(st.color2), 0.12), r = rate(st.speed);
    return Array.from({ length: n }, (_, i) => {
      const k = 0.5 + hash01(i, 11), p = hash01(i, 12);
      return lerp(bg, c, Math.pow(Math.max(0, Math.sin(2 * Math.PI * (t * r * k + p))), 6));
    });
  },
  fire: (t, n, st) => {
    const r = rate(st.speed) * 4;
    return Array.from({ length: n }, (_, i) => {
      const heat = 0.35 + 0.65 * (0.6 * noise(t * r + i * 3.7, 1) + 0.4 * noise(t * r * 2.3 + i * 5.1, 2));
      const pos = heat * (FIRE.length - 1), k = Math.min(Math.floor(pos), FIRE.length - 2);
      return lerp(FIRE[k], FIRE[k + 1], pos - k);
    });
  },
  aurora: (t, n, st) => {
    const r = rate(st.speed);
    return Array.from({ length: n }, (_, i) => {
      const x = i / n * 3;
      return hsvToRgb(0.33 + 0.42 * noise(x + t * r, 3), 0.85, 0.25 + 0.75 * noise(x * 1.7 - t * r * 1.3, 4));
    });
  },
  typing: (t, n, st) => {   /* someone typing: a key every 0.17 s, with pauses between words */
    const c = hexToRgb(st.color), bg = scaleRgb(hexToRgb(st.color2), 0.08), tau = 1.6 * Math.pow(0.12, st.speed / 100);
    const level = new Array(n).fill(0), step = 0.17, last = Math.floor(t / step);
    for (let k = last; k >= Math.max(0, last - Math.ceil(3 / step)); k--) {
      if (hash01(k, 21) < 0.22) continue;
      const age = t - k * step, x = hash01(k, 22) * 10;
      for (let i = 0; i < n; i++) {
        const d = Math.abs((i + 0.5) / n * 10 - x);
        level[i] += Math.exp(-((d / 0.6) ** 2)) * Math.exp(-age / tau)
          + 0.6 * Math.exp(-(((d - 6 * age / tau) / 1.0) ** 2)) * Math.exp(-age / (1.2 * tau));
      }
    }
    return level.map(v => lerp(bg, c, Math.min(1, v)));
  },
  daylight: (t, n) => {
    const k = (t * 1.0) % SUN_DAY.length, i = Math.floor(k);
    return Array(n).fill(lerp(SUN_DAY[i], SUN_DAY[(i + 1) % SUN_DAY.length], k - i));
  },
  screen_sound: (t, n, st) => {   /* the screen, and a shot every ~1.3 s, left or right */
    const base = PREVIEW.screen(t, n, st), shot = Math.floor(t / 1.3), age = t - shot * 1.3;
    const x = hash01(shot, 31) < 0.5 ? 0.2 : 0.8, k = Math.exp(-age / 0.09);
    return base.map((c, i) => lerp(c, [255, 236, 210], Math.min(1, k * (0.2 + 0.8 * Math.exp(-((((i + 0.5) / n - x) / 0.3) ** 2))))));
  },
  screen: (t, n) => {
    const k = (t * 0.25) % SCREEN_DEMO.length, i = Math.floor(k);
    return Array(n).fill(lerp(SCREEN_DEMO[i], SCREEN_DEMO[(i + 1) % SCREEN_DEMO.length], k - i));
  },
  /* Music previews run on a made-up 118 BPM track (see fakeAudio); the lights use real audio. */
  music_pulse: (t, n, st) => {
    const a = fakeAudio(t), base = hexToRgb(a.beat_count % 2 ? st.color2 : st.color);
    const flash = Math.exp(-a.since_beat / (0.08 + 0.45 * (1 - st.speed / 100)));
    return Array(n).fill(scaleRgb(base, Math.max(0.12 + 0.35 * a.level, flash)));
  },
  music_spectrum: (t, n) => {
    const b = fakeAudio(t).bands;
    return Array.from({ length: n }, (_, i) => {
      const p = i / (n - 1), f = p * (b.length - 1), k = Math.min(Math.floor(f), b.length - 2);
      return hsvToRgb(p * 0.78, 1, 0.05 + 0.95 * (b[k] + (b[k + 1] - b[k]) * (f - k)));
    });
  },
  music_show: (t, n) => {  // a few of the show's looks in turn: chase, alternate, center-out
    const a = fakeAudio(t), beat = t / (60 / 118), k = Math.floor(beat), look = Math.floor(beat / 8) % 3;
    const pal = [[0, 229, 255], [42, 75, 255], [255, 0, 200], [255, 255, 255]], c = pal[k % pal.length];
    if (look === 2 && beat % 8 > 7.6) return Array(n).fill([255, 255, 255]);  // a strobe hit into the next look
    const kick = Math.exp(-a.since_beat / 0.11);
    return Array.from({ length: n }, (_, i) => {
      const x = (i + 0.5) / n;
      if (look === 0) {
        const pos = Math.floor(k / 4) % 2 ? 1 - beat % 1 : beat % 1;
        return scaleRgb(c, 0.08 + 0.92 * Math.max(0, 1 - Math.abs(x - pos) / 0.25) ** 2);
      }
      if (look === 1) return (Math.floor(x * 6) + k) % 2 ? scaleRgb(pal[(k + 1) % pal.length], 0.08) : scaleRgb(c, 0.4 + 0.6 * kick);
      return scaleRgb(c, 0.06 + 0.94 * Math.exp(-(((Math.abs(x - 0.5) * 2 - beat % 1) / 0.2) ** 2)));
    });
  },
  music_party: (t, n, st) => {
    const a = fakeAudio(t), punch = st.speed / 100, step = 0.05 + 0.07 * punch;
    const hue = (a.beat_count - 1 + 1 - Math.exp(-a.since_beat / (0.6 - 0.4 * punch))) * step;
    const swell = Math.exp(-a.since_beat / (0.25 + 0.35 * (1 - punch)));
    const v = Math.min(1, 0.4 + 0.35 * a.level + (0.15 + 0.15 * punch) * swell);
    return Array.from({ length: n }, (_, i) => hsvToRgb(hue + 0.1 * i / n, 1, v));
  },
  music_ripple: (t, n, st) => {
    const a = fakeAudio(t), travel = 0.6 + 1.6 * st.speed / 100;
    return Array.from({ length: n }, (_, i) => {
      const x = i / (n - 1);
      let v = 0.05 + 0.18 * a.level;
      for (const age of a.beats_ago) { const d = Math.abs(x - age * travel); if (d < 0.12) v += (1 - d / 0.12) * Math.exp(-age * 1.2); }
      return hsvToRgb(t * 0.04 + x * 0.35, 1, Math.min(1, v));
    });
  },
  music_vu: (t, n, st) => {
    const a = fakeAudio(t), lo = hexToRgb(st.color), hi = hexToRgb(st.color2), lit = a.level * n;
    return Array.from({ length: n }, (_, i) => (i < lit ? lerp(lo, hi, i / (n - 1)) : scaleRgb(lo, 0.04)));
  },
  music_romantic: (t, n, st) => {
    const a = fakeAudio(t), c1 = hexToRgb(st.color), c2 = hexToRgb(st.color2), r = rate(st.speed) * 0.25;
    const env = 0.35 + 0.4 * Math.exp(-a.since_beat / 0.5);
    return Array.from({ length: n }, (_, i) => {
      const x = i / (n - 1), mix = clamp(0.5 + 0.35 * Math.sin(2 * Math.PI * (x * 1.2 - t * r)), 0, 1);
      let swell = 0;
      for (const age of a.beats_ago) {
        const d = Math.abs(Math.abs(x - 0.5) * 2 - age * 0.9);
        if (d < 0.35) swell += (1 - d / 0.35) * Math.exp(-age * 1.4) * 0.35;
      }
      return scaleRgb(lerp(c1, c2, mix), Math.min(1, 0.12 + 0.88 * env + swell));
    });
  },
  music_organ: (t, n) => {
    const a = fakeAudio(t), avg = (s, e) => a.bands.slice(s, e).reduce((x, y) => x + y, 0) / (e - s);
    const b = avg(0, 4), m = avg(4, 10), h = avg(10, 16);
    return Array.from({ length: n }, (_, i) => {
      const x = i / (n - 1);
      return [b * (0.55 + 0.45 * (1 - x)), m * (0.55 + 0.45 * (1 - Math.abs(x - 0.5) * 2)), h * (0.55 + 0.45 * x)].map(v => Math.min(1, v) * 255);
    });
  },
  music_bloom: (t, n, st) => {
    const a = fakeAudio(t), c1 = hexToRgb(st.color), c2 = hexToRgb(st.color2);
    const size = Math.min(1, 0.75 * a.level + 0.35 * Math.exp(-a.since_beat / 0.25) * 0.8);
    return Array.from({ length: n }, (_, i) => {
      const d = Math.abs(i / (n - 1) - 0.5) * 2;
      return d <= size ? lerp(c1, c2, d) : scaleRgb(c1, 0.04);
    });
  },
  music_sparkle: (t, n, st) => {
    const a = fakeAudio(t), decay = 0.25 + 0.6 * (1 - st.speed / 100);
    return Array.from({ length: n }, (_, i) => {
      const lit = hash01(i * 31 + a.beat_count * 131, 5) < 0.22;
      const v = lit ? Math.exp(-a.since_beat / decay) : 0;
      const glitter = hash01(i * 17 + Math.floor(t * 12), 6) < 0.05 ? 0.8 : 0;
      return v >= glitter ? hsvToRgb(hash01(i + a.beat_count * 7, 8), 1, Math.max(0.04, v)) : hsvToRgb(0, 0, glitter);
    });
  },
  music_meteor: (t, n, st) => {
    const a = fakeAudio(t), travel = 0.9 + 1.8 * st.speed / 100, tail = 0.28, first = a.beat_count - a.beats_ago.length + 1;
    const out = Array.from({ length: n }, () => [0, 0, 0]);
    a.beats_ago.slice().reverse().forEach((age, j) => {
      const index = a.beat_count - j, head = age * travel, color = hsvToRgb(hash01(index, 11), 0.9, 1);
      for (let i = 0; i < n; i++) {
        const x = i / (n - 1), d = head - (index % 2 === 0 ? x : 1 - x);
        if (d >= 0 && d < tail) {
          const c = scaleRgb(color, Math.pow(1 - d / tail, 2) * Math.exp(-age * 0.6));
          out[i] = out[i].map((p, k) => Math.max(p, c[k]));
        }
      }
    });
    return out;
  },
  music_fire: (t, n, st) => {
    const a = fakeAudio(t), phase = t * rate(st.speed) * (1.5 + 6 * a.level * 0.7), glow = 0.35 + 0.65 * a.level;
    return Array.from({ length: n }, (_, i) => {
      const heat = Math.min(1, 0.15 + (0.45 + 0.4 * a.level) * (0.6 * noise(phase + i * 3.7, 1) + 0.4 * noise(phase * 2.3 + i * 5.1, 2)));
      const pos = heat * (FIRE.length - 1), k = Math.min(Math.floor(pos), FIRE.length - 2);
      return scaleRgb(lerp(FIRE[k], FIRE[k + 1], pos - k), glow);
    });
  },
  music_aurora: (t, n, st) => {
    const a = fakeAudio(t), phase = t * rate(st.speed) * (0.3 + 2.5 * a.level * 0.6);
    return Array.from({ length: n }, (_, i) => {
      const x = i / n * 3;
      return hsvToRgb(0.33 + 0.42 * noise(x + phase, 3), 0.85, Math.min(1, (0.2 + 0.6 * a.level) * (0.35 + 0.65 * noise(x * 1.7 - phase * 1.3, 4))));
    });
  },
  music_heart: (t, n, st) => {
    const a = fakeAudio(t), c1 = hexToRgb(st.color), c2 = hexToRgb(st.color2), w = 0.05 + 0.05 * (1 - st.speed / 100);
    const s = a.since_beat, v = Math.max(Math.exp(-(((s - 0.04) / w) ** 2)), 0.65 * Math.exp(-(((s - 0.04 - 3.2 * w) / w) ** 2)));
    const rest = 0.1 + 0.15 * a.level;
    return Array(n).fill(scaleRgb(lerp(c2, c1, v), rest + (1 - rest) * v));
  },
};
function fakeAudio(t) {
  const period = 60 / 118, beat = Math.floor(t / period), since = t - beat * period;
  const kick = Math.exp(-since / 0.2);
  return {
    level: Math.min(1, 0.45 + 0.4 * kick), beat_count: beat, since_beat: since,
    beats_ago: [0, 1, 2, 3].map(k => since + k * period),
    bands: Array.from({ length: 16 }, (_, i) => clamp((i < 4 ? 0.35 + 0.6 * kick : 0.2) + 0.55 * noise(t * 2.5 + i * 0.9, 9) - 0.15, 0, 1)),
  };
}

function buildEffects() {
  const sig = S.data.effects.map(e => e.id).join();
  if (S.fxBuilt === sig) return;
  S.fxBuilt = sig;
  const card = e => h("button", {
    class: "fx-card" + (e.group === "music" ? " fx-music" : ""), type: "button", "data-fx": e.id,
    onclick: () => sendTargets({ mode: "effect", on: true, effect: e.id }, "fx"),
  }, h("canvas", { height: "14" }), h("span", { class: "fx-name", text: e.name, title: e.name }));
  const anim = S.data.effects.filter(e => e.group !== "music" && e.group !== "game");
  const music = S.data.effects.filter(e => e.group === "music");
  fill($("#fx-grid"), 
    h("div", { class: "fx-group" }, icon("spark"), "Animations"),
    ...anim.map(card),
    music.length ? h("div", { class: "fx-group" }, icon("music"), "Moves to the music",
      h("span", { class: "fx-group-hint", text: "Follows what your PC plays" })) : null,
    ...music.map(card));
}

function currentFxState() {
  const p = primary();
  return p ? p.state : { speed: 50, color: "#7c4dff", color2: "#00e5ff", effect: "rainbow", mode: "color" };
}

function renderEffects() {
  buildEffects();
  const st = currentFxState();
  for (const card of document.querySelectorAll("#fx-grid .fx-card")) {
    card.setAttribute("aria-pressed", String(st.mode === "effect" && st.effect === card.dataset.fx));
  }
  const speed = $("#fx-speed");
  if (document.activeElement !== speed) speed.value = st.speed;
  $("#fx-speed-val").textContent = st.speed;
  if (document.activeElement !== $("#fx-c1")) $("#fx-c1").value = hexToRgb(st.color) ? st.color : "#7c4dff";
  if (document.activeElement !== $("#fx-c2")) $("#fx-c2").value = hexToRgb(st.color2) ? st.color2 : "#00e5ff";
  const fx = st.mode === "effect" && S.data.effects.find(e => e.id === st.effect);
  const usesColor = !fx || fx.uses.includes("color");
  $("#fx-colors").style.display = usesColor ? "" : "none";
  speed.closest(".field-row").style.display = !fx || fx.uses.includes("speed") ? "" : "none";
  $("#fx-flow").hidden = !(fx && fx.flows);
  $("#fx-flow-on").checked = !!(S.data.settings || {}).flow;
  const audio = S.data.audio || {};
  $("#fx-note").textContent = !fx ? "Pick an effect to start it."
    : fx.id === "screen" ? "Follows the main colors on your primary display. Great for the monitor strip during games and films."
    : fx.id === "screen_sound" ? `Follows your screen, and every shot or explosion in the game flashes the lights on the side it came from. Listening to ${audio.device || "your PC's sound output"}.`
    : fx.id === "daylight" ? sunNote(S.data.sun)
    : fx.id === "typing" ? "Every key you press lights up and sends a ripple across your keyboard; your other lights glow where the key sits, left to right. Speed sets how quickly it fades. Glowstage only looks at which key is down while this runs, keeps that for three seconds, and saves and sends nothing."
    : fx.id === "music_show" ? showNote(S.data.show, audio)
    : fx.group === "music" ? `Listening to ${audio.device || "your PC's sound output"}. Speed sets how punchy it is; when the music stops, the lights glow softly.`
    : fx.uses.includes("color") ? "" : "This effect uses its own colors.";
}

function sunNote(sun) {
  const how = "Cool and bright by day, golden as the sun sets, warm in the evening and a low amber glow at night.";
  if (!sun) return how;
  const times = sun.sunrise && sun.sunset ? `: sunrise ${sun.sunrise}, sunset ${sun.sunset}` : "";
  return `Follows the sun in ${sun.place}${times}. Now: ${sun.phase}, ${sun.kelvin} K. ${how}`;
}

const SHOW_SECTIONS = { groove: "groove", break: "break", build: "build-up", gap: "about to drop", drop: "drop",
  calm: "calm part", silent: "waiting for music" };

function showNote(show, audio) {
  const ear = `Listening to ${audio.device || "your PC's sound output"}.`;
  if (!show) return `${ear} A festival-style show across all your lights, styled by the genre the AI hears. Speed sets how wild it gets.`;
  const genre = show.ai
    ? `AI hears ${show.style}${show.genres.length ? ` (${show.genres.slice(0, 2).join(", ")})` : ""}${show.mood ? `, ${show.mood.toLowerCase()}` : ""}`
    : show.aiError ? `${show.aiError}; style from the tempo: ${show.style}` : `Style: ${show.style} (the AI is still listening)`;
  const tempo = show.bpm ? ` · ${show.bpm} BPM${show.half ? " (half time)" : ""}` : "";
  const chord = show.chord ? ` · colors follow the chords (${show.chord})` : "";
  return `${genre} · ${SHOW_SECTIONS[show.section] || show.section}${tempo}${chord}. Speed sets how wild it gets.`;
}

/* ---- light show page ---- */

const inShow = d => !(S.data.showExcluded || []).includes(d.id);
const showLights = () => visible().filter(d => d.online && inShow(d));
const runningShow = () => visible().filter(d => d.state.on && d.state.mode === "effect" && d.state.effect === "music_show");
const showRunning = () => runningShow().length > 0;

/* Which lights join the show: the rest keep their own look while it runs. */
function openShowLights() {
  const lights = visible().filter(d => d.online || inShow(d));
  const boxes = new Map();
  const row = d => {
    const box = h("input", { type: "checkbox", class: "switch", checked: inShow(d) });
    boxes.set(d.id, box);
    return h("label", { class: "check-row" },
      h("span", { class: "check-text" }, h("span", { class: "check-name", text: d.name }),
        h("span", { class: "muted", text: d.group === "pc" ? "In your PC" : "In your room" })), box);
  };
  openModal({
    title: "Lights in the show",
    body: [h("p", { class: "hint", text: "Lights you switch off here keep their own look while the show runs, like a desk lamp that stays white." }),
      h("div", { class: "check-list" }, lights.map(row))],
    ok: "Save",
    onOk: async () => {
      const excluded = [...boxes].filter(([, b]) => !b.checked).map(([id]) => id);
      const r = await callRaw("set_show_excluded", excluded);
      if (!r || r.ok === false) return (r && r.error) || "Couldn't save";
      // A light taken out while the show runs goes back to its color right away.
      const leaving = runningShow().filter(d => excluded.includes(d.id)).map(d => d.id);
      if (leaving.length) send(leaving, { mode: "color" }, "fx:show-out");
      refresh(true);
      return true;
    },
  });
}

function renderShow() {
  const running = runningShow(), all = showLights();
  const btn = $("#show-start");
  btn.textContent = running.length ? "Stop the show" : "Start the show";
  btn.classList.toggle("btn-primary", !running.length);
  btn.classList.toggle("is-running", !!running.length);
  const audio = S.data.audio || {};
  const out = visible().filter(d => d.online && !inShow(d)).length;
  $("#show-sub").textContent = (running.length && S.data.auto === "music"
      ? `Started by itself for the music · back to your lights after 30 s of quiet`
      : running.length ? `Running on ${running.length} of ${all.length} lights`
      : out ? `Uses ${all.length} lights, ${out} sit${out === 1 ? "s" : ""} it out` : `Uses all ${all.length} lights`)
    + ` · listening to ${audio.device || "your PC's sound output"}`;
  const p = running[0] || all[0];
  if (S.dragging !== "__intensity") {
    const v = p ? p.state.speed : 70;
    setSlider($("#show-intensity"), v, null, false);
    $("#show-int-val").textContent = `${v}%`;
  }
  $("#show-strobe").checked = S.data.settings.show_strobe !== false;
  $("#show-auto").checked = !!S.data.settings.auto_show;
  const g = S.data.genreAi;
  $("#show-how").textContent = g && g.installed
    ? "Listens to what your PC plays (no microphone), hears which of 400 music styles it is with the enhanced genre AI on this PC, and follows the beat, build-ups and drops."
    : "Listens to what your PC plays (no microphone), hears the genre with a small AI model on this PC, and follows the beat, build-ups and drops.";
  $("#show-ai-get").hidden = !g || g.installed;
  renderSync(audio);
}

/* Sync: the slider runs from 100 ms earlier to 400 ms later, in steps of 5 ms (0 sits at 20%). */
const SYNC_MIN = -100, SYNC_STEP = 5;
const syncToSlider = ms => Math.round((ms - SYNC_MIN) / SYNC_STEP);
const sliderToSync = v => SYNC_MIN + v * SYNC_STEP;
const syncText = ms => ms === 0 ? "0 ms" : `${ms > 0 ? "+" : "−"}${Math.abs(ms)} ms`;

function renderSync(audio) {
  const sync = audio.sync || { ms: 0, auto: true };
  const el = $("#show-sync");
  el.style.setProperty("--zero", (syncToSlider(0) / 100).toFixed(3));
  if (S.dragging !== "__sync") {
    setSlider(el, syncToSlider(sync.ms), null, false);
    el.setAttribute("aria-valuetext", syncText(sync.ms));
    $("#show-sync-val").textContent = syncText(sync.ms);
  }
  const out = audio.device ? audio.device.replace(/^\w+ \((?:\d+- )?(.+)\)$/, "$1") : "";
  $("#show-sync-hint").textContent = !sync.auto
    ? `Set for ${out || "every sound output"}.`
    : audio.kind === "bluetooth" ? "Bluetooth plays the sound about 0.2 s late, so the lights wait for it."
    : audio.kind === "remote" ? "Remote Desktop delays the sound. Lights ahead of the music? Slide right."
    : "Lights ahead of the music? Slide right. Behind it? Slide left.";
  $("#show-sync-auto").hidden = !!sync.auto;
}

/* Windows' "Show animations" off (prefers-reduced-motion): the previews hold still, and the live
   cards and the stage follow the lights a few times a second instead of smoothly. */
const STILL = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };

let fxFrame = 0;
function animatePreviews(ts) {
  requestAnimationFrame(animatePreviews);
  if (!S.sheetOpen || S.tab !== "effects" || !S.data || ts - fxFrame < (STILL.matches ? 500 : 33)) return;
  fxFrame = ts;
  const st = currentFxState(), t = STILL.matches ? 2.5 : ts / 1000, dpr = window.devicePixelRatio || 1;
  for (const card of document.querySelectorAll("#fx-grid .fx-card")) {
    const cv = card.firstChild, w = Math.round(cv.clientWidth * dpr), hgt = Math.round(14 * dpr);
    if (!w) continue;
    if (cv.width !== w || cv.height !== hgt) { cv.width = w; cv.height = hgt; }
    const ctx = cv.getContext("2d");
    const n = Math.max(8, Math.floor(cv.clientWidth / 9));
    const colors = PREVIEW[card.dataset.fx] ? PREVIEW[card.dataset.fx](t, n, st) : [];
    ctx.clearRect(0, 0, w, hgt);
    const step = w / n, rad = Math.min(step * 0.34, hgt * 0.32);
    colors.forEach((c, i) => {
      const css = `rgb(${c.map(Math.round).join(",")})`;
      ctx.beginPath();
      ctx.fillStyle = css;
      ctx.shadowColor = css;
      ctx.shadowBlur = 6 * dpr;
      ctx.arc(step * (i + 0.5), hgt / 2, rad, 0, Math.PI * 2);
      ctx.fill();
    });
  }
}

/* ---- built-in effects tab ---- */

function nativeDevice() {
  if (!S.selected || S.selected.length !== 1) return null;
  const d = byId(S.selected[0]);
  return d && d.hasNative ? d : null;
}

async function renderNative() {
  const d = nativeDevice();
  $("#native-empty").hidden = !!d;
  $("#native-body").hidden = !d;
  if (!d) {
    const one = S.selected && S.selected.length === 1 && byId(S.selected[0]);
    $("#native-empty").querySelector(".empty-title").textContent =
      one && !one.online ? `${one.name} is offline` : one ? `${one.name} has no built-in effects` : "Pick one strip or panel set";
    return;
  }
  if (!S.native[d.id]) {
    const opts = await call("native_options", d.id);
    if (!opts) return;
    S.native[d.id] = opts;
  }
  const opts = S.native[d.id];
  const query = $("#native-search").value.trim().toLowerCase();
  const sig = d.id + "|" + query;
  const list = $("#native-list");
  if (S.nativeBuiltFor !== sig) {
    S.nativeBuiltFor = sig;
    const items = opts.effects.filter(e => !query || e.name.toLowerCase().includes(query));
    fill(list, ...items.map(e => h("button", {
      class: "native-item", type: "button", "data-id": e.id, title: e.name, text: e.name,
      onclick: () => send([d.id], { mode: "native", native: e.id, on: true }, "native:" + d.id),
    })));
    if (!items.length) list.append(h("p", { class: "hint", text: `No effects match “${query}”.` }));
    const pal = $("#native-palette");
    fill(pal, h("option", { value: "", text: "Effect default" }),
      ...opts.palettes.map(p => h("option", { value: String(p.id), text: p.name })));
  }
  const cur = d.state.mode === "native" ? String(d.state.native) : null;
  for (const b of list.children) if (b.dataset) b.setAttribute("aria-pressed", String(b.dataset.id === cur));
  $("#native-palette-wrap").hidden = !opts.palettes.length;
  $("#native-speed-wrap").hidden = d.kind === "nanoleaf" || d.kind === "qmk";
  const pal = $("#native-palette");
  if (document.activeElement !== pal) pal.value = d.state.palette == null ? "" : String(d.state.palette);
  const sp = $("#native-speed");
  if (document.activeElement !== sp) sp.value = d.state.speed;
}

/* ============================== modal and toasts ============================== */

let modalOk = null;
let modalFrom = null;      // what had the focus before the window opened: it gets it back
function openModal({ title, body, ok, onOk, wide = false, cancel = true }) {
  if ($("#modal").hidden) modalFrom = document.activeElement;
  $("#modal-title").textContent = title;
  $("#modal-cancel").hidden = !cancel;     // (a window with only information needs just its one button)
  $(".modal-card").classList.toggle("is-wide", wide);
  fill($("#modal-body"), body);
  $("#modal-ok").textContent = ok;
  $("#modal-ok").disabled = false;
  $("#modal-error").hidden = true;
  modalOk = onOk;
  $("#modal").hidden = false;
  setTimeout(() => $("#modal-ok").focus(), 20);
}
function closeModal() {
  $("#modal").hidden = true;
  modalOk = null;
  if (modalFrom && modalFrom.isConnected && typeof modalFrom.focus === "function") modalFrom.focus();
  modalFrom = null;
}

/** Tab and Shift+Tab stay inside an open window (it covers the page). */
function trapFocus(e) {
  if (e.key !== "Tab" || $("#modal").hidden) return;
  const all = [...$(".modal-card").querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
    .filter(el => !el.disabled && !el.hidden && el.offsetParent !== null);
  if (!all.length) return;
  const first = all[0], last = all[all.length - 1];
  if (e.shiftKey && (document.activeElement === first || !$(".modal-card").contains(document.activeElement))) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && (document.activeElement === last || !$(".modal-card").contains(document.activeElement))) {
    e.preventDefault(); first.focus();
  }
}

function toast(msg, isError = false, action = null) {
  const t = h("div", { class: "toast" + (isError ? " is-error" : ""), role: isError ? "alert" : "status" },
    h("span", { text: msg }),
    action ? h("button", { class: "toast-action", type: "button", text: action.label,
      onclick: () => { t.remove(); action.run(); } }) : null);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isError ? 6000 : action ? 6000 : 2600);
}

/** A toast for a big change, with Undo: back to the lights as they were before it. */
const undoNow = async () => {
  S.undoReady = false;
  await call("undo"); S.quietUntil = 0; refresh(true); toast("Your lights are back as they were");
};
const undoable = msg => { S.undoReady = true; toast(msg, false, { label: "Undo", run: undoNow }); };

/* ============================== wiring ============================== */

function switchTab(tab) {
  S.tab = tab;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const id of ["color", "effects", "native"]) $(`#tab-${id}`).hidden = id !== tab;
  render();
}

function bindStatic() {
  for (const b of document.querySelectorAll(".nav-item[data-page]")) b.addEventListener("click", () => go(b.dataset.page));
  $("#support").addEventListener("click", openSupport);
  $("#see-presets").addEventListener("click", () => go("presets"));
  $("#home-setup").addEventListener("click", () => go("setup"));
  $("#all-color").addEventListener("click", () => { S.selected = null; openSheet(); });
  $("#all-power").addEventListener("click", () => toggleStrip("__all"));
  $("#sleep-timer").addEventListener("click", openSleepTimer);
  bindSlider($("#master-slider"), "__all", v => send(null, { brightness: v }, "bri:all"));
  $("#sheet-close").addEventListener("click", closeSheet);
  $("#sheet-power").addEventListener("click", () => {
    const anyOn = targetIds().map(byId).some(d => d && d.state.on && d.online);
    sendTargets({ on: !anyOn }, "on:sheet");
  });
  bindSlider($("#sheet-slider"), "__sheet", v => sendTargets({ brightness: v }, "bri:sheet"));

  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => switchTab(b.dataset.tab));
  $("#tab-native").addEventListener("input", e => { if (e.target.id === "native-search") renderNative(); });

  drawWheel();
  bindWheel();
  bindKelvin();
  fill($("#swatches"), ...SWATCHES.map(([name, c]) => h("button", {
    class: "swatch", type: "button", title: name, "aria-label": name, "data-color": c, style: `background:${c}`,
    onclick: () => sendTargets({ mode: "color", on: true, color: c }, "color"),
  })));
  const hex = $("#hex");
  const commitHex = () => {
    const rgb = hexToRgb(hex.value);
    if (rgb) sendTargets({ mode: "color", on: true, color: rgbToHex(rgb) }, "color");
    else { toast("Use a hex color like #FF8800.", true); render(); }
  };
  hex.addEventListener("keydown", e => { if (e.key === "Enter") { commitHex(); hex.blur(); } });
  hex.addEventListener("change", commitHex);

  $("#fx-speed").addEventListener("input", e => sendTargets({ speed: Number(e.target.value) }, "speed"));
  $("#fx-flow-on").addEventListener("change", async e => {
    await call("set_setting", "flow", e.target.checked);
    toast(e.target.checked ? "Your animations now run from light to light. Light show → Arrange sets where each light is"
      : "Each light runs its own animation again");
    refresh(true);
  });
  $("#fx-c1").addEventListener("input", e => sendTargets({ color: e.target.value }, "c1"));
  $("#fx-c2").addEventListener("input", e => sendTargets({ color2: e.target.value }, "c2"));
  $("#native-speed").addEventListener("input", e => { const d = nativeDevice(); if (d) send([d.id], { speed: Number(e.target.value) }, "nspeed"); });
  $("#native-palette").addEventListener("change", e => {
    const d = nativeDevice();
    if (d) send([d.id], { palette: e.target.value === "" ? null : Number(e.target.value) }, "npal");
  });

  $("#save-preset").addEventListener("click", openSavePreset);
  $("#show-start").addEventListener("click", () => {
    const running = runningShow();
    if (running.length) send(running.map(d => d.id), { mode: "color" }, "fx:show");
    else send(showLights().map(d => d.id), { mode: "effect", on: true, effect: "music_show" }, "fx:show");
  });
  $("#show-lights").addEventListener("click", openShowLights);
  bindSlider($("#show-intensity"), "__intensity", v => {
    $("#show-int-val").textContent = `${v}%`;
    const running = runningShow();
    send(running.length ? running.map(d => d.id) : null, { speed: v }, "speed:show");
  }, 1);
  $("#show-strobe").addEventListener("change", async e => { await call("set_setting", "show_strobe", e.target.checked); refresh(true); });
  $("#show-auto").addEventListener("change", async e => {
    await call("set_setting", "auto_show", e.target.checked);
    if (e.target.checked) toast("The show starts by itself when music plays");
    refresh(true);
  });
  let syncTimer = 0;
  bindSlider($("#show-sync"), "__sync", v => {
    const ms = sliderToSync(v);
    $("#show-sync-val").textContent = syncText(ms);
    $("#show-sync").setAttribute("aria-valuetext", syncText(ms));
    clearTimeout(syncTimer);   // (sent once the slider rests for 80 ms, not once per pixel dragged)
    syncTimer = setTimeout(() => call("set_show_sync", ms), 80);
  });
  $("#show-sync-auto").addEventListener("click", async () => { await call("set_show_sync", null); refresh(true); });
  $("#show-ai-get").addEventListener("click", () => {
    go("setup");
    const sec = $("#set-genre");
    if (sec) { sec.scrollIntoView({ behavior: "smooth", block: "center" }); sec.classList.add("is-flash"); setTimeout(() => sec.classList.remove("is-flash"), 1600); }
  });
  bindSetup();
  bindSearch();
  $("#modal-cancel").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
  $("#modal-ok").addEventListener("click", async () => {
    if (!modalOk) return closeModal();
    $("#modal-ok").disabled = true;
    const res = await modalOk();
    $("#modal-ok").disabled = false;
    if (res === true) closeModal();
    else { $("#modal-error").textContent = res || "That didn't work."; $("#modal-error").hidden = false; }
  });
  document.addEventListener("keydown", e => {
    trapFocus(e);
    if (e.key !== "Escape") return;
    if (!$("#modal").hidden) closeModal();
    else if (S.arrange) { S.arrange = false; renderStage(); }
    else if (S.sheetOpen) closeSheet();
  });
}

/* ===== quick search ===== */
/* Ctrl+K (or the Search button): a small box over the page. Type a few letters, pick a preset, light,
   effect, place or action, press Enter. */

/** What a click on a preset's card does: switch it on, and offer Undo. */
async function applyPreset(p) {
  await call("apply_preset", p.name); S.quietUntil = 0; refresh(true); undoable(`${p.name} is on`);
}

const QS = { open: false, items: [], shown: [], active: 0, from: null, root: null, input: null, list: null, empty: null };
const QS_KINDS = ["Action", "Preset", "Light", "Effect", "Go to"];   // (also the order ties keep)

/** Everything the box can run right now, in group order: actions, presets, lights, effects, places. */
function searchItems() {
  const d = S.data;
  if (!d) return [];
  const items = [];
  const add = (kind, name, run) => items.push({ kind, name, run, id: "qs-opt-" + items.length });
  const anyOn = visible().some(x => x.state.on && x.online);
  add(0, "All lights on", () => send(null, { on: true }, "on:all"));
  add(0, "All lights off", () => { if (anyOn) toggleStrip("__all"); else toast("Your lights are already off"); });
  add(0, "Sleep timer", openSleepTimer);
  add(0, runningShow().length ? "Stop the light show" : "Start the light show", () => $("#show-start").click());
  // (the window can't ask the app whether there is something to undo: it knows what it just offered Undo for)
  if (d.canUndo === undefined ? S.undoReady : d.canUndo) add(0, "Undo", undoNow);
  add(0, "Report a problem", openReport);
  for (const p of d.presets || []) add(1, p.name, () => applyPreset(p));
  for (const l of visible()) add(2, l.name, () => selectStrip(l.id, false));
  for (const e of d.effects || []) add(3, e.name, () => {
    S.selected = null;                                  // (every light, as with all lights chosen in Color & effects)
    sendTargets({ mode: "effect", on: true, effect: e.id }, "fx");
    toast(`${e.name} is on for all lights`);
  });
  for (const [name, page] of [["Home", "home"], ["Presets", "presets"], ["Light show", "show"], ["Setup", "setup"]]) {
    add(4, name, () => go(page));
  }
  for (const [name, id] of [["Setup: Lights", "set-lights"], ["Setup: Add a light", "add-section"], ["Setup: PC lighting", "set-pc"],
    ["Setup: Genre AI", "set-genre"], ["Setup: Games", "set-games"], ["Setup: Automatic", "set-automatic"],
    ["Setup: Schedule", "set-schedule"], ["Setup: Phone remote", "set-phone"], ["Setup: Backups", "set-backups"],
    ["Setup: App", "set-app"]]) {
    add(4, name, () => id.startsWith("set-") ? showNew("setup:" + id.slice(4)) : goToSection(id));
  }
  return items;
}

/** Setup, scrolled to one section (the sections that have no "set-" name). */
function goToSection(id) {
  go("setup");
  setTimeout(() => { const s = document.getElementById(id); if (s) s.scrollIntoView({ block: "start", behavior: "smooth" }); }, 60);
}

/** Names that start with the letters first, then names with a word that does, then names that contain them. */
function searchMatches(items, query) {
  const q = query.trim().toLowerCase();
  if (!q) return [...items.filter(i => i.kind === 0), ...items.filter(i => i.kind === 1).slice(0, 4)];
  const rank = name => {
    const n = name.toLowerCase();
    if (n.startsWith(q)) return 0;
    if (n.split(/[^a-z0-9]+/).some(w => w.startsWith(q))) return 1;
    return n.includes(q) ? 2 : -1;
  };
  return items.map((it, i) => ({ it, i, r: rank(it.name) })).filter(x => x.r >= 0)
    .sort((a, b) => a.r - b.r || a.it.kind - b.it.kind || a.i - b.i).map(x => x.it);
}

function buildSearch() {
  const input = h("input", {
    class: "qs-input", type: "text", role: "combobox", autocomplete: "off", spellcheck: "false",
    placeholder: "Search presets, lights, effects, settings…", "aria-label": "Search",
    "aria-expanded": "true", "aria-controls": "qs-list", "aria-autocomplete": "list", "aria-activedescendant": "",
  });
  const list = h("ul", { class: "qs-list", id: "qs-list", role: "listbox", "aria-label": "Results" });
  const empty = h("p", { class: "qs-empty muted", role: "status", text: "Nothing matches that", hidden: "" });
  const root = h("div", { class: "qs", id: "qs", hidden: "" },
    h("div", { class: "qs-box", role: "dialog", "aria-modal": "true", "aria-label": "Search" }, input, list, empty));
  document.body.append(root);
  QS.root = root; QS.input = input; QS.list = list; QS.empty = empty;
  input.addEventListener("input", () => { QS.active = 0; renderSearch(); });
  root.addEventListener("mousedown", e => {
    if (e.target === root) closeSearch();                 // (a click on the dimmed page closes it)
    else if (e.target !== input) e.preventDefault();      // (clicking a row keeps the focus in the box)
  });
  list.addEventListener("click", e => {
    const row = e.target.closest(".qs-row");
    if (row) runSearch(QS.shown.find(i => i.id === row.id));
  });
  list.addEventListener("mousemove", e => {
    const row = e.target.closest(".qs-row");
    const at = row ? QS.shown.findIndex(i => i.id === row.id) : -1;
    if (at >= 0 && at !== QS.active) { QS.active = at; markSearch(false); }
  });
  root.addEventListener("keydown", e => {
    if (e.isComposing) return;
    const n = QS.shown.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (n) { QS.active = (QS.active + (e.key === "ArrowDown" ? 1 : n - 1)) % n; markSearch(true); }
    } else if (e.key === "Enter") { e.preventDefault(); runSearch(QS.shown[QS.active]); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSearch(); }
    else if (e.key === "Tab") e.preventDefault();         // (the box is only the field: focus stays in it)
  });
}

function renderSearch() {
  QS.items = searchItems();
  QS.shown = searchMatches(QS.items, QS.input.value);
  QS.active = Math.min(QS.active, Math.max(0, QS.shown.length - 1));
  fill(QS.list, QS.shown.map(it => h("li", { class: "qs-row", id: it.id, role: "option", "aria-selected": "false" },
    h("span", { class: "qs-name", text: it.name }), h("span", { class: "qs-kind", text: QS_KINDS[it.kind] }))));
  QS.empty.hidden = QS.shown.length > 0;
  QS.input.setAttribute("aria-expanded", String(QS.shown.length > 0));
  markSearch(true);
}

/** Highlight the chosen row (and keep it in view when the keys moved it). */
function markSearch(scroll) {
  const cur = QS.shown[QS.active];
  for (const row of QS.list.children) {
    const on = !!cur && row.id === cur.id;
    row.setAttribute("aria-selected", String(on));
    if (on && scroll) row.scrollIntoView({ block: "nearest" });
  }
  QS.input.setAttribute("aria-activedescendant", cur ? cur.id : "");
}

function openSearch() {
  if (QS.open || !S.data) return;
  if (!QS.root) buildSearch();
  QS.from = document.activeElement;
  QS.open = true; QS.active = 0;
  QS.input.value = "";
  QS.root.hidden = false;
  renderSearch();
  QS.input.focus();
}

function closeSearch() {
  if (!QS.open) return;
  QS.open = false;
  QS.root.hidden = true;
  const back = QS.from;
  QS.from = null;
  if (back && back.isConnected && typeof back.focus === "function") back.focus();
}

function runSearch(item) {
  if (!item) return;
  closeSearch();                                          // (first, so a window the action opens gets the focus)
  item.run();
}

function bindSearch() {
  $("#search-open").addEventListener("click", openSearch);
  document.addEventListener("keydown", e => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "k") return;
    e.preventDefault();                                   // (the browser's own Ctrl+K stays out of it)
    if (QS.open) closeSearch();
    else if ($("#modal").hidden && !(S.wiz && S.wiz.open)) openSearch();
  }, true);
}
/* ===== end of quick search ===== */

let started = false;
async function start() {
  if (started) return;
  started = true;
  bindStatic();
  bindStage();
  requestAnimationFrame(animatePreviews);
  requestAnimationFrame(liveTiles);
  await refresh(true);
  setTimeout(pollState, 1000);
  const page = new URLSearchParams(location.search).get("page");   // (the README's screenshots)
  go(["home", "presets", "show", "setup"].includes(page) ? page : "home");
  if (S.data && S.data.settings.setup_done === false) openWizard();
}

window.addEventListener("pywebviewready", start);
if (window.pywebview && window.pywebview.api) start();
// Opened in a normal browser (design preview): load sample data instead of the Python backend.
setTimeout(() => {
  if (!started && !window.pywebview) {
    const s = document.createElement("script");
    s.src = "mock.js";
    s.onload = start;
    document.head.append(s);
  }
}, 800);
