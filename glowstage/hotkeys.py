"""Global hotkeys (off until switched on in Setup), from any program.

Every action has a key you can change (Setup → App → Change keys), or clear. Out of the box
they are, and stay, the same as always:

    Ctrl+Alt+Shift+L     all lights on / off
    Ctrl+Alt+Shift+S     the light show on / off
    Ctrl+Alt+Shift+1..9  the first nine presets
    Ctrl+Alt+Shift+Up    brighter (hold it to keep going)
    Ctrl+Alt+Shift+Down  darker
    Ctrl+Alt+Shift+N     the sleep timer: fade out in 30 minutes, or cancel it
    Ctrl+Alt+Shift+Z     undo the last big change (a preset, all on or off, the show)

More actions start without a key (the PC's or the room's lights, next and previous preset, the
focus timer, stop presets in turn, colors from the wallpaper), and any preset can get a key of
its own by name.

The defaults have three modifiers, so they don't take a shortcut from another program
(Ctrl+Alt+L, say, is a command in several editors, and a global hotkey would take it from them
everywhere). A key you choose yourself may have fewer, but never none (F13 to F24 are the
exception: they hardly exist on a keyboard, and macro keys often send them), and not the
combinations Windows and nearly every program already use (Ctrl+C, Alt+F4, ...).

What is saved is only what you changed: settings["hotkey_keys"] = {action: "Ctrl+Alt+L" or ""}
("" = no key). The rest is the default.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, WM_SYNC = 0x0312, 0x0012, 0x8001      # (WM_APP + 1: "register what's wanted now")
STEP = 10           # brightness, per press
PAUSE_LIMIT = 90    # seconds: paused hotkeys come back by themselves if nobody says "resume"

# ---- keys and combinations -----------------------------------------------------------------

MODIFIERS = (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN))
_MOD_NAMES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL, "alt": MOD_ALT,
              "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN}


def _key_table() -> dict[str, int]:
    keys = {chr(c): c for c in range(ord("A"), ord("Z") + 1)}
    keys.update({str(n): ord(str(n)) for n in range(10)})
    keys.update({f"F{n}": 0x6F + n for n in range(1, 25)})
    keys.update({"Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27, "Home": 0x24, "End": 0x23,
                 "PageUp": 0x21, "PageDown": 0x22, "Insert": 0x2D, "Delete": 0x2E, "Space": 0x20, "Tab": 0x09})
    keys.update({f"Num{n}": 0x60 + n for n in range(10)})
    keys.update({"Num*": 0x6A, "Num+": 0x6B, "Num-": 0x6D, "Num.": 0x6E, "Num/": 0x6F})
    return keys


KEY_VK = _key_table()
VK_NAME = {vk: name for name, vk in KEY_VK.items()}
_KEY_BY_LOWER = {name.lower(): vk for name, vk in KEY_VK.items()}
_KEY_BY_LOWER.update({"pgup": 0x21, "pgdn": 0x22, "pagedn": 0x22, "ins": 0x2D, "del": 0x2E,
                      "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27})
FREE_KEYS = {f"F{n}" for n in range(13, 25)}
_TYPING = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") | {"Space"}

# What Windows or nearly every program already does with these. A short list on purpose: a key
# another program has is found out when it is registered; these are the ones worth refusing up front.
VK_TAB = KEY_VK["Tab"]
RESERVED = (
    {(MOD_CONTROL, ord(k)) for k in "CVXZYAS"}
    | {(MOD_ALT, KEY_VK["F4"]), (MOD_ALT, VK_TAB), (MOD_ALT, KEY_VK["Space"]),
       (MOD_CONTROL | MOD_ALT, KEY_VK["Delete"])}
    | {(MOD_WIN, ord(k)) for k in "LDER"} | {(MOD_WIN, VK_TAB), (MOD_WIN, KEY_VK["Space"])}
)


def format_combo(mods: int, vk: int) -> str:
    """"Ctrl+Alt+Shift+L": the modifiers in a fixed order, then the key."""
    return "+".join([name for name, bit in MODIFIERS if mods & bit] + [VK_NAME.get(vk, f"0x{vk:02X}")])


def parse_combo(text: str, check: bool = True) -> tuple[int, int]:
    """(modifiers, virtual key) of "ctrl + alt + l" and the like. With `check` (the default) it
    also refuses what can't be a hotkey, with a message that says why in plain words."""
    parts = [p.strip() for p in str(text or "").strip().split("+")]
    if len(parts) >= 2 and parts[-1] == "" and parts[-2].lower() == "num":
        parts[-2:] = ["Num+"]                    # (the numpad plus sign is a key, not a separator)
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("Press a key combination")
    mods, vk = 0, None
    for p in parts:
        bit = _MOD_NAMES.get(p.lower())
        if bit:
            mods |= bit
        elif p.lower() in _KEY_BY_LOWER:
            if vk is not None:
                raise ValueError("Use just one key, with Ctrl, Alt, Shift or Windows")
            vk = _KEY_BY_LOWER[p.lower()]
        else:
            raise ValueError(f"Glowstage can't use the key {p}")
    if vk is None:
        raise ValueError("Add a key after Ctrl, Alt, Shift or Windows")
    if not check:
        return mods, vk
    name = VK_NAME[vk]
    if not mods and name not in FREE_KEYS:
        raise ValueError("Add Ctrl, Alt, Shift or Windows: a key on its own would stop working in "
                         "every program. (Only F13 to F24 can go without.)")
    if mods == MOD_SHIFT and name in _TYPING:
        raise ValueError("Shift on its own would take capital letters from every program: "
                         "add Ctrl, Alt or Windows too")
    if (mods, vk) in RESERVED:
        raise ValueError(f"{format_combo(mods, vk)} is already used by Windows or nearly every "
                         "program: choose another combination")
    return mods, vk


