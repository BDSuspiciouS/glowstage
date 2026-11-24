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
    devices: [
      { id: "openrgb:Corsair Commander Core", kind: "openrgb", group: "pc", name: "Commander Core", online: true, error: null,
        pixels: 131, hasNative: false, label: "Cooler · 131 LEDs", zones, state: st({ mode: "effect", effect: "rainbow" }), hidden: false },
      { id: "openrgb:QCK", kind: "openrgb", group: "pc", name: "QCK Prism XL", online: true, error: null, pixels: 2,
        hasNative: false, label: "Mousemat · 2 LEDs", zones: [], state: st({ color: "#ff0090" }), hidden: false },
      { id: "wled:1", kind: "wled", group: "room", name: "Desk strip", online: true, error: null, pixels: 297, hasNative: true,
        label: "WLED 0.15.3", host: "192.168.1.26", state: st({ color: "#ff7a00", brightness: 70 }), hidden: false },
      { id: "wled:2", kind: "wled", group: "room", name: "Monitor", online: true, error: null, pixels: 99, hasNative: true,
        label: "WLED 0.15.3", host: "192.168.1.27", state: st({ on: false, color: "#1e00ff" }), hidden: false },
      { id: "nanoleaf:1", kind: "nanoleaf", group: "room", name: "Shapes 1FF1", online: false, error: "Not paired", pixels: 0,
        hasNative: false, label: "Nanoleaf", host: "192.168.1.30", paired: false, state: st(), hidden: false },
    ],
    openrgb: { status: "connected", message: "Connected · 2 devices", path: "C:\\Program Files\\Glowstage\\tools\\OpenRGB\\OpenRGB.exe",
      bundled: true, elevated: false, pawnio: true, autoLaunch: true },
    presets: [{ name: "Rainbow", effects: ["rainbow"], colors: [] }, { name: "Warm white", effects: [], colors: ["#ffb46b"] },
      { name: "Focus", effects: [], colors: ["#eaf1ff"] }, { name: "Chill", effects: ["wave"], colors: ["#5b2bff", "#00b8ff"] },
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
    audio: { device: "Speakers (Realtek(R) Audio)", listening: false },
    show: { style: "Techno", ai: true, aiError: "", confidence: 0.62, genres: ["Techno", "Electronic music"],
      mood: "Exciting", section: "drop", scene: "chase16", bpm: 128, half: false, chord: "Am in C" },
    settings: { close_to_tray: true, start_minimized: false, restore_on_launch: true, autostart: false },
  };
  const wledFx = ["Android", "Aurora", "Blink", "Bouncing Balls", "Breathe", "Candle", "Chase", "Colorloop", "Fire 2012",
    "Fireworks", "Lake", "Meteor", "Noise 1", "Pacifica", "Plasma", "Rainbow", "Ripple", "Sinelon", "Twinkle", "Wipe"]
    .map((name, i) => ({ id: String(i + 1), name }));
  const ok = result => Promise.resolve({ ok: true, result });
  window.pywebview = { api: {
    get_state: () => ok(JSON.parse(JSON.stringify(data))),
    update: (ids, patch) => {
      for (const d of data.devices) if (!ids || ids.includes(d.id)) Object.assign(d.state, patch);
      return ok();
    },
    native_options: () => ok({ effects: wledFx, palettes: [{ id: 0, name: "Default" }, { id: 6, name: "Party" }, { id: 11, name: "Rainbow" }] }),
    apply_preset: () => ok(), save_preset: () => ok(), delete_preset: () => ok(),
    pair_nanoleaf: () => Promise.resolve({ ok: false, error: "The panels aren't in pairing mode." }),
    add_device: () => ok(), rename: () => ok(), set_hidden: () => ok(), forget: () => ok(), resize_zone: () => ok(),
    openrgb_restart: () => ok(), openrgb_stop: () => ok(), set_openrgb_option: () => ok(), set_setting: () => ok(),
    hide_window: () => ok(),
  } };
})();
