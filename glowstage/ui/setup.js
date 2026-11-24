"use strict";
/* Setup: lights and brands, bridges, PC lighting (OpenRGB) and app settings; pairing, the DMX
   patch, and the welcome tour that runs the first time. */

const driverOf = kind => (S.data.drivers || []).find(x => x.kind === kind);
const hubOf = d => (S.data.hubs || []).find(hb => d.id.startsWith(hb.id + ":"));

function kindTitle(d) {
  if (d.kind === "openrgb") return "PC · OpenRGB";
  if (d.kind === "qmk") return "Keyboard";
  if (d.kind === "asrock_gpu") return "Graphics card";
  const dr = driverOf(d.kind);
  return dr ? dr.title : d.kind;
}

function placeTape(d) {
  const s = S.data.stage && S.data.stage[d.id];
  if (s && s.layer === "back") return h("span", { class: "tape tape-back", text: "Back wall" });
  return h("span", { class: "tape" + (d.group === "pc" ? " tape-pc" : ""), text: d.group === "pc" ? "PC" : "Front" });
}

/* ============================== the drawer ============================== */

S.addForm = { kind: "wled", values: {} };

function settingsSignature() {
  const d = S.data;
  return JSON.stringify([d.devices.map(x => [x.id, x.name, x.online, x.error, x.paired, x.hidden, x.zones, x.label]),
    d.hubs, d.openrgb, d.settings, (d.drivers || []).length, d.stage]);
}

function renderSettings(force = false) {
  const body = $("#settings-body");
  if (!force && body.contains(document.activeElement) && document.activeElement.matches("input, select")) return;
  const sig = settingsSignature();
  if (!force && sig === S.settingsSig) return;
  S.settingsSig = sig;
  const scroll = body.scrollTop;
  fill(body, lightsSection(), hubsSection(), addSection(), openrgbSection(), appSection());
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
    const dr = driverOf(d.kind);
    const sub = [kindTitle(d) + (dr && dr.beta ? " (beta)" : ""), d.host, d.online ? d.label : meta.text]
      .filter(Boolean).join(" · ");
    const child = hubOf(d);
    const actions = h("div", { class: "dev-actions" },
      d.paired === false ? h("button", { class: "btn btn-primary", type: "button", text: "Pair", onclick: () => openPairDialog(d) }) : null,
      h("button", {
        class: "btn", type: "button", text: d.hidden ? "Show again" : "Hide",
        title: d.hidden ? "Show it on the stage and in the fader bank" : "Hide it from the stage and the fader bank",
        onclick: async () => { await call("set_hidden", d.id, !d.hidden); refresh(true); },
      }),
      dr && !child ? h("button", {
        class: "btn btn-quiet", type: "button", text: "Forget", title: "Remove it; it comes back if Glowstage finds it again",
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
          : `How many LEDs are on this channel, 0 if nothing is plugged in. Add up everything daisy-chained on it. Max ${zones[0].max}.` })) : null;
    return h("div", { class: "dev-row" },
      h("div", { class: "dev-line" }, h("span", { class: dotClass }), name, placeTape(d)),
      h("div", { class: "dev-sub", text: sub }),
      actions, zoneGrid);
  });
  return h("section", { class: "set-section" },
    h("h3", {}, "Lights", h("span", { class: "section-tools" },
      h("button", { class: "btn", type: "button", text: "Search now",
        onclick: async () => { await call("search"); toast("Looking for lights on your network…"); } }))),
    h("p", { class: "hint", text: "Everything Glowstage found, in your PC and on your network. Rename lights, hide ones you don't use, set up fan LED counts." }),
    rows.length ? rows : h("p", { class: "hint", text: "No lights found yet." }));
}

