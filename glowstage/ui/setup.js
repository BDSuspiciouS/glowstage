"use strict";
/* Setup: lights and brands, bridges, PC lighting (OpenRGB) and app settings; pairing, the DMX
   patch, and the welcome tour that runs the first time. */

const driverOf = kind => (S.data.drivers || []).find(x => x.kind === kind);
const hubOf = d => (S.data.hubs || []).find(hb => d.id.startsWith(hb.id + ":"));

function kindTitle(d) {
  if (d.kind === "openrgb") return "PC · OpenRGB";
  if (d.kind === "steelseries") return "PC · SteelSeries GG (beta)";
  if (d.kind === "qmk") return "Keyboard";
  if (d.kind === "razer") return "PC · Razer Synapse";
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
  return JSON.stringify([d.devices.map(x => [x.id, x.name, x.online, x.error, x.paired, x.hidden, x.zones, x.label, x.balance]),
    d.hubs, d.openrgb, d.razer, d.steelseries, d.genreAi, d.settings, d.games, d.obs, (d.drivers || []).length, d.stage, d.schedules,
    (d.presets || []).map(p => p.name), d.hotkeysTaken, d.hotkeyKeys, d.remote, d.appLooks, d.groups]);
}

function renderSettings(force = false) {
  const body = $("#settings-body");
  if (!force && body.contains(document.activeElement) && document.activeElement.matches("input, select")) return;
  const sig = settingsSignature();
  if (!force && sig === S.settingsSig) return;
  S.settingsSig = sig;
  const scroll = body.scrollTop;
  fill(body, lightsSection(), hubsSection(), addSection(), openrgbSection(), razerSection(), steelseriesSection(), genreSection(), gamesSection(), automaticSection(), scheduleSection(), phoneSection(), backupSection(), appSection());
  // (the page title is a level 1: the sections are level 2 and what's inside them level 3, whatever their size)
  for (const hd of body.querySelectorAll("h3")) hd.setAttribute("aria-level", "2");
  for (const hd of body.querySelectorAll("h4")) hd.setAttribute("aria-level", "3");
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
    const sub = [kindTitle(d) + (dr && dr.beta ? " (beta)" : ""), d.host, d.online ? d.label : meta.text, balanceHint(d)]
      .filter(Boolean).join(" · ");
    const child = hubOf(d);
    const actions = h("div", { class: "dev-actions" },
      d.paired === false ? h("button", { class: "btn btn-primary", type: "button", text: "Pair", onclick: () => openPairDialog(d) }) : null,
      h("button", {
        class: "btn", type: "button", text: "Find", disabled: !d.online,
        title: "Flash this light for a few seconds so you can see which one it is",
        "aria-label": `Find ${d.name}: flash it for a few seconds`,
        onclick: async () => { const r = await callRaw("identify", d.id); if (r && r.ok !== false) toast(`${d.name} is flashing`); else toast((r && r.error) || "Couldn't make it flash", true); },
      }),
      h("button", {
        class: "btn", type: "button", text: "Balance",
        title: "Make this light match the others: its white, and how bright it is at full",
        "aria-label": `Balance ${d.name}: its white and its brightness at full`,
        onclick: () => openBalanceDialog(d),
      }),
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
  return h("section", { class: "set-section", id: "set-lights" },
    h("h3", {}, "Lights", h("span", { class: "section-tools" },
      h("button", { class: "btn", type: "button", text: "Search now",
        onclick: async () => { await call("search"); toast("Looking for lights on your network…"); } }))),
    h("p", { class: "hint", text: "Everything Glowstage found, in your PC and on your network. Rename lights, hide ones you don't use, set up fan LED counts." }),
    rows.length ? groupsPart() : null,
    rows.length ? rows : h("p", { class: "hint", text: "No lights found yet." }),
    h("label", { class: "toggle-row", style: "margin-top:12px" }, h("span", { text: "When a light changes to a new color or preset" }),
      h("select", { "aria-label": "How lights change", onchange: async e => {
          const r = await callRaw("set_setting", "glide", e.target.value);
          if (!r || r.ok === false) toast((r && r.error) || "Couldn't save that", true);
          S.settingsSig = ""; refresh(true); } },
        [["instant", "Change at once"], ["smooth", "Glide (a third of a second)"], ["slow", "Fade slowly (two seconds)"]]
          .map(([v, t]) => h("option", { value: v, text: t, selected: v === (S.data.settings.glide || "smooth") })))),
    h("p", { class: "hint", text: "Dragging a slider or the color wheel always follows your hand at once, and effects move by themselves. Some lights (Govee, WiZ, Elgato, Magic Home, Twinkly) change at once whatever you pick." }));
}

/* ---- your own groups: "Desk", "Bed", "Living room"... Home, the phone, the tray and links use them ---- */

const MAX_GROUPS = 12;

function groupsPart() {
  const groups = S.data.groups || [];
  const rows = groups.map((g, i) => {
    const names = g.ids.map(id => byId(id)).filter(Boolean).map(d => d.name);
    const away = g.ids.length - names.length;
    const sub = names.length ? `${plural(names.length, "light")}: ${names.join(", ")}` : "No lights right now";
    return h("div", { class: "dev-row" },
      h("div", { class: "dev-line" }, h("span", { class: "group-row-name", text: g.name })),
      h("div", { class: "dev-sub", text: sub + (away && names.length ? ` (and ${away} that ${away === 1 ? "isn't" : "aren't"} here now)` : "") }),
      h("div", { class: "dev-actions" },
        h("button", { class: "btn", type: "button", text: "Edit", "aria-label": `Edit ${g.name}`, onclick: () => openGroupDialog(g) }),
        h("button", { class: "btn", type: "button", text: "Move up", "aria-label": `Move ${g.name} up`, disabled: i === 0,
          onclick: async () => { await call("move_group", g.name, -1); refresh(true); } }),
        h("button", { class: "btn", type: "button", text: "Move down", "aria-label": `Move ${g.name} down`, disabled: i === groups.length - 1,
          onclick: async () => { await call("move_group", g.name, 1); refresh(true); } }),
        h("button", { class: "btn btn-quiet", type: "button", text: "Delete", "aria-label": `Delete ${g.name}`, onclick: () => confirmDeleteGroup(g.name) })));
  });
  return h("section", { class: "your-groups" },
    h("h4", {}, "Your groups", h("span", { class: "count", text: groups.length || "" })),
    h("p", { class: "hint", text: groups.length
      ? "Each group has its own card on Home, a row on your phone and a place in the tray menu. A light can be in more than one."
      : "Put lights together under your own name, like Desk or Bed, to switch them with one button. A light can be in more than one group." }),
    rows,
    h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", text: "Make a group", disabled: groups.length >= MAX_GROUPS, onclick: () => openGroupDialog(null) }),
      groups.length >= MAX_GROUPS ? h("span", { class: "hint", text: `You can have ${MAX_GROUPS} groups at most.` }) : null));
}

function confirmDeleteGroup(name) {
  openModal({
    title: `Delete “${name}”?`,
    body: h("p", { class: "hint", text: "The group is removed from Home, your phone and the tray menu. Your lights don't change." }),
    ok: "Delete group",
    onOk: async () => { await call("delete_group", name); refresh(true); return true; },
  });
}

/** The window to make a group (`group` is null) or change one: a name and the lights to tick, in
 *  the same two parts as when saving a preset. Lights that are gone right now stay in the group. */
