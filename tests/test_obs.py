import base64
import hashlib
import json
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from glowstage import automations, calls, diagnostics, obs
from glowstage.api import Api
from glowstage.automations import Automations
from glowstage.config import Config
from glowstage.manager import DeviceManager
from glowstage.obs import (Closed, ObsError, ObsWatcher, WebSocket, WrongPassword, auth_string, encode_frame,
                           handshake, identify, parse_frame)
from tests.test_app_looks import FakeManager
from tests.test_calls import FakeStop
from tests.test_diagnostics import FakeApp

WINDOWS = sys.platform == "win32"
KEY = b"\x37\xfa\x21\x3d"       # the masking key of RFC 6455's own example


def wait_for(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return check()


# ---- a stand-in for OBS: the other end of a socketpair, run on a thread ----------------------------

class Server:
    """What OBS's side of the conversation looks like, over one end of a socketpair."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        sock.settimeout(5)

    def upgrade(self, accept=None, extra=b""):
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.sock.recv(4096)
        self.request = data.decode("latin-1")
        key = re.search(r"Sec-WebSocket-Key: (\S+)", self.request).group(1)
        good = base64.b64encode(hashlib.sha1((key + obs.WS_GUID).encode()).digest()).decode()
        self.sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          b"Sec-WebSocket-Accept: " + (accept or good).encode() + b"\r\n\r\n" + extra)

    def send(self, obj):
        self.sock.sendall(encode_frame(obs.OP_TEXT, json.dumps(obj).encode()))

    def frame(self):
        while True:
            got = parse_frame(self.buf)
            if got:
                del self.buf[:got[3]]
                return got[:3]
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def json(self):
        fin, op, payload = self.frame()
        assert op == obs.OP_TEXT, op
        return json.loads(payload)

    def close(self, code=1000):
        try:
            self.sock.sendall(encode_frame(obs.OP_CLOSE, code.to_bytes(2, "big")))
        except OSError:
            pass


SALT, CHALLENGE = "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=", "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="


def answer(server, request, streaming, recording):
    kind = request["d"]["requestType"]
    data = {"GetVersion": {"obsVersion": "30.2.3", "obsWebSocketVersion": "5.5.2"},
            "GetStreamStatus": {"outputActive": streaming}, "GetRecordStatus": {"outputActive": recording}}[kind]
    server.send({"op": 7, "d": {"requestType": kind, "requestId": request["d"]["requestId"],
                                "requestStatus": {"result": True, "code": 100}, "responseData": data}})


def obs_says(server, password=None, streaming=False, recording=False, after=None):
    """Greet, check the answer, tell what's running; then `after(server)` to play on."""
    server.upgrade()
    hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.2", "rpcVersion": 1}}
    if password:
        hello["d"]["authentication"] = {"challenge": CHALLENGE, "salt": SALT}
    server.send(hello)
    ident = server.json()
    assert ident["op"] == 1 and ident["d"]["rpcVersion"] == 1 and ident["d"]["eventSubscriptions"] == 64, ident
    if password and ident["d"].get("authentication") != auth_string(password, SALT, CHALLENGE):
        server.close(obs.WRONG_PASSWORD)
        return
    if not password:
        assert "authentication" not in ident["d"]
    server.send({"op": 2, "d": {"negotiatedRpcVersion": 1}})
    for _ in range(3):
        answer(server, server.json(), streaming, recording)
    if after:
        after(server)


def serve(script, *args, **kw):
    """A socketpair whose far end plays `script` on a thread: gives the near end."""
    near, far = socket.socketpair()
    server = Server(far)
    server.finish = threading.Event()      # (the connection stays open until the test is over)

    def run():
        try:
            script(server, *args, **kw)
            server.finish.wait(15)
        except (OSError, EOFError, AssertionError):
            pass
        finally:
            far.close()

    threading.Thread(target=run, daemon=True).start()
    return near, server


def event(kind, active):
    return {"op": 5, "d": {"eventType": kind, "eventIntent": 64,
                           "eventData": {"outputActive": active, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"}}}


# ---- the password's proof ----------------------------------------------------------------------------

class Auth(unittest.TestCase):
    def test_the_example_from_obs_websockets_own_docs(self):
        self.assertEqual(auth_string("supersecretpassword", SALT, CHALLENGE), "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=")

    def test_it_is_what_the_spec_says(self):
        secret = base64.b64encode(hashlib.sha256(b"hunter2" + b"somesalt").digest()).decode()
        want = base64.b64encode(hashlib.sha256((secret + "somechallenge").encode()).digest()).decode()
        self.assertEqual(auth_string("hunter2", "somesalt", "somechallenge"), want)

    def test_another_password_another_answer(self):
        self.assertNotEqual(auth_string("a", SALT, CHALLENGE), auth_string("b", SALT, CHALLENGE))


# ---- frames --------------------------------------------------------------------------------------------

class Frames(unittest.TestCase):
    def test_rfc_6455_masked_hello(self):
        self.assertEqual(encode_frame(obs.OP_TEXT, b"Hello", KEY).hex(), "818537fa213d7f9f4d5158")

    def test_rfc_6455_unmasked_hello(self):
        self.assertEqual(encode_frame(obs.OP_TEXT, b"Hello").hex(), "810548656c6c6f")

    def test_lengths_round_trip_masked_and_not(self):
        for n in (0, 1, 125, 126, 127, 65535, 65536, 70000):
            payload = bytes(i % 251 for i in range(n))
            for key in (None, KEY):
                frame = encode_frame(obs.OP_BINARY, payload, key)
                head = 2 + (0 if n < 126 else 2 if n < 65536 else 8) + (4 if key else 0)
                self.assertEqual(len(frame), head + n, (n, key))
                self.assertEqual(frame[1] & 0x7F, n if n < 126 else 126 if n < 65536 else 127)
                self.assertEqual(parse_frame(frame), (True, obs.OP_BINARY, payload, len(frame)))

    def test_masking_changes_the_bytes_and_unmasks(self):
        frame = encode_frame(obs.OP_TEXT, b"secret text", KEY)
        self.assertNotIn(b"secret", frame)
        self.assertEqual(parse_frame(frame)[2], b"secret text")

    def test_a_frame_that_isnt_all_there_yet_waits(self):
        frame = encode_frame(obs.OP_TEXT, b"x" * 300, KEY)
        for cut in (0, 1, 2, 3, 5, 8, len(frame) - 1):
            self.assertIsNone(parse_frame(frame[:cut]), cut)

    def test_two_frames_in_a_row(self):
        a, b = encode_frame(obs.OP_TEXT, b"one"), encode_frame(obs.OP_TEXT, b"two")
        _, _, payload, used = parse_frame(a + b)
        self.assertEqual((payload, used), (b"one", len(a)))

    def test_too_big_is_refused_before_it_arrives(self):
        head = bytes([0x81, 127]) + (2 << 20).to_bytes(8, "big")
        with self.assertRaises(ObsError):
            parse_frame(head, max_size=1 << 20)

    def test_a_control_frame_must_be_small_and_whole(self):
        with self.assertRaises(ObsError):
            parse_frame(encode_frame(obs.OP_PING, b"x" * 126))
        with self.assertRaises(ObsError):
            parse_frame(encode_frame(obs.OP_PING, b"x", fin=False))

    def test_extension_bits_are_refused(self):
        with self.assertRaises(ObsError):
            parse_frame(bytes([0xC1, 0]))


class Talking(unittest.TestCase):
    """WebSocket.recv over a real socketpair."""

    def setUp(self):
        self.near, self.far = socket.socketpair()
        self.near.settimeout(2)
        self.far.settimeout(2)
        self.ws = WebSocket(self.near, rng=lambda n: KEY[:n] if n == 4 else bytes(n))
        self.addCleanup(self.near.close)
        self.addCleanup(self.far.close)

    def feed(self, *frames):
        self.far.sendall(b"".join(frames))

    def test_a_text_message(self):
        self.feed(encode_frame(obs.OP_TEXT, "héllo".encode()))
        self.assertEqual(self.ws.recv(), "héllo")

    def test_what_we_send_is_masked(self):
        self.ws.send_text("hi")
        raw = self.far.recv(100)
        self.assertEqual(raw[0], 0x81)
        self.assertTrue(raw[1] & 0x80)
        self.assertEqual(parse_frame(raw)[2], b"hi")

    def test_a_ping_gets_its_pong_and_the_message_still_comes(self):
        self.feed(encode_frame(obs.OP_PING, b"are you there"), encode_frame(obs.OP_TEXT, b"yes"))
        self.assertEqual(self.ws.recv(), "yes")
        fin, op, payload, _ = parse_frame(self.far.recv(100))
        self.assertEqual((op, payload), (obs.OP_PONG, b"are you there"))

    def test_a_pong_is_ignored(self):
        self.feed(encode_frame(obs.OP_PONG, b"x"), encode_frame(obs.OP_TEXT, b"after"))
        self.assertEqual(self.ws.recv(), "after")

    def test_fragments_are_put_together_around_a_ping(self):
        self.feed(encode_frame(obs.OP_TEXT, b"Hel", fin=False), encode_frame(obs.OP_PING, b"p"),
                  encode_frame(obs.OP_CONT, b"l", fin=False), encode_frame(obs.OP_CONT, b"o"))
        self.assertEqual(self.ws.recv(), "Hello")

    def test_a_continuation_with_nothing_to_continue(self):
        self.feed(encode_frame(obs.OP_CONT, b"x"))
        with self.assertRaises(ObsError):
            self.ws.recv()

    def test_a_new_message_in_the_middle_of_one(self):
        self.feed(encode_frame(obs.OP_TEXT, b"a", fin=False), encode_frame(obs.OP_TEXT, b"b"))
        with self.assertRaises(ObsError):
            self.ws.recv()

    def test_a_binary_message_is_skipped(self):
        self.feed(encode_frame(obs.OP_BINARY, b"\x00\x01"), encode_frame(obs.OP_TEXT, b"text"))
        self.assertEqual(self.ws.recv(), "text")

    def test_the_cap_on_fragments_adding_up(self):
        self.ws.max_size = 100
        self.feed(encode_frame(obs.OP_TEXT, b"x" * 60, fin=False), encode_frame(obs.OP_CONT, b"y" * 60))
        with self.assertRaises(ObsError):
            self.ws.recv()

    def test_the_cap_on_one_frame(self):
        self.ws.max_size = 100
        self.feed(encode_frame(obs.OP_TEXT, b"x" * 101))
        with self.assertRaises(ObsError):
            self.ws.recv()

    def test_close_gives_its_code_and_is_answered(self):
        self.feed(encode_frame(obs.OP_CLOSE, (4009).to_bytes(2, "big") + b"bad auth"))
        with self.assertRaises(Closed) as caught:
            self.ws.recv()
        self.assertEqual((caught.exception.code, caught.exception.reason), (4009, "bad auth"))
        fin, op, payload, _ = parse_frame(self.far.recv(100))
        self.assertEqual((op, int.from_bytes(payload[:2], "big")), (obs.OP_CLOSE, 4009))

    def test_a_close_without_a_code(self):
        self.feed(encode_frame(obs.OP_CLOSE))
        with self.assertRaises(Closed) as caught:
            self.ws.recv()
        self.assertEqual(caught.exception.code, 1005)

    def test_the_connection_ending(self):
        self.far.close()
        with self.assertRaises(Closed) as caught:
            self.ws.recv()
        self.assertEqual(caught.exception.code, 1006)

    def test_a_timeout_in_the_middle_of_a_frame_loses_nothing(self):
        frame = encode_frame(obs.OP_TEXT, b"x" * 300)
        self.near.settimeout(0.05)
        self.feed(frame[:100])
        with self.assertRaises(TimeoutError):
            self.ws.recv()
        self.feed(frame[100:])
        self.near.settimeout(2)
        self.assertEqual(self.ws.recv(), "x" * 300)


# ---- the handshake ---------------------------------------------------------------------------------------

class Handshake(unittest.TestCase):
    def connect(self, script, *args, **kw):
        near, server = serve(script, *args, **kw)
        near.settimeout(3)
        self.addCleanup(near.close)
        self.addCleanup(server.finish.set)
        return near, server

    def test_the_upgrade_asks_the_right_things(self):
        near, server = self.connect(lambda s: s.upgrade())
        handshake(near, 4455)
        self.assertTrue(wait_for(lambda: hasattr(server, "request")))
        self.assertIn("Host: 127.0.0.1:4455", server.request)
        self.assertIn("Sec-WebSocket-Version: 13", server.request)
        self.assertIn("Sec-WebSocket-Protocol: obswebsocket.json", server.request)

    def test_a_wrong_accept_key_is_not_obs(self):
        near, _ = self.connect(lambda s: s.upgrade(accept="bm90IHRoZSByaWdodCBrZXk="))
        with self.assertRaises(ObsError):
            handshake(near, 4455)

    def test_some_other_service(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        far.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaises(ObsError):
            handshake(near, 4455)

    def test_bytes_that_come_with_the_headers_are_not_lost(self):
        # (OBS's hello can arrive in the same packet as the 101)
        hello = encode_frame(obs.OP_TEXT, json.dumps({"op": 0, "d": {}}).encode())
        near, _ = self.connect(lambda s: s.upgrade(extra=hello))
        self.assertEqual(json.loads(handshake(near, 4455).recv()), {"op": 0, "d": {}})

    def test_hello_without_a_password(self):
        near, server = self.connect(obs_says)
        identify(handshake(near, 4455), None)

    def test_hello_with_a_password_and_the_right_one(self):
        near, server = self.connect(obs_says, "hunter2")
        ws = handshake(near, 4455)
        identify(ws, "hunter2")

    def test_the_wrong_password_is_4009(self):
        near, server = self.connect(obs_says, "hunter2")
        ws = handshake(near, 4455)
        with self.assertRaises(WrongPassword) as caught:
            identify(ws, "nope")
        self.assertFalse(caught.exception.missing)
        self.assertEqual(str(caught.exception), "OBS didn't accept the password")

    def test_a_password_wanted_and_none_kept(self):
        near, server = self.connect(obs_says, "hunter2")
        ws = handshake(near, 4455)
        with self.assertRaises(WrongPassword) as caught:
            identify(ws, None)
        self.assertTrue(caught.exception.missing)

    def test_the_password_itself_never_goes_over_the_wire(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        far.settimeout(2)
        server = Server(far)
        server.send({"op": 0, "d": {"rpcVersion": 1, "authentication": {"challenge": CHALLENGE, "salt": SALT}}})
        server.send({"op": 2, "d": {}})
        near.settimeout(2)
        identify(WebSocket(near), "my-very-own-password")
        raw = far.recv(4096)
        self.assertNotIn(b"my-very-own-password", raw)
        self.assertNotIn(b"my-very-own-password", parse_frame(raw)[2])

    def test_something_that_doesnt_say_hello(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        near.settimeout(2)
        far.sendall(encode_frame(obs.OP_TEXT, b'{"op": 9}'))
        with self.assertRaises(ObsError):
            identify(WebSocket(near), None)


# ---- the watcher ----------------------------------------------------------------------------------------

class FakeConfig:
    def __init__(self, **obs_settings):
        self.data = {"obs": {"on": True, "port": 4455, "password": "", "when": "both", **obs_settings}}
        self.saved = 0

    def save_soon(self):
        self.saved += 1


class Watching(unittest.TestCase):
    def setUp(self):
        self.pairs: list = []          # sockets to hand out, one per connection
        self.tried: list = []
        self.changes = 0
        self.config = FakeConfig()
        self.w = ObsWatcher(self.config, self.changed, connect=self.connect, retry=0.02)
        for name, fake in (("protect", lambda s: "p:" + s), ("unprotect", lambda b: b[2:])):
            patcher = mock.patch.object(obs, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.finish)

    def finish(self):
        self.w.stop()
        if self.w._thread:
            self.w._thread.join(3)

    def changed(self):
        self.changes += 1

    def connect(self, host, port):
        self.tried.append((host, port))
        if not self.pairs:
            raise ConnectionRefusedError(10061, "refused")
        return self.pairs.pop(0)

    def obs_runs(self, script=obs_says, *args, **kw):
        near, server = serve(script, *args, **kw)
        self.pairs.append(near)
        self.addCleanup(server.finish.set)
        return server

    def test_it_only_ever_calls_this_pc(self):
        self.obs_runs()
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "connected"))
        self.assertEqual({h for h, _ in self.tried}, {"127.0.0.1"})
        self.assertEqual(self.tried[0][1], 4455)

    def test_nothing_happens_while_it_is_off(self):
        self.config.data["obs"]["on"] = False
        self.w.start()
        time.sleep(0.2)
        self.assertEqual(self.tried, [])
        self.assertEqual(self.w.describe()["status"], "off")
        self.assertFalse(self.w.live())

    def test_connected_and_the_version(self):
        self.obs_runs()
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["version"] == "30.2"))
        d = self.w.describe()
        self.assertEqual((d["status"], d["live"], d["text"]), ("connected", False, "Connected to OBS 30.2"))

    def test_already_streaming_when_we_connect(self):
        self.obs_runs(streaming=True)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        self.assertEqual(self.w.describe()["text"], "Live now")

    def test_already_recording_when_we_connect(self):
        self.obs_runs(recording=True)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        self.assertTrue(self.w.describe()["recording"])

    def test_events_start_and_end_a_stream_and_a_recording(self):
        gate, step, last = threading.Event(), threading.Event(), threading.Event()

        def play(server):
            gate.wait(3)
            server.send(event("StreamStateChanged", True))
            step.wait(3)
            server.send(event("StreamStateChanged", False))
            server.send(event("RecordStateChanged", True))
            last.wait(3)
            server.send(event("RecordStateChanged", False))

        self.obs_runs(after=play)
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "connected"))
        self.assertFalse(self.w.live())
        gate.set()
        self.assertTrue(wait_for(lambda: self.w.describe()["streaming"]))
        self.assertTrue(self.w.live())
        step.set()
        self.assertTrue(wait_for(lambda: self.w.describe()["recording"]))
        self.assertFalse(self.w.describe()["streaming"])
        self.assertTrue(self.w.live())
        last.set()
        self.assertTrue(wait_for(lambda: not self.w.live()))

    def test_which_of_the_two_counts(self):
        self.config.data["obs"]["when"] = "record"
        self.obs_runs(streaming=True)
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["streaming"]))
        self.assertFalse(self.w.live())               # a stream, but only recordings count
        self.config.data["obs"]["when"] = "stream"
        self.assertTrue(self.w.live())
        self.config.data["obs"]["when"] = "both"
        self.assertTrue(self.w.live())

    def test_a_refused_connection_is_tried_again_until_obs_is_there(self):
        self.w.start()
        self.assertTrue(wait_for(lambda: len(self.tried) >= 3))
        self.assertEqual(self.w.describe()["status"], "waiting")
        self.assertEqual(self.w.describe()["text"], "OBS isn't running, or its WebSocket server is off")
        self.obs_runs(streaming=True)               # OBS starts
        self.assertTrue(wait_for(self.w.live))
        self.assertEqual(self.w.describe()["status"], "connected")

    def test_obs_closing_ends_the_live_state_and_it_connects_again(self):
        def quit_soon(server):
            time.sleep(0.2)
            server.close(1001)

        self.obs_runs(streaming=True, after=quit_soon)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        self.assertTrue(wait_for(lambda: not self.w.live()))      # OBS went away: the stream is over for us
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "waiting"))
        self.obs_runs(recording=True)
        self.assertTrue(wait_for(self.w.live))                    # and it is back, recording this time
        self.assertGreaterEqual(len(self.tried), 2)

    def test_a_wrong_password_says_so_and_stops_trying(self):
        self.config.data["obs"]["password"] = "p:not-it"
        self.obs_runs(password="hunter2")
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "password"))
        self.assertEqual(self.w.describe()["text"], "OBS didn't accept the password")
        tries = len(self.tried)
        time.sleep(0.3)
        self.assertEqual(len(self.tried), tries)               # not hammering OBS with the same one

    def test_a_new_password_is_tried_at_once(self):
        self.config.data["obs"]["password"] = "p:not-it"
        self.obs_runs(password="hunter2")
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "password"))
        self.obs_runs(password="hunter2", streaming=True)
        self.w.set_password("hunter2")
        self.assertTrue(wait_for(self.w.live))
        self.assertEqual(self.w.describe()["status"], "connected")

    def test_a_password_wanted_and_none_saved(self):
        self.obs_runs(password="hunter2")
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "needs_password"))
        self.assertEqual(self.w.describe()["text"], "OBS needs a password")

    def test_the_right_password_gets_in(self):
        self.config.data["obs"]["password"] = "p:hunter2"
        self.obs_runs(password="hunter2", streaming=True)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))

    def test_something_else_on_the_port_is_not_obs(self):
        near, far = socket.socketpair()
        far.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
        self.pairs.append(near)
        self.addCleanup(far.close)
        self.w.start()
        self.assertTrue(wait_for(lambda: len(self.tried) >= 2))
        self.assertEqual(self.w.describe()["status"], "waiting")

    def test_switching_it_off_lets_go_of_obs(self):
        self.obs_runs(streaming=True)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        self.w.set_options(False, 4455, "both")
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "off"))
        self.assertFalse(self.w.live())

    def test_another_port_means_another_connection(self):
        self.obs_runs(streaming=True)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        self.obs_runs(recording=True)
        self.w.set_options(True, 4456, "both")
        self.assertTrue(wait_for(lambda: self.tried[-1][1] == 4456 and self.w.describe()["recording"]))
        self.assertFalse(self.w.describe()["streaming"])

    def test_changing_what_counts_keeps_the_connection(self):
        self.obs_runs(streaming=True)
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        tries = len(self.tried)
        self.w.set_options(True, 4455, "stream")
        time.sleep(0.2)
        self.assertEqual(len(self.tried), tries)
        self.assertTrue(self.w.live())

    def test_the_window_hears_about_changes(self):
        self.obs_runs(streaming=True)
        before = self.changes
        self.w.start()
        self.assertTrue(wait_for(self.w.live))
        self.assertGreater(self.changes, before)

    def test_stopping_ends_the_thread(self):
        self.obs_runs()
        self.w.start()
        self.assertTrue(wait_for(lambda: self.w.describe()["status"] == "connected"))
        self.w.stop()
        self.w._thread.join(3)
        self.assertFalse(self.w._thread.is_alive())