# ---- the actions -----------------------------------------------------------------------------

LABEL = "Ctrl+Alt+Shift+"
# (id, what it does, the default key; "" = none until you give it one)
ACTIONS = [
    ("lights", "All lights on or off", LABEL + "L"),
    ("pc_power", "The PC's lights on or off", ""),
    ("room_power", "The room's lights on or off", ""),
    ("show", "The light show on or off", LABEL + "S"),
    ("brighter", "Brighter (hold it to keep going)", LABEL + "Up"),
    ("darker", "Darker (hold it to keep going)", LABEL + "Down"),
    ("sleep", "Sleep timer: fade out in 30 minutes, or cancel it", LABEL + "N"),
    ("undo", "Undo", LABEL + "Z"),
    ("next_preset", "Next preset", ""),
    ("prev_preset", "Previous preset", ""),
    ("focus", "Focus timer: start it, or stop it", ""),
    ("stop_cycle", "Stop presets in turn", ""),
    ("wallpaper", "Colors from my wallpaper", ""),
    ("pause_auto", "Automatic looks: pause for an hour, or resume", ""),
]
POSITIONS = [(f"preset_{n}", f"Preset {n}", f"{LABEL}{n}") for n in range(1, 10)]
FIXED = {a[0]: a for a in ACTIONS + POSITIONS}
DEFAULTS = {a[0]: a[2] for a in ACTIONS + POSITIONS}
REPEATING = {"brighter", "darker"}
NAMED = "preset:"       # an action of its own for one preset: "preset:Cozy"


def named(name: str) -> str:
    return NAMED + name


def keys_from(settings: dict, preset_names) -> dict[str, str]:
    """Every action's key as it counts now: the default, or what was chosen. A key that can't
    be read (a hand-edited file) or that another action already has counts as no key."""
    over = settings.get("hotkey_keys")
    over = over if isinstance(over, dict) else {}
    ids = [a[0] for a in ACTIONS] + [a[0] for a in POSITIONS] \
        + [k for k in over if k.startswith(NAMED) and k[len(NAMED):] in preset_names]
    out, used = {}, set()
    for action in ids:
        raw = over.get(action, DEFAULTS.get(action, ""))
        out[action] = ""
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            combo = parse_combo(raw)
        except ValueError as e:
            log.warning("Hotkey %s: %r is not usable (%s)", action, raw, e)
            continue
        if combo not in used:
            used.add(combo)
            out[action] = format_combo(*combo)
    return out


def rename_preset_key(settings: dict, old: str, new: str) -> None:
    """A preset got a new name: its key follows it (manager.rename_preset)."""
    over = settings.get("hotkey_keys")
    if isinstance(over, dict) and named(old) in over:
        over[named(new)] = over.pop(named(old))


def forget_preset_key(settings: dict, name: str) -> None:
    """A preset was deleted: its key goes with it (manager.delete_preset)."""
    over = settings.get("hotkey_keys")
    if isinstance(over, dict):
        over.pop(named(name), None)


def summary(settings: dict, preset_names, taken=()) -> str:
    """For the problem report: "on (16 keys set)", and which ones another program has."""
    keys = keys_from(settings, preset_names)
    text = f"{'on' if settings.get('hotkeys') else 'off'} ({sum(1 for k in keys.values() if k)} keys set"
    return text + (f"; another program has {', '.join(taken)}" if taken else "") + ")"


