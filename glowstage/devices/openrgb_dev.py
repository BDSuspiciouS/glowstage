"""PC components (motherboard, RAM, fan/AIO hubs, peripherals) through OpenRGB's SDK server.

The provider keeps one TCP connection to OpenRGB on 127.0.0.1:6742, starts the bundled
portable OpenRGB in headless server mode when nothing is listening, and reconnects if
OpenRGB restarts. All devices share the provider's lock because they share the socket.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import threading
import time
import winreg
from collections import Counter
from pathlib import Path

from openrgb import OpenRGBClient
from openrgb.utils import ModeColors, OpenRGBDisconnected, RGBColor

from .. import winutil
from ..colors import RGB, hex_to_rgb, rgb_to_hex, scale
from ..paths import APP_NAME, DATA_DIR, INSTALL_DIR
from ..state import LightState
from . import commander_core
from .base import Device

log = logging.getLogger(__name__)

# OpenRGB next to the app (a source checkout's tools folder), or the copy the app downloaded.
BUNDLED_EXES = [INSTALL_DIR / "tools" / "OpenRGB" / "OpenRGB Windows 64-bit" / "OpenRGB.exe",
                DATA_DIR / "OpenRGB" / "OpenRGB Windows 64-bit" / "OpenRGB.exe"]
BUNDLED_EXE = next((p for p in BUNDLED_EXES if p.is_file()), BUNDLED_EXES[-1])
INSTALLED_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OpenRGB" / "OpenRGB.exe"
# Protocol 3 skips the plugin-list request, which OpenRGB 1.x never answers in --server mode.
PROTOCOL_VERSION = 3
CLIENT_NAME = APP_NAME
NON_EFFECT_MODES = {"direct", "custom", "off"}
COMMANDER_CORE = "Corsair Commander Core"     # OpenRGB's name for it (the XT is a different device)


def openrgb_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OpenRGB.exe", "/NH", "/FO", "CSV"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return "openrgb.exe" in out.lower()


def pawnio_installed() -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\PawnIO"))
        return True
    except OSError:
        return False


class OpenRGBDevice(Device):
    kind = "openrgb"
    group = "pc"
    hardware_brightness = False
    # OpenRGB's Commander Core driver drops frames that arrive within 33 ms of the previous
    # one, so stay a little under 30 fps to keep effects smooth instead of stuttering.
    max_fps = 25

    def __init__(self, provider: "OpenRGBProvider", key: str, dev):
        # "Corsair Commander Core" -> "Commander Core": the vendor is noise on a fader label.
        vendor = (dev.metadata.vendor or "").strip()
        short = dev.name[len(vendor):].strip() if vendor and dev.name.lower().startswith(vendor.lower() + " ") else dev.name
        super().__init__(key, short)
        self.lock = provider.lock
        self.reported_name = short
        self.parent_key = key                       # the OpenRGB device this light belongs to
        self.detected: dict[str, int | None] = {}   # zone name -> LEDs the hardware reports (None = empty)
        self.bind(dev)

    def _count(self, dev) -> int:
        return len(dev.leds)

    def bind(self, dev) -> None:
        self._dev = dev
        self.pixel_count = self._count(dev)
        self.online = True
        self.error = None
        self._direct = False

    def _ensure_direct(self) -> None:
        if not self._direct:
            self._dev.set_custom_mode()
            self._direct = True

    def _fill(self, rgb: RGB) -> None:
        self._ensure_direct()
        if self.pixel_count:
            self._dev.set_colors([RGBColor(*rgb)] * self.pixel_count, fast=True)

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._fill(scale(rgb, state.brightness / 100))

    def power_off(self) -> None:
        self._fill((0, 0, 0))

    def begin_stream(self, state: LightState) -> None:
        self._ensure_direct()

    def send_frame(self, colors: list[RGB]) -> None:
        if self.pixel_count and len(colors) == self.pixel_count:
            self._dev.set_colors([RGBColor(*c) for c in colors], fast=True)

    def show_native(self, state: LightState) -> None:
        mode = next((m for m in self._dev.modes if m.name == state.native), None)
        if mode is None:
            return self.show_color(hex_to_rgb(state.color), state)
        if mode.speed_min is not None and mode.speed_max is not None:
            mode.speed = round(mode.speed_min + (mode.speed_max - mode.speed_min) * state.speed / 100)
        if mode.color_mode == ModeColors.MODE_SPECIFIC and mode.colors_max:
            picks = [RGBColor(*hex_to_rgb(state.color)), RGBColor(*hex_to_rgb(state.color2))]
            mode.colors = (picks * mode.colors_max)[:max(mode.colors_min or 1, min(2, mode.colors_max))]
        self._dev.set_mode(mode)
        self._direct = False

    def native_effects(self) -> list[dict]:
        return [{"id": m.name, "name": m.name} for m in self._dev.modes if m.name.lower() not in NON_EFFECT_MODES]

    def zones(self) -> list[dict]:
        return [
            {"index": i, "name": z.name, "leds": len(z.leds), "min": z.leds_min, "max": z.leds_max,
             "resizable": z.leds_min != z.leds_max,
             "detected": self.detected[z.name] if z.name in self.detected else -1}   # -1 = unknown
            for i, z in enumerate(self._dev.data.zones)
        ]

    def resize_zone(self, index: int, size: int) -> None:
        with self.lock:
            self._dev.zones[index].resize(size)
            self.pixel_count = self._count(self._dev)
            self._direct = False

    def read_state(self) -> dict | None:
        lit = [c for c in self._dev.colors if (c.red, c.green, c.blue) != (0, 0, 0)]
        if not self._dev.colors:
            return None
        if not lit:
            return {"on": False}
        return {"on": True, "color": rgb_to_hex((lit[0].red, lit[0].green, lit[0].blue))}

    def extra_info(self) -> dict:
        kind = self._dev.type.name.replace("_", " ").title()
        return {"label": f"{kind} · {self.pixel_count} LEDs", "deviceType": self._dev.type.name,
                "vendor": self._dev.metadata.vendor, "zones": self.zones()}


class OpenRGBZoneDevice(OpenRGBDevice):
    """Some zones of an OpenRGB device as a light of their own, e.g. one motherboard ARGB
    header with a fan chain on it, so the pump and each fan chain get their own fader."""

    def __init__(self, provider: "OpenRGBProvider", key: str, parent_key: str, dev, zone_names: list[str], name: str):
        self.zone_names = zone_names
        super().__init__(provider, key, dev)
        self.parent_key = parent_key
        self.name = self.reported_name = name

    def _own(self, dev=None) -> list:
        return [z for z in (dev or self._dev).zones if z.name in self.zone_names]

    @property
    def fixture(self) -> dict | None:
        """ARGB headers here drive chains of 8-LED fans (Corsair RS120) and the 8-LED pump: rings
        the light show can spin."""
        if (self.zone_names and all(z.upper().startswith("ARGB") for z in self.zone_names)
                and self.pixel_count and self.pixel_count % 8 == 0 and self.pixel_count <= 64):
            return {"ring": 8}
        return None

    def _count(self, dev) -> int:
        return sum(len(z.leds) for z in self._own(dev))

    def _fill(self, rgb: RGB) -> None:
        self._ensure_direct()
        for z in self._own():
            if z.leds:
                z.set_colors([RGBColor(*rgb)] * len(z.leds), fast=True)

    def send_frame(self, colors: list[RGB]) -> None:
        if len(colors) != self.pixel_count:
            return
        start = 0
        for z in self._own():
            n = len(z.leds)
            if n:
                z.set_colors([RGBColor(*c) for c in colors[start:start + n]], fast=True)
            start += n

    def native_effects(self) -> list[dict]:
        return []  # hardware modes run on the whole board, not on one header

    def zones(self) -> list[dict]:
        return [z for z in super().zones() if z["name"] in self.zone_names]

    def read_state(self) -> dict | None:
        colors = [c for z in self._own() for c in z.colors]
        if not colors:
            return None
        lit = [c for c in colors if (c.red, c.green, c.blue) != (0, 0, 0)]
        return {"on": True, "color": rgb_to_hex((lit[0].red, lit[0].green, lit[0].blue))} if lit else {"on": False}

    def extra_info(self) -> dict:
        info = super().extra_info()
        info["label"] = f"{', '.join(self.zone_names)} · {self.pixel_count} LEDs"
        return info


def split_plan(dev) -> list[tuple[str, str, list[str]]]:
    """Motherboards: each addressable (resizable) header becomes its own light and the fixed
    onboard LEDs share one. Other devices stay whole. Returns (key suffix, name, zone names)."""
    if dev.type.name != "MOTHERBOARD":
        return []
    zones = dev.data.zones
    headers = [z.name for z in zones if z.leds_min != z.leds_max]
    if not headers:
        return []
    parts = []
    for name in headers:
        number = re.search(r"(\d+)$", name)
        parts.append((name, f"ARGB header {number.group(1)}" if number else name, [name]))
    onboard = [z.name for z in zones if z.leds_min == z.leds_max]
    if onboard:
        parts.append(("onboard", "Motherboard", onboard))
    return parts


class OpenRGBProvider:
    def __init__(self, settings: dict, on_change):
        self.settings = settings            # live reference to config["openrgb"]
        self.on_change = on_change          # called with no args when the device list changes
        self.lock = threading.RLock()
        self.client: OpenRGBClient | None = None
        self.devices: dict[str, OpenRGBDevice] = {}
        self.status = "starting"
        self.message = ""
        self.launched = False
        self.launched_elevated = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="openrgb", daemon=True)

    # ---- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self.lock:
            if self.client:
                try:
                    self.client.disconnect()
                except Exception:
                    pass

    def exe_path(self) -> Path | None:
        custom = self.settings.get("path")
        for candidate in (Path(custom) if custom else None, BUNDLED_EXE, INSTALLED_EXE):
            if candidate and candidate.is_file():
                return candidate
        return None

    def launch(self, elevated: bool = False) -> None:
        exe = self.exe_path()
        if not exe:
            raise FileNotFoundError("OpenRGB.exe not found. Set its path in Settings.")
        args = ["--server", "--server-port", str(self.port)]
        if exe == BUNDLED_EXE:
            args.append("--localconfig")
        if not openrgb_running():
            self._detect_fan_leds()  # only safe while OpenRGB isn't talking to the controller
        if elevated:
            self._launch_elevated(exe, args)
        else:
            subprocess.Popen(
                [str(exe), *args], cwd=str(exe.parent),
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        self.launched, self.launched_elevated = True, elevated
        self.status, self.message = "launching", "Starting OpenRGB and detecting devices..."
        self._wake.set()

    def _launch_elevated(self, exe: Path, args: list[str]) -> None:
        """Start OpenRGB as administrator through the scheduled task, registering it first if
        needed (the only time Windows asks). Falls back to a plain UAC prompt."""
        if winutil.openrgb_task_command() != str(exe):
            winutil.create_openrgb_task(exe, args)
        if winutil.openrgb_task_command() == str(exe) and winutil.run_openrgb_task():
            return
        params = " ".join(f'"{a}"' if " " in a else a for a in args)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, str(exe.parent), 0)
        if rc <= 32:
            raise PermissionError("Administrator start was cancelled.")

    def restart(self, elevated: bool) -> None:
        self.kill()
        time.sleep(1.0)
        self.launch(elevated)

    def kill(self) -> None:
        with self.lock:
            if self.client:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
                self.client = None
            for dev in self.devices.values():
                dev.online = False
        subprocess.run(["taskkill", "/IM", "OpenRGB.exe", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if openrgb_running() and winutil.openrgb_task_command() is not None:
            winutil.end_openrgb_task()  # started through our task: stopping it needs no prompt
            for _ in range(20):
                if not openrgb_running():
                    break
                time.sleep(0.25)
        if openrgb_running():
            # An OpenRGB started as administrator can only be stopped with administrator rights.
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "taskkill.exe", "/IM OpenRGB.exe /F", None, 0)
            if rc <= 32:
                self.status, self.message = "error", "OpenRGB runs as administrator; stopping it was cancelled."
                self.on_change()
                raise PermissionError(self.message)
            for _ in range(20):
                if not openrgb_running():
                    break
                time.sleep(0.25)
        self.status, self.message = "stopped", "OpenRGB stopped"
        self.on_change()

    def _detect_fan_leds(self) -> None:
        try:
            counts = commander_core.detect()
        except Exception as e:
            log.warning("Commander Core detection failed: %s", e)
            return
        if counts:
            log.info("Commander Core reports LEDs: %s", counts)
            self.settings["commander_core_leds"] = counts

    def _detected_sizes(self, dev: "OpenRGBDevice") -> dict[str, int | None]:
        """Zone name -> LED count reported by the hardware, for devices we can ask."""
        counts = self.settings.get("commander_core_leds")
        if dev._dev.name != COMMANDER_CORE or not counts:
            return {}
        return {f"Corsair Fan Header {i + 1}": n for i, n in enumerate(counts.get("ports", []))}

    @property
    def port(self) -> int:
        return int(self.settings.get("port", 6742))

    # ---- connection loop -----------------------------------------------------------

    def _run(self) -> None:
        launch_deadline = 0.0
        while not self._stop.is_set():
            if self.client is None:
                if self._try_connect():
                    launch_deadline = 0.0
                elif self.settings.get("auto_launch", True) and not self.launched and self.exe_path():
                    try:
                        self.launch(bool(self.settings.get("elevated")))
                    except Exception as e:
                        self.status, self.message = "error", str(e)
                        self.launched = True  # don't retry a cancelled UAC prompt in a loop
                    launch_deadline = time.monotonic() + 45
                elif not launch_deadline or time.monotonic() > launch_deadline:
                    if self.status != "error":
                        self.status = "offline"
                        self.message = ("OpenRGB isn't running." if self.exe_path()
                                        else "OpenRGB not found. Set its path in Settings.")
            else:
                self._health_check()
            # OpenRGB opens its server before detection finishes, so poll quickly until devices show up.
            self._wake.wait(2.0 if self.client is None else 1.0 if not self.devices else 5.0)
            self._wake.clear()

    def _try_connect(self) -> bool:
        try:
            client = OpenRGBClient("127.0.0.1", self.port, CLIENT_NAME, protocol_version=PROTOCOL_VERSION)
        except (OSError, OpenRGBDisconnected):
            return False
        except Exception as e:  # parsing errors from unexpected server data
            self.status, self.message = "error", f"OpenRGB error: {e}"
            return False
        with self.lock:
            self.client = client
            self._sync_devices()
        self.status = "connected"
        self.message = f"Connected · {len(self.devices)} devices" if self.devices else "Connected · detecting devices..."
        self.on_change()
        return True

    def _health_check(self) -> None:
        try:
            with self.lock:
                before = self._signature()
                self.client.comms.requestDeviceNum()  # also drains DEVICE_LIST_UPDATED notifications
                changed = self._signature() != before
                if changed:
                    self._sync_devices()
            if changed:
                self.message = f"Connected · {len(self.devices)} devices"
                self.on_change()
        except Exception:
            self.mark_disconnected()

    def mark_disconnected(self) -> None:
        with self.lock:
            if self.client:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
            self.client = None
            for dev in self.devices.values():
                dev.online = False
                dev.streaming = False
        self.status, self.message = "offline", "Lost connection to OpenRGB, reconnecting..."
        self.launched = False  # allow an automatic relaunch
        self.on_change()

    def _signature(self) -> list:
        return [(id(d), d.name, d.metadata.location) if d else None for d in self.client.devices]

    def _sync_devices(self) -> None:
        devs = [d for d in self.client.devices if d is not None]
        counts = Counter(d.name for d in devs)
        seen = set()
        for d in devs:
            key = "openrgb:" + (d.name if counts[d.name] == 1 else f"{d.name}@{d.metadata.location}")
            parts = split_plan(d)
            for suffix, label, zone_names in parts or [(None, None, None)]:
                k = f"{key}#{suffix}" if suffix else key
                seen.add(k)
                if k in self.devices:
                    self.devices[k].bind(d)
                elif suffix:
                    self.devices[k] = OpenRGBZoneDevice(self, k, key, d, zone_names, label)
                else:
                    self.devices[k] = OpenRGBDevice(self, k, d)
                self._apply_saved_zone_sizes(self.devices[k])
        for key, dev in self.devices.items():
            if key not in seen:
                dev.online = False

    def _apply_saved_zone_sizes(self, dev: OpenRGBDevice) -> None:
        """Size resizable zones: a size set in Settings wins, otherwise what the hardware reports."""
        saved = self.settings.get("zone_sizes", {}).get(dev.parent_key, {})
        dev.detected = self._detected_sizes(dev)
        for zone in dev.zones():
            want = saved.get(zone["name"])
            if want is None and zone["name"] in dev.detected:
                want = dev.detected[zone["name"]] or 0
            if zone["resizable"] and want is not None and want != zone["leds"] and zone["min"] <= want <= zone["max"]:
                try:
                    dev.resize_zone(zone["index"], int(want))
                except Exception as e:
                    log.warning("Resizing %s/%s failed: %s", dev.name, zone["name"], e)

    def save_zone_size(self, dev_id: str, index: int, size: int) -> None:
        dev = self.devices[dev_id]
        zone = next(z for z in dev.zones() if z["index"] == index)
        size = max(zone["min"], min(zone["max"], int(size)))
        dev.resize_zone(index, size)
        self.settings.setdefault("zone_sizes", {}).setdefault(dev.parent_key, {})[zone["name"]] = size

    def describe(self) -> dict:
        exe = self.exe_path()
        return {
            "status": self.status,
            "message": self.message,
            "path": str(exe) if exe else "",
            "bundled": exe == BUNDLED_EXE,
            "elevated": self.launched_elevated or bool(self.settings.get("elevated")),
            "pawnio": pawnio_installed(),
            "autoLaunch": bool(self.settings.get("auto_launch", True)),
        }