function hubsSection() {
  const hubs = S.data.hubs || [];
  if (!hubs.length) return null;
  const rows = hubs.map(hb => {
    const dr = driverOf(hb.kind);
    const dot = "dev-dot" + (hb.online ? " is-on" : " is-warn");
    const status = hb.paired === false ? "Not paired" : hb.online ? `${hb.lights} light${hb.lights === 1 ? "" : "s"}` : (hb.error || "Offline");
    return h("div", { class: "dev-row" },
      h("div", { class: "dev-line" }, h("span", { class: dot }),
        h("span", { style: "flex:1; font-weight:600", text: hb.name || (dr ? dr.title : hb.kind) })),
      h("div", { class: "dev-sub", text: [dr ? dr.title : hb.kind, hb.host, status].filter(Boolean).join(" · ") }),
      h("div", { class: "dev-actions" },
        hb.paired === false ? h("button", { class: "btn btn-primary", type: "button", text: "Pair", onclick: () => openPairDialog(hb) }) : null,
        hb.kind === "dmx" ? h("button", { class: "btn", type: "button", text: "Edit fixtures", onclick: () => openDmxEditor(hb) }) : null,
        h("button", { class: "btn btn-quiet", type: "button", text: "Forget",
          onclick: async () => { await call("forget", hb.id); refresh(true); } })));
  });
  return h("section", { class: "set-section" },
    h("h3", { text: "Bridges and hubs" }),
    h("p", { class: "hint", text: "One bridge can run many lights: a Hue bridge, Home Assistant, a DMX universe." }),
    rows);
}

function addSection() {
  const list = S.data.drivers || [];
  if (!list.find(x => x.kind === S.addForm.kind) && list.length) S.addForm.kind = list[0].kind;
  const dr = list.find(x => x.kind === S.addForm.kind);
  const kind = h("select", { "aria-label": "Brand" },
    list.map(x => h("option", { value: x.kind, text: x.title + (x.beta ? " (beta)" : ""), selected: x.kind === S.addForm.kind })));
  kind.addEventListener("change", () => { S.addForm = { kind: kind.value, values: {} }; renderSettings(true); });
  const inputs = (dr ? dr.fields : []).map(f => {
    const val = S.addForm.values[f.key] ?? "";
    const el = f.options
      ? h("select", { "aria-label": f.label }, f.options.map(o => h("option", { value: o, text: o, selected: (val || f.placeholder) === o })))
      : h("input", { class: "text-input", type: f.secret ? "password" : "text", placeholder: f.placeholder || "", value: val,
          "aria-label": f.label, autocomplete: "off", spellcheck: "false" });
    el.addEventListener("input", () => { S.addForm.values[f.key] = el.value; });
    el.addEventListener("change", () => { S.addForm.values[f.key] = el.value; });
    if (f.options && !val) S.addForm.values[f.key] = el.value;
    return [h("span", { text: f.label }), el];
  });
  const add = h("button", {
    class: "btn btn-primary", type: "button", text: "Add light",
    onclick: async () => {
      const values = { ...S.addForm.values };
      const missing = (dr ? dr.fields : []).find(f => !f.options && !f.label.includes("empty") && !String(values[f.key] || "").trim());
      if (missing) return toast(`Fill in ${missing.label.toLowerCase()}.`, true);
      add.disabled = true;
      const r = await callRaw("add_device", S.addForm.kind, values);
      add.disabled = false;
      if (!r.ok) return toast(`Couldn't add it: ${r.error}`, true);
      toast(`${dr ? dr.title : "Light"} added`);
      S.addForm.values = {};
      refresh(true);
      renderSettings(true);
    },
  });
  return h("section", { class: "set-section", id: "add-section" },
    h("h3", { text: "Add a light" }),
    h("p", { class: "hint", text: "Most lights show up on their own. If yours doesn't, pick its brand and fill in where it is." }),
    h("div", { class: "form-grid" }, h("span", { text: "Brand" }), kind, inputs.flat()),
    dr && dr.beta ? h("p", { class: "hint", style: "margin-top:8px; font-size:12px",
      text: `${dr.title} support is new. If something doesn't work, please open an issue on the project page.` }) : null,
    dr && dr.pairing ? h("div", { class: "pairing", text: dr.pairing }) : null,
    h("div", { class: "form-actions" }, add));
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
    installBlock(o),
    o.path ? h("p", { class: "hint", style: "margin-bottom:12px",
      text: o.pawnio
        ? "Memory (RAM) and some motherboard lighting only appear when OpenRGB runs as administrator."
        : "Memory (RAM) lighting needs the PawnIO driver (pawnio.eu) and OpenRGB running as administrator." }) : null,
    o.path ? h("div", { class: "dev-actions", style: "padding-left:0" },
      h("button", { class: "btn btn-primary", type: "button", text: "Restart as administrator", onclick: restart(true) }),
      h("button", { class: "btn", type: "button", text: "Restart normally", onclick: restart(false) }),
      h("button", { class: "btn btn-quiet", type: "button", text: "Stop OpenRGB",
        onclick: async () => { await call("openrgb_stop"); refresh(true); } })) : null,
    o.path ? h("label", { class: "toggle-row" }, h("span", { text: "Start OpenRGB automatically" }), auto) : null,
    o.path ? h("p", { class: "path mono", text: o.path }) : null);
}

