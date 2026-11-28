"""Philips Hue driver against a fake bridge on 127.0.0.1 (no real network)."""

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from glowstage.devices import hue
from glowstage.devices.hub import NotPairedError
from glowstage.state import LightState

TOKEN = "abc123token"
GAMUT_C = [[0.6915, 0.3083], [0.17, 0.7], [0.1532, 0.0475]]

LIGHTS = {
    "1": {"name": "Desk", "type": "Extended color light", "modelid": "LCT015",
          "uniqueid": "00:17:88:01:00:AA:BB:CC-0B",
          "state": {"on": True, "bri": 200, "xy": [0.3, 0.3], "ct": 300, "colormode": "xy", "reachable": True},
          "capabilities": {"control": {"colorgamut": GAMUT_C, "colorgamuttype": "C"}}},
    "2": {"name": "Ceiling", "type": "Color temperature light", "modelid": "LTW001",
          "uniqueid": "00:17:88:01:00:DD:EE:FF-0B",
          "state": {"on": False, "bri": 100, "ct": 400, "colormode": "ct", "reachable": True}},
    "3": {"name": "Off at wall", "type": "Dimmable light", "modelid": "LWB010",
          "uniqueid": "",
          "state": {"on": False, "bri": 1, "reachable": False}},
}


class FakeBridge:
    def __init__(self):
        self.lights = json.loads(json.dumps(LIGHTS))
        self.link_button = True
        self.requests: list[tuple[str, str, object, float]] = []
        self.connections = 0
        self.lock = threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                with bridge.lock:
                    bridge.connections += 1

            def _reply(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _handle(self, method):
                payload = self._body()
                with bridge.lock:
                    bridge.requests.append((method, self.path, payload, time.monotonic()))
                parts = self.path.strip("/").split("/")          # api, <user>, lights, <id>, state
                if method == "GET" and self.path == "/api/config":
                    return self._reply({"name": "Hue Test", "bridgeid": "001788FFFE123456",
                                        "modelid": "BSB002", "apiversion": "1.50.0"})
                if method == "POST" and self.path == "/api":
                    if not bridge.link_button:
                        return self._reply([{"error": {"type": 101, "address": "", "description": "link button not pressed"}}])
                    return self._reply([{"success": {"username": TOKEN}}])
                if parts[0] != "api" or len(parts) < 3 or parts[1] != TOKEN:
                    return self._reply([{"error": {"type": 1, "address": "/", "description": "unauthorized user"}}])
                if method == "GET" and parts[2:] == ["lights"]:
                    return self._reply(bridge.lights)
                if method == "GET" and len(parts) == 4 and parts[3] in bridge.lights:
                    return self._reply(bridge.lights[parts[3]])
                if method == "PUT" and len(parts) == 5 and parts[4] == "state" and parts[3] in bridge.lights:
                    return self._reply([{"success": {f"/lights/{parts[3]}/state/{k}": v}} for k, v in payload.items()])
                return self._reply([{"error": {"type": 3, "address": self.path, "description": "resource not available"}}])

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def puts(self, light_id=None):
        with self.lock:
            return [(p, body, t) for m, p, body, t in self.requests
                    if m == "PUT" and (light_id is None or p.endswith(f"/lights/{light_id}/state"))]


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class ColorTests(unittest.TestCase):
    def assertXY(self, rgb, x, y):
        got = hue.rgb_to_xy(rgb)
        self.assertAlmostEqual(got[0], x, delta=0.001)
        self.assertAlmostEqual(got[1], y, delta=0.001)

    def test_primaries_and_white(self):
        self.assertXY((255, 0, 0), 0.7006, 0.2993)
        self.assertXY((0, 255, 0), 0.1724, 0.7468)
        self.assertXY((0, 0, 255), 0.1355, 0.0399)
        self.assertXY((255, 255, 255), 0.3227, 0.3290)

    def test_black_gives_white_point(self):
        self.assertEqual(hue.rgb_to_xy((0, 0, 0)), hue.WHITE_XY)

    def test_gamut_clamp(self):
        g = hue.GAMUTS["C"]
        inside = (0.3, 0.3)
        self.assertEqual(hue.clamp_to_gamut(inside, g), inside)
        out = hue.clamp_to_gamut((0.7006, 0.2993), g)      # pure red is outside gamut C
        self.assertNotEqual(out, (0.7006, 0.2993))
        self.assertTrue(hue._in_gamut((out[0] + (0.3 - out[0]) * 1e-6, out[1] + (0.3 - out[1]) * 1e-6), g))
        self.assertAlmostEqual(out[0], 0.6915, delta=0.01)
        self.assertAlmostEqual(out[1], 0.3083, delta=0.01)
        far = hue.clamp_to_gamut((0.9, 0.9), g)
        self.assertTrue(hue._in_gamut((far[0] * 0.999 + 0.0003, far[1] * 0.999 + 0.0003), g))

    def test_rgb_to_xy_applies_gamut(self):
        x, y = hue.rgb_to_xy((255, 0, 0), hue.GAMUTS["C"])
        self.assertAlmostEqual(x, 0.6915, delta=0.01)

    def test_xy_roundtrip_hue(self):
        r, g, b = hue.xy_to_rgb(*hue.rgb_to_xy((0, 255, 0)))
        self.assertGreater(g, 240)
        self.assertLess(r, 40)


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBridge()
        self.addCleanup(self.fake.stop)
        self.driver = hue.Driver()
        self.hubs = []

    def make_hub(self, token=TOKEN, min_interval=0.05):
        entry = {"kind": "hue", "host": "127.0.0.1", "port": self.fake.port, "label": "Hue Test",
                 "bridgeid": "001788fffe123456"}
        if token:
            entry["token"] = token
        hub = hue.HueBridge("hue:001788fffe123456", entry, min_interval=min_interval)
        self.hubs.append(hub)
        self.addCleanup(hub.close)
        return hub

    def connected(self, **kw):
        hub = self.make_hub(**kw)
        hub.connect()
        for dev in hub.devices.values():
            try:
                dev.connect()
            except RuntimeError:
                pass
        return hub

    def light(self, hub, light_id):
        return next(d for d in hub.devices.values() if d.light_id == light_id)


class DriverTests(BridgeCase):
    def test_describe(self):
        d = self.driver.describe()
        self.assertEqual((d["kind"], d["title"], d["hub"], d["beta"]), ("hue", "Philips Hue", True, True))
        self.assertEqual(self.driver.mdns, ("_hue._tcp.local.",))

    def test_from_mdns(self):
        f = self.driver.from_mdns("_hue._tcp.local.", "Hue Bridge - 123456._hue._tcp.local.", "10.0.0.5", 80,
                                  {"bridgeid": "001788FFFE123456", "modelid": "BSB002"})
        self.assertEqual(f.dev_id, "hue:001788fffe123456")
        self.assertEqual(f.host, "10.0.0.5")
        self.assertEqual(f.extra["bridgeid"], "001788fffe123456")
        self.assertIsNone(self.driver.from_mdns("_hue._tcp.local.", "x", "10.0.0.5", 80, {}))

    def test_probe(self):
        f = self.driver.probe({"host": f"127.0.0.1:{self.fake.port}"})
        self.assertEqual(f.kind, "hue")
        self.assertEqual(f.dev_id, "hue:001788fffe123456")
        self.assertEqual(f.port, self.fake.port)
        self.assertEqual(f.label, "Hue Test")
        self.assertEqual(f.extra["bridgeid"], "001788fffe123456")

    def test_probe_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(ValueError) as cm:
            self.driver.probe({"host": f"127.0.0.1:{port}"})
        self.assertIn("No Hue bridge", str(cm.exception))

    def test_probe_not_a_bridge(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (BaseHTTPRequestHandler,), {
            "log_message": lambda *a: None,
            "do_GET": lambda self: (self.send_response(200), self.send_header("Content-Length", "2"),
                                    self.end_headers(), self.wfile.write(b"{}"))}))
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        try:
            with self.assertRaises(ValueError):
                self.driver.probe({"host": f"127.0.0.1:{srv.server_address[1]}"})
        finally:
            srv.shutdown()
            srv.server_close()

    def test_create_does_no_io(self):
        hub = self.driver.create("hue:x", {"host": "203.0.113.1", "port": 80, "token": "t"})
        self.assertIsInstance(hub, hue.HueBridge)
        self.assertTrue(hub.paired)
        hub.close()

    def test_parse_ssdp(self):
        self.assertIsInstance(hue.parse_ssdp(b"HTTP/1.1 200 OK\r\nHUE-BRIDGEID: ABC\r\n\r\n", "10.0.0.9"),
                              hue.registry.Found)
        self.assertIsNone(hue.parse_ssdp(b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n", "10.0.0.9"))


class PairTests(BridgeCase):
    def test_pair_success(self):
        hub = self.make_hub(token=None)
        self.assertFalse(hub.paired)
        self.assertEqual(self.driver.pair(hub), {"token": TOKEN})
        self.assertTrue(hub.paired)
        sent = [b for m, p, b, t in self.fake.requests if m == "POST"]
        self.assertEqual(sent, [{"devicetype": "glowstage#pc"}])

    def test_pair_link_button_not_pressed(self):
        self.fake.link_button = False
        hub = self.make_hub(token=None)
        with self.assertRaises(NotPairedError) as cm:
            self.driver.pair(hub)
        self.assertEqual(str(cm.exception), "Press the link button on the bridge first, then Pair again within 30 seconds.")
        self.assertFalse(hub.paired)


class ConnectTests(BridgeCase):
    def test_not_paired(self):
        hub = self.make_hub(token=None)
        with self.assertRaises(NotPairedError):
            hub.connect()
        self.assertFalse(hub.online)

    def test_unauthorized(self):
        hub = self.make_hub(token="wrong")
        with self.assertRaises(NotPairedError) as cm:
            hub.connect()
        self.assertEqual(str(cm.exception), "The bridge forgot this PC: pair again.")

    def test_lights_listed(self):
        hub = self.make_hub()
        hub.connect()
        self.assertTrue(hub.online)
        self.assertEqual(sorted(hub.devices), [
            "hue:001788fffe123456:00:17:88:01:00:aa:bb:cc-0b",
            "hue:001788fffe123456:00:17:88:01:00:dd:ee:ff-0b",
            "hue:001788fffe123456:3",
        ])
        desk = self.light(hub, "1")
        self.assertTrue(desk.has_color)
        self.assertEqual(desk.gamut, hue.GAMUTS["C"])
        self.assertFalse(self.light(hub, "2").has_color)
        before = dict(hub.devices)
        hub.connect()                                   # reconnect keeps the same Device objects
        self.assertEqual({k: id(v) for k, v in before.items()}, {k: id(v) for k, v in hub.devices.items()})

    def test_light_connect(self):
        hub = self.make_hub()
        hub.connect()
        desk = self.light(hub, "1")
        desk.connect()
        self.assertTrue(desk.online)
        self.assertIsNone(desk.error)
        self.assertEqual(desk.pixel_count, 1)
        self.assertEqual(desk.reported_name, "Desk")
        info = desk.extra_info()
        self.assertEqual(info["host"], "127.0.0.1")
        self.assertEqual(info["label"], "Philips Hue · LCT015")
        with self.assertRaises(RuntimeError):
            self.light(hub, "3").connect()
        self.assertFalse(self.light(hub, "3").online)

    def test_light_connect_unauthorized(self):
        hub = self.make_hub()
        hub.connect()
        hub.token = "revoked"
        with self.assertRaises(NotPairedError):
            self.light(hub, "1").connect()


class ControlTests(BridgeCase):
    def test_show_color_body(self):
        hub = self.connected()
        desk = self.light(hub, "1")
        desk.moment = 0.1                   # (a glide of a tenth of a second: transitiontime 1)
        desk.show_color((255, 0, 0), LightState(brightness=100))
        path, body, _ = self.fake.puts("1")[-1]
        self.assertEqual(path, f"/api/{TOKEN}/lights/1/state")
        self.assertEqual(body["on"], True)
        self.assertEqual(body["bri"], 254)
        self.assertEqual(body["transitiontime"], 1)
        self.assertEqual(body["xy"], list(hue.rgb_to_xy((255, 0, 0), hue.GAMUTS["C"])))
        desk.show_color((128, 0, 0), LightState(brightness=50))
        body = self.fake.puts("1")[-1][1]
        self.assertEqual(body["bri"], round(254 * 0.5 * 128 / 255))

    def test_show_color_white_is_inside_gamut(self):
        hub = self.connected()
        self.light(hub, "1").show_color((255, 255, 255), LightState(brightness=100))
        x, y = self.fake.puts("1")[-1][1]["xy"]
        self.assertAlmostEqual(x, 0.3227, delta=0.001)
        self.assertAlmostEqual(y, 0.3290, delta=0.001)

    def test_black_turns_off(self):
        hub = self.connected()
        self.light(hub, "1").show_color((0, 0, 0), LightState(brightness=100))
        body = self.fake.puts("1")[-1][1]
        self.assertEqual(body["on"], False)
        self.assertNotIn("xy", body)

    def test_ambiance_light_gets_brightness_only(self):
        hub = self.connected()
        self.light(hub, "2").show_color((0, 255, 0), LightState(brightness=100))
        body = self.fake.puts("2")[-1][1]
        self.assertEqual(body["on"], True)
        self.assertEqual(body["bri"], 254)
        self.assertNotIn("xy", body)

    def test_power_off(self):
        hub = self.connected()
        self.light(hub, "1").power_off(timeout=1.0)
        self.assertEqual(self.fake.puts("1")[-1][1], {"on": False, "transitiontime": 4})   # ("smooth": 0.35 s)

    def test_apply_uses_state(self):
        hub = self.connected()
        desk = self.light(hub, "1")
        desk.apply(LightState(on=True, brightness=100, color="#00ff00"))
        self.assertEqual(self.fake.puts("1")[-1][1]["on"], True)
        desk.apply(LightState(on=False))
        self.assertEqual(self.fake.puts("1")[-1][1], {"on": False, "transitiontime": 4})

    def test_read_state(self):
        hub = self.connected()
        st = self.light(hub, "1").read_state()
        self.assertEqual(st["on"], True)
        self.assertIn("color", st)
        self.assertEqual(st["brightness"], round(200 / 2.54))


class StreamTests(BridgeCase):
    def test_collapses_to_latest_and_rate_limits(self):
        hub = self.connected(min_interval=0.05)
        desk = self.light(hub, "1")
        desk.apply(LightState(mode="effect", brightness=100))
        self.assertEqual(self.fake.puts("1")[-1][1], {"on": True, "transitiontime": 1})
        before = len(self.fake.puts("1"))
        colors = [(i * 10, 0, 255 - i * 10) for i in range(1, 25)]
        t0 = time.monotonic()
        for c in colors:
            desk.send_frame([c])          # returns immediately, collapses to the latest
        self.assertLess(time.monotonic() - t0, 0.05)
        final = colors[-1]
        expected = desk._body(final, 1.0)
        self.assertTrue(wait_for(lambda: len(self.fake.puts("1")) > before and
                                 self.fake.puts("1")[-1][1] == expected))
        sent = self.fake.puts("1")[before:]
        self.assertLess(len(sent), len(colors))          # frames were dropped, not queued
        stamps = [t for _, _, t in sent]
        for a, b in zip(stamps, stamps[1:]):
            self.assertGreaterEqual(b - a, 0.05 * 0.7)

    def test_average_of_frame_colors(self):
        hub = self.connected()
        desk = self.light(hub, "1")
        desk.send_frame([(255, 0, 0), (0, 0, 255)])
        expected = desk._body((128, 0, 128), 1.0)
        self.assertTrue(wait_for(lambda: self.fake.puts("1") and self.fake.puts("1")[-1][1] == expected))

    def test_identical_frames_not_resent(self):
        hub = self.connected(min_interval=0.01)
        desk = self.light(hub, "1")
        desk.send_frame([(0, 200, 0)])
        self.assertTrue(wait_for(lambda: len(self.fake.puts("1")) == 1))
        for _ in range(5):
            desk.send_frame([(0, 200, 0)])
            time.sleep(0.03)
        self.assertEqual(len(self.fake.puts("1")), 1)

    def test_round_robin_and_one_connection(self):
        hub = self.connected(min_interval=0.03)
        desk, ceiling = self.light(hub, "1"), self.light(hub, "2")
        for i in range(6):
            desk.send_frame([(255, i * 20, 0)])
            ceiling.send_frame([(0, 255, i * 20)])
            time.sleep(0.01)
        want_desk, want_ceil = desk._body((255, 100, 0), 1.0), ceiling._body((0, 255, 100), 1.0)
        self.assertTrue(wait_for(lambda: self.fake.puts("1") and self.fake.puts("1")[-1][1] == want_desk and
                                 self.fake.puts("2") and self.fake.puts("2")[-1][1] == want_ceil))
        order = [p.split("/")[-2] for p, _, _ in self.fake.puts()]
        # consecutive commands alternate between lights while both have changes pending
        self.assertGreaterEqual(len(set(order[:4])), 2)
        stamps = [t for _, _, t in self.fake.puts()]
        for a, b in zip(stamps, stamps[1:]):
            self.assertGreaterEqual(b - a, 0.03 * 0.7)       # the limit is for the whole bridge

    def test_sender_uses_one_persistent_connection(self):
        hub = self.make_hub(min_interval=0.01)
        hub.connect()
        base = self.fake.connections
        desk = self.light(hub, "1")
        for i in range(5):
            desk.send_frame([(i * 40, 10, 10)])
            self.assertTrue(wait_for(lambda i=i: len(self.fake.puts("1")) >= i + 1))
        self.assertEqual(self.fake.connections - base, 1)

    def test_end_stream_drops_pending(self):
        hub = self.connected(min_interval=0.4)
        desk, ceiling = self.light(hub, "1"), self.light(hub, "2")
        desk.send_frame([(255, 0, 0)])
        self.assertTrue(wait_for(lambda: len(self.fake.puts("1")) == 1))
        desk.send_frame([(0, 255, 0)])         # pending for >= 0.4 s
        ceiling.send_frame([(0, 0, 255)])
        desk.end_stream()
        self.assertTrue(wait_for(lambda: len(self.fake.puts("2")) == 1))
        time.sleep(0.5)
        self.assertEqual(len(self.fake.puts("1")), 1)

    def test_close_stops_thread(self):
        hub = self.connected()
        self.light(hub, "1").send_frame([(1, 2, 3)])
        self.assertTrue(wait_for(lambda: self.fake.puts("1")))
        thread = hub._thread
        hub.close()
        self.assertFalse(thread.is_alive())
        self.light(hub, "1").send_frame([(9, 9, 9)])         # ignored after close
        time.sleep(0.1)
        self.assertEqual(len(self.fake.puts("1")), 1)

    def test_sender_survives_unreachable_bridge(self):
        hub = self.connected()
        desk = self.light(hub, "1")
        self.fake.stop()
        self.fake.stop = lambda: None
        desk.send_frame([(5, 5, 5)])
        time.sleep(0.2)
        self.assertTrue(hub._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
