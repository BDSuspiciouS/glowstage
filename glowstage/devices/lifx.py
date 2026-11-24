"""LIFX lights (bulbs, Z strips, Beams) over the LAN protocol: UDP port 56700, little-endian.

Discovery is a tagged GetService broadcast; every light answers with StateService (its MAC is in
the target field of the answer). Colors go out as SetColor; multizone strips and beams are
streamed with SetExtendedColorZones (82 zones per message). Streaming is fire-and-forget UDP;
static colors and power are sent with an acknowledgement request and one retry.

Not supported: tiles and candles (matrix devices, Set64). They answer like a plain bulb here, so
they show one color across the whole device.
"""

from __future__ import annotations

import itertools
import random
import socket
import struct
import threading
import time
from typing import NamedTuple

from ..colors import RGB, rgb_to_hsv
from ..state import LightState
from . import registry
from .base import Device

PORT = 56700
HEADER_SIZE = 36
PROTOCOL = 1024
ADDRESSABLE = 0x1000
TAGGED = 0x2000
KELVIN = 3500
ZONES_PER_MESSAGE = 82

GET_SERVICE, STATE_SERVICE = 2, 3
GET_LABEL, STATE_LABEL = 23, 25
GET_VERSION, STATE_VERSION = 32, 33
ACKNOWLEDGEMENT = 45
SET_COLOR = 102
LIGHT_SET_POWER = 117
STATE_UNHANDLED = 223
SET_EXTENDED_COLOR_ZONES = 510
GET_EXTENDED_COLOR_ZONES, STATE_EXTENDED_COLOR_ZONES = 511, 512

HSBK = tuple[int, int, int, int]
_sequence = itertools.count()


# ---- wire format (pure functions, easy to test) ------------------------------------------

class Header(NamedTuple):
    size: int
    tagged: bool
    source: int
    mac: str            # lower-case hex of the 6-byte target, "" when zero
    flags: int
    sequence: int
    type: int
    payload: bytes


def build_message(msg_type: int, payload: bytes = b"", *, source: int, mac: str | None = None,
                  tagged: bool = False, res_required: bool = False, ack_required: bool = False,
                  sequence: int = 0) -> bytes:
    """A complete LIFX message: the 36-byte header followed by the payload."""
    target = (bytes.fromhex(mac) if mac else b"\0" * 6) + b"\0\0"
    protocol = PROTOCOL | ADDRESSABLE | (TAGGED if tagged else 0)
    flags = (1 if res_required else 0) | (2 if ack_required else 0)
    header = struct.pack("<HHI8s6sBB8sH2s", HEADER_SIZE + len(payload), protocol, source, target,
                         b"\0" * 6, flags, sequence & 0xFF, b"\0" * 8, msg_type, b"\0\0")
    return header + payload


def parse_message(data: bytes) -> Header | None:
    """Split a received datagram into its header fields and payload; None when it isn't LIFX."""
    if len(data) < HEADER_SIZE:
        return None
    size, protocol, source, target, _, flags, seq, _, msg_type, _ = struct.unpack("<HHI8s6sBB8sH2s", data[:HEADER_SIZE])
    if protocol & 0x0FFF != PROTOCOL or size < HEADER_SIZE or size > len(data):
        return None
    mac = target[:6].hex() if any(target[:6]) else ""
    return Header(size, bool(protocol & TAGGED), source, mac, flags, seq, msg_type, data[HEADER_SIZE:size])


def rgb_to_hsbk(rgb: RGB, brightness: float = 1.0, kelvin: int = KELVIN) -> HSBK:
    """RGB (0-255) to LIFX HSBK (each 0-65535, kelvin 1500-9000). `brightness` (0..1) scales the value."""
    h, s, v = rgb_to_hsv(rgb)
    top = lambda x: max(0, min(65535, int(round(x * 65535))))  # noqa: E731
    return (int(round(h * 65536)) % 65536, top(s), top(v * brightness), max(1500, min(9000, kelvin)))


def pack_hsbk(hsbk: HSBK) -> bytes:
    return struct.pack("<HHHH", *hsbk)


def set_color_payload(hsbk: HSBK, duration_ms: int = 0) -> bytes:
    return struct.pack("<B", 0) + pack_hsbk(hsbk) + struct.pack("<I", duration_ms)


def set_power_payload(on: bool, duration_ms: int = 0) -> bytes:
    return struct.pack("<HI", 65535 if on else 0, duration_ms)


def set_zones_payload(colors: list[HSBK], start: int, apply: int, duration_ms: int = 0) -> bytes:
    """One SetExtendedColorZones message for up to 82 zones beginning at index `start`."""
    chunk = colors[start:start + ZONES_PER_MESSAGE]
    padded = list(chunk) + [(0, 0, 0, 0)] * (ZONES_PER_MESSAGE - len(chunk))
    return (struct.pack("<IBHB", duration_ms, apply, start, len(chunk))
            + b"".join(pack_hsbk(c) for c in padded))