/* "Get OpenRGB": only when it isn't on this PC yet, or while it's being fetched. */
function installBlock(o) {
  const inst = o.install || { state: "idle" };
  const busy = inst.state === "downloading" || inst.state === "unpacking";
  if (o.path && !busy && inst.state !== "failed") return null;
  return h("div", { class: "install" },
    h("p", { class: "hint", style: "margin-bottom:10px",
      text: "OpenRGB runs the RGB inside your PC: motherboard, memory, fans, coolers, mouse and keyboard. Glowstage gets the official portable version from its makers (codeberg.org/OpenRGB, free and open source) and keeps it in its own folder." }),
    busy ? h("div", { class: "progress" }, h("span", { style: `width:${Math.round((inst.progress || 0) * 100)}%` })) : null,
    inst.message && inst.state !== "idle" ? h("p", { class: "hint", style: "font-size:12px", text: inst.message }) : null,
    busy ? null : h("div", { class: "form-actions", style: "margin:0 0 10px" },
      h("button", { class: "btn btn-primary", type: "button", text: inst.state === "failed" ? "Try again" : `Get OpenRGB ${inst.version || ""}`.trim(),
        onclick: async () => { await call("openrgb_install"); refresh(true); } })));
}

function appSection() {
  const s = S.data.settings;
  const row = (key, label) => h("label", { class: "toggle-row" }, h("span", { text: label }),
    h("input", { type: "checkbox", checked: !!s[key],
      onchange: async e => { await call("set_setting", key, e.target.checked); refresh(true); } }));
  const about = S.data.about || {};
  return h("section", { class: "set-section" },
    h("h3", { text: "App" }),
    row("close_to_tray", "Keep running in the tray when I close the window"),
    row("autostart", "Start with Windows"),
    row("start_minimized", "Start hidden in the tray"),
    row("restore_on_launch", "Restore my lights when the app starts"),
    row("lights_on_at_startup", "Switch all lights on when Windows starts"),
    row("pc_lights_off_at_shutdown", "Switch the PC's lights off when Windows shuts down"),
    row("room_lights_off_at_shutdown", "Switch the room lights off when Windows shuts down"),
    row("show_strobe", "Strobe flashes in the light show"),
    h("div", { class: "form-actions", style: "margin-top:14px" },
      h("button", { class: "btn", type: "button", text: "Show the welcome tour", onclick: () => { closeSettings(); openWizard(); } }),
      h("button", { class: "btn", type: "button", text: "Restart Glowstage",
        onclick: async () => { toast("Restarting Glowstage…"); await call("restart_app"); } })),
    h("p", { class: "hint", style: "margin-top:16px", text: `${about.name || "Glowstage"} ${about.version || ""} · free and open source (GPL-3.0)` }),
    h("div", { class: "form-actions" },
      ...(about.donate || []).map(d => h("button", { class: "btn btn-support", type: "button",
        text: `♥ Support on ${d.name}`, onclick: () => call("open_link", d.url) })),
      about.repo ? h("button", { class: "btn", type: "button", text: "Project page", onclick: () => call("open_link", about.repo) }) : null,
      about.repo ? h("button", { class: "btn btn-quiet", type: "button", text: "Check for updates",
        onclick: () => call("open_link", about.repo + "/releases") }) : null));
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

/* ============================== pairing and the DMX patch ============================== */

function openPairDialog(target) {
  const dr = driverOf(target.kind);
  openModal({
    title: `Pair ${target.name || (dr ? dr.title : "this light")}`,
    body: h("p", { class: "hint", style: "font-size:14px",
      text: (dr && dr.pairing) || "Put the light in pairing mode, then press Pair." }),
    ok: "Pair",
    onOk: async () => {
      const r = await callRaw("pair", target.id);
      if (!r.ok) return r.error;
      toast(`Paired with ${target.name || "the light"}`);
      delete S.native[target.id];
      refresh(true);
      return true;
    },
  });
}

function openDmxEditor(hub) {
  const layouts = hub.layouts || ["RGB", "GRB", "RGBW", "DRGB", "DRGBW"];
  let rows = (hub.fixtures || []).map(f => ({ ...f }));
  const table = h("div", { class: "fixture-table" });
  const draw = () => {
    fill(table, 
      h("span", { class: "eyebrow", text: "Name" }), h("span", { class: "eyebrow", text: "Address" }),
      h("span", { class: "eyebrow", text: "Pixels" }), h("span", { class: "eyebrow", text: "Channels" }), h("span"),
      ...rows.flatMap((f, i) => {
        const inp = (key, type, extra = {}) => {
          const el = h("input", { class: "text-input" + (type === "number" ? " mono" : ""), type, value: f[key] ?? "", ...extra });
          el.addEventListener("input", () => { f[key] = type === "number" ? Number(el.value) : el.value; });
          return el;
        };
        const sel = h("select", {}, layouts.map(l => h("option", { value: l, text: l, selected: (f.layout || "RGB") === l })));
        sel.addEventListener("change", () => { f.layout = sel.value; });
        return [inp("name", "text", { "aria-label": "Fixture name", placeholder: `Fixture ${i + 1}` }),
          inp("start", "number", { min: 1, max: 512, "aria-label": "First DMX channel" }),
          inp("pixels", "number", { min: 1, max: 170, "aria-label": "Pixels" }), sel,
          h("button", { class: "icon-btn", type: "button", "aria-label": "Remove fixture", text: "✕",
            onclick: () => { rows.splice(i, 1); draw(); } })];
      }));
  };
  draw();
  const addRow = h("button", { class: "btn", type: "button", text: "+ Add fixture", onclick: () => {
    const last = rows[rows.length - 1];
    const next = last ? (Number(last.start) || 1) + (Number(last.pixels) || 1) * (last.layout || "RGB").length : 1;
    rows.push({ name: "", start: next, pixels: 1, layout: "RGB" });
    draw();
  } });
  openModal({
    title: `Fixtures on ${hub.name || "this universe"}`,
    wide: true,
    body: h("div", {},
      h("p", { class: "hint", style: "margin-bottom:12px",
        text: "Each fixture starts at its DMX address and uses one channel per color for every pixel (a plain RGB par is 1 pixel, RGB)." }),
      table, h("div", { class: "form-actions" }, addRow)),
    ok: "Save fixtures",
    onOk: async () => {
      const used = new Set();
      const out = [];
      for (const [i, f] of rows.entries()) {
        const start = Math.round(Number(f.start)), pixels = Math.round(Number(f.pixels) || 1), layout = f.layout || "RGB";
        if (!(start >= 1 && start <= 512)) return `Fixture ${i + 1}: the address must be 1 to 512.`;
        if (start + pixels * layout.length - 1 > 512) return `Fixture ${i + 1} runs past channel 512.`;
        let id = f.id || `f${i + 1}`;
        while (used.has(id)) id += "x";
        used.add(id);
        out.push({ ...f, id, name: (f.name || "").trim() || `Fixture ${i + 1}`, start, pixels, layout });
      }
      const r = await callRaw("set_dmx_fixtures", hub.id, out);
      if (!r.ok) return r.error;
      toast("Fixtures saved");
      refresh(true);
      return true;
    },
  });
}

/* ============================== the welcome tour ============================== */

const WIZ_STEPS = ["Find lights", "PC lighting", "Place them", "Play music"];
S.wiz = { open: false, step: 0, sig: "" };

function openWizard() {
  S.wiz.open = true;
  S.wiz.step = 0;
  S.wiz.sig = "";
  $("#wizard").hidden = false;
  call("search");
  renderWizard(true);
}

async function finishWizard() {
  S.wiz.open = false;
  $("#wizard").hidden = true;
  await call("set_setting", "setup_done", true);
  refresh(true);
}

function renderWizard(force = false) {
  if (!S.wiz.open || !S.data) return;
  const step = S.wiz.step;
  const devs = S.data.devices.filter(d => !d.hidden);
  const sig = JSON.stringify([step, devs.map(d => [d.id, d.name, d.online, d.paired]), S.data.openrgb, S.data.hubs]);
  if (!force && sig === S.wiz.sig) return;
  S.wiz.sig = sig;

  fill($("#wizard-steps"), ...WIZ_STEPS.map((t, i) =>
    h("li", { class: i < step ? "is-done" : i === step ? "is-current" : "", text: t, "aria-current": i === step ? "step" : null })));
  $("#wizard-back").hidden = step === 0;
  $("#wizard-next").textContent = step === WIZ_STEPS.length - 1 ? "Finish" : "Next";
  $("#wizard-skip").hidden = step === WIZ_STEPS.length - 1;

  const body = $("#wizard-body");
  if (step === 0) {
    const brands = (S.data.drivers || []).map(x => x.title.replace(/ \(.*\)$/, ""));
    fill(body, 
      h("h2", { id: "wizard-title", text: "Welcome to Glowstage" }),
      h("p", { class: "hint", text: "Let's find your lights. Glowstage looks on your network and inside your PC; lights show up here as they're found." }),
      devs.length ? h("div", { class: "found-list" }, devs.map(d => h("div", { class: "found-item" },
        h("span", { class: "dev-dot" + (d.online ? " is-on" : d.paired === false ? " is-warn" : "") }),
        h("span", { class: "found-name", text: d.name }),
        h("span", { class: "found-kind", text: kindTitle(d) }),
        d.paired === false ? h("button", { class: "btn btn-primary", type: "button", text: "Pair", onclick: () => openPairDialog(d) }) : null)))
        : null,
      h("div", { class: "scanning", text: devs.length ? "Still looking…" : "Looking for lights… this can take up to a minute." }),
      h("p", { class: "hint", style: "margin-top:14px; font-size:12px",
        text: `Works with ${brands.join(", ")}, and the RGB in your PC through OpenRGB. Missing one? Add it by IP address later in Setup.` }));
  } else if (step === 1) {
    const o = S.data.openrgb;
    const pc = devs.filter(d => d.group === "pc");
    fill(body, 
      h("h2", { id: "wizard-title", text: "The RGB in your PC" }),
      o.status === "connected"
        ? h("p", { class: "hint", text: `OpenRGB is running and found ${pc.length} light${pc.length === 1 ? "" : "s"} in your PC: ${pc.map(d => d.name).join(", ") || "none yet"}.` })
        : o.path ? h("p", { class: "hint", text: o.message || "OpenRGB isn't running yet." }) : null,
      installBlock(o),
      o.path && o.status !== "connected" ? h("div", { class: "form-actions" },
        h("button", { class: "btn btn-primary", type: "button", text: "Start OpenRGB",
          onclick: async () => { await call("openrgb_restart", false); toast("Starting OpenRGB"); } })) : null,
      h("p", { class: "hint", style: "margin-top:14px; font-size:12px", text: "No RGB in your PC? Just press Next." }));
  } else if (step === 2) {
    fill(body, 
      h("h2", { id: "wizard-title", text: "Put them where they are" }),
      h("p", { class: "hint", text: "The light show runs chases from left to right across the lights in front of you, and lights the wall behind you as one wash. Glowstage made a first guess from what each light is." }),
      h("p", { class: "hint", text: "Check the stage on the left of the window: if a light is in the wrong spot, press Arrange and drag it to where it is, as seen from your chair." }),
      h("div", { class: "form-actions" },
        h("button", { class: "btn", type: "button", text: "Arrange the stage now",
          onclick: async () => { await finishWizard(); S.arrange = true; renderStage(); } })));
  } else {
    fill(body, 
      h("h2", { id: "wizard-title", text: "Play something loud" }),
      h("p", { class: "hint", text: "Start any music on this PC: Spotify, YouTube, a game. The light show listens to what the PC plays (no microphone), hears the genre and follows the beat, build-ups and drops." }),
      h("button", { class: "btn btn-show", type: "button", text: "Start the light show",
        onclick: async () => {
          await call("update", null, { on: true, mode: "effect", effect: "music_show" });
          await finishWizard();
          switchTab("show");
        } }),
      h("p", { class: "hint", style: "margin-top:14px; font-size:12px", text: "Presets, colors and effects are on the right side of the window, brightness per light at the bottom." }));
  }
}

function bindSetup() {
  $("#open-settings").addEventListener("click", openSettings);
  $("#close-settings").addEventListener("click", closeSettings);
  $("#scrim").addEventListener("click", closeSettings);
  $("#wizard-skip").addEventListener("click", finishWizard);
  $("#wizard-back").addEventListener("click", () => { S.wiz.step = Math.max(0, S.wiz.step - 1); renderWizard(true); });
  $("#wizard-next").addEventListener("click", () => {
    if (S.wiz.step >= WIZ_STEPS.length - 1) return finishWizard();
    S.wiz.step += 1;
    renderWizard(true);
  });
}
