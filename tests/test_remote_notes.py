import datetime
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

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


class PhoneNotes(unittest.TestCase):
    """The lines at the top of the phone page: what holds the lights back now, with a way out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        self.m._register(FakeLight("lamp"))
        self.m.update(None, {"on": True, "mode": "color", "color": "#ffffff", "brightness": 80})
        self.r = Remote(self.m, self.config, host="127.0.0.1")

    def tearDown(self):
        self.r.stop()
        self.m._stop.set()
        self.tmp.cleanup()

    def ids(self):
        return [n["id"] for n in self.r.state()["notes"]]

    def test_nothing_holds_them_back(self):
        self.assertEqual(self.r.state()["notes"], [])

    def test_the_night_limit_and_not_tonight(self):
        night = datetime.datetime.now().replace(hour=23, minute=30).timestamp()
        self.m.night.configure(True, "23:00", "07:00", 30)
        self.m.night.tick(night)
        note = self.r.state()["notes"][0]
        self.assertEqual((note["id"], note["action"]), ("night", "Not tonight"))
        self.assertIn("at most 30%", note["text"])
        self.r.act({"do": "note", "id": "night"})
        self.assertGreater(self.config["settings"]["night_skip_until"], 0)

    def test_paused_and_resume(self):
        self.m.automations.pause(60)
        self.assertEqual(self.ids(), ["paused"])
        self.r.act({"do": "note", "id": "paused"})
        self.assertEqual(self.m.automations.paused_until(), 0)
        self.assertEqual(self.ids(), [])

    def test_live_has_no_button(self):
        self.m.automations.active = "live"
        self.assertEqual(self.r.state()["notes"], [{"id": "live", "text": "You're live: your lights show your on air look", "action": ""}])

    def test_an_unknown_line(self):
        with self.assertRaises(ValueError):
            self.r.act({"do": "note", "id": "live"})

    def test_over_http(self):
        self.r.set_on(True)
        base, key = self.r.url().split("/?")[0], self.r.settings["token"]
        self.m.automations.pause(60)
        req = urllib.request.Request(f"{base}/api/do?k={key}", data=json.dumps({"do": "note", "id": "paused"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        self.assertEqual((body["ok"], body["state"]["notes"]), (True, []))
        with urllib.request.urlopen(f"{base}/?k={key}", timeout=5) as r:
            self.assertIn('id="notes"', r.read().decode())


if __name__ == "__main__":
    unittest.main()
