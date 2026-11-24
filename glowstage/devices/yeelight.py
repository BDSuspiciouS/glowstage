"""Yeelight WiFi bulbs and strips (LAN control, "Yeelight WiFi Light Inter-Operation Specification").

The light must have LAN Control turned on in the Yeelight app. Static colors go over the
normal control connection (TCP 55443, one JSON command per line). That connection is limited
to about 60 commands a minute, so app effects use "music mode": the PC opens a TCP server, the
light connects back to it and then accepts unlimited set_rgb lines on that connection.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time

from ..colors import RGB
from ..state import LightState
from . import registry
from .base import Device

CONTROL_PORT = 55443
SSDP_ADDR = ("239.255.255.250", 1982)
SEARCH = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n'
PAIRING = "Turn on LAN Control for the light in the Yeelight app."


def _line(cid: int, method: str, params: list) -> bytes:
    return (json.dumps({"id": cid, "method": method, "params": params}, separators=(",", ":")) + "\r\n").encode()


def _rgb_int(rgb: RGB) -> int:
    return (int(rgb[0]) << 16) | (int(rgb[1]) << 8) | int(rgb[2])


def _bright(percent: float) -> int:
    return max(1, min(100, round(percent)))


def _local_ip(host: str) -> str:
    """The address of this PC that the light can reach (UDP connect sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def parse_ssdp(data: bytes) -> registry.Found | None:
    """Turn one SSDP answer (or NOTIFY) from a Yeelight into a Found; None for anything else."""
    headers: dict[str, str] = {}
    for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    location = headers.get("location", "")
    if not location.startswith("yeelight://"):
        return None
    host, _, port = location[len("yeelight://"):].split("/")[0].partition(":")
    if not host:
        return None
    try:
        port_num = int(port) if port else CONTROL_PORT
    except ValueError:
        port_num = CONTROL_PORT
    ident = headers.get("id", "").lower() or host
    model = headers.get("model", "")
    label = headers.get("name", "") or model or "Yeelight"
    return registry.Found("yeelight", f"yeelight:{ident}", host, port_num, label,
                          {"model": model, "support": headers.get("support", "")})


def discover(timeout: float = 2.0, target: tuple[str, int] = SSDP_ADDR) -> list[registry.Found]:
    """SSDP search for Yeelights. `target` is the multicast group, or one light's address."""
    found: dict[str, registry.Found] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        sock.sendto(SEARCH, target)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                data, _ = sock.recvfrom(4096)
            except OSError:   # timeout, or an ICMP "unreachable" for a unicast search
                break
            item = parse_ssdp(data)
            if item:
                found[item.dev_id] = item
    finally:
        sock.close()
    return list(found.values())


class YeelightDevice(Device):
    kind = "yeelight"
    group = "room"
    hardware_brightness = True    # brightness is set on the light; streamed colors stay full strength
    max_fps = 20

    def __init__(self, dev_id: str, name: str, host: str, port: int = CONTROL_PORT,
                 model: str = "", support: str = ""):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.model = model
        self.support = support
        self._color = ("set_rgb" in support.split()) if support else True   # refined in connect()
        self._ids = itertools.count(1)
        self._music: socket.socket | None = None
        self._brightness = 100
        self._last: tuple | None = None

    # ---- control connection ------------------------------------------------------------

    def _read_reply(self, reader, cid: int) -> list:
        while True:
            raw = reader.readline()
            if not raw:
                raise ConnectionError("The light closed the connection")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("id") != cid:   # skip "props" notifications
                continue
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(err.get("message", "error") if isinstance(err, dict) else str(err))
            return msg.get("result") or []

    def _talk(self, commands: list[tuple[str, list]], timeout: float) -> list[list]:
        """Send commands one after the other on one connection and return their results."""
        out = []
        with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            reader = sock.makefile("rb")
            for method, params in commands:
                cid = next(self._ids)
                sock.sendall(_line(cid, method, params))
                out.append(self._read_reply(reader, cid))
        return out

    # ---- Device ------------------------------------------------------------------------

    def connect(self) -> None:
        result = self._talk([("get_prop", ["power", "bright", "rgb", "name"])], 2.0)[0]
        result = list(result) + [""] * (4 - len(result))
        self._props = result
        if not self.support:   # white-only lights answer "" for rgb
            self._color = result[2] != ""
        self.reported_name = result[3] or "Yeelight"
        self.pixel_count = 1
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        props = getattr(self, "_props", None)
        if not props:
            return None
        try:
            return {"on": props[0] == "on", "brightness": int(props[1])}
        except (TypeError, ValueError):
            return None

    def show_color(self, rgb: RGB, state: LightState) -> None:
        cmds: list[tuple[str, list]] = [("set_power", ["on", "sudden", 0])]
        if self._color:
            cmds.append(("set_rgb", [_rgb_int(rgb), "sudden", 0]))
        cmds.append(("set_bright", [_bright(state.brightness), "sudden", 0]))
        self._talk(cmds, 3.0)

    def power_off(self, timeout: float = 3.0) -> None:
        self._close_music()
        self._talk([("set_power", ["off", "sudden", 0])], timeout)

    def begin_stream(self, state: LightState) -> None:
        self._close_music()
        self._brightness = _bright(state.brightness)
        self._last = None
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self._talk([
                ("set_power", ["on", "sudden", 0]),
                ("set_bright", [self._brightness, "sudden", 0]),
                ("set_music", [1, _local_ip(self.host), port]),
            ], 3.0)
            server.settimeout(3.0)
            try:
                conn, _ = server.accept()
            except OSError:
                raise ConnectionError("The light didn't connect back for music mode "
                                      "(a firewall may be blocking it)") from None
        finally:
            server.close()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(1.0)
        self._music = conn

    def send_frame(self, colors: list[RGB]) -> None:
        conn = self._music
        if conn is None or not colors:
            return
        n = len(colors)
        r = sum(c[0] for c in colors) // n
        g = sum(c[1] for c in colors) // n
        b = sum(c[2] for c in colors) // n
        if self._color:
            key = (r, g, b)
            line = _line(next(self._ids), "set_rgb", [_rgb_int(key), "sudden", 0])
        else:   # white-only light: the frame's brightness
            key = (_bright(self._brightness * max(r, g, b) / 255),)
            line = _line(next(self._ids), "set_bright", [key[0], "sudden", 0])
        if key == self._last:
            return
        try:
            conn.sendall(line)
        except OSError as e:
            self._close_music()
            raise ConnectionError(f"Lost the music-mode connection ({e})") from None
        self._last = key

    def end_stream(self) -> None:
        self._close_music()
        try:
            self._talk([("set_music", [0])], 2.0)
        except Exception:   # the light may be unreachable; the next command works either way
            pass

    def _close_music(self) -> None:
        conn, self._music = self._music, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def extra_info(self) -> dict:
        return {"host": self.host, "label": f"Yeelight · {self.model}" if self.model else "Yeelight"}


def _split_host(raw: str, default_port: int) -> tuple[str, int]:
    raw = raw.strip().removeprefix("http://").rstrip("/")
    host, sep, port = raw.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return raw, default_port


class Driver(registry.Driver):
    kind, title = "yeelight", "Yeelight"
    pairing = PAIRING
    beta = True

    def scan(self, timeout: float = 2.0):
        try:
            return discover(timeout)
        except Exception:
            return []

    def probe(self, values):
        host, port = _split_host(values.get("host", ""), CONTROL_PORT)
        if values.get("port"):
            port = int(values["port"])
        if not host:
            raise ValueError("Enter the light's IP address")
        try:   # a unicast search tells us the id and model; plenty of lights answer it
            found = discover(1.5, (host, SSDP_ADDR[1]))
        except Exception:
            found = []
        if found:
            return found[0]
        dev = YeelightDevice(f"yeelight:{host}", "Yeelight", host, port)
        try:
            dev.connect()
        except Exception as e:
            raise ValueError(f"No Yeelight answered at {host} ({e}). "
                             "Is LAN Control turned on in the Yeelight app?") from None
        return registry.Found("yeelight", f"yeelight:{host}", host, port, dev.reported_name,
                              {"model": "", "support": ""})

    def create(self, dev_id, entry):
        return YeelightDevice(dev_id, entry.get("label") or "Yeelight", entry["host"],
                              int(entry.get("port") or CONTROL_PORT),
                              entry.get("model", ""), entry.get("support", ""))
