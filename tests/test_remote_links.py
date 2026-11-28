import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.remote import Remote


class FakeLight(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True

    def apply(self, state):
        pass


class ActionLinks(unittest.TestCase):
    """The plain links for Stream Deck, AutoHotkey and Home Assistant: /do/<action>?k=<key>."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i in ("desk", "lamp"):
            self.m._register(FakeLight(i))
        self.m.update(None, {"on": True, "brightness": 60})
        self.r = Remote(self.m, self.config, host="127.0.0.1")
        self.r.set_on(True)
        self.base = self.r.url().split("/?")[0]
        self.key = self.r.settings["token"]

    def tearDown(self):
        self.r.stop()
        self.m._stop.set()
        self.tmp.cleanup()

    def call(self, path, method="GET", key=None):
        """(status, json) for a link; HTTP errors come back as answers, not exceptions."""
        url = f"{self.base}/do/{path}?k={self.key if key is None else key}"
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if e.headers.get_content_type() == "application/json" else None)

    def lit(self):
        return [self.m.states[i].on for i in ("desk", "lamp")]

    def test_off_on_toggle(self):
        self.assertEqual(self.call("off"), (200, {"ok": True, "action": "off"}))
        self.assertEqual(self.lit(), [False, False])
        self.assertEqual(self.call("on")[0], 200)
        self.assertEqual(self.lit(), [True, True])
        self.call("toggle")
        self.assertEqual(self.lit(), [False, False])
        self.call("toggle")
        self.assertEqual(self.lit(), [True, True])

    def test_get_and_post_both_work(self):
        for method in ("GET", "POST"):
            self.m.update(None, {"on": True})
            self.assertEqual(self.call("off", method)[0], 200)
            self.assertEqual(self.lit(), [False, False])

    def test_preset_with_spaces_and_unknown(self):
        status, body = self.call("preset/" + quote("Warm white", safe=""))
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.config["settings"]["active_preset"], "Warm white")
        self.assertEqual(self.call("preset/Warm%20white")[0], 200)
        status, body = self.call("preset/Nope%20nope")
        self.assertEqual((status, body), (404, {"ok": False, "error": "No preset called Nope nope"}))
        self.assertEqual(self.call("preset")[0], 404)

    def test_a_preset_typed_in_any_case(self):
        self.assertEqual(self.call("preset/warm%20WHITE")[0], 200)
        self.assertEqual(self.config["settings"]["active_preset"], "Warm white")
        self.m.save_preset("Cozy two")
        self.m.save_preset("COZY TWO")                       # two the same but for case: say no
        self.assertEqual(self.call("preset/cozy%20two")[0], 404)
        self.assertEqual(self.call("preset/COZY%20TWO")[0], 200)   # (an exact match still works)

    def test_preset_name_with_a_slash(self):
        self.m.save_preset("Day/Night")
        self.assertEqual(self.call("preset/" + quote("Day/Night", safe=""))[0], 200)

    def test_brightness(self):
        self.assertEqual(self.call("brightness/25")[0], 200)
        self.assertEqual({self.m.states[i].brightness for i in ("desk", "lamp")}, {25})
        self.assertEqual(self.call("brightness/100")[0], 200)
        self.assertEqual(self.m.states["desk"].brightness, 100)
        self.call("brightness/0")                        # (the lowest light, not off)
        self.assertEqual((self.m.states["desk"].brightness, self.m.states["desk"].on), (1, True))
        for bad in ("101", "abc", "-5", "5.5", "", "99999"):
            status, body = self.call("brightness/" + bad)
            self.assertIn(status, (400, 404), bad)
            self.assertFalse((body or {}).get("ok", False))
        self.assertEqual(self.call("brightness/101")[0], 400)
        self.assertEqual(self.call("brightness/abc")[0], 400)

    def test_brighter_and_darker(self):
        self.call("brighter")
        self.assertEqual(self.m.states["desk"].brightness, 70)
        self.call("darker")
        self.call("darker")
        self.assertEqual(self.m.states["lamp"].brightness, 50)

    def test_show(self):
        self.assertEqual(self.call("show/start")[0], 200)
        self.assertTrue(self.m.show_running())
        self.assertEqual(self.call("show/stop")[0], 200)
        self.assertFalse(self.m.show_running())
        self.assertEqual(self.call("show/dance")[0], 404)

    def test_sleep_timer(self):
        self.assertEqual(self.call("sleep/30")[0], 200)
        self.assertTrue(self.m.sleep.active)
        self.assertEqual(self.call("sleep/cancel")[0], 200)
        self.assertFalse(self.m.sleep.active)
        self.assertEqual(self.call("sleep/0")[0], 400)
        self.assertEqual(self.call("sleep/9999")[0], 400)
        self.assertEqual(self.call("sleep/soon")[0], 400)
        self.assertFalse(self.m.sleep.active)

    def test_sleep_timer_needs_lights_on(self):
        self.m.update(None, {"on": False})
        status, body = self.call("sleep/30")
        self.assertEqual((status, body["ok"]), (400, False))

    def test_undo(self):
        self.assertEqual(self.call("undo")[0], 409)       # (nothing to undo yet)
        self.call("off")
        self.assertEqual(self.lit(), [False, False])
        self.assertEqual(self.call("undo")[0], 200)
        self.assertEqual(self.lit(), [True, True])
        self.call("preset/Warm%20white")
        self.call("undo")
        self.assertEqual(self.config["settings"].get("active_preset", ""), "")

    def test_unknown_action(self):
        for path in ("explode", "on/now", "", "brightness", "brightness/5/6"):
            self.assertEqual(self.call(path)[0], 404, path)
        self.assertEqual(self.lit(), [True, True])

    def test_trailing_slash(self):
        self.assertEqual(self.call("off/")[0], 200)

    def test_wrong_key(self):
        for key in ("", "wrong"):
            for method in ("GET", "POST"):
                self.assertEqual(self.call("off", method, key)[0], 403)
        self.assertEqual(self.lit(), [True, True])

    def test_a_new_link_shuts_out_old_links(self):
        old = self.key
        self.r.new_link()
        self.assertEqual(self.call("off", key=old)[0], 403)
        self.assertEqual(self.lit(), [True, True])
        self.assertEqual(self.call("off", key=self.r.settings["token"])[0], 200)

    def test_off_means_no_links(self):
        self.r.set_on(False)
        with self.assertRaises(OSError):
            self.call("off")
        self.assertEqual(self.lit(), [True, True])

    def test_a_failing_manager_doesnt_end_the_server(self):
        def boom(*a):
            raise RuntimeError("boom")
        self.m.set_power_all = boom
        with self.assertLogs("glowstage.remote", "ERROR"):
            self.assertEqual(self.call("off")[0], 500)
        del self.m.set_power_all
        self.assertEqual(self.call("off")[0], 200)

    def test_what_setup_needs(self):
        d = self.r.describe()
        self.assertEqual(d["api"], self.base + "/do")
        self.assertEqual(d["key"], self.key)
        self.assertEqual(f"{d['api']}/on?k={d['key']}", f"{self.base}/do/on?k={self.key}")
        self.r.set_on(False)
        d = self.r.describe()
        self.assertEqual((d["api"], d["key"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
