import http.client
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from glowstage import palette
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.remote import MAX_BODY, Remote


class FakeLight(Device):
    def __init__(self, dev_id):
        super().__init__(dev_id, dev_id)
        self.online = True

    def apply(self, state):
        pass


def photo(colors=((230, 40, 30), (30, 60, 230)), size=(320, 180), kind="JPEG") -> bytes:
    """A photo the way the phone page sends it: small, one band of color per entry, left to right."""
    img = Image.new("RGB", size)
    band = size[0] // len(colors)
    for k, c in enumerate(colors):
        img.paste(c, (k * band, 0, size[0] if k == len(colors) - 1 else (k + 1) * band, size[1]))
    out = io.BytesIO()
    img.save(out, kind)
    return out.getvalue()


class PhonePhoto(unittest.TestCase):
    """Presets → From a picture, from the phone: a photo taken there lights the room."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        for i in ("desk", "lamp"):
            self.m._register(FakeLight(i))
        self.m.update(None, {"on": True, "mode": "color", "color": "#ffffff", "brightness": 60})
        self.r = Remote(self.m, self.config, host="127.0.0.1")
        self.r.set_on(True)
        self.base = self.r.url().split("/?")[0]
        self.key = self.r.settings["token"]

    def tearDown(self):
        self.r.stop()
        self.m._stop.set()
        self.tmp.cleanup()

    def post(self, path, body: bytes, kind="application/json", key=None):
        req = urllib.request.Request(f"{self.base}{path}?k={self.key if key is None else key}", data=body,
                                     method="POST", headers={"Content-Type": kind})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def send(self, data: bytes, key=None):
        return self.post("/api/picture", data, "image/jpeg", key)

    def act(self, **req):
        return self.post("/api/do", json.dumps(req).encode())

    def colors(self):
        return [self.m.states[i].color for i in ("desk", "lamp")]

    def test_a_photo_lights_the_room(self):
        status, body = self.send(photo())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(len(body["colors"]), 2)
        self.assertNotIn("#ffffff", self.colors())
        self.assertEqual(body["state"]["on"], 2)
        self.assertTrue(body["state"]["canUndo"])

    def test_png_and_a_photo_held_sideways(self):
        self.assertEqual(self.send(photo(kind="PNG"))[0], 200)
        img = Image.open(io.BytesIO(photo()))
        exif = img.getexif()
        exif[0x0112] = 6                                    # "turn it a quarter to read it"
        out = io.BytesIO()
        img.save(out, "JPEG", exif=exif)
        self.assertEqual(palette.read_bytes(out.getvalue()).size, (135, 240))   # upright: taller than wide

    def test_undo_puts_the_lights_back(self):
        self.send(photo())
        status, body = self.act(do="undo")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.colors(), ["#ffffff", "#ffffff"])
        self.m._undo = None
        status, body = self.act(do="undo")
        self.assertEqual((status, body["error"]), (400, "There's nothing to undo"))

    def test_save_as_a_preset(self):
        self.send(photo())
        status, body = self.act(do="save", name="  Phone photo ")
        self.assertEqual(status, 200)
        self.assertIn("Phone photo", self.m.preset_names())
        self.assertEqual(body["state"]["active"], "Phone photo")
        before = [p for p in self.config["presets"] if p["name"] == "Phone photo"]
        status, body = self.act(do="save", name="Phone photo")       # never over one of yours
        self.assertEqual((status, body["error"]), (400, "There's already a preset called Phone photo"))
        self.assertEqual([p for p in self.config["presets"] if p["name"] == "Phone photo"], before)
        for bad in ("", "   ", None, 5, "x" * 41):
            self.assertEqual(self.act(do="save", name=bad)[0], 400, bad)

    def test_not_a_photo(self):
        status, body = self.send(b"definitely not a picture")
        self.assertEqual((status, body["error"]), (400, palette.NOT_A_PICTURE))
        self.assertEqual(self.colors(), ["#ffffff", "#ffffff"])
        self.assertEqual(self.send(photo()[:200])[0], 400)          # cut off on the way

    def test_lights_off_keeps_the_last_undo(self):
        self.m.remember()
        before = self.m._undo
        self.m.update(None, {"on": False})
        status, body = self.send(photo())
        self.assertEqual((status, body["error"]), (400, "Switch a light on first, then try again"))
        self.assertIs(self.m._undo, before)

    def big(self, path, size):
        """The answer to a request that says it carries `size` bytes (none are sent: it's refused first)."""
        host, port = self.base.split("//")[1].split(":")
        c = http.client.HTTPConnection(host, int(port), timeout=5)
        c.putrequest("POST", f"{path}?k={self.key}")
        c.putheader("Content-Length", str(size))
        c.endheaders()
        try:
            return c.getresponse().status
        finally:
            c.close()

    def test_too_big(self):
        self.assertEqual(self.big("/api/picture", palette.PHOTO_BYTES + 1), 413)
        self.assertEqual(self.big("/api/do", MAX_BODY + 1), 413)        # (the rest stays small)
        with self.assertRaisesRegex(ValueError, "too big"):
            palette.read_bytes(photo(size=(5000, 4000), colors=((10, 10, 10),), kind="PNG"))
        with self.assertRaisesRegex(ValueError, "too big"):
            palette.read_bytes(b"\xff" * (palette.PHOTO_BYTES + 1))

    def test_wrong_key(self):
        self.assertEqual(self.send(photo(), key="wrong")[0], 403)
        self.assertEqual(self.colors(), ["#ffffff", "#ffffff"])

    def test_the_page_has_the_button(self):
        with urllib.request.urlopen(f"{self.base}/?k={self.key}", timeout=5) as r:
            page = r.read().decode()
        self.assertIn('id="photo"', page)
        self.assertIn("/api/picture", page)


if __name__ == "__main__":
    unittest.main()
