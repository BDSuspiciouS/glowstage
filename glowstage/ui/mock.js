"use strict";
/* Sample backend so the UI can be previewed in a plain browser. Not used by the app. */
(() => {
  const OFFLINE = /[?&]offline\b/.test(location.search);   // ?offline: two lights stop answering
  const st = (o = {}) => ({ on: true, brightness: 100, mode: "color", color: "#7c4dff", color2: "#00e5ff",
    effect: "rainbow", speed: 50, native: null, palette: null, ...o });
  const effects = [["rainbow", "Rainbow wave", ["speed"]], ["spectrum", "Spectrum cycle", ["speed"]],
    ["breathing", "Breathing", ["speed", "color", "color2"]], ["wave", "Color wave", ["speed", "color", "color2"]],
    ["comet", "Comet", ["speed", "color", "color2"]], ["twinkle", "Starlight", ["speed", "color", "color2"]],
    ["fire", "Fire", ["speed"]], ["aurora", "Aurora", ["speed"]], ["typing", "Typing ripples", ["speed", "color", "color2"]], ["daylight", "Follow the sun", []], ["screen", "Screen sync", []], ["screen_sound", "Screen + sound", []],
    ["music_show", "Light show", ["speed"], "music"],
    ["music_party", "Party", ["speed"], "music"], ["music_ripple", "Bass ripple", ["speed"], "music"],
    ["music_spectrum", "Spectrum", ["speed"], "music"], ["music_pulse", "Beat pulse", ["speed", "color", "color2"], "music"],
    ["music_vu", "VU meter", ["speed", "color", "color2"], "music"], ["music_romantic", "Romantic", ["speed", "color", "color2"], "music"],
    ["music_organ", "Color organ", ["speed"], "music"], ["music_bloom", "Center bloom", ["speed", "color", "color2"], "music"],
    ["music_sparkle", "Sparkle", ["speed"], "music"], ["music_meteor", "Meteor", ["speed"], "music"],
    ["music_fire", "Music fire", ["speed"], "music"], ["music_aurora", "Northern lights", ["speed"], "music"],
    ["music_heart", "Heartbeat", ["speed", "color", "color2"], "music"]]
    .map(([id, name, uses, group]) => ({ id, name, uses, group: group || "animation",
      flows: ["rainbow", "spectrum", "breathing", "wave", "comet", "aurora"].includes(id) }));
  const zones = [{ index: 0, name: "Pump", leds: 29, min: 29, max: 29, resizable: false, detected: -1 },
    ...[1, 2, 3, 4, 5, 6].map(i => ({ index: i, name: `Corsair Fan Header ${i}`, leds: i <= 3 ? 8 : 0, min: 0, max: 34,
      resizable: true, detected: i <= 3 ? 8 : null }))];
  const data = {
    revision: 1,
    showExcluded: [],
    appLooks: [{ exe: "photoshop.exe", name: "Adobe Photoshop 2025", action: "preset:Focus" }],
    schedules: [{ time: "23:00", days: [0, 1, 2, 3, 4, 5, 6], action: "preset:Late night", enabled: true },
      { time: "sunset", offset: -30, days: [0, 1, 2, 3, 4, 5, 6], action: "preset:Focus", enabled: true },
      { time: "01:00", days: [0, 1, 2, 3, 6], action: "off", enabled: true }],
    hotkeysTaken: [],
    update: /[?&]update\b/.test(location.search)
      ? { version: "0.9.8", url: "https://github.com/BDSuspiciouS/glowstage/releases/tag/v0.9.8" } : null,
    askSupport: /[?&]support\b/.test(location.search),
    offlineAge: OFFLINE ? 120 : 0,   // with ?offline the lights count as already gone for two minutes
    activePreset: "",
    devices: [
      { id: "openrgb:Corsair Commander Core", kind: "openrgb", group: "pc", name: "Commander Core", online: true, error: null,
        pixels: 131, hasNative: false, label: "Cooler · 131 LEDs", zones, state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "openrgb:QCK", kind: "openrgb", group: "pc", name: "QCK Prism XL", online: true, error: null, pixels: 2,
        hasNative: false, label: "Mousemat · 2 LEDs", zones: [], state: st({ color: "#ff0090" }), hidden: false },
      { id: "asrock_gpu:0", kind: "asrock_gpu", group: "pc", name: "Graphics card", online: true, error: null, pixels: 12,
        hasNative: false, label: "ASRock · 12 LEDs", state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "qmk:gx87", kind: "qmk", group: "pc", name: "Keyboard", online: true, error: null, pixels: 87,
        hasNative: false, label: "GX87 · 87 keys", state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "wled:1", kind: "wled", group: "room", name: "Desk strip", online: true, error: null, pixels: 297, hasNative: true,
        label: "WLED 0.15.3", host: "192.168.1.26", state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "wled:2", kind: "wled", group: "room", name: "Monitor", online: !OFFLINE, error: OFFLINE ? "Not responding" : null, pixels: 99, hasNative: true,
        label: "WLED 0.15.3", host: "192.168.1.27", state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "nanoleaf:1", kind: "nanoleaf", group: "room", name: "Shapes", online: !OFFLINE, error: OFFLINE ? "Unreachable" : null, pixels: 9,
        hasNative: true, label: "Nanoleaf Shapes · 9 panels", host: "192.168.1.30", paired: true, state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "hue:001788:3", kind: "hue", group: "room", name: "Floor lamp", online: false, error: "Not paired", pixels: 1,
        hasNative: false, label: "Hue color lamp", host: "192.168.1.41", paired: false, state: st({ color: "#ffb46b" }), hidden: false },
    ],
    hubs: [{ id: "hue:001788", kind: "hue", name: "Hue bridge", host: "192.168.1.41", online: false, error: "Not paired", paired: false, lights: 1 },
      { id: "dmx:artnet-0", kind: "dmx", name: "Art-Net universe 0", host: "", online: true, error: null, paired: true, lights: 2,
        protocol: "artnet", universe: 0, layouts: ["RGB", "GRB", "RGBW", "DRGB", "DRGBW"],
        fixtures: [{ id: "f1", name: "Par left", start: 1, pixels: 1, layout: "DRGB" }, { id: "f2", name: "Par right", start: 5, pixels: 1, layout: "DRGB" }] }],
    drivers: [["wled", "WLED"], ["nanoleaf", "Nanoleaf", "Hold the controller's power button for 5-7 seconds until the lights flash, then press Pair within 30 seconds."],
      ["hue", "Philips Hue", "Press the round link button on the Hue bridge, then press Pair within 30 seconds.", true],
      ["govee", "Govee", "Turn on LAN Control for the light in the Govee Home app (device settings → LAN Control).", true],
      ["lifx", "LIFX", "", true], ["yeelight", "Yeelight", "Turn on LAN Control for the light in the Yeelight app.", true],
      ["elgato", "Elgato", "", true], ["twinkly", "Twinkly", "", true], ["magichome", "Magic Home", "", true]]
      .map(([kind, title, pairing, beta]) => ({ kind, title, hub: kind === "hue", pairing: pairing || "", beta: !!beta,
        fields: [{ key: "host", label: "IP address", placeholder: "192.168.1.40" }] }))
      .concat([{ kind: "homeassistant", title: "Home Assistant", hub: true, beta: true,
        pairing: "Create a long-lived access token in Home Assistant (your profile → Security → Long-lived access tokens) and add it here.",
        fields: [{ key: "url", label: "Home Assistant address", placeholder: "http://homeassistant.local:8123" },
          { key: "token", label: "Long-lived access token", placeholder: "From your Home Assistant profile → Security", secret: true }] },
        { kind: "dmx", title: "DMX (Art-Net / sACN)", hub: true, beta: true, pairing: "",
          fields: [{ key: "protocol", label: "Protocol", options: ["artnet", "sacn"], placeholder: "artnet" },
            { key: "host", label: "Node IP address (empty = broadcast / multicast)", placeholder: "192.168.1.60" },
            { key: "universe", label: "Universe", placeholder: "0" }] }]),
    stage: {
      "wled:1": { start: 0, end: 0.68, layer: "front", custom: false }, "wled:2": { start: 0.12, end: 0.6, layer: "front", custom: false },
      "qmk:gx87": { start: 0.22, end: 0.52, layer: "front", custom: false },
      "asrock_gpu:0": { start: 0.72, end: 0.82, layer: "front", custom: false },
      "openrgb:Corsair Commander Core": { start: 0.82, end: 0.91, layer: "front", custom: false },
      "openrgb:QCK": { start: 0.91, end: 1, layer: "front", custom: false },
      "nanoleaf:1": { start: 0, end: 1, layer: "back", custom: false }, "hue:001788:3": { start: 0, end: 1, layer: "back", custom: false },
    },
    about: { name: "Glowstage", version: "0.9.7", repo: "https://github.com/example/glowstage",
      donate: [{ name: "GitHub Sponsors", url: "https://github.com/sponsors/example" }, { name: "Card or iDEAL", url: "https://buy.stripe.com/example" }, { name: "PayPal", url: "https://paypal.me/example" }] },
    openrgb: { status: "connected", message: "Connected · 2 devices", path: "C:\\Program Files\\Glowstage\\tools\\OpenRGB\\OpenRGB.exe",
      bundled: true, elevated: false, pawnio: true, autoLaunch: true, install: { state: "idle", progress: 0, message: "", version: "1.1" } },
    genreAi: { state: "idle", progress: 0, message: "", name: "Discogs-EffNet", maker: "Music Technology Group, Universitat Pompeu Fabra",
      license: "CC BY-NC-SA 4.0", licenseUrl: "https://creativecommons.org/licenses/by-nc-sa/4.0/",
      aboutUrl: "https://essentia.upf.edu/models.html", mb: 18, installed: false, active: "YAMNet" },
    presets: [{ name: "Rainbow", effects: ["rainbow"], colors: [] }, { name: "Warm white", effects: [], colors: ["#ffb46b"] },
      { name: "Follow the sun", effects: ["daylight"], colors: [], split: true },
      { name: "Focus", effects: [], colors: ["#eaf1ff"], split: true },
      { name: "Reading", effects: [], colors: ["#ffdcb0", "#ffb46b"], split: true },
      { name: "Cozy", effects: ["breathing"], colors: ["#ff8f3f", "#ff6a1a"], split: true },
      { name: "Gaming", effects: ["rainbow", "wave"], colors: ["#3a00ff", "#00d0ff"], split: true },
      { name: "Chill", effects: ["wave"], colors: ["#5b2bff", "#00b8ff"] },
      { name: "Cyberpunk", effects: ["wave"], colors: ["#ff0090", "#00f0ff"] }, { name: "Movie", effects: ["screen"], colors: [] },
      { name: "Late night", effects: ["wave"], colors: ["#2a3cff", "#8a2cff"] },
      { name: "Festival", effects: ["music_show"], colors: [] },
      { name: "Party", effects: ["music_party"], colors: [] }, { name: "Romantic", effects: ["music_romantic"], colors: ["#1428ff", "#6a0dc8"] },
      { name: "Chill beats", effects: ["music_party"], colors: [] }, { name: "Club", effects: ["music_ripple"], colors: [] },
      { name: "Rave", effects: ["music_party"], colors: [] }, { name: "Neon pulse", effects: ["music_pulse"], colors: ["#ff1493", "#00e5ff"] },
      { name: "Inferno", effects: ["music_pulse"], colors: ["#ff2200", "#ff9500"] }, { name: "Equalizer", effects: ["music_pulse"], colors: ["#6a0dc8", "#1428ff"] },
      { name: "Ocean", effects: ["music_romantic"], colors: ["#0038ff", "#00c2c7"] }, { name: "Sunset", effects: ["music_romantic"], colors: ["#ff3d00", "#ff1f7a"] },
      { name: "Fireplace", effects: ["music_fire"], colors: [] }, { name: "Northern lights", effects: ["music_aurora"], colors: [] },
      { name: "Heartbeat", effects: ["music_heart"], colors: ["#ff0030", "#5a00ff"] }, { name: "Disco", effects: ["music_sparkle"], colors: [] },
      { name: "Retro", effects: ["music_organ"], colors: [] },
      { name: "Red light", effects: ["wave"], colors: ["#d0001c", "#70003c"] }, { name: "Velvet", effects: ["breathing"], colors: ["#5000c0", "#9a0060"] },
      { name: "Midnight", effects: ["twinkle"], colors: ["#7a3cff", "#0a14ff"] },
      { name: "Boudoir", effects: ["wave"], colors: ["#4b00c8", "#8a0070", "#c8001e", "#2a10ff"] },
      { name: "Neon noir", effects: ["comet"], colors: ["#ff0048", "#2a00ff"] },
      { name: "Slow jam", effects: ["music_romantic"], colors: ["#0a1aff", "#b0002e"] },
      { name: "Seduction", effects: ["music_romantic"], colors: ["#9a0024", "#5200c8"] },
      { name: "Ultraviolet", effects: ["music_bloom"], colors: ["#9000ff", "#1200a0"] },
      { name: "Red room", effects: ["music_pulse"], colors: ["#c8001a", "#5a00c8"] },
      { name: "After dark", effects: ["music_bloom", "music_heart", "music_romantic"], colors: ["#1a10c8", "#7a00a8", "#c0001c", "#6a0060"] }],
    effects,
    audio: { device: "Headphones (WH-1000XM4)", listening: false, kind: "bluetooth", sync: { ms: 200, auto: true } },
    show: { style: "Techno", ai: true, aiError: "", confidence: 0.62, genres: ["Techno", "Electronic music"],
      mood: "Exciting", section: "drop", scene: "chase16", bpm: 128, half: false, chord: "Am in C" },
    auto: (location.search.match(/[?&]auto=(cs2|game|video|music|app:[a-z0-9.]+)/) || [])[1] || null,
    updating: { state: "idle", progress: 0, message: "", canInstall: true },
    whatsNew: /[?&]new/.test(location.search) ? { version: "0.9.7", items: [
      { title: "Phone remote", go: "setup:phone", text: "Scan a code and control your lights from your phone: on and off, brightness, presets, the sleep timer." },
      { title: "Screen + sound", go: "setup:automatic", text: "In any game your lights follow the screen and flash with every shot or explosion, on the side it came from." },
      { title: "Typing ripples", go: "effect:typing", text: "Every key you press lights up and sends a ripple across your keyboard." },
      { title: "A look per app", go: "setup:automatic", text: "Focus while Photoshop is open, the light show with your DJ app: back to your own look when you close it." },
      { title: "Sleep timer", go: "sleep", text: "At bedtime your lights fade out like a sunset and switch off." }] } : null,
    remote: /[?&]phone/.test(location.search) ? { on: true, url: "http://192.168.178.23:52341/?k=q7Jd0aXv3K_bS9mPwE2yTg", error: "" } : { on: false, url: "", error: "" },
    sleep: /[?&]sleep/.test(location.search) ? { started: Date.now() / 1000 - 300, until: Date.now() / 1000 + 1500, lights: 7 } : null,
    wake: /[?&]wake/.test(location.search) ? { until: Date.now() / 1000 + 900, minutes: 30 } : null,
    sun: { place: "Netherlands", sunrise: "08:18", sunset: "16:31", phase: "golden hour", kelvin: 3640, level: 0.9 },
    updateResult: (location.search.match(/[?&]result=(updated|update-failed)/) || [])[1] || "",
    recovered: /[?&]recovered\b/.test(location.search) ? "2026-11-26" : "",
    games: { cs2: { found: true, connected: /[?&]cs2\b/.test(location.search), fileThere: true, receiving: /[?&]cs2\b/.test(location.search),
      inMatch: /[?&]cs2\b/.test(location.search), team: "CT", health: 87 } },
    settings: { recent_colors: ["#ff8800", "#2d9cff", "#ffb46b", "#9b59ff", "#00e0a0"], close_to_tray: true, start_minimized: false, restore_on_launch: true, autostart: false, show_strobe: true, auto_show: /[?&]auto=music/.test(location.search), auto_game: "preset:Gaming", auto_video: "",
      lights_on_at_startup: true, pc_lights_off_at_shutdown: true, room_lights_off_at_shutdown: true, away_lights: "", flow: false,
      setup_done: !/[?&]tour\b/.test(location.search) },
  };
  const wledFx = ["Android", "Aurora", "Blink", "Bouncing Balls", "Breathe", "Candle", "Chase", "Colorloop", "Fire 2012",
    "Fireworks", "Lake", "Meteor", "Noise 1", "Pacifica", "Plasma", "Rainbow", "Ripple", "Sinelon", "Twinkle", "Wipe"]
    .map((name, i) => ({ id: String(i + 1), name }));
  const ok = result => Promise.resolve({ ok: true, result });
  // A made-up 128 BPM show: a block chasing across the front, the back wall pumping on the beat.
  const live = () => {
    const t = performance.now() / 1000, beat = t / (60 / 128), pal = [[0, 229, 255], [255, 0, 200], [42, 75, 255]];
    const c = pal[Math.floor(beat / 4) % 3], pump = Math.exp(-(beat % 1) * 3);
    const out = {};
    for (const d of data.devices) {
      if (!d.online || !d.state.on || d.state.mode !== "effect") continue;
      const s = data.stage[d.id] || { start: 0, end: 1, layer: "front" }, n = Math.min(24, Math.max(1, d.pixels));
      out[d.id] = Array.from({ length: n }, (_, i) => {
        let v;
        if (s.layer === "back") v = 0.15 + 0.85 * pump;
        else {
          const x = s.start + (s.end - s.start) * (i + 0.5) / n, pos = (beat / 2) % 2 < 1 ? (beat / 2) % 1 : 1 - (beat / 2) % 1;
          v = 0.06 + 0.94 * Math.max(0, 1 - Math.abs(x - pos) / 0.18) ** 1.5;
        }
        return "#" + c.map(k => Math.round(k * v).toString(16).padStart(2, "0")).join("");
      });
    }
    return out;
  };
  window.pywebview = { api: {
    get_state: () => ok(JSON.parse(JSON.stringify(data))),
    update: (ids, patch) => {
      for (const d of data.devices) if (!ids || ids.includes(d.id)) Object.assign(d.state, patch);
      return ok();
    },
    native_options: () => ok({ effects: wledFx, palettes: [{ id: 0, name: "Default" }, { id: 6, name: "Party" }, { id: 11, name: "Rainbow" }] }),
    apply_preset: name => { data.activePreset = name; data.revision++; return ok(); }, save_preset: () => ok(), delete_preset: () => ok(),
    pair_nanoleaf: () => Promise.resolve({ ok: false, error: "The panels aren't in pairing mode." }),
    add_device: () => ok(), rename: () => ok(), set_hidden: () => ok(), forget: () => ok(), identify: () => ok(), resize_zone: () => ok(),
    openrgb_restart: () => ok(), openrgb_stop: () => ok(), set_openrgb_option: () => ok(),
    remember_color: c => { const l = data.settings.recent_colors; data.settings.recent_colors = [c.toLowerCase(), ...l.filter(x => x !== c.toLowerCase())].slice(0, 8); data.revision++; return ok(); },
    set_setting: (k, v) => { data.settings[k] = v; data.revision++; return ok(); },
    stop_automation: () => { data.auto = null; data.revision++; return ok(); },
    start_sleep_timer: minutes => { const now = Date.now() / 1000; data.sleep = { started: now, until: now + minutes * 60, lights: 7 }; return ok(); },
    cancel_sleep_timer: () => { data.sleep = null; return ok(); },
    cancel_wake: () => { data.wake = null; return ok(); },
    whats_new_seen: () => { data.whatsNew = null; return ok(); },
    undo: () => ok(),
    rename_preset: (o, n) => { const p = data.presets.find(x => x.name === o); if (p) p.name = n; data.revision++; return ok(); },
    move_preset: (name, step) => { const i = data.presets.findIndex(x => x.name === name); const [p] = data.presets.splice(i, 1);
      data.presets.splice(Math.max(0, Math.min(data.presets.length, i + step)), 0, p); data.revision++; return ok(); },
    duplicate_preset: name => { const i = data.presets.findIndex(x => x.name === name); let n = `${name} copy`, k = 2;
      while (data.presets.some(x => x.name === n)) n = `${name} copy ${k++}`;
      data.presets.splice(i + 1, 0, { ...data.presets[i], name: n }); data.revision++; return ok(n); },
    nudge_brightness: delta => { for (const d of data.devices) if (d.state.on) d.state.brightness = Math.max(1, Math.min(100, d.state.brightness + delta));
      data.revision++; return ok(); },
    diagnostics: () => ok("Glowstage 0.9.7 (app) on Windows 11 (build 26300)\nLights: 8 (7 answering)\n- Commander Core (openrgb, pc, 32 LEDs): answering\n- Graphics card (asrock_gpu, pc, 80 LEDs): answering\n- Shapes (nanoleaf, room, 9 LEDs): not answering: Not responding\nOpenRGB: running\nAutomatic: games preset:Gaming, videos off, music off, away off, apps 1, CS2 off\nAlso: phone remote off, hotkeys off, schedules 2, sound Headphones (WH-1000XM4)\nLast warnings in the log:\n2026-11-27 14:18:53,884 WARNING glowstage.manager: Shapes: Not responding"),
    report_problem: () => ok(),
    list_backups: () => ok(/[?&]nobackups\b/.test(location.search) ? [] : [
      { file: "config-2026-11-27-141203-before.json", saved: "2026-11-27T14:09", before: true, ok: true, presets: 39, lights: 13 },
      { file: "config-2026-11-26.json", saved: "2026-11-26T23:41", before: false, ok: true, presets: 39, lights: 13 },
      { file: "config-2026-11-25.json", saved: "2026-11-25T22:17", before: false, ok: true, presets: 37, lights: 13 },
      { file: "config-2026-11-21.json", saved: "2026-11-21T19:02", before: false, ok: true, presets: 36, lights: 12 }]),
    restore_backup: () => ok(),
    export_settings: () => ok("Glowstage settings 2026-11-27.json"),
    pick_settings_file: () => ok({ file: "Glowstage settings 2026-11-20.json", saved: "2026-11-20T18:30", before: false, ok: true, presets: 31, lights: 11 }),
    load_settings_file: () => ok(),
    remote_set: on => { data.remote = { on, url: on ? "http://192.168.178.23:52341/?k=q7Jd0aXv3K_bS9mPwE2yTg" : "", error: "" }; data.revision++; return ok(); },
    remote_new_link: () => { data.remote = { ...data.remote, url: data.remote.url.replace(/k=.*/, "k=" + Math.random().toString(36).slice(2, 24)) }; data.revision++; return ok(); },
    remote_qr: () => ok(window.SAMPLE_QR || ""),
    install_update: () => {
      let p = 0;
      const tick = () => { p += 0.12; Object.assign(data.updating, p < 1
        ? { state: "downloading", progress: p, message: `Getting the new version… ${Math.round(p * 56)} of 56 MB` }
        : { state: "installing", progress: 1, message: "Installing: Glowstage restarts in a moment…" }); if (p < 1) setTimeout(tick, 400); };
      tick(); return ok();
    },
    games_connect: () => { Object.assign(data.games.cs2, { connected: true }); data.revision++; return ok(); },
    games_disconnect: () => { Object.assign(data.games.cs2, { connected: false, receiving: false, inMatch: false }); data.revision++; return ok(); },
    hide_window: () => ok(),
    get_live: () => ok(live()),
    set_stage: (id, span) => {
      if (span) data.stage[id] = { start: span[0], end: span[1], layer: span[2], custom: true };
      else delete data.stage[id].custom;
      return ok();
    },
    support_answer: () => ok(), check_online: () => ok(),
    set_schedules: items => { data.schedules = items; data.revision++; return ok(); },
    set_app_looks: items => { data.appLooks = items; data.revision++; return ok(); },
    open_apps: () => ok([{ exe: "obs64.exe", name: "OBS Studio" }, { exe: "photoshop.exe", name: "Adobe Photoshop 2025" },
      { exe: "spotify.exe", name: "Spotify" }, { exe: "code.exe", name: "Visual Studio Code" }]),
    set_show_excluded: ids => { data.showExcluded = ids; data.revision++; return ok(); },
    set_show_sync: ms => { data.audio.sync = ms === null ? { ms: 200, auto: true } : { ms, auto: false }; return ok(); },
    reset_stage: () => ok(), search: () => ok(), open_link: () => ok(), restart_app: () => ok(),
    pair: () => Promise.resolve({ ok: false, error: "The bridge's link button wasn't pressed." }),
    set_dmx_fixtures: (id, fixtures) => { data.hubs[1].fixtures = fixtures; return ok(); },
    genre_ai_install: () => {
      const g = data.genreAi;
      Object.assign(g, { state: "downloading", progress: 0, message: "Downloading the enhanced genre AI…" });
      const tick = setInterval(() => {
        g.progress = Math.min(1, g.progress + 0.125);
        g.message = `Downloading the enhanced genre AI… ${Math.round(g.progress * 18)} of 18 MB`;
        if (g.progress >= 1) {
          clearInterval(tick);
          Object.assign(g, { state: "done", installed: true, active: "Discogs-EffNet", message: "The enhanced genre AI is installed" });
          Object.assign(data.show, { style: "House / Disco", genres: ["Deep House", "Tech House"], aiModel: "Discogs-EffNet" });
        }
      }, 350);
      return ok();
    },
    genre_ai_remove: () => {
      Object.assign(data.genreAi, { state: "idle", progress: 0, message: "", installed: false, active: "YAMNet" });
      Object.assign(data.show, { style: "Techno", genres: ["Techno", "Electronic music"], aiModel: "YAMNet" });
      return ok();
    },
    openrgb_install: () => {
      const inst = data.openrgb.install;
      Object.assign(inst, { state: "downloading", progress: 0, message: "Downloading OpenRGB…" });
      const tick = setInterval(() => {
        inst.progress = Math.min(1, inst.progress + 0.1);
        inst.message = `Downloading OpenRGB… ${Math.round(inst.progress * 31)} of 31 MB`;
        if (inst.progress >= 1) { clearInterval(tick); Object.assign(inst, { state: "done", message: "OpenRGB 1.1 is installed" }); }
      }, 400);
      return ok();
    },
  } };
})();

