import time
import unittest

import numpy as np

from glowstage.effects import EFFECTS
from glowstage.state import LightState

LOOKS = ("candle", "ocean", "lava", "storm")
FPS = 30
STAGE = {"flow": True, "span": (0.0, 1.0, "front")}


def state(speed=50):
    return LightState().patched({"speed": speed})


def frames(fx, seconds, n=8, speed=50, ctx=None, mem=None):
    mem = {} if mem is None else mem
    st = state(speed)
    return np.array([EFFECTS[fx].render(i / FPS, n, st, mem, ctx or {}) for i in range(int(seconds * FPS))], float)


class Basics(unittest.TestCase):
    def test_registered_as_animations(self):
        for fx in LOOKS:
            self.assertEqual(EFFECTS[fx].group, "animation", fx)
            self.assertIn("speed", EFFECTS[fx].uses, fx)

    def test_flowing(self):
        self.assertEqual({fx for fx in LOOKS if EFFECTS[fx].flows}, {"ocean", "lava", "storm"})

    def test_n_colors_in_range(self):
        for fx in LOOKS:
            for n in (0, 1, 300):
                for t in (0.0, 3.3, 61.7):
                    out = EFFECTS[fx].render(t, n, state(), {}, {})
                    self.assertEqual(len(out), n, fx)
                    for c in out:
                        self.assertEqual(len(c), 3)
                        self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in c), (fx, c))

    def test_same_t_same_colors(self):
        for fx in LOOKS:
            mem = {}
            a = EFFECTS[fx].render(7.25, 50, state(), mem, {})
            EFFECTS[fx].render(30.0, 50, state(), mem, {})
            self.assertEqual(a, EFFECTS[fx].render(7.25, 50, state(), mem, {}), fx)

    def test_speed_extremes(self):
        for fx in LOOKS:
            for speed in (1, 100):
                self.assertEqual(len(EFFECTS[fx].render(5.5, 20, state(speed), {}, {})), 20)


class Colors(unittest.TestCase):
    def test_candle_stays_warm(self):
        for speed in (1, 50, 100):
            a = frames("candle", 60, n=40, speed=speed)
            self.assertTrue(((a[..., 0] >= a[..., 1]) & (a[..., 1] >= a[..., 2])).all(), speed)
            self.assertGreater(a[..., 0].min(), 40)

    def test_candle_flickers_per_led_without_jumps(self):
        a = frames("candle", 20, n=10)[..., 0]
        self.assertGreater(a.std(axis=0).min(), 5)                  # every flame moves
        self.assertGreater(np.abs(a[:, 0] - a[:, 1]).mean(), 5)     # and on its own
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 140)      # a gust is quick but not a jump

    def test_ocean_is_blue_and_teal(self):
        for ctx in ({}, {"flow": True, "span": (0.2, 0.6, "front")}):
            a = frames("ocean", 40, n=60, ctx=ctx)
            self.assertTrue((a[..., 2] >= a[..., 0]).all())
            self.assertGreater(a[..., 2].mean(), a[..., 0].mean() + 40)

    def test_lava_is_warm_with_a_dark_background(self):
        a = frames("lava", 30, n=60)
        self.assertTrue((a[..., 0] >= a[..., 1]).all())
        self.assertLess(np.percentile(a.sum(axis=-1), 10), 120)     # a dark background
        self.assertGreater(a[..., 0].max(), 200)                    # and bright blobs


def flashes_in(a, level=100):
    """When a flash starts: the frames where the brightest LED's blue passes `level` from below."""
    peak = a[..., 2].max(axis=1)
    return [i / FPS for i in range(1, len(peak)) if peak[i] >= level > peak[i - 1]]


def strike_starts(flashes):
    """The first flash of every strike (the flashes of one strike are within half a second)."""
    return [f for i, f in enumerate(flashes) if i == 0 or f - flashes[i - 1] > 1.0]


