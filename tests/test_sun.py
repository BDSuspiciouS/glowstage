import time
import unittest
from datetime import datetime, timezone

from glowstage import sun
from glowstage.config import DEFAULT_PRESETS, upgrade_presets
from glowstage.effects import EFFECTS
from glowstage.state import LightState


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class Elevation(unittest.TestCase):
    AMSTERDAM = (52.37, 4.90)

    def test_highest_sun_of_the_year(self):
        # Midsummer around noon: 90 - latitude + the earth's tilt.
        top = max(sun.elevation(*self.AMSTERDAM, utc(2026, 6, 21, 11, m)) for m in range(0, 60, 2))
        self.assertAlmostEqual(top, 90 - 52.37 + 23.44, delta=0.3)

    def test_lowest_noon_of_the_year(self):
        top = max(sun.elevation(*self.AMSTERDAM, utc(2026, 12, 21, 11, m)) for m in range(0, 60, 2))
        self.assertAlmostEqual(top, 90 - 52.37 - 23.44, delta=0.3)

    def test_sunset(self):
        # Amsterdam, 21 June 2026: the sun sets at about 22:06 CEST (20:06 UTC).
        self.assertAlmostEqual(sun.elevation(*self.AMSTERDAM, utc(2026, 6, 21, 20, 6)), sun.SUNRISE, delta=0.4)


class Look(unittest.TestCase):
    def setUp(self):
        # A place in the middle of this computer's own time zone, so local midnight is night there.
        self.sun = sun.Sun()
        self.sun._place = ("test", 52.0, -time.timezone / 3600 * 15)

    def at(self, month, day, hour, minute=0) -> dict:
        return self.sun.look(datetime(2026, month, day, hour, minute).timestamp())

    def test_night_is_a_low_amber_glow(self):
        night = self.at(1, 15, 1)
        self.assertEqual(night["phase"], "night")
        self.assertLess(night["level"], 0.3)
        r, g, b = night["rgb"]
        self.assertGreater(r, g)
        self.assertLess(b, 30)

    def test_summer_noon_is_cool_and_bright(self):
        noon = self.at(6, 21, 12, 30 if time.localtime().tm_isdst else 0)
        self.assertEqual(noon["phase"], "daylight")
        self.assertEqual(noon["level"], 1.0)
        self.assertGreaterEqual(noon["kelvin"], 5500)

    def test_the_evening_is_warm_and_the_night_fades_in(self):
        evening, late, night = self.at(1, 15, 20), self.at(1, 15, 23), self.at(1, 15, 23, 45)
        self.assertEqual(evening["phase"], "evening")
        self.assertGreater(evening["level"], late["level"])
        self.assertGreater(late["level"], night["level"])

    def test_sunrise_and_sunset_times(self):
        rise, set_ = self.sun.times(datetime(2026, 3, 20))   # near the equinox: 12 hours of day
        h = lambda s: int(s[:2]) + int(s[3:]) / 60             # noqa: E731
        self.assertAlmostEqual(h(set_) - h(rise), 12.1, delta=0.3)

    def test_the_light_never_jumps(self):
        start = datetime(2026, 11, 27, 0, 0).timestamp()
        prev = None
        for k in range(0, 24 * 60, 1):
            look = self.sun.look(start + k * 60)
            rgb = [v * look["level"] for v in look["rgb"]]
            if prev:
                self.assertLess(max(abs(a - b) for a, b in zip(rgb, prev)), 4, f"minute {k}")
            prev = rgb


class InTheApp(unittest.TestCase):
    def test_effect(self):
        fx = EFFECTS["daylight"]
        self.assertEqual(fx.describe()["uses"], [])
        colors = fx.render(0.0, 5, LightState(), {}, {})
        self.assertEqual(len(colors), 5)
        self.assertEqual(len(set(colors)), 1)

    def test_older_installs_get_the_preset(self):
        old = [p for p in DEFAULT_PRESETS if p["name"] != "Follow the sun"]
        names = [p["name"] for p in upgrade_presets(old)]
        self.assertEqual(names[:3], ["Rainbow", "Warm white", "Follow the sun"])


if __name__ == "__main__":
    unittest.main()
