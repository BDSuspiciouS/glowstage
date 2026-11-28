import time
import unittest

import numpy as np

from glowstage.effects import EFFECTS, SUN_GLOW
from glowstage.state import LightState

LOOKS = ("fireflies", "rain", "plasma", "sunset")
FPS = 30
STAGE = {"flow": True, "span": (0.0, 1.0, "front")}
MAX_JUMP = 60       # the brightest change between two frames these may have: under the Candy cane's 67, far under the 140 the holiday looks are held to


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


def moved(fx, speed, **kw):
    """How much a light's picture changes in half a second: a measure of the pace."""
    a = frames(fx, 20, n=100, speed=speed, mem={"seed": 7}, **kw)
    return np.abs(a[15:] - a[:-15]).mean()


class Basics(unittest.TestCase):
    def test_registered_as_animations(self):
        for fx in LOOKS:
            self.assertEqual(EFFECTS[fx].group, "animation", fx)
            self.assertIn("speed", EFFECTS[fx].uses, fx)
        self.assertEqual([EFFECTS[fx].name for fx in LOOKS], ["Fireflies", "Rain", "Plasma", "Sunset"])
        self.assertEqual(EFFECTS["plasma"].uses, ("speed", "color", "color2"))
        for fx in ("fireflies", "rain", "sunset"):
            self.assertEqual(EFFECTS[fx].uses, ("speed",), fx)

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
        """Never a flash: even the quickest picture changes gently from one frame to the next."""
        worst = {}
        for fx in LOOKS:
            for ctx in ({}, STAGE):
                for speed in (1, 50, 100):
                    for n in (1, 8, 60, 300):
                        a = frames(fx, 30, n=n, speed=speed, ctx=ctx, mem={"seed": 7})
                        jump = float(np.abs(np.diff(a, axis=0)).max())
                        worst[fx] = max(worst.get(fx, 0), jump)
                        self.assertLessEqual(jump, MAX_JUMP, (fx, bool(ctx), speed, n))
        print("brightest change between two frames:", {k: round(v) for k, v in worst.items()})

    def test_the_whole_room_shares_the_run_seed(self):
        """Lights with no seed of their own take the run's, so they all show the same picture."""
        for fx in LOOKS:
            a, b = {}, {}
            frames(fx, 1, n=5, mem=a)
            frames(fx, 1, n=9, mem=b)
            self.assertEqual(a["seed"], b["seed"], fx)
            self.assertEqual(EFFECTS[fx].render(12.5, 40, state(), {}, {}), EFFECTS[fx].render(12.5, 40, state(), {"seed": a["seed"]}, {}), fx)

    def test_a_seed_gives_one_picture_and_another_seed_another(self):
        for fx in LOOKS:
            a, b, c = {"seed": 7}, {"seed": 7}, {"seed": 8}
            for t in (1.0, 20.0, 99.5):
                self.assertTrue(np.array_equal(one(fx, t, 60, a), one(fx, t, 60, b)), fx)
            self.assertFalse(np.array_equal(one(fx, 20.0, 60, a), one(fx, 20.0, 60, c)), fx)

    def test_speed_sets_the_pace(self):
        for fx in LOOKS:
            self.assertLess(moved(fx, 1), moved(fx, 50), fx)
            self.assertLess(moved(fx, 50), moved(fx, 100), fx)


