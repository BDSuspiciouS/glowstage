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


class Presets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.m._register(FakeLight("desk"))
        self.m.update(None, {"on": True, "color": "#112233"})

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def names(self):
        return self.m.preset_names()

    def test_saving_over_keeps_the_place(self):
        self.m.save_preset("Mine")
        self.m.save_preset("Other")
        before = self.names()
        self.m.update(None, {"color": "#445566"})
        self.m.save_preset("Mine")
        self.assertEqual(self.names(), before)
        saved = next(p for p in self.m.config["presets"] if p["name"] == "Mine")
        self.assertEqual(saved["states"]["desk"]["color"], "#445566")

    def test_rename_everywhere(self):
        c = self.m.config
        c["settings"].update(active_preset="Focus", auto_game="preset:Focus", auto_video="screen")
        c.data["schedules"] = [{"time": "09:00", "days": [0], "action": "preset:Focus", "enabled": True}]
        c.data["app_looks"] = [{"exe": "photoshop.exe", "name": "Photoshop", "action": "preset:Focus"}]
        at = self.names().index("Focus")
        self.m.rename_preset("Focus", "Deep work")
        self.assertEqual(self.names()[at], "Deep work")
        self.assertNotIn("Focus", self.names())
        self.assertEqual(c["settings"]["active_preset"], "Deep work")
        self.assertEqual(c["settings"]["auto_game"], "preset:Deep work")
        self.assertEqual(c["settings"]["auto_video"], "screen")
        self.assertEqual(c.data["schedules"][0]["action"], "preset:Deep work")
        self.assertEqual(c.data["app_looks"][0]["action"], "preset:Deep work")

    def test_rename_refuses(self):
        with self.assertRaises(ValueError):
            self.m.rename_preset("Focus", "Reading")      # taken
        with self.assertRaises(ValueError):
            self.m.rename_preset("Focus", "  ")
        with self.assertRaises(ValueError):
            self.m.rename_preset("Nope", "Something")

    def test_move(self):
        first, second = self.names()[:2]
        self.m.move_preset(second, -1)
        self.assertEqual(self.names()[:2], [second, first])
        self.m.move_preset(second, -1)                    # already first: stays
        self.assertEqual(self.names()[0], second)
        last = self.names()[-1]
        self.m.move_preset(last, 1)
        self.assertEqual(self.names()[-1], last)


if __name__ == "__main__":
    unittest.main()
