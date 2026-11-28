"""The light show in your own colors: the colors come from the person, the choreography stays."""

from __future__ import annotations

import copy
import random
import unittest

import numpy as np

from glowstage import show
from glowstage.show import ShowDirector, clean_show_colors, led_color

BPM = 128
BEAT = 60 / BPM
DT = 0.02
CHORDS = [(0, False), (5, False), (7, False), (9, True)]   # C, F, G, Am: a pop progression in C
MINE = ["#ff3ca0", "#00d9ff", "#ffd000"]


class _Genre:
    """A genre detector that always hears one style (no AI model in the tests)."""
    error = ""
    model_name = ""

    def __init__(self, style: str = "edm"):
        self.snap = {"style": style, "top": [], "mood": None, "confidence": 0.9}

    def request(self) -> dict:
        return dict(self.snap)


def _analysis(t: float) -> dict:
    """What the PC's sound looks like at second t: a quiet start, a groove, a break, a build-up
    with a snare roll, the breath before the drop, the drop, then a groove again. Chords run
    through it in a clear key."""
    since = t % BEAT
    a = {"level": 0.6, "bands": [0.0] * 16, "bass": 0.6, "mid": 0.4, "treble": 0.3, "beat_count": int(t / BEAT),
         "silent": False, "rms": 0.3, "bass_raw": 0.8, "high_raw": 0.2, "bpm": float(BPM), "bpm_conf": 0.9,
         "grid_ago": since, "grid_conf": 0.8, "hit_ago": 99.0, "hat_ago": t % (BEAT / 2), "centroid": 0.3,
         "since_beat": since, "beats_ago": [since + 2 * BEAT, since + BEAT, since],
         "chord": CHORDS[int(t / (BEAT * 8)) % 4], "chord_ago": 0.2, "key": (0, False), "key_conf": 0.9,
         "impacts": []}
    if t > BEAT:
        a["hit_ago"] = (t - BEAT) % (2 * BEAT)         # a snare on beats 2 and 4
    no_kick = dict(since_beat=99.0, beats_ago=[], beat_count=int(30 / BEAT))
    if t < 2:
        a.update(silent=True, rms=0.0, bass_raw=0.0, high_raw=0.0, level=0.0, bass=0.0, mid=0.0, treble=0.0)
    elif 30 <= t < 40:                                  # break: the bass is gone
        a.update(bass_raw=0.03, bass=0.05, rms=0.14, level=0.3, hit_ago=99.0, **no_kick)
    elif 40 <= t < 52:                                  # build-up: the riser sweeps up, the snare rolls
        k = (t - 40) / 12
        roll = BEAT / (1 + int(4 * k) * 2)
        a.update(bass_raw=0.03, bass=0.05, rms=0.14 + 0.25 * k, level=0.3 + 0.4 * k, centroid=0.3 + 0.9 * k,
                 high_raw=0.2 + 0.6 * k, hit_ago=t % roll, **no_kick)
    elif 52 <= t < 52.5:                                # the breath before the drop
        a.update(rms=0.005, bass_raw=0.0, bass=0.0, high_raw=0.0, level=0.1, hit_ago=99.0, **no_kick)
    return a


def _run(director: ShowDirector, seconds: float = 100.0, strobe: bool = True, intensity: float = 0.7):
    """The frames the director gives for the whole song, each as a copy."""
    frames, t = [], 0.0
    while t < seconds:
        frames.append(copy.copy(director.update(_analysis(t), t, intensity, strobe)))
        t += DT
    return frames


def _director(colors=None, style: str = "edm", seed: int = 7) -> ShowDirector:
    d = ShowDirector(_Genre(style))
    d._rng = random.Random(seed)
    if colors is not None:
        d.set_colors(colors)
    return d


def _is_blend(c: np.ndarray, palette: list[np.ndarray], tol: float = 1e-4) -> bool:
    """c is made of your colors only: one of them, or a mix of them (a crossfade; a crossfade
    cut short by the next phrase starts from the mix it had reached, so it can be a mix of three)."""
    m = np.vstack([np.stack(palette, axis=1), np.ones(len(palette))])      # colors as columns, plus sum = 1
    w, *_ = np.linalg.lstsq(m, np.append(c, 1.0), rcond=None)
    return bool(np.abs(m @ w - np.append(c, 1.0)).max() < tol and w.min() > -tol)


