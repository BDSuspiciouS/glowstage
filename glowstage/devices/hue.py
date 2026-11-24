"""Philips Hue lights through the bridge's local CLIP API v1 (plain HTTP on port 80).

One HueBridge (a Hub) holds the token; connect() lists the bridge's lights and returns one
HueLight per light. Pairing: press the round link button on the bridge, then call pair()
within 30 seconds.

Streaming: the bridge accepts about 10 light commands per second in total, however many lights
there are. The hub therefore owns ONE sender thread for all its lights. send_frame() only stores
the frame's average color as that light's latest target; the thread sends the pending targets
round-robin, at most one PUT per `min_interval` (0.1 s) over one persistent connection.
Frames that arrive faster are collapsed to the newest one.

Known limitation: the Hue Entertainment API (DTLS over UDP) would give 25+ fps per light and
sync several lights in one packet, but it needs a DTLS library (not in the standard library),
so it isn't used. Effects on Hue therefore look like slow, smooth color changes (~10 updates
per second shared between all lights on the bridge).

A black color (or brightness 0) switches the light off; any other color keeps it on with bri >= 1.
While streaming, black only dims to the lowest brightness (no off/on clicking between beats).
White-ambiance and white-only lights get brightness only (no color temperature is set).
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time

from ..colors import RGB
from ..state import LightState
from . import registry
from .base import Device
from .hub import Hub, NotPairedError
from .net import HttpError, http_json

log = logging.getLogger(__name__)

PAIRING = "Press the round link button on the Hue bridge, then press Pair within 30 seconds."
DEVICETYPE = "glowstage#pc"
SEND_INTERVAL = 0.1            # seconds between two light commands (about 10 per second)
TRANSITION = 1                 # transitiontime in 100 ms units

# Color gamuts (red, green, blue vertices in xy) per Hue gamut type.
GAMUTS = {
    "A": ((0.704, 0.296), (0.2151, 0.7106), (0.138, 0.08)),
    "B": ((0.675, 0.322), (0.409, 0.518), (0.167, 0.04)),
    "C": ((0.6915, 0.3083), (0.17, 0.7), (0.1532, 0.0475)),
}
Gamut = tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
DEFAULT_GAMUT: Gamut = GAMUTS["C"]
WHITE_XY = (0.3127, 0.3290)    # D65


# ---- color conversion ---------------------------------------------------------------------

def _linear(c: int) -> float:
    c = max(0, min(255, c)) / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _encode(c: float) -> float:
    c = max(0.0, c)
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _cross(ax, ay, bx, by) -> float:
    return ax * by - ay * bx


def _in_gamut(p: tuple[float, float], g: Gamut) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = g
    px, py = p
    d1 = _cross(x2 - x1, y2 - y1, px - x1, py - y1)
    d2 = _cross(x3 - x2, y3 - y2, px - x2, py - y2)
    d3 = _cross(x1 - x3, y1 - y3, px - x3, py - y3)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def _closest_on_segment(p, a, b) -> tuple[float, float]:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return (ax + t * dx, ay + t * dy)


def clamp_to_gamut(p: tuple[float, float], g: Gamut) -> tuple[float, float]:
    """The point itself when inside the gamut triangle, else the closest point on its edges."""
    if _in_gamut(p, g):
        return p
    best, best_d = p, float("inf")
    for a, b in ((g[0], g[1]), (g[1], g[2]), (g[2], g[0])):
        q = _closest_on_segment(p, a, b)
        d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
        if d < best_d:
            best, best_d = q, d
    return best


def rgb_to_xy(rgb: RGB, gamut: Gamut | None = None) -> tuple[float, float]:
    """sRGB (0-255) to CIE xy (Wide-RGB D65), clamped into `gamut` when one is given."""
    r, g, b = (_linear(c) for c in rgb)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    point = WHITE_XY if total <= 0 else (x / total, y / total)
    if gamut is not None:
        point = clamp_to_gamut(point, gamut)
    return (round(point[0], 4), round(point[1], 4))


def xy_to_rgb(x: float, y: float) -> RGB:
    """Inverse of rgb_to_xy at full brightness (used to read a light's current color)."""
    if y <= 0:
        return (255, 255, 255)
    big_x, big_y, big_z = x / y, 1.0, (1 - x - y) / y
    r = big_x * 1.656492 - big_y * 0.354851 - big_z * 0.255038
    g = -big_x * 0.707196 + big_y * 1.655397 + big_z * 0.036152
    b = big_x * 0.051713 - big_y * 0.121364 + big_z * 1.011530
    top = max(r, g, b, 1e-9)
    return tuple(int(round(255 * _encode(max(0.0, c) / top))) for c in (r, g, b))  # type: ignore[return-value]


def _gamut_of(info: dict) -> Gamut:
    control = (info.get("capabilities") or {}).get("control") or {}
    raw = control.get("colorgamut")
    try:
        if raw and len(raw) == 3:
            return tuple((float(p[0]), float(p[1])) for p in raw)  # type: ignore[return-value]
    except (TypeError, ValueError, IndexError):
        pass
    return GAMUTS.get(str(control.get("colorgamuttype", "")).upper(), DEFAULT_GAMUT)


# ---- replies from the bridge ----------------------------------------------------------------

def _check(data):
    """Raise for an error reply ([{"error": {...}}]); return the data otherwise."""
    if isinstance(data, list):
        for item in data:
            err = item.get("error") if isinstance(item, dict) else None
            if err:
                if err.get("type") == 1:
                    raise NotPairedError("The bridge forgot this PC: pair again.")
                raise RuntimeError(f"Hue bridge: {err.get('description') or err}")
    return data


# ---- the lights --------------------------------------------------------------------------------

class HueLight(Device):
    kind = "hue"
    group = "room"
    hardware_brightness = False   # frames arrive already scaled by brightness
    max_fps = 10                  # the whole bridge takes about 10 commands per second

    def __init__(self, dev_id: str, name: str, hub: "HueBridge", light_id: str, info: dict | None = None):
        super().__init__(dev_id, name)
        self.hub = hub
        self.light_id = light_id
        self.model = ""
        self.light_type = ""
        self.has_color = False
        self.has_bri = True
        self.gamut: Gamut = DEFAULT_GAMUT
        self._current: dict = {}
        self.pixel_count = 1
        if info:
            self.update_info(info)

    def update_info(self, info: dict) -> None:
        state = info.get("state") or {}
        self.reported_name = info.get("name") or f"Hue light {self.light_id}"
        self.model = info.get("modelid", "") or self.model
        self.light_type = info.get("type", "") or self.light_type
        self.has_color = "xy" in state
        self.has_bri = "bri" in state
        self.gamut = _gamut_of(info)
        self._current = state

    def _path(self, tail: str = "") -> str:
        return f"lights/{self.light_id}" + (f"/{tail}" if tail else "")

    def connect(self) -> None:
        info = self.hub.request("GET", self._path())
        if not isinstance(info, dict) or "state" not in info:
            raise RuntimeError(f"Hue light {self.light_id} is gone from the bridge")
        self.update_info(info)
        self.pixel_count = 1
        if not (info["state"] or {}).get("reachable", True):
            self.online = False
            raise RuntimeError("The light isn't reachable (switched off at the wall?)")
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        st = self._current
        if not st:
            return None
        out = {"on": bool(st.get("on", True))}
        if "bri" in st:
            out["brightness"] = max(1, round(int(st["bri"]) / 2.54))
        if st.get("colormode") == "xy" and st.get("xy"):
            out["color"] = "#{:02x}{:02x}{:02x}".format(*xy_to_rgb(*st["xy"]))
        return out

    def _body(self, rgb: RGB, factor: float, streaming: bool = False) -> dict:
        """The state to send for `rgb` scaled by `factor` (0..1): off for black, else on with bri >= 1.
        While streaming, black stays on at the lowest brightness instead: switching a Hue light
        off and on between the beats of a show makes it flicker and click."""
        top = max(rgb)
        if (top <= 0 or factor <= 0) and not streaming:
            return {"on": False, "transitiontime": TRANSITION}
        body: dict = {"on": True, "transitiontime": TRANSITION}
        if self.has_bri:
            body["bri"] = max(1, min(254, round(254 * factor * top / 255)))
        if self.has_color and top > 0:
            body["xy"] = list(rgb_to_xy(rgb, self.gamut))
        return body

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self.hub.drop(self.id)
        self.hub.request("PUT", self._path("state"), self._body(rgb, state.brightness / 100))

    def power_off(self, timeout: float = 3.0) -> None:
        self.hub.drop(self.id)
        self.hub.request("PUT", self._path("state"), {"on": False}, timeout=timeout)

    def begin_stream(self, state: LightState) -> None:
        self.hub.drop(self.id)
        self.hub.request("PUT", self._path("state"), {"on": True, "transitiontime": TRANSITION})

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        n = len(colors)
        avg = (round(sum(c[0] for c in colors) / n), round(sum(c[1] for c in colors) / n),
               round(sum(c[2] for c in colors) / n))
        self.hub.set_target(self.id, self.light_id, self._body(avg, 1.0, streaming=True))

    def end_stream(self) -> None:
        self.hub.drop(self.id)

    def extra_info(self) -> dict:
        return {"host": self.hub.host, "model": self.model,
                "label": "Philips Hue" + (f" · {self.model}" if self.model else "")}


# ---- the bridge ------------------------------------------------------------------------------------

class HueBridge(Hub):
    kind = "hue"

    def __init__(self, hub_id: str, entry: dict, min_interval: float = SEND_INTERVAL):
        super().__init__(hub_id, entry)
        self.name = entry.get("label") or "Hue Bridge"
        self.port = int(entry.get("port") or 80)
        self.token: str | None = entry.get("token") or None
        self.bridge_id = (entry.get("bridgeid") or hub_id.removeprefix("hue:")).lower()
        self.min_interval = min_interval
        self._cond = threading.Condition()
        self._pending: dict[str, tuple[str, dict]] = {}   # device id -> (light id, body), oldest first
        self._last_sent: dict[str, dict] = {}
        self._epoch: dict[str, int] = {}                  # bumped by drop(), so a send in flight isn't recorded
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._conn: http.client.HTTPConnection | None = None

    @property
    def paired(self) -> bool:
        return bool(self.token)

    # -- plain requests (one connection per request) --

    def _url(self, path: str = "") -> str:
        return f"http://{self.host}:{self.port}/api/{self.token}/{path}"

    def request(self, method: str, path: str, payload=None, timeout: float = 3.0):
        try:
            data = http_json(method, self._url(path), payload, timeout=timeout)
        except HttpError as e:
            if e.status in (401, 403):
                raise NotPairedError("The bridge forgot this PC: pair again.") from None
            raise
        return _check(data)

    def pair(self) -> str:
        """Ask the bridge for a username. The link button must have been pressed."""
        data = http_json("POST", f"http://{self.host}:{self.port}/api", {"devicetype": DEVICETYPE}, timeout=5.0)
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            if "success" in item and item["success"].get("username"):
                self.token = item["success"]["username"]
                self.entry["token"] = self.token
                return self.token
            err = item.get("error")
            if err:
                if err.get("type") == 101:
                    raise NotPairedError("Press the link button on the bridge first, then Pair again within 30 seconds.")
                raise RuntimeError(f"Hue bridge: {err.get('description') or err}")
        raise RuntimeError("Unexpected answer from the Hue bridge")

    def connect(self) -> None:
        if not self.token:
            self.online = False
            self.error = "Not paired"
            raise NotPairedError("Not paired")
        lights = self.request("GET", "lights", timeout=5.0)
        if not isinstance(lights, dict):
            raise RuntimeError("Unexpected answer from the Hue bridge")
        found: dict[str, Device] = {}
        for light_id, info in lights.items():
            if not isinstance(info, dict):
                continue
            dev_id = f"hue:{self.bridge_id}:{str(info.get('uniqueid') or light_id).lower()}"
            existing = self.devices.get(dev_id)
            if isinstance(existing, HueLight):
                existing.light_id = str(light_id)
                existing.update_info(info)
                found[dev_id] = existing
            else:
                found[dev_id] = HueLight(dev_id, info.get("name") or f"Hue light {light_id}", self, str(light_id), info)
        self.devices = found
        self.online = True
        self.error = None

    # -- streaming: one sender thread for the whole bridge --

    def set_target(self, dev_id: str, light_id: str, body: dict) -> None:
        """Remember the newest state for a light; the sender thread delivers it. Never blocks."""
        with self._cond:
            if self._stopping:
                return
            if self._last_sent.get(dev_id) == body:
                self._pending.pop(dev_id, None)
                return
            self._pending[dev_id] = (light_id, body)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"hue-{self.bridge_id}", daemon=True)
                self._thread.start()
            self._cond.notify()

    def drop(self, dev_id: str) -> None:
        """Forget what is pending (and what was last sent) for a light."""
        with self._cond:
            self._pending.pop(dev_id, None)
            self._last_sent.pop(dev_id, None)
            self._epoch[dev_id] = self._epoch.get(dev_id, 0) + 1

    def _run(self) -> None:
        next_at = 0.0
        while True:
            with self._cond:
                while True:
                    if self._stopping:
                        return
                    if not self._pending:
                        self._cond.wait()
                        continue
                    delay = next_at - time.monotonic()
                    if delay > 0:
                        self._cond.wait(delay)
                        continue
                    break
                dev_id = next(iter(self._pending))      # oldest pending light = round-robin
                light_id, body = self._pending.pop(dev_id)
                epoch = self._epoch.get(dev_id, 0)
            ok = self._send(light_id, body)
            next_at = time.monotonic() + self.min_interval
            if ok:
                with self._cond:
                    if not self._stopping and self._epoch.get(dev_id, 0) == epoch:
                        self._last_sent[dev_id] = body

    def _send(self, light_id: str, body: dict) -> bool:
        payload = json.dumps(body).encode()
        path = f"/api/{self.token}/lights/{light_id}/state"
        for attempt in (0, 1):    # a kept-alive connection may have been closed by the bridge: once more
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(self.host, self.port, timeout=3.0)
                self._conn.request("PUT", path, payload, {"Content-Type": "application/json"})
                reply = self._conn.getresponse().read()
                try:
                    _check(json.loads(reply) if reply else None)
                except NotPairedError:
                    log.warning("Hue bridge %s no longer accepts this PC's token", self.bridge_id)
                    return False
                except RuntimeError as e:
                    log.debug("%s", e)
                    return False
                return True
            except (OSError, http.client.HTTPException):
                self._close_conn()
                if attempt:
                    return False
        return False

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._cond:
            self._stopping = True
            self._pending.clear()
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close_conn()


