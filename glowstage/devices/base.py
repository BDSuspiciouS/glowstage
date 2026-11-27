"""Common interface for every kind of light the app controls."""

from __future__ import annotations

import threading

from ..colors import RGB, hex_to_rgb
from ..state import LightState


class Device:
    kind = "generic"
    group = "room"               # "pc" or "room", used to group devices in the UI
    hardware_brightness = False  # True when the device applies brightness itself to streamed frames
    max_fps = 30
    fixture: dict | None = None  # its shape, for the light show: {"ring": 8} fans, {"geometry": [...]} keys

    def __init__(self, dev_id: str, name: str):
        self.id = dev_id
        self.name = name
        self.online = False
        self.error: str | None = None
        self.pixel_count = 0
        self.streaming = False
        self.lock = threading.RLock()

    # ---- implemented by subclasses -------------------------------------------------

    def connect(self) -> None:
        """Fetch device info. Raise on failure."""

    def show_color(self, rgb: RGB, state: LightState) -> None:
        raise NotImplementedError

    def power_off(self) -> None:
        raise NotImplementedError

    def begin_stream(self, state: LightState) -> None:
        """Prepare the device to receive frames from the effect engine."""

    def send_frame(self, colors: list[RGB]) -> None:
        raise NotImplementedError

    def end_stream(self) -> None:
        """Hand control back from the effect engine."""

    def show_native(self, state: LightState) -> None:
        raise NotImplementedError

    def native_effects(self) -> list[dict]:
        return []

    def palettes(self) -> list[dict]:
        return []

    def extra_info(self) -> dict:
        return {}

    def read_state(self) -> dict | None:
        """The device's current state (as a LightState patch), used when the app has none saved."""
        return None

    # ---- shared ----------------------------------------------------------------------

    def apply(self, state: LightState) -> None:
        """Apply a full desired state. Runs on the device's worker thread."""
        with self.lock:
            if not state.on:
                self._stop_stream()
                self.power_off()
            elif state.mode == "effect":
                self.begin_stream(state)
                self.streaming = True
            else:
                self._stop_stream()
                if state.mode == "native" and state.native is not None and self.native_effects():
                    self.show_native(state)
                else:
                    self.show_color(hex_to_rgb(state.color), state)

    def _stop_stream(self) -> None:
        if self.streaming:
            self.streaming = False
            self.end_stream()

    def desk_item(self) -> str:
        """What sort of thing on the desk this is ("KEYBOARD", "MOUSEMAT", "MOUSE", "HEADSET_STAND",
        "HEADSET", "KEYPAD"), for where it stands on the stage; "" for anything else."""
        return ""

    def describe(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "group": self.group,
            "name": self.name,
            "online": self.online,
            "error": self.error,
            "pixels": self.pixel_count,
            "hasNative": bool(self.native_effects()),
            **self.extra_info(),
        }
