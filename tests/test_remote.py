import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.remote import Remote, lan_ip


class FakeLight(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True

    def apply(self, state):
        pass


class PhoneRemote(unittest.TestCase):
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

    def get(self, path, key=None):
        url = f"{self.base}{path}?k={self.key if key is None else key}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(), r.headers

    def post(self, body, key=None):
        req = urllib.request.Request(f"{self.base}/api/do?k={self.key if key is None else key}",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_the_page(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Glowstage</title>", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_without_the_key_nothing(self):
        for key in ("", "wrong"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get("/api/state", key)
            self.assertEqual(e.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.post({"do": "power", "on": False}, key)
            self.assertEqual(e.exception.code, 403)
        self.assertTrue(self.m.states["desk"].on)

    def test_a_new_link_shuts_out_the_old_one(self):
        old = self.key
        self.r.new_link()
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/api/state", old)
        self.assertEqual(self.get("/api/state", self.r.settings["token"])[0], 200)

    def test_state(self):
        state = json.loads(self.get("/api/state")[1])
        self.assertEqual((state["on"], state["lights"], state["brightness"]), (2, 2, 60))
        self.assertIn("Warm white", [p["name"] for p in state["presets"]])

    def test_actions(self):
        self.assertEqual(self.post({"do": "power", "on": False})["state"]["on"], 0)
        self.post({"do": "power", "on": True})
        self.assertEqual(self.post({"do": "brightness", "value": 25})["state"]["brightness"], 25)
        self.assertEqual(self.post({"do": "preset", "name": "Warm white"})["state"]["active"], "Warm white")
        self.assertTrue(self.post({"do": "sleep", "minutes": 30})["state"]["sleep"])
        self.assertIsNone(self.post({"do": "sleep", "minutes": 0})["state"]["sleep"])

    def test_bad_requests(self):
        for body in ({"do": "explode"}, {"do": "brightness", "value": 500}, {"do": "preset", "name": "Nope"}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.post(body)
            self.assertEqual(e.exception.code, 400)

    def test_off_means_off(self):
        self.r.set_on(False)
        with self.assertRaises(OSError):
            self.get("/api/state")
        self.assertEqual(self.r.describe()["url"], "")

    def test_keeps_its_port(self):
        port = urlparse(self.r.url()).port
        self.r.set_on(False)
        self.r.set_on(True)
        self.assertEqual(urlparse(self.r.url()).port, port)

    def test_qr(self):
        self.assertTrue(self.r.qr_svg().startswith("<svg"))


class Network(unittest.TestCase):
    def test_lan_ip(self):
        ip = lan_ip()
        self.assertTrue(ip == "" or ip.count(".") == 3)
        self.assertFalse(ip.startswith("127."))


if __name__ == "__main__":
    unittest.main()
