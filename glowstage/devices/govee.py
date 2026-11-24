"""Govee lights through the official "Govee LAN API" (UDP + JSON).

Discovery: a multicast "scan" to 239.255.255.250:4001; every light answers to UDP port 4002 of
our IP. Control goes to UDP port 4003 of the light (turn, brightness, colorwc, devStatus), and
status answers also come back to port 4002. One shared listener on 4002 serves discovery and
the status checks of every Govee device (UDP can only be bound once per machine, so it is
shared). The light needs "LAN Control" switched on in the Govee Home app.

App effects are streamed as one colorwc per frame with the frame's average color: the LAN API
has no per-segment control. (The per-segment "razer"/DreamView mode that some models have is
undocumented, so it isn't used.)
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time

from ..colors import RGB, kelvin_to_rgb, rgb_to_hex
from ..state import LightState
from . import registry
from .base import Device

log = logging.getLogger(__name__)

MULTICAST_GROUP = "239.255.255.250"
SCAN_PORT = 4001       # multicast scan requests go here
REPLY_PORT = 4002      # scan and status answers come back here (on our side)
CONTROL_PORT = 4003    # commands go here (on the light)
SCAN_MESSAGE = b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}'
PAIRING = "Turn on LAN Control for the light in the Govee Home app (device settings → LAN Control)."
LISTEN_RETRY_S = 15.0   # how long to wait before trying to bind port 4002 again after a failure


def _encode(cmd: str, data: dict) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode()


def _decode(raw: bytes) -> tuple[str, dict] | None:
    """(cmd, data) of a Govee datagram, None when it isn't one."""
    try:
        msg = json.loads(raw.decode("utf-8", "replace"))["msg"]
        cmd, data = msg["cmd"], msg.get("data") or {}
    except (ValueError, KeyError, TypeError):
        return None
    return (cmd, data) if isinstance(cmd, str) and isinstance(data, dict) else None


def parse_scan_reply(raw: bytes) -> registry.Found | None:
    """A scan answer ({"msg":{"cmd":"scan","data":{"ip":...,"device":...,"sku":...}}}) as a Found."""
    parsed = _decode(raw)
    if not parsed or parsed[0] != "scan":
        return None
    data = parsed[1]
    ip, device, sku = data.get("ip"), data.get("device"), str(data.get("sku") or "")
    if not isinstance(ip, str) or not ip or not isinstance(device, str) or not device:
        return None
    ident = device.replace(":", "").lower()
    return registry.Found("govee", f"govee:{ident}", ip, CONTROL_PORT, sku, {"sku": sku})


def parse_status_reply(raw: bytes) -> dict | None:
    """The data of a devStatus answer, None for anything else."""
    parsed = _decode(raw)
    return parsed[1] if parsed and parsed[0] == "devStatus" else None


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


class Listener:
    """A UDP socket on port 4002 that hands every datagram to its subscribers (on its own thread)."""

    def __init__(self, port: int = REPLY_PORT, host: str = "0.0.0.0"):
        self.port = port
        self.host = host
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._failed_at: float | None = None
        self._subs: list = []

    def ensure(self) -> bool:
        """Start listening if we aren't yet. False when the port can't be bound."""
        with self._lock:
            if self._sock is not None:
                return True
            if self._failed_at is not None and time.monotonic() - self._failed_at < LISTEN_RETRY_S:
                return False
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
                sock.settimeout(0.5)
            except OSError as e:
                sock.close()
                self._failed_at = time.monotonic()
                log.info("Govee: can't listen on UDP %s: %s", self.port, e)
                return False
            self._failed_at = None
            self.port = sock.getsockname()[1]
            self._sock = sock
            threading.Thread(target=self._run, args=(sock,), name="govee-listener", daemon=True).start()
            return True

    def _run(self, sock: socket.socket) -> None:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                if self._sock is not sock:
                    return
                continue
            except ConnectionResetError:   # Windows reports an ICMP "port unreachable" this way
                continue
            except OSError:
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                return
            for callback in list(self._subs):
                try:
                    callback(data, addr[0])
                except Exception:
                    log.debug("Govee listener callback failed", exc_info=True)

    def subscribe(self, callback):
        """Call callback(data, sender_ip) for every datagram. Returns the function that unsubscribes."""
        self._subs.append(callback)

        def unsubscribe() -> None:
            try:
                self._subs.remove(callback)
            except ValueError:
                pass
        return unsubscribe

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()


