import time
import unittest

import numpy as np

from glowstage.effects import BULBS, CANE_RED, CANE_WHITE, EFFECTS
from glowstage.state import LightState

LOOKS = ("snow", "string_lights", "candy_cane")
FPS = 30
STAGE = {"flow": True, "span": (0.0, 1.0, "front")}
MAX_JUMP = 140      # the brightest change between two frames the Candle is allowed (test_ambient); these stay well under it


def state(speed=50, **extra):
    return LightState().patched({"speed": speed, **extra})


def frames(fx, seconds, n=8, speed=50, ctx=None, mem=None, start=0.0, **extra):
    mem = {} if mem is None else mem
    st = state(speed, **extra)
    return np.array([EFFECTS[fx].render(start + i / FPS, n, st, mem, ctx or {}) for i in range(int(seconds * FPS))], float)


def one(fx, t, n, mem=None, speed=50, ctx=None, **extra):
    return np.array(EFFECTS[fx].render(t, n, state(speed, **extra), {"seed": 7} if mem is None else mem, ctx or {}), float)


def shift_of(a, b, span=8):
    """How many LEDs the picture moved from a to b (positive: towards the end of the light)."""
    a, b = a.sum(axis=1), b.sum(axis=1)
    a, b = a - a.mean(), b - b.mean()
    return max(range(-span, span + 1), key=lambda s: float((np.roll(a, s) * b).sum()))


def nearest_bulb(rgb):
    """Which of the four bulb colors (red, green, gold, blue) a color is, whatever its brightness."""
    c = np.asarray(rgb, float) / max(float(np.max(rgb)), 1.0)
    ref = BULBS / BULBS.max(axis=1, keepdims=True)
    return int(np.argmin(((ref - c) ** 2).sum(axis=1)))


class Basics(unittest.TestCase):
    def test_registered_as_animations(self):
        for fx in LOOKS:
            self.assertEqual(EFFECTS[fx].group, "animation", fx)
            self.assertIn("speed", EFFECTS[fx].uses, fx)
        self.assertEqual(EFFECTS["snow"].name, "Snowfall")
        self.assertEqual(EFFECTS["string_lights"].name, "Holiday lights")
        self.assertEqual(EFFECTS["candy_cane"].uses, ("speed", "color", "color2"))

    def test_flowing(self):
        self.assertEqual({fx for fx in LOOKS if EFFECTS[fx].flows}, set(LOOKS))

    def test_n_colors_in_range(self):
        for fx in LOOKS:
            for ctx in ({}, STAGE):
                for n in (0, 1, 2, 9, 10, 23, 24, 300):
                    for t in (0.0, 3.3, 61.7):
                        out = EFFECTS[fx].render(t, n, state(), {}, ctx)
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
                for n in (1, 20):
                    self.assertEqual(len(EFFECTS[fx].render(5.5, n, state(speed), {}, {})), n)

    def test_one_stage_across_lights(self):
        """With Flow two lights side by side show what one light along the whole stage would."""
        left, right = {"flow": True, "span": (0.0, 0.5, "front")}, {"flow": True, "span": (0.5, 1.0, "front")}
        for fx in LOOKS:
            for t in (0.0, 4.2, 40.0):
                whole = one(fx, t, 100, ctx=STAGE)
                halves = np.concatenate([one(fx, t, 50, ctx=left), one(fx, t, 50, ctx=right)])
                self.assertTrue(np.array_equal(whole, halves), (fx, t))

    def test_no_jumps_between_frames(self):
        worst = {}
        for fx in LOOKS:
            for ctx in ({}, STAGE):
                for speed in (1, 50, 100):
                    for n in (1, 8, 60, 300):
                        a = frames(fx, 30, n=n, speed=speed, ctx=ctx, mem={"seed": 7})
                        jump = float(np.abs(np.diff(a, axis=0)).max())
                        worst[fx] = max(worst.get(fx, 0), jump)
                        self.assertLess(jump, MAX_JUMP, (fx, bool(ctx), speed, n))
        print("brightest change between two frames:", {k: round(v) for k, v in worst.items()})


