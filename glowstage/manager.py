"""Owns every device, its desired state and the presets, and runs the background threads."""

from __future__ import annotations

import copy
import datetime
import logging
import re
import threading
import time
from typing import Callable

from . import genre_install
from .config import Config
from .devices import asrock_gpu, qmk_keyboard
from .automations import Automations, clean_app_looks
from .away import Away
from .cycle import PresetCycle
from .focus import FocusTimer
from . import hotkeys, wallpaper as wallpaper_look
from .wallpaper import WallpaperFollow
from .timer import SleepTimer
from .wake import WakeUp, wake_minutes
from .games import Games
from .devices.base import MAX_FLOOR, WARMTH_RANGE, Device
from .devices.discovery import Discovery
from .devices.hub import Hub, NotPairedError
from .devices.openrgb_dev import OpenRGBProvider
from .devices.registry import Found, drivers
from .colors import RGB, hex_to_rgb, scale
from .effects import EFFECTS, _rgb, blend
from .engine import EffectEngine
from .show import SLOW_FPS, stage_spans
from .sun import SUN
from .state import LightState

log = logging.getLogger(__name__)

BLUETOOTH_SYNC_MS = 200          # a typical Bluetooth delay (SBC/AAC: 150-250 ms)
SYNC_MIN_MS, SYNC_MAX_MS = -100, 400


def is_network(dev: Device) -> bool:
    """A light the app reaches over the network (any brand in the driver registry)."""
    return dev.kind in drivers()


