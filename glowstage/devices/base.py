"""Common interface for every kind of light the app controls."""

from __future__ import annotations

import threading

from ..colors import RGB, hex_to_rgb
from ..state import LightState


# "How lights change" (Setup → Lights): the seconds a new look takes, on every light that can glide
# (the local ones step through it themselves, manager.DeviceWorker; the others are told: WLED's tt,
# Hue's transitiontime, LIFX's duration, Home Assistant's transition, Nanoleaf's duration).
GLIDES = {"instant": 0.0, "smooth": 0.35, "slow": 2.0}

WARMTH_RANGE = 50        # the white balance slider goes from -50 (cooler) to +50 (warmer)
MAX_FLOOR = 20           # "brightness at 100%" can't go below this (percent)
KEEP_MIN = 0.4           # white balance never takes a channel below 40% of what it was
COLOR_ORDERS = ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")   # RGB: as it comes (the default)


def order_swap(order: str = "RGB") -> tuple[int, int, int] | None:
    """For a light whose red, green and blue are wired in another order: which of Glowstage's
    (r, g, b) goes into each of the three values it is sent. None for RGB, so that nothing is done.

    The name is the order the LIGHT uses, the way WLED, FastLED and most controller apps name it:
    a "GRB" strip takes its green first, then red, then blue. So to show red on it, Glowstage must
    send (0, 255, 0): the first value is the green one. Value i of what is sent is the channel
    that the letter at position i names, so GRB gives (g, r, b) and BRG gives (b, r, g)."""
    order = str(order).upper()
    if order not in COLOR_ORDERS:
        raise ValueError(f"'{order}' isn't a color order")
    if order == "RGB":
        return None
    return tuple("RGB".index(c) for c in order)


def balance_gains(warmth: int = 0, top: int = 100) -> tuple[float, float, float] | None:
    """How much of red, green and blue a light gets so that it matches the others, or None when
    nothing needs correcting. Warmer takes blue (and a little green) out, cooler takes red (and a
    little green) out; `top` is what full brightness means for this light (percent). Kept gentle:
    the white balance alone never takes a channel below KEEP_MIN."""
    t = max(-WARMTH_RANGE, min(WARMTH_RANGE, int(warmth))) / WARMTH_RANGE
    top = max(MAX_FLOOR, min(100, int(top))) / 100
    if t == 0 and top == 1:
        return None
    r = g = b = 1.0
    if t > 0:
        b, g = 1 - 0.5 * t, 1 - 0.15 * t
    elif t < 0:
        r, g = 1 + 0.5 * t, 1 + 0.15 * t
    return tuple(max(KEEP_MIN, c) * top for c in (r, g, b))


class Device:
    kind = "generic"
    group = "room"               # "pc" or "room", used to group devices in the UI
    hardware_brightness = False  # True when the device applies brightness itself to streamed frames
    max_fps = 30
    fades = False                # True: a new color glides in (local, quick lights; manager.DeviceWorker)
    fixture: dict | None = None  # its shape, for the light show: {"ring": 8} fans, {"geometry": [...]} keys
    gains: tuple[float, float, float] | None = None   # the light's balance (set_balance); None: as it comes
    swap: tuple[int, int, int] | None = None          # its color order (order_swap); None: RGB, as it comes
    cap: int | None = None       # the night limit (night.py): no brightness above this now; None: none
    glide = 0.35                 # seconds a new color, brightness or on/off takes ("How lights change", GLIDES)
    moment = 0.35                # ... for the change being applied now: 0 for a drag or a flash (DeviceWorker sets it)

    def __init__(self, dev_id: str, name: str):
        self.id = dev_id
        self.name = name
        self.online = False
        self.error: str | None = None
        self.pixel_count = 0
        self.streaming = False
        self.lock = threading.RLock()

    # ---- balance: what the light is sent, so that it looks like the others -------------------

    def set_balance(self, warmth: int = 0, top: int = 100, order: str = "RGB") -> None:
        self.gains = balance_gains(warmth, top)
        self.swap = order_swap(order)

    def balanced(self, rgb: RGB) -> RGB:
        """One color, corrected for this light. Exactly the same color when the light isn't balanced."""
        g = self.gains
        if g is not None:
            rgb = (min(255, int(rgb[0] * g[0] + 0.5)), min(255, int(rgb[1] * g[1] + 0.5)), min(255, int(rgb[2] * g[2] + 0.5)))
        # The color order is the very last step: it only moves values around.
        s = self.swap
        return rgb if s is None else (rgb[s[0]], rgb[s[1]], rgb[s[2]])

    def balanced_frame(self, colors: list[RGB]) -> list[RGB]:
        """A whole frame, corrected. The very same list when the light isn't balanced (nearly always)."""
        g = self.gains
        if g is not None:
            gr, gg, gb = g
            colors = [(min(255, int(r * gr + 0.5)), min(255, int(gc * gg + 0.5)), min(255, int(b * gb + 0.5))) for r, gc, b in colors]
        # The color order is the very last step: it only moves values around.
        s = self.swap
        if s is None:
            return colors
        i, j, k = s
        return [(c[i], c[j], c[k]) for c in colors]

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
        if self.cap is not None and state.on and state.brightness > self.cap:
            state = state.patched({"brightness": self.cap})     # (the night limit: night.py)
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
                    self.show_color(self.balanced(hex_to_rgb(state.color)), state)

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
