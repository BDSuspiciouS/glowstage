"""Razer keyboards, mice, headsets and mouse mats through Razer Synapse (beta).

OpenRGB can't reach a Razer device while Synapse runs, because Synapse holds it. Razer's own Chroma
SDK is the way in that Synapse offers: it serves a small REST API on this PC (Chroma Connect, the
module that comes with Synapse) and apps set effects per device type. Written from Razer's
published documentation, not yet tried on a real Synapse (hence beta):

  POST   http://localhost:54235/razer/chromasdk   register: {"title", "description", "author":
         {"name", "contact"}, "device_supported": [...], "category": "application"}
         -> {"sessionid": n, "uri": "http://localhost:<port>/chromasdk"}; that uri is the session.
  PUT    <uri>/heartbeat                          at least every 15 s, or the session ends
  PUT    <uri>/<type>                             {"effect": ..., "param": ...} -> {"result": 0}
  DELETE <uri>                                    ends the session: Synapse gets its own effects back

Types are keyboard, mouse, headset, mousepad, keypad and chromalink. Effects used here:
  CHROMA_STATIC   {"color": BGR}                  one color (a plain color, brightness included)
  CHROMA_CUSTOM   a grid of BGR colors            keyboard 6 rows x 22, keypad 4 x 5, mouse mat 15,
                                                  headset 5, Chroma Link 5 (a flat list for those)
  CHROMA_CUSTOM2  9 rows x 7 of BGR colors        the mouse
  CHROMA_NONE     no param                        off
Colors are BGR, 0x00BBGGRR, not RGB. A result that isn't 0 means Synapse refuses it (for example 1167:
no device of that type is connected). The SDK can't say which types are connected, so the person ticks
the ones they have. Synapse not running shows as a refused connection on 54235, which is instant.

Only this PC is ever talked to: the session address Synapse hands back is used only when it is
localhost, and the app never sends anything else than lighting. Taking a device means Synapse's own
effects pause while Glowstage runs, which is why this is off until it is switched on.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from urllib.parse import urlsplit

from ..colors import RGB, scale
from ..paths import APP_NAME, REPO
from ..state import LightState
from .base import Device

log = logging.getLogger(__name__)

DEFAULT_BASE = "http://localhost:54235/razer/chromasdk"
HEARTBEAT = 3.0               # seconds between heartbeats (Synapse ends the session after 15)
RETRY = 5.0                   # seconds between tries while Synapse isn't there (a refused connection is instant)
REFUSED_RETRY = 30.0          # a type Synapse refused is tried again after this long (it may have been plugged in)
TIMEOUT = 2.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# (id, name, rows, columns, effect, what it is on the desk). Rows is 1 for the single-row ones.
TYPES = (
    ("keyboard", "Razer keyboard", 6, 22, "CHROMA_CUSTOM", "KEYBOARD"),
    ("mouse", "Razer mouse", 9, 7, "CHROMA_CUSTOM2", "MOUSE"),
    ("headset", "Razer headset", 1, 5, "CHROMA_CUSTOM", "HEADSET"),
    ("mousepad", "Razer mouse mat", 1, 15, "CHROMA_CUSTOM", "MOUSEMAT"),
    ("keypad", "Razer keypad", 4, 5, "CHROMA_CUSTOM", "KEYPAD"),
    ("chromalink", "Razer Chroma Link", 1, 5, "CHROMA_CUSTOM", ""),
)
TYPE_IDS = tuple(t[0] for t in TYPES)
DEFAULT_TYPES = ["keyboard", "mouse", "mousepad"]

STATUS_TEXT = {"off": "", "waiting": "Razer Synapse isn't running, or Chroma Connect isn't installed in it",
               "connected": "Connected to Razer Synapse"}


class ChromaError(OSError):
    """Synapse (or whatever sits on the port) didn't answer the way the Chroma SDK does."""


def bgr(rgb: RGB) -> int:
    """A color the way the Chroma SDK wants it: 0x00BBGGRR."""
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return (b << 16) | (g << 8) | r


