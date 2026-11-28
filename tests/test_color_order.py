import itertools
import tempfile
import time
import unittest
from pathlib import Path

from glowstage import diagnostics
from glowstage.config import Config
from glowstage.devices.base import COLOR_ORDERS, Device, order_swap
from glowstage.manager import DeviceManager


def wired(order, sent):
    """What a strip that takes its colors in `order` shows when it is sent three values:
    the first value drives the color the first letter names, and so on."""
    shown = {}
    for letter, value in zip(order, sent):
        shown[letter] = value
    return shown["R"], shown["G"], shown["B"]


class Strip(Device):
    def __init__(self, dev_id="strip"):
        super().__init__(dev_id, dev_id)
        self.online = True
        self.seen = []

    def apply(self, state):
        self.seen.append(state)


class Wire(Device):
    """A light that keeps the very values it is sent."""

    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True
        self.sent = []

    def show_color(self, rgb, state):
        self.sent.append(tuple(rgb))

    def power_off(self):
        pass


class Orders(unittest.TestCase):
    def dev(self, order, warmth=0, top=100):
        d = Device("x", "x")
        d.set_balance(warmth, top, order)
        return d

    def test_the_real_case_a_grb_strip_shown_red_is_sent_green_first(self):
        d = self.dev("GRB")
        self.assertEqual(d.balanced((255, 0, 0)), (0, 255, 0))      # (G, R, B) as WLED and FastLED name it
        self.assertEqual(d.balanced((0, 255, 0)), (255, 0, 0))
        self.assertEqual(d.balanced((0, 0, 255)), (0, 0, 255))

    def test_every_order_makes_the_light_show_what_was_meant(self):
        for order in COLOR_ORDERS:
            d = self.dev(order)
            for color in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 120, 250), (255, 255, 255)]:
                self.assertEqual(wired(order, d.balanced(color)), color, order)
                self.assertEqual(wired(order, d.balanced_frame([color])[0]), color, order)

    def test_each_mapping(self):
        r, g, b = (1, 2, 3)
        want = {"RGB": (1, 2, 3), "RBG": (1, 3, 2), "GRB": (2, 1, 3), "GBR": (2, 3, 1), "BRG": (3, 1, 2), "BGR": (3, 2, 1)}
        for order, out in want.items():
            self.assertEqual(self.dev(order).balanced((r, g, b)), out, order)

    def test_an_order_then_its_inverse_gives_the_original(self):
        # every order has exactly one order that undoes it (two swaps undo themselves, the two turns each other)
        for order in COLOR_ORDERS:
            undoers = [o for o in COLOR_ORDERS if self.dev(o).balanced(self.dev(order).balanced((1, 2, 3))) == (1, 2, 3)]
            self.assertEqual(len(undoers), 1, order)
            self.assertEqual(self.dev(undoers[0]).balanced(self.dev(order).balanced((9, 8, 7))), (9, 8, 7))
        a, b = self.dev("GBR"), self.dev("BRG")
        self.assertEqual(b.balanced(a.balanced((9, 8, 7))), (9, 8, 7))
        self.assertEqual(self.dev("GRB").balanced(self.dev("GRB").balanced((9, 8, 7))), (9, 8, 7))

    def test_neutral_is_exactly_a_no_op(self):
        d = self.dev("RGB")
        self.assertIsNone(d.swap)
        self.assertIsNone(order_swap("RGB"))
        color, frame = (1, 2, 3), [(1, 2, 3), (4, 5, 6)]
        self.assertIs(d.balanced(color), color)
        self.assertIs(d.balanced_frame(frame), frame)
        self.assertIs(Device("y", "y").balanced_frame(frame), frame)

    def test_frames_and_single_colors_agree(self):
        frame = [(255, 0, 0), (0, 200, 40), (12, 34, 56), (255, 255, 255)]
        for order in COLOR_ORDERS:
            for warmth, top in [(0, 100), (30, 100), (-20, 60)]:
                d = self.dev(order, warmth, top)
                self.assertEqual(d.balanced_frame(frame), [d.balanced(c) for c in frame], (order, warmth, top))

    def test_the_order_comes_after_the_white_balance_and_the_maximum(self):
        plain, warm = self.dev("RGB", 50, 80), self.dev("GRB", 50, 80)
        c = (200, 150, 100)
        r, g, b = plain.balanced(c)
        self.assertEqual(warm.balanced(c), (g, r, b))
        self.assertEqual(warm.balanced_frame([c]), [(g, r, b)])
        top_only = self.dev("BGR", 0, 50)
        self.assertEqual(top_only.balanced((200, 100, 40)), (20, 50, 100))     # half of each, then turned around

    def test_a_bad_order_is_refused(self):
        for bad in ("RRG", "RGBA", "", "xyz", "RG", None):
            with self.assertRaises(ValueError):
                order_swap(bad)
        self.assertEqual(order_swap("grb"), (1, 0, 2))      # (case doesn't matter)
        self.assertEqual(sorted(order_swap(o) for o in COLOR_ORDERS if o != "RGB"), sorted(set(itertools.permutations(range(3))) - {(0, 1, 2)}))


