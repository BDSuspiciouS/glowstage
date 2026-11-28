"""The light show listening to a microphone, if you choose it (audio.py, manager.py, automations.py).

Nothing here opens a real device: soundcard is replaced by a small fake."""

import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from glowstage import audio
from glowstage.audio import BLOCK, MIC_FLOOR, MIC_TARGET, AudioAnalyzer, MicGain, list_microphones
from glowstage.automations import LEVEL, SOUND_FIRST, Automations
from glowstage.config import DEFAULTS
from glowstage.manager import DeviceManager as Manager
from tests.test_app_looks import FakeManager

RNG = np.random.default_rng(7)


def noise(rms: float, blocks: int = 1) -> np.ndarray:
    """Mono-ish sound (frames x 2) with a given rms."""
    return (RNG.standard_normal((BLOCK * blocks, 2)) * rms).astype(np.float32)


class DictConfig:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]

    def save_soon(self):
        pass


class FakeRecorder:
    def __init__(self, owner, device, sound, blocks):
        self.owner, self.device, self.sound, self.left = owner, device, sound, blocks

    def __enter__(self):
        self.device.opened += 1
        self.device.open_now += 1
        return self

    def __exit__(self, *exc):
        self.device.open_now -= 1
        self.device.closed += 1

    def record(self, numframes):
        self.left -= 1
        if self.left <= 0:          # nothing needs the sound any more
            self.owner._wanted_until = 0.0
        return self.sound(numframes)


class FakeDevice:
    def __init__(self, id, name, sound=None):
        self.id, self.name = id, name
        self.sound = sound or (lambda n: noise(0.05))
        self.opened = self.closed = self.open_now = 0
        self.blocks = 3
        self.owner = None

    def recorder(self, samplerate, blocksize):
        return FakeRecorder(self.owner, self, self.sound, self.blocks)


class FakeSoundcard:
    """What _listen_* use of soundcard: the microphones, the default one, the speaker and its loopback."""

    def __init__(self, mics, default=None):
        self.mics = list(mics)
        self.default = default or (mics[0].id if mics else "")
        self.speaker = FakeDevice("spk-1", "Headphones (Test)")
        self.loopback = FakeDevice("loop-1", "Headphones (Test)")
        self.asked_loopback = []

    def bind(self, analyzer):
        for d in [*self.mics, self.loopback]:
            d.owner = analyzer
        return self

    def all_microphones(self, include_loopback=False):
        self.asked_loopback.append(include_loopback)
        return list(self.mics)

    def default_microphone(self):
        found = next((m for m in self.mics if m.id == self.default), None)
        if found is None:
            raise RuntimeError("no default microphone")
        return found

    def default_speaker(self):
        return self.speaker

    def get_microphone(self, id, include_loopback=False):
        assert include_loopback, "the PC's sound is the loopback of the speaker"
        return self.loopback


def analyzer_for(sc, source=""):
    a = AudioAnalyzer()
    sc.bind(a)
    a.set_source(source)
    a._wanted_until = time.monotonic() + 100
    return a


