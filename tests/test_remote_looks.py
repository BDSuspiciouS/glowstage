import http.client
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from glowstage import effects
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.remote import LOOKS, MAX_BODY, Remote


class FakeLight(Device):
    def __init__(self, dev_id, group="room"):
        super().__init__(dev_id, f"Name {dev_id}")
        self.group = group
        self.online = True

    def apply(self, state):
        pass


class PhoneLooks(unittest.TestCase):
    """Color, Looks and the focus timer on the phone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i, group in (("desk", "pc"), ("lamp", "room"), ("strip", "room")):
            self.m._register(FakeLight(i, group))
        self.m.update(None, {"on": True, "mode": "color", "color": "#123456", "brightness": 60})
        self.m.update(["strip"], {"on": False})
        self.config["settings"]["focus_last"] = {"work": 25, "rest": 5, "rounds": 4, "look": ""}   # (keep my lights)
        alive = type("Alive", (), {"is_alive": lambda self: True})()   # (the timer is stepped by hand: no thread)
        self.m.focus._thread = alive
        self.r = Remote(self.m, self.config, host="127.0.0.1")
        self.r.set_on(True)
        self.base = self.r.url().split("/?")[0]
        self.key = self.r.settings["token"]

    def tearDown(self):
        self.r.stop()
        self.m._stop.set()
        self.tmp.cleanup()

    def get(self, path, key=None):
        with urllib.request.urlopen(f"{self.base}{path}?k={self.key if key is None else key}", timeout=5) as r:
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
        return json.loads(e.exception.read())["error"]

    def link(self, path):
        try:
            with urllib.request.urlopen(f"{self.base}/do/{path}?k={self.key}", timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def looks(self):
        return {i: (st.on, st.mode, st.color, st.effect) for i, st in self.m.states.items()}

    # ---- color ----------------------------------------------------------------------------

    def test_a_color_goes_to_every_light_that_is_on(self):
        state = self.post({"do": "color", "color": "#FF8800"})["state"]
        for i in ("desk", "lamp"):
            st = self.m.states[i]
            self.assertEqual((st.mode, st.color, st.on), ("color", "#ff8800", True))
        self.assertEqual(self.m.states["strip"].color, "#123456")      # (off: stays as it was)
        self.assertFalse(self.m.states["strip"].on)
        self.assertEqual(state["current"], {"color": "#ff8800", "effect": ""})

    def test_a_color_replaces_a_look(self):
        self.post({"do": "effect", "effect": "fire"})
        self.post({"do": "color", "color": "#00c8ff"})
        self.assertEqual(self.m.states["lamp"].mode, "color")

    def test_bad_colors_are_refused_and_change_nothing(self):
        before = self.looks()
        for color in ("ff8800", "#ff88", "#ff88000", "#gg8800", "red", "", None, 5, ["#ff8800"], "#ff8800 ", "rgb(1,2,3)"):
            with self.subTest(color=color):
                self.assertEqual(self.refused({"do": "color", "color": color}), "A color looks like #ff8800")
        self.refused({"do": "color"})
        self.assertEqual(self.looks(), before)

    def test_a_color_needs_a_light_that_is_on(self):
        self.m.update(None, {"on": False})
        self.m.remember()
        mark = self.m._undo
        self.assertIn("Switch a light on", self.refused({"do": "color", "color": "#ff8800"}))
        self.assertIs(self.m._undo, mark)                # (a refused change keeps the last undo)

    def test_undo_after_a_phone_color(self):
        self.post({"do": "color", "color": "#ff0000"})
        self.assertEqual(self.m.states["desk"].color, "#ff0000")
        state = self.post({"do": "undo"})["state"]
        self.assertEqual((self.m.states["desk"].color, self.m.states["lamp"].color), ("#123456", "#123456"))
        self.assertEqual(state["current"]["color"], "#123456")

    def test_a_color_is_one_change(self):
        seen = []
        self.m.listeners.append(lambda: seen.append(1))
        self.post({"do": "color", "color": "#ff0000"})
        self.assertEqual(len(seen), 1)

    # ---- looks ----------------------------------------------------------------------------

    def test_the_looks_in_the_state(self):
        looks = self.get("/api/state")["looks"]
        self.assertEqual([x["id"] for x in looks], [i for i, _ in LOOKS])
        for x in looks:
            self.assertEqual(set(x), {"id", "name", "colors"})
            self.assertEqual(x["name"], effects.EFFECTS[x["id"]].name)
            self.assertTrue(x["colors"] and all(c.startswith("#") and len(c) == 7 for c in x["colors"]))

    def test_only_everyday_and_calm_looks_and_screen_sync_are_offered(self):
        ids = {i for i, _ in LOOKS}
        for i in ids:
            self.assertIn(i, effects.EFFECTS)
            self.assertEqual(effects.EFFECTS[i].group, "animation")
        self.assertFalse(ids & {"typing", "pc_load", "game_cs2", "game_lol", "music_show", "screen_sound", "spectrum"})
        for want in ("rainbow", "wave", "comet", "aurora", "fire", "candle", "ocean", "lava", "storm",
                     "gradient", "breathing", "twinkle", "daylight", "screen"):
            self.assertIn(want, ids)

    def test_a_look_goes_to_every_light_that_is_on_and_is_marked(self):
        state = self.post({"do": "effect", "effect": "aurora"})["state"]
        for i in ("desk", "lamp"):
            st = self.m.states[i]
            self.assertEqual((st.mode, st.effect), ("effect", "aurora"))
        self.assertFalse(self.m.states["strip"].on)
        self.assertEqual(state["current"], {"color": "", "effect": "aurora"})
        self.assertEqual(self.get("/api/state")["current"]["effect"], "aurora")

    def test_current_is_empty_when_the_lights_differ(self):
        self.m.update(["desk"], {"mode": "effect", "effect": "fire"})
        self.assertEqual(self.get("/api/state")["current"], {"color": "", "effect": ""})
        self.m.update(["desk"], {"mode": "color", "color": "#123456"})
        self.assertEqual(self.get("/api/state")["current"], {"color": "#123456", "effect": ""})
        self.m.update(None, {"on": False})
        self.assertEqual(self.get("/api/state")["current"], {"color": "", "effect": ""})

    def test_looks_that_do_not_belong_on_a_phone_are_refused(self):
        before = self.looks()
        for look in ("game_cs2", "game_lol", "music_show", "music_party", "typing", "screen_sound", "nope", "", None, 3, ["fire"], "FIRE"):
            with self.subTest(look=look):
                self.assertEqual(self.refused({"do": "effect", "effect": look}), "There's no such look")
        self.refused({"do": "effect"})
        self.assertEqual(self.looks(), before)

    def test_undo_after_a_phone_look(self):
        self.post({"do": "effect", "effect": "ocean"})
        self.post({"do": "undo"})
        self.assertEqual((self.m.states["lamp"].mode, self.m.states["lamp"].color), ("color", "#123456"))

    def test_a_look_needs_a_light_that_is_on(self):
        self.m.update(None, {"on": False})
        self.assertIn("Switch a light on", self.refused({"do": "effect", "effect": "fire"}))

    # ---- links ----------------------------------------------------------------------------

    def test_color_link(self):
        self.assertEqual(self.link("color/ff8800"), (200, {"ok": True, "action": "color/ff8800"}))
        self.assertEqual((self.m.states["lamp"].mode, self.m.states["lamp"].color), ("color", "#ff8800"))
        self.assertTrue(self.m.undo())
        self.assertEqual(self.m.states["lamp"].color, "#123456")
        for bad in ("color/nothex", "color/ff88", "color/%23ff8800", "color/ff8800/x", "color"):
            with self.subTest(bad=bad):
                self.assertIn(self.link(bad)[0], (400, 404))
        self.assertEqual(self.m.states["lamp"].color, "#123456")
        self.m.update(None, {"on": False})
        self.assertEqual(self.link("color/ff8800")[0], 409)

    def test_effect_link(self):
        self.assertEqual(self.link("effect/candle"), (200, {"ok": True, "action": "effect/candle"}))
        self.assertEqual((self.m.states["lamp"].mode, self.m.states["lamp"].effect), ("effect", "candle"))
        self.assertEqual(self.link("effect/Fire")[0], 200)                # (any case)
        self.assertEqual(self.m.states["lamp"].effect, "fire")
        for bad in ("effect/game_cs2", "effect/music_show", "effect/nope"):
            self.assertEqual(self.link(bad)[0], 404)
        self.assertEqual(self.m.states["lamp"].effect, "fire")

    # ---- focus ----------------------------------------------------------------------------

    def test_focus_state_when_it_is_off(self):
        focus = self.get("/api/state")["focus"]
        self.assertEqual(focus["phase"], "")
        self.assertEqual(focus["last"], "25 min of work, 5 min breaks, 4 rounds")

    def test_focus_last_choice_in_words(self):
        self.config["settings"]["focus_last"] = {"work": 50, "rest": 10, "rounds": 1, "look": ""}
        self.assertEqual(self.get("/api/state")["focus"]["last"], "50 min of work, 10 min breaks, 1 round")
        self.config.data["presets"] = [{"name": "Deep work", "states": {}}]
        self.config["settings"]["focus_last"] = {"work": 25, "rest": 5, "rounds": 2, "look": "Deep work"}
        self.assertEqual(self.get("/api/state")["focus"]["last"],
                         "25 min of work, 5 min breaks, 2 rounds, with the Deep work preset")

    def test_focus_start_skip_stop(self):
        state = self.post({"do": "focus", "action": "start"})["state"]
        self.assertTrue(self.m.focus.active)
        self.assertEqual(state["focus"]["phase"], "work")
        self.assertRegex(state["focus"]["label"], r"^Focus · \d+ min left · round 1 of 4$")
        self.assertTrue(state["focus"]["text"])
        state = self.post({"do": "focus", "action": "skip"})["state"]
        self.assertEqual(state["focus"]["phase"], "break")
        self.assertRegex(state["focus"]["label"], r"^Break · \d+ min left$")
        self.assertEqual(self.m.states["lamp"].color, "#4fd1a5")
        state = self.post({"do": "focus", "action": "stop"})["state"]
        self.assertFalse(self.m.focus.active)
        self.assertEqual(state["focus"]["phase"], "")
        self.assertEqual(self.m.states["lamp"].color, "#123456")        # (a stopped break gives the work light back)

    def test_focus_start_uses_the_last_choice(self):
        self.config["settings"]["focus_last"] = {"work": 45, "rest": 10, "rounds": 3, "look": ""}
        self.post({"do": "focus", "action": "start"})
        f = self.m.focus
        self.assertEqual((f.work, f.rest, f.rounds), (45, 10, 3))

    def test_focus_start_can_be_undone(self):
        self.config.data["presets"] = [{"name": "Focus", "states": {i: {"on": True, "mode": "color", "color": "#ff0000"}
                                                                for i in ("desk", "lamp", "strip")}}]
        self.config["settings"]["focus_last"]["look"] = "Focus"
        self.post({"do": "focus", "action": "start"})
        self.assertEqual(self.m.states["lamp"].color, "#ff0000")
        self.post({"do": "undo"})
        self.assertEqual(self.m.states["lamp"].color, "#123456")

    def test_focus_start_with_every_light_off_says_so(self):
        self.m.update(None, {"on": False})
        self.assertIn("Switch some lights on", self.refused({"do": "focus", "action": "start"}))
        self.assertFalse(self.m.focus.active)

    def test_focus_skip_and_stop_when_it_is_not_running_do_nothing(self):
        for action in ("skip", "stop"):
            state = self.post({"do": "focus", "action": action})["state"]
            self.assertEqual(state["focus"]["phase"], "")

    def test_bad_focus_actions_are_refused(self):
        for body in ({"do": "focus"}, {"do": "focus", "action": "pause"}, {"do": "focus", "action": None},
                     {"do": "focus", "action": ["start"]}):
            with self.subTest(body=body):
                self.refused(body)
        self.assertFalse(self.m.focus.active)

    # ---- the same rules as the rest ---------------------------------------------------------

    def test_without_the_key_nothing(self):
        before = self.looks()
        for body in ({"do": "color", "color": "#ff0000"}, {"do": "effect", "effect": "fire"},
                     {"do": "focus", "action": "start"}):
            for key in ("", "wrong"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    self.post(body, key)
                self.assertEqual(e.exception.code, 403)
        for path in ("color/ff0000", "effect/fire"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"{self.base}/do/{path}?k=wrong", timeout=5)
            self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.looks(), before)
        self.assertFalse(self.m.focus.active)

    def test_the_body_limit_still_holds(self):
        host, port = self.base.split("//")[1].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.putrequest("POST", f"/api/do?k={self.key}")        # (says it carries too much: refused before reading)
        conn.putheader("Content-Length", str(MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()
        self.assertEqual(self.m.states["lamp"].color, "#123456")

    def test_state_stays_small(self):
        self.assertLess(len(json.dumps(self.get("/api/state"))), 6000)


if __name__ == "__main__":
    unittest.main()
