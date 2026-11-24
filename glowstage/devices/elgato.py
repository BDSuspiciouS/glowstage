"""Elgato lights (Light Strip, Key Light, Ring Light) via the local HTTP API on port 9123.

Light Strips are color lights (hue/saturation/brightness); Key and Ring Lights are white
lights (brightness and color temperature). Which one it is follows from GET /elgato/lights.
The lights are slow to answer, so app effects hand the latest frame (averaged to one color)
to a sender thread that PUTs at most 10 times a second over one persistent connection.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

from ..colors import RGB, rgb_to_hsv
from ..state import LightState
from . import registry
from .base import Device
from .net import http_json

PORT = 9123


class ElgatoDevice(Device):
    kind = "elgato"
    group = "room"
    hardware_brightness = False   # streamed frames are already scaled by the brightness
    max_fps = 10

    def __init__(self, dev_id: str, name: str, host: str, port: int = PORT, product: str = ""):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.product = product
        self._color = False
        self._frame_lock = threading.Lock()
        self._latest: bytes | None = None
        self._wake: threading.Event | None = None
        self._halt: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}/elgato/{path}"

    def _put(self, light: dict, timeout: float = 3.0) -> None:
        http_json("PUT", self._url("lights"), {"numberOfLights": 1, "lights": [light]}, timeout=timeout)

    def connect(self) -> None:
        info = http_json("GET", self._url("accessory-info"), timeout=4.0) or {}
        lights = http_json("GET", self._url("lights"), timeout=4.0) or {}
        light = (lights.get("lights") or [{}])[0]
        self._color = "hue" in light
        self._current = light
        self.product = info.get("productName") or self.product
        self.reported_name = info.get("displayName") or self.product or "Elgato"
        self.pixel_count = 1
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        light = getattr(self, "_current", None)
        if not light:
            return None
        return {"on": bool(light.get("on")), "brightness": int(light.get("brightness", 100))}

    def _light_for(self, rgb: RGB, percent: float) -> dict:
        """The light's body for a color at a brightness (0-100): hue/saturation/brightness for
        color lights, brightness only for white ones."""
        if not self._color:
            return {"on": 1, "brightness": max(1, round(percent))}
        h, s, v = rgb_to_hsv(rgb)
        return {"on": 1, "brightness": max(1, round(percent * v)),
                "hue": round(h * 360) % 360, "saturation": round(s * 100)}

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._put(self._light_for(rgb, state.brightness))

    def power_off(self, timeout: float = 3.0) -> None:
        self._stop_sender()
        self._put({"on": 0}, timeout)

    # ---- streaming ---------------------------------------------------------------------

    def begin_stream(self, state: LightState) -> None:
        self._stop_sender()
        self._put({"on": 1})
        with self._frame_lock:
            self._latest = None
        wake, halt = threading.Event(), threading.Event()
        self._wake, self._halt = wake, halt
        self._thread = threading.Thread(target=self._sender, args=(wake, halt), daemon=True,
                                        name=f"elgato-{self.host}")
        self._thread.start()

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors or self._wake is None:
            return
        n = len(colors)
        avg = (sum(c[0] for c in colors) // n, sum(c[1] for c in colors) // n,
               sum(c[2] for c in colors) // n)
        h, s, v = rgb_to_hsv(avg)
        if v < 0.005:
            light: dict = {"on": 0}
        else:   # frames already include the brightness, so v is the brightness
            light = {"on": 1, "brightness": max(1, round(v * 100))}
            if self._color:
                light.update(hue=round(h * 360) % 360, saturation=round(s * 100))
        body = json.dumps({"numberOfLights": 1, "lights": [light]}).encode()
        with self._frame_lock:
            self._latest = body
        self._wake.set()

    def end_stream(self) -> None:
        self._stop_sender()

    def _stop_sender(self) -> None:
        thread, halt, wake = self._thread, self._halt, self._wake
        self._thread = self._halt = self._wake = None
        if halt is not None:
            halt.set()
            wake.set()
        if thread is not None:
            thread.join(2.0)

    def _sender(self, wake: threading.Event, halt: threading.Event) -> None:
        conn: http.client.HTTPConnection | None = None
        last: bytes | None = None
        interval = 1.0 / self.max_fps
        while not halt.is_set():
            wake.wait()
            wake.clear()
            if halt.is_set():
                break
            with self._frame_lock:
                body, self._latest = self._latest, None
            if body is None or body == last:
                continue
            started = time.monotonic()
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(self.host, self.port, timeout=2.0)
                conn.request("PUT", "/elgato/lights", body, {"Content-Type": "application/json"})
                conn.getresponse().read()
                last = body
            except Exception:   # connection dropped: reconnect on the next frame
                if conn is not None:
                    conn.close()
                conn = None
            halt.wait(max(0.0, interval - (time.monotonic() - started)))
        if conn is not None:
            conn.close()

    def extra_info(self) -> dict:
        return {"host": self.host, "label": f"Elgato · {self.product}" if self.product else "Elgato"}


def _split_host(raw: str, default_port: int) -> tuple[str, int]:
    raw = raw.strip().removeprefix("http://").rstrip("/")
    host, sep, port = raw.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return raw, default_port


def _found(host: str, port: int, info: dict, fallback_id: str = "", fallback_name: str = "") -> registry.Found:
    serial = str(info.get("serialNumber") or "").strip()
    label = info.get("displayName") or info.get("productName") or fallback_name or "Elgato"
    return registry.Found("elgato", f"elgato:{serial or fallback_id or host}", host, port, label,
                          {"product": info.get("productName", "")})


class Driver(registry.Driver):
    kind, title = "elgato", "Elgato"
    mdns = ("_elg._tcp.local.",)
    beta = True

    def from_mdns(self, service, name, host, port, props):
        port = port or PORT
        try:
            info = http_json("GET", f"http://{host}:{port}/elgato/accessory-info", timeout=2.0) or {}
        except Exception:
            info = {}
        return _found(host, port, info, str(props.get("id") or ""), name)

    def probe(self, values):
        host, port = _split_host(values.get("host", ""), PORT)
        if values.get("port"):
            port = int(values["port"])
        if not host:
            raise ValueError("Enter the light's IP address")
        try:
            info = http_json("GET", f"http://{host}:{port}/elgato/accessory-info", timeout=4.0)
        except Exception as e:
            raise ValueError(f"No Elgato light answered at {host} ({e})") from None
        if not isinstance(info, dict):
            raise ValueError(f"No Elgato light answered at {host}")
        return _found(host, port, info)

    def create(self, dev_id, entry):
        return ElgatoDevice(dev_id, entry.get("label") or "Elgato", entry["host"],
                            int(entry.get("port") or PORT), entry.get("product", ""))
