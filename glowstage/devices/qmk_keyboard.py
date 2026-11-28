"""Keyboards running QMK with SignalRGB's lighting protocol (SRGBmods), such as the MCHOSE GX87.

Raw HID interface (usage page 0xFF60, usage 0x61), 32-byte reports, protocol 1.0.6:
  0x21 QMK version · 0x22 protocol version · 0x23 unique id · 0x27 LED count · 0x28 firmware type
  0x25 the computer takes over the lighting · 0x26 back to the keyboard's own lighting
  0x24 [first LED, count, R, G, B, ...] with up to 9 LEDs per report
Each key is addressed on its own; pixels are ordered left to right so effects sweep across.
"""

from __future__ import annotations

import logging
import threading
import time
from itertools import chain

from ..colors import RGB, scale
from ..state import LightState
from .base import Device
from .hidio import HidDevice, hid_interfaces

log = logging.getLogger(__name__)

RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61
CMD_PROTOCOL, CMD_LEDS, CMD_FIRMWARE = 0x22, 0x27, 0x28
CMD_SOFTWARE, CMD_HARDWARE, CMD_COLORS = 0x25, 0x26, 0x24
LEDS_PER_REPORT = 9

# LED positions (x, y) in LED order, from each keyboard's QMK keyboard.json rgb_matrix layout.
LAYOUTS: dict[tuple[int, int], list[tuple[int, int]]] = {
    (0xFFFE, 0x006F): [  # MCHOSE GX87 (keyboards/mchose/gx87 in MCHOSE's QMK fork)
        (6, 7), (21, 7), (34, 7), (46, 7), (58, 7), (74, 7), (86, 7), (98, 7), (110, 7), (126, 7), (138, 7),
        (150, 7), (163, 7), (178, 7), (193, 7), (206, 7), (218, 7),
        (6, 22), (18, 22), (31, 22), (43, 22), (55, 22), (68, 22), (80, 22), (92, 22), (104, 22), (117, 22),
        (129, 22), (141, 22), (153, 22), (172, 22), (193, 22), (206, 22), (218, 22),
        (9, 32), (25, 32), (37, 32), (49, 32), (61, 32), (74, 32), (86, 32), (98, 32), (110, 32), (123, 32),
        (135, 32), (147, 32), (160, 32), (175, 32), (193, 32), (206, 32), (218, 32),
        (11, 42), (28, 42), (40, 42), (52, 42), (64, 42), (77, 42), (89, 42), (101, 42), (114, 42), (126, 42),
        (138, 42), (150, 42), (170, 42),
        (14, 52), (34, 52), (46, 52), (58, 52), (71, 52), (83, 52), (95, 52), (107, 52), (120, 52), (132, 52),
        (144, 52), (167, 52), (206, 52),
        (8, 62), (23, 62), (38, 62), (84, 62), (130, 62), (146, 62), (161, 62), (176, 62), (193, 62), (206, 62),
        (218, 62),
        (181, 42), (193, 42), (206, 42), (218, 42),
    ],
}


def _raw_interfaces() -> list[dict]:
    return [i for i in hid_interfaces() if i["usage_page"] == RAW_USAGE_PAGE and i["usage"] == RAW_USAGE]


