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
        self._find = games.find_cs2
        games.find_cs2 = self.slow_find
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: None)

    def tearDown(self):
        games.find_cs2 = self._find
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


if __name__ == "__main__":
    unittest.main()