class Hotkeys:
    def __init__(self, manager):
        self.manager = manager
        self.failed: list[str] = []          # keys another program already has
        self.taken: dict[str, str] = {}      # the same, per action: {action: key}
        self.paused = False                  # while the key dialog is open: nothing registered
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._lock = threading.Lock()
        self._registered: dict[int, tuple] = {}     # id -> (action, modifiers, key, repeats); the hotkey thread's own
        self._applied: tuple | None = None          # what the thread last registered: (paused, wanted)
        self._synced = threading.Event()
        self._next_id = 1
        self._deadman: threading.Timer | None = None

    # ---- what the keys are ----------------------------------------------------------------

    @property
    def _settings(self) -> dict:
        return self.manager.config["settings"]

    def keys(self) -> dict[str, str]:
        return keys_from(self._settings, self.manager.preset_names())

    def actions(self) -> list[dict]:
        """Every action with its key, for the key dialog: id, what it does, key, default key,
        whether another program has it, and which part of the list it belongs in."""
        keys, names = self.keys(), self.manager.preset_names()
        out = [{"id": a, "label": text, "key": keys[a], "default": default,
                "taken": a in self.taken, "section": "actions"} for a, text, default in ACTIONS]
        for n, (a, text, default) in enumerate(POSITIONS):
            out.append({"id": a, "label": text, "key": keys[a], "default": default,
                        "detail": names[n] if n < len(names) else "", "taken": a in self.taken,
                        "section": "positions"})
        out += [{"id": a, "label": a[len(NAMED):], "key": key, "default": "", "taken": a in self.taken,
                 "section": "named"} for a, key in keys.items() if a.startswith(NAMED)]
        return out

    def label(self, action: str) -> str:
        if action.startswith(NAMED):
            return action[len(NAMED):]
        return FIXED[action][1] if action in FIXED else action

    def _known(self, action: str) -> None:
        if action in FIXED:
            return
        if action.startswith(NAMED) and action[len(NAMED):] in self.manager.preset_names():
            return
        raise ValueError("There's no such action")

    def _store(self, action: str, key: str) -> None:
        over = self._settings.setdefault("hotkey_keys", {})
        if action in DEFAULTS and key == DEFAULTS[action]:
            over.pop(action, None)           # (back to the default: nothing to remember)
        else:
            over[action] = key
        self.taken.pop(action, None)         # (not known any more until it's registered again)
        self.failed = list(self.taken.values())
        self._saved()

    def _saved(self) -> None:
        self.manager.config.save_soon()
        self.manager._changed()              # (the window refreshes, and App has the keys registered again)
        self.sync()

    def set_key(self, action: str, text: str) -> str:
        """Give an action a key: the key as it is kept. Refuses what can't be a hotkey and a key
        another action already has."""
        self._known(action)
        key = format_combo(*parse_combo(text))
        for other, k in self.keys().items():
            if other != action and k == key:
                raise ValueError(f"{key} is already the key for {self.label(other)}: clear that one first")
        self._store(action, key)
        return key

    def clear_key(self, action: str) -> None:
        self._known(action)
        self._store(action, "")

    def add_preset(self, name: str) -> None:
        """A row for a preset of its own (it gets its key next)."""
        if name not in self.manager.preset_names():
            raise ValueError("There's no such preset")
        self._settings.setdefault("hotkey_keys", {}).setdefault(named(name), "")
        self._saved()

    def remove_preset(self, name: str) -> None:
        forget_preset_key(self._settings, name)
        self.taken.pop(named(name), None)
        self.failed = list(self.taken.values())
        self._saved()

    def reset(self) -> None:
        """Every action back to its default key; the keys for presets by name are gone."""
        self._settings["hotkey_keys"] = {}
        self.taken, self.failed = {}, []
        self._saved()

    # ---- registering ----------------------------------------------------------------------

    def _wanted(self) -> list[tuple]:
        return [(a, *parse_combo(k), a in REPEATING) for a, k in self.keys().items() if k]

    def _api_register(self, ident: int, mods: int, vk: int) -> bool:
        return bool(ctypes.windll.user32.RegisterHotKey(None, ident, mods, vk))

    def _api_unregister(self, ident: int) -> None:
        ctypes.windll.user32.UnregisterHotKey(None, ident)

    def _sync_now(self) -> None:
        """Make the registered hotkeys what's wanted now: drop what went, add what's new. Only
        on the hotkey thread (a hotkey belongs to the thread that registered it)."""
        want = [] if self.paused else self._wanted()
        self._applied = (self.paused, tuple(want))
        for ident, spec in list(self._registered.items()):
            if spec not in want:
                self._api_unregister(ident)
                del self._registered[ident]
        have = set(self._registered.values())
        failed = {}
        for spec in want:
            action, mods, vk, repeat = spec
            if spec in have:
                continue
            ident, self._next_id = self._next_id, self._next_id + 1
            if self._api_register(ident, mods if repeat else mods | MOD_NOREPEAT, vk):
                self._registered[ident] = spec
            else:
                failed[action] = format_combo(mods, vk)     # (tried again at the next sync)
        if self.paused:
            return                            # (what another program has stays as it was found)
        if failed != self.taken:
            if failed:
                log.warning("Hotkeys another program already has: %s", ", ".join(failed.values()))
            self.taken, self.failed = failed, list(failed.values())

    def sync(self, wait: bool = True) -> None:
        """The registration follows the keys (they changed, a preset was renamed or deleted, the
        dialog paused or resumed them). It happens on the hotkey thread; with `wait` this returns
        when it's done. Nothing to do while hotkeys are off or nothing changed."""
        if self._thread is None:
            return
        try:
            if self._applied == (self.paused, tuple(self._wanted())):
                return
        except Exception:
            log.exception("Hotkeys: could not work out the keys")
            return
        self._synced.clear()
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_SYNC, 0, 0)
        if wait:
            self._synced.wait(2.0)

    def pause(self) -> None:
        """While the key dialog is open: the app's own hotkeys would eat the keys before the window
        sees them. They come back by themselves after a while if nobody resumes (a window that was
        reloaded, say)."""
        self.paused = True
        if self._deadman:
            self._deadman.cancel()
        self._deadman = threading.Timer(PAUSE_LIMIT, self.resume)
        self._deadman.daemon = True
        self._deadman.start()
        self.sync()

    def resume(self) -> None:
        if self._deadman:
            self._deadman.cancel()
            self._deadman = None
        self.paused = False
        self.sync()

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            if on and self._thread is None:
                ready = threading.Event()
                self._thread = threading.Thread(target=self._run, args=(ready,), name="hotkeys", daemon=True)
                self._thread.start()
                ready.wait(2.0)
            elif not on and self._thread is not None:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                self._thread.join(2.0)
                self._thread, self.failed, self.taken, self._applied = None, [], {}, None

    def _run(self, ready: threading.Event) -> None:
        # A hotkey belongs to the thread that registered it: register, listen and unregister here.
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0x0400, 0x0400, 0)    # (this makes the thread's message queue)
        try:
            self._sync_now()
        except Exception:
            log.exception("Hotkeys: could not register")
        ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    spec = self._registered.get(msg.wParam)
                    if spec:
                        self._pressed(spec[0])
                elif msg.message == WM_SYNC:
                    try:
                        self._sync_now()
                    except Exception:
                        log.exception("Hotkeys: could not register")
                    self._synced.set()
        finally:
            for ident in list(self._registered):
                self._api_unregister(ident)
            self._registered.clear()

    # ---- what a key does -------------------------------------------------------------------

    # (the first version named its keys by their last key: "L", "Up", "2")
    _OLD_NAMES = {"L": "lights", "S": "show", "Up": "brighter", "Down": "darker", "N": "sleep", "Z": "undo"}

    def _pressed(self, action: str) -> None:
        m = self.manager
        action = self._OLD_NAMES.get(action) or (f"preset_{action}" if action.isdigit() else action)
        try:
            if action == "lights":
                m.remember()
                m.set_power_all(not m.any_on())
            elif action in ("pc_power", "room_power"):
                group = action[:-6]
                m.remember()
                m.set_power_group(group, not m.group_any_on(group))
            elif action == "show":
                m.remember()
                m.toggle_show()
            elif action in ("brighter", "darker"):
                m.nudge_brightness(STEP if action == "brighter" else -STEP)
            elif action == "sleep":
                if m.sleep.active:
                    m.sleep.cancel()
                else:
                    try:
                        m.sleep.start(30)
                    except ValueError:
                        pass            # no lights on: nothing to fade
            elif action == "undo":
                m.undo()
            elif action in ("next_preset", "prev_preset"):
                m.step_preset(1 if action == "next_preset" else -1)
            elif action == "focus":
                if m.focus.active:
                    m.focus.cancel()
                else:
                    try:
                        m.remember()
                        m.focus.start_last()
                    except ValueError:
                        pass            # no lights on: nothing to show a break with
            elif action == "stop_cycle":
                m.cycle.cancel()
            elif action == "pause_auto":
                if m.automations.paused_until():
                    m.automations.resume()
                else:
                    m.automations.pause(60)
            elif action == "wallpaper":
                try:
                    m.light_from_wallpaper()
                except ValueError as e:
                    log.info("Hotkey: colors from the wallpaper: %s", e)
            elif action.startswith("preset_"):
                names = m.preset_names()
                n = int(action[7:]) - 1
                if n < len(names):
                    m.remember()
                    m.apply_preset(names[n])
            elif action.startswith(NAMED):
                name = action[len(NAMED):]
                if name in m.preset_names():
                    m.remember()
                    m.apply_preset(name)
        except Exception:
            log.exception("Hotkey %s failed", action)
