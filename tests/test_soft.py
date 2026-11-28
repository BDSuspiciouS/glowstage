import tempfile
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.soft import FALL, RISE, STALE, Softener

DARK, FULL = (0, 0, 0), (255, 255, 255)


class Limits(unittest.TestCase):
    def setUp(self):
        self.s = Softener()

    def test_the_first_frame_is_the_look_itself(self):
        self.assertEqual(self.s.apply("a", [FULL, DARK], 10.0), [FULL, DARK])

    def test_a_flash_swells_in(self):
        self.s.apply("a", [DARK], 10.0)
        steps = [self.s.apply("a", [FULL], 10.0 + k / 30)[0][0] for k in range(1, 12)]
        self.assertEqual(steps, sorted(steps))                     # always on the way up
        self.assertLess(steps[0], 40)                              # not a jump
        self.assertEqual(steps[round(RISE * 30) - 1], 255)         # full after RISE seconds
        self.assertLess(steps[round(RISE * 30) - 3], 255)

    def test_and_dims_softly(self):
        self.s.apply("a", [FULL], 10.0)
        self.assertLess(self.s.apply("a", [DARK], 10.0 + FALL / 2)[0][0], 140)
        self.assertGreater(self.s.apply("a", [DARK], 10.0 + FALL / 2)[0][0], 100)   # (same moment: no further)
        self.assertEqual(self.s.apply("a", [DARK], 10.0 + FALL * 1.01), [DARK])

    def test_a_short_flash_never_gets_bright(self):
        """A strike of a tenth of a second (the light show, Thunderstorm) stays a glow."""
        self.s.apply("a", [DARK], 10.0)
        peak = 0
        for k in range(1, 31):
            t = 10.0 + k / 60
            peak = max(peak, self.s.apply("a", [FULL if k <= 6 else DARK], t)[0][0])
        self.assertLess(peak, 0.4 * 255)

    def test_each_channel_and_each_led(self):
        self.s.apply("a", [(255, 0, 0), (0, 0, 255)], 10.0)
        out = self.s.apply("a", [(0, 0, 255), (0, 0, 255)], 10.05)
        self.assertEqual(out[1], (0, 0, 255))                      # nothing changed there
        self.assertGreater(out[0][0], 0)                           # red on its way down
        self.assertLess(out[0][2], 255)                            # blue on its way up

    def test_lights_are_apart(self):
        self.s.apply("a", [DARK], 10.0)
        self.assertEqual(self.s.apply("b", [FULL], 10.03), [FULL])

    def test_a_new_look_after_a_pause_starts_at_once(self):
        self.s.apply("a", [DARK], 10.0)
        self.assertEqual(self.s.apply("a", [FULL], 10.0 + STALE + 0.1), [FULL])

    def test_a_light_that_changed_size_starts_again(self):
        self.s.apply("a", [DARK], 10.0)
        self.assertEqual(self.s.apply("a", [FULL, FULL], 10.03), [FULL, FULL])

    def test_forget(self):
        self.s.apply("a", [DARK], 10.0)
        self.s.forget()
        self.assertEqual(self.s.apply("a", [FULL], 10.03), [FULL])


class FakeStrip(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online, self.streaming, self.pixel_count = True, True, 4
        self.frames = []

    def apply(self, state):
        pass

    def send_frame(self, colors):
        self.frames.append(colors)


class InTheEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.d = FakeStrip("a")
        self.m._register(self.d)
        self.m.update(None, {"on": True, "mode": "effect", "effect": "rainbow", "speed": 50})
        self.e = self.m.engine
        self.e.show.update = self.fake_show

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def fake_show(self, heard, now, intensity, strobe, sync):
        self.strobe = strobe
        return {}

    def frames(self, renders, start=1000.0):
        """Two frames 1/30 s apart, each with `renders` as what the look draws."""
        out = []
        for k, colors in enumerate(renders):
            self.e._last.clear()
            self.e._mem.clear()
            fx = self.m.states["a"].effect
            from glowstage.effects import EFFECTS
            real = EFFECTS[fx].render
            EFFECTS[fx].render = lambda *a, c=colors: [c] * 4
            try:
                self.e._frame(start + k / 30)
            finally:
                EFFECTS[fx].render = real
            out.append(self.d.frames[-1][0])
        return out

    def test_off_by_default(self):
        self.assertEqual(self.frames([DARK, FULL]), [DARK, FULL])

    def test_on_a_jump_becomes_a_swell(self):
        self.m.config["settings"]["soft_flashes"] = True
        first, second = self.frames([DARK, FULL])
        self.assertEqual(first, DARK)
        self.assertLess(second[0], 60)
        self.assertEqual(self.e._live["a"][1][0], second)          # the window shows what the light shows

    def test_switching_it_off_lets_go_at_once(self):
        self.m.config["settings"]["soft_flashes"] = True
        self.frames([DARK])
        self.m.config["settings"]["soft_flashes"] = False
        self.assertEqual(self.frames([FULL], start=1000.1), [FULL])

    def test_no_strobes_in_the_light_show(self):
        self.m.update(None, {"effect": "music_show"})
        self.m.config["settings"]["show_strobe"] = True
        self.frames([DARK])
        self.assertTrue(self.strobe)
        self.m.config["settings"]["soft_flashes"] = True
        self.frames([DARK], start=1001.0)
        self.assertFalse(self.strobe)


if __name__ == "__main__":
    unittest.main()
