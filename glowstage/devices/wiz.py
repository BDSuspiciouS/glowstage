"""WiZ lights (Philips WiZ and the other WiZ Connected brands: bulbs, strips, lamps) over the LAN.

The protocol, as far as it is known (written from the community documentation, not from the
vendor: there is none). Nothing here has been run against a real light yet, hence "beta".

- Every message is one JSON datagram over UDP to port 38899 of the light, and the light answers
  with one JSON datagram to the address and port the question came from. Nothing to pair: the
  light only has to be on the same Wi-Fi, with "Allow local communication" on in the WiZ app
  (it is on by default on most lights).
- {"method": "getPilot", "params": {}} asks for the state. The answer is
  {"method": "getPilot", "env": "pro", "result": {"mac": "a8bb50...", "rssi": -60, "state": true,
  "sceneId": 0, "r": 255, "g": 0, "b": 0, "c": 0, "w": 0, "dimming": 100, ...}}. A light that is
  in a white mode reports "temp" instead of r/g/b, a light in a scene reports its "sceneId".
  Broadcasting this to 255.255.255.255:38899 finds the lights: every one of them answers, and
  its MAC address is in the answer, which is what we keep a light by (so a new IP follows).
- {"method": "getSystemConfig", "params": {}} answers with the "mac", the "fwVersion" and the
  "moduleName", e.g. "ESP01_SHRGB1C_31" or "ESP03_SHRGB3_01ABI". The second part of that name
  says what the light can do: SHRGB / DHRGB color, SHTW tunable white, SHDW dimmable only.
- {"method": "setPilot", "params": {...}} sets the look: "r", "g", "b" (0-255, the light mixes
  them with its white LEDs), or "temp" (2200-6500 K) for whites, or "sceneId" (1-32) with "speed"
  (10-200) for one of its own scenes; and "dimming" (10-100, lower is refused on most lights).
  "state" switches it on or off too. {"method": "setState", "params": {"state": false}} switches
  it off. The answer is {"method": "setPilot", "env": "pro", "result": {"success": true}}, or an
  "error" object with a code and a message.
- It takes roughly 10-20 messages a second; we send at most 10 (max_fps), which also makes the
  light show treat it as a slow light. There is no way to ask for a fade: a change is at once.

A WiZ strip is one color as a whole over this API, so an effect is shown as the average of the
frame's colors, as for Govee and Yeelight.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time

from ..colors import RGB, kelvin_to_rgb, rgb_to_hex
from ..state import LightState
from . import registry
from .base import Device

PORT = 38899
PAIRING = "Found automatically once Allow local communication is on in the WiZ app"
MIN_DIMMING = 10                 # the lowest level the light takes; below it the command is refused
TEMP_MIN, TEMP_MAX = 2200, 6500
SPEED_MIN, SPEED_MAX = 10, 200   # a scene's own speed
WHITE_DISTANCE = 28              # how close (0-255 per channel) a color must be to a white to be sent as one
STALL_AFTER = 4.0                # seconds of no answer to a stream before the light counts as gone

# The lights' own scenes (sceneId -> name). Lights without color only have the white ones.
SCENES = {1: "Ocean", 2: "Romance", 3: "Sunset", 4: "Party", 5: "Fireplace", 6: "Cozy", 7: "Forest",
          8: "Pastel colors", 9: "Wake up", 10: "Bedtime", 11: "Warm white", 12: "Daylight",
          13: "Cool white", 14: "Night light", 15: "Focus", 16: "Relax", 17: "True colors",
          18: "TV time", 19: "Plantgrowth", 20: "Spring", 21: "Summer", 22: "Fall", 23: "Deepdive",
          24: "Jungle", 25: "Mojito", 26: "Club", 27: "Christmas", 28: "Halloween",
          29: "Candlelight", 30: "Golden white", 31: "Pulse", 32: "Steampunk"}
WHITE_SCENES = {9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 30}

COLOR, TUNABLE, DIM = "color", "tunable", "dim"
KIND_LABELS = {COLOR: "color", TUNABLE: "tunable white", DIM: "dimmable"}


# ---- wire format (pure functions, easy to test) ------------------------------------------

def build(method: str, params: dict | None = None) -> bytes:
    return json.dumps({"method": method, "params": params or {}}, separators=(",", ":")).encode()


def parse(data: bytes) -> dict | None:
    """One received datagram as a dict with a "method"; None for anything else."""
    try:
        msg = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("method"), str) else None


def clean_mac(value) -> str:
    mac = str(value or "").replace(":", "").replace("-", "").lower()
    return mac if re.fullmatch(r"[0-9a-f]{12}", mac) else ""


def capabilities(module: str = "", pilot: dict | None = None) -> str:
    """What the light can do: COLOR, TUNABLE (white only, any temperature) or DIM (brightness only).
    The module name decides ("ESP01_SHRGB1C_31"); without one, what its state shows does."""
    parts = module.upper().split("_")
    kind = parts[1] if len(parts) > 1 else ""
    if "RGB" in kind:
        return COLOR
    if "TW" in kind:
        return TUNABLE
    if "DW" in kind:
        return DIM
    pilot = pilot or {}
    if "r" in pilot or "g" in pilot or "b" in pilot:
        return COLOR
    if "temp" in pilot:
        return TUNABLE
    return COLOR if not pilot else DIM


def _normal(rgb: RGB) -> RGB:
    """The same color at full strength (its biggest channel at 255); black stays black."""
    top = max(rgb)
    if top <= 0:
        return (0, 0, 0)
    return tuple(min(255, round(c * 255 / top)) for c in rgb)


_WHITES = [(k, _normal(kelvin_to_rgb(k))) for k in range(TEMP_MIN, TEMP_MAX + 1, 100)]


def nearest_white(rgb: RGB) -> tuple[int, float]:
    """(the temperature in kelvin that looks most like this color, how far off it still is)."""
    norm = _normal(rgb)
    best = min(_WHITES, key=lambda w: sum((a - b) ** 2 for a, b in zip(norm, w[1])))
    return best[0], sum((a - b) ** 2 for a, b in zip(norm, best[1])) ** 0.5


def level(rgb: RGB, brightness: float) -> int:
    """The "dimming" for a color at a brightness (percent): both count, and never under the floor."""
    return max(MIN_DIMMING, min(100, round(brightness * max(rgb) / 255)))


def pilot_params(rgb: RGB, brightness: float, caps: str = COLOR) -> dict:
    """The setPilot parameters that show this color. WiZ mixes RGB with its white LEDs, so a white
    made of red, green and blue looks bluish: a color that is (nearly) a white goes as a temperature."""
    rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    dimming = level(rgb, brightness)
    if caps == DIM:
        return {"dimming": dimming}
    temp, distance = nearest_white(rgb)
    if caps == TUNABLE or (max(rgb) > 0 and distance <= WHITE_DISTANCE):
        return {"temp": temp, "dimming": dimming}
    r, g, b = _normal(rgb)   # the dimming carries the strength; the color is sent full
    return {"r": r, "g": g, "b": b, "dimming": dimming}


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


def _drain(sock: socket.socket) -> list[dict]:
    """Take every answer that is waiting (without waiting for more) and return the good ones."""
    out = []
    sock.settimeout(0.0)
    for _ in range(200):
        try:
            data, _addr = sock.recvfrom(4096)
        except (BlockingIOError, socket.timeout):
            break
        except OSError:
            continue
        msg = parse(data)
        if msg:
            out.append(msg)
    return out


def _receive(sock: socket.socket, deadline: float):
    """Yield (message, address) for the datagrams that are WiZ messages, until the deadline."""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        sock.settimeout(left)
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            return
        except OSError:
            continue
        msg = parse(data)
        if msg:
            yield msg, addr


def exchange(sock: socket.socket, addr: tuple[str, int], method: str, params: dict | None = None,
             timeout: float = 1.0, retries: int = 1) -> dict | None:
    """Send a message and wait for the answer to the same method. None when nothing came (UDP is
    lossy: one retry by default). A light that answers with an error raises RuntimeError."""
    _drain(sock)
    data = build(method, params)
    for _ in range(retries + 1):
        sock.sendto(data, addr)
        for msg, _src in _receive(sock, time.monotonic() + timeout):
            if msg.get("method") != method:
                continue
            err = msg.get("error")
            if err:
                raise RuntimeError(err.get("message", "error") if isinstance(err, dict) else str(err))
            return msg
    return None


def _result(msg: dict | None) -> dict:
    res = (msg or {}).get("result")
    return res if isinstance(res, dict) else {}


def _broadcast_targets() -> list[str]:
    """Where to shout: the whole-network broadcast, and the /24 broadcast of each of this PC's
    addresses (Windows sends the first one out of one network card only)."""
    out = ["255.255.255.255"]
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                out.append(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    return list(dict.fromkeys(out))


def _ask_info(sock: socket.socket, addr: tuple[str, int], timeout: float, retries: int) -> tuple[str, str]:
    """(moduleName, firmware) of a light; empty for whatever it doesn't answer."""
    try:
        res = _result(exchange(sock, addr, "getSystemConfig", None, timeout, retries))
    except RuntimeError:
        return "", ""
    return str(res.get("moduleName") or ""), str(res.get("fwVersion") or "")


