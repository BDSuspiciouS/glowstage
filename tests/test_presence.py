import datetime
import tempfile
import unittest
from pathlib import Path

from glowstage.automations import Automations
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.presence import BRIGHTNESS, MAX_CHANGES, SPREAD, WARM, plan_for
from tests.test_app_looks import FakeManager

D = datetime.date(2026, 11, 28)
SETTINGS = {"on": True, "lights": "room", "groups": [], "when": "time", "at": "18:00", "off": "23:30"}
IDS = ["a", "b", "c", "d"]


def at(hour, minute=0, day=28):
    return datetime.datetime(2026, 11, day, hour, minute)


def ts(hour, minute=0, day=28):
    return at(hour, minute, day).timestamp()


def minutes(delta):
    return delta.total_seconds() / 60


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, dev_id)
        self.group = group
        self.online = True

    def apply(self, state):
        pass


class Planning(unittest.TestCase):
    def test_times_stay_within_25_minutes_of_the_settings(self):
        for k in range(60):
            day = D + datetime.timedelta(days=k)
            p = plan_for(day, SETTINGS, "", IDS)
            self.assertLessEqual(abs(minutes(p.on - datetime.datetime.combine(day, datetime.time(18)))), SPREAD)
            self.assertLessEqual(abs(minutes(p.off - datetime.datetime.combine(day, datetime.time(23, 30)))), SPREAD)

    def test_the_same_day_gives_the_same_plan(self):
        a, b = plan_for(D, SETTINGS, "", IDS), plan_for(D, SETTINGS, "", list(reversed(IDS)))
        self.assertEqual(a, b)

    def test_other_days_differ(self):
        plans = {(p.on, p.off - datetime.datetime.combine(p.day, datetime.time())) for p in
                 (plan_for(D + datetime.timedelta(days=k), SETTINGS, "", IDS) for k in range(20))}
        self.assertGreater(len({(on.time(), off) for on, off in plans}), 10)
        # (and the times really move, not just the changes)
        starts = {plan_for(D + datetime.timedelta(days=k), SETTINGS, "", IDS).on.time() for k in range(20)}
        self.assertGreater(len(starts), 5)

    def test_around_sunset_follows_the_sun_not_the_time(self):
        s = {**SETTINGS, "when": "sunset"}
        for sunset in ("16:31", "21:40"):
            p = plan_for(D, s, sunset, IDS)
            want = datetime.datetime.combine(D, datetime.time(int(sunset[:2]), int(sunset[3:])))
            self.assertLessEqual(abs(minutes(p.on - want)), SPREAD)

    def test_no_sunset_falls_back_to_the_time(self):
        p = plan_for(D, {**SETTINGS, "when": "sunset"}, "", IDS)
        self.assertLessEqual(abs(minutes(p.on - at(18))), SPREAD)

    def test_a_late_sunset_still_gives_an_evening(self):
        p = plan_for(D, {**SETTINGS, "when": "sunset"}, "23:50", IDS)      # (the off time is before it)
        self.assertGreater(minutes(p.off - p.on), 30)
        self.assertLess(minutes(p.off - p.on), 4 * 60)

    def test_an_off_time_after_midnight_is_the_next_day(self):
        p = plan_for(D, {**SETTINGS, "off": "00:30"}, "", IDS)
        self.assertEqual(p.off.date(), D + datetime.timedelta(days=1))
        self.assertLessEqual(abs(minutes(p.off - at(0, 30, day=29))), SPREAD)
        self.assertGreater(p.off, p.on)

    def test_changes_are_few_and_spaced(self):
        for k in range(80):
            p = plan_for(D + datetime.timedelta(days=k), SETTINGS, "", IDS)
            self.assertLessEqual(len(p.changes), MAX_CHANGES)
            times = [p.on] + [t for t, _, _ in p.changes]
            for early, late in zip(times, times[1:]):
                self.assertGreaterEqual(minutes(late - early), 20)      # (a moment that changes nothing can lie between)
            for t, _, _ in p.changes:
                self.assertTrue(p.on < t < p.off)
        self.assertTrue(any(plan_for(D + datetime.timedelta(days=k), SETTINGS, "", IDS).changes for k in range(30)))

    def test_a_light_never_changes_to_what_it_already_is_and_one_stays_on(self):
        for k in range(80):
            p = plan_for(D + datetime.timedelta(days=k), SETTINGS, "", IDS)
            lit = set(p.start)
            self.assertGreaterEqual(len(lit), 2)
            for _, dev_id, on in p.changes:
                self.assertEqual(dev_id in lit, not on)
                (lit.add if on else lit.discard)(dev_id)
                self.assertGreaterEqual(len(lit), 1)

    def test_one_light_has_no_changes(self):
        p = plan_for(D, SETTINGS, "", ["a"])
        self.assertEqual((p.start, p.changes), (["a"], []))

    def test_lit_is_nothing_outside_the_evening(self):
        p = plan_for(D, SETTINGS, "", IDS)
        self.assertEqual(p.lit(p.on - datetime.timedelta(minutes=1)), set())
        self.assertTrue(p.lit(p.on))
        self.assertEqual(p.lit(p.off), set())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in (("lamp", "room"), ("strip", "room"), ("shelf", "room"), ("case", "pc")):
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": True, "mode": "effect", "effect": "rainbow", "color": "#123456", "brightness": 40})
        self.m.sun_times = lambda day: ("08:00", "17:00")
        self.p = self.m.presence
        alive = type("Alive", (), {"is_alive": lambda self: True})()   # steps by hand here
        for t in (self.p, self.m.cycle, self.m.focus, self.m.sleep, self.m.wake):
            t._thread = alive

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def on(self, when="time", at_="18:00", off="23:30", lights="room", groups=(), now=None):
        self.p.configure(True, lights, list(groups), when, at_, off, now=now)

    def lit(self):
        return {i for i, st in self.m.states.items() if st.on}

    def today(self):
        return self.p.plan(D)

    def state(self, dev_id):
        return self.m.states[dev_id]


