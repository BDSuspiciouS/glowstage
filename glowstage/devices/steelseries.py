"""SteelSeries keyboards, mice and headsets through SteelSeries GG's GameSense (beta).

When SteelSeries GG (Engine) runs it holds its devices, so OpenRGB can't reach them. GG serves
SteelSeries' official GameSense SDK on this PC, a small REST API, and this is how Glowstage uses it.
Everything below is written from the GameSense SDK documentation; it has not been tried on real
SteelSeries hardware yet, which is why it is labelled beta and is off until you switch it on.

What GameSense wants (the parts used here):
  - Where it listens: %PROGRAMDATA%\\SteelSeries\\SteelSeries Engine 3\\coreProps.json holds
    {"address": "127.0.0.1:PORT", ...}. The port changes every time GG starts, so the file is read
    again whenever a call fails. No file, or nothing listening, means GG isn't running.
  - POST /game_metadata {"game", "game_display_name", "developer", "deinitialize_timer_length_ms"}
    registers us. If we go quiet for that long (a crash), GG takes its own lighting back by itself.
  - POST /bind_game_event {"game", "event", "value_optional", "handlers": [...]} says what an event
    does to the devices. POST /game_event {"game", "event", "data"} then sends a value for it.
  - POST /game_heartbeat {"game"} keeps the registration alive (sending an event counts too).
  - POST /stop_game and POST /remove_game give the lighting back to GG's own effects.

How each device type is lit (the simplest thing that gives a whole frame):
  - Keyboard: a "bitmap" handler on the "rgb-per-key-zones" device type. One event ("BITMAP")
    carries the whole keyboard as 132 [r, g, b] entries, 22 columns by 6 rows, row after row
    (the entry of the key in row r, column c is bitmap[r * 22 + c]).
  - Mouse and headset: the "rgb-N-zone" device types, with a "context-color" handler per zone.
    The event's data.frame holds one color per zone under the handler's "context-frame-key", like
    {"zone2-one": {"red": 255, "green": 0, "blue": 0}}. GameSense doesn't say how many zones the
    device in front of it has, so a mouse event binds every size (1, 2, 3, 5, 8, 12, 17 and 24
    zones) and GG uses the handlers that fit its device. Each size has its own frame keys, so a
    3-zone mouse gets three colors spread over the whole light, not the first three of 24.
    A headset is the 2-zone type. Mice and headsets share the name "rgb-2-zone", so while the
    headset is ticked the 2-zone handler belongs to the headset and a 2-zone mouse isn't lit.
Glowstage's lights map onto these: the keyboard light has one pixel per key, ordered left to
right and then top down (so effects sweep across the board); the mouse and headset lights have a
few pixels that are spread over the zones.

All calls go to 127.0.0.1 and nowhere else. One thread does all the talking (so one connection is
enough and the effect engine never waits for HTTP), and only frames that changed are sent.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
from pathlib import Path

from ..colors import RGB, scale
from ..state import LightState
from .base import Device

log = logging.getLogger(__name__)

GAME = "GLOWSTAGE"
PROPS_PATH = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "SteelSeries" / "SteelSeries Engine 3" / "coreProps.json"
HEARTBEAT_EVERY = 10.0      # seconds without any call before a heartbeat (GG lets go of a game that is quiet for a minute)
DEINITIALIZE_MS = 60000     # how long GG waits for us before it takes the lighting back itself
RETRY = 4.0                 # seconds between tries while GG can't be reached
TIMEOUT = 2.0               # seconds one call may take
CONNECT_TIMEOUT = 1.0       # ... and opening the connection (Windows can take two seconds to refuse one on this PC)
LOCAL = ("127.0.0.1", "localhost")

NOT_RUNNING = "SteelSeries GG isn't running"
NOT_ANSWERING = "SteelSeries GG isn't answering"

COLUMNS, ROWS = 22, 6       # the per-key bitmap
KEYS = COLUMNS * ROWS

ONES = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]
ZONE_WORDS = ONES + ["twenty-" + w for w in ONES[:4]]      # the names GameSense gives the zones: "one" ... "twenty-four"
MOUSE_ZONES = (1, 2, 3, 5, 8, 12, 17, 24)

# What can be ticked. `pixels`: how many pixels the light has; `desk`: where it stands on the stage.
TYPES = {
    "keyboard": {"title": "SteelSeries keyboard (per key)", "name": "SteelSeries keyboard", "event": "BITMAP",
                 "pixels": KEYS, "desk": "KEYBOARD"},
    "mouse": {"title": "SteelSeries mouse", "name": "SteelSeries mouse", "event": "MOUSE",
              "pixels": 8, "desk": "MOUSE"},
    "headset": {"title": "SteelSeries headset", "name": "SteelSeries headset", "event": "HEADSET",
                "pixels": 2, "desk": "HEADSET"},
}
DEFAULT_TYPES = ["keyboard", "mouse"]


def clean_types(types) -> list[str]:
    """The ticked types that exist, in the usual order (what the settings may hold)."""
    return [t for t in TYPES if t in (types if isinstance(types, (list, tuple, set)) else [])]


def zone_counts(kind: str, types: list[str]) -> tuple[int, ...]:
    """The zone sizes a mouse or a headset event is bound for."""
    if kind == "headset":
        return (2,)
    return tuple(n for n in MOUSE_ZONES if not (n == 2 and "headset" in types))


def frame_key(zones: int, i: int) -> str:
    return f"zone{zones}-{ZONE_WORDS[i]}"


def key_pixel(row: int, col: int) -> int:
    """Which pixel of the keyboard light is the key at this row and column: left to right, then top down."""
    return col * ROWS + row


def bitmap(colors: list[RGB]) -> list[list[int]]:
    """The keyboard light's pixels as GameSense's 132 entries, row after row."""
    return [list(colors[key_pixel(r, c)]) for r in range(ROWS) for c in range(COLUMNS)]


def spread(colors: list[RGB], zones: int) -> list[RGB]:
    """A light's few pixels over `zones` zones: each zone takes the average of the pixels it covers."""
    n = len(colors)
    out = []
    for j in range(zones):
        lo = j * n // zones
        part = colors[lo:max(lo + 1, (j + 1) * n // zones)]
        out.append(tuple(round(sum(c[k] for c in part) / len(part)) for k in range(3)))
    return out


def zone_frame(colors: list[RGB], zones: int) -> dict:
    return {frame_key(zones, i): {"red": c[0], "green": c[1], "blue": c[2]}
            for i, c in enumerate(spread(colors, zones))}


def bindings(types: list[str]) -> dict[str, dict]:
    """event -> the body of its bind_game_event, for the ticked types."""
    out = {}
    for t in types:
        info = TYPES[t]
        if t == "keyboard":
            handlers = [{"device-type": "rgb-per-key-zones", "mode": "bitmap"}]
        else:
            handlers = [{"device-type": f"rgb-{n}-zone", "zone": ZONE_WORDS[i], "mode": "context-color",
                         "context-frame-key": frame_key(n, i)}
                        for n in zone_counts(t, types) for i in range(n)]
        out[info["event"]] = {"game": GAME, "event": info["event"], "icon_id": 0, "value_optional": True,
                              "handlers": handlers}
    return out


class GameSenseError(Exception):
    """A call to GG failed; the message is what to tell the person, in plain words."""

    def __init__(self, message: str, gone: bool = False):
        super().__init__(message)
        self.gone = gone          # True: GG isn't running (so there's nothing to fix but starting it)


class GameSenseRestarted(GameSenseError):
    """GG is on another port than a moment ago: it was restarted, and the game has to be registered again."""


class GameSense:
    """The GameSense REST calls over one kept-open connection. Not for several threads at once."""

    def __init__(self, props_path=None, timeout: float = TIMEOUT):
        self.props_path = Path(props_path) if props_path else PROPS_PATH
        self.timeout = timeout
        self._address: tuple[str, int] | None = None
        self._conn: http.client.HTTPConnection | None = None

    def read_address(self) -> tuple[str, int]:
        """Where GG listens now, from coreProps.json (the port is new every time GG starts)."""
        try:
            address = json.loads(self.props_path.read_text(encoding="utf-8")).get("address")
            host, _, port = str(address).rpartition(":")
            port = int(port)
        except (OSError, ValueError, AttributeError):
            raise GameSenseError(NOT_RUNNING, gone=True) from None
        if host not in LOCAL or not 0 < port < 65536:
            raise GameSenseError("SteelSeries GG gave an address that isn't on this PC")
        return host, port

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None
        self._address = None

    def _connect(self) -> None:
        """Open a connection to where coreProps.json says GG listens. A local port that doesn't take the
        connection (refused, or Windows taking its time to refuse) just means GG isn't running."""
        host, port = self._address = self.read_address()
        conn = http.client.HTTPConnection(host, port, timeout=min(self.timeout, CONNECT_TIMEOUT))
        try:
            conn.connect()
        except OSError:
            conn.close()
            self._address = None
            raise GameSenseError(NOT_RUNNING, gone=True) from None
        conn.sock.settimeout(self.timeout)
        self._conn = conn

    def post(self, path: str, body: dict, retry: bool = True) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        before = self._address
        for attempt in range(2):
            reused = self._conn is not None
            if not reused:
                self._connect()
            try:
                self._conn.request("POST", path, payload, {"Content-Type": "application/json"})
                resp = self._conn.getresponse()
                text = resp.read()
            except (OSError, http.client.HTTPException) as e:
                # A kept-open connection can go stale, and GG may have restarted on another port:
                # forget it and look at coreProps.json again before giving up.
                self.close()
                if reused and retry and attempt == 0:
                    continue
                if isinstance(e, TimeoutError):
                    raise GameSenseError(NOT_ANSWERING) from None
                if isinstance(e, ConnectionRefusedError):
                    raise GameSenseError(NOT_RUNNING, gone=True) from None
                raise GameSenseError("Lost the connection to SteelSeries GG") from None
            break
        if before is not None and self._address != before:
            raise GameSenseRestarted("SteelSeries GG started again")       # (it has forgotten us: register anew)
        if resp.status != 200:
            raise GameSenseError(f"SteelSeries GG said no ({resp.status}): {self._why(text)}")

    @staticmethod
    def _why(text: bytes) -> str:
        try:
            said = json.loads(text).get("error")
        except (ValueError, AttributeError):
            said = None
        return str(said or text.decode("utf-8", "replace") or "no reason given").strip()[:100]


class SteelSeriesDevice(Device):
    kind = "steelseries"
    group = "pc"
    fades = True                  # local and quick: a new look glides in (manager.DeviceWorker)
    hardware_brightness = False
    max_fps = 25                  # frames go out on the provider's own thread, only when they changed

    def __init__(self, provider: "SteelSeriesProvider", type_id: str):
        info = TYPES[type_id]
        super().__init__(f"steelseries:{type_id}", info["name"])
        self.provider, self.type_id = provider, type_id
        self.reported_name = info["name"]
        self.pixel_count = info["pixels"]
        if type_id == "keyboard":
            # Where each pixel sits on the board (0..1 both ways), for the light show's ripples.
            geometry = [(0.0, 0.0)] * KEYS
            for r in range(ROWS):
                for c in range(COLUMNS):
                    geometry[key_pixel(r, c)] = ((c + 0.5) / COLUMNS, (r + 0.5) / ROWS)
            self.fixture = {"geometry": geometry}

    def send_frame(self, colors: list[RGB]) -> None:
        if len(colors) == self.pixel_count and self.online:
            self.provider.queue(self.type_id, colors)

    def wait_written(self, timeout: float) -> bool:
        """Wait until the newest frame has gone out (used at shutdown)."""
        return self.provider.wait_idle(timeout)

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self.send_frame([scale(rgb, state.brightness / 100)] * self.pixel_count)

    def power_off(self) -> None:
        self.send_frame([(0, 0, 0)] * self.pixel_count)

    def desk_item(self) -> str:
        return TYPES[self.type_id]["desk"]

    def extra_info(self) -> dict:
        label = {"keyboard": f"per key · {KEYS} keys", "mouse": f"{self.pixel_count} zones", "headset": "2 zones"}
        return {"label": f"SteelSeries GG · {label[self.type_id]}", "deviceType": TYPES[self.type_id]["desk"]}


class SteelSeriesProvider:
    def __init__(self, settings: dict, on_change, save=None, props_path=None,
                 heartbeat_every: float = HEARTBEAT_EVERY, retry: float = RETRY):
        self.settings = settings              # live reference to config["steelseries"]
        self.on_change = on_change            # called with no args when the lights or their state change
        self._save = save or (lambda: None)
        self.client = GameSense(props_path)
        self.heartbeat_every, self.retry = heartbeat_every, retry
        self.devices: dict[str, SteelSeriesDevice] = {}     # the lights offered now (switched on, type ticked)
        self._made: dict[str, SteelSeriesDevice] = {}       # every light made this session (reused: the manager keeps their workers)
        self.status = "off"                   # off, idle (nothing ticked), waiting (GG isn't there), error, connected
        self.message = ""
        self._lock = threading.Lock()
        self._pending: dict[str, list[RGB]] = {}    # type -> the newest frame not sent yet
        self._busy = False
        self._sent: dict[str, dict] = {}            # event -> the data last sent (only changes go out)
        self._bound: dict[str, dict] = {}           # event -> its binding, while registered
        self._bound_types: tuple = ()               # the types those bindings were made for
        self._registered = False
        self._last_call = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- the settings --------------------------------------------------------------------------

    def wanted(self) -> list[str]:
        return clean_types(self.settings.get("types", DEFAULT_TYPES))

    def set_options(self, on, types) -> None:
        """The switch, and which device types to light."""
        if not isinstance(types, (list, tuple)) or any(t not in TYPES for t in types):
            raise ValueError("Choose keyboard, mouse or headset")
        self.settings.update(on=bool(on), types=clean_types(types))
        self._save()
        self._wake.set()
        self.on_change()

    # ---- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="steelseries", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Give the devices back to GG and stop (the thread does it, so nothing talks at the same time)."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(4.0)
        else:
            self._release(quick=True)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.clear()
                wait = self._step()
                self._wake.wait(wait)
        finally:
            self._release(quick=True)

    def _step(self) -> float | None:
        """One round of work; returns how long to sleep (None: until something changes)."""
        if not self.settings.get("on"):
            self._release()
            return None
        types = self.wanted()
        self._offer(types)
        try:
            if not types:
                self._release(quiet=True)
                self._state("idle", "Pick the devices to light")
                return None
            if not self._registered or tuple(types) != self._bound_types:
                self._register(types)
            self._send_pending(types)
            due = self._last_call + self.heartbeat_every - time.monotonic()
            if due <= 0:
                self._post("/game_heartbeat", {"game": GAME})
                due = self.heartbeat_every
            return max(0.02, due)
        except GameSenseRestarted:
            self._registered, self._bound, self._bound_types, self._sent = False, {}, (), {}
            return 0.0
        except GameSenseError as e:
            self._fail(e)
        except Exception:
            log.exception("SteelSeries GG failed")
            self._fail(GameSenseError(NOT_ANSWERING))
        return self.retry

    # ---- talking to GG ---------------------------------------------------------------------------

    def _post(self, path: str, body: dict, retry: bool = True) -> None:
        self.client.post(path, body, retry)
        self._last_call = time.monotonic()

    def _register(self, types: list[str]) -> None:
        if not self._registered:
            self._post("/game_metadata", {"game": GAME, "game_display_name": "Glowstage", "developer": "Glowstage",
                                          "deinitialize_timer_length_ms": DEINITIALIZE_MS})
            self._registered = True
            self._bound = {}
            self._sent.clear()
        want = bindings(types)
        for event in [e for e in self._bound if e not in want]:     # a type that was unticked
            self._post("/remove_game_event", {"game": GAME, "event": event})
            del self._bound[event]
            self._sent.pop(event, None)
        for event, body in want.items():
            if self._bound.get(event) != body:
                self._post("/bind_game_event", body)
                self._bound[event] = body
                self._sent.pop(event, None)
        self._bound_types = tuple(types)
        self._state("connected", "")

    def _event(self, type_id: str, colors: list[RGB], types: list[str]) -> tuple[str, dict]:
        event = TYPES[type_id]["event"]
        if type_id == "keyboard":
            return event, {"frame": {"bitmap": bitmap(colors)}}
        frame: dict = {}
        for n in zone_counts(type_id, types):
            frame.update(zone_frame(colors, n))
        return event, {"frame": frame}

    def _send_pending(self, types: list[str]) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._busy = bool(pending)
        try:
            for type_id, colors in list(pending.items()):
                if type_id not in types:
                    del pending[type_id]
                    continue
                event, data = self._event(type_id, colors, types)
                if self._sent.get(event) != data:
                    self._post("/game_event", {"game": GAME, "event": event, "data": data})
                    self._sent[event] = data
                del pending[type_id]
        finally:
            with self._lock:
                for type_id, colors in pending.items():      # (a call failed: what didn't go out waits for the next try)
                    self._pending.setdefault(type_id, colors)
                self._busy = False

    def queue(self, type_id: str, colors: list[RGB]) -> None:
        """A light has a new frame; the thread sends the newest one."""
        with self._lock:
            self._pending[type_id] = list(colors)
        self._wake.set()

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending and not self._busy:
                    return True
            time.sleep(0.01)
        return False

    # ---- what the lights and the window see ----------------------------------------------------------

    def _offer(self, types: list[str]) -> None:
        """The lights for the ticked types exist while this is on, answering or not."""
        want = {}
        for t in types:
            dev = self._made.get(t) or self._made.setdefault(t, SteelSeriesDevice(self, t))
            want[dev.id] = dev
        if list(want) != list(self.devices):
            self.devices = want
            self.on_change()

    def _fail(self, e: GameSenseError) -> None:
        """A call failed (or GG went away): the lights say why, and everything is registered anew
        at the next try, because GG may have restarted and forgotten us."""
        self._registered = False
        self._bound, self._bound_types, self._sent = {}, (), {}
        with self._lock:
            self._pending.clear()
        self.client.close()
        for dev in self.devices.values():
            dev.online, dev.streaming, dev.error = False, False, str(e)
        if self.status != ("waiting" if e.gone else "error") or self.message != str(e):
            log.warning("SteelSeries GG: %s", e)
        self._state("waiting" if e.gone else "error", str(e))

    def _state(self, status: str, message: str) -> None:
        changed = (status, message) != (self.status, self.message)
        self.status, self.message = status, message
        if status == "connected":
            for dev in self.devices.values():
                if not dev.online or dev.error:
                    dev.online, dev.error, changed = True, None, True
        if changed:
            self.on_change()

    def _release(self, quick: bool = False, quiet: bool = False) -> None:
        """Back to GG's own lighting: stop and remove our game. Off means no lights here either."""
        if self._registered:
            for path in ("/stop_game", "/remove_game"):
                try:
                    self._post(path, {"game": GAME}, retry=not quick)
                except GameSenseError as e:
                    log.info("SteelSeries GG: giving the lighting back failed (%s)", e)
                    break
        self._registered = False
        self._bound, self._bound_types, self._sent = {}, (), {}
        with self._lock:
            self._pending.clear()
        self.client.close()
        if quiet:
            return
        for dev in self._made.values():
            dev.online, dev.streaming, dev.error = False, False, None
        had = bool(self.devices)
        self.devices = {}
        self._state("off", "")
        if had:
            self.on_change()

    def describe(self) -> dict:
        on = bool(self.settings.get("on"))
        text = self.message
        if on and self.status == "connected" and not text:
            n = len(self.devices)
            text = f"Connected to SteelSeries GG · {n} light{'' if n == 1 else 's'}"
        return {"on": on, "types": self.wanted(), "status": self.status if on else "off", "message": text if on else "",
                "available": [{"id": t, "title": info["title"]} for t, info in TYPES.items()]}

    def summary(self) -> str:
        """For the problem report."""
        if not self.settings.get("on"):
            return "off"
        return "connected" if self.status == "connected" else (self.message or self.status)
