import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from glowstage import games, racing
from glowstage.automations import RULES, Automations
from glowstage.config import Config
from glowstage.effects import EFFECTS, GAME_EFFECTS
from glowstage.racing import (ALIVE, END_LINGER, F1_PORT, FORZA_PORT, HEADER, RacingListener, RacingState, parse_f1,
                              parse_forza, rpm_fraction)
from tests.test_app_looks import FakeManager

# ---- packets, built the way the games write them ----------------------------------------------------

LENGTHS = {"sled": 232, "dash": 311, "horizon": 324, "motorsport": 331}


def forza(length, race_on=1, rpm=5000.0, idle=800.0, top=8000.0, speed=30.0, gear=3):
    """A Forza packet of this length: the sled part, and where there is one, the dash part (12 bytes
    further on in Horizon's)."""
    b = bytearray(length)
    struct.pack_into("<iIfff", b, 0, race_on, 123456, top, idle, rpm)
    if length > 232:
        shift = 12 if length == 324 else 0
        struct.pack_into("<f", b, 244 + shift, speed)
        b[307 + shift] = gear
    return bytes(b)


def f1_header(pid, player=0, fmt=2024, year=24):
    return struct.pack("<HBBBBBQfIIBB", fmt, year, 1, 7, 1, pid, 99, 12.5, 100, 100, player, 255)


def car_telemetry(rpm=11000, rev=40, gear=4, speed=250, drs=0):
    return struct.pack("<HfffBbHBBH", speed, 1.0, 0.0, 0.0, 0, gear, rpm, drs, rev, 0) + bytes(60 - 22)


def f1_telemetry(player=0, fmt=2024, year=24, **mine):
    """22 cars: every other car sits at 70 % and 3rd gear, so the player's car must be picked by index."""
    cars = [car_telemetry(rev=70, gear=3, speed=100) for _ in range(22)]
    cars[player] = car_telemetry(**mine)
    return f1_header(6, player, fmt, year) + b"".join(cars) + bytes(3)


def f1_event(code, detail=b"", player=0, fmt=2024, year=24):
    return f1_header(3, player, fmt, year) + code.encode() + detail.ljust(12, b"\0")


def f1_status(fia, player=0, fmt=2024, year=24):
    cars = [bytearray(55) for _ in range(22)]
    cars[player][28] = fia & 0xFF
    return f1_header(7, player, fmt, year) + b"".join(bytes(c) for c in cars)


# ---- Forza ---------------------------------------------------------------------------------------------

class ForzaPackets(unittest.TestCase):
    def test_every_length_is_read(self):
        for name, length in LENGTHS.items():
            p = parse_forza(forza(length))
            self.assertIsNotNone(p, name)
            self.assertEqual((p["source"], p["format"], p["race_on"]), ("forza", name, True))
            self.assertAlmostEqual(p["rpm"], 5000.0)
            self.assertAlmostEqual(p["max"], 8000.0)
            self.assertAlmostEqual(p["idle"], 800.0)

    def test_the_sled_has_no_dash_part(self):
        p = parse_forza(forza(232))
        self.assertNotIn("speed", p)
        self.assertNotIn("gear", p)

    def test_speed_and_gear_come_from_where_the_dash_puts_them(self):
        for length in (311, 324, 331):
            p = parse_forza(forza(length, speed=30.0, gear=5))
            self.assertAlmostEqual(p["speed"], 108.0, places=3, msg=length)      # 30 m/s in km/h
            self.assertEqual(p["gear"], 5, length)

    def test_horizon_reads_its_dash_twelve_bytes_further_on(self):
        b = bytearray(forza(324, speed=10.0, gear=2))
        self.assertAlmostEqual(struct.unpack_from("<f", b, 256)[0], 10.0)
        self.assertEqual(b[319], 2)

    def test_reverse_and_odd_gears(self):
        self.assertEqual(parse_forza(forza(311, gear=0))["gear"], -1)
        self.assertEqual(parse_forza(forza(311, gear=11))["gear"], 0)

    def test_a_wrong_length_is_ignored(self):
        for n in (0, 1, 16, 100, 231, 233, 310, 312, 323, 325, 330, 332, 500):
            self.assertIsNone(parse_forza(bytes(n)), n)

    def test_nonsense_numbers_are_ignored(self):
        self.assertIsNone(parse_forza(forza(232, race_on=2)))
        self.assertIsNone(parse_forza(forza(232, rpm=float("nan"))))
        self.assertIsNone(parse_forza(forza(232, top=float("inf"))))
        self.assertIsNone(parse_forza(forza(232, rpm=-5.0)))
        self.assertIsNone(parse_forza(forza(232, top=1e9)))

    def test_a_packet_while_paused_says_so(self):
        p = parse_forza(forza(311, race_on=0, rpm=0, idle=0, top=0, speed=0, gear=0))
        self.assertFalse(p["race_on"])

    def test_a_silly_speed_is_zero(self):
        self.assertEqual(parse_forza(forza(311, speed=float("nan")))["speed"], 0.0)
        self.assertEqual(parse_forza(forza(311, speed=-3.0))["speed"], 0.0)

    def test_the_rpm_fraction(self):
        self.assertEqual(rpm_fraction(800, 800, 8000), 0.0)
        self.assertEqual(rpm_fraction(8000, 800, 8000), 1.0)
        self.assertAlmostEqual(rpm_fraction(4400, 800, 8000), 0.5)
        self.assertEqual(rpm_fraction(9000, 800, 8000), 1.0)
        self.assertEqual(rpm_fraction(100, 800, 8000), 0.0)
        self.assertEqual(rpm_fraction(5, 0, 0), 0.0)
        self.assertAlmostEqual(rpm_fraction(4000, 9000, 8000), 0.5)            # (an idle above the limit: plain share of the limit)


# ---- F1 ------------------------------------------------------------------------------------------------

