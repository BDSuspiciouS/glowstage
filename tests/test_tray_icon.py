import unittest

from glowstage.icon import icon_image
from glowstage.state import LightState
from glowstage.tray import LookUpdater, tray_look, tray_title


def light(color="#ff0000", brightness=100, on=True, mode="color", effect="rainbow"):
    return LightState(on=on, brightness=brightness, mode=mode, color=color, effect=effect)


class Look(unittest.TestCase):
    def test_everything_off(self):
        self.assertEqual(tray_look({"a": light(on=False), "b": light(on=False)}), "off")

    def test_no_lights_at_all(self):
        self.assertEqual(tray_look({}), "off")

    def test_one_red_light(self):
        look = tray_look({"a": light("#ff0000")})
        self.assertIsInstance(look, tuple)
        self.assertGreaterEqual(look[0], 224)
        self.assertLessEqual(look[1], 16)
        self.assertLessEqual(look[2], 16)

    def test_dim_red_still_reads_red(self):
        look = tray_look({"a": light("#1a0000", brightness=10)})
        self.assertGreaterEqual(look[0], 224)
        self.assertLessEqual(look[1], 16)

    def test_weighted_by_brightness(self):
        look = tray_look({"a": light("#ff0000", brightness=10), "b": light("#0000ff", brightness=90)})
        self.assertGreater(look[2], look[0] * 3)

    def test_whiteish_is_warm_white(self):
        r, g, b = tray_look({"a": light("#ffffff")})
        self.assertGreater(r, g)
        self.assertGreater(g, b)
        self.assertGreaterEqual(b, 160)

    def test_mostly_effects_is_rainbow(self):
        states = {"a": light(mode="effect"), "b": light(mode="effect"), "c": light("#00ff00")}
        self.assertEqual(tray_look(states), "rainbow")

    def test_few_effects_keep_the_color(self):
        states = {"a": light(mode="effect"), "b": light("#00ff00"), "c": light("#00ff00")}
        self.assertIsInstance(tray_look(states), tuple)

    def test_light_show_is_rainbow(self):
        states = {"a": light(mode="effect", effect="music_show"), "b": light(mode="effect", effect="music_show")}
        self.assertEqual(tray_look(states), "rainbow")
        self.assertIn("light show", tray_title(states))

    def test_hidden_lights_are_ignored(self):
        states = {"a": light("#ff0000"), "hidden": light("#0000ff")}
        look = tray_look(states, ids={"a"})
        self.assertGreater(look[0], 200)
        self.assertLess(look[2], 40)
        self.assertEqual(tray_look(states, ids={"nothing"}), "off")

    def test_off_lights_and_zero_brightness_do_not_count(self):
        states = {"a": light("#ff0000"), "b": light("#0000ff", on=False), "c": light("#0000ff", brightness=0)}
        self.assertLess(tray_look(states)[2], 40)

    def test_look_is_in_steps(self):
        a = tray_look({"a": light("#ff0000")})
        b = tray_look({"a": light("#fe0300")})
        self.assertEqual(a, b)
        self.assertTrue(all(v % 16 == 0 or v == 255 for v in a))

    def test_title(self):
        states = {"a": light(), "b": light(on=False), "c": light()}
        self.assertTrue(tray_title(states).endswith("2 of 3 lights on"))
        self.assertTrue(tray_title({"a": light(on=False)}).endswith("lights off"))


class Drawing(unittest.TestCase):
    def test_tint_leans_to_the_color(self):
        img = icon_image(32, tint=(240, 16, 16))
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (32, 32))
        ring = img.getpixel((16, 2))   # top of the ring
        self.assertGreater(ring[0], ring[1] + 80)
        self.assertGreater(ring[0], ring[2] + 80)
        self.assertGreater(ring[3], 200)

    def test_off_is_grey(self):
        img = icon_image(32, off=True)
        self.assertEqual(img.size, (32, 32))
        for xy in ((16, 2), (2, 16), (16, 16), (16, 29)):
            r, g, b, a = img.getpixel(xy)
            self.assertLessEqual(max(r, g, b) - min(r, g, b), 12)
            self.assertGreater(a, 200)

    def test_same_shape_as_the_normal_icon(self):
        base = icon_image(64)
        for other in (icon_image(64, off=True), icon_image(64, tint=(240, 16, 16))):
            self.assertEqual(other.size, base.size)
            self.assertEqual(other.getchannel("A").tobytes(), base.getchannel("A").tobytes())

    def test_default_call_is_the_rainbow(self):
        self.assertIs(icon_image(64), icon_image(64))


class Throttle(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.look = ("off", "t")
        self.applied = []
        self.timers = []
        self.u = LookUpdater(lambda: self.look, lambda *a: self.applied.append(a),
                             clock=lambda: self.now, schedule=lambda d, fn: self.timers.append((d, fn)))

    def test_first_change_is_shown_at_once(self):
        self.u.poke()
        self.assertEqual(self.applied, [("off", "t")])

    def test_same_look_does_nothing(self):
        self.u.poke()
        self.now += 5
        self.u.poke()
        self.u.poke()
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.timers, [])

    def test_a_quick_second_change_waits(self):
        self.u.poke()
        self.now += 0.2
        self.look = ("rainbow", "t2")
        self.u.poke()
        self.u.poke()   # a burst: one timer only
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(len(self.timers), 1)
        delay, fn = self.timers[0]
        self.assertAlmostEqual(delay, 0.8, places=3)
        self.now += 0.8
        fn()
        self.assertEqual(self.applied[-1], ("rainbow", "t2"))

    def test_change_after_a_second_is_shown_at_once(self):
        self.u.poke()
        self.now += 1.5
        self.look = ((240, 16, 16), "t3")
        self.u.poke()
        self.assertEqual(self.applied[-1], ((240, 16, 16), "t3"))

    def test_burst_that_ends_where_it_began_draws_nothing_more(self):
        self.u.poke()
        self.now += 0.2
        self.look = ("rainbow", "t2")
        self.u.poke()
        self.look = ("off", "t")
        self.now += 0.8
        self.timers[0][1]()
        self.assertEqual(len(self.applied), 1)

    def test_a_failing_draw_does_not_raise(self):
        def boom(*a):
            raise RuntimeError("no pillow")
        u = LookUpdater(lambda: ("off", "t"), boom, clock=lambda: self.now)
        with self.assertLogs("glowstage.tray", level="ERROR"):
            u.poke()

    def test_a_failing_read_does_not_raise(self):
        def boom():
            raise RuntimeError("x")
        u = LookUpdater(boom, lambda *a: None, clock=lambda: self.now)
        with self.assertLogs("glowstage.tray", level="ERROR"):
            u.poke()


if __name__ == "__main__":
    unittest.main()
