import unittest
from unittest import mock

import numpy as np

from glowstage import screen
from glowstage.api import Api
from glowstage.screen import Monitor, ScreenSampler

MAIN = Monitor("\\\\.\\DISPLAY1", (0, 0, 3456, 2169), True)
SECOND = Monitor("\\\\.\\DISPLAY2", (3456, 0, 6016, 1440), False)
THIRD = Monitor("\\\\.\\DISPLAY3", (-1920, 0, 0, 1080), False)   # left of the main display: negative coordinates
BOTH = [MAIN, SECOND]


class Choosing(unittest.TestCase):
    def test_nothing_chosen_is_the_main_display(self):
        self.assertEqual(screen.choose("", [SECOND, MAIN]), (MAIN, ""))

    def test_a_named_display(self):
        self.assertEqual(screen.choose(SECOND.device, BOTH).monitor, SECOND)
        self.assertEqual(screen.choose(THIRD.device, BOTH + [THIRD]).monitor.rect, (-1920, 0, 0, 1080))

    def test_a_display_that_is_gone_falls_back_to_main_and_says_so(self):
        choice = screen.choose(THIRD.device, BOTH)
        self.assertEqual((choice.monitor, choice.missing), (MAIN, THIRD.device))

    def test_no_displays_at_all_is_not_a_crash(self):
        self.assertEqual(screen.choose("", []), (None, ""))
        self.assertEqual(screen.choose(SECOND.device, []).monitor, None)

    def test_auto_follows_a_full_screen_window_on_another_display(self):
        self.assertEqual(screen.choose("auto", BOTH, SECOND.device).monitor, SECOND)

    def test_auto_without_a_full_screen_window_stays_on_main(self):
        self.assertEqual(screen.choose("auto", BOTH, None).monitor, MAIN)

    def test_auto_with_a_window_on_a_display_that_just_went_stays_on_main(self):
        self.assertEqual(screen.choose("auto", [MAIN], SECOND.device).monitor, MAIN)

    def test_names_say_where_a_display_stands(self):
        self.assertEqual(screen.names(BOTH + [THIRD]), {MAIN.device: "Main display",
                                                         SECOND.device: "Right display · 2560 × 1440",
                                                         THIRD.device: "Left display · 1920 × 1080"})

    def test_two_on_one_side_are_numbered_left_to_right(self):
        far = Monitor("\\\\.\\DISPLAY129", (6016, 0, 7936, 1080), False)
        above = Monitor("\\\\.\\DISPLAY7", (0, -1440, 2560, 0), False)
        got = screen.names([MAIN, far, SECOND, above])
        self.assertEqual(got[SECOND.device], "Right display 1 · 2560 × 1440")
        self.assertEqual(got[far.device], "Right display 2 · 1920 × 1080")
        self.assertEqual(got[above.device], "Upper display · 2560 × 1440")

    def test_without_a_main_display_they_are_numbered(self):
        self.assertEqual(screen.names([SECOND._replace(primary=False)]), {SECOND.device: "Display 1 · 2560 × 1440"})


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Picking(unittest.TestCase):
    """The sampler's own choice over time, with its clock, displays and full-screen window faked."""

    def setUp(self):
        self.clock = Clock()
        self.setting = "auto"
        self.monitors = list(BOTH)
        self.full = None
        self.looks = 0

        def monitors():
            self.looks += 1
            return list(self.monitors)

        self.s = ScreenSampler(display=lambda: self.setting, monitors=monitors,
                               full_screen=lambda: self.full, clock=self.clock)

    def at(self, seconds):
        self.clock.now += seconds
        return self.s.pick().monitor

    def test_auto_moves_when_a_full_screen_window_appears_and_waits_a_moment_after_it_goes(self):
        self.assertEqual(self.at(0), MAIN)
        self.full = SECOND.device
        self.assertEqual(self.at(screen.REFRESH), SECOND)
        self.full = None
        self.assertEqual(self.at(screen.REFRESH), SECOND)      # a quick alt-tab: no jump
        self.assertEqual(self.at(screen.AUTO_HOLD + 0.1), MAIN)

    def test_auto_does_not_look_for_full_screen_windows_when_another_display_is_chosen(self):
        self.setting = SECOND.device
        self.full = MAIN.device
        self.assertEqual(self.at(0), SECOND)
        self.assertIsNone(self.s._held)

    def test_the_display_list_is_read_again_every_few_seconds(self):
        self.setting = SECOND.device
        self.assertEqual(self.at(0), SECOND)
        self.monitors = [MAIN]                                  # unplugged
        self.assertEqual(self.at(0.5), SECOND)                  # (not looked at yet)
        self.assertEqual(self.at(screen.REFRESH), MAIN)
        self.assertEqual(self.s.choice.missing, SECOND.device)
        self.monitors = list(BOTH)                              # plugged in again
        self.assertEqual(self.at(screen.REFRESH), SECOND)
        self.assertEqual(self.s.choice.missing, "")

    def test_a_listing_that_fails_keeps_the_last_list(self):
        self.setting = ""
        self.at(0)
        self.s._monitors = mock.Mock(side_effect=OSError("no"))
        self.assertEqual(self.at(screen.REFRESH), MAIN)


class FakeGrabber:
    def __init__(self, clock, fail_on=()):
        self.clock, self.rects, self.closed, self.fail_on = clock, [], False, set(fail_on)

    def grab(self, rect=None):
        self.rects.append(rect)
        self.clock.now += 0.1
        if len(self.rects) in self.fail_on:
            raise OSError("gone")
        return np.full((screen.H, screen.W, 3), 200, np.uint8)

    def close(self):
        self.closed = True


