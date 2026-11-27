import tempfile
import unittest
from pathlib import Path
from unittest import mock

from glowstage import automations, calls
from glowstage.automations import Automations
from glowstage.config import Config
from glowstage.manager import DeviceManager
from tests.test_app_looks import FakeManager

BASE = calls.BASE


class FakeRegistry:
    """The consent store as a dict: path -> {"LastUsedTimeStart": n, "LastUsedTimeStop": n}."""

    def __init__(self, keys=None):
        self.keys = keys or {}

    def subkeys(self, path):
        prefix = path + "\\"
        return sorted({p[len(prefix):].split("\\")[0] for p in self.keys if p.startswith(prefix)})

    def value(self, path, name):
        return self.keys.get(path, {}).get(name)


def used(start=1, stop=0):
    return {"LastUsedTimeStart": start, "LastUsedTimeStop": stop}


class Detection(unittest.TestCase):
    def test_nothing_in_use_when_the_keys_are_missing(self):
        self.assertEqual(calls.in_use(FakeRegistry()), {})

    def test_the_real_reader_never_raises(self):
        self.assertIsInstance(calls.in_use(), dict)

    def test_start_without_stop_counts(self):
        reg = FakeRegistry({f"{BASE}\\webcam\\Microsoft.WindowsCamera_8wekyb3d8bbwe": used(134283603743943273, 0)})
        self.assertEqual(calls.in_use(reg), {"camera": ["Microsoft.WindowsCamera"]})

    def test_stopped_doesnt_count(self):
        reg = FakeRegistry({f"{BASE}\\microphone\\Some.App_abc": used(134283603743943273, 134283603769660266)})
        self.assertEqual(calls.in_use(reg), {})

    def test_never_started_doesnt_count(self):
        reg = FakeRegistry({f"{BASE}\\microphone\\Some.App_abc": used(0, 0)})
        self.assertEqual(calls.in_use(reg), {})

    def test_desktop_apps_give_the_exe_name(self):
        reg = FakeRegistry({
            f"{BASE}\\microphone\\NonPackaged\\C:#Program Files#Zoom#bin#Zoom.exe": used(),
            f"{BASE}\\webcam\\NonPackaged\\C:#Program Files#Zoom#bin#Zoom.exe": used(),
            f"{BASE}\\webcam\\NonPackaged\\C:#Tools#old.exe": used(5, 6),
        })
        self.assertEqual(calls.in_use(reg), {"camera": ["Zoom.exe"], "microphone": ["Zoom.exe"]})

    def test_glowstage_itself_is_ignored(self):
        reg = FakeRegistry({f"{BASE}\\microphone\\NonPackaged\\C:#Apps#Glowstage#Glowstage.exe": used()})
        self.assertEqual(calls.in_use(reg), {})

    def test_a_reader_that_fails_is_nothing_in_use(self):
        class Broken(FakeRegistry):
            def subkeys(self, path):
                raise OSError("no")

        self.assertEqual(calls.in_use(Broken()), {})


class OnACall(unittest.TestCase):
    def setUp(self):
        self.m = FakeManager()
        self.m.config.data["settings"]["auto_call"] = "preset:Focus"
        self.a = Automations(self.m)
        self.t = 0

    def run_for(self, seconds, call, kind=None):
        for _ in range(seconds):
            self.a.step(self.t, self.a.rules(), kind, 0.0, False, frozenset(), call)
            self.t += 1

    def color(self):
        return self.m.look["lamp"].get("color")

    def test_the_look_comes_after_the_delay_and_goes_back_after_the_call(self):
        self.run_for(4, True)
        self.assertEqual(self.color(), "#ffffff")           # a quick voice message: nothing yet
        self.run_for(2, True)
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "call")
        self.run_for(8, False)
        self.assertEqual(self.color(), "#eaf1ff")           # not yet: a short pause in the call
        self.run_for(4, False)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_a_short_use_doesnt_flick_the_lights(self):
        self.run_for(3, True)
        self.run_for(12, False)
        self.assertEqual(self.color(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_a_change_by_hand_is_left_alone(self):
        self.run_for(6, True)
        self.m.look = {"lamp": {"on": True, "mode": "color", "color": "#123456"}}
        self.run_for(20, True)
        self.assertEqual(self.color(), "#123456")
        self.assertIsNone(self.a.active)
        self.run_for(12, False)
        self.run_for(6, True)                                # a new call is a new chance
        self.assertEqual(self.color(), "#eaf1ff")

    def test_a_game_keeps_its_look_with_voice_chat_on(self):
        # (in a game with voice chat the microphone is on all the time: the game's look stays)
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.run_for(5, False, "game")
        self.assertEqual(self.color(), "#3a00ff")
        self.run_for(6, True, "game")
        self.assertEqual(self.color(), "#3a00ff")
        self.assertEqual(self.a.active, "game")
        self.run_for(6, True, None)                          # the game closed, the call goes on
        self.assertEqual(self.color(), "#eaf1ff")
        self.assertEqual(self.a.active, "call")
        self.run_for(12, False, None)
        self.assertEqual(self.color(), "#ffffff")
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_off_does_nothing(self):
        self.m.config.data["settings"]["auto_call"] = ""
        self.run_for(20, True)
        self.assertEqual(self.color(), "#ffffff")


class FakeStop:
    """Stands in for the thread's stop event: lets the loop go round `turns` times."""

    def __init__(self, turns):
        self.turns = turns

    def wait(self, _):
        self.turns -= 1
        return self.turns < 0


class WithAManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.m.config["settings"]["auto_call"] = "preset:Focus"

    def tearDown(self):
        self.m._stop.set()
        self.tmp.cleanup()

    def loop(self, turns=2):
        a = Automations(self.m)
        a._stop = FakeStop(turns)
        seen = []
        with mock.patch.object(calls, "in_use", return_value={"camera": ["Zoom.exe"]}), \
                mock.patch.object(Automations, "step", lambda s, *args: seen.append(args)):
            a._run()
        return seen

    def test_the_loop_tells_step_about_the_call(self):
        seen = self.loop()
        self.assertTrue(seen and all(args[-1] is True for args in seen))

    def test_nothing_runs_while_away(self):
        self.m.away.reasons.add("locked")
        self.assertEqual(self.loop(), [])

    def test_the_loop_doesnt_look_when_the_setting_is_off(self):
        self.m.config["settings"]["auto_call"] = ""
        self.m.config["settings"]["auto_game"] = "screen"
        with mock.patch.object(automations, "full_screen", return_value=(None, "")):
            seen = self.loop()
        self.assertTrue(seen and all(args[-1] is False for args in seen))

    def test_renaming_a_preset_updates_the_call_look(self):
        old = self.m.preset_names()[0]
        self.m.config["settings"]["auto_call"] = f"preset:{old}"
        self.m.rename_preset(old, "Renamed look")
        self.assertEqual(self.m.config["settings"]["auto_call"], "preset:Renamed look")


if __name__ == "__main__":
    unittest.main()
