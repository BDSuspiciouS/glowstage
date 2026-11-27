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
  if (s && s.layer === "back") return h("span", { class: "tape", text: "Back wall" });
  return h("span", { class: "tape", text: d.group === "pc" ? "In your PC" : "In front of you" });
}

/* ============================== the drawer ============================== */

S.addForm = { kind: "wled", values: {} };

function settingsSignature() {
  const d = S.data;
  return JSON.stringify([d.devices.map(x => [x.id, x.name, x.online, x.error, x.paired, x.hidden, x.zones, x.label]),
    d.hubs, d.openrgb, d.genreAi, d.settings, d.games, (d.drivers || []).length, d.stage, d.schedules,
    (d.presets || []).map(p => p.name), d.hotkeysTaken, d.remote, d.appLooks]);
}

function renderSettings(force = false) {
  const body = $("#settings-body");
  if (!force && body.contains(document.activeElement) && document.activeElement.matches("input, select")) return;
  const sig = settingsSignature();
  if (!force && sig === S.settingsSig) return;
  S.settingsSig = sig;
  const scroll = body.scrollTop;
  fill(body, lightsSection(), hubsSection(), addSection(), openrgbSection(), genreSection(), gamesSection(), automaticSection(), scheduleSection(), phoneSection(), appSection());
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
    const fans = zones.filter(z => z.detected > 0 || (z.detected === -1 && z.leds > 0)).length;
    const zoneGrid = zones.length ? h("details", { class: "zones", open: S.openZones && S.openZones.has(d.id) ? "" : null,
      ontoggle: e => { S.openZones = S.openZones || new Set(); e.target.open ? S.openZones.add(d.id) : S.openZones.delete(d.id); } },
      h("summary", { text: `LEDs per channel · ${zones.length} channels${fans ? `, ${fans} in use` : ""}` }),
      h("div", { class: "zone-grid" },
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
          : `How many LEDs are on this channel, 0 if nothing is plugged in. Add up everything daisy-chained on it. Max ${zones[0].max}.` }))) : null;
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

/* The enhanced genre AI: MTG's Discogs-EffNet, fetched from its makers when the user asks. It isn't
   shipped (non-commercial license), and the built-in YAMNet takes over again after Remove. */
function genreSection() {
  const g = S.data.genreAi;
  if (!g) return null;
  const busy = g.state === "downloading";
  const on = g.installed;
  if (S.genreWas === "downloading" && g.state === "done") toast("Enhanced genre AI is on: the show now knows 400 music styles");
  S.genreWas = g.state;
  const quiet = (text, onclick) => h("button", { class: "btn btn-quiet", type: "button", text, onclick });
  return h("section", { class: "set-section", id: "set-genre" },
    h("h3", { text: "Genre AI" }),
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (on ? " is-on" : "") }),
      on ? "Enhanced · knows 400 music styles" : "Built in · knows about 50 broad genres"),
    h("p", { class: "hint", style: "margin-bottom:10px", text: on
      ? "The light show picks its style from 400 music styles, like deep house, tech house, tribal house, trap and reggaeton. It runs on this PC, offline."
      : "The built-in AI often hears afro, melodic and tech house as jazz or folk, so those songs get a soft show without drops. The enhanced AI knows the 400 styles record shops file music under, like deep house, tech house, trap and reggaeton, and gets the style right far more often." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px",
      text: `${g.name} by the ${g.maker}, free for non-commercial use (${g.license}).`
        + (on ? "" : ` Glowstage gets it from its makers (essentia.upf.edu, ${g.mb} MB) and checks it before using it.`) }),
    busy ? h("div", { class: "progress" }, h("span", { style: `width:${Math.round((g.progress || 0) * 100)}%` })) : null,
    g.message && g.state !== "idle" && !(on && g.state === "done") ? h("p", { class: "hint", style: "font-size:12px", text: g.message }) : null,
    h("div", { class: "form-actions", style: "margin:0" },
      !on && !busy ? h("button", { class: "btn btn-primary", type: "button",
        text: g.state === "failed" ? "Try again" : `Get the enhanced genre AI · ${g.mb} MB`,
        onclick: async () => { await call("genre_ai_install"); refresh(true); } }) : null,
      on ? quiet("Remove", async () => { await call("genre_ai_remove"); toast("Back to the built-in genre AI"); refresh(true); }) : null,
      quiet("License", () => call("open_link", g.licenseUrl)),
      quiet("About the model", () => call("open_link", g.aboutUrl))));
}

