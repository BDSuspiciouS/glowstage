"""Phone remote: a small page on your phone (same Wi-Fi) to switch the lights on and off, set the
brightness, pick a preset, start the light show or the sleep timer, or light the room in the colors
of a photo you take there and then (Setup → Phone remote).

Off until you switch it on. Then Glowstage listens on your home network, on a port it keeps
(a new one if that's taken), and answers only to its secret link: the code in Setup, which
carries a random key. Without the key every request is refused; "New link" makes the old one
stop working. Windows asks once whether Glowstage may use the network: allow it on private
networks.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .paths import UI_DIR

log = logging.getLogger(__name__)

PAGE = UI_DIR / "phone.html"
MAX_BODY = 4096
PHOTO_PATH = "/api/picture"     # a photo from the phone (shrunk there first; palette.PHOTO_BYTES at most)
PRESET_NAME = 40                # the longest preset name the phone can give
LINK_STEP = 10                  # "brighter" and "darker" by link (the same step as the hotkeys)
GROUPS = (("pc", "PC"), ("room", "Room"))


def lan_ip() -> str:
    """This PC's address on the home network ("" when there's none)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))          # (nothing is sent: it only picks the way out)
            ip = s.getsockname()[0]
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""
    return "" if ip.startswith(("127.", "0.")) else ip


