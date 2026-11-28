"""Hotkeys you choose yourself (hotkeys.py): reading and writing combinations, what is refused,
presets that keep their key, what each key does and the registration. Nothing here ever registers
a real hotkey: the two calls into Windows are replaced."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from glowstage import diagnostics, hotkeys, wallpaper
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.hotkeys import (MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT, MOD_WIN, Hotkeys,
                               format_combo, parse_combo)
from glowstage.manager import DeviceManager

THREE = MOD_CONTROL | MOD_ALT | MOD_SHIFT
OLD = {"lights": "Ctrl+Alt+Shift+L", "show": "Ctrl+Alt+Shift+S", "brighter": "Ctrl+Alt+Shift+Up",
       "darker": "Ctrl+Alt+Shift+Down", "sleep": "Ctrl+Alt+Shift+N", "undo": "Ctrl+Alt+Shift+Z",
       **{f"preset_{n}": f"Ctrl+Alt+Shift+{n}" for n in range(1, 10)}}


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, dev_id)
        self.online = True
        self.group = group

    def apply(self, state):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.m._register(FakeLight("tower", "pc"))
        self.m._register(FakeLight("lamp", "room"))
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 50})
        self.hk = Hotkeys(self.m)
        self.registered, self.refused = {}, set()           # what "Windows" has: id -> (modifiers, key)
        self.hk._api_register = self._register
        self.hk._api_unregister = lambda ident: self.registered.pop(ident, None)

    def _register(self, ident, mods, vk):
        if (mods & ~MOD_NOREPEAT, vk) in self.refused:      # (another program has that one)
            return False
        self.registered[ident] = (mods, vk)
        return True

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def names(self):
        return self.m.preset_names()

    def active(self):
        return self.m.config["settings"].get("active_preset", "")


class Combinations(unittest.TestCase):
    def test_reading_and_writing(self):
        self.assertEqual(parse_combo("Ctrl+Alt+Shift+L"), (THREE, ord("L")))
        self.assertEqual(parse_combo(" shift + ALT + control + l "), (THREE, ord("L")))
        self.assertEqual(format_combo(MOD_WIN | MOD_SHIFT | MOD_CONTROL, 0x70), "Ctrl+Shift+Win+F1")
        self.assertEqual(parse_combo("Ctrl+Windows+PgUp"), (MOD_CONTROL | MOD_WIN, 0x21))
        for text in ("Ctrl+Alt+7", "Alt+F24", "Ctrl+Home", "Ctrl+End", "Ctrl+PageDown", "Alt+Insert",
                     "Ctrl+Left", "Ctrl+Right", "Win+Num5", "Ctrl+Num*", "Ctrl+Num+", "Ctrl+Num-",
                     "Ctrl+Num.", "Ctrl+Num/", "Ctrl+Delete", "Ctrl+Space"):
            mods, vk = parse_combo(text)
            self.assertEqual(format_combo(mods, vk), text)
            self.assertEqual(parse_combo(format_combo(mods, vk)), (mods, vk), text)

    def test_virtual_key_codes(self):
        for text, vk in (("Ctrl+B", 0x42), ("Ctrl+0", 0x30), ("Ctrl+F1", 0x70), ("Ctrl+F12", 0x7B),
                         ("F13", 0x7C), ("F24", 0x87), ("Ctrl+Up", 0x26), ("Ctrl+Down", 0x28),
                         ("Ctrl+Num0", 0x60), ("Ctrl+Num9", 0x69), ("Ctrl+Num+", 0x6B),
                         ("Ctrl+Num-", 0x6D), ("Ctrl+Delete", 0x2E), ("Ctrl+Home", 0x24)):
            self.assertEqual(parse_combo(text)[1], vk, text)

    def test_a_modifier_is_needed_except_for_f13_to_f24(self):
        for text in ("L", "F1", "F12", "Home", "5", "Num3"):
            with self.assertRaisesRegex(ValueError, "Add Ctrl, Alt, Shift or Windows", msg=text):
                parse_combo(text)
        for n in range(13, 25):
            self.assertEqual(parse_combo(f"F{n}"), (0, 0x6F + n))
        self.assertEqual(parse_combo("Ctrl+F13"), (MOD_CONTROL, 0x7C))

    def test_shift_alone_is_not_enough_for_typing_keys(self):
        for text in ("Shift+L", "Shift+5", "Shift+Space"):
            with self.assertRaisesRegex(ValueError, "capital letters", msg=text):
                parse_combo(text)
        parse_combo("Shift+F5")
        parse_combo("Shift+Home")

    def test_what_windows_and_every_program_use_is_refused(self):
        for text in ("Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+A", "Ctrl+S", "Ctrl+Y", "Alt+F4",
                     "Alt+Tab", "Ctrl+Alt+Delete", "Win+L", "Win+D", "Win+E", "Win+R", "Alt+Space"):
            with self.assertRaisesRegex(ValueError, "already used by Windows or nearly every program", msg=text):
                parse_combo(text)
        parse_combo("Ctrl+Shift+Z")             # (other combinations of those keys are fine)
        parse_combo("Ctrl+Alt+Shift+Z")
        parse_combo("Ctrl+Alt+C")

    def test_nonsense_is_refused_in_plain_words(self):
        for text, why in (("", "Press a key"), ("Ctrl+", "Add a key"), ("Ctrl+Alt", "Add a key"),
                          ("Ctrl+A+B", "just one key"), ("Ctrl+Escape", "can't use the key"),
                          ("Ctrl+;", "can't use the key")):
            with self.assertRaisesRegex(ValueError, why, msg=text):
                parse_combo(text)


class Defaults(Base):
    def test_the_defaults_are_the_keys_of_before(self):
        keys = self.hk.keys()
        self.assertEqual({a: k for a, k in keys.items() if k}, OLD)
        self.assertEqual(self.m.config["settings"]["hotkey_keys"], {})
        for action in ("pc_power", "room_power", "next_preset", "prev_preset", "focus", "stop_cycle", "wallpaper"):
            self.assertEqual(keys[action], "")

    def test_every_default_is_allowed_and_different(self):
        combos = [parse_combo(k) for k in hotkeys.DEFAULTS.values() if k]
        self.assertEqual(len(set(combos)), len(combos))

    def test_the_list_for_the_dialog(self):
        rows = {a["id"]: a for a in self.hk.actions()}
        self.assertEqual(rows["lights"]["key"], "Ctrl+Alt+Shift+L")
        self.assertEqual(rows["lights"]["default"], "Ctrl+Alt+Shift+L")
        self.assertEqual(rows["preset_2"]["detail"], self.names()[1])
        self.assertEqual(rows["wallpaper"]["key"], "")
        self.assertEqual(rows["undo"]["section"], "actions")
        self.assertEqual(rows["preset_1"]["section"], "positions")


class Choosing(Base):
    def test_set_change_and_clear(self):
        self.assertEqual(self.hk.set_key("lights", "ctrl+alt+l"), "Ctrl+Alt+L")
        self.assertEqual(self.hk.keys()["lights"], "Ctrl+Alt+L")
        self.assertEqual(self.m.config["settings"]["hotkey_keys"], {"lights": "Ctrl+Alt+L"})
        self.hk.clear_key("show")
        self.assertEqual(self.hk.keys()["show"], "")
        self.assertEqual(self.m.config["settings"]["hotkey_keys"]["show"], "")
        self.hk.set_key("focus", "F14")
        self.assertEqual(self.hk.keys()["focus"], "F14")

    def test_the_default_again_is_nothing_to_remember(self):
        self.hk.set_key("lights", "Ctrl+Alt+L")
        self.hk.set_key("lights", "Ctrl+Alt+Shift+L")
        self.assertEqual(self.m.config["settings"]["hotkey_keys"], {})

    def test_refused_keys_change_nothing(self):
        with self.assertRaises(ValueError):
            self.hk.set_key("lights", "Ctrl+C")
        with self.assertRaises(ValueError):
            self.hk.set_key("lights", "L")
        with self.assertRaisesRegex(ValueError, "no such action"):
            self.hk.set_key("nope", "Ctrl+Alt+L")
        self.assertEqual(self.m.config["settings"]["hotkey_keys"], {})

    def test_two_actions_can_not_share_a_key(self):
        with self.assertRaisesRegex(ValueError, "already the key for All lights on or off"):
            self.hk.set_key("focus", "Ctrl+Alt+Shift+L")
        with self.assertRaisesRegex(ValueError, "already the key for All lights"):
            self.hk.set_key("focus", "shift+control+alt+l")      # (the same key, written differently)
        self.hk.clear_key("lights")                              # free it, then it works
        self.hk.set_key("focus", "Ctrl+Alt+Shift+L")
        self.assertEqual(self.hk.keys()["focus"], "Ctrl+Alt+Shift+L")
        self.hk.set_key("focus", "Ctrl+Alt+Shift+L")             # (an action keeps its own key)

    def test_a_hand_edited_file_never_gives_two_actions_one_key(self):
        self.m.config["settings"]["hotkey_keys"] = {
            "focus": "Ctrl+Alt+Shift+L", "wallpaper": "Ctrl+C", "stop_cycle": "gibberish", "undo": 7}
        keys = self.hk.keys()
        self.assertEqual(keys["lights"], "Ctrl+Alt+Shift+L")
        self.assertEqual((keys["focus"], keys["wallpaper"], keys["stop_cycle"], keys["undo"]), ("", "", "", ""))

    def test_reset_all(self):
        first = self.names()[0]
        self.hk.set_key("lights", "Ctrl+Alt+L")
        self.hk.clear_key("undo")
        self.hk.add_preset(first)
        self.hk.set_key(hotkeys.named(first), "Ctrl+Alt+9")
        self.hk.reset()
        self.assertEqual({a: k for a, k in self.hk.keys().items() if k}, OLD)
        self.assertEqual(self.m.config["settings"]["hotkey_keys"], {})


class PresetsByName(Base):
    def setUp(self):
        super().setUp()
        self.p = self.names()[2]
        self.hk.add_preset(self.p)
        self.hk.set_key(hotkeys.named(self.p), "Ctrl+Alt+P")

    def test_a_key_for_a_preset(self):
        self.assertEqual(self.hk.keys()[hotkeys.named(self.p)], "Ctrl+Alt+P")
        rows = [a for a in self.hk.actions() if a["section"] == "named"]
        self.assertEqual([(r["label"], r["key"]) for r in rows], [(self.p, "Ctrl+Alt+P")])
        self.hk._pressed(hotkeys.named(self.p))
        self.assertEqual(self.active(), self.p)

    def test_only_a_preset_that_exists(self):
        with self.assertRaisesRegex(ValueError, "no such preset"):
            self.hk.add_preset("Nothing like it")
        with self.assertRaisesRegex(ValueError, "no such action"):
            self.hk.set_key(hotkeys.named("Nothing like it"), "Ctrl+Alt+9")

    def test_a_renamed_preset_keeps_its_key(self):
        self.m.rename_preset(self.p, "Brand new name")
        self.assertEqual(self.hk.keys()[hotkeys.named("Brand new name")], "Ctrl+Alt+P")
        self.assertNotIn(hotkeys.named(self.p), self.hk.keys())
        self.hk._pressed(hotkeys.named("Brand new name"))
        self.assertEqual(self.active(), "Brand new name")

    def test_a_deleted_preset_loses_its_key(self):
        self.m.delete_preset(self.p)
        self.assertNotIn(hotkeys.named(self.p), self.hk.keys())
        self.assertNotIn(hotkeys.named(self.p), self.m.config["settings"]["hotkey_keys"])
        self.hk._pressed(hotkeys.named(self.p))          # (nothing happens, nothing breaks)
        self.assertFalse([a for a in self.hk.actions() if a["section"] == "named"])

    def test_the_row_can_be_taken_away_again(self):
        self.hk.remove_preset(self.p)
        self.assertNotIn(hotkeys.named(self.p), self.hk.keys())

    def test_a_key_that_another_action_has_is_refused(self):
        other = self.names()[3]
        self.hk.add_preset(other)
        with self.assertRaisesRegex(ValueError, "already the key for"):
            self.hk.set_key(hotkeys.named(other), "Ctrl+Alt+P")


class Presets(Base):
    def test_next_and_previous_go_round(self):
        names = self.names()
        self.m.apply_preset(names[0])
        self.hk._pressed("next_preset")
        self.assertEqual(self.active(), names[1])
        self.hk._pressed("prev_preset")
        self.hk._pressed("prev_preset")
        self.assertEqual(self.active(), names[-1])       # (round the corner)
        self.hk._pressed("next_preset")
        self.assertEqual(self.active(), names[0])

    def test_with_none_on_next_is_the_first_and_previous_the_last(self):
        self.m.config["settings"]["active_preset"] = ""
        self.hk._pressed("next_preset")
        self.assertEqual(self.active(), self.names()[0])
        self.m.config["settings"]["active_preset"] = ""
        self.hk._pressed("prev_preset")
        self.assertEqual(self.active(), self.names()[-1])

    def test_it_follows_the_order_of_the_list(self):
        names = self.names()
        self.m.move_preset(names[0], 1)                  # the first two swap places
        self.m.apply_preset(names[1])
        self.hk._pressed("next_preset")
        self.assertEqual(self.active(), names[0])

    def test_undo_goes_back_after_a_step(self):
        self.m.apply_preset(self.names()[0])
        before = self.m.looks()
        self.hk._pressed("next_preset")
        self.assertNotEqual(self.m.looks(), before)
        self.hk._pressed("undo")
        self.assertEqual(self.m.looks(), before)

    def test_preset_by_position(self):
        self.hk._pressed("preset_3")
        self.assertEqual(self.active(), self.names()[2])
        self.hk._pressed("preset_9")                     # (there may be fewer than nine: nothing then)


class Actions(Base):
    def test_all_lights_on_and_off_with_undo(self):
        self.hk._pressed("lights")
        self.assertFalse(self.m.any_on())
        self.hk._pressed("undo")
        self.assertTrue(self.m.any_on())

    def test_the_pcs_lights_and_the_rooms_lights(self):
        def on():
            return self.m.states["tower"].on, self.m.states["lamp"].on
        self.hk._pressed("pc_power")
        self.assertEqual(on(), (False, True))
        self.hk._pressed("room_power")
        self.assertEqual(on(), (False, False))
        self.hk._pressed("pc_power")
        self.assertEqual(on(), (True, False))
        self.hk._pressed("undo")
        self.assertEqual(on(), (False, False))

    def test_the_show(self):
        self.hk._pressed("show")
        self.assertTrue(self.m.show_running())
        self.hk._pressed("show")
        self.assertFalse(self.m.show_running())

    def test_brighter_and_darker(self):
        self.hk._pressed("brighter")
        self.assertEqual(self.m.states["lamp"].brightness, 60)
        self.hk._pressed("darker")
        self.hk._pressed("darker")
        self.assertEqual(self.m.states["lamp"].brightness, 40)

    def test_the_sleep_timer(self):
        self.m.sleep._thread = type("Alive", (), {"is_alive": lambda self: True})()
        self.hk._pressed("sleep")
        self.assertTrue(self.m.sleep.active)
        self.hk._pressed("sleep")
        self.assertFalse(self.m.sleep.active)

    def test_the_focus_timer_starts_and_stops(self):
        with mock.patch.object(type(self.m.focus), "active", new_callable=mock.PropertyMock) as active, \
                mock.patch.object(self.m.focus, "start_last") as start, \
                mock.patch.object(self.m.focus, "cancel") as cancel:
            active.return_value = False
            self.hk._pressed("focus")
            start.assert_called_once()
            cancel.assert_not_called()
            active.return_value = True
            self.hk._pressed("focus")
            cancel.assert_called_once()
            start.assert_called_once()

    def test_the_focus_timer_without_lights_is_quiet(self):
        with mock.patch.object(self.m.focus, "start_last", side_effect=ValueError("no lights")):
            self.hk._pressed("focus")

    def test_stop_presets_in_turn(self):
        with mock.patch.object(self.m.cycle, "cancel") as cancel:
            self.hk._pressed("stop_cycle")
        cancel.assert_called_once()

    def test_pause_and_resume_the_automatic_looks(self):
        self.m.config.save_soon = lambda: None
        self.hk._pressed("pause_auto")
        self.assertGreater(self.m.automations.paused_until(), 0)
        self.hk._pressed("pause_auto")
        self.assertEqual(self.m.automations.paused_until(), 0)

    def test_colors_from_the_wallpaper(self):
        with mock.patch.object(wallpaper, "look", return_value=(["#ff0000", "#0000ff"], object())), \
                mock.patch.object(self.m, "light_from_colors", return_value=2) as put:
            self.hk._pressed("wallpaper")
        put.assert_called_once_with(["#ff0000", "#0000ff"])
        self.assertTrue(self.m.can_undo())

    def test_no_wallpaper_is_no_error(self):
        with mock.patch.object(wallpaper, "look", side_effect=ValueError("No wallpaper")):
            self.hk._pressed("wallpaper")
        self.assertFalse(self.m.can_undo())

    def test_a_failing_action_never_stops_the_keys(self):
        with mock.patch.object(self.m, "undo", side_effect=ZeroDivisionError), \
                self.assertLogs("glowstage.hotkeys", "ERROR"):
            self.hk._pressed("undo")


class Registering(Base):
    def setUp(self):
        super().setUp()
        self.hk._thread = object()          # (as if the hotkey thread ran, so sync() has someone to tell)
        self.posted = []

        def post(thread, msg, w, l):        # (the thread's side: do what the message asks)
            self.posted.append(msg)
            self.hk._sync_now()
            self.hk._synced.set()
        fake = type("Windll", (), {"user32": type("User32", (), {"PostThreadMessageW": staticmethod(post)})})
        patch = mock.patch.object(hotkeys.ctypes, "windll", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_defaults_repeat_only_for_brighter_and_darker(self):
        self.hk._sync_now()
        found = set(self.registered.values())
        self.assertEqual(len(self.registered), 15)
        self.assertIn((THREE | MOD_NOREPEAT, ord("L")), found)
        self.assertIn((THREE | MOD_NOREPEAT, ord("1")), found)
        self.assertIn((THREE, 0x26), found)
        self.assertIn((THREE, 0x28), found)
        self.assertEqual(sorted(v for m, v in found if not m & MOD_NOREPEAT), [0x26, 0x28])
        self.assertEqual(self.hk.taken, {})

    def test_a_changed_key_is_registered_again_without_the_rest(self):
        self.hk._sync_now()
        before = dict(self.registered)
        self.hk.set_key("lights", "Ctrl+Alt+L")
        self.assertIn(hotkeys.WM_SYNC, self.posted)
        self.assertNotIn((THREE | MOD_NOREPEAT, ord("L")), self.registered.values())
        self.assertIn((MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("L")), self.registered.values())
        kept = [i for i in before if self.registered.get(i) == before[i]]
        self.assertEqual(len(kept), 14)                  # (the other keys weren't touched)

    def test_a_cleared_key_is_unregistered(self):
        self.hk._sync_now()
        self.hk.clear_key("undo")
        self.assertEqual(len(self.registered), 14)
        self.assertNotIn(ord("Z"), [v for m, v in self.registered.values()])

    def test_a_new_action_and_a_preset_by_name(self):
        self.hk._sync_now()
        self.hk.set_key("wallpaper", "F20")
        self.hk.add_preset(self.names()[0])
        self.hk.set_key(hotkeys.named(self.names()[0]), "Ctrl+Alt+9")
        self.assertEqual(len(self.registered), 17)
        self.assertIn((MOD_NOREPEAT, 0x83), self.registered.values())

    def test_a_deleted_preset_unregisters_its_key(self):
        self.hk._sync_now()
        self.hk.add_preset(self.names()[0])
        self.hk.set_key(hotkeys.named(self.names()[0]), "Ctrl+Alt+9")
        self.assertEqual(len(self.registered), 16)
        self.m.delete_preset(self.names()[0])
        self.hk.sync()                                   # (App does that on every change)
        self.assertEqual(len(self.registered), 15)

    def test_a_key_another_program_has_is_reported_for_its_action(self):
        self.refused.add((THREE, ord("S")))
        self.hk._sync_now()
        self.assertEqual(self.hk.taken, {"show": "Ctrl+Alt+Shift+S"})
        self.assertEqual(self.hk.failed, ["Ctrl+Alt+Shift+S"])
        self.assertEqual(len(self.registered), 14)
        self.hk.set_key("show", "Ctrl+Alt+S")            # a key of its own: no longer reported
        self.assertEqual(self.hk.taken, {})

    def test_it_is_tried_again_when_the_other_program_lets_go(self):
        self.refused.add((THREE, ord("S")))
        self.hk._sync_now()
        self.refused.clear()
        self.hk._sync_now()
        self.assertEqual(self.hk.taken, {})
        self.assertEqual(len(self.registered), 15)

    def test_pause_and_resume(self):
        self.hk._sync_now()
        self.hk.pause()
        self.assertTrue(self.hk.paused)
        self.assertEqual(self.registered, {})
        self.hk.set_key("lights", "Ctrl+Alt+L")         # (changed while paused: still nothing registered)
        self.assertEqual(self.registered, {})
        self.hk.resume()
        self.assertFalse(self.hk.paused)
        self.assertEqual(len(self.registered), 15)
        self.assertIn((MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("L")), self.registered.values())
        self.assertIsNone(self.hk._deadman)

    def test_pause_keeps_what_was_known_about_other_programs(self):
        self.refused.add((THREE, ord("S")))
        self.hk._sync_now()
        self.hk.pause()
        self.assertEqual(self.hk.taken, {"show": "Ctrl+Alt+Shift+S"})
        self.hk.resume()
        self.assertEqual(self.hk.taken, {"show": "Ctrl+Alt+Shift+S"})

    def test_a_pause_that_is_never_ended_ends_by_itself(self):
        self.hk._sync_now()
        with mock.patch.object(hotkeys, "PAUSE_LIMIT", 0.05):
            self.hk.pause()
        self.hk._deadman.join(2)
        self.assertFalse(self.hk.paused)
        self.assertEqual(len(self.registered), 15)

    def test_nothing_is_posted_when_nothing_changed(self):
        self.hk._sync_now()
        self.posted.clear()
        self.hk.sync()
        self.assertEqual(self.posted, [])

    def test_while_hotkeys_are_off_nothing_is_registered(self):
        self.hk._thread = None
        self.hk.set_key("lights", "Ctrl+Alt+L")
        self.assertEqual((self.posted, self.registered), ([], {}))


class Report(Base):
    def test_the_problem_report_counts_the_keys(self):
        self.assertEqual(hotkeys.summary({"hotkeys": False}, self.names()), "off (15 keys set)")
        self.hk.clear_key("undo")
        self.hk.set_key("focus", "F13")
        s = dict(self.m.config["settings"], hotkeys=True)
        self.assertEqual(hotkeys.summary(s, self.names(), ["Show Ctrl+Alt+Shift+S"]),
                         "on (15 keys set; another program has Show Ctrl+Alt+Shift+S)")

    def test_it_is_in_the_report(self):
        class App:
            manager = self.m
            config = self.m.config
            remote = type("R", (), {"settings": {}})()
            hotkeys = self.hk
        self.m.openrgb.describe = lambda: {}
        self.hk.taken = {"show": "Ctrl+Alt+Shift+S"}
        self.assertIn("hotkeys off (15 keys set; another program has The light show on or off Ctrl+Alt+Shift+S)",
                      diagnostics.report(App()))


if __name__ == "__main__":
    unittest.main()
