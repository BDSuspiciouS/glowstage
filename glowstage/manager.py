"""Owns every device, its desired state and the presets, and runs the background threads."""

from __future__ import annotations

import datetime
import logging
import re
import threading
import time
from typing import Callable

from . import genre_install
from .config import Config
from .devices import asrock_gpu, qmk_keyboard
from .devices.base import Device
from .devices.discovery import Discovery
from .devices.hub import Hub, NotPairedError
from .devices.openrgb_dev import OpenRGBProvider
from .devices.registry import Found, drivers
from .effects import EFFECTS
from .engine import EffectEngine
from .show import stage_spans
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
    """Applies the latest desired state to one device, dropping intermediate ones (slider drags)."""

    def __init__(self, device: Device, on_error):
        self.device = device
        self.on_error = on_error
        self._pending: LightState | None = None
        self._cv = threading.Condition()
        threading.Thread(target=self._run, name=f"worker:{device.id}", daemon=True).start()

    def submit(self, state: LightState) -> None:
        with self._cv:
            self._pending = state
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._pending is None:
                    self._cv.wait()
                state, self._pending = self._pending, None
            if not self.device.online:
                continue  # re-applied when the device reconnects
            try:
                self.device.apply(state)
            except Exception as e:
                self.on_error(self.device, e)


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
        self._connecting: set[str] = set()
        self.force_on = False                # set when started with Windows: lights come on
        self._forced: set[str] = set()
        self._stop = threading.Event()
        self.openrgb = OpenRGBProvider(config["openrgb"], self._on_openrgb_change)
        self.discovery = Discovery(self._on_discovered)
        self.engine = EffectEngine(self)

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        for dev_id, entry in list(self.config["devices"].items()):
            self._add_entry(dev_id, entry)
        self.openrgb.start()
        self.discovery.start()
        self.engine.start()
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
        log.warning("%s: %s", dev.name, exc)
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

    def _apply(self, patches: dict[str, dict], preset: str) -> None:
        """Patch many lights at once: one save and one change for all of them."""
        with self.lock:
            for dev_id, patch in patches.items():
                if dev_id not in self.devices or not patch:
                    continue
                st = self.states[dev_id].patched(patch)
                self.states[dev_id] = st
                self.config["states"][dev_id] = st.to_dict()
                self._applied.add(dev_id)
                self.workers[dev_id].submit(st)
            self.config["settings"]["active_preset"] = preset
        self.config.save_soon()
        self._changed()

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

    # ---- schedules: presets, on, off or the show at set times ---------------------------------

    def set_schedules(self, items) -> None:
        clean = []
        for s in items or []:
            t = str(s.get("time", ""))
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"{t or 'A time'} isn't a time of day (like 23:30)")
            action = str(s.get("action", ""))
            if action not in ("off", "on", "show") and not action.startswith("preset:"):
                raise ValueError("Choose what should happen")
            days = sorted({int(d) for d in s.get("days", range(7)) if 0 <= int(d) <= 6})
            clean.append({"time": t, "days": days, "action": action, "enabled": bool(s.get("enabled", True))})
        self.config.data["schedules"] = sorted(clean, key=lambda s: s["time"])
        self.config.save_soon()
        self._changed()

    def _scheduler(self) -> None:
        """Every 15 s: run what's due. A time missed by more than two minutes (the PC was asleep)
        is skipped rather than run late."""
        fired: dict[str, datetime.date] = {}
        while not self._stop.wait(15):
            for s in self.due_schedules(datetime.datetime.now(), fired):
                log.info("Schedule %s: %s", s["time"], s["action"])
                try:
                    self.run_action(s["action"])
                except Exception:
                    log.exception("Schedule %s failed", s["time"])

    def due_schedules(self, now: datetime.datetime, fired: dict) -> list[dict]:
        """The schedules to run at `now` (each once a day; `fired` remembers which ran when)."""
        due = []
        for s in list(self.config.data.get("schedules") or []):
            if not s.get("enabled", True) or now.weekday() not in s.get("days", range(7)):
                continue
            hh, mm = (int(v) for v in s["time"].split(":"))
            late = (now - now.replace(hour=hh, minute=mm, second=0, microsecond=0)).total_seconds()
            key = f"{s['time']} {s['action']}"
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

    def any_on(self) -> bool:
        return any(self.states[i].on for i in self.visible_ids() if self.devices[i].online)

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

    def set_hidden(self, dev_id: str, hidden: bool) -> None:
        ids = [i for i in self.config["hidden"] if i != dev_id]
        if hidden:
            ids.append(dev_id)
        self.config.data["hidden"] = ids
        self.config.save_soon()
        self._changed()

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
            out.append({"name": p["name"], "effects": sorted(e for e in effects if e), "colors": colors[:4],
                        "off": not lit, "builtIn": "states" not in p,
                        "split": bool(p.get("pc") or p.get("room"))})
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

    def save_preset(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Give the preset a name")
        with self.lock:
            states = {i: self.states[i].to_dict() for i in self.visible_ids()}
            presets = [p for p in self.config["presets"] if p["name"] != name]
            presets.append({"name": name, "states": states})
            self.config.data["presets"] = presets
            self.config["settings"]["active_preset"] = name
        self.config.save_soon()
        self._changed()

    def delete_preset(self, name: str) -> None:
        self.config.data["presets"] = [p for p in self.config["presets"] if p["name"] != name]
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
                devices.append(d)
        devices.sort(key=lambda d: (d["group"] != "pc", d["name"].lower()))
        return {
            "revision": self.revision,
            "devices": devices,
            "presets": self.preset_previews(),
            "activePreset": self.config["settings"].get("active_preset", ""),
            "showExcluded": list(self.config.data.get("show_excluded") or []),
            "schedules": list(self.config.data.get("schedules") or []),
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