/* Schedule: a preset, on, off or the light show at set times on chosen days. */
const DAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"];
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
function gamesSection() {
  const c = ((S.data.games || {}).cs2) || {};
  const status = !c.found ? "Not found on this PC"
    : !c.connected ? "Found on this PC"
    : c.inMatch ? `Playing along · ${c.team === "CT" ? "Counter-Terrorists" : c.team === "T" ? "Terrorists" : "in a match"}${c.team ? ` · ${c.health} HP` : ""}`
    : c.receiving ? "Connected · waiting for a match"
    : "Connected · starts when you open CS2";
  return h("section", { class: "set-section", id: "set-games" },
    h("h3", { text: "Games" }),
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (c.connected ? " is-on" : "") }), h("strong", { text: "Counter-Strike 2" }), h("span", { class: "muted", text: " · " + status })),
    h("p", { class: "hint", style: "margin-bottom:10px", text: "While you play, your room plays along: your team's color, your health across the lights, white when you're flashed, the bomb blinking ever faster, a flash for every kill. When you leave the match your own look comes back." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px", text: c.connected
      ? "CS2 sends its state to Glowstage on this PC through Valve's own game state integration. If CS2 was already running when you connected, restart it once."
      : "Uses Valve's own game state integration, the way RGB makers and stream overlays do: the game sends what happens, nothing reads or changes the game, so it's safe for VAC. Connecting puts one small file in CS2's cfg folder." }),
    h("div", { class: "form-actions", style: "margin:0" },
      !c.connected ? h("button", { class: "btn btn-primary", type: "button", text: "Connect", disabled: !c.found,
        onclick: async () => { if (await call("games_connect") !== undefined) toast("Connected: start CS2 and play"); refresh(true); } }) : null,
      c.connected ? h("button", { class: "btn btn-quiet", type: "button", text: "Disconnect",
        onclick: async () => { await call("games_disconnect"); toast("CS2 is disconnected"); refresh(true); } }) : null));
}

function automaticSection() {
  const s = S.data.settings;
  const looks = [["", "Nothing"], ["screen", "Follow the screen"], ["screen_sound", "Follow the screen, flash with the sound"],
    ...(S.data.presets || []).map(p => ["preset:" + p.name, p.name])];
  const save = async (key, value) => {
    const r = await callRaw("set_setting", key, value);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save that", true);
    S.settingsSig = "";
    refresh(true);
  };
  const pick = (key, label, options, value) => h("label", { class: "toggle-row" }, h("span", { text: label }),
    h("select", { class: "auto-look", onchange: e => save(key, key === "auto_show" ? e.target.value === "show" : e.target.value) },
      options.map(([v, t]) => h("option", { value: v, text: t, selected: v === value }))));
  return h("section", { class: "set-section", id: "set-automatic" },
    h("h3", { text: "Automatic" }),
    h("p", { class: "hint", text: "Your lights change by themselves and go back to how they were afterwards. Change anything yourself and Glowstage leaves them alone until it's over." }),
    pick("auto_game", "When a game is full screen", looks, s.auto_game || ""),
    pick("auto_video", "When a video is full screen (a browser or video player)", looks, s.auto_video || ""),
    pick("auto_show", "When music plays", [["", "Nothing"], ["show", "Start the light show"]], s.auto_show ? "show" : ""),
    pick("away_lights", "When you step away: the PC is locked, its screen goes off or it sleeps",
      [["", "Nothing"], ["pc", "Switch the PC's lights off"], ["all", "Switch all lights off"]], s.away_lights || ""),
    appLooks());
}

