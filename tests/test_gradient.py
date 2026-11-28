import re
import unittest
from pathlib import Path

from glowstage.effects import EFFECTS, blend
from glowstage.state import LightState


def look(color="#ff0000", color2="#0000ff"):
    st = LightState()
    st.color, st.color2 = color, color2
    return st


class Blend(unittest.TestCase):
    def test_the_ends_are_the_colors_themselves(self):
        out = blend((255, 120, 0), (124, 77, 255), [0, 1])
        self.assertEqual(out[0].tolist(), [255, 120, 0])
        self.assertEqual(out[1].tolist(), [124, 77, 255])

    def test_red_to_blue_passes_a_clear_magenta(self):
        mid = blend((255, 0, 0), (0, 0, 255), [0.5])[0]
        self.assertGreater(mid[0], 150)         # (a blend of the numbers gives a dark 128, 0, 128)
        self.assertGreater(mid[2], 150)
        self.assertLess(mid[1], 30)

    def test_white_to_red_stays_on_the_red_side(self):
        for r, g, b in blend((255, 255, 255), (255, 0, 0), [0.25, 0.5, 0.75]):
            self.assertEqual(r, 255)
            self.assertGreaterEqual(g, b)        # pink, never a cold or a green tint

    def test_black_to_white_is_grey_all_the_way(self):
        for r, g, b in blend((0, 0, 0), (255, 255, 255), [0.2, 0.5, 0.8]):
            self.assertEqual(r, g)
            self.assertEqual(g, b)

    def test_the_same_color_is_one_color(self):
        self.assertEqual({tuple(c) for c in blend((0, 229, 255), (0, 229, 255), [0, 0.3, 0.6, 1]).tolist()},
                         {(0, 229, 255)})


class GradientLook(unittest.TestCase):
    def setUp(self):
        self.fx = EFFECTS["gradient"]

    def test_end_to_end_along_a_light(self):
        out = self.fx.render(0, 5, look(), {}, {})
        self.assertEqual(out[0], (255, 0, 0))
        self.assertEqual(out[-1], (0, 0, 255))
        self.assertEqual(len(out), 5)

    def test_a_bulb_takes_the_first_color(self):
        self.assertEqual(self.fx.render(0, 1, look(), {}, {}), [(255, 0, 0)])

    def test_with_flow_a_light_takes_its_part_of_the_stage(self):
        left = self.fx.render(0, 3, look(), {}, {"flow": True, "span": (0.0, 0.2, "front")})
        right = self.fx.render(0, 3, look(), {}, {"flow": True, "span": (0.8, 1.0, "front")})
        self.assertGreater(left[0][0], right[0][0])     # red on the left
        self.assertLess(left[-1][2], right[-1][2])      # blue on the right

    def test_it_stands_still(self):
        mem = {}
        self.assertEqual(self.fx.render(0, 8, look(), mem, {}), self.fx.render(42.5, 8, look(), mem, {}))

    def test_new_colors_are_picked_up(self):
        mem = {}
        self.fx.render(0, 4, look(), mem, {})
        self.assertEqual(self.fx.render(1, 4, look("#00ff00", "#00ff00"), mem, {}), [(0, 255, 0)] * 4)

    def test_no_leds_no_colors(self):
        self.assertEqual(self.fx.render(0, 0, look(), {}, {}), [])

    def test_it_takes_two_colors_and_can_flow(self):
        d = self.fx.describe()
        self.assertEqual(d["uses"], ["color", "color2"])
        self.assertTrue(d["flows"])


class ThePreview(unittest.TestCase):
    def test_the_window_blends_with_the_same_numbers(self):
        """app.js draws the preview with its own copy of the OKLab matrices."""
        js = (Path(__file__).parent.parent / "glowstage" / "ui" / "app.js").read_text(encoding="utf-8")
        from glowstage import effects
        for name, matrix in (("OK_LMS", effects._LMS), ("OK_LAB", effects._LAB),
                             ("OK_LAB_INV", effects._LAB_INV), ("OK_LMS_INV", effects._LMS_INV)):
            found = re.search(rf"const {name} = (\[\[.*?\]\]);", js)
            self.assertIsNotNone(found, name)
            rows = [[float(v) for v in row.split(",")] for row in re.findall(r"\[([^\[\]]+)\]", found.group(1))]
            for row_js, row_py in zip(rows, matrix.tolist()):
                for a, b in zip(row_js, row_py):
                    self.assertAlmostEqual(a, b, places=6, msg=name)


if __name__ == "__main__":
    unittest.main()