function openGroupDialog(group) {
  const input = h("input", { class: "text-input", style: "width:100%", placeholder: "e.g. Desk", maxlength: "24", "aria-label": "Group name", value: group ? group.name : "" });
  const boxes = new Map(), picker = [];
  const ticked = new Set(group ? group.ids : []);
  for (const [key, title] of [["pc", "In your PC"], ["room", "In your room"]]) {
    const lights = visible().filter(d => (d.group === "pc") === (key === "pc"));
    if (!lights.length) continue;
    const setAll = on => { for (const d of lights) boxes.get(d.id).checked = on; };
    const rows = lights.map(d => {
      const box = h("input", { type: "checkbox", class: "switch", checked: ticked.has(d.id), "aria-label": d.name });
      boxes.set(d.id, box);
      return h("label", { class: "check-row" }, h("span", { class: "check-text" }, h("span", { class: "check-name", text: d.name })), box);
    });
    picker.push(
      h("div", { class: "pick-group" },
        h("span", { class: "field-label", text: title }),
        h("span", { class: "pick-all" },
          h("button", { class: "btn btn-quiet", type: "button", text: "All", "aria-label": `All lights ${title.toLowerCase()}`, onclick: () => setAll(true) }),
          h("button", { class: "btn btn-quiet", type: "button", text: "None", "aria-label": `No lights ${title.toLowerCase()}`, onclick: () => setAll(false) }))),
      h("div", { class: "check-list" }, rows));
  }
  openModal({
    title: group ? `Edit “${group.name}”` : "Make a group",
    body: h("div", {}, h("p", { class: "hint", style: "margin-bottom:12px", text: "Choose a name and tick the lights that belong in it." }), input, picker),
    ok: group ? "Save group" : "Make group",
    onOk: async () => {
      const name = input.value.trim();
      if (!name) return "Give the group a name.";
      const ids = [...boxes].filter(([, b]) => b.checked).map(([id]) => id);
      if (!ids.length) return "Choose at least one light.";
      let r;
      if (!group) r = await callRaw("create_group", name, ids);
      else {
        r = name === group.name ? { ok: true } : await callRaw("rename_group", group.name, name);
        if (r.ok !== false) { group = { ...group, name }; r = await callRaw("set_group_lights", name, ids); }   // (renamed: a retry goes by the new name)
      }
      if (!r || r.ok === false) return (r && r.error) || "That didn't work.";
      toast(group ? `Saved ${name}` : `Made ${name}`);
      refresh(true);
      return true;
    },
  });
  setTimeout(() => input.focus(), 30);
  input.addEventListener("keydown", e => { if (e.key === "Enter") $("#modal-ok").click(); });
}

/** "Balanced: warmer, 70% at full, colors in GRB order" for a light that has something set; nothing for one that hasn't. */
function balanceHint(d) {
  const b = d.balance;
  if (!b) return "";
  const order = b.order && b.order !== "RGB" ? b.order : "";
  if (!b.warmth && (b.max ?? 100) >= 100 && !order) return "";
  const parts = [b.warmth > 0 ? "warmer" : b.warmth < 0 ? "cooler" : "", (b.max ?? 100) < 100 ? `${b.max}% at full` : "",
    order ? `colors in ${order} order` : ""].filter(Boolean);
  return `Balanced: ${parts.join(", ")}`;
}

/** The order a light is wired in, from what you see on it. `current` is the order it is set to now
    (the test already goes through it); `seenRed` and `seenGreen` are what the light showed while
    Glowstage showed red and green ("R", "G" or "B"). Blue is whatever is left. Slot i of what the
    light is sent carries the channel `current[i]` and shows `seen[that channel]`, so the order the
    light uses is those seen colors in slot order. Null when the two answers can't both be true. */
function orderFromSightings(current, seenRed, seenGreen) {
  if (!seenRed || !seenGreen || seenRed === seenGreen) return null;
  const seen = { R: seenRed, G: seenGreen, B: "RGB".replace(seenRed, "").replace(seenGreen, "") };
  return [...current].map(c => seen[c]).join("");
}

/** Match one light against the others: its white (cooler to warmer) and what full brightness means
    for it. Changes show while you drag; "Show white" holds a plain white on this light while the
    window is open, so you can put it next to the others. "Test colors" shows red, green and blue in
    turn, and two questions about what you saw set the light's color order. Closing puts the light back. */
function openBalanceDialog(d) {
  const start = d.balance || { warmth: 0, max: 100 };
  const cur = { warmth: start.warmth || 0, max: start.max ?? 100, order: start.order || "RGB" };
  const warmthText = v => v === 0 ? "As it comes" : v > 0 ? `Warmer ${v}` : `Cooler ${-v}`;
  const warmth = h("input", { type: "range", min: "-50", max: "50", step: "1", value: String(cur.warmth), "aria-label": `White balance of ${d.name}` });
  const warmthVal = h("span", { class: "num muted balance-val" });
  const top = h("input", { type: "range", min: "20", max: "100", step: "1", value: String(cur.max), "aria-label": `Brightness at 100% of ${d.name}` });
  const topVal = h("span", { class: "num muted balance-val" });
  const orderVal = h("span", { class: "num muted balance-val" });
  const sync = () => {
    warmthVal.textContent = warmthText(cur.warmth);
    warmth.setAttribute("aria-valuetext", warmthText(cur.warmth));
    topVal.textContent = `${cur.max}%`;
    orderVal.textContent = cur.order === "RGB" ? "As it comes" : `${cur.order} order`;
    resetOrder.hidden = cur.order === "RGB";
  };
  const save = () => callRaw("set_balance", d.id, cur.warmth, cur.max);
  const saveSoon = throttle(save, 90);          // (a few times a second while dragging, the last value always)
  warmth.addEventListener("input", () => { cur.warmth = Number(warmth.value); sync(); saveSoon(); });
  top.addEventListener("input", () => { cur.max = Number(top.value); sync(); saveSoon(); });
  const resetWarmth = h("button", { class: "link-btn", type: "button", text: "Reset",
    onclick: () => { warmth.value = "0"; cur.warmth = 0; sync(); save(); } });
  const resetTop = h("button", { class: "link-btn", type: "button", text: "Reset",
    onclick: () => { top.value = "100"; cur.max = 100; sync(); save(); } });
  let white = false;
  const showWhite = h("input", { type: "checkbox", class: "switch", id: "balance-white", "aria-label": "Show white on this light", disabled: !d.online,
    onchange: async e => {
      const box = e.target;
      const r = await callRaw("show_white", d.id, box.checked);
      if (!r || r.ok === false) { box.checked = !box.checked; toast((r && r.error) || "That didn't work", true); return; }
      white = box.checked;
    } });

  // ---- the color test and the color order
  const COLOR_NAMES = { R: "Red", G: "Green", B: "Blue" };
  const orderChoice = label => {
    const sel = h("select", { "aria-label": label },
      h("option", { value: "", text: "Pick one" }),
      Object.entries(COLOR_NAMES).map(([v, t]) => h("option", { value: v, text: t })));
    sel.addEventListener("change", applyOrder);
    return sel;
  };
  const seenRed = orderChoice("What this light shows when Glowstage shows red");
  const seenGreen = orderChoice("What this light shows when Glowstage shows green");
  const orderNote = h("p", { class: "control-hint", role: "status" });
  const setOrder = async order => {
    const r = await callRaw("set_color_order", d.id, order);
    if (!r || r.ok === false) { toast((r && r.error) || "That didn't work", true); return false; }
    cur.order = order; seenRed.value = ""; seenGreen.value = ""; sync();
    return true;
  };
  async function applyOrder() {
    if (!seenRed.value || !seenGreen.value) { orderNote.textContent = ""; return; }
    const next = orderFromSightings(cur.order, seenRed.value, seenGreen.value);
    if (!next) { orderNote.textContent = "Red and green can't look the same. Run the test again and look closely."; return; }
    if (next === cur.order) { orderNote.textContent = "Those colors already match."; seenRed.value = ""; seenGreen.value = ""; return; }
    if (await setOrder(next)) orderNote.textContent = `Color order set to ${next}. Run Test colors again: red should look red.`;
  }
  const resetOrder = h("button", { class: "link-btn", type: "button", text: "Reset",
    onclick: async () => { if (await setOrder("RGB")) orderNote.textContent = ""; } });
  const nowText = h("span", { class: "balance-now", "aria-live": "polite" });
  let testing = false, poll = null;
  const testBtn = h("button", { class: "btn", type: "button", text: "Test colors", disabled: !d.online });
  const testEnded = () => {
    testing = false; clearInterval(poll); poll = null;
    testBtn.textContent = "Test colors"; nowText.textContent = "";
  };
  const watchTest = async () => {
    const r = await callRaw("color_test_step", d.id);
    const step = r && r.ok !== false ? r.result : "";
    if (!testing) return;
    if (step) nowText.textContent = `Now: ${step}`; else testEnded();
  };
  testBtn.addEventListener("click", async () => {
    if (testing) { testEnded(); await callRaw("color_test", d.id, false); return; }
    const r = await callRaw("color_test", d.id, true);
    if (!r || r.ok === false) { toast((r && r.error) || "That didn't work", true); return; }
    showWhite.checked = false; white = false;   // (the test takes over from the white)
    testing = true; testBtn.textContent = "Stop test"; nowText.textContent = "Now: red";
    poll = setInterval(watchTest, 300);
  });
  sync();
  openModal({
    title: `Balance ${d.name}`,
    body: h("div", { class: "balance-dialog" },
      h("p", { class: "hint", text: "Lights show the same color a little differently. Match this one to the others: put it next to them with Show white, then nudge the sliders until the whites look alike." }),
      h("div", { class: "control-head" }, h("span", { class: "control-title", text: "White balance" }), warmthVal),
      h("div", { class: "balance-row" }, h("span", { class: "muted", text: "Cooler" }), warmth, h("span", { class: "muted", text: "Warmer" })),
      h("div", { class: "control-hint" }, resetWarmth),
      h("div", { class: "control-head" }, h("span", { class: "control-title", text: "Brightness at 100%" }), topVal),
      h("div", { class: "balance-row" }, top),
      h("div", { class: "control-hint" }, h("span", { text: "Lower it if this light is brighter than the others at full. The brightness numbers in Glowstage stay the same. " }), resetTop),
      h("label", { class: "check-row", for: "balance-white" },
        h("span", { class: "check-text" }, h("span", { class: "check-name", text: "Show white" }),
          h("span", { class: "muted", text: d.online ? "A plain white on this light while this window is open." : "This light isn't answering." })),
        showWhite),
      h("div", { class: "control-head" }, h("span", { class: "control-title", text: "Color order" }), orderVal),
      h("div", { class: "control-hint" }, h("span", { text: "Pick red and the strip shows green? Its colors are wired in another order. " }), resetOrder),
      h("div", { class: "balance-row" }, testBtn, nowText),
      h("p", { class: "balance-ask", text: "Colors look mixed up?" }),
      h("label", { class: "balance-row" }, h("span", { text: "When Glowstage shows red, this light shows" }), seenRed),
      h("label", { class: "balance-row" }, h("span", { text: "When it shows green, it shows" }), seenGreen),
      orderNote,
      h("div", { class: "control-hint", text: "Lights playing their own built-in effects keep those effects' own colors." })),
    ok: "Done",
    cancel: false,
    onOk: () => true,
    onClose: async () => {
      await save();                       // (the last value, and before the light goes back to what it showed)
      const wasTesting = testing;
      testEnded();
      if (wasTesting) await callRaw("color_test", d.id, false);
      if (white) await callRaw("show_white", d.id, false);
      refresh(true);
    },
  });
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
  return h("section", { class: "set-section", id: "set-pc" },
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

/* Razer devices through Razer Synapse's Chroma SDK (devices/razer_chroma.py): its own small card under OpenRGB. */
function razerSection() {
  const r = S.data.razer || { types: [], kinds: [] };
  const save = async (on, types) => {
    const res = await callRaw("razer_set", on, types);
    if (!res || res.ok === false) toast((res && res.error) || "Couldn't save that", true);
    S.settingsSig = "";
    refresh(true);
  };
  const toggleType = (id, yes) => save(!!r.on, r.kinds.map(k => k.id).filter(k => k === id ? yes : (r.types || []).includes(k)));
  const ok = r.status === "connected";
  return h("section", { class: "set-section", id: "set-razer" },
    h("h3", { text: "Razer devices (Synapse, beta)" }),
    h("p", { class: "hint", text: "Razer Synapse holds Razer keyboards, mice, headsets and mouse mats, so OpenRGB can't reach them. With this on, Glowstage lights them through Synapse's Chroma SDK. While it runs, Synapse's own lighting for these devices pauses. Switch it off, or quit Glowstage, and Synapse takes them back. It needs Razer Synapse running with its Chroma Connect module." }),
    h("label", { class: "toggle-row" }, h("span", { text: "Light my Razer devices through Synapse" }),
      h("input", { type: "checkbox", checked: !!r.on, "aria-label": "Light my Razer devices through Synapse",
        onchange: e => save(e.target.checked, r.types || []) })),
    r.on ? h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (ok ? " is-on" : " is-warn") }),
      h("span", { class: "muted", text: ok ? `${r.text} · ${r.lights} of ${(r.types || []).length} answering` : (r.text || "Starting...") })) : null,
    h("p", { class: "hint", style: "margin-top:10px", text: "Synapse can't say which devices are connected: tick the ones you have." }),
    h("div", { class: "check-list", role: "group", "aria-label": "Razer devices you have" },
      (r.kinds || []).map(k => h("label", { class: "check-row" }, h("span", { text: k.name.replace(/^Razer /, "") }),
        h("input", { type: "checkbox", checked: (r.types || []).includes(k.id), "aria-label": k.name,
          onchange: e => toggleType(k.id, e.target.checked) })))));
}