class SnowTest(unittest.TestCase):
    def test_a_winter_night_with_white_flakes(self):
        for ctx in ({}, STAGE):
            a = frames("snow", 60, n=60, ctx=ctx, mem={"seed": 7})
            self.assertTrue((a[..., 2] >= a[..., 0]).all())               # never warm
            self.assertTrue((a[..., 2] >= a[..., 1]).all())
            self.assertLess(np.median(a[..., 2]), 80)                      # mostly the dark night
            self.assertGreater(np.median(a[..., 2]), 25)                   # and never black
            self.assertLess(np.median(a[..., 0]), 25)
            self.assertGreater(a.sum(axis=-1).max(), 600)                  # with white flakes in it

    def test_flakes_fall_towards_the_start(self):
        for seed in (7, 123):
            for t in (3.0, 50.0):
                a, b = one("snow", t, 200, {"seed": seed}), one("snow", t + 0.5, 200, {"seed": seed})
                self.assertLess(shift_of(a, b, 12), 0, (seed, t))

    def test_flakes_differ_in_speed_and_size(self):
        mem = {"seed": 7}
        one("snow", 0.0, 50, mem)
        f = mem["flakes"]
        self.assertGreater(np.ptp(f["fall"]), 0.02)
        self.assertGreater(np.ptp(f["sigma"]), 0.005)
        self.assertGreater(np.ptp(f["amp"]), 0.2)

    def test_a_flake_now_and_then_catches_the_light(self):
        a = frames("snow", 60, n=60, mem={"seed": 7})
        self.assertGreater(a[..., 0].max(), 225)       # a plain flake stops at 206: only a glint is whiter
        quiet = a[..., 0] > 225
        self.assertLess(quiet.mean(), 0.02)             # it is a moment, not the look

    def test_a_bulb_is_icy_blue_and_brightens_as_a_flake_passes(self):
        a = frames("snow", 120, n=1, mem={"seed": 7})[:, 0]
        self.assertTrue((a[:, 2] >= a[:, 1]).all() and (a[:, 1] >= a[:, 0]).all())
        self.assertGreater(a[:, 2].min(), 90)           # a calm blue between flakes
        self.assertGreater(a[:, 2].max(), a[:, 2].min() + 40)
        self.assertGreater(a[:, 0].std(), 5)

    def test_speed_sets_how_fast_it_snows(self):
        def moved(speed):
            a = frames("snow", 20, n=100, speed=speed, mem={"seed": 7})
            return np.abs(a[15:] - a[:-15]).mean()
        self.assertLess(moved(1), moved(50))
        self.assertLess(moved(50), moved(100))

    def test_the_same_seed_gives_the_same_snow(self):
        a, b, c = {"seed": 7}, {"seed": 7}, {"seed": 8}
        for t in (1.0, 20.0, 99.5):
            self.assertTrue(np.array_equal(one("snow", t, 60, a), one("snow", t, 60, b)))
        self.assertFalse(np.array_equal(one("snow", 20.0, 60, a), one("snow", 20.0, 60, c)))
        m1, m2 = {}, {}                             # lights with no seed of their own share the run's
        frames("snow", 2, n=5, mem=m1)
        frames("snow", 2, n=9, mem=m2)
        self.assertEqual(m1["seed"], m2["seed"])
        for key in ("start", "fall", "sigma", "amp", "period", "offset"):
            self.assertTrue(np.array_equal(m1["flakes"][key], m2["flakes"][key]), key)

    def test_flakes_cross_from_light_to_light(self):
        """The flake that leaves one light enters the one before it."""
        left, right = {"flow": True, "span": (0.0, 0.5, "front")}, {"flow": True, "span": (0.5, 1.0, "front")}
        edges = []
        for t in np.arange(0.0, 90.0, 1.0):
            r, l = one("snow", t, 20, ctx=right).sum(axis=1), one("snow", t, 20, ctx=left).sum(axis=1)
            edges.append((r[:3].max(), l[-3:].max()))
        self.assertGreater(max(e[1] for e in edges), 300)       # a flake near the end of the left light, ...
        self.assertGreater(max(e[0] for e in edges), 300)       # ... and one at the start of the right


