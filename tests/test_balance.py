import tempfile
import time
import unittest
from pathlib import Path

from glowstage import diagnostics
from glowstage.config import Config
from glowstage.devices.base import KEEP_MIN, Device, balance_gains
from glowstage.manager import DeviceManager, DeviceWorker
from glowstage.state import LightState


class Strip(Device):
    """Writes down every color and frame it is sent, like a real light would be sent them."""

    fades = True
    max_fps = 30

    def __init__(self, dev_id="strip"):
        super().__init__(dev_id, dev_id)
        self.online, self.streaming, self.pixel_count = True, False, 4
        self.colors, self.frames = [], []

    def show_color(self, rgb, state):
        self.colors.append(tuple(rgb))

    def power_off(self):
        self.colors.append("off")

    def begin_stream(self, state):
        pass

    def send_frame(self, colors):
        self.frames.append(colors)


class Recorder(Device):
    """Only keeps the states it is asked to show."""

    def __init__(self, dev_id="desk"):
        super().__init__(dev_id, dev_id)
        self.online = True
        self.seen = []

    def apply(self, state):
        self.seen.append(state)


class Gains(unittest.TestCase):
    def test_neutral_is_nothing(self):
        self.assertIsNone(balance_gains(0, 100))
        self.assertIsNone(Device("x", "x").gains)

    def test_warmer_takes_blue_and_a_little_green_out(self):
        r, g, b = balance_gains(50, 100)
        self.assertEqual(r, 1.0)
        self.assertLess(b, g)
        self.assertLess(g, 1.0)
        mild = balance_gains(10, 100)
        self.assertGreater(mild[2], b)

    def test_cooler_takes_red_and_a_little_green_out(self):
        r, g, b = balance_gains(-50, 100)
        self.assertEqual(b, 1.0)
        self.assertLess(r, g)
        self.assertLess(g, 1.0)

    def test_no_channel_goes_below_the_floor(self):
        for warmth in (-500, -50, 50, 500):
            self.assertGreaterEqual(min(balance_gains(warmth, 100)), KEEP_MIN)

    def test_brightness_at_full_scales_everything(self):
        self.assertEqual(balance_gains(0, 70), (0.7, 0.7, 0.7))
        r, g, b = balance_gains(50, 50)
        w = balance_gains(50, 100)
        self.assertAlmostEqual(b, w[2] * 0.5)
        self.assertEqual(balance_gains(0, 5), (0.2, 0.2, 0.2))     # (20% is the lowest it goes)

    def test_neutral_is_exactly_a_no_op(self):
        d = Device("x", "x")
        d.set_balance(0, 100)
        rgb, frame = (12, 200, 255), [(1, 2, 3), (255, 255, 255)]
        self.assertIs(d.balanced(rgb), rgb)
        self.assertIs(d.balanced_frame(frame), frame)

    def test_a_balanced_light_changes_colors(self):
        d = Device("x", "x")
        d.set_balance(50, 80)
        r, g, b = d.balanced((255, 255, 255))
        self.assertEqual(r, 204)
        self.assertLess(b, g)
        self.assertLess(g, r)
        self.assertEqual(d.balanced((0, 0, 0)), (0, 0, 0))
        self.assertEqual(d.balanced_frame([(255, 255, 255), (0, 0, 0)]), [(r, g, b), (0, 0, 0)])
        d.set_balance(0, 100)                                       # back to neutral
        self.assertEqual(d.balanced((255, 255, 255)), (255, 255, 255))


class OutputPoints(unittest.TestCase):
    def test_a_solid_color_goes_through_it(self):
        dev = Strip()
        dev.set_balance(0, 50)
        dev.apply(LightState(on=True, mode="color", color="#ffffff", brightness=100))
        self.assertEqual(dev.colors, [(128, 128, 128)])

    def test_the_steps_of_a_fade_go_through_it(self):
        dev = Strip()
        dev.set_balance(0, 50)
        w = DeviceWorker(dev, on_error=lambda d, e: self.fail(e))
        w.FADE = 0.1
        w.submit(LightState(on=True, mode="color", color="#ffffff"))
        self.settle(w)
        time.sleep(w.QUICK + 0.05)
        dev.colors.clear()
        w.submit(LightState(on=True, mode="color", color="#000000"))
        self.settle(w)
        self.assertGreater(len(dev.colors), 2)
        self.assertTrue(all(c[0] <= 128 for c in dev.colors if c != "off"))   # never above half
        self.assertEqual(w._shown, (0, 0, 0))

    def settle(self, w):
        end = time.monotonic() + 2
        while w.busy and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(w.busy)


class Frames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.dev = Strip()
        self.m._register(self.dev)
        self.m.update(None, {"on": True, "mode": "effect", "effect": "rainbow", "speed": 50})
        self.dev.streaming = True

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def frame(self):
        self.m.engine._last.clear()
        self.m.engine._frame(1000.0)
        return self.dev.frames[-1], self.m.engine._live["strip"][1]

    def test_neutral_sends_the_frame_as_drawn(self):
        sent, live = self.frame()
        self.assertEqual(sent, live)

    def test_frames_are_corrected_but_what_the_window_shows_is_not(self):
        sent, live = self.frame()
        self.m.set_balance("strip", 40, 60)
        sent2, live2 = self.frame()
        self.assertEqual(live2, live)                        # the look, not the correction
        self.assertNotEqual(sent2, live2)
        self.assertEqual(sent2, self.dev.balanced_frame(live2))
        self.assertTrue(all(max(c) <= 153 for c in sent2))   # (60% of 255)


