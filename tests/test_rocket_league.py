import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from glowstage import games, rocket_league
from glowstage.automations import RULES, Automations
from glowstage.config import Config
from glowstage.effects import EFFECTS, GAME_EFFECTS
from glowstage.rocket_league import (ALIVE, END_LINGER, FLASHES, LENGTH, MAX_BUFFER, RocketLeagueReader,
                                     RocketLeagueState, StreamParser)
from tests.test_app_looks import FakeManager

# ---- what Rocket League's Stats API says (shaped like the real thing, trimmed) -------------------

ME = {"Name": "Batikan", "Shortcut": 1, "TeamNum": 0}
MATE = {"Name": "Mate", "Shortcut": 2, "TeamNum": 0}
RIVAL = {"Name": "Rival", "Shortcut": 3, "TeamNum": 1}


def player(who, boost=64, supersonic=False, demolished=False):
    return {**who, "PrimaryId": f"Steam|7656119800000000{who['Shortcut']}|0", "Score": 120, "Goals": 1, "Shots": 3,
            "Assists": 0, "Saves": 1, "Touches": 40, "Demos": 0, "bHasCar": not demolished, "Speed": 1400,
            "Boost": boost, "bBoosting": False, "bOnGround": True, "bSupersonic": supersonic, "bDemolished": demolished}


def update(target=ME, guid="match-1", boost=64, supersonic=False, demolished=False, replay=False, overtime=False,
           colors=("1873FF", "E67E00"), has_target=True, winner=None, score=(0, 0)):
    """The Data of an UpdateState message: you (Batikan, blue), a mate and a rival."""
    players = [player(ME, boost, supersonic, demolished), player(MATE), player(RIVAL)]
    return {"MatchGuid": guid, "Players": players,
            "Game": {"Teams": [{"Name": "Blue", "TeamNum": 0, "Score": score[0], "ColorPrimary": colors[0], "ColorSecondary": "E5E5E5"},
                               {"Name": "Orange", "TeamNum": 1, "Score": score[1], "ColorPrimary": colors[1], "ColorSecondary": "E5E5E5"}],
                     "TimeSeconds": 212, "bOverTime": overtime, "Ball": {"Speed": 900, "TeamNum": 255}, "bReplay": replay,
                     "bHasWinner": winner is not None, "Winner": "" if winner is None else winner, "Arena": "Stadium_P",
                     "bHasTarget": has_target, "Target": dict(target) if has_target else {}}}


def goal(scorer):
    return {"GoalSpeed": 87.5, "GoalTime": 190.0, "ImpactLocation": {"X": 0, "Y": 5100, "Z": 300},
            "Scorer": dict(scorer), "Assister": {"Name": "", "Shortcut": 0, "TeamNum": 0}, "BallLastTouch": {"Player": dict(scorer), "Speed": 80}}


def statfeed(name, main, other=None):
    return {"MatchGuid": "match-1", "EventName": name, "Type": name, "MainTarget": dict(main),
            "SecondaryTarget": dict(other or {"Name": "", "Shortcut": 0, "TeamNum": 0})}


def wire(event, data=None, as_string=True):
    """One message as the game writes it to the socket: Data is a JSON string, as often as not."""
    body = json.dumps(data) if as_string and data is not None else data
    return json.dumps({"Event": event, "Data": body}).encode()


def started(**kw):
    """A state that has seen the match once, so what follows is news."""
    s = RocketLeagueState()
    s.feed("UpdateState", update(**kw), 1.0)
    return s


# ---- the stream -------------------------------------------------------------------------------