class F1Packets(unittest.TestCase):
    def test_the_header_is_29_bytes(self):
        self.assertEqual(HEADER, 29)
        self.assertEqual(len(f1_header(6)), 29)

    def test_the_players_car_is_picked_by_index(self):
        for idx in (0, 5, 21):
            p = parse_f1(f1_telemetry(player=idx, rev=85, gear=6, speed=301, rpm=12000, drs=1))
            self.assertEqual((p["kind"], p["game"]), ("telemetry", "F1 24"))
            self.assertAlmostEqual(p["rev"], 0.85)
            self.assertEqual((p["gear"], p["speed"], p["rpm"], p["drs"]), (6, 301, 12000, True), idx)

    def test_the_game_is_named_from_the_header(self):
        self.assertEqual(parse_f1(f1_telemetry(fmt=2023, year=23))["game"], "F1 23")
        self.assertEqual(parse_f1(f1_telemetry(fmt=2025, year=25))["game"], "F1 25")

    def test_other_formats_are_ignored(self):
        for fmt in (2020, 2021, 2022, 2026, 0, 65535):
            self.assertIsNone(parse_f1(f1_telemetry(fmt=fmt)), fmt)

    def test_reverse_gear_is_minus_one(self):
        self.assertEqual(parse_f1(f1_telemetry(gear=-1))["gear"], -1)

    def test_short_or_broken_packets_are_ignored(self):
        self.assertIsNone(parse_f1(b""))
        self.assertIsNone(parse_f1(bytes(28)))
        self.assertIsNone(parse_f1(f1_telemetry()[:-70]))                       # (cut in the middle of the cars)
        self.assertIsNone(parse_f1(f1_header(6, player=22) + f1_telemetry()[HEADER:]))   # (a car that isn't there)
        self.assertIsNone(parse_f1(f1_header(6, player=255) + bytes(1400)))
        self.assertIsNone(parse_f1(f1_telemetry(rev=101)))                      # (a percentage over 100)
        self.assertIsNone(parse_f1(f1_telemetry(drs=7)))

    def test_a_forza_packet_is_not_an_f1_packet_and_the_other_way_round(self):
        for n in LENGTHS.values():
            self.assertIsNone(parse_f1(forza(n)), n)
        self.assertIsNone(parse_forza(f1_telemetry()))

    def test_ids_we_dont_use_are_ignored(self):
        for pid in (0, 1, 2, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 99, 255):
            self.assertIsNone(parse_f1(f1_header(pid) + bytes(1400)), pid)

    def test_events(self):
        for code in ("SSTA", "SEND", "DRSE", "DRSD", "CHQF", "LGOT", "RCWN"):
            p = parse_f1(f1_event(code))
            self.assertEqual((p["kind"], p["code"]), ("event", code))

    def test_start_lights_one_to_five(self):
        for n in range(1, 6):
            self.assertEqual(parse_f1(f1_event("STLG", bytes([n])))["lights"], n)
        self.assertIsNone(parse_f1(f1_event("STLG", bytes([0]))))
        self.assertIsNone(parse_f1(f1_event("STLG", bytes([6]))))
        self.assertIsNone(parse_f1(f1_header(3) + b"STLG"))                     # (no count)

    def test_fastest_lap_says_whose(self):
        p = parse_f1(f1_event("FTLP", bytes([7]) + struct.pack("<f", 81.2), player=3))
        self.assertEqual((p["vehicle"], p["player"]), (7, 3))
        self.assertIsNone(parse_f1(f1_header(3) + b"FTLP"))

    def test_safety_car(self):
        p = parse_f1(f1_event("SCAR", bytes([1, 0])))
        self.assertEqual((p["car"], p["change"]), (1, 0))
        self.assertIsNone(parse_f1(f1_header(3) + b"SCAR" + b"\x01"))

    def test_unknown_codes_and_junk_are_ignored(self):
        self.assertIsNone(parse_f1(f1_event("ZZZZ")))
        self.assertIsNone(parse_f1(f1_event("PENA")))
        self.assertIsNone(parse_f1(f1_header(3) + b"\xff\xfe\xfd\xfc" + bytes(12)))
        self.assertIsNone(parse_f1(f1_header(3) + b"SS"))

    def test_the_fia_flag(self):
        for flag, name in ((-1, "unknown"), (0, "none"), (1, "green"), (2, "blue"), (3, "yellow")):
            self.assertEqual(parse_f1(f1_status(flag, player=4))["fia"], flag, name)
        self.assertEqual(len(f1_status(0)), racing.STATUS_LENGTH)

    def test_the_flag_is_read_only_from_a_packet_of_the_size_we_know(self):
        self.assertIsNone(parse_f1(f1_status(3) + b"\0"))
        self.assertIsNone(parse_f1(f1_status(3)[:-1]))
        self.assertIsNone(parse_f1(f1_status(3, fmt=2025, year=25) + bytes(22 * 5)))


# ---- the race ------------------------------------------------------------------------------------------

def feed_forza(s, now, **kw):
    s.feed(parse_forza(forza(kw.pop("length", 311), **kw)), now)


def feed_f1(s, packet, now):
    s.feed(parse_f1(packet), now)


