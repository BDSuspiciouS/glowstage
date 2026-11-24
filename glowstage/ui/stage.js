"use strict";
/* The stage plot: every light drawn where it is in the room, as seen from the desk chair, glowing
   in the colors it shows right now. The light show uses the same layout: chases run from left to
   right across the front, lights on the back wall glow together as one wash. In Arrange mode the
   lights can be dragged into place (that's stored as the show layout). */

const LABEL_FONT = "600 12px 'Segoe UI Variable Text', 'Segoe UI', sans-serif";
const OFF_RGB = [29, 28, 34];

const ST = {
  canvas: null, ctx: null, w: 0, h: 0, dpr: 1,
  fixtures: [],        // drawn this frame: {d, layer, type, x0, x1, y, y0, y1, a, b, rev}
  bands: null,         // {backTop, backBottom, frontTop, seatY}
  live: {},            // id -> ["#rrggbb", ...] from the engine
  smooth: {},          // id -> [[r,g,b], ...] eased toward the live colors
  pending: {},         // id -> {start, end, layer} while dragging / until the backend has it
  drag: null,
  focus: null,         // the light picked in Arrange mode
  lastDraw: 0,
  hudSig: "",
};

const stageOf = d => ST.pending[d.id] || (S.data.stage && S.data.stage[d.id]) || { start: 0, end: 1, layer: "front", custom: false };

function fixtureType(d) {
  if (d.kind === "nanoleaf") return "panels";
  if (d.kind === "qmk") return "keys";
  if ((d.pixels || 0) <= 1) return "bulb";
  if (d.group === "pc") return "pc";
  return "strip";
}
function hashTilt(id) {
  let x = 0;
  for (const ch of id) x = (x * 31 + ch.charCodeAt(0)) >>> 0;
  return ((x % 5) - 2) * 0.012;      // a hand-stuck look: about ±1.4°
}

/* ---- colors ---- */

function resample(list, n) {
  if (!list.length) return Array(n).fill(OFF_RGB);
  return Array.from({ length: n }, (_, i) => list[Math.min(list.length - 1, Math.floor((i + 0.5) * list.length / n))]);
}

function fixtureColors(d, n, rev) {
  const st = d.state;
  if (!d.online || !st.on) return Array(n).fill(OFF_RGB);
  const live = ST.live[d.id];
  let target;
  if (live && live.length) {
    target = resample(live.map(hexToRgb).filter(Boolean), n);
    if (rev) target = target.reverse();
  } else {
    const f = 0.25 + 0.75 * st.brightness / 100;
    const c1 = hexToRgb(st.color) || [255, 255, 255], c2 = hexToRgb(st.color2) || c1;
    if (st.mode === "color") target = Array(n).fill(scaleRgb(c1, f));
    else target = Array.from({ length: n }, (_, i) => scaleRgb(lerp(c1, c2, n > 1 ? i / (n - 1) : 0), f * 0.8));
  }
  // ease toward the target so 15 updates a second still move smoothly
  const prev = ST.smooth[d.id];
  const out = prev && prev.length === n ? prev.map((p, i) => lerp(p, target[i], 0.5)) : target.map(c => c.slice());
  ST.smooth[d.id] = out;
  return out;
}
const rgbCss = (c, a = 1) => `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${a})`;
const luma = c => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
function average(colors) {
  const s = [0, 0, 0];
  for (const c of colors) { s[0] += c[0]; s[1] += c[1]; s[2] += c[2]; }
  return s.map(v => v / Math.max(1, colors.length));
}

/* ---- layout ---- */

