"""Check the real Glowstage window, not the browser preview: JavaScript errors while the page
loads, and whether the main parts show up with the app's real data.

The app has to run with WebView2's debugging port open (a developer check, on this PC only):

    set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333
    pythonw Glowstage.pyw --minimized
    python tools/window_check.py 9333 [screenshot.png]

It reloads the page, collects every error and warning the page reports, runs a few checks on
Home, the effects panel and Setup, and prints a summary (exit code 1 when something is wrong).
Only the standard library: a tiny WebSocket client for the DevTools protocol.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlparse


class DevTools:
    def __init__(self, ws_url: str):
        u = urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.decode(errors="replace"))
        self.next_id = 0
        self.events: list[dict] = []

    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("closed")
            out += chunk
        return out

    def _send(self, text: str) -> None:
        data = text.encode()
        head = bytearray([0x81])
        mask = os.urandom(4)
        if len(data) < 126:
            head.append(0x80 | len(data))
        elif len(data) < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if b1 & 0x0F in (0x1, 0x0):
                message += payload
                if b1 & 0x80:
                    return json.loads(message)
            # (pings and the like: ignored)

    def call(self, method: str, **params) -> dict:
        self.next_id += 1
        wanted = self.next_id
        self._send(json.dumps({"id": wanted, "method": method, "params": params}))
        while True:
            msg = self._recv()
            if msg.get("id") == wanted:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def listen(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        self.sock.settimeout(0.2)
        while time.monotonic() < end:
            try:
                msg = self._recv()
            except (socket.timeout, TimeoutError):
                continue
            if "method" in msg:
                self.events.append(msg)
        self.sock.settimeout(10)

    def js(self, expression: str):
        r = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "error"))
        return r["result"].get("value")


CHECKS = r"""
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const ready = () => typeof S !== 'undefined' && S.data;
  for (let i = 0; i < 40 && !ready(); i++) await wait(250);
  const out = { loaded: !!ready() };
  if (!out.loaded) return out;
  const q = s => document.querySelector(s);
  out.version = (S.data.about || {}).version;
  out.lights = S.data.devices.length;
  out.lightCards = document.querySelectorAll('#page-home .tile').length;
  out.presets = document.querySelectorAll('#page-home .preset').length;
  out.homeButtons = ['#all-color', '#sleep-timer', '#all-power'].filter(s => !q(s));
  out.whatsNewCard = !!q('.note-new');
  q('#all-color').click(); await wait(300);
  switchTab('effects'); await wait(300);
  out.effects = [...document.querySelectorAll('#fx-grid .fx-card')].map(c => c.dataset.fx);
  closeSheet();
  go('setup'); await wait(500);
  out.setup = [...document.querySelectorAll('#settings-body h3')].map(h => h.textContent);
  out.setupIds = ['set-automatic', 'set-phone'].filter(id => !document.getElementById(id));
  go('home'); await wait(200);
  return out;
})()
"""


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
    shot = sys.argv[2] if len(sys.argv) > 2 else ""
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
    page = next((p for p in pages if p.get("type") == "page" and "index.html" in p.get("url", "")), None)
    if not page:
        print("No Glowstage page found:", [p.get("url") for p in pages])
        return 1
    d = DevTools(page["webSocketDebuggerUrl"])
    d.call("Runtime.enable")
    d.call("Log.enable")
    d.call("Page.enable")
    d.events.clear()
    d.call("Page.reload", ignoreCache=True)
    d.listen(4.0)
    try:
        result = d.js(CHECKS)
    except RuntimeError as e:
        result = {"error": str(e)}
    problems = []
    for e in d.events:
        m, p = e["method"], e.get("params", {})
        if m == "Runtime.exceptionThrown":
            det = p.get("exceptionDetails", {})
            problems.append("exception: " + (det.get("exception", {}).get("description") or det.get("text", "")).split("\n")[0])
        elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning", "assert"):
            problems.append(f"console.{p['type']}: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))
        elif m == "Log.entryAdded" and p.get("entry", {}).get("level") in ("error", "warning"):
            problems.append(f"log {p['entry']['level']}: {p['entry'].get('text', '')} {p['entry'].get('url', '')}")
    if shot:
        try:
            png = d.call("Page.captureScreenshot", format="png").get("data", "")
            open(shot, "wb").write(base64.b64decode(png))
        except Exception as e:     # (a hidden window may have nothing to show)
            problems.append(f"no screenshot: {e}")
    print(json.dumps({"checks": result, "problems": problems}, indent=2))
    bad = problems or not result.get("loaded") or result.get("homeButtons") or result.get("setupIds")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