class MyColorsTests(unittest.TestCase):
    def test_the_song_has_the_big_moments(self):
        """The synthetic song really has a break, a build-up and a drop (or the rest proves little)."""
        frames = _run(_director())
        sections = {f.section for f in frames}
        self.assertTrue({"groove", "break", "build", "drop"} <= sections, sections)
        self.assertTrue(any(f.flash > 0.9 for f in frames))
        self.assertTrue(any(f.flash_color is show.WHITE for f in frames))

    def test_every_color_is_one_of_yours(self):
        palette = [led_color(c) for c in MINE]
        for f in _run(_director(MINE), strobe=False):
            for name in ("main", "accent", "flash_color", "white"):
                self.assertTrue(_is_blend(getattr(f, name), palette), (f.t, name, getattr(f, name)))
            for c in f.colors:
                self.assertTrue(any(np.allclose(c, p) for p in palette))

    def test_with_strobes_only_the_blinder_is_white(self):
        palette = [led_color(c) for c in MINE]
        frames = _run(_director(MINE), strobe=True)
        self.assertTrue(any(f.flash_color is show.WHITE for f in frames), "the blinder is white with strobes on")
        for f in frames:
            if f.flash_color is not show.WHITE:
                self.assertTrue(_is_blend(f.flash_color, palette))

    def test_no_white_blinder_when_strobes_are_off(self):
        frames = _run(_director(MINE), strobe=False)
        self.assertFalse(any(f.flash_color is show.WHITE for f in frames))
        self.assertTrue(any(f.flash > 0.9 for f in frames), "the big moment still flashes, in your color")

    def test_follow_the_music_still_blinds_white(self):
        """Without your colors the blinder is white whatever the strobe setting says (as before)."""
        frames = _run(_director(), strobe=False)
        self.assertTrue(any(f.flash_color is show.WHITE for f in frames))

    def test_the_choreography_is_the_same(self):
        plain, mine = _run(_director(seed=3)), _run(_director(MINE, seed=3))
        self.assertEqual(len(plain), len(mine))
        for a, b in zip(plain, mine):
            self.assertEqual((a.section, a.scene, a.prev_scene, a.wipe, a.seed, a.half, a.blackout, a.strobe),
                             (b.section, b.scene, b.prev_scene, b.wipe, b.seed, b.half, b.blackout, b.strobe), a.t)
            for name in ("beat", "bpm", "hard", "kick", "pump", "offbeat", "hit", "hat", "level", "bass", "mid",
                         "build", "tension", "flash", "master", "trans"):
                self.assertAlmostEqual(getattr(a, name), getattr(b, name), 9, (a.t, name))
        self.assertGreater(len({f.scene for f in plain}), 3)

    def test_the_chords_do_not_recolor(self):
        plain_d, mine_d = _director(seed=5), _director(MINE, seed=5)
        _run(plain_d), _run(mine_d)
        # Without your colors the chords took over at some point ...
        self.assertGreater(plain_d._harmony_until, 0)
        # ... with them they never did, and the info line says so.
        self.assertEqual(mine_d._harmony_until, 0.0)
        self.assertEqual(mine_d.info()["chord"], "")
        self.assertTrue(mine_d.info()["mine"])
        self.assertFalse(plain_d.info()["mine"])
        # The chords still find the "one" of the bar: the phrase grid is the same.
        self.assertEqual(plain_d._phrase_start, mine_d._phrase_start)

    def test_two_colors_swap_who_is_main(self):
        d = _director(["#ff0000", "#0000ff"])
        red, blue = led_color("#ff0000"), led_color("#0000ff")
        for i in range(8):
            main, accent = d._pair(show.STYLES["edm"], i)
            want = (red, blue) if i % 2 == 0 else (blue, red)
            self.assertTrue(np.allclose(main, want[0]) and np.allclose(accent, want[1]), i)
        seen = {tuple(np.round(f.main, 3)) for f in _run(d, 60.0)}
        self.assertTrue({tuple(np.round(red, 3)), tuple(np.round(blue, 3))} <= seen)

    def test_each_phrase_gets_a_different_pair(self):
        d = _director(MINE)
        pairs = [d._pair(show.STYLES["edm"], i) for i in range(6)]
        for main, accent in pairs:
            self.assertFalse(np.allclose(main, accent))
        self.assertEqual(len({tuple(p[0]) for p in pairs[:3]}), 3, "each color takes a turn as main")
        self.assertEqual(len({(tuple(p[0]), tuple(p[1])) for p in pairs[:6]}), 6, "and no pair comes back at once")

    def test_the_render_has_no_other_hue(self):
        """Red and blue only: with the strobe off nothing the lights draw has any green in it."""
        d = _director(["#ff0000", "#0000ff"], style="pop")
        mem: dict = {}
        for f in _run(d, strobe=False):
            for span in ((0.0, 1.0, "front"), (0.0, 1.0, "back")):
                px = show.render(f, 12, span, mem)
                self.assertEqual(max(p[1] for p in px), 0, (f.t, f.scene, f.section))

    def test_colors_set_while_the_show_runs_glide_in(self):
        d = _director()
        t, last = 0.0, None
        while t < 20:
            last = d.update(_analysis(t), t, 0.7, False)
            t += DT
        before = last.main.copy()
        d.set_colors(["#ff0000", "#0000ff"])
        f = d.update(_analysis(t), t, 0.7, False)
        self.assertTrue(np.allclose(f.main, before, atol=0.1), "no jump on the frame it changes")
        for _ in range(int(2 * BEAT / DT) + 1):
            t += DT
            f = d.update(_analysis(t), t, 0.7, False)
        self.assertTrue(_is_blend(f.main, [led_color("#ff0000"), led_color("#0000ff")]))
        d.set_colors([])
        self.assertIsNone(d._mine)

    def test_follow_the_music_is_unchanged(self):
        """With no colors set, or none valid, the frames are the ones the show always gave."""
        base = _run(_director(seed=11))
        for colors in (None, [], ["#ff0000"], ["nope", "#00ff00"]):
            other = _run(_director(colors, seed=11))
            for a, b in zip(base, other):
                for name in ("main", "accent", "flash_color", "white"):
                    self.assertTrue(np.array_equal(getattr(a, name), getattr(b, name)), (a.t, name))
                self.assertEqual(a.scene, b.scene)
        self.assertTrue(np.array_equal(base[0].white, show.WHITE))