/* A look while an app is open: Focus while Photoshop is, the light show while a DJ app is. */
function appLooks() {
  const items = (S.data.appLooks || []).map(a => ({ ...a }));
  const presets = (S.data.presets || []).map(p => ["preset:" + p.name, p.name]);
  const looks = [["screen", "Follow the screen"], ["screen_sound", "Follow the screen, flash with the sound"],
    ["show", "The light show"], ...presets];
  const save = async () => {
    const r = await callRaw("set_app_looks", items);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save that", true);
    S.settingsSig = "";
    refresh(true);
  };
  const add = async () => {
    const open = await call("open_apps");
    if (!open) return;
    const have = new Set(items.map(a => a.exe));
    const choices = open.filter(a => !have.has(a.exe));
    const focus = presets.find(([v]) => v === "preset:Focus");
    openModal({
      title: "Give an app its own look",
      body: choices.length
        ? h("div", {}, h("p", { class: "hint", text: "Apps that are open right now. Pick one, then choose its look." }),
          h("div", { class: "app-pick-list" }, choices.map(a => h("button", {
            class: "app-pick", type: "button",
            onclick: () => { items.push({ ...a, action: (focus || presets[0] || ["screen"])[0] }); closeModal(); save(); },
          }, h("span", { text: a.name }), h("span", { class: "muted", text: a.exe })))))
        : h("p", { class: "hint", text: "Open the app first (it needs a window), then come back here." }),
      ok: "Close", cancel: false, onOk: () => true,
    });
  };
  const row = (a, i) => h("div", { class: "sched-row" },
    h("span", { class: "app-look-name", text: `While ${a.name} is open`, title: a.exe }),
    h("select", { class: "auto-look", "aria-label": `Look while ${a.name} is open`,
      onchange: e => { a.action = e.target.value; save(); } },
      looks.map(([v, t]) => h("option", { value: v, text: t, selected: v === a.action }))),
    h("button", { class: "icon-btn", type: "button", "aria-label": `Remove ${a.name}`, title: "Remove",
      onclick: () => { items.splice(i, 1); save(); } }, icon("x")));
  return h("div", { class: "app-looks" },
    h("p", { class: "hint", text: "Apps: a look while an app is open, back to your own when you close it. The first app in the list goes first." }),
    items.length ? h("div", { class: "sched-list" }, items.map(row)) : null,
    h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", text: items.length ? "Add another app" : "Add an app", onclick: add })));
}

function scheduleSection() {
  const items = (S.data.schedules || []).map(s => ({ ...s, days: [...s.days] }));
  const save = async () => {
    const r = await callRaw("set_schedules", items);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save the schedule", true);
    S.settingsSig = "";
    refresh(true);
  };
  const actions = [["off", "Lights off"], ["on", "Lights on"], ["show", "Start the light show"],
    ...(S.data.presets || []).map(p => ["preset:" + p.name, p.name])];
  const row = (s, i) => h("div", { class: "sched-row" + (s.enabled === false ? " is-paused" : "") },
    h("input", { class: "text-input sched-time", type: "time", value: s.time, "aria-label": "Time",
      onchange: e => { if (e.target.value) { s.time = e.target.value; save(); } } }),
    h("select", { class: "sched-action", "aria-label": "What happens", onchange: e => { s.action = e.target.value; save(); } },
      actions.map(([v, t]) => h("option", { value: v, text: t, selected: v === s.action }))),
    h("div", { class: "sched-days", role: "group", "aria-label": "Days" }, DAY_LETTERS.map((l, k) =>
      h("button", { type: "button", class: "day" + (s.days.includes(k) ? " is-on" : ""), text: l, title: DAY_NAMES[k],
        "aria-pressed": String(s.days.includes(k)), "aria-label": DAY_NAMES[k],
        onclick: () => {
          const on = s.days.includes(k);
          if (on && s.days.length === 1) return;          // at least one day
          s.days = on ? s.days.filter(x => x !== k) : [...s.days, k].sort();
          save();
        } }))),
    h("input", { type: "checkbox", class: "switch", checked: s.enabled !== false, "aria-label": "On",
      title: s.enabled !== false ? "On" : "Paused", onchange: e => { s.enabled = e.target.checked; save(); } }),
    h("button", { class: "icon-btn", type: "button", "aria-label": `Remove ${s.time}`, title: "Remove",
      onclick: () => { items.splice(i, 1); save(); } }, icon("x")));
  return h("section", { class: "set-section" },
    h("h3", { text: "Schedule" }),
    h("p", { class: "hint", text: "Your lights change by themselves at these times, as long as Glowstage runs (in the tray is enough)." }),
    items.length ? h("div", { class: "sched-list" }, items.map(row)) : null,
    h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", text: items.length ? "Add another time" : "Add a time",
        onclick: () => {
          const late = (S.data.presets || []).find(p => /late night/i.test(p.name));
          items.push({ time: "23:00", days: [0, 1, 2, 3, 4, 5, 6], action: late ? "preset:" + late.name : "off", enabled: true });
          save();
        } })));
}

