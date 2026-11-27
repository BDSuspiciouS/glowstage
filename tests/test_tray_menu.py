import unittest
from types import SimpleNamespace

from glowstage.tray import Tray
from tests.test_undo import Base, FakeLight


class Groups(Base):
    def setUp(self):
        super().setUp()
        self.m._register(FakeLight("tower"))
        self.m.devices["tower"].group = "pc"
        self.m.devices["desk"].group = "pc"
        # desk (pc) and lamp (room) and tower (pc) are on at 50
        self.m.update(None, {"on": True, "brightness": 50})

    def on(self, i):
        return self.m.states[i].on

    def bright(self, i):
        return self.m.states[i].brightness


class Brightness(Groups):
    def test_only_lights_that_are_on_change(self):
        self.m.update(["lamp"], {"on": False})
        self.m.set_brightness_on(75)
        self.assertEqual((self.bright("desk"), self.bright("tower")), (75, 75))
        self.assertEqual(self.bright("lamp"), 50)
        self.assertFalse(self.on("lamp"))

    def test_hidden_lights_are_left_alone(self):
        self.m.set_hidden("lamp", True)
        self.m.set_brightness_on(25)
        self.assertEqual(self.bright("lamp"), 50)
        self.assertEqual(self.bright("desk"), 25)

    def test_nothing_on_changes_nothing(self):
        self.m.set_power_all(False)
        self.m.set_brightness_on(10)
        self.assertEqual({self.bright(i) for i in ("desk", "lamp", "tower")}, {50})
        self.assertEqual(self.m.brightness_on(), ())

    def test_undo_brings_back_the_old_brightness(self):
        self.m.update(["desk"], {"brightness": 30})
        self.m.remember()
        self.m.set_brightness_on(100)
        self.assertEqual(self.bright("desk"), 100)
        self.assertTrue(self.m.undo())
        self.assertEqual((self.bright("desk"), self.bright("lamp")), (30, 50))

    def test_brightness_on_lists_the_values(self):
        self.assertEqual(self.m.brightness_on(), (50,))
        self.m.update(["desk"], {"brightness": 10})
        self.assertEqual(self.m.brightness_on(), (10, 50))


class PowerGroup(Groups):
    def test_pc_off_leaves_the_room(self):
        self.m.set_power_group("pc", False)
        self.assertEqual((self.on("desk"), self.on("tower"), self.on("lamp")), (False, False, True))

    def test_room_off_and_on_leaves_the_pc(self):
        self.m.set_power_group("room", False)
        self.assertEqual((self.on("desk"), self.on("tower"), self.on("lamp")), (True, True, False))
        self.m.set_power_group("room", True)
        self.assertTrue(self.on("lamp"))

    def test_hidden_lights_are_left_alone(self):
        self.m.set_hidden("tower", True)
        self.m.set_power_group("pc", False)
        self.assertFalse(self.on("desk"))
        self.assertTrue(self.on("tower"))

    def test_undo_after_a_group_change(self):
        self.m.remember()
        self.m.set_power_group("pc", False)
        self.assertTrue(self.m.undo())
        self.assertTrue(self.on("desk") and self.on("tower") and self.on("lamp"))

    def test_groups_present(self):
        self.assertEqual(self.m.groups_present(), ("pc", "room"))
        self.m.set_hidden("lamp", True)
        self.assertEqual(self.m.groups_present(), ("pc",))


def build_items(manager):
    tray = Tray.__new__(Tray)   # without starting the icon
    tray.app = SimpleNamespace(manager=manager)
    return list(tray._items())


def by_text(items, text):
    return next(i for i in items if getattr(i, "text", None) == text)


class TrayItems(Groups):
    def test_brightness_greyed_out_without_a_light_on(self):
        self.m.set_power_all(False)
        item = by_text(build_items(self.m), "Brightness")
        self.assertFalse(item.enabled)

    def test_brightness_check_follows_the_lights(self):
        self.m.set_brightness_on(75)
        item = by_text(build_items(self.m), "Brightness")
        self.assertTrue(item.enabled)
        checked = {sub.text: sub.checked for sub in item.submenu.items}
        self.assertEqual(list(checked), ["100%", "75%", "50%", "25%", "10%"])
        self.assertEqual([k for k, v in checked.items() if v], ["75%"])
        self.m.update(["desk"], {"brightness": 10})
        item = by_text(build_items(self.m), "Brightness")
        self.assertFalse(any(sub.checked for sub in item.submenu.items))

    def test_choosing_a_value_sets_it_with_undo(self):
        item = by_text(build_items(self.m), "Brightness")
        next(s for s in item.submenu.items if s.text == "25%")(None)
        self.assertEqual(self.m.brightness_on(), (25,))
        self.assertTrue(self.m.can_undo())

    def test_lights_menu_leaves_out_an_empty_group(self):
        self.m.set_hidden("lamp", True)
        item = by_text(build_items(self.m), "Lights")
        self.assertEqual([s.text for s in item.submenu.items], ["PC lights on", "PC lights off"])

    def test_lights_menu_has_both_groups(self):
        item = by_text(build_items(self.m), "Lights")
        self.assertEqual([s.text for s in item.submenu.items],
                         ["PC lights on", "PC lights off", "Room lights on", "Room lights off"])
        next(s for s in item.submenu.items if s.text == "PC lights off")(None)
        self.assertFalse(self.on("desk"))
        self.assertTrue(self.on("lamp"))


if __name__ == "__main__":
    unittest.main()
