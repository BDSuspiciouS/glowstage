import random
import tempfile
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager

T0 = 1_000_000.0
NAMES = ["Cozy", "Reading", "Late night"]


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
        self.c = self.m.cycle
        self.c._rng = random.Random(7)
        self.c._thread = type("Alive", (), {"is_alive": lambda self: True})()   # steps by hand here
        self.m.sleep._thread = type("Alive", (), {"is_alive": lambda self: True})()
        self.m.wake._thread = type("Alive", (), {"is_alive": lambda self: True})()

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def shown(self):
        return self.config["settings"]["active_preset"]


class Turns(Base):
    def test_the_first_preset_comes_on_at_once(self):
        self.c.start(NAMES, 10, False, now=T0)
        self.assertEqual(self.shown(), "Cozy")
        self.assertEqual(self.c.describe()["now"], "Cozy")
        self.assertEqual(self.c.describe()["next"], "Reading")
        self.assertEqual(self.c.describe()["at"], T0 + 600)

    def test_the_next_one_after_the_minutes_and_round_and_round(self):
        self.c.start(NAMES, 5, False, now=T0)
        seen = [self.shown()]
        for k in range(1, 6):
            self.assertTrue(self.c.tick(T0 + k * 300 - 1))      # not yet
            self.assertEqual(self.shown(), seen[-1])
            self.assertTrue(self.c.tick(T0 + k * 300))
            seen.append(self.shown())
        self.assertEqual(seen, ["Cozy", "Reading", "Late night", "Cozy", "Reading", "Late night"])

    def test_the_lights_follow_the_preset(self):
        self.c.start(["Cozy", "Reading"], 1, False, now=T0)
        cozy = self.m.states["lamp"].to_dict()
        self.c.tick(T0 + 60)
        self.assertNotEqual(self.m.states["lamp"].to_dict(), cozy)

    def test_shuffle_every_preset_once_per_round_never_twice_in_a_row(self):
        names = ["Cozy", "Reading", "Late night", "Focus"]
        self.c.start(names, 1, True, now=T0)
        order = [self.shown()]
        for k in range(1, 4 * 40):
            self.c.tick(T0 + k * 60)
            order.append(self.shown())
        for k in range(0, len(order), 4):                       # a round: each one once
            self.assertEqual(sorted(order[k:k + 4]), sorted(names))
        for a, b in zip(order, order[1:]):                      # (also from one round into the next)
            self.assertNotEqual(a, b)

    def test_shuffle_with_two_presets_alternates(self):
        self.c.start(["Cozy", "Reading"], 1, True, now=T0)
        order = [self.shown()]
        for k in range(1, 10):
            self.c.tick(T0 + k * 60)
            order.append(self.shown())
        for a, b in zip(order, order[1:]):
            self.assertNotEqual(a, b)

    def test_a_turn_isnt_remembered_for_undo(self):
        self.m.update(None, {"color": "#ff0000"})
        self.m.remember()
        before = self.m._undo
        self.c.start(NAMES, 1, False, now=T0)
        self.c.tick(T0 + 60)
        self.assertIs(self.m._undo, before)
        self.assertEqual(self.m._undo[0]["lamp"]["color"], "#ff0000")


class Stops(Base):
    def setUp(self):
        super().setUp()
        self.c.start(NAMES, 5, False, now=T0)

    def test_a_color_by_hand_stops_it_and_nothing_more_is_applied(self):
        self.m.update(["lamp"], {"color": "#00ff00"})
        self.assertFalse(self.c.tick(T0 + 10))
        self.assertFalse(self.c.active)
        self.assertIsNone(self.c.describe())
        self.assertFalse(self.c.tick(T0 + 600))
        self.assertEqual(self.m.states["lamp"].color, "#00ff00")
        self.assertEqual(self.shown(), "")

    def test_another_preset_by_hand_stops_it(self):
        self.m.apply_preset("Focus")
        self.assertFalse(self.c.tick(T0 + 10))
        self.assertEqual(self.shown(), "Focus")

    def test_the_same_look_under_another_preset_name_stops_it_too(self):
        self.config["settings"]["active_preset"] = "Focus"
        self.assertFalse(self.c.tick(T0 + 10))

    def test_switching_the_lights_off_stops_it(self):
        self.m.set_power_all(False)
        self.assertFalse(self.c.tick(T0 + 10))
        self.assertFalse(self.m.states["lamp"].on)

    def test_a_change_just_before_a_turn_stops_it_instead_of_turning(self):
        self.m.update(["case"], {"brightness": 5})
        self.assertFalse(self.c.tick(T0 + 300))
        self.assertEqual(self.m.states["case"].brightness, 5)
        self.assertNotEqual(self.shown(), "Reading")

    def test_undo_stops_it(self):
        self.c.cancel()
        self.m.update(None, {"mode": "color", "color": "#123456"})
        self.m.remember()                                       # (what the window does before starting it)
        self.c.start(NAMES, 5, False, now=T0)
        self.m.undo()                                           # back to before the first preset
        self.assertFalse(self.c.tick(T0 + 10))
        self.assertEqual(self.m.states["lamp"].color, "#123456")

    def test_the_sleep_timer_starting_stops_it(self):
        self.m.sleep.start(30)
        self.assertFalse(self.c.active)
        self.assertFalse(self.c.tick(T0 + 300))

    def test_the_wake_up_starting_stops_it(self):
        self.m.wake.start(30)
        self.assertFalse(self.c.active)

    def test_stop_leaves_the_lights_as_they_are(self):
        look = self.m.looks()
        self.c.cancel()
        self.assertFalse(self.c.active)
        self.assertEqual(self.m.looks(), look)

    def test_a_new_start_begins_again(self):
        self.c.tick(T0 + 300)
        self.c.start(["Focus", "Movie"], 1, False, now=T0 + 400)
        self.assertEqual(self.shown(), "Focus")
        self.assertEqual(self.c.names, ["Focus", "Movie"])


