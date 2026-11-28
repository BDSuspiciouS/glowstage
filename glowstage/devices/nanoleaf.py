"""Nanoleaf panels (Shapes, Canvas, Light Panels, Elements, Lines) via the local OpenAPI.

Pairing: hold the controller's power button for 5-7 seconds until the lights flash, then
call pair() within 30 seconds. App effects are streamed with External Control v2 (UDP).
"""

from __future__ import annotations

import socket
import struct

from ..colors import RGB, hsv, kelvin_to_rgb, rgb_to_hex, rgb_to_hsv
from ..state import LightState
from . import registry
from .base import Device
from .hub import NotPairedError
from .net import HttpError, http_json

STREAM_PORT = 60222
# Shape types without LEDs: Shapes controller, Lines connector, controller cap, power connector.
NO_LED_SHAPES = {12, 16, 19, 20}
PAIRING = ("Hold the controller's power button for 5-7 seconds until the lights flash, "
           "then press Pair within 30 seconds.")


class NanoleafDevice(Device):
    kind = "nanoleaf"
    group = "room"
    hardware_brightness = True
    max_fps = 20

    def __init__(self, dev_id: str, name: str, host: str, port: int = 16021, token: str | None = None):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.token = token
        self.model = ""
        self.firmware = ""
        self._panel_ids: list[int] = []
        self._effects: list[dict] = []
        self._ext_active = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @property
    def paired(self) -> bool:
        return bool(self.token)

    def _url(self, path: str = "") -> str:
        return f"http://{self.host}:{self.port}/api/v1/{self.token}/{path}"

    def pair(self) -> str:
        """Request an auth token. The controller must be in pairing mode."""
        try:
            data = http_json("POST", f"http://{self.host}:{self.port}/api/v1/new", timeout=5.0)
        except HttpError as e:
            if e.status == 403:
                raise NotPairedError("The panels aren't in pairing mode. " + PAIRING) from None
            raise
        self.token = data["auth_token"]
        return self.token

    def connect(self) -> None:
        if not self.token:
            self.online = False
            self.error = "Not paired"
            raise NotPairedError("Not paired")
        try:
            data = http_json("GET", self._url(), timeout=4.0)
        except HttpError as e:
            if e.status in (401, 403):
                self.token = None
                raise NotPairedError("Pairing expired, pair again") from None
            raise
        self.reported_name = data.get("name") or "Nanoleaf"
        self.model = data.get("model", "")
        self.firmware = data.get("firmwareVersion", "")
        positions = data.get("panelLayout", {}).get("layout", {}).get("positionData", [])
        panels = [p for p in positions if p.get("shapeType") not in NO_LED_SHAPES and p.get("panelId")]
        panels.sort(key=lambda p: (p.get("x", 0), p.get("y", 0)))  # left-to-right for waves
        self._panel_ids = [p["panelId"] for p in panels]
        self.pixel_count = len(self._panel_ids)
        effects = data.get("effects", {}).get("effectsList", [])
        self._effects = [{"id": n, "name": n} for n in sorted(effects, key=str.lower)]
        self._current = (data.get("state", {}), data.get("effects", {}).get("select"))
        self._ext_active = False
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        st, selected = getattr(self, "_current", ({}, None))
        if not st:
            return None
        value = lambda key, default: (st.get(key) or {}).get("value", default)  # noqa: E731
        out = {"on": bool(value("on", True)), "brightness": int(value("brightness", 100))}
        if selected and not selected.startswith("*"):   # "*Solid*", "*Static*", "*ExtControl*" are built-ins
            out.update(mode="native", native=selected)
        elif st.get("colorMode") == "ct":
            out["color"] = rgb_to_hex(kelvin_to_rgb(value("ct", 4000)))
        else:
            out["color"] = rgb_to_hex(hsv(value("hue", 0) / 360, value("sat", 0) / 100, 1))
        return out

    def _state(self, payload: dict, timeout: float = 3.0) -> None:
        http_json("PUT", self._url("state"), payload, timeout=timeout)

    def show_color(self, rgb: RGB, state: LightState) -> None:
        h, s, v = rgb_to_hsv(rgb)
        self._state({
            "on": {"value": True},
            "brightness": {"value": round(state.brightness * max(v, 0.01)), "duration": round(self.moment)},
            "hue": {"value": round(h * 360) % 360},
            "sat": {"value": round(s * 100)},
        })

    def power_off(self, timeout: float = 3.0) -> None:
        self._state({"on": {"value": False}}, timeout)

    def begin_stream(self, state: LightState) -> None:
        if not self._ext_active:
            http_json("PUT", self._url("effects"), {
                "write": {"command": "display", "animType": "extControl", "extControlVersion": "v2"}
            })
            self._ext_active = True
        self._state({"on": {"value": True}, "brightness": {"value": state.brightness, "duration": 0}})

    def send_frame(self, colors: list[RGB]) -> None:
        buf = bytearray(struct.pack(">H", len(self._panel_ids)))
        for pid, (r, g, b) in zip(self._panel_ids, colors):
            buf += struct.pack(">HBBBBH", pid, r, g, b, 0, 1)  # transition 1 = 100 ms
        self._sock.sendto(bytes(buf), (self.host, STREAM_PORT))

    def end_stream(self) -> None:
        self._ext_active = False  # the next color/effect call takes the panels out of extControl

    def show_native(self, state: LightState) -> None:
        http_json("PUT", self._url("effects"), {"select": state.native})
        self._state({"on": {"value": True}, "brightness": {"value": state.brightness, "duration": 0}})

    def native_effects(self) -> list[dict]:
        return self._effects

    def extra_info(self) -> dict:
        label = "Nanoleaf"
        if self.pixel_count:
            label += f" · {self.pixel_count} panels"
        return {"host": self.host, "paired": self.paired, "model": self.model,
                "firmware": self.firmware, "label": label}


class Driver(registry.Driver):
    kind, title = "nanoleaf", "Nanoleaf"
    mdns = ("_nanoleafapi._tcp.local.",)
    pairing = PAIRING

    def from_mdns(self, service, name, host, port, props):
        return registry.Found("nanoleaf", f"nanoleaf:{props.get('id', host).upper()}", host, port or 16021, name)

    def probe(self, values):
        host = values.get("host", "").strip()
        try:   # without a token the controller answers 401: that's enough to know it's there
            http_json("GET", f"http://{host}:16021/api/v1/", timeout=4.0)
        except HttpError:
            pass
        except Exception as e:
            raise ValueError(f"No Nanoleaf answered at {host} ({e})") from None
        return registry.Found("nanoleaf", f"nanoleaf:{host}", host, 16021, "Nanoleaf")

    def create(self, dev_id, entry):
        return NanoleafDevice(dev_id, entry.get("label") or "Nanoleaf", entry["host"],
                              int(entry.get("port") or 16021), entry.get("token"))

    def pair(self, target):
        return {"token": target.pair()}