def friendly_error(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    if "timed out" in text or isinstance(exc, TimeoutError):
        return "Not responding"
    if "refused" in text.lower() or "unreachable" in text.lower() or "No route" in text:
        return "Unreachable"
    return text[:120]


class DeviceWorker:
    """Applies the latest desired state to one device, dropping intermediate ones (slider drags).

    On lights that can take many quick updates (`device.fades`: OpenRGB, the keyboard, DMX: local,
    a frame in a few milliseconds), a new color, a new brightness, switching on or off glides over
    FADE seconds instead of jumping: a preset slides into place. A change that comes right after
    another one (a slider or the color wheel being dragged) is shown at once, so the light keeps up
    with your hand; so is anything submitted with instant=True (Find a light's flashes). Effects
    and the lights' own effects are never faded: they move by themselves."""

    FADE = 0.35        # seconds a change glides over
    QUICK = 0.25       # a change this soon after the last one is part of a drag: no glide

    def __init__(self, device: Device, on_error):
        self.device = device
        self.on_error = on_error
        self._pending: LightState | None = None
        self._instant = False
        self._last_submit = float("-inf")
        self._shown: RGB | None = None      # what a solid light shows now (None: an effect, or not known)
        self._working = False
        self._cv = threading.Condition()
        threading.Thread(target=self._run, name=f"worker:{device.id}", daemon=True).start()

    def submit(self, state: LightState, instant: bool = False) -> None:
        with self._cv:
            now = time.monotonic()
            self._instant = instant or now - self._last_submit < self.QUICK
            self._last_submit = now
            self._pending = state
            self._cv.notify()

    @property
    def busy(self) -> bool:
        """A state is waiting or being applied."""
        return self._working or self._pending is not None

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._pending is None:
                    self._cv.wait()
                self._working = True
                state, self._pending, instant = self._pending, None, self._instant
            try:
                if not self.device.online:   # (an offline one gets its state again when it reconnects)
                    self._shown = None
                    continue
                target = solid(state)
                if not instant and getattr(self.device, "fades", False) and self._shown is not None \
                        and target is not None and target != self._shown:
                    if not self._glide(self._shown, target, state):
                        continue           # a newer state came: it glides on from where this one is
                self.device.apply(state)
                self._shown = target
            except Exception as e:
                self._shown = None
                self.on_error(self.device, e)
            finally:
                self._working = False

    def _glide(self, start: RGB, end: RGB, state: LightState) -> bool:
        """Steps from `start` to `end` at the light's own pace. False when a newer state arrived
        on the way (self._shown is then where the glide got to)."""
        dev = self.device
        steps = max(2, round(self.FADE * min(dev.max_fps, 30)))
        full = state.patched({"on": True, "mode": "color", "brightness": 100})
        colors = blend(start, end, [(k / steps) ** 2 * (3 - 2 * k / steps) for k in range(1, steps)])
        for rgb in _rgb(colors):
            with dev.lock:
                dev._stop_stream()
                dev.show_color(dev.balanced(rgb), full)       # (the brightness is in the color already)
            self._shown = rgb
            with self._cv:
                if self._pending is None:
                    self._cv.wait(self.FADE / steps)
                if self._pending is not None:
                    return False
        return True


def solid(state: LightState) -> RGB | None:
    """What a light in this state shows on every LED (off is black), or None for an effect."""
    if not state.on:
        return (0, 0, 0)
    if state.mode != "color":
        return None
    return scale(hex_to_rgb(state.color), state.brightness / 100)


class DeviceManager:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.hubs: dict[str, Hub] = {}         # bridges with many lights: Hue, Home Assistant, DMX
        self.states: dict[str, LightState] = {}
        self.workers: dict[str, DeviceWorker] = {}
        self.listeners: list[Callable[[], None]] = []
        self.revision = 0
        self._applied: set[str] = set()      # devices we have pushed a state to this session
        self._touches: dict[str, int] = {}   # how often each light was changed (away.py)
        self._identifying: set[str] = set()  # lights that are flashing so you can find them
        self._white: dict[str, LightState] = {}   # lights showing a plain white while their Balance window is open
        self._undo: tuple[dict, str] | None = None   # the lights before the last big change by hand
        self._connecting: set[str] = set()
        self.force_on = False                # set when started with Windows: lights come on
        self._forced: set[str] = set()
        self._stop = threading.Event()
        self.openrgb = OpenRGBProvider(config["openrgb"], self._on_openrgb_change)
        self.discovery = Discovery(self._on_discovered)
        self.engine = EffectEngine(self)
        self.games = Games(config, self._changed)
        self.automations = Automations(self)
        self.away = Away(self)
        self.sleep = SleepTimer(self)
        self.wake = WakeUp(self)
        self.cycle = PresetCycle(self)
        self.focus = FocusTimer(self)
        self.wallpaper = WallpaperFollow(self)

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        for dev_id, entry in list(self.config["devices"].items()):
            self._add_entry(dev_id, entry)
        self.openrgb.start()
        self.discovery.start()
        self.engine.start()
        self.games.start()
        self.automations.start()
        threading.Thread(target=self._monitor, name="monitor", daemon=True).start()
        threading.Thread(target=self._scheduler, name="schedule", daemon=True).start()
        threading.Thread(target=self._discover_direct, name="direct-discovery", daemon=True).start()

    def _discover_direct(self) -> None:
        """Lights OpenRGB can't drive that the app talks to itself: ASRock graphics cards over
        the card's I2C, and QMK keyboards with SignalRGB's protocol over USB."""
        for dev in asrock_gpu.discover():
            dev.controller.on_status = self._changed
            self._register(dev)
            self._came_online(dev)
        for dev in qmk_keyboard.discover():
            self._register(dev)
            self._came_online(dev)
        self._changed()

    def stop(self) -> None:
        self._stop.set()
        self.automations.stop()
        self.sleep.stop()
        self.wake.stop()
        self.cycle.stop()
        self.focus.stop()
        self.wallpaper.stop()
        self.games.stop()
        self.engine.stop()
        self.discovery.stop()
        self.openrgb.stop()
        for hub in list(self.hubs.values()):
            hub.close()
        self.config.save_now()

    def _changed(self) -> None:
        self.revision += 1
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                log.exception("listener failed")

    # ---- device registry -------------------------------------------------------------

    def _register(self, dev: Device) -> None:
        with self.lock:
            self.devices[dev.id] = dev
            saved = self.config["states"].get(dev.id)
            self.states.setdefault(dev.id, LightState.from_dict(saved))
            self.workers.setdefault(dev.id, DeviceWorker(dev, self.report_error))
            dev.name = self.config["names"].get(dev.id) or dev.name
            self._balance_device(dev)

    def _add_entry(self, dev_id: str, entry: dict) -> Device | Hub | None:
        """Make the light (or hub) for a config entry and connect it in the background."""
        driver = drivers().get(entry.get("kind", ""))
        if driver is None:
            return None
        try:
            made = driver.create(dev_id, entry)
        except Exception:
            log.exception("Could not set up %s", dev_id)
            return None
        if isinstance(made, Hub):
            self.hubs[dev_id] = made
            self._connect_hub_async(made)
        else:
            self._register(made)
            self._connect_async(made)
        return made

    def _connect_hub_async(self, hub: Hub) -> None:
        with self.lock:
            if hub.id in self._connecting:
                return
            self._connecting.add(hub.id)
        threading.Thread(target=self._connect_hub, args=(hub,), name=f"connect:{hub.id}", daemon=True).start()

    def _connect_hub(self, hub: Hub) -> None:
        try:
            hub.connect()
        except NotPairedError as e:
            hub.online, hub.error = False, str(e)
        except Exception as e:
            hub.online, hub.error = False, friendly_error(e)
        else:
            hub.online, hub.error = True, None
            for dev in list(hub.devices.values()):
                if dev.id not in self.devices:
                    self._register(dev)
                self._connect_async(dev)
        finally:
            with self.lock:
                self._connecting.discard(hub.id)
            self._changed()

    def _connect_async(self, dev: Device) -> None:
        with self.lock:
            if dev.id in self._connecting:
                return
            self._connecting.add(dev.id)
        threading.Thread(target=self._connect, args=(dev,), name=f"connect:{dev.id}", daemon=True).start()

    def _connect(self, dev: Device) -> None:
        try:
            dev.connect()
        except NotPairedError as e:
            dev.online, dev.error = False, str(e)
        except Exception as e:
            dev.online, dev.error = False, friendly_error(e)
        else:
            self._came_online(dev)
        finally:
            with self.lock:
                self._connecting.discard(dev.id)
            self._changed()

    def _came_online(self, dev: Device) -> None:
        dev.name = self.config["names"].get(dev.id) or getattr(dev, "reported_name", dev.name)
        with self.lock:
            has_saved = dev.id in self.config["states"]
            if self.force_on and has_saved and dev.id not in self._forced and dev.id not in self.config["hidden"]:
                # Started with Windows: switch the light on with its last look.
                self._forced.add(dev.id)
                self.states[dev.id] = self.states[dev.id].patched({"on": True})
                self.config["states"][dev.id] = self.states[dev.id].to_dict()
                self.config.save_soon()
            if dev.id in self._applied or (has_saved and self.config["settings"].get("restore_on_launch", True)):
                self._applied.add(dev.id)
                self.workers[dev.id].submit(self.states[dev.id])
            elif not has_saved:
                current = dev.read_state()
                if current:
                    self.states[dev.id] = self.states[dev.id].patched(current)

    def _on_openrgb_change(self) -> None:
        for dev in list(self.openrgb.devices.values()):
            if dev.id not in self.devices:
                self._register(dev)
            if dev.online:
                self._came_online(dev)
        self._changed()

    def _on_discovered(self, found: Found) -> None:
        """A light or hub seen on the network (or added by hand): add it, or follow a new IP."""
        with self.lock:
            known = self.hubs.get(found.dev_id) or self.devices.get(found.dev_id) or next(
                (d for d in [*self.hubs.values(), *self.devices.values()]
                 if d.id in self.config["devices"] and d.kind == found.kind
                 and getattr(d, "host", None) == found.host), None)
            if known is None:
                entry = found.entry()
                self.config["devices"][found.dev_id] = entry
                self.config.save_soon()
        if known is None:
            log.info("Found %s %s at %s", found.kind, found.label or found.dev_id, found.host)
            self._add_entry(found.dev_id, entry)
            self._changed()
            return
        if found.host and getattr(known, "host", found.host) != found.host:
            known.host = found.host
            self.config["devices"].get(known.id, {})["host"] = found.host
            self.config.save_soon()
        if not known.online:
            self._reconnect(known)

    def _reconnect(self, target: Device | Hub) -> None:
        if isinstance(target, Hub):
            self._connect_hub_async(target)
        else:
            self._connect_async(target)

    def _monitor(self) -> None:
        while not self._stop.wait(15):
            for hub in list(self.hubs.values()):
                if not hub.online and hub.paired:
                    self._connect_hub_async(hub)
            for dev in list(self.devices.values()):
                # Retried while offline: network lights, and a keyboard that was re-plugged.
                if (is_network(dev) or dev.kind == "qmk") and not dev.online and getattr(dev, "paired", True):
                    self._connect_async(dev)

    def report_error(self, dev: Device, exc: Exception) -> None:
        if getattr(exc, "winerror", None) == 10055:  # WSAENOBUFS: send queue briefly full, drop the frame
            return
        log.warning("%s: %s", dev.name, str(exc) or type(exc).__name__)   # (some say nothing, like OpenRGBDisconnected)
        if dev.kind == "asrock_gpu":
            pass  # its controller retries on its own and reports its state
        elif dev.kind == "openrgb":
            if isinstance(exc, OSError):
                self.openrgb.mark_disconnected()
            else:
                dev.error = friendly_error(exc)
        elif isinstance(exc, OSError):
            dev.online, dev.streaming, dev.error = False, False, friendly_error(exc)
        else:
            dev.error = friendly_error(exc)
        self._changed()

    # ---- used by the effect engine ------------------------------------------------------

    def stream_targets(self) -> list[tuple[Device, LightState]]:
        out = []
        for dev_id, dev in list(self.devices.items()):
            st = self.states.get(dev_id)
            if dev_id in self._identifying:
                continue   # (flashing: the effect must not draw over it)
            if st and dev.streaming and dev.online and st.on and st.mode == "effect" and dev.pixel_count:
                out.append((dev, st))
        return out

    # ---- operations (UI / tray) --------------------------------------------------------------

    def visible_ids(self) -> list[str]:
        hidden = set(self.config["hidden"])
        return [i for i in self.devices if i not in hidden]

    def update(self, ids: list[str] | None, patch: dict) -> None:
        """Change lights by hand (the window, the tray): no preset is showing any more."""
        targets = ids if ids is not None else self.visible_ids()
        # (switching lights off and on again doesn't change the look: the preset stays)
        keep = set(patch) <= {"on"}
        self._apply({dev_id: patch for dev_id in targets},
                    preset=self.config["settings"].get("active_preset", "") if keep else "")

    def _apply(self, patches: dict[str, dict], preset: str, save: bool = True) -> None:
        """Patch many lights at once: one save and one change for all of them. (save=False:
        for now only, the lights come back with their saved state next time.)"""
        with self.lock:
            for dev_id, patch in patches.items():
                if dev_id not in self.devices or not patch:
                    continue
                st = self.states[dev_id].patched(patch)
                self.states[dev_id] = st
                if save:
                    self.config["states"][dev_id] = st.to_dict()
                self._applied.add(dev_id)
                self._touches[dev_id] = self._touches.get(dev_id, 0) + 1
                self.workers[dev_id].submit(st)
            self.config["settings"]["active_preset"] = preset
        if save:
            self.config.save_soon()
        self._changed()

    # ---- undo: back to the lights as they were before the last big change by hand -------------

    def remember(self) -> None:
        """Before a big change by hand (a preset, everything on or off, an effect): how the
        lights look now, for undo(). Automatic looks and schedules don't: they put back their own."""
        with self.lock:
            self._undo = (self.looks(), self.config["settings"].get("active_preset", ""))

    def can_undo(self) -> bool:
        return self._undo is not None

    def undo(self) -> bool:
        """The lights as they were before the last big change; undo again to redo it."""
        with self.lock:
            saved = self._undo
            if saved is None:
                return False
            self._undo = (self.looks(), self.config["settings"].get("active_preset", ""))
        self.restore_looks(*saved)
        return True

    def nudge_brightness(self, delta: int) -> None:
        """Every light that's on a bit brighter or darker (a hotkey), each from where it is."""
        with self.lock:
            patches = {i: {"brightness": max(1, min(100, self.states[i].brightness + delta))}
                       for i in self.visible_ids() if i in self.states and self.states[i].on}
        if patches:
            self._apply(patches, preset="")

    def set_brightness_on(self, value: int) -> None:
        """Every light that's on gets this brightness (the tray); lights that are off stay as they are."""
        value = max(1, min(100, int(value)))
        with self.lock:
            patches = {i: {"brightness": value}
                       for i in self.visible_ids() if i in self.states and self.states[i].on}
        if patches:
            self._apply(patches, preset="")

    def brightness_on(self) -> tuple[int, ...]:
        """The different brightness values of the lights that are on, sorted (for the tray's check)."""
        with self.lock:
            return tuple(sorted({self.states[i].brightness
                                 for i in self.visible_ids() if i in self.states and self.states[i].on}))

    def groups_present(self) -> tuple[str, ...]:
        """Which of "pc" and "room" have visible lights (the PC's lights, and everything else)."""
        with self.lock:
            found = {"pc" if self.devices[i].group == "pc" else "room" for i in self.visible_ids()}
        return tuple(g for g in ("pc", "room") if g in found)

    def set_power_group(self, group: str, on: bool) -> None:
        """Switch the PC's lights ("pc") or the room's lights (all the others) on or off."""
        with self.lock:
            ids = [i for i in self.visible_ids() if (self.devices[i].group == "pc") == (group == "pc")]
        if ids:
            self.update(ids, {"on": on})

    def group_any_on(self, group: str) -> bool:
        """Is any light of the PC ("pc") or of the room (the others) on? (What a hotkey flips.)"""
        with self.lock:
            return any(self.states[i].on for i in self.visible_ids()
                       if self.devices[i].online and (self.devices[i].group == "pc") == (group == "pc"))

    def set_power_all(self, on: bool) -> None:
        self.update(None, {"on": on})

    def any_on(self) -> bool:
        with self.lock:
            return any(self.states[i].on for i in self.visible_ids() if self.devices[i].online)

    # ---- the light show, from the tray, hotkeys and schedules ------------------------------

    def show_lights(self) -> list[str]:
        """The lights that join the show: visible and online, not taken out of it."""
        out = set(self.config.data.get("show_excluded") or [])
        with self.lock:
            return [i for i in self.visible_ids() if self.devices[i].online and i not in out]

    def show_running(self) -> bool:
        with self.lock:
            return any(st.on and st.mode == "effect" and st.effect == "music_show"
                       for i, st in self.states.items() if i in self.devices)

    def start_show(self) -> None:
        self._apply({i: {"on": True, "mode": "effect", "effect": "music_show"} for i in self.show_lights()},
                    preset="")

    def stop_show(self) -> None:
        with self.lock:
            running = [i for i, st in self.states.items()
                       if i in self.devices and st.mode == "effect" and st.effect == "music_show"]
        self._apply({i: {"mode": "color"} for i in running}, preset="")

    def toggle_show(self) -> None:
        self.stop_show() if self.show_running() else self.start_show()

    def looks(self) -> dict[str, dict]:
        """How every light looks now, to put back later with restore_looks."""
        with self.lock:
            return {i: st.to_dict() for i, st in self.states.items() if i in self.devices}

    def restore_looks(self, looks: dict[str, dict], preset: str) -> None:
        """Put the looks from looks() back (on or off too), as one change."""
        with self.lock:
            now = {i: st.to_dict() for i, st in self.states.items() if i in self.devices}
        self._apply({i: look for i, look in looks.items() if i in now and now[i] != look}, preset=preset)

    def game_lights(self, effect: str = "game_cs2") -> None:
        """The lights of the show play along with the game."""
        self._apply({i: {"mode": "effect", "effect": effect} for i in self.show_lights()}, preset="")

    def screen_sync(self, effect: str = "screen") -> None:
        """The lights of the show follow the screen (each the part where it sits on the stage);
        "screen_sound": and flash with the game's shots and explosions."""
        self._apply({i: {"mode": "effect", "effect": effect} for i in self.show_lights()}, preset="")

    # ---- away: lights off while you're away (away.py) -----------------------------------------

    def touches(self, dev_id: str) -> int:
        """How often a light was changed this session: to see whether anything else changed it."""
        with self.lock:
            return self._touches.get(dev_id, 0)

    def away_off(self, scope: str) -> dict[str, int]:
        """Switch off the lights in `scope` ("pc" or "all") that are on, without saving that.
        Returns them, each with its change count right after (to see later whether anything
        else changed it)."""
        with self.lock:
            ids = [i for i in self.visible_ids() if i in self.states and self.states[i].on
                   and (scope == "all" or self.devices[i].group == "pc")]
            preset = self.config["settings"].get("active_preset", "")
        if ids:
            # Lights the sleep timer was fading go off at their own brightness: back as they were.
            own = self.sleep.release(ids)
            self._apply({i: {"on": False, **({"brightness": own[i]} if i in own else {})} for i in ids},
                        preset=preset, save=False)
        with self.lock:
            return {i: self._touches.get(i, 0) for i in ids}

    def away_back(self, off: dict[str, int]) -> list[str]:
        """Switch the lights from away_off on again, except ones changed since."""
        with self.lock:
            ids = [i for i, n in off.items() if i in self.devices and self._touches.get(i, 0) == n]
            preset = self.config["settings"].get("active_preset", "")
        if ids:
            self._apply({i: {"on": True} for i in ids}, preset=preset)
        return ids

    def wait_applied(self, ids: list[str], timeout: float) -> bool:
        """Wait (at most `timeout` s) until these lights have their new state."""
        deadline = time.monotonic() + timeout
        while any(self.workers[i].busy for i in ids if i in self.workers):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def set_app_looks(self, items) -> None:
        """Setup → Automatic → Apps: a look while an app is open (automations.py)."""
        self.config.data["app_looks"] = clean_app_looks(items, self.preset_names())
        self.config.save_soon()
        self._changed()

    # ---- schedules: presets, on, off or the show at set times ---------------------------------

    # Where sunrise and sunset come from (tests swap it): day -> ("HH:MM", "HH:MM"), "" = none.
    sun_times = SUN.times

    def schedule_minute(self, s: dict, day: datetime.datetime) -> int | None:
        """The minute of the day (0..1439) a schedule happens on `day`; None when it follows a
        sun time that doesn't happen that day. Sun times with their offset stay within the day."""
        kind = s.get("time", "")
        if kind in ("sunrise", "sunset"):
            at = self.sun_times(day)[0 if kind == "sunrise" else 1]
            if not at:
                return None
            minute = int(at[:2]) * 60 + int(at[3:5]) + int(s.get("offset", 0))
            return min(1439, max(0, minute))
        return int(kind[:2]) * 60 + int(kind[3:5])

    def set_schedules(self, items) -> None:
        clean = []
        for s in items or []:
            t = str(s.get("time", ""))
            offset = 0
            if t in ("sunrise", "sunset"):
                offset = s.get("offset", 0)
                if isinstance(offset, bool) or not isinstance(offset, int) or not -180 <= offset <= 180:
                    raise ValueError("Minutes before or after sunrise or sunset must be a whole number, at most 180")
            elif not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"{t or 'A time'} isn't a time of day (like 23:30), sunrise or sunset")
            action = str(s.get("action", ""))
            if action not in ("off", "on", "show", "wake:15", "wake:30", "wake:45") and not action.startswith("preset:"):
                raise ValueError("Choose what should happen")
            days = sorted({int(d) for d in s.get("days", range(7)) if 0 <= int(d) <= 6})
            item = {"time": t, "days": days, "action": action, "enabled": bool(s.get("enabled", True))}
            if t in ("sunrise", "sunset"):
                item["offset"] = offset
            clean.append(item)
        today = datetime.datetime.now()

        def when(s):    # in order of when they happen today; a sun time that doesn't happen today goes last
            m = self.schedule_minute(s, today)
            return (m is None, m or 0)
        self.config.data["schedules"] = sorted(clean, key=when)
        self.config.save_soon()
        self._changed()

    @staticmethod
    def schedule_name(s: dict) -> str:
        """A schedule's time for the log: "23:00", "sunset-30", "sunrise+15", "sunset"."""
        off = s.get("offset", 0) if s.get("time") in ("sunrise", "sunset") else 0
        name = f"{s['time']}{off:+d}" if off else str(s["time"])
        early = wake_minutes(s)
        if early:   # a wake-up starts earlier: "07:00 (wake-up from 06:30)"
            t = str(s["time"])
            if re.fullmatch(r"\d\d:\d\d", t):
                start = max(0, int(t[:2]) * 60 + int(t[3:5]) - early)
                name += f" (wake-up from {start // 60:02d}:{start % 60:02d})"
            else:
                name += f" (wake-up {early} min early)"
        return name

    def _scheduler(self) -> None:
        """Every 15 s: run what's due. A time missed by more than two minutes (the PC was asleep)
        is skipped rather than run late."""
        fired: dict[str, datetime.date] = {}
        while not self._stop.wait(15):
            for s in self.due_schedules(datetime.datetime.now(), fired):
                log.info("Schedule %s: %s", self.schedule_name(s), s["action"])
                try:
                    self.run_action(s["action"])
                except Exception:
                    log.exception("Schedule %s failed", self.schedule_name(s))

    def due_schedules(self, now: datetime.datetime, fired: dict) -> list[dict]:
        """The schedules to run at `now` (each once a day; `fired` remembers which ran when)."""
        due = []
        for s in list(self.config.data.get("schedules") or []):
            if not s.get("enabled", True) or now.weekday() not in s.get("days", range(7)):
                continue
            minute = self.schedule_minute(s, now)
            if minute is None:
                continue
            minute = max(0, minute - wake_minutes(s))    # a wake-up starts earlier
            late = (now - now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)).total_seconds()
            key = f"{self.schedule_name(s)} {s['action']}"
            if 0 <= late < 120 and fired.get(key) != now.date():
                fired[key] = now.date()
                due.append(s)
        return due

    def run_action(self, action: str) -> None:
        if action == "off":
            self.set_power_all(False)
        elif action == "on":
            self.set_power_all(True)
        elif action == "show":
            self.start_show()
        elif action.startswith("preset:"):
            self.apply_preset(action[len("preset:"):])
        elif action.startswith("wake:"):
            self.wake.start(int(action[len("wake:"):]))

    def lights_off_now(self, pc: bool = True, room: bool = True) -> None:
        """Windows is shutting down: switch the lights off right away. Their saved state stays
        as it is, so they come back as they were next time. Runs on the shutdown thread: no new
        threads and no unbounded waits, so it can never hold up the shutdown. The room lights go
        first, while the network is surely still up; each needs only one quick request."""
        self.engine.stop()
        if room:
            self._group_off_now("room", "room lights", budget=1.5)
        if pc:
            self._group_off_now("pc", "PC lights", budget=3.0)

    def _group_off_now(self, group: str, label: str, budget: float) -> None:
        deadline = time.monotonic() + budget
        done = 0
        targets = [d for d in list(self.devices.values()) if d.group == group and d.online]
        for dev in targets:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if not dev.lock.acquire(timeout=min(1.0, left)):
                log.warning("Shutdown: %s was busy, skipped", dev.name)
                continue
            try:
                dev.streaming = False
                if is_network(dev):   # when a network light doesn't answer, don't wait long
                    dev.power_off(timeout=max(0.2, min(1.0, deadline - time.monotonic())))
                else:
                    dev.power_off()
                done += 1
            except Exception as e:
                log.warning("Shutdown: switching %s off failed: %s", dev.name, e)
            finally:
                dev.lock.release()
        for dev in targets:  # graphics card and keyboard write on their own threads
            wait = getattr(dev, "wait_written", None)
            if wait:
                wait(max(0.0, deadline - time.monotonic()))
        log.info("Shutdown: switched off %d of %d %s", done, len(targets), label)

    def rename(self, dev_id: str, name: str) -> None:
        name = name.strip()
        dev = self.devices[dev_id]
        if name:
            self.config["names"][dev_id] = name
            dev.name = name
        else:
            self.config["names"].pop(dev_id, None)
            dev.name = getattr(dev, "reported_name", dev.name)
        self.config.save_soon()
        self._changed()

    # ---- balance: white balance and brightness at 100% per light -----------------------------

    def _balance_device(self, dev: Device) -> None:
        b = self.balance(dev.id)
        dev.set_balance(b["warmth"], b["max"])

    def balance(self, dev_id: str) -> dict:
        """What is set for this light: {"warmth": -50..50, "max": 20..100} (0 and 100 when nothing is)."""
        b = self.config.data.get("balance", {}).get(dev_id)
        try:
            return {"warmth": max(-WARMTH_RANGE, min(WARMTH_RANGE, int(b["warmth"]))),
                    "max": max(MAX_FLOOR, min(100, int(b["max"])))}
        except (TypeError, KeyError, ValueError):   # (nothing set, or a file edited by hand)
            return {"warmth": 0, "max": 100}

    def set_balance(self, dev_id: str, warmth, top) -> None:
        """Save a light's balance and use it from the next color on. A neutral one is not kept."""
        with self.lock:
            dev = self.devices.get(dev_id)
            if dev is None:
                raise ValueError("That light isn't there any more")
            warmth = max(-WARMTH_RANGE, min(WARMTH_RANGE, int(round(float(warmth)))))
            top = max(MAX_FLOOR, min(100, int(round(float(top)))))
            table = self.config.data.setdefault("balance", {})
            if warmth == 0 and top == 100:
                table.pop(dev_id, None)
            else:
                table[dev_id] = {"warmth": warmth, "max": top}
            self._balance_device(dev)
            shown = self._white.get(dev_id) or self.states.get(dev_id)
            worker = self.workers.get(dev_id)
        self.config.save_soon()
        # What a solid light shows is sent again so you see the change at once; an effect
        # picks the new balance up with its next frame.
        if worker is not None and shown is not None and (dev_id in self._white or shown.mode != "effect"):
            worker.submit(shown, instant=True)
        self._changed()

    def show_white(self, dev_id: str, on: bool) -> None:
        """While a light's Balance window is open: a plain white at its current brightness, to hold
        it against the other lights. Nothing is saved; off puts the light back as it was."""
        with self.lock:
            dev = self.devices.get(dev_id)
            if dev is None:
                raise ValueError("That light isn't there any more")
            worker, state = self.workers.get(dev_id), self.states.get(dev_id)
            if on:
                if not dev.online:
                    raise ValueError(f"{dev.name} isn't answering")
                white = state.patched({"on": True, "mode": "color", "color": "#ffffff"})
                self._white[dev_id] = white
            else:
                if self._white.pop(dev_id, None) is None:
                    return
                white = state
        if worker is not None and white is not None:
            worker.submit(white, instant=True)   # (a change made meanwhile is in self.states: that wins)

    def set_hidden(self, dev_id: str, hidden: bool) -> None:
        ids = [i for i in self.config["hidden"] if i != dev_id]
        if hidden:
            ids.append(dev_id)
        self.config.data["hidden"] = ids
        self.config.save_soon()
        self._changed()

    def identify(self, dev_id: str, seconds: float = 3.0) -> None:
        """Make one light flash white for a few seconds so you can see which one it is, then put
        it back as it was. Returns at once; the flashing runs on its own thread."""
        with self.lock:
            dev = self.devices.get(dev_id)
            if dev is None:
                raise ValueError("That light isn't there any more")
            if not dev.online:
                raise ValueError(f"{dev.name} isn't answering")
            if dev_id in self._identifying:
                return   # already flashing
            self._identifying.add(dev_id)
        threading.Thread(target=self._flash, args=(dev_id, seconds), name=f"identify:{dev_id}", daemon=True).start()

    def _flash(self, dev_id: str, seconds: float) -> None:
        """The flashing itself: only the light's worker is told; nothing is saved or remembered."""
        try:
            worker, dev = self.workers.get(dev_id), self.devices.get(dev_id)
            if worker is None or dev is None:
                return
            # slow lights (over the network, a few updates a second) flash about once a second
            half = 0.5 if dev.max_fps <= SLOW_FPS else 0.2
            base = self.states[dev_id]
            white = base.patched({"mode": "color", "color": "#ffffff", "brightness": 100, "on": True})
            dark = base.patched({"on": False})
            end = time.monotonic() + seconds
            lit = True
            while time.monotonic() < end and not self._stop.is_set():
                worker.submit(white if lit else dark, instant=True)
                lit = not lit
                if self._stop.wait(min(half, max(0.0, end - time.monotonic()))):
                    break
        finally:
            with self.lock:
                self._identifying.discard(dev_id)
                worker, st = self.workers.get(dev_id), self.states.get(dev_id)
            if worker is not None and st is not None:
                worker.submit(st, instant=True)   # the light's real state now: a change made meanwhile wins

    def forget(self, dev_id: str) -> None:
        """Remove a network light or hub (it comes back if discovery sees it again)."""
        with self.lock:
            hub = self.hubs.pop(dev_id, None)
            if hub is not None:
                hub.close()
                gone = list(hub.devices)
            else:
                dev = self.devices.get(dev_id)
                if not dev or not is_network(dev):
                    return
                gone = [dev_id]
            for i in gone:
                dev = self.devices.pop(i, None)
                if dev is not None:
                    dev.streaming = dev.online = False
                self.workers.pop(i, None)
                self.config["states"].pop(i, None)
            self.config["devices"].pop(dev_id, None)
        self.config.save_soon()
        self._changed()

    def pair(self, dev_id: str) -> None:
        """Pair a light or hub after the user did what its brand asks (press the link button,
        hold the power button, ...); stores the credentials it hands out."""
        target = self.hubs.get(dev_id) or self.devices.get(dev_id)
        if target is None:
            raise ValueError("That light is gone")
        updates = drivers()[target.kind].pair(target)
        entry = self.config["devices"].setdefault(dev_id, {"kind": target.kind, "host": getattr(target, "host", "")})
        entry.update(updates)
        if isinstance(target, Hub):
            target.entry.update(updates)
        self.config.save_now()
        if isinstance(target, Hub):
            self._connect_hub(target)
        else:
            self._connect(target)

    def add_manual(self, kind: str, values: dict | str) -> str:
        """"Add a light by hand": check it with its brand's driver, then add it like a found one."""
        driver = drivers().get(kind)
        if driver is None:
            raise ValueError("Unknown kind of light")
        if isinstance(values, str):
            values = {"host": values}
        found = driver.probe({k: (v.strip() if isinstance(v, str) else v) for k, v in values.items()})
        self._on_discovered(found)
        return found.dev_id

    def search(self) -> None:
        """Look for lights on the network right now."""
        self.discovery.scan_now()

    def resize_zone(self, dev_id: str, index: int, size: int) -> None:
        self.openrgb.save_zone_size(dev_id, index, size)
        self.config.save_soon()
        dev = self.devices[dev_id]
        self.workers[dev_id].submit(self.states[dev_id])
        dev.error = None
        self._changed()

    def native_options(self, dev_id: str) -> dict:
        dev = self.devices[dev_id]
        return {"effects": dev.native_effects(), "palettes": dev.palettes()}

    # ---- presets ---------------------------------------------------------------------------

    def preset_names(self) -> list[str]:
        return [p["name"] for p in self.config["presets"]]

    def preset_previews(self) -> list[dict]:
        """Name plus the colors/effect to draw each preset's swatch in the UI."""
        out = []
        with self.lock:
            visible = self.visible_ids()
        for p in self.config["presets"]:
            states = [p[k] for k in ("all", "room", "pc") if p.get(k)] or list(p.get("states", {}).values())
            lit = [s for s in states if s.get("on", True)]
            effects = {s.get("effect") for s in lit if s.get("mode") == "effect"}
            colors = []
            for s in lit:
                for key in (("color", "color2") if s.get("mode") == "effect" else ("color",)):
                    c = s.get(key)
                    if c and c not in colors:
                        colors.append(c)
            # (which lights it sets: a user preset may hold only some; None = every light)
            ids = [i for i in visible if i in p["states"]] if "states" in p else None
            some = ids is not None and len(ids) < len(visible)
            out.append({"name": p["name"], "effects": sorted(e for e in effects if e), "colors": colors[:4],
                        "off": not lit, "builtIn": "states" not in p,
                        "split": bool(p.get("pc") or p.get("room")),
                        "lights": len(ids) if some else None, "ids": ids if some else None})
        return out

    def apply_preset(self, name: str) -> None:
        preset = next((p for p in self.config["presets"] if p["name"] == name), None)
        if not preset:
            return
        per_device = preset.get("states", {})
        with self.lock:
            groups = {dev_id: self.devices[dev_id].group for dev_id in self.visible_ids()}
        patches = {dev_id: per_device.get(dev_id) or preset.get(group) or preset.get("all")
                   for dev_id, group in groups.items()}
        self._apply({i: p for i, p in patches.items() if p}, preset=name)

    def light_from_colors(self, colors: list[str]) -> int:
        """Spread the colors of a picture over the lights that are on, left to right as they stand
        on the stage, as one change (how many lights took one). No preset shows after it: it is
        saved as one only if the person says so."""
        from . import palette
        with self.lock:
            lights = [self.devices[i] for i in self.visible_ids() if self.states[i].on]
            everyone = list(self.devices.values())
        spans = stage_spans(everyone, self.config.data.get("show_layout"))
        lights.sort(key=lambda d: d.name.lower())   # (lights that share a place divide it in this order)
        patches = palette.spread(colors, [(d.id, d.pixel_count, spans[d.id]) for d in lights])
        self._apply(patches, preset="")
        return len(patches)

    def light_from_wallpaper(self):
        """The desktop wallpaper's colors on the lights (Undo like a preset): the colors and a small
        picture of it for the window. The hotkey and the window both come here."""
        colors, img = wallpaper_look.look()
        self.remember()
        if not self.light_from_colors(colors):
            raise ValueError("Switch a light on first, then try again")
        return colors, img

    def step_preset(self, step: int) -> None:
        """The next (+1) or previous (-1) preset in the list's order, from the one that's on, round
        the corner at the ends. With none on, next is the first and previous the last."""
        names = self.preset_names()
        if not names:
            return
        now = self.config["settings"].get("active_preset", "")
        at = names.index(now) + step if now in names else (0 if step > 0 else -1)
        self.remember()
        self.apply_preset(names[at % len(names)])

    def save_preset(self, name: str, ids: list[str] | None = None) -> None:
        """Save the lights as they are now. ids = only those lights (None = every visible light; an
        existing preset that holds only some lights keeps its own set)."""
        name = name.strip()
        if not name:
            raise ValueError("Give the preset a name")
        with self.lock:
            visible = self.visible_ids()
            presets = list(self.config["presets"])
            at = next((k for k, p in enumerate(presets) if p["name"] == name), None)
            if ids is None:
                old = presets[at].get("states") if at is not None else None
                ids = [i for i in visible if i in old] if old else visible
                ids = ids or visible        # (all of its lights are gone: start again with every light)
            else:
                ids = list(dict.fromkeys(ids))
                if not ids:
                    raise ValueError("Choose at least one light")
                if any(i not in visible for i in ids):
                    raise ValueError("One of those lights is gone")
                ids = [i for i in visible if i in ids]
            states = {i: self.states[i].to_dict() for i in ids}
            new = {"name": name, "states": states}
            if at is None:
                presets.append(new)
            else:
                presets[at] = new           # (saved over: it keeps its place, and its hotkey)
            self.config.data["presets"] = presets
            self.config["settings"]["active_preset"] = name
        self.config.save_soon()
        self._changed()

    def rename_preset(self, old: str, new: str) -> None:
        """A preset's new name, everywhere it's used: schedules, apps, automatic looks."""
        new = new.strip()
        names = self.preset_names()
        if not new:
            raise ValueError("Give the preset a name")
        if old not in names:
            raise ValueError("There's no such preset")
        if new != old and new in names:
            raise ValueError(f"There's already a preset called {new}")
        hotkeys.rename_preset_key(self.config["settings"], old, new)
        self.cycle.rename(old, new)     # (before the settings change: it accepts both names for a moment)
        self.focus.rename(old, new)
        with self.lock:
            for p in self.config["presets"]:
                if p["name"] == old:
                    p["name"] = new
            s = self.config["settings"]
            if s.get("active_preset") == old:
                s["active_preset"] = new
            for key in ("auto_game", "auto_video", "auto_call"):
                if s.get(key) == f"preset:{old}":
                    s[key] = f"preset:{new}"
            for item in [*(self.config.data.get("schedules") or []), *(self.config.data.get("app_looks") or [])]:
                if item.get("action") == f"preset:{old}":
                    item["action"] = f"preset:{new}"
        self.config.save_soon()
        self._changed()

    def move_preset(self, name: str, step: int) -> None:
        """A preset earlier (-1) or later (+1) in the list: the tray, Home and hotkeys 1-9 follow it."""
        with self.lock:
            presets = list(self.config["presets"])
            at = next((k for k, p in enumerate(presets) if p["name"] == name), None)
            if at is None:
                raise ValueError("There's no such preset")
            to = max(0, min(len(presets) - 1, at + int(step)))
            presets.insert(to, presets.pop(at))
            self.config.data["presets"] = presets
        self.config.save_soon()
        self._changed()

    def duplicate_preset(self, name: str) -> str:
        """A copy of a preset right after it ("Cozy copy", "Cozy copy 2", ...), to change without
        losing the original. The copy's name."""
        with self.lock:
            presets = list(self.config["presets"])
            at = next((k for k, p in enumerate(presets) if p["name"] == name), None)
            if at is None:
                raise ValueError("There's no such preset")
            names = {p["name"] for p in presets}
            new, n = f"{name} copy", 2
            while new in names:
                new, n = f"{name} copy {n}", n + 1
            presets.insert(at + 1, {**copy.deepcopy(presets[at]), "name": new})
            self.config.data["presets"] = presets
        self.config.save_soon()
        self._changed()
        return new

    def delete_preset(self, name: str) -> None:
        self.config.data["presets"] = [p for p in self.config["presets"] if p["name"] != name]
        self.cycle.forget(name)
        self.focus.forget(name)
        hotkeys.forget_preset_key(self.config["settings"], name)
        self.config.save_soon()
        self._changed()

    # ---- snapshot for the UI ---------------------------------------------------------------

    def snapshot(self) -> dict:
        hidden = set(self.config["hidden"])
        with self.lock:
            devices = []
            for dev_id, dev in self.devices.items():
                d = dev.describe()
                d["state"] = self.states[dev_id].to_dict()
                d["hidden"] = dev_id in hidden
                d["balance"] = self.balance(dev_id)
                devices.append(d)
        devices.sort(key=lambda d: (d["group"] != "pc", d["name"].lower()))
        return {
            "revision": self.revision,
            "devices": devices,
            "presets": self.preset_previews(),
            "activePreset": self.config["settings"].get("active_preset", ""),
            "showExcluded": list(self.config.data.get("show_excluded") or []),
            "schedules": list(self.config.data.get("schedules") or []),
            "appLooks": list(self.config.data.get("app_looks") or []),
            "effects": [e.describe() for e in EFFECTS.values()],
            "hubs": [h.describe() for h in list(self.hubs.values())],
            "drivers": [d.describe() for d in drivers().values()],
            "stage": self.stage(),
            **self.live_status(),
        }

    def live_status(self) -> dict:
        """The parts of the snapshot that change without a new revision: what the show hears and
        does, the sound output, and the downloads in progress. The window gets these every second,
        the rest only when the revision changed."""
        return {
            "revision": self.revision,
            "openrgb": self.openrgb.describe(),
            "genreAi": {**genre_install.describe(), "active": self.engine.show.genre.model_name},
            "audio": {"device": self.engine.audio.device, "listening": self.engine.audio.active,
                      "kind": self.engine.audio.device_kind, "sync": self.show_sync()},
            "show": self.engine.show.info() if self.engine.show_active else None,
            "auto": self.automations.active,
            "autoPaused": self.automations.paused_until(),
            "games": self.games.describe(),
        }

    # ---- stage layout (where each light sits for the light show) ------------------------------

    def stage(self) -> dict:
        overrides = self.config.data.get("show_layout") or {}
        with self.lock:
            devices = list(self.devices.values())
        return {dev_id: {"start": a, "end": b, "layer": layer, "custom": dev_id in overrides}
                for dev_id, (a, b, layer) in stage_spans(devices, overrides).items()}

    def set_stage(self, dev_id: str, span) -> None:
        """Place a light on the stage: [start, end, "front"|"back"] (0 = left, 1 = right; start >
        end runs it the other way), or None to put it back where Glowstage would put it."""
        layout = self.config.data.setdefault("show_layout", {})
        if span is None:
            layout.pop(dev_id, None)
        else:
            a, b = (round(min(1.0, max(0.0, float(v))), 3) for v in span[:2])
            layer = span[2] if len(span) > 2 and span[2] in ("front", "back") else "front"
            if abs(a - b) < 0.02:
                raise ValueError("A light needs some width on the stage")
            layout[dev_id] = [a, b, layer]
        self.config.save_soon()
        self._changed()

    def set_show_excluded(self, ids) -> None:
        """The lights that sit the light show out (keep their own look when it starts)."""
        self.config.data["show_excluded"] = sorted({str(i) for i in (ids or [])})
        self.config.save_soon()
        self._changed()

    def reset_stage(self) -> None:
        self.config.data["show_layout"] = {}
        self.config.save_soon()
        self._changed()

    # ---- sync: when you hear the sound, per sound output --------------------------------------

    def show_sync(self) -> dict:
        """How long the music lights wait for the sound to reach your ears (ms; below 0 they run
        further ahead instead), for the sound output in use: what was set for it, else a guess
        from how it is connected. Bluetooth plays the sound ~0.2 s after the PC sends it."""
        audio = self.engine.audio
        saved = self.config.data.get("show_sync") or {}
        if audio.device and audio.device in saved:
            return {"ms": int(saved[audio.device]), "auto": False}
        if audio.device_kind == "bluetooth":
            return {"ms": BLUETOOTH_SYNC_MS, "auto": True}
        return {"ms": int(saved.get("", 0)), "auto": "" not in saved}

    def set_show_sync(self, ms) -> None:
        """Set the sync for the sound output in use (or, before anything was heard, for every
        output that has none of its own); None goes back to the automatic guess."""
        saved = self.config.data.setdefault("show_sync", {})
        key = self.engine.audio.device or ""
        if ms is None:
            saved.pop(key, None)
        else:
            saved[key] = int(round(min(SYNC_MAX_MS, max(SYNC_MIN_MS, float(ms))) / 5) * 5)
        self.config.save_soon()
        self._changed()

    def set_dmx_fixtures(self, hub_id: str, fixtures: list[dict]) -> None:
        """Patch the fixtures of a DMX universe (from the DMX editor)."""
        hub = self.hubs.get(hub_id)
        if hub is None or hub.kind != "dmx":
            raise ValueError("Not a DMX universe")
        self.config["devices"][hub_id]["fixtures"] = fixtures
        hub.entry["fixtures"] = fixtures
        with self.lock:
            for old in list(hub.devices):
                self.devices.pop(old, None)
                self.workers.pop(old, None)
        for dev in hub.set_fixtures(fixtures):
            self._register(dev)
            self._connect_async(dev)
        self.config.save_soon()
        self._changed()
