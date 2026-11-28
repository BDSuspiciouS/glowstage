"""SteelSeries GG's GameSense: reading where GG listens, registering and binding, the keyboard's
bitmap and the zone frames, only changed frames, the heartbeat, calls that fail, the port that
changes, and giving the devices back. Everything talks to a fake GameSense on 127.0.0.1 and to a
coreProps.json in a temp folder: never to a real GG, and never to the real coreProps.json."""

import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from glowstage.config import Config
from glowstage.devices import steelseries as ss
from glowstage.manager import DeviceManager
from glowstage.state import LightState


def wait_for(cond, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # (connections are kept open, like GG's)

    def setup(self):
        super().setup()
        self.server.clients.append(self.request)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        self.server.records.append((self.path, body))
        status = self.server.fail.get(self.path, 200)
        out = json.dumps({"error": "GAME_NOT_REGISTERED"} if status != 200 else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class FakeGameSense:
    """A GameSense server on 127.0.0.1 that records every call."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *args: None       # (closing the server cuts connections: no noise)
        self.server.records, self.server.clients, self.server.fail = [], [], {}
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def fail(self):
        return self.server.fail

    def calls(self, path=None, event=None):
        out = [(p, b) for p, b in list(self.server.records) if path is None or p == path]
        return [(p, b) for p, b in out if event is None or b.get("event") == event]

    def bodies(self, path, event=None):
        return [b for _, b in self.calls(path, event)]

    def paths(self):
        return [p for p, _ in list(self.server.records)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        for c in list(self.server.clients):      # (a GG that quits drops its connections too)
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.props = Path(self.tmp.name) / "coreProps.json"
        self.gg = FakeGameSense()
        self.point(self.gg.port)
        self.changes = 0
        self.settings = {"on": False, "types": ["keyboard", "mouse"]}
        self.saved = 0
        self.p = None
        self.extra = []

    def tearDown(self):
        if self.p:
            self.p.stop()
        self.gg.close()
        for g in self.extra:
            g.close()
        self.tmp.cleanup()

    def point(self, port):
        self.props.write_text(json.dumps({"address": f"127.0.0.1:{port}", "encryptedAddress": "x"}), encoding="utf-8")

    def provider(self, **kw):
        def changed():
            self.changes += 1

        def saved():
            self.saved += 1
        kw = {"heartbeat_every": 0.15, "retry": 0.1, **kw}
        self.p = ss.SteelSeriesProvider(self.settings, changed, saved, props_path=self.props, **kw)
        self.p.start()
        return self.p

    def switch_on(self, types=None, **kw):
        p = self.provider(**kw)
        p.set_options(True, types or self.settings["types"])
        self.assertTrue(wait_for(lambda: p.status == "connected"), p.message)
        return p

    def sent(self, event):
        return self.gg.bodies("/game_event", event)


def px(n, f=lambda i: (i % 256, (i * 3) % 256, 7)):
    return [f(i) for i in range(n)]


class Address(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.props = Path(self.tmp.name) / "coreProps.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_the_address(self):
        self.props.write_text('{"address": "127.0.0.1:51234", "encryptedAddress": "127.0.0.1:51235"}', encoding="utf-8")
        self.assertEqual(ss.GameSense(self.props).read_address(), ("127.0.0.1", 51234))

    def test_no_file_means_gg_isnt_running(self):
        with self.assertRaises(ss.GameSenseError) as cm:
            ss.GameSense(self.props).read_address()
        self.assertEqual(str(cm.exception), "SteelSeries GG isn't running")
        self.assertTrue(cm.exception.gone)

    def test_a_broken_file_is_the_same(self):
        for text in ("", "not json", "[]", "{}", '{"address": "no port"}', '{"address": "127.0.0.1:x"}'):
            self.props.write_text(text, encoding="utf-8")
            with self.assertRaises(ss.GameSenseError, msg=text) as cm:
                ss.GameSense(self.props).read_address()
            self.assertTrue(cm.exception.gone, text)

    def test_only_this_pc(self):
        for address in ("192.168.1.5:5000", "example.com:80"):
            self.props.write_text(json.dumps({"address": address}), encoding="utf-8")
            with self.assertRaises(ss.GameSenseError):
                ss.GameSense(self.props).read_address()

    def test_the_real_file_is_where_gg_keeps_it(self):
        self.assertEqual(ss.PROPS_PATH.name, "coreProps.json")
        self.assertEqual(ss.PROPS_PATH.parent.name, "SteelSeries Engine 3")


class Frames(unittest.TestCase):
    """The pure part: how pixels become GameSense data."""

    def test_the_keyboard_has_132_keys_in_6_rows_of_22(self):
        self.assertEqual((ss.COLUMNS, ss.ROWS, ss.KEYS), (22, 6, 132))
        self.assertEqual(ss.TYPES["keyboard"]["pixels"], 132)

    def test_bitmap_goes_row_after_row_while_pixels_go_left_to_right(self):
        colors = [(p, 0, 0) for p in range(132)]
        b = ss.bitmap(colors)
        self.assertEqual(len(b), 132)
        self.assertTrue(all(isinstance(e, list) and len(e) == 3 for e in b))
        self.assertEqual(b[0], [0, 0, 0])                    # row 0, column 0
        self.assertEqual(b[1], [6, 0, 0])                    # row 0, column 1: the next column's first pixel
        self.assertEqual(b[22], [1, 0, 0])                   # row 1, column 0
        self.assertEqual(b[21], [126, 0, 0])                 # row 0, last column
        self.assertEqual(b[131], [131, 0, 0])                # row 5, last column
        self.assertEqual(sorted(e[0] for e in b), list(range(132)))    # every pixel once

    def test_a_gradient_runs_left_to_right_on_the_board(self):
        colors = [(c * 11, 0, 0) for c in range(22) for _ in range(6)]       # one value per column
        b = ss.bitmap(colors)
        for r in range(6):
            self.assertEqual([b[r * 22 + c][0] for c in range(22)], [c * 11 for c in range(22)])

    def test_spread_over_zones(self):
        eight = [(i * 10, 0, 0) for i in range(8)]
        self.assertEqual(ss.spread(eight, 8), eight)
        self.assertEqual(ss.spread(eight, 1), [(35, 0, 0)])                  # the average
        self.assertEqual(ss.spread(eight, 2), [(15, 0, 0), (55, 0, 0)])
        self.assertEqual([c[0] for c in ss.spread(eight, 24)], [i // 3 * 10 for i in range(24)])   # more zones than pixels: repeated

    def test_zone_frame_keys(self):
        f = ss.zone_frame([(1, 2, 3), (4, 5, 6)], 2)
        self.assertEqual(f, {"zone2-one": {"red": 1, "green": 2, "blue": 3}, "zone2-two": {"red": 4, "green": 5, "blue": 6}})
        self.assertEqual(len(ss.zone_frame([(0, 0, 0)] * 8, 24)), 24)
        self.assertEqual(ss.ZONE_WORDS[0], "one")
        self.assertEqual(ss.ZONE_WORDS[23], "twenty-four")
        self.assertEqual(len(set(ss.ZONE_WORDS)), 24)

    def test_bindings(self):
        b = ss.bindings(["keyboard", "mouse", "headset"])
        self.assertEqual(set(b), {"BITMAP", "MOUSE", "HEADSET"})
        k = b["BITMAP"]
        self.assertEqual((k["game"], k["event"], k["value_optional"]), ("GLOWSTAGE", "BITMAP", True))
        self.assertEqual(k["handlers"], [{"device-type": "rgb-per-key-zones", "mode": "bitmap"}])
        h = b["HEADSET"]["handlers"]
        self.assertEqual(h, [{"device-type": "rgb-2-zone", "zone": "one", "mode": "context-color", "context-frame-key": "zone2-one"},
                             {"device-type": "rgb-2-zone", "zone": "two", "mode": "context-color", "context-frame-key": "zone2-two"}])
        types = {x["device-type"] for x in b["MOUSE"]["handlers"]}
        self.assertEqual(types, {"rgb-1-zone", "rgb-3-zone", "rgb-5-zone", "rgb-8-zone", "rgb-12-zone", "rgb-17-zone", "rgb-24-zone"})
        self.assertEqual(len(b["MOUSE"]["handlers"]), 1 + 3 + 5 + 8 + 12 + 17 + 24)
        self.assertTrue(all(x["mode"] == "context-color" and x["context-frame-key"] for x in b["MOUSE"]["handlers"]))

    def test_a_mouse_alone_gets_the_two_zone_type(self):
        types = {x["device-type"] for x in ss.bindings(["mouse"])["MOUSE"]["handlers"]}
        self.assertIn("rgb-2-zone", types)

    def test_clean_types(self):
        self.assertEqual(ss.clean_types(["headset", "keyboard", "nonsense", "keyboard"]), ["keyboard", "headset"])
        self.assertEqual(ss.clean_types("keyboard"), [])
        self.assertEqual(ss.clean_types(None), [])


class Switching(Base):
    def test_it_is_off_by_default_and_says_nothing(self):
        from glowstage.config import DEFAULTS
        self.assertEqual(DEFAULTS["steelseries"], {"on": False, "types": ["keyboard", "mouse"]})
        p = self.provider()
        time.sleep(0.3)
        self.assertEqual(self.gg.paths(), [])
        self.assertEqual(p.devices, {})
        self.assertEqual(p.describe()["status"], "off")

    def test_registers_and_binds(self):
        self.switch_on()
        meta = self.gg.bodies("/game_metadata")
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0], {"game": "GLOWSTAGE", "game_display_name": "Glowstage", "developer": "Glowstage",
                                   "deinitialize_timer_length_ms": 60000})
        binds = self.gg.bodies("/bind_game_event")
        self.assertEqual({b["event"] for b in binds}, {"BITMAP", "MOUSE"})
        self.assertEqual(self.gg.paths()[0], "/game_metadata")       # registered before anything is bound
        bitmap = next(b for b in binds if b["event"] == "BITMAP")
        self.assertEqual(bitmap["handlers"], [{"device-type": "rgb-per-key-zones", "mode": "bitmap"}])
        self.assertTrue(bitmap["value_optional"])

    def test_the_lights(self):
        p = self.switch_on()
        self.assertEqual(sorted(p.devices), ["steelseries:keyboard", "steelseries:mouse"])
        kb, mouse = p.devices["steelseries:keyboard"], p.devices["steelseries:mouse"]
        self.assertEqual((kb.kind, kb.group, kb.pixel_count, kb.online), ("steelseries", "pc", 132, True))
        self.assertEqual((mouse.group, mouse.pixel_count, mouse.online), ("pc", 8, True))
        self.assertEqual(kb.desk_item(), "KEYBOARD")
        self.assertEqual(mouse.desk_item(), "MOUSE")
        self.assertEqual(len(kb.fixture["geometry"]), 132)
        d = kb.describe()
        self.assertEqual((d["pixels"], d["group"], d["kind"], d["deviceType"]), (132, "pc", "steelseries", "KEYBOARD"))
        self.assertIn("SteelSeries", d["name"])

    def test_the_types_you_tick_are_the_lights_you_get(self):
        p = self.switch_on(["headset"])
        self.assertEqual(list(p.devices), ["steelseries:headset"])
        self.assertEqual(p.devices["steelseries:headset"].pixel_count, 2)
        self.assertEqual(p.devices["steelseries:headset"].desk_item(), "HEADSET")
        self.assertEqual({b["event"] for b in self.gg.bodies("/bind_game_event")}, {"HEADSET"})

    def test_settings_are_saved_and_cleaned(self):
        p = self.provider()
        p.set_options(True, ["headset", "keyboard"])
        self.assertEqual(self.settings, {"on": True, "types": ["keyboard", "headset"]})
        self.assertGreaterEqual(self.saved, 1)
        with self.assertRaises(ValueError):
            p.set_options(True, ["toaster"])
        with self.assertRaises(ValueError):
            p.set_options(True, "keyboard")
        self.assertEqual(self.settings["types"], ["keyboard", "headset"])

    def test_nothing_ticked_means_nothing_to_light(self):
        p = self.provider()
        p.set_options(True, [])
        self.assertTrue(wait_for(lambda: p.status == "idle"))
        self.assertEqual(p.devices, {})
        self.assertEqual(self.gg.paths(), [])
        self.assertIn("Pick", p.describe()["message"])

    def test_describe(self):
        p = self.switch_on()
        d = p.describe()
        self.assertEqual((d["on"], d["status"], d["types"]), (True, "connected", ["keyboard", "mouse"]))
        self.assertIn("2 lights", d["message"])
        self.assertEqual([a["id"] for a in d["available"]], ["keyboard", "mouse", "headset"])
        self.assertEqual(d["available"][0]["title"], "SteelSeries keyboard (per key)")
        self.assertEqual(p.summary(), "connected")


class Lighting(Base):
    def keyboard(self, p):
        return p.devices["steelseries:keyboard"]

    def test_the_bitmap_body(self):
        p = self.switch_on()
        colors = px(132)
        self.keyboard(p).send_frame(colors)
        self.assertTrue(wait_for(lambda: self.sent("BITMAP")))
        call = self.gg.calls("/game_event", "BITMAP")[-1][1]
        self.assertEqual(call["game"], "GLOWSTAGE")
        bitmap = call["data"]["frame"]["bitmap"]
        self.assertEqual(len(bitmap), 132)
        self.assertEqual(bitmap, ss.bitmap(colors))
        self.assertEqual(bitmap[1], list(colors[6]))                 # row 0, column 1
        self.assertEqual(bitmap[22], list(colors[1]))                # row 1, column 0

    def test_zone_bodies(self):
        p = self.switch_on(["mouse", "headset"])
        mouse, headset = p.devices["steelseries:mouse"], p.devices["steelseries:headset"]
        mouse.send_frame([(i * 30, 0, 255 - i * 30) for i in range(8)])
        headset.send_frame([(10, 20, 30), (40, 50, 60)])
        self.assertTrue(wait_for(lambda: self.sent("MOUSE") and self.sent("HEADSET")))
        frame = self.sent("MOUSE")[-1]["data"]["frame"]
        self.assertEqual(frame["zone8-three"], {"red": 60, "green": 0, "blue": 195})
        self.assertEqual(frame["zone1-one"], {"red": 105, "green": 0, "blue": 150})     # the average of all eight
        self.assertEqual(frame["zone3-one"]["red"], round((0 + 30) / 2))
        self.assertNotIn("zone2-one", frame)                                           # the headset has the 2-zone type
        keys = {k for k in frame}
        for event_body in self.gg.bodies("/bind_game_event", "MOUSE"):
            for h in event_body["handlers"]:
                self.assertIn(h["context-frame-key"], keys)                           # every handler has its color in the frame
        self.assertEqual(self.sent("HEADSET")[-1]["data"]["frame"],
                         {"zone2-one": {"red": 10, "green": 20, "blue": 30}, "zone2-two": {"red": 40, "green": 50, "blue": 60}})

    def test_a_solid_color_fills_the_keyboard(self):
        p = self.switch_on()
        kb = self.keyboard(p)
        kb.apply(LightState(on=True, brightness=50, color="#ff0000"))
        self.assertTrue(wait_for(lambda: self.sent("BITMAP")))
        self.assertEqual(set(map(tuple, self.sent("BITMAP")[-1]["data"]["frame"]["bitmap"])), {(128, 0, 0)})
        kb.apply(LightState(on=False))
        self.assertTrue(wait_for(lambda: len(self.sent("BITMAP")) == 2))
        self.assertEqual(set(map(tuple, self.sent("BITMAP")[-1]["data"]["frame"]["bitmap"])), {(0, 0, 0)})

    def test_only_changed_frames_are_sent(self):
        p = self.switch_on()
        kb = self.keyboard(p)
        a, b = px(132), px(132, lambda i: (9, i, 0))
        for _ in range(5):
            kb.send_frame(a)
            time.sleep(0.03)
        kb.send_frame(b)
        self.assertTrue(wait_for(lambda: len(self.sent("BITMAP")) == 2))
        time.sleep(0.2)
        self.assertEqual(len(self.sent("BITMAP")), 2)
        kb.send_frame(a)
        self.assertTrue(wait_for(lambda: len(self.sent("BITMAP")) == 3))

    def test_only_the_newest_frame_goes_when_they_come_faster_than_they_leave(self):
        p = self.switch_on()
        kb = self.keyboard(p)
        for i in range(50):
            kb.send_frame([(i, 0, 0)] * 132)
        self.assertTrue(wait_for(lambda: kb.wait_written(0.05)))
        self.assertEqual(self.sent("BITMAP")[-1]["data"]["frame"]["bitmap"][0], [49, 0, 0])

    def test_a_frame_of_the_wrong_size_is_ignored(self):
        p = self.switch_on()
        self.keyboard(p).send_frame([(1, 1, 1)] * 10)
        time.sleep(0.2)
        self.assertEqual(self.sent("BITMAP"), [])

    def test_the_heartbeat(self):
        self.switch_on()
        self.assertTrue(wait_for(lambda: len(self.gg.bodies("/game_heartbeat")) >= 3))
        self.assertEqual(self.gg.bodies("/game_heartbeat")[0], {"game": "GLOWSTAGE"})

    def test_events_count_as_a_heartbeat(self):
        p = self.switch_on(heartbeat_every=0.4)
        kb = self.keyboard(p)
        for i in range(25):                      # a second of changing frames
            kb.send_frame([(i, 0, 0)] * 132)
            time.sleep(0.04)
        self.assertLessEqual(len(self.gg.bodies("/game_heartbeat")), 1)

    def test_the_real_heartbeat_is_ten_seconds(self):
        self.assertEqual(ss.HEARTBEAT_EVERY, 10.0)
        self.assertLessEqual(ss.HEARTBEAT_EVERY * 1000, ss.DEINITIALIZE_MS)


class Trouble(Base):
    def test_no_coreprops_means_gg_isnt_running(self):
        self.props.unlink()
        p = self.provider()
        p.set_options(True, ["keyboard", "mouse"])
        self.assertTrue(wait_for(lambda: p.status == "waiting"))
        self.assertEqual(p.message, "SteelSeries GG isn't running")
        for dev in p.devices.values():
            self.assertFalse(dev.online)
            self.assertEqual(dev.error, "SteelSeries GG isn't running")
        self.assertEqual(self.gg.paths(), [])

    def test_a_refused_connection_means_gg_isnt_running(self):
        self.point(free_port())                       # a file from the last start, nothing listening
        p = self.provider()
        p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: p.status == "waiting"))
        self.assertEqual(p.devices["steelseries:keyboard"].error, "SteelSeries GG isn't running")
        self.assertEqual(p.summary(), "SteelSeries GG isn't running")

    def test_the_lights_come_on_when_gg_starts_later(self):
        self.props.unlink()
        p = self.provider()
        p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: p.status == "waiting"))
        self.point(self.gg.port)
        self.assertTrue(wait_for(lambda: p.status == "connected"))
        kb = p.devices["steelseries:keyboard"]
        self.assertTrue(kb.online)
        self.assertIsNone(kb.error)

    def test_a_refused_call_is_a_plain_reason(self):
        p = self.switch_on(["keyboard"])
        self.gg.fail["/game_event"] = 400
        p.devices["steelseries:keyboard"].send_frame(px(132))
        self.assertTrue(wait_for(lambda: not p.devices["steelseries:keyboard"].online))
        err = p.devices["steelseries:keyboard"].error
        self.assertIn("SteelSeries GG said no (400)", err)
        self.assertIn("GAME_NOT_REGISTERED", err)
        self.assertEqual(p.status, "error")

    def test_a_refused_registration_is_a_plain_reason(self):
        self.gg.fail["/game_metadata"] = 500
        p = self.provider()
        p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: p.status == "error"))
        self.assertIn("said no (500)", p.message)
        self.assertFalse(p.devices["steelseries:keyboard"].online)
        self.gg.fail.clear()                          # and it tries again by itself
        self.assertTrue(wait_for(lambda: p.status == "connected"))

    def test_gg_restarting_on_the_same_port_registers_again(self):
        p = self.switch_on(["keyboard"])
        kb = p.devices["steelseries:keyboard"]
        self.gg.fail["/game_event"] = 400
        kb.send_frame(px(132))
        self.assertTrue(wait_for(lambda: not kb.online))
        self.gg.fail.clear()
        self.assertTrue(wait_for(lambda: kb.online))
        self.assertEqual(len(self.gg.bodies("/game_metadata")), 2)
        self.assertEqual(len(self.gg.bodies("/bind_game_event")), 2)

    def test_the_port_changes_when_gg_restarts(self):
        p = self.switch_on(["keyboard"], heartbeat_every=60)         # (a quiet connection: the next frame is what finds out)
        kb = p.devices["steelseries:keyboard"]
        kb.send_frame(px(132))
        self.assertTrue(wait_for(lambda: self.sent("BITMAP")))
        self.gg.close()                               # GG quits ...
        again = FakeGameSense()                       # ... and starts again on another port
        self.extra.append(again)
        self.point(again.port)
        kb.send_frame(px(132, lambda i: (1, 2, 3)))
        self.assertTrue(wait_for(lambda: again.bodies("/game_event", "BITMAP")), "the new port wasn't used")
        self.assertEqual(len(again.bodies("/game_metadata")), 1)                 # registered anew
        self.assertEqual(len(again.bodies("/bind_game_event")), 1)               # bound anew
        self.assertTrue(wait_for(lambda: kb.online))
        self.assertIsNone(kb.error)
        self.assertEqual(again.bodies("/game_event", "BITMAP")[-1]["data"]["frame"]["bitmap"][5], [1, 2, 3])

    def test_the_port_changes_while_an_effect_keeps_streaming(self):
        p = self.switch_on(["keyboard"])              # (heartbeats and frames are in flight when GG goes)
        kb = p.devices["steelseries:keyboard"]
        kb.send_frame(px(132))
        self.assertTrue(wait_for(lambda: self.sent("BITMAP")))
        self.gg.close()
        again = FakeGameSense()
        self.extra.append(again)
        self.assertTrue(wait_for(lambda: not kb.online))     # (GG is gone for a moment: the light says so)
        self.point(again.port)

        def stream():                                 # (an effect sends a frame whenever the light is answering)
            kb.send_frame(px(132, lambda i: (4, 5, 6)))
            return again.bodies("/game_event", "BITMAP")
        self.assertTrue(wait_for(stream, 5), "the new port wasn't used")
        self.assertTrue(wait_for(lambda: kb.online))
        self.assertEqual(len(again.bodies("/game_metadata")), 1)

    def test_the_client_reads_coreprops_again_after_a_failure(self):
        client = ss.GameSense(self.props)
        self.addCleanup(client.close)
        client.post("/game_heartbeat", {"game": "GLOWSTAGE"})
        self.assertEqual(len(self.gg.bodies("/game_heartbeat")), 1)
        self.gg.close()
        other = FakeGameSense()
        self.extra.append(other)
        self.point(other.port)
        with self.assertRaises(ss.GameSenseRestarted):              # the old port fails, the new one answers: GG started again
            client.post("/game_heartbeat", {"game": "GLOWSTAGE"})
        self.assertEqual(len(other.bodies("/game_heartbeat")), 1)
        client.post("/game_heartbeat", {"game": "GLOWSTAGE"})       # (and from then on it is just the new port)
        self.assertEqual(len(other.bodies("/game_heartbeat")), 2)

    def test_a_dead_gg_gives_an_error_not_a_hang(self):
        client = ss.GameSense(self.props, timeout=0.5)
        self.addCleanup(client.close)
        self.gg.close()
        t = time.monotonic()
        with self.assertRaises(ss.GameSenseError) as cm:
            client.post("/game_heartbeat", {"game": "GLOWSTAGE"})
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(str(cm.exception), "SteelSeries GG isn't running")

    def test_a_gg_that_doesnt_answer(self):
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(5)                              # accepts connections (the OS does) and never answers
        self.addCleanup(silent.close)
        self.point(silent.getsockname()[1])
        client = ss.GameSense(self.props, timeout=0.2)
        self.addCleanup(client.close)
        with self.assertRaises(ss.GameSenseError) as cm:
            client.post("/game_heartbeat", {"game": "GLOWSTAGE"})
        self.assertEqual(str(cm.exception), "SteelSeries GG isn't answering")
        self.assertFalse(cm.exception.gone)

    def test_only_this_pc_is_ever_called(self):
        self.props.write_text(json.dumps({"address": f"192.0.2.7:{self.gg.port}"}), encoding="utf-8")
        p = self.provider()
        p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: p.status == "error"))
        self.assertIn("isn't on this PC", p.message)
        self.assertEqual(self.gg.paths(), [])


class GivingBack(Base):
    def test_switching_off_stops_and_removes_the_game(self):
        p = self.switch_on()
        self.assertEqual(sorted(p.devices), ["steelseries:keyboard", "steelseries:mouse"])
        kb = p.devices["steelseries:keyboard"]
        p.set_options(False, ["keyboard", "mouse"])
        self.assertTrue(wait_for(lambda: "/remove_game" in self.gg.paths()))
        paths = self.gg.paths()
        self.assertLess(paths.index("/stop_game"), paths.index("/remove_game"))
        self.assertEqual(self.gg.bodies("/stop_game"), [{"game": "GLOWSTAGE"}])
        self.assertEqual(self.gg.bodies("/remove_game"), [{"game": "GLOWSTAGE"}])
        self.assertTrue(wait_for(lambda: p.devices == {}))
        self.assertFalse(kb.online)
        self.assertEqual(p.describe()["status"], "off")
        n = len(self.gg.paths())
        time.sleep(0.4)
        self.assertEqual(len(self.gg.paths()), n, "still talking to GG after it was switched off")

    def test_switching_on_again_registers_again_with_the_same_lights(self):
        p = self.switch_on()
        kb = p.devices["steelseries:keyboard"]
        p.set_options(False, ["keyboard", "mouse"])
        self.assertTrue(wait_for(lambda: p.devices == {}))
        p.set_options(True, ["keyboard", "mouse"])
        self.assertTrue(wait_for(lambda: p.status == "connected" and p.devices))
        self.assertIs(p.devices["steelseries:keyboard"], kb)
        self.assertEqual(len(self.gg.bodies("/game_metadata")), 2)
        kb.send_frame(px(132))
        self.assertTrue(wait_for(lambda: self.sent("BITMAP")))

    def test_quitting_gives_the_devices_back_too(self):
        p = self.switch_on()
        p.stop()
        paths = self.gg.paths()
        self.assertIn("/stop_game", paths)
        self.assertEqual(paths[-1], "/remove_game")
        self.assertEqual(self.settings["on"], True)       # (quitting isn't switching it off: it is on next time)

    def test_quitting_while_off_says_nothing(self):
        p = self.provider()
        p.stop()
        self.assertEqual(self.gg.paths(), [])

    def test_quitting_with_gg_gone_doesnt_hang(self):
        p = self.switch_on()
        self.gg.close()
        t = time.monotonic()
        p.stop()
        self.assertLess(time.monotonic() - t, 4)

    def test_unticking_a_type_removes_its_binding_only(self):
        p = self.switch_on()
        before = len(self.gg.bodies("/game_metadata"))
        p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: list(p.devices) == ["steelseries:keyboard"]))
        self.assertTrue(wait_for(lambda: self.gg.bodies("/remove_game_event")))
        self.assertEqual(self.gg.bodies("/remove_game_event"), [{"game": "GLOWSTAGE", "event": "MOUSE"}])
        self.assertEqual(len(self.gg.bodies("/game_metadata")), before)       # not registered again
        self.assertNotIn("/stop_game", self.gg.paths())                       # the keyboard keeps its lighting

    def test_ticking_the_headset_moves_the_two_zone_type_to_it(self):
        p = self.switch_on(["mouse"])
        first = {h["device-type"] for h in self.gg.bodies("/bind_game_event", "MOUSE")[-1]["handlers"]}
        self.assertIn("rgb-2-zone", first)
        p.set_options(True, ["mouse", "headset"])
        self.assertTrue(wait_for(lambda: "steelseries:headset" in p.devices and self.gg.bodies("/bind_game_event", "HEADSET")))
        again = {h["device-type"] for h in self.gg.bodies("/bind_game_event", "MOUSE")[-1]["handlers"]}
        self.assertNotIn("rgb-2-zone", again)


class InTheApp(unittest.TestCase):
    """The manager gets the lights when it is switched on, and loses them when it is off."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gg = FakeGameSense()
        self.props = Path(self.tmp.name) / "coreProps.json"
        self.props.write_text(json.dumps({"address": f"127.0.0.1:{self.gg.port}"}), encoding="utf-8")
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.p = self.m.steelseries
        self.p.client.props_path = self.props
        self.p.heartbeat_every, self.p.retry = 0.2, 0.1
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.gg.close()
        self.m._stop.set()
        self.tmp.cleanup()

    def test_it_is_off_and_has_no_lights(self):
        self.assertEqual(self.m.config["steelseries"], {"on": False, "types": ["keyboard", "mouse"]})
        self.assertFalse([d for d in self.m.devices.values() if d.kind == "steelseries"])
        self.assertFalse(self.m.live_status()["steelseries"]["on"])

    def test_the_lights_appear_take_a_color_and_go_away(self):
        self.p.set_options(True, ["keyboard", "mouse"])
        self.assertTrue(wait_for(lambda: self.m.devices.get("steelseries:keyboard") is not None
                                 and self.m.devices["steelseries:keyboard"].online))
        self.assertIn("steelseries:mouse", self.m.devices)
        self.m.update(["steelseries:keyboard"], {"color": "#00ff00", "brightness": 100, "mode": "color", "on": True})
        self.assertTrue(wait_for(lambda: self.gg.bodies("/game_event", "BITMAP")))
        self.assertTrue(wait_for(lambda: set(map(tuple, self.gg.bodies("/game_event", "BITMAP")[-1]["data"]["frame"]["bitmap"])) == {(0, 255, 0)}))
        info = {d["id"]: d for d in self.m.snapshot()["devices"]}["steelseries:keyboard"]
        self.assertEqual((info["kind"], info["group"], info["pixels"]), ("steelseries", "pc", 132))
        self.p.set_options(False, ["keyboard", "mouse"])
        self.assertTrue(wait_for(lambda: "steelseries:keyboard" not in self.m.devices))
        self.assertIn("/remove_game", self.gg.paths())
        self.p.set_options(True, ["keyboard", "mouse"])           # back on: the look it had comes back
        self.assertTrue(wait_for(lambda: self.m.devices.get("steelseries:keyboard") is not None
                                 and self.m.devices["steelseries:keyboard"].online))
        self.assertTrue(wait_for(lambda: len(self.gg.bodies("/game_event", "BITMAP")) >= 2))
        self.assertEqual(set(map(tuple, self.gg.bodies("/game_event", "BITMAP")[-1]["data"]["frame"]["bitmap"])), {(0, 255, 0)})

    def test_an_effect_streams_frames(self):
        self.p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: self.m.devices.get("steelseries:keyboard") is not None
                                 and self.m.devices["steelseries:keyboard"].online))
        kb = self.m.devices["steelseries:keyboard"]
        kb.apply(LightState(mode="effect", effect="rainbow"))
        self.assertTrue(kb.streaming)
        kb.send_frame([(i, 0, 0) for i in range(132)])
        self.assertTrue(wait_for(lambda: self.gg.bodies("/game_event", "BITMAP")))

    def test_the_report_says_how_it_is(self):
        self.assertEqual(self.p.summary(), "off")
        self.p.set_options(True, ["keyboard"])
        self.assertTrue(wait_for(lambda: self.p.status == "connected"))
        self.assertEqual(self.p.summary(), "connected")


if __name__ == "__main__":
    unittest.main()