class TheSettings(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig(on=False)
        self.w = ObsWatcher(self.config)

    def test_the_port_is_1_to_65535(self):
        for bad in (0, -1, 65536, "abc", None, ""):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.w.set_options(True, bad, "both")
        self.w.set_options(True, "65535", "both")
        self.assertEqual(self.config.data["obs"]["port"], 65535)
        self.w.set_options(False, 1, "both")
        self.assertEqual(self.config.data["obs"]["port"], 1)

    def test_what_counts_is_one_of_three(self):
        for bad in ("", "all", None, "Stream"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.w.set_options(True, 4455, bad)
        for good in ("both", "stream", "record"):
            self.w.set_options(True, 4455, good)
            self.assertEqual(self.config.data["obs"]["when"], good)

    def test_a_refused_change_leaves_things_as_they_were(self):
        with self.assertRaises(ValueError):
            self.w.set_options(True, 99999, "both")
        self.assertFalse(self.config.data["obs"]["on"])

    def test_a_password_has_to_be_something(self):
        for bad in ("", None, 5, "x" * 300):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.w.set_password(bad)

    def test_the_description_never_holds_the_password(self):
        with mock.patch.object(obs, "protect", lambda s: "p:" + s):
            self.w.set_password("do-not-show-me")
        d = self.w.describe()
        self.assertTrue(d["hasPassword"])
        self.assertNotIn("do-not-show-me", json.dumps(d))
        self.assertNotIn("password", {k.lower() for k in d} - {"haspassword"})
        self.assertNotIn("do-not-show-me", self.w.summary())

    def test_clearing_the_password(self):
        with mock.patch.object(obs, "protect", lambda s: "p:" + s):
            self.w.set_password("x")
        self.w.clear_password()
        self.assertFalse(self.w.describe()["hasPassword"])
        self.assertEqual(self.config.data["obs"]["password"], "")

    def test_a_password_that_cant_be_opened_counts_as_none(self):
        self.config.data["obs"]["password"] = "bm90IGEgYmxvYg=="       # (a blob from another user or PC)
        with mock.patch.object(obs, "unprotect", return_value=None):
            self.assertIsNone(self.w._password())

    def test_the_status_texts(self):
        text = lambda **kw: (self.w._set(**kw), self.w.describe()["text"])[1]
        self.assertEqual(text(status="waiting"), "OBS isn't running, or its WebSocket server is off")
        self.assertEqual(text(status="password"), "OBS didn't accept the password")
        self.assertEqual(text(status="needs_password"), "OBS needs a password")
        self.assertEqual(text(status="connected", version=""), "Connected to OBS")
        self.assertEqual(text(version="30.2"), "Connected to OBS 30.2")
        self.assertEqual(text(streaming=True), "Live now")
        self.assertEqual(text(streaming=False, recording=True), "Live now")
        self.assertEqual(text(status="off", recording=False), "")

    def test_the_summary_for_the_problem_report(self):
        self.assertEqual(self.w.summary(), "off")
        self.config.data["obs"]["on"] = True
        for status, word in (("connected", "connected"), ("waiting", "not reachable"), ("password", "wrong password")):
            self.w._set(status=status)
            self.assertEqual(self.w.summary(), word)

    def test_not_live_unless_connected(self):
        self.w._set(status="waiting", streaming=True)
        self.assertFalse(self.w.live())


# ---- Windows' own protection --------------------------------------------------------------------------

@unittest.skipUnless(WINDOWS, "Windows' DPAPI")
class Protected(unittest.TestCase):
    def test_the_round_trip(self):
        for text in ("hunter2", "pässwörd with spaces", "日本語", "x" * 200):
            blob = obs.protect(text)
            self.assertNotIn(text, blob)
            self.assertEqual(obs.unprotect(blob), text)

    def test_it_is_not_just_base64_of_the_text(self):
        self.assertNotEqual(base64.b64decode(obs.protect("hunter2")), b"hunter2")
        self.assertNotIn(b"hunter2", base64.b64decode(obs.protect("hunter2")))

    def test_the_same_text_protects_differently_each_time(self):
        self.assertNotEqual(obs.protect("same"), obs.protect("same"))

    def test_junk_doesnt_open(self):
        self.assertIsNone(obs.unprotect("bm90IGEgYmxvYg=="))
        self.assertIsNone(obs.unprotect("%%% not base64 %%%"))
        self.assertIsNone(obs.unprotect(""))

    def test_a_changed_blob_doesnt_open(self):
        blob = bytearray(base64.b64decode(obs.protect("hunter2")))
        blob[-1] ^= 0xFF
        self.assertIsNone(obs.unprotect(base64.b64encode(bytes(blob)).decode()))


@unittest.skipUnless(WINDOWS, "Windows' DPAPI")
class NeverInTheFile(unittest.TestCase):
    SECRET = "correct-horse-battery-staple"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.m = DeviceManager(Config(self.path))

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def test_config_json_has_no_password_in_it(self):
        with self.assertNoLogs(level="DEBUG"):
            self.m.obs.set_password(self.SECRET)
        self.m.config.save_now()
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn(base64.b64encode(self.SECRET.encode()).decode(), text)
        blob = json.loads(text)["obs"]["password"]
        self.assertTrue(blob)
        self.assertEqual(obs.unprotect(blob), self.SECRET)

    def test_a_new_run_still_opens_it(self):
        self.m.obs.set_password(self.SECRET)
        self.m.config.save_now()
        again = DeviceManager(Config(self.path))
        self.addCleanup(again._stop.set)
        self.assertEqual(again.obs._password(), self.SECRET)
        self.assertTrue(again.obs.describe()["hasPassword"])

    def test_not_in_the_state_the_window_gets_or_the_report(self):
        self.m.obs.set_password(self.SECRET)
        self.assertNotIn(self.SECRET, json.dumps(self.m.live_status()))
        self.assertNotIn(self.SECRET, json.dumps(self.m.snapshot()))

    def test_not_in_the_problem_report(self):
        with tempfile.TemporaryDirectory() as folder:
            app = FakeApp(folder)
            try:
                app.manager.obs.set_password(self.SECRET)
                text = diagnostics.report(app)
                self.assertNotIn(self.SECRET, text)
                self.assertNotIn(app.config["obs"]["password"], text)
                self.assertIn("OBS off", text)
            finally:
                app.manager._stop.set()

    def test_an_old_config_without_obs_gets_the_defaults(self):
        self.assertEqual(self.m.config["obs"], {"on": False, "port": 4455, "password": "", "when": "both"})
        self.assertEqual(self.m.config["settings"]["auto_live"], "")


# ---- the look -----------------------------------------------------------------------------------------------

class OnAir(unittest.TestCase):
    def setUp(self):
        self.m = FakeManager()
        self.m.config.data["settings"]["auto_live"] = "preset:Focus"
        self.a = Automations(self.m)
        self.t = 0

    def run_for(self, seconds, live, **kw):
        for _ in range(seconds):
            self.a.step(self.t, self.a.rules(), kw.get("kind"), 0.0, False, frozenset(), kw.get("call", False),
                        live=live)
            self.t += 1

    def color(self):
        return self.m.look["lamp"].get("color")

    def test_the_look_comes_after_a_moment_and_goes_back_after_a_little_longer(self):
        self.run_for(1, True)
        self.assertEqual(self.color(), "#ffffff")           # not on the very first look
        self.run_for(2, True)
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "live")
        self.run_for(2, False)
        self.assertEqual(self.color(), "#eaf1ff")           # a hiccup of OBS's connection isn't the end
        self.run_for(2, False)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_the_settle_times_are_short_but_not_nothing(self):
        self.assertEqual((automations.LIVE_ON, automations.LIVE_OFF), (1.0, 3.0))

    def test_a_blink_of_a_second_doesnt_flick_the_lights(self):
        self.run_for(1, True)
        self.run_for(10, False)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_a_short_gap_doesnt_end_it(self):
        self.run_for(4, True)
        self.run_for(2, False)
        self.run_for(3, True)
        self.run_for(1, False)
        self.assertEqual(self.a.active, "live")
        self.assertEqual(self.color(), "#eaf1ff")

    def test_nothing_without_a_look_chosen(self):
        self.m.config.data["settings"]["auto_live"] = ""
        self.run_for(20, True)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_a_look_that_was_deleted_does_nothing(self):
        self.m.config.data["settings"]["auto_live"] = "preset:Gone"
        self.run_for(20, True)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_lights_that_are_off_stay_off(self):
        self.m.look["lamp"]["on"] = False
        self.run_for(10, True)
        self.assertIsNone(self.a.active)

    def test_a_change_by_hand_is_left_alone_until_it_is_over(self):
        self.run_for(4, True)
        self.m.look = {"lamp": {"on": True, "mode": "color", "color": "#123456"}}
        self.run_for(20, True)
        self.assertEqual(self.color(), "#123456")
        self.assertIsNone(self.a.active)
        self.run_for(5, False)
        self.run_for(4, True)                                # the next stream is a new chance
        self.assertEqual(self.color(), "#eaf1ff")

    def test_a_pause_keeps_it_from_starting(self):
        self.m.config.data["settings"]["auto_paused_until"] = time.time() + 3600
        self.run_for(20, True)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_a_pause_puts_the_lights_back_and_resuming_brings_the_look_again(self):
        self.run_for(4, True)
        self.assertEqual(self.color(), "#eaf1ff")
        self.m.config.data["settings"]["auto_paused_until"] = time.time() + 3600
        self.run_for(2, True)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.m.config.data["settings"]["auto_paused_until"] = 0
        self.run_for(2, True)
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "live")

    def test_it_goes_before_a_call_and_the_call_comes_after_it(self):
        """Live first: a stream is often a call too (the microphone is on), and the ON AIR look
        is what you chose for it."""
        self.m.config.data["settings"]["auto_call"] = "preset:Gaming"
        self.run_for(4, True)
        self.assertEqual(self.color(), "#eaf1ff")
        self.run_for(12, True, call=True)
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "live")
        self.run_for(12, False, call=True)
        self.assertEqual(self.color(), "#3a00ff")
        self.assertEqual(self.a.active, "call")
        self.run_for(12, False, call=False)
        self.assertEqual(self.color(), "#ffffff")
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_it_goes_before_a_video_and_music(self):
        self.m.config.data["settings"]["auto_video"] = "preset:Gaming"
        self.run_for(5, True, kind="video")
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "live")

    def test_everything_off_forgets_it(self):
        self.run_for(4, True)
        self.m.config.data["settings"]["auto_live"] = ""
        self.run_for(1, True)
        self.assertFalse(self.a._live)
        self.assertEqual(self.a._live_seen[0], False)

    def test_the_rule_is_there_for_the_loop(self):
        self.assertIn("live", automations.RULES)
        self.assertEqual(self.a.rules()["live"], "preset:Focus")
        self.assertEqual(list(self.a.rules())[0], "live")          # first: see Automations.rules
        self.assertEqual(automations.RULES[0], "live")


class TheLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def loop_args(self, turns=2):
        a = Automations(self.m)
        a._stop = FakeStop(turns)
        seen = []
        with mock.patch.object(calls, "in_use", return_value={}), \
                mock.patch.object(Automations, "step", lambda s, *args, **kw: seen.append(args)):
            a._run()
        return seen

    def test_the_loop_asks_the_watcher_when_a_look_is_chosen(self):
        self.m.config["settings"]["auto_live"] = "preset:Focus"
        with mock.patch.object(self.m.obs, "live", return_value=True) as asked:
            seen = self.loop_args()
        self.assertTrue(seen and all(args[-1] is True for args in seen))
        self.assertTrue(asked.called)

    def test_and_says_no_when_nothing_is_on_air(self):
        self.m.config["settings"]["auto_live"] = "preset:Focus"
        with mock.patch.object(self.m.obs, "live", return_value=False):
            seen = self.loop_args()
        self.assertTrue(seen and all(args[-1] is False for args in seen))

    def test_and_doesnt_ask_without_a_look(self):
        self.m.config["settings"]["auto_live"] = ""
        with mock.patch.object(self.m.obs, "live", return_value=True) as asked:
            seen = self.loop_args()
        self.assertTrue(seen and all(args[-1] is False for args in seen))
        self.assertFalse(asked.called)

    def test_nothing_runs_while_away(self):
        self.m.config["settings"]["auto_live"] = "preset:Focus"
        self.m.away.reasons.add("locked")
        self.assertEqual(self.loop_args(), [])

    def test_renaming_a_preset_updates_the_on_air_look(self):
        old = self.m.preset_names()[0]
        self.m.config["settings"]["auto_live"] = f"preset:{old}"
        self.m.rename_preset(old, "Renamed look")
        self.assertEqual(self.m.config["settings"]["auto_live"], "preset:Renamed look")

    def test_the_manager_owns_the_watcher_and_tells_the_window(self):
        self.assertIsInstance(self.m.obs, ObsWatcher)
        self.assertEqual(self.m.live_status()["obs"]["status"], "off")


