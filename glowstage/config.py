"""Persistent settings, devices, last states and presets in %APPDATA%\\Glowstage\\config.json."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path

from .paths import APP_DIR

log = logging.getLogger(__name__)

CONFIG_PATH = APP_DIR / "config.json"

# A preset sets every light the same ("all"), the PC's lights and the room's lights each their own
# way ("pc", "room", falling back to "all"), or each light as it was saved ("states": id -> state).
DEFAULT_PRESETS = [
    {"name": "Rainbow", "all": {"on": True, "brightness": 100, "mode": "effect", "effect": "rainbow", "speed": 40}},
    {"name": "Warm white", "all": {"on": True, "brightness": 70, "mode": "color", "color": "#ffb46b"}},
    {"name": "Follow the sun", "room": {"on": True, "brightness": 100, "mode": "effect", "effect": "daylight"},
                               "pc": {"on": True, "brightness": 35, "mode": "effect", "effect": "daylight"}},
    {"name": "Focus", "room": {"on": True, "brightness": 95, "mode": "color", "color": "#eaf1ff"},
                      "pc": {"on": True, "brightness": 30, "mode": "color", "color": "#eaf1ff"}},
    {"name": "Reading", "room": {"on": True, "brightness": 90, "mode": "color", "color": "#ffdcb0"},
                        "pc": {"on": True, "brightness": 12, "mode": "color", "color": "#ffb46b"}},
    {"name": "Cozy", "room": {"on": True, "brightness": 45, "mode": "color", "color": "#ff8f3f"},
                     "pc": {"on": True, "brightness": 35, "mode": "effect", "effect": "breathing",
                            "color": "#ff6a1a", "speed": 12}},
    {"name": "Gaming", "pc": {"on": True, "brightness": 100, "mode": "effect", "effect": "rainbow", "speed": 55},
                       "room": {"on": True, "brightness": 55, "mode": "effect", "effect": "wave",
                                "color": "#3a00ff", "color2": "#00d0ff", "speed": 30}},
    {"name": "Chill", "all": {"on": True, "brightness": 60, "mode": "effect", "effect": "wave",
                              "color": "#5b2bff", "color2": "#00b8ff", "speed": 22}},
    {"name": "Cyberpunk", "all": {"on": True, "brightness": 100, "mode": "effect", "effect": "wave",
                                  "color": "#ff0090", "color2": "#00f0ff", "speed": 45}},
    {"name": "Movie", "pc": {"on": True, "brightness": 70, "mode": "effect", "effect": "screen"},
                      "room": {"on": True, "brightness": 35, "mode": "effect", "effect": "screen"}},
    {"name": "Late night", "all": {"on": True, "brightness": 45, "mode": "effect", "effect": "wave",
                                   "color": "#2a3cff", "color2": "#8a2cff", "speed": 15}},
    {"name": "Festival", "all": {"on": True, "brightness": 100, "mode": "effect", "effect": "music_show",
                                 "speed": 70}},
    {"name": "Party", "all": {"on": True, "brightness": 100, "mode": "effect", "effect": "music_party",
                              "speed": 60}},
    {"name": "Club", "all": {"on": True, "brightness": 100, "mode": "effect", "effect": "music_ripple",
                             "speed": 55}},
    {"name": "Slow jam", "all": {"on": True, "brightness": 70, "mode": "effect", "effect": "music_romantic",
                                 "color": "#0a1aff", "color2": "#b0002e", "speed": 35}},
]

# Built-in presets as earlier versions had them: a preset still exactly like this is replaced by
# its new version on upgrade (one the user changed or saved over is left alone).
OLD_PRESETS = {
    "Focus": {"name": "Focus", "all": {"on": True, "brightness": 90, "mode": "color", "color": "#eaf1ff"}},
    "Movie": {"name": "Movie", "all": {"on": True, "brightness": 70, "mode": "effect", "effect": "screen"}},
}
PRESETS_VERSION = 3


def upgrade_presets(presets: list[dict]) -> list[dict]:
    """Bring the built-in presets of an older install up to date: untouched old ones become their
    new version, new ones are added after the built-in one before them; the user's own stay."""
    out = [copy.deepcopy(next((d for d in DEFAULT_PRESETS if d["name"] == p.get("name")), p))
           if OLD_PRESETS.get(p.get("name")) == p else p for p in presets]
    names = {p.get("name") for p in out}
    for i, d in enumerate(DEFAULT_PRESETS):
        if d["name"] in names:
            continue
        before = next((j for j, p in enumerate(out) if i and p.get("name") == DEFAULT_PRESETS[i - 1]["name"]), None)
        out.insert(0 if i == 0 else len(out) if before is None else before + 1, copy.deepcopy(d))
        names.add(d["name"])
    return out