class Remote:
    def __init__(self, manager, config, on_change=None, host: str = "0.0.0.0"):
        self.manager = manager
        self.host = host                            # (the home network; tests use 127.0.0.1)
        self.config = config
        self.on_change = on_change or (lambda: None)
        self._server: ThreadingHTTPServer | None = None
        self.error = ""
        self._qr: tuple[str, str] = ("", "")       # (url, svg)

    @property
    def settings(self) -> dict:
        return self.config.data.setdefault("remote", {"on": False, "port": 0, "token": ""})

    # ---- on and off -------------------------------------------------------------------------

    def start(self) -> None:
        if self.settings.get("on") and self._server is None:
            self._listen()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def set_on(self, on: bool) -> None:
        self.settings["on"] = bool(on)
        if on and not self.settings.get("token"):
            self.settings["token"] = secrets.token_urlsafe(16)
        self.config.save_soon()
        self.stop()
        self.error = ""
        if on:
            self._listen()
        self.on_change()

    def new_link(self) -> None:
        """A new key: the old link (and any phone that has it) stops working."""
        self.settings["token"] = secrets.token_urlsafe(16)
        self.config.save_soon()
        self.on_change()

    def url(self) -> str:
        ip = lan_ip() if self.host == "0.0.0.0" else self.host
        if not (self._server and ip):
            return ""
        return f"http://{ip}:{self._server.server_address[1]}/?k={self.settings['token']}"

    def describe(self) -> dict:
        url = self.url()
        error = self.error or ("" if url or not self._server else "This PC isn't on a network")
        # `api` + "/<action>?k=" + `key` is a link other apps can call (Setup builds them from this)
        api = url.split("/?k=")[0] + "/do" if url else ""
        return {"on": bool(self.settings.get("on")), "url": url, "error": error,
                "api": api, "key": self.settings.get("token", "") if url else ""}

    def qr_svg(self) -> str:
        """The link as a QR code (made once per link)."""
        url = self.url()
        if not url:
            return ""
        if self._qr[0] != url:
            from .qr import QR
            self._qr = (url, QR(url).svg())
        return self._qr[1]

    # ---- the server --------------------------------------------------------------------------

    def _listen(self) -> None:
        remote = self

        class Handler(BaseHTTPRequestHandler):
            def _key_ok(self) -> bool:
                key = (parse_qs(urlparse(self.path).query).get("k") or [""])[0]
                token = remote.settings.get("token") or ""
                return bool(token) and hmac.compare_digest(key.encode(), token.encode())

            def _send(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, code: int = 200) -> None:
                self._send(code, json.dumps(data).encode(), "application/json")

            def do_GET(self):
                path = urlparse(self.path).path
                if not self._key_ok():
                    return self._send(403, OLD_LINK, "text/html; charset=utf-8")
                if path.startswith("/do/"):
                    return self._link(path)
                if path == "/":
                    return self._send(200, remote.page(), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(remote.state())
                self._send(404, b"", "text/plain")

            def do_POST(self):
                from .palette import PHOTO_BYTES
                path = urlparse(self.path).path
                size = int(self.headers.get("Content-Length") or 0)
                if size > (PHOTO_BYTES if path == PHOTO_PATH else MAX_BODY):
                    self.close_connection = True
                    return self._json({"ok": False, "error": "Too much"}, 413)
                body = self.rfile.read(size) if size else b""   # (read first: an answer before that resets the connection)
                if not self._key_ok():
                    return self._json({"ok": False, "error": "This link is no longer valid"}, 403)
                if path.startswith("/do/"):
                    return self._link(path)
                if path == PHOTO_PATH:
                    try:
                        colors = remote.photo(body)
                    except ValueError as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    return self._json({"ok": True, "colors": colors, "state": remote.state()})
                if path != "/api/do":
                    return self._send(404, b"", "text/plain")
                try:
                    remote.act(json.loads(body or b"{}"))
                except (ValueError, TypeError) as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                self._json({"ok": True, "state": remote.state()})

            def _link(self, path: str) -> None:
                """A plain action link for other apps (Stream Deck, AutoHotkey, Home Assistant)."""
                try:
                    code, data = remote.link([unquote(p) for p in path[len("/do/"):].split("/")])
                except Exception:
                    log.exception("Phone remote: a link failed")     # (a bad request never ends the server)
                    code, data = 500, {"ok": False, "error": "Something went wrong"}
                self._json(data, code)

            def log_message(self, *args):   # (no line per request)
                pass

        server = None
        for port in (int(self.settings.get("port") or 0), 0):
            try:
                server = ThreadingHTTPServer((self.host, port), Handler)
                break
            except OSError:
                continue
        if server is None:
            self.error = "No free port to listen on"
            log.warning("Phone remote: %s", self.error)
            return
        server.daemon_threads = True
        self._server = server
        port = server.server_address[1]
        if port != self.settings.get("port"):
            self.settings["port"] = port
            self.config.save_soon()
        threading.Thread(target=server.serve_forever, name="phone-remote", daemon=True).start()
        log.info("Phone remote: listening on port %d", port)

    # ---- what the phone sees and does ----------------------------------------------------------

    def page(self) -> bytes:
        return PAGE.read_bytes()

    def state(self) -> dict:
        m = self.manager
        with m.lock:
            visible = [i for i in m.visible_ids() if i in m.states]
            lights = [m.states[i] for i in visible if m.devices[i].online]
            rows = [{"id": i, "name": m.devices[i].name, "group": _group_of(m.devices[i]),
                     "on": m.states[i].on, "brightness": m.states[i].brightness,
                     "color": m.states[i].color, "online": bool(m.devices[i].online)} for i in visible]
        on = [st for st in lights if st.on]
        groups = []
        for key, name in GROUPS:
            mine = [r for r in rows if r["group"] == key]
            if not mine:
                continue                                  # (no lights in this group: no row)
            lit = [r for r in mine if r["on"] and r["online"]]
            groups.append({"key": key, "name": name, "on": bool(lit),
                           "brightness": round(sum(r["brightness"] for r in lit) / len(lit)) if lit else 0})
        return {
            "on": len(on), "count": len(lights),
            "brightness": round(sum(st.brightness for st in on) / len(on)) if on else 0,
            "groups": groups, "lights": rows,
            "presets": [{k: p[k] for k in ("name", "effects", "colors", "off")} for p in m.preset_previews()],
            "active": m.config["settings"].get("active_preset", ""),
            "show": m.show_running(),
            "sleep": m.sleep.describe(),
            "canUndo": m.can_undo(),
            "now": time.time(),
        }

    def act(self, req: dict) -> None:
        m, what = self.manager, str(req.get("do", ""))
        if what in ("group", "group_brightness", "light", "light_brightness"):
            return self._act_lights(what, req)
        if what in ("power", "preset", "show"):
            m.remember()                      # (undo works for the phone's changes too)
        if what == "power":
            m.set_power_all(bool(req.get("on")))
        elif what == "brightness":
            value = int(req.get("value"))
            if not 1 <= value <= 100:
                raise ValueError("Brightness goes from 1 to 100")
            m.update(None, {"brightness": value})
        elif what == "preset":
            name = str(req.get("name", ""))
            if name not in m.preset_names():
                raise ValueError("There's no such preset")
            m.apply_preset(name)
        elif what == "show":
            m.toggle_show()
        elif what == "sleep":
            minutes = int(req.get("minutes") or 0)
            m.sleep.start(minutes) if minutes else m.sleep.cancel()
        elif what == "undo":
            if not m.undo():
                raise ValueError("There's nothing to undo")
        elif what == "save":
            name = req.get("name")
            name = name.strip() if isinstance(name, str) else ""
            if not name:
                raise ValueError("Give the preset a name")
            if len(name) > PRESET_NAME:
                raise ValueError(f"A preset name can be {PRESET_NAME} letters at most")
            if name in m.preset_names():     # (the phone never saves over one of your presets)
                raise ValueError(f"There's already a preset called {name}")
            m.save_preset(name)
        else:
            raise ValueError("Unknown action")

    def photo(self, data: bytes) -> list[str]:
        """A photo from the phone: its main colors on the lights that are on, left to right as
        they stand on the stage, like Presets → From a picture in the window (Undo puts the
        lights back). The colors, for the phone to show."""
        from . import palette
        m = self.manager
        img = palette.read_bytes(data)
        colors = palette.main_colors(img)
        if not m.any_on():          # (checked before remember(): a refused photo keeps the last undo)
            raise ValueError("Switch a light on first, then try again")
        m.remember()
        m.light_from_colors(colors)
        return colors

    def link(self, parts: list[str]) -> tuple[int, dict]:
        """An action link, /do/<action>[/<value>]: the same calls as the tray and the hotkeys
        (so Undo covers it too). Gives the HTTP status and the answer."""
        m = self.manager
        while parts and parts[-1] == "":
            parts = parts[:-1]                      # (a slash at the end is fine)
        action, args = (parts[0] if parts else ""), parts[1:]

        def fail(code: int, error: str) -> tuple[int, dict]:
            return code, {"ok": False, "error": error}

        def number(text: str) -> int | None:
            return int(text) if text.isascii() and text.isdigit() and len(text) <= 4 else None

        if action in ("on", "off", "toggle", "brighter", "darker", "undo") and not args:
            if action == "undo":
                if not m.undo():
                    return fail(409, "There's nothing to undo")
            elif action in ("brighter", "darker"):
                m.nudge_brightness(LINK_STEP if action == "brighter" else -LINK_STEP)
            else:
                m.remember()
                m.set_power_all(not m.any_on() if action == "toggle" else action == "on")
        elif action == "preset" and len(args) == 1:
            names = m.preset_names()
            if args[0] not in names:        # typed by hand ("cozy" for Cozy): any case will do
                same = [n for n in names if n.casefold() == args[0].casefold()]
                if len(same) != 1:
                    return fail(404, f"No preset called {args[0]}")
                args = same
            m.remember()
            m.apply_preset(args[0])
        elif action == "brightness" and len(args) == 1:
            value = number(args[0])
            if value is None or value > 100:
                return fail(400, "Brightness goes from 0 to 100")
            m.remember()
            m.set_brightness_on(value)              # (0 is the lowest light, not off)
        elif action == "show" and len(args) == 1 and args[0] in ("start", "stop"):
            m.remember()
            m.start_show() if args[0] == "start" else m.stop_show()
        elif action == "sleep" and len(args) == 1:
            if args[0] == "cancel":
                m.sleep.cancel()
            else:
                minutes = number(args[0])
                if minutes is None:
                    return fail(400, "Minutes must be a number")
                try:
                    m.sleep.start(minutes)
                except ValueError as e:
                    return fail(400, str(e))
        else:
            return fail(404, "Unknown action")
        return 200, {"ok": True, "action": "/".join([action, *args])}

    def _act_lights(self, what: str, req: dict) -> None:
        """The groups and single lights: everything is checked before anything changes."""
        m = self.manager
        if what in ("group", "group_brightness"):
            group = req.get("group")
            if group not in ("pc", "room"):
                raise ValueError("There's no such group")
        else:
            dev_id = req.get("id")
            with m.lock:
                known = isinstance(dev_id, str) and dev_id in m.visible_ids() and dev_id in m.states
                online = known and bool(m.devices[dev_id].online)
            if not known:
                raise ValueError("There's no such light")
            if not online:
                raise ValueError("That light isn't answering")
        if what in ("group", "light"):
            on = req.get("on")
            if not isinstance(on, bool):
                raise ValueError("on must be true or false")
            m.remember()                      # (one change, one undo)
            if what == "group":
                m.set_power_group(group, on)
            else:
                m.update([dev_id], {"on": on})
            return
        value = req.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
            raise ValueError("Brightness must be a number")
        value = max(1, min(100, int(value)))
        if what == "light_brightness":
            m.update([dev_id], {"brightness": value})
            return
        with m.lock:                          # (only the group's lights that are on)
            ids = [i for i in m.visible_ids()
                   if i in m.states and m.states[i].on and _group_of(m.devices[i]) == group]
        if ids:
            m.update(ids, {"brightness": value})


def _group_of(device) -> str:
    """"pc" for the PC's lights, "room" for all the others (the same rule as the manager's)."""
    return "pc" if device.group == "pc" else "room"


OLD_LINK = (b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            b"<body style='font:16px system-ui;background:#0f1013;color:#f3f1ed;padding:32px'>"
            b"<h2>This link doesn't work any more</h2><p>Open Glowstage on your PC, go to Setup &rarr; "
            b"Phone remote and scan the code again.</p>")