class Running(Base):
    def test_before_the_evening_the_room_is_dark_and_the_pc_stays(self):
        self.on(now=ts(12))
        self.assertEqual(self.lit(), {"case"})
        self.assertEqual(self.state("case").mode, "effect")         # (the PC's lights as they were)

    def test_lights_come_on_in_warm_white_without_effects(self):
        self.on(now=ts(12))
        p = self.today()
        self.assertTrue(self.p.tick(p.on.timestamp()))
        for i in p.start:
            st = self.state(i)
            self.assertEqual((st.on, st.mode, st.color, st.brightness), (True, "color", WARM, BRIGHTNESS))
        self.assertEqual(self.lit(), set(p.start) | {"case"})

    def test_only_the_chosen_lights_are_touched(self):
        self.m.create_group("Desk", ["lamp", "strip"])
        self.on(lights="groups", groups=["Desk"], now=ts(12))
        self.assertEqual(self.p.lights(), ["lamp", "strip"])
        p = self.today()
        self.p.tick(p.on.timestamp())
        self.assertEqual(self.state("shelf").mode, "effect")        # (not in the group: as it was)
        self.assertTrue(self.state("shelf").on)
        self.assertEqual(self.state("case").mode, "effect")
        self.assertEqual({i for i in self.lit() if i in ("lamp", "strip")}, set(p.start))

    def test_a_group_with_a_pc_light_leaves_that_one_alone(self):
        self.m.create_group("Mixed", ["lamp", "case"])
        self.on(lights="groups", groups=["Mixed"], now=ts(12))
        self.assertEqual(self.p.lights(), ["lamp"])

    def test_it_follows_the_plan_through_the_evening_and_goes_off(self):
        self.on(now=ts(12))
        p = self.today()
        seen = []
        t = p.on
        while t < p.off + datetime.timedelta(minutes=5):
            self.p.tick(t.timestamp())
            room = {i for i in self.lit() if i != "case"}
            self.assertEqual(room, p.lit(t), t)
            seen.append(len(room))
            t += datetime.timedelta(minutes=5)
        self.assertEqual({i for i in self.lit() if i != "case"}, set())
        self.assertEqual(self.lit(), {"case"})

    def test_it_acts_at_the_moments_of_change_only(self):
        self.on(now=ts(12))
        p = self.today()
        self.assertTrue(self.p.tick(p.on.timestamp()))
        self.assertFalse(self.p.tick(p.on.timestamp() + 60))       # (nothing to change: no send)
        self.assertFalse(self.p.tick(p.on.timestamp() + 120))

    def test_a_change_by_hand_is_not_fought(self):
        self.on(now=ts(12))
        p = self.today()
        self.p.tick(p.on.timestamp())
        i = p.start[0]
        self.m.update([i], {"color": "#ff0000"})
        self.p.tick(p.on.timestamp() + 600)
        self.assertEqual(self.state(i).color, "#ff0000")

    def test_after_midnight_the_evening_goes_on(self):
        self.on(off="00:30", now=ts(12))
        p = self.today()
        self.assertEqual(p.off.date(), datetime.date(2026, 11, 29))
        self.p.tick(p.on.timestamp())
        self.p.tick(ts(23, 55))
        self.assertTrue({i for i in self.lit() if i != "case"})
        # past midnight it is still yesterday's evening, until it ends
        mid = p.off - datetime.timedelta(minutes=1)
        self.p.tick(mid.timestamp())
        self.assertEqual({i for i in self.lit() if i != "case"}, p.lit(mid))
        self.assertTrue(p.lit(mid))
        self.p.tick(p.off.timestamp())
        self.assertEqual({i for i in self.lit() if i != "case"}, set())

    def test_the_next_evening_has_its_own_times(self):
        self.on(now=ts(12))
        a, b = self.p.plan(D), self.p.plan(D + datetime.timedelta(days=1))
        self.assertNotEqual((a.on.time(), a.off.time()), (b.on.time(), b.off.time()))
        self.p.tick(ts(23, 59))
        self.p.tick(ts(18, 0, day=29) + 1800)
        self.assertTrue({i for i in self.lit() if i != "case"})

    def test_a_restart_in_the_evening_carries_on_with_the_same_plan(self):
        self.on(now=ts(12))
        p = self.today()
        mid = p.on + datetime.timedelta(minutes=(p.off - p.on).total_seconds() / 120)
        self.p.tick(mid.timestamp())
        want = p.lit(mid)
        # (a new run: nothing remembered, the lights as they were saved: off)
        self.tearDown()
        self.setUp()
        self.config.data["settings"]["presence"] = {**SETTINGS, "on": True}
        self.p.tick(mid.timestamp())
        self.assertEqual({i for i in self.lit() if i != "case"}, want)
        self.assertEqual(self.today(), p)

    def test_off_in_the_settings_does_nothing(self):
        self.assertFalse(self.p.tick(ts(20)))
        self.assertEqual(self.state("lamp").mode, "effect")

    def test_lights_you_locked_away_are_given_the_plan_again_when_you_are_back(self):
        self.on(now=ts(12))
        p = self.today()
        self.p.tick(p.on.timestamp())
        self.m.away._off = {}                    # (the lights are off because the PC is locked)
        self.assertTrue(self.m.away.holding)
        self.assertFalse(self.p.tick(p.on.timestamp() + 60))
        self.m.away._off = None
        self.assertFalse(self.m.away.holding)
        self.assertTrue(self.p.tick(p.on.timestamp() + 120))

    def test_describe_says_the_plan(self):
        self.on(now=ts(12))
        p = self.today()
        d = self.p.describe(ts(12))
        self.assertEqual((d["on"], d["plan"]["on"], d["plan"]["off"], d["plan"]["day"]),
                         (True, p.on.strftime("%H:%M"), p.off.strftime("%H:%M"), "tonight"))
        late = self.p.describe(ts(23, 59))
        self.assertEqual(late["plan"]["day"], "tomorrow")


