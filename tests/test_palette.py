import colorsys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
from PIL import Image

from glowstage import palette
from glowstage.api import Api
from glowstage.colors import hex_to_rgb
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager


def save(folder, name, rgb: np.ndarray) -> str:
    path = str(Path(folder) / name)
    Image.fromarray(rgb.astype(np.uint8)).save(path)
    return path


def halves(left, right, w=200, h=100) -> np.ndarray:
    img = np.zeros((h, w, 3))
    img[:, : w // 2], img[:, w // 2:] = left, right
    return img


def sat(hexed):
    return colorsys.rgb_to_hsv(*(c / 255 for c in hex_to_rgb(hexed)))[1]


class Colors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def colors(self, rgb, name="p.png"):
        return palette.main_colors(palette.read(save(self.tmp.name, name, rgb)))

    def test_left_red_right_blue_comes_out_red_then_blue(self):
        found = self.colors(halves((220, 30, 30), (30, 40, 220)))
        self.assertEqual(len(found), 2)
        r, b = (hex_to_rgb(c) for c in found)
        self.assertTrue(r[0] > 200 and r[1] < 60 and r[2] < 60, found)
        self.assertTrue(b[2] > 200 and b[0] < 60, found)

    def test_the_same_picture_gives_the_same_colors(self):
        rng = np.random.default_rng(3)
        noise = rng.integers(0, 256, (80, 120, 3))
        self.assertEqual(self.colors(noise, "a.png"), self.colors(noise, "b.png"))

    def test_left_to_right_not_by_size(self):
        # the small patch on the left comes first even though the right is much bigger
        img = halves((30, 200, 40), (230, 40, 40))
        img[:, :40] = (30, 200, 40)
        img[:, 40:] = (230, 40, 40)
        found = self.colors(img)
        self.assertGreater(hex_to_rgb(found[0])[1], hex_to_rgb(found[0])[0])
        self.assertGreater(hex_to_rgb(found[-1])[0], hex_to_rgb(found[-1])[1])

    def test_a_vivid_patch_beats_a_large_dull_area(self):
        img = np.full((100, 200, 3), (120, 118, 116))
        img[40:60, 90:110] = (255, 0, 120)
        found = self.colors(img)
        self.assertTrue(any(sat(c) > 0.8 for c in found), found)

    def test_greys_give_a_soft_white(self):
        found = self.colors(np.full((60, 60, 3), 128))
        self.assertEqual(len(found), 1)
        r, g, b = hex_to_rgb(found[0])
        self.assertTrue(r >= g >= b and r > 120, found)       # warm, and not dark

    def test_whites_stay_white(self):
        self.assertEqual(self.colors(np.full((60, 60, 3), 250)), ["#ffffff"])

    def test_near_black_is_dropped(self):
        found = self.colors(halves((3, 3, 3), (30, 200, 60)))
        self.assertEqual(len(found), 1)
        self.assertGreater(hex_to_rgb(found[0])[1], 180)

    def test_a_black_picture_has_no_colors(self):
        with self.assertRaises(ValueError):
            self.colors(np.zeros((40, 40, 3)))

    def test_dull_colors_are_made_stronger_and_brighter(self):
        found = self.colors(np.full((50, 50, 3), (90, 60, 50)))
        self.assertGreater(max(hex_to_rgb(found[0])), 160)
        self.assertGreater(sat(found[0]), sat("#5a3c32"))

    def test_never_more_than_five(self):
        rng = np.random.default_rng(5)
        self.assertLessEqual(len(self.colors(rng.integers(0, 256, (90, 90, 3)))), 5)

    def test_a_huge_picture_is_read_small(self):
        path = save(self.tmp.name, "big.png", np.zeros((3000, 4000, 3)) + 120)
        img = palette.read(path)
        self.assertLessEqual(max(img.size), palette.THUMB)
        self.assertEqual(img.mode, "RGB")

    def test_jpeg_gif_and_bmp_open(self):
        for ext in ("jpg", "gif", "bmp"):
            path = str(Path(self.tmp.name) / f"p.{ext}")
            Image.fromarray(halves((200, 30, 30), (30, 30, 200)).astype(np.uint8)).save(path)
            self.assertEqual(len(palette.main_colors(palette.read(path))), 2, ext)

    def test_not_a_picture(self):
        path = Path(self.tmp.name) / "notes.png"
        path.write_text("just words", encoding="utf-8")
        with self.assertRaises(ValueError) as e:
            palette.read(str(path))
        self.assertIn("isn't a picture", str(e.exception))
        with self.assertRaises(ValueError):
            palette.read(str(Path(self.tmp.name) / "gone.png"))

    def test_too_large_a_file(self):
        path = save(self.tmp.name, "p.png", halves((200, 30, 30), (30, 30, 200)))
        with mock.patch.object(palette, "MAX_BYTES", 100):
            with self.assertRaises(ValueError) as e:
                palette.read(path)
        self.assertIn("too big", str(e.exception))

    def test_the_thumbnail_is_a_data_url(self):
        url = palette.thumbnail_url(palette.read(save(self.tmp.name, "p.png", halves((200, 30, 30), (30, 30, 200)))))
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))


class FakeLight(Device):
    def __init__(self, dev_id, pixels=1, name=None):
        super().__init__(dev_id, name or dev_id)
        self.online = True
        self.pixel_count = pixels

    def apply(self, state):
        pass


RED, GREEN, BLUE = "#ff0000", "#00ff00", "#0000ff"


