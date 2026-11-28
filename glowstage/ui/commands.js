"use strict";

/* Ctrl+K understands what you type: "desk red", "storm", "dim", "sleep 45", "pause 1h".
   Plain functions, no page and no DOM, so they can be tested with Node (tests/test_commands.py).

   Commands.parse(text, ctx) -> [{ label, action }], at most three, best first. Nothing when the text
   doesn't read clearly as an instruction (then the search shows only its normal matches).

   ctx is what the search knows right now:
     presets  [name]                      lights  [{ id, name, group: "pc" | "room" }]
     groups   [{ name }]  (your groups)   effects [{ id, name, group }]
     showOn   the light show is running
     focus    { work: [..], rest: [..], lastWork, lastRest }  (the minutes the focus timer offers)

   An action is a plain description the window carries out:
     { type: "color", target, hex | kelvin }      { type: "brightness", target, value }
     { type: "relative", target, delta }          { type: "power", target, on }
     { type: "effect", target, effect }           { type: "show", want: "toggle" | "on" | "off" }
     { type: "sleep", minutes }   { type: "sleepStop" }   { type: "focus", work, rest | null }   { type: "focusStop" }
     { type: "pause", minutes: 60 | 180 | "tomorrow" }    { type: "resume" }
   target: { kind: "every" } (the lights that are on) | { kind: "group", name } | { kind: "light", id }
           | { kind: "scope", scope: "pc" | "room" } */
