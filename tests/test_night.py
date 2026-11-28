import datetime
import tempfile
import threading
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager, solid
from glowstage.night import in_window, next_end
from glowstage.state import LightState


def at(hour, minute=0, day=28):
    return datetime.datetime(2026, 11, day, hour, minute)


class Window(unittest.TestCase):
    def test_over_midnight(self):
        for t, inside in ((at(22, 59), False), (at(23), True), (at(2), True), (at(6, 59), True), (at(7), False), (at(12), False)):
            self.assertEqual(in_window("23:00", "07:00", t), inside, t)

    def test_within_a_day(self):
        self.assertTrue(in_window("13:00", "15:30", at(14)))
        self.assertFalse(in_window("13:00", "15:30", at(15, 30)))
        self.assertFalse(in_window("13:00", "15:30", at(9)))

    def test_when_it_ends(self):
        self.assertEqual(next_end("07:00", at(23)), at(7, day=29))
        self.assertEqual(next_end("07:00", at(3)), at(7))


class FakeLight(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True
        self.shown = []
        self.changed = threading.Event()

    def show_color(self, rgb, state):
        self.shown.append((rgb, state.brightness))
        self.changed.set()

    def power_off(self):
        pass


class Limit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.lamp = FakeLight("lamp")
        self.m._register(self.lamp)
        self.m.update(None, {"on": True, "mode": "color", "color": "#ffffff", "brightness": 80})
        self.n = self.m.night
        self.night = at(23, 30).timestamp()
        self.day = at(12).timestamp()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def settle(self):
        w = self.m.workers["lamp"]
        for _ in range(300):
            if not w.busy:
                break
            threading.Event().wait(0.01)

    def test_off_by_default(self):
        self.assertIsNone(self.n.ceiling(self.night))

    def test_only_at_night(self):
        self.n.configure(True, "23:00", "07:00", 30)
        self.assertEqual(self.n.ceiling(self.night), 30)
        self.assertIsNone(self.n.ceiling(self.day))

    def test_the_lights_go_down_and_come_back(self):
        self.n.configure(True, "23:00", "07:00", 30)
        self.assertTrue(self.n.tick(self.night))
        self.assertEqual(self.lamp.cap, 30)
        self.settle()
        self.assertEqual(self.lamp.shown[-1][1], 30)                # sent at the ceiling
        self.assertEqual(self.m.states["lamp"].brightness, 80)       # your own brightness is kept
        self.assertFalse(self.n.tick(self.night + 60))               # (no change, nothing sent)
        self.assertTrue(self.n.tick(self.day))
        self.assertIsNone(self.lamp.cap)
        self.settle()
        self.assertEqual(self.lamp.shown[-1][1], 80)

    def test_a_dim_light_is_left_alone(self):
        self.m.update(None, {"brightness": 10})
        self.n.configure(True, "23:00", "07:00", 30)
        self.n.tick(self.night)
        self.settle()
        self.assertEqual(self.lamp.shown[-1][1], 10)

    def test_a_light_that_comes_later_gets_it_too(self):
        self.n.configure(True, "23:00", "07:00", 30)
        self.n.tick(self.night)
        late = FakeLight("late")
        self.m._register(late)
        self.assertEqual(late.cap, 30)

    def test_a_wake_up_comes_through(self):
        self.n.configure(True, "23:00", "07:00", 30)
        self.m.wake.until = self.night + 600         # (a wake-up light running)
        self.assertIsNone(self.n.ceiling(self.night))
        self.m.wake.until = 0.0
        self.assertEqual(self.n.ceiling(self.night), 30)

    def test_not_tonight(self):
        self.n.configure(True, "23:00", "07:00", 30)
        self.n.skip_tonight(self.night)
        self.assertIsNone(self.n.ceiling(self.night))
        self.assertIsNone(self.n.ceiling(at(6, 59, day=29).timestamp()))
        self.assertEqual(self.n.ceiling(at(23, 30, day=29).timestamp()), 30)     # the next night it's back

    def test_checked_before_saved(self):
        for bad in (("25:00", "07:00", 30), ("23:00", "7", 30), ("23:00", "23:00", 30), ("23:00", "07:00", 33), ("23:00", "07:00", "x")):
            with self.assertRaises(ValueError, msg=bad):
                self.n.configure(True, *bad)
        self.assertFalse(self.n.settings["on"])

    def test_described(self):
        self.n.configure(True, "22:30", "06:45", 20)
        d = self.n.describe(self.night)
        self.assertEqual((d["on"], d["from"], d["to"], d["max"]), (True, "22:30", "06:45", 20))
        self.assertEqual(d["until"], at(6, 45, day=29).timestamp())


class EveryWayIn(unittest.TestCase):
    def test_apply_keeps_to_it(self):
        lamp = FakeLight("x")
        lamp.cap = 25
        lamp.apply(LightState(on=True, mode="color", color="#ffffff", brightness=90))
        self.assertEqual(lamp.shown[-1][1], 25)
        lamp.apply(LightState(on=True, mode="color", color="#ffffff", brightness=15))
        self.assertEqual(lamp.shown[-1][1], 15)

    def test_glides_aim_at_it(self):
        st = LightState(on=True, mode="color", color="#ffffff", brightness=100)
        self.assertEqual(solid(st, 50), (128, 128, 128))
        self.assertEqual(solid(st), (255, 255, 255))

    def test_effects_keep_to_it(self):
        from tests.test_soft import FakeStrip
        tmp = tempfile.TemporaryDirectory()
        m = DeviceManager(Config(Path(tmp.name) / "config.json"))
        try:
            strip = FakeStrip("s")
            m._register(strip)
            m.update(None, {"on": True, "mode": "effect", "effect": "rainbow", "brightness": 100})
            m.engine._frame(1000.0)
            full = max(max(c) for c in strip.frames[-1])
            strip.cap = 20
            m.engine._last.clear()
            m.engine._frame(1000.0)
            self.assertLessEqual(max(max(c) for c in strip.frames[-1]), round(full * 0.2) + 1)
        finally:
            m._stop.set()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