class StormTest(unittest.TestCase):
    def test_strikes_gaps_and_flash_rate(self):
        for speed in (1, 50, 100):
            a = frames("storm", 120, n=12, speed=speed)
            flashes = flashes_in(a)
            starts = strike_starts(flashes)
            self.assertGreaterEqual(len(starts), 4, speed)
            self.assertGreaterEqual(np.diff(starts).min(), 4.0 - 1 / FPS, speed)
            for f in flashes:   # never more than three flashes in any second
                self.assertLessEqual(sum(1 for g in flashes if f <= g < f + 1.0), 3, speed)

    def test_slowest_is_rarer_than_fastest(self):
        slow = len(strike_starts(flashes_in(frames("storm", 120, speed=1))))
        fast = len(strike_starts(flashes_in(frames("storm", 120, speed=100))))
        self.assertLess(slow, fast)

    def test_dim_between_strikes(self):
        a = frames("storm", 120, n=12)
        bright = a.max(axis=(1, 2))
        self.assertGreater(bright.max(), 200)
        self.assertLess(np.median(bright), 60)
        self.assertGreater(a[..., 2].max(axis=1).min(), 5)          # clouds are never black
        self.assertTrue((a[..., 2] >= a[..., 0]).all())             # cold

    def test_every_light_hears_the_same_storm(self):
        m1, m2 = {}, {}
        frames("storm", 40, n=5, mem=m1)
        frames("storm", 40, n=9, mem=m2)
        self.assertEqual(m1["strikes"][:4], m2["strikes"][:4])

    def test_flow_strike_is_brightest_at_its_place(self):
        n, mem, st = 100, {}, state()
        # the moment of the brightest flash, then the whole stage at that moment
        peak_t = max((i / FPS for i in range(120 * FPS)), key=lambda t: max(EFFECTS["storm"].render(t, 4, st, mem, {})[0]))
        fresh = {}      # (a fresh look: the one that ran on keeps only the last strikes)
        EFFECTS["storm"].render(peak_t, 4, st, fresh, {})
        place = next(s["place"] for s in fresh["strikes"] if s["at"] <= peak_t < s["at"] + 1.5)
        lum = np.array(EFFECTS["storm"].render(peak_t, n, st, fresh, STAGE), float).sum(axis=1)
        self.assertAlmostEqual((np.argmax(lum) + 0.5) / n, place, delta=0.06)
        self.assertGreater(lum.max(), lum.min() + 100)
        quiet = np.array(EFFECTS["storm"].render(1.0, n, st, {}, STAGE), float).sum(axis=1)
        self.assertGreater(lum.min(), quiet.min())                  # the rest of the stage lights up a little

    def test_a_storm_left_on_keeps_only_the_last_strikes(self):
        mem, st = {}, state()
        for i in range(0, 3600 * 4, 3):            # an hour, a frame every 0.75 s
            EFFECTS["storm"].render(i / 4, 3, st, mem, {})
        self.assertLessEqual(len(mem["strikes"]), 3)
        self.assertGreater(mem["made"], 200)

    def test_slow_lights_hold_a_strike(self):
        def longest(ctx):
            on = frames("storm", 120, n=4, ctx=ctx)[..., 2].max(axis=1) >= 150
            best = run = 0
            for v in on:
                run = run + 1 if v else 0
                best = max(best, run)
            return best / FPS
        self.assertGreaterEqual(longest({"fps": 10}), 0.25)
        self.assertLess(longest({"fps": 30}), 0.25)

    def test_slow_lights_one_flash_per_strike(self):
        flashes = flashes_in(frames("storm", 120, n=4, ctx={"fps": 5}), 150)
        self.assertGreaterEqual(np.diff(flashes).min(), 4.0 - 1 / FPS)


class Speed(unittest.TestCase):
    def test_frame_time_for_300_leds(self):
        for fx in LOOKS:
            for ctx in ({}, STAGE):
                mem, st = {}, state()
                EFFECTS[fx].render(0.0, 300, st, mem, ctx)
                start = time.perf_counter()
                for i in range(300):
                    EFFECTS[fx].render(i / FPS, 300, st, mem, ctx)
                ms = (time.perf_counter() - start) / 300 * 1000
                print(f"{fx} flow={bool(ctx)}: {ms:.3f} ms/frame")
                self.assertLess(ms, 2.0, fx)


if __name__ == "__main__":
    unittest.main()