function layoutStage() {
  const W = ST.w, H = ST.h, padX = 26;
  const devs = visible();
  const usable = W - padX * 2;
  const back = [], front = [];
  for (const d of devs) {
    const s = stageOf(d), type = fixtureType(d);
    let a = Math.min(s.start, s.end), b = Math.max(s.start, s.end);
    if (type === "bulb") { const c = (a + b) / 2; a = c - 0.035; b = c + 0.035; }
    (s.layer === "back" ? back : front).push({ d, type, a, b, rev: s.start > s.end, layer: s.layer === "back" ? "back" : "front", custom: !!s.custom });
  }
  // front lanes: overlapping lights stack; the keyboard sits on the desk, nearest to you
  const keys = front.filter(f => f.type === "keys");
  const rest = front.filter(f => f.type !== "keys").sort((p, q) => p.a - q.a || (q.b - q.a) - (p.b - p.a));
  const ends = [];
  for (const it of rest) {
    let lane = ends.findIndex(e => e <= it.a - 0.015);
    if (lane < 0) { lane = ends.length; ends.push(0); }
    ends[lane] = it.b;
    it.lane = lane;
  }
  const lanes = ends.length + (keys.length ? 1 : 0);
  for (const k of keys) k.lane = ends.length;

  const compact = H < 340;
  const backTop = compact ? 22 : 26, backRow = compact ? 40 : 48;
  const backBottom = backTop + backRow + 4;
  const seatY = H - (compact ? 22 : 30);
  const frontTop = backBottom + (compact ? 16 : 30);
  const laneH = clamp((seatY - 18 - frontTop) / Math.max(1, lanes), 40, 110);
  ST.bands = { backTop, backBottom, frontTop, seatY, padX, usable };

  const fixtures = [];
  back.forEach((f, i) => {   // the back wall is one wash: its lights share the row side by side
    const y = backTop + 18, cell = usable / back.length;
    fixtures.push({ ...f, x0: padX + i * cell + (i ? 8 : 0), x1: padX + (i + 1) * cell - (i < back.length - 1 ? 8 : 0), y, y0: y - 16, y1: y + 22 });
  });
  const spare = Math.max(0, seatY - 18 - frontTop - lanes * laneH);   // a tall window: float the lights a bit lower
  for (const f of front) {
    const y = frontTop + spare * 0.35 + f.lane * laneH + 14;
    const x0 = padX + f.a * usable, x1 = padX + f.b * usable;
    fixtures.push({ ...f, x0, x1, y, y0: y - 16, y1: y + 26 });
  }
  ST.fixtures = fixtures;
}

/* ---- drawing ---- */