/* SteelSeries keyboards, mice and headsets through SteelSeries GG (devices/steelseries.py). Off until
   switched on, because the lights then replace GG's own effects on the devices that are ticked. */
function steelseriesSection() {
  const s = S.data.steelseries || { on: false, types: [], available: [], status: "off", message: "" };
  const save = async (on, types) => {
    const r = await callRaw("steelseries_set", on, types);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save that", true);
    S.settingsSig = "";
    refresh(true);
  };
  const ids = (s.available || []).map(x => x.id);
  const ticks = (s.available || []).map(t => h("label", { class: "toggle-row" }, h("span", { text: t.title }),
    h("input", { type: "checkbox", checked: s.types.includes(t.id), "aria-label": t.title,
      onchange: e => save(true, ids.filter(id => id === t.id ? e.target.checked : s.types.includes(id))) })));
  return h("section", { class: "set-section", id: "set-steelseries" },
    h("h3", { text: "SteelSeries devices (beta)" }),
    s.on ? h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (s.status === "connected" ? " is-on" : " is-warn") }), s.message || s.status) : null,
    h("p", { class: "hint", style: "margin-bottom:12px",
      text: "When SteelSeries GG is running it holds your SteelSeries keyboard, mouse and headset, and OpenRGB can't reach them. Glowstage can talk to GG instead, through the lighting connection SteelSeries made for apps like this. It only talks to GG on this PC." }),
    h("label", { class: "toggle-row" }, h("span", { text: "Light my SteelSeries devices through GG" }),
      h("input", { type: "checkbox", checked: !!s.on, "aria-label": "Light my SteelSeries devices through GG",
        onchange: e => save(e.target.checked, s.types) })),
    s.on ? h("div", {}, ticks) : null,
    h("p", { class: "hint", style: "font-size:12px;margin-top:8px",
      text: "While this is on, Glowstage's colors and effects replace GG's own on the devices you tick. Switch it off and GG's lighting comes back. Mice and headsets with two zones can't be told apart, so with the headset ticked it gets them." }),
    h("p", { class: "hint", style: "font-size:12px",
      text: "SteelSeries support is new. If something doesn't work, please open an issue on the project page." }));
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
        onclick: async () => { await call("games_disconnect"); toast("CS2 is disconnected"); refresh(true); } }) : null),
    leagueCard(), dotaCard(), rocketLeagueCard(), racingCard());
}

/* Setup → Games: League of Legends. Nothing to install: the game answers Riot's own live data API on this PC (lol.py). */
function leagueCard() {
  const l = ((S.data.games || {}).lol) || {};
  const status = !l.on ? "Off"
    : l.inMatch ? `In a match now · ${l.team === "ORDER" ? "blue side" : l.team === "CHAOS" ? "red side" : "playing along"}${l.team ? ` · ${l.health}% health` : ""}`
    : "On · starts when a match begins";
  return h("div", { class: "league-card", style: "margin-top:22px" },
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (l.on ? " is-on" : "") }), h("strong", { text: "League of Legends" }), h("span", { class: "muted", text: " · " + status })),
    h("p", { class: "hint", style: "margin-bottom:10px", text: "While you play, your room plays along: your side's color, your health across the lights, grey while you wait to respawn, a flash for your kills, your team's dragons, the baron and the herald, and gold when you win." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px", text: "Uses Riot's own Live Client Data API: while you play, the game tells Glowstage on this PC your health and what happens in the match. Nothing reads or changes the game." }),
    h("label", { class: "toggle-row" }, h("span", { text: "Play along with League of Legends" }),
      h("input", { type: "checkbox", checked: !!l.on, "aria-label": "Play along with League of Legends",
        onchange: async e => {
          const on = e.target.checked;
          if (await call("games_lol", on) !== undefined) toast(on ? "On: start a match and play" : "League of Legends is off");
          refresh(true);
        } })));
}

