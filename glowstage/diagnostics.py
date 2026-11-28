"""Report a problem (Setup → App): a short summary of this Glowstage for a bug report, which
you see in full before anything leaves the PC, and a GitHub issue with it filled in.

It holds the version, Windows, each light (its name, kind, and whether it answers), how
OpenRGB is doing, which automatic things are on, and the last warnings from the log. Never a
key or a password: not the phone remote's, not CS2's, not a light's token.
"""

from __future__ import annotations

import platform
import re
import sys
from urllib.parse import urlencode

from . import hotkeys
from .paths import APP_DIR, FROZEN, REPO, VERSION

LOG = APP_DIR / "glowstage.log"
PROBLEM_LINES = 25
MAX_URL_TEXT = 5000          # GitHub refuses links much longer than ~8000 characters


def windows() -> str:
    try:
        v = sys.getwindowsversion()
        name = "Windows 11" if v.major == 10 and v.build >= 22000 else f"Windows {platform.release()}"
        return f"{name} (build {v.build})"
    except AttributeError:
        return platform.platform()


def recent_problems(path=None, lines: int = PROBLEM_LINES) -> list[str]:
    """The last warnings and errors in the log, one line each, newest last."""
    try:
        text = (path or LOG).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = [ln for ln in text if re.match(r"^\d{4}-\d\d-\d\d [\d:,]+ (WARNING|ERROR|CRITICAL) ", ln)]
    return [ln[:240] for ln in found[-lines:]]


def report(app) -> str:
    m = app.manager
    s = app.config["settings"]
    devices = [d.describe() for d in list(m.devices.values()) if d.id not in set(app.config["hidden"])]
    online = sum(1 for d in devices if d["online"])
    out = [f"Glowstage {VERSION} ({'app' if FROZEN else 'from source'}) on {windows()}",
           f"Lights: {len(devices)} ({online} answering)"]
    for d in sorted(devices, key=lambda d: (d["group"], d["kind"], d["name"])):
        state = "answering" if d["online"] else f"not answering: {d['error'] or 'no reason given'}"
        out.append(f"- {d['name']} ({d['kind']}, {d['group']}, {d['pixels']} LEDs): {state}")
    shown = {d["id"] for d in devices}
    balanced = [f"{m.devices[i].name} ({b['warmth']:+d} warmth, {b['max']}% at full)"
                for i in sorted(shown) if i in m.devices and i in (app.config.data.get("balance") or {})
                for b in [m.balance(i)]]
    if balanced:
        out.append("Balanced lights: " + ", ".join(balanced))
    ordered = [f"{m.devices[i].name} ({m.balance(i)['order']})"
               for i in sorted(shown) if i in m.devices and m.balance(i)["order"] != "RGB"]
    if ordered:
        out.append("Lights with a color order: " + ", ".join(ordered))
    o = m.openrgb.describe()
    out.append(f"OpenRGB: {o.get('status') or '?'}" + (f" ({o['message']})" if o.get("message") else "")
               + (", as administrator" if o.get("elevated") else ""))
    apps = len(app.config.data.get("app_looks") or [])
    out.append("Automatic: " + ", ".join([
        f"games {s.get('auto_game') or 'off'}", f"videos {s.get('auto_video') or 'off'}",
        f"music {'on' if s.get('auto_show') else 'off'}", f"calls {s.get('auto_call') or 'off'}", f"away {s.get('away_lights') or 'off'}",
        f"apps {apps}", f"CS2 {'connected' if m.games.settings.get('cs2') else 'off'}",
        f"League of Legends {'on' if m.games.settings.get('lol') else 'off'}",
        f"Dota 2 {'connected' if m.games.settings.get('dota') else 'off'}",
        f"Rocket League {'connected' if m.games.settings.get('rl') else 'off'}", f"OBS {m.obs.summary()}",
        f"OBS look {s.get('auto_live') or 'off'}"]))
    keys = getattr(app, "hotkeys", None)      # (the keys another program already has: which actions)
    taken = [f"{keys.label(a)} {k}" for a, k in keys.taken.items()] if keys else []
    out.append("Also: " + ", ".join([
        f"phone remote {'on' if app.remote.settings.get('on') else 'off'}",
        f"hotkeys {hotkeys.summary(s, m.preset_names(), taken)}", f"presets in turn {'on' if m.cycle.active else 'off'}", f"focus timer {'on' if m.focus.active else 'off'}", f"following the wallpaper {'on' if m.wallpaper.active else 'off'}", f"automatic looks {'paused' if m.automations.paused_until() else 'on'}", f"night limit {m.night.describe()['max']}% now" if m.night.applied is not None else f"night limit {'on' if m.night.settings['on'] else 'off'}", f"schedules {len(app.config.data.get('schedules') or [])}",
        f"sound {m.engine.audio.device or 'not listening now'}"]))
    problems = recent_problems()
    out.append("Last warnings in the log:" if problems else "No warnings in the log.")
    out += problems
    return "\n".join(out)


def issue_url(text: str) -> str:
    """A new "Something doesn't work" issue with the version, the lights and the summary filled in."""
    if len(text) > MAX_URL_TEXT:
        head, _, tail = text.partition("Last warnings in the log:")
        keep = MAX_URL_TEXT - len(head) - 60
        text = head + "Last warnings in the log (the newest):\n" + tail[-keep:].split("\n", 1)[-1] if keep > 0 else head[:MAX_URL_TEXT]
    names = [ln[2:].split(" (")[0] for ln in text.splitlines() if ln.startswith("- ")]
    lights = ", ".join(names)[:300]
    query = urlencode({"template": "bug_report.yml", "version": VERSION, "lights": lights,
                       "logs": "```\n" + text + "\n```"})
    return f"https://github.com/{REPO}/issues/new?{query}"
