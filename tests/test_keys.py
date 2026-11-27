import math
import unittest

from glowstage import keys
from glowstage.devices.qmk_keyboard import LAYOUTS
from glowstage.effects import EFFECTS
from glowstage.state import LightState

GX87 = LAYOUTS[(0xFFFE, 0x006F)]


class FakeKeyboard:
    """GetAsyncKeyState as Windows answers it: 0x8000 while down, 1 once after a press."""

    def __init__(self):
        self.down, self.tapped = set(), set()

    def __call__(self, vk):
        state = (0x8000 if vk in self.down else 0) | (1 if vk in self.tapped else 0)
        self.tapped.discard(vk)
        return state


class Watching(unittest.TestCase):
    def setUp(self):
        self.board = FakeKeyboard()
        self.w = keys.KeyWatcher(self.board)
        self.held: set[int] = set()

    def presses(self):
        return [vk for _, vk in self.w._presses]

    def test_a_press_counts_once_however_long_it_is_held(self):
        self.board.down.add(ord("A"))
        self.board.tapped.add(ord("A"))
        for k in range(5):
            self.w.poll(k * 0.016, self.held)
        self.assertEqual(self.presses(), [ord("A")])

    def test_a_quick_tap_between_two_looks(self):
        self.board.tapped.add(ord("K"))          # down and up again before we looked
        self.w.poll(0.0, self.held)
        self.assertEqual(self.presses(), [ord("K")])

    def test_old_presses_are_forgotten(self):
        self.w._presses.append((0.0, ord("Q")))
        self.w._presses.append((2.5, ord("W")))
        self.w._wanted_until = 1e9
        self.w._thread = type("Alive", (), {"is_alive": lambda self: True})()
        got = self.w.recent(4.0)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][0], 1.5)
        self.assertEqual(got[0][1:], keys.KEYS[ord("W")])


class KeyPlaces(unittest.TestCase):
    def test_every_key_finds_its_own_led_on_the_gx87(self):
        taken = {}
        for vk, (x, y) in keys.KEYS.items():
            d, i = min((math.hypot(px / 224 * keys.WIDTH - x * keys.WIDTH, py / 64 * keys.HEIGHT - y * keys.HEIGHT), i)
                       for i, (px, py) in enumerate(GX87))
            self.assertLess(d, 0.7, hex(vk))
            taken.setdefault(i, []).append(vk)
        shared = [v for v in taken.values() if len(v) > 1]
        self.assertEqual(shared, [[0xA0, 0xE2]])     # left Shift and the ISO key next to it


class Ripples(unittest.TestCase):
    def setUp(self):
        self.fx = EFFECTS["typing"]
        self.st = LightState().patched({"color": "#00e5ff", "color2": "#7c4dff", "speed": 50})
        order = sorted(range(len(GX87)), key=lambda i: GX87[i])
        self.places = [GX87[i] for i in order]
        self.geo = [(x / 224, y / 64) for x, y in self.places]

    def frame(self, presses, geo=True, n=None):
        ctx = {"keys": presses}
        if geo:
            ctx["fixture"] = {"geometry": self.geo}
        return self.fx.render(0.0, n or len(self.geo), self.st, {}, ctx)

    def test_the_pressed_key_lights_up(self):
        for ch, at in (("Q", (25, 32)), ("L", (126, 42)), (" ", (84, 62))):
            x, y = keys.KEYS[ord(ch)]
            out = self.frame([(0.0, x, y)])
            self.assertEqual(self.places[max(range(len(out)), key=lambda i: sum(out[i]))], at, ch)

    def test_the_ripple_spreads_and_fades(self):
        x, y = keys.KEYS[ord("G")]
        k = self.places.index((114, 42))           # K: three keys to the right
        at_first, later, gone = (sum(self.frame([(age, x, y)])[k]) for age in (0.0, 0.28, 3.0))
        self.assertGreater(later, at_first + 60)   # the ring reaches it
        self.assertLess(gone, at_first + 2)

    def test_no_keys_is_the_dim_background(self):
        out = self.frame([])
        self.assertEqual(len(set(out)), 1)
        self.assertLessEqual(max(out[0]), 21)      # 8% of the second color

    def test_a_strip_glows_where_the_key_sits(self):
        x, y = keys.KEYS[ord("P")]               # right of the middle
        out = self.frame([(0.5, x, y)], geo=False, n=40)   # (a moment later: not every LED near it at full)
        brightest = max(range(40), key=lambda i: sum(out[i]))
        self.assertAlmostEqual((brightest + 0.5) / 40, x, delta=0.03)


if __name__ == "__main__":
    unittest.main()