class Stream(unittest.TestCase):
    def test_one_message(self):
        out = StreamParser().feed(wire("GoalScored", goal(ME)))
        self.assertEqual([e for e, _ in out], ["GoalScored"])
        self.assertEqual(out[0][1]["Scorer"]["Name"], "Batikan")

    def test_data_as_a_string_is_decoded_again_and_as_an_object_is_kept(self):
        a = StreamParser().feed(wire("MatchEnded", {"WinnerTeamNum": 1}, as_string=True))
        b = StreamParser().feed(wire("MatchEnded", {"WinnerTeamNum": 1}, as_string=False))
        self.assertEqual(a, b)
        self.assertEqual(a[0][1], {"WinnerTeamNum": 1})

    def test_a_string_that_isnt_json_stays_a_string_and_no_data_is_none(self):
        self.assertEqual(StreamParser().feed(wire("MatchCreated", "hello", as_string=False)), [("MatchCreated", "hello")])
        self.assertEqual(StreamParser().feed(b'{"Event":"RoundStarted"}'), [("RoundStarted", None)])

    def test_several_messages_in_one_read(self):
        data = wire("CountdownBegin", {"MatchGuid": "m"}) + wire("RoundStarted") + b"\n" + wire("UpdateState", update())
        self.assertEqual([e for e, _ in StreamParser().feed(data)], ["CountdownBegin", "RoundStarted", "UpdateState"])

    def test_a_message_split_anywhere_arrives_whole(self):
        data = wire("UpdateState", update()) + wire("GoalScored", goal(ME))
        for cut in range(1, len(data)):
            p = StreamParser()
            out = p.feed(data[:cut]) + p.feed(data[cut:])
            self.assertEqual([e for e, _ in out], ["UpdateState", "GoalScored"], cut)

    def test_a_message_dribbling_in_a_byte_at_a_time(self):
        p, out = StreamParser(), []
        for b in wire("GoalScored", goal(ME)) + wire("MatchEnded", {"WinnerTeamNum": 0}):
            out += p.feed(bytes([b]))
        self.assertEqual([e for e, _ in out], ["GoalScored", "MatchEnded"])

    def test_a_letter_split_in_the_middle_of_its_bytes(self):
        data = json.dumps({"Event": "UpdateState", "Data": {"Name": "Zoë ⚽"}}, ensure_ascii=False).encode("utf-8")
        for cut in range(1, len(data)):
            p = StreamParser()
            out = p.feed(data[:cut]) + p.feed(data[cut:])
            self.assertEqual(out[0][1]["Name"], "Zoë ⚽", cut)

    def test_braces_inside_strings_dont_end_a_message(self):
        data = b'{"Event":"StatfeedEvent","Data":{"EventName":"Shot } {","Name":"a\\"}b"}}'
        out = StreamParser().feed(data)
        self.assertEqual(out[0][1]["EventName"], "Shot } {")

    def test_garbage_between_messages_is_skipped(self):
        data = b"hello }{ \x00\xff junk " + wire("RoundStarted") + b" ]]] {not json} " + wire("GoalScored", goal(ME)) + b"{{{"
        out = StreamParser().feed(data)
        self.assertEqual([e for e, _ in out], ["RoundStarted", "GoalScored"])

    def test_objects_without_an_event_are_not_messages(self):
        self.assertEqual(StreamParser().feed(b'{"Hello": 1} {"Event": 5} [1,2] {"Event": "Ok"}'), [("Ok", None)])

    def test_a_broken_message_doesnt_swallow_the_next(self):
        p = StreamParser()
        out = p.feed(b'{"Event":"UpdateState","Data":{"Players":[{"Name":' + wire("GoalScored", goal(ME)) + wire("RoundStarted"))
        self.assertEqual([e for e, _ in out], ["GoalScored", "RoundStarted"])
        self.assertGreaterEqual(p.dropped, 1)

    def test_a_message_that_never_ends_is_dropped_at_the_cap_and_the_stream_goes_on(self):
        p = StreamParser()
        out = p.feed(b'{"Event":"UpdateState","Data":"' + b"a" * 200_000)
        for _ in range(MAX_BUFFER // 200_000 + 2):
            out += p.feed(b"a" * 200_000)
        self.assertEqual(out, [])
        self.assertLess(len(p._buf), MAX_BUFFER)
        self.assertGreaterEqual(p.dropped, 1)
        self.assertEqual(p.feed(b'"}' + wire("RoundStarted")), [("RoundStarted", None)])

    def test_a_finished_message_over_the_cap_is_dropped(self):
        p = StreamParser()
        out = p.feed(b'{"Event":"UpdateState","Data":"' + b"a" * (MAX_BUFFER + 10) + b'"}' + wire("RoundStarted"))
        self.assertEqual([e for e, _ in out], ["RoundStarted"])
        self.assertEqual(p.dropped, 1)

    def test_a_normal_buffer_stays_small(self):
        p = StreamParser()
        for _ in range(200):
            p.feed(wire("UpdateState", update()))
        self.assertEqual(p._buf, "")


# ---- finding the game -------------------------------------------------------------------------

def steam_lib(tmp, name="SteamLibrary", game=True, manifest=True):
    lib = Path(tmp) / name
    (lib / "steamapps").mkdir(parents=True)
    if manifest:
        (lib / "steamapps" / "appmanifest_252950.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"252950"\n\t"installdir"\t\t"rocketleague"\n}\n', encoding="utf-8")
    if game:
        (lib / "steamapps" / "common" / "rocketleague" / "TAGame" / "Config").mkdir(parents=True)
    return lib


def epic_item(folder, install, name="Rocket League", app="Sugar"):
    item = {"FormatVersion": 0, "DisplayName": name, "AppName": app, "InstallLocation": str(install), "bIsApplication": True}
    (Path(folder) / f"{app}{name[:2]}.item").write_text(json.dumps(item), encoding="utf-8")


class Finding(unittest.TestCase):
    def test_steam_manifest_gives_the_install_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = steam_lib(tmp)
            self.assertEqual(rocket_league.find_in_steam([Path(tmp) / "nope", lib]), lib / "steamapps" / "common" / "rocketleague")

    def test_steam_without_the_game_or_the_manifest_means_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rocket_league.find_in_steam([steam_lib(tmp, "a", game=False)]))       # (manifest left behind, game removed)
            self.assertIsNone(rocket_league.find_in_steam([steam_lib(tmp, "b", manifest=False)]))
            self.assertIsNone(rocket_league.find_in_steam([]))

    def test_steam_with_no_installdir_in_the_manifest_tries_the_usual_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = steam_lib(tmp)
            (lib / "steamapps" / "appmanifest_252950.acf").write_text('"AppState"\n{\n}\n', encoding="utf-8")
            self.assertIsNotNone(rocket_league.find_in_steam([lib]))

    def test_epic_manifest_gives_the_install_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifests, install = Path(tmp) / "Manifests", Path(tmp) / "Epic" / "rocketleague"
            manifests.mkdir()
            (install / "TAGame").mkdir(parents=True)
            epic_item(manifests, Path(tmp) / "elsewhere", name="Fortnite", app="Fortnite")
            (manifests / "broken.item").write_text("{not json", encoding="utf-8")
            (manifests / "list.item").write_text("[1]", encoding="utf-8")
            epic_item(manifests, install)
            self.assertEqual(rocket_league.find_in_epic(manifests), install)

    def test_epic_finds_it_by_its_app_name_too_and_skips_a_folder_that_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifests, install = Path(tmp) / "Manifests", Path(tmp) / "rl"
            manifests.mkdir()
            epic_item(manifests, install, name="Rocket League®", app="Sugar")
            self.assertIsNone(rocket_league.find_in_epic(manifests))            # (the folder isn't there)
            (install / "TAGame").mkdir(parents=True)
            self.assertEqual(rocket_league.find_in_epic(manifests), install)

    def test_no_epic_means_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rocket_league.find_in_epic(Path(tmp) / "missing"))
            self.assertIsNone(rocket_league.find_in_epic(Path(tmp)))

    def test_the_game_is_found_in_steam_or_epic(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifests, epic = Path(tmp) / "Manifests", Path(tmp) / "Epic" / "rocketleague"
            manifests.mkdir()
            (epic / "TAGame").mkdir(parents=True)
            epic_item(manifests, epic)
            lib = steam_lib(tmp)
            with mock.patch.object(rocket_league, "epic_manifests", return_value=manifests):
                with mock.patch.object(games, "steam_libraries", return_value=[lib]):
                    self.assertEqual(games.find_rocket_league(), lib / "steamapps" / "common" / "rocketleague")
                with mock.patch.object(games, "steam_libraries", return_value=[]):
                    self.assertEqual(games.find_rocket_league(), epic)
            with mock.patch.object(rocket_league, "epic_manifests", return_value=Path(tmp) / "no"), \
                    mock.patch.object(games, "steam_libraries", return_value=[]):
                self.assertIsNone(games.find_rocket_league())

    def test_the_ini_lives_in_tagame_config(self):
        self.assertEqual(rocket_league.ini_path(Path("X")), Path("X") / "TAGame" / "Config" / "DefaultStatsAPI.ini")


# ---- the ini file -----------------------------------------------------------------------------

ORIGINAL = ("[TAGame.MatchStatsExporter_TA]\r\n"
            "; how often the game sends its state, per second\r\n"
            "PacketSendRate=0\r\n"
            "Port=49123\r\n"
            "\r\n"
            "[Other.Section]\r\n"
            "Keep=me\r\n"
            "PacketSendRate=99\r\n")


class Ini(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "TAGame" / "Config" / "DefaultStatsAPI.ini"

    def tearDown(self):
        self.tmp.cleanup()

    def text(self):
        return self.path.read_bytes().decode("utf-8")

    def put(self, text, enc="utf-8"):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(text.encode(enc))

    def test_a_file_without_the_section_gets_it_and_keeps_everything_else(self):
        before = "[Engine.Settings]\r\nA=1\r\n; a comment\r\nB = two"          # (no newline at the end)
        self.put(before)
        orig, port = rocket_league.switch_on(self.path)
        self.assertEqual((orig, port), ({"PacketSendRate": None, "Port": None}, 49123))
        self.assertTrue(self.text().startswith(before + "\r\n\r\n[TAGame.MatchStatsExporter_TA]\r\n"))
        self.assertTrue(self.text().endswith("PacketSendRate=30\r\nPort=49123\r\n"))
        self.assertEqual(rocket_league.read_stats_api(self.path), {"PacketSendRate": "30", "Port": "49123"})
        self.assertTrue(rocket_league.is_on(self.path))

    def test_a_missing_file_is_created_with_the_section(self):
        rocket_league.switch_on(self.path)
        self.assertEqual(self.text(), "[TAGame.MatchStatsExporter_TA]\r\nPacketSendRate=30\r\nPort=49123\r\n")

    def test_an_empty_section_gets_its_keys_and_other_sections_stay(self):
        self.put("[TAGame.MatchStatsExporter_TA]\n\n[Next]\nX=1\n")
        rocket_league.switch_on(self.path)
        self.assertEqual(self.text(), "[TAGame.MatchStatsExporter_TA]\nPacketSendRate=30\nPort=49123\n\n[Next]\nX=1\n")

    def test_existing_keys_change_in_place_and_other_lines_are_untouched(self):
        self.put(ORIGINAL)
        orig, port = rocket_league.switch_on(self.path)
        self.assertEqual(orig, {"PacketSendRate": "0", "Port": "49123"})
        self.assertEqual(self.text(), ORIGINAL.replace("PacketSendRate=0", "PacketSendRate=30"))   # (the other section's key, too, stays 99)

    def test_a_missing_key_is_added_inside_the_section_not_after_the_next_one(self):
        self.put("[TAGame.MatchStatsExporter_TA]\nPort=50000\n\n[Other]\nA=1\n")
        rocket_league.switch_on(self.path)
        self.assertEqual(self.text(), "[TAGame.MatchStatsExporter_TA]\nPort=50000\nPacketSendRate=30\n\n[Other]\nA=1\n")

    def test_the_original_value_goes_back(self):
        self.put(ORIGINAL)
        orig, _ = rocket_league.switch_on(self.path)
        rocket_league.switch_off(self.path, orig)
        self.assertEqual(self.text(), ORIGINAL)
        self.assertFalse(rocket_league.is_on(self.path))

    def test_without_an_original_it_goes_back_to_off(self):
        self.put("[Engine.Settings]\nA=1\n")
        orig, _ = rocket_league.switch_on(self.path)
        rocket_league.switch_off(self.path, orig)
        self.assertEqual(rocket_league.read_stats_api(self.path)["PacketSendRate"], "0")
        self.assertIn("[Engine.Settings]\nA=1\n", self.text())
        rocket_league.switch_off(self.path, None)
        self.assertEqual(rocket_league.read_stats_api(self.path)["PacketSendRate"], "0")

    def test_a_send_rate_somebody_else_set_stays_and_comes_back(self):
        self.put("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=60\nPort=50001\n")
        orig, port = rocket_league.switch_on(self.path)
        self.assertEqual(port, 50001)
        self.assertEqual(rocket_league.read_stats_api(self.path), {"PacketSendRate": "60", "Port": "50001"})
        rocket_league.switch_off(self.path, orig)
        self.assertEqual(rocket_league.read_stats_api(self.path)["PacketSendRate"], "60")

    def test_a_port_that_makes_no_sense_is_replaced(self):
        for bad in ("abc", "80", "99999", ""):
            self.put(f"[TAGame.MatchStatsExporter_TA]\nPort={bad}\n")
            self.assertEqual(rocket_league.switch_on(self.path)[1], 49123, bad)

    def test_keys_and_sections_in_any_case(self):
        self.put("[tagame.matchstatsexporter_ta]\npacketsendrate = 0\nPORT=49123\n")
        rocket_league.switch_on(self.path)
        self.assertEqual(rocket_league.read_stats_api(self.path)["PacketSendRate"], "30")
        self.assertEqual(self.text().lower().count("packetsendrate"), 1)

    def test_the_encoding_and_line_endings_stay(self):
        self.put("﻿[Engine]\nA=1\n", "utf-8")                              # a byte order mark, and \n
        rocket_league.switch_on(self.path)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf[Engine]\n"))
        self.assertNotIn(b"\r", raw)
        self.put("[Engine]\r\nA=1\r\n", "utf-16")
        rocket_league.switch_on(self.path)
        self.assertTrue(self.path.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff")))
        self.assertEqual(rocket_league.read_stats_api(self.path)["PacketSendRate"], "30")

    def test_bytes_that_arent_text_survive(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"[Engine]\nName=Caf\xe9\n")                      # (not utf-8)
        rocket_league.switch_on(self.path)
        self.assertTrue(self.path.read_bytes().startswith(b"[Engine]\nName=Caf\xe9\n"))


# ---- the match --------------------------------------------------------------------------------

class TheMatch(unittest.TestCase):
    def test_who_you_are_comes_from_the_target(self):
        s = started()
        snap = s.snapshot(1.0)
        self.assertTrue(snap["inMatch"])
        self.assertEqual(snap["team"], 0)
        s = started(target=RIVAL)                       # spectating: the lights follow the camera
        self.assertEqual(s.snapshot(1.0)["team"], 1)

    def test_team_colors_come_from_the_game_and_are_made_vivid(self):
        s = started(colors=("00FF00", "FF00FF"))
        snap = s.snapshot(1.0)
        self.assertEqual((snap["color"], snap["other"]), ((0, 255, 0), (255, 0, 255)))
        snap = started(colors=("5A82B4", "000000")).snapshot(1.0)      # a dull blue gets full strength, black falls back to orange
        self.assertEqual(max(snap["color"]), 255)
        self.assertGreater(snap["color"][2], snap["color"][0])
        self.assertEqual(snap["other"], rocket_league.ORANGE)

    def test_colors_that_say_nothing_fall_back_to_blue_and_orange(self):
        for bad in ("", None, "xyz", "12345", "FFFFFF", "808080", "050505"):
            self.assertEqual(rocket_league.vivid(bad, (1, 2, 3)), (1, 2, 3), bad)
        snap = started(colors=("", None)).snapshot(1.0)
        self.assertEqual((snap["color"], snap["other"]), (rocket_league.BLUE, rocket_league.ORANGE))
        self.assertEqual(started(target=RIVAL, colors=("", "")).snapshot(1.0)["color"], rocket_league.ORANGE)

    def test_boost_supersonic_and_score(self):
        s = started(boost=37, supersonic=True, score=(2, 1))
        snap = s.snapshot(1.0)
        self.assertAlmostEqual(snap["boost"], 0.37)
        self.assertTrue(snap["supersonic"])
        self.assertEqual(snap["score"], [2, 1])
        s.feed("UpdateState", update(boost=250), 1.1)
        self.assertEqual(s.snapshot(1.1)["boost"], 1.0)
        s.feed("UpdateState", update(boost=-5), 1.2)
        self.assertEqual(s.snapshot(1.2)["boost"], 0.0)

    def test_demolished_until_the_car_is_back(self):
        s = started()
        s.feed("UpdateState", update(demolished=True, supersonic=True), 2.0)
        snap = s.snapshot(2.0)
        self.assertTrue(snap["demolished"])
        self.assertFalse(snap["supersonic"])
        s.feed("UpdateState", update(), 5.0)
        self.assertFalse(s.snapshot(5.0)["demolished"])

    def test_overtime_start(self):
        s = started()
        self.assertFalse(s.snapshot(1.0)["overtime"])
        s.feed("UpdateState", update(overtime=True), 3.0)
        snap = s.snapshot(4.0)
        self.assertTrue(snap["overtime"])
        self.assertAlmostEqual(snap["ago"]["overtime"], 1.0)

    def test_a_goal_by_your_team_a_goal_against_and_a_goal_by_you(self):
        s = started()
        s.feed("GoalScored", goal(MATE), 2.0)
        self.assertEqual(s.snapshot(2.0)["ago"].get("goal_for"), 0.0)
        s.feed("GoalScored", goal(RIVAL), 10.0)
        ago = s.snapshot(10.5)["ago"]
        self.assertAlmostEqual(ago["goal_against"], 0.5)
        self.assertNotIn("my_goal", ago)
        s.feed("GoalScored", goal(ME), 20.0)
        ago = s.snapshot(20.0)["ago"]
        self.assertEqual(ago["my_goal"], 0.0)
        self.assertGreater(ago["goal_for"], 17)             # (the earlier one, not a second flash for the same goal)

    def test_saves_and_demolitions_are_yours_only_when_you_made_them(self):
        s = started()
        s.feed("StatfeedEvent", statfeed("Save", MATE), 2.0)
        s.feed("StatfeedEvent", statfeed("Demolish", RIVAL, MATE), 2.0)
        s.feed("StatfeedEvent", statfeed("Shot", ME), 2.0)
        self.assertEqual({k for k in s.snapshot(2.0)["ago"]}, set())
        s.feed("StatfeedEvent", statfeed("Save", ME), 3.0)
        s.feed("StatfeedEvent", statfeed("EpicSave", ME), 4.0)
        s.feed("StatfeedEvent", statfeed("Demolish", ME, RIVAL), 5.0)
        self.assertEqual(set(s.snapshot(5.0)["ago"]), {"save", "epic_save", "demo"})

    def test_being_demolished_is_noted(self):
        s = started()
        s.feed("StatfeedEvent", statfeed("Demolish", RIVAL, ME), 2.0)
        self.assertEqual(set(s.snapshot(2.0)["ago"]), {"demolished"})            # (no flash: the car shows the dim)

    def test_nothing_flashes_in_a_replay(self):
        s = started()
        s.feed("GoalReplayStart", None, 2.0)
        self.assertTrue(s.snapshot(2.0)["replay"])
        s.feed("StatfeedEvent", statfeed("Save", ME), 2.5)
        s.feed("GoalScored", goal(ME), 2.6)
        self.assertEqual(set(s.snapshot(3.0)["ago"]), set())
        s.feed("GoalReplayEnd", None, 8.0)
        s.feed("StatfeedEvent", statfeed("Save", ME), 8.5)
        self.assertIn("save", s.snapshot(8.5)["ago"])

    def test_a_flash_waiting_when_the_replay_starts_waits_it_out(self):
        s = started()
        s.feed("StatfeedEvent", statfeed("Save", ME), 2.0)
        s.feed("GoalScored", goal(ME), 2.05)                      # (queued behind the save)
        s.feed("GoalReplayStart", None, 2.1)
        self.assertNotIn("my_goal", s.snapshot(2.6)["ago"])
        self.assertNotIn("my_goal", s.snapshot(30.0)["ago"])      # (by the end of the replay it's old news)

    def test_the_replay_flag_in_the_state_counts_too(self):
        s = started()
        s.feed("UpdateState", update(replay=True), 2.0)
        self.assertTrue(s.snapshot(2.0)["replay"])
        s.feed("StatfeedEvent", statfeed("Save", ME), 2.2)
        self.assertEqual(set(s.snapshot(2.3)["ago"]), set())
        s.feed("UpdateState", update(replay=False), 6.0)
        self.assertFalse(s.snapshot(6.0)["replay"])

    def test_the_camera_going_elsewhere_in_a_replay_doesnt_change_who_you_are(self):
        s = started()
        s.feed("UpdateState", update(target=RIVAL, replay=True), 2.0)
        self.assertEqual(s.snapshot(2.0)["team"], 0)
        s.feed("UpdateState", update(target=RIVAL), 6.0)             # ... but after it, following a rival is following a rival
        self.assertEqual(s.snapshot(6.0)["team"], 1)

    def test_a_missing_target_keeps_who_you_were(self):
        s = started()
        s.feed("UpdateState", update(has_target=False), 2.0)
        self.assertEqual(s.snapshot(2.0)["team"], 0)
        self.assertAlmostEqual(s.snapshot(2.0)["boost"], 0.64)

    def test_the_match_ends_with_a_win(self):
        s = started()
        s.feed("MatchEnded", {"WinnerTeamNum": 0}, 100.0)
        snap = s.snapshot(100.0)
        self.assertEqual(snap["ago"]["win"], 0.0)
        self.assertTrue(snap["won"])
        self.assertTrue(s.in_match(100.0 + END_LINGER - 1))
        self.assertFalse(s.in_match(100.0 + END_LINGER + 1))        # the look goes back
        s.feed("MatchDestroyed", None, 101.0)
        self.assertTrue(s.in_match(105.0))                          # (the result still shows)

    def test_the_match_ends_with_a_loss(self):
        s = started()
        s.feed("MatchEnded", {"WinnerTeamNum": 1}, 100.0)
        snap = s.snapshot(100.5)
        self.assertIn("lose", snap["ago"])
        self.assertNotIn("win", snap["ago"])
        self.assertFalse(snap["won"])

    def test_the_end_can_come_as_a_winner_in_the_state_and_counts_once(self):
        s = started()
        s.feed("UpdateState", update(winner=1), 100.0)
        s.feed("MatchEnded", {"WinnerTeamNum": 1}, 100.5)
        self.assertEqual(s.snapshot(101.0)["ago"]["lose"], 1.0)

    def test_nothing_flashes_after_the_end(self):
        s = started()
        s.feed("MatchEnded", {"WinnerTeamNum": 0}, 100.0)
        s.feed("GoalScored", goal(ME), 100.2)
        s.feed("StatfeedEvent", statfeed("Save", ME), 100.3)
        self.assertEqual(set(s.snapshot(100.5)["ago"]), {"win"})

    def test_leaving_a_match_early_ends_it(self):
        s = started()
        self.assertTrue(s.in_match(1.5))
        s.feed("MatchDestroyed", None, 2.0)
        self.assertFalse(s.in_match(2.1))
        s.feed("UpdateState", update(guid="match-2"), 30.0)           # the next one
        self.assertTrue(s.in_match(30.1))

    def test_the_match_is_gone_when_the_game_goes_quiet(self):
        s = started()
        self.assertTrue(s.in_match(1.0 + ALIVE - 0.5))
        self.assertFalse(s.in_match(1.0 + ALIVE + 0.5))
        self.assertTrue(s.alive(2.0))
        self.assertFalse(RocketLeagueState().in_match(5.0))

    def test_a_new_match_forgets_the_old_one(self):
        for new in ("guid", "created"):
            s = started(score=(3, 2))
            s.feed("GoalScored", goal(ME), 2.0)
            s.feed("MatchEnded", {"WinnerTeamNum": 0}, 3.0)
            if new == "guid":
                s.feed("UpdateState", update(guid="match-2", target=RIVAL), 50.0)
            else:
                s.feed("MatchCreated", {"MatchGuid": "match-2"}, 50.0)
                s.feed("UpdateState", update(guid="match-2", target=RIVAL), 50.1)
            snap = s.snapshot(50.2)
            self.assertEqual((snap["ago"], snap["won"], snap["team"], snap["inMatch"]), ({}, None, 1, True), new)
            self.assertEqual(snap["score"], [0, 0], new)

    def test_the_same_match_guid_doesnt_reset(self):
        s = started()
        s.feed("GoalScored", goal(ME), 2.0)
        s.feed("UpdateState", update(), 2.1)
        self.assertIn("my_goal", s.snapshot(2.1)["ago"])

    def test_a_goal_before_we_know_who_you_are_is_ignored(self):
        s = RocketLeagueState()
        s.feed("GoalScored", goal(ME), 1.0)
        s.feed("StatfeedEvent", statfeed("Save", ME), 1.0)
        self.assertEqual(s.snapshot(1.0)["ago"], {})

    def test_every_kind_of_nonsense_is_survived(self):
        s = RocketLeagueState()
        for event, data in (("UpdateState", None), ("UpdateState", "x"), ("UpdateState", {"Players": "a", "Game": 3}),
                            ("UpdateState", {"Players": [None, 5, {}], "Game": {"Teams": [None, {"TeamNum": "z"}, {"TeamNum": 1, "Score": "q"}],
                                                                                   "Target": "me", "bHasTarget": True}}),
                            ("GoalScored", None), ("GoalScored", {"Scorer": 7}), ("StatfeedEvent", []), ("MatchEnded", "x"),
                            ("Unknown", {}), ("BallHit", {}), ("CrossbarHit", None)):
            s.feed(event, data, 1.0)
        self.assertIsInstance(s.snapshot(1.0), dict)


class TheFlashRate(unittest.TestCase):
    def test_every_flash_is_long_enough_for_a_slow_light(self):
        for kind, length in LENGTH.items():
            self.assertGreaterEqual(length, 0.35, kind)
        self.assertEqual(set(LENGTH), set(FLASHES))

    def test_never_more_than_three_flashes_in_a_second(self):
        s = started()
        for i in range(40):                                     # a wild moment: goals, saves, demos at once
            who = (ME, MATE)[i % 2]
            s.feed("GoalScored", goal(who), 2.0)
            s.feed("StatfeedEvent", statfeed("Save", ME), 2.0)
            s.feed("StatfeedEvent", statfeed("EpicSave", ME), 2.0)
            s.feed("StatfeedEvent", statfeed("Demolish", ME, RIVAL), 2.0)
        starts, seen = [], {}
        for i in range(0, 1200):
            now = 2.0 + i * 0.01
            ago = s.snapshot(now)["ago"]
            for kind in FLASHES:
                if kind in ago and seen.get(kind) != round(now - ago[kind], 3):
                    seen[kind] = round(now - ago[kind], 3)
                    starts.append(seen[kind])
        starts.sort()
        self.assertGreaterEqual(len(starts), 4)
        for i, at in enumerate(starts):
            self.assertLessEqual(len([x for x in starts[i:] if x < at + 1.0]), 3)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, rocket_league.MIN_GAP - 1e-6)

    def test_the_strongest_waiting_flash_goes_first(self):
        s = started()
        s.feed("StatfeedEvent", statfeed("Demolish", ME, RIVAL), 2.0)       # starts at once
        s.feed("StatfeedEvent", statfeed("Save", ME), 2.01)
        s.feed("GoalScored", goal(MATE), 2.02)
        self.assertEqual(set(s.snapshot(2.02)["ago"]), {"demo"})
        later = s.snapshot(2.0 + LENGTH["demo"] + rocket_league.PAUSE + 0.01)["ago"]
        self.assertIn("goal_for", later)
        self.assertNotIn("save", later)

    def test_a_flash_that_waited_too_long_is_dropped(self):
        s = started()
        for _ in range(3):
            s.feed("GoalScored", goal(ME), 2.0)
        s.feed("StatfeedEvent", statfeed("Save", ME), 2.0)
        self.assertNotIn("save", s.snapshot(2.0 + rocket_league.STALE + 1)["ago"])


# ---- the light --------------------------------------------------------------------------------

def render(snap, n=10, fps=100, t=0.0, span=(0.0, 1.0, "front"), mem=None):
    return EFFECTS["game_rl"].render(t, n, None, {} if mem is None else mem, {"rl": snap, "fps": fps, "span": span})


def lum(p):
    return sum(p)


def total(px):
    return sum(lum(p) for p in px)


class TheLight(unittest.TestCase):
    def test_the_effect_is_known(self):
        self.assertIn("game_rl", EFFECTS)
        self.assertEqual(EFFECTS["game_rl"].group, "game")
        self.assertEqual(EFFECTS["game_rl"].name, "Rocket League")
        self.assertIn("game_rl", GAME_EFFECTS)

    def test_no_match_drifts_between_the_two_teams(self):
        px = render(RocketLeagueState().snapshot(0.0), n=20)
        self.assertEqual(len(px), 20)
        self.assertTrue(any(p[2] > p[0] for p in px) and any(p[0] > p[2] for p in px))

    def test_the_room_is_in_your_teams_color(self):
        blue = render(started().snapshot(1.0), n=1)[0]
        orange = render(started(target=RIVAL).snapshot(1.0), n=1)[0]
        self.assertGreater(blue[2], blue[0] + 40)
        self.assertGreater(orange[0], orange[2] + 40)

    def test_the_games_team_color_is_used(self):
        px = render(started(colors=("00FF00", "FF00FF")).snapshot(1.0), n=1)[0]
        self.assertGreater(px[1], px[0] + 40)
        self.assertGreater(px[1], px[2] + 40)

    def test_boost_is_a_bar_across_the_stage(self):
        half = render(started(boost=50).snapshot(1.0), n=10)
        self.assertGreater(lum(half[0]), lum(half[9]) * 3)
        self.assertGreater(lum(half[4]), lum(half[5]) * 2)                       # (the edge sits at the middle)
        full = render(started(boost=100).snapshot(1.0), n=10)
        empty = render(started(boost=0).snapshot(1.0), n=10)
        self.assertGreater(lum(full[9]), lum(empty[9]) * 3)
        self.assertGreater(total(full), total(half))
        self.assertGreater(total(half), total(empty))
        self.assertGreater(lum(empty[0]), 0)                                      # (never fully dark: the room stays in its color)

    def test_boost_on_one_light_is_its_brightness(self):
        lo, hi = render(started(boost=10).snapshot(1.0), n=1)[0], render(started(boost=90).snapshot(1.0), n=1)[0]
        self.assertGreater(lum(hi), lum(lo) * 2)

    def test_the_bar_follows_where_the_light_sits_on_the_stage(self):
        right = render(started(boost=50).snapshot(1.0), n=4, span=(0.5, 1.0, "front"))
        left = render(started(boost=50).snapshot(1.0), n=4, span=(0.0, 0.5, "front"))
        self.assertGreater(total(left), total(right) * 2)

    def test_supersonic_ripples_along_the_bar_without_flashing(self):
        calm = render(started(boost=100).snapshot(1.0), n=10, t=0.3)
        fast = render(started(boost=100, supersonic=True).snapshot(1.0), n=10, t=0.3)
        self.assertNotEqual(calm, fast)
        s = started(boost=100, supersonic=True).snapshot(1.0)
        series = [lum(render(s, n=10, t=i * 0.01)[3]) for i in range(400)]        # four seconds of one pixel
        mean = sum(series) / len(series)
        crossings = sum(1 for a, b in zip(series, series[1:]) if (a - mean) * (b - mean) < 0)
        self.assertLessEqual(crossings / 4 / 2, 3.0)                               # (cycles a second: below the flash limit)
        self.assertLess(max(series) - min(series), 0.5 * max(series))              # (a ripple, not a blink)

    def test_a_goal_by_your_team_sweeps_across_in_its_color(self):
        s = started()
        s.feed("GoalScored", goal(MATE), 2.0)
        idle = render(started().snapshot(1.0), n=20)
        early, late = render(s.snapshot(2.2), n=20), render(s.snapshot(3.0), n=20)
        self.assertGreater(total(early), total(idle))
        peak = lambda px: max(range(len(px)), key=lambda i: lum(px[i]) if i > 9 else 0)
        self.assertLessEqual(peak(early), peak(late))
        self.assertEqual(render(s.snapshot(2.0 + LENGTH["goal_for"] + 0.1), n=20), idle)       # and it's over

    def test_your_own_goal_has_gold_on_top(self):
        mate, me = started(), started()
        mate.feed("GoalScored", goal(MATE), 2.0)
        me.feed("GoalScored", goal(ME), 2.0)
        a, b = render(mate.snapshot(2.1), n=1)[0], render(me.snapshot(2.1), n=1)[0]
        self.assertGreater(b[1], a[1])                                  # more green and red than blue's own: gold
        self.assertGreater(b[0], a[0])
        self.assertGreater(LENGTH["my_goal"], LENGTH["goal_for"])

    def test_their_goal_is_a_short_dim_in_their_color(self):
        s = started()
        idle = render(s.snapshot(1.5), n=10)
        s.feed("GoalScored", goal(RIVAL), 2.0)
        dim = render(s.snapshot(2.75), n=10)
        self.assertLess(total(dim), total(idle) * 0.85)
        self.assertEqual(render(s.snapshot(2.0 + 1.6), n=10), idle)

    def test_saves_and_demolitions_flash_briefly(self):
        base = render(started().snapshot(1.0), n=1)[0]
        for name, main, other in (("Save", ME, None), ("EpicSave", ME, None), ("Demolish", ME, RIVAL)):
            s = started()
            s.feed("StatfeedEvent", statfeed(name, main, other), 2.0)
            self.assertNotEqual(render(s.snapshot(2.05), n=1)[0], base, name)
            self.assertEqual(render(s.snapshot(3.5), n=1)[0], base, name)

    def test_a_slow_light_holds_a_flash_instead_of_letting_it_fade(self):
        s = started()
        s.feed("StatfeedEvent", statfeed("Demolish", ME, RIVAL), 2.0)
        slow = [render(s.snapshot(2.0 + a), n=1, fps=10)[0] for a in (0.0, 0.2, 0.35)]
        self.assertEqual(slow[0], slow[1])
        self.assertEqual(slow[0], slow[2])
        fast = [render(s.snapshot(2.0 + a), n=1, fps=100)[0] for a in (0.0, 0.2)]
        self.assertNotEqual(fast[0], fast[1])
        s = started()
        s.feed("GoalScored", goal(ME), 2.0)
        held = [render(s.snapshot(2.0 + a), n=10, fps=10) for a in (0.0, 0.5)]
        self.assertEqual(held[0], held[1])

    def test_a_demolished_car_dims_the_room_to_grey_until_it_is_back(self):
        alive = render(started().snapshot(1.0), n=10)
        s = started()
        s.feed("UpdateState", update(demolished=True), 2.0)
        down = render(s.snapshot(2.0), n=10)
        self.assertLess(total(down), total(alive) * 0.2)
        for r, g, b in down:
            self.assertLess(max(r, g, b) - min(r, g, b), 30)
        s.feed("UpdateState", update(), 5.0)
        self.assertEqual(render(s.snapshot(5.0), n=10), alive)

    def test_overtime_pulses_slowly(self):
        s = started()
        s.feed("UpdateState", update(overtime=True), 2.0)
        snap = s.snapshot(30.0)
        seen = [total(render(snap, n=10, t=i * 0.05)) for i in range(60)]          # three seconds: more than one slow breath
        self.assertGreater(max(seen), min(seen) * 1.1)
        plain = total(render(started().snapshot(1.0), n=10))
        self.assertLessEqual(max(seen), plain + 1)

    def test_a_win_holds_your_color_and_a_loss_dims(self):
        idle = render(started(boost=0).snapshot(1.0), n=10)
        s = started(boost=0)
        s.feed("MatchEnded", {"WinnerTeamNum": 0}, 100.0)
        won = render(s.snapshot(101.0), n=10)
        self.assertGreater(total(won), total(idle) * 2)
        self.assertTrue(all(p[2] > p[0] for p in won))                              # blue all along the stage, the empty end too
        lost = started(boost=100)
        lost.feed("MatchEnded", {"WinnerTeamNum": 1}, 100.0)
        dim = render(lost.snapshot(101.0), n=10)
        self.assertLess(total(dim), total(render(started(boost=100).snapshot(1.0), n=10)) * 0.4)
        self.assertEqual(render(s.snapshot(100.0 + END_LINGER + 20), n=10)[0:1], render(RocketLeagueState().snapshot(0.0), n=10, t=0.0)[0:1])

    def test_the_result_shows_even_after_the_match_state_is_gone(self):
        s = started()
        s.feed("MatchEnded", {"WinnerTeamNum": 0}, 100.0)
        snap = s.snapshot(100.5)
        snap["inMatch"] = False
        self.assertNotEqual(render(snap, n=4), render(RocketLeagueState().snapshot(0.0), n=4))

    def test_no_snapshot_at_all_is_fine(self):
        self.assertEqual(len(EFFECTS["game_rl"].render(0.0, 5, None, {}, {})), 5)


# ---- listening to the game --------------------------------------------------------------------

class FakeSocket:
    """What the game would send, a piece at a time (an Exception in the list is raised)."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if isinstance(c, Exception):
            raise c
        return c

    def close(self):
        self.closed = True


def refuse(port):
    raise ConnectionRefusedError(10061, "refused")


class Reading(unittest.TestCase):
    def reader(self, *chunks, clock=lambda: 100.0):
        self.sock = FakeSocket(chunks)
        self.ports = []
        self.changes = []
        r = RocketLeagueReader(on_change=lambda: self.changes.append(1), port=49123,
                               connect=lambda port: self.ports.append(port) or self.sock, clock=clock)
        return r

    def test_a_match_comes_through_even_in_odd_pieces(self):
        data = wire("MatchCreated", {"MatchGuid": "match-1"}) + wire("UpdateState", update(boost=80)) + wire("GoalScored", goal(MATE))
        r = self.reader(data[:50], data[50:3000], data[3000:])
        self.assertTrue(r.session())
        snap = r.snapshot(100.0)
        self.assertTrue(snap["inMatch"])
        self.assertEqual((snap["team"], snap["boost"]), (0, 0.8))
        self.assertIn("goal_for", snap["ago"])
        self.assertEqual(self.ports, [49123])
        self.assertTrue(self.sock.closed)
        self.assertFalse(r.connected)

    def test_it_says_when_a_match_begins_and_ends(self):
        now = [100.0]
        r = self.reader(wire("UpdateState", update()), clock=lambda: now[0])
        r.session()
        self.assertEqual(len(self.changes), 1)
        now[0] += ALIVE + 1
        r._noticed()
        self.assertEqual(len(self.changes), 2)
        self.assertFalse(r.in_match(now[0]))

    def test_a_quiet_second_isnt_the_end_but_the_game_closing_is(self):
        r = self.reader(wire("UpdateState", update()), TimeoutError(), TimeoutError(), wire("GoalScored", goal(ME)))
        self.assertTrue(r.session())
        self.assertIn("my_goal", r.snapshot(100.0)["ago"])
        self.assertEqual(self.sock.timeout, rocket_league.READ_WAIT)

    def test_a_connection_that_breaks_is_survived(self):
        r = self.reader(wire("UpdateState", update()), ConnectionResetError(10054, "reset"))
        self.assertTrue(r.session())
        self.assertTrue(self.sock.closed)
        self.assertFalse(r.connected)

    def test_nobody_home_is_not_an_error(self):
        r = RocketLeagueReader(connect=refuse)
        self.assertFalse(r.session())
        self.assertFalse(r.connected)
        self.assertFalse(r.in_match(time.monotonic()))
        self.assertFalse(r.receiving(time.monotonic()))

    def test_receiving_means_connected_and_hearing_it(self):
        r = self.reader(wire("UpdateState", update()))
        sock = self.sock
        seen = []
        orig = sock.recv

        def recv(n):
            seen.append(r.receiving(100.0))
            return orig(n)
        sock.recv = recv
        r.session()
        self.assertEqual(seen, [False, True])              # (nothing heard yet at the first ask, hearing it at the second)
        self.assertFalse(r.receiving(100.0))

    def test_a_stop_ends_a_session_that_is_waiting(self):
        stop = threading.Event()
        stop.set()
        r = self.reader(wire("UpdateState", update()))
        self.assertTrue(r.session(stop))
        self.assertFalse(r.in_match(100.0))                # (it never read anything)

    def test_the_thread_tries_again_and_stops(self):
        tries = []

        def connect(port):
            tries.append(port)
            raise ConnectionRefusedError()
        r = RocketLeagueReader(connect=connect, port=50000)
        with mock.patch.object(rocket_league, "RETRY", 0.02):
            r.start()
            r.start()                                      # (twice: still one thread)
            time.sleep(0.3)
            r.stop()
        n = len(tries)
        self.assertGreaterEqual(n, 3)
        self.assertEqual(set(tries), {50000})
        time.sleep(0.2)
        self.assertLessEqual(len(tries) - n, 1)            # stopped

    def test_the_thread_reads_a_match_from_a_fake_game(self):
        sock = FakeSocket([wire("UpdateState", update(boost=55))])
        r = RocketLeagueReader(connect=lambda port: sock if not sock.closed else refuse(port))
        with mock.patch.object(rocket_league, "RETRY", 0.02):
            r.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not r.state.playing:
                time.sleep(0.02)
            r.stop()
        self.assertAlmostEqual(r.state.boost, 0.55)

    def test_the_real_connection_only_goes_to_this_pc(self):
        with mock.patch.object(socket, "create_connection", return_value="sock") as c:
            self.assertEqual(rocket_league._connect(49123), "sock")
        self.assertEqual(c.call_args.args[0], ("127.0.0.1", 49123))
        self.assertIs(RocketLeagueReader().connect, rocket_league._connect)


# ---- connecting from Setup → Games --------------------------------------------------------------

class Connecting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.install = Path(self.tmp.name) / "rocketleague"
        (self.install / "TAGame" / "Config").mkdir(parents=True)
        self.ini = rocket_league.ini_path(self.install)
        self.ini.write_bytes(ORIGINAL.encode("utf-8"))
        self.patches = [mock.patch.object(games, "find_rocket_league", return_value=self.install),
                        mock.patch.object(games, "find_cs2", return_value=None),
                        mock.patch.object(games, "find_dota2", return_value=None)]
        for p in self.patches:
            p.start()
        self.changed = []
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: self.changed.append(1))
        self.tried = []
        self.g.rl.connect = lambda port: self.tried.append(port) or refuse(port)     # (never a real port)

    def tearDown(self):
        self.g.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def files(self):
        return sorted(str(p.relative_to(self.install)) for p in self.install.rglob("*") if p.is_file())

    def test_it_is_off_by_default(self):
        d = Config(Path(self.tmp.name) / "other.json").data["games"]
        self.assertIs(d["rl"], False)
        self.assertEqual(d["rl_port"], 49123)
        self.assertIsNone(d["rl_original"])

    def test_connecting_switches_the_stats_api_on_and_disconnecting_puts_it_back(self):
        self.g.connect_rl()
        self.assertEqual(rocket_league.read_stats_api(self.ini), {"PacketSendRate": "30", "Port": "49123"})
        self.assertTrue(self.g.settings["rl"])
        self.assertEqual(self.g.settings["rl_original"], {"PacketSendRate": "0", "Port": "49123"})
        d = self.g.describe()["rl"]
        self.assertTrue(d["found"] and d["connected"] and d["fileThere"])
        self.assertTrue(self.changed)
        self.g.disconnect_rl()
        self.assertEqual(self.ini.read_bytes().decode("utf-8"), ORIGINAL)           # byte for byte what it was
        self.assertFalse(self.g.settings["rl"])
        self.assertIsNone(self.g.settings["rl_original"])
        d = self.g.describe()["rl"]
        self.assertFalse(d["connected"] or d["fileThere"])

    def test_no_other_game_file_is_touched(self):
        (self.install / "TAGame" / "Config" / "DefaultEngine.ini").write_text("[A]\nB=1\n", encoding="utf-8")
        before = self.files()
        stamp = (self.install / "TAGame" / "Config" / "DefaultEngine.ini").read_bytes()
        self.g.connect_rl()
        self.g.disconnect_rl()
        self.assertEqual(self.files(), before)
        self.assertEqual((self.install / "TAGame" / "Config" / "DefaultEngine.ini").read_bytes(), stamp)

    def test_a_game_without_the_ini_gets_it_made(self):
        self.ini.unlink()
        self.g.connect_rl()
        self.assertTrue(rocket_league.is_on(self.ini))
        self.assertEqual(self.files(), ["TAGame\\Config\\DefaultStatsAPI.ini"])
        self.g.disconnect_rl()
        self.assertEqual(rocket_league.read_stats_api(self.ini)["PacketSendRate"], "0")

    def test_connecting_twice_keeps_the_real_original(self):
        self.g.connect_rl()
        self.g.connect_rl()
        self.assertEqual(self.g.settings["rl_original"]["PacketSendRate"], "0")
        self.g.disconnect_rl()
        self.assertEqual(rocket_league.read_stats_api(self.ini)["PacketSendRate"], "0")

    def test_a_port_in_the_file_is_the_one_we_listen_on(self):
        self.ini.write_text("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=0\nPort=50123\n", encoding="utf-8")
        self.g.connect_rl()
        self.assertEqual(self.g.settings["rl_port"], 50123)
        self.assertEqual(self.g.rl.port, 50123)
        time.sleep(0.1)
        self.assertIn(50123, self.tried)

    def test_connecting_without_the_game_says_so(self):
        with mock.patch.object(games, "find_rocket_league", return_value=None):
            with self.assertRaises(ValueError) as e:
                self.g.connect_rl()
        self.assertIn("Rocket League", str(e.exception))
        self.assertFalse(self.g.settings["rl"])

    def test_a_file_we_may_not_write_says_so_in_plain_words(self):
        with mock.patch.object(rocket_league, "switch_on", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(ValueError) as e:
                self.g.connect_rl()
        self.assertIn("administrator", str(e.exception))
        self.assertFalse(self.g.settings["rl"])
        self.assertIsNone(self.g.settings["rl_original"])

    def test_a_file_we_cannot_put_back_leaves_it_connected_to_try_again(self):
        self.g.connect_rl()
        with mock.patch.object(rocket_league, "switch_off", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(ValueError):
                self.g.disconnect_rl()
        self.assertTrue(self.g.settings["rl"])
        self.assertIsNotNone(self.g.settings["rl_original"])
        self.g.disconnect_rl()
        self.assertFalse(self.g.settings["rl"])

    def test_the_reader_runs_only_while_connected(self):
        self.g.start()
        time.sleep(0.1)
        self.assertEqual(self.tried, [])
        self.g.connect_rl()
        time.sleep(0.2)
        self.assertTrue(self.tried)
        self.g.disconnect_rl()
        time.sleep(0.1)
        n = len(self.tried)
        time.sleep(0.2)
        self.assertEqual(len(self.tried), n)

    def test_a_connected_game_listens_again_after_a_restart(self):
        self.g.settings.update(rl=True, rl_port=50555)
        self.g.start()
        time.sleep(0.2)
        self.assertIn(50555, self.tried)

    def test_the_describe_follows_the_match(self):
        d = self.g.describe()
        self.assertEqual(set(d), {"cs2", "lol", "dota", "rl"})
        self.assertEqual(set(d["rl"]), {"found", "connected", "fileThere", "receiving", "inMatch", "team", "boost"})
        self.g.connect_rl()
        self.g.rl.state.feed("UpdateState", update(boost=40, target=RIVAL), time.monotonic())
        d = self.g.describe()["rl"]
        self.assertEqual((d["inMatch"], d["team"], d["boost"]), (True, "orange", 100 * 64 // 100))
        self.assertTrue(self.g.rl_active(time.monotonic()))
        self.g.disconnect_rl()
        self.assertFalse(self.g.rl_active(time.monotonic()))
        self.assertFalse(self.g.describe()["rl"]["inMatch"])

    def test_the_other_games_are_untouched(self):
        d = self.g.describe()
        self.assertFalse(d["cs2"]["connected"] or d["dota"]["connected"] or d["lol"]["on"])
        self.g.connect_rl()
        self.assertFalse(self.g.settings["cs2"] or self.g.settings["dota"])
        self.assertIsNone(self.g._server)                         # (Rocket League needs no listener of ours)


class SlowDisk(unittest.TestCase):
    """A Steam library on a hard disk that spun down: the window must never wait for it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.install = Path(self.tmp.name) / "rocketleague"
        (self.install / "TAGame" / "Config").mkdir(parents=True)
        self._find = games.find_cs2, games.find_dota2, games.find_rocket_league
        games.find_cs2 = games.find_dota2 = lambda: None
        games.find_rocket_league = self.slow_find
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: None)
        self.g.rl.connect = refuse

    def tearDown(self):
        self.g.stop()
        games.find_cs2, games.find_dota2, games.find_rocket_league = self._find
        self.tmp.cleanup()

    def slow_find(self):
        time.sleep(1.0)
        return self.install

    def test_the_window_doesnt_wait(self):
        started_at = time.monotonic()
        first = self.g.describe()["rl"]
        self.assertLess(time.monotonic() - started_at, 0.2)
        self.assertFalse(first["found"])                           # not known yet: looked up in the background
        time.sleep(1.4)
        started_at = time.monotonic()
        self.assertTrue(self.g.describe()["rl"]["found"])
        self.assertLess(time.monotonic() - started_at, 0.2)

    def test_it_is_looked_up_again_in_the_background_when_old(self):
        self.g.rl_folder(fresh=True)
        at, folder = self.g._found_rl
        self.g._found_rl = (at - 61, folder)
        started_at = time.monotonic()
        self.g.rl_folder()
        self.assertLess(time.monotonic() - started_at, 0.2)
        time.sleep(1.4)
        self.assertGreater(self.g._found_rl[0], at)

    def test_connecting_looks_right_away(self):
        self.g.connect_rl()
        d = self.g.describe()["rl"]
        self.assertTrue(d["found"] and d["connected"] and d["fileThere"])
        self.g.disconnect_rl()
        self.assertFalse(self.g.describe()["rl"]["fileThere"])

    def test_starting_never_waits_for_the_disk(self):
        self.g.settings["rl"] = True
        started_at = time.monotonic()
        self.g.start()
        self.assertLess(time.monotonic() - started_at, 0.3)
        self.g.stop()


# ---- the automatic look -----------------------------------------------------------------------

class Manager(FakeManager):
    def __init__(self):
        super().__init__()
        self.games.settings["rl"] = True

    def game_lights(self, effect="game_cs2"):
        self.look = {"lamp": {"on": True, "mode": "effect", "effect": effect}}


class Automatic(unittest.TestCase):
    def setUp(self):
        self.m = Manager()
        self.a = Automations(self.m)
        self.t = 0

    def run_for(self, seconds, rl=False, dota=False, lol=False, cs2=False, kind=None):
        for _ in range(seconds):
            self.a.step(self.t, self.a.rules(), kind, 0.0, cs2, frozenset(), False, lol, dota, rl)
            self.t += 1

    def effect(self):
        return self.m.look["lamp"].get("effect") or self.m.look["lamp"].get("color")

    def test_the_rule_comes_after_dota_and_only_when_connected(self):
        self.assertEqual(self.a.rules()["rl"], "game:rl")
        self.assertEqual(list(self.a.rules())[:5], ["live", "cs2", "lol", "dota", "rl"])
        self.assertEqual(RULES[:6], ("live", "call", "cs2", "lol", "dota", "rl"))
        self.m.games.settings["rl"] = False
        self.assertEqual(self.a.rules()["rl"], "")

    def test_a_match_changes_the_lights_and_the_end_puts_them_back(self):
        self.run_for(1, rl=True)
        self.assertEqual(self.effect(), "#ffffff")                        # not yet: a match has to last a second
        self.run_for(2, rl=True)
        self.assertEqual(self.effect(), "game_rl")
        self.assertEqual(self.a.active, "rl")
        self.run_for(4)
        self.assertEqual(self.effect(), "game_rl")                        # a short gap: still playing along
        self.run_for(4)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_nothing_happens_when_not_connected(self):
        self.m.games.settings["rl"] = False
        self.run_for(10, rl=True)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_the_other_games_go_before_it_and_a_full_screen_game_after(self):
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.m.games.settings.update(cs2=True, lol=True, dota=True)
        self.run_for(10, rl=True, kind="game")
        self.assertEqual(self.a.active, "rl")
        self.assertEqual(self.effect(), "game_rl")
        self.run_for(10, rl=True, dota=True, kind="game")
        self.assertEqual(self.a.active, "dota")
        self.run_for(10, rl=True, dota=True, lol=True, kind="game")
        self.assertEqual(self.a.active, "lol")
        self.run_for(10, rl=True, dota=True, lol=True, cs2=True, kind="game")
        self.assertEqual(self.a.active, "cs2")

    def test_a_match_keeps_music_from_taking_over(self):
        self.m.config.data["settings"]["auto_show"] = True
        self.run_for(5, rl=True)
        self.assertTrue(self.a._rl)
        self.a._listen(self.t, self.a.rules(), 1.0)
        self.assertEqual(self.a._checks, 0)

    def test_a_change_by_hand_is_left_alone(self):
        self.run_for(3, rl=True)
        self.m.look = {"lamp": {"on": True, "mode": "color", "color": "#123456"}}
        self.run_for(10, rl=True)
        self.assertEqual(self.effect(), "#123456")

    def test_switching_everything_off_forgets_the_match(self):
        self.run_for(3, rl=True)
        self.m.games.settings["rl"] = False
        self.run_for(1, rl=True)
        self.assertFalse(self.a._rl)

    def test_the_loop_tells_step_about_a_rocket_league_match(self):
        class Stop:
            def __init__(self, n):
                self.n = n

            def wait(self, _):
                self.n -= 1
                return self.n < 0

            def is_set(self):
                return self.n < 0

        seen = []
        self.m.away = type("A", (), {"active": False})()
        self.m.games.active = lambda now: False
        self.m.games.lol_active = lambda now: False
        self.m.games.dota_active = lambda now: False
        self.m.games.rl_active = lambda now: True
        self.a._stop = Stop(2)
        with mock.patch.object(Automations, "step", lambda s, *args: seen.append(args)):
            self.a._run()
        self.assertTrue(seen and all(args[9] is True and args[8] is False for args in seen))   # (rl is the tenth, after dota)


class TheApi(unittest.TestCase):
    def test_the_window_can_connect_and_disconnect(self):
        from glowstage.api import Api
        g = mock.Mock()
        api = Api(SimpleNamespace(manager=SimpleNamespace(games=g)))
        self.assertEqual(api.games_rl_connect(), {"ok": True, "result": None})
        g.connect_rl.assert_called_once()
        self.assertEqual(api.games_rl_disconnect(), {"ok": True, "result": None})
        g.disconnect_rl.assert_called_once()
        g.connect_rl.side_effect = ValueError("Rocket League isn't installed on this PC")
        self.assertEqual(api.games_rl_connect(), {"ok": False, "error": "Rocket League isn't installed on this PC"})

    def test_the_engine_hands_the_effect_its_snapshot_only_while_it_runs(self):
        import inspect
        from glowstage.engine import EffectEngine
        src = inspect.getsource(EffectEngine._frame)
        self.assertIn('st.effect == "game_rl"', src)
        self.assertIn('ctx["rl"]', src)


if __name__ == "__main__":
    unittest.main()