# ---- the window's calls -----------------------------------------------------------------------------------

class TheApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.api = Api(SimpleNamespace(config=self.m.config, manager=self.m))
        patcher = mock.patch.object(obs, "protect", lambda s: "p:" + s)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def test_switch_port_and_choice(self):
        self.assertTrue(self.api.obs_set(True, 4456, "record")["ok"])
        s = self.m.config["obs"]
        self.assertEqual((s["on"], s["port"], s["when"]), (True, 4456, "record"))

    def test_a_port_that_cant_be_one(self):
        for bad in (0, 70000, "x"):
            r = self.api.obs_set(True, bad, "both")
            self.assertFalse(r["ok"])
            self.assertIn("1 to 65535", r["error"])

    def test_a_choice_that_isnt_one(self):
        self.assertFalse(self.api.obs_set(True, 4455, "everything")["ok"])

    def test_the_password_set_and_cleared(self):
        self.assertTrue(self.api.obs_set_password("hunter2")["ok"])
        self.assertTrue(self.m.live_status()["obs"]["hasPassword"])
        self.assertTrue(self.api.obs_clear_password()["ok"])
        self.assertFalse(self.m.live_status()["obs"]["hasPassword"])

    def test_an_empty_password_is_refused(self):
        self.assertFalse(self.api.obs_set_password("")["ok"])

    def test_the_error_never_repeats_the_password(self):
        r = self.api.obs_set_password("x" * 300)
        self.assertFalse(r["ok"])
        self.assertNotIn("xxxx", r["error"])

    def test_the_look_is_a_preset_or_nothing(self):
        name = self.m.preset_names()[0]
        self.assertTrue(self.api.set_setting("auto_live", f"preset:{name}")["ok"])
        self.assertEqual(self.m.config["settings"]["auto_live"], f"preset:{name}")
        self.assertTrue(self.api.set_setting("auto_live", "")["ok"])
        for bad in ("screen", "show", "preset:No such preset", "stuff"):
            self.assertFalse(self.api.set_setting("auto_live", bad)["ok"], bad)

    def test_the_settings_the_window_gets_hold_no_password(self):
        self.m.obs.set_password("hunter2")
        self.assertNotIn("hunter2", json.dumps(self.m.config["settings"]))
        self.assertNotIn("password", " ".join(self.m.config["settings"]))


if __name__ == "__main__":
    unittest.main()
