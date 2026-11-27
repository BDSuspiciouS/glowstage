"use strict";
/* Sample backend so the UI can be previewed in a plain browser. Not used by the app. */
(() => {
  const st = (o = {}) => ({ on: true, brightness: 100, mode: "color", color: "#7c4dff", color2: "#00e5ff",
    effect: "rainbow", speed: 50, native: null, palette: null, ...o });
  const effects = [["rainbow", "Rainbow wave", ["speed"]], ["spectrum", "Spectrum cycle", ["speed"]],
    ["breathing", "Breathing", ["speed", "color", "color2"]], ["wave", "Color wave", ["speed", "color", "color2"]],
    ["comet", "Comet", ["speed", "color", "color2"]], ["twinkle", "Starlight", ["speed", "color", "color2"]],
    ["fire", "Fire", ["speed"]], ["aurora", "Aurora", ["speed"]], ["screen", "Screen sync", []],
    ["music_show", "Light show", ["speed"], "music"],
    ["music_party", "Party", ["speed"], "music"], ["music_ripple", "Bass ripple", ["speed"], "music"],
    ["music_spectrum", "Spectrum", ["speed"], "music"], ["music_pulse", "Beat pulse", ["speed", "color", "color2"], "music"],
    ["music_vu", "VU meter", ["speed", "color", "color2"], "music"], ["music_romantic", "Romantic", ["speed", "color", "color2"], "music"],
    ["music_organ", "Color organ", ["speed"], "music"], ["music_bloom", "Center bloom", ["speed", "color", "color2"], "music"],
    ["music_sparkle", "Sparkle", ["speed"], "music"], ["music_meteor", "Meteor", ["speed"], "music"],
    ["music_fire", "Music fire", ["speed"], "music"], ["music_aurora", "Northern lights", ["speed"], "music"],
    ["music_heart", "Heartbeat", ["speed", "color", "color2"], "music"]]
    .map(([id, name, uses, group]) => ({ id, name, uses, group: group || "animation" }));
  const zones = [{ index: 0, name: "Pump", leds: 29, min: 29, max: 29, resizable: false, detected: -1 },
    ...[1, 2, 3, 4, 5, 6].map(i => ({ index: i, name: `Corsair Fan Header ${i}`, leds: i <= 3 ? 8 : 0, min: 0, max: 34,
      resizable: true, detected: i <= 3 ? 8 : null }))];
  const data = {
    revision: 1,
    showExcluded: [],
    schedules: [{ time: "23:00", days: [0, 1, 2, 3, 4, 5, 6], action: "preset:Late night", enabled: true },
      { time: "01:00", days: [0, 1, 2, 3, 6], action: "off", enabled: true }],
    hotkeysTaken: [],
    update: /[?&]update\b/.test(location.search)
      ? { version: "0.9.4", url: "https://github.com/BDSuspiciouS/glowstage/releases/tag/v0.9.4" } : null,
    askSupport: /[?&]support\b/.test(location.search),
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
      { id: "wled:2", kind: "wled", group: "room", name: "Monitor", online: true, error: null, pixels: 99, hasNative: true,
        label: "WLED 0.15.3", host: "192.168.1.27", state: st({ mode: "effect", effect: "music_show", speed: 70 }), hidden: false },
      { id: "nanoleaf:1", kind: "nanoleaf", group: "room", name: "Shapes", online: true, error: null, pixels: 9,
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
    about: { name: "Glowstage", version: "0.9.3", repo: "https://github.com/example/glowstage",
      donate: [{ name: "GitHub Sponsors", url: "https://github.com/sponsors/example" }, { name: "Card or iDEAL", url: "https://buy.stripe.com/example" }, { name: "PayPal", url: "https://paypal.me/example" }] },
    openrgb: { status: "connected", message: "Connected · 2 devices", path: "C:\\Program Files\\Glowstage\\tools\\OpenRGB\\OpenRGB.exe",
      bundled: true, elevated: false, pawnio: true, autoLaunch: true, install: { state: "idle", progress: 0, message: "", version: "1.1" } },
    genreAi: { state: "idle", progress: 0, message: "", name: "Discogs-EffNet", maker: "Music Technology Group, Universitat Pompeu Fabra",
      license: "CC BY-NC-SA 4.0", licenseUrl: "https://creativecommons.org/licenses/by-nc-sa/4.0/",
      aboutUrl: "https://essentia.upf.edu/models.html", mb: 18, installed: false, active: "YAMNet" },
    presets: [{ name: "Rainbow", effects: ["rainbow"], colors: [] }, { name: "Warm white", effects: [], colors: ["#ffb46b"] },
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
    auto: (location.search.match(/[?&]auto=(cs2|game|video|music)/) || [])[1] || null,
    games: { cs2: { found: true, connected: /[?&]cs2\b/.test(location.search), fileThere: true, receiving: /[?&]cs2\b/.test(location.search),
      inMatch: /[?&]cs2\b/.test(location.search), team: "CT", health: 87 } },
    settings: { close_to_tray: true, start_minimized: false, restore_on_launch: true, autostart: false, show_strobe: true, auto_show: /[?&]auto=music/.test(location.search), auto_game: "preset:Gaming", auto_video: "",
      lights_on_at_startup: true, pc_lights_off_at_shutdown: true, room_lights_off_at_shutdown: true,
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
    add_device: () => ok(), rename: () => ok(), set_hidden: () => ok(), forget: () => ok(), resize_zone: () => ok(),
    openrgb_restart: () => ok(), openrgb_stop: () => ok(), set_openrgb_option: () => ok(),
    set_setting: (k, v) => { data.settings[k] = v; data.revision++; return ok(); },
    stop_automation: () => { data.auto = null; data.revision++; return ok(); },
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