/* Setup → Games: Dota 2. Valve's game state integration, like CS2: one small file in Dota's cfg folder, plus a Steam launch option the person adds. */
function dotaCard() {
  const d = ((S.data.games || {}).dota) || {};
  const status = !d.found ? "Not found on this PC"
    : !d.connected ? "Found on this PC"
    : d.inMatch ? `Playing along · ${d.team === "radiant" ? "Radiant" : d.team === "dire" ? "Dire" : "in a match"}${d.team ? ` · ${d.health}% health` : ""}`
    : d.receiving ? "Connected · waiting for a match"
    : "Connected · starts when you open Dota 2";
  return h("div", { class: "league-card", style: "margin-top:22px" },
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (d.connected ? " is-on" : "") }), h("strong", { text: "Dota 2" }), h("span", { class: "muted", text: " · " + status })),
    h("p", { class: "hint", style: "margin-bottom:10px", text: "While you play, your room plays along: your side's color, the day and night of the map, your health across the lights, grey while you wait to respawn, a flash for your kills and for Roshan, and gold when you win." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px", text: "Uses Valve's own game state integration, like for CS2: the game sends what happens, nothing reads or changes the game. Connecting puts one small file in Dota 2's cfg folder." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px", text: "Dota 2 only sends once you add one launch option: in Steam, right-click Dota 2, choose Properties, and under Launch options type -gamestateintegration. Glowstage doesn't change Steam for you. Restart Dota 2 after that." }),
    h("div", { class: "form-actions", style: "margin:0" },
      !d.connected ? h("button", { class: "btn btn-primary", type: "button", text: "Connect", disabled: !d.found,
        onclick: async () => { if (await call("games_dota_connect") !== undefined) toast("Connected: add the launch option, then start Dota 2"); refresh(true); } }) : null,
      d.connected ? h("button", { class: "btn btn-quiet", type: "button", text: "Disconnect",
        onclick: async () => { await call("games_dota_disconnect"); toast("Dota 2 is disconnected"); refresh(true); } }) : null));
}

/* Setup → Games: Rocket League. Psyonix's Stats API, switched on in one line of the game's own ini file (rocket_league.py); the game then sends what happens to this PC. */
function rocketLeagueCard() {
  const r = ((S.data.games || {}).rl) || {};
  const status = !r.found ? "Not found on this PC"
    : !r.connected ? "Found on this PC"
    : r.inMatch ? `Playing along · ${r.team === "blue" ? "blue team" : r.team === "orange" ? "orange team" : "in a match"}${r.team ? ` · ${r.boost}% boost` : ""}`
    : r.receiving ? "Connected · waiting for the match"
    : "Connected · starts when a match begins";
  return h("div", { class: "league-card", style: "margin-top:22px" },
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (r.connected ? " is-on" : "") }), h("strong", { text: "Rocket League" }), h("span", { class: "muted", text: " · " + status })),
    h("p", { class: "hint", style: "margin-bottom:10px", text: "While you play, your room plays along: your team's color, your boost as a bar across the lights, a ripple while you're supersonic, a sweep when your team scores, a flash for your saves and demolitions, grey while your car is demolished, a slow pulse in overtime, and your color when you win." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px", text: r.connected
      ? "Restart Rocket League if it's open, so it reads its settings file again. The lights follow the car your camera follows: when you watch someone else, they play along with that player."
      : "Uses Psyonix's own Stats API, the one stream overlays use: the game tells this PC what happens, nothing reads the game's memory. Connecting changes one line in one file of the game (PacketSendRate in TAGame/Config/DefaultStatsAPI.ini), and disconnecting puts it back." }),
    h("div", { class: "form-actions", style: "margin:0" },
      !r.connected ? h("button", { class: "btn btn-primary", type: "button", text: "Connect", disabled: !r.found,
        onclick: async () => { if (await call("games_rl_connect") !== undefined) toast("Connected: restart Rocket League if it's open, then play"); refresh(true); } }) : null,
      r.connected ? h("button", { class: "btn btn-quiet", type: "button", text: "Disconnect",
        onclick: async () => { await call("games_rl_disconnect"); toast("Rocket League is disconnected: restart it if it's open"); refresh(true); } }) : null));
}

/* Setup → Games: racing games (racing.py, simracing.py). Forza and the F1 games send their telemetry as UDP to an address typed into the game's own settings; this listens on this PC only. iRacing and Assetto Corsa publish theirs in shared memory, read-only, with nothing to set up. Both only while the switch is on. */
function racingCard() {
  const r = ((S.data.games || {}).racing) || {};
  const status = !r.on ? "Off"
    : r.problem ? r.problem
    : r.inRace ? `In a race${r.game ? " · " + r.game : ""}`
    : r.seen ? `${r.seen} is running · no race yet`
    : "Listening · no race yet";
  const apply = async (on, forza, f1) => {
    const r2 = await callRaw("games_racing", on, forza, f1);
    if (!r2 || r2.ok === false) toast((r2 && r2.error) || "Couldn't save that", true);
    else if (on !== !!r.on) toast(on ? "On: start a race" : "Racing games are off");
    S.settingsSig = "";
    refresh(true);
  };
  const forza = h("input", { class: "text-input mono", type: "number", min: 1024, max: 65535, value: r.forzaPort || 5300,
    style: "width:90px", "aria-label": "Forza port", onchange: () => apply(!!r.on, forza.value, f1.value) });
  const f1 = h("input", { class: "text-input mono", type: "number", min: 1024, max: 65535, value: r.f1Port || 20777,
    style: "width:90px", "aria-label": "F1 port", onchange: () => apply(!!r.on, forza.value, f1.value) });
  return h("div", { class: "league-card", style: "margin-top:22px" },
    h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (r.on && !r.problem ? " is-on" : "") }), h("strong", { text: "Racing games" }), h("span", { class: "muted", text: " · " + status })),
    h("p", { class: "hint", style: "margin-bottom:10px", text: "While you race, your lights are the rev lights of a real wheel: the revs fill the stage from left to right in green, yellow and red, and pulse blue at the limiter. In the F1 games the start lights come on, a yellow flag or the safety car shows as an amber band, DRS as a green edge, your fastest lap as a purple sweep and the chequered flag as a slow black and white checker." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:10px", text: "Works with Forza, the F1 games, iRacing, Assetto Corsa and Assetto Corsa Competizione. Uses the telemetry these games share on purpose: the game tells this PC how the car is doing. Nothing reads or changes the game, and Glowstage only listens while this is on." }),
    h("label", { class: "toggle-row" }, h("span", { text: "Play along with racing games" }),
      h("input", { type: "checkbox", checked: !!r.on, "aria-label": "Play along with racing games",
        onchange: e => apply(e.target.checked, forza.value, f1.value) })),
    h("div", { class: "toggle-row", style: "cursor:default" }, h("span", { text: "Forza port" }), forza),
    h("div", { class: "toggle-row", style: "cursor:default" }, h("span", { text: "F1 port" }), f1),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:6px", text: "Forza: in Settings, open HUD and Gameplay (Gameplay and HUD in Motorsport 7), scroll down to Data out and set it to On, the IP address to 127.0.0.1 and the port to the number above. Forza Motorsport 7, Motorsport and Horizon 4 and 5 work." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:6px", text: "F1 23 and F1 24: in Settings, open Telemetry settings and set UDP telemetry to On, the IP address to 127.0.0.1, the port to the number above (the game's own is 20777) and the UDP format to 2023 or 2024. F1 25 works too, with its format set to 2025." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:6px", text: "iRacing: nothing to set up. Start the sim and drive, and the lights follow the shift lights of your car (a yellow flag shows as the amber band and the chequered flag as the checker)." }),
    h("p", { class: "hint", style: "font-size:12px;margin-bottom:6px", text: "Assetto Corsa and Assetto Corsa Competizione: nothing to set up either. Start a session and drive: the revs follow your car's limit, and a yellow or chequered flag shows as in the other games." }),
    h("p", { class: "hint", style: "font-size:12px", text: "The F1 and Forza games send only while you drive. If the status says a port is busy, another program is using it: pick another number here and in the game." }));
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
    S.data.autoPaused
      ? h("div", { class: "toggle-row" }, h("span", { text: `All of these are paused until ${pausedUntil(S.data.autoPaused)}` }),
          h("button", { class: "btn", type: "button", text: "Resume", onclick: resumeAutomatic }))
      : h("div", { class: "toggle-row" }, h("span", { text: "Pause all of these for a while" }),
          h("div", { class: "form-actions", style: "margin:0" },
            h("button", { class: "btn", type: "button", text: "For an hour", onclick: () => pauseAutomatic(60) }),
            h("button", { class: "btn", type: "button", text: "Until tomorrow", onclick: () => pauseAutomatic("tomorrow") }))),
    pick("auto_game", "When a game is full screen", looks, s.auto_game || ""),
    pick("auto_video", "When a video is full screen (a browser or video player)", looks, s.auto_video || ""),
    pick("auto_call", "When your camera or microphone is on (a call)",
      [["", "Nothing"], ...(S.data.presets || []).map(p => ["preset:" + p.name, p.name])], s.auto_call || ""),
    h("p", { class: "hint", text: "Glowstage only looks whether they're in use, never at what they pick up. A game in full screen goes first, so voice chat while you play keeps your game's lighting." }),
    obsLooks(pick),
    pick("auto_show", "When music plays", [["", "Nothing"], ["show", "Start the light show"]], s.auto_show ? "show" : ""),
    pick("away_lights", "When you step away: the PC is locked, its screen goes off or it sleeps",
      [["", "Nothing"], ["pc", "Switch the PC's lights off"], ["all", "Switch all lights off"]], s.away_lights || ""),
    appLooks());
}

