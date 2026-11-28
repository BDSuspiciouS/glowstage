"""Lights controlled by Home Assistant, through its REST API.

One Home Assistant entry is a Hub: connect() reads /api/states and makes one HALight per
`light.*` entity that can show a color or a brightness. Add it with the address of Home
Assistant and a long-lived access token (profile -> Security). Home Assistant is a gateway to
lights of any brand, which often can't take 30 service calls per second, so streaming is
throttled: the hub owns one sender thread that keeps each light's latest color and makes at most
~10 service calls per second in total, round-robin over the lights whose color changed.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from urllib.parse import urlsplit

from ..colors import RGB, rgb_to_hex
from ..state import LightState
from . import registry
from .base import Device
from .hub import Hub, NotPairedError
from .net import HttpError

PAIRING = ("Create a long-lived access token in Home Assistant (your profile → Security → "
           "Long-lived access tokens) and add it here.")
BAD_TOKEN = "Home Assistant didn't accept the token"

COLOR_MODES = {"rgb", "rgbw", "rgbww", "hs", "xy"}
BRIGHTNESS_MODES = {"brightness", "color_temp", "white"}
MAX_CALLS_PER_SECOND = 10          # all streamed service calls of one Home Assistant together
UNAVAILABLE = "Unavailable in Home Assistant"


def _norm_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def _split(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path prefix) of a Home Assistant address."""
    parts = urlsplit(_norm_url(url))
    scheme = parts.scheme or "http"
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, parts.hostname or "", port or (443 if scheme == "https" else 8123), parts.path.rstrip("/")


