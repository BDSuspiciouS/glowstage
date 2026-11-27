import datetime
import tempfile
import unittest
from pathlib import Path

from glowstage.colors import hex_to_rgb
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.wake import FINAL, look

DAY = datetime.date(2026, 6, 10)


def at(hh, mm, ss=0):
    return datetime.datetime.combine(DAY, datetime.time(hh, mm, ss))


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, dev_id)
        self.group = group
        self.online = True

    def apply(self, state):
        pass


class Base(unittest.TestCase):
    lights = (("lamp", "room"), ("strip", "room"), ("case", "pc"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in self.lights:
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": False, "mode": "color", "color": "#123456", "brightness": 40})
        self.w = self.m.wake
        self.w._thread = type("Alive", (), {"is_alive": lambda self: True})()   # steps by hand here
        self.m.sleep._thread = type("Alive", (), {"is_alive": lambda self: True})()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def st(self, i):
        return self.m.states[i]


class WakeUp(Base):
    def test_from_dim_red_to_full_warm_white(self):
        self.w.start(30)
        t0 = self.w.started
        first = self.st("lamp")
        self.assertTrue(first.on)
        self.assertEqual(first.mode, "color")
        self.assertEqual(first.brightness, 1)
        r, g, b = hex_to_rgb(first.color)
        self.assertGreater(r, 200)
        self.assertLess(g, 100)
        self.assertEqual(b, 0)
        self.w.tick(t0 + 15 * 60)                                  # halfway: amber to warm, still dim-ish
        half = self.st("lamp")
        self.assertTrue(20 < half.brightness < 40)
        self.assertGreater(hex_to_rgb(half.color)[1], 120)
        self.w.tick(t0 + 29 * 60)
        self.assertGreater(self.st("lamp").brightness, 85)
        self.w.tick(t0 + 30 * 60)
        end = self.st("lamp")
        self.assertEqual((end.on, end.brightness, end.color), (True, 100, FINAL))
        self.assertFalse(self.w.active)

    def test_brightness_rises_slowly_at_first(self):
        steps = [look(p / 10)[1] for p in range(11)]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(steps[0], 1)
        self.assertEqual(steps[-1], 100)
        self.assertLess(steps[5], 35)                              # less than a third at half time

    def test_the_end_is_saved_the_fade_is_not(self):
        self.w.start(30)
        self.w.tick(self.w.started + 20 * 60)
        self.assertEqual(self.config["states"]["lamp"]["brightness"], 40)
        self.assertFalse(self.config["states"]["lamp"]["on"])
        self.w.tick(self.w.started + 30 * 60)
        saved = self.config["states"]["lamp"]
        self.assertEqual((saved["on"], saved["brightness"], saved["color"]), (True, 100, FINAL))

    def test_only_room_lights(self):
        self.w.start(15)
        self.assertEqual(set(self.w._lights), {"lamp", "strip"})
        self.assertFalse(self.st("case").on)
        self.w.tick(self.w.started + 15 * 60)
        self.assertFalse(self.st("case").on)
        self.assertEqual(self.st("strip").brightness, 100)

    def test_all_lights_when_there_are_no_room_lights(self):
        self.m._stop.set()
        self.tmp.cleanup()
        self.lights = (("case", "pc"), ("fan", "pc"))
        self.setUp()
        self.w.start(15)
        self.assertEqual(set(self.w._lights), {"case", "fan"})
        self.assertTrue(self.st("case").on)

    def test_a_light_you_change_is_left_alone(self):
        self.w.start(30)
        self.w.tick(self.w.started + 10 * 60)
        self.m.update(["lamp"], {"color": "#00ff00", "brightness": 55})
        self.w.tick(self.w.started + 20 * 60)
        self.assertEqual((self.st("lamp").color, self.st("lamp").brightness), ("#00ff00", 55))
        self.assertGreater(self.st("strip").brightness, 10)
        self.w.tick(self.w.started + 30 * 60)
        self.assertEqual(self.st("lamp").color, "#00ff00")          # still yours at the end
        self.assertEqual(self.st("strip").brightness, 100)

    def test_switching_every_light_off_ends_it(self):
        self.w.start(30)
        self.w.tick(self.w.started + 5 * 60)
        self.m.update(["lamp", "strip"], {"on": False})
        self.assertFalse(self.w.tick(self.w.started + 10 * 60))
        self.assertFalse(self.w.active)
        self.assertFalse(self.st("lamp").on)

    def test_describe_and_cancel(self):
        self.assertIsNone(self.w.describe())
        self.w.start(45)
        info = self.w.describe()
        self.assertEqual(info["minutes"], 45)
        self.assertAlmostEqual(info["until"], self.w.started + 45 * 60)
        self.w.cancel()
        self.assertIsNone(self.w.describe())
        self.assertFalse(self.st("lamp").on)                       # back as it was before: off

    def test_stop_puts_back_only_what_it_still_had(self):
        self.w.start(30)
        self.w.tick(self.w.started + 10 * 60)
        self.m.update(["lamp"], {"color": "#00ff00", "brightness": 55})
        self.w.cancel()
        self.assertEqual((self.st("lamp").on, self.st("lamp").color), (True, "#00ff00"))   # yours stays
        self.assertEqual((self.st("strip").on, self.st("strip").color, self.st("strip").brightness),
                         (False, "#123456", 40))                   # the rest as before it started
        self.assertFalse(self.config["states"]["strip"]["on"])

    def test_run_action_starts_it(self):
        self.m.run_action("wake:30")
        self.assertEqual(self.w.describe()["minutes"], 30)


class WakeAndSleep(Base):
    def test_sleep_timer_cancels_a_wake_up(self):
        self.w.start(30)
        self.m.sleep.start(15)
        self.assertFalse(self.w.active)
        self.assertTrue(self.m.sleep.active)

    def test_wake_up_cancels_the_sleep_timer(self):
        self.m.update(None, {"on": True, "brightness": 80})
        self.m.sleep.start(15)
        self.w.start(30)
        self.assertFalse(self.m.sleep.active)
        self.assertTrue(self.w.active)


class WakeSchedule(Base):
    def setUp(self):
        super().setUp()
        self.m.sun_times = lambda day: ("05:30", "21:00")
        self.fired = {}

    def due(self, now):
        return [s["action"] for s in self.m.due_schedules(now, self.fired)]

    def test_fires_at_the_start_not_at_the_time(self):
        self.m.set_schedules([{"time": "07:00", "action": "wake:30"}])
        self.assertEqual(self.due(at(6, 29, 59)), [])
        self.assertEqual(self.due(at(6, 30, 5)), ["wake:30"])
        self.assertEqual(self.due(at(6, 31, 0)), [])                # once a day
        self.assertEqual(self.due(at(7, 0, 0)), [])

    def test_too_late_is_skipped(self):
        self.m.set_schedules([{"time": "07:00", "action": "wake:45"}])
        self.assertEqual(self.due(at(6, 15, 0) + datetime.timedelta(minutes=3)), [])

    def test_stays_within_the_day(self):
        self.m.set_schedules([{"time": "00:10", "action": "wake:30"}])
        self.assertEqual(self.due(at(0, 0, 10)), ["wake:30"])

    def test_follows_the_sun(self):
        self.m.set_schedules([{"time": "sunrise", "offset": 0, "action": "wake:15"}])
        self.assertEqual(self.due(at(5, 15, 0)), ["wake:15"])

    def test_name_for_the_log(self):
        self.assertEqual(self.m.schedule_name({"time": "07:00", "action": "wake:30"}), "07:00 (wake-up from 06:30)")
        self.assertEqual(self.m.schedule_name({"time": "07:00", "action": "off"}), "07:00")

    def test_validation(self):
        for ok in ("wake:15", "wake:30", "wake:45"):
            self.m.set_schedules([{"time": "07:00", "action": ok}])
            self.assertEqual(self.config.data["schedules"][0]["action"], ok)
        for bad in ("wake:20", "wake:", "wake:30 "):
            with self.assertRaises(ValueError):
                self.m.set_schedules([{"time": "07:00", "action": bad}])


if __name__ == "__main__":
    unittest.main()
