import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from glowstage.config import Config
from glowstage.manager import DeviceManager
from glowstage.remote import Remote


class HomeScreen(unittest.TestCase):
    """"Add to home screen" on the phone: a name, the app's icon, and the page full screen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.m = DeviceManager(self.config)
        self.r = Remote(self.m, self.config, host="127.0.0.1")
        self.r.set_on(True)
        self.base = self.r.url().split("/?")[0]
        self.key = self.r.settings["token"]

    def tearDown(self):
        self.r.stop()
        self.m._stop.set()
        self.tmp.cleanup()

    def get(self, path, key=None):
        sep = "&" if "?" in path else "?"
        try:
            with urllib.request.urlopen(f"{self.base}{path}{sep}k={self.key if key is None else key}", timeout=5) as r:
                return r.status, r.headers.get_content_type(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get_content_type(), e.read()

    def test_the_manifest(self):
        status, kind, body = self.get("/manifest.webmanifest")
        self.assertEqual((status, kind), (200, "application/manifest+json"))
        m = json.loads(body)
        self.assertEqual((m["name"], m["display"]), ("Glowstage", "standalone"))
        self.assertEqual(m["start_url"], f"/?k={self.key}")          # the icon opens the page with its key
        self.assertEqual({i["sizes"] for i in m["icons"]}, {"192x192", "512x512"})
        for icon in m["icons"]:
            self.assertIn(f"k={self.key}", icon["src"])

    def test_the_icons(self):
        for size in (180, 192, 512):
            status, kind, body = self.get(f"/icon.png?s={size}")
            self.assertEqual((status, kind), (200, "image/png"))
            img = Image.open(io.BytesIO(body))
            self.assertEqual(img.size, (size, size))
            self.assertEqual(img.getpixel((2, 2))[:3], (15, 16, 19))    # the page's dark background, no see-through corners
        self.assertEqual(Image.open(io.BytesIO(self.get("/icon.png?s=7")[2])).size, (192, 192))

    def test_only_with_the_key(self):
        for path in ("/manifest.webmanifest", "/icon.png?s=192"):
            self.assertEqual(self.get(path, key="wrong")[0], 403)

    def test_the_page_points_at_them(self):
        page = self.get("/")[2].decode()
        for part in ('rel="manifest"', 'rel="apple-touch-icon"', "apple-mobile-web-app-capable", "/manifest.webmanifest"):
            self.assertIn(part, page)


if __name__ == "__main__":
    unittest.main()