function drawStage(ts) {
  requestAnimationFrame(drawStage);
  if (!S.data || !ST.ctx || !ST.w) return;
  const busy = Object.keys(ST.live).length || ST.drag;
  if (ts - ST.lastDraw < (busy ? 33 : 250)) return;
  ST.lastDraw = ts;

  layoutStage();
  const ctx = ST.ctx, W = ST.w, H = ST.h, B = ST.bands;
  ctx.setTransform(ST.dpr, 0, 0, ST.dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const arranging = S.arrange;
  // back wall
  ctx.fillStyle = "rgba(255, 255, 255, 0.025)";
  roundRect(ctx, 10, 10, W - 20, B.backBottom - 4, 8);
  ctx.fill();
  if (arranging) {
    ctx.setLineDash([4, 5]);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.22)";
    ctx.stroke();
    ctx.setLineDash([]);
  }
  label(ctx, "Back wall", 20, 26, "rgba(156, 156, 165, 0.75)");
  if (!ST.fixtures.some(f => f.layer === "back")) {
    ctx.fillStyle = "rgba(155, 150, 163, 0.5)";
    ctx.font = "12px 'Segoe UI Variable Text', 'Segoe UI', sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(arranging ? "Drop lights here that are behind you" : "Nothing on the wall behind you", W / 2, (B.backTop + B.backBottom) / 2 + 8);
    ctx.textAlign = "left";
  }

  // seat and desk edge: the stage is seen from here
  ctx.strokeStyle = "rgba(155, 150, 163, 0.18)";
  ctx.setLineDash([2, 6]);
  ctx.beginPath(); ctx.moveTo(14, B.seatY - 8); ctx.lineTo(W - 14, B.seatY - 8); ctx.stroke();
  ctx.setLineDash([]);
  label(ctx, "Left", 16, B.seatY + 8, "rgba(156, 156, 165, 0.5)");
  label(ctx, "Right", W - 16, B.seatY + 8, "rgba(156, 156, 165, 0.5)", "right");
  ctx.fillStyle = "rgba(243, 241, 237, 0.75)";
  ctx.beginPath(); ctx.moveTo(W / 2, B.seatY - 2); ctx.lineTo(W / 2 - 7, B.seatY + 8); ctx.lineTo(W / 2 + 7, B.seatY + 8); ctx.closePath(); ctx.fill();
  label(ctx, "You", W / 2 + 12, B.seatY + 8, "rgba(243, 241, 237, 0.75)", "left");

  // pass 1: light spilling onto the floor and wall
  const colorsOf = new Map();
  ctx.globalCompositeOperation = "lighter";
  for (const f of ST.fixtures) {
    const n = ledCount(f);
    const colors = fixtureColors(f.d, n, f.rev);
    colorsOf.set(f, colors);
    const avg = average(colors), l = luma(avg);
    if (l < 0.02) continue;
    const cx = (f.x0 + f.x1) / 2, rx = (f.x1 - f.x0) / 2 + (f.layer === "back" ? 10 : 34);
    const cy = f.layer === "back" ? f.y + 4 : f.y + 16, ry = f.layer === "back" ? 26 : 46;
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
    const k = Math.min(0.55, 0.12 + l * 0.7);
    g.addColorStop(0, rgbCss(avg, k));
    g.addColorStop(1, rgbCss(avg, 0));
    ctx.save();
    ctx.translate(cx, cy);
    ctx.scale(rx, ry);
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(0, 0, 1, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }
  ctx.globalCompositeOperation = "source-over";

  // pass 2: the fixtures, their LEDs and tape labels
  const sel = new Set(S.selected || []);
  for (const f of ST.fixtures) {
    drawFixture(ctx, f, colorsOf.get(f));
    const picked = arranging ? ST.focus === f.d.id : sel.has(f.d.id);
    if (picked) brackets(ctx, f);
    if (arranging) handles(ctx, f, ST.focus === f.d.id);
  }
  for (const t of placeTapes(ctx)) tape(ctx, t);
}

/* Where each light's tape label goes: under the light, nudged down or sideways when it would
   cover another label. */
function placeTapes(ctx) {
  ctx.save();
  ctx.font = LABEL_FONT;
  const placed = [];
  const order = ST.fixtures.slice().sort((p, q) => p.x0 - q.x0);
  for (const f of order) {
    const text = f.d.name + (f.d.online ? "" : " · offline");
    const tw = Math.min(ctx.measureText(text).width + 4, Math.max(60, ST.w - 40));
    let ty = f.layer === "back" ? f.y + 13 : f.y + (f.type === "keys" ? 21 : f.type === "pc" ? 19 : 14);
    let tx = f.type === "bulb" ? (f.x0 + f.x1) / 2 - tw / 2 : f.x0;
    tx = clamp(tx, 6, ST.w - tw - 6);
    const hits = (x, y) => placed.some(o => x < o.tx + o.tw + 4 && o.tx < x + tw + 4 && y < o.ty + 16 && o.ty < y + 16);
    for (let k = 0; k < 3 && hits(tx, ty); k++) {
      const prev = placed.filter(o => o.ty < ty + 16 && ty < o.ty + 16).reduce((m, o) => Math.max(m, o.tx + o.tw + 6), tx);
      if (prev + tw <= Math.max(f.x1, f.x0 + tw) + 40 && prev + tw < ST.w - 6) tx = prev;
      else ty += 16;
    }
    placed.push({ f, text, tx, ty, tw });
  }
  ctx.restore();
  return placed;
}


function ledCount(f) {
  const w = f.x1 - f.x0;
  if (f.type === "bulb") return 1;
  if (f.type === "pc") return clamp(Math.floor(w / 16), 2, 14);
  if (f.type === "keys") return clamp(Math.floor(w / 9), 6, 28) * 2;
  if (f.type === "panels") return clamp(Math.floor(w / 26), 4, 30);
  return clamp(Math.floor(w / 7), 4, 64);
}

function drawFixture(ctx, f, colors) {
  const x0 = f.x0, x1 = f.x1, y = f.y, w = x1 - x0;
  const lit = c => luma(c) > 0.03;
  const glow = (c, blur) => { ctx.shadowColor = rgbCss(c, 0.9); ctx.shadowBlur = lit(c) ? blur : 0; };
  ctx.save();
  if (!f.d.online) ctx.globalAlpha = 0.5;
  if (f.type === "strip") {
    ctx.fillStyle = "#0c0b0f"; ctx.strokeStyle = "#2f2d37"; ctx.lineWidth = 1;
    roundRect(ctx, x0, y - 5, w, 10, 5); ctx.fill(); ctx.stroke();
    const step = w / colors.length;
    colors.forEach((c, i) => {
      ctx.fillStyle = lit(c) ? rgbCss(c) : "#1e1d24";
      glow(c, 7);
      ctx.beginPath(); ctx.arc(x0 + step * (i + 0.5), y, Math.min(2.6, step * 0.38), 0, Math.PI * 2); ctx.fill();
    });
  } else if (f.type === "pc") {
    ctx.fillStyle = "#0c0b0f"; ctx.strokeStyle = "#2f2d37";
    roundRect(ctx, x0, y - 11, w, 22, 5); ctx.fill(); ctx.stroke();
    const step = w / colors.length, r = Math.min(7, step * 0.36);
    colors.forEach((c, i) => {   // fans: rings of light
      ctx.strokeStyle = lit(c) ? rgbCss(c) : "#26242d"; ctx.lineWidth = 2.2;
      glow(c, 9);
      ctx.beginPath(); ctx.arc(x0 + step * (i + 0.5), y, r, 0, Math.PI * 2); ctx.stroke();
    });
  } else if (f.type === "keys") {
    ctx.fillStyle = "#0c0b0f"; ctx.strokeStyle = "#2f2d37";
    roundRect(ctx, x0, y - 12, w, 26, 4); ctx.fill(); ctx.stroke();
    const cols = colors.length / 2, step = w / cols;
    colors.forEach((c, i) => {
      const row = i < cols ? 0 : 1, col = i % cols;
      ctx.fillStyle = lit(c) ? rgbCss(c, 0.95) : "#1e1d24";
      glow(c, 5);
      roundRect(ctx, x0 + step * col + 2, y - 8 + row * 11, Math.max(2, step - 4), 7, 1.5); ctx.fill();
    });
  } else if (f.type === "panels") {
    const step = w / colors.length, s = Math.min(14, step * 0.62);
    colors.forEach((c, i) => {   // triangles, alternately up and down, like a panel set
      const cx = x0 + step * (i + 0.5), up = i % 2 === 0;
      ctx.fillStyle = lit(c) ? rgbCss(c, 0.95) : "#1e1d24";
      ctx.strokeStyle = "#0c0b0f"; ctx.lineWidth = 1.5;
      glow(c, 10);
      ctx.beginPath();
      if (up) { ctx.moveTo(cx, y - s); ctx.lineTo(cx + s, y + s * 0.7); ctx.lineTo(cx - s, y + s * 0.7); }
      else { ctx.moveTo(cx, y + s * 0.85); ctx.lineTo(cx + s, y - s * 0.85); ctx.lineTo(cx - s, y - s * 0.85); }
      ctx.closePath(); ctx.fill(); ctx.shadowBlur = 0; ctx.stroke();
    });
  } else {   // a bulb: one lens
    const c = colors[0], cx = (x0 + x1) / 2;
    ctx.fillStyle = "#0c0b0f";
    ctx.beginPath(); ctx.arc(cx, y, 12, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = lit(c) ? rgbCss(c) : "#1e1d24";
    glow(c, 16);
    ctx.beginPath(); ctx.arc(cx, y, 8.5, 0, Math.PI * 2); ctx.fill();
  }
  ctx.restore();
}

function tape(ctx, t) {
  const d = t.f.d, picked = S.arrange ? ST.focus === d.id : (S.selected || []).includes(d.id);
  ctx.save();
  ctx.font = LABEL_FONT;
  ctx.textBaseline = "middle";
  ctx.fillStyle = picked ? "#f3f1ed" : d.online ? "rgba(243, 241, 237, 0.62)" : "rgba(156, 156, 165, 0.45)";
  ctx.beginPath(); ctx.rect(t.tx, t.ty, t.tw, 16); ctx.clip();
  ctx.fillText(t.text, t.tx + 2, t.ty + 8);
  ctx.restore();
}

function brackets(ctx, f) {
  const x0 = f.x0 - 7, x1 = f.x1 + 7, y0 = f.y - 18, y1 = f.y + (f.layer === "back" ? 30 : 36), k = 7;
  ctx.save();
  ctx.strokeStyle = "rgba(239, 233, 223, 0.85)"; ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (const [x, y, dx, dy] of [[x0, y0, 1, 1], [x1, y0, -1, 1], [x0, y1, 1, -1], [x1, y1, -1, -1]]) {
    ctx.moveTo(x + dx * k, y); ctx.lineTo(x, y); ctx.lineTo(x, y + dy * k);
  }
  ctx.stroke();
  ctx.restore();
}

function handles(ctx, f, focused) {
  if (f.layer === "back" || f.type === "bulb") return;
  ctx.save();
  ctx.fillStyle = focused ? "#f3f1ed" : "rgba(243, 241, 237, 0.35)";
  for (const x of [f.x0, f.x1]) { roundRect(ctx, x - 2.5, f.y - 13, 5, 26, 2); ctx.fill(); }
  // which way the light runs: from its first LED to its last
  const head = f.rev ? f.x0 - 4 : f.x1 + 4, dir = f.rev ? -1 : 1;
  ctx.beginPath(); ctx.moveTo(head + dir * 7, f.y); ctx.lineTo(head, f.y - 5); ctx.lineTo(head, f.y + 5); ctx.closePath(); ctx.fill();
  ctx.restore();
}

function label(ctx, text, x, y, color, align = "left") {
  ctx.save();
  ctx.font = "500 11.5px 'Segoe UI Variable Text', 'Segoe UI', sans-serif";
  ctx.fillStyle = color; ctx.textAlign = align;
  ctx.fillText(text, x, y);
  ctx.restore();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}

/* ---- the show readout under the stage ---- */

const SECTION_NAMES = { groove: "Groove", break: "Break", build: "Build-up", gap: "About to drop", drop: "Drop",
  calm: "Calm part", silent: "Waiting for music" };

function renderStage() {
  const empty = !visible().length;
  $("#plot-empty").hidden = !empty;
  const hud = $("#plot-hud");
  const show = S.data.show;
  const sig = show ? JSON.stringify([show.style, show.section, show.bpm, show.half, show.chord, show.ai]) : "";
  hud.hidden = !show || !!S.arrange;
  if (sig !== ST.hudSig) {
    ST.hudSig = sig;
    if (show) {
      const cell = (label, value, sub, cls = "") => h("div", { class: "readout-cell" },
        h("div", { class: "readout-label", text: label }), h("div", { class: "readout-value " + cls, text: value }),
        h("div", { class: "readout-sub", text: sub || "\u00a0", title: sub || "" }));
      fill(hud,
        cell(show.ai ? "AI hears" : "Style", show.style || "…",
          show.ai ? [show.genres.slice(0, 2).join(", "), show.mood].filter(Boolean).join(" · ") : (show.aiError || "Still listening")),
        cell("Right now", SECTION_NAMES[show.section] || show.section || "…",
          show.section === "drop" ? "Everything goes" : show.section === "build" ? "Tension rising" : "", show.section === "drop" ? "is-hot" : ""),
        cell("Tempo", show.bpm ? `${show.bpm} BPM` : "Finding the beat", show.half ? "Half time" : ""),
        cell("Harmony", show.chord || "…", show.chord ? "Colors follow the chords" : ""));
    }
  }
  renderInspector();
}

function renderInspector() {
  const box = $("#inspector");
  $("#plot-floor").classList.toggle("is-arranging", !!S.arrange);
  $("#arrange").setAttribute("aria-pressed", String(!!S.arrange));
  $("#arrange span").textContent = S.arrange ? "Done" : "Arrange";
  $("#stage-reset").hidden = !S.arrange;
  $("#plot-hint").textContent = S.arrange
    ? "Drag each light to where it is, as seen from your chair"
    : "Click a light to edit it · Ctrl+click for several";
  box.hidden = !S.arrange;
  if (!S.arrange) return;
  const d = ST.focus && byId(ST.focus);
  const sig = d ? JSON.stringify([d.id, d.name, stageOf(d)]) : "none";
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  if (!d) {
    fill(box, h("span", { class: "hint",
      text: "Click a light to pick it, drag it sideways to move it, drag its ends to make it wider or narrower. Lights on the back wall glow together as one wash behind you." }));
    return;
  }
  const s = stageOf(d), type = fixtureType(d), back = s.layer === "back";
  const put = span => setStage(d.id, span);
  fill(box, 
    h("span", { class: "tape", text: back ? "Back wall" : "In front of you" }),
    h("span", { class: "inspector-name", text: d.name }),
    h("span", { class: "spacer" }),
    !back && type !== "bulb" ? h("button", { class: "btn", type: "button", text: "Flip direction", title: "The light runs the other way",
      onclick: () => put([s.end, s.start, s.layer]) }) : null,
    h("button", { class: "btn", type: "button", text: back ? "Move to the front" : "Move to the back wall",
      onclick: () => put(back ? [0.3, 0.7, "front"] : [0, 1, "back"]) }),
    s.custom ? h("button", { class: "btn btn-quiet", type: "button", text: "Auto place", title: "Put it where Glowstage would",
      onclick: () => put(null) }) : null);
}

async function setStage(id, span) {
  if (span) ST.pending[id] = { start: span[0], end: span[1], layer: span[2], custom: true };
  else delete ST.pending[id];
  const r = await callRaw("set_stage", id, span);
  if (!r.ok) { delete ST.pending[id]; toast(r.error, true); }
  await refresh(true);
  delete ST.pending[id];
  renderStage();
}

/* ---- pointer and keyboard ---- */

function fixtureAt(x, y) {
  for (let i = ST.fixtures.length - 1; i >= 0; i--) {
    const f = ST.fixtures[i];
    const pad = f.type === "bulb" ? 14 : 8;
    if (x >= f.x0 - pad && x <= f.x1 + pad && y >= f.y0 && y <= f.y1) return f;
  }
  return null;
}
const toStage = x => clamp((x - ST.bands.padX) / ST.bands.usable, 0, 1);

function bindStage() {
  ST.canvas = $("#plot-canvas");
  ST.ctx = ST.canvas.getContext("2d");
  ST.canvas.tabIndex = 0;
  const floor = $("#plot-floor");
  const fit = () => {
    const r = floor.getBoundingClientRect();
    ST.dpr = window.devicePixelRatio || 1;
    ST.w = r.width; ST.h = r.height;
    ST.canvas.width = Math.round(r.width * ST.dpr);
    ST.canvas.height = Math.round(r.height * ST.dpr);
    ST.lastDraw = 0;
  };
  new ResizeObserver(fit).observe(floor);
  fit();

  const pos = e => { const r = ST.canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  ST.canvas.addEventListener("pointerdown", e => {
    if (!S.data) return;
    const [x, y] = pos(e);
    const f = fixtureAt(x, y);
    if (!S.arrange) {
      if (f) selectStrip(f.d.id, e.ctrlKey || e.shiftKey || e.metaKey);
      else selectStrip("__all");
      return;
    }
    ST.focus = f ? f.d.id : null;
    renderInspector();
    if (!f) return;
    const s = stageOf(f.d);
    let mode = "move";
    if (f.layer !== "back" && f.type !== "bulb") {
      if (Math.abs(x - f.x0) < 9) mode = "left";
      else if (Math.abs(x - f.x1) < 9) mode = "right";
    }
    ST.drag = { id: f.d.id, mode, x0: x, a: f.a, b: f.b, rev: f.rev, layer: f.layer, type: f.type, moved: false, start: s };
    ST.canvas.setPointerCapture(e.pointerId);
    floor.classList.add("is-dragging");
  });
  ST.canvas.addEventListener("pointermove", e => {
    const [x, y] = pos(e);
    if (!ST.drag) {
      if (S.arrange) {
        const f = fixtureAt(x, y);
        const edge = f && f.layer !== "back" && f.type !== "bulb" && (Math.abs(x - f.x0) < 9 || Math.abs(x - f.x1) < 9);
        ST.canvas.style.cursor = edge ? "ew-resize" : f ? "grab" : "";
      } else ST.canvas.style.cursor = fixtureAt(x, y) ? "pointer" : "";
      return;
    }
    const g = ST.drag, dx = (x - g.x0) / ST.bands.usable;
    if (Math.abs(x - g.x0) > 2) g.moved = true;
    const toBack = y < ST.bands.backBottom + 8;
    let a = g.a, b = g.b, layer = toBack ? "back" : "front";
    if (g.layer === "back" && layer === "front") {           // pulled off the wall: give it a place in front
      const w = g.type === "bulb" ? 0.07 : 0.34, c = toStage(x);
      a = clamp(c - w / 2, 0, 1 - w); b = a + w;
      g.moved = true;
    } else if (layer === "back") {
      a = 0; b = 1;
      g.moved = true;
    } else if (g.mode === "move") {
      const w = b - a; a = clamp(a + dx, 0, 1 - w); b = a + w;
    } else if (g.mode === "left") a = clamp(a + dx, 0, b - 0.04);
    else b = clamp(b + dx, a + 0.04, 1);
    ST.pending[g.id] = g.rev ? { start: b, end: a, layer, custom: true } : { start: a, end: b, layer, custom: true };
    ST.lastDraw = 0;
  });
  const end = async () => {
    const g = ST.drag;
    ST.drag = null;
    floor.classList.remove("is-dragging");
    if (!g || !g.moved || !ST.pending[g.id]) { delete ST.pending[g && g.id]; return; }
    const p = ST.pending[g.id];
    await setStage(g.id, [p.start, p.end, p.layer]);
  };
  ST.canvas.addEventListener("pointerup", end);
  ST.canvas.addEventListener("pointercancel", end);
  ST.canvas.addEventListener("keydown", e => {
    if (!S.arrange || !ST.focus) return;
    const d = byId(ST.focus);
    if (!d) return;
    const s = stageOf(d), step = e.shiftKey ? 0.01 : 0.03;
    const dir = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (!dir || s.layer === "back") return;
    e.preventDefault();
    const a = Math.min(s.start, s.end), b = Math.max(s.start, s.end), w = b - a;
    const na = clamp(a + dir * step, 0, 1 - w), nb = na + w;
    setStage(d.id, s.start > s.end ? [nb, na, s.layer] : [na, nb, s.layer]);
  });

  $("#arrange").addEventListener("click", () => {
    S.arrange = !S.arrange;
    ST.focus = S.arrange && S.selected && S.selected.length === 1 ? S.selected[0] : null;
    ST.lastDraw = 0;
    renderStage();
  });
  $("#stage-reset").addEventListener("click", () => openModal({
    title: "Reset the stage layout?",
    body: h("p", { class: "hint", text: "Every light goes back to where Glowstage places it on its own." }),
    ok: "Reset layout",
    onOk: async () => { await call("reset_stage"); ST.pending = {}; refresh(true); return true; },
  }));
  $("#plot-setup").addEventListener("click", () => openSettings());

  requestAnimationFrame(drawStage);
  pollLive();
}

/* What the lights running an app effect show right now: about 14 times a second while any does. */
async function pollLive() {
  let wait = 400;
  try {
    const running = S.data && !document.hidden && visible().some(d => d.online && d.state.on && d.state.mode === "effect");
    if (running) {
      const r = await callRaw("get_live");
      if (r && r.ok) ST.live = r.result || {};
      wait = 70;
    } else if (Object.keys(ST.live).length) ST.live = {};
  } finally {
    setTimeout(pollLive, wait);
  }
}