/* On air with OBS Studio: a look while you stream or record (obs.py). The password goes to Glowstage and never comes back: only "Saved" shows. */
function obsLooks(pick) {
  const o = S.data.obs || {};
  const s = S.data.settings;
  const apply = async (on, port, when) => {
    const r = await callRaw("obs_set", on, port, when);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save that", true);
    S.settingsSig = "";
    refresh(true);
  };
  const port = h("input", { class: "text-input mono", type: "number", min: 1, max: 65535, value: o.port || 4455,
    style: "width:90px", "aria-label": "OBS WebSocket port", onchange: () => apply(!!o.on, port.value, o.when || "both") });
  const secret = h("input", { class: "text-input", type: "password", autocomplete: "off", placeholder: "OBS WebSocket password",
    style: "width:200px", "aria-label": "OBS WebSocket password" });
  const keep = async () => {
    const r = await callRaw("obs_set_password", secret.value);
    secret.value = "";
    if (!r || r.ok === false) { toast((r && r.error) || "Couldn't save that", true); return; }
    S.obsChange = false;
    S.settingsSig = "";
    refresh(true);
  };
  secret.addEventListener("keydown", e => { if (e.key === "Enter") keep(); });
  const forget = async () => {
    await call("obs_clear_password");
    S.obsChange = false;
    S.settingsSig = "";
    refresh(true);
  };
  const changing = !o.hasPassword || S.obsChange;
  const lines = ["Connected", "Live now"].some(t => (o.text || "").startsWith(t));
  return h("div", { class: "obs-looks" },
    h("h4", { text: "When you stream or record in OBS" }),
    h("label", { class: "toggle-row" }, h("span", { text: "Connect to OBS Studio on this PC" }),
      h("input", { type: "checkbox", checked: !!o.on, "aria-label": "Connect to OBS Studio",
        onchange: e => apply(e.target.checked, o.port || 4455, o.when || "both") })),
    o.on ? h("div", { class: "status-line" }, h("span", { class: "dev-dot" + (lines ? " is-on" : "") }), h("span", { class: "muted", text: o.text || "" })) : null,
    h("div", { class: "toggle-row", style: "cursor:default" }, h("span", { text: "Port of OBS's WebSocket server" }), port),
    h("div", { class: "toggle-row", style: "cursor:default" }, h("span", { text: "Password (if OBS has one)" }),
      changing
        ? h("div", { class: "form-actions", style: "margin:0" }, secret,
            h("button", { class: "btn", type: "button", text: "Save", onclick: keep }),
            o.hasPassword ? h("button", { class: "btn btn-quiet", type: "button", text: "Cancel", onclick: () => { S.obsChange = false; S.settingsSig = ""; refresh(true); } }) : null)
        : h("div", { class: "form-actions", style: "margin:0" }, h("span", { class: "muted", text: "Saved" }),
            h("button", { class: "btn", type: "button", text: "Change", onclick: () => { S.obsChange = true; S.settingsSig = ""; refresh(true); } }),
            h("button", { class: "btn btn-quiet", type: "button", text: "Remove", onclick: forget }))),
    h("label", { class: "toggle-row" }, h("span", { text: "It counts when you" }),
      h("select", { class: "auto-look", "aria-label": "What counts as on air", onchange: e => apply(!!o.on, o.port || 4455, e.target.value) },
        [["both", "Stream or record"], ["stream", "Stream"], ["record", "Record"]].map(([v, t]) => h("option", { value: v, text: t, selected: v === (o.when || "both") })))),
    pick("auto_live", "Your lights while you're on air",
      [["", "Nothing"], ...(S.data.presets || []).map(p => ["preset:" + p.name, p.name])], s.auto_live || ""),
    h("p", { class: "hint", text: "Make a preset that sets just one light, a red one say, and it works as an ON AIR sign: presets only change the lights they hold, the others stay as they are. In OBS, switch the WebSocket server on under Tools, WebSocket Server Settings. Glowstage only talks to OBS on this PC, and the password stays protected by Windows." }));
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
  const actions = [["off", "Lights off"], ["on", "Lights on"],
    ["wake:15", "Wake up slowly (15 min)"], ["wake:30", "Wake up slowly (30 min)"], ["wake:45", "Wake up slowly (45 min)"],
    ["show", "Start the light show"],
    ...(S.data.presets || []).map(p => ["preset:" + p.name, p.name])];
  const sunNow = S.data.sun || {};
  const isSun = s => s.time === "sunrise" || s.time === "sunset";
  // Today's time for a sun schedule, with its offset, kept within the day; "" when the sun doesn't rise or set today.
  const sunToday = s => {
    const at = sunNow[s.time];
    if (!at) return "";
    const m = Math.min(1439, Math.max(0, +at.slice(0, 2) * 60 + +at.slice(3, 5) + (s.offset || 0)));
    return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
  };
  const offsets = [[-120, "2 h before"], [-90, "1½ h before"], [-60, "1 h before"], [-45, "45 min before"],
    [-30, "30 min before"], [-15, "15 min before"], [0, "on time"], [15, "15 min after"], [30, "30 min after"],
    [45, "45 min after"], [60, "1 h after"], [90, "1½ h after"], [120, "2 h after"]];
  // (each choice shows the time it means today: "30 min before · 16:01")
  const offsetText = (s, v, t) => { const at = sunToday({ ...s, offset: v }); return at ? `${t} · ${at}` : t; };
  const offsetsFor = s => offsets.some(o => o[0] === (s.offset || 0)) ? offsets
    : [...offsets, [s.offset, `${Math.abs(s.offset)} min ${s.offset < 0 ? "before" : "after"}`]].sort((a, b) => a[0] - b[0]);
  const row = (s, i) => h("div", { class: "sched-row" + (s.enabled === false ? " is-paused" : "") },
    h("select", { class: "sched-when", "aria-label": "When", onchange: e => {
      const v = e.target.value;
      if (v === "at") { s.time = /^\d\d:\d\d$/.test(s.time) ? s.time : "23:00"; delete s.offset; }
      else { s.time = v; s.offset = s.offset || 0; }
      save();
    } }, [["at", "At"], ["sunrise", "At sunrise"], ["sunset", "At sunset"]].map(([v, t]) =>
      h("option", { value: v, text: t, selected: v === (isSun(s) ? s.time : "at") }))),
    isSun(s)
      ? [h("select", { class: "sched-offset", "aria-label": "How long before or after", onchange: e => { s.offset = +e.target.value; save(); } },
          offsetsFor(s).map(([v, t]) => h("option", { value: String(v), text: offsetText(s, v, t), selected: v === (s.offset || 0) }))),
        sunToday(s) ? null : h("span", { class: "muted sched-hint", text: "not today" })]
      : h("input", { class: "text-input sched-time", type: "time", value: s.time, "aria-label": "Time",
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
  return h("section", { class: "set-section", id: "set-schedule" },
    h("h3", { text: "Schedule" }),
    h("p", { class: "hint", text: "Your lights change by themselves at these times, as long as Glowstage runs (in the tray is enough)." }),
    items.length ? h("div", { class: "sched-list" }, items.map(row)) : null,
    h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", text: items.length ? "Add another time" : "Add a time",
        onclick: () => {
          const late = (S.data.presets || []).find(p => /late night/i.test(p.name));
          items.push({ time: "23:00", days: [0, 1, 2, 3, 4, 5, 6], action: late ? "preset:" + late.name : "off", enabled: true });
          save();
        } })),
    nightPart(),
    awayPart());
}

