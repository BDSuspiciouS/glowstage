import tempfile
import time
import unittest
from pathlib import Path

from glowstage import games
from glowstage.config import Config


class SlowDisk(unittest.TestCase):
    """A Steam library on a hard disk that spun down: the window must never wait for it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "cfg"
        self.cfg.mkdir()
        self._find, self._find_dota = games.find_cs2, games.find_dota2
        games.find_dota2 = lambda: None
        self._find_rl, games.find_rocket_league = games.find_rocket_league, lambda: None
        games.find_cs2 = self.slow_find
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: None)

    def tearDown(self):
        games.find_cs2, games.find_dota2, games.find_rocket_league = self._find, self._find_dota, self._find_rl
        self.tmp.cleanup()

    def slow_find(self):
        time.sleep(1.0)
        return self.cfg

    def test_the_window_doesnt_wait(self):
        started = time.monotonic()
        first = self.g.describe()["cs2"]
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(first["found"])            # not known yet: looked up in the background
        time.sleep(1.3)
        started = time.monotonic()
        self.assertTrue(self.g.describe()["cs2"]["found"])
        self.assertLess(time.monotonic() - started, 0.2)

    def test_connecting_looks_right_away(self):
        self.g._listen = lambda: None
        self.g.connect()
        d = self.g.describe()["cs2"]
        self.assertTrue(d["found"] and d["connected"] and d["fileThere"])
        self.g.disconnect()
        self.assertFalse(self.g.describe()["cs2"]["fileThere"])



class SlowOpenRGBFolder(unittest.TestCase):
    """The window asks where OpenRGB.exe is every second: a slow folder must not hold it up."""

    def setUp(self):
        from glowstage.devices import openrgb_dev
        self.mod = openrgb_dev
        self.p = openrgb_dev.OpenRGBProvider({}, lambda: None)
        self.looks = 0
        self.p.exe_path = self.slow_exe

    def slow_exe(self):
        self.looks += 1
        time.sleep(0.5)
        return Path("C:/OpenRGB/OpenRGB.exe")

    def test_the_answer_is_kept(self):
        self.assertEqual(self.p.found()[0], Path("C:/OpenRGB/OpenRGB.exe"))   # (the first time: looked up)
        started = time.monotonic()
        for _ in range(5):
            self.p.found()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(self.looks, 1)

    def test_an_old_answer_is_renewed_in_the_background(self):
        self.p.found()
        at, key, exe, bundled = self.p._found
        self.p._found = (at - self.mod.FIND_EVERY - 1, key, exe, bundled)
        started = time.monotonic()
        self.p.found()
        self.assertLess(time.monotonic() - started, 0.1)
        time.sleep(0.8)
        self.assertEqual(self.looks, 2)

    def test_a_path_you_set_counts_at_once(self):
        self.p.found()
        self.p.settings["path"] = "D:/Tools/OpenRGB.exe"
        self.p.found()
        self.assertEqual(self.looks, 2)


if __name__ == "__main__":
    unittest.main()