class FirefliesTest(unittest.TestCase):
    def test_mostly_a_dark_green_blue_night_with_a_few_warm_glows(self):
        for ctx in ({}, STAGE):
            a = frames("fireflies", 90, n=60, ctx=ctx, mem={"seed": 7})
            bright = a.max(axis=-1)
            self.assertLess(np.median(bright), 60, bool(ctx))                  # dark
            self.assertGreater(np.median(bright), 12)                          # but not black
            self.assertGreater(np.median(a[..., 2]), np.median(a[..., 0]) + 10)    # the dusk leans blue-green
            self.assertGreater(np.median(a[..., 1]), np.median(a[..., 0]) + 10)
            glow = bright > 100
            self.assertGreater(glow.mean(), 0.005)                             # there are glows ...
            self.assertLess(glow.mean(), 0.12)                                 # ... a handful, not a lit room
            g = a[glow]
            self.assertTrue(((g[:, 1] >= g[:, 0]) & (g[:, 0] > g[:, 2])).all())    # yellow-green: green, then red, then little blue

    def test_a_glow_lights_up_and_fades_slowly(self):
        a = frames("fireflies", 120, n=60, mem={"seed": 7}).max(axis=-1)
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 12)
        led = int(np.argmax(a.max(axis=0)))
        series = a[:, led]
        self.assertGreater(series.max(), 120)
        lit = np.flatnonzero(series > series.min() + 0.5 * (series.max() - series.min()))
        self.assertGreater(len(lit), 30)                                       # a glow of seconds, not a blink

    def test_every_firefly_has_its_own_rhythm_and_place(self):
        mem = {"seed": 7}
        one("fireflies", 0.0, 50, mem)
        f = mem["fireflies"]
        self.assertGreater(np.ptp(f["period"]), 3.0)
        self.assertGreater(np.ptp(f["duty"]), 0.05)
        self.assertGreater(np.ptp(f["sigma"]), 0.005)
        places = [int(np.argmax(one("fireflies", t, 200, mem).max(axis=1))) for t in (5.0, 25.0, 45.0, 65.0, 85.0)]
        self.assertGreater(len(set(places)), 3)                                # it lights up somewhere new each time

    def test_a_firefly_drifts_a_little_along_the_light(self):
        a = frames("fireflies", 60, n=200, mem={"seed": 7})
        peaks = a.max(axis=-1)
        for t in (10.0, 30.0):
            i = int(t * FPS)
            before, after = int(np.argmax(peaks[i])), int(np.argmax(peaks[i + 20]))
            self.assertLess(abs(after - before), 40, t)                         # it stays where it was, give or take

    def test_a_bulb_glows_softly_now_and_then(self):
        a = frames("fireflies", 120, n=1, mem={"seed": 7})[:, 0]
        bright = a.max(axis=-1)
        self.assertLess(np.median(bright), 70)
        self.assertGreater(bright.max(), 100)
        self.assertGreater(a[:, 1].mean(), a[:, 0].mean())
        self.assertLess(np.abs(np.diff(bright)).max(), 6)