/* A real code for the sample link, so the preview shows what the app draws. */
window.SAMPLE_QR = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 41 41" shape-rendering="crispEdges"><rect width="41" height="41" fill="#fff"/><path d="M4,4h7v1h-7zM13,4h1v1h-1zM15,4h2v1h-2zM20,4h1v1h-1zM23,4h1v1h-1zM26,4h3v1h-3zM30,4h7v1h-7zM4,5h1v1h-1zM10,5h1v1h-1zM13,5h1v1h-1zM15,5h2v1h-2zM19,5h3v1h-3zM24,5h2v1h-2zM28,5h1v1h-1zM30,5h1v1h-1zM36,5h1v1h-1zM4,6h1v1h-1zM6,6h3v1h-3zM10,6h1v1h-1zM12,6h1v1h-1zM14,6h1v1h-1zM16,6h1v1h-1zM18,6h1v1h-1zM20,6h1v1h-1zM23,6h1v1h-1zM25,6h3v1h-3zM30,6h1v1h-1zM32,6h3v1h-3zM36,6h1v1h-1zM4,7h1v1h-1zM6,7h3v1h-3zM10,7h1v1h-1zM12,7h4v1h-4zM22,7h1v1h-1zM26,7h1v1h-1zM30,7h1v1h-1zM32,7h3v1h-3zM36,7h1v1h-1zM4,8h1v1h-1zM6,8h3v1h-3zM10,8h1v1h-1zM12,8h1v1h-1zM17,8h8v1h-8zM27,8h2v1h-2zM30,8h1v1h-1zM32,8h3v1h-3zM36,8h1v1h-1zM4,9h1v1h-1zM10,9h1v1h-1zM12,9h1v1h-1zM14,9h1v1h-1zM17,9h1v1h-1zM21,9h2v1h-2zM25,9h1v1h-1zM30,9h1v1h-1zM36,9h1v1h-1zM4,10h7v1h-7zM12,10h1v1h-1zM14,10h1v1h-1zM16,10h1v1h-1zM18,10h1v1h-1zM20,10h1v1h-1zM22,10h1v1h-1zM24,10h1v1h-1zM26,10h1v1h-1zM28,10h1v1h-1zM30,10h7v1h-7zM12,11h2v1h-2zM18,11h1v1h-1zM20,11h1v1h-1zM22,11h1v1h-1zM24,11h2v1h-2zM27,11h2v1h-2zM4,12h1v1h-1zM6,12h5v1h-5zM14,12h1v1h-1zM16,12h2v1h-2zM20,12h3v1h-3zM24,12h1v1h-1zM30,12h5v1h-5zM5,13h3v1h-3zM9,13h1v1h-1zM11,13h1v1h-1zM14,13h5v1h-5zM21,13h1v1h-1zM23,13h1v1h-1zM27,13h5v1h-5zM33,13h2v1h-2zM4,14h2v1h-2zM7,14h1v1h-1zM9,14h3v1h-3zM15,14h7v1h-7zM25,14h2v1h-2zM29,14h1v1h-1zM32,14h1v1h-1zM34,14h2v1h-2zM4,15h1v1h-1zM7,15h2v1h-2zM13,15h3v1h-3zM17,15h1v1h-1zM20,15h4v1h-4zM25,15h2v1h-2zM28,15h1v1h-1zM30,15h1v1h-1zM32,15h5v1h-5zM8,16h3v1h-3zM13,16h1v1h-1zM15,16h2v1h-2zM18,16h1v1h-1zM20,16h1v1h-1zM24,16h3v1h-3zM32,16h2v1h-2zM35,16h1v1h-1zM4,17h4v1h-4zM12,17h1v1h-1zM19,17h1v1h-1zM22,17h1v1h-1zM24,17h1v1h-1zM26,17h1v1h-1zM29,17h2v1h-2zM35,17h2v1h-2zM5,18h1v1h-1zM8,18h4v1h-4zM13,18h1v1h-1zM19,18h2v1h-2zM24,18h1v1h-1zM26,18h1v1h-1zM28,18h1v1h-1zM31,18h1v1h-1zM35,18h1v1h-1zM4,19h6v1h-6zM11,19h1v1h-1zM13,19h1v1h-1zM15,19h2v1h-2zM19,19h1v1h-1zM25,19h1v1h-1zM27,19h3v1h-3zM31,19h4v1h-4zM4,20h3v1h-3zM10,20h2v1h-2zM13,20h1v1h-1zM16,20h1v1h-1zM18,20h2v1h-2zM21,20h4v1h-4zM31,20h2v1h-2zM36,20h1v1h-1zM5,21h1v1h-1zM12,21h1v1h-1zM14,21h1v1h-1zM16,21h1v1h-1zM23,21h1v1h-1zM25,21h7v1h-7zM33,21h4v1h-4zM6,22h2v1h-2zM10,22h3v1h-3zM14,22h1v1h-1zM17,22h4v1h-4zM22,22h1v1h-1zM25,22h1v1h-1zM28,22h1v1h-1zM32,22h1v1h-1zM34,22h1v1h-1zM9,23h1v1h-1zM12,23h1v1h-1zM16,23h4v1h-4zM23,23h3v1h-3zM27,23h1v1h-1zM29,23h2v1h-2zM32,23h4v1h-4zM4,24h1v1h-1zM7,24h1v1h-1zM10,24h2v1h-2zM14,24h3v1h-3zM18,24h2v1h-2zM22,24h3v1h-3zM26,24h4v1h-4zM33,24h1v1h-1zM4,25h2v1h-2zM7,25h2v1h-2zM12,25h3v1h-3zM17,25h4v1h-4zM23,25h5v1h-5zM33,25h2v1h-2zM36,25h1v1h-1zM4,26h1v1h-1zM9,26h2v1h-2zM12,26h2v1h-2zM17,26h2v1h-2zM21,26h2v1h-2zM25,26h1v1h-1zM30,26h1v1h-1zM34,26h2v1h-2zM4,27h1v1h-1zM9,27h1v1h-1zM12,27h2v1h-2zM15,27h3v1h-3zM20,27h1v1h-1zM22,27h2v1h-2zM25,27h3v1h-3zM29,27h1v1h-1zM31,27h1v1h-1zM34,27h2v1h-2zM4,28h1v1h-1zM7,28h1v1h-1zM9,28h2v1h-2zM12,28h3v1h-3zM16,28h2v1h-2zM20,28h3v1h-3zM27,28h7v1h-7zM35,28h1v1h-1zM12,29h1v1h-1zM14,29h2v1h-2zM17,29h2v1h-2zM21,29h1v1h-1zM23,29h1v1h-1zM25,29h2v1h-2zM28,29h1v1h-1zM32,29h1v1h-1zM34,29h3v1h-3zM4,30h7v1h-7zM13,30h1v1h-1zM17,30h3v1h-3zM21,30h1v1h-1zM26,30h3v1h-3zM30,30h1v1h-1zM32,30h1v1h-1zM34,30h2v1h-2zM4,31h1v1h-1zM10,31h1v1h-1zM12,31h2v1h-2zM15,31h3v1h-3zM19,31h4v1h-4zM25,31h2v1h-2zM28,31h1v1h-1zM32,31h1v1h-1zM34,31h2v1h-2zM4,32h1v1h-1zM6,32h3v1h-3zM10,32h1v1h-1zM12,32h1v1h-1zM14,32h1v1h-1zM17,32h1v1h-1zM19,32h2v1h-2zM23,32h4v1h-4zM28,32h6v1h-6zM35,32h2v1h-2zM4,33h1v1h-1zM6,33h3v1h-3zM10,33h1v1h-1zM12,33h1v1h-1zM16,33h4v1h-4zM22,33h2v1h-2zM25,33h1v1h-1zM31,33h2v1h-2zM34,33h3v1h-3zM4,34h1v1h-1zM6,34h3v1h-3zM10,34h1v1h-1zM12,34h1v1h-1zM14,34h1v1h-1zM16,34h1v1h-1zM19,34h4v1h-4zM24,34h1v1h-1zM26,34h1v1h-1zM28,34h4v1h-4zM34,34h1v1h-1zM4,35h1v1h-1zM10,35h1v1h-1zM14,35h2v1h-2zM19,35h1v1h-1zM24,35h5v1h-5zM30,35h5v1h-5zM4,36h7v1h-7zM12,36h4v1h-4zM17,36h1v1h-1zM19,36h4v1h-4zM24,36h1v1h-1zM27,36h6v1h-6zM35,36h1v1h-1z" fill="#000"/></svg>';
