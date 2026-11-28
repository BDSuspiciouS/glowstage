"""Startup stays light: the heavy parts load when something first needs them, and still work then."""

import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from glowstage import genre, lol, palette, qr
from glowstage.devices import discovery
from glowstage.devices.registry import Found

ROOT = Path(__file__).resolve().parents[1]


def loaded_after(code: str) -> dict:
    """Run `code` in a fresh interpreter (importing the app first) and say which of the heavy
    modules are loaded afterwards."""
    names = ["zeroconf", "onnxruntime", "soundcard", "PIL.ImageOps"]
    script = (f"import glowstage.app, sys\n{code}\n"
              f"print(repr({{n: n in sys.modules for n in {names!r}}}))")
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return eval(out.stdout.strip().splitlines()[-1])


class ImportStaysLight(unittest.TestCase):
    def test_importing_the_app_loads_none_of_the_heavy_modules(self):
        self.assertEqual(loaded_after(""), dict.fromkeys(["zeroconf", "onnxruntime", "soundcard", "PIL.ImageOps"], False))

    def test_building_the_manager_does_not_either(self):
        loaded = loaded_after(
            "import tempfile, pathlib\n"
            "from glowstage.config import Config\n"
            "from glowstage.manager import DeviceManager\n"
            "DeviceManager(Config(pathlib.Path(tempfile.mkdtemp()) / 'config.json'))")
        self.assertFalse(any(loaded.values()), loaded)

    def test_the_riot_opener_is_not_made_at_import(self):
        out = subprocess.run([sys.executable, "-c", "import glowstage.app, glowstage.lol as l; print(l._opener)"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip(), "None", out.stderr)


class LazyPathsStillWork(unittest.TestCase):
    def test_a_picture_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "p.png")
            Image.fromarray(np.full((40, 60, 3), (200, 30, 30), dtype=np.uint8)).save(path)
            img = palette.read(path)
            self.assertEqual(img.mode, "RGB")
            self.assertEqual(palette.main_colors(img)[0][:1], "#")
            data = Path(path).read_bytes()
        self.assertEqual(palette.read_bytes(data).mode, "RGB")

    def test_a_qr_is_made(self):
        code = qr.QR("http://192.168.1.2:4000/?k=abc")
        self.assertEqual(len(code.dark), code.size)

    def test_the_opener_is_made_once_on_first_use(self):
        lol._opener = None
        try:
            first = lol._get_opener()
            self.assertIs(first, lol._get_opener())
        finally:
            lol._opener = None

    def test_the_real_ai_model_loads_when_asked(self):
        if not (genre.MODEL.is_file() and genre.CLASS_MAP.is_file()):
            self.skipTest("the model isn't in this checkout")
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            self.skipTest("onnxruntime isn't installed")
        model = genre.load_yamnet()
        self.assertEqual(model.name, "YAMNet")
        self.assertEqual(len(model.scores(np.zeros(genre.SR * 2, dtype=np.float32))), len(model.names))


class FakeZeroconf(types.ModuleType):
    """Stands in for zeroconf, so no socket is opened."""

    def __init__(self):
        super().__init__("zeroconf")
        self.made: list = []
        outer = self

        class ServiceListener:
            pass

        class Zeroconf:
            def __init__(self):
                self.closed = False
                outer.made.append(self)

            def close(self):
                self.closed = True

        class ServiceBrowser:
            def __init__(self, zc, service, listener):
                self.zc, self.service, self.listener = zc, service, listener

        self.ServiceListener, self.Zeroconf, self.ServiceBrowser = ServiceListener, Zeroconf, ServiceBrowser


class MdnsStartsOnItsOwnThread(unittest.TestCase):
    def setUp(self):
        self.fake = FakeZeroconf()
        patcher = mock.patch.dict(sys.modules, {"zeroconf": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.found: list[Found] = []
        self.d = discovery.Discovery(self.found.append)
        self.d._by_service = {"_wled._tcp.local.": [], "_hue._tcp.local.": []}

    def test_start_does_not_wait_for_zeroconf(self):
        with mock.patch.object(discovery.Discovery, "_scan_loop"), \
                mock.patch.object(discovery.Discovery, "_start_mdns") as start:
            self.d.start()
            time.sleep(0.2)
        start.assert_called_once()

    def test_browsers_listen_and_hand_on_what_they_hear(self):
        self.d._start_mdns()
        self.assertEqual(len(self.fake.made), 1)
        self.assertEqual({b.service for b in self.d._browsers}, set(self.d._by_service))
        listener = self.d._browsers[0].listener
        self.assertIsInstance(listener, self.fake.ServiceListener)
        with mock.patch.object(self.d, "_resolve") as resolve:
            listener.add_service(self.d._zc, "_wled._tcp.local.", "x._wled._tcp.local.")
            listener.update_service(self.d._zc, "_wled._tcp.local.", "x._wled._tcp.local.")
            listener.remove_service(self.d._zc, "_wled._tcp.local.", "x._wled._tcp.local.")
        self.assertEqual(resolve.call_count, 2)

    def test_stop_closes_it_and_a_late_start_makes_nothing(self):
        self.d._start_mdns()
        zc = self.fake.made[0]
        self.d.stop()
        self.assertTrue(zc.closed)
        self.d._start_mdns()                 # (stopped before the thread got going)
        self.assertEqual(len(self.fake.made), 1)


class SilentAudio:
    def recent(self, seconds):
        return None


class ModelIsLetGoOfWhenTheShowIsOff(unittest.TestCase):
    def setUp(self):
        for name, value in (("INTERVAL", 0.02), ("RELEASE_AFTER", 0.1)):
            patcher = mock.patch.object(genre, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        stub = types.SimpleNamespace(name="stub", names=["a"], window=1.0)
        patcher = mock.patch.object(genre, "load_yamnet", return_value=stub)
        self.load = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(genre, "MODEL", Path(__file__))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(genre, "CLASS_MAP", Path(__file__))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.det = genre.GenreDetector(SilentAudio())

    def wait_for(self, check, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.01)
        return False

    def test_nothing_is_loaded_until_the_show_asks(self):
        self.assertEqual(self.det.model_name, "")
        self.assertEqual(self.load.call_count, 0)

    def test_loads_on_request_then_lets_go_when_idle_and_loads_again(self):
        self.det.request()
        self.assertTrue(self.wait_for(lambda: self.det.model_name == "stub"))
        self.assertTrue(self.wait_for(lambda: self.det._model is None and self.det._thread is None),
                        "the model was kept although nobody asked")
        self.assertEqual(self.det.model_name, "")
        self.det.request()
        self.assertTrue(self.wait_for(lambda: self.det.model_name == "stub"))
        self.assertEqual(self.load.call_count, 2)

    def test_keeps_the_model_while_the_show_keeps_asking(self):
        self.det.request()
        self.assertTrue(self.wait_for(lambda: self.det.model_name == "stub"))
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            self.det.request()
            time.sleep(0.02)
        self.assertEqual(self.det.model_name, "stub")
        self.assertEqual(self.load.call_count, 1)


if __name__ == "__main__":
    unittest.main()
