import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from glowstage import api as api_module
from glowstage import wallpaper
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager

T0 = 1_000_000.0
REAL_TRANSCODED = wallpaper._transcoded_path     # (the tests put their own in its place)


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, dev_id)
        self.group = group
        self.online = True

    def apply(self, state):
        pass


def save_picture(path, rgb=(220, 60, 20), fmt="JPEG"):
    """A picture file of one color, under any name (the real one has no extension)."""
    Image.new("RGB", (64, 48), rgb).save(path, fmt)
    return str(path)


class Desktop(unittest.TestCase):
    """The Windows lookups point at a temp folder: the real desktop is never touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.transcoded = str(self.dir / "TranscodedWallpaper")
        self.setting = ""
        self.background = "0 0 0"
        for name, fn in (("_transcoded_path", lambda: self.transcoded),
                         ("_desktop_setting_path", lambda: self.setting),
                         ("_background_rgb", lambda: self.background)):
            patcher = mock.patch.object(wallpaper, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def set_wallpaper(self, rgb, name="TranscodedWallpaper"):
        path = save_picture(self.dir / name, rgb)
        if os.path.basename(path) == "TranscodedWallpaper":
            self.transcoded = path
        return path


class Which(Desktop):
    def test_the_transcoded_file_is_the_one(self):
        self.set_wallpaper((255, 0, 0))
        self.setting = save_picture(self.dir / "other.jpg", (0, 0, 255))
        self.assertEqual(wallpaper.picture_path(), self.transcoded)

    def test_the_setting_when_there_is_no_transcoded_file(self):
        self.setting = save_picture(self.dir / "mine.jpg")
        self.assertEqual(wallpaper.picture_path(), self.setting)

    def test_an_empty_transcoded_file_is_skipped(self):
        Path(self.transcoded).write_bytes(b"")
        self.setting = save_picture(self.dir / "mine.jpg")
        self.assertEqual(wallpaper.picture_path(), self.setting)

    def test_nothing(self):
        self.assertEqual(wallpaper.picture_path(), "")
        self.setting = str(self.dir / "gone.jpg")      # (a setting that points at a file that isn't there)
        self.assertEqual(wallpaper.picture_path(), "")

    def test_the_real_transcoded_path_lives_in_appdata(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.dir)}):
            self.assertEqual(REAL_TRANSCODED(), str(self.dir / "Microsoft" / "Windows" / "Themes" / "TranscodedWallpaper"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(REAL_TRANSCODED(), "")


class PlainColor(Desktop):
    def test_the_registry_color(self):
        self.background = "10 100 200"
        self.assertEqual(wallpaper.plain_color(), "#0a64c8")
        colors, img = wallpaper.look()
        self.assertEqual(colors, ["#0a64c8"])
        self.assertEqual(img.getpixel((5, 5)), (10, 100, 200))

    def test_nonsense_is_no_color(self):
        for bad in ("", "red", "1 2", "300 0 0", "-1 0 0"):
            self.background = bad
            self.assertIsNone(wallpaper.plain_color(), bad)
        with self.assertRaises(ValueError):
            wallpaper.look()

    def test_black_is_too_dark(self):
        self.background = "0 0 0"
        with self.assertRaises(ValueError) as e:
            wallpaper.look()
        self.assertEqual(str(e.exception), wallpaper.TOO_DARK)

    def test_a_picture_wins_over_the_color(self):
        self.background = "10 100 200"
        self.set_wallpaper((255, 40, 0))
        colors, _ = wallpaper.look()
        self.assertNotEqual(colors, ["#0a64c8"])


class Reading(Desktop):
    def test_a_jpeg_without_an_extension_reads(self):
        self.set_wallpaper((255, 40, 0))
        self.assertEqual(Path(self.transcoded).suffix, "")
        colors, img = wallpaper.look()
        self.assertEqual(img.mode, "RGB")
        self.assertTrue(colors)
        r, g, b = (int(colors[0][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(r, 200)
        self.assertLess(b, 100)

    def test_a_png_without_an_extension_reads_too(self):
        Image.new("RGB", (40, 40), (0, 200, 60)).save(self.transcoded, "PNG")
        colors, _ = wallpaper.look()
        self.assertTrue(colors)

    def test_a_file_that_is_no_picture(self):
        Path(self.transcoded).write_bytes(b"this is not a picture at all")
        with self.assertRaises(ValueError):
            wallpaper.look()


class Stamp(Desktop):
    def test_it_changes_with_the_file_and_not_without(self):
        self.set_wallpaper((255, 0, 0))
        first = wallpaper.stamp()
        self.assertEqual(wallpaper.stamp(), first)
        self.assertEqual(first[0], self.transcoded)
        self.set_wallpaper((0, 0, 255))
        os.utime(self.transcoded, ns=(1, 1))               # (a different time of change)
        self.assertNotEqual(wallpaper.stamp(), first)

    def test_it_changes_when_another_picture_comes_with_the_same_size(self):
        self.set_wallpaper((255, 0, 0))
        first = wallpaper.stamp()
        os.utime(self.transcoded, ns=(5, 5))
        self.assertNotEqual(wallpaper.stamp(), first)

    def test_a_plain_color_has_a_stamp_of_its_own(self):
        self.background = "10 20 30"
        a = wallpaper.stamp()
        self.assertEqual(a, ("color", "#0a141e"))
        self.background = "40 50 60"
        self.assertNotEqual(wallpaper.stamp(), a)


class Base(Desktop):
    def setUp(self):
        super().setUp()
        self.config = Config(self.dir / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in (("lamp", "room"), ("strip", "room"), ("case", "pc")):
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 40})
        self.w = self.m.wallpaper
        self.w._thread = type("Alive", (), {"is_alive": lambda self: True})()    # steps by hand here
        for part in (self.m.sleep, self.m.wake, self.m.cycle):
            part._thread = type("Alive", (), {"is_alive": lambda self: True})()
        self.set_wallpaper((255, 0, 0))
        self.addCleanup(lambda: self.m._stop.set())

    def lamp(self):
        return self.m.states["lamp"].to_dict()

    def change_wallpaper(self, rgb, at):
        self.set_wallpaper(rgb)
        os.utime(self.transcoded, ns=(int(at * 1e9), int(at * 1e9)))


class FromWallpaper(Base):
    def make_api(self):
        app = mock.Mock()
        app.manager = self.m
        return api_module.Api(app)

    def test_the_lights_take_the_colors_and_undo_goes_back(self):
        before = self.lamp()
        r = self.make_api().colors_from_wallpaper()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["name"], "My wallpaper")
        self.assertTrue(r["result"]["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(r["result"]["colors"])
        self.assertNotEqual(self.lamp(), before)
        self.assertTrue(self.m.can_undo())
        self.m.undo()
        self.assertEqual(self.lamp(), before)

    def test_no_light_on_says_so(self):
        self.m.set_power_all(False)
        r = self.make_api().colors_from_wallpaper()
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "Switch a light on first, then try again")

    def test_a_plain_background_gives_every_light_that_color(self):
        os.remove(self.transcoded)
        self.background = "10 100 200"
        r = self.make_api().colors_from_wallpaper()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["colors"], ["#0a64c8"])
        for dev_id in ("lamp", "strip", "case"):
            self.assertEqual(self.m.states[dev_id].color, "#0a64c8")

    def test_start_and_stop_through_the_api_and_the_state(self):
        api = self.make_api()
        self.assertTrue(api.start_wallpaper_follow()["ok"])
        self.assertTrue(self.w.active)
        self.assertIn("since", self.m.wallpaper.describe())
        self.assertTrue(api.stop_wallpaper_follow()["ok"])
        self.assertFalse(self.w.active)
        self.assertIsNone(self.w.describe())


class Following(Base):
    def start(self):
        self.w.start(now=T0)

    def test_it_starts_without_changing_the_lights(self):
        before = self.m.looks()
        self.start()
        self.assertTrue(self.w.active)
        self.assertEqual(self.m.looks(), before)

    def test_it_needs_a_light_that_is_on(self):
        self.m.set_power_all(False)
        with self.assertRaises(ValueError):
            self.start()
        self.assertFalse(self.w.active)

    def test_nothing_happens_without_a_change(self):
        self.start()
        before = self.m.looks()
        for k in range(1, 6):
            self.assertTrue(self.w.tick(T0 + k * 15))
        self.assertEqual(self.m.looks(), before)

    def test_a_change_of_wallpaper_changes_the_lights(self):
        self.start()
        before = self.lamp()
        self.change_wallpaper((0, 80, 255), T0)
        self.assertTrue(self.w.tick(T0 + 5))            # (not time to look yet)
        self.assertEqual(self.lamp(), before)
        self.assertTrue(self.w.tick(T0 + 15))
        self.assertNotEqual(self.lamp(), before)
        self.assertTrue(self.w.active)
        self.assertEqual(self.m.config["settings"].get("active_preset", ""), "")

    def test_it_goes_on_with_the_next_change_and_does_not_mistake_its_own_for_yours(self):
        self.start()
        self.change_wallpaper((0, 80, 255), T0)
        self.assertTrue(self.w.tick(T0 + 15))
        blue = self.lamp()
        self.change_wallpaper((20, 255, 20), T0 + 20)
        self.assertTrue(self.w.tick(T0 + 30))
        self.assertTrue(self.w.active)
        self.assertNotEqual(self.lamp(), blue)

    def test_a_change_while_an_automatic_look_is_on_waits_for_its_end(self):
        self.start()
        self.m.automations.active = "game"
        self.change_wallpaper((0, 80, 255), T0)
        before = self.lamp()
        self.assertTrue(self.w.tick(T0 + 15))
        self.assertTrue(self.w.tick(T0 + 30))
        self.assertEqual(self.lamp(), before)            # (skipped)
        self.assertTrue(self.w.active)
        self.m.automations.active = None
        self.assertTrue(self.w.tick(T0 + 45))
        self.assertNotEqual(self.lamp(), before)         # (picked up after it)

    def test_what_an_automatic_look_changes_is_not_you(self):
        self.start()
        looks = self.m.looks()
        self.m.automations.active = "video"
        self.m.update(None, {"mode": "color", "color": "#ff00ff"})
        self.assertTrue(self.w.tick(T0 + 1))
        self.m.restore_looks(looks, "")                  # (what automations.py does at the end)
        self.m.automations.active = None
        self.assertTrue(self.w.tick(T0 + 2))
        self.assertTrue(self.w.active)

    def test_a_light_changed_by_hand_ends_it(self):
        self.start()
        self.m.update(["lamp"], {"color": "#00ff00"})
        self.assertFalse(self.w.tick(T0 + 1))
        self.assertFalse(self.w.active)

    def test_a_preset_ends_it(self):
        self.start()
        self.m.apply_preset("Cozy")
        self.assertFalse(self.w.tick(T0 + 1))
        self.assertFalse(self.w.active)

    def test_it_does_not_take_a_change_of_wallpaper_after_you_took_over(self):
        self.start()
        self.m.update(["lamp"], {"color": "#00ff00"})
        self.change_wallpaper((0, 80, 255), T0)
        self.assertFalse(self.w.tick(T0 + 15))
        self.assertEqual(self.lamp()["color"], "#00ff00")

    def test_all_lights_off_ends_it(self):
        self.start()
        self.m.set_power_all(False)
        self.assertFalse(self.w.tick(T0 + 1))
        self.assertFalse(self.w.active)

    def test_cancel_leaves_the_lights_and_ticks_say_it_is_over(self):
        self.start()
        before = self.m.looks()
        self.w.cancel()
        self.assertFalse(self.w.active)
        self.assertEqual(self.m.looks(), before)
        self.assertFalse(self.w.tick(T0 + 20))
        self.w.cancel()                                   # (again: nothing happens)

    def test_a_wallpaper_that_can_not_be_read_is_not_tried_again_until_it_changes(self):
        self.start()
        Path(self.transcoded).write_bytes(b"not a picture")
        before = self.lamp()
        self.assertTrue(self.w.tick(T0 + 15))
        self.assertEqual(self.lamp(), before)
        with mock.patch.object(wallpaper, "look", side_effect=AssertionError("tried again")):
            self.assertTrue(self.w.tick(T0 + 30))
        self.assertTrue(self.w.active)


class TakingTurns(Base):
    def test_starting_presets_in_turn_stops_following(self):
        self.w.start(now=T0)
        self.m.cycle.start(["Cozy", "Reading"], 10, False, now=T0)
        self.assertFalse(self.w.active)
        self.assertTrue(self.m.cycle.active)

    def test_following_stops_presets_in_turn(self):
        self.m.cycle.start(["Cozy", "Reading"], 10, False, now=T0)
        self.w.start(now=T0)
        self.assertFalse(self.m.cycle.active)
        self.assertTrue(self.w.active)

    def test_the_sleep_timer_starting_stops_it(self):
        self.w.start(now=T0)
        self.m.sleep.start(30)
        self.assertFalse(self.w.active)

    def test_the_wake_up_starting_stops_it(self):
        self.w.start(now=T0)
        self.m.wake.start(30)
        self.assertFalse(self.w.active)


class Around(Base):
    def test_get_state_carries_it(self):
        self.assertIsNone(self.m.wallpaper.describe())
        self.w.start(now=T0)
        self.assertEqual(self.m.wallpaper.describe(), {"since": T0})



class SteppingAway(Base):
    """Locking the PC switches the lights off (away.py): that isn't you taking over."""

    def leave(self):
        """Lock the PC with "lights off when you step away" on for every light."""
        self.m.config["settings"]["away_lights"] = "all"
        self.m.away.set("locked", True)

    def come_back(self, settled=True):
        self.m.away.set("locked", False)
        if settled:
            self.m.away.back_at = -1e9              # (the few seconds for the lights to settle are over)

    def test_locking_the_pc_doesnt_stop_it(self):
        self.w.start(now=T0)
        self.leave()
        self.assertTrue(self.w.tick(T0 + 1))
        self.come_back(settled=False)
        self.assertTrue(self.w.tick(T0 + 2))           # (the lights only just came back)
        self.come_back()
        self.assertTrue(self.w.tick(T0 + 3))
        self.assertTrue(self.w.active)
        self.m.update(["lamp"], {"color": "#00ff00"})
        self.assertFalse(self.w.tick(T0 + 4))          # (by hand still ends it)


if __name__ == "__main__":
    unittest.main()
