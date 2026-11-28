import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import MAX_GROUPS, DeviceManager
from glowstage.remote import Remote
from glowstage.tray import Tray


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, f"Name {dev_id}")
        self.group = group
        self.online = True

    def apply(self, state):
        pass


class Base(unittest.TestCase):
    """Four lights: two in the PC, two in the room, all on at 60."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in (("desk", "pc"), ("case", "pc"), ("lamp", "room"), ("strip", "room")):
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": True, "brightness": 60, "mode": "color", "color": "#123456"})

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def look(self, i):
        st = self.m.states[i]
        return st.on, st.mode, st.color, st.brightness


class Rules(Base):
    def test_create_keeps_order_and_returns_the_name(self):
        self.assertEqual(self.m.create_group("  Desk ", ["lamp", "desk"]), "Desk")
        self.m.create_group("Bed", ["strip"])
        self.assertEqual([g["name"] for g in self.m.my_groups()], ["Desk", "Bed"])
        # (the lights come in the order Glowstage lists them, whatever order they were ticked in)
        self.assertEqual(self.m.my_groups()[0]["ids"], ["desk", "lamp"])
        self.assertEqual(self.config.data["groups"][1], {"name": "Bed", "ids": ["strip"]})

    def test_name_rules(self):
        for bad, why in (("", "name"), ("   ", "name"), ("x" * 25, "24"), (None, "name"), (7, "name")):
            with self.assertRaises(ValueError, msg=repr(bad)) as e:
                self.m.create_group(bad, ["desk"])
            self.assertIn(why, str(e.exception))
        self.m.create_group("x" * 24, ["desk"])             # (24 is fine)
        self.m.create_group("Desk", ["desk"])
        for same in ("Desk", "desk", "DESK", " dEsK "):
            with self.assertRaises(ValueError) as e:
                self.m.create_group(same, ["lamp"])
            self.assertIn("already a group", str(e.exception))
        self.assertEqual(len(self.m.my_groups()), 2)

    def test_at_most_twelve(self):
        for n in range(MAX_GROUPS):
            self.m.create_group(f"Group {n}", ["desk"])
        with self.assertRaises(ValueError) as e:
            self.m.create_group("One more", ["desk"])
        self.assertIn("12 groups", str(e.exception))
        self.assertEqual(len(self.m.my_groups()), 12)

    def test_the_lights_must_be_real_and_not_none(self):
        for bad in ([], None, "desk", ["nope"], ["desk", "nope"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.m.create_group("Desk", bad)
        self.assertEqual(self.m.my_groups(), [])

    def test_rename(self):
        self.m.create_group("Desk", ["desk"])
        self.m.create_group("Bed", ["lamp"])
        self.m.rename_group("desk", "Office")                 # (the old name in any case)
        self.assertEqual([g["name"] for g in self.m.my_groups()], ["Office", "Bed"])
        self.m.rename_group("Office", "OFFICE")               # (only the case changes: fine)
        self.assertEqual(self.m.my_groups()[0]["name"], "OFFICE")
        with self.assertRaises(ValueError):
            self.m.rename_group("OFFICE", "bed")             # (another group has it)
        with self.assertRaises(ValueError):
            self.m.rename_group("OFFICE", " ")
        with self.assertRaises(ValueError):
            self.m.rename_group("Nobody", "Fine")
        self.assertEqual([g["name"] for g in self.m.my_groups()], ["OFFICE", "Bed"])

    def test_delete_leaves_the_lights_alone(self):
        self.m.create_group("Desk", ["desk", "case"])
        before = self.look("desk")
        self.m.delete_group("DESK")
        self.assertEqual(self.m.my_groups(), [])
        self.assertEqual(self.look("desk"), before)
        with self.assertRaises(ValueError):
            self.m.delete_group("Desk")

    def test_set_the_lights_of_a_group(self):
        self.m.create_group("Desk", ["desk"])
        self.m.set_group_lights("desk", ["case", "lamp"])
        self.assertEqual(self.m.my_groups()[0]["ids"], ["case", "lamp"])
        with self.assertRaises(ValueError):
            self.m.set_group_lights("Desk", [])
        with self.assertRaises(ValueError):
            self.m.set_group_lights("Nobody", ["desk"])
        self.assertEqual(self.m.my_groups()[0]["ids"], ["case", "lamp"])

    def test_move(self):
        for n in ("A", "B", "C"):
            self.m.create_group(n, ["desk"])
        names = lambda: [g["name"] for g in self.m.my_groups()]
        self.m.move_group("c", -1)
        self.assertEqual(names(), ["A", "C", "B"])
        self.m.move_group("A", 1)
        self.assertEqual(names(), ["C", "A", "B"])
        self.m.move_group("C", -1)                           # (already first: stays)
        self.m.move_group("B", 1)                            # (already last: stays)
        self.assertEqual(names(), ["C", "A", "B"])

    def test_it_is_saved_and_read_back(self):
        self.m.create_group("Desk", ["desk", "lamp"])
        self.config.save_now()
        again = Config(Path(self.tmp.name) / "config.json")
        self.assertEqual(again.data["groups"], [{"name": "Desk", "ids": ["desk", "lamp"]}])

    def test_a_hand_edited_file_with_junk_is_survived(self):
        self.config.data["groups"] = [{"name": "Good", "ids": ["desk", 5, "desk"]}, "junk", {"name": "", "ids": []},
                                      {"name": "good", "ids": ["lamp"]}, {"name": "NoIds"}, {"ids": ["x"]}]
        self.assertEqual(self.m.my_groups(), [{"name": "Good", "ids": ["desk"]}])


class Membership(Base):
    def test_a_light_in_two_groups(self):
        self.m.create_group("Desk", ["desk", "lamp"])
        self.m.create_group("Evening", ["lamp", "strip"])
        self.m.set_group_power("Desk", False)
        self.assertEqual([self.m.states[i].on for i in ("desk", "case", "lamp", "strip")], [False, True, False, True])
        self.m.set_group_power("Evening", True)
        self.assertTrue(self.m.states["lamp"].on)
        # the shared light counts in both groups' numbers: switched on through Evening, it is on in Desk too
        info = {g["name"]: g for g in self.m.groups_info()}
        self.assertEqual((info["Desk"]["on"], info["Evening"]["on"]), (1, 2))

    def test_lights_that_are_gone_stay_and_come_back(self):
        self.m.create_group("Desk", ["desk", "lamp"])
        gone = self.m.devices.pop("lamp")
        self.m.states.pop("lamp")
        self.m.workers.pop("lamp")
        info = self.m.groups_info()[0]
        self.assertEqual((info["ids"], info["lights"], info["on"]), (["desk", "lamp"], 1, 1))
        self.assertEqual(self.m.group_lights("Desk"), ["desk"])
        self.m.set_group_power("Desk", False)                # (works on the one that is here)
        self.assertFalse(self.m.states["desk"].on)
        # changing the group's lights while a light is away keeps that light in it
        self.m.set_group_lights("Desk", ["desk", "case"])
        self.assertEqual(self.m.my_groups()[0]["ids"], ["desk", "case", "lamp"])
        self.m._register(gone)
        self.assertEqual(self.m.group_lights("Desk"), ["desk", "case", "lamp"])

    def test_a_group_whose_lights_are_all_gone_is_empty(self):
        self.m.create_group("Bed", ["lamp"])
        self.m.devices.pop("lamp")
        self.m.states.pop("lamp")
        self.m.workers.pop("lamp")
        info = self.m.groups_info()[0]
        self.assertEqual((info["lights"], info["on"], info["ids"]), (0, 0, ["lamp"]))
        with self.assertRaises(ValueError) as e:
            self.m.set_group_power("Bed", True)
        self.assertIn("no lights", str(e.exception))
        self.assertFalse(self.m.can_undo())                  # (a refused change keeps the last undo)

    def test_hidden_lights_are_left_alone(self):
        self.m.create_group("Desk", ["desk", "case"])
        self.m.set_hidden("case", True)
        self.m.set_group_power("Desk", False)
        self.assertEqual((self.m.states["desk"].on, self.m.states["case"].on), (False, True))
        self.m.set_group_lights("Desk", ["desk"])
        self.assertEqual(self.m.my_groups()[0]["ids"], ["desk", "case"])      # (hidden, so kept)

    def test_on_counts_only_lights_that_answer(self):
        self.m.create_group("Room", ["lamp", "strip"])
        self.m.devices["strip"].online = False
        info = self.m.groups_info()[0]
        self.assertEqual((info["lights"], info["on"]), (2, 1))

    def test_state_has_the_groups(self):
        self.m.create_group("Desk", ["desk", "lamp"])
        snap = self.m.snapshot()
        self.assertEqual(snap["groups"], [{"name": "Desk", "ids": ["desk", "lamp"], "lights": 2, "on": 2}])
        self.m.update(["desk"], {"on": False})
        self.assertEqual(self.m.snapshot()["groups"][0]["on"], 1)


class OneChange(Base):
    def setUp(self):
        super().setUp()
        self.m.create_group("Desk", ["desk", "lamp"])
        self.m._undo = None

    def test_power_is_one_change_with_undo(self):
        self.m.set_group_power("desk", False)               # (any case)
        self.assertEqual([self.m.states[i].on for i in ("desk", "case", "lamp", "strip")], [False, True, False, True])
        self.assertTrue(self.m.undo())
        self.assertTrue(all(self.m.states[i].on for i in ("desk", "case", "lamp", "strip")))

    def test_power_keeps_the_preset_showing(self):
        self.m.apply_preset("Warm white")
        self.m.set_group_power("Desk", False)
        self.assertEqual(self.config["settings"]["active_preset"], "Warm white")

    def test_brightness_only_for_lights_that_are_on(self):
        self.m.update(["lamp"], {"on": False})
        self.m._undo = None
        self.m.set_group_brightness("Desk", 20)
        self.assertEqual([self.m.states[i].brightness for i in ("desk", "case", "lamp", "strip")], [20, 60, 60, 60])
        self.assertTrue(self.m.undo())
        self.assertEqual(self.m.states["desk"].brightness, 60)

    def test_brightness_is_clamped_and_a_slider_is_not_remembered(self):
        self.m.set_group_brightness("Desk", 500, undo=False)
        self.assertEqual(self.m.states["desk"].brightness, 100)
        self.m.set_group_brightness("Desk", 0, undo=False)
        self.assertEqual(self.m.states["desk"].brightness, 1)
        self.assertFalse(self.m.can_undo())

    def test_nothing_on_changes_nothing_and_keeps_the_undo(self):
        self.m.set_group_power("Desk", False)
        self.assertTrue(self.m.can_undo())
        before = self.m._undo
        self.m.set_group_brightness("Desk", 30)
        self.assertIs(self.m._undo, before)

    def test_a_look_for_the_group_only(self):
        self.m.set_group_look("Desk", {"mode": "color", "color": "#ff8800", "on": True})
        self.assertEqual(self.look("desk")[1:3], ("color", "#ff8800"))
        self.assertEqual(self.look("lamp")[1:3], ("color", "#ff8800"))
        self.assertEqual(self.look("case")[1:3], ("color", "#123456"))
        self.m.set_group_look("Desk", {"mode": "effect", "effect": "rainbow"})
        self.assertEqual(self.m.states["desk"].mode, "effect")
        self.assertTrue(self.m.undo())
        self.assertEqual(self.look("desk")[1:3], ("color", "#ff8800"))
        self.assertTrue(self.m.undo())                         # (undo again redoes: the second look)
        self.assertEqual(self.m.states["desk"].mode, "effect")

    def test_unknown_group(self):
        for call in (lambda: self.m.set_group_power("Nope", True), lambda: self.m.set_group_brightness("Nope", 5),
                     lambda: self.m.set_group_look("Nope", {"mode": "color"})):
            with self.assertRaises(ValueError):
                call()
        self.assertFalse(self.m.can_undo())

    def test_any_on(self):
        self.assertTrue(self.m.custom_any_on("Desk"))
        self.m.update(["desk", "lamp"], {"on": False})
        self.assertFalse(self.m.custom_any_on("Desk"))


class PhoneBase(Base):
    def setUp(self):
        super().setUp()
        self.m.create_group("Desk", ["desk", "lamp"])
        self.m.create_group("Bed Room", ["strip"])
        self.m._undo = None
        self.r = Remote(self.m, self.config, host="127.0.0.1")
        self.r.set_on(True)
        self.base = self.r.url().split("/?")[0]
        self.key = self.r.settings["token"]

    def tearDown(self):
        self.r.stop()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(f"{self.base}{path}?k={self.key}", timeout=5) as r:
            return json.loads(r.read())

    def post(self, body):
        req = urllib.request.Request(f"{self.base}/api/do?k={self.key}",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def refused(self, body, code=400):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(body)
        self.assertEqual(e.exception.code, code)

    def on(self, *ids):
        return [self.m.states[i].on for i in ids]


class PhoneGroups(PhoneBase):
    def test_state_lists_them_after_pc_and_room(self):
        state = self.get("/api/state")
        self.assertEqual([(g["key"], g["name"]) for g in state["groups"]],
                         [("pc", "PC"), ("room", "Room"), ("g:Desk", "Desk"), ("g:Bed Room", "Bed Room")])
        self.assertEqual(state["groups"][2], {"key": "g:Desk", "name": "Desk", "on": True, "brightness": 60})

    def test_a_group_without_lights_has_no_row(self):
        self.m.devices.pop("strip")
        self.m.states.pop("strip")
        keys = [g["key"] for g in self.get("/api/state")["groups"]]
        self.assertNotIn("g:Bed Room", keys)

    def test_the_average_counts_lights_that_are_on(self):
        self.m.update(["lamp"], {"brightness": 100})
        self.m.update(["desk"], {"on": False})
        self.assertEqual(next(g for g in self.get("/api/state")["groups"] if g["key"] == "g:Desk")["brightness"], 100)

    def test_power_by_name_in_any_case_and_by_index(self):
        self.assertTrue(self.post({"do": "group", "group": "g:desk", "on": False})["ok"])
        self.assertEqual(self.on("desk", "case", "lamp", "strip"), [False, True, False, True])
        self.post({"do": "group", "group": 1, "on": False})              # (1 is the second of your groups)
        self.assertEqual(self.on("strip"), [False])
        self.post({"do": "group", "group": 0, "on": True})
        self.assertEqual(self.on("desk", "lamp"), [True, True])

    def test_pc_and_room_keys_still_work(self):
        self.post({"do": "group", "group": "pc", "on": False})
        self.assertEqual(self.on("desk", "case", "lamp"), [False, False, True])
        self.post({"do": "group_brightness", "group": "room", "value": 25})
        self.assertEqual(self.m.states["lamp"].brightness, 25)

    def test_brightness_only_for_lights_that_are_on(self):
        self.m.update(["lamp"], {"on": False})
        self.post({"do": "group_brightness", "group": "g:Desk", "value": 30})
        self.assertEqual([self.m.states[i].brightness for i in ("desk", "lamp", "case")], [30, 60, 60])
        self.post({"do": "group_brightness", "group": "g:Desk", "value": 500})
        self.assertEqual(self.m.states["desk"].brightness, 100)

    def test_bad_requests_change_nothing(self):
        for body in ({"do": "group", "group": "g:Nope", "on": False}, {"do": "group", "group": 7, "on": False},
                     {"do": "group", "group": -1, "on": False}, {"do": "group", "group": True, "on": False},
                     {"do": "group", "group": 1.0, "on": False}, {"do": "group", "group": ["g:Desk"], "on": False},
                     {"do": "group", "group": "Desk", "on": False}, {"do": "group", "group": "g:Desk", "on": "no"},
                     {"do": "group_brightness", "group": "g:Desk", "value": "high"},
                     {"do": "group_brightness", "group": "g:Nope", "value": 5}):
            self.refused(body)
        self.assertTrue(all(self.on("desk", "case", "lamp", "strip")))
        self.assertFalse(self.m.can_undo())

    def test_a_group_with_no_lights_is_refused(self):
        self.m.devices.pop("strip")
        self.m.states.pop("strip")
        self.refused({"do": "group", "group": "g:Bed Room", "on": False})
        self.assertFalse(self.m.can_undo())

    def test_power_can_be_undone_but_a_slider_is_not_remembered(self):
        self.post({"do": "group_brightness", "group": "g:Desk", "value": 20})
        self.assertFalse(self.m.can_undo())
        self.post({"do": "group", "group": "g:Desk", "on": False})
        self.assertTrue(self.m.undo())
        self.assertEqual(self.on("desk", "lamp"), [True, True])


class PhoneLinks(PhoneBase):
    def call(self, path, method="GET"):
        req = urllib.request.Request(f"{self.base}/do/{path}?k={self.key}", method=method,
                                     data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_on_off_toggle(self):
        self.assertEqual(self.call("group/Desk/off"), (200, {"ok": True, "action": "group/Desk/off"}))
        self.assertEqual(self.on("desk", "case", "lamp", "strip"), [False, True, False, True])
        self.assertEqual(self.call("group/Desk/on")[0], 200)
        self.assertEqual(self.on("desk", "lamp"), [True, True])
        self.call("group/Desk/toggle")
        self.assertEqual(self.on("desk", "lamp"), [False, False])
        self.call("group/Desk/toggle")
        self.assertEqual(self.on("desk", "lamp"), [True, True])

    def test_a_toggle_with_one_light_on_switches_off(self):
        self.m.update(["lamp"], {"on": False})
        self.call("group/Desk/toggle")
        self.assertEqual(self.on("desk", "lamp"), [False, False])

    def test_names_with_spaces_any_case_and_post(self):
        self.assertEqual(self.call("group/" + quote("bed room", safe="") + "/off", "POST"),
                         (200, {"ok": True, "action": "group/Bed Room/off"}))
        self.assertEqual(self.on("strip"), [False])
        self.assertEqual(self.call("group/Bed%20Room/on")[0], 200)
        self.assertEqual(self.call("group/DESK/off/")[0], 200)       # (a slash at the end is fine)

    def test_a_name_with_a_slash(self):
        self.m.create_group("In/Out", ["case"])
        self.assertEqual(self.call("group/" + quote("In/Out", safe="") + "/off")[0], 200)
        self.assertEqual(self.on("case"), [False])

    def test_brightness(self):
        self.assertEqual(self.call("group/Desk/brightness/25")[0], 200)
        self.assertEqual([self.m.states[i].brightness for i in ("desk", "lamp", "case")], [25, 25, 60])
        self.call("group/Desk/brightness/0")                          # (the lowest light, not off)
        self.assertEqual((self.m.states["desk"].brightness, self.m.states["desk"].on), (1, True))
        for bad in ("101", "abc", "-5", "5.5", "99999"):
            self.assertEqual(self.call("group/Desk/brightness/" + bad)[0], 400, bad)
        self.assertEqual(self.call("group/Desk/brightness")[0], 404)
        self.assertEqual(self.m.states["desk"].brightness, 1)

    def test_unknown_group_and_actions(self):
        self.assertEqual(self.call("group/Nope/on"), (404, {"ok": False, "error": "No group called Nope"}))
        self.assertEqual(self.call("group/Desk/dance")[0], 404)
        self.assertEqual(self.call("group/Desk")[0], 404)
        self.assertEqual(self.call("group")[0], 404)
        self.assertEqual(self.call("group/Desk/on/extra")[0], 404)
        self.assertTrue(all(self.on("desk", "case", "lamp", "strip")))

    def test_a_group_without_lights_says_so(self):
        self.m.devices.pop("strip")
        self.m.states.pop("strip")
        status, body = self.call("group/Bed%20Room/off")
        self.assertEqual((status, body["ok"]), (409, False))

    def test_undo_covers_a_link(self):
        self.call("group/Desk/off")
        self.assertEqual(self.call("undo")[0], 200)
        self.assertEqual(self.on("desk", "lamp"), [True, True])

    def test_other_links_still_work(self):
        self.assertEqual(self.call("off")[0], 200)
        self.assertEqual(self.call("preset/Warm%20white")[0], 200)

    def test_without_the_key_nothing(self):
        req = urllib.request.Request(f"{self.base}/do/group/Desk/off?k=wrong")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        self.assertTrue(self.on("desk")[0])


def tray_items(manager):
    tray = Tray.__new__(Tray)   # without starting the icon
    tray.app = SimpleNamespace(manager=manager)
    return list(tray._items())


class TrayGroups(Base):
    def lights_menu(self):
        item = next(i for i in tray_items(self.m) if getattr(i, "text", None) == "Lights")
        return item, {s.text: s for s in item.submenu.items}

    def test_each_group_has_on_and_off_after_pc_and_room(self):
        self.m.create_group("Desk", ["desk", "lamp"])
        self.m.create_group("Bed", ["strip"])
        _, menu = self.lights_menu()
        self.assertEqual(list(menu), ["PC lights on", "PC lights off", "Room lights on", "Room lights off",
                                      "Desk on", "Desk off", "Bed on", "Bed off"])

    def test_choosing_one_switches_that_group_with_undo(self):
        self.m.create_group("Desk", ["desk", "lamp"])
        _, menu = self.lights_menu()
        menu["Desk off"](None)
        self.assertEqual([self.m.states[i].on for i in ("desk", "case", "lamp", "strip")], [False, True, False, True])
        self.assertTrue(self.m.can_undo())
        menu["Desk on"](None)
        self.assertTrue(self.m.states["desk"].on)

    def test_a_group_with_no_lights_has_no_entry(self):
        self.m.create_group("Bed", ["strip"])
        self.m.devices.pop("strip")
        self.m.states.pop("strip")
        _, menu = self.lights_menu()
        self.assertNotIn("Bed on", menu)

    def test_the_menu_shows_without_pc_or_room_lights_when_a_group_exists(self):
        self.m.create_group("Bed", ["strip"])
        for i in ("desk", "case", "lamp", "strip"):
            self.m.set_hidden(i, True)
        self.assertFalse([i for i in tray_items(self.m) if getattr(i, "text", None) == "Lights"])

    def test_a_group_that_went_away_since_the_menu_was_built_does_nothing(self):
        self.m.create_group("Desk", ["desk"])
        _, menu = self.lights_menu()
        self.m.delete_group("Desk")
        menu["Desk off"](None)                      # (no error)
        self.assertTrue(self.m.states["desk"].on)

    def test_no_groups_leaves_the_menu_as_it_was(self):
        _, menu = self.lights_menu()
        self.assertEqual(list(menu), ["PC lights on", "PC lights off", "Room lights on", "Room lights off"])


class Diagnostics(Base):
    def test_the_report_lists_groups_and_sizes(self):
        from glowstage import diagnostics
        self.m.create_group("Desk", ["desk", "lamp"])
        self.m.create_group("Bed", ["strip"])
        self.m.devices.pop("strip")
        self.m.states.pop("strip")
        remote = SimpleNamespace(settings={"on": False})
        app = SimpleNamespace(manager=self.m, config=self.config, remote=remote, hotkeys=None)
        text = diagnostics.report(app)
        self.assertIn("Groups: Desk (2 of 2 lights there), Bed (0 of 1 lights there)", text)


if __name__ == "__main__":
    unittest.main()