LISTENER = Listener()   # shared by every Govee device and the driver; binds lazily


class GoveeDevice(Device):
    kind = "govee"
    group = "room"
    hardware_brightness = False   # frames arrive already scaled by the brightness
    max_fps = 10

    def __init__(self, dev_id: str, name: str, host: str, port: int = CONTROL_PORT, sku: str = "",
                 listener: Listener | None = None):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.sku = sku
        self.listener = listener or LISTENER
        self.note = ""
        self.reported_name = f"Govee {sku}".strip()
        self._status: dict | None = None
        self._last_frame: RGB | None = None
        self._last_frame_at = 0.0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ---- wire ---------------------------------------------------------------------

    def _send(self, cmd: str, data: dict) -> None:
        self._sock.sendto(_encode(cmd, data), (self.host, self.port))

    def _request_status(self, timeout: float) -> dict | None:
        """Ask for devStatus and wait for the answer (needs the listener). None when it never comes."""
        done = threading.Event()
        box: list[dict] = []

        def on_datagram(raw: bytes, ip: str) -> None:
            if ip == self.host:
                status = parse_status_reply(raw)
                if status is not None:
                    box.append(status)
                    done.set()

        unsubscribe = self.listener.subscribe(on_datagram)
        try:
            for _ in range(2):   # UDP loses packets: ask once more halfway
                self._send("devStatus", {})
                if done.wait(timeout / 2):
                    break
        finally:
            unsubscribe()
        return box[0] if box else None

    # ---- Device ---------------------------------------------------------------------

    def connect(self) -> None:
        if self.listener.ensure():
            status = self._request_status(1.5)
            if status is None:
                raise TimeoutError(f"No answer from the Govee light at {self.host}. "
                                   "Is LAN Control switched on in the Govee Home app?")
            self._status = status
            self.note = ""
        else:
            # UDP is fire-and-forget: without port 4002 we can't hear the light, but can still control it.
            self.note = "Can't listen on UDP port 4002 (in use), so the light's answer isn't checked."
            log.warning("Govee %s: %s", self.host, self.note)
        self.pixel_count = 1
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        st = self._status
        if not st:
            return None
        out: dict = {"on": bool(st.get("onOff", 1))}
        if isinstance(st.get("brightness"), (int, float)):
            out["brightness"] = int(max(0, min(100, st["brightness"])))
        color = st.get("color")
        kelvin = st.get("colorTemInKelvin") or 0
        if kelvin:
            out["color"] = rgb_to_hex(kelvin_to_rgb(kelvin))
        elif isinstance(color, dict) and any(color.get(k) for k in "rgb"):
            out["color"] = rgb_to_hex((color.get("r", 0), color.get("g", 0), color.get("b", 0)))
        return out

    def _color(self, rgb: RGB) -> None:
        self._send("colorwc", {"color": {"r": int(rgb[0]), "g": int(rgb[1]), "b": int(rgb[2])},
                               "colorTemInKelvin": 0})

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._last_frame = None
        self._send("turn", {"value": 1})
        self._send("brightness", {"value": max(1, min(100, int(state.brightness)))})
        self._color(rgb)

    def power_off(self, timeout: float = 3.0) -> None:
        self._last_frame = None
        deadline = time.monotonic() + max(timeout, 0.0)
        self._send("turn", {"value": 0})
        if time.monotonic() + 0.05 < deadline:   # one repeat: a lost datagram would leave the light on
            time.sleep(0.05)
            self._send("turn", {"value": 0})

    def begin_stream(self, state: LightState) -> None:
        self._last_frame = None
        self._send("turn", {"value": 1})
        self._send("brightness", {"value": 100})   # the frames carry the brightness

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        rgb = average_color(colors)
        now = time.monotonic()
        if rgb == self._last_frame and now - self._last_frame_at < 1.0:
            return   # nothing changed (re-sent once a second in case a datagram was lost)
        try:
            self._color(rgb)
        except OSError:
            return   # drop the frame
        self._last_frame, self._last_frame_at = rgb, now

    def end_stream(self) -> None:
        self._last_frame = None

    def extra_info(self) -> dict:
        info = {"host": self.host, "label": f"Govee · {self.sku}" if self.sku else "Govee"}
        if self.note:
            info["note"] = self.note
        return info