class RainTest(unittest.TestCase):
    def test_a_dark_slate_blue_with_bright_drops(self):
        for ctx in ({}, STAGE):
            a = frames("rain", 60, n=100, ctx=ctx, mem={"seed": 7})
            self.assertTrue(((a[..., 2] >= a[..., 1]) & (a[..., 1] >= a[..., 0])).all(), bool(ctx))     # always cool
            self.assertLess(np.median(a[..., 2]), 80)
            self.assertGreater(np.median(a[..., 2]), 20)
            self.assertGreater(a[..., 2].max(), 190)                            # the drops
            self.assertLess((a[..., 2] > 120).mean(), 0.2)                      # a few, not a downpour

    def test_drops_run_down_towards_the_start(self):
        for seed in (7, 123):
            for t in (3.0, 50.0):
                a, b = one("rain", t, 200, {"seed": seed}), one("rain", t + 0.3, 200, {"seed": seed})
                self.assertLess(shift_of(a, b, 12), 0, (seed, t))

    def test_quicker_and_thinner_than_snow(self):
        mem, snow = {"seed": 7}, {"seed": 7}
        one("rain", 0.0, 50, mem)
        one("snow", 0.0, 50, snow)
        self.assertGreater(mem["drops"]["fall"].min(), snow["flakes"]["fall"].max())
        self.assertLess(mem["drops"]["sigma"].max(), snow["flakes"]["sigma"].max())

    def test_a_drop_is_a_bright_head_with_a_fading_tail(self):
        mem = {"seed": 7}
        one("rain", 0.0, 300, mem)
        mem["drops"]["amp"][1:] = 0.0                  # one drop alone, so the others don't blur what it looks like
        mem["drops"]["splash"][:] = 0.0
        a = one("rain", 3.0, 300, mem).sum(axis=1)
        head = int(np.argmax(a))
        self.assertGreater(a[head], 300)
        tail = a[head + 1:head + 40]
        self.assertTrue(20 < head < 250)
        self.assertGreater(tail[2], tail[20])                                   # fading ...
        self.assertGreater(a[head + 8:head + 24].mean() - np.median(a), 1.5 * (a[head - 24:head - 8].mean() - np.median(a)))    # ... behind the head, not in front of it
        before = a[max(head - 25, 0):max(head - 8, 1)]
        self.assertLess(before.max(), a[head] * 0.5)                            # the head is short

    def test_some_drops_end_in_a_splash(self):
        with_splash = {"seed": 7}
        frames("rain", 1, n=200, mem=with_splash)
        without = {"seed": 7}
        frames("rain", 1, n=200, mem=without)
        without["drops"]["splash"] = np.zeros_like(without["drops"]["splash"])
        a = frames("rain", 60, n=200, mem=with_splash)
        b = frames("rain", 60, n=200, mem=without)
        extra = (a - b).sum(axis=-1)
        self.assertGreater(extra[:, :8].max(), 60)                              # a glow at the start of the light ...
        self.assertEqual(float(np.abs(extra[:, 70:]).max()), 0.0)               # ... and nowhere else
        self.assertGreater((extra[:, :8].max(axis=1) > 20).mean(), 0.05)        # now and then
        self.assertLess((extra[:, :8].max(axis=1) > 20).mean(), 0.5)            # (sometimes, not always)

    def test_a_bulb_is_cool_blue_that_dips_and_brightens_softly(self):
        a = frames("rain", 120, n=1, mem={"seed": 7})[:, 0]
        self.assertTrue((a[:, 2] >= a[:, 1]).all() and (a[:, 1] >= a[:, 0]).all())
        self.assertGreater(a[:, 2].min(), 60)                                   # always a calm blue
        self.assertGreater(a[:, 2].max(), a[:, 2].min() + 60)                   # that moves
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 8)                    # softly

    def test_drops_cross_from_light_to_light(self):
        """The drop that leaves one light enters the one before it."""
        left, right = {"flow": True, "span": (0.0, 0.5, "front")}, {"flow": True, "span": (0.5, 1.0, "front")}
        edges = []
        for t in np.arange(0.0, 60.0, 0.5):
            r, l = one("rain", t, 40, ctx=right).sum(axis=1), one("rain", t, 40, ctx=left).sum(axis=1)
            edges.append((r[:3].max(), l[-3:].max()))
        self.assertGreater(max(e[1] for e in edges), 300)
        self.assertGreater(max(e[0] for e in edges), 300)


class PlasmaTest(unittest.TestCase):
    def test_magenta_violet_and_cyan_by_default(self):
        a = frames("plasma", 60, n=60, mem={"seed": 7})
        self.assertGreater(a[..., 2].min(), 100)                                # magenta, violet and cyan all carry blue
        self.assertGreater(a[..., 0].max(), 200)                                # magenta
        self.assertLess(a[..., 0].min(), 40)                                    # cyan
        self.assertGreater(a[..., 1].max(), 150)
        self.assertLess(a[..., 1].min(), 70)
        violet = (a[..., 0] > 80) & (a[..., 0] < 170) & (a[..., 2] > 200)
        self.assertGreater(violet.mean(), 0.05)                                 # the violet between the two

    def test_uses_the_colors_picked(self):
        a = frames("plasma", 60, n=60, mem={"seed": 7}, color="#ff0000", color2="#00ff00")
        self.assertLess(a[..., 2].max(), 60)                                    # red and green: no blue anywhere
        self.assertGreater(a[..., 0].max(), 150)
        self.assertGreater(a[..., 1].max(), 150)
        b = frames("plasma", 60, n=60, mem={"seed": 7}, color="#ff8800", color2="#0000ff")
        self.assertGreater(b[..., 0].max(), 150)
        self.assertGreater(b[..., 2].max(), 150)
        self.assertLess(b[..., 1].max(), 200)
        d = frames("plasma", 5, n=20, mem={"seed": 7})
        self.assertFalse(np.array_equal(a[:150, :20], d[:, :20]))
        same = frames("plasma", 20, n=40, mem={"seed": 7}, color="#7c4dff", color2="#ffffff")      # one app color still counts as picked
        self.assertGreater(same[..., 1].min(), 0)
        self.assertGreater(same[..., 0].max(), 150)

    def test_smooth_and_slow_and_always_moving(self):
        a = frames("plasma", 30, n=300, mem={"seed": 7})
        self.assertLess(np.abs(np.diff(a, axis=1)).max(), 25)                   # no hard edges along the light
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 20)                   # and no quick changes in time at the usual speed
        self.assertGreater(np.abs(a[60:] - a[:-60]).mean(), 5)                  # but never still
        self.assertGreater(a.max(axis=-1).min(), 100)                           # vivid: never dark

    def test_the_fields_mix_so_the_picture_has_several_colors_at_once(self):
        a = one("plasma", 10.0, 200)
        self.assertGreater(a[..., 0].max() - a[..., 0].min(), 150)
        self.assertGreater(np.abs(np.diff(a[..., 0])).max(), 0.5)


