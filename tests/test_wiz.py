"""WiZ driver: message building, capabilities, white handling and a fake light on 127.0.0.1
(no real network, nothing is ever broadcast)."""

import json
import socket
import threading
import time
import unittest

from glowstage.devices import registry, wiz
from glowstage.state import LightState

MAC = "a8bb50123456"


def wait_for(cond, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeWiz:
    """A WiZ light on 127.0.0.1: answers getPilot / getSystemConfig / setPilot / setState and
    records every command it gets."""

    def __init__(self, module="ESP01_SHRGB1C_31", pilot=None, silent=False, garbage=False, error=None):
        self.module = module
        self.pilot = {"mac": MAC, "rssi": -60, "state": True, "sceneId": 0, "r": 255, "g": 0, "b": 0,
                      "c": 0, "w": 0, "dimming": 100}
        if pilot is not None:
            self.pilot = pilot
        self.silent, self.garbage, self.error = silent, garbage, error
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.records: list[tuple[str, dict]] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        self._thread.join(1)
        self.sock.close()

    def params(self, method):
        return [p for m, p in list(self.records) if m == method]

    def _run(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if self.silent:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            method, params = msg.get("method"), msg.get("params") or {}
            if self.garbage:   # noise first: not JSON, JSON without a method, an answer to something else
                for noise in (b"\xff\xfe nonsense", b'{"result": {"mac": "zz"}}', b'["a"]',
                              json.dumps({"method": "other", "result": {}}).encode()):
                    self.sock.sendto(noise, addr)
            if method == "getPilot":
                reply = {"method": method, "env": "pro", "result": dict(self.pilot)}
            elif method == "getSystemConfig":
                reply = {"method": method, "env": "pro",
                         "result": {"mac": MAC, "moduleName": self.module, "fwVersion": "1.25.0"}}
            elif method in ("setPilot", "setState"):
                self.records.append((method, params))
                reply = {"method": method, "env": "pro", "result": {"success": True}}
                if self.error:
                    reply = {"method": method, "env": "pro", "error": {"code": -32602, "message": self.error}}
            else:
                reply = {"method": method, "env": "pro", "error": {"code": -32601, "message": "Method not found"}}
            self.sock.sendto(json.dumps(reply).encode(), addr)


class PureTests(unittest.TestCase):
    def test_build_and_parse(self):
        self.assertEqual(json.loads(wiz.build("getPilot")), {"method": "getPilot", "params": {}})
        self.assertEqual(wiz.parse(b'{"method":"getPilot","result":{}}')["method"], "getPilot")
        for bad in (b"", b"garbage", b"[]", b'{"result": {}}', b'{"method": 5}'):
            self.assertIsNone(wiz.parse(bad))

    def test_clean_mac(self):
        self.assertEqual(wiz.clean_mac("A8:BB:50:12:34:56"), MAC)
        self.assertEqual(wiz.clean_mac("zz"), "")
        self.assertEqual(wiz.clean_mac(None), "")

    def test_module_name_decides_capabilities(self):
        self.assertEqual(wiz.capabilities("ESP01_SHRGB1C_31"), wiz.COLOR)
        self.assertEqual(wiz.capabilities("ESP03_SHRGB3_01ABI"), wiz.COLOR)
        self.assertEqual(wiz.capabilities("ESP01_SHTW1C_31"), wiz.TUNABLE)
        self.assertEqual(wiz.capabilities("ESP01_SHDW1_31"), wiz.DIM)

    def test_pilot_decides_when_the_module_is_unknown(self):
        self.assertEqual(wiz.capabilities("", {"r": 1, "g": 2, "b": 3}), wiz.COLOR)
        self.assertEqual(wiz.capabilities("", {"temp": 2700}), wiz.TUNABLE)
        self.assertEqual(wiz.capabilities("", {"state": True, "dimming": 50}), wiz.DIM)

    def test_color_params(self):
        self.assertEqual(wiz.pilot_params((255, 128, 0), 80), {"r": 255, "g": 128, "b": 0, "dimming": 80})
        # a dark color is sent at full strength with the strength in the dimming
        self.assertEqual(wiz.pilot_params((0, 0, 128), 100), {"r": 0, "g": 0, "b": 255, "dimming": 50})

    def test_dimming_floor(self):
        self.assertEqual(wiz.pilot_params((255, 0, 0), 3)["dimming"], 10)
        self.assertEqual(wiz.pilot_params((10, 0, 0), 100)["dimming"], 10)
        self.assertEqual(wiz.pilot_params((0, 0, 0), 100)["dimming"], 10)
        self.assertEqual(wiz.pilot_params((255, 0, 0), 100)["dimming"], 100)

    def test_whites_go_as_a_temperature(self):
        white = wiz.pilot_params((255, 255, 255), 100)
        self.assertEqual(set(white), {"temp", "dimming"})
        self.assertGreaterEqual(white["temp"], 6000)
        warm = wiz.pilot_params((255, 180, 107), 70)    # the "warm white" color
        self.assertEqual(set(warm), {"temp", "dimming"})
        self.assertTrue(2200 <= warm["temp"] <= 3200)
        self.assertEqual(warm["dimming"], 70)
        for rgb in ((255, 0, 0), (0, 255, 0), (80, 0, 255)):
            self.assertIn("r", wiz.pilot_params(rgb, 100))

    def test_tunable_white_light_gets_temperatures_only(self):
        p = wiz.pilot_params((255, 0, 0), 60, wiz.TUNABLE)
        self.assertEqual(set(p), {"temp", "dimming"})
        self.assertTrue(wiz.TEMP_MIN <= p["temp"] <= wiz.TEMP_MAX)

    def test_dim_only_light_gets_dimming_only(self):
        self.assertEqual(wiz.pilot_params((255, 255, 255), 40, wiz.DIM), {"dimming": 40})


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.lights = []

    def tearDown(self):
        for light in self.lights:
            light.close()

    def light(self, **kw):
        light = FakeWiz(**kw)
        self.lights.append(light)
        return light

    def make(self, light, timeout=0.3, **kw):
        dev = wiz.WizDevice(f"wiz:{MAC}", "Test", "127.0.0.1", light.port, MAC, **kw)
        dev.timeout = timeout
        self.addCleanup(dev._sock.close)
        return dev

    def test_connect_reads_module_and_state(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        self.assertTrue(dev.online)
        self.assertIsNone(dev.error)
        self.assertEqual(dev.pixel_count, 1)
        self.assertEqual(dev.caps, wiz.COLOR)
        self.assertEqual(dev.module, "ESP01_SHRGB1C_31")
        self.assertEqual(dev.extra_info()["label"], "WiZ · color")
        self.assertEqual(dev.extra_info()["host"], "127.0.0.1")
        self.assertLessEqual(dev.max_fps, 12)      # a slow light for the light show

    def test_connect_tunable_and_dim_models(self):
        for module, caps in (("ESP01_SHTW1C_31", wiz.TUNABLE), ("ESP01_SHDW1_31", wiz.DIM)):
            light = self.light(module=module)
            dev = self.make(light)
            dev.connect()
            self.assertEqual(dev.caps, caps)

    def test_connect_without_answer_raises(self):
        light = self.light(silent=True)
        dev = self.make(light, timeout=0.1)
        with self.assertRaises(TimeoutError):
            dev.connect()
        self.assertFalse(dev.online)

    def test_read_state(self):
        light = self.light(pilot={"mac": MAC, "state": True, "dimming": 40, "r": 10, "g": 20, "b": 30})
        dev = self.make(light)
        dev.connect()
        self.assertEqual(dev.read_state(), {"on": True, "brightness": 40, "color": "#0a141e"})

    def test_read_state_white_and_scene(self):
        light = self.light(pilot={"mac": MAC, "state": False, "dimming": 100, "temp": 2700})
        dev = self.make(light)
        dev.connect()
        st = dev.read_state()
        self.assertFalse(st["on"])
        self.assertTrue(st["color"].startswith("#ff"))
        light = self.light(pilot={"mac": MAC, "state": True, "dimming": 60, "sceneId": 4, "speed": 100})
        dev = self.make(light)
        dev.connect()
        st = dev.read_state()
        self.assertEqual((st["mode"], st["native"]), ("native", "4"))
        self.assertTrue(1 <= st["speed"] <= 100)

    def test_show_color_sends_set_pilot(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.show_color((255, 128, 0), LightState(brightness=80))
        self.assertEqual(light.params("setPilot"), [{"state": True, "r": 255, "g": 128, "b": 0, "dimming": 80}])

    def test_show_color_floor_and_white(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.show_color((255, 0, 0), LightState(brightness=2))
        self.assertEqual(light.params("setPilot")[-1]["dimming"], 10)
        dev.show_color((255, 255, 255), LightState(brightness=100))
        last = light.params("setPilot")[-1]
        self.assertIn("temp", last)
        self.assertNotIn("r", last)

    def test_power_off(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.power_off()
        self.assertEqual(light.params("setState"), [{"state": False}])

    def test_apply_off_and_color(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.apply(LightState(on=True, brightness=100, color="#00ff00"))
        self.assertEqual(light.params("setPilot")[-1], {"state": True, "r": 0, "g": 255, "b": 0, "dimming": 100})
        dev.apply(LightState(on=False))
        self.assertEqual(light.params("setState"), [{"state": False}])

    def test_night_limit_caps_dimming(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.cap = 30
        dev.apply(LightState(on=True, brightness=100, color="#ff0000"))
        self.assertEqual(light.params("setPilot")[-1]["dimming"], 30)

    def test_light_that_refuses_raises(self):
        light = self.light(error="Invalid params")
        dev = self.make(light)
        dev.connect()
        with self.assertRaises(RuntimeError):
            dev.show_color((255, 0, 0), LightState())

    def test_unanswered_command_raises(self):
        light = self.light()
        dev = self.make(light, timeout=0.1)
        dev.connect()
        light.silent = True
        with self.assertRaises(TimeoutError):
            dev.show_color((255, 0, 0), LightState())

    def test_native_scenes_listed_by_capability(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        scenes = dev.native_effects()
        self.assertEqual(len(scenes), 32)
        self.assertIn({"id": "4", "name": "Party"}, scenes)
        names = [s["name"] for s in scenes]
        self.assertEqual(names, sorted(names, key=str.lower))
        tw = self.make(self.light(module="ESP01_SHTW1C_31"))
        tw.connect()
        white = {s["name"] for s in tw.native_effects()}
        self.assertIn("Warm white", white)
        self.assertNotIn("Party", white)
        dw = self.make(self.light(module="ESP01_SHDW1_31"))
        dw.connect()
        self.assertEqual(dw.native_effects(), [])

    def test_show_native_scene(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.apply(LightState(mode="native", native="4", speed=100, brightness=70))
        self.assertEqual(light.params("setPilot")[-1],
                         {"state": True, "sceneId": 4, "speed": 200, "dimming": 70})
        dev.apply(LightState(mode="native", native="1", speed=1, brightness=5))
        self.assertEqual(light.params("setPilot")[-1],
                         {"state": True, "sceneId": 1, "speed": 10, "dimming": 10})

    def test_stream_sends_average_color(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.begin_stream(LightState(mode="effect"))
        dev.send_frame([(255, 0, 0), (0, 0, 255)])
        self.assertTrue(wait_for(lambda: len(light.params("setPilot")) == 2))
        self.assertEqual(light.params("setPilot")[0], {"state": True})
        self.assertEqual(light.params("setPilot")[1],
                         {"state": True, "r": 255, "g": 0, "b": 255, "dimming": 50})

    def test_stream_skips_an_unchanged_frame(self):
        light = self.light()
        dev = self.make(light)
        dev.connect()
        dev.begin_stream(LightState(mode="effect"))
        for _ in range(3):
            dev.send_frame([(0, 255, 0)])
        dev.send_frame([(0, 0, 255)])
        self.assertTrue(wait_for(lambda: len(light.params("setPilot")) == 3))
        time.sleep(0.1)
        self.assertEqual(len(light.params("setPilot")), 3)

    def test_stream_goes_offline_when_the_light_stops_answering(self):
        light = self.light()
        dev = self.make(light)
        dev.stall_after = 0.2
        dev.connect()
        dev.begin_stream(LightState(mode="effect"))
        light.silent = True
        dev.send_frame([(255, 0, 0)])
        time.sleep(0.35)
        with self.assertRaises(TimeoutError):   # (the manager marks a light offline on this)
            dev.send_frame([(0, 255, 0)])

    def test_a_still_stream_is_not_taken_for_a_dead_light(self):
        light = self.light()
        dev = self.make(light)
        dev.stall_after = 0.2
        dev.connect()
        dev.begin_stream(LightState(mode="effect"))
        dev.send_frame([(255, 0, 0)])
        self.assertTrue(wait_for(lambda: len(light.params("setPilot")) == 2))
        time.sleep(0.4)
        dev.send_frame([(255, 0, 0)])      # same frame, nothing sent, and the light did answer before
        dev.send_frame([(255, 0, 0)])

    def test_comes_back_after_going_quiet(self):
        light = self.light(silent=True)
        dev = self.make(light, timeout=0.1)
        with self.assertRaises(TimeoutError):
            dev.connect()
        self.assertFalse(dev.online)
        light.silent = False
        dev.connect()
        self.assertTrue(dev.online)
        dev.show_color((255, 0, 0), LightState())
        self.assertEqual(len(light.params("setPilot")), 1)

    def test_garbled_replies_are_ignored(self):
        light = self.light(garbage=True)
        dev = self.make(light)
        dev.connect()
        self.assertEqual(dev.caps, wiz.COLOR)
        dev.show_color((0, 255, 0), LightState(brightness=100))
        self.assertEqual(light.params("setPilot")[-1]["g"], 255)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.lights = []

    def tearDown(self):
        for light in self.lights:
            light.close()

    def light(self, **kw):
        light = FakeWiz(**kw)
        self.lights.append(light)
        return light

    def test_registered_as_beta(self):
        driver = registry.drivers()["wiz"]
        self.assertTrue(driver.beta)
        self.assertEqual(driver.title, "WiZ")
        self.assertEqual(driver.pairing, "Found automatically once Allow local communication is on in the WiZ app")

    def test_scan_finds_the_light(self):
        light = self.light()
        found = wiz.Driver().scan(0.6, light.port, "127.0.0.1")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual((item.kind, item.dev_id, item.host, item.port), ("wiz", f"wiz:{MAC}", "127.0.0.1", light.port))
        self.assertEqual(item.extra["mac"], MAC)
        self.assertEqual(item.extra["module"], "ESP01_SHRGB1C_31")
        self.assertIn("color", item.label)
        self.assertEqual(item.entry()["module"], "ESP01_SHRGB1C_31")

    def test_scan_ignores_garbage_and_finds_nothing_in_silence(self):
        light = self.light(garbage=True)
        self.assertEqual(len(wiz.Driver().scan(0.6, light.port, "127.0.0.1")), 1)
        quiet = self.light(silent=True)
        self.assertEqual(wiz.Driver().scan(0.4, quiet.port, "127.0.0.1"), [])

    def test_scan_ignores_a_reply_without_a_mac(self):
        light = self.light(pilot={"state": True, "dimming": 10})
        self.assertEqual(wiz.Driver().scan(0.4, light.port, "127.0.0.1"), [])

    def test_probe(self):
        light = self.light(module="ESP01_SHTW1C_31")
        found = wiz.Driver().probe({"host": "127.0.0.1"}, port=light.port)
        self.assertEqual(found.dev_id, f"wiz:{MAC}")
        self.assertIn("tunable white", found.label)
        self.assertEqual(found.extra["module"], "ESP01_SHTW1C_31")

    def test_probe_no_answer_explains(self):
        light = self.light(silent=True)
        with self.assertRaises(ValueError) as ctx:
            wiz.Driver().probe({"host": "127.0.0.1"}, port=light.port)
        self.assertIn("Allow local communication", str(ctx.exception))
        with self.assertRaises(ValueError):
            wiz.Driver().probe({"host": ""})

    def test_create_from_entry(self):
        found = wiz.Driver()._found(MAC, "192.168.1.9", wiz.PORT, "ESP01_SHDW1_31")
        dev = wiz.Driver().create(found.dev_id, found.entry())
        self.addCleanup(dev._sock.close)
        self.assertEqual((dev.host, dev.port, dev.mac, dev.caps), ("192.168.1.9", wiz.PORT, MAC, wiz.DIM))


if __name__ == "__main__":
    unittest.main()
