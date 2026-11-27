import tempfile
import unittest
from pathlib import Path

from glowstage.api import Api
from glowstage.config import Config


class FakeApp:
    def __init__(self, config):
        self.config = config


class RecentColors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.api = Api(FakeApp(self.config))

    def tearDown(self):
        self.tmp.cleanup()

    def recent(self):
        return self.config["settings"]["recent_colors"]

    def test_starts_empty(self):
        self.assertEqual(self.recent(), [])

    def test_newest_first_and_lower_case(self):
        self.assertTrue(self.api.remember_color("#FF8800")["ok"])
        self.api.remember_color("#00ff00")
        self.assertEqual(self.recent(), ["#00ff00", "#ff8800"])

    def test_no_duplicates_it_moves_to_the_front(self):
        for c in ("#111111", "#222222", "#333333"):
            self.api.remember_color(c)
        self.api.remember_color("#111111")
        self.api.remember_color("#222222")
        self.assertEqual(self.recent(), ["#222222", "#111111", "#333333"])

    def test_keeps_eight(self):
        for n in range(12):
            self.api.remember_color(f"#0000{n:02x}")
        self.assertEqual(len(self.recent()), 8)
        self.assertEqual(self.recent()[0], "#00000b")
        self.assertEqual(self.recent()[-1], "#000004")

    def test_bad_values_are_refused(self):
        for bad in ("", None, "ff8800", "#ff88", "#ff88000", "#gg0000", "red", "#ff8800; x"):
            self.assertFalse(self.api.remember_color(bad)["ok"], bad)
        self.assertEqual(self.recent(), [])

    def test_saved_in_the_settings_file(self):
        self.api.remember_color("#abcdef")
        self.config.save_now()
        again = Config(self.config.path)
        self.assertEqual(again["settings"]["recent_colors"], ["#abcdef"])


if __name__ == "__main__":
    unittest.main()