class StringLightsTest(unittest.TestCase):
    def test_the_four_colors_in_order(self):
        a = frames("string_lights", 20, n=60, mem={"seed": 7})
        for frame in (a[0], a[300], a[599]):
            for bulb in range(10):
                self.assertEqual(nearest_bulb(frame[bulb * 6 + 2]), bulb % 4, bulb)
        self.assertEqual([nearest_bulb(c) for c in BULBS], [0, 1, 2, 3])

    def test_dark_gaps_between_bulbs(self):
        a = frames("string_lights", 20, n=60, mem={"seed": 7})
        for k in range(10):
            self.assertTrue((a[:, k * 6 + 4:k * 6 + 6] == 0).all(), k)        # two dark LEDs after each bulb
            self.assertGreater(a[:, k * 6 + 1:k * 6 + 3].sum(axis=-1).min(), 100)
        mid = frames("string_lights", 5, n=12, mem={"seed": 7})
        self.assertTrue((mid.sum(axis=-1) == 0).any())                        # a 12-LED light has gaps too
        short = frames("string_lights", 20, n=6, mem={"seed": 7})
        self.assertGreater(short.sum(axis=-1).min(), 100)                     # a very short one is a bulb per LED, no gaps

    def test_every_bulb_twinkles_slowly_on_its_own(self):
        a = frames("string_lights", 60, n=60, mem={"seed": 7}).max(axis=-1)
        lit = a[:, [k * 6 + 2 for k in range(10)]]
        self.assertGreater(lit.std(axis=0).min(), 4)                          # each one moves
        self.assertGreater(np.abs(lit[:, 0] - lit[:, 1]).mean(), 5)           # and not together
        self.assertGreater(lit.min(), 0.45 * lit.max())                       # a dim, never out
        self.assertLess(np.abs(np.diff(lit, axis=0)).max(), 12)               # and slowly

    def test_speed_sets_how_lively_the_twinkle_is(self):
        def lively(speed):
            lit = frames("string_lights", 40, n=60, speed=speed, mem={"seed": 7}).max(axis=-1)[:, 2::6]
            return np.abs(np.diff(lit, axis=0)).mean()
        self.assertLess(lively(1), lively(50))
        self.assertLess(lively(50), lively(100))

    def test_a_bulb_cycles_through_the_four_colors(self):
        a = frames("string_lights", 60, n=1, mem={"seed": 7})[:, 0]
        seen = {nearest_bulb(c) for c in a[::3]}
        self.assertEqual(seen, {0, 1, 2, 3})
        for k in range(8):      # (speed 50: a color every 6.7 s, held for the first half of it)
            self.assertEqual(nearest_bulb(one("string_lights", (k + 0.25) / 0.15, 1)[0]), k % 4, k)

    def test_with_flow_the_string_runs_on_across_the_stage(self):
        a = one("string_lights", 3.0, 200, ctx=STAGE)
        picks = [nearest_bulb(c) for c in a if c.sum() > 120]
        self.assertEqual(set(picks), {0, 1, 2, 3})
        bulbs = (a.sum(axis=1) > 0).astype(int)
        self.assertGreater(np.abs(np.diff(bulbs)).sum(), 20)                  # many bulbs, with gaps between


class CandyCaneTest(unittest.TestCase):
    def test_red_and_white_by_default(self):
        a = frames("candy_cane", 20, n=48)
        self.assertGreater(a[..., 0].min(), 215)                              # always red or white
        self.assertLess(a[..., 1].min(), 40)                                  # red stripes
        self.assertGreater(a[..., 1].max(), 235)                              # white stripes
        near = lambda c, ref: np.abs(a - np.array(ref, float)).sum(axis=-1) < 24      # noqa: E731
        self.assertGreater(near(a, CANE_RED).mean(), 0.25)
        self.assertGreater(near(a, CANE_WHITE).mean(), 0.25)

    def test_uses_the_colors_picked(self):
        a = frames("candy_cane", 20, n=48, color="#00b43c", color2="#ffffff")
        self.assertLess(a[..., 0].min(), 20)
        self.assertGreater(a[..., 0].max(), 245)
        self.assertGreater(a[..., 1].min(), 170)                              # green or white, never red or violet
        self.assertTrue(((a[..., 1] >= a[..., 0]) & (a[..., 1] >= a[..., 2])).all())
        b = frames("candy_cane", 5, n=48, color="#ff8800", color2="#0000ff")
        self.assertGreater(b[..., 0].max(), 250)
        self.assertGreater(b[..., 2].max(), 250)
        same = frames("candy_cane", 3, n=20, color="#7c4dff", color2="#ffffff")        # one app color still counts as picked
        self.assertLess(same[..., 0].min(), 130)                              # the violet, not red
        self.assertGreater(same[..., 2].min(), 250)

    def test_stripes_move_slowly_along_the_light(self):
        for t in (2.0, 30.0):
            a, b = one("candy_cane", t, 96), one("candy_cane", t + 1.5, 96)
            self.assertGreater(shift_of(a, b, 4), 0, t)                       # towards the end
            self.assertLess(shift_of(a, b, 4), 4, t)                          # a couple of LEDs in a second and a half

    def test_edges_are_soft(self):
        a = frames("candy_cane", 5, n=300)
        self.assertLess(np.abs(np.diff(a, axis=1)).max(), 150)                # the full step is over 230
        self.assertGreater(np.abs(np.diff(a, axis=1)).max(), 40)              # but still stripes, not a gradient

    def test_speed_sets_how_fast_the_stripes_move(self):
        def moved(speed):
            return np.abs(frames("candy_cane", 3, n=100, speed=speed)[:-15] - frames("candy_cane", 3, n=100, speed=speed)[15:]).mean()
        self.assertLess(moved(1), moved(50))
        self.assertLess(moved(50), moved(100))

    def test_a_bulb_fades_between_the_two_colors(self):
        a = frames("candy_cane", 40, n=1)[:, 0]
        self.assertLess(a[:, 1].min(), 40)
        self.assertGreater(a[:, 1].max(), 235)
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 20)                 # a fade, never a cut

    def test_every_light_shows_the_same_stripes_at_once(self):
        a =EFFECTS["candy_cane"].render(12.5, 40, state(), {}, {})
        self.assertEqual(a, EFFECTS["candy_cane"].render(12.5, 40, state(), {"seed": 99}, {}))


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
