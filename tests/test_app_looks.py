import unittest

from glowstage import automations
from glowstage.automations import Automations, clean_app_looks


class FakeManager:
    def __init__(self):
        self.look = {"lamp": {"on": True, "mode": "color", "color": "#ffffff"}}
        self.config = type("C", (), {})()
        self.config.data = {"app_looks": []}
        self.config.__class__.__getitem__ = lambda c, k: c.data[k]
        self.config.data["settings"] = {"active_preset": "Mine", "auto_game": "", "auto_video": "", "auto_show": False}
        self.games = type("G", (), {"settings": {"cs2": False}})()
        self.presence = type("P", (), {"active": False})()      # (Someone's home mode: off)
        self.presets = ["Focus", "Gaming", "Mine"]

    def looks(self):
        return {k: dict(v) for k, v in self.look.items()}

    def restore_looks(self, looks, preset):
        self.look = {k: dict(v) for k, v in looks.items()}
        self.config.data["settings"]["active_preset"] = preset

    def any_on(self):
        return any(v["on"] for v in self.look.values())

    def show_running(self):
        return False

    def preset_names(self):
        return self.presets

    def apply_preset(self, name):
        self.look = {"lamp": {"on": True, "mode": "color", "color": {"Focus": "#eaf1ff", "Gaming": "#3a00ff"}[name]}}
        self.config.data["settings"]["active_preset"] = name

    def screen_sync(self):
        self.look = {"lamp": {"on": True, "mode": "effect", "effect": "screen"}}


class AppLooks(unittest.TestCase):
    def setUp(self):
        self.m = FakeManager()
        self.a = Automations(self.m)

    def use(self, *items):
        self.m.config.data["app_looks"] = [{"exe": e, "name": e, "action": act} for e, act in items]

    def run_for(self, start, seconds, apps, kind=None):
        for k in range(seconds + 1):
            self.a.step(start + k, self.a.rules(), kind, 0.0, False, apps)
        return start + seconds + 1

    def color(self):
        return self.m.look["lamp"].get("color")

    def test_open_then_close(self):
        self.use(("photoshop.exe", "preset:Focus"))
        t = self.run_for(0, 4, {"photoshop.exe"})
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "app:photoshop.exe")
        self.run_for(t, 4, set())
        self.assertEqual(self.color(), "#ffffff")
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_a_quick_switch_doesnt_count(self):
        self.use(("photoshop.exe", "preset:Focus"))
        self.run_for(0, 1, {"photoshop.exe"})       # open for two seconds only
        self.run_for(2, 4, set())
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_the_first_app_goes_first(self):
        self.use(("obs64.exe", "preset:Gaming"), ("photoshop.exe", "preset:Focus"))
        t = self.run_for(0, 4, {"photoshop.exe"})
        self.assertEqual(self.color(), "#eaf1ff")
        t = self.run_for(t, 4, {"photoshop.exe", "obs64.exe"})
        self.assertEqual(self.color(), "#3a00ff")
        self.run_for(t, 4, {"photoshop.exe"})
        self.assertEqual(self.color(), "#eaf1ff")

    def test_an_app_goes_before_a_full_screen_game(self):
        self.use(("obs64.exe", "preset:Focus"))
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.run_for(0, 4, {"obs64.exe"}, kind="game")
        self.assertEqual(self.color(), "#eaf1ff")

    def test_taken_over_by_hand(self):
        self.use(("photoshop.exe", "preset:Focus"))
        t = self.run_for(0, 4, {"photoshop.exe"})
        self.m.look["lamp"]["color"] = "#ff0000"   # you pick a color yourself
        t = self.run_for(t, 2, {"photoshop.exe"})
        self.run_for(t, 4, set())
        self.assertEqual(self.color(), "#ff0000")    # left alone


class TheList(unittest.TestCase):
    def test_checked(self):
        items = [{"exe": "Photoshop.EXE", "name": "Adobe Photoshop", "action": "preset:Focus"},
                 {"exe": "photoshop.exe", "action": "screen"},            # twice: the first counts
                 {"exe": "C:\\evil\\x.exe", "action": "screen"},          # a path, not a program name
                 {"exe": "notes.txt", "action": "screen"}]
        self.assertEqual(clean_app_looks(items, ["Focus"]),
                         [{"exe": "photoshop.exe", "name": "Adobe Photoshop", "action": "preset:Focus"}])

    def test_a_look_that_doesnt_exist(self):
        with self.assertRaises(ValueError):
            clean_app_looks([{"exe": "a.exe", "action": "preset:Gone"}], ["Focus"])

    def test_open_apps_on_this_pc(self):
        apps = automations.open_apps()
        self.assertIsInstance(apps, list)
        for a in apps:
            self.assertTrue(a["exe"].endswith(".exe"))
            self.assertNotIn(a["exe"], automations.SYSTEM_APPS)
            self.assertTrue(a["name"])


if __name__ == "__main__":
    unittest.main()
