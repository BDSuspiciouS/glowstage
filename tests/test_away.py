import tempfile
import threading
import time
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager


class FakeLight(Device):
    def __init__(self, dev_id, group, delay=0.0):
        super().__init__(dev_id, dev_id)
        self.group = group
        self.online = True
        self.delay = delay
        self.applied = []
        self.done = threading.Event()

    def apply(self, state):
        time.sleep(self.delay)
        self.applied.append(state.on)
        self.done.set()


class Away(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        self.fan = FakeLight("fan", "pc")
        self.keys = FakeLight("keys", "pc")
        self.lamp = FakeLight("lamp", "room")
        for d in (self.fan, self.keys, self.lamp):
            self.m._register(d)
        self.m.update(None, {"on": True})
        self.m.update(["keys"], {"on": False})       # already off: stays off afterwards too
        self.m.config["settings"]["away_lights"] = "pc"

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def on(self):
        return {i: st.on for i, st in self.m.states.items()}

    def test_pc_lights_go_off_and_come_back(self):
        self.m.away.set("locked", True)
        self.assertEqual(self.on(), {"fan": False, "keys": False, "lamp": True})
        self.m.away.set("locked", False)
        self.assertEqual(self.on(), {"fan": True, "keys": False, "lamp": True})

    def test_all_lights(self):
        self.m.config["settings"]["away_lights"] = "all"
        self.m.away.set("screen off", True)
        self.assertEqual(self.on(), {"fan": False, "keys": False, "lamp": False})
        self.m.away.set("screen off", False)
        self.assertEqual(self.on(), {"fan": True, "keys": False, "lamp": True})

    def test_nothing_when_switched_off(self):
        self.m.config["settings"]["away_lights"] = ""
        self.m.away.set("locked", True)
        self.assertEqual(self.on(), {"fan": True, "keys": False, "lamp": True})

    def test_away_until_every_reason_is_over(self):
        self.m.away.set("locked", True)
        self.m.away.set("screen off", True)
        self.m.away.set("screen off", False)          # the screen is back, but still locked
        self.assertFalse(self.on()["fan"])
        self.m.away.set("locked", False)
        self.assertTrue(self.on()["fan"])

    def test_off_while_away_is_not_saved(self):
        self.m.away.set("locked", True)
        self.assertTrue(self.config["states"]["fan"]["on"])   # Windows shutting down now: back on next time

    def test_a_change_meanwhile_wins(self):
        self.m.away.set("locked", True)
        self.m.run_action("off")                       # a schedule at 1:00
        self.m.away.set("locked", False)
        self.assertEqual(self.on(), {"fan": False, "keys": False, "lamp": False})

    def test_before_sleep_the_lights_go_dark_first(self):
        self.fan.delay = 0.3
        self.fan.done.clear()
        started = time.monotonic()
        self.m.away.set("asleep", True)
        self.assertTrue(self.fan.done.is_set())
        self.assertFalse(self.fan.applied[-1])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_no_automatic_looks_while_away(self):
        steps = []
        auto = self.m.automations
        auto.step = lambda *a, **k: steps.append(a)
        self.m.away.set("locked", True)
        auto.start()
        time.sleep(1.3)
        self.assertEqual(steps, [])
        self.m.away.set("locked", False)
        time.sleep(1.1)
        auto.stop()
        self.assertTrue(steps)


if __name__ == "__main__":
    unittest.main()
