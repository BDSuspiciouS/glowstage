import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from glowstage import backups
from glowstage.api import Api
from glowstage.config import Config

DAY = 86400


def age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.folder = Path(self.tmp.name) / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def copies(self):
        return sorted(f.name for f in self.folder.glob("*.json")) if self.folder.is_dir() else []


class Daily(Base):
    def test_the_first_save_of_a_day_keeps_the_file_as_it_was(self):
        c = Config(self.path)
        c["presets"].append({"name": "Mine"})
        c.save_now()
        age(self.path, 1)
        yesterday = time.strftime("%Y-%m-%d", time.localtime(self.path.stat().st_mtime))
        c["presets"].append({"name": "Today's"})
        c.save_now()
        self.assertEqual(self.copies(), [f"config-{yesterday}.json"])
        kept = json.loads((self.folder / f"config-{yesterday}.json").read_text(encoding="utf-8"))
        self.assertIn("Mine", [p["name"] for p in kept["presets"]])
        self.assertNotIn("Today's", [p["name"] for p in kept["presets"]])
        c.save_now()                                        # the same day again: no second copy
        self.assertEqual(len(self.copies()), 1)

    def test_saving_twice_today_keeps_nothing(self):
        c = Config(self.path)
        c.save_now()
        c.save_now()
        self.assertEqual(self.copies(), [])

    def test_only_the_last_days_stay(self):
        self.folder.mkdir()
        for d in range(1, 20):
            (self.folder / f"config-2026-10-{d:02d}.json").write_text("{}")
        for n in range(8):
            (self.folder / f"config-2026-10-20-1200{n:02d}-before.json").write_text("{}")
        (self.folder / "notes.json").write_text("{}")       # (not ours: left alone)
        backups.prune(self.folder)
        names = self.copies()
        self.assertEqual(len([n for n in names if n.endswith("-before.json")]), backups.KEEP_BEFORE)
        daily = [n for n in names if n.startswith("config-") and not n.endswith("-before.json")]
        self.assertEqual(daily[0], "config-2026-10-06.json")
        self.assertEqual(len(daily), backups.KEEP)
        self.assertIn("notes.json", names)


class Broken(Base):
    def test_a_broken_file_goes_back_to_the_newest_copy(self):
        c = Config(self.path)
        c["presets"].append({"name": "Mine"})
        c.save_now()
        age(self.path, 2)
        c.save_now()                                        # (keeps the copy from two days ago)
        self.path.write_text("{ not json", encoding="utf-8")
        again = Config(self.path)
        self.assertIn("Mine", [p["name"] for p in again["presets"]])
        self.assertRegex(again.recovered, r"^\d{4}-\d\d-\d\d$")
        self.assertTrue(self.path.with_suffix(".broken.json").is_file())

    def test_without_copies_it_starts_fresh(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        c = Config(self.path)
        self.assertEqual(c.recovered, "")
        self.assertTrue(c["presets"])                       # the built-in ones


class Checks(unittest.TestCase):
    def test_what_counts_as_glowstage_settings(self):
        self.assertTrue(backups.valid({"presets": [{"name": "A"}], "settings": {}}))
        self.assertFalse(backups.valid({"a": 1}))
        self.assertFalse(backups.valid([1]))
        self.assertFalse(backups.valid({"presets": [{"no": "name"}]}))
        self.assertFalse(backups.valid({"presets": [], "settings": []}))
        self.assertTrue(backups.valid({}, loose=True))

    def test_only_its_own_copies_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            (Path(d) / "backups").mkdir()
            (Path(d) / "backups" / "config-2026-11-26.json").write_text("{}")
            self.assertIsNotNone(backups.find(path, "config-2026-11-26.json"))
            for bad in ("../config.json", "config.json", "..\\config-2026-11-26.json",
                        "sub/config-2026-11-26.json", "config-2026-11-27.json"):
                self.assertIsNone(backups.find(path, bad), bad)


class FakeWindow:
    def __init__(self, answer):
        self.answer = answer

    def create_file_dialog(self, *args, **kwargs):
        return self.answer


class FakeApp:
    def __init__(self, config, answer=None):
        self.config = config
        self.window = FakeWindow(answer)
        self.restarted = threading.Event()

    def restart(self, show=False):
        self.restarted.set()


class Loading(Base):
    def test_going_back_to_a_copy(self):
        c = Config(self.path)
        c["presets"].append({"name": "Old"})
        c.save_now()
        age(self.path, 1)
        c["presets"].append({"name": "New"})
        c.save_now()
        c["presets"].append({"name": "Not saved yet"})
        app = FakeApp(c)
        name = next(b["file"] for b in backups.listing(self.path) if not b["before"])
        self.assertTrue(Api(app).restore_backup(name)["ok"])
        self.assertTrue(app.restarted.wait(2))
        now = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("New", [p["name"] for p in now["presets"]])
        before = next(f for f in self.folder.glob("*-before.json"))
        kept = [p["name"] for p in json.loads(before.read_text(encoding="utf-8"))["presets"]]
        self.assertIn("Not saved yet", kept)                # what you had, up to the last change
        c["presets"].append({"name": "After"})
        c.save_now()                                        # closed: the restart loads the copy
        self.assertNotIn("After", self.path.read_text(encoding="utf-8"))

    def test_a_file_to_another_pc_and_back(self):
        c = Config(self.path)
        c["presets"].append({"name": "Mine"})
        out = Path(self.tmp.name) / "Glowstage settings.json"
        api = Api(FakeApp(c, answer=str(out)))
        self.assertEqual(api.export_settings()["result"], out.name)
        other = Config(Path(self.tmp.name) / "other" / "config.json")
        app = FakeApp(other, answer=(str(out),))
        api = Api(app)
        info = api.pick_settings_file()["result"]
        self.assertEqual(info["presets"], len(c["presets"]))
        self.assertTrue(api.load_settings_file()["ok"])
        self.assertTrue(app.restarted.wait(2))
        self.assertIn("Mine", other.path.read_text(encoding="utf-8"))

    def test_a_file_that_isnt_ours(self):
        wrong = Path(self.tmp.name) / "photo.json"
        wrong.write_text('{"width": 10}', encoding="utf-8")
        api = Api(FakeApp(Config(self.path), answer=(str(wrong),)))
        r = api.pick_settings_file()
        self.assertFalse(r["ok"])
        self.assertIn("photo.json", r["error"])
        self.assertFalse(api.load_settings_file()["ok"])

    def test_cancelled(self):
        api = Api(FakeApp(Config(self.path), answer=None))
        self.assertEqual(api.export_settings()["result"], "")
        self.assertIsNone(api.pick_settings_file()["result"])


if __name__ == "__main__":
    unittest.main()