class Saved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.m = DeviceManager(Config(self.path))
        self.dev = Strip("desk")
        self.m._register(self.dev)
        self.m.update(None, {"on": True, "mode": "color", "color": "#ff0000", "brightness": 50})
        self.settle()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def settle(self):
        self.m.wait_applied(["desk"], 2)
        time.sleep(0.05)

    def test_it_is_saved_and_comes_back(self):
        self.m.set_color_order("desk", "grb")
        self.assertEqual(self.dev.swap, (1, 0, 2))
        self.assertEqual(self.m.config.data["balance"]["desk"]["order"], "GRB")
        self.m.config.save_now()
        again = DeviceManager(Config(self.path))
        try:
            fresh = Strip("desk")
            again._register(fresh)
            self.assertEqual(again.balance("desk")["order"], "GRB")
            self.assertEqual(fresh.swap, (1, 0, 2))
            self.assertEqual(again.snapshot()["devices"][0]["balance"]["order"], "GRB")
        finally:
            again._stop.set()

    def test_rgb_and_nothing_else_set_keeps_no_entry(self):
        self.m.set_color_order("desk", "BRG")
        self.m.set_color_order("desk", "RGB")
        self.assertNotIn("desk", self.m.config.data["balance"])
        self.assertIsNone(self.dev.swap)
        self.m.set_balance("desk", 10, 100)
        self.assertNotIn("order", self.m.config.data["balance"]["desk"])      # (RGB is not written down)

    def test_the_sliders_and_the_order_leave_each_other_alone(self):
        self.m.set_color_order("desk", "GBR")
        self.m.set_balance("desk", 20, 70)
        self.assertEqual(self.m.balance("desk"), {"warmth": 20, "max": 70, "order": "GBR"})
        self.m.set_color_order("desk", "RBG")
        self.assertEqual(self.m.balance("desk"), {"warmth": 20, "max": 70, "order": "RBG"})
        self.m.set_balance("desk", 0, 100)
        self.assertEqual(self.m.balance("desk")["order"], "RBG")

    def test_a_bad_order_is_refused_and_nothing_changes(self):
        for bad in ("RRB", "", "banana", None):
            with self.assertRaisesRegex(ValueError, "isn't a color order"):
                self.m.set_color_order("desk", bad)
        self.assertNotIn("desk", self.m.config.data.get("balance", {}))
        with self.assertRaisesRegex(ValueError, "isn't there any more"):
            self.m.set_color_order("nope", "GRB")

    def test_a_hand_edited_order_is_read_as_rgb(self):
        self.m.config.data["balance"] = {"desk": {"warmth": 5, "max": 90, "order": "purple"}, "odd": "x"}
        self.assertEqual(self.m.balance("desk"), {"warmth": 5, "max": 90, "order": "RGB"})
        self.assertEqual(self.m.balance("odd")["order"], "RGB")

    def test_a_light_showing_a_color_shows_it_again_at_once(self):
        self.dev.seen.clear()
        self.m.set_color_order("desk", "GRB")
        self.settle()
        self.assertEqual(len(self.dev.seen), 1)
        self.assertEqual(self.m.states["desk"].color, "#ff0000")      # (what is saved is what you meant)

    def test_the_report_lists_lights_with_a_color_order(self):
        class App:
            manager = self.m
            config = self.m.config
            remote = type("R", (), {"settings": {}})()
        self.m.openrgb.describe = lambda: {}
        self.assertNotIn("color order", diagnostics.report(App()))
        self.m.set_color_order("desk", "GRB")
        self.assertIn("Lights with a color order: desk (GRB)", diagnostics.report(App()))


class ColorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.m.TEST_STEP = 0.15
        self.dev = Strip("desk")
        self.m._register(self.dev)
        self.m.update(None, {"on": True, "mode": "color", "color": "#336699", "brightness": 40})
        self.settle()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def settle(self):
        self.m.wait_applied(["desk"], 2)
        time.sleep(0.05)

    def wait_until(self, check, seconds=3.0):
        end = time.time() + seconds
        while time.time() < end and not check():
            time.sleep(0.01)
        return check()

    def colors_shown(self):
        return [s.color for s in self.dev.seen]

    def test_red_then_green_then_blue_then_back_as_it_was(self):
        before = self.m.states["desk"].to_dict()
        self.dev.seen.clear()
        self.m.color_test("desk", True)
        self.assertEqual(self.m.color_test_step("desk"), "red")
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("desk") == "blue"))
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("desk") == ""))
        self.settle()
        self.assertEqual(self.colors_shown(), ["#ff0000", "#00ff00", "#0000ff", "#336699"])
        shown = self.dev.seen[0]
        self.assertEqual((shown.on, shown.mode, shown.brightness), (True, "color", 40))     # (at the light's brightness)
        self.assertEqual(self.m.states["desk"].to_dict(), before)     # nothing is saved or changed
        self.assertEqual(self.m._white, {})
        self.assertEqual(self.m._tests, {})

    def test_stopping_early_puts_the_light_back_and_it_stays_stopped(self):
        self.dev.seen.clear()
        self.m.color_test("desk", True)
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("desk") == "green"))
        self.m.color_test("desk", False)
        self.settle()
        self.assertEqual(self.m.color_test_step("desk"), "")
        self.assertEqual(self.colors_shown()[-1], "#336699")
        count = len(self.dev.seen)
        time.sleep(0.4)
        self.assertEqual(len(self.dev.seen), count)           # (the blue never comes)
        self.m.color_test("desk", False)                      # (twice does nothing)
        self.settle()
        self.assertEqual(len(self.dev.seen), count)

    def test_a_light_that_is_off_shows_the_colors_and_goes_back_off(self):
        self.m.update(["desk"], {"on": False})
        self.settle()
        self.dev.seen.clear()
        self.m.color_test("desk", True)
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("desk") == ""))
        self.settle()
        self.assertTrue(all(s.on for s in self.dev.seen[:3]))
        self.assertFalse(self.dev.seen[-1].on)

    def test_changing_the_order_during_the_test_shows_the_same_color_again(self):
        self.m.TEST_STEP = 30          # (it stays on red for the whole test: nothing else moves on)
        self.m.color_test("desk", True)
        self.assertTrue(self.wait_until(lambda: self.dev.seen and self.dev.seen[-1].color == "#ff0000"))
        self.settle()                  # (the red is on, and nothing else is on its way)
        self.dev.seen.clear()
        self.m.set_color_order("desk", "GRB")
        self.settle()
        self.assertEqual(len(self.dev.seen), 1)
        self.assertEqual(self.dev.seen[0].color, "#ff0000")
        self.m.color_test("desk", False)

    def test_show_white_takes_over_from_a_running_test(self):
        self.m.color_test("desk", True)
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("desk") == "red"))
        self.m.show_white("desk", True)
        self.settle()
        self.assertEqual(self.m.color_test_step("desk"), "")
        time.sleep(0.5)
        self.assertEqual(self.dev.seen[-1].color, "#ffffff")           # (the test does not push its colors over it)
        self.m.show_white("desk", False)
        self.settle()
        self.assertEqual(self.dev.seen[-1].color, "#336699")

    def test_starting_twice_runs_one_test(self):
        self.dev.seen.clear()
        self.m.color_test("desk", True)
        self.m.color_test("desk", True)
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("desk") == ""))
        self.settle()
        self.assertEqual(self.colors_shown().count("#ff0000"), 1)

    def test_an_offline_or_missing_light_is_refused(self):
        self.dev.online = False
        with self.assertRaisesRegex(ValueError, "desk isn't answering"):
            self.m.color_test("desk", True)
        self.assertEqual((self.m._tests, self.m._test_step, self.m._white), ({}, {}, {}))
        with self.assertRaisesRegex(ValueError, "isn't there any more"):
            self.m.color_test("nope", True)

    def test_the_test_goes_through_the_color_order(self):
        """What reaches the light is the corrected color, so that a GRB strip really shows red first."""
        real = Wire("wire")
        self.m._register(real)
        self.m.set_color_order("wire", "GRB")
        self.m.update(["wire"], {"on": True, "mode": "color", "color": "#336699", "brightness": 100})
        self.m.wait_applied(["wire"], 2)
        time.sleep(0.05)
        real.sent.clear()
        self.m.color_test("wire", True)
        self.assertTrue(self.wait_until(lambda: self.m.color_test_step("wire") == ""))
        time.sleep(0.1)
        self.assertEqual(real.sent[:3], [(0, 255, 0), (255, 0, 0), (0, 0, 255)])
        self.assertEqual(real.sent[3], (0x66, 0x33, 0x99))             # (back: the saved color, in its order)


if __name__ == "__main__":
    unittest.main()
