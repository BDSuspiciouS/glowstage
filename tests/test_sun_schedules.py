import datetime
import tempfile
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.manager import DeviceManager

DAY = datetime.date(2026, 6, 10)


def at(hh, mm, ss=0):
    return datetime.datetime.combine(DAY, datetime.time(hh, mm, ss))


class SunSchedules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.sun = ("05:30", "21:00")
        self.m.sun_times = lambda day: self.sun
        self.fired = {}

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def due(self, now):
        return [s["action"] for s in self.m.due_schedules(now, self.fired)]

    def test_clock_entries_fire_as_before(self):
        self.m.set_schedules([{"time": "23:00", "action": "off"}])
        self.assertEqual(self.due(at(22, 59, 59)), [])
        self.assertEqual(self.due(at(23, 0, 5)), ["off"])
        self.assertEqual(self.due(at(23, 1, 0)), [])          # once a day
        self.assertEqual(self.due(at(23, 5, 0)), [])          # too late
        self.assertEqual(self.m.config.data["schedules"][0].get("offset", 0), 0)

    def test_old_entries_without_offset_load_and_fire(self):
        self.m.config.data["schedules"] = [{"time": "07:00", "days": list(range(7)), "action": "on", "enabled": True}]
        self.assertEqual(self.due(at(7, 0, 30)), ["on"])

    def test_sunset_minus_30_fires_once(self):
        self.m.set_schedules([{"time": "sunset", "offset": -30, "action": "preset:Cozy"}])
        self.assertEqual(self.due(at(20, 29, 0)), [])
        self.assertEqual(self.due(at(20, 30, 10)), ["preset:Cozy"])
        self.assertEqual(self.due(at(20, 31, 0)), [])
        self.assertEqual(self.due(at(21, 0, 0)), [])          # not at sunset itself

    def test_sunrise_plus_15(self):
        self.m.set_schedules([{"time": "sunrise", "offset": 15, "action": "on"}])
        self.assertEqual(self.due(at(5, 30, 0)), [])
        self.assertEqual(self.due(at(5, 45, 0)), ["on"])

    def test_a_day_without_sunset_never_fires(self):
        self.sun = ("", "")
        self.m.set_schedules([{"time": "sunset", "offset": 0, "action": "off"},
                              {"time": "sunrise", "offset": 0, "action": "on"}])
        for hh in range(24):
            self.assertEqual(self.due(at(hh, 0, 0)), [])

    def test_two_sun_schedules_do_not_block_each_other(self):
        self.m.set_schedules([{"time": "sunset", "offset": -30, "action": "off"},
                              {"time": "sunset", "offset": -30, "action": "on"},
                              {"time": "sunset", "offset": -29, "action": "off"}])
        self.assertEqual(self.due(at(20, 30, 0)), ["off", "on"])
        self.assertEqual(self.due(at(20, 31, 0)), ["off"])

    def test_offset_stays_within_the_day(self):
        self.sun = ("00:10", "23:50")
        self.m.set_schedules([{"time": "sunrise", "offset": -60, "action": "on"},
                              {"time": "sunset", "offset": 60, "action": "off"}])
        self.assertEqual(self.due(at(0, 0, 0)), ["on"])
        self.assertEqual(self.due(at(23, 59, 30)), ["off"])

    def test_validation(self):
        for bad in ({"time": "noon", "action": "off"},
                    {"time": "sunset", "offset": 181, "action": "off"},
                    {"time": "sunset", "offset": -181, "action": "off"},
                    {"time": "sunrise", "offset": 1.5, "action": "off"},
                    {"time": "sunrise", "offset": "15", "action": "off"},
                    {"time": "sunset", "offset": 0, "action": "dance"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.m.set_schedules([bad])

    def test_stored_shape_and_order(self):
        self.m.set_schedules([{"time": "sunset", "offset": -30, "action": "off", "days": [1, 0]},
                              {"time": "23:00", "action": "off"},
                              {"time": "sunrise", "action": "on"},
                              {"time": "06:00", "action": "on"}])
        got = self.m.config.data["schedules"]
        self.assertEqual([(s["time"], s.get("offset", 0)) for s in got],
                         [("sunrise", 0), ("06:00", 0), ("sunset", -30), ("23:00", 0)])
        self.assertEqual(got[2], {"time": "sunset", "offset": -30, "days": [0, 1], "action": "off", "enabled": True})

    def test_a_sun_time_that_does_not_happen_today_goes_last(self):
        self.sun = ("", "")
        self.m.set_schedules([{"time": "sunset", "action": "off"}, {"time": "23:00", "action": "off"}])
        self.assertEqual([s["time"] for s in self.m.config.data["schedules"]], ["23:00", "sunset"])

    def test_log_names(self):
        name = DeviceManager.schedule_name
        self.assertEqual(name({"time": "sunset", "offset": -30}), "sunset-30")
        self.assertEqual(name({"time": "sunrise", "offset": 15}), "sunrise+15")
        self.assertEqual(name({"time": "sunset", "offset": 0}), "sunset")
        self.assertEqual(name({"time": "23:00"}), "23:00")


if __name__ == "__main__":
    unittest.main()
