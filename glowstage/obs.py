"""On air with OBS Studio: while you stream or record, your lights take a look of your choice (a
preset, for example one that only sets a light red like an ON AIR sign) and go back when you stop
(Setup → Automatic → "When you stream or record in OBS", automations.py).

OBS (28 and later) has a WebSocket server built in: Tools → WebSocket Server Settings. Glowstage
talks to it the way OBS's own remote tools do (protocol v5), only on this PC (127.0.0.1, never
another address), only while the switch in Setup is on, and only to ask whether a stream or a
recording is running and to hear when that changes. It never changes anything in OBS.

There is no websocket package in Glowstage, so the small part of the protocol that is needed is
written out here with the standard library: the HTTP upgrade, masked text frames out, text, ping,
close and fragmented frames in, with a cap on the size of a message.

The password (if OBS has one) is kept with Windows' own protection for the current user (DPAPI),
never as text in config.json, and goes nowhere but into OBS's local handshake: OBS sends a random
challenge and Glowstage answers with a hash of the password and that challenge, not the password.
It is never shown back and never written to the log or to the problem report."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import socket
import threading
from ctypes import wintypes as w

log = logging.getLogger(__name__)

HOST = "127.0.0.1"               # only this PC: OBS's server listens here, and nothing else is ever asked
DEFAULT_PORT = 4455
WHEN = ("both", "stream", "record")
RETRY = 3.0                      # seconds between tries while OBS isn't there (a refused connection is instant)
PASSWORD_RETRY = 30.0            # after a wrong password: the same one won't suddenly work (a new one wakes us)
CONNECT_TIMEOUT = 3.0
LISTEN_TIMEOUT = 0.5             # how often a quiet connection looks whether it should end
MAX_MESSAGE = 1 << 20            # OBS's answers are tiny; anything bigger isn't OBS
MAX_HEADERS = 16 << 10
PASSWORD_MAX = 256
EVENT_OUTPUTS = 64               # the event group with StreamStateChanged and RecordStateChanged
WRONG_PASSWORD = 4009            # the code OBS closes the connection with when the answer is wrong
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SUBPROTOCOL = "obswebsocket.json"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class ObsError(Exception):
    """OBS (or whatever sits on the port) didn't talk the way OBS does."""


class WrongPassword(ObsError):
    def __init__(self, missing: bool = False):
        super().__init__("OBS needs a password" if missing else "OBS didn't accept the password")
        self.missing = missing


class Closed(ObsError):
    def __init__(self, code: int = 1006, reason: str = ""):
        super().__init__(f"connection closed ({code})")
        self.code, self.reason = code, reason


# ---- the password: stored with Windows' own protection ------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.c_void_p)]


_ENTROPY = b"Glowstage OBS password"     # (so a blob from another program's use of DPAPI isn't taken for ours)
_UI_FORBIDDEN = 0x1


def _dpapi(name: str, data: bytes) -> bytes:
    crypt32, k32 = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32")
    fn = getattr(crypt32, name)
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, w.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = w.BOOL
    k32.LocalFree.argtypes, k32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    buf, ent = ctypes.create_string_buffer(data, len(data)), ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.c_void_p))
    salt = _Blob(len(_ENTROPY), ctypes.cast(ent, ctypes.c_void_p))
    out = _Blob()
    if not fn(ctypes.byref(src), None, ctypes.byref(salt), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "Windows couldn't protect or open the password")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        k32.LocalFree(out.pbData)


def protect(text: str) -> str:
    """The text, protected for this Windows user, as base64 (what config.json holds)."""
    return base64.b64encode(_dpapi("CryptProtectData", text.encode("utf-8"))).decode("ascii")


def unprotect(blob: str) -> str | None:
    """What protect() made into text again; None when it can't be opened (another user or PC, say)."""
    try:
        return _dpapi("CryptUnprotectData", base64.b64decode(blob.encode("ascii"), validate=True)).decode("utf-8")
    except (OSError, ValueError):
        return None


