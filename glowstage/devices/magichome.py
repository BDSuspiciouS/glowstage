"""Magic Home / Flux LED WiFi controllers (also sold under many other names, ZJ200 / HF-LPB100 modules).

Discovery: a UDP broadcast of "HF-A11ASSISTHREAD" to port 48899; each controller answers
"ip,MAC,model". Control: one persistent TCP connection to port 5577 per controller. Every command
ends with a checksum byte (the sum of the bytes before it, & 0xFF). There is no separate
brightness on these controllers, so brightness is baked into the color we send.
"""

from __future__ import annotations

import select
import socket
import threading
import time

from ..colors import RGB, rgb_to_hex, scale
from ..state import LightState
from . import registry
from .base import Device

PORT = 5577
DISCOVERY_PORT = 48899
DISCOVERY_MESSAGE = b"HF-A11ASSISTHREAD"
CONNECT_TIMEOUT_S = 1.5
COMMAND_TIMEOUT_S = 1.0
FRAME_BUDGET_S = 0.05       # a streamed frame never blocks longer than this
RECONNECT_BACKOFF_S = 0.5   # while streaming: after a failed connect, drop frames for this long
STATUS_LEN = 14


def with_checksum(body: list[int] | bytes) -> bytes:
    data = bytes(body)
    return data + bytes([sum(data) & 0xFF])


def color_command(rgb: RGB) -> bytes:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return with_checksum([0x31, r, g, b, 0x00, 0x00, 0x0F])


POWER_ON = with_checksum([0x71, 0x23, 0x0F])
POWER_OFF = with_checksum([0x71, 0x24, 0x0F])
STATUS_QUERY = with_checksum([0x81, 0x8A, 0x8B])


def parse_status(data: bytes) -> dict | None:
    """The 14-byte answer to the status query: {"on": bool, "rgb": (r, g, b)}; None when it isn't one."""
    if len(data) < STATUS_LEN or data[0] != 0x81:
        return None
    return {"on": data[2] == 0x23, "rgb": (data[6], data[7], data[8])}


def parse_discovery_reply(raw: bytes) -> registry.Found | None:
    """b"192.168.1.50,ACCF235FFFFF,AK001-ZJ200" as a Found."""
    parts = raw.decode("ascii", "ignore").strip().split(",")
    if len(parts) < 2:
        return None
    ip, mac = parts[0].strip(), parts[1].strip().lower()
    model = parts[2].strip() if len(parts) > 2 else ""
    try:
        socket.inet_aton(ip)
        int(mac, 16)
    except (OSError, ValueError):
        return None
    if len(mac) != 12 or ip.count(".") != 3:
        return None
    return registry.Found("magichome", f"magichome:{mac}", ip, PORT, model, {"model": model})


def average_color(colors: list[RGB]) -> RGB:
    n = len(colors)
    return (round(sum(c[0] for c in colors) / n), round(sum(c[1] for c in colors) / n),
            round(sum(c[2] for c in colors) / n))


def _local_ipv4s() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


class MagicHomeDevice(Device):
    kind = "magichome"
    group = "room"
    hardware_brightness = False   # frames arrive already scaled by the brightness
    max_fps = 20

    def __init__(self, dev_id: str, name: str, host: str, port: int = PORT, model: str = ""):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.model = model
        self.reported_name = f"Magic Home {model}".strip() if model else "Magic Home"
        self._io = threading.Lock()          # one conversation on the socket at a time
        self._sock: socket.socket | None = None
        self._last_frame: RGB | None = None
        self._retry_at = 0.0
        self._status: dict | None = None

    # ---- the connection ---------------------------------------------------------------

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        self._last_frame = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _drain(self, sock: socket.socket) -> None:
        """Throw away what the controller sent unasked (it answers most commands) and notice a
        connection it closed. Raises OSError when the connection is gone."""
        while select.select([sock], [], [], 0)[0]:
            data = sock.recv(4096)
            if not data:
                raise ConnectionError("The controller closed the connection")

    def _ready(self, deadline: float) -> socket.socket:
        """The connected, healthy socket (opens a new one when needed). Call with _io held."""
        if self._sock is not None:
            try:
                self._drain(self._sock)
                return self._sock
            except OSError:
                self._close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("out of time")
        sock = socket.create_connection((self.host, self.port), timeout=min(CONNECT_TIMEOUT_S, remaining))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        return sock

    def _send(self, *commands: bytes, timeout: float = COMMAND_TIMEOUT_S, block: bool = True) -> bool:
        """Send commands in order. Reconnects once when the connection turns out to be dead.
        With block=False a busy socket (or a controller that can't be reached in `timeout`) means
        True is not returned and the commands are dropped; otherwise errors are raised."""
        deadline = time.monotonic() + timeout
        if not self._io.acquire(blocking=block, timeout=timeout if block else -1):
            return False
        try:
            last_error: OSError | None = None
            for _attempt in range(2):
                try:
                    sock = self._ready(deadline)
                    for command in commands:
                        sock.settimeout(max(0.005, deadline - time.monotonic()))
                        sock.sendall(command)
                    return True
                except OSError as e:   # includes timeouts
                    last_error = e
                    self._close()
                    if time.monotonic() >= deadline:
                        break
            if block:
                raise ConnectionError(f"Can't reach the Magic Home controller at {self.host} ({last_error})")
            self._retry_at = time.monotonic() + RECONNECT_BACKOFF_S   # streaming: stop trying for a moment
            return False
        finally:
            self._io.release()

    def _query_status(self, timeout: float = 2.0) -> dict | None:
        """Send the status query and read the 14-byte answer. Raises OSError when unreachable;
        None when the controller connected but didn't answer like a Magic Home."""
        deadline = time.monotonic() + timeout
        with self._io:
            for attempt in range(2):
                try:
                    sock = self._ready(deadline)   # a timeout here means unreachable: raised
                except OSError:
                    self._close()
                    raise
                buf = b""
                try:
                    sock.settimeout(max(0.05, deadline - time.monotonic()))
                    sock.sendall(STATUS_QUERY)
                    while len(buf) < STATUS_LEN:
                        sock.settimeout(max(0.05, deadline - time.monotonic()))
                        chunk = sock.recv(STATUS_LEN - len(buf))
                        if not chunk:
                            raise ConnectionError("The controller closed the connection")
                        buf += chunk
                    return parse_status(buf)
                except socket.timeout:   # connected, but no (complete) answer
                    return parse_status(buf) if len(buf) >= STATUS_LEN else None
                except OSError:
                    self._close()
                    if attempt or time.monotonic() >= deadline:
                        raise
        return None

    # ---- Device --------------------------------------------------------------------------

    def connect(self) -> None:
        # A controller that accepts the TCP connection is online; the status answer is a bonus
        # (some firmware answers with a different length).
        self._status = self._query_status()
        self.pixel_count = 1
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        st = self._status
        if not st:
            return None
        out: dict = {"on": st["on"]}
        peak = max(st["rgb"])
        if peak > 0:   # brightness is baked into the color: split it back out
            out["brightness"] = max(1, round(peak / 2.55))
            out["color"] = rgb_to_hex(tuple(min(255, round(c * 255 / peak)) for c in st["rgb"]))
        return out

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._send(POWER_ON, color_command(scale(rgb, state.brightness / 100)))

    def power_off(self, timeout: float = 3.0) -> None:
        self._send(POWER_OFF, timeout=max(0.05, timeout))

    def begin_stream(self, state: LightState) -> None:
        self._send(POWER_ON)

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        rgb = average_color(colors)
        if rgb == self._last_frame:
            return
        if self._sock is None and time.monotonic() < self._retry_at:
            return   # the controller was unreachable a moment ago: drop frames instead of stalling
        if self._send(color_command(rgb), timeout=FRAME_BUDGET_S, block=False):
            self._last_frame = rgb

    def end_stream(self) -> None:
        self._last_frame = None

    def extra_info(self) -> dict:
        return {"host": self.host, "label": f"Magic Home · {self.model}" if self.model else "Magic Home"}