class RazerLight(Device):
    """One Razer device type as a light. Frames go to a writer thread of its own, so the effect
    engine never waits on an HTTP call, and only the newest one is sent (and only when it differs
    from the last one Synapse took)."""

    kind = "razer"
    group = "pc"
    fades = True                 # local and quick: a new look glides in (manager.DeviceWorker)
    hardware_brightness = False
    max_fps = 30                 # what the SDK takes per device

    def __init__(self, provider: "RazerChroma", spec: tuple):
        type_id, name, self.rows, self.cols, self.effect, self._desk = spec
        super().__init__(f"razer:{type_id}", name)
        self.type_id = type_id
        self.reported_name = name
        self.provider = provider
        self.pixel_count = self.rows * self.cols
        if self.rows > 1:
            # Pixels run left to right, column by column, so an effect sweeps across the keys;
            # where each one sits (0..1 both ways) is for the light show's ripples.
            self.fixture = {"geometry": [(c / (self.cols - 1), r / (self.rows - 1))
                                         for c in range(self.cols) for r in range(self.rows)]}
        self.refused_at = 0.0
        self._last: dict | None = None
        self._pending: dict | None = None
        self._busy = False
        self._closed = False
        self._cv = threading.Condition()
        threading.Thread(target=self._writer, name=f"writer:{self.id}", daemon=True).start()

    # ---- what is sent -----------------------------------------------------------------------

    def grid(self, colors: list[RGB]) -> list:
        """A frame as the SDK's grid: rows of BGR ints, or one flat list for the single-row types."""
        if self.rows == 1:
            return [bgr(c) for c in colors]
        return [[bgr(colors[c * self.rows + r]) for c in range(self.cols)] for r in range(self.rows)]

    def _submit(self, body: dict) -> None:
        with self._cv:
            self._pending = body
            self._cv.notify()

    def send_frame(self, colors: list[RGB]) -> None:
        if len(colors) == self.pixel_count:
            self._submit({"effect": self.effect, "param": self.grid(colors)})

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self._submit({"effect": "CHROMA_STATIC", "param": {"color": bgr(scale(rgb, state.brightness / 100))}})

    def power_off(self) -> None:
        self._submit({"effect": "CHROMA_NONE"})

    def connect(self) -> None:
        self.provider.require_session()
        self._last = None
        self.online, self.error = True, None

    def desk_item(self) -> str:
        return self._desk

    def extra_info(self) -> dict:
        return {"label": f"Razer Chroma · {self.pixel_count} zones"}

    # ---- the writer -------------------------------------------------------------------------

    def wait_written(self, timeout: float) -> bool:
        """Wait until the newest frame has gone out (used at shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cv:
                if self._pending is None and not self._busy:
                    return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._pending = None
            self._cv.notify()

    def refuse(self, code) -> None:
        self.refused_at = time.monotonic()
        self.online = self.streaming = False
        self.error = ("Synapse doesn't see one connected" if code == 1167
                      else f"Synapse refused it (code {code})")
        log.warning("%s: %s", self.name, self.error)

    def _writer(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                body, self._pending, self._busy = self._pending, None, True
            try:
                if self.online and body != self._last:
                    answer = self.provider.call("PUT", "/" + self.type_id, body)
                    code = answer.get("result", -1) if isinstance(answer, dict) else -1
                    if code == 0:
                        self._last = body
                    else:
                        self.refuse(code)
                        self.provider.changed()
            except ChromaError as e:
                log.warning("%s: %s", self.name, e)
                self.provider.lost()
            finally:
                with self._cv:
                    self._busy = False


class RazerChroma:
    """The session with Synapse and the lights it offers, while the switch in Setup is on.
    Owned by the device manager next to OpenRGB's provider.

    `base` is the registration address (tests point it at a fake on 127.0.0.1)."""

    def __init__(self, settings: dict, on_change=None, save=None, base: str = DEFAULT_BASE,
                 heartbeat: float = HEARTBEAT, retry: float = RETRY, refused_retry: float = REFUSED_RETRY):
        self.settings = settings            # live reference to config["razer"]
        self.on_change = on_change or (lambda: None)
        self.save = save or (lambda: None)
        self.base = base
        self._heartbeat, self._retry, self._refused_retry = heartbeat, retry, refused_retry
        self.devices: dict[str, RazerLight] = {}          # the lights offered now, in the order of TYPES
        self._made: dict[str, RazerLight] = {t[0]: RazerLight(self, t) for t in TYPES}
        self._lock = threading.RLock()                    # the session: made, used and ended one at a time
        self._uri: tuple[str, int, str] | None = None     # (host, port, path) of the session
        self._status = "off"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- the settings -----------------------------------------------------------------------

    def types(self) -> list[str]:
        picked = self.settings.get("types")
        picked = DEFAULT_TYPES if not isinstance(picked, list) else picked
        return [t for t in TYPE_IDS if t in picked]

    def set_options(self, on, types) -> None:
        """The switch, and which device types you have (the SDK can't tell which are connected)."""
        types = list(types) if isinstance(types, (list, tuple)) else []
        unknown = [t for t in types if t not in TYPE_IDS]
        if unknown:
            raise ValueError(f"'{unknown[0]}' isn't a Razer device type")
        if on and not types:
            raise ValueError("Tick at least one kind of Razer device")
        self.settings["on"] = bool(on)
        self.settings["types"] = [t for t in TYPE_IDS if t in types]
        self.save()
        self._wake.set()

    def describe(self) -> dict:
        with self._lock:
            status = self._status
        lights = list(self.devices.values())
        return {"on": bool(self.settings.get("on")), "types": self.types(), "status": status,
                "text": STATUS_TEXT.get(status, ""), "lights": sum(1 for d in lights if d.online),
                "kinds": [{"id": t[0], "name": t[1]} for t in TYPES]}

    def summary(self) -> str:
        """One line for the problem report."""
        if not self.settings.get("on"):
            return "off"
        if self._status != "connected":
            return "Synapse not reachable"
        lights = list(self.devices.values())
        return f"connected ({sum(1 for d in lights if d.online)} of {len(lights)} types answering)"

    # ---- talking to Synapse -----------------------------------------------------------------

    def call(self, method: str, path: str = "", body: dict | None = None) -> dict:
        """One request on the session. ChromaError when there is no session or Synapse doesn't answer."""
        uri = self._uri
        if uri is None:
            raise ChromaError("No session with Razer Synapse")
        return self._request(uri[0], uri[1], uri[2] + path, method, body)

    @staticmethod
    def _request(host: str, port: int, path: str, method: str, body: dict | None) -> dict:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, raw, {"Content-Type": "application/json"} if raw else {})
            resp = conn.getresponse()
            data = resp.read(1 << 16)
            if not 200 <= resp.status < 300:
                raise ChromaError(f"Synapse answered {resp.status}")
            return json.loads(data) if data.strip() else {}
        except (OSError, http.client.HTTPException, ValueError) as e:
            if isinstance(e, ChromaError):
                raise
            raise ChromaError(str(e) or type(e).__name__) from e
        finally:
            conn.close()

    def require_session(self) -> None:
        if self._uri is None:
            raise ChromaError("Razer Synapse isn't running")

    def _register(self) -> None:
        parts = urlsplit(self.base)
        hello = {"title": APP_NAME, "description": "Lights for the whole room: PC, strips and smart lights",
                 "author": {"name": APP_NAME, "contact": f"https://github.com/{REPO}"},
                 "device_supported": list(TYPE_IDS), "category": "application"}
        answer = self._request(parts.hostname, parts.port or 80, parts.path, "POST", hello)
        got = urlsplit(str(answer.get("uri") or ""))
        if got.hostname not in LOCAL_HOSTS or not got.port:
            raise ChromaError("Synapse gave an address that isn't on this PC")   # (nothing else is ever talked to)
        self._uri = (got.hostname, got.port, got.path.rstrip("/"))

    def _end_session(self) -> None:
        with self._lock:
            uri, self._uri = self._uri, None
            if uri is not None:
                try:
                    self._request(uri[0], uri[1], uri[2], "DELETE", None)    # Synapse gets its lighting back
                except ChromaError as e:
                    log.info("Razer: ending the session: %s", e)

    # ---- the lights -------------------------------------------------------------------------

    def _sync(self) -> bool:
        """Offer the ticked types while the switch is on, none when off. True when that changed."""
        wanted = self.types() if self.settings.get("on") else []
        now = {t: self._made[t] for t in TYPE_IDS if t in wanted}
        for gone in (d for t, d in self._made.items() if t not in now and d.id in self.devices):
            gone.online = gone.streaming = False         # (it stops writing: there is no session for it)
            gone.refused_at = 0.0
            gone._pending = None
        if list(now) == [d.type_id for d in self.devices.values()]:
            return False
        self.devices = {d.id: d for d in now.values()}
        return True

    def _set(self, status: str, error: str | None = None, online: bool = False) -> None:
        """The status, and every offered light answering or not to match."""
        before = (self._status, [(d.online, d.error) for d in self.devices.values()])
        self._status = status
        for d in self.devices.values():
            if online and d.refused_at and time.monotonic() - d.refused_at < self._refused_retry:
                continue                      # (refused earlier: it waits its turn, see _retry_refused)
            if online and not d.online:
                d._last = None
            if not online:
                d.streaming, d.refused_at = False, 0.0
            d.online, d.error = online, None if online else error
        if before != (self._status, [(d.online, d.error) for d in self.devices.values()]):
            if before[0] != status:
                log.info("Razer: %s", self.summary())
            self.on_change()

    def _retry_refused(self) -> None:
        again = [d for d in self.devices.values()
                 if not d.online and d.refused_at and time.monotonic() - d.refused_at >= self._refused_retry]
        for d in again:
            d.refused_at, d._last = 0.0, None
            d.online, d.error = True, None
        if again:
            self.on_change()           # (the manager gives them their look again, and a refusal shows again)

    def changed(self) -> None:
        self.on_change()

    def lost(self) -> None:
        """A write found Synapse gone: look again right away."""
        self._uri = None
        self._wake.set()

    # ---- the thread -------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="razer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Quitting: end the session so that Synapse gets its own effects back."""
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(3)
        self._end_session()
        for light in self._made.values():
            light.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            on = bool(self.settings.get("on"))
            if not on:
                self._end_session()
                changed = self._sync()
                self._set("off")
                if changed:
                    self.on_change()
                self._wake.wait()                 # nothing to do until it's switched on
                continue
            changed = self._sync()
            wait = self._heartbeat
            with self._lock:
                if self._stop.is_set():
                    break
                try:
                    if self._uri is None:
                        self._register()
                        self._set("connected", online=True)
                    else:
                        self.call("PUT", "/heartbeat")
                        self._retry_refused()
                except ChromaError as e:
                    self._uri = None
                    log.info("Razer: %s", e)
                    self._set("waiting", "Razer Synapse isn't running")
                    wait = self._retry
                else:
                    if changed:
                        self._set("connected", online=True)
            if changed:
                self.on_change()
            self._wake.wait(wait)