class Saved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.m = DeviceManager(Config(self.path))
        self.dev = Recorder()
        self.m._register(self.dev)
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 50})
        self.settle()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def settle(self):
        self.m.wait_applied(["desk"], 2)
        time.sleep(0.05)

    def test_it_is_saved_and_comes_back(self):
        self.m.set_balance("desk", 20, 70)
        self.assertIsNotNone(self.dev.gains)
        self.m.config.save_now()
        again = DeviceManager(Config(self.path))
        try:
            fresh = Recorder()
            again._register(fresh)
            self.assertEqual(again.balance("desk"), {"warmth": 20, "max": 70, "order": "RGB"})
            self.assertEqual(fresh.gains, self.dev.gains)
            self.assertEqual(again.snapshot()["devices"][0]["balance"], {"warmth": 20, "max": 70, "order": "RGB"})
        finally:
            again._stop.set()

    def test_neutral_is_not_kept(self):
        self.m.set_balance("desk", 20, 70)
        self.m.set_balance("desk", 0, 100)
        self.assertNotIn("desk", self.m.config.data["balance"])
        self.assertIsNone(self.dev.gains)

    def test_values_are_kept_in_range(self):
        self.m.set_balance("desk", 900, 3)
        self.assertEqual(self.m.balance("desk"), {"warmth": 50, "max": 20, "order": "RGB"})

    def test_a_light_showing_a_color_shows_it_again_at_once(self):
        self.dev.seen.clear()
        self.m.set_balance("desk", 20, 70)
        self.settle()
        self.assertEqual(len(self.dev.seen), 1)
        self.assertEqual(self.dev.seen[0].to_dict(), self.m.states["desk"].to_dict())

    def test_the_brightness_numbers_do_not_change(self):
        self.m.set_balance("desk", 20, 70)
        self.assertEqual(self.m.states["desk"].brightness, 50)
        self.assertEqual(self.m.snapshot()["devices"][0]["state"]["brightness"], 50)

    def test_a_removed_light_does_not_break_anything(self):
        self.m.config.data["balance"]["gone"] = {"warmth": 30, "max": 60}
        self.m.config.data["balance"]["odd"] = {"warmth": "warm", "max": None}
        self.assertEqual(self.m.balance("odd"), {"warmth": 0, "max": 100, "order": "RGB"})
        self.m._register(Recorder("odd"))                    # (a hand-edited entry is read as neutral)
        self.assertIsNone(self.m.devices["odd"].gains)
        with self.assertRaisesRegex(ValueError, "isn't there any more"):
            self.m.set_balance("nope", 10, 100)
        with self.assertRaisesRegex(ValueError, "isn't there any more"):
            self.m.show_white("nope", True)
        self.m.snapshot()

    def test_the_report_lists_balanced_lights(self):
        class App:
            manager = self.m
            config = self.m.config
            remote = type("R", (), {"settings": {}})()
        self.m.set_balance("desk", -10, 80)
        self.m.openrgb.describe = lambda: {}
        text = diagnostics.report(App())
        self.assertIn("Balanced lights: desk (-10 warmth, 80% at full)", text)
        self.m.set_balance("desk", 0, 100)
        self.assertNotIn("Balanced lights", diagnostics.report(App()))


class ShowWhite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.dev = Recorder()
        self.m._register(self.dev)
        self.m.update(None, {"on": True, "mode": "color", "color": "#ff0000", "brightness": 40})
        self.settle()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def settle(self):
        self.m.wait_applied(["desk"], 2)
        time.sleep(0.05)

    def test_white_at_the_current_brightness_then_back_as_it_was(self):
        before = self.m.states["desk"].to_dict()
        self.m.show_white("desk", True)
        self.settle()
        shown = self.dev.seen[-1]
        self.assertEqual((shown.on, shown.mode, shown.color, shown.brightness), (True, "color", "#ffffff", 40))
        self.assertEqual(self.m.states["desk"].to_dict(), before)      # nothing is saved or changed
        self.m.show_white("desk", False)
        self.settle()
        self.assertEqual(self.dev.seen[-1].to_dict(), before)

    def test_the_white_follows_the_sliders(self):
        self.m.show_white("desk", True)
        self.settle()
        self.dev.seen.clear()
        self.m.set_balance("desk", 30, 80)
        self.settle()
        self.assertEqual(self.dev.seen[-1].color, "#ffffff")           # still the white, shown again
        self.m.show_white("desk", False)
        self.settle()
        self.assertEqual(self.dev.seen[-1].color, "#ff0000")

    def test_putting_it_back_twice_does_nothing(self):
        self.m.show_white("desk", False)
        self.settle()
        self.assertEqual(self.dev.seen[-1].color, "#ff0000")
        count = len(self.dev.seen)
        self.m.show_white("desk", False)
        self.settle()
        self.assertEqual(len(self.dev.seen), count)

    def test_a_light_that_is_off_shows_white_and_goes_back_off(self):
        self.m.update(["desk"], {"on": False})
        self.settle()
        self.m.show_white("desk", True)
        self.settle()
        self.assertTrue(self.dev.seen[-1].on)
        self.m.show_white("desk", False)
        self.settle()
        self.assertFalse(self.dev.seen[-1].on)

    def test_an_offline_light_is_refused(self):
        self.dev.online = False
        with self.assertRaisesRegex(ValueError, "desk isn't answering"):
            self.m.show_white("desk", True)
        self.assertEqual(self.m._white, {})


if __name__ == "__main__":
    unittest.main()
