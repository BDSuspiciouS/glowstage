import unittest
from pathlib import Path

from glowstage import whatsnew
from glowstage.effects import EFFECTS


class WhatsNew(unittest.TestCase):
    def test_after_an_update_until_got_it(self):
        settings = {"setup_done": True, "seen_version": "0.9.6"}
        card = whatsnew.card(settings, "0.9.7")
        self.assertEqual(card["version"], "0.9.7")
        self.assertTrue(card["items"])
        settings["seen_version"] = "0.9.7"
        self.assertIsNone(whatsnew.card(settings, "0.9.7"))

    def test_someone_from_before_this_card_existed_gets_it(self):
        settings = {"setup_done": True}
        self.assertFalse(whatsnew.first_run(settings, "0.9.7"))
        self.assertIsNotNone(whatsnew.card(settings, "0.9.7"))

    def test_a_new_install_doesnt(self):
        settings = {"setup_done": False}
        self.assertTrue(whatsnew.first_run(settings, "0.9.7"))
        self.assertIsNone(whatsnew.card(settings, "0.9.7"))

    def test_a_version_without_notes(self):
        self.assertIsNone(whatsnew.card({"setup_done": True}, "0.0.1"))

    def test_every_line_goes_somewhere_real(self):
        for version, items in whatsnew.NOTES.items():
            for it in items:
                self.assertTrue(it["title"] and it["text"], version)
                go = it["go"]
                if go.startswith("effect:"):
                    self.assertIn(go[7:], EFFECTS, go)
                else:
                    self.assertIn(go, whatsnew.GOES, go)

    def test_setup_places_are_real_sections(self):
        setup = (Path(__file__).parent.parent / "glowstage" / "ui" / "setup.js").read_text(encoding="utf-8")
        for go in whatsnew.GOES:
            if go.startswith("setup:"):
                self.assertIn(f'id: "set-{go[6:]}"', setup, go)


if __name__ == "__main__":
    unittest.main()
