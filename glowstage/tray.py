"""System tray icon: open the window, switch everything on/off, the light show, presets, the
sleep timer, quit."""

from __future__ import annotations

import logging
import threading
import time

import pystray
from pystray import Menu, MenuItem

from .colors import hex_to_rgb
from .icon import icon_image
from .paths import APP_NAME
from .timer import CHOICES

log = logging.getLogger(__name__)

WARM_WHITE = (255, 236, 205)
ICON_STEP = 16           # colors closer than this look the same in the tray
ICON_INTERVAL = 1.0      # the icon changes at most once a second


def _quantize(rgb) -> tuple:
    return tuple(min(255, int(round(v / ICON_STEP)) * ICON_STEP) for v in rgb)


def _lit(states, ids):
    """The lights that really give light: switched on, not at 0 %, and not hidden/offline."""
    return [st for i, st in states.items() if (ids is None or i in ids) and st.on and st.brightness > 0]


def tray_look(states, ids=None):
    """What the tray icon should look like: "off", "rainbow" or an (r, g, b) tint.
    `states` maps light id -> LightState; `ids` limits it to the lights that count (shown + answering)."""
    lit = _lit(states, ids)
    if not lit:
        return "off"
    moving = sum(1 for st in lit if st.mode in ("effect", "native"))
    if moving * 2 > len(lit):
        return "rainbow"
    total = [0.0, 0.0, 0.0]
    weight = 0.0
    for st in lit:
        if st.mode != "color":
            continue
        try:
            rgb = hex_to_rgb(st.color)
        except Exception:
            continue
        for k in range(3):
            total[k] += rgb[k] * st.brightness
        weight += st.brightness
    if not weight:
        return "rainbow"
    avg = [v / weight for v in total]
    top = max(avg)
    if top <= 0:
        return "off"
    if top - min(avg) < 0.12 * top:       # white-ish: keep it a warm white
        return _quantize(WARM_WHITE)
    # bright enough to see: the strongest channel goes (almost) to full
    return _quantize(tuple(v * 245 / top for v in avg))


def tray_title(states, ids=None) -> str:
    lit = _lit(states, ids)
    if not lit:
        return f"{APP_NAME} \u2014 lights off"
    if sum(1 for st in lit if st.mode == "effect" and st.effect == "music_show") * 2 > len(lit):
        return f"{APP_NAME} \u2014 light show"
    total = len(states) if ids is None else len([i for i in states if i in ids])
    return f"{APP_NAME} \u2014 {len(lit)} of {total} lights on"


