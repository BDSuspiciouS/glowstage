"""ASRock Radeon graphics cards (Polychrome): the card's RGB chip at I2C address 0x36.

Protocol, as SignalRGB's ASRock GPU plugin drives this card. Every message is a 12-byte
block written to register 0x00 (no count byte):
  query  [0x14, 0x00, what]           then read 12 bytes back from register 0x00
         what=0x01: bitmask of enabled channels in bytes 4-5 (0xC9 = 0, 3, 6, 7 here)
         what=0x02: LED count per channel from byte 4 on
  color  [0x10, 0x00, channel, 0x01, R, G, B, 0x80, 0xFF, 0x00, 0x1A]
The chip needs ~20 ms between writes, and each channel takes one color (no per-LED data).
Every color command makes the LEDs blink briefly, even when it repeats the current color
(tested on the RX 7900 XTX Taichi), so animated effects can't run on it without flashing:
effects are shown as one still frame instead.
The card's 3-pin ARGB header (channel 3) drives whatever is plugged into it.
"""

from __future__ import annotations

import logging
import threading
import time

from ..colors import RGB, hex_to_rgb, scale
from ..effects import EFFECTS
from ..state import LightState
from .amd_i2c import AmdGpu, amd_gpus
from .base import Device

log = logging.getLogger(__name__)

ASROCK_SUBVENDOR = 0x1849
ADDRESS = 0x36
BLOCK = 12
WRITE_GAP_S = 0.025
# Every color command also re-sets the channel's lighting mode, and a stream of them makes
# the LEDs flicker. So at most 5 updates a second per channel, and small steps wait a
# little longer so they get folded into the next visible change.
MIN_INTERVAL_S = 0.2
SMALL_STEP = 24            # RGB distance (SignalRGB skips changes under ~22)
SMALL_STEP_WAIT_S = 0.5
CMD_QUERY, CMD_COLOR = 0x14, 0x10
QUERY_CHANNELS, QUERY_LED_COUNTS = 0x01, 0x02
ARGB_HEADER = 3
CHANNEL_NAMES = {0: "Backplate", 3: "ARGB header", 6: "Top side", 7: "Fan"}


