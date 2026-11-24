"""Twinkly string lights and strips, via the unofficial but well documented XLED API.

Static colors use the HTTP API (login with a challenge, then an X-Auth-Token header on every
call). App effects switch the lights to "rt" (realtime) mode and stream protocol v3 UDP
packets to port 7777: one packet per 900 bytes of LED data.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from itertools import chain

from ..colors import RGB
from ..state import LightState
from . import registry
from .base import Device
from .net import HttpError

UDP_PORT = 7777
DISCOVER_ADDR = ("255.255.255.255", 5555)
MAX_PACKET_DATA = 900


def parse_discover(data: bytes) -> registry.Found | None:
    """The answer to b"\\x01discover": IPv4 (reversed) + b"OK" + the device name (NUL-terminated)."""
    if len(data) < 6 or data[4:6] != b"OK":
        return None
    ip = ".".join(str(b) for b in reversed(data[:4]))
    name = data[6:].split(b"\x00")[0].decode("utf-8", "replace").strip()
    return registry.Found("twinkly", f"twinkly:{ip}", ip, 80, name or "Twinkly")


def discover(timeout: float = 2.0, target: tuple[str, int] = DISCOVER_ADDR) -> list[registry.Found]:
    found: dict[str, registry.Found] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.sendto(b"\x01discover", target)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                data, _ = sock.recvfrom(1024)
            except OSError:
                break
            item = parse_discover(data)
            if item:
                found[item.dev_id] = item
    finally:
        sock.close()
    return list(found.values())


class TwinklyDevice(Device):
    kind = "twinkly"
    group = "room"
    hardware_brightness = False   # frames are scaled by the app; the lights are set to 100% for the stream
    max_fps = 25

    def __init__(self, dev_id: str, name: str, host: str, port: int = 80):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.udp_port = UDP_PORT
        self.profile = "RGB"
        self.product = ""
        self._token: str | None = None
        self._token8 = b"\x00" * 8
        self._auth_lock = threading.RLock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ---- HTTP API ----------------------------------------------------------------------

    def _raw(self, method: str, path: str, payload=None, headers: dict | None = None, timeout: float = 4.0):
        data = json.dumps(payload).encode() if payload is not None else None
        hdrs = dict(headers or {})
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://{self.host}:{self.port}/xled/v1/{path}",
                                     data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, e.reason) from None
        try:
            return json.loads(body) if body else None
        except ValueError:
            return None

    def _login(self, timeout: float = 4.0) -> None:
        with self._auth_lock:
            challenge = base64.b64encode(os.urandom(32)).decode()
            data = self._raw("POST", "login", {"challenge": challenge}, timeout=timeout)
            try:
                token, response = data["authentication_token"], data["challenge-response"]
            except (TypeError, KeyError):
                raise ConnectionError("The Twinkly refused to log in") from None
            self._raw("POST", "verify", {"challenge-response": response},
                      {"X-Auth-Token": token}, timeout)
            self._token = token
            raw = base64.b64decode(token + "=" * (-len(token) % 4))
            self._token8 = raw[:8].ljust(8, b"\x00")

    def _request(self, method: str, path: str, payload=None, timeout: float = 4.0, _retry: bool = True):
        if self._token is None:
            self._login(timeout)
        try:
            return self._raw(method, path, payload, {"X-Auth-Token": self._token or ""}, timeout)
        except HttpError as e:
            if e.status == 401 and _retry:   # tokens last about 4 hours
                self._token = None
                return self._request(method, path, payload, timeout, _retry=False)
            raise

    # ---- Device ------------------------------------------------------------------------

    def connect(self) -> None:
        self._login(4.0)
        info = self._request("GET", "gestalt", timeout=4.0) or {}
        self.pixel_count = int(info["number_of_led"])
        self.profile = str(info.get("led_profile") or "RGB").upper()
        self.product = info.get("product_code") or info.get("product_name") or ""
        self.reported_name = info.get("device_name") or "Twinkly"
        self.online = True
        self.error = None

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._request("POST", "led/mode", {"mode": "color"})
        self._request("POST", "led/color", {"red": rgb[0], "green": rgb[1], "blue": rgb[2]})
        self._set_brightness(state.brightness)

    def _set_brightness(self, percent: int) -> None:
        self._request("POST", "led/out/brightness",
                      {"mode": "enabled", "type": "A", "value": max(0, min(100, int(percent)))})

    def power_off(self, timeout: float = 3.0) -> None:
        self._request("POST", "led/mode", {"mode": "off"}, timeout=timeout)

    def begin_stream(self, state: LightState) -> None:
        self._set_brightness(100)   # the app scales the frames itself
        self._request("POST", "led/mode", {"mode": "rt"})

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        if self.profile == "RGBW":   # W, R, G, B with W = 0
            data = bytes(x for c in colors for x in (0, c[0], c[1], c[2]))
        else:
            data = bytes(chain.from_iterable(colors))
        header = b"\x03" + self._token8 + b"\x00\x00"
        for number, start in enumerate(range(0, len(data), MAX_PACKET_DATA)):
            packet = header + bytes((number & 0xFF,)) + data[start:start + MAX_PACKET_DATA]
            self._sock.sendto(packet, (self.host, self.udp_port))

    def end_stream(self) -> None:
        pass   # the next mode call takes the lights out of rt

    def extra_info(self) -> dict:
        return {"host": self.host, "label": f"Twinkly · {self.product}" if self.product else "Twinkly"}


def _split_host(raw: str, default_port: int) -> tuple[str, int]:
    raw = raw.strip().removeprefix("http://").rstrip("/")
    host, sep, port = raw.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return raw, default_port


class Driver(registry.Driver):
    kind, title = "twinkly", "Twinkly"
    beta = True

    def scan(self, timeout: float = 2.0):
        try:
            return discover(timeout)
        except Exception:
            return []

    def probe(self, values):
        host, port = _split_host(values.get("host", ""), 80)
        if values.get("port"):
            port = int(values["port"])
        if not host:
            raise ValueError("Enter the lights' IP address")
        dev = TwinklyDevice(f"twinkly:{host}", "Twinkly", host, port)
        try:
            info = dev._raw("GET", "gestalt") or {}   # answered without logging in
            name = info["device_name"]
        except Exception as e:
            raise ValueError(f"No Twinkly answered at {host} ({e})") from None
        # The id is the IP, like discovery uses, so a light found both ways is one light.
        return registry.Found("twinkly", f"twinkly:{host}", host, port, name or "Twinkly",
                              {"mac": info.get("mac", "")})

    def create(self, dev_id, entry):
        return TwinklyDevice(dev_id, entry.get("label") or "Twinkly", entry["host"],
                             int(entry.get("port") or 80))
