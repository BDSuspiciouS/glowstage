"""Home Assistant driver, against a fake Home Assistant on 127.0.0.1 (no real network)."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from glowstage.devices import homeassistant as ha
from glowstage.devices.hub import NotPairedError
from glowstage.state import LightState

TOKEN = "secret-token"


def light(entity_id, state="on", modes=("rgb",), name=None, **attrs):
    a = {"friendly_name": name or entity_id, "supported_color_modes": list(modes)}
    a.update(attrs)
    return {"entity_id": entity_id, "state": state, "attributes": a}


STATES = [
    light("light.desk", name="Desk", brightness=128, rgb_color=[255, 0, 0]),
    light("light.lamp", modes=["color_temp"], name="Lamp", brightness=255),
    light("light.strip", modes=["hs", "xy"], name="Strip"),
    light("light.plug", modes=["onoff"], name="Plug"),
    light("light.gone", state="unavailable", modes=["rgb"], name="Gone"),
    {"entity_id": "switch.fan", "state": "on", "attributes": {"friendly_name": "Fan"}},
    {"entity_id": "sensor.temp", "state": "21", "attributes": {}},
]


class FakeHA(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        auth = self.headers.get("Authorization")
        self.server.log.append({"method": method, "path": self.path, "auth": auth, "body": body,
                                "time": time.monotonic(), "peer": self.client_address[1]})
        if auth != f"Bearer {TOKEN}":
            return self._reply(401, {"message": "401: Unauthorized"})
        if self.path == "/api/":
            return self._reply(200, {"message": "API running."})
        if self.path == "/api/config":
            return self._reply(200, {"location_name": "Home"})
        if self.path == "/api/states":
            return self._reply(200, STATES)
        if self.path.startswith("/api/states/"):
            eid = self.path[len("/api/states/"):]
            for st in STATES:
                if st["entity_id"] == eid:
                    return self._reply(200, st)
            return self._reply(404, {"message": "Entity not found."})
        if self.path.startswith("/api/services/light/"):
            return self._reply(200, [])
        return self._reply(404, {})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class Base(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)
        self.server.daemon_threads = True
        self.server.log = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.driver = ha.Driver()
        self.hubs = []

    def tearDown(self):
        for hub in self.hubs:
            hub.close()
        self.server.shutdown()
        self.server.server_close()

    def hub(self, token=TOKEN):
        entry = {"kind": "homeassistant", "host": "127.0.0.1", "port": self.port, "label": "Home",
                 "url": self.url, "token": token}
        hub = self.driver.create(f"homeassistant:127.0.0.1:{self.port}", entry)
        self.hubs.append(hub)
        return hub

    def services(self):
        return [r for r in self.server.log if r["path"].startswith("/api/services/")]

    def wait_for(self, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()


class TestDriver(Base):
    def test_describe(self):
        d = self.driver.describe()
        self.assertEqual((d["kind"], d["title"], d["hub"], d["beta"]), ("homeassistant", "Home Assistant", True, True))
        self.assertEqual([f["key"] for f in d["fields"]], ["url", "token"])
        self.assertTrue(d["fields"][1]["secret"])

    def test_from_mdns(self):
        found = self.driver.from_mdns("_home-assistant._tcp.local.", "x", "10.0.0.5", 8123,
                                      {"internal_url": "", "base_url": "", "location_name": "Casa"})
        self.assertEqual(found.dev_id, "homeassistant:10.0.0.5:8123")
        self.assertEqual(found.label, "Casa")
        self.assertEqual(found.extra, {"url": "http://10.0.0.5:8123"})
        found = self.driver.from_mdns("s", "x", "10.0.0.5", 8123, {"internal_url": "http://ha.lan:8123"})
        self.assertEqual(found.label, "Home Assistant")
        self.assertEqual(found.extra["url"], "http://ha.lan:8123")

    def test_probe_ok(self):
        found = self.driver.probe({"url": self.url + "/", "token": TOKEN})
        self.assertEqual(found.kind, "homeassistant")
        self.assertEqual(found.dev_id, f"homeassistant:127.0.0.1:{self.port}")
        self.assertEqual((found.host, found.port, found.label), ("127.0.0.1", self.port, "Home"))
        self.assertEqual(found.extra, {"url": self.url, "token": TOKEN})
        self.assertEqual(found.entry()["token"], TOKEN)

    def test_probe_bad_token(self):
        with self.assertRaises(ValueError) as cm:
            self.driver.probe({"url": self.url, "token": "nope"})
        self.assertEqual(str(cm.exception), "Home Assistant didn't accept the token")

    def test_probe_unreachable(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(ValueError):
            self.driver.probe({"url": self.url, "token": TOKEN})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)   # so tearDown has something to close
        self.server.log = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def test_probe_missing_values(self):
        with self.assertRaises(ValueError):
            self.driver.probe({"url": "", "token": TOKEN})
        with self.assertRaises(ValueError):
            self.driver.probe({"url": self.url, "token": ""})

    def test_pair(self):
        with self.assertRaises(NotPairedError):
            self.driver.pair(self.hub(token=""))
        self.assertEqual(self.driver.pair(self.hub()), {})


class TestHub(Base):
    def test_connect_filters_and_builds_devices(self):
        hub = self.hub()
        self.assertTrue(hub.paired)
        hub.connect()
        prefix = f"homeassistant:127.0.0.1:{self.port}:"
        self.assertEqual(sorted(hub.devices), sorted(prefix + e for e in
                                                     ("light.desk", "light.lamp", "light.strip", "light.gone")))
        desk = hub.devices[prefix + "light.desk"]
        self.assertEqual((desk.name, desk.color, desk.pixel_count, desk.max_fps, desk.group),
                         ("Desk", True, 1, 10, "room"))
        self.assertFalse(hub.devices[prefix + "light.lamp"].color)
        self.assertTrue(hub.online)
        desk.connect()
        self.assertTrue(desk.online)
        self.assertIsNone(desk.error)
        self.assertEqual(desk.reported_name, "Desk")
        self.assertEqual(desk.extra_info()["label"], "Home Assistant · light.desk")
        self.assertEqual(desk.extra_info()["host"], "127.0.0.1")
        self.assertEqual(desk.read_state(), {"on": True, "brightness": 50, "color": "#ff0000"})

    def test_unavailable_light(self):
        hub = self.hub()
        hub.connect()
        gone = hub.devices[f"{hub.id}:light.gone"]
        self.assertFalse(gone.online)
        self.assertEqual(gone.error, "Unavailable in Home Assistant")
        with self.assertRaises(RuntimeError):
            gone.connect()
        self.assertFalse(gone.online)

    def test_connect_bad_token(self):
        hub = self.hub(token="wrong")
        with self.assertRaises(NotPairedError):
            hub.connect()
        self.assertFalse(hub.online)

    def test_connect_without_token(self):
        hub = self.hub(token="")
        self.assertFalse(hub.paired)
        with self.assertRaises(NotPairedError):
            hub.connect()
        self.assertEqual(self.server.log, [])

    def test_reconnect_keeps_light_objects(self):
        hub = self.hub()
        hub.connect()
        before = dict(hub.devices)
        hub.connect()
        self.assertTrue(all(hub.devices[k] is v for k, v in before.items()))

    def test_authorization_header(self):
        hub = self.hub()
        hub.connect()
        desk = hub.devices[f"{hub.id}:light.desk"]
        desk.connect()
        desk.power_off()
        desk.send_frame([(1, 2, 3)])
        self.assertTrue(self.wait_for(lambda: len(self.services()) >= 2))
        self.assertTrue(self.server.log)
        for r in self.server.log:
            self.assertEqual(r["auth"], f"Bearer {TOKEN}", r["path"])


class TestLight(Base):
    def setUp(self):
        super().setUp()
        self.hub_ = self.hub()
        self.hub_.connect()
        self.desk = self.hub_.devices[f"{self.hub_.id}:light.desk"]
        self.desk.moment = 0          # (at once: the glide is tested apart)
        self.lamp = self.hub_.devices[f"{self.hub_.id}:light.lamp"]
        self.lamp.moment = 0          # (at once: the glide is tested apart)

    def test_show_color_body(self):
        self.desk.show_color((255, 0, 0), LightState(brightness=50))
        req = self.services()[-1]
        self.assertEqual((req["method"], req["path"]), ("POST", "/api/services/light/turn_on"))
        self.assertEqual(req["body"], {"entity_id": "light.desk", "rgb_color": [255, 0, 0],
                                       "brightness": 128, "transition": 0})

    def test_show_color_uses_value_of_the_color(self):
        self.desk.show_color((100, 0, 0), LightState(brightness=100))
        body = self.services()[-1]["body"]
        self.assertEqual(body["rgb_color"], [255, 0, 0])      # hue at full value, dimming goes in brightness
        self.assertEqual(body["brightness"], 100)

    def test_show_color_brightness_only_light(self):
        self.lamp.show_color((0, 255, 0), LightState(brightness=100))
        self.assertEqual(self.services()[-1]["body"],
                         {"entity_id": "light.lamp", "brightness": 255, "transition": 0})

    def test_power_off(self):
        self.desk.power_off(timeout=1.0)
        req = self.services()[-1]
        self.assertEqual(req["path"], "/api/services/light/turn_off")
        self.assertEqual(req["body"], {"entity_id": "light.desk", "transition": 0})

    def test_apply_off(self):
        self.desk.apply(LightState(on=False))
        self.assertEqual(self.services()[-1]["path"], "/api/services/light/turn_off")


class TestStreaming(Base):
    def setUp(self):
        super().setUp()
        self.hub_ = self.hub()
        self.hub_.connect()
        self.desk = self.hub_.devices[f"{self.hub_.id}:light.desk"]
        self.desk.moment = 0          # (at once: the glide is tested apart)
        self.strip = self.hub_.devices[f"{self.hub_.id}:light.strip"]

    def test_frames_collapse_to_latest(self):
        for i in range(100):
            self.desk.send_frame([(i, 0, 0), (i, 0, 0)])
        self.desk.send_frame([(0, 200, 0), (0, 100, 0)])   # average (0, 150, 0)
        self.assertTrue(self.wait_for(lambda: self.services() and
                                      self.services()[-1]["body"]["rgb_color"] == [0, 255, 0]))
        time.sleep(0.3)
        calls = self.services()
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(calls[-1]["body"]["brightness"], 150)   # value 150/255 of full
        self.assertEqual(calls[-1]["body"]["entity_id"], "light.desk")

    def test_unchanged_frames_are_not_resent(self):
        self.desk.send_frame([(10, 20, 30)])
        self.assertTrue(self.wait_for(lambda: len(self.services()) == 1))
        for _ in range(5):
            self.desk.send_frame([(10, 20, 30)])
            time.sleep(0.12)
        self.assertEqual(len(self.services()), 1)

    def test_rate_limit_and_round_robin_over_one_connection(self):
        start = time.monotonic()
        i = 0
        while time.monotonic() - start < 1.0:   # both lights change every 5 ms
            i += 1
            self.desk.send_frame([(i % 250 + 1, 0, 0)])
            self.strip.send_frame([(0, i % 250 + 1, 0)])
            time.sleep(0.005)
        calls = self.services()
        self.assertTrue(8 <= len(calls) <= 13, len(calls))
        times = [r["time"] for r in calls]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreaterEqual(min(gaps), 0.08)
        ids = [r["body"]["entity_id"] for r in calls]
        self.assertIn("light.desk", ids)
        self.assertIn("light.strip", ids)
        self.assertLessEqual(max(ids.count("light.desk"), ids.count("light.strip")), len(ids) // 2 + 2)
        self.assertEqual(len({r["peer"] for r in calls}), 1)   # one persistent connection

    def test_close_stops_the_thread(self):
        self.desk.send_frame([(1, 2, 3)])
        self.assertTrue(self.wait_for(lambda: self.services()))
        thread = self.hub_._thread
        self.hub_.close()
        self.assertFalse(thread.is_alive())
        n = len(self.services())
        self.desk.send_frame([(9, 9, 9)])
        time.sleep(0.25)
        self.assertEqual(len(self.services()), n)

    def test_direct_color_replaces_waiting_frame(self):
        self.desk.send_frame([(1, 2, 3)])
        self.assertTrue(self.wait_for(lambda: self.services()))
        self.desk.send_frame([(50, 60, 70)])
        self.desk.show_color((0, 0, 255), LightState(brightness=100))   # drops the waiting frame
        time.sleep(0.3)
        self.assertEqual(self.services()[-1]["body"]["rgb_color"], [0, 0, 255])


if __name__ == "__main__":
    unittest.main()