# ---- device ------------------------------------------------------------------------------

class WizDevice(Device):
    kind = "wiz"
    group = "room"
    hardware_brightness = False   # streamed frames arrive already scaled by the brightness
    max_fps = 10                  # the light takes 10-20 messages a second; we keep to the safe end

    def __init__(self, dev_id: str, name: str, host: str, port: int = PORT, mac: str = "", module: str = ""):
        super().__init__(dev_id, name)
        self.host = host
        self.port = port
        self.mac = clean_mac(mac) or clean_mac(dev_id.split(":", 1)[-1])
        self.module = module
        self.firmware = ""
        self.caps = capabilities(module)
        self.timeout = 1.0                    # seconds per attempt for requests that expect an answer
        self.stall_after = STALL_AFTER
        self._sock = _udp_socket()
        self._io = threading.Lock()           # one request/answer exchange at a time
        self._pilot: dict = {}
        self._last: dict | None = None        # what the stream sent last, so that a still frame sends nothing
        self._waiting: float | None = None    # since when a stream message has had no answer

    @property
    def _addr(self) -> tuple[str, int]:
        return (self.host, self.port)

    def _call(self, method: str, params: dict, timeout: float | None = None) -> dict:
        """Send a command and wait for the light to confirm it (TimeoutError when it doesn't)."""
        with self._io:
            msg = exchange(self._sock, self._addr, method, params, self.timeout if timeout is None else timeout)
        if msg is None:
            raise TimeoutError(f"WiZ at {self.host} did not answer")
        res = _result(msg)
        if res.get("success") is False:
            raise RuntimeError("The light refused the command")
        return res

    def connect(self) -> None:
        with self._io:
            pilot = exchange(self._sock, self._addr, "getPilot", None, self.timeout)
            if pilot is None:
                raise TimeoutError(f"No WiZ answered at {self.host}")
            if not self.module:
                self.module, self.firmware = _ask_info(self._sock, self._addr, self.timeout, 1)
        self._pilot = _result(pilot)
        self.caps = capabilities(self.module, self._pilot)
        self.reported_name = "WiZ light"
        self.pixel_count = 1
        self._last, self._waiting = None, None
        self.online = True
        self.error = None

    def read_state(self) -> dict | None:
        p = self._pilot
        if not p:
            return None
        out: dict = {"on": bool(p.get("state"))}
        if isinstance(p.get("dimming"), (int, float)):
            out["brightness"] = int(max(0, min(100, p["dimming"])))
        scene = p.get("sceneId")
        if isinstance(scene, int) and scene in self._scenes():
            out.update(mode="native", native=str(scene))
            if isinstance(p.get("speed"), (int, float)):
                out["speed"] = max(1, min(100, round((p["speed"] - SPEED_MIN) * 99 / (SPEED_MAX - SPEED_MIN)) + 1))
        elif isinstance(p.get("temp"), (int, float)) and p["temp"] > 0:
            out["color"] = rgb_to_hex(kelvin_to_rgb(p["temp"]))
        elif any(p.get(k) for k in "rgb"):
            out["color"] = rgb_to_hex((p.get("r", 0), p.get("g", 0), p.get("b", 0)))
        return out

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._last = None
        self._call("setPilot", {"state": True, **pilot_params(rgb, state.brightness, self.caps)})

    def power_off(self, timeout: float = 3.0) -> None:
        self._last = None
        self._call("setState", {"state": False}, min(timeout / 2, self.timeout))

    # ---- the light's own scenes ---------------------------------------------------------

    def _scenes(self) -> dict[int, str]:
        if self.caps == COLOR:
            return SCENES
        if self.caps == TUNABLE:
            return {i: n for i, n in SCENES.items() if i in WHITE_SCENES}
        return {}

    def native_effects(self) -> list[dict]:
        return [{"id": str(i), "name": n} for i, n in sorted(self._scenes().items(), key=lambda s: s[1].lower())]

    def show_native(self, state: LightState) -> None:
        self._last = None
        speed = SPEED_MIN + round((state.speed - 1) * (SPEED_MAX - SPEED_MIN) / 99)
        self._call("setPilot", {"state": True, "sceneId": int(state.native), "speed": speed,
                                "dimming": max(MIN_DIMMING, min(100, int(state.brightness)))})

    # ---- effects: one message per frame, no waiting for the answer -----------------------

    def begin_stream(self, state: LightState) -> None:
        self._last = None
        self._call("setPilot", {"state": True})
        self._waiting = None

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        with self._io:
            if _drain(self._sock):   # the light answers every message; any answer says it is there
                self._waiting = None
            now = time.monotonic()
            if self._waiting is not None and now - self._waiting > self.stall_after:
                self._waiting = None
                raise TimeoutError(f"WiZ at {self.host} stopped answering")
            n = len(colors)
            avg = (sum(c[0] for c in colors) // n, sum(c[1] for c in colors) // n, sum(c[2] for c in colors) // n)
            params = pilot_params(avg, 100, self.caps)   # (the frame already carries the brightness)
            if params == self._last:
                return
            self._sock.sendto(build("setPilot", {"state": True, **params}), self._addr)
            self._last = params
            if self._waiting is None:
                self._waiting = now

    def extra_info(self) -> dict:
        return {"host": self.host, "label": "WiZ · " + KIND_LABELS[self.caps],
                "firmware": self.firmware, "model": self.module}


# ---- driver ------------------------------------------------------------------------------

class Driver(registry.Driver):
    kind, title = "wiz", "WiZ"
    pairing = PAIRING
    beta = True

    def scan(self, timeout: float = 2.0, port: int = PORT, broadcast: str | None = None) -> list[registry.Found]:
        try:
            return self._scan(timeout, port, [broadcast] if broadcast else _broadcast_targets())
        except Exception:
            return []

    def _scan(self, timeout: float, port: int, targets: list[str]) -> list[registry.Found]:
        sock = _udp_socket()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            seen: dict[str, str] = {}
            for _ in range(2):   # UDP is lossy: ask twice
                for target in targets:
                    try:
                        sock.sendto(build("getPilot"), (target, port))
                    except OSError:
                        continue
                for msg, addr in _receive(sock, time.monotonic() + timeout / 2):
                    mac = clean_mac(_result(msg).get("mac")) if msg.get("method") == "getPilot" else ""
                    if mac:
                        seen[mac] = addr[0]
            found = []
            for mac, ip in sorted(seen.items()):
                module, firmware = _ask_info(sock, (ip, port), 0.5, 1)
                found.append(self._found(mac, ip, port, module, firmware))
            return found
        finally:
            sock.close()

    @staticmethod
    def _found(mac: str, host: str, port: int, module: str, firmware: str = "") -> registry.Found:
        label = f"WiZ {KIND_LABELS[capabilities(module)]} light ({mac[-4:]})"
        return registry.Found("wiz", f"wiz:{mac}", host, port, label,
                              {"mac": mac, "module": module, "firmware": firmware})

    def probe(self, values: dict, port: int = PORT) -> registry.Found:
        host = values.get("host", "").strip()
        if not host:
            raise ValueError("Enter the IP address of the WiZ light")
        sock = _udp_socket()
        try:
            msg = exchange(sock, (host, port), "getPilot", None, 1.0, 1)
            mac = clean_mac(_result(msg).get("mac"))
            if not mac:
                raise ValueError(f"No WiZ light answered at {host}. "
                                 "Is Allow local communication turned on in the WiZ app?")
            module, firmware = _ask_info(sock, (host, port), 1.0, 1)
        except (OSError, RuntimeError) as e:
            raise ValueError(f"No WiZ light answered at {host} ({e}). "
                             "Is Allow local communication turned on in the WiZ app?") from None
        finally:
            sock.close()
        return self._found(mac, host, port, module, firmware)

    def create(self, dev_id: str, entry: dict) -> WizDevice:
        return WizDevice(dev_id, entry.get("label") or "WiZ light", entry["host"],
                         int(entry.get("port") or PORT), entry.get("mac") or "", entry.get("module") or "")