def auth_string(password: str, salt: str, challenge: str) -> str:
    """OBS's answer to its challenge: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


# ---- the websocket, as far as it's needed ---------------------------------------------------------

def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes = b"", key: bytes | None = None, fin: bool = True) -> bytes:
    """One frame. A client masks (`key`: 4 random bytes); a server doesn't (key None)."""
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    flag = 0x80 if key else 0
    if n < 126:
        head += bytes([flag | n])
    elif n < 1 << 16:
        head += bytes([flag | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([flag | 127]) + n.to_bytes(8, "big")
    return head + (key + _mask(payload, key) if key else payload)


def parse_frame(buf, max_size: int = MAX_MESSAGE):
    """(fin, opcode, payload, bytes used) for the first frame in `buf`, None while it isn't all
    there yet. A frame that can't be right (or is too big) raises ObsError."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    if b0 & 0x70:
        raise ObsError("Unexpected websocket extension")
    fin, opcode, masked, n, at = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, at = int.from_bytes(buf[2:4], "big"), 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, at = int.from_bytes(buf[2:10], "big"), 10
    if opcode >= 0x8 and (n > 125 or not fin):
        raise ObsError("Bad websocket control frame")
    if n > max_size:
        raise ObsError("Websocket message too big")
    key = b""
    if masked:
        if len(buf) < at + 4:
            return None
        key, at = bytes(buf[at:at + 4]), at + 4
    if len(buf) < at + n:
        return None
    payload = bytes(buf[at:at + n])
    return fin, opcode, (_mask(payload, key) if masked else payload), at + n


class WebSocket:
    """A client over a connected socket: whole text messages in and out. Ping gets its pong,
    a close ends it (Closed, with the code), fragments are put together. A read that times out
    keeps what it had so far: ask again."""

    def __init__(self, sock, leftover: bytes = b"", max_size: int = MAX_MESSAGE, rng=os.urandom):
        self.sock = sock
        self.max_size = max_size
        self._rng = rng
        self._buf = bytearray(leftover)
        self._op: int | None = None            # the message being put together: its first opcode
        self._parts = bytearray()

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8"), self._rng(4)))

    def send_json(self, data: dict) -> None:
        self.send_text(json.dumps(data, separators=(",", ":")))

    def close(self, code: int = 1000) -> None:
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, code.to_bytes(2, "big"), self._rng(4)))
        except OSError:
            pass

    def recv(self) -> str:
        """The next text message. TimeoutError when the socket's timeout passes first."""
        while True:
            frame = parse_frame(self._buf, self.max_size)
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise Closed(1006, "the connection ended")
                self._buf += chunk
                continue
            fin, op, payload, used = frame
            del self._buf[:used]
            if op == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload, self._rng(4)))
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 1005
                self.close(code if code != 1005 else 1000)
                raise Closed(code, payload[2:].decode("utf-8", "replace"))
            if op in (OP_TEXT, OP_BINARY):
                if self._op is not None:
                    raise ObsError("A new websocket message began before the last one ended")
                self._op, self._parts = op, bytearray(payload)
            elif op == OP_CONT:
                if self._op is None:
                    raise ObsError("A websocket continuation with nothing to continue")
                self._parts += payload
            else:
                raise ObsError("Unknown websocket frame")
            if len(self._parts) > self.max_size:
                raise ObsError("Websocket message too big")
            if fin:
                kind, data, self._op, self._parts = self._op, bytes(self._parts), None, bytearray()
                if kind == OP_TEXT:
                    return data.decode("utf-8")
                # (OBS only sends text with the json subprotocol: anything binary is not for us)


