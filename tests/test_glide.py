import tempfile
import threading
import time
import unittest
from pathlib import Path

from glowstage.config import Config
from glowstage.devices import wled
from glowstage.devices.base import GLIDES, Device
from glowstage.manager import DeviceManager, DeviceWorker
from glowstage.state import LightState
from tests.test_fades import FakeLight, color


class Setting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.a, self.b = FakeLight(), FakeLight()
        self.b.id = "fake2"
        for d in (self.a, self.b):
            self.m._register(d)

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def test_smooth_by_default(self):
        self.assertEqual(self.m.glide_seconds(), GLIDES["smooth"])
        self.assertEqual(self.a.glide, 0.35)

    def test_every_light_takes_it(self):
        self.m.set_glide("slow")
        self.assertEqual((self.a.glide, self.b.glide), (2.0, 2.0))
        self.assertEqual(self.m.config["settings"]["glide"], "slow")
        late = FakeLight()
        late.id = "late"
        self.m._register(late)
        self.assertEqual(late.glide, 2.0)                    # (a light found later too)

    def test_only_the_choices(self):
        for bad in ("fast", "", None, 3):
            with self.assertRaises(ValueError):
                self.m.set_glide(bad)
        self.assertEqual(self.a.glide, 0.35)


class Worker(unittest.TestCase):
    """The worker tells a light how long the change it applies now may take."""

    def run_one(self, dev, state, instant=False, first=None):
        w = DeviceWorker(dev, on_error=lambda d, e: self.fail(e))
        if first:
            w.submit(first)
            self.wait(w)
            time.sleep(w.QUICK + 0.05)
        seen = []
        dev.show_color = lambda rgb, st, _old=dev.show_color: (seen.append(dev.moment), _old(rgb, st))
        w.submit(state, instant=instant)
        self.wait(w)
        return seen

    def wait(self, w):
        end = time.monotonic() + 3
        while w.busy and time.monotonic() < end:
            time.sleep(0.01)

    def test_a_change_takes_the_glide(self):
        dev = FakeLight(fades=False)
        dev.glide = 2.0
        self.assertEqual(self.run_one(dev, color("#ff0000"))[-1], 2.0)

    def test_a_flash_or_a_drag_is_at_once(self):
        dev = FakeLight(fades=False)
        dev.glide = 2.0
        self.assertEqual(self.run_one(dev, color("#ff0000"), instant=True)[-1], 0.0)

    def test_local_lights_step_through_it(self):
        quick, slow = FakeLight(), FakeLight()
        quick.glide, slow.glide = 0.0, 0.6
        for dev in (quick, slow):
            self.run_one(dev, color("#ff0000"), first=color("#0000ff"))
        self.assertEqual(len(quick.shown[1:]), 1)            # instantly: one step
        self.assertGreater(len(slow.shown[1:]), 10)          # slowly: many


class WledIsTold(unittest.TestCase):
    def test_tt_follows_the_moment(self):
        sent = []
        dev = wled.WledDevice.__new__(wled.WledDevice)
        Device.__init__(dev, "wled:x", "Strip")
        dev._segments = [0]
        dev._post = lambda body, timeout=3.0: sent.append(body)
        for moment, tt in ((0.0, 0), (0.35, 4), (2.0, 20)):
            dev.moment = moment
            dev.show_color((255, 0, 0), LightState(brightness=50))
            dev.power_off()
            self.assertEqual((sent[-2]["tt"], sent[-1]["tt"]), (tt, tt))


class FromTheWindow(unittest.TestCase):
    def test_set_setting(self):
        from types import SimpleNamespace
        from glowstage.api import Api
        tmp = tempfile.TemporaryDirectory()
        cfg = Config(Path(tmp.name) / "config.json")
        m = DeviceManager(cfg)
        try:
            api = Api(SimpleNamespace(config=cfg, manager=m))
            self.assertTrue(api.set_setting("glide", "instant")["ok"])
            self.assertEqual(m.glide_seconds(), 0.0)
            self.assertFalse(api.set_setting("glide", "warp")["ok"])
        finally:
            m._stop.set()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