DEFAULTS = {
    "version": 1,
    "devices": {},      # network lights: id -> {kind, host, port, token}
    "names": {},        # custom display names: id -> name
    "hidden": [],       # device ids hidden from the UI
    "states": {},       # id -> LightState dict (restored on launch)
    "presets": DEFAULT_PRESETS,
    "openrgb": {"auto_launch": True, "elevated": False, "path": "", "port": 6742, "zone_sizes": {}},
    "show_layout": {},  # id -> [start, end, "front"|"back"]: where a light sits on the stage
    "show_sync": {},      # sound output name -> ms the music lights wait for the sound ("" = any output)
    "show_excluded": [],  # ids of lights that keep their own look while the light show runs
    "online": {},         # what online.py last heard from GitHub (latest version, support links)
    "schedules": [],      # [{"time": "23:00", "days": [0..6] (Monday = 0), "action": "off" | "on" | "show"
                          #   | "preset:<name>", "enabled": true}]
    "games": {"cs2": False, "port": 0, "token": ""},   # game lighting (games.py): connected, where it listens
    "settings": {"close_to_tray": True, "start_minimized": False, "restore_on_launch": True,
                 "lights_on_at_startup": True, "pc_lights_off_at_shutdown": True,
                 "room_lights_off_at_shutdown": True, "show_strobe": True,
                 "setup_done": False,    # the welcome tour ran (or was skipped)
                 "active_preset": "",    # the preset the lights show now ("" once changed by hand)
                 "hotkeys": False,       # Ctrl+Alt+Shift+L/S/1-9 from any program (see hotkeys.py)
                 "check_updates": True,  # ask GitHub once a day for a new version and the support links
                 "auto_show": False,     # start the light show by itself when music plays (automations.py)
                 "auto_game": "",        # a game in full screen: "", "screen" or "preset:<name>"
                 "auto_video": "",       # a video in full screen: the same
                 "away_lights": "",      # locked, screen off or asleep: "" nothing, "pc" or "all" off (away.py)
                 "first_run": "",        # the day Glowstage first ran (YYYY-MM-DD)
                 "support_asked": "",    # "", "later:YYYY-MM-DD", "never" or "done" (the support card)
                 "presets_version": PRESETS_VERSION},
}


def _merge(defaults: dict, data: dict) -> dict:
    out = copy.deepcopy(defaults)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("devices", "names", "states"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.lock = threading.RLock()
        self._timer: threading.Timer | None = None
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("Could not read %s (%s); starting fresh", path, e)
                path.replace(path.with_suffix(".broken.json"))
        if data and "setup_done" not in data.get("settings", {}):
            # Someone who already used the app (or the older RGB Control) doesn't need the welcome tour.
            data.setdefault("settings", {})["setup_done"] = True
        old_presets = bool(data) and data.get("settings", {}).get("presets_version", 1) < PRESETS_VERSION
        self.data = _merge(DEFAULTS, data)
        if old_presets:
            self.data["presets"] = upgrade_presets(self.data["presets"])
            self.data["settings"]["presets_version"] = PRESETS_VERSION
            self.save_soon()

    def __getitem__(self, key):
        return self.data[key]

    def save_soon(self, delay: float = 0.8) -> None:
        with self.lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self.save_now)
            self._timer.daemon = True
            self._timer.start()

    def save_now(self) -> None:
        with self.lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