/* Setup → Schedule → While you're away: the room looks lived in, with lights that come on and go off at a
   slightly different time each day (presence.py). */
function awayPart() {
  const p = S.data.presence || { on: false, lights: "room", groups: [], when: "sunset", at: "17:30", off: "23:30" };
  const fields = { on: p.on, lights: p.lights, groups: [...(p.groups || [])], when: p.when, at: p.at, off: p.off };
  const groups = S.data.groups || [];
  const save = async () => {
    const r = await callRaw("set_presence", fields.on, fields.lights, fields.groups, fields.when, fields.at, fields.off);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save that", true);
    S.settingsSig = "";
    refresh(true);
  };
  const sunset = (S.data.sun || {}).sunset;
  return h("div", { class: "night-part away-part" },
    h("h4", { text: "While you're away" }),
    h("label", { class: "toggle-row" }, h("span", { text: "Make it look like someone's home" }),
      h("input", { type: "checkbox", checked: !!p.on, onchange: e => { fields.on = e.target.checked; save(); } })),
    h("div", { class: "night-row" + (p.on ? "" : " is-off") },
      h("span", { text: "Lights" }),
      h("select", { class: "night-max", "aria-label": "Which lights", onchange: e => { fields.lights = e.target.value; save(); } },
        [["room", "The room's lights"], ["groups", "Some of my groups"]].map(([v, t]) =>
          h("option", { value: v, text: t, selected: v === p.lights })))),
    p.lights === "groups"
      ? (groups.length
        ? h("div", { class: "night-row", role: "group", "aria-label": "Groups that play along" }, groups.map(g =>
          h("label", { class: "check-row" },
            h("input", { type: "checkbox", checked: fields.groups.includes(g.name), onchange: e => {
              fields.groups = e.target.checked ? [...fields.groups, g.name] : fields.groups.filter(n => n !== g.name); save(); } }),
            h("span", { text: g.name }))))
        : h("p", { class: "hint", text: "You have no groups yet: make one under Lights." }))
      : null,
    h("div", { class: "night-row" + (p.on ? "" : " is-off") },
      h("span", { text: "Come on" }),
      h("select", { class: "night-max", "aria-label": "When the lights come on", onchange: e => { fields.when = e.target.value; save(); } },
        [["sunset", sunset ? `Around sunset · ${sunset} today` : "Around sunset"], ["time", "Around a time"]].map(([v, t]) =>
          h("option", { value: v, text: t, selected: v === p.when }))),
      p.when === "time"
        ? h("input", { type: "time", class: "text-input night-time", value: fields.at, "aria-label": "Time the lights come on",
          onchange: e => { if (e.target.value) { fields.at = e.target.value; save(); } } })
        : null,
      h("span", { text: "and go off around" }),
      h("input", { type: "time", class: "text-input night-time", value: fields.off, "aria-label": "Time the lights go off",
        onchange: e => { if (e.target.value) { fields.off = e.target.value; save(); } } })),
    h("p", { class: "hint", text: "Each day the times move by up to 25 minutes, and now and then one light goes off while another comes on, in a plain warm white. Your PC's own lights stay as they are. While this is on, schedules and automatic looks leave your lights alone, and when you switch it off your lights go back to how they were." }),
    h("p", { class: "hint", text: "This only works while this PC is on and Glowstage runs." }));
}

/* Setup → Schedule → Night limit: from the evening to the morning no light goes above a brightness (night.py). */
function nightPart() {
  const n = S.data.night || { on: false, from: "23:00", to: "07:00", max: 30, choices: [5, 10, 15, 20, 30, 40, 50, 60, 75] };
  const fields = { on: n.on, from: n.from, to: n.to, max: n.max };
  const save = async () => {
    const r = await callRaw("set_night_limit", fields.on, fields.from, fields.to, fields.max);
    if (!r || r.ok === false) toast((r && r.error) || "Couldn't save the night limit", true);
    S.settingsSig = "";
    refresh(true);
  };
  const time = key => h("input", { type: "time", class: "text-input night-time", value: fields[key], "aria-label": key === "from" ? "From" : "Until",
    onchange: e => { if (e.target.value) { fields[key] = e.target.value; save(); } } });
  return h("div", { class: "night-part" },
    h("h4", { text: "Night limit" }),
    h("label", { class: "toggle-row" }, h("span", { text: "Keep my lights dim at night" }),
      h("input", { type: "checkbox", checked: !!n.on, onchange: e => { fields.on = e.target.checked; save(); } })),
    h("div", { class: "night-row" + (n.on ? "" : " is-off") },
      h("span", { text: "From" }), time("from"), h("span", { text: "until" }), time("to"),
      h("span", { text: "no light above" }),
      h("select", { class: "night-max", "aria-label": "Highest brightness at night", onchange: e => { fields.max = +e.target.value; save(); } },
        (n.choices || []).map(v => h("option", { value: v, text: `${v}%`, selected: v === n.max })))),
    h("p", { class: "hint", text: "Whatever switches a light on, a preset, a game, the light show or your phone, it stays at or below this. The brightness you set isn't changed, and a wake-up light still comes up to full." }));
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
    h("p", { class: "hint", text: "Switch your lights, set the brightness, pick a preset, start the light show or the sleep timer, or light the room in the colors of a photo you take, from your phone on the same Wi-Fi. Only a phone with this code's link can: it carries a secret key." }),
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
              onclick: async () => { await call("remote_new_link"); toast("New link: a phone with the old one has to scan again"); S.settingsSig = ""; refresh(true); } })))),
    linksPart(r));
}

/* "Use from other apps": plain links (remote.py's /do/...) for Stream Deck, AutoHotkey, Home Assistant. */
let linkPick = "on";                                   // (kept while the page redraws)
function linksPart(r) {
  const part = h("div", { class: "phone-links" }, h("h4", { text: "Use from other apps" }));
  if (!r.on || !r.api) {
    part.append(h("p", { class: "hint", text: "Switch the phone remote on to use these links." }));
    return part;
  }
  const actions = [["on", "All on"], ["off", "All off"], ["toggle", "Switch all on or off"],
    ...(S.data.presets || []).map(p => ["preset/" + encodeURIComponent(p.name), `Preset: ${p.name}`]),
    ...[100, 75, 50, 25, 10].map(v => ["brightness/" + v, `Brightness ${v} %`]),
    ["brighter", "Brighter"], ["darker", "Darker"],
    ...[["ffb46b", "Warm white"], ["f2f5ff", "Cool white"], ["ff1a1a", "Red"], ["00e05a", "Green"], ["1f4bff", "Blue"]]
      .map(([hex, name]) => ["color/" + hex, `Color: ${name} (swap the code for any color)`]),
    ...[["rainbow", "Rainbow wave"], ["fire", "Fire"], ["candle", "Candle"], ["aurora", "Aurora"], ["ocean", "Ocean"],
      ["screen", "Screen sync"]].map(([id, name]) => ["effect/" + id, `Look: ${name}`]),
    ...(S.data.groups || []).flatMap(g => [[`group/${encodeURIComponent(g.name)}/on`, `Group ${g.name}: on`],
      [`group/${encodeURIComponent(g.name)}/off`, `Group ${g.name}: off`],
      [`group/${encodeURIComponent(g.name)}/toggle`, `Group ${g.name}: switch on or off`],
      [`group/${encodeURIComponent(g.name)}/brightness/50`, `Group ${g.name}: brightness 50 % (0 to 100)`]]),
    ["show/start", "Start the light show"], ["show/stop", "Stop the light show"],
    ["sleep/30", "Sleep timer 30 min"], ["sleep/cancel", "Cancel the sleep timer"],
    ["pause/60", "Pause automatic looks for an hour"], ["pause/tomorrow", "Pause automatic looks until tomorrow"],
    ["resume", "Resume automatic looks"], ["undo", "Undo"]];
  if (!actions.some(([v]) => v === linkPick)) linkPick = "on";
  const linkFor = v => `${r.api}/${v}?k=${r.key}`;
  const field = h("input", { class: "phone-link", type: "text", readonly: true, id: "phone-link", value: linkFor(linkPick) });
  const pick = h("select", { id: "phone-action", onchange: e => { linkPick = e.target.value; field.value = linkFor(linkPick); } },
    actions.map(([v, t]) => h("option", { value: v, text: t, selected: v === linkPick })));
  const copy = async () => {
    try { await navigator.clipboard.writeText(field.value); toast("Link copied"); }
    catch { field.select(); toast("Couldn't copy it: it's selected, press Ctrl+C", true); }
  };
  part.append(
    h("p", { class: "hint", text: "Stream Deck (Website or API request), AutoHotkey, Home Assistant and other apps can switch your lights with a link." }),
    h("label", { class: "toggle-row", for: "phone-action" }, h("span", { text: "What the link does" }), pick),
    h("label", { class: "sr-only", for: "phone-link", text: "Link for this action" }),
    field,
    h("div", { class: "form-actions" }, h("button", { class: "btn", type: "button", text: "Copy link", onclick: copy })),
    h("p", { class: "hint", text: "Anyone with a link can switch your lights on this network. New link above shuts out every old link." }));
  return part;
}