def parse_state_service(payload: bytes) -> tuple[int, int] | None:
    """(service, port); service 1 is UDP."""
    if len(payload) < 5:
        return None
    return struct.unpack("<BI", payload[:5])


def parse_state_label(payload: bytes) -> str:
    return payload[:32].split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def parse_state_version(payload: bytes) -> tuple[int, int] | None:
    """(vendor, product)."""
    if len(payload) < 8:
        return None
    return struct.unpack("<II", payload[:8])


def parse_state_extended_zones(payload: bytes) -> tuple[int, int, list[HSBK]] | None:
    """(zones_count, zone_index, the colors carried by this message)."""
    if len(payload) < 5:
        return None
    count, index, n = struct.unpack("<HHB", payload[:5])
    n = min(n, ZONES_PER_MESSAGE, (len(payload) - 5) // 8)
    colors = [struct.unpack("<HHHH", payload[5 + i * 8:13 + i * 8]) for i in range(n)]
    return count, index, colors


# ---- network helpers ---------------------------------------------------------------------

def _udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    if hasattr(socket, "SIO_UDP_CONNRESET"):   # Windows: don't turn ICMP "port unreachable" into recv errors
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except OSError:
            pass
    return sock


def _drain(sock: socket.socket) -> None:
    sock.settimeout(0.0)
    for _ in range(200):
        try:
            sock.recvfrom(2048)
        except (BlockingIOError, socket.timeout):
            return
        except OSError:
            continue


def _receive(sock: socket.socket, deadline: float):
    """Yield (Header, addr) for LIFX datagrams until the deadline."""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        sock.settimeout(left)
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            return
        except OSError:
            continue
        msg = parse_message(data)
        if msg:
            yield msg, addr


def exchange(sock: socket.socket, addr: tuple[str, int], source: int, mac: str | None, msg_type: int,
             payload: bytes, want: int, timeout: float = 1.0, retries: int = 1) -> Header | None:
    """Send a message and wait for the answer of type `want` (45 = acknowledgement). None when no
    answer came (or the light says it doesn't handle the message). One retry by default."""
    _drain(sock)
    for _ in range(retries + 1):
        sock.sendto(build_message(msg_type, payload, source=source, mac=mac, res_required=want != ACKNOWLEDGEMENT,
                                  ack_required=want == ACKNOWLEDGEMENT, sequence=next(_sequence)), addr)
        for msg, _addr in _receive(sock, time.monotonic() + timeout):
            if msg.source != source or (mac and msg.mac != mac):
                continue
            if msg.type == want:
                return msg
            if msg.type == STATE_UNHANDLED:
                return None
    return None


def query_info(sock: socket.socket, addr: tuple[str, int], source: int, mac: str,
               timeout: float = 1.0, retries: int = 1) -> tuple[str, int]:
    """(label, product) of a light; empty/0 for whatever it doesn't answer."""
    label = exchange(sock, addr, source, mac, GET_LABEL, b"", STATE_LABEL, timeout, retries)
    version = exchange(sock, addr, source, mac, GET_VERSION, b"", STATE_VERSION, timeout, retries)
    parsed = parse_state_version(version.payload) if version else None
    return (parse_state_label(label.payload) if label else "", parsed[1] if parsed else 0)


def _new_source() -> int:
    return random.randint(2, 0xFFFFFFFF)


# ---- device ------------------------------------------------------------------------------

class LifxDevice(Device):
    kind = "lifx"
    group = "room"
    hardware_brightness = False
    max_fps = 20

    def __init__(self, dev_id: str, name: str, host: str, port: int = PORT, mac: str = ""):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.mac = (mac or dev_id.split(":", 1)[-1]).lower().replace(":", "")
        self.product = 0
        self.multizone = False
        self.timeout = 1.0            # seconds per attempt for requests that expect an answer
        self.source = _new_source()
        self._sock = _udp_socket()
        self._io = threading.Lock()   # one request/answer exchange at a time

    @property
    def _addr(self) -> tuple[str, int]:
        return (self.host, self.port)

    def _ask(self, msg_type: int, payload: bytes, want: int) -> Header | None:
        with self._io:
            return exchange(self._sock, self._addr, self.source, self.mac, msg_type, payload, want, self.timeout)

    def _send(self, msg_type: int, payload: bytes) -> None:
        """Fire and forget."""
        self._sock.sendto(build_message(msg_type, payload, source=self.source, mac=self.mac,
                                        sequence=next(_sequence)), self._addr)

    def _send_acked(self, msg_type: int, payload: bytes, timeout: float | None = None) -> None:
        wait = self.timeout if timeout is None else timeout
        with self._io:
            if exchange(self._sock, self._addr, self.source, self.mac, msg_type, payload, ACKNOWLEDGEMENT, wait) is None:
                raise TimeoutError(f"LIFX at {self.host} did not answer")

    def connect(self) -> None:
        label = self._ask(GET_LABEL, b"", STATE_LABEL)
        if label is None:
            raise TimeoutError(f"No LIFX answered at {self.host}")
        version = self._ask(GET_VERSION, b"", STATE_VERSION)
        parsed = parse_state_version(version.payload) if version else None
        self.product = parsed[1] if parsed else 0
        zones = self._ask(GET_EXTENDED_COLOR_ZONES, b"", STATE_EXTENDED_COLOR_ZONES)
        info = parse_state_extended_zones(zones.payload) if zones else None
        self.multizone = bool(info and info[0] > 0)
        self.pixel_count = info[0] if self.multizone else 1
        self.reported_name = parse_state_label(label.payload) or "LIFX"
        self.online = True
        self.error = None

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._send_acked(LIGHT_SET_POWER, set_power_payload(True))
        self._send_acked(SET_COLOR, set_color_payload(rgb_to_hsbk(rgb, state.brightness / 100)))

    def power_off(self, timeout: float = 3.0) -> None:
        self._send_acked(LIGHT_SET_POWER, set_power_payload(False), min(timeout / 2, self.timeout))

    def begin_stream(self, state: LightState) -> None:
        self._send_acked(LIGHT_SET_POWER, set_power_payload(True))

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        if not self.multizone:
            self._send(SET_COLOR, set_color_payload(rgb_to_hsbk(colors[0])))
            return
        hsbk = [rgb_to_hsbk(c) for c in colors[:self.pixel_count]]
        for start in range(0, len(hsbk), ZONES_PER_MESSAGE):
            last = start + ZONES_PER_MESSAGE >= len(hsbk)
            self._send(SET_EXTENDED_COLOR_ZONES, set_zones_payload(hsbk, start, 1 if last else 0))

    def extra_info(self) -> dict:
        if self.multizone:
            detail = f"{self.pixel_count} zones"
        else:
            detail = f"product {self.product}" if self.product else ""
        return {"host": self.host, "label": "LIFX · " + detail if detail else "LIFX"}


# ---- driver ------------------------------------------------------------------------------

class Driver(registry.Driver):
    kind, title = "lifx", "LIFX"
    beta = True

    def scan(self, timeout: float = 2.0, port: int = PORT, broadcast: str = "255.255.255.255") -> list[registry.Found]:
        try:
            return self._scan(timeout, port, broadcast)
        except Exception:
            return []

    def _scan(self, timeout: float, port: int, broadcast: str) -> list[registry.Found]:
        source = _new_source()
        sock = _udp_socket()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            seen: dict[str, tuple[str, int]] = {}
            for _ in range(2):   # UDP is lossy: ask twice
                sock.sendto(build_message(GET_SERVICE, source=source, tagged=True, res_required=True,
                                          sequence=next(_sequence)), (broadcast, port))
                for msg, addr in _receive(sock, time.monotonic() + timeout / 2):
                    service = parse_state_service(msg.payload) if msg.type == STATE_SERVICE else None
                    if msg.source == source and msg.mac and service and service[0] == 1:
                        seen[msg.mac] = (addr[0], service[1] or port)
            found = []
            for mac, (ip, svc_port) in sorted(seen.items()):
                label, product = query_info(sock, (ip, svc_port), source, mac, timeout=0.5, retries=1)
                found.append(registry.Found("lifx", f"lifx:{mac}", ip, svc_port, label,
                                            {"mac": mac, "product": product}))
            return found
        finally:
            sock.close()

    def probe(self, values: dict, port: int = PORT) -> registry.Found:
        host = values.get("host", "").strip()
        if not host:
            raise ValueError("Enter the IP address of the LIFX light")
        source = _new_source()
        sock = _udp_socket()
        try:
            answer = exchange(sock, (host, port), source, None, GET_SERVICE, b"", STATE_SERVICE, 1.0, 1)
            service = parse_state_service(answer.payload) if answer else None
            if not answer or not answer.mac or not service:
                raise ValueError(f"No LIFX answered at {host}")
            svc_port = service[1] or port
            label, product = query_info(sock, (host, svc_port), source, answer.mac)
        except OSError as e:
            raise ValueError(f"No LIFX answered at {host} ({e})") from None
        finally:
            sock.close()
        return registry.Found("lifx", f"lifx:{answer.mac}", host, svc_port, label or "LIFX",
                              {"mac": answer.mac, "product": product})

    def create(self, dev_id: str, entry: dict) -> LifxDevice:
        return LifxDevice(dev_id, entry.get("label") or "LIFX", entry["host"],
                          int(entry.get("port") or PORT), entry.get("mac") or "")