class QmkKeyboard(Device):
    kind = "qmk"
    group = "pc"
    fades = True                 # local and quick: a new look glides in (manager.DeviceWorker)
    hardware_brightness = False
    max_fps = 25   # a full frame is 11 reports (~40 ms), sent by the writer thread below

    def __init__(self, info: dict):
        super().__init__(f"qmk:{info['vid']:04x}:{info['pid']:04x}", info["product"] or "Keyboard")
        self.vid, self.pid = info["vid"], info["pid"]
        self.reported_name = self.name
        self.protocol = ""
        self._hid: HidDevice | None = None
        self._software = False
        self._order: list[int] = []
        # The effect engine must never wait on USB writes: frames go to a writer thread and
        # only the newest one is sent.
        self._pending: list[RGB] | None = None
        self._busy = False
        self._cv = threading.Condition()
        threading.Thread(target=self._writer, name=f"writer:{self.id}", daemon=True).start()

    # ---- connection ------------------------------------------------------------------------

    def _ask(self, cmd: int) -> bytes | None:
        """Send a query and return its reply (the firmware may add an empty echo; skip it)."""
        self._hid.flush()
        self._hid.write(bytes([cmd]))
        for _ in range(4):
            reply = self._hid.read(400)
            if reply is None:
                return None
            if reply[:1] == bytes([cmd]):
                return reply
        return None

    def connect(self) -> None:
        self.close()
        match = next((i for i in _raw_interfaces() if (i["vid"], i["pid"]) == (self.vid, self.pid)), None)
        if not match:
            raise OSError(0, "Keyboard not found (unplugged, or not in wired mode)")
        self._hid = HidDevice(match["path"])
        version = self._ask(CMD_PROTOCOL)
        leds = self._ask(CMD_LEDS)
        if not version or not leds or not leds[1]:
            self.close()
            raise OSError(0, "Keyboard doesn't speak SignalRGB's protocol")
        self.protocol = f"{version[1]}.{version[2]}.{version[3]}"
        count = leds[1]
        positions = LAYOUTS.get((self.vid, self.pid))
        if not positions or len(positions) != count:
            positions = [(i, 0) for i in range(count)]
        self._order = sorted(range(count), key=lambda i: positions[i])   # left to right, then top down
        # Where each frame pixel sits on the board (0..1 both ways), for the light show's ripples.
        if (self.vid, self.pid) in LAYOUTS and len(positions) == count:
            self.fixture = {"geometry": [(positions[i][0] / 224, positions[i][1] / 64) for i in self._order]}
        self.pixel_count = count
        self._software = False
        self.online, self.error = True, None

    def close(self) -> None:
        if self._hid:
            self._hid.close()
            self._hid = None

    # ---- lighting --------------------------------------------------------------------------

    def _take_over(self) -> None:
        if not self._software:
            self._hid.write(bytes([CMD_SOFTWARE]))
            self._software = True

    def send_frame(self, colors: list[RGB]) -> None:
        if len(colors) != self.pixel_count:
            return
        with self._cv:
            self._pending = list(colors)
            self._cv.notify()

    def wait_written(self, timeout: float) -> bool:
        """Wait until the newest frame has gone out (used at shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cv:
                if self._pending is None and not self._busy:
                    return True
            time.sleep(0.01)
        return False

    def _writer(self) -> None:
        while True:
            with self._cv:
                while self._pending is None:
                    self._cv.wait()
                colors, self._pending, self._busy = self._pending, None, True
            try:
                if self._hid and self.online:
                    self._write_frame(colors)
            except OSError as e:
                log.warning("%s: %s", self.name, e)
                self.online, self.error = False, "Not responding (unplugged?)"
                self.close()
            finally:
                with self._cv:
                    self._busy = False

    def _write_frame(self, colors: list[RGB]) -> None:
        self._take_over()
        by_led: list[RGB] = [(0, 0, 0)] * self.pixel_count
        for px, led in enumerate(self._order):
            by_led[led] = colors[px]
        self._hid.flush()  # VIA builds echo every report back; we don't need those
        for start in range(0, self.pixel_count, LEDS_PER_REPORT):
            chunk = by_led[start:start + LEDS_PER_REPORT]
            self._hid.write(bytes([CMD_COLORS, start, len(chunk)]) + bytes(chain.from_iterable(chunk)))

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self.send_frame([scale(rgb, state.brightness / 100)] * self.pixel_count)

    def power_off(self) -> None:
        self.send_frame([(0, 0, 0)] * self.pixel_count)

    def begin_stream(self, state: LightState) -> None:
        self._take_over()

    def native_effects(self) -> list[dict]:
        return [{"id": "hardware", "name": "Keyboard's own lighting"}]

    def show_native(self, state: LightState) -> None:
        self.wait_written(0.5)  # don't let a queued frame take the lighting back afterwards
        self._hid.write(bytes([CMD_HARDWARE]))
        self._software = False

    def extra_info(self) -> dict:
        return {"label": f"QMK · {self.pixel_count} keys", "protocol": self.protocol}

    def desk_item(self) -> str:
        return "KEYBOARD"


def discover() -> list[QmkKeyboard]:
    """Keyboards that answer SignalRGB's protocol. Never raises."""
    found = []
    seen = set()
    for info in _raw_interfaces():
        key = (info["vid"], info["pid"])
        if key in seen:
            continue
        seen.add(key)
        kb = QmkKeyboard(info)
        try:
            kb.connect()
        except (OSError, TimeoutError) as e:
            log.info("%s: no SignalRGB lighting (%s)", info["product"] or key, e)
            continue
        log.info("Keyboard %s: SignalRGB protocol %s, %d LEDs", kb.name, kb.protocol, kb.pixel_count)
        found.append(kb)
    return found
