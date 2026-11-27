import unittest

import numpy as np

from glowstage.effects import EFFECTS
from glowstage.impacts import Impacts
from glowstage.state import LightState

RATE, BLOCK = 48000, 1024
rng = np.random.default_rng(3)


def burst(seconds, decay, level, smooth=0):
    n = int(seconds * RATE)
    x = rng.standard_normal(n)
    if smooth:
        x = np.convolve(x, np.ones(smooth) / smooth, mode="same") * np.sqrt(smooth)
    return x * np.exp(-np.arange(n) / (decay * RATE)) * level


class Scene:
    """Ten seconds of a game: wind, a music bed with kicks and hats, footsteps, and shots."""

    def __init__(self):
        n = 10 * RATE
        self.left, self.right = np.zeros(n), np.zeros(n)
        wind = np.convolve(rng.standard_normal(n), np.ones(200) / 200, mode="same") * 0.15
        self.left += wind
        self.right += wind
        for at in np.arange(0, 10, 0.5):
            self.add(at, 0.08 * np.sin(2 * np.pi * 60 * np.arange(4000) / RATE) * np.exp(-np.arange(4000) / 1500))
            self.add(at + 0.25, burst(0.03, 0.008, 0.02))                      # hi-hat
        for at in np.arange(1.0, 4.0, 0.45):
            self.add(at, burst(0.04, 0.012, 0.025, smooth=6), 1.0, 0.6)          # footsteps
        self.shots = []

    def add(self, at, sound, gl=1.0, gr=1.0):
        s = int(at * RATE)
        self.left[s:s + len(sound)] += sound * gl
        self.right[s:s + len(sound)] += sound * gr

    def shot(self, at, side, level=0.6):
        self.add(at, burst(0.25, 0.05, level), *((1.0, 0.3) if side < 0 else (0.3, 1.0) if side > 0 else (1.0, 1.0)))
        self.shots.append((at, side))

    def run(self):
        d, found = Impacts(BLOCK / RATE), []
        audio = np.stack([self.left, self.right], axis=1).astype(np.float32)
        for i in range(0, len(audio) - BLOCK, BLOCK):
            if d.feed(audio[i:i + BLOCK], i / RATE):
                found.append(d._hits[-1])
        return found


class Detecting(unittest.TestCase):
    def test_shots_and_their_side(self):
        s = Scene()
        s.shot(5.0, -1)
        s.shot(6.5, +1)
        s.shot(8.0, 0)
        s.shot(9.2, -1, level=0.2)        # far away
        found = s.run()
        self.assertEqual(len(found), 4, found)
        for (at, side), (t, strength, pan) in zip(s.shots, found):
            self.assertAlmostEqual(t, at, delta=0.05)
            self.assertEqual(np.sign(round(pan, 1)), side)
            self.assertGreater(strength, 0.3)

    def test_footsteps_hats_and_kicks_dont_count(self):
        self.assertEqual(Scene().run(), [])

    def test_ages_and_forgetting(self):
        d = Impacts(BLOCK / RATE)
        quiet = np.zeros((BLOCK, 2), np.float32)
        for k in range(10):
            d.feed(quiet, k * 0.02)
        loud = (rng.standard_normal((BLOCK, 2)) * 0.5).astype(np.float32)
        self.assertTrue(d.feed(loud, 0.2))
        self.assertAlmostEqual(d.recent(0.5)[0][0], 0.3)
        self.assertEqual(d.recent(1.5), [])


class Flashing(unittest.TestCase):
    class Shot:
        def colors(self, a, b, n):
            return np.tile([20.0, 40.0, 120.0], (n, 1))

    def frame(self, impacts, layer="front", n=10):
        ctx = {"screen": self.Shot(), "span": (0.0, 1.0, layer), "audio": {"impacts": impacts}}
        return EFFECTS["screen_sound"].render(0.0, n, LightState(), {}, ctx)

    def test_the_screen_when_its_quiet(self):
        self.assertEqual(set(self.frame([])), {(20, 40, 120)})

    def test_a_shot_on_the_left_lights_the_left(self):
        out = self.frame([(0.02, 1.0, -0.6)])
        self.assertGreater(sum(out[0]), sum(out[-1]) + 150)

    def test_the_flash_is_short(self):
        self.assertLess(sum(self.frame([(0.5, 1.0, 0.0)])[5]) - 180, 20)

    def test_lights_behind_you_flash_whatever_the_side(self):
        left, right = self.frame([(0.02, 1.0, -1.0)], "back"), self.frame([(0.02, 1.0, 1.0)], "back")
        self.assertEqual(left, right)
        self.assertGreater(sum(left[0]), 300)


if __name__ == "__main__":
    unittest.main()