class Sampling(unittest.TestCase):
    def sampler(self, setting, grabber, clock, monitors):
        s = ScreenSampler(fps=1e6, display=lambda: setting, monitors=lambda: list(monitors),
                          full_screen=lambda: None, grabber=lambda: grabber, clock=clock)
        s._wanted_until = clock() + 0.45
        return s

    def test_it_asks_for_the_rectangle_of_the_chosen_display(self):
        clock = Clock()
        g = FakeGrabber(clock)
        s = self.sampler(SECOND.device, g, clock, BOTH)
        s._loop(g)
        self.assertTrue(g.rects)
        self.assertEqual(set(g.rects), {SECOND.rect})
        self.assertIsNotNone(s.shot)
        self.assertTrue(g.closed)

    def test_the_main_display_by_default(self):
        clock = Clock()
        g = FakeGrabber(clock)
        self.sampler("", g, clock, BOTH)._loop(g)
        self.assertEqual(set(g.rects), {MAIN.rect})

    def test_a_missing_display_samples_the_main_one(self):
        clock = Clock()
        g = FakeGrabber(clock)
        s = self.sampler(THIRD.device, g, clock, BOTH)
        s._loop(g)
        self.assertEqual(set(g.rects), {MAIN.rect})
        self.assertEqual(s.choice.missing, THIRD.device)

    def test_a_failed_grab_goes_on_and_looks_at_the_displays_again(self):
        clock = Clock()
        g = FakeGrabber(clock, fail_on={1})
        s = self.sampler("", g, clock, BOTH)
        s._loop(g)
        self.assertGreater(len(g.rects), 1)
        self.assertIsNotNone(s.shot)

    def test_without_any_display_it_still_asks_for_the_default_rectangle(self):
        clock = Clock()
        g = FakeGrabber(clock)
        self.sampler("", g, clock, [])._loop(g)
        self.assertEqual(set(g.rects), {None})


class FakeSettings(dict):
    saved = 0

    def save_soon(self):
        self.saved += 1


class FakeApp:
    def __init__(self, setting="", monitors=BOTH, full=None):
        self.config = FakeSettings(settings={"screen_display": setting})
        self.manager = mock.Mock()
        self.manager.preset_names.return_value = []
        self.manager.engine.screen = ScreenSampler(display=lambda: self.config["settings"]["screen_display"],
                                                   monitors=lambda: list(monitors), full_screen=lambda: full)


class Settings(unittest.TestCase):
    def test_the_choices_that_can_be_saved(self):
        for value in ("", "auto", "\\\\.\\DISPLAY2", "\\\\.\\DISPLAY12"):
            app = FakeApp()
            self.assertTrue(Api(app).set_setting("screen_display", value)["ok"], value)
            self.assertEqual(app.config["settings"]["screen_display"], value)
            self.assertEqual(app.config.saved, 1)

    def test_nothing_else_can_be_saved(self):
        for value in ("DISPLAY2", "\\\\.\\DISPLAY", "\\\\.\\DISPLAYX", "yes", "preset:Gaming", "\\\\.\\DISPLAY2\\..", "\\\\.\\DISPLAY1234"):
            app = FakeApp()
            answer = Api(app).set_setting("screen_display", value)
            self.assertFalse(answer["ok"], value)
            self.assertEqual(answer["error"], "Choose a display from the list")
            self.assertEqual(app.config["settings"]["screen_display"], "")

    def test_none_means_the_main_display(self):
        app = FakeApp("auto")
        self.assertTrue(Api(app).set_setting("screen_display", None)["ok"])
        self.assertEqual(app.config["settings"]["screen_display"], "")

    def test_the_setting_has_a_default(self):
        from glowstage.config import DEFAULTS
        self.assertEqual(DEFAULTS["settings"]["screen_display"], "")


class Listing(unittest.TestCase):
    def listing(self, setting="", monitors=BOTH, full=None):
        app = FakeApp(setting, monitors, full)
        with mock.patch.object(screen, "list_monitors", lambda: list(monitors)), \
                mock.patch.object(screen, "full_screen_device", lambda: full):
            answer = Api(app).list_displays()
        self.assertTrue(answer["ok"], answer)
        return answer["result"]

    def test_the_options(self):
        r = self.listing()
        self.assertEqual(r["options"], [
            {"id": "", "name": "Main display", "primary": True},
            {"id": "\\\\.\\DISPLAY2", "name": "Right display · 2560 × 1440", "primary": False},
            {"id": "auto", "name": "Where the game or video is", "primary": False}])
        self.assertEqual((r["count"], r["current"], r["note"]), (2, "", ""))
        self.assertEqual(r["using"]["name"], "Main display")

    def test_one_display_is_counted_so_the_window_can_hide_the_choice(self):
        self.assertEqual(self.listing(monitors=[MAIN])["count"], 1)

    def test_the_chosen_display(self):
        r = self.listing(SECOND.device)
        self.assertEqual((r["current"], r["using"]["id"], r["note"]), (SECOND.device, SECOND.device, ""))

    def test_a_display_that_is_gone_says_so(self):
        r = self.listing(THIRD.device)
        self.assertEqual(r["note"], "The display you picked isn't connected: following the main display")
        self.assertEqual(r["using"]["name"], "Main display")
        self.assertIn({"id": THIRD.device, "name": "The display you picked (not connected)", "primary": False, "missing": True},
                      r["options"])

    def test_auto_says_where_it_is(self):
        self.assertEqual(self.listing("auto", full=SECOND.device)["using"]["id"], SECOND.device)
        self.assertEqual(self.listing("auto")["using"]["name"], "Main display")


if __name__ == "__main__":
    unittest.main()
