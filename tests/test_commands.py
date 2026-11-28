"""Ctrl+K understands what you type: the parser in glowstage/ui/commands.js, run with Node against
a table of inputs. Each input gives the labels of the commands it reads as (best first), or none."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
RUNNER = Path(__file__).parent / "commands_runner.js"

EFFECTS = [("rainbow", "Rainbow wave"), ("spectrum", "Spectrum cycle"), ("breathing", "Breathing"), ("fire", "Fire"),
           ("aurora", "Aurora"), ("candle", "Candle"), ("ocean", "Ocean"), ("storm", "Thunderstorm"), ("snow", "Snowfall"),
           ("string_lights", "Holiday lights"), ("rain", "Rain"), ("daylight", "Follow the sun"),
           ("music_spectrum", "Spectrum", "music"), ("music_show", "Light show", "music"),
           ("music_party", "Party", "music"), ("music_fire", "Music fire", "music"),
           ("game_cs2", "Counter-Strike 2", "game")]
CTX = {
    "presets": ["Rainbow", "Focus", "Red room", "Warm white", "Party", "Reading"],
    "groups": [{"name": "Desk"}, {"name": "Evening"}],
    "lights": [{"id": "core", "name": "Commander Core", "group": "pc"}, {"id": "kb", "name": "Keyboard", "group": "pc"},
               {"id": "strip", "name": "Desk strip", "group": "room"}, {"id": "mon", "name": "Monitor", "group": "room"}],
    "effects": [{"id": e[0], "name": e[1], "group": e[2] if len(e) > 2 else "animation"} for e in EFFECTS],
    "showOn": False,
    "focus": {"lastWork": 25, "lastRest": 5},
}


def run(inputs, ctx=None):
    out = subprocess.run(["node", str(RUNNER)], input=json.dumps({"ctx": ctx or CTX, "inputs": inputs}),
                         capture_output=True, text=True, encoding="utf-8", check=True, cwd=ROOT).stdout
    return {text: found for text, found in json.loads(out)}


@unittest.skipUnless(shutil.which("node"), "needs Node on the PATH")
class Commands(unittest.TestCase):
    def check(self, table, ctx=None):
        got = run(list(table), ctx)
        for text, expected in table.items():
            labels = [c["label"] for c in got[text]]
            self.assertEqual(labels, expected, f"for {text!r}")

    def test_colors(self):
        self.check({
            "red": ["Set every light to red"],
            "Orange": ["Set every light to orange"],
            "amber": ["Set every light to amber"],
            "yellow": ["Set every light to yellow"],
            "lime": ["Set every light to lime"],
            "green": ["Set every light to green"],
            "teal": ["Set every light to teal"],
            "cyan": ["Set every light to cyan"],
            "blue": ["Set every light to blue"],
            "indigo": ["Set every light to indigo"],
            "purple": ["Set every light to purple"],
            "violet": ["Set every light to violet"],
            "magenta": ["Set every light to magenta"],
            "pink": ["Set every light to pink"],
            "white": ["Set every light to white"],
            "cool white": ["Set every light to cool white"],
            "candlelight": ["Set every light to candlelight"],
            "daylight": ["Set every light to daylight"],
            "#ff8800": ["Set every light to #ff8800"],
            "ff8800": ["Set every light to #ff8800"],
            "#f80": ["Set every light to #ff8800"],
            "#FF8800": ["Set every light to #ff8800"],
            "2700k": ["Set every light to 2700 K (warm white)"],
            "4000 k": ["Set every light to 4000 K (neutral white)"],
            "6500K": ["Set every light to 6500 K (daylight)"],
        })

    def test_the_color_action(self):
        got = run(["#f80", "2700k", "desk red"])
        self.assertEqual(got["#f80"][0]["action"], {"type": "color", "target": {"kind": "every"}, "hex": "#ff8800"})
        action = got["2700k"][0]["action"]
        self.assertEqual((action["type"], action["kelvin"]), ("color", 2700))
        self.assertEqual(got["desk red"][0]["action"]["target"], {"kind": "group", "name": "Desk"})

    def test_a_group_or_light_and_a_color(self):
        self.check({
            "desk red": ["Set Desk to red"],
            "keyboard blue": ["Set Keyboard to blue"],
            "Keyboard Blue": ["Set Keyboard to blue"],
            "evening warm white": ["Set Evening to warm white"],
            "desk strip pink": ["Set Desk strip to pink"],
            "monitor #ff8800": ["Set Monitor to #ff8800"],
            "pc green": ["Set PC lights to green"],
            "room 2700k": ["Set room lights to 2700 K (warm white)"],
        })

    def test_forgiving_words(self):
        self.check({
            "set every light to red": ["Set every light to red"],
            "make everything red": ["Set every light to red"],
            "turn the lights blue": ["Set every light to blue"],
            "set the desk to red": ["Set Desk to red"],
            "make keyboard blue please": ["Set Keyboard to blue"],
            "red on desk": ["Set Desk to red"],
            "color red": ["Set every light to red"],
            "SET ALL LIGHTS TO PINK": ["Set every light to pink"],
            "  red  ": ["Set every light to red"],
            "make it brighter": ["Make every light that's on brighter"],
            "turn off the room lights": ["Room lights off"],
        })

    def test_brightness(self):
        self.check({
            "30%": ["Brightness 30% for every light that's on"],
            "30": ["Brightness 30% for every light that's on"],
            "brightness 30": ["Brightness 30% for every light that's on"],
            "set brightness to 30%": ["Brightness 30% for every light that's on"],
            "30 percent": ["Brightness 30% for every light that's on"],
            "dim": ["Dim every light that's on"],
            "dim to 20": ["Brightness 20% for every light that's on"],
            "brighter": ["Make every light that's on brighter"],
            "darker": ["Make every light that's on darker"],
            "max": ["Brightness 100% for every light that's on"],
            "full": ["Brightness 100% for every light that's on"],
            "full brightness": ["Brightness 100% for every light that's on"],
            "desk 30%": ["Brightness 30% for Desk"],
            "keyboard 50": ["Brightness 50% for Keyboard"],
            "dim the desk": ["Dim Desk"],
            "pc max": ["Brightness 100% for PC lights"],
            "0": [],
            "0%": [],
            "150": [],
            "brightness": [],
        })

    def test_on_and_off(self):
        self.check({
            "on": ["All lights on"],
            "off": ["All lights off"],
            "lights off": ["All lights off"],
            "all lights on": ["All lights on"],
            "turn off": ["All lights off"],
            "desk off": ["Desk off"],
            "desk on": ["Desk on"],
            "turn off the keyboard": ["Keyboard off"],
            "pc off": ["PC lights off"],
            "room on": ["Room lights on"],
            "monitor off": ["Monitor off"],
        })
        self.assertEqual(run(["pc off"])["pc off"][0]["action"]["target"], {"kind": "scope", "scope": "pc"})
        self.assertEqual(run(["keyboard off"])["keyboard off"][0]["action"],
                         {"type": "power", "target": {"kind": "light", "id": "kb"}, "on": False})

    def test_looks(self):
        self.check({
            "storm": ["Thunderstorm on every light"],
            "thunderstorm": ["Thunderstorm on every light"],
            "fire": ["Fire on every light"],
            "candle": ["Candle on every light"],
            "snow": ["Snowfall on every light"],
            "snowfall": ["Snowfall on every light"],
            "holiday": ["Holiday lights on every light"],
            "storm on desk": ["Thunderstorm on Desk"],
            "desk storm": ["Thunderstorm on Desk"],
            "keyboard fire": ["Fire on Keyboard"],
            "thunderstorm on every light": ["Thunderstorm on every light"],
            "put the aurora on all lights": ["Aurora on every light"],
            "ocean": ["Ocean on every light"],
        })
        self.assertEqual(run(["storm"])["storm"][0]["action"], {"type": "effect", "target": {"kind": "every"}, "effect": "storm"})

    def test_a_look_that_is_also_a_preset(self):
        got = run(["rainbow", "party"])
        self.assertEqual([c["label"] for c in got["rainbow"]], ["Rainbow wave on every light"])   # (the preset comes first, in the search)
        self.assertEqual([c["label"] for c in got["party"]], ["Party on every light"])

    def test_the_light_show(self):
        self.check({"show": ["Start the light show"], "light show": ["Start the light show"], "start the light show": ["Start the light show"],
                    "stop show": ["Stop the light show"], "light show off": ["Stop the light show"]})
        self.check({"show": ["Stop the light show"], "light show": ["Stop the light show"]}, dict(CTX, showOn=True))
        self.assertEqual(run(["show off"])["show off"][0]["action"], {"type": "show", "want": "off"})

    def test_sleep_timer(self):
        self.check({
            "sleep 30": ["Sleep timer: 30 minutes"],
            "sleep 45 min": ["Sleep timer: 45 minutes"],
            "sleep 1h": ["Sleep timer: 1 hour"],
            "sleep 1 hour": ["Sleep timer: 1 hour"],
            "sleep 1.5h": ["Sleep timer: 1 hour 30 minutes"],
            "sleep for an hour": ["Sleep timer: 1 hour"],
            "sleep in half an hour": ["Sleep timer: 30 minutes"],
            "sleep timer 20 minutes": ["Sleep timer: 20 minutes"],
            "set a sleep timer for 30 minutes": ["Sleep timer: 30 minutes"],
            "Sleep 4h": ["Sleep timer: 4 hours"],
            "sleep 5h": [],
            "sleep 0": [],
            "sleep": [],
            "sleep soon": [],
            "cancel sleep": ["Cancel the sleep timer"],
            "stop the sleep timer": ["Cancel the sleep timer"],
            "sleep off": ["Cancel the sleep timer"],
        })
        self.assertEqual(run(["sleep 45"])["sleep 45"][0]["action"], {"type": "sleep", "minutes": 45})

    def test_focus_timer(self):
        self.check({
            "focus": ["Focus timer: 25 minutes"],
            "focus 25": ["Focus timer: 25 minutes"],
            "focus 45": ["Focus timer: 45 minutes"],
            "focus 50 10": ["Focus timer: 50 minutes, 10 minute break"],
            "focus 25 min": ["Focus timer: 25 minutes"],
            "start a focus timer": ["Focus timer: 25 minutes"],
            "focus 30": [],                                    # (not one of the work times the timer offers)
            "focus 25 7": [],                                  # (nor a break)
            "focus 25 5 5": [],
            "stop focus": ["Stop the focus timer"],
            "stop the focus timer": ["Stop the focus timer"],
            "focus off": ["Stop the focus timer"],
        })
        self.assertEqual(run(["focus 50 10"])["focus 50 10"][0]["action"], {"type": "focus", "work": 50, "rest": 10})
        self.assertEqual(run(["focus"])["focus"][0]["action"], {"type": "focus", "work": 25, "rest": None})
        self.check({"focus": ["Focus timer: 45 minutes"]}, dict(CTX, focus={"lastWork": 45, "lastRest": 10}))

    def test_pause(self):
        self.check({
            "pause": ["Pause automatic looks for an hour"],
            "pause 1h": ["Pause automatic looks for an hour"],
            "pause 60": ["Pause automatic looks for an hour"],
            "pause for an hour": ["Pause automatic looks for an hour"],
            "pause 3h": ["Pause automatic looks for 3 hours"],
            "pause 3 hours": ["Pause automatic looks for 3 hours"],
            "pause until tomorrow": ["Pause automatic looks until tomorrow"],
            "pause tomorrow": ["Pause automatic looks until tomorrow"],
            "pause automatic looks": ["Pause automatic looks for an hour"],
            "pause 2h": [],
            "pause the music": [],
            "resume": ["Resume automatic looks"],
            "resume automatic looks": ["Resume automatic looks"],
        })
        self.assertEqual(run(["pause until tomorrow"])["pause until tomorrow"][0]["action"], {"type": "pause", "minutes": "tomorrow"})
        self.assertEqual(run(["pause 3h"])["pause 3h"][0]["action"], {"type": "pause", "minutes": 180})

    def test_presets_stay_presets(self):
        got = run(["red room", "Red Room", "warm white", "reading", "focus", "rainbow"])
        for text in ("red room", "Red Room", "warm white", "reading"):
            self.assertEqual(got[text], [], text)           # (no color, no switch: the preset alone)
        self.assertEqual([c["label"] for c in got["focus"]], ["Focus timer: 25 minutes"])   # (after the preset)
        self.assertEqual([c["label"] for c in got["rainbow"]], ["Rainbow wave on every light"])

    def test_the_same_words_are_a_command_without_that_preset(self):
        ctx = dict(CTX, presets=[])
        self.check({"red room": ["Set room lights to red"], "warm white": ["Set every light to warm white"]}, ctx)

    def test_things_that_are_not_commands(self):
        nothing = ["o", "r", "re", "hello", "desk", "red desk blue", "facade", "decade", "add", "bad", "fee", "ace",
                   "#12", "#1234", "123456789", "all", "lights", "every", "the", "set", "make it so", "turn", "to",
                   "stop", "cancel", "show me", "brightness desk", "sunny", "rain bow", "xyz", "k", "2700", "50 k", "12000k",
                   "red blue", "red 30 blue", "sleep desk", "storm fire", "dimm", "rd", "gre", "blu", "off on", "desk strip", ""]
        got = run(nothing)
        for text in nothing:
            self.assertEqual(got[text], [], f"{text!r} is not an instruction")

    def test_only_what_you_have(self):
        ctx = dict(CTX, groups=[], lights=[{"id": "x", "name": "Lamp", "group": "room"}])
        self.check({"desk red": [], "lamp red": ["Set Lamp to red"], "pc off": [], "room off": ["Room lights off"]}, ctx)

    def test_a_name_that_two_things_share_shows_both(self):
        ctx = dict(CTX, groups=[{"name": "Desk"}], lights=[{"id": "d1", "name": "Desk", "group": "pc"}, {"id": "d2", "name": "Monitor", "group": "room"}])
        got = run(["desk red", "desk off"], ctx)
        self.assertEqual([c["label"] for c in got["desk red"]], ["Set Desk (group) to red", "Set Desk (PC) to red"])
        self.assertEqual([c["label"] for c in got["desk off"]], ["Desk (group) off", "Desk (PC) off"])
        self.assertEqual([c["action"]["target"]["kind"] for c in got["desk red"]], ["group", "light"])

    def test_a_part_of_a_name_that_fits_several_lights_shows_each(self):
        ctx = dict(CTX, groups=[], lights=[{"id": "a", "name": "Desk strip", "group": "room"}, {"id": "b", "name": "Desk lamp", "group": "room"},
                                          {"id": "c", "name": "Shelf", "group": "room"}])
        got = run(["desk red", "desk lamp red", "strip red"], ctx)
        self.assertEqual([c["label"] for c in got["desk red"]], ["Set Desk strip to red", "Set Desk lamp to red"])
        self.assertEqual([c["label"] for c in got["desk lamp red"]], ["Set Desk lamp to red"])
        self.assertEqual([c["label"] for c in got["strip red"]], ["Set Desk strip to red"])

    def test_the_whole_name_beats_the_start_of_one(self):
        # a group "Desk" and a light "Desk strip": "desk" is the group, "desk strip" the light
        got = run(["desk red", "desk strip red"])
        self.assertEqual([c["label"] for c in got["desk red"]], ["Set Desk to red"])
        self.assertEqual([c["label"] for c in got["desk strip red"]], ["Set Desk strip to red"])

    def test_two_looks_with_one_name_show_both(self):
        self.check({"spectrum": ["Spectrum cycle on every light", "Spectrum on every light"]})

    def test_at_most_three(self):
        ctx = dict(CTX, groups=[], lights=[{"id": str(i), "name": f"Strip {i}", "group": "room"} for i in range(6)])
        self.assertEqual(len(run(["strip red"], ctx)["strip red"]), 3)

    def test_the_games_and_the_show_are_not_looks(self):
        got = run(["counter", "counter strike", "light show look"])
        self.assertEqual(got["counter"], [])
        self.assertEqual(got["counter strike"], [])


if __name__ == "__main__":
    unittest.main()
