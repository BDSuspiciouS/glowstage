import tempfile
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager


class FakeLight(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True

    def apply(self, state):
        pass


class SleepTimer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i in ("desk", "lamp", "fan"):
            self.m._register(FakeLight(i))
        self.m.update(None, {"on": True, "brightness": 80})
        self.m.update(["fan"], {"on": False})
        self.t = self.m.sleep
        self.t._thread = type("Alive", (), {"is_alive": lambda self: True})()   # steps by hand here

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def bright(self, i):
        return self.m.states[i].brightness

    def test_fades_and_switches_off(self):
        self.t.start(30)
        start = self.t.started
        self.t.tick(start + 15 * 60)                 # halfway
        self.assertAlmostEqual(self.bright("desk"), 42, delta=2)
        self.t.tick(start + 29 * 60)
        self.assertLess(self.bright("desk"), 8)
        self.t.tick(start + 30 * 60)
        self.assertFalse(self.m.states["desk"].on)
        self.assertEqual(self.bright("desk"), 80)    # comes on as it was next time
        self.assertFalse(self.t.active)

    def test_only_lights_that_are_on(self):
        self.t.start(30)
        self.assertEqual(set(self.t._lights), {"desk", "lamp"})

    def test_the_fading_isnt_saved(self):
        self.t.start(30)
        self.t.tick(self.t.started + 20 * 60)
        self.assertEqual(self.config["states"]["desk"]["brightness"], 80)

    def test_a_light_you_change_is_left_alone(self):
        self.t.start(30)
        self.t.tick(self.t.started + 10 * 60)
        self.m.update(["lamp"], {"brightness": 100})
        self.t.tick(self.t.started + 20 * 60)
        self.assertEqual(self.bright("lamp"), 100)
        self.t.tick(self.t.started + 30 * 60)
        self.assertTrue(self.m.states["lamp"].on)
        self.assertFalse(self.m.states["desk"].on)

    def test_cancel_puts_the_brightness_back(self):
        self.t.start(30)
        self.t.tick(self.t.started + 20 * 60)
        self.t.cancel()
        self.assertEqual(self.bright("desk"), 80)
        self.assertTrue(self.m.states["desk"].on)
        self.assertFalse(self.t.active)

    def test_a_new_time_starts_from_the_lights_as_they_were(self):
        self.t.start(30)
        self.t.tick(self.t.started + 20 * 60)
        self.t.start(60)
        self.assertEqual(self.bright("desk"), 80)
        self.assertEqual(self.t._lights["desk"][0], 80)

    def test_stepping_away_meanwhile(self):
        self.m.config["settings"]["away_lights"] = "all"
        self.t.start(30)
        self.t.tick(self.t.started + 20 * 60)        # dimmed to about a third
        self.m.away.set("screen off", True)
        self.assertFalse(self.t.active)
        self.m.away.set("screen off", False)         # the next morning
        self.assertTrue(self.m.states["desk"].on)
        self.assertEqual(self.bright("desk"), 80)
        self.assertEqual(self.config["states"]["desk"]["brightness"], 80)

    def test_nothing_on(self):
        self.m.update(None, {"on": False})
        with self.assertRaises(ValueError):
            self.t.start(15)


if __name__ == "__main__":
    unittest.main()
