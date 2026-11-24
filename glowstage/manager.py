"""Owns every device, its desired state and the presets, and runs the background threads."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .config import Config
from .devices import asrock_gpu, qmk_keyboard
from .devices.base import Device
from .devices.discovery import Discovery
from .devices.nanoleaf import NanoleafDevice, NotPairedError
from .devices.net import http_json
from .devices.openrgb_dev import OpenRGBProvider
from .devices.wled import WledDevice
from .effects import EFFECTS
from .engine import EffectEngine
from .state import LightState

log = logging.getLogger(__name__)

NETWORK_KINDS = ("wled", "nanoleaf")
RECONNECT_KINDS = NETWORK_KINDS + ("qmk",)   # retried every 15 s while offline (e.g. keyboard re-plugged)


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
            self._add_network_device(dev_id, entry)
        self.openrgb.start()
        self.discovery.start()
        self.engine.start()
        threading.Thread(target=self._monitor, name="monitor", daemon=True).start()
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

    def _add_network_device(self, dev_id: str, entry: dict) -> Device | None:
        kind, label = entry.get("kind"), entry.get("label")
        if kind == "wled":
            dev = WledDevice(dev_id, label or "WLED", entry["host"])
        elif kind == "nanoleaf":
            dev = NanoleafDevice(dev_id, label or "Nanoleaf", entry["host"], int(entry.get("port", 16021)),
                                 entry.get("token"))
        else:
            return None
        self._register(dev)
        self._connect_async(dev)
        return dev

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

    def _on_discovered(self, kind: str, dev_id: str, host: str, port: int, label: str) -> None:
        with self.lock:
            dev = self.devices.get(dev_id) or next(
                (d for d in self.devices.values() if d.kind == kind and getattr(d, "host", None) == host), None)
            if dev is None:
                entry = {"kind": kind, "host": host, "port": port, "label": label}
                self.config["devices"][dev_id] = entry
                self.config.save_soon()
        if dev is None:
            self._add_network_device(dev_id, entry)
            return
        if getattr(dev, "host", host) != host:
            dev.host = host
            self.config["devices"].get(dev.id, {})["host"] = host
            self.config.save_soon()
        if not dev.online:
            self._connect_async(dev)

    def _monitor(self) -> None:
        while not self._stop.wait(15):
            for dev in list(self.devices.values()):
                if dev.kind in RECONNECT_KINDS and not dev.online and getattr(dev, "paired", True):
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
        with self.lock:
            for dev_id in (ids if ids is not None else self.visible_ids()):
                if dev_id not in self.devices:
                    continue
                st = self.states[dev_id].patched(patch)
                self.states[dev_id] = st
                self.config["states"][dev_id] = st.to_dict()
                self._applied.add(dev_id)
                self.workers[dev_id].submit(st)
        self.config.save_soon()
        self._changed()

    def set_power_all(self, on: bool) -> None:
        self.update(None, {"on": on})

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
                if dev.kind in NETWORK_KINDS:  # without WLED/Nanoleaf answering, don't wait long
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
        """Remove a network light (it comes back if discovery sees it again)."""
        with self.lock:
            dev = self.devices.get(dev_id)
            if not dev or dev.kind not in NETWORK_KINDS:
                return
            dev.streaming = False
            dev.online = False
            self.devices.pop(dev_id, None)
            self.workers.pop(dev_id, None)
            self.config["devices"].pop(dev_id, None)
            self.config["states"].pop(dev_id, None)
        self.config.save_soon()
        self._changed()

    def pair_nanoleaf(self, dev_id: str) -> None:
        dev = self.devices[dev_id]
        if not isinstance(dev, NanoleafDevice):
            raise ValueError("Not a Nanoleaf device")
        token = dev.pair()
        self.config["devices"].setdefault(dev_id, {"kind": "nanoleaf", "host": dev.host, "port": dev.port})["token"] = token
        self.config.save_now()
        self._connect(dev)

    def add_manual(self, kind: str, host: str) -> str:
        host = host.strip().removeprefix("http://").rstrip("/")
        if kind == "wled":
            info = http_json("GET", f"http://{host}/json/info", timeout=4.0)
            dev_id = f"wled:{info.get('mac', host)}"
            label = info.get("name") or "WLED"
            port = 80
        elif kind == "nanoleaf":
            dev_id, label, port = f"nanoleaf:{host}", "Nanoleaf", 16021
        else:
            raise ValueError("Unknown device type")
        self._on_discovered(kind, dev_id, host, port, label)
        return dev_id

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
            states = [p["all"]] if p.get("all") else list(p.get("states", {}).values())
            lit = [s for s in states if s.get("on", True)]
            effects = {s.get("effect") for s in lit if s.get("mode") == "effect"}
            colors = []
            for s in lit:
                for key in (("color", "color2") if s.get("mode") == "effect" else ("color",)):
                    c = s.get(key)
                    if c and c not in colors:
                        colors.append(c)
            out.append({"name": p["name"], "effects": sorted(e for e in effects if e), "colors": colors[:4],
                        "off": not lit})
        return out

    def apply_preset(self, name: str) -> None:
        preset = next((p for p in self.config["presets"] if p["name"] == name), None)
        if not preset:
            return
        per_device = preset.get("states", {})
        for dev_id in self.visible_ids():
            data = per_device.get(dev_id) or preset.get("all")
            if data:
                self.update([dev_id], data)

    def save_preset(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("Give the preset a name")
        with self.lock:
            states = {i: self.states[i].to_dict() for i in self.visible_ids()}
            presets = [p for p in self.config["presets"] if p["name"] != name]
            presets.append({"name": name, "states": states})
            self.config.data["presets"] = presets
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
            "openrgb": self.openrgb.describe(),
            "presets": self.preset_previews(),
            "effects": [e.describe() for e in EFFECTS.values()],
            "audio": {"device": self.engine.audio.device, "listening": self.engine.audio.active},
            "show": self.engine.show.info() if self.engine.show_active else None,
        }
