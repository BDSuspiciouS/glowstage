"""Phone remote: a small page on your phone (same Wi-Fi) to switch the lights on and off, set the
brightness, pick a preset, start the light show or the sleep timer (Setup → Phone remote).

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
from urllib.parse import parse_qs, urlparse

from .paths import UI_DIR

log = logging.getLogger(__name__)

PAGE = UI_DIR / "phone.html"
MAX_BODY = 4096


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
        return {"on": bool(self.settings.get("on")), "url": url, "error": error}

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
                if path == "/":
                    return self._send(200, remote.page(), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(remote.state())
                self._send(404, b"", "text/plain")

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                if size > MAX_BODY:
                    self.close_connection = True
                    return self._json({"ok": False, "error": "Too much"}, 413)
                body = self.rfile.read(size) if size else b""   # (read first: an answer before that resets the connection)
                if not self._key_ok():
                    return self._json({"ok": False, "error": "This link is no longer valid"}, 403)
                if urlparse(self.path).path != "/api/do":
                    return self._send(404, b"", "text/plain")
                try:
                    remote.act(json.loads(body or b"{}"))
                except (ValueError, TypeError) as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                self._json({"ok": True, "state": remote.state()})

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
            lights = [m.states[i] for i in m.visible_ids() if i in m.states and m.devices[i].online]
        on = [st for st in lights if st.on]
        return {
            "on": len(on), "lights": len(lights),
            "brightness": round(sum(st.brightness for st in on) / len(on)) if on else 0,
            "presets": [{k: p[k] for k in ("name", "effects", "colors", "off")} for p in m.preset_previews()],
            "active": m.config["settings"].get("active_preset", ""),
            "show": m.show_running(),
            "sleep": m.sleep.describe(),
            "now": time.time(),
        }

    def act(self, req: dict) -> None:
        m, what = self.manager, str(req.get("do", ""))
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
        else:
            raise ValueError("Unknown action")


OLD_LINK = (b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            b"<body style='font:16px system-ui;background:#0f1013;color:#f3f1ed;padding:32px'>"
            b"<h2>This link doesn't work any more</h2><p>Open Glowstage on your PC, go to Setup &rarr; "
            b"Phone remote and scan the code again.</p>")