class LiftTests(unittest.TestCase):
    def test_a_dark_pick_is_brightened_but_keeps_its_hue(self):
        c = led_color("#10002a")
        self.assertAlmostEqual(float(c.max()), show.DARKEST_PEAK, 5)
        self.assertGreater(c[2], c[0] * 2)
        self.assertEqual(float(c[1]), 0.0)

    def test_vivid_and_light_picks_stay_as_they_are(self):
        for h in ("#ff8800", "#00d9ff", "#ff3ca0", "#ffd000"):
            self.assertTrue(np.allclose(led_color(h), show._c(h), atol=0.01), h)

    def test_a_grey_becomes_a_soft_white(self):
        grey = led_color("#555555")
        self.assertGreater(grey[0], grey[2])                     # warm, not grey
        self.assertGreater(float(grey.max()), 0.5)
        self.assertTrue(np.allclose(led_color("#f5f5f5"), show.WHITE))
        self.assertGreater(float(led_color("#000000").max()), 0.5)

    def test_the_flash_is_the_lightest_color(self):
        d = _director(["#2a00ff", "#ffee00", "#ff0000"])
        self.assertTrue(np.allclose(d._lightest, led_color("#ffee00")))


class ValidationTests(unittest.TestCase):
    def test_good_input(self):
        self.assertEqual(clean_show_colors(None), [])
        self.assertEqual(clean_show_colors([]), [])
        self.assertEqual(clean_show_colors(["#FF0000", "#00ff00"]), ["#ff0000", "#00ff00"])
        self.assertEqual(len(clean_show_colors(["#111111", "#222222", "#333333", "#444444", "#555555"])), 5)

    def test_bad_input_is_refused(self):
        for bad in (["#ff0000"], ["#ff0000", "#ff0000"], ["#ff0000", "red"], ["#ff0000", "#00ff0"],
                    ["#ff0000", "#00ff00", "#0000ff", "#111111", "#222222", "#333333"], "#ff0000", 5,
                    {"a": "#ff0000"}, ["#ff0000", None]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                clean_show_colors(bad)

    def test_a_damaged_setting_follows_the_music(self):
        d = _director()
        d.set_colors(["#ff0000"])
        self.assertIsNone(d._mine)
        d.set_colors(12)
        self.assertIsNone(d._mine)


if __name__ == "__main__":
    unittest.main()
