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

async function callRaw(method, ...args) {
  try {
    return await window.pywebview.api[method](...args);
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
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
  selected: null,          // null = all lights, otherwise an array of device ids
  tab: "color",
  dragging: null,          // id of the fader being dragged
  quietUntil: 0,           // ignore polls right after local edits so sliders don't jump back
  native: {},              // device id -> {effects, palettes}
  nativeBuiltFor: "",
  strips: new Map(),
  settingsOpen: false,
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

async function refresh(force = false) {
  const d = await call("get_state");
  if (!d) return;
  if (!force && (S.dragging || Date.now() < S.quietUntil)) return;
  S.data = d;
  if (S.selected) {
    S.selected = S.selected.filter(id => d.devices.some(x => x.id === id && !x.hidden));
    if (!S.selected.length) S.selected = null;
  }
  render();
}

/* ============================== rendering ============================== */

function render() {
  if (!S.data) return;
  const p = primary();
  const tint = p ? (hexToRgb(p.state.color) ? p.state.color : "#ffd6a0") : "#ffd6a0";
  document.documentElement.style.setProperty("--tint", tint);
  renderPresets();
  renderStrips();
  renderTarget();
  if (S.tab === "color") renderColor();
  else if (S.tab === "effects") renderEffects();
  else renderNative();
  if (S.settingsOpen) renderSettings();
}

/* ---- presets ---- */

const RAINBOW = "conic-gradient(#ff3b3b, #ffd23b, #3bff6e, #3bd8ff, #6e3bff, #ff3bd2, #ff3b3b)";
const isMusicPreset = p => (p.effects || []).some(e => e.startsWith("music_"));

function gelBackground(p) {
  if (p.off) return "#2a2c33";
  const fx = p.effects || [];
  if (fx.some(e => ["rainbow", "spectrum", "music_party", "music_spectrum", "music_ripple", "music_sparkle", "music_meteor"].includes(e))) return RAINBOW;
  if (fx.includes("screen")) return "conic-gradient(#3a6bff, #ff8a3a, #2bd3c4, #3a6bff)";
  if (fx.some(e => ["fire", "music_fire"].includes(e))) return "radial-gradient(circle at 50% 70%, #ffd25a, #ff5a00 55%, #7a1000)";
  if (fx.some(e => ["aurora", "music_aurora"].includes(e))) return "conic-gradient(#2bff9a, #2bd3ff, #8a4bff, #2bff9a)";
  if (fx.includes("music_organ")) return "conic-gradient(#ff2a2a, #2aff5a, #2a6bff, #ff2a2a)";
  if (fx.includes("music_show")) return "conic-gradient(#00e5ff, #ff00c8, #ffffff, #2a4bff, #ff0040, #00e5ff)";
  const c = p.colors || [];
  if (c.length > 1) return `conic-gradient(${[...c, c[0]].join(", ")})`;
  return c[0] || "#888";
}

function renderPresets() {
  const host = $("#preset-list");
  const sig = JSON.stringify(S.data.presets);
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  const chip = p => h("button", {
    class: "chip", type: "button", title: `Apply “${p.name}”`,
    onclick: async () => { await call("apply_preset", p.name); S.quietUntil = 0; refresh(true); toast(`Applied ${p.name}`); },
  },
    h("span", { class: "chip-gel", style: `background:${gelBackground(p)}` }),
    p.name,
    h("span", {
      class: "chip-x", role: "button", "aria-label": `Delete preset ${p.name}`, text: "✕",
      onclick: e => { e.stopPropagation(); confirmDeletePreset(p.name); },
    }),
  );
  const lighting = S.data.presets.filter(p => !isMusicPreset(p));
  const music = S.data.presets.filter(isMusicPreset);
  host.replaceChildren(...lighting.map(chip),
    music.length ? h("span", { class: "eyebrow preset-group", text: "♪ Music" }) : null,
    ...music.map(chip));
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
    title: "Save current as preset",
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

/* ---- fader bank ---- */

function lampLook(d) {
  const st = d.state;
  if (st.mode === "effect") {
    if (["rainbow", "spectrum"].includes(st.effect)) return RAINBOW;
    if (st.effect === "fire") return "conic-gradient(#ff3a00, #ffb000, #ff5a00, #ffd25a, #ff3a00)";
    if (st.effect === "aurora") return "conic-gradient(#2bff9a, #2bd3ff, #8a4bff, #2bff9a)";
    if (st.effect === "screen") return "conic-gradient(#3a6bff, #ff8a3a, #2bd3c4, #3a6bff)";
    return `conic-gradient(${st.color}, ${st.color2}, ${st.color})`;
  }
  if (st.mode === "native") return `conic-gradient(${st.color}, ${st.color2 || st.color}, ${st.color})`;
  return null;
}

function stripMeta(d) {
  if (d.kind === "nanoleaf" && !d.paired) return { text: "Not paired", warn: true };
  if (!d.online) return { text: d.error || "Offline", warn: true };
  if (d.state.on && d.state.mode === "effect") {
    const fx = S.data.effects.find(e => e.id === d.state.effect);
    return { text: (fx ? fx.name : "Effect") + (d.stillEffects ? " · still" : "") };
  }
  if (d.state.on && d.state.mode === "native") {
    const list = S.native[d.id] && S.native[d.id].effects;
    const fx = list && list.find(e => String(e.id) === String(d.state.native));
    return { text: fx ? fx.name : "Built-in effect" };
  }
  return { text: d.label || d.kind };
}

function makeStrip(id, master) {
  const strip = h("div", { class: "strip" + (master ? " strip-master" : ""), "data-id": id });
  const head = h("button", { class: "strip-head", type: "button", title: master ? "Edit all lights" : "Edit this light (Ctrl+click to add)" },
    h("span", { class: "lamp" + (master ? " lamp-master" : "") }),
    h("span", { class: "strip-name" }),
    h("span", { class: "strip-meta" }));
  const fader = h("div", { class: "fader", role: "slider", tabindex: "0", "aria-valuemin": "0", "aria-valuemax": "100" },
    h("div", { class: "fader-scale" }),
    h("div", { class: "fader-track" }, h("div", { class: "fader-fill" })),
    h("div", { class: "fader-cap" }));
  const value = h("div", { class: "strip-value mono" });
  const toggle = h("button", { class: "strip-toggle", type: "button" });
  strip.append(head, fader, value, toggle);

  head.addEventListener("click", e => selectStrip(id, e.ctrlKey || e.shiftKey || e.metaKey));
  toggle.addEventListener("click", () => toggleStrip(id));
  bindFader(fader, id);
  return strip;
}

function selectStrip(id, additive) {
  if (id === "__all") S.selected = null;
  else if (additive) {
    const cur = new Set(S.selected || []);
    cur.has(id) ? cur.delete(id) : cur.add(id);
    S.selected = cur.size ? [...cur] : null;
  } else S.selected = [id];
  render();
}

function toggleStrip(id) {
  if (id === "__all") {
    const anyOn = visible().some(d => d.state.on && d.online);
    send(null, { on: !anyOn }, "on:all");
    return;
  }
  const d = byId(id);
  if (!d) return;
  if (d.kind === "nanoleaf" && !d.paired) return openPairDialog(d);
  send([id], { on: !d.state.on }, "on:" + id);
}

function bindFader(fader, id) {
  const valueAt = e => {
    const r = fader.getBoundingClientRect();
    return Math.round(clamp((r.bottom - 8 - e.clientY) / (r.height - 16), 0, 1) * 100);
  };
  const set = v => {
    v = clamp(Math.round(v), 0, 100);
    setFader(fader, v);
    if (id === "__all") send(null, { brightness: v }, "bri:all");
    else send([id], { brightness: v }, "bri:" + id);
  };
  fader.addEventListener("pointerdown", e => {
    fader.setPointerCapture(e.pointerId);
    S.dragging = id;
    set(valueAt(e));
  });
  fader.addEventListener("pointermove", e => { if (fader.hasPointerCapture(e.pointerId)) set(valueAt(e)); });
  const end = () => { S.dragging = null; S.quietUntil = Date.now() + 900; };
  fader.addEventListener("pointerup", end);
  fader.addEventListener("pointercancel", end);
  fader.addEventListener("keydown", e => {
    const cur = Number(fader.getAttribute("aria-valuenow")) || 0;
    const step = { ArrowUp: 5, ArrowRight: 5, ArrowDown: -5, ArrowLeft: -5, PageUp: 20, PageDown: -20 }[e.key];
    if (step) { e.preventDefault(); set(cur + step); }
    if (e.key === "Home") set(0);
    if (e.key === "End") set(100);
  });
}

function setFader(fader, v) {
  fader.style.setProperty("--v", (v / 100).toFixed(3));
  fader.setAttribute("aria-valuenow", v);
  fader.parentElement.querySelector(".strip-value").textContent = String(v).padStart(3, " ");
}

function renderStrips() {
  const host = $("#strips");
  const devs = visible();
  const order = ["__all", "__sep", ...devs.map(d => d.id)];
  for (const id of order) {
    if (S.strips.has(id)) continue;
    S.strips.set(id, id === "__sep" ? h("div", { class: "strip-sep" }) : makeStrip(id, id === "__all"));
  }
  for (const [id, node] of S.strips) if (!order.includes(id)) { node.remove(); S.strips.delete(id); }
  const current = [...host.children].map(n => n.dataset.id || "__sep");
  if (current.join("|") !== order.join("|")) host.replaceChildren(...order.map(id => S.strips.get(id)));

  const sel = new Set(targetIds());
  // master
  const m = S.strips.get("__all");
  const anyOn = devs.some(d => d.state.on && d.online);
  const avg = devs.length ? Math.round(devs.reduce((a, d) => a + d.state.brightness, 0) / devs.length) : 100;
  m.classList.toggle("is-selected", !S.selected);
  m.classList.toggle("is-off", !anyOn);
  m.style.setProperty("--c", "#ffd6a0");
  m.style.setProperty("--lit", anyOn ? 1 : 0);
  $(".strip-name", m).textContent = "All lights";
  $(".strip-meta", m).textContent = `${devs.filter(d => d.online).length} of ${devs.length} online`;
  if (S.dragging !== "__all") setFader($(".fader", m), avg);
  $(".fader", m).setAttribute("aria-label", "Brightness, all lights");
  const mt = $(".strip-toggle", m);
  mt.textContent = anyOn ? "On" : "Off";
  mt.classList.toggle("is-on", anyOn);
  mt.title = anyOn ? "Turn all lights off" : "Turn all lights on";

  for (const d of devs) {
    const node = S.strips.get(d.id);
    const st = d.state;
    const on = st.on && d.online;
    node.classList.toggle("is-selected", !!S.selected && sel.has(d.id));
    node.classList.toggle("is-off", !st.on);
    node.classList.toggle("is-offline", !d.online);
    node.style.setProperty("--c", hexToRgb(st.color) ? st.color : "#888888");
    node.style.setProperty("--lit", on ? (0.25 + 0.75 * st.brightness / 100).toFixed(2) : 0);
    const lamp = $(".lamp", node);
    const look = lampLook(d);
    lamp.style.background = look || "";
    lamp.classList.toggle("lamp-effect", !!look && on);
    $(".strip-name", node).textContent = d.name;
    $(".strip-name", node).title = d.name;
    const meta = stripMeta(d);
    const metaEl = $(".strip-meta", node);
    metaEl.textContent = meta.text;
    metaEl.title = meta.text;
    metaEl.classList.toggle("is-warn", !!meta.warn);
    const fader = $(".fader", node);
    fader.setAttribute("aria-label", `Brightness, ${d.name}`);
    if (S.dragging !== d.id) setFader(fader, st.brightness);
    const t = $(".strip-toggle", node);
    const needsPair = d.kind === "nanoleaf" && !d.paired;
    t.textContent = needsPair ? "Pair" : st.on ? "On" : "Off";
    t.classList.toggle("is-on", !needsPair && st.on);
    t.classList.toggle("is-pair", needsPair);
    t.title = needsPair ? "Pair these panels" : st.on ? `Turn ${d.name} off` : `Turn ${d.name} on`;
  }
}

function renderTarget() {
  const ids = targetIds();
  const name = !S.selected ? "All lights"
    : ids.length === 1 ? byId(ids[0]).name
    : ids.map(id => byId(id).name).join(", ");
  $("#target-name").textContent = name;
  const offline = ids.filter(id => !byId(id).online).length;
  $("#target-meta").textContent = !S.selected
    ? `${ids.length} lights${offline ? ` · ${offline} offline` : ""} · Ctrl+click faders to pick several`
    : ids.length > 1 ? `${ids.length} lights` : (byId(ids[0]).label || "");
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
  }, h("canvas", { height: "16" }), h("span", { class: "fx-name", text: e.name }));
  const anim = S.data.effects.filter(e => e.group !== "music");
  const music = S.data.effects.filter(e => e.group === "music");
  $("#fx-grid").replaceChildren(
    h("div", { class: "fx-group" }, h("span", { class: "eyebrow", text: "Animations" })),
    ...anim.map(card),
    music.length ? h("div", { class: "fx-group" }, h("span", { class: "eyebrow", text: "Music" }),
      h("span", { class: "fx-group-hint", text: "Moves to whatever your PC is playing. No microphone." })) : null,
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
  const audio = S.data.audio || {};
  $("#fx-note").textContent = !fx ? "Pick an effect to start it."
    : fx.id === "screen" ? "Follows the main colors on your primary display. Great for the monitor strip during games and films."
    : fx.id === "music_show" ? showNote(S.data.show, audio)
    : fx.group === "music" ? `Listening to ${audio.device || "your PC's sound output"}. Speed sets how punchy it is; when the music stops, the lights glow softly.`
    : fx.uses.includes("color") ? "" : "This effect uses its own colors.";
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

let fxFrame = 0;
function animatePreviews(ts) {
  requestAnimationFrame(animatePreviews);
  if (S.tab !== "effects" || !S.data || ts - fxFrame < 33) return;
  fxFrame = ts;
  const st = currentFxState(), t = ts / 1000, dpr = window.devicePixelRatio || 1;
  for (const card of document.querySelectorAll("#fx-grid .fx-card")) {
    const cv = card.firstChild, w = Math.round(cv.clientWidth * dpr), hgt = Math.round(16 * dpr);
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
    list.replaceChildren(...items.map(e => h("button", {
      class: "native-item", type: "button", "data-id": e.id, title: e.name, text: e.name,
      onclick: () => send([d.id], { mode: "native", native: e.id, on: true }, "native:" + d.id),
    })));
    if (!items.length) list.append(h("p", { class: "hint", text: `No effects match “${query}”.` }));
    const pal = $("#native-palette");
    pal.replaceChildren(h("option", { value: "", text: "Effect default" }),
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

/* ============================== settings ============================== */

function settingsSignature() {
  const d = S.data;
  return JSON.stringify([d.devices.map(x => [x.id, x.name, x.online, x.error, x.paired, x.hidden, x.zones, x.label]),
    d.openrgb, d.settings]);
}

function renderSettings(force = false) {
  const body = $("#settings-body");
  if (!force && body.contains(document.activeElement) && document.activeElement.matches("input, select")) return;
  const sig = settingsSignature();
  if (!force && sig === S.settingsSig) return;
  S.settingsSig = sig;
  const scroll = body.scrollTop;
  body.replaceChildren(lightsSection(), addSection(), openrgbSection(), appSection());
  body.scrollTop = scroll;
}

function lightsSection() {
  const rows = S.data.devices.map(d => {
    const name = h("input", { class: "text-input", value: d.name, "aria-label": `Name of ${d.name}`, maxlength: "40" });
    const commit = async () => { if (name.value.trim() !== d.name) { await call("rename", d.id, name.value); refresh(true); } };
    name.addEventListener("change", commit);
    name.addEventListener("keydown", e => { if (e.key === "Enter") name.blur(); });
    const meta = stripMeta(d);
    const dotClass = "dev-dot" + (d.online ? " is-on" : meta.warn ? " is-warn" : "");
    const sub = [d.kind === "openrgb" ? "PC · OpenRGB" : d.kind === "wled" ? "WLED" : "Nanoleaf", d.host, d.online ? d.label : meta.text]
      .filter(Boolean).join(" · ");
    const actions = h("div", { class: "dev-actions" },
      d.kind === "nanoleaf" && !d.paired ? h("button", { class: "btn btn-primary", type: "button", text: "Pair", onclick: () => openPairDialog(d) }) : null,
      h("button", {
        class: "btn", type: "button", text: d.hidden ? "Show in fader bank" : "Hide from fader bank",
        onclick: async () => { await call("set_hidden", d.id, !d.hidden); refresh(true); },
      }),
      d.kind !== "openrgb" ? h("button", {
        class: "btn btn-quiet", type: "button", text: "Forget",
        onclick: async () => { await call("forget", d.id); refresh(true); },
      }) : null);
    const zones = (d.zones || []).filter(z => z.resizable);
    const zoneGrid = zones.length ? h("div", { class: "zone-grid" },
      h("span", { class: "eyebrow", style: "grid-column: 1 / -1", text: "LEDs per channel" }),
      zones.flatMap(z => {
        const inp = h("input", { class: "text-input mono", type: "number", min: z.min, max: z.max, value: z.leds, "aria-label": `${z.name} LED count` });
        inp.addEventListener("change", async () => {
          await call("resize_zone", d.id, z.index, clamp(Number(inp.value) || 0, z.min, z.max));
          refresh(true);
        });
        const found = z.detected === -1 ? null : z.detected === null ? "Nothing connected" : `Fan with ${z.detected} LEDs found`;
        return [h("span", {}, z.name, found ? h("span", { class: "zone-found", text: found }) : null), inp];
      }),
      h("p", { class: "hint", style: "grid-column: 1 / -1; font-size: 12px",
        text: zones.some(z => z.detected !== -1)
          ? "Filled in from what the controller detects. Only change a number if a fan lights up wrong."
          : `How many LEDs are on this channel, 0 if nothing is plugged in. Add up everything daisy-chained on it: Corsair RS120 ARGB fans and the Nautilus RS pump have 8 each. Max ${zones[0].max}.` })) : null;
    return h("div", { class: "dev-row" },
      h("div", { class: "dev-line" }, h("span", { class: dotClass }), name),
      h("div", { class: "dev-sub", text: sub }),
      actions, zoneGrid);
  });
  return h("section", { class: "set-section" },
    h("h3", { text: "Lights" }),
    h("p", { class: "hint", text: "Rename lights, hide ones you don't use, and set up fan LED counts." }),
    rows.length ? rows : h("p", { class: "hint", text: "No lights found yet." }));
}

function addSection() {
  const kind = h("select", { "aria-label": "Light type" },
    h("option", { value: "wled", text: "WLED" }), h("option", { value: "nanoleaf", text: "Nanoleaf" }));
  const host = h("input", { class: "text-input", placeholder: "IP address, e.g. 192.168.1.40", "aria-label": "IP address" });
  const add = h("button", {
    class: "btn", type: "button", text: "Add light",
    onclick: async () => {
      if (!host.value.trim()) return toast("Enter the light's IP address.", true);
      add.disabled = true;
      const r = await callRaw("add_device", kind.value, host.value);
      add.disabled = false;
      if (!r.ok) return toast(`Couldn't reach ${host.value}: ${r.error}`, true);
      toast("Light added");
      host.value = "";
      refresh(true);
    },
  });
  return h("section", { class: "set-section" },
    h("h3", { text: "Add a light" }),
    h("p", { class: "hint", text: "WLED and Nanoleaf lights on your network show up on their own. Add one by IP address if it doesn't." }),
    h("div", { class: "add-row" }, kind, host, add));
}

function openrgbSection() {
  const o = S.data.openrgb;
  const ok = o.status === "connected";
  const pcCount = S.data.devices.filter(d => d.kind === "openrgb" && d.online).length;
  const restart = elevated => async () => {
    await call("openrgb_restart", elevated);
    toast(elevated ? "Restarting OpenRGB as administrator. Approve the Windows prompt." : "Restarting OpenRGB");
    setTimeout(() => refresh(true), 1500);
  };
  const auto = h("input", { type: "checkbox", checked: o.autoLaunch,
    onchange: async e => { await call("set_openrgb_option", "auto_launch", e.target.checked); refresh(true); } });
  return h("section", { class: "set-section" },
    h("h3", { text: "PC lighting (OpenRGB)" }),
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (ok ? " is-on" : " is-warn") }),
      ok ? `Connected · ${pcCount} PC device${pcCount === 1 ? "" : "s"}` : (o.message || o.status)),
    h("p", { class: "hint", style: "margin-bottom:12px",
      text: o.pawnio
        ? "Memory (RAM) and some motherboard lighting only appear when OpenRGB runs as administrator."
        : "Memory (RAM) lighting needs the PawnIO driver (pawnio.eu) and OpenRGB running as administrator." }),
    h("div", { class: "dev-actions", style: "padding-left:0" },
      h("button", { class: "btn btn-primary", type: "button", text: "Restart as administrator", onclick: restart(true) }),
      h("button", { class: "btn", type: "button", text: "Restart normally", onclick: restart(false) }),
      h("button", { class: "btn btn-quiet", type: "button", text: "Stop OpenRGB",
        onclick: async () => { await call("openrgb_stop"); refresh(true); } })),
    h("label", { class: "toggle-row" }, h("span", { text: "Start OpenRGB automatically" }), auto),
    o.path ? h("p", { class: "path mono", text: o.path }) : null);
}

function appSection() {
  const s = S.data.settings;
  const row = (key, label) => h("label", { class: "toggle-row" }, h("span", { text: label }),
    h("input", { type: "checkbox", checked: !!s[key],
      onchange: async e => { await call("set_setting", key, e.target.checked); refresh(true); } }));
  const about = S.data.about || {};
  return h("section", { class: "set-section" },
    h("h3", { text: "App" }),
    h("p", { class: "hint", text: `${about.name || "Glowstage"} ${about.version || ""} · free and open source` }),
    h("div", { class: "dev-actions", style: "padding-left:0; margin: 6px 0 4px" },
      h("button", { class: "btn", type: "button", text: "Restart Glowstage",
        onclick: async () => { toast("Restarting Glowstage…"); await call("restart_app"); } }),
      ...(about.donate || []).map(d => h("button", { class: "btn btn-primary", type: "button",
        text: `♥ Support on ${d.name}`, onclick: () => call("open_link", d.url) })),
      about.repo ? h("button", { class: "btn", type: "button", text: "Project page",
        onclick: () => call("open_link", about.repo) }) : null),
    row("close_to_tray", "Keep running in the tray when I close the window"),
    row("autostart", "Start with Windows"),
    row("lights_on_at_startup", "Switch all lights on when Windows starts"),
    row("pc_lights_off_at_shutdown", "Switch the PC's lights off when Windows shuts down"),
    row("room_lights_off_at_shutdown", "Switch the room lights (WLED, Nanoleaf) off when Windows shuts down"),
    row("show_strobe", "Strobe flashes in the light show"),
    row("start_minimized", "Start hidden in the tray"),
    row("restore_on_launch", "Restore my lights when the app starts"));
}

function openSettings() {
  S.settingsOpen = true;
  $("#settings").hidden = false;
  $("#scrim").hidden = false;
  renderSettings(true);
}
function closeSettings() {
  S.settingsOpen = false;
  $("#settings").hidden = true;
  $("#scrim").hidden = true;
}

/* ============================== modal, pairing, toasts ============================== */

let modalOk = null;
function openModal({ title, body, ok, onOk }) {
  $("#modal-title").textContent = title;
  $("#modal-body").replaceChildren(body);
  $("#modal-ok").textContent = ok;
  $("#modal-ok").disabled = false;
  $("#modal-error").hidden = true;
  modalOk = onOk;
  $("#modal").hidden = false;
  setTimeout(() => $("#modal-ok").focus(), 20);
}
function closeModal() { $("#modal").hidden = true; modalOk = null; }

function openPairDialog(d) {
  openModal({
    title: `Pair ${d.name}`,
    body: h("ol", {},
      h("li", { text: "Hold the power button on the Nanoleaf controller for 5–7 seconds, until the panels flash." }),
      h("li", { text: "Press Pair within 30 seconds." })),
    ok: "Pair",
    onOk: async () => {
      const r = await callRaw("pair_nanoleaf", d.id);
      if (!r.ok) return r.error;
      toast(`Paired with ${d.name}`);
      delete S.native[d.id];
      refresh(true);
      return true;
    },
  });
}

function toast(msg, isError = false) {
  const t = h("div", { class: "toast" + (isError ? " is-error" : ""), role: isError ? "alert" : "status", text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isError ? 6000 : 2600);
}

/* ============================== wiring ============================== */

function switchTab(tab) {
  S.tab = tab;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const id of ["color", "effects", "native"]) $(`#tab-${id}`).hidden = id !== tab;
  render();
}

function bindStatic() {
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => switchTab(b.dataset.tab));
  $("#tab-native").addEventListener("input", e => { if (e.target.id === "native-search") renderNative(); });

  drawWheel();
  bindWheel();
  bindKelvin();
  $("#swatches").replaceChildren(...SWATCHES.map(([name, c]) => h("button", {
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
  $("#fx-c1").addEventListener("input", e => sendTargets({ color: e.target.value }, "c1"));
  $("#fx-c2").addEventListener("input", e => sendTargets({ color2: e.target.value }, "c2"));
  $("#native-speed").addEventListener("input", e => { const d = nativeDevice(); if (d) send([d.id], { speed: Number(e.target.value) }, "nspeed"); });
  $("#native-palette").addEventListener("change", e => {
    const d = nativeDevice();
    if (d) send([d.id], { palette: e.target.value === "" ? null : Number(e.target.value) }, "npal");
  });

  $("#save-preset").addEventListener("click", openSavePreset);
  $("#open-settings").addEventListener("click", openSettings);
  $("#close-settings").addEventListener("click", closeSettings);
  $("#scrim").addEventListener("click", closeSettings);
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
    if (e.key !== "Escape") return;
    if (!$("#modal").hidden) closeModal();
    else if (S.settingsOpen) closeSettings();
  });
}

let started = false;
async function start() {
  if (started) return;
  started = true;
  bindStatic();
  requestAnimationFrame(animatePreviews);
  await refresh(true);
  setInterval(refresh, 1000);
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
