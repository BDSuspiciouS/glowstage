import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from glowstage import diagnostics
from glowstage.config import Config
from glowstage.devices.base import Device
from glowstage.manager import DeviceManager
from glowstage.remote import Remote


class FakeLight(Device):
    kind = "wled"

    def __init__(self, dev_id, online=True, error=None):
        super().__init__(dev_id, dev_id)
        self.online, self.error, self.pixel_count = online, error, 60

    def apply(self, state):
        pass


class FakeApp:
    def __init__(self, folder):
        self.config = Config(Path(folder) / "config.json")
        self.manager = DeviceManager(self.config)
        self.manager._register(FakeLight("Desk strip"))
        self.manager._register(FakeLight("Shelf", online=False, error="Not responding"))
        self.remote = Remote(self.manager, self.config, host="127.0.0.1")
        self.remote.settings.update(on=True, token="SECRET-PHONE-KEY")
        self.manager.games.settings.update(cs2=True, token="SECRET-CS2-KEY")


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = FakeApp(self.tmp.name)
        self.log = Path(self.tmp.name) / "glowstage.log"
        self.log.write_text("\n".join(
            [f"2026-11-27 10:00:{i:02d},000 INFO glowstage.app: fine {i}" for i in range(50)]
            + [f"2026-11-27 11:00:{i:02d},000 WARNING glowstage.manager: Shelf: Not responding ({i})" for i in range(40)]
            + ["Traceback (most recent call last):", "2026-11-27 12:00:00,000 ERROR glowstage.api: broke"]), encoding="utf-8")
        self._old = diagnostics.LOG
        diagnostics.LOG = self.log

    def tearDown(self):
        diagnostics.LOG = self._old
        self.app.manager._stop.set()
        self.tmp.cleanup()

    def text(self):
        return diagnostics.report(self.app)

    def test_whats_in_it(self):
        t = self.text()
        self.assertIn("Glowstage ", t)
        self.assertIn("Lights: 2 (1 answering)", t)
        self.assertIn("- Shelf (wled, room, 60 LEDs): not answering: Not responding", t)
        self.assertIn("phone remote on", t)
        self.assertIn("CS2 connected", t)
        self.assertIn("Dota 2 off", t)
        self.assertIn("ERROR glowstage.api: broke", t)
        self.assertNotIn("INFO", t)

    def test_never_a_key(self):
        t = self.text()
        self.assertNotIn("SECRET", t)
        self.assertNotIn("SECRET", diagnostics.issue_url(t))

    def test_only_the_last_warnings(self):
        problems = diagnostics.recent_problems(self.log)
        self.assertEqual(len(problems), diagnostics.PROBLEM_LINES)
        self.assertTrue(problems[-1].endswith("broke"))

    def test_the_issue_link(self):
        url = diagnostics.issue_url(self.text() + "\n" + "x" * 9000)
        self.assertLess(len(url), 8000)
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["template"], ["bug_report.yml"])
        self.assertEqual(q["lights"], ["Desk strip, Shelf"])
        self.assertIn("Lights: 2", q["logs"][0])

    def test_no_log(self):
        self.assertEqual(diagnostics.recent_problems(Path(self.tmp.name) / "nope.log"), [])


if __name__ == "__main__":
    unittest.main()
