import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.focus import BREAK_COLOR
from glowstage.manager import DeviceManager

T0 = 1_000_000.0
WORK, REST = 25 * 60, 5 * 60


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, dev_id)
        self.group = group
        self.online = True

    def apply(self, state):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in (("lamp", "room"), ("strip", "room"), ("case", "pc")):
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 40})
        self.m.update(["strip"], {"brightness": 70})
        self.f = self.m.focus
        alive = type("Alive", (), {"is_alive": lambda self: True})()   # steps by hand here
        for t in (self.f, self.m.cycle, self.m.sleep, self.m.wake):
            t._thread = alive

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def shown(self):
        return self.config["settings"]["active_preset"]

    def colors(self):
        return {i: st.color for i, st in self.m.states.items()}

    def go_to_break(self, rounds=3):
        self.f.start(25, 5, rounds, "", now=T0)
        self.assertTrue(self.f.tick(T0 + WORK))
        self.assertEqual(self.f.phase, "break")


class Phases(Base):
    def test_it_starts_with_work(self):
        self.f.start(25, 5, 4, "", now=T0)
        d = self.f.describe(now=T0)
        self.assertTrue(self.f.active)
        self.assertEqual((d["phase"], d["round"], d["rounds"], d["at"]), ("work", 1, 4, T0 + WORK))

    def test_work_then_break_then_work_round_after_round(self):
        self.f.start(25, 5, 3, "", now=T0)
        seen = []
        for _ in range(5):
            seen.append((self.f.phase, self.f.round))
            end = self.f.at
            self.f.tick(end - 1)                                # not yet: nothing moves
            self.assertEqual((self.f.phase, self.f.round), seen[-1])
            if not self.f.tick(end):
                break
        self.assertEqual(seen, [("work", 1), ("break", 1), ("work", 2), ("break", 2), ("work", 3)])
        self.assertFalse(self.f.active)                         # (the last work ended it)

    def test_phases_have_the_times_asked_for(self):
        self.f.start(45, 15, 2, "", now=T0)
        self.assertEqual(self.f.at, T0 + 45 * 60)
        self.f.tick(T0 + 45 * 60)
        self.assertEqual(self.f.at, T0 + 45 * 60 + 15 * 60)
        self.f.tick(T0 + 60 * 60)
        self.assertEqual((self.f.phase, self.f.at), ("work", T0 + 60 * 60 + 45 * 60))

    def test_nothing_moves_before_the_time(self):
        self.f.start(25, 5, 2, "", now=T0)
        self.assertTrue(self.f.tick(T0 + WORK - 1))
        self.assertEqual(self.f.phase, "work")

    def test_the_last_round_has_no_break(self):
        self.f.start(25, 5, 2, "", now=T0)
        self.f.tick(T0 + WORK)
        self.f.tick(T0 + WORK + REST)
        self.assertEqual((self.f.phase, self.f.round), ("work", 2))
        self.assertFalse(self.f.tick(T0 + 2 * WORK + REST))
        self.assertFalse(self.f.active)
        self.assertIsNone(self.f.describe())

    def test_one_round_is_work_and_done(self):
        self.f.start(15, 5, 1, "", now=T0)
        self.assertFalse(self.f.tick(T0 + 15 * 60))
        self.assertFalse(self.f.active)