class AutomaticLooks(Base):
    def setUp(self):
        super().setUp()
        self.c.start(NAMES, 5, False, now=T0)

    def test_a_turn_is_skipped_while_an_automatic_look_is_on(self):
        self.m.automations.active = "game"
        self.m.apply_preset("Gaming")                           # (what the automatic look put on)
        self.assertTrue(self.c.tick(T0 + 300))
        self.assertEqual(self.shown(), "Gaming")                # not fought over
        self.assertTrue(self.c.active)
        self.assertEqual(self.c.describe()["at"], T0 + 600)     # this turn is gone: the next one comes later

    def test_it_goes_on_when_the_automatic_look_gives_the_lights_back(self):
        look = self.m.looks()
        self.m.automations.active = "video"
        self.m.apply_preset("Movie")
        self.assertTrue(self.c.tick(T0 + 100))
        self.m.restore_looks(look, "Cozy")                      # (what automations.py does at the end)
        self.m.automations.active = None
        self.assertTrue(self.c.tick(T0 + 200))
        self.assertTrue(self.c.active)
        self.assertTrue(self.c.tick(T0 + 600))
        self.assertEqual(self.shown(), "Reading")               # the skipped turn didn't use up Reading

    def test_by_hand_after_the_automatic_look_still_stops_it(self):
        self.m.automations.active = "call"
        self.m.apply_preset("Gaming")
        self.assertTrue(self.c.tick(T0 + 100))
        self.m.automations.active = None                        # (you took over: automations.py lets go)
        self.assertFalse(self.c.tick(T0 + 200))


class Edits(Base):
    def setUp(self):
        super().setUp()
        self.c.start(NAMES, 5, False, now=T0)

    def test_rename_updates_the_list_and_the_last_choice_and_doesnt_stop_it(self):
        self.m.rename_preset("Reading", "Books")
        self.assertEqual(self.c.names, ["Cozy", "Books", "Late night"])
        self.assertEqual(self.config["settings"]["cycle_last"]["names"], ["Cozy", "Books", "Late night"])
        self.assertTrue(self.c.tick(T0 + 10))
        self.assertTrue(self.c.tick(T0 + 300))
        self.assertEqual(self.shown(), "Books")

    def test_renaming_the_one_that_is_on(self):
        self.m.rename_preset("Cozy", "Warm evening")
        self.assertTrue(self.c.tick(T0 + 10))
        self.assertEqual(self.c.describe()["now"], "Warm evening")

    def test_rename_while_not_running_still_updates_the_last_choice(self):
        self.c.cancel()
        self.m.rename_preset("Cozy", "Warm evening")
        self.assertEqual(self.config["settings"]["cycle_last"]["names"], ["Warm evening", "Reading", "Late night"])

    def test_delete_removes_it_from_the_list(self):
        self.m.delete_preset("Reading")
        self.assertEqual(self.c.names, ["Cozy", "Late night"])
        self.assertEqual(self.config["settings"]["cycle_last"]["names"], ["Cozy", "Late night"])
        self.assertTrue(self.c.tick(T0 + 10))
        self.c.tick(T0 + 300)
        self.assertEqual(self.shown(), "Late night")

    def test_delete_with_fewer_than_two_left_stops_it(self):
        self.m.delete_preset("Reading")
        self.m.delete_preset("Late night")
        self.assertFalse(self.c.active)


class Start(Base):
    def test_fewer_than_two_presets(self):
        for names in ([], ["Cozy"], ["Cozy", "Cozy"]):
            with self.assertRaisesRegex(ValueError, "at least two"):
                self.c.start(names, 10, False)
        self.assertFalse(self.c.active)

    def test_unknown_minutes(self):
        for minutes in (0, 2, 61, "x", None):
            with self.assertRaisesRegex(ValueError, "Choose 1, 5, 10"):
                self.c.start(NAMES, minutes, False)
        self.assertFalse(self.c.active)

    def test_a_preset_that_isnt_there(self):
        with self.assertRaisesRegex(ValueError, "gone"):
            self.c.start(["Cozy", "Nope"], 10, False)

    def test_the_last_choice_is_saved(self):
        self.c.start(["Reading", "Cozy"], 15, True, now=T0)
        self.assertEqual(self.config["settings"]["cycle_last"],
                         {"names": ["Reading", "Cozy"], "minutes": 15, "shuffle": True})

    def test_nothing_is_saved_but_the_last_choice(self):
        self.c.start(NAMES, 10, False, now=T0)
        fresh = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.assertFalse(fresh.cycle.active)                    # (after a restart it's off)
        self.assertIsNone(fresh.cycle.describe())


if __name__ == "__main__":
    unittest.main()
