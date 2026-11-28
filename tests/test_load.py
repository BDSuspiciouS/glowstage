import math
import unittest

from glowstage import load
from glowstage.effects import EFFECTS
from glowstage.engine import is_gpu
from glowstage.state import LightState


class FakeGpu:
    def __init__(self, value=None, boom=False):
        self.value, self.boom, self.looks = value, boom, 0

    def read(self):
        self.looks += 1
        if self.boom:
            raise OSError("no counters")
        return self.value


class CpuMath(unittest.TestCase):
    def test_idle_share_of_the_kernel_time_is_not_load(self):
        # 1 s of time on all cores: kernel (which includes idle) 800, user 200, idle 700
        self.assertAlmostEqual(load.cpu_load((0, 0, 0), (700, 800, 200)), 0.30)

    def test_fully_idle_and_fully_busy(self):
        self.assertAlmostEqual(load.cpu_load((10, 20, 10), (110, 120, 10)), 0.0)
        self.assertAlmostEqual(load.cpu_load((10, 20, 10), (10, 60, 70)), 1.0)

    def test_nothing_to_compare_with(self):
        self.assertIsNone(load.cpu_load(None, (1, 2, 3)))
        self.assertIsNone(load.cpu_load((1, 2, 3), (1, 2, 3)))      # no time has passed

    def test_odd_numbers_stay_in_range(self):
        self.assertEqual(load.cpu_load((0, 0, 0), (500, 100, 0)), 0.0)    # idle > total

    def test_gpu_engines_sum_per_card_and_the_busiest_card_counts(self):
        items = [("pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 30.0),
                 ("pid_2_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 25.0),
                 ("pid_3_luid_0x0_0x2_phys_0_eng_0_engtype_3D", 5.0)]
        self.assertAlmostEqual(load.adapter_loads(items), 0.55)
        self.assertEqual(load.adapter_loads([("luid_0x0_0x1_x", 90.0), ("luid_0x0_0x1_y", 80.0)]), 1.0)
        self.assertIsNone(load.adapter_loads([]))


class Watching(unittest.TestCase):
    def setUp(self):
        self.times = iter([(0, 0, 0), (700, 800, 200), (1400, 1600, 400)])
        self.gpu = FakeGpu(0.4)
        self.w = load.LoadWatcher(lambda: next(self.times), self.gpu)

    def test_two_looks_give_the_processor_load(self):
        prev = self.w.sample(None)
        self.assertIsNone(self.w._cpu)                  # one look can't tell yet
        self.w.sample(prev)
        self.assertAlmostEqual(self.w._cpu, 0.30)
        self.assertAlmostEqual(self.w._gpu_load, 0.4)

    def test_a_broken_gpu_reader_gives_none_and_never_raises(self):
        w = load.LoadWatcher(lambda: (0, 0, 0), FakeGpu(boom=True))
        w.sample(None)
        self.assertIsNone(w._gpu_load)

    def test_the_real_reader_fails_quietly(self):
        reader = load.GpuReader()
        reader._open = lambda: (_ for _ in ()).throw(OSError("no counters"))
        self.assertIsNone(reader.read())
        self.assertIsNone(reader.read())                # and doesn't try again

    def test_a_broken_clock_call_never_raises(self):
        def boom():
            raise OSError("x")
        w = load.LoadWatcher(boom, FakeGpu(0.2))
        w.sample(None)
        self.assertIsNone(w._cpu)

    def test_nobody_asking_means_nobody_sampling(self):
        self.assertIsNone(self.w._thread)
        self.assertEqual(self.gpu.looks, 0)
        self.w._wanted_until = 0.0                      # nobody asked: the loop ends at once
        self.w._run()
        self.assertEqual(self.gpu.looks, 0)

    def test_asking_starts_the_thread_and_it_stops_when_asking_stops(self):
        load_every = load.EVERY
        load.EVERY = 0.01
        try:
            self.w.request(0.0)                          # keeps it alive for IDLE seconds of "now"
            self.w._wanted_until = 0.0                   # ... then pretend they all passed
            self.w._thread.join(2)
            self.assertFalse(self.w._thread.is_alive())
            self.assertIsNone(self.w._cpu)               # stale numbers are dropped
        finally:
            load.EVERY = load_every


class Bar(unittest.TestCase):
    def setUp(self):
        self.fx = EFFECTS["pc_load"]
        self.st = LightState().patched({"color": "#00ff00", "color2": "#ff0000"})

    def frame(self, cpu, n=10, gpu=None, mem=None, t=0.0, **ctx):
        return self.fx.render(t, n, self.st, {} if mem is None else mem, {"load": (cpu, gpu), **ctx})

    def test_registered_like_the_other_looks(self):
        d = self.fx.describe()
        self.assertEqual((d["id"], d["name"], d["uses"], d["flows"]), ("pc_load", "PC load", ["color", "color2"], True))

    def test_no_load_is_all_dim_idle_color(self):
        out = self.frame(0.0)
        self.assertEqual(len(set(out)), 1)
        r, g, b = out[0]
        self.assertTrue(0 < g < 60 and r == 0 and b == 0)

    def test_half_load_fills_half_the_leds(self):
        out = self.frame(0.5)
        self.assertEqual(out[:5], [out[0]] * 5)
        self.assertEqual(out[5:], [out[9]] * 5)
        self.assertGreater(out[4][1] + out[4][0], 3 * out[5][1])      # lit part is far brighter
        self.assertGreater(out[0][0], 100)                            # shifted towards the busy color

    def test_full_load_is_the_busy_color(self):
        self.assertEqual(self.frame(1.0), [(255, 0, 0)] * 10)

    def test_the_tip_of_the_bar_is_partly_lit(self):
        out = self.frame(0.55)
        self.assertTrue(out[4][0] > out[5][0] > out[6][0] or out[5][1] > out[6][1])

    def test_flow_makes_one_bar_across_the_stage(self):
        left = self.frame(0.5, flow=True, span=(0.0, 0.5, "front"))
        right = self.frame(0.5, flow=True, span=(0.5, 1.0, "front"))
        self.assertEqual(left, [left[0]] * 10)          # the left light is entirely filled ...
        self.assertGreater(left[0][0], 100)
        self.assertEqual(right, [right[0]] * 10)         # ... the right one entirely dim
        self.assertEqual(right[0][0], 0)

    def test_the_graphics_card_shows_its_own_load(self):
        cpu_light = self.frame(0.0, gpu=1.0)
        self.assertEqual(cpu_light[0][0], 0)
        gpu_light = self.fx.render(0.0, 10, self.st, {}, {"load": (0.0, 1.0), "gpu": True})
        self.assertEqual(gpu_light, [(255, 0, 0)] * 10)

    def test_without_a_gpu_reading_everything_shows_the_processor(self):
        out = self.fx.render(0.0, 10, self.st, {}, {"load": (1.0, None), "gpu": True})
        self.assertEqual(out, [(255, 0, 0)] * 10)

    def test_no_reading_yet_is_calm(self):
        out = self.fx.render(0.0, 4, self.st, {}, {})
        self.assertEqual(len(set(out)), 1)

    def test_it_glides_to_a_new_load(self):
        mem = {}
        self.frame(0.0, mem=mem, t=0.0)
        self.frame(1.0, mem=mem, t=0.033)
        early = mem["level"]
        self.assertTrue(0.05 < early < 0.3)               # one frame later: barely moved
        for k in range(2, 14):                            # ~0.4 s later: nearly there
            self.frame(1.0, mem=mem, t=k * 0.033)
        self.assertTrue(0.85 < mem["level"] < 1.0)
        self.assertTrue(math.isfinite(mem["level"]))

    def test_the_first_frame_starts_at_the_load_without_a_swoop(self):
        mem = {}
        self.frame(0.8, mem=mem, t=100.0)
        self.assertAlmostEqual(mem["level"], 0.8)


class Which(unittest.TestCase):
    def test_which_light_is_the_graphics_card(self):
        card = type("D", (), {"kind": "asrock_gpu"})()
        strip = type("D", (), {"kind": "wled"})()
        gpu = type("D", (), {"kind": "openrgb", "_dev": type("O", (), {"type": type("T", (), {"name": "GPU"})()})()})()
        ram = type("D", (), {"kind": "openrgb", "_dev": type("O", (), {"type": type("T", (), {"name": "DRAM"})()})()})()
        self.assertEqual([is_gpu(d) for d in (card, strip, gpu, ram)], [True, False, True, False])


if __name__ == "__main__":
    unittest.main()
