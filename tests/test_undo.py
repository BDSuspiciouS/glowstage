import tempfile
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.hotkeys import Hotkeys
from glowstage.manager import DeviceManager


class FakeLight(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True

    def apply(self, state):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        for i in ("desk", "lamp"):
            self.m._register(FakeLight(i))
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 50})
        self.m.sleep._thread = type("Alive", (), {"is_alive": lambda self: True})()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def look(self, i="desk"):
        st = self.m.states[i]
        return st.on, st.mode, st.color, st.brightness


class Undo(Base):
    def test_nothing_to_undo_at_first(self):
        self.assertFalse(self.m.can_undo())
        self.assertFalse(self.m.undo())

    def test_a_preset_and_back(self):
        before = self.look()
        self.m.remember()
        self.m.apply_preset("Warm white")
        self.assertNotEqual(self.look(), before)
        self.assertTrue(self.m.undo())
        self.assertEqual(self.look(), before)

    def test_undo_twice_is_redo(self):
        self.m.remember()
        self.m.apply_preset("Warm white")
        after = self.look()
        self.m.undo()
        self.m.undo()
        self.assertEqual(self.look(), after)
        self.assertEqual(self.m.config["settings"]["active_preset"], "Warm white")

    def test_all_off_and_back(self):
        self.m.remember()
        self.m.set_power_all(False)
        self.m.undo()
        self.assertTrue(self.m.states["desk"].on and self.m.states["lamp"].on)


class Brightness(Base):
    def test_each_light_from_where_it_is(self):
        self.m.update(["lamp"], {"brightness": 95})
        self.m.nudge_brightness(10)
        self.assertEqual((self.m.states["desk"].brightness, self.m.states["lamp"].brightness), (60, 100))
        for _ in range(10):
            self.m.nudge_brightness(-10)
        self.assertEqual(self.m.states["desk"].brightness, 1)

    def test_lights_that_are_off_stay_off(self):
        self.m.update(["lamp"], {"on": False})
        self.m.nudge_brightness(10)
        self.assertFalse(self.m.states["lamp"].on)
        self.assertEqual(self.m.states["lamp"].brightness, 50)


class Keys(Base):
    def test_hotkeys(self):
        k = Hotkeys(self.m)
        k._pressed("Up")
        self.assertEqual(self.m.states["desk"].brightness, 60)
        k._pressed("Down")
        k._pressed("Down")
        self.assertEqual(self.m.states["desk"].brightness, 40)
        k._pressed("2")                         # the second preset: Warm white
        self.assertEqual(self.m.config["settings"]["active_preset"], "Warm white")
        k._pressed("Z")
        self.assertEqual(self.look()[2], "#123456")
        k._pressed("N")
        self.assertTrue(self.m.sleep.active)
        k._pressed("N")
        self.assertFalse(self.m.sleep.active)
        k._pressed("L")
        self.assertFalse(self.m.any_on())
        k._pressed("Z")
        self.assertTrue(self.m.any_on())


if __name__ == "__main__":
    unittest.main()
