import datetime
import json
import time
import unittest

from glowstage.automations import MORNING, Automations
from tests.test_app_looks import FakeManager


class Manager(FakeManager):
    def __init__(self):
        super().__init__()
        self.config.save_soon = lambda: None
        self.changes = 0
        self.config.data["settings"]["auto_game"] = "preset:Gaming"

    def _changed(self):
        self.changes += 1


class Pausing(unittest.TestCase):
    def setUp(self):
        self.m = Manager()
        self.a = Automations(self.m)

    def settings(self):
        return self.m.config.data["settings"]

    def game(self, start, seconds, on=True):
        """A game full screen (or not) for `seconds`, one look at the PC a second."""
        for k in range(seconds):
            self.a.step(start + k, self.a.rules(), "game" if on else None, 0.0)

    def test_an_hour_and_three(self):
        now = time.time()
        self.assertEqual(self.a.pause(60, now=now), now + 3600)
        self.assertEqual(self.a.paused_until(now), now + 3600)
        self.assertEqual(self.a.pause("180", now=now), now + 3 * 3600)
        self.assertGreater(self.m.changes, 0)

    def test_until_tomorrow_morning(self):
        evening = datetime.datetime(2026, 11, 28, 22, 15).timestamp()
        until = datetime.datetime.fromtimestamp(self.a.pause("tomorrow", now=evening))
        self.assertEqual((until.day, until.hour, until.minute), (29, MORNING, 0))
        night = datetime.datetime(2026, 11, 29, 2, 0).timestamp()       # (after midnight: this morning)
        until = datetime.datetime.fromtimestamp(self.a.pause("tomorrow", now=night))
        self.assertEqual((until.day, until.hour), (29, MORNING))

    def test_only_the_choices(self):
        for bad in (0, 5, 61, "soon", None, -60):
            with self.assertRaises(ValueError):
                self.a.pause(bad)
        self.assertEqual(self.a.paused_until(), 0)

    def test_over_by_itself(self):
        now = time.time()
        self.a.pause(60, now=now)
        self.assertEqual(self.a.paused_until(now + 3601), 0)

    def test_resume(self):
        self.a.pause(60)
        self.a.resume()
        self.assertEqual(self.a.paused_until(), 0)
        self.assertEqual(self.settings()["auto_paused_until"], 0)

    def test_kept_in_the_settings(self):
        until = self.a.pause(180)
        again = Automations(self.m)                  # (a restart reads it back)
        self.assertEqual(again.paused_until(), until)
        json.dumps(self.settings())                  # (and it can be saved)

    def test_no_new_look_while_paused(self):
        self.a.pause(60)
        self.game(1000.0, 8)
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.look["lamp"]["color"], "#ffffff")

    def test_the_look_that_is_on_ends_and_puts_the_lights_back(self):
        self.game(1000.0, 8)
        self.assertEqual(self.a.active, "game")
        self.assertEqual(self.m.look["lamp"]["color"], "#3a00ff")
        self.a.pause(60)
        self.game(1008.0, 1)
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.look["lamp"]["color"], "#ffffff")
        self.assertEqual(self.settings()["active_preset"], "Mine")

    def test_after_the_pause_it_works_again(self):
        self.a.pause(60)
        self.game(1000.0, 8)
        self.a.resume()
        self.game(1008.0, 2)
        self.assertEqual(self.a.active, "game")


class Links(unittest.TestCase):
    """/do/pause/60, /do/pause/tomorrow and /do/resume for Stream Deck and the like."""

    def test_links(self):
        from glowstage.remote import Remote
        m = Manager()
        m.automations = Automations(m)
        r = Remote(m, m.config)
        self.assertEqual(r.link(["pause", "60"])[0], 200)
        self.assertGreater(m.automations.paused_until(), 0)
        self.assertEqual(r.link(["resume"])[0], 200)
        self.assertEqual(m.automations.paused_until(), 0)
        self.assertEqual(r.link(["pause", "tomorrow"])[0], 200)
        self.assertEqual(r.link(["pause", "7"])[0], 400)
        self.assertEqual(r.link(["pause", "soon"])[0], 400)


if __name__ == "__main__":
    unittest.main()