class AsrockGpuController:
    """Owns the I2C conversation; a writer thread pushes the latest color of each channel."""

    def __init__(self, gpu: AmdGpu):
        self.gpu = gpu
        self.lock = threading.RLock()
        self.channels: list[int] = []
        self.led_counts: list[int] = []
        self.error: str | None = None
        self.on_status = lambda: None
        self._want: dict[int, RGB] = {}
        self._sent: dict[int, RGB] = {}
        self._last_write: dict[int, float] = {}
        self._cv = threading.Condition()

    def _block(self, *payload: int) -> bytes:
        return bytes(payload).ljust(BLOCK, b"\0")

    def _query(self, what: int) -> bytes:
        self.gpu.write_block(ADDRESS, 0x00, self._block(CMD_QUERY, 0x00, what))
        return self.gpu.read_block(ADDRESS, 0x00, BLOCK)

    def probe(self) -> None:
        enabled = self._query(QUERY_CHANNELS)
        if enabled[3] != QUERY_CHANNELS:  # byte 3 of a reply echoes the query; byte 2 is a sequence counter
            raise OSError(0, f"Unexpected reply from the graphics card RGB chip: {enabled[:4].hex()}")
        bits = enabled[4] | enabled[5] << 8
        self.channels = [i for i in range(16) if bits >> i & 1]
        self.led_counts = list(self._query(QUERY_LED_COUNTS)[4:])
        log.info("ASRock GPU channels %s, LED counts %s", self.channels, self.led_counts)

    def start(self) -> None:
        threading.Thread(target=self._run, name="asrock-gpu", daemon=True).start()

    def set(self, channel: int, rgb: RGB) -> None:
        with self._cv:
            self._want[channel] = rgb
            self._cv.notify()

    def flush(self, timeout: float) -> bool:
        """Wait until every channel shows its latest color (used at shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self._sent.get(ch) == rgb for ch, rgb in list(self._want.items())):
                return True
            time.sleep(0.02)
        return False

    def _due(self, now: float) -> tuple[list[tuple[int, RGB]], float | None]:
        """Channels to write now, and the seconds until the next held-back one is due."""
        due, wait = [], None
        for ch, rgb in self._want.items():
            sent = self._sent.get(ch)
            if sent == rgb:
                continue
            small = sent is not None and sum((a - b) ** 2 for a, b in zip(sent, rgb)) ** 0.5 < SMALL_STEP
            ready_at = self._last_write.get(ch, 0.0) + (SMALL_STEP_WAIT_S if small else MIN_INTERVAL_S)
            if now >= ready_at:
                due.append((ch, rgb))
            else:
                wait = ready_at - now if wait is None else min(wait, ready_at - now)
        return due, wait

    def _run(self) -> None:
        while True:
            with self._cv:
                while True:
                    todo, wait = self._due(time.monotonic())
                    if todo:
                        break
                    self._cv.wait(timeout=wait)  # None: sleep until a new color arrives
            for ch, rgb in todo:
                try:
                    self.gpu.write_block(ADDRESS, 0x00, self._block(CMD_COLOR, 0x00, ch, 0x01, *rgb,
                                                                    0x80, 0xFF, 0x00, 0x1A))
                    self._sent[ch] = rgb
                    self._last_write[ch] = time.monotonic()
                    if self.error:
                        self.error = None
                        self.on_status()
                except OSError as e:
                    log.warning("ASRock GPU write failed: %s", e)
                    self.error = "Graphics card isn't answering"
                    self.on_status()
                    time.sleep(2.0)
                    break
                time.sleep(WRITE_GAP_S)


class AsrockGpuDevice(Device):
    kind = "asrock_gpu"
    group = "pc"
    hardware_brightness = False
    max_fps = 10   # the controller paces the actual writes; see MIN_INTERVAL_S

    def __init__(self, controller: AsrockGpuController, dev_id: str, name: str, channels: list[int], label: str):
        super().__init__(dev_id, name)
        self.controller = controller
        self.lock = controller.lock
        self.channels = channels
        self.reported_name = name
        self.label = label
        self.pixel_count = len(channels)
        self.online = True

    @property
    def error(self) -> str | None:
        return self.controller.error if hasattr(self, "controller") else None

    @error.setter
    def error(self, value) -> None:
        pass  # the controller owns the error state

    def apply(self, state: LightState) -> None:
        if not (state.on and state.mode == "effect"):
            return super().apply(state)
        # Each write blinks the chip, so show the effect as a still frame instead of streaming it.
        with self.lock:
            self.streaming = False
            effect = EFFECTS.get(state.effect) or EFFECTS["rainbow"]
            colors = effect.render(0.0, self.pixel_count, state, {}, {})
            if not any(any(c) for c in colors):  # e.g. screen sync before it has sampled anything
                colors = [hex_to_rgb(state.color)] * self.pixel_count
            self.send_frame([scale(c, state.brightness / 100) for c in colors])

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self.send_frame([scale(rgb, state.brightness / 100)] * self.pixel_count)

    def wait_written(self, timeout: float) -> bool:
        return self.controller.flush(timeout)

    def power_off(self) -> None:
        self.send_frame([(0, 0, 0)] * self.pixel_count)

    def send_frame(self, colors: list[RGB]) -> None:
        for ch, rgb in zip(self.channels, colors):
            self.controller.set(ch, tuple(int(c) for c in rgb))

    def extra_info(self) -> dict:
        zones = ", ".join(CHANNEL_NAMES.get(ch, f"Channel {ch}") for ch in self.channels)
        return {"label": self.label, "zones": [], "channels": zones, "stillEffects": True}


def discover() -> list[AsrockGpuDevice]:
    """Devices for every ASRock Radeon card whose RGB chip answers. Never raises."""
    devices: list[AsrockGpuDevice] = []
    for gpu in amd_gpus():
        if gpu.subvendor != ASROCK_SUBVENDOR:
            continue
        ctrl = AsrockGpuController(gpu)
        try:
            ctrl.probe()
        except OSError as e:
            log.warning("ASRock GPU RGB chip did not answer: %s", e)
            continue
        if not ctrl.channels:
            continue
        ctrl.start()
        card = gpu.name.replace("AMD Radeon", "ASRock").strip() or "ASRock graphics card"
        base = f"asrock-gpu:{gpu.device:04x}-{gpu.subdevice:04x}"
        lights = [ch for ch in ctrl.channels if ch != ARGB_HEADER]
        if lights:
            devices.append(AsrockGpuDevice(ctrl, base, "Graphics card", lights, f"{card} · {len(lights)} zones"))
        if ARGB_HEADER in ctrl.channels:
            devices.append(AsrockGpuDevice(ctrl, base + ":argb", "GPU ARGB header", [ARGB_HEADER],
                                           "3-pin ARGB output on the graphics card"))
    return devices
