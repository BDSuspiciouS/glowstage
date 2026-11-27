import unittest

from glowstage.devices.base import Device
from glowstage.show import stage_spans


class Thing(Device):
    def __init__(self, dev_id, kind, group="pc", desk="", pixels=10):
        super().__init__(dev_id, dev_id)
        self.kind, self.group, self._desk, self.pixel_count = kind, group, desk, pixels

    def desk_item(self):
        return self._desk


class Stage(unittest.TestCase):
    def setUp(self):
        self.devices = [
            Thing("keyboard", "qmk", desk="KEYBOARD"),
            Thing("mousepad", "openrgb", desk="MOUSEMAT"),
            Thing("mouse", "openrgb", desk="MOUSE"),
            Thing("gpu", "asrock_gpu"), Thing("fans", "openrgb"), Thing("ram", "openrgb"),
            Thing("strip", "wled", group="room", pixels=120),
            Thing("panels", "nanoleaf", group="room"),
        ]
        self.spans = stage_spans(self.devices)

    def test_the_desk_in_front_of_the_monitor(self):
        self.assertEqual(self.spans["keyboard"], (0.22, 0.52, "front"))
        start, end, _ = self.spans["mousepad"]
        self.assertLess(start, 0.3)
        self.assertGreater(end, 0.6)
        self.assertTrue(0.52 < self.spans["mouse"][0] < 0.66)

    def test_the_case_on_the_right_without_the_desk(self):
        case = sorted(self.spans[i][:2] for i in ("fans", "gpu", "ram"))
        self.assertEqual(case[0][0], 0.72)
        self.assertAlmostEqual(case[-1][1], 1.0)

    def test_the_rest_as_before(self):
        self.assertEqual(self.spans["strip"], (0.0, 0.68, "front"))
        self.assertEqual(self.spans["panels"], (0.0, 1.0, "back"))

    def test_your_own_places_win(self):
        spans = stage_spans(self.devices, {"mousepad": [0.9, 1.0, "back"]})
        self.assertEqual(spans["mousepad"], (0.9, 1.0, "back"))


if __name__ == "__main__":
    unittest.main()