class TheState(unittest.TestCase):
    def test_nothing_before_a_packet(self):
        s = RacingState()
        snap = s.snapshot(0.0)
        self.assertFalse(snap["inRace"])
        self.assertFalse(s.in_race(0.0))
        self.assertEqual(snap["startLights"], 0)

    def test_forza_in_a_race(self):
        s = RacingState()
        feed_forza(s, 1.0, rpm=4400.0)
        snap = s.snapshot(1.1)
        self.assertTrue(snap["inRace"])
        self.assertEqual((snap["source"], snap["game"], snap["gear"]), ("forza", "Forza", 3))
        self.assertAlmostEqual(snap["rpm"], 0.5)
        self.assertAlmostEqual(snap["speed"], 108.0, places=3)
        self.assertFalse(snap["limiter"])

    def test_forza_stops_the_moment_the_race_is_off(self):
        s = RacingState()
        feed_forza(s, 1.0)
        self.assertTrue(s.in_race(1.1))
        feed_forza(s, 1.2, race_on=0, rpm=0.0)
        self.assertFalse(s.in_race(1.3))
        self.assertEqual(s.snapshot(1.3)["rpm"], 0.0)
        feed_forza(s, 1.4)
        self.assertTrue(s.in_race(1.5))

    def test_a_race_is_over_when_the_packets_stop(self):
        s = RacingState()
        feed_forza(s, 10.0)
        self.assertTrue(s.in_race(10.0 + ALIVE - 0.1))
        self.assertFalse(s.in_race(10.0 + ALIVE + 0.1))
        feed_f1(s, f1_telemetry(), 50.0)
        self.assertTrue(s.in_race(50.0 + ALIVE - 0.1))
        self.assertFalse(s.in_race(50.0 + ALIVE + 0.1))

    def test_forza_at_the_limiter_with_a_little_give(self):
        s = RacingState()
        feed_forza(s, 1.0, rpm=7990.0)
        self.assertTrue(s.snapshot(1.0)["limiter"])
        feed_forza(s, 1.1, rpm=7800.0)                                          # bounces off a little: still the limiter
        self.assertTrue(s.snapshot(1.1)["limiter"])
        feed_forza(s, 1.2, rpm=6000.0)
        self.assertFalse(s.snapshot(1.2)["limiter"])

    def test_f1_prefers_the_games_own_rev_lights(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(rev=64, rpm=3000), 1.0)                         # (a low rpm number, but the lights say 64 %)
        snap = s.snapshot(1.0)
        self.assertAlmostEqual(snap["rpm"], 0.64)
        self.assertEqual(snap["game"], "F1 24")
        self.assertFalse(snap["limiter"])
        feed_f1(s, f1_telemetry(rev=100), 1.1)
        self.assertTrue(s.snapshot(1.1)["limiter"])

    def test_f1_session_start_and_end(self):
        s = RacingState()
        feed_f1(s, f1_event("SSTA"), 1.0)
        feed_f1(s, f1_telemetry(), 1.1)
        self.assertTrue(s.in_race(1.2))
        feed_f1(s, f1_event("SEND"), 5.0)
        feed_f1(s, f1_telemetry(), 5.1)
        feed_f1(s, f1_telemetry(), 5.0 + END_LINGER - 0.6)
        self.assertTrue(s.in_race(5.0 + END_LINGER - 0.5))                      # (the last moment still shows)
        feed_f1(s, f1_telemetry(), 5.0 + END_LINGER + 0.4)
        self.assertFalse(s.in_race(5.0 + END_LINGER + 0.5))
        feed_f1(s, f1_telemetry(), 30.0)                                        # packets after the end don't bring it back
        self.assertFalse(s.in_race(30.1))
        feed_f1(s, f1_event("SSTA"), 40.0)                                      # a new session does
        feed_f1(s, f1_telemetry(), 40.1)
        self.assertTrue(s.in_race(40.2))

    def test_switching_games_forgets_the_old_one(self):
        s = RacingState()
        feed_f1(s, f1_event("STLG", bytes([3])), 1.0)
        feed_f1(s, f1_telemetry(), 1.0)
        feed_forza(s, 2.0)
        self.assertEqual((s.snapshot(2.0)["game"], s.snapshot(2.0)["startLights"]), ("Forza", 0))

    def test_start_lights_come_on_one_by_one_and_go_out_together(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(rev=0), 0.5)
        shown = []
        for n in range(1, 6):
            feed_f1(s, f1_telemetry(rev=0), 1.0 + n)
            feed_f1(s, f1_event("STLG", bytes([n])), 1.0 + n)
            shown.append(s.snapshot(1.0 + n)["startLights"])
        self.assertEqual(shown, [1, 2, 3, 4, 5])
        feed_f1(s, f1_telemetry(rev=0), 6.5)
        feed_f1(s, f1_event("LGOT"), 7.0)
        snap = s.snapshot(7.0)
        self.assertEqual(snap["startLights"], 0)                                # all five go off in one go
        self.assertAlmostEqual(snap["ago"]["lights_out"], 0.0)

    def test_start_lights_that_never_go_out_are_forgotten(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_event("STLG", bytes([4])), 1.0)
        self.assertEqual(s.snapshot(2.0)["startLights"], 4)
        feed_f1(s, f1_telemetry(), 1 + racing.START_HOLD + 0.5)
        self.assertEqual(s.snapshot(1 + racing.START_HOLD + 0.6)["startLights"], 0)

    def test_drs(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(drs=0), 1.0)
        self.assertFalse(s.snapshot(1.0)["drs"])
        feed_f1(s, f1_event("DRSE"), 1.1)
        self.assertTrue(s.snapshot(1.1)["drsAllowed"])
        feed_f1(s, f1_telemetry(drs=1), 1.2)
        self.assertTrue(s.snapshot(1.2)["drs"])
        feed_f1(s, f1_telemetry(drs=0), 1.3)
        feed_f1(s, f1_event("DRSD"), 1.3)
        snap = s.snapshot(1.3)
        self.assertFalse(snap["drs"])
        self.assertFalse(snap["drsAllowed"])

    def test_only_your_own_fastest_lap_counts(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_event("FTLP", bytes([7]), player=0), 2.0)                 # somebody else's
        self.assertNotIn("fastest", s.snapshot(2.0)["ago"])
        feed_f1(s, f1_event("FTLP", bytes([0]), player=0), 3.0)
        self.assertAlmostEqual(s.snapshot(3.0)["ago"]["fastest"], 0.0)
        self.assertAlmostEqual(s.snapshot(3.5)["ago"]["fastest"], 0.5)

    def test_the_chequered_flag_and_the_winner(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_event("CHQF"), 2.0)
        feed_f1(s, f1_event("RCWN", bytes([0])), 2.5)
        ago = s.snapshot(3.0)["ago"]
        self.assertAlmostEqual(ago["chequered"], 1.0)
        self.assertAlmostEqual(ago["winner"], 0.5)

    def test_the_safety_car_comes_and_goes(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        self.assertFalse(s.snapshot(1.0)["caution"])
        for now, (kind, change), expected in ((2.0, (1, 0), True),            # full safety car, deployed
                                              (3.0, (1, 1), True),            # coming in
                                              (4.0, (1, 3), False),           # race resumes
                                              (5.0, (2, 0), True),            # virtual safety car
                                              (6.0, (2, 2), False)):
            feed_f1(s, f1_telemetry(), now)
            feed_f1(s, f1_event("SCAR", bytes([kind, change])), now)
            self.assertEqual(s.snapshot(now)["caution"], expected, now)

    def test_the_formation_lap_is_not_a_safety_car(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_event("SCAR", bytes([3, 0])), 1.0)
        self.assertFalse(s.snapshot(1.0)["caution"])

    def test_a_yellow_flag_at_your_car_lasts_while_the_game_says_so(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_status(3), 1.0)
        self.assertTrue(s.snapshot(1.5)["caution"])
        feed_f1(s, f1_status(0), 2.0)
        self.assertFalse(s.snapshot(2.0)["caution"])
        feed_f1(s, f1_status(3), 3.0)                                           # (and a flag that is never taken back runs out)
        feed_f1(s, f1_telemetry(), 3.0 + racing.YELLOW_HOLD - 0.2)
        self.assertTrue(s.snapshot(3.0 + racing.YELLOW_HOLD - 0.1)["caution"])
        feed_f1(s, f1_telemetry(), 3.0 + racing.YELLOW_HOLD + 0.5)
        self.assertFalse(s.snapshot(3.0 + racing.YELLOW_HOLD + 0.6)["caution"])

    def test_a_new_session_forgets_the_flags(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_event("SCAR", bytes([1, 0])), 1.0)
        feed_f1(s, f1_event("CHQF"), 1.0)
        feed_f1(s, f1_event("SSTA"), 2.0)
        snap = s.snapshot(2.0)
        self.assertFalse(snap["caution"])
        self.assertNotIn("chequered", snap["ago"])

    def test_nothing_is_queued_after_the_session_ended(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(), 1.0)
        feed_f1(s, f1_event("SEND"), 2.0)
        feed_f1(s, f1_event("FTLP", bytes([0])), 2.1)
        self.assertNotIn("fastest", s.snapshot(2.2)["ago"])

    def test_a_packet_that_isnt_ours_changes_nothing(self):
        s = RacingState()
        s.feed({}, 1.0)
        s.feed({"source": "other"}, 1.0)
        self.assertFalse(s.in_race(1.0))


class PulsesNeverFasterThanThree(unittest.TestCase):
    """The limiter blink and the sweep of a fastest lap start only after the one before is over."""

    def starts(self, seconds, hz=60, lap_at=()):
        s = RacingState()
        seen, last = [], None
        for i in range(int(seconds * hz)):
            now = i / hz
            feed_f1(s, f1_telemetry(rev=100), now)
            if any(abs(now - x) < 0.5 / hz for x in lap_at):
                feed_f1(s, f1_event("FTLP", bytes([0])), now)
            snap = s.snapshot(now)
            for kind in ("limiter", "fastest"):
                a = snap["ago"].get(kind)
                if a is not None and a < 0.5 / hz:
                    seen.append((now, kind))
        return seen

    def windows(self, seen, width=1.0):
        times = [t for t, _ in seen]
        return max((sum(1 for u in times if t <= u < t + width) for t in times), default=0)

    def test_the_limiter_blink_is_at_most_three_a_second(self):
        seen = self.starts(10)
        self.assertGreater(len(seen), 20)                                       # (it does blink: about 2.9 a second)
        self.assertLessEqual(self.windows(seen), 3)
        gaps = [b[0] - a[0] for a, b in zip(seen, seen[1:])]
        self.assertGreaterEqual(min(gaps), racing.MIN_GAP - 1e-9)

    def test_with_a_fastest_lap_in_between_it_still_holds(self):
        seen = self.starts(10, lap_at=(2.0, 2.1, 5.0))
        self.assertIn("fastest", [k for _, k in seen])
        self.assertLessEqual(self.windows(seen), 3)

    def test_the_limiter_only_blinks_while_you_are_at_it(self):
        s = RacingState()
        feed_f1(s, f1_telemetry(rev=60), 1.0)
        for i in range(120):
            feed_f1(s, f1_telemetry(rev=60), 1.0 + i / 60)
            s.snapshot(1.0 + i / 60)
        self.assertNotIn("limiter", s.snapshot(3.0)["ago"])

    def test_not_while_the_start_lights_are_on(self):
        s = RacingState()
        feed_f1(s, f1_event("STLG", bytes([3])), 1.0)
        for i in range(120):
            feed_f1(s, f1_telemetry(rev=100), 1.0 + i / 60)
        self.assertNotIn("limiter", s.snapshot(2.9)["ago"])


# ---- the light -----------------------------------------------------------------------------------------

def snap(**kw):
    base = {"inRace": True, "source": "f1", "game": "F1 24", "rpm": 0.5, "gear": 4, "speed": 200.0, "limiter": False,
            "drs": False, "drsAllowed": False, "startLights": 0, "caution": False, "safetyCar": False, "ended": False, "ago": {}}
    base.update(kw)
    return base


def render(s, n=10, fps=100, t=0.0, span=(0.0, 1.0, "front"), mem=None):
    return EFFECTS["game_racing"].render(t, n, None, {} if mem is None else mem, {"racing": s, "fps": fps, "span": span})


def bright(p):
    return sum(p)


def is_blue(p):
    return p[2] > 200 and p[0] < 80 and p[1] < 120


class TheLight(unittest.TestCase):
    def test_it_is_a_game_effect_with_a_name(self):
        self.assertIn("game_racing", EFFECTS)
        self.assertEqual(EFFECTS["game_racing"].group, "game")
        self.assertEqual(EFFECTS["game_racing"].name, "Racing")
        self.assertIn("game_racing", GAME_EFFECTS)

    def test_no_race_is_a_dim_waiting_look(self):
        px = render(snap(inRace=False))
        self.assertEqual(len(px), 10)
        self.assertTrue(all(0 < bright(p) < 250 for p in px))

    def test_nothing_at_all_still_renders(self):
        self.assertEqual(len(EFFECTS["game_racing"].render(0.0, 5, None, {}, {})), 5)

    def test_the_bar_fills_from_left_to_right(self):
        previous = -1
        for f in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            px = render(snap(rpm=f))
            lit = [bright(p) > 150 for p in px]
            count = sum(lit)
            self.assertGreaterEqual(count, previous, f)
            previous = count
            self.assertEqual(lit, sorted(lit, reverse=True), f)                  # (lit pixels first, none after a dark one)
        self.assertEqual(sum(bright(p) > 150 for p in render(snap(rpm=0.0))), 0)
        self.assertEqual(sum(bright(p) > 150 for p in render(snap(rpm=1.0))), 10)
        half = sum(bright(p) > 150 for p in render(snap(rpm=0.5)))
        self.assertIn(half, (4, 5, 6))

    def test_green_then_yellow_then_red(self):
        px = render(snap(rpm=1.0), n=20)
        low, mid, top = px[1], px[12], px[19]
        self.assertTrue(low[1] > 200 and low[0] < 60, low)                       # green
        self.assertTrue(mid[0] > 200 and mid[1] > 150, mid)                      # yellow
        self.assertTrue(top[0] > 200 and top[1] < 60, top)                       # red

    def test_the_bar_follows_the_lights_place_on_the_stage(self):
        # (the bar is the whole stage's: at half the revs the left half of the stage is lit and the right half isn't)
        left_half = render(snap(rpm=0.5), n=10, span=(0.0, 0.5, "front"))
        self.assertTrue(all(bright(p) > 150 for p in left_half))
        right_half = render(snap(rpm=0.5), n=10, span=(0.5, 1.0, "front"))
        self.assertTrue(all(bright(p) < 60 for p in right_half))
        full = render(snap(rpm=1.0), n=10, span=(0.5, 1.0, "front"))
        self.assertTrue(all(bright(p) > 150 for p in full))

    def test_at_the_limiter_the_bar_pulses_blue(self):
        on = render(snap(rpm=1.0, limiter=True, ago={"limiter": 0.05}))
        self.assertTrue(all(is_blue(p) for p in on), on)
        off = render(snap(rpm=1.0, limiter=True, ago={"limiter": 0.4}))
        self.assertFalse(any(is_blue(p) for p in off))

    def test_the_blue_comes_at_most_three_times_a_second(self):
        s = RacingState()
        seen = []
        for i in range(600):
            now = i / 60
            feed_f1(s, f1_telemetry(rev=100), now)
            px = render(s.snapshot(now), t=now)
            seen.append(all(is_blue(p) for p in px))
        onsets = [i / 60 for i in range(1, 600) if seen[i] and not seen[i - 1]]
        self.assertGreater(len(onsets), 20)
        for t0 in onsets:
            self.assertLessEqual(sum(1 for u in onsets if t0 <= u < t0 + 1.0), 3)

    def test_start_lights_show_blocks(self):
        for lights in range(1, 6):
            px = render(snap(startLights=lights, rpm=0.0), n=50)
            red = [p[0] > 200 and p[1] < 30 for p in px]
            blocks = sum(1 for i in range(1, 50) if red[i] and not red[i - 1]) + (1 if red[0] else 0)
            self.assertEqual(blocks, lights, lights)
            # they come on from the left: the first `lights` fifths hold them and the rest stay dark
            self.assertFalse(any(red[int(lights * 10):]), lights)

    def test_start_lights_go_out_together_in_a_single_off(self):
        lit = render(snap(startLights=5, rpm=0.0), n=20)
        self.assertTrue(any(bright(p) > 200 for p in lit))
        out = render(snap(startLights=0, rpm=0.0, ago={"lights_out": 0.1}), n=20)
        self.assertTrue(all(bright(p) == 0 for p in out))
        later = render(snap(startLights=0, rpm=0.9, ago={"lights_out": 3.0}), n=20)
        self.assertTrue(any(bright(p) > 200 for p in later))                     # (the rev lights are back)

    def test_start_lights_on_a_slow_light_are_a_steady_red_that_grows(self):
        levels = [render(snap(startLights=k), n=3, fps=8)[0] for k in range(1, 6)]
        self.assertTrue(all(p[0] > p[1] + 20 for p in levels))
        self.assertEqual([p[0] for p in levels], sorted(p[0] for p in levels))
        self.assertLess(levels[0][0], levels[-1][0])

    def test_a_yellow_flag_is_an_amber_band_in_the_middle_that_breathes(self):
        base = render(snap(rpm=0.0), n=21)
        amber = render(snap(rpm=0.0, caution=True), n=21, t=0.5)                 # (0.5 Hz: the fullest at t = 0.5)
        mid = amber[10]
        self.assertTrue(mid[0] > 150 and 60 < mid[1] < 180 and mid[2] < 60, mid)
        self.assertGreater(bright(amber[10]), bright(base[10]) + 100)
        self.assertEqual(amber[0], base[0])                                      # (the ends are untouched)
        quiet = render(snap(rpm=0.0, caution=True), n=21, t=1.5)[10]              # (the weakest: t = 1.5)
        self.assertLess(bright(quiet), bright(mid))
        steps = [bright(render(snap(rpm=0.0, caution=True), n=21, t=k / 30)[10]) for k in range(60)]
        changes = sum(1 for a, b in zip(steps, steps[1:]) if abs(a - b) > 60)
        self.assertEqual(changes, 0)                                             # (a slow swell: no jump between two frames of 1/30 s)

    def test_drs_is_a_green_edge(self):
        px = render(snap(rpm=0.0, drs=True), n=20)
        for p in (px[0], px[-1]):
            self.assertTrue(p[1] > 200 and p[0] < 60, p)
        self.assertTrue(bright(px[10]) < 150)

    def test_a_fastest_lap_sweeps_purple_across(self):
        early = render(snap(rpm=0.0, ago={"fastest": 0.1}), n=30)
        late = render(snap(rpm=0.0, ago={"fastest": 0.9}), n=30)
        centre = lambda px: sum(i * p[2] for i, p in enumerate(px)) / sum(p[2] for p in px)     # (where the purple sits)
        self.assertLess(centre(early), centre(late) - 3)
        self.assertGreater(centre(late), 18)                                     # the band moves to the right
        peak = max(late, key=lambda p: p[2])
        self.assertTrue(peak[2] > 150 and peak[0] > peak[1] * 2, peak)           # (purple: blue and red, little green)
        done = render(snap(rpm=0.0, ago={"fastest": 2.0}), n=30)
        self.assertTrue(all(p[2] < 40 for p in done))

    def test_the_chequered_flag_is_a_slow_black_and_white_checker(self):
        px = render(snap(ago={"chequered": 1.0}), n=40, t=0.0)
        values = [bright(p) for p in px]
        self.assertGreater(max(values), 600)
        self.assertLess(min(values), 100)
        self.assertTrue(all(abs(p[0] - p[1]) < 3 and abs(p[1] - p[2]) < 3 for p in px))   # (grey scale only)
        flips = 0
        prev = None
        for k in range(120):                                                     # 4 seconds at 30 frames
            frame = [bright(p) > 380 for p in render(snap(ago={"chequered": 1.0}), n=40, t=k / 30)]
            if prev is not None:
                flips += sum(1 for a, b in zip(prev, frame) if a != b)
            prev = frame
        self.assertLessEqual(flips / 40 / 4, 2.0)                                # a pixel changes at most twice a second
        gone = render(snap(rpm=0.0, ago={"chequered": 40.0}), n=10)
        self.assertTrue(all(bright(p) < 100 for p in gone))                      # (long after: back to the rev lights)

    def test_slow_lights_show_a_calm_color_green_to_red_and_skip_the_blink(self):
        low = render(snap(rpm=0.0), n=3, fps=8)
        self.assertEqual(low[0], low[1])
        self.assertTrue(low[0][1] > low[0][0])                                   # green side
        high = render(snap(rpm=1.0), n=3, fps=8)
        self.assertTrue(high[0][0] > high[0][1])                                 # red side
        blink = render(snap(rpm=1.0, limiter=True, ago={"limiter": 0.05}), n=3, fps=8)
        self.assertFalse(any(is_blue(p) for p in blink))
        one = render(snap(rpm=0.0), n=1)
        self.assertTrue(one[0][1] > one[0][0])

    def test_a_slow_light_follows_the_revs_smoothly(self):
        mem = {}
        first = render(snap(rpm=0.0), n=1, fps=8, t=0.0, mem=mem)[0]
        jump = render(snap(rpm=1.0), n=1, fps=8, t=0.1, mem=mem)[0]
        later = render(snap(rpm=1.0), n=1, fps=8, t=3.0, mem=mem)[0]
        self.assertTrue(jump[1] > jump[0] or abs(jump[0] - first[0]) < 120)      # (not at the red at once)
        self.assertTrue(later[0] > later[1])

    def test_a_slow_light_holds_a_fastest_lap_for_its_whole_length(self):
        for a in (0.1, 0.6, 1.1):
            p = render(snap(rpm=0.0, ago={"fastest": a}), n=2, fps=8)[0]
            self.assertTrue(p[2] > 120, (a, p))

    def test_it_asks_nothing_of_the_stage_when_the_span_is_missing(self):
        self.assertEqual(len(EFFECTS["game_racing"].render(0.0, 4, None, {}, {"racing": snap()})), 4)


# ---- the listener --------------------------------------------------------------------------------------

class FakeSocket:
    """A socket that hands out packets and then, like a real one, times out until closed."""

    def __init__(self, packets=()):
        self.packets = list(packets)
        self.closed = False
        self.timeout = None
        self.reads = 0

    def settimeout(self, t):
        self.timeout = t

    def recvfrom(self, n):
        self.reads += 1
        if self.packets:
            return self.packets.pop(0)
        if self.closed:
            raise OSError("closed")
        time.sleep(0.005)
        raise TimeoutError()

    def close(self):
        self.closed = True


LOCAL = ("127.0.0.1", 50000)


class TheListener(unittest.TestCase):
    def serve(self, kind, packets, clock=None):
        changes = []
        lis = RacingListener(on_change=lambda: changes.append(1), clock=clock or (lambda: 100.0))
        sock = FakeSocket(packets)
        stop = threading.Event()
        t = threading.Thread(target=lis.serve, args=(kind, sock, stop))
        t.start()
        deadline = time.time() + 3
        while sock.packets and time.time() < deadline:
            time.sleep(0.005)
        time.sleep(0.02)
        stop.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        return lis, changes, sock

    def test_forza_packets_feed_the_state(self):
        lis, changes, sock = self.serve("forza", [(forza(311, rpm=4400.0), LOCAL)])
        snap_ = lis.snapshot(100.0)
        self.assertTrue(snap_["inRace"])
        self.assertAlmostEqual(snap_["rpm"], 0.5)
        self.assertEqual(snap_["game"], "Forza")
        self.assertEqual(changes, [1])                                          # (the window is told a race began)
        self.assertEqual(sock.timeout, racing.READ_WAIT)

    def test_f1_packets_feed_the_state(self):
        lis, _, _ = self.serve("f1", [(f1_event("SSTA"), LOCAL), (f1_telemetry(rev=80), LOCAL)])
        self.assertTrue(lis.in_race(100.0))
        self.assertAlmostEqual(lis.snapshot(100.0)["rpm"], 0.8)
        self.assertTrue(lis.receiving(100.0))
        self.assertEqual(lis.label(), "F1 24")

    def test_packets_from_anywhere_but_this_pc_are_ignored(self):
        for addr in (("192.168.1.20", 5300), ("8.8.8.8", 5300), ("127.0.0.2", 5300), ("::1", 5300, 0, 0), ("", 0), None):
            lis, changes, _ = self.serve("forza", [(forza(311), addr)])
            self.assertFalse(lis.in_race(100.0), addr)
            self.assertEqual(changes, [], addr)
            self.assertEqual(lis.ignored, 1, addr)

    def test_junk_and_the_wrong_games_packets_are_ignored(self):
        lis, _, _ = self.serve("forza", [(b"", LOCAL), (b"hello", LOCAL), (f1_telemetry(), LOCAL), (bytes(250), LOCAL)])
        self.assertFalse(lis.in_race(100.0))
        self.assertEqual(lis.ignored, 4)
        lis, _, _ = self.serve("f1", [(forza(311), LOCAL), (b"\0" * 2000, LOCAL)])
        self.assertFalse(lis.in_race(100.0))

    def test_a_race_that_stops_is_told_to_the_window(self):
        now = [100.0]
        lis, changes, _ = self.serve("forza", [(forza(311), LOCAL)], clock=lambda: now[0])
        self.assertEqual(changes, [1])
        now[0] = 100.0 + ALIVE + 1
        lis._noticed()
        self.assertEqual(changes, [1, 1])

    def test_a_closed_socket_ends_the_loop(self):
        lis = RacingListener()
        sock = FakeSocket()
        sock.closed = True
        lis.serve("forza", sock, threading.Event())                             # (returns by itself)
        self.assertEqual(sock.reads, 1)

    def test_it_opens_one_socket_per_game_and_closes_them_when_stopped(self):
        opened = []

        def make(port):
            s = FakeSocket()
            opened.append((port, s))
            return s

        lis = RacingListener(make_socket=make)
        lis.start(5301, 20778)
        deadline = time.time() + 3
        while len(opened) < 2 and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(sorted(p for p, _ in opened), [5301, 20778])
        threads = list(lis._threads)
        lis.stop()
        for t in threads:
            t.join(3)
            self.assertFalse(t.is_alive())
        self.assertTrue(all(s.closed for _, s in opened))
        self.assertEqual(lis._threads, [])

    def test_nothing_is_opened_until_it_starts(self):
        made = []
        RacingListener(make_socket=lambda port: made.append(port))
        self.assertEqual(made, [])

    def test_a_busy_port_is_reported_and_the_other_game_still_listens(self):
        opened = []

        def make(port):
            if port == 5300:
                raise OSError("in use")
            opened.append(port)
            return FakeSocket()

        lis = RacingListener(make_socket=make)
        lis.start(5300, 20777)
        deadline = time.time() + 3
        while (not opened or "forza" not in lis.problems) and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(lis.problems, {"forza": "Port 5300 is busy"})
        self.assertEqual(opened, [20777])
        threads = list(lis._threads)
        lis.stop()
        for t in threads:
            t.join(3)
            self.assertFalse(t.is_alive())
        self.assertEqual(lis.problems, {})

    def test_restarting_with_other_ports(self):
        opened = []
        lis = RacingListener(make_socket=lambda port: opened.append(port) or FakeSocket())
        lis.start(5300, 20777)
        lis.start(6000, 6001)
        deadline = time.time() + 3
        while len(opened) < 4 and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(sorted(opened), [5300, 6000, 6001, 20777])
        lis.stop()

    def test_stopping_ends_the_race_at_once(self):
        lis, _, _ = self.serve("forza", [(forza(311), LOCAL)], clock=lambda: time.monotonic())
        lis.state.feed(parse_forza(forza(311)), time.monotonic())
        self.assertTrue(lis.in_race(time.monotonic()))
        lis.stop()
        self.assertFalse(lis.in_race(time.monotonic()))

    def test_the_real_default_makes_no_socket_by_itself(self):
        with mock.patch.object(racing.socket, "socket", side_effect=AssertionError("no real sockets")):
            RacingListener()


# ---- games.py ------------------------------------------------------------------------------------------

class TheGames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(games, "find_rocket_league", return_value=None),
                        mock.patch.object(games, "find_cs2", return_value=None),
                        mock.patch.object(games, "find_dota2", return_value=None)]
        for p in self.patches:
            p.start()
        self.changed = []
        self.opened = []
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: self.changed.append(1))
        self.g.racing.make_socket = lambda port: self.opened.append(port) or FakeSocket()     # (never a real port)
        self.g.racing.sim.opener = lambda name: None                                          # (nor a real block of shared memory)

    def tearDown(self):
        self.g.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def wait_for(self, n):
        deadline = time.time() + 3
        while len(self.opened) < n and time.time() < deadline:
            time.sleep(0.005)

    def test_it_is_off_by_default_with_the_usual_ports(self):
        d = Config(Path(self.tmp.name) / "other.json").data["games"]["racing"]
        self.assertEqual(d, {"on": False, "forza_port": 5300, "f1_port": 20777})
        self.assertEqual((FORZA_PORT, F1_PORT), (5300, 20777))
        self.assertFalse(self.g.racing_on())
        self.assertEqual(self.g.racing_ports(), (5300, 20777))

    def test_an_old_config_without_it_gets_the_defaults(self):
        path = Path(self.tmp.name) / "old.json"
        path.write_text('{"games": {"cs2": true, "rl": false}}', encoding="utf-8")
        d = Config(path).data["games"]
        self.assertEqual(d["racing"], {"on": False, "forza_port": 5300, "f1_port": 20777})
        self.assertTrue(d["cs2"])

    def test_switching_on_opens_the_ports_and_off_closes_them(self):
        self.g.set_racing(True, 5300, 20777)
        self.wait_for(2)
        self.assertEqual(sorted(self.opened), [5300, 20777])
        self.assertTrue(self.g.settings["racing"]["on"])
        self.assertTrue(self.changed)
        threads = list(self.g.racing._threads)
        self.g.set_racing(False, 5300, 20777)
        for t in threads:
            t.join(3)
            self.assertFalse(t.is_alive())
        self.assertFalse(self.g.racing_on())

    def test_ports_are_checked(self):
        for bad in ((True, 80, 20777), (True, 5300, 70000), (True, "x", 20777), (True, 5300, None), (True, 1023, 20777), (True, 5300, 0)):
            with self.assertRaises(ValueError, msg=bad):
                self.g.set_racing(*bad)
        with self.assertRaises(ValueError):
            self.g.set_racing(True, 5400, 5400)
        self.assertFalse(self.g.racing_on())
        self.assertEqual(self.opened, [])
        self.g.set_racing(False, "5400", "5401")                                  # (numbers typed into a box arrive as text)
        self.assertEqual(self.g.racing_ports(), (5400, 5401))

    def test_changing_a_port_while_on_listens_on_the_new_one(self):
        self.g.set_racing(True, 5300, 20777)
        self.wait_for(2)
        self.g.set_racing(True, 5400, 20777)
        self.wait_for(4)
        self.assertEqual(sorted(self.opened), [5300, 5400, 20777, 20777])

    def test_saving_the_same_thing_twice_doesnt_restart_it(self):
        self.g.set_racing(True, 5300, 20777)
        self.wait_for(2)
        self.g.set_racing(True, 5300, 20777)
        time.sleep(0.05)
        self.assertEqual(len(self.opened), 2)

    def test_the_games_start_listens_when_it_was_on(self):
        self.g.settings["racing"].update(on=True, forza_port=5500, f1_port=5501)
        self.g.start()
        self.wait_for(2)
        self.assertEqual(sorted(self.opened), [5500, 5501])

    def test_nothing_is_opened_when_it_is_off(self):
        self.g.start()
        time.sleep(0.05)
        self.assertEqual(self.opened, [])

    def test_describe_follows_the_race(self):
        d = self.g.describe()
        self.assertEqual(set(d["racing"]), {"on", "forzaPort", "f1Port", "receiving", "inRace", "game", "seen", "problem"})
        self.assertFalse(d["racing"]["on"])
        self.g.settings["racing"]["on"] = True
        now = time.monotonic()
        self.g.racing.state.feed(parse_f1(f1_telemetry()), now)
        d = self.g.describe()["racing"]
        self.assertTrue(d["receiving"] and d["inRace"])
        self.assertEqual(d["game"], "F1 24")
        self.g.racing.state.feed(parse_forza(forza(311)), now)
        self.assertEqual(self.g.describe()["racing"]["game"], "Forza")
        self.g.settings["racing"]["on"] = False
        d = self.g.describe()["racing"]
        self.assertFalse(d["inRace"] or d["receiving"])
        self.assertEqual(d["game"], "")

    def test_describe_tells_about_a_busy_port(self):
        self.g.settings["racing"]["on"] = True
        self.g.racing.problems = {"forza": "Port 5300 is busy"}
        self.assertEqual(self.g.describe()["racing"]["problem"], "Port 5300 is busy")
        self.g.settings["racing"]["on"] = False
        self.assertEqual(self.g.describe()["racing"]["problem"], "")

    def test_racing_active_needs_the_switch_and_a_race(self):
        now = time.monotonic()
        self.g.racing.state.feed(parse_forza(forza(311)), now)
        self.assertFalse(self.g.racing_active(now))
        self.g.settings["racing"]["on"] = True
        self.assertTrue(self.g.racing_active(now))
        self.assertFalse(self.g.racing_active(now + ALIVE + 1))

    def test_a_race_starting_tells_the_window(self):
        self.g.racing.on_change()
        self.assertTrue(self.changed)