class Lights(Base):
    def test_keep_my_lights_changes_nothing_at_the_start(self):
        look = self.m.looks()
        self.f.start(25, 5, 4, "", now=T0)
        self.assertEqual(self.m.looks(), look)

    def test_a_preset_is_the_work_light(self):
        self.f.start(25, 5, 4, "Focus", now=T0)
        self.assertEqual(self.shown(), "Focus")
        self.assertNotEqual(self.m.states["lamp"].color, "#123456")

    def test_the_break_is_a_calm_green_at_their_own_brightness(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.f.tick(T0 + WORK)
        self.assertEqual(BREAK_COLOR, "#4fd1a5")
        self.assertEqual({i: (st.mode, st.color) for i, st in self.m.states.items()},
                         {i: ("color", BREAK_COLOR) for i in ("lamp", "strip", "case")})
        self.assertEqual(self.m.states["lamp"].brightness, 40)
        self.assertEqual(self.m.states["strip"].brightness, 70)
        self.assertEqual(self.shown(), "")

    def test_lights_that_are_off_stay_off_in_the_break(self):
        self.m.update(["case"], {"on": False})
        self.f.start(25, 5, 3, "", now=T0)
        self.f.tick(T0 + WORK)
        self.assertFalse(self.m.states["case"].on)
        self.assertEqual(self.m.states["case"].color, "#123456")

    def test_after_the_break_the_work_light_is_back(self):
        self.f.start(25, 5, 3, "Focus", now=T0)
        work = self.m.looks()
        self.f.tick(T0 + WORK)
        self.assertNotEqual(self.m.looks(), work)
        self.f.tick(T0 + WORK + REST)
        self.assertEqual(self.m.looks(), work)
        self.assertEqual(self.shown(), "Focus")

    def test_after_the_last_round_the_lights_are_as_before_the_timer(self):
        self.m.apply_preset("Cozy")
        before, preset = self.m.looks(), self.shown()
        self.f.start(25, 5, 2, "Focus", now=T0)
        self.f.tick(T0 + WORK)
        self.f.tick(T0 + WORK + REST)
        self.assertFalse(self.f.tick(T0 + 2 * WORK + REST))
        self.assertEqual(self.m.looks(), before)
        self.assertEqual(self.shown(), preset)

    def test_a_change_by_hand_during_work_survives_the_break(self):
        self.f.start(25, 5, 3, "Focus", now=T0)
        self.m.update(["lamp"], {"color": "#00ff00"})
        self.assertTrue(self.f.tick(T0 + 100))                  # (it just becomes your work light)
        self.assertTrue(self.f.active)
        self.f.tick(T0 + WORK)
        self.assertEqual(self.m.states["lamp"].color, BREAK_COLOR)
        self.f.tick(T0 + WORK + REST)
        self.assertEqual(self.m.states["lamp"].color, "#00ff00")

    def test_a_change_by_hand_during_the_break_stays_until_it_ends(self):
        self.go_to_break()
        self.m.update(["lamp"], {"color": "#ff0000"})
        self.assertTrue(self.f.tick(T0 + WORK + 60))
        self.assertEqual(self.m.states["lamp"].color, "#ff0000")
        self.f.tick(T0 + WORK + REST)
        self.assertEqual(self.m.states["lamp"].color, "#123456")

    def test_the_break_isnt_saved_if_glowstage_stops_in_it(self):
        self.go_to_break()
        fresh = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.assertEqual(self.config["states"]["lamp"]["color"], "#123456")
        self.assertFalse(fresh.focus.active)
        self.assertIsNone(fresh.focus.describe())

    def test_a_turn_isnt_remembered_for_undo(self):
        self.m.remember()
        before = self.m._undo
        self.f.start(25, 5, 3, "", now=T0)
        self.f.tick(T0 + WORK)
        self.f.tick(T0 + WORK + REST)
        self.assertIs(self.m._undo, before)


class AutomaticLooks(Base):
    def test_a_break_starting_under_an_automatic_look_changes_no_light(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.m.automations.active = "game"
        self.m.apply_preset("Gaming")                           # (what the automatic look put on)
        look = self.m.looks()
        self.assertTrue(self.f.tick(T0 + WORK))
        self.assertEqual(self.m.looks(), look)
        self.assertEqual((self.f.phase, self.f.at), ("break", T0 + WORK + REST))   # only the time counts

    def test_the_end_of_a_skipped_break_changes_no_light_either(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.m.automations.active = "call"
        self.f.tick(T0 + WORK)
        self.m.apply_preset("Gaming")
        look = self.m.looks()
        self.assertTrue(self.f.tick(T0 + WORK + REST))
        self.assertEqual(self.m.looks(), look)
        self.assertEqual((self.f.phase, self.f.round), ("work", 2))

    def test_an_automatic_look_in_a_break_keeps_its_lights_at_the_end_of_it(self):
        self.go_to_break()
        self.m.automations.active = "video"
        self.m.apply_preset("Movie")
        look = self.m.looks()
        self.f.tick(T0 + WORK + REST)
        self.assertEqual(self.m.looks(), look)
        self.assertEqual(self.f.phase, "work")

    def test_the_last_end_leaves_the_lights_to_the_automatic_look(self):
        self.f.start(25, 5, 1, "", now=T0)
        self.m.automations.active = "game"
        self.m.apply_preset("Gaming")
        look = self.m.looks()
        self.assertFalse(self.f.tick(T0 + WORK))
        self.assertEqual(self.m.looks(), look)

    def test_lights_off_under_an_automatic_look_dont_end_it(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.m.automations.active = "game"
        self.m.set_power_all(False)
        self.assertTrue(self.f.tick(T0 + 10))


class Ends(Base):
    def test_all_lights_off_ends_it_in_work(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.m.set_power_all(False)
        self.assertFalse(self.f.tick(T0 + 10))
        self.assertFalse(self.f.active)
        self.assertFalse(self.m.states["lamp"].on)
        self.assertFalse(self.f.tick(T0 + WORK))

    def test_all_lights_off_ends_it_in_a_break_and_nothing_is_put_back(self):
        self.go_to_break()
        self.m.set_power_all(False)
        self.assertFalse(self.f.tick(T0 + WORK + 10))
        self.assertFalse(self.m.states["lamp"].on)
        self.assertFalse(self.f.tick(T0 + WORK + REST))
        self.assertEqual(self.m.states["lamp"].color, BREAK_COLOR)

    def test_one_light_off_isnt_leaving(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.m.update(["case"], {"on": False})
        self.assertTrue(self.f.tick(T0 + 10))


class Skip(Base):
    def test_skip_work_goes_to_the_break_now(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.f.skip(now=T0 + 60)
        self.assertEqual((self.f.phase, self.f.at), ("break", T0 + 60 + REST))
        self.assertEqual(self.m.states["lamp"].color, BREAK_COLOR)

    def test_skip_the_break_goes_to_the_next_work_now(self):
        self.go_to_break()
        self.f.skip(now=T0 + WORK + 30)
        self.assertEqual((self.f.phase, self.f.round, self.f.at), ("work", 2, T0 + WORK + 30 + WORK))
        self.assertEqual(self.m.states["lamp"].color, "#123456")

    def test_skip_the_last_work_ends_it_with_the_lights_back(self):
        self.m.update(None, {"color": "#abcdef"})
        before = self.m.looks()
        self.f.start(25, 5, 1, "Focus", now=T0)
        self.f.skip(now=T0 + 10)
        self.assertFalse(self.f.active)
        self.assertEqual(self.m.looks(), before)

    def test_skip_while_it_doesnt_run_does_nothing(self):
        self.f.skip(now=T0)
        self.assertFalse(self.f.active)


class Cancel(Base):
    def test_stop_in_work_leaves_the_lights(self):
        self.f.start(25, 5, 3, "Focus", now=T0)
        look = self.m.looks()
        self.f.cancel()
        self.assertFalse(self.f.active)
        self.assertEqual(self.m.looks(), look)
        self.assertFalse(self.f.tick(T0 + WORK))

    def test_stop_in_a_break_brings_the_work_light_back(self):
        self.f.start(25, 5, 3, "Focus", now=T0)
        work = self.m.looks()
        self.f.tick(T0 + WORK)
        self.f.cancel()
        self.assertFalse(self.f.active)
        self.assertEqual(self.m.looks(), work)

    def test_starting_stops_presets_in_turn(self):
        self.m.cycle.start(["Cozy", "Reading"], 5, False, now=T0)
        self.f.start(25, 5, 3, "", now=T0)
        self.assertFalse(self.m.cycle.active)
        self.assertTrue(self.f.active)

    def test_the_sleep_timer_starting_stops_it_and_fades_from_the_work_light(self):
        self.go_to_break()
        self.m.sleep.start(30)
        self.assertFalse(self.f.active)
        self.assertFalse(self.f.tick(T0 + WORK + 10))
        self.assertEqual(self.m.sleep._lights["lamp"][0], 40)
        self.assertTrue(self.m.sleep.active)

    def test_the_wake_up_starting_stops_it(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.m.wake.start(30)
        self.assertFalse(self.f.active)
        self.assertFalse(self.f.tick(T0 + 10))

    def test_a_new_start_in_a_break_begins_again_from_the_work_light(self):
        self.go_to_break()
        self.f.start(15, 10, 2, "", now=T0 + WORK + 60)
        self.assertEqual((self.f.phase, self.f.round, self.f.rounds), ("work", 1, 2))
        self.assertEqual(self.m.states["lamp"].color, "#123456")    # (not the green room)
        self.f.skip(now=T0 + WORK + 61)
        self.f.skip(now=T0 + WORK + 62)
        self.assertEqual(self.m.states["lamp"].color, "#123456")


class Describe(Base):
    def test_the_card_text_in_work(self):
        self.f.start(25, 5, 4, "", now=T0)
        d = self.f.describe(now=T0 + 7 * 60 + 10)
        self.assertEqual(d["label"], "Focus · 18 min left · round 1 of 4")
        self.assertEqual(d["left"], 18)
        self.assertIn("break of 5 minutes", d["text"])

    def test_the_card_text_in_the_break(self):
        self.go_to_break(rounds=4)
        d = self.f.describe(now=T0 + WORK + 2 * 60)
        self.assertEqual(d["label"], "Break · 3 min left")
        self.assertEqual(d["phase"], "break")
        self.assertIn("work light", d["text"])

    def test_the_last_round_says_the_lights_go_back(self):
        self.f.start(25, 5, 2, "", now=T0)
        self.f.skip(now=T0 + 1)
        self.f.skip(now=T0 + 2)
        d = self.f.describe(now=T0 + 3)
        self.assertEqual(d["round"], 2)
        self.assertIn("go back", d["text"])

    def test_a_single_round_has_no_round_count(self):
        self.f.start(25, 5, 1, "", now=T0)
        self.assertEqual(self.f.describe(now=T0)["label"], "Focus · 25 min left")

    def test_it_never_says_zero_minutes(self):
        self.f.start(25, 5, 2, "", now=T0)
        self.assertEqual(self.f.describe(now=T0 + WORK - 5)["left"], 1)

    def test_nothing_when_it_doesnt_run(self):
        self.assertIsNone(self.f.describe())


class Start(Base):
    def test_the_last_choice_is_saved(self):
        self.f.start(45, 10, 6, "Reading", now=T0)
        self.assertEqual(self.config["settings"]["focus_last"],
                         {"work": 45, "rest": 10, "rounds": 6, "look": "Reading"})

    def test_the_default_work_light_is_the_focus_preset(self):
        c = self.f.last()
        self.assertEqual((c["work"], c["rest"], c["rounds"], c["look"]), (25, 5, 4, "Focus"))

    def test_without_a_focus_preset_the_default_keeps_the_lights(self):
        self.m.delete_preset("Focus")
        self.assertEqual(self.f.last()["look"], "")

    def test_start_with_the_last_choice(self):
        self.f.start(50, 15, 2, "", now=T0)
        self.f.cancel()
        self.f.start_last()
        self.assertEqual((self.f.work, self.f.rest, self.f.rounds), (50, 15, 2))

    def test_a_last_work_light_that_is_gone_becomes_keep_my_lights(self):
        self.f.start(25, 5, 2, "Reading", now=T0)
        self.f.cancel()
        self.m.delete_preset("Reading")
        self.assertEqual(self.config["settings"]["focus_last"]["look"], "")
        self.assertEqual(self.f.last()["look"], "")

    def test_renaming_the_work_light_follows(self):
        self.f.start(25, 5, 2, "Reading", now=T0)
        self.m.rename_preset("Reading", "Books")
        self.assertEqual(self.f.look, "Books")
        self.assertEqual(self.config["settings"]["focus_last"]["look"], "Books")

    def test_the_choices_that_are_offered(self):
        for args in ((20, 5, 4), (25, 7, 4), (25, 5, 0), (25, 5, 9), ("x", 5, 4), (25, None, 4)):
            with self.assertRaises(ValueError):
                self.f.start(*args, "", now=T0)
        self.assertFalse(self.f.active)
        for work in (15, 25, 45, 50):
            self.f.start(work, 5, 8, "", now=T0)
            self.assertEqual(self.f.work, work)

    def test_a_preset_that_isnt_there(self):
        with self.assertRaisesRegex(ValueError, "gone"):
            self.f.start(25, 5, 4, "Nope", now=T0)
        self.assertFalse(self.f.active)
        self.assertNotIn("focus_last", self.config["settings"])

    def test_keeping_the_lights_with_every_light_off(self):
        self.m.set_power_all(False)
        with self.assertRaisesRegex(ValueError, "Switch some lights on"):
            self.f.start(25, 5, 4, "", now=T0)
        self.assertFalse(self.f.active)

    def test_nothing_is_saved_but_the_last_choice(self):
        self.f.start(25, 5, 4, "", now=T0)
        fresh = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.assertFalse(fresh.focus.active)                    # (after a restart it's off)



class SteppingAway(Base):
    """A break is when you lock the PC and walk away: that must not end the timer."""

    def leave(self):
        """Lock the PC with "lights off when you step away" on for every light."""
        self.m.config["settings"]["away_lights"] = "all"
        self.m.away.set("locked", True)

    def come_back(self, settled=True):
        self.m.away.set("locked", False)
        if settled:
            self.m.away.back_at = -1e9              # (the few seconds for the lights to settle are over)

    def test_locking_the_pc_in_a_break_doesnt_end_it(self):
        self.go_to_break()
        self.leave()
        self.assertFalse(any(st.on for st in self.m.states.values()))
        self.assertTrue(self.f.tick(T0 + WORK + 60))
        self.assertTrue(self.f.active)
        self.come_back()
        self.assertTrue(self.f.tick(T0 + WORK + 120))
        self.assertEqual(set(self.colors().values()), {BREAK_COLOR})       # still the break
        self.assertTrue(self.f.tick(T0 + WORK + REST))
        self.assertEqual((self.f.phase, set(self.colors().values())), ("work", {"#123456"}))

    def test_a_break_that_ends_while_you_are_away_shows_work_when_you_are_back(self):
        self.go_to_break()
        self.leave()
        self.assertTrue(self.f.tick(T0 + WORK + REST))
        self.assertEqual(self.f.phase, "work")
        self.assertFalse(any(st.on for st in self.m.states.values()))      # (left alone while away)
        self.come_back()
        self.assertTrue(self.f.tick(T0 + WORK + REST + 60))
        self.assertTrue(all(st.on for st in self.m.states.values()))
        self.assertEqual(set(self.colors().values()), {"#123456"})

    def test_a_break_that_begins_while_you_are_away_shows_when_you_are_back(self):
        self.f.start(25, 5, 3, "", now=T0)
        self.leave()
        self.assertTrue(self.f.tick(T0 + WORK))
        self.assertEqual(self.f.phase, "break")
        self.come_back()
        self.assertTrue(self.f.tick(T0 + WORK + 60))
        self.assertEqual(set(self.colors().values()), {BREAK_COLOR})
        self.assertTrue(self.f.tick(T0 + WORK + REST))
        self.assertEqual(set(self.colors().values()), {"#123456"})

    def test_the_moment_the_lights_come_back_is_not_you(self):
        self.go_to_break()
        self.leave()
        self.m.away._off = None                        # (Windows said you're back; the lights come on a moment later)
        self.m.away.back_at = time.monotonic()
        self.assertTrue(self.f.tick(T0 + WORK + 60))   # not "you switched every light off"
        self.assertTrue(self.f.active)

    def test_switching_everything_off_yourself_still_ends_it(self):
        self.go_to_break()
        self.m.update(None, {"on": False})
        self.assertFalse(self.f.tick(T0 + WORK + 60))


class TheOtherModes(Base):
    def test_one_at_a_time(self):
        self.m.wallpaper._thread = self.f._thread       # (steps by hand here too)
        patcher = mock.patch("glowstage.wallpaper.stamp", return_value=("color", ""))   # (not the real desktop)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m.save_preset("A")
        self.m.save_preset("B")
        self.f.start(25, 5, 2, "", now=T0)
        self.m.cycle.start(["A", "B"], 5, now=T0)
        self.assertFalse(self.f.active)                # presets in turn stops the focus timer
        self.f.start(25, 5, 2, "", now=T0)
        self.assertFalse(self.m.cycle.active)          # ... and the other way round
        self.m.wallpaper.start(now=T0)
        self.assertFalse(self.f.active)                # following the wallpaper too
        self.f.start(25, 5, 2, "", now=T0)
        self.assertFalse(self.m.wallpaper.active)


if __name__ == "__main__":
    unittest.main()