const Commands = (() => {
  const MAX = 3;

  /* ---------- words ---------- */

  /** Lower case, no accents or apostrophes, numbers and units apart ("45min" -> "45", "min"), "%" as "percent". */
  function words(text) {
    const s = String(text == null ? "" : text).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "")
      .replace(/['’]/g, "").replace(/%/g, " percent ").replace(/(\d)([a-z])/g, "$1 $2").replace(/([a-z])(\d)/g, "$1 $2");
    return s.match(/\d+(?:\.\d+)?|[a-z]+/g) || [];
  }

  /** "ff8800", "#ff8800", "#f80" as "#ff8800". Without the # a word like "facade" is not a color:
      it needs a digit, or doubled pairs ("ffeedd"). */
  function hexOf(raw) {
    const m = /^(#?)([0-9a-f]{3}|[0-9a-f]{6})$/.exec(raw);
    if (!m) return null;
    let h = m[2];
    if (!m[1]) {
      if (h.length !== 6 || /^\d+$/.test(h)) return null;
      if (!/\d/.test(h) && !/^([a-f])\1([a-f])\2([a-f])\3$/.test(h)) return null;
    }
    if (h.length === 3) h = [...h].map(c => c + c).join("");
    return "#" + h;
  }

  /** The words of what was typed; a hex color stays one word, "#rrggbb". */
  function tokenize(text) {
    const out = [];
    for (const raw of String(text == null ? "" : text).toLowerCase().split(/\s+/)) {
      if (!raw) continue;
      const hex = hexOf(raw.replace(/^[^#0-9a-f]+|[^0-9a-f]+$/g, ""));
      if (hex) out.push(hex);
      else if (raw[0] === "#") out.push("?" + raw);             // ("#12" is no color and no number: it reads as nothing)
      else out.push(...words(raw));
    }
    return out;
  }

  const FILLER = new Set(("set make turn switch put change the my please lights light all every everything to them it " +
    "for in at of with").split(" "));
  const STOP_VERBS = new Set(["stop", "cancel", "end", "clear", "disable", "remove", "abort", "quit"]);

  /* ---------- colors ---------- */

  const COLORS = {
    red: "#ff1a1a", orange: "#ff6a00", amber: "#ffb000", yellow: "#ffe600", lime: "#8cff00", green: "#00e05a",
    teal: "#00e0c0", cyan: "#00c8ff", blue: "#1f4bff", indigo: "#4b2cff", purple: "#9b30ff", violet: "#7c3dff",
    magenta: "#ff00c8", pink: "#ff5c8a", white: "#ffffff",
  };
  const WHITES = { "warm white": 2700, "cool white": 5500, candlelight: 2000, daylight: 6000 };
  const whiteName = k => k < 2600 ? "candlelight" : k < 3500 ? "warm white" : k < 4800 ? "neutral white" : k < 6000 ? "cool white" : "daylight";
  const COLOR_WORDS = new Set([...Object.keys(COLORS), "warm", "cool", "white", "candlelight", "daylight"]);

  const KEYWORDS = new Set([...FILLER, ...COLOR_WORDS, "on", "off", "max", "full", "maximum", "dim", "dimmer", "brighter", "darker",
    "brightness", "percent", "color", "colour", "effect", "look", "mode"]);

  /* ---------- how long ---------- */

  const UNIT = { min: 1, mins: 1, minute: 1, minutes: 1, m: 1, h: 60, hr: 60, hrs: 60, hour: 60, hours: 60 };

  /** "30", "45 min", "1h", "1.5 hours", "an hour", "half an hour", "1 hour 30 minutes": minutes, or null. */
  function duration(t) {
    t = t.filter(w => w !== "and");
    if (t[0] === "half") {
      const rest = t.slice(1).filter(w => w !== "an" && w !== "a" && w !== "of");
      return rest.length === 1 && UNIT[rest[0]] === 60 ? 30 : null;
    }
    let total = 0;
    for (let i = 0; i < t.length;) {
      const w = t[i];
      const n = w === "a" || w === "an" ? 1 : /^\d+(\.\d+)?$/.test(w) ? parseFloat(w) : NaN;
      if (isNaN(n)) return null;
      const unit = t[i + 1];
      if (unit !== undefined && UNIT[unit]) { total += n * UNIT[unit]; i += 2; }
      else if (i === t.length - 1 && i === 0 && w !== "a" && w !== "an") { total += n; i += 1; }   // (a bare number: minutes)
      else return null;
    }
    total = Math.round(total);
    return total >= 1 ? total : null;
  }

  const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
  /** 45 -> "45 minutes", 60 -> "1 hour", 90 -> "1 hour 30 minutes". */
  function minutesText(m) {
    const h = Math.floor(m / 60), r = m % 60;
    return [h ? plural(h, "hour") : "", r ? plural(r, "minute") : ""].filter(Boolean).join(" ");
  }

  /* ---------- timers, pause, the show: no light is named ---------- */

  const ownTokens = (t, word) => t.filter(w => w !== word);

  /** "a" and "an" only where a unit follows ("an hour"): in "a sleep timer for 30" they are just words. */
  const dropArticles = list => list.filter((w, i) => !((w === "a" || w === "an") && !UNIT[list[i + 1]]));

  function sleepCommand(t) {
    if (!t.includes("sleep")) return null;
    const others = ownTokens(t, "sleep");
    if (others.some(w => STOP_VERBS.has(w) || w === "off")) {
      const left = dropArticles(others.filter(w => !STOP_VERBS.has(w) && w !== "off" && !FILLER.has(w) && w !== "timer"));
      return left.length ? null : [{ label: "Cancel the sleep timer", action: { type: "sleepStop" } }];
    }
    const left = dropArticles(others.filter(w => !FILLER.has(w) && !["timer", "start", "begin", "after", "me"].includes(w)));
    const m = duration(left);
    if (m === null || m > 240) return null;     // (the timer takes 1 minute to 4 hours)
    return [{ label: `Sleep timer: ${minutesText(m)}`, action: { type: "sleep", minutes: m } }];
  }

  function focusCommand(t, ctx) {
    if (!t.includes("focus")) return null;
    const choices = ctx.focus || {};
    const workChoices = choices.work || [15, 25, 45, 50], restChoices = choices.rest || [5, 10, 15];
    const others = ownTokens(t, "focus");
    if (others.some(w => STOP_VERBS.has(w) || w === "off")) {
      const left = others.filter(w => !STOP_VERBS.has(w) && w !== "off" && !FILLER.has(w) && !["timer", "a", "an", "session"].includes(w));
      return left.length ? null : [{ label: "Stop the focus timer", action: { type: "focusStop" } }];
    }
    const skip = new Set(["start", "begin", "run", "a", "an", "timer", "session", "min", "mins", "minute", "minutes", "m",
      "break", "work", "rest", "and", "mode"]);
    const nums = others.filter(w => !FILLER.has(w) && !skip.has(w));
    if (nums.some(w => !/^\d+$/.test(w)) || nums.length > 2) return null;
    let work = nums.length ? +nums[0] : +(workChoices.includes(choices.lastWork) ? choices.lastWork : 25);
    const rest = nums.length > 1 ? +nums[1] : null;
    if (!workChoices.includes(work) || (rest !== null && !restChoices.includes(rest))) return null;
    const label = `Focus timer: ${plural(work, "minute")}` + (rest !== null ? `, ${rest} minute break` : "");
    return [{ label, action: { type: "focus", work, rest } }];
  }

  function pauseCommand(t) {
    const has = w => t.includes(w);
    const noise = new Set(["automatic", "automatically", "auto", "looks", "look", "automation", "automations", "the", "my", "for",
      "until", "till", "untill", "please", "all"]);
    if (has("resume") || has("unpause") || (has("pause") && t.some(w => STOP_VERBS.has(w)))) {
      const left = t.filter(w => !noise.has(w) && !["resume", "unpause", "pause"].includes(w) && !STOP_VERBS.has(w));
      return left.length ? null : [{ label: "Resume automatic looks", action: { type: "resume" } }];
    }
    if (!has("pause")) return null;
    const rest = dropArticles(ownTokens(t, "pause").filter(w => !noise.has(w) && !FILLER.has(w)));
    if (!rest.length) return [pauseResult(60)];
    if (rest.every(w => w === "tomorrow" || w === "morning")) return [pauseResult("tomorrow")];
    const m = duration(rest);
    return m === 60 || m === 180 ? [pauseResult(m)] : null;
  }
  const pauseResult = minutes => ({
    label: "Pause automatic looks " + (minutes === "tomorrow" ? "until tomorrow" : minutes === 60 ? "for an hour" : "for 3 hours"),
    action: { type: "pause", minutes },
  });

  function showCommand(t, ctx) {
    t = t.map(w => w === "lightshow" ? "show" : w);
    if (!t.includes("show")) return null;
    const noise = new Set(["the", "a", "my", "light", "lights", "music", "please", "turn", "switch", "put", "set", "make"]);
    const start = new Set(["start", "begin", "play", "run", "on"]), stop = new Set(["stop", "end", "cancel", "off", "quit"]);
    const others = ownTokens(t, "show").filter(w => !noise.has(w));
    if (others.some(w => !start.has(w) && !stop.has(w))) return null;
    const off = others.some(w => stop.has(w)), on = others.some(w => start.has(w));
    if (off && on) return null;
    const want = off ? "off" : on ? "on" : "toggle";
    const stopping = want === "off" || (want === "toggle" && ctx.showOn);
    return [{ label: stopping ? "Stop the light show" : "Start the light show", action: { type: "show", want } }];
  }

  /* ---------- what to do to the lights, without saying which ---------- */

  const MATCH_TIER = (text, name) => {       // the effect (or color) whose name this is: 0 exactly, 1 as its beginning
    if (name === text) return 0;
    return text.length >= 4 && name.startsWith(text) ? 1 : -1;
  };

  function effectsFor(strings, ctx) {
    let best = 9, found = [];
    for (const e of ctx.effects || []) {
      if (e.id === "music_show" || /^game_/.test(e.id)) continue;      // (the show has its own words; games play by themselves)
      const names = [words(e.name).join(" "), e.id.replace(/_/g, " ")];
      let tier = 9;
      for (const s of strings) for (const n of names) { const t = MATCH_TIER(s, n); if (t >= 0) tier = Math.min(tier, t); }
      if (tier === 9 || tier > best) continue;
      if (tier < best) { best = tier; found = []; }
      found.push(e);
    }
    return found;
  }

  /** What a few words say to do, with no light named: color, brightness, on or off, a look. [] when they say nothing clear. */
  function look(rest, ctx) {
    let a = rest.filter(w => !FILLER.has(w));
    if (a.length > 1 && a[a.length - 1] === "on" && a[a.length - 2] !== "off") a = a.slice(0, -1);            // ("storm on", "red on")
    let b = a.slice();
    while (b.length > 1 && ["color", "colour", "effect", "look", "mode"].includes(b[0])) b.shift();
    while (b.length > 1 && ["color", "colour", "effect", "look", "mode"].includes(b[b.length - 1])) b.pop();
    if (!b.length) return [];
    const joined = b.join(" ");

    if (joined === "on" || joined === "off") return [{ type: "power", on: joined === "on" }];

    // a color: a name, a hex, or a white by temperature
    if (b.length === 1 && b[0][0] === "#") return [{ type: "color", hex: b[0], text: b[0] }];
    if (COLORS[joined]) return [{ type: "color", hex: COLORS[joined], text: joined }];
    if (WHITES[joined]) return [{ type: "color", kelvin: WHITES[joined], text: joined }];
    if (b.length === 2 && /^\d{4,5}$/.test(b[0]) && (b[1] === "k" || b[1] === "kelvin")) {
      const k = +b[0];
      return k >= 1500 && k <= 10000 ? [{ type: "color", kelvin: k, text: `${k} K (${whiteName(k)})` }] : [];
    }

    // brightness
    const c = b.filter(w => !["brightness", "bri", "level"].includes(w));
    const asked = c.length < b.length;
    if (asked || /^\d/.test(c[0] || "") || ["max", "full", "maximum", "dim", "dimmer", "brighter", "darker"].includes(c[0])) {
      if (c.length === 1 && ["max", "full", "maximum"].includes(c[0])) return [{ type: "brightness", value: 100 }];
      if (c.length === 1 && c[0] === "brighter") return [{ type: "relative", delta: 20, word: "brighter" }];
      if (c.length === 1 && c[0] === "darker") return [{ type: "relative", delta: -20, word: "darker" }];
      if (c.length === 1 && (c[0] === "dim" || c[0] === "dimmer")) return [{ type: "relative", delta: -20, word: "dim" }];
      const n = c.slice();
      if (n[0] === "dim" || n[0] === "dimmer") n.shift();
      if (n.length === 2 && n[1] === "percent") n.pop();
      if (n.length === 1 && /^\d+(\.\d+)?$/.test(n[0])) {
        const v = Math.round(parseFloat(n[0]));
        return v >= 1 && v <= 100 ? [{ type: "brightness", value: v }] : [];
      }
      return [];
    }

    // a look: by its name, or the start of it
    return effectsFor([joined, a.join(" ")], ctx).map(e => ({ type: "effect", effect: e.id, name: e.name }));
  }

  /* ---------- the lights and groups a command can name ---------- */

  /** Groups, lights, "PC" and "room": each with the words it goes by. */
  function entities(ctx) {
    const list = [];
    for (const g of ctx.groups || []) list.push({ kind: "group", name: g.name, ref: { kind: "group", name: g.name }, w: words(g.name) });
    for (const l of ctx.lights || []) list.push({ kind: "light", name: l.name, ref: { kind: "light", id: l.id }, w: words(l.name), group: l.group });
    for (const [scope, name] of [["pc", "PC"], ["room", "room"]])
      if ((ctx.lights || []).some(l => (l.group === "pc" ? "pc" : "room") === scope))
        list.push({ kind: "scope", name, ref: { kind: "scope", scope }, w: [scope] });
    return list.filter(e => e.w.length);
  }

  const same = (a, b) => a.length === b.length && a.every((w, i) => w === b[i]);

  /** How the words typed (a run of the text) name this thing: 0 all of its name, 1 the start of it, 2 words inside it, -1 not at all. */
  function tierOf(run, name) {
    if (same(run, name)) return 0;
    if (run.length >= name.length) return -1;
    if (same(run, name.slice(0, run.length))) return 1;
    for (let i = 1; i + run.length <= name.length; i++) if (same(run, name.slice(i, i + run.length))) return 2;
    return -1;
  }

  const targetText = (e, shown, cap) => e.kind === "scope" ? (e.ref.scope === "pc" ? "PC lights" : cap ? "Room lights" : "room lights") : shown;

  function describe(d, target, e, shown) {
    const named = !!e;
    const who = named ? targetText(e, shown, false) : null;
    switch (d.type) {
      case "color":
        return { label: `Set ${named ? who : "every light"} to ${d.text}`, action: { type: "color", target, hex: d.hex, kelvin: d.kelvin } };
      case "brightness":
        return { label: `Brightness ${d.value}% for ${named ? who : "every light that's on"}`, action: { type: "brightness", target, value: d.value } };
      case "relative": {
        const to = named ? who : "every light that's on";
        return { label: d.word === "dim" ? `Dim ${to}` : `Make ${to} ${d.word}`, action: { type: "relative", target, delta: d.delta } };
      }
      case "power":
        return { label: named ? `${targetText(e, shown, true)} ${d.on ? "on" : "off"}` : `All lights ${d.on ? "on" : "off"}`,
          action: { type: "power", target, on: d.on } };
      case "effect":
        return { label: `${d.name} on ${named ? who : "every light"}`, action: { type: "effect", target, effect: d.effect } };
    }
    return null;
  }

  /* ---------- the whole thing ---------- */

  function parse(text, ctx) {
    ctx = ctx || {};
    const t = tokenize(text);
    if (!t.length) return [];
    const plain = words(text).join(" ");
    const isPreset = (ctx.presets || []).some(p => words(p).join(" ") === plain);

    let out = null;
    for (const f of [sleepCommand, focusCommand, pauseCommand, showCommand]) {
      out = f(t, ctx);
      if (out) break;
    }
    if (!out) {
      out = lightCommands(t, ctx);
      // a preset's own name ("Red room") is the preset: not a color or a switch for some lights.
      // Another look with the same name ("Rainbow") may still follow it.
      if (isPreset) out = out.filter(r => r.action.type === "effect");
    }
    return out.slice(0, MAX);
  }

  function lightCommands(t, ctx) {
    // 1) a light, group, "pc" or "room" named somewhere in the words, and the rest says what to do to it
    const found = [];
    for (const e of entities(ctx)) {
      for (let i = 0; i < t.length; i++) for (let n = 1; i + n <= t.length; n++) {
        const run = t.slice(i, i + n), tier = tierOf(run, e.w);
        if (tier < 0) continue;
        if (tier > 0 && (run.join("").length < 3 || run.every(w => KEYWORDS.has(w)))) continue;   // (a loose match is a real name, not "on" or "red")
        let rest = [...t.slice(0, i), ...t.slice(i + n)];
        // "red on desk": the "on" that joins the two is not "on"
        if (i > 0 && t[i - 1] === "on" && rest.filter(w => !FILLER.has(w)).length > 1) rest = [...t.slice(0, i - 1), ...t.slice(i + n)];
        const ds = look(rest, ctx);
        if (ds.length) found.push({ e, tier, n, ds });
      }
    }
    if (found.length) {
      const bestTier = Math.min(...found.map(f => f.tier));
      const ofTier = found.filter(f => f.tier === bestTier);
      const bestLen = Math.max(...ofTier.map(f => f.n));
      const picked = [], seen = new Set();
      for (const f of ofTier) if (f.n === bestLen && !seen.has(f.e)) { seen.add(f.e); picked.push(f); }
      // two things with one name: say which (a group, a PC light, a room light), else number them
      const names = picked.map(f => f.e.name.toLowerCase());
      const shown = picked.map((f, i) => {
        if (names.filter(x => x === names[i]).length < 2) return f.e.name;
        return f.e.name + (f.e.kind === "group" ? " (group)" : f.e.kind === "light" ? (f.e.group === "pc" ? " (PC)" : " (room)") : "");
      });
      const again = {};
      for (const s of shown) again[s] = (again[s] || 0) + 1;
      const used = {};
      const results = [];
      picked.forEach((f, i) => {
        const label = again[shown[i]] > 1 ? `${shown[i]} ${(used[shown[i]] = (used[shown[i]] || 0) + 1)}` : shown[i];
        for (const d of f.ds) results.push(describe(d, f.e.ref, f.e, label));
      });
      return results.filter(Boolean);
    }
    // 2) every light that's on
    return look(t, ctx).map(d => describe(d, { kind: "every" }, null, "")).filter(Boolean);
  }

  return { parse, tokenize, words, minutesText };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Commands;
