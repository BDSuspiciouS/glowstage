import threading
import time
import unittest

from glowstage.devices.base import Device
from glowstage.manager import DeviceWorker, solid
from glowstage.state import LightState


class FakeLight(Device):
    """Writes down every color it shows (and when), like a local light would show it."""

    fades = True
    max_fps = 30

    def __init__(self, fades=True):
        super().__init__("fake", "Fake")
        self.online = True
        self.fades = fades
        self.shown: list[tuple] = []
        self.effects = 0
        self.changed = threading.Event()

    def show_color(self, rgb, state):
        f = state.brightness / 100
        self.shown.append(tuple(round(c * f) for c in rgb))
        self.changed.set()

    def power_off(self):
        self.shown.append("off")
        self.changed.set()

    def begin_stream(self, state):
        self.effects += 1
        self.changed.set()


def color(hex_, brightness=100, on=True):
    return LightState(on=on, mode="color", color=hex_, brightness=brightness)


class Glides(unittest.TestCase):
    def setUp(self):
        self.dev = FakeLight()
        self.w = DeviceWorker(self.dev, on_error=lambda d, e: self.fail(e))
        self.w.FADE = 0.12          # (quicker than in the app, so the tests are)

    def settle(self, timeout=2.0):
        """Wait until the worker is done with everything it was given."""
        end = time.monotonic() + timeout
        while self.w.busy and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(self.w.busy)

    def first(self, state):
        self.w.submit(state)
        self.settle()
        self.dev.shown.clear()
        time.sleep(self.w.QUICK + 0.05)     # (so what comes next isn't part of a drag)

    def test_the_first_look_comes_at_once(self):
        self.w.submit(color("#ff0000"))
        self.settle()
        self.assertEqual(self.dev.shown, [(255, 0, 0)])

    def test_a_new_color_glides_in(self):
        self.first(color("#ff0000"))
        self.w.submit(color("#0000ff"))
        self.settle()
        steps = self.dev.shown
        self.assertGreater(len(steps), 2)
        self.assertEqual(steps[-1], (0, 0, 255))
        reds = [c[0] for c in steps]
        self.assertEqual(reds, sorted(reds, reverse=True))      # always on the way, never back

    def test_switching_off_fades_to_black_then_off(self):
        self.first(color("#ffffff"))
        self.w.submit(color("#ffffff", on=False))
        self.settle()
        self.assertEqual(self.dev.shown[-1], "off")
        levels = [c[0] for c in self.dev.shown[:-1]]
        self.assertTrue(levels and levels == sorted(levels, reverse=True))

    def test_switching_on_comes_up_from_black(self):
        self.first(color("#ffffff", on=False))
        self.w.submit(color("#ffffff"))
        self.settle()
        self.assertLess(self.dev.shown[0][0], 128)
        self.assertEqual(self.dev.shown[-1], (255, 255, 255))

    def test_brightness_glides_too(self):
        self.first(color("#ffffff", brightness=100))
        self.w.submit(color("#ffffff", brightness=20))
        self.settle()
        self.assertGreater(len(self.dev.shown), 2)
        self.assertEqual(self.dev.shown[-1], (51, 51, 51))

    def test_a_drag_keeps_up_with_your_hand(self):
        self.first(color("#000000", brightness=100))
        for level in range(10, 101, 10):                         # a slider dragged, ~60 updates a second
            self.w.submit(color("#ffffff", brightness=level))
            time.sleep(0.016)
        self.settle()
        self.assertEqual(self.dev.shown[-1], (255, 255, 255))
        start = time.monotonic()
        self.w.submit(color("#ffffff", brightness=50))           # still part of the drag: at once
        self.settle()
        self.assertLess(time.monotonic() - start, self.w.FADE)
        self.assertEqual(self.dev.shown[-1], (128, 128, 128))

    def test_a_new_look_on_the_way_glides_on_from_there(self):
        self.first(color("#ff0000"))
        self.w.FADE = 0.4
        self.w.submit(color("#0000ff"))
        time.sleep(0.12)
        middle = self.dev.shown[-1]
        self.w.submit(color("#00ff00"), instant=True)
        self.settle()
        self.assertNotEqual(middle, (255, 0, 0))
        self.assertEqual(self.dev.shown[-1], (0, 255, 0))
        self.assertNotIn((0, 0, 255), self.dev.shown)            # it never got to blue

    def test_instant_is_instant(self):
        self.first(color("#ff0000"))
        self.w.submit(color("#ffffff"), instant=True)
        self.settle()
        self.assertEqual(self.dev.shown, [(255, 255, 255)])

    def test_effects_are_not_faded(self):
        self.first(color("#ff0000"))
        self.w.submit(LightState(mode="effect", effect="rainbow"))
        self.settle()
        self.assertEqual((self.dev.shown, self.dev.effects), ([], 1))
        time.sleep(self.w.QUICK + 0.05)
        self.w.submit(color("#00ff00"))                          # from an effect: no known color to glide from
        self.settle()
        self.assertEqual(self.dev.shown, [(0, 255, 0)])

    def test_lights_that_dont_fade(self):
        self.dev.fades = False
        self.first(color("#ff0000"))
        self.w.submit(color("#0000ff"))
        self.settle()
        self.assertEqual(self.dev.shown, [(0, 0, 255)])

    def test_an_offline_light_starts_fresh(self):
        self.first(color("#ff0000"))
        self.dev.online = False
        self.w.submit(color("#00ff00"))
        self.settle()
        self.dev.online = True
        time.sleep(self.w.QUICK + 0.05)
        self.w.submit(color("#0000ff"))
        self.settle()
        self.assertEqual(self.dev.shown, [(0, 0, 255)])          # back online: what it shows isn't known


class Solid(unittest.TestCase):
    def test_what_a_light_shows(self):
        self.assertEqual(solid(color("#ff8000", brightness=50)), (128, 64, 0))
        self.assertEqual(solid(color("#ff8000", on=False)), (0, 0, 0))
        self.assertIsNone(solid(LightState(mode="effect")))
        self.assertIsNone(solid(LightState(mode="native", native="Rainbow")))


class WhichLightsFade(unittest.TestCase):
    def test_the_local_quick_ones(self):
        from glowstage.devices import dmx, openrgb_dev, qmk_keyboard, wled, hue
        self.assertTrue(openrgb_dev.OpenRGBDevice.fades)
        self.assertTrue(qmk_keyboard.QmkKeyboard.fades)
        self.assertTrue(dmx.DmxFixture.fades)
        self.assertFalse(wled.WledDevice.fades)      # (WLED glides by itself: "tt" in its commands)
        self.assertFalse(hue.HueLight.fades)         # (a bridge takes ~10 commands a second for all its lights)


if __name__ == "__main__":
    unittest.main()