class Driver(registry.Driver):
    kind, title = "govee", "Govee"
    pairing = PAIRING
    beta = True

    def __init__(self, listener: Listener | None = None, scan_target: tuple[str, int] | None = None,
                 scan_port: int = SCAN_PORT, control_port: int = CONTROL_PORT):
        # The arguments exist for the tests; the app builds Driver() with the defaults.
        self.listener = listener or LISTENER
        self.scan_target = scan_target   # None = multicast on every network interface
        self.scan_port = scan_port
        self.control_port = control_port

    def _send_scan(self) -> None:
        if self.scan_target is not None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(SCAN_MESSAGE, self.scan_target)
            return
        # Multicast leaves through one interface only: send it through each local address.
        for local in ["", *_local_ipv4s()]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                    if local:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
                    sock.sendto(SCAN_MESSAGE, (MULTICAST_GROUP, self.scan_port))
            except OSError:
                continue

    def scan(self, timeout: float = 2.0) -> list[registry.Found]:
        try:
            if not self.listener.ensure():
                return []
            found: dict[str, registry.Found] = {}

            def on_datagram(raw: bytes, ip: str) -> None:
                item = parse_scan_reply(raw)
                if item is not None:
                    found[item.dev_id] = item

            unsubscribe = self.listener.subscribe(on_datagram)
            try:
                self._send_scan()
                time.sleep(timeout / 2)
                self._send_scan()   # once more: UDP and multicast lose packets
                time.sleep(timeout / 2)
            finally:
                unsubscribe()
            return list(found.values())
        except Exception:
            return []

    def probe(self, values: dict) -> registry.Found:
        host = str(values.get("host", "")).strip()
        if not host:
            raise ValueError("Enter the light's IP address")
        try:
            socket.inet_aton(host)
        except OSError:
            raise ValueError(f"{host} isn't an IP address") from None
        if not self.listener.ensure():   # can't hear answers: accept it unchecked
            return registry.Found("govee", f"govee:{host}", host, self.control_port, "Govee", {"sku": ""})

        scan_hit: list[registry.Found] = []
        status_seen = threading.Event()

        def on_datagram(raw: bytes, ip: str) -> None:
            if ip != host:
                return
            item = parse_scan_reply(raw)
            if item is not None:
                scan_hit.append(item)
            elif parse_status_reply(raw) is not None:
                status_seen.set()

        unsubscribe = self.listener.subscribe(on_datagram)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                deadline = time.monotonic() + 2.0
                next_send = 0.0
                while time.monotonic() < deadline and not scan_hit:
                    if time.monotonic() >= next_send:
                        try:
                            sock.sendto(SCAN_MESSAGE, (host, self.scan_port))
                            sock.sendto(_encode("devStatus", {}), (host, self.control_port))
                        except OSError as e:
                            raise ValueError(f"Can't reach {host} ({e})") from None
                        next_send = time.monotonic() + 0.7
                    if status_seen.is_set():
                        deadline = min(deadline, time.monotonic() + 0.4)   # a scan answer may still follow
                    time.sleep(0.02)
        finally:
            unsubscribe()
        if scan_hit:
            item = scan_hit[0]
            item.host = host
            item.port = self.control_port
            return item
        if status_seen.is_set():
            return registry.Found("govee", f"govee:{host}", host, self.control_port, "Govee", {"sku": ""})
        raise ValueError(f"No Govee light answered at {host}. "
                         "Is LAN Control switched on in the Govee Home app?")

    def create(self, dev_id: str, entry: dict) -> GoveeDevice:
        sku = str(entry.get("sku") or "")
        return GoveeDevice(dev_id, f"Govee {sku}".strip(), entry["host"],
                           int(entry.get("port") or CONTROL_PORT), sku, self.listener)