# ---- discovery and pairing ------------------------------------------------------------------------------

def _split_host(text: str) -> tuple[str, int]:
    host = text.strip().removeprefix("http://").rstrip("/")
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isdigit():
            return name, int(port)
    return host, 80


def parse_ssdp(data: bytes, addr: str) -> registry.Found | None:
    """A Hue bridge's SSDP reply carries a 'hue-bridgeid' header."""
    headers = {}
    for line in data.decode("latin-1").split("\r\n")[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    bridge_id = headers.get("hue-bridgeid", "").lower()
    if not bridge_id:
        return None
    return registry.Found("hue", f"hue:{bridge_id}", addr, 80, "Hue Bridge", {"bridgeid": bridge_id})


class Driver(registry.Driver):
    kind, title = "hue", "Philips Hue"
    hub = True
    beta = True
    mdns = ("_hue._tcp.local.",)
    pairing = PAIRING

    def from_mdns(self, service, name, host, port, props):
        bridge_id = (props.get("bridgeid") or "").lower()
        if not bridge_id:
            return None
        label = name.split("._")[0] or "Hue Bridge"
        return registry.Found("hue", f"hue:{bridge_id}", host, port or 80, label,
                              {"bridgeid": bridge_id, "modelid": props.get("modelid", "")})

    def scan(self, timeout: float = 2.0) -> list[registry.Found]:
        """SSDP M-SEARCH for bridges that mDNS missed. Never raises."""
        found: dict[str, registry.Found] = {}
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.3)
            msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                   "MX: 1\r\nST: upnp:rootdevice\r\n\r\n").encode()
            sock.sendto(msg, ("239.255.255.250", 1900))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    data, (addr, _port) = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                item = parse_ssdp(data, addr)
                if item is not None:
                    found[item.dev_id] = item
        except Exception as e:   # scan must never raise
            log.debug("Hue SSDP scan failed: %s", e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        return list(found.values())

    def probe(self, values):
        host, port = _split_host(values.get("host", ""))
        try:
            cfg = http_json("GET", f"http://{host}:{port}/api/config", timeout=4.0)
        except Exception as e:
            raise ValueError(f"No Hue bridge answered at {host} ({e})") from None
        if not isinstance(cfg, dict) or not cfg.get("bridgeid"):
            raise ValueError(f"The device at {host} isn't a Hue bridge")
        bridge_id = str(cfg["bridgeid"]).lower()
        return registry.Found("hue", f"hue:{bridge_id}", host, port, cfg.get("name") or "Hue Bridge",
                              {"bridgeid": bridge_id, "modelid": cfg.get("modelid", "")})

    def create(self, dev_id, entry):
        return HueBridge(dev_id, entry)

    def pair(self, target):
        return {"token": target.pair()}