/* Setup → Phone remote: the lights from your phone, on the same Wi-Fi (remote.py). */
function phoneSection() {
  const r = S.data.remote || {};
  const set = async on => {
    const res = await callRaw("remote_set", on);
    if (!res || res.ok === false) toast((res && res.error) || "Couldn't switch the phone remote", true);
    S.settingsSig = "";
    refresh(true);
  };
  const qr = h("div", { class: "phone-qr", role: "img", "aria-label": "QR code with the phone remote's link" });
  if (r.on && r.url) {
    if (S.qrFor === r.url) qr.innerHTML = S.qrSvg;     // (made here by Glowstage itself)
    else call("remote_qr").then(svg => { if (svg) { S.qrFor = r.url; S.qrSvg = svg; qr.innerHTML = svg; } });
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(r.url); toast("Link copied"); }
    catch { toast("Couldn't copy it: select the link and copy it yourself", true); }
  };
  return h("section", { class: "set-section", id: "set-phone" },
    h("h3", { text: "Phone remote" }),
    h("p", { class: "hint", text: "Switch your lights, set the brightness, pick a preset, start the light show or the sleep timer from your phone, on the same Wi-Fi. Only a phone with this code's link can: it carries a secret key." }),
    h("label", { class: "toggle-row" }, h("span", { text: "Control Glowstage from my phone" }),
      h("input", { type: "checkbox", checked: !!r.on, onchange: e => set(e.target.checked) })),
    !r.on ? null : !r.url ? h("p", { class: "hint", text: r.error || "Starting…" })
      : h("div", { class: "phone-box" }, qr,
        h("div", { class: "phone-text" },
          h("p", { text: "Scan the code with your phone's camera and open the link. Add the page to your home screen to keep it at hand." }),
          h("code", { class: "phone-url", text: r.url }),
          h("p", { class: "hint", text: "The first time, Windows may ask whether Glowstage may use the network: allow it for private networks." }),
          h("div", { class: "form-actions" },
            h("button", { class: "btn", type: "button", text: "Copy link", onclick: copy }),
            h("button", { class: "btn btn-quiet", type: "button", text: "New link", title: "The old link stops working",
              onclick: async () => { await call("remote_new_link"); toast("New link: a phone with the old one has to scan again"); S.settingsSig = ""; refresh(true); } })))));
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
    row("check_updates", "Check once a day for a new version (asks GitHub; sends nothing about you)"),
    row("hotkeys", "Hotkeys from any program: Ctrl+Alt+Shift+L lights on/off, +S the light show, +1 to +9 your first nine presets, +↑/↓ brightness, +N the sleep timer, +Z undo"),
    (S.data.hotkeysTaken || []).length && s.hotkeys
      ? h("p", { class: "hint", text: `Another program already uses ${S.data.hotkeysTaken.join(", ")}: those don't work here.` }) : null,
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

function openSettings() { go("setup"); }
function closeSettings() { go("home"); }

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
      h("span", { class: "th", text: "Name" }), h("span", { class: "th", text: "Address" }),
      h("span", { class: "th", text: "Pixels" }), h("span", { class: "th", text: "Channels" }), h("span"),
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
      h("p", { class: "hint", text: "You'll find the stage under Light show in the menu: if a light is in the wrong spot, press Arrange and drag it to where it is, as seen from your chair." }),
      h("div", { class: "form-actions" },
        h("button", { class: "btn", type: "button", text: "Arrange the stage now",
          onclick: async () => { await finishWizard(); go("show"); S.arrange = true; renderStage(); } })));
  } else {
    fill(body, 
      h("h2", { id: "wizard-title", text: "Play something loud" }),
      h("p", { class: "hint", text: "Start any music on this PC: Spotify, YouTube, a game. The light show listens to what the PC plays (no microphone), hears the genre and follows the beat, build-ups and drops." }),
      h("button", { class: "btn btn-show", type: "button", text: "Start the light show",
        onclick: async () => {
          await call("update", null, { on: true, mode: "effect", effect: "music_show" });
          await finishWizard();
          go("show");
        } }),
      h("p", { class: "hint", style: "margin-top:14px; font-size:12px", text: "Presets are on Home. Click any light to give it a color or an effect." }));
  }
}

function bindSetup() {
  $("#wizard-skip").addEventListener("click", finishWizard);
  $("#wizard-back").addEventListener("click", () => { S.wiz.step = Math.max(0, S.wiz.step - 1); renderWizard(true); });
  $("#wizard-next").addEventListener("click", () => {
    if (S.wiz.step >= WIZ_STEPS.length - 1) return finishWizard();
    S.wiz.step += 1;
    renderWizard(true);
  });
}