# ---- the automatic look -----------------------------------------------------------------------------

class Manager(FakeManager):
    def __init__(self):
        super().__init__()
        self.games.settings["racing"] = {"on": True, "forza_port": 5300, "f1_port": 20777}

    def game_lights(self, effect="game_cs2"):
        self.look = {"lamp": {"on": True, "mode": "effect", "effect": effect}}


class Automatic(unittest.TestCase):
    def setUp(self):
        self.m = Manager()
        self.a = Automations(self.m)
        self.t = 0

    def run_for(self, seconds, racing=False, rl=False, dota=False, lol=False, cs2=False, kind=None):
        for _ in range(seconds):
            self.a.step(self.t, self.a.rules(), kind, 0.0, cs2, frozenset(), False, lol, dota, rl, racing=racing)
            self.t += 1

    def effect(self):
        return self.m.look["lamp"].get("effect") or self.m.look["lamp"].get("color")

    def test_the_rule_comes_after_rocket_league_and_only_when_on(self):
        self.assertEqual(self.a.rules()["racing"], "game:racing")
        self.assertEqual(list(self.a.rules())[:6], ["live", "cs2", "lol", "dota", "rl", "racing"])
        self.assertEqual(RULES[:7], ("live", "call", "cs2", "lol", "dota", "rl", "racing"))
        self.assertLess(RULES.index("racing"), RULES.index("game"))
        self.m.games.settings["racing"]["on"] = False
        self.assertEqual(self.a.rules()["racing"], "")

    def test_a_missing_setting_means_off(self):
        del self.m.games.settings["racing"]
        self.assertEqual(self.a.rules()["racing"], "")

    def test_a_race_changes_the_lights_and_the_end_puts_them_back(self):
        self.run_for(1, racing=True)
        self.assertEqual(self.effect(), "#ffffff")                        # not yet: a race has to last a second
        self.run_for(2, racing=True)
        self.assertEqual(self.effect(), "game_racing")
        self.assertEqual(self.a.active, "racing")
        self.run_for(4)
        self.assertEqual(self.effect(), "game_racing")                    # a short gap (a pause): still playing along
        self.run_for(4)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_nothing_happens_when_it_is_off(self):
        self.m.games.settings["racing"]["on"] = False
        self.run_for(10, racing=True)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_the_other_games_go_before_it_and_a_full_screen_game_after(self):
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.m.games.settings.update(cs2=True, lol=True, dota=True, rl=True)
        self.run_for(10, racing=True, kind="game")
        self.assertEqual(self.a.active, "racing")
        self.run_for(10, racing=True, rl=True, kind="game")
        self.assertEqual(self.a.active, "rl")
        self.run_for(10, racing=True, rl=True, dota=True, kind="game")
        self.assertEqual(self.a.active, "dota")
        self.run_for(10, racing=True, rl=True, dota=True, lol=True, kind="game")
        self.assertEqual(self.a.active, "lol")
        self.run_for(10, racing=True, rl=True, dota=True, lol=True, cs2=True, kind="game")
        self.assertEqual(self.a.active, "cs2")

    def test_a_race_keeps_music_from_taking_over(self):
        self.m.config.data["settings"]["auto_show"] = True
        self.run_for(5, racing=True)
        self.assertTrue(self.a._racing)
        self.a._listen(self.t, self.a.rules(), 1.0)
        self.assertEqual(self.a._checks, 0)

    def test_a_change_by_hand_is_left_alone(self):
        self.run_for(3, racing=True)
        self.m.look = {"lamp": {"on": True, "mode": "color", "color": "#123456"}}
        self.run_for(10, racing=True)
        self.assertEqual(self.effect(), "#123456")

    def test_switching_it_off_forgets_the_race(self):
        self.run_for(3, racing=True)
        self.m.games.settings["racing"]["on"] = False
        self.run_for(1, racing=True)
        self.assertFalse(self.a._racing)

    def test_step_still_works_without_the_new_argument(self):
        self.m.games.settings["racing"]["on"] = False
        self.m.games.settings["cs2"] = True
        for _ in range(5):
            self.a.step(self.t, self.a.rules(), None, 0.0, True)
            self.t += 1
        self.assertEqual(self.a.active, "cs2")

    def test_the_loop_tells_step_about_a_race_by_keyword(self):
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
        self.m.games.racing_active = lambda now: True
        self.a._stop = Stop(2)
        with mock.patch.object(Automations, "step", lambda s, *args, **kw: seen.append((args, kw))):
            self.a._run()
        self.assertTrue(seen and all(kw.get("racing") is True for _, kw in seen))
        self.assertTrue(all(len(args) == 11 for args, _ in seen))                 # (the old arguments keep their places)

    def test_the_loop_doesnt_ask_the_games_when_it_is_off(self):
        class Stop:
            def __init__(self, n):
                self.n = n

            def wait(self, _):
                self.n -= 1
                return self.n < 0

            def is_set(self):
                return self.n < 0

        seen = []
        self.m.games.settings["racing"]["on"] = False
        self.m.away = type("A", (), {"active": False})()
        self.a._stop = Stop(1)
        with mock.patch.object(Automations, "step", lambda s, *args, **kw: seen.append(kw)):
            self.a._run()                                                          # (the fake games have no racing_active: it must not be asked)
        self.assertTrue(seen and all(kw.get("racing") is False for kw in seen))


class TheApiAndTheEngine(unittest.TestCase):
    def test_the_window_can_set_it(self):
        from glowstage.api import Api
        g = mock.Mock()
        api = Api(SimpleNamespace(manager=SimpleNamespace(games=g)))
        self.assertEqual(api.games_racing(True, 5300, 20777), {"ok": True, "result": None})
        g.set_racing.assert_called_once_with(True, 5300, 20777)
        g.set_racing.side_effect = ValueError("Forza and F1 need two different ports")
        self.assertEqual(api.games_racing(True, 5400, 5400), {"ok": False, "error": "Forza and F1 need two different ports"})

    def test_the_engine_hands_the_effect_its_snapshot_only_while_it_runs(self):
        import inspect
        from glowstage.engine import EffectEngine
        src = inspect.getsource(EffectEngine._frame)
        self.assertIn('st.effect == "game_racing"', src)
        self.assertIn('ctx["racing"]', src)

    def test_diagnostics_mention_it(self):
        import inspect
        from glowstage import diagnostics
        self.assertIn("Racing games", inspect.getsource(diagnostics))


if __name__ == "__main__":
    unittest.main()