/* Backups: a copy for each day, to go back to; a file of it all, to take to another PC. */
const savedWhen = iso => iso ? new Date(iso).toLocaleString("en-GB",
  { weekday: "short", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "an unknown day";
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const holds = b => `${plural(b.presets, "preset")} · ${plural(b.lights, "light")}`;
const counted = b => `${plural(b.presets, "preset")}, ${plural(b.lights, "light")}`;

function backupSection() {
  return h("section", { class: "set-section", id: "set-backups" },
    h("h3", { text: "Backups" }),
    h("p", { class: "hint", text: "Each day you use Glowstage, it keeps a copy of your presets, lights and settings: the last 14 days. Go back to one, or save them all to a file to take to another PC." }),
    h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", text: "Go back to a copy…", onclick: openBackups }),
      h("button", { class: "btn", type: "button", text: "Save to a file…", onclick: async () => {
        const name = await call("export_settings");
        if (name) toast(`Saved as ${name}`);
      } }),
      h("button", { class: "btn", type: "button", text: "Load from a file…", onclick: async () => {
        const b = await call("pick_settings_file");
        if (b) confirmLoad(`Load ${b.file}?`, `It holds ${counted(b)} and settings, saved ${savedWhen(b.saved)}.`,
          "Load and restart", () => callRaw("load_settings_file"));
      } })));
}

async function openBackups() {
  const list = await call("list_backups");
  if (list === undefined) return;
  const good = list.filter(b => b.ok);
  openModal({
    title: "Go back to a copy",
    cancel: false,
    body: good.length
      ? h("div", {},
          h("p", { class: "hint", text: "Your presets, lights and settings as they were then. What you have now is kept as a copy too." }),
          h("div", { class: "app-pick-list" }, ...good.map(b => h("button", {
            class: "app-pick backup-pick", type: "button",
            onclick: () => confirmLoad(`Go back to ${savedWhen(b.saved)}?`,
              `Your ${counted(b)} and your settings, as they were then.`, "Go back and restart",
              () => callRaw("restore_backup", b.file)) },
            h("span", { text: savedWhen(b.saved) + (b.before ? " (what you had before a load)" : "") }),
            h("span", { class: "muted", text: holds(b) })))))
      : h("p", { class: "hint", text: "No copies yet: the first one is made tomorrow, the first time something changes." }),
    ok: "Close",
    onOk: () => true,
  });
}

function confirmLoad(title, text, ok, run) {
  openModal({
    title,
    body: h("p", { class: "hint", style: "font-size:14px",
      text: `${text} Glowstage restarts with them. What you have now is kept as a copy, under Go back to a copy.` }),
    ok,
    onOk: async () => {
      const r = await run();
      if (!r.ok) return r.error;
      toast("Restarting Glowstage with it…");
      return true;
    },
  });
}

function appSection() {
  const s = S.data.settings;
  const row = (key, label) => h("label", { class: "toggle-row" }, h("span", { text: label }),
    h("input", { type: "checkbox", checked: !!s[key],
      onchange: async e => { await call("set_setting", key, e.target.checked); refresh(true); } }));
  const about = S.data.about || {};
  return h("section", { class: "set-section", id: "set-app" },
    h("h3", { text: "App" }),
    row("close_to_tray", "Keep running in the tray when I close the window"),
    row("autostart", "Start with Windows"),
    row("start_minimized", "Start hidden in the tray"),
    row("restore_on_launch", "Restore my lights when the app starts"),
    row("lights_on_at_startup", "Switch all lights on when Windows starts"),
    row("pc_lights_off_at_shutdown", "Switch the PC's lights off when Windows shuts down"),
    row("room_lights_off_at_shutdown", "Switch the room lights off when Windows shuts down"),
    row("show_strobe", "Strobe flashes in the light show"),
    row("soft_flashes", "Soften flashes: no look flashes, every light brightens and dims softly"),
    s.soft_flashes ? h("p", { class: "hint", text: "For people who are sensitive to flashing light. Lightning, kills in games and the beats of the light show swell softly instead, and the light show leaves out its strobes." }) : null,
    row("check_updates", "Check once a day for a new version (asks GitHub; sends nothing about you)"),
    row("hotkeys", "Hotkeys from any program: your lights, presets and timers from a key, also in a game"),
    h("p", { class: "hint", text: keySummary() }),
    s.hotkeys && (S.data.hotkeysTaken || []).length
      ? h("p", { class: "hint", text: `Another program already uses ${S.data.hotkeysTaken.map(t => `${t.key} (${t.label})`).join(", ")}: that doesn't work here. Choose another key under Change keys.` }) : null,
    h("div", { class: "form-actions" },
      h("button", { class: "btn", type: "button", text: "Change keys", onclick: openHotkeys })),
    h("div", { class: "form-actions", style: "margin-top:14px" },
      h("button", { class: "btn", type: "button", text: "Show the welcome tour", onclick: () => { closeSettings(); openWizard(); } }),
      h("button", { class: "btn", type: "button", text: "Restart Glowstage",
        onclick: async () => { toast("Restarting Glowstage…"); await call("restart_app"); } }),
      h("button", { class: "btn", type: "button", text: "Report a problem", onclick: openReport }),
      h("button", { class: "btn", type: "button", text: "Keyboard shortcuts", onclick: openKeys })),
    h("p", { class: "hint", style: "margin-top:16px", text: `${about.name || "Glowstage"} ${about.version || ""} · free and open source (GPL-3.0)` }),
    h("div", { class: "form-actions" },
      ...(about.donate || []).map(d => h("button", { class: "btn btn-support", type: "button",
        text: `♥ Support on ${d.name}`, onclick: () => call("open_link", d.url) })),
      about.repo ? h("button", { class: "btn", type: "button", text: "Project page", onclick: () => call("open_link", about.repo) }) : null,
      about.repo ? h("button", { class: "btn btn-quiet", type: "button", text: "Check for updates",
        onclick: () => call("open_link", about.repo + "/releases") }) : null));
}

/* "Change keys": every hotkey action with its key (hotkeys.py keeps and checks them). Click a key, or
   press Enter on it, then press the new combination: it shows while you press, Escape cancels,
   Backspace or Delete clears it. Glowstage's own hotkeys are paused while this window is open: they
   would take the keys before the window sees them. */
const HK_CODES = { ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", Home: "Home", End: "End",
  PageUp: "PageUp", PageDown: "PageDown", Insert: "Insert", Delete: "Delete", Space: "Space",
  NumpadMultiply: "Num*", NumpadAdd: "Num+", NumpadSubtract: "Num-", NumpadDecimal: "Num.", NumpadDivide: "Num/" };

/** The name hotkeys.py knows for a physical key (by its code, so Shift+1 is still the 1 key), or "". */
function hotkeyName(code) {
  let m = /^Key([A-Z])$/.exec(code) || /^Digit(\d)$/.exec(code);
  if (m) return m[1];
  m = /^F(\d{1,2})$/.exec(code);
  if (m) return +m[1] >= 1 && +m[1] <= 24 ? code : "";
  m = /^Numpad(\d)$/.exec(code);
  return m ? "Num" + m[1] : (HK_CODES[code] || "");
}

async function openHotkeys() {
  let st = await call("get_hotkeys");
  if (!st) return;
  const msgs = {};              // what went wrong with an action's key, in words
  let capturing = null;         // the key button that is waiting for a combination
  let focusAction = null;       // the key to put the focus back on after a redraw
  const wrap = h("div", { class: "hk" });
  const change = async (fn, ...args) => {
    const r = await callRaw(fn, ...args);
    if (r && r.ok !== false) { st = r.result; return ""; }
    return (r && r.error) || "That didn't work";
  };
  const modsOf = e => [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean);

  const keyButton = a => {
    const btn = h("button", { class: "hk-key" + (a.key ? "" : " is-empty"), type: "button", "data-action": a.id,
      "aria-label": `${a.label}: ${a.key || "no key"}. Press Enter to change it.`, text: a.key || "No key" });
    const stop = () => { capturing = null; btn.classList.remove("is-capturing"); btn.textContent = a.key || "No key"; };
    const start = () => {
      if (capturing === btn) return;
      capturing = btn;
      delete msgs[a.id];
      btn.classList.add("is-capturing");
      btn.textContent = "Press the new keys…";
    };
    const redraw = () => { focusAction = a.id; draw(); };
    btn.addEventListener("click", e => { if (e.detail > 0) start(); });      // (the mouse; the keyboard starts below)
    btn.addEventListener("blur", () => { if (capturing === btn) stop(); });
    btn.addEventListener("keyup", e => {
      if (capturing !== btn) return;
      e.preventDefault(); e.stopPropagation();
      const mods = modsOf(e);
      btn.textContent = mods.length ? mods.join("+") + "+…" : "Press the new keys…";
    });
    btn.addEventListener("keydown", async e => {
      if (capturing !== btn) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); start(); }
        return;
      }
      const mods = modsOf(e);
      if (e.key === "Tab" && !mods.length) { stop(); return; }              // (on to the next field)
      e.preventDefault(); e.stopPropagation();                              // (the keys are for the combination)
      if (e.key === "Escape") { stop(); return; }
      if (!mods.length && (e.key === "Backspace" || e.key === "Delete")) {
        stop();
        const err = await change("clear_hotkey", a.id);
        if (err) msgs[a.id] = err;
        redraw();
        return;
      }
      if (/^(Control|Alt|Shift|Meta|OS)$/.test(e.key)) { btn.textContent = mods.join("+") + "+…"; return; }
      const key = hotkeyName(e.code);
      if (!key) { btn.textContent = [...mods, "…"].join("+") + " (that key can't be used)"; return; }
      const combo = [...mods, key].join("+");
      btn.textContent = combo;
      stop();
      const err = await change("set_hotkey", a.id, combo);
      if (err) msgs[a.id] = err;
      redraw();
    });
    return btn;
  };

  const row = a => h("div", { class: "hk-row" },
    h("div", { class: "hk-what" }, h("span", { text: a.label }), a.detail ? h("span", { class: "hk-detail", text: a.detail }) : null),
    h("div", { class: "hk-keycell" },
      keyButton(a),
      a.key ? h("button", { class: "btn btn-quiet btn-sm hk-clear", type: "button", text: "Clear", "aria-label": `Clear the key for ${a.label}`,
        onclick: async () => { const err = await change("clear_hotkey", a.id); if (err) msgs[a.id] = err; focusAction = a.id; draw(); } }) : h("span", { class: "hk-gap" }),
      a.section === "named" ? h("button", { class: "btn btn-quiet btn-sm", type: "button", text: "Remove", "aria-label": `Remove the row for ${a.label}`,
        onclick: async () => { const err = await change("remove_preset_hotkey", a.label); if (err) toast(err, true); draw(); } }) : null),
    msgs[a.id] || (a.taken && a.key)
      ? h("p", { class: "hk-msg", role: msgs[a.id] ? "alert" : null,
          text: msgs[a.id] || "Another program already has this key, so it doesn't work here. Choose another." }) : null);

  const section = (title, items, extra) => h("div", {}, h("h3", { class: "keys-head", text: title }), items.map(row), extra);

  function draw() {
    const of = name => st.actions.filter(a => a.section === name);
    const free = st.presets.filter(n => !st.actions.some(a => a.id === "preset:" + n));
    fill(wrap,
      h("p", { class: "hint", text: "Click a key, or press Enter on it, then press the new combination. Escape cancels; Backspace or Delete clears it." }),
      h("p", { class: "hint", text: "Use Ctrl, Alt, Shift or Win with a letter, a digit, F1 to F24, an arrow, Home, End, Page Up, Page Down, Insert, Delete, Space or a numpad key. F13 to F24 need no modifier: macro keys on a keyboard or mouse often send them. Glowstage's own hotkeys are paused while this window is open." }),
      !st.enabled ? h("p", { class: "hint", text: "The hotkeys are off now: switch them on in Setup → App." }) : null,
      section("Lights and timers", of("actions")),
      section("Presets by place in the list", of("positions")),
      section("Presets by name", of("named"), h("div", { class: "hk-add" },
        free.length ? h("select", { class: "text-input", "aria-label": "Add a key for a preset",
          onchange: async e => {
            const name = e.target.value;
            if (!name) return;
            const err = await change("add_preset_hotkey", name);
            if (err) toast(err, true); else focusAction = "preset:" + name;
            draw();
          } },
          h("option", { value: "", text: "Add a key for a preset…" }), free.map(n => h("option", { value: n, text: n }))) : null,
        h("p", { class: "hint", text: "A preset keeps its key when you rename it, and loses it when you delete it." }))),
      h("div", { class: "form-actions", style: "margin-top:16px" },
        h("button", { class: "btn", type: "button", text: "Reset all", onclick: async () => {
          Object.keys(msgs).forEach(k => delete msgs[k]);
          const err = await change("reset_hotkeys");
          if (err) toast(err, true);
          draw();
        } }),
        h("span", { class: "hint", text: "Puts every key back as it came, and removes the keys for presets by name." })));
    if (focusAction) {
      const btn = [...wrap.querySelectorAll(".hk-key")].find(b => b.dataset.action === focusAction);
      focusAction = null;
      if (btn) btn.focus();
    }
  }
  draw();

  const paused = await callRaw("pause_hotkeys");
  if (!paused || paused.ok === false) { toast((paused && paused.error) || "Couldn't pause the hotkeys", true); return; }
  const beat = setInterval(() => callRaw("pause_hotkeys"), 30000);    // (they come back by themselves if this window vanishes)
  const finish = async () => { clearInterval(beat); await callRaw("resume_hotkeys"); refresh(true); };
  try {
    openModal({ title: "Change keys", wide: true, cancel: false, body: wrap, ok: "Done", onOk: () => true, onClose: finish });
  } catch (e) {
    finish();
    throw e;
  }
  setTimeout(() => { const first = wrap.querySelector(".hk-key"); if (first) first.focus(); }, 40);
}

