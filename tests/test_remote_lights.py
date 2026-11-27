import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.remote import Remote


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, f"Name {dev_id}")
        self.group = group
        self.online = True

    def apply(self, state):
        pass


class PhoneLights(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in (("desk", "pc"), ("case", "pc"), ("lamp", "room"), ("strip", "room")):
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": True, "brightness": 60})
        self.m.update(["case"], {"brightness": 80})
        self.r = Remote(self.m, self.config, host="127.0.0.1")
        self.r.set_on(True)
        self.base = self.r.url().split("/?")[0]
        self.key = self.r.settings["token"]

    def tearDown(self):
        self.r.stop()
        self.m._stop.set()
        self.tmp.cleanup()

    def get(self, path, key=None):
        url = f"{self.base}{path}?k={self.key if key is None else key}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())

    def post(self, body, key=None):
        req = urllib.request.Request(f"{self.base}/api/do?k={self.key if key is None else key}",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def refused(self, body, code=400):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(body)
        self.assertEqual(e.exception.code, code)

    def light(self, state, dev_id):
        return next(x for x in state["lights"] if x["id"] == dev_id)

    def group(self, state, key):
        return next(x for x in state["groups"] if x["key"] == key)

    def test_state_has_groups_and_lights(self):
        state = self.get("/api/state")
        self.assertEqual([g["key"] for g in state["groups"]], ["pc", "room"])
        self.assertEqual(self.group(state, "pc"), {"key": "pc", "name": "PC", "on": True, "brightness": 70})
        self.assertEqual(self.group(state, "room")["brightness"], 60)
        self.assertEqual({x["id"] for x in state["lights"]}, {"desk", "case", "lamp", "strip"})
        desk = self.light(state, "desk")
        self.assertEqual(set(desk), {"id", "name", "group", "on", "brightness", "color", "online"})
        self.assertEqual((desk["name"], desk["group"], desk["on"], desk["online"]), ("Name desk", "pc", True, True))
        self.assertEqual(self.light(state, "lamp")["group"], "room")

    def test_state_has_no_secrets(self):
        text = json.dumps(self.get("/api/state"))
        self.assertNotIn(self.key, text)
        self.assertNotIn("token", text)
        self.assertNotIn("127.0.0.1", text)

    def test_group_average_counts_only_lights_that_are_on(self):
        self.m.update(["case"], {"on": False})
        self.assertEqual(self.group(self.get("/api/state"), "pc")["brightness"], 60)
        self.m.update(["desk"], {"on": False})
        pc = self.group(self.get("/api/state"), "pc")
        self.assertEqual((pc["on"], pc["brightness"]), (False, 0))

    def test_a_group_without_lights_is_left_out(self):
        self.config.data["hidden"] = ["desk", "case"]
        self.assertEqual([g["key"] for g in self.get("/api/state")["groups"]], ["room"])

    def test_hidden_lights_are_left_out_and_refused(self):
        self.config.data["hidden"] = ["lamp"]
        self.assertNotIn("lamp", [x["id"] for x in self.get("/api/state")["lights"]])
        self.refused({"do": "light", "id": "lamp", "on": False})

    def test_an_offline_light_is_listed_but_cannot_be_changed(self):
        self.m.devices["lamp"].online = False
        state = self.get("/api/state")
        self.assertFalse(self.light(state, "lamp")["online"])
        self.refused({"do": "light", "id": "lamp", "on": False})
        self.refused({"do": "light_brightness", "id": "lamp", "value": 10})
        self.assertTrue(self.m.states["lamp"].on)

    def test_group_power_changes_only_that_group(self):
        state = self.post({"do": "group", "group": "pc", "on": False})["state"]
        self.assertFalse(self.group(state, "pc")["on"])
        self.assertTrue(self.group(state, "room")["on"])
        self.assertEqual([i for i, st in self.m.states.items() if st.on], ["lamp", "strip"])
        self.post({"do": "group", "group": "pc", "on": True})
        self.post({"do": "group", "group": "room", "on": False})
        self.assertEqual([i for i, st in self.m.states.items() if st.on], ["desk", "case"])

    def test_group_brightness_only_for_lights_that_are_on(self):
        self.m.update(["strip"], {"on": False, "brightness": 33})
        state = self.post({"do": "group_brightness", "group": "room", "value": 40})["state"]
        self.assertEqual(self.m.states["lamp"].brightness, 40)
        self.assertEqual(self.m.states["strip"].brightness, 33)       # (it's off: stays)
        self.assertEqual((self.m.states["desk"].brightness, self.m.states["case"].brightness), (60, 80))
        self.assertEqual(self.group(state, "room")["brightness"], 40)

    def test_group_brightness_is_clamped(self):
        self.post({"do": "group_brightness", "group": "pc", "value": 500})
        self.assertEqual(self.m.states["desk"].brightness, 100)
        self.post({"do": "group_brightness", "group": "pc", "value": -5})
        self.assertEqual(self.m.states["desk"].brightness, 1)

    def test_one_light(self):
        state = self.post({"do": "light", "id": "desk", "on": False})["state"]
        self.assertFalse(self.light(state, "desk")["on"])
        self.assertTrue(self.light(state, "case")["on"])
        state = self.post({"do": "light_brightness", "id": "lamp", "value": 70})["state"]
        self.assertEqual(self.light(state, "lamp")["brightness"], 70)
        self.assertEqual(self.light(state, "strip")["brightness"], 60)
        self.assertEqual(self.light(self.post({"do": "light_brightness", "id": "lamp", "value": 0})["state"], "lamp")["brightness"], 1)

    def test_bad_requests_are_refused_and_change_nothing(self):
        before = self.m.looks()
        for body in (
            {"do": "group", "group": "garage", "on": True}, {"do": "group", "on": True},
            {"do": "group", "group": ["pc"], "on": True},
            {"do": "group", "group": "pc", "on": "yes"}, {"do": "group", "group": "pc", "on": 1},
            {"do": "group", "group": "pc"},
            {"do": "group_brightness", "group": "nope", "value": 10},
            {"do": "group_brightness", "group": "pc", "value": "high"},
            {"do": "group_brightness", "group": "pc", "value": None},
            {"do": "group_brightness", "group": "pc", "value": True},
            {"do": "light", "id": "nope", "on": True}, {"do": "light", "on": True},
            {"do": "light", "id": ["desk"], "on": True},
            {"do": "light", "id": "desk", "on": "no"}, {"do": "light", "id": "desk"},
            {"do": "light_brightness", "id": "nope", "value": 10},
            {"do": "light_brightness", "id": "desk", "value": "10"},
            {"do": "light_brightness", "id": "desk"},
        ):
            with self.subTest(body=body):
                self.refused(body)
        self.assertEqual(self.m.looks(), before)

    def test_without_the_key_nothing(self):
        for body in ({"do": "group", "group": "pc", "on": False}, {"do": "light", "id": "desk", "on": False},
                     {"do": "group_brightness", "group": "pc", "value": 5},
                     {"do": "light_brightness", "id": "desk", "value": 5}):
            for key in ("", "wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    self.post(body, key)
                self.assertEqual(e.exception.code, 403)
        self.assertTrue(all(st.on for st in self.m.states.values()))
        self.assertEqual(self.m.states["desk"].brightness, 60)

    def test_group_and_light_power_can_be_undone(self):
        self.post({"do": "group", "group": "pc", "on": False})
        self.assertFalse(self.m.states["desk"].on)
        self.assertTrue(self.m.undo())
        self.assertTrue(self.m.states["desk"].on and self.m.states["case"].on)
        self.post({"do": "light", "id": "lamp", "on": False})
        self.assertTrue(self.m.undo())
        self.assertTrue(self.m.states["lamp"].on)

    def test_sliders_are_not_remembered(self):
        self.m.remember()
        self.post({"do": "group_brightness", "group": "pc", "value": 20})
        self.post({"do": "light_brightness", "id": "lamp", "value": 20})
        self.m.undo()                       # (back to how it was when remembered: the sliders did not replace it)
        self.assertEqual(self.m.states["desk"].brightness, 60)
        self.assertEqual(self.m.states["lamp"].brightness, 60)


if __name__ == "__main__":
    unittest.main()