class WithTheRest(Base):
    def test_a_sunset_start_uses_the_sun(self):
        self.on(when="sunset", now=ts(12))
        self.assertLessEqual(abs(minutes(self.today().on - at(17))), SPREAD)

    def test_schedules_are_skipped_but_marked_done(self):
        self.config.data["schedules"] = [{"time": "20:00", "days": list(range(7)), "action": "off", "enabled": True}]
        self.on(now=ts(12))
        p = self.today()
        self.p.tick(p.on.timestamp())
        before = {i: st.on for i, st in self.m.states.items()}
        fired = {}
        self.m.run_schedules(at(20, 0), fired)
        self.assertEqual({i: st.on for i, st in self.m.states.items()}, before)
        self.assertTrue(fired)

    def test_schedules_run_again_when_it_is_off(self):
        self.config.data["schedules"] = [{"time": "20:00", "days": list(range(7)), "action": "off", "enabled": True}]
        self.m.run_schedules(at(20, 0), {})
        self.assertEqual(self.lit(), set())

    def test_presets_in_turn_and_the_focus_timer_stop_when_it_starts(self):
        self.m.save_preset("One")
        self.m.save_preset("Two")
        self.m.cycle.start(["One", "Two"], 5)
        self.assertTrue(self.m.cycle.active)
        self.on(now=ts(12))
        self.assertFalse(self.m.cycle.active)

    def test_stop_puts_the_lights_back(self):
        before = self.m.looks()
        preset = self.config["settings"]["active_preset"]
        self.on(now=ts(12))
        p = self.today()
        self.p.tick(p.on.timestamp())
        self.assertNotEqual(self.m.looks(), before)
        self.p.stop_now()
        self.assertEqual(self.m.looks(), before)
        self.assertEqual(self.config["settings"]["active_preset"], preset)
        self.assertFalse(self.p.active)
        self.assertIsNone(self.config["settings"]["presence_before"])

    def test_switching_it_off_in_setup_puts_the_lights_back_too(self):
        before = self.m.looks()
        self.on(now=ts(12))
        self.p.configure(False, "room", [], "time", "18:00", "23:30")
        self.assertEqual(self.m.looks(), before)

    def test_what_it_did_is_not_saved_with_the_lights(self):
        saved = {i: dict(s) for i, s in self.config["states"].items()}
        self.on(now=ts(12))
        self.p.tick(self.today().on.timestamp())
        self.assertEqual({i: self.config["states"][i] for i in saved}, saved)


