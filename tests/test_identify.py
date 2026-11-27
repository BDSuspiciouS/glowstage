import tempfile
import threading
import time
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager


class FakeLight(Device):
    def __init__(self, dev_id, fps=30):
        super().__init__(dev_id, dev_id)
        self.online = True
        self.max_fps = fps
        self.seen = []

    def apply(self, state):
        self.seen.append(state)


class Identify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.light = FakeLight("desk")
        self.m._register(self.light)
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 50})
        self.settle()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def settle(self):
        self.m.wait_applied(["desk"], 2)
        time.sleep(0.05)

    def wait_done(self):
        deadline = time.monotonic() + 5
        while self.m._identifying and time.monotonic() < deadline:
            time.sleep(0.02)
        self.settle()

    def run_flash(self, seconds=0.4):
        self.light.seen.clear()
        self.m.identify("desk", seconds)
        self.wait_done()

    def test_flashes_white_and_off_then_goes_back(self):
        self.run_flash()
        seen = self.light.seen
        self.assertTrue(any(s.on and s.color == "#ffffff" and s.brightness == 100 for s in seen))
        self.assertTrue(any(not s.on for s in seen))
        self.assertEqual(seen[-1].to_dict(), self.m.states["desk"].to_dict())

    def test_nothing_is_changed_or_saved(self):
        before = (self.m.states["desk"].to_dict(), dict(self.m.config["states"]),
                  dict(self.m._touches), self.m.config["settings"]["active_preset"])
        self.run_flash()
        after = (self.m.states["desk"].to_dict(), dict(self.m.config["states"]),
                 dict(self.m._touches), self.m.config["settings"]["active_preset"])
        self.assertEqual(before, after)

    def test_a_light_that_was_off_ends_off(self):
        self.m.update(["desk"], {"on": False})
        self.settle()
        self.run_flash()
        self.assertFalse(self.light.seen[-1].on)

    def test_the_effect_engine_leaves_it_alone_while_flashing(self):
        self.light.streaming = True
        self.light.pixel_count = 10
        self.m.update(["desk"], {"on": True, "mode": "effect"})
        self.settle()
        self.light.streaming = True   # (the fake's apply doesn't start a stream itself)
        self.assertEqual(len(self.m.stream_targets()), 1)
        self.m.identify("desk", 0.6)
        self.assertEqual(self.m.stream_targets(), [])
        self.wait_done()
        self.assertEqual(len(self.m.stream_targets()), 1)

    def test_unknown_and_offline_lights_are_refused(self):
        with self.assertRaisesRegex(ValueError, "isn't there any more"):
            self.m.identify("nope")
        self.light.online = False
        with self.assertRaisesRegex(ValueError, "desk isn't answering"):
            self.m.identify("desk")
        self.assertEqual(self.m._identifying, set())

    def test_a_change_made_meanwhile_wins(self):
        self.light.seen.clear()
        self.m.identify("desk", 0.6)
        time.sleep(0.15)
        self.m.update(["desk"], {"color": "#ff0000"})
        self.wait_done()
        self.assertEqual(self.light.seen[-1].color, "#ff0000")
        self.assertTrue(self.light.seen[-1].on)

    def test_a_second_request_does_not_start_a_second_run(self):
        before = threading.active_count()
        self.m.identify("desk", 0.6)
        self.m.identify("desk", 0.6)
        self.assertEqual(threading.active_count(), before + 1)
        self.wait_done()

    def test_slow_lights_flash_slower(self):
        slow = FakeLight("hue", fps=10)
        self.m._register(slow)
        self.m.update(["hue"], {"on": True, "mode": "color"})
        self.m.wait_applied(["hue"], 2)
        time.sleep(0.05)
        slow.seen.clear()
        self.light.seen.clear()
        self.m.identify("hue", 1.2)
        self.m.identify("desk", 1.2)
        self.wait_done()
        self.assertLess(len(slow.seen), len(self.light.seen))

    def test_stopping_the_app_puts_the_light_back_at_once(self):
        self.light.seen.clear()
        started = time.monotonic()
        self.m.identify("desk", 30)
        time.sleep(0.2)
        self.m._stop.set()
        self.wait_done()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.light.seen[-1].to_dict(), self.m.states["desk"].to_dict())


if __name__ == "__main__":
    unittest.main()