class LookUpdater:
    """Keeps the tray icon in step with the lights, but only when the look really changes and at
    most once a second (a change that comes too soon is applied a moment later)."""

    def __init__(self, compute, apply, clock=time.monotonic, schedule=None, interval: float = ICON_INTERVAL):
        self.compute = compute        # () -> (look, title)
        self.apply = apply            # (look, title) -> None
        self.clock = clock
        self.schedule = schedule or self._timer
        self.interval = interval
        self.shown = None             # (look, title) on the icon now
        self.last = None              # when it last changed
        self.pending = False
        self.lock = threading.Lock()

    @staticmethod
    def _timer(delay, fn):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()

    def poke(self) -> None:
        """Call this on every change of the lights; it decides if the icon needs a new look."""
        try:
            now_look = self.compute()   # (outside our lock: it takes the manager's)
        except Exception:
            log.exception("tray icon: could not read the lights")
            return
        with self.lock:
            if now_look == self.shown:
                return
            wait = 0.0 if self.last is None else self.last + self.interval - self.clock()
            if wait > 0:
                if not self.pending:
                    self.pending = True
                    self.schedule(wait, self._later)
                return
            self._show(now_look)

    def _later(self) -> None:
        with self.lock:
            self.pending = False
        self.poke()

    def _show(self, look) -> None:
        self.shown = look
        self.last = self.clock()
        try:
            self.apply(*look)
        except Exception:
            log.exception("tray icon: could not draw the new icon")


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = pystray.Icon(APP_NAME, icon_image(64), APP_NAME, menu=Menu(self._items))
        self.looks = LookUpdater(self._current_look, self._set_look)
        self.looks.shown = ("rainbow", APP_NAME)   # (what the icon starts as)
        # The icon is worked out and drawn on its own thread: never on the one that just changed
        # the lights (a new color takes a moment to draw, and a slider drag mustn't wait for it).
        self._look_due = threading.Event()
        self._look_stop = threading.Event()

    def _items(self):
        m = self.app.manager
        yield MenuItem(f"Open {APP_NAME}", lambda: self.app.show_window(), default=True)
        yield Menu.SEPARATOR
        yield MenuItem("All lights on", lambda: (m.remember(), m.set_power_all(True)))
        yield MenuItem("All lights off", lambda: (m.remember(), m.set_power_all(False)))
        yield MenuItem("Brightness", Menu(*self._brightness_items()), enabled=lambda item: bool(m.brightness_on()))
        groups = m.groups_present()
        if groups:
            yield MenuItem("Lights", Menu(*self._group_items(groups)))
        yield MenuItem("Stop the light show" if m.show_running() else "Start the light show",
                       lambda: (m.remember(), m.toggle_show()))
        presets = m.preset_names()
        if presets:
            active = m.config["settings"].get("active_preset", "")
            yield MenuItem("Presets", Menu(*(MenuItem(name, self._preset_action(name), radio=True,
                                                      checked=lambda item, n=name: n == active)
                                             for name in presets)))
        yield MenuItem("Sleep timer", Menu(*self._sleep_items()))
        if m.cycle.active:
            yield MenuItem("Stop presets in turn", lambda: m.cycle.cancel())
        if m.focus.active:
            yield MenuItem("Stop focus timer", lambda: m.focus.cancel())
        else:
            yield MenuItem("Start focus timer", self._focus_start)
        if m.wallpaper.active:
            yield MenuItem("Stop following my wallpaper", lambda: m.wallpaper.cancel())
        yield MenuItem("Undo", lambda: m.undo(), enabled=lambda item: m.can_undo())
        yield Menu.SEPARATOR
        yield MenuItem(f"Restart {APP_NAME}", lambda: self.app.restart())
        yield MenuItem("Quit", lambda: self.app.quit())

    def _brightness_items(self):
        m = self.app.manager
        for value in (100, 75, 50, 25, 10):
            yield MenuItem(f"{value}%", self._brightness_action(value), radio=True,
                           checked=lambda item, v=value: m.brightness_on() == (v,))

    def _brightness_action(self, value: int):
        m = self.app.manager
        return lambda: (m.remember(), m.set_brightness_on(value))

    def _group_items(self, groups):
        for group, label in (("pc", "PC"), ("room", "Room")):
            if group in groups:
                for on in (True, False):
                    yield MenuItem(f"{label} lights {'on' if on else 'off'}", self._group_action(group, on))

    def _group_action(self, group: str, on: bool):
        m = self.app.manager
        return lambda: (m.remember(), m.set_power_group(group, on))

    def _sleep_items(self):
        sleep = self.app.manager.sleep
        if sleep.active:
            yield MenuItem(f"Off at {time.strftime('%H:%M', time.localtime(sleep.until))}: cancel",
                           lambda: sleep.cancel())
            yield Menu.SEPARATOR
        for minutes in CHOICES:
            label = {60: "1 hour", 90: "1½ hours"}.get(minutes, f"{minutes} minutes")
            yield MenuItem(f"Fade out in {label}", self._sleep_action(minutes))

    def _sleep_action(self, minutes: int):
        def start():
            try:
                self.app.manager.sleep.start(minutes)
            except ValueError:
                pass   # no lights on: nothing to fade
        return start

    def _focus_start(self) -> None:
        """The focus timer with the choice you made last time."""
        m = self.app.manager
        try:
            m.remember()
            m.focus.start_last()
        except ValueError:
            pass   # no lights on: nothing to show a break with

    def _preset_action(self, name: str):
        m = self.app.manager
        return lambda: (m.remember(), m.apply_preset(name))  # pystray wants 0-2 positional args

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        threading.Thread(target=self._look_worker, name="tray-icon", daemon=True).start()

    def _look_worker(self) -> None:
        while not self._look_stop.is_set():
            self._look_due.wait()
            self._look_due.clear()
            if not self._look_stop.is_set():
                self.looks.poke()

    def _current_look(self):
        m = self.app.manager
        with m.lock:
            ids = {i for i in m.visible_ids() if m.devices[i].online}
            states = {i: m.states[i] for i in ids if i in m.states}
            return tray_look(states), tray_title(states)

    def _set_look(self, look, title) -> None:
        if look == "off":
            image = icon_image(64, off=True)
        elif look == "rainbow":
            image = icon_image(64)
        else:
            image = icon_image(64, tint=look)
        self.icon.icon = image
        self.icon.title = title

    def refresh_look(self) -> None:
        self._look_due.set()          # (the tray-icon thread does the rest)

    def refresh(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        self._look_stop.set()
        self._look_due.set()
        try:
            self.icon.stop()
        except Exception:
            pass
