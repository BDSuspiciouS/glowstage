"""WLED (ESP8266/ESP32) strips.

Static colors and WLED's own effects go through the JSON API; app effects are streamed
over WLED's UDP realtime port using the DNRGB protocol. The stream never times out on the
strip: when WiFi drops packets for a moment, the strip holds the last frame instead of
falling back to its own state (which flashed white). Every other command ends the stream
explicitly with "live": false.
"""

from __future__ import annotations

import socket
import time
from itertools import chain

from ..colors import RGB, hex_to_rgb, rgb_to_hex
from ..state import LightState
from . import registry
from .base import Device
from .net import http_json

DNRGB = 4
REALTIME_TIMEOUT_S = 255   # 255 = stay in realtime mode until told otherwise
LEDS_PER_PACKET = 489


def _bri(percent: int) -> int:
    return max(1, round(percent * 2.55))


class WledDevice(Device):
    kind = "wled"
    group = "room"
    hardware_brightness = True
    max_fps = 50

    def __init__(self, dev_id: str, name: str, host: str):
        super().__init__(dev_id, name)
        self.host = host
        self.version = ""
        self.udp_port = 21324
        self._segments: list[int] = [0]
        self._effects: list[dict] = []
        self._palettes: list[dict] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _post(self, payload: dict, timeout: float = 3.0) -> None:
        url = f"http://{self.host}/json/state"
        try:
            http_json("POST", url, payload, timeout=timeout)
        except ConnectionError:  # WLED sometimes drops a connection on weak WiFi; once more
            time.sleep(0.2)
            http_json("POST", url, payload, timeout=timeout)

    def connect(self) -> None:
        data = http_json("GET", f"http://{self.host}/json", timeout=4.0)
        info, state = data["info"], data["state"]
        self.reported_name = info.get("name") or "WLED"
        self.pixel_count = int(info["leds"]["count"])
        self.version = info.get("ver", "")
        self.udp_port = int(info.get("udpport") or 21324)
        self._segments = [s["id"] for s in state.get("seg", []) if "id" in s] or [0]
        # Index 0 is "Solid" (covered by color mode); "RSVD"/"-" are placeholders.
        self._effects = [
            {"id": str(i), "name": n}
            for i, n in enumerate(data.get("effects", []))
            if i > 0 and n and n not in ("RSVD", "-")
        ]
        self._effects.sort(key=lambda e: e["name"].lower())
        self._palettes = [{"id": i, "name": n} for i, n in enumerate(data.get("palettes", []))]
        self._current = state
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        st = getattr(self, "_current", None)
        if not st:
            return None
        seg = (st.get("seg") or [{}])[0]
        cols = [c for c in seg.get("col", []) if isinstance(c, list) and len(c) >= 3]
        out = {"on": bool(st.get("on")), "brightness": round(st.get("bri", 255) / 2.55)}
        if cols:
            out["color"] = rgb_to_hex(tuple(cols[0][:3]))
        if len(cols) > 1 and any(cols[1][:3]):
            out["color2"] = rgb_to_hex(tuple(cols[1][:3]))
        fx = seg.get("fx", 0)
        if fx:
            out.update(mode="native", native=str(fx), speed=max(1, round(seg.get("sx", 128) / 2.55)),
                       palette=seg.get("pal"))
        return out

    def _segs(self, **fields) -> list[dict]:
        return [{"id": i, **fields} for i in self._segments]

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._post({
            "on": True, "bri": _bri(state.brightness), "live": False, "tt": round(self.moment * 10),
            "seg": self._segs(fx=0, col=[list(rgb)]),
        })

    def power_off(self, timeout: float = 3.0) -> None:
        self._post({"on": False, "live": False, "tt": round(self.moment * 10)}, timeout)

    def begin_stream(self, state: LightState) -> None:
        self._post({"on": True, "bri": _bri(state.brightness), "tt": 0})

    def send_frame(self, colors: list[RGB]) -> None:
        for start in range(0, len(colors), LEDS_PER_PACKET):
            chunk = colors[start:start + LEDS_PER_PACKET]
            header = bytes((DNRGB, REALTIME_TIMEOUT_S, start >> 8, start & 0xFF))
            self._sock.sendto(header + bytes(chain.from_iterable(chunk)), (self.host, self.udp_port))

    def end_stream(self) -> None:
        self._post({"live": False})

    def show_native(self, state: LightState) -> None:
        seg = {
            "fx": int(state.native),
            "sx": round(state.speed * 2.55),
            "col": [list(hex_to_rgb(state.color)), list(hex_to_rgb(state.color2)), [0, 0, 0]],
        }
        if state.palette is not None:
            seg["pal"] = int(state.palette)
        self._post({"on": True, "bri": _bri(state.brightness), "live": False, "seg": self._segs(**seg)})

    def native_effects(self) -> list[dict]:
        return self._effects

    def palettes(self) -> list[dict]:
        return self._palettes

    def extra_info(self) -> dict:
        return {"host": self.host, "version": self.version, "label": f"WLED {self.version}".strip()}


class Driver(registry.Driver):
    kind, title = "wled", "WLED"
    mdns = ("_wled._tcp.local.",)

    def from_mdns(self, service, name, host, port, props):
        mac = props.get("mac", "").lower().replace(":", "")
        return registry.Found("wled", f"wled:{mac or host}", host, port, name)

    def probe(self, values):
        host = values.get("host", "").strip().removeprefix("http://").rstrip("/")
        try:
            info = http_json("GET", f"http://{host}/json/info", timeout=4.0)
        except Exception as e:
            raise ValueError(f"No WLED answered at {host} ({e})") from None
        mac = str(info.get("mac", "")).lower().replace(":", "")
        return registry.Found("wled", f"wled:{mac or host}", host, 80, info.get("name") or "WLED")

    def create(self, dev_id, entry):
        return WledDevice(dev_id, entry.get("label") or "WLED", entry["host"])
