"""DMX stage lights over Art-Net and sACN (E1.31).

One config entry is one DMX universe on one node (or broadcast / multicast). The hub owns the
512-byte universe buffer and ONE sender thread that sends it at 40 fps (so it doubles as the
keepalive receivers want). Every fixture is a Device that writes its channels into that buffer;
overlapping fixtures are allowed, the last write wins.

Config entry:
    {"kind": "dmx", "protocol": "artnet" | "sacn", "host": "", "universe": 0, "label": "...",
     "fixtures": [{"id": "f1", "name": "Moving head 1", "start": 1, "pixels": 1,
                   "layout": "RGB", "fixed": {"6": 255}}]}

`start` is the 1-based DMX address of the fixture's first channel, each pixel uses len(layout)
channels, `fixed` maps 1-based channel offsets inside the fixture (pan, tilt, shutter, ...) to
constant values.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time
import uuid

from ..colors import RGB, scale
from ..state import LightState
from . import registry
from .base import Device
from .hub import Hub

log = logging.getLogger(__name__)

ARTNET_PORT = 6454
SACN_PORT = 5568
UNIVERSE_SIZE = 512
FPS = 40
LAYOUTS = ("RGB", "GRB", "RGBW", "DRGB", "DRGBW")
OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000
ARTNET_ID = b"Art-Net\x00"
ACN_ID = b"ASC-E1.17\x00\x00\x00"
SOURCE_NAME = "Glowstage"


# ---- packets ----------------------------------------------------------------------------

def build_artdmx(universe: int, sequence: int, data: bytes) -> bytes:
    """An ArtDmx packet carrying 512 channels (data is padded / cut to 512 bytes)."""
    data = bytes(data[:UNIVERSE_SIZE]).ljust(UNIVERSE_SIZE, b"\x00")
    return (ARTNET_ID + struct.pack("<H", OP_DMX) + struct.pack(">H", 14)
            + bytes((sequence & 0xFF, 0, universe & 0xFF, (universe >> 8) & 0x7F))
            + struct.pack(">H", UNIVERSE_SIZE) + data)


def build_sacn(cid: bytes, universe: int, sequence: int, data: bytes, priority: int = 100) -> bytes:
    """An E1.31 data packet (root + framing + DMP layer) carrying start code 0 and 512 channels."""
    data = bytes(data[:UNIVERSE_SIZE]).ljust(UNIVERSE_SIZE, b"\x00")
    dmp = (struct.pack(">H", 0x7000 | 523) + bytes((0x02, 0xA1)) + struct.pack(">HHH", 0, 1, 513)
           + b"\x00" + data)
    framing = (struct.pack(">H", 0x7000 | (77 + len(dmp))) + struct.pack(">I", 2)
               + SOURCE_NAME.encode().ljust(64, b"\x00")
               + bytes((priority,)) + struct.pack(">H", 0) + bytes((sequence & 0xFF, 0))
               + struct.pack(">H", universe) + dmp)
    root = (struct.pack(">HH", 0x0010, 0) + ACN_ID + struct.pack(">H", 0x7000 | (22 + len(framing)))
            + struct.pack(">I", 4) + cid)
    return root + framing


def build_artpoll() -> bytes:
    return ARTNET_ID + struct.pack("<H", OP_POLL) + struct.pack(">H", 14) + bytes((0x00, 0x10))


def parse_artpoll_reply(data: bytes, addr: tuple | None = None) -> registry.Found | None:
    """Turn an ArtPollReply into a Found (None for anything else)."""
    if len(data) < 44 or data[:8] != ARTNET_ID or struct.unpack("<H", data[8:10])[0] != OP_POLL_REPLY:
        return None
    ip = ".".join(str(b) for b in data[10:14])
    if ip == "0.0.0.0" and addr:
        ip = addr[0]
    name = data[26:44].split(b"\x00", 1)[0].decode("ascii", "replace").strip() or "Art-Net node"
    return registry.Found("dmx", f"dmx:artnet:{ip}:0", ip, ARTNET_PORT, name,
                          {"protocol": "artnet", "universe": 0, "fixtures": []})


# ---- fixtures ---------------------------------------------------------------------------

def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DmxFixture(Device):
    kind = "dmx"
    group = "room"
    fades = True                 # local and quick: a new look glides in (manager.DeviceWorker)
    hardware_brightness = False
    max_fps = FPS

    def __init__(self, dev_id: str, hub: "DmxHub", spec: dict):
        super().__init__(dev_id, str(spec.get("name") or "DMX fixture"))
        self.hub = hub
        self.start = max(1, min(UNIVERSE_SIZE, _int(spec.get("start"), 1)))
        self.pixel_count = max(1, _int(spec.get("pixels"), 1))
        layout = str(spec.get("layout") or "RGB").upper()
        self.layout = layout if layout in LAYOUTS else "RGB"
        self.fixed: dict[int, int] = {}
        for k, v in (spec.get("fixed") or {}).items():
            off = _int(k, 0)
            if off >= 1:
                self.fixed[off] = max(0, min(255, _int(v, 0)))
        self.width = len(self.layout)
        span = max([self.pixel_count * self.width, *self.fixed])
        self.end = min(UNIVERSE_SIZE, self.start + span - 1)

    def connect(self) -> None:
        self.pixel_count = max(1, self.pixel_count)
        self.reported_name = self.name
        self.online = True
        self.error = None
        self._write(bytes(self.pixel_count * self.width))

    # ---- channel mapping ----

    def _encode(self, rgb: RGB) -> bytes:
        r, g, b = (max(0, min(255, int(c))) for c in rgb)
        out = bytearray()
        w = min(r, g, b) if "W" in self.layout else 0
        if w:
            r, g, b = r - w, g - w, b - w
        values = {"R": r, "G": g, "B": b, "W": w, "D": 255}
        for ch in self.layout:
            out.append(values[ch])
        return bytes(out)

    def _write(self, color_bytes: bytes) -> None:
        self.hub.write(self.start - 1, color_bytes, {self.start - 1 + o - 1: v for o, v in self.fixed.items()})

    def _fill(self, rgb: RGB) -> None:
        self._write(self._encode(rgb) * self.pixel_count)

    # ---- Device contract ----

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._fill(scale(rgb, state.brightness / 100))

    def power_off(self, timeout: float = 3.0) -> None:
        self._write(bytes(self.pixel_count * self.width))

    def send_frame(self, colors: list[RGB]) -> None:
        out = bytearray()
        for i in range(self.pixel_count):
            out += self._encode(colors[i] if i < len(colors) else (0, 0, 0))
        self._write(bytes(out))

    def extra_info(self) -> dict:
        h = self.hub
        return {"host": h.host, "label": f"DMX · universe {h.universe} · channel {self.start}-{self.end}"}


# ---- hub --------------------------------------------------------------------------------

class DmxHub(Hub):
    kind = "dmx"

    def __init__(self, hub_id: str, entry: dict, port: int | None = None, fps: float = FPS):
        super().__init__(hub_id, entry)
        self.protocol = "sacn" if str(entry.get("protocol", "artnet")).lower() == "sacn" else "artnet"
        self.host = str(entry.get("host") or "").strip()
        self.universe = _int(entry.get("universe"), 1 if self.protocol == "sacn" else 0)
        self.port = port or (SACN_PORT if self.protocol == "sacn" else ARTNET_PORT)
        self.interval = 1.0 / fps
        self.cid = uuid.uuid5(uuid.NAMESPACE_URL, f"glowstage:{hub_id}").bytes
        self._buf = bytearray(UNIVERSE_SIZE)
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq = 0
        self._dest: tuple[str, int] = (self.host or "255.255.255.255", self.port)

    # ---- buffer ----

    def write(self, offset: int, data: bytes, fixed: dict[int, int] | None = None) -> None:
        """Copy data into the universe at a 0-based offset (cut off at channel 512)."""
        with self._lock:
            end = min(UNIVERSE_SIZE, offset + len(data))
            if end > offset:
                self._buf[offset:end] = data[:end - offset]
            for pos, value in (fixed or {}).items():
                if 0 <= pos < UNIVERSE_SIZE:
                    self._buf[pos] = value

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    # ---- lifecycle ----

    def connect(self) -> None:
        self._halt()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if self.protocol == "artnet":
                if not self.host:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                self._dest = (self.host or "255.255.255.255", self.port)
            else:
                if not self.host:
                    u = self.universe
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
                    self._dest = (f"239.255.{(u >> 8) & 0xFF}.{u & 0xFF}", self.port)
                else:
                    self._dest = (self.host, self.port)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._build_fixtures(self.entry.get("fixtures") or [])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(sock, self._stop), name=f"dmx-{self.id}",
                                        daemon=True)
        self._thread.start()
        self.online = True
        self.error = None

    def describe(self) -> dict:
        return {**super().describe(), "protocol": self.protocol, "universe": self.universe,
                "fixtures": [dict(f) for f in self.entry.get("fixtures", [])], "layouts": list(LAYOUTS)}

    def set_fixtures(self, fixtures: list[dict]) -> dict[str, Device]:
        """Rebuild the fixtures (the UI edits them). Returns the new devices."""
        self.entry["fixtures"] = [dict(f) for f in fixtures]
        with self._lock:
            self._buf[:] = bytes(UNIVERSE_SIZE)
        self._build_fixtures(self.entry["fixtures"])
        return self.devices

    def _build_fixtures(self, fixtures: list[dict]) -> None:
        devices: dict[str, Device] = {}
        for i, spec in enumerate(fixtures, 1):
            fid = str(spec.get("id") or f"f{i}")
            dev_id = f"{self.id}:{fid}"
            devices[dev_id] = DmxFixture(dev_id, self, spec)
        self.devices.clear()
        self.devices.update(devices)

    def close(self) -> None:
        self._halt()
        self.online = False

    def _halt(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ---- sender thread ----

    def _packet(self) -> bytes:
        data = self.snapshot()
        if self.protocol == "sacn":
            self._seq = (self._seq + 1) & 0xFF
            return build_sacn(self.cid, self.universe, self._seq, data)
        self._seq = self._seq % 255 + 1          # Art-Net: 1..255, 0 means "no sequencing"
        return build_artdmx(self.universe, self._seq, data)

    def _run(self, sock: socket.socket, stop: threading.Event) -> None:
        next_at = time.monotonic()
        while not stop.is_set():
            try:
                sock.sendto(self._packet(), self._dest)
                if self.error:
                    self.error = None
            except OSError as e:
                if stop.is_set():
                    break
                self.error = f"DMX send failed: {e}"
            next_at += self.interval
            delay = next_at - time.monotonic()
            if delay < -1.0:                      # fell far behind (sleep / stall): don't burst
                next_at = time.monotonic()
                delay = 0
            if stop.wait(max(0.0, delay)):
                break


# ---- driver -----------------------------------------------------------------------------

class Driver(registry.Driver):
    kind, title = "dmx", "DMX (Art-Net / sACN)"
    hub = True
    beta = True
    fields = (
        {"key": "protocol", "label": "Protocol", "options": ["artnet", "sacn"], "placeholder": "artnet"},
        {"key": "host", "label": "Node IP address (empty = broadcast / multicast)", "placeholder": "192.168.1.60"},
        {"key": "universe", "label": "Universe", "placeholder": "0"},
    )

    def probe(self, values):
        protocol = str(values.get("protocol") or "artnet").strip().lower()
        if protocol not in ("artnet", "sacn"):
            raise ValueError("Protocol must be artnet or sacn")
        host = str(values.get("host") or "").strip()
        if host:
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                raise ValueError(f"{host} is not a valid IPv4 address") from None
        raw = str(values.get("universe") if values.get("universe") is not None else "").strip()
        try:
            universe = int(raw) if raw else (1 if protocol == "sacn" else 0)
        except ValueError:
            raise ValueError("Universe must be a whole number") from None
        lo, hi = (1, 63999) if protocol == "sacn" else (0, 32767)
        if not lo <= universe <= hi:
            raise ValueError(f"Universe must be between {lo} and {hi} for {protocol}")
        port = SACN_PORT if protocol == "sacn" else ARTNET_PORT
        return registry.Found("dmx", f"dmx:{protocol}:{host or 'broadcast'}:{universe}", host, port,
                              f"DMX universe {universe}",
                              {"protocol": protocol, "universe": universe, "fixtures": []})

    def create(self, dev_id, entry):
        return DmxHub(dev_id, entry)

    def scan(self, timeout: float = 2.0) -> list[registry.Found]:
        try:
            return self._scan(timeout)
        except Exception:
            return []

    def _scan(self, timeout: float) -> list[registry.Found]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.bind(("0.0.0.0", ARTNET_PORT))
            except OSError:
                return []
            sock.sendto(build_artpoll(), ("255.255.255.255", ARTNET_PORT))
            found: dict[str, registry.Found] = {}
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except ConnectionResetError:      # Windows reports ICMP errors on UDP sockets
                    continue
                except OSError:
                    break
                item = parse_artpoll_reply(data, addr)
                if item is not None:
                    found.setdefault(item.dev_id, item)
            return list(found.values())
        finally:
            sock.close()
