import tempfile
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.effects import EFFECTS
from glowstage.manager import DeviceManager
from glowstage.state import LightState

FLOWING = ("rainbow", "spectrum", "breathing", "wave", "comet", "aurora", "ocean", "lava", "storm",
           "snow", "string_lights", "candy_cane")
ST = LightState().patched({"color": "#ff0000", "color2": "#0000ff", "speed": 50})


class Effects(unittest.TestCase):
    def frame(self, fx, span=None, t=1.3, n=6):
        ctx = {"flow": True, "span": span} if span else {}
        return EFFECTS[fx].render(t, n, ST, {}, ctx)

    def test_without_flow_every_light_is_the_same(self):
        for fx in FLOWING:
            self.assertEqual(EFFECTS[fx].render(1.3, 6, ST, {}, {"span": (0.0, 0.3, "front")}),
                             EFFECTS[fx].render(1.3, 6, ST, {}, {"span": (0.7, 1.0, "front")}), fx)

    def test_with_flow_lights_on_the_left_and_right_differ(self):
        for fx in FLOWING:
            self.assertNotEqual(self.frame(fx, (0.0, 0.3, "front")), self.frame(fx, (0.7, 1.0, "front")), fx)

    def test_only_these_flow(self):
        self.assertEqual({e.id for e in EFFECTS.values() if e.flows}, set(FLOWING) | {"pc_load", "gradient"})   # (tested in test_load, test_gradient)

    def test_the_comet_travels_from_light_to_light(self):
        left, right = (0.0, 0.3, "front"), (0.7, 1.0, "front")
        bright = lambda colors: max(sum(c) for c in colors)      # noqa: E731
        # (speed 50: the head goes once along the stage in ~6.7 s)
        early, late = 0.15 * 6.67, 0.85 * 6.67
        self.assertGreater(bright(self.frame("comet", left, early)), bright(self.frame("comet", right, early)))
        self.assertGreater(bright(self.frame("comet", right, late)), bright(self.frame("comet", left, late)))


class FakeStrip(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online, self.streaming, self.pixel_count = True, True, 6
        self.frames = []

    def apply(self, state):
        pass

    def send_frame(self, colors):
        self.frames.append(colors)


class Engine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.a, self.b = FakeStrip("a"), FakeStrip("b")
        for d in (self.a, self.b):
            self.m._register(d)
        self.m.config.data["show_layout"] = {"a": [0.0, 0.3, "front"], "b": [0.7, 1.0, "front"]}
        self.m.update(None, {"on": True, "mode": "effect", "effect": "rainbow", "speed": 50})

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def frame(self):
        self.m.engine._last.clear()
        self.m.engine._frame(1000.0)
        return self.a.frames[-1], self.b.frames[-1]

    def test_off_by_default(self):
        a, b = self.frame()
        self.assertEqual(a, b)

    def test_on(self):
        self.m.config["settings"]["flow"] = True
        a, b = self.frame()
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