class SunsetTest(unittest.TestCase):
    def test_orange_then_pink_then_violet_blue(self):
        a = frames("sunset", 60, n=60, mem={"seed": 7}).mean(axis=0)
        self.assertTrue(a[:4, 0].min() > 180 and a[:4, 2].max() < 70, a[:4])          # deep orange at the start
        mid = a[27:33]
        self.assertTrue(mid[:, 0].min() > 170 and 80 < mid[:, 2].min() and mid[:, 2].max() < 175, mid)   # pink in the middle
        self.assertTrue(a[-4:, 2].min() > 150 and a[-4:, 0].max() < 130, a[-4:])      # violet-blue at the end
        self.assertGreater(np.corrcoef(np.arange(60), a[:, 2])[0, 1], 0.7)            # blue comes up along the light, red goes down
        self.assertLess(np.corrcoef(np.arange(60), a[:, 0])[0, 1], -0.7)

    def test_a_soft_glow_where_the_sun_sits_low(self):
        a = frames("sunset", 60, n=100, mem={"seed": 7}).mean(axis=0)
        self.assertGreater(a[16, 1], a[45, 1] + 20)                               # warmer and brighter near the orange end
        self.assertGreater(a[16, 1], a[2, 1] + 15)                                # a glow, not only the ramp
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 30)                     # and soft
        self.assertLessEqual(a.max(), SUN_GLOW.max())

    def test_drifts_very_slowly(self):
        a = frames("sunset", 60, n=60, mem={"seed": 7})
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 6)
        self.assertGreater(np.abs(a[-1] - a[0]).max(), 4)                         # but it does move
        self.assertLess(np.abs(a[30:] - a[:-30]).mean(), 8)                       # a second changes next to nothing

    def test_with_flow_one_sky_spans_the_stage(self):
        left, right = {"flow": True, "span": (0.0, 0.3, "front")}, {"flow": True, "span": (0.7, 1.0, "front")}
        a, b = one("sunset", 5.0, 20, ctx=left), one("sunset", 5.0, 20, ctx=right)
        self.assertGreater(a[:, 0].mean(), b[:, 0].mean() + 80)                   # orange on the left, violet-blue on the right
        self.assertGreater(b[:, 2].mean(), a[:, 2].mean() + 80)

    def test_a_bulb_breathes_slowly_through_the_colors(self):
        a = frames("sunset", 120, n=1, mem={"seed": 7})[:, 0]
        self.assertGreater(a[:, 0].max(), 220)                                    # orange ...
        self.assertGreater(a[:, 2].max(), 190)                                    # ... and violet-blue
        pink = (a[:, 0] > 200) & (a[:, 2] > 100) & (a[:, 1] < 90)
        self.assertGreater(pink.mean(), 0.03)                                     # with the pink between
        self.assertLess(np.abs(np.diff(a, axis=0)).max(), 6)
        self.assertGreater(a.sum(axis=-1).min(), 200)                             # never dark


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