/* Report a problem: what goes with it, to read first, then a GitHub issue with it filled in. */
async function openReport() {
  const text = await call("diagnostics");
  if (text === undefined) return;
  const box = h("textarea", { class: "text-input report-text", readonly: true, rows: "12", "aria-label": "What goes with the report" });
  box.value = text;
  openModal({
    title: "Report a problem",
    wide: true,
    body: h("div", {},
      h("p", { class: "hint", text: "This goes with your report: your Glowstage version, Windows, your lights and whether they answer, and the last warnings from the log. No keys or passwords. Read it first; on GitHub you add what happened before you send it." }),
      box,
      h("div", { class: "form-actions", style: "margin-top:10px" },
        h("button", { class: "btn", type: "button", text: "Copy", onclick: async () => {
          try { await navigator.clipboard.writeText(text); toast("Copied: paste it wherever you ask for help"); }
          catch { box.select(); toast("Couldn't copy it: it's selected, press Ctrl+C", true); }
        } }))),
    ok: "Open a GitHub issue",
    onOk: async () => { await call("report_problem"); toast("Your browser opens GitHub with the report filled in"); return true; },
  });
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
        const sel = h("select", { "aria-label": "Channel layout" }, layouts.map(l => h("option", { value: l, text: l, selected: (f.layout || "RGB") === l })));
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
  S.wiz.from = document.activeElement;       // (it gets the focus back when the tour closes)
  $("#wizard").hidden = false;
  call("search");
  renderWizard(true);
  $("#wizard-next").focus();
}

async function finishWizard() {
  S.wiz.open = false;
  $("#wizard").hidden = true;
  const back = S.wiz.from;
  S.wiz.from = null;
  if (back && back.isConnected && back !== document.body && typeof back.focus === "function") back.focus();
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