class AutomaticLooks(unittest.TestCase):
    """A game in full screen starts its look, unless Someone's home mode is on."""

    def setUp(self):
        self.m = FakeManager()
        self.m.config.save_soon = lambda: None
        self.m._changed = lambda: None
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.a = Automations(self.m)

    def game(self, seconds=8):
        for k in range(seconds):
            self.a.step(1000.0 + k, self.a.rules(), "game", 0.0)

    def test_a_game_changes_the_lights_normally(self):
        self.game()
        self.assertEqual(self.a.active, "game")

    def test_but_not_while_someones_home_mode_is_on(self):
        self.m.presence.active = True
        before = self.m.looks()
        self.game()
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.looks(), before)

    def test_and_a_look_that_runs_ends_when_it_is_switched_on(self):
        self.game()
        self.assertEqual(self.a.active, "game")
        self.m.presence.active = True
        self.a.step(2000.0, self.a.rules(), "game", 0.0)
        self.assertIsNone(self.a.active)


class Checks(Base):
    def test_bad_times(self):
        for bad in ("7pm", "", "24:00"):
            with self.assertRaises(ValueError):
                self.p.configure(True, "room", [], "time", bad, "23:30")
            with self.assertRaises(ValueError):
                self.p.configure(True, "room", [], "time", "18:00", bad)

    def test_bad_choices(self):
        with self.assertRaises(ValueError):
            self.p.configure(True, "everything", [], "time", "18:00", "23:30")
        with self.assertRaises(ValueError):
            self.p.configure(True, "room", [], "tomorrow", "18:00", "23:30")

    def test_groups_need_to_exist_and_be_chosen(self):
        with self.assertRaises(ValueError):
            self.p.configure(True, "groups", [], "time", "18:00", "23:30")
        with self.assertRaises(ValueError):
            self.p.configure(True, "groups", ["Nope"], "time", "18:00", "23:30")
        self.assertFalse(self.p.active)

    def test_a_setting_edited_by_hand_falls_back(self):
        self.config.data["settings"]["presence"] = {"on": True, "lights": 5, "groups": "x", "when": "never", "at": "soon", "off": None}
        s = self.p.settings
        self.assertEqual((s["lights"], s["groups"], s["when"], s["at"], s["off"]), ("room", [], "sunset", "17:30", "23:30"))

    def test_a_deleted_group_is_skipped(self):
        self.m.create_group("Desk", ["lamp"])
        self.on(lights="groups", groups=["Desk"], now=ts(12))
        self.m.delete_group("Desk")
        self.assertEqual(self.p.lights(), [])


if __name__ == "__main__":
    unittest.main()