class Spread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        for dev in (FakeLight("bulb"), FakeLight("strip", 60), FakeLight("lamp")):
            self.m._register(dev)
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456"})
        # where each stands: the bulb at the left, the lamp at the right, the strip across the left half
        self.m.config.data["show_layout"] = {"bulb": [0.0, 0.2, "back"], "lamp": [0.8, 1.0, "back"],
                                             "strip": [0.0, 0.5, "front"]}

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def test_a_bulb_gets_one_color_by_its_place(self):
        self.assertEqual(self.m.light_from_colors([RED, GREEN, BLUE]), 3)
        bulb, lamp = self.m.states["bulb"], self.m.states["lamp"]
        self.assertEqual((bulb.mode, bulb.color), ("color", RED))
        self.assertEqual((lamp.mode, lamp.color), ("color", BLUE))

    def test_a_strip_gets_the_gradient_between_the_colors_around_its_place(self):
        self.m.light_from_colors([RED, GREEN, BLUE])
        strip = self.m.states["strip"]
        self.assertEqual((strip.mode, strip.effect), ("effect", "gradient"))
        self.assertEqual((strip.color, strip.color2), (RED, GREEN))

    def test_a_strip_laid_out_backwards_runs_the_other_way(self):
        self.m.config.data["show_layout"]["strip"] = [0.5, 0.0, "front"]
        self.m.light_from_colors([RED, GREEN, BLUE])
        strip = self.m.states["strip"]
        self.assertEqual((strip.color, strip.color2), (GREEN, RED))

    def test_a_short_strip_still_gets_two_colors(self):
        self.m.config.data["show_layout"]["strip"] = [0.40, 0.45, "front"]
        self.m.light_from_colors([RED, GREEN, BLUE])
        strip = self.m.states["strip"]
        self.assertNotEqual(strip.color, strip.color2)

    def test_lights_that_are_off_stay_off_and_unchanged(self):
        self.m.update(["lamp"], {"on": False})
        before = self.m.states["lamp"].to_dict()
        self.assertEqual(self.m.light_from_colors([RED, GREEN, BLUE]), 2)
        self.assertEqual(self.m.states["lamp"].to_dict(), before)

    def test_bulbs_in_the_same_place_share_the_picture(self):
        for dev in (FakeLight("b1", name="One"), FakeLight("b2", name="Two"), FakeLight("b3", name="Three")):
            self.m._register(dev)
        self.m.update(["bulb", "strip", "lamp"], {"on": False})
        self.m.config.data["show_layout"] = {}      # none of them placed: the default "whole room"
        self.m.light_from_colors([RED, GREEN, BLUE])
        taken = {self.m.states[i].color for i in ("b1", "b2", "b3")}
        self.assertEqual(len(taken), 3)

    def test_one_color_makes_every_light_that_color(self):
        self.m.light_from_colors(["#ff8800"])
        self.assertEqual({self.m.states[i].color for i in ("bulb", "strip", "lamp")}, {"#ff8800"})
        self.assertEqual(self.m.states["strip"].mode, "color")

    def test_no_preset_shows_after_it(self):
        self.m.config["settings"]["active_preset"] = "Warm white"
        self.m.light_from_colors([RED, GREEN, BLUE])
        self.assertEqual(self.m.config["settings"]["active_preset"], "")

    def test_undo_brings_the_old_look_back(self):
        before = {i: self.m.states[i].to_dict() for i in ("bulb", "strip", "lamp")}
        self.m.remember()
        self.m.light_from_colors([RED, GREEN, BLUE])
        self.assertNotEqual(self.m.states["bulb"].to_dict(), before["bulb"])
        self.assertTrue(self.m.undo())
        self.assertEqual({i: self.m.states[i].to_dict() for i in before}, before)

    def test_saved_as_a_preset(self):
        self.m.light_from_colors([RED, GREEN, BLUE])
        self.m.save_preset("Sunset")
        saved = next(p for p in self.m.config["presets"] if p["name"] == "Sunset")
        self.assertEqual(saved["states"]["strip"]["effect"], "gradient")
        self.assertEqual(saved["states"]["bulb"]["color"], RED)


class Bridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.m._register(FakeLight("bulb"))
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456"})
        self.api = Api(SimpleNamespace(manager=self.m))

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def test_colors_from_picture_applies_and_can_be_undone(self):
        path = save(self.tmp.name, "sunset_over_the_bay.png", halves((220, 30, 30), (30, 40, 220)))
        r = self.api.colors_from_picture(path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["name"], "Sunset over the bay")
        self.assertEqual(len(r["result"]["colors"]), 2)
        self.assertTrue(r["result"]["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.m.states["bulb"].mode, "color")
        self.assertNotEqual(self.m.states["bulb"].color, "#123456")
        self.assertTrue(self.m.can_undo())
        self.m.undo()
        self.assertEqual(self.m.states["bulb"].color, "#123456")

    def test_a_bad_file_says_so_and_changes_nothing(self):
        path = Path(self.tmp.name) / "x.jpg"
        path.write_text("nope", encoding="utf-8")
        r = self.api.colors_from_picture(str(path))
        self.assertFalse(r["ok"])
        self.assertIn("isn't a picture", r["error"])
        self.assertEqual(self.m.states["bulb"].color, "#123456")
        self.assertFalse(self.m.can_undo())

    def test_with_every_light_off_it_asks_for_one_on(self):
        self.m.update(None, {"on": False})
        path = save(self.tmp.name, "p.png", halves((220, 30, 30), (30, 40, 220)))
        r = self.api.colors_from_picture(path)
        self.assertFalse(r["ok"])
        self.assertIn("Switch a light on", r["error"])


if __name__ == "__main__":
    unittest.main()