class Choice(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(DEFAULTS["settings"]["show_input"], "")
        a = AudioAnalyzer()
        self.assertEqual(a.source, "")
        self.assertFalse(a.missing)
        self.assertEqual(a.device_kind, "local")

    def test_the_choices(self):
        a = AudioAnalyzer()
        a.set_source("default")
        self.assertEqual((a.source, a.device_kind), ("default", "microphone"))
        a.set_source(" {0.0.1}.{abc} ", "Microphone (USB Audio)")
        self.assertEqual((a.source, a.device, a.device_kind), ("{0.0.1}.{abc}", "Microphone (USB Audio)", "microphone"))
        a.set_source("")
        self.assertEqual((a.source, a.device, a.device_kind), ("", "", "local"))
        a.set_source(None)
        self.assertEqual(a.source, "")

    def test_the_pc_sound_is_listened_to_as_before(self):
        mic = FakeDevice("mic-1", "Microphone (USB Audio)")
        sc = FakeSoundcard([mic])
        a = analyzer_for(sc)
        with mock.patch.object(audio, "output_kind", return_value="local"):
            a._listen_once(sc)
        self.assertEqual(sc.loopback.opened, 1)
        self.assertEqual(mic.opened, 0)         # the microphone was never touched
        self.assertEqual(a.device, "Headphones (Test)")
        self.assertIsNone(a._agc)               # (no gain on what the PC plays)

    def test_the_chosen_microphone_is_opened(self):
        a_mic, b_mic = FakeDevice("mic-a", "Microphone (A)"), FakeDevice("mic-b", "Microphone (B)")
        sc = FakeSoundcard([a_mic, b_mic])
        a = analyzer_for(sc, "mic-b")
        a._listen_once(sc)
        self.assertEqual((a_mic.opened, b_mic.opened, sc.loopback.opened), (0, 1, 0))
        self.assertEqual((a.device, a.device_kind, a.missing), ("Microphone (B)", "microphone", False))
        self.assertEqual(set(sc.asked_loopback), {False})       # never lists what the speakers play

    def test_listing_leaves_out_loopback_and_marks_the_default(self):
        sc = FakeSoundcard([FakeDevice("mic-a", "Microphone (A)"), FakeDevice("mic-b", "Microphone (B)")], default="mic-b")
        found = list_microphones(sc)
        self.assertEqual(found, {"inputs": [{"id": "mic-a", "name": "Microphone (A)"}, {"id": "mic-b", "name": "Microphone (B)"}],
                                 "default": "mic-b"})
        self.assertEqual(sc.asked_loopback, [False])
        self.assertEqual(list_microphones(FakeSoundcard([]))["default"], "")

    def test_the_audio_thread_lists_them(self):
        sc = FakeSoundcard([FakeDevice("mic-a", "Microphone (A)")])
        a = AudioAnalyzer()
        a._ensure_thread = lambda: threading.Timer(0.05, a._serve_jobs, [sc]).start()   # (stands for the audio thread)
        self.assertEqual(a.microphones(timeout=3)["inputs"][0]["id"], "mic-a")


class Following(unittest.TestCase):
    def test_the_default_microphone_is_followed(self):
        a_mic, b_mic = FakeDevice("mic-a", "Microphone (A)"), FakeDevice("mic-b", "Microphone (B)")
        sc = FakeSoundcard([a_mic, b_mic], default="mic-a")
        an = analyzer_for(sc, "default")
        a_mic.blocks = 50
        real = a_mic.recorder

        def switch_default(*args, **kw):
            sc.default = "mic-b"            # the person picks another default in Windows
            return real(*args, **kw)

        a_mic.recorder = switch_default
        with mock.patch.object(audio, "FOLLOW_SECONDS", 0.0):
            an._listen_once(sc)             # leaves the old one ...
            self.assertEqual((a_mic.opened, a_mic.closed, an.device), (1, 1, "Microphone (A)"))
            an._wanted_until = time.monotonic() + 100
            an._listen_once(sc)             # ... and listens to the new default
        self.assertEqual((b_mic.opened, an.device), (1, "Microphone (B)"))

    def test_a_missing_microphone_shows_nothing_and_never_another_one(self):
        other = FakeDevice("mic-other", "Microphone (Other)")
        sc = FakeSoundcard([other])
        an = analyzer_for(sc, "mic-gone")
        with mock.patch.object(audio.time, "sleep"):
            an._listen_once(sc)
        self.assertTrue(an.missing)
        self.assertEqual((other.opened, sc.loopback.opened), (0, 0))   # not the other microphone, not the PC's sound
        self.assertEqual(an.request()["silent"], True)

    def test_a_microphone_that_is_back_is_used_again(self):
        mic = FakeDevice("mic-a", "Microphone (A)")
        sc = FakeSoundcard([])
        an = analyzer_for(sc, "mic-a")
        with mock.patch.object(audio.time, "sleep"):
            an._listen_once(sc)
            self.assertTrue(an.missing)
            sc.mics.append(mic)
            mic.owner = an
            an._listen_once(sc)
        self.assertFalse(an.missing)
        self.assertEqual(mic.opened, 1)

    def test_no_default_microphone_at_all(self):
        sc = FakeSoundcard([])
        an = analyzer_for(sc, "default")
        with mock.patch.object(audio.time, "sleep"):
            an._listen_once(sc)
        self.assertTrue(an.missing)

    def test_one_that_stops_answering_is_missing(self):
        mic = FakeDevice("mic-a", "Microphone (A)")

        def unplugged(n):
            raise OSError("The device was unplugged")

        mic.sound = unplugged
        sc = FakeSoundcard([mic])
        an = analyzer_for(sc, "mic-a")
        with mock.patch.object(audio.time, "sleep"), self.assertLogs("glowstage.audio", "WARNING"):
            an._step(sc)
        self.assertTrue(an.missing)
        self.assertEqual(mic.open_now, 0)       # (and it was let go)


class Lifetime(unittest.TestCase):
    def test_nothing_is_open_while_nothing_needs_the_sound(self):
        mic = FakeDevice("mic-a", "Microphone (A)")
        sc = FakeSoundcard([mic])
        an = AudioAnalyzer()
        sc.bind(an)
        an.set_source("mic-a")
        with mock.patch.object(audio.time, "sleep"):
            for _ in range(3):
                an._step(sc)                    # (no effect asked for sound)
        self.assertEqual((mic.opened, an.active), (0, False))

    def test_the_microphone_is_let_go_when_the_show_stops(self):
        mic = FakeDevice("mic-a", "Microphone (A)")
        mic.blocks = 4
        sc = FakeSoundcard([mic])
        an = analyzer_for(sc, "mic-a")
        an._step(sc)
        self.assertEqual((mic.opened, mic.closed, mic.open_now), (1, 1, 0))
        with mock.patch.object(audio.time, "sleep"):
            an._step(sc)                        # still nothing asking: it is not opened again
        self.assertEqual(mic.opened, 1)

    def test_switching_back_to_the_pc_releases_the_microphone(self):
        mic = FakeDevice("mic-a", "Microphone (A)")
        mic.blocks = 1000
        sc = FakeSoundcard([mic])
        an = analyzer_for(sc, "mic-a")
        real = mic.sound
        calls = []

        def sound(n):
            calls.append(1)
            if len(calls) == 3:
                an.set_source("")               # (chosen on the Light show page)
            return real(n)

        mic.sound = sound
        an._listen_once(sc)
        self.assertEqual((mic.opened, mic.closed), (1, 1))
        self.assertLess(len(calls), 10)


class Gain(unittest.TestCase):
    def feed(self, gain, rms, blocks):
        out = None
        for _ in range(blocks):
            out = gain.apply(noise(rms))
        return out

    def test_quiet_music_is_brought_up(self):
        out = self.feed(MicGain(), 0.004, 20)
        self.assertAlmostEqual(float(np.sqrt(np.mean(out ** 2))), MIC_TARGET, delta=0.02)

    def test_a_quieter_room_is_followed_over_a_few_seconds(self):
        g = MicGain()
        self.feed(g, 0.05, 30)                  # loud at first ...
        early = float(np.sqrt(np.mean(self.feed(g, 0.004, 1) ** 2)))
        late = float(np.sqrt(np.mean(self.feed(g, 0.004, 700) ** 2)))     # ... then ~15 s of quiet
        self.assertLess(early, 0.03)            # (not turned up all at once)
        self.assertAlmostEqual(late, MIC_TARGET, delta=0.02)

    def test_silence_is_not_turned_into_noise(self):
        g = MicGain()
        quiet_room = noise(MIC_FLOOR / 4)
        out = self.feed(g, MIC_FLOOR / 4, 50)
        self.assertFalse(g.heard)
        self.assertLessEqual(float(np.abs(out).max()), float(np.abs(quiet_room).max()) * 2)   # not amplified
        self.assertEqual(float(np.abs(g.apply(np.zeros((BLOCK, 2), np.float32))).max()), 0.0)

    def test_a_loud_burst_does_not_clip(self):
        g = MicGain()
        self.feed(g, 0.004, 100)                # turned up for a quiet room
        burst = g.apply(noise(0.4))
        self.assertTrue(g.heard)
        self.assertEqual(int((np.abs(burst) >= 1.0).sum()), 0)
        self.assertLess(float(np.sqrt(np.mean(burst ** 2))), 0.15)      # right away down at the usual level

    def test_the_analysis_hears_music_in_a_quiet_room_but_not_the_room(self):
        an = AudioAnalyzer()
        an._agc = MicGain()
        for _ in range(60):
            an._analyze(noise(0.004))
        self.assertFalse(an._snap["silent"])
        self.assertGreater(an._snap["rms"], 0.05)
        an = AudioAnalyzer()
        an._agc = MicGain()
        for _ in range(120):
            an._analyze(noise(MIC_FLOOR / 3))      # the hiss of a quiet room
        an._last_sound = 0.0
        an._analyze(noise(MIC_FLOOR / 3))
        self.assertTrue(an._snap["silent"])
        self.assertEqual(an._snap["level"], 0.0)

    def test_what_the_pc_plays_is_not_touched(self):
        an = AudioAnalyzer()
        mono = noise(0.2)[:, :1]
        an._analyze(np.repeat(mono, 2, axis=1))
        self.assertAlmostEqual(an._snap["rms"], 0.2, delta=0.02)    # (no gain: as before)


class StubAudio:
    def __init__(self, source="mic-a", active=False):
        self.source, self._active, self.asked = source, active, []
        self._pc = False

    @property
    def active(self):
        return self._active

    def pc_requested(self):
        return self._pc

    def request(self, pc_sound=False):
        self.asked.append(pc_sound)
        self._pc = pc_sound
        self._active = True
        return {"silent": False, "bpm_conf": 0.0, "bpm": 0.0}


class AutoStart(unittest.TestCase):
    def setUp(self):
        self.m = FakeManager()
        self.m.engine = types.SimpleNamespace(audio=StubAudio())
        self.m.config.data["settings"]["auto_show"] = True
        self.a = Automations(self.m)
        self.rules = {"music": "show"}

    def listen(self, peak, seconds):
        for t in range(seconds):
            self.a._listen(1000.0 + t, self.rules, peak)

    def test_room_noise_never_wakes_the_analysis(self):
        # The meter reads the PC's output: a noisy room leaves it at 0.
        self.listen(0.0, 30)
        self.assertEqual(self.m.engine.audio.asked, [])
        self.assertFalse(self.a._music)

    def test_pc_sound_is_checked_on_the_pc_sound_not_the_room(self):
        self.listen(LEVEL * 10, int(SOUND_FIRST) + 3)
        self.assertTrue(self.m.engine.audio.asked)
        self.assertTrue(all(self.m.engine.audio.asked))         # always asked for the PC's own sound

    def test_while_a_microphone_is_in_use_it_is_left_alone(self):
        self.m.engine.audio._active = True      # the show listens to the microphone right now
        self.listen(LEVEL * 10, int(SOUND_FIRST) + 5)
        self.assertEqual(self.m.engine.audio.asked, [])
        self.assertFalse(self.a._music)

    def test_the_analyzer_gives_the_pc_sound_when_asked_and_the_microphone_otherwise(self):
        an = AudioAnalyzer()
        an.set_source("mic-a")
        an._ensure_thread = lambda: None
        self.assertEqual(an._source_now(), "mic-a")
        an.request(pc_sound=True)
        self.assertEqual(an._source_now(), "")
        self.assertTrue(an.pc_requested())
        an.request()                            # the show asks: back to the microphone
        self.assertEqual(an._source_now(), "mic-a")


class Sync(unittest.TestCase):
    def manager(self, source, device, kind, saved=None):
        a = AudioAnalyzer()
        a.source, a.device, a.device_kind = source, device, kind
        m = types.SimpleNamespace(engine=types.SimpleNamespace(audio=a), config=types.SimpleNamespace(data={"show_sync": dict(saved or {})}),
                                  _changed=lambda: None)
        m.config.save_soon = lambda: None
        return m

    def test_a_microphone_has_no_delay_to_start_with(self):
        m = self.manager("mic-a", "Microphone (USB Audio)", "microphone", {"": 150, "Headphones (X)": 200})
        self.assertEqual(Manager.show_sync(m), {"ms": 0, "auto": True})     # (not the output's, not Bluetooth's)

    def test_it_is_kept_per_microphone(self):
        m = self.manager("mic-a", "Microphone (USB Audio)", "microphone", {"Headphones (X)": 200})
        Manager.set_show_sync(m, 40)
        self.assertEqual(m.config.data["show_sync"], {"Headphones (X)": 200, "Microphone (USB Audio)": 40})
        self.assertEqual(Manager.show_sync(m), {"ms": 40, "auto": False})
        Manager.set_show_sync(m, None)
        self.assertEqual(Manager.show_sync(m), {"ms": 0, "auto": True})
        self.assertEqual(m.config.data["show_sync"], {"Headphones (X)": 200})

    def test_an_output_keeps_its_own(self):
        m = self.manager("", "Headphones (X)", "bluetooth", {"Microphone (USB Audio)": 40})
        self.assertEqual(Manager.show_sync(m), {"ms": 200, "auto": True})
        Manager.set_show_sync(m, 120)
        self.assertEqual(m.config.data["show_sync"]["Headphones (X)"], 120)

    def test_without_a_name_the_output_setting_is_not_touched(self):
        m = self.manager("mic-a", "", "microphone")
        with self.assertRaises(ValueError):
            Manager.set_show_sync(m, 40)
        self.assertEqual(m.config.data["show_sync"], {})


class Choosing(unittest.TestCase):
    def manager(self):
        a = AudioAnalyzer()
        found = {"inputs": [{"id": "mic-a", "name": "Microphone (A)"}, {"id": "mic-b", "name": "Microphone (B)"}], "default": "mic-b"}
        a.microphones = lambda timeout=5.0: found
        config = DictConfig({"settings": {"show_input": ""}})
        m = types.SimpleNamespace(engine=types.SimpleNamespace(audio=a), config=config, changes=0)
        m._changed = lambda: setattr(m, "changes", m.changes + 1)
        return m

    def test_choose_a_microphone_and_go_back(self):
        m = self.manager()
        Manager.set_show_input(m, "mic-a")
        self.assertEqual(m.config.data["settings"]["show_input"], "mic-a")
        self.assertEqual((m.engine.audio.source, m.engine.audio.device), ("mic-a", "Microphone (A)"))
        Manager.set_show_input(m, "default")
        self.assertEqual((m.engine.audio.source, m.engine.audio.device), ("default", "Microphone (B)"))
        Manager.set_show_input(m, "")
        self.assertEqual((m.config.data["settings"]["show_input"], m.engine.audio.source, m.engine.audio.device_kind), ("", "", "local"))
        self.assertEqual(m.changes, 3)

    def test_a_microphone_that_is_not_there_is_refused(self):
        m = self.manager()
        with self.assertRaises(ValueError):
            Manager.set_show_input(m, "mic-zzz")
        self.assertEqual(m.config.data["settings"]["show_input"], "")
        self.assertEqual(m.engine.audio.source, "")

    def test_the_list_marks_the_default(self):
        m = self.manager()
        out = Manager.show_inputs(m)
        self.assertEqual([(i["name"], i["default"]) for i in out["inputs"]], [("Microphone (A)", False), ("Microphone (B)", True)])
        self.assertEqual(out["input"], "")


if __name__ == "__main__":
    unittest.main()