def handshake(sock, port: int, rng=os.urandom) -> WebSocket:
    """The HTTP upgrade to a websocket, with the accept key checked."""
    key = base64.b64encode(rng(16)).decode("ascii")
    sock.sendall((f"GET / HTTP/1.1\r\nHost: {HOST}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                  f"Sec-WebSocket-Protocol: {SUBPROTOCOL}\r\n\r\n").encode("ascii"))
    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        if len(raw) > MAX_HEADERS:
            raise ObsError("Too much before the websocket began")
        chunk = sock.recv(4096)
        if not chunk:
            raise ObsError("The connection ended before the websocket began")
        raw += chunk
    head, _, rest = bytes(raw).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if len(lines[0].split(" ", 2)) < 2 or lines[0].split(" ", 2)[1] != "101":
        raise ObsError("That port isn't OBS's WebSocket server")
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    if headers.get("sec-websocket-accept") != want:
        raise ObsError("That port isn't OBS's WebSocket server")
    return WebSocket(sock, rest, rng=rng)


def identify(ws: WebSocket, password: str | None) -> None:
    """Hello, Identify, Identified. WrongPassword when OBS wants one we don't have or refuses ours."""
    hello = json.loads(ws.recv())
    if not isinstance(hello, dict) or hello.get("op") != 0 or not isinstance(hello.get("d"), dict):
        raise ObsError("OBS didn't say hello")
    d: dict = {"rpcVersion": 1, "eventSubscriptions": EVENT_OUTPUTS}
    auth = hello["d"].get("authentication")
    if isinstance(auth, dict):
        if not password:
            raise WrongPassword(missing=True)
        d["authentication"] = auth_string(password, str(auth.get("salt", "")), str(auth.get("challenge", "")))
    ws.send_json({"op": 1, "d": d})
    try:
        reply = json.loads(ws.recv())
    except Closed as e:
        if e.code == WRONG_PASSWORD:
            raise WrongPassword() from None
        raise
    if not isinstance(reply, dict) or reply.get("op") != 2:
        raise ObsError("OBS didn't accept Glowstage")


def _request(ws: WebSocket, kind: str, request_id: str) -> None:
    ws.send_json({"op": 6, "d": {"requestType": kind, "requestId": request_id}})


# ---- the watcher ---------------------------------------------------------------------------------

def _connect(host: str, port: int):
    return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)


class ObsWatcher:
    """Keeps up with whether OBS is streaming or recording, while it's switched on (Setup).
    Owned by the device manager next to the game lighting; automations.py asks live().

    `connect(host, port)` makes the socket: tests give it a fake, so no test opens a real port."""

    def __init__(self, config, on_change=None, connect=None, retry: float = RETRY):
        self.config = config
        self.on_change = on_change or (lambda: None)
        self._connect = connect or _connect
        self._retry = retry
        self._lock = threading.Lock()
        self._wake = threading.Event()      # the settings changed (or we're stopping): look again now
        self._stop = threading.Event()
        self._gen = 0                       # counts changes that need a new connection
        self._status = "off"                # off, waiting, password, needs_password or connected
        self._version = ""
        self._streaming = False
        self._recording = False
        self._opened: tuple[str, str] = ("", "")      # (the protected blob, what it opens to): opened once
        self._thread: threading.Thread | None = None

    @property
    def settings(self) -> dict:
        return self.config.data.setdefault("obs", {"on": False, "port": DEFAULT_PORT, "password": "", "when": "both"})

    # ---- the settings ---------------------------------------------------------------------------

    def set_options(self, on, port, when) -> None:
        """The switch, the port (1-65535) and what counts (stream, record or both)."""
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            raise ValueError("The port is a number from 1 to 65535")
        if when not in WHEN:
            raise ValueError("Choose stream, record or both")
        s = self.settings
        again = bool(s.get("on")) != bool(on) or s.get("port") != port
        s.update(on=bool(on), port=port, when=when)
        self.config.save_soon()
        if again:
            self._reconnect()
        self.on_change()

    def set_password(self, password) -> None:
        """OBS's password, kept protected for this Windows user. Never shown back."""
        if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
            raise ValueError("Type the password OBS has")
        try:
            self.settings["password"] = protect(password)
        except OSError:
            raise ValueError("Windows couldn't keep the password safely, so it wasn't saved") from None
        self.config.save_soon()
        self._reconnect()
        self.on_change()

    def clear_password(self) -> None:
        self.settings["password"] = ""
        self.config.save_soon()
        self._reconnect()
        self.on_change()

    def _password(self) -> str | None:
        blob = self.settings.get("password") or ""
        if not blob:
            return None
        if self._opened[0] != blob:
            self._opened = (blob, unprotect(blob) or "")
        return self._opened[1] or None

    def _reconnect(self) -> None:
        with self._lock:
            self._gen += 1
        self._wake.set()

    # ---- what it knows --------------------------------------------------------------------------

    def live(self) -> bool:
        """You stream or record now (whichever the setting counts)."""
        with self._lock:
            if self._status != "connected":
                return False
            when = self.settings.get("when", "both")
            return (when in ("both", "stream") and self._streaming) or (when in ("both", "record") and self._recording)

    def status_text(self) -> str:
        with self._lock:
            status, version = self._status, self._version
        if status == "connected":
            return "Live now" if self.live() else f"Connected to OBS{' ' + version if version else ''}"
        return {"password": "OBS didn't accept the password", "needs_password": "OBS needs a password",
                "waiting": "OBS isn't running, or its WebSocket server is off"}.get(status, "")

    def describe(self) -> dict:
        s = self.settings
        with self._lock:
            out = {"status": self._status, "version": self._version, "streaming": self._streaming,
                   "recording": self._recording}
        return {"on": bool(s.get("on")), "port": s.get("port") or DEFAULT_PORT, "when": s.get("when") or "both",
                "hasPassword": bool(s.get("password")), **out, "live": self.live(), "text": self.status_text()}

    def summary(self) -> str:
        """One word for the problem report: never the password."""
        if not self.settings.get("on"):
            return "off"
        with self._lock:
            return {"connected": "connected", "waiting": "not reachable", "password": "wrong password",
                    "needs_password": "needs a password"}.get(self._status, self._status)

    def _set(self, **changes) -> None:
        with self._lock:
            before = (self._status, self._version, self._streaming, self._recording)
            for k, v in changes.items():
                setattr(self, "_" + k, v)
            after = (self._status, self._version, self._streaming, self._recording)
        if before != after:
            if before[0] != after[0]:
                log.info("OBS: %s", self.summary())
            self.on_change()

    # ---- the connection -------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="obs", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if not self.settings.get("on"):
                self._set(status="off", streaming=False, recording=False, version="")
                self._wake.wait()          # nothing to do until it's switched on
                continue
            wait = self._retry
            try:
                self._session()
                self._set(status="waiting")
            except WrongPassword as e:
                self._set(status="needs_password" if e.missing else "password")
                wait = PASSWORD_RETRY
            except (OSError, ObsError, ValueError) as e:     # (not running: refused at once; not OBS: no handshake)
                log.debug("OBS: %s", e)
                self._set(status="waiting")
            except Exception:
                log.exception("OBS watcher failed")
                self._set(status="waiting")
            self._wake.wait(wait)          # a changed setting or stop ends the wait early

    def _session(self) -> None:
        """One connection, from the first byte until it ends or the settings change."""
        with self._lock:
            gen = self._gen
        port = int(self.settings.get("port") or DEFAULT_PORT)
        sock = self._connect(HOST, port)
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            ws = handshake(sock, port)
            identify(ws, self._password())
            self._set(status="connected")
            for kind, rid in (("GetVersion", "version"), ("GetStreamStatus", "stream"), ("GetRecordStatus", "record")):
                _request(ws, kind, rid)
            sock.settimeout(LISTEN_TIMEOUT)
            while not self._stop.is_set() and self.settings.get("on"):
                with self._lock:
                    if gen != self._gen:
                        return
                try:
                    self._handle(ws.recv())
                except TimeoutError:
                    continue
        except Closed as e:
            log.debug("OBS closed the connection (%s)", e.code)
        finally:
            self._set(streaming=False, recording=False, version="")
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        d = msg.get("d") if isinstance(msg, dict) else None
        if not isinstance(d, dict):
            return
        op = msg.get("op")
        if op == 5:                                    # an event
            data = d.get("eventData") if isinstance(d.get("eventData"), dict) else {}
            if d.get("eventType") == "StreamStateChanged":
                self._set(streaming=bool(data.get("outputActive")))
            elif d.get("eventType") == "RecordStateChanged":
                self._set(recording=bool(data.get("outputActive")))
        elif op == 7:                                  # the answer to one of our questions
            answer = d.get("responseData") if isinstance(d.get("responseData"), dict) else {}
            ok = isinstance(d.get("requestStatus"), dict) and d["requestStatus"].get("result")
            if not ok:
                return
            if d.get("requestId") == "version":
                self._set(version=".".join(str(answer.get("obsVersion", "")).split(".")[:2]))
            elif d.get("requestId") == "stream":
                self._set(streaming=bool(answer.get("outputActive")))
            elif d.get("requestId") == "record":
                self._set(recording=bool(answer.get("outputActive")))