class Driver(registry.Driver):
    kind, title = "magichome", "Magic Home"
    beta = True

    def __init__(self, port: int = PORT, discovery_target: tuple[str, int] | None = None,
                 discovery_port: int = DISCOVERY_PORT):
        # The arguments exist for the tests; the app builds Driver() with the defaults.
        self.port = port
        self.discovery_target = discovery_target   # None = broadcast on every network interface
        self.discovery_port = discovery_port

    def scan(self, timeout: float = 2.0) -> list[registry.Found]:
        socks: list[socket.socket] = []
        try:
            if self.discovery_target is not None:
                binds = ["127.0.0.1"]
                targets = [self.discovery_target]
            else:
                binds = ["", *_local_ipv4s()]   # a broadcast leaves through one interface only
                targets = [("255.255.255.255", self.discovery_port)]
            for bind in binds:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    socks.append(sock)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.bind((bind, 0))
                except OSError:
                    continue
            found: dict[str, registry.Found] = {}
            end = time.monotonic() + timeout
            sent_at = 0.0
            while True:
                now = time.monotonic()
                if now >= end:
                    break
                if now >= sent_at:   # twice per scan: UDP broadcasts get lost
                    for sock in socks:
                        for target in targets:
                            try:
                                sock.sendto(DISCOVERY_MESSAGE, target)
                            except OSError:
                                pass
                    sent_at = now + max(timeout / 2, 0.2)
                try:
                    ready = select.select(socks, [], [], min(0.1, end - now))[0] if socks else []
                except (OSError, ValueError):
                    break
                for sock in ready:
                    try:
                        raw, _addr = sock.recvfrom(1024)
                    except OSError:
                        continue
                    item = parse_discovery_reply(raw)
                    if item is not None:
                        found[item.dev_id] = item
                if not socks:
                    time.sleep(0.1)
            return list(found.values())
        except Exception:
            return []
        finally:
            for sock in socks:
                try:
                    sock.close()
                except OSError:
                    pass

    def _lookup(self, host: str) -> registry.Found | None:
        """Ask one controller for its MAC and model (best effort, short)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.3)
                for _ in range(2):
                    sock.sendto(DISCOVERY_MESSAGE, (host, self.discovery_port))
                    try:
                        raw, _addr = sock.recvfrom(1024)
                    except socket.timeout:
                        continue
                    item = parse_discovery_reply(raw)
                    if item is not None:
                        return item
        except OSError:
            pass
        return None

    def probe(self, values: dict) -> registry.Found:
        host = str(values.get("host", "")).strip()
        if not host:
            raise ValueError("Enter the controller's IP address")
        device = MagicHomeDevice("probe", "Magic Home", host, self.port)
        try:
            status = device._query_status(3.0)
        except OSError as e:
            raise ValueError(f"No Magic Home controller answered at {host} ({e})") from None
        finally:
            device._close()
        if status is None:
            raise ValueError(f"{host} accepted a connection but didn't answer like a Magic Home controller")
        item = self._lookup(host)
        if item is not None:
            item.host, item.port = host, self.port
            return item
        return registry.Found("magichome", f"magichome:{host}", host, self.port, "Magic Home", {"model": ""})

    def create(self, dev_id: str, entry: dict) -> MagicHomeDevice:
        model = str(entry.get("model") or "")
        return MagicHomeDevice(dev_id, f"Magic Home {model}".strip(), entry["host"],
                               int(entry.get("port") or PORT), model)