class _Api:
    """Bearer-authenticated JSON calls to one Home Assistant."""

    def __init__(self, url: str, token: str):
        self.scheme, self.host, self.port, self.prefix = _split(url)
        self.token = token or ""

    def connection(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout)

    def send(self, conn: http.client.HTTPConnection, method: str, path: str, payload=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, self.prefix + path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise HttpError(resp.status, resp.reason or "")
        try:
            return json.loads(data) if data else None
        except ValueError:
            return None

    def call(self, method: str, path: str, payload=None, timeout: float = 3.0):
        """One request on its own connection."""
        conn = self.connection(timeout)
        try:
            return self.send(conn, method, path, payload)
        finally:
            conn.close()


def _full_value(rgb: RGB) -> tuple[RGB, float]:
    """A color as (the same hue at full value, its value 0..1): Home Assistant gets the
    brightness separately, so the color must not be dimmed twice."""
    top = max(rgb)
    if top <= 0:
        return (0, 0, 0), 0.0
    k = 255 / top
    return (min(255, int(rgb[0] * k + 0.5)), min(255, int(rgb[1] * k + 0.5)),
            min(255, int(rgb[2] * k + 0.5))), top / 255


def _brightness(fraction: float) -> int:
    """0..1 -> 1..255 (0 would switch the light off)."""
    return max(1, min(255, int(fraction * 255 + 0.5)))


class HALight(Device):
    kind = "homeassistant"
    group = "room"
    max_fps = 10

    def __init__(self, dev_id: str, name: str, hub: "HAHub", entity_id: str, color: bool = True):
        super().__init__(dev_id, name)
        self.hub = hub
        self.entity_id = entity_id
        self.color = color
        self.pixel_count = 1
        self.host = hub.host
        self._last_state: dict = {}

    def _turn_on(self, rgb: RGB, brightness_pct: float) -> dict:
        full, value = _full_value(rgb)
        body: dict = {"entity_id": self.entity_id}
        if self.color:
            body["rgb_color"] = list(full)
        body["brightness"] = _brightness(brightness_pct / 100 * value)
        body["transition"] = round(self.moment, 2)
        return body

    def connect(self) -> None:
        if not self.hub.token:
            raise NotPairedError(PAIRING)
        try:
            st = self.hub.api.call("GET", f"/api/states/{self.entity_id}", timeout=4.0)
        except HttpError as e:
            if e.status == 401:
                raise NotPairedError(BAD_TOKEN) from None
            if e.status == 404:
                self.online, self.error = False, "Not found in Home Assistant"
                raise RuntimeError(self.error) from None
            raise
        self.update_from_state(st or {})
        if not self.online:
            raise RuntimeError(self.error)

    def update_from_state(self, st: dict) -> None:
        attrs = st.get("attributes") or {}
        self._last_state = st
        self.reported_name = attrs.get("friendly_name") or self.entity_id
        self.pixel_count = 1
        if st.get("state") == "unavailable":
            self.online, self.error = False, UNAVAILABLE
        else:
            self.online, self.error = True, None

    def read_state(self) -> dict | None:
        st = self._last_state
        if not st or st.get("state") not in ("on", "off"):
            return None
        attrs = st.get("attributes") or {}
        out: dict = {"on": st["state"] == "on"}
        if attrs.get("brightness") is not None:
            out["brightness"] = int(attrs["brightness"] / 255 * 100 + 0.5)
        rgb = attrs.get("rgb_color")
        if self.color and isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
            out["color"] = rgb_to_hex(_full_value((int(rgb[0]), int(rgb[1]), int(rgb[2])))[0])
        return out

    def _service(self, service: str, body: dict, timeout: float = 3.0) -> None:
        self.hub.api.call("POST", f"/api/services/light/{service}", body, timeout=timeout)

    def show_color(self, rgb: RGB, state: LightState) -> None:
        self.hub.forget(self.entity_id)
        self._service("turn_on", self._turn_on(rgb, state.brightness))

    def power_off(self, timeout: float = 3.0) -> None:
        self.hub.forget(self.entity_id)
        self._service("turn_off", {"entity_id": self.entity_id, "transition": round(self.moment, 2)}, timeout)

    def send_frame(self, colors: list[RGB]) -> None:
        if not colors:
            return
        n = len(colors)
        avg = (sum(c[0] for c in colors) // n, sum(c[1] for c in colors) // n,
               sum(c[2] for c in colors) // n)
        # The engine already applied the brightness to the frame, so only the color's value counts.
        self.hub.set_target(self.entity_id, self._turn_on(avg, 100))

    def end_stream(self) -> None:
        self.hub.forget(self.entity_id)

    def extra_info(self) -> dict:
        return {"host": self.hub.host, "entity_id": self.entity_id, "color": self.color,
                "label": f"Home Assistant · {self.entity_id}"}


class HAHub(Hub):
    kind = "homeassistant"

    def __init__(self, hub_id: str, entry: dict):
        super().__init__(hub_id, entry)
        self.name = entry.get("label") or "Home Assistant"
        self.token = entry.get("token") or ""
        url = entry.get("url") or f"http://{entry.get('host', '')}:{entry.get('port') or 8123}"
        self.api = _Api(url, self.token)
        self.host = self.api.host or self.host
        self.port = self.api.port
        self._cond = threading.Condition()
        self._pending: dict[str, dict] = {}      # entity_id -> latest service body to send
        self._queue: list[str] = []               # entity ids with a pending body, in turn order
        self._last_sent: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def paired(self) -> bool:
        return bool(self.token)

    # ---- hub contract ----------------------------------------------------------------

    def connect(self) -> None:
        if not self.token:
            self.online, self.error = False, "Add an access token"
            raise NotPairedError(PAIRING)
        try:
            states = self.api.call("GET", "/api/states", timeout=6.0)
        except HttpError as e:
            self.online = False
            if e.status == 401:
                self.error = BAD_TOKEN
                raise NotPairedError(BAD_TOKEN) from None
            self.error = f"Home Assistant answered {e}"
            raise
        except Exception as e:
            self.online, self.error = False, f"Can't reach Home Assistant ({e})"
            raise
        found: dict[str, Device] = {}
        for st in states if isinstance(states, list) else []:
            if not isinstance(st, dict):
                continue
            entity_id = str(st.get("entity_id", ""))
            attrs = st.get("attributes") or {}
            if not entity_id.startswith("light."):
                continue
            modes = set(attrs.get("supported_color_modes") or [])
            color = bool(modes & COLOR_MODES)
            if not color and not modes & BRIGHTNESS_MODES:
                continue
            dev_id = f"{self.id}:{entity_id}"
            light = self.devices.get(dev_id)
            if not isinstance(light, HALight):
                light = HALight(dev_id, attrs.get("friendly_name") or entity_id, self, entity_id, color)
            light.color = color
            light.update_from_state(st)
            found[dev_id] = light
        self.devices = found
        self.online, self.error = True, None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._pending.clear()
            self._queue.clear()
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ---- streaming ---------------------------------------------------------------------

    def set_target(self, entity_id: str, body: dict) -> None:
        """Remember the latest color for a light; the sender thread delivers it."""
        with self._cond:
            if self._closed:
                return
            if entity_id not in self._pending:
                self._queue.append(entity_id)
            self._pending[entity_id] = body
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=f"ha-sender:{self.id}", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def forget(self, entity_id: str) -> None:
        """Drop a light's waiting color (it is being set directly, or the stream ended)."""
        with self._cond:
            self._pending.pop(entity_id, None)
            if entity_id in self._queue:
                self._queue.remove(entity_id)
            self._last_sent.pop(entity_id, None)

    def _next(self) -> tuple[str, dict] | None:
        """The next light whose color changed, in turn order; blocks until there is one."""
        with self._cond:
            while True:
                if self._closed:
                    return None
                while self._queue:
                    entity_id = self._queue.pop(0)
                    body = self._pending.pop(entity_id, None)
                    if body is not None and body != self._last_sent.get(entity_id):
                        return entity_id, body
                self._cond.wait()

    def _run(self) -> None:
        interval = 1.0 / MAX_CALLS_PER_SECOND
        conn: http.client.HTTPConnection | None = None
        last = 0.0
        while True:
            with self._cond:   # sleeps until the next call is allowed, but wakes at once on close()
                while not self._closed:
                    wait = last + interval - time.monotonic()
                    if wait <= 0:
                        break
                    self._cond.wait(wait)   # new frames also wake it: just look at the clock again
                if self._closed:
                    break
            item = self._next()
            if item is None:
                break
            entity_id, body = item
            path = "/api/services/light/turn_on"
            ok = False
            for _ in range(2):     # the second try is on a fresh connection
                try:
                    if conn is None:
                        conn = self.api.connection(3.0)
                    self.api.send(conn, "POST", path, body)
                    ok = True
                    break
                except Exception:
                    if conn is not None:
                        conn.close()
                    conn = None
            last = time.monotonic()
            with self._cond:
                if ok and not self._closed and entity_id not in self._pending:
                    self._last_sent[entity_id] = body
        if conn is not None:
            conn.close()


class Driver(registry.Driver):
    kind, title = "homeassistant", "Home Assistant"
    hub = True
    beta = True
    mdns = ("_home-assistant._tcp.local.",)
    pairing = PAIRING
    fields = (
        {"key": "url", "label": "Home Assistant address", "placeholder": "http://homeassistant.local:8123"},
        {"key": "token", "label": "Long-lived access token",
         "placeholder": "From your Home Assistant profile → Security", "secret": True},
    )

    def from_mdns(self, service, name, host, port, props):
        props = props or {}
        url = props.get("internal_url") or props.get("base_url") or f"http://{host}:{port}"
        return registry.Found("homeassistant", f"homeassistant:{host}:{port}", host, port,
                              props.get("location_name") or "Home Assistant", {"url": url})

    def probe(self, values):
        url = _norm_url(values.get("url", ""))
        token = (values.get("token") or "").strip()
        if not url:
            raise ValueError("Enter the address of Home Assistant")
        if not token:
            raise ValueError("Enter a long-lived access token")
        api = _Api(url, token)
        if not api.host:
            raise ValueError(f"{url} isn't a valid address")
        try:
            data = api.call("GET", "/api/", timeout=5.0)
        except HttpError as e:
            if e.status == 401:
                raise ValueError(BAD_TOKEN) from None
            raise ValueError(f"Home Assistant answered {e} at {url}") from None
        except Exception as e:
            raise ValueError(f"Couldn't reach Home Assistant at {url} ({e})") from None
        if not isinstance(data, dict) or "message" not in data:
            raise ValueError(f"That doesn't look like Home Assistant ({url})")
        label = "Home Assistant"
        try:
            config = api.call("GET", "/api/config", timeout=5.0)
            if isinstance(config, dict) and config.get("location_name"):
                label = str(config["location_name"])
        except Exception:
            pass   # the name is a nicety
        return registry.Found("homeassistant", f"homeassistant:{api.host}:{api.port}", api.host, api.port,
                              label, {"url": url, "token": token})

    def create(self, dev_id, entry):
        return HAHub(dev_id, entry)

    def pair(self, target):
        if not getattr(target, "token", ""):
            raise NotPairedError(PAIRING)
        return {}
