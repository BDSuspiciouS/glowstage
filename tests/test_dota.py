import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from glowstage import games
from glowstage.automations import Automations
from glowstage.config import Config
from glowstage.effects import EFFECTS
from glowstage.games import DotaState
from tests.test_app_looks import FakeManager

PLAY = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"
PRE = "DOTA_GAMERULES_STATE_PRE_GAME"
POST = "DOTA_GAMERULES_STATE_POST_GAME"
PICK = "DOTA_GAMERULES_STATE_HERO_SELECTION"


# ---- what Dota 2's game state integration says (shaped like the real thing, trimmed) ---------------

def ev(at, kind, **more):
    return {"game_time": at, "event_type": kind, **more}


def msg(t=600, state=PLAY, team="radiant", kills=0, deaths=0, hp=100, mana=100, alive=True, respawn=0,
        day=True, night_stalker=False, win="none", events=(), stunned=False, hexed=False, activity="playing"):
    return {
        "provider": {"name": "Dota 2", "appid": 570, "version": 47, "timestamp": 1700000000 + t},
        "map": {"name": "start", "matchid": "7300000001", "game_time": t, "clock_time": t - 90, "daytime": day,
                "nightstalker_night": night_stalker, "game_state": state, "paused": False, "win_team": win,
                "customgamename": "", "ward_purchase_cooldown": 0},
        "player": {"steamid": "76561198000000001", "name": "Batikan", "activity": activity, "kills": kills,
                   "deaths": deaths, "assists": 3, "last_hits": 41, "denies": 4, "kill_streak": 0,
                   "team_name": team, "player_id": 2, "gold": 1200},
        "hero": {"id": 74, "name": "npc_dota_hero_invoker", "level": 9, "alive": alive, "respawn_seconds": respawn,
                 "buyback_cost": 400, "buyback_cooldown": 0, "health": 600, "max_health": 900, "health_percent": hp,
                 "mana": 200, "max_mana": 500, "mana_percent": mana, "silenced": False, "stunned": stunned,
                 "disarmed": False, "magicimmune": False, "hexed": hexed, "muted": False, "break": False,
                 "smoked": False, "has_debuff": stunned or hexed},
        "events": list(events),
    }


def started(state=None, **kw):
    """A state that has seen the match once, so what follows is news."""
    state = state or DotaState()
    state.feed(msg(**kw), 1.0)
    return state


def render(snap, n=10, fps=100, t=0.0, span=(0.0, 1.0, "front"), mem=None):
    return EFFECTS["game_dota"].render(t, n, None, {} if mem is None else mem, {"dota": snap, "fps": fps, "span": span})


def lum(p):
    return sum(p)


# ---- the state ---------------------------------------------------------------------------------------

class Colors(unittest.TestCase):
    def test_radiant_is_green_and_dire_is_red(self):
        for team, strong in (("radiant", 1), ("dire", 0)):
            s = started(team=team)
            c = render(s.snapshot(1.0), n=1)[0]
            self.assertEqual(max(range(3), key=lambda i: c[i]), strong, (team, c))
            self.assertEqual(s.snapshot(1.0)["team"], team)
            self.assertTrue(s.snapshot(1.0)["inMatch"])

    def test_day_is_brighter_and_warmer_than_night(self):
        day = render(started(day=True).snapshot(1.0), n=1)[0]
        night = render(started(day=False).snapshot(1.0), n=1)[0]
        self.assertGreater(lum(day), 1.4 * lum(night))
        self.assertGreater(day[0] / max(1, day[2]), night[0] / max(1, night[2]))      # warmer by day
        self.assertGreater(night[2] / max(1, night[1]), day[2] / max(1, day[1]))      # bluer by night
        dire_day = render(started(team="dire", day=True).snapshot(1.0), n=1)[0]
        dire_night = render(started(team="dire", day=False).snapshot(1.0), n=1)[0]
        self.assertGreater(lum(dire_day), lum(dire_night))

    def test_the_night_stalkers_night_is_a_night(self):
        s = started(day=True, night_stalker=True)
        self.assertFalse(s.snapshot(1.0)["daytime"])

    def test_the_turn_from_day_to_night_is_gradual(self):
        mem = {}
        s = started(day=True)
        first = render(s.snapshot(1.0), n=1, t=0.0, mem=mem)[0]
        s.feed(msg(day=False), 2.0)
        soon = render(s.snapshot(2.0), n=1, t=0.2, mem=mem)[0]
        later = render(s.snapshot(2.0), n=1, t=6.0, mem=mem)[0]
        self.assertLess(lum(later), lum(soon))
        self.assertLess(lum(soon), lum(first))

    def test_health_is_a_bar_across_the_stage(self):
        s = started(hp=50)
        px = render(s.snapshot(1.0), n=10)
        self.assertGreater(lum(px[0]), 3 * lum(px[-1]))       # the right side goes dark first
        self.assertEqual(s.snapshot(1.0)["health"], 0.5)
        full = render(started(hp=100).snapshot(1.0), n=10)
        self.assertGreater(lum(full[-1]), 3 * lum(px[-1]))

    def test_mana_is_a_blue_bar_on_the_pcs_lights(self):
        pc = (0.72, 1.0, "front")
        full = render(started(hp=100, mana=100).snapshot(1.0), n=10, span=pc)
        empty = render(started(hp=100, mana=0).snapshot(1.0), n=10, span=pc)
        self.assertGreater(full[0][2], empty[0][2] + 60)
        self.assertEqual(full[-1][2] > empty[-1][2], True)
        left = render(started(hp=100, mana=0).snapshot(1.0), n=10, span=(0.0, 0.5, "front"))
        left_full = render(started(hp=100, mana=100).snapshot(1.0), n=10, span=(0.0, 0.5, "front"))
        self.assertEqual(left, left_full)                      # a light that is not the PC's doesn't show mana

    def test_no_match_drifts_in_both_colors_and_stays_dim(self):
        px = render({"inMatch": False, "ago": {}}, n=8)
        self.assertTrue(all(max(p) < 140 for p in px))

    def test_missing_fields_dont_break_it(self):
        s = DotaState()
        s.feed({"map": {"game_state": PLAY}, "player": {"team_name": "dire", "activity": "playing"}}, 1.0)
        snap = s.snapshot(1.0)
        self.assertTrue(snap["inMatch"])
        self.assertEqual(len(render(snap, n=4)), 4)
        for junk in ({}, {"map": 3, "player": "x", "hero": [], "events": "no"},
                     {"map": {"game_state": None}, "player": {"team_name": "dire", "kills": "many", "activity": "playing"},
                      "hero": {"health_percent": None, "alive": None, "respawn_seconds": "soon"},
                      "events": [None, 3, {"event_type": None}]}):
            s.feed(junk, 2.0)
            render(s.snapshot(2.0), n=3)

    def test_it_stops_counting_as_a_match_when_the_messages_stop(self):
        s = started()
        self.assertTrue(s.snapshot(10.0)["inMatch"])
        self.assertFalse(s.snapshot(1.0 + games.ALIVE + 1)["inMatch"])

    def test_the_menu_is_not_a_match(self):
        s = DotaState()
        s.feed(msg(activity="menu"), 1.0)
        self.assertFalse(s.snapshot(1.0)["inMatch"])


class Death(unittest.TestCase):
    def test_respawn_fills_from_zero_to_one_as_it_counts_down(self):
        s = started(hp=0)
        s.feed(msg(hp=0, alive=False, respawn=20, deaths=1), 10.0)
        snap = s.snapshot(10.0)
        self.assertFalse(snap["alive"])
        self.assertAlmostEqual(snap["respawn"], 0.0)
        self.assertEqual(snap["ago"]["death"], 0.0)
        s.feed(msg(hp=0, alive=False, respawn=10, deaths=1), 20.0)
        self.assertAlmostEqual(s.snapshot(20.0)["respawn"], 0.5)
        self.assertAlmostEqual(s.snapshot(25.0)["respawn"], 0.75)       # between two messages it keeps counting
        s.feed(msg(hp=100, alive=True, deaths=1), 31.0)
        self.assertTrue(s.snapshot(31.0)["alive"])
        self.assertEqual(s.snapshot(31.0)["respawn"], 0.0)

    def test_dead_is_a_dim_cold_grey_and_the_respawn_bar_fills(self):
        s = started()
        s.feed(msg(hp=0, alive=False, respawn=20, deaths=1), 10.0)
        grey = render(s.snapshot(10.0), n=10)
        self.assertTrue(all(max(p) < 40 for p in grey))
        self.assertTrue(all(abs(p[0] - p[2]) < 40 for p in grey))       # no side color yet: cold grey
        s.feed(msg(hp=0, alive=False, respawn=5, deaths=1), 25.0)
        bar = render(s.snapshot(25.0), n=10)
        self.assertGreater(lum(bar[0]), 2 * lum(bar[-1]))                # filling from the left
        self.assertGreater(bar[0][1], bar[0][0])                         # in the side's color (Radiant green)

    def test_a_second_death_starts_a_new_countdown(self):
        s = started()
        s.feed(msg(alive=False, respawn=8, deaths=1), 5.0)
        s.feed(msg(alive=True, deaths=1), 14.0)
        s.feed(msg(alive=False, respawn=30, deaths=2), 40.0)
        s.feed(msg(alive=False, respawn=15, deaths=2), 55.0)
        self.assertAlmostEqual(s.snapshot(55.0)["respawn"], 0.5)

    def test_dying_is_not_a_flash(self):
        s = started()
        s.feed(msg(alive=False, respawn=20, deaths=1), 5.0)
        self.assertEqual(set(s.snapshot(5.0)["ago"]), {"death"})


class Moments(unittest.TestCase):
    def test_your_kill_flashes_once(self):
        s = started(kills=2)
        s.feed(msg(t=601, kills=3), 10.0)
        self.assertEqual(s.snapshot(10.0)["ago"].get("my_kill"), 0.0)
        s.feed(msg(t=602, kills=3), 11.0)                                # the next message says the same
        self.assertAlmostEqual(s.snapshot(11.0)["ago"]["my_kill"], 1.0)  # ... and doesn't flash again
        px = render(s.snapshot(10.0), n=1)[0]
        quiet = render(started(kills=2).snapshot(1.0), n=1)[0]
        self.assertGreater(px[0], quiet[0] + 100)                        # gold on top of the green

    def test_a_kill_count_you_join_with_is_not_news(self):
        s = DotaState()
        s.feed(msg(kills=7), 1.0)
        self.assertNotIn("my_kill", s.snapshot(1.0)["ago"])
        s.feed(msg(kills=7), 2.0)
        self.assertNotIn("my_kill", s.snapshot(2.0)["ago"])

    def test_coming_back_after_a_long_silence_is_not_news(self):
        s = started(kills=1)
        s.feed(msg(kills=4), 100.0)
        self.assertNotIn("my_kill", s.snapshot(100.0)["ago"])

    def test_roshan_killed_by_your_team_flashes_the_enemys_doesnt(self):
        s = started()
        s.feed(msg(t=700, events=[ev(700, "roshan_killed", killed_by_team="radiant", killer_player_id=3)]), 10.0)
        self.assertEqual(s.snapshot(10.0)["ago"].get("roshan"), 0.0)
        s.feed(msg(t=701, events=[ev(700, "roshan_killed", killed_by_team="radiant", killer_player_id=3)]), 11.0)
        self.assertAlmostEqual(s.snapshot(11.0)["ago"]["roshan"], 1.0)   # the same event again: not again
        s2 = started()
        s2.feed(msg(t=700, events=[ev(700, "roshan_killed", killed_by_team="dire", killer_player_id=7)]), 10.0)
        self.assertNotIn("roshan", s2.snapshot(10.0)["ago"])

    def test_the_aegis_flashes_when_you_pick_it_up(self):
        s = started()
        s.feed(msg(t=700, events=[ev(700, "aegis_picked_up", player_id=2, snatched=False)]), 10.0)
        self.assertEqual(s.snapshot(10.0)["ago"].get("aegis"), 0.0)
        s2 = started()
        s2.feed(msg(t=700, events=[ev(700, "aegis_picked_up", player_id=6, snatched=False)]), 10.0)
        self.assertNotIn("aegis", s2.snapshot(10.0)["ago"])

    def test_roshan_and_the_aegis_are_purple_turning_gold(self):
        s = started(hp=100)
        s.feed(msg(t=700, events=[ev(700, "roshan_killed", killed_by_team="radiant")]), 10.0)
        quiet = render(started(hp=100).snapshot(1.0), n=1)[0]
        start = render(s.snapshot(10.05), n=1, fps=10)[0]                # (a slow light holds the flash: same strength all along)
        end = render(s.snapshot(10.0 + 1.2), n=1, fps=10)[0]
        self.assertGreater(start[2], quiet[2] + 80)                      # purple: blue and red up
        self.assertGreater(start[0], quiet[0] + 40)
        self.assertGreater(end[1], start[1] + 60)                        # gold at the end: more green, less blue
        self.assertLess(end[2], start[2] - 40)

    def test_other_events_dont_flash(self):
        s = started()
        s.feed(msg(t=700, events=[ev(700, "bounty_rune_pickup", player_id=2, team="radiant", bounty_gold=60, team_gold=40),
                                  ev(701, "courier_killed", team="dire"), ev(702, "tip", sender_player_id=1, receiver_player_id=2, gold_amount=50)]), 10.0)
        self.assertEqual(s.snapshot(10.0)["ago"], {})

    def test_joining_late_doesnt_replay_old_events(self):
        s = DotaState()
        s.feed(msg(t=1500, events=[ev(700, "roshan_killed", killed_by_team="radiant")]), 1.0)
        self.assertNotIn("roshan", s.snapshot(1.0)["ago"])
        s.feed(msg(t=1501, events=[ev(1500, "roshan_killed", killed_by_team="radiant")]), 2.0)
        self.assertIn("roshan", s.snapshot(2.0)["ago"])

    def test_stunned_is_a_dip_not_a_flash(self):
        s = started()
        s.feed(msg(stunned=True), 10.0)
        snap = s.snapshot(10.0)
        self.assertEqual(set(snap["ago"]), {"dip"})
        quiet = render(started().snapshot(1.0), n=1, t=0.0)[0]
        mid = render(s.snapshot(10.4), n=1)[0]
        self.assertLess(max(mid) - min(mid), 0.7 * (max(quiet) - min(quiet)))   # drained of color ...
        self.assertLess(max(mid), max(quiet))                                    # ... and not brighter: no flash
        self.assertEqual(render(s.snapshot(11.0), n=1)[0], quiet)                # and back

    def test_hexed_dips_too_and_a_chain_of_stuns_is_one_dip(self):
        s = started()
        s.feed(msg(hexed=True), 10.0)
        self.assertEqual(s.snapshot(10.0)["ago"]["dip"], 0.0)
        for k in range(1, 10):                                           # off, on, off, on ... every 0.1 s
            s.feed(msg(stunned=k % 2 == 0), 10.0 + k * 0.1)
        starts = {round(10.0 + 0.9 - a, 1) for a in [s.snapshot(10.9)["ago"]["dip"]]}
        self.assertEqual(starts, {10.0})                                 # never started a second dip inside a second
        s.feed(msg(), 12.0)
        s.feed(msg(stunned=True), 12.5)
        self.assertEqual(s.snapshot(12.5)["ago"]["dip"], 0.0)            # a new one a second later is fine


class TheEnd(unittest.TestCase):
    def test_the_end_holds_gold_for_a_win_and_dim_red_for_a_loss(self):
        s = started(team="radiant")
        s.feed(msg(t=1800, state=POST, win="radiant"), 10.0)
        snap = s.snapshot(10.0)
        self.assertEqual(snap["ago"].get("win"), 0.0)
        self.assertNotIn("lose", snap["ago"])
        px = render(snap, n=1)[0]
        self.assertGreater(px[0], 150)
        self.assertGreater(px[1], 130)
        self.assertGreater(px[0], px[2] + 60)                            # gold
        s2 = started(team="radiant")
        s2.feed(msg(t=1800, state=POST, win="dire"), 10.0)
        snap2 = s2.snapshot(10.0)
        self.assertEqual(snap2["ago"].get("lose"), 0.0)
        px2 = render(snap2, n=1)[0]
        self.assertGreater(px2[0], px2[1] + 40)                          # red ...
        self.assertLess(max(px2), 140)                                   # ... and dim
        s3 = started(team="dire")
        s3.feed(msg(t=1800, state=POST, team="dire", win="dire"), 10.0)
        self.assertIn("win", s3.snapshot(10.0)["ago"])

    def test_the_result_counts_once_and_is_held_not_flickered(self):
        s = started()
        s.feed(msg(t=1800, state=POST, win="radiant"), 10.0)
        s.feed(msg(t=1801, state=POST, win="radiant"), 12.0)
        self.assertAlmostEqual(s.snapshot(12.0)["ago"]["win"], 2.0)
        seen = {tuple(render(s.snapshot(10.0 + k * 0.5), n=1)[0]) for k in range(8)}
        self.assertEqual(len(seen), 1)

    def test_no_winner_yet_is_no_result(self):
        s = started()
        s.feed(msg(t=1800, state=POST, win="none"), 10.0)
        self.assertNotIn("win", s.snapshot(10.0)["ago"])
        self.assertNotIn("lose", s.snapshot(10.0)["ago"])

    def test_the_match_is_over_a_while_after_the_end_and_the_look_comes_back(self):
        s = started()
        s.feed(msg(t=1800, state=POST, win="radiant"), 10.0)
        self.assertTrue(s.snapshot(15.0)["inMatch"])
        s.feed(msg(t=1830, state=POST, win="radiant"), 25.0)
        self.assertFalse(s.snapshot(25.0)["inMatch"])

    def test_joining_at_the_end_screen_isnt_news(self):
        s = DotaState()
        s.feed(msg(t=1800, state=POST, win="radiant"), 1.0)
        self.assertNotIn("win", s.snapshot(1.0)["ago"])


class NewMatch(unittest.TestCase):
    def test_kills_going_down_is_a_new_match(self):
        s = started(kills=9, deaths=4)
        s.feed(msg(t=600, kills=9, deaths=4, events=[ev(590, "roshan_killed", killed_by_team="radiant")]), 2.0)
        s.feed(msg(t=20, kills=0, deaths=0, state=PRE), 100.0)
        snap = s.snapshot(100.0)
        self.assertEqual(snap["ago"], {})
        self.assertTrue(snap["alive"])
        s.feed(msg(t=21, kills=1, deaths=0, state=PLAY), 101.0)
        self.assertEqual(s.snapshot(101.0)["ago"].get("my_kill"), 0.0)    # the first kill of the new match flashes

    def test_going_back_to_the_start_is_a_new_match_even_when_the_last_ended(self):
        s = started()
        s.feed(msg(t=1800, state=POST, win="radiant"), 10.0)
        s.feed(msg(t=-60, state=PICK, kills=0, team="dire"), 200.0)
        snap = s.snapshot(200.0)
        self.assertEqual(snap["ago"], {})
        self.assertTrue(snap["inMatch"])
        self.assertEqual(snap["team"], "dire")
        s.feed(msg(t=600, state=PLAY, team="dire"), 205.0)
        s.feed(msg(t=1800, state=POST, team="dire", win="dire"), 210.0)
        self.assertIn("win", s.snapshot(210.0)["ago"])                    # and its end counts again

    def test_kills_in_the_second_match_flash_again(self):
        s = started(kills=5)
        s.feed(msg(t=10, kills=0, state=PRE), 100.0)
        s.feed(msg(t=700, kills=0), 110.0)
        s.feed(msg(t=800, kills=1), 120.0)
        self.assertEqual(s.snapshot(120.0)["ago"].get("my_kill"), 0.0)


class Spectating(unittest.TestCase):
    def watching(self):
        """Dota sends one player per team, not a player of your own."""
        data = msg()
        data["player"] = {"team2": {"player0": {"kills": 3, "deaths": 1, "team_name": "radiant"}},
                          "team3": {"player5": {"kills": 2, "deaths": 4, "team_name": "dire"}}}
        data.pop("hero")
        return data

    def test_spectating_is_not_playing(self):
        s = DotaState()
        s.feed(self.watching(), 1.0)
        self.assertFalse(s.snapshot(1.0)["inMatch"])

    def test_another_players_steam_id_is_not_you(self):
        data = msg()
        data["provider"]["steamid"] = "76561198000000001"
        data["player"]["steamid"] = "76561198999999999"
        s = DotaState()
        s.feed(data, 1.0)
        self.assertFalse(s.snapshot(1.0)["inMatch"])
        data["player"]["steamid"] = data["provider"]["steamid"]
        s.feed(data, 2.0)
        self.assertTrue(s.snapshot(2.0)["inMatch"])

    def test_watching_after_playing_stops_the_match_and_flashes_nothing(self):
        s = started(kills=1)
        s.feed(self.watching(), 5.0)
        self.assertFalse(s.snapshot(5.0)["inMatch"])
        self.assertNotIn("my_kill", s.snapshot(5.0)["ago"])


class FlashRate(unittest.TestCase):
    def starts(self, s, seconds=12.0, step=0.05, begin=10.0):
        """When each flash started, found by watching its age jump back to nothing."""
        out, last, t = [], {}, begin
        while t <= begin + seconds:
            for k, a in s.snapshot(t)["ago"].items():
                if k in ("dip", "death"):
                    continue
                if k in last and a < last[k] - 1e-9 or k not in last:
                    out.append(round(t - a, 3))
                last[k] = a
            t += step
        return sorted(set(out))

    def busy_match(self):
        s = started()
        kills = 0
        for i in range(8):                                               # a kill every 0.1 s, a Roshan and an aegis on top
            kills += 1
            extra = [ev(700, "roshan_killed", killed_by_team="radiant"), ev(700, "aegis_picked_up", player_id=2)] if i == 3 else []
            s.feed(msg(t=700 + i, kills=kills, events=extra), 10.0 + i * 0.1)
        return s

    def test_never_more_than_three_flashes_in_a_second(self):
        s = self.busy_match()
        starts = self.starts(s)
        self.assertGreaterEqual(len(starts), 4)
        for a in starts:
            self.assertLessEqual(sum(1 for b in starts if a <= b < a + 1.0), 3)
        self.assertTrue(all(b - a >= games.MIN_GAP - 1e-6 for a, b in zip(starts, starts[1:])))

    def test_the_strongest_goes_first_and_old_news_is_dropped(self):
        s = started()
        s.feed(msg(t=700, kills=1, events=[ev(700, "roshan_killed", killed_by_team="radiant")]), 10.0)
        self.assertEqual(set(s.snapshot(10.0)["ago"]), {"roshan"})
        self.assertIn("my_kill", s.snapshot(12.0)["ago"])                # its turn came when Roshan's was over
        s2 = started()
        s2.feed(msg(t=700, kills=1, events=[ev(700, "roshan_killed", killed_by_team="radiant")]), 10.0)
        s2.snapshot(10.0)
        self.assertNotIn("my_kill", s2.snapshot(30.0)["ago"])            # asked far too late: no longer news

    def test_a_slow_light_holds_the_flash_for_at_least_a_third_of_a_second(self):
        s = started(hp=100)
        s.feed(msg(t=601, hp=100, kills=1), 10.0)
        quiet = render(started(hp=100).snapshot(1.0), n=1, fps=10)[0]
        for age in (0.0, 0.1, 0.2, 0.3, 0.34):
            px = render(s.snapshot(10.0 + age), n=1, fps=10)[0]
            self.assertGreater(px[0], quiet[0] + 100, age)               # gold on top of the green, all the way
        self.assertEqual(render(s.snapshot(10.6), n=1, fps=10)[0], quiet)
        fast = [render(s.snapshot(10.0 + a), n=1, fps=100)[0][0] for a in (0.0, 0.15, 0.3)]
        self.assertGreater(fast[0], fast[1])
        self.assertGreater(fast[1], fast[2])

    def test_a_dip_on_a_slow_light_is_held_for_its_whole_length(self):
        s = started()
        s.feed(msg(stunned=True), 10.0)
        quiet = render(started().snapshot(1.0), n=1, fps=10)[0]
        for age in (0.0, 0.3, 0.6):
            self.assertNotEqual(render(s.snapshot(10.0 + age), n=1, fps=10)[0], quiet)

    def test_every_flash_is_shorter_than_its_slot(self):
        for kind, length in games.DOTA_LENGTH.items():
            self.assertGreaterEqual(length, 0.35, kind)
        self.assertEqual(set(games.DOTA_LENGTH), set(games.DOTA_FLASHES))


# ---- the connection: one listener for both games -----------------------------------------------------

def steam(tmp, dota=True, cs2=False):
    """A fake Steam library in a temp folder: never a real one."""
    lib = Path(tmp) / "SteamLibrary"
    (lib / "steamapps").mkdir(parents=True)
    if dota:
        (lib / "steamapps" / "appmanifest_570.acf").write_text('"AppState"\n{\n\t"appid"\t\t"570"\n\t"installdir"\t\t"dota 2 beta"\n}\n', encoding="utf-8")
        (lib / "steamapps" / "common" / "dota 2 beta" / "game" / "dota").mkdir(parents=True)
    if cs2:
        (lib / "steamapps" / "appmanifest_730.acf").write_text('"AppState"\n{\n\t"appid"\t\t"730"\n\t"installdir"\t\t"Counter-Strike Global Offensive"\n}\n', encoding="utf-8")
        (lib / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg").mkdir(parents=True)
    return lib


def cs2_msg(kills=0, health=100):
    return {"provider": {"name": "Counter-Strike 2", "appid": 730, "steamid": "76561198000000001"},
            "map": {"name": "de_mirage", "phase": "live"},
            "player": {"steamid": "76561198000000001", "activity": "playing", "team": "CT",
                       "state": {"health": health, "armor": 100, "flashed": 0, "smoked": 0, "burning": 0,
                                 "round_kills": kills, "round_killhs": 0}},
            "round": {"phase": "live"}}


class Cfg(unittest.TestCase):
    def test_the_file_asks_for_what_the_effect_reads_and_goes_to_this_pc(self):
        text = games.dota_cfg_text(51234, "tok123")
        self.assertIn('"uri"\t\t"http://127.0.0.1:51234/dota2"', text)
        self.assertIn('"token"\t"tok123"', text)
        for block in ("provider", "map", "player", "hero", "events"):
            self.assertIn(f'"{block}"', text)
        self.assertNotIn('"abilities"', text)
        self.assertNotIn('"items"', text)
        self.assertIn('"heartbeat"', text)
        self.assertEqual(text.count("{"), text.count("}"))

    def test_the_cs2_file_is_as_it_was(self):
        text = games.cfg_text(51234, "tok123")
        self.assertIn("http://127.0.0.1:51234/cs2", text)
        self.assertIn('"round"', text)
        self.assertNotIn("dota", text)

    def test_the_folder_is_found_through_steams_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = steam(tmp)
            with mock.patch.object(games, "steam_libraries", return_value=[Path(tmp) / "nope", lib]):
                found = games.find_dota2()
            self.assertEqual(found, lib / "steamapps" / "common" / "dota 2 beta" / "game" / "dota" / "cfg" / "gamestate_integration")
            self.assertFalse(found.exists())                              # Dota doesn't ship it: connecting makes it

    def test_no_dota_means_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(games, "steam_libraries", return_value=[steam(tmp, dota=False)]):
                self.assertIsNone(games.find_dota2())
            lib = steam(Path(tmp) / "b")
            (lib / "steamapps" / "common" / "dota 2 beta" / "game" / "dota").rmdir()   # (manifest left behind, game removed)
            with mock.patch.object(games, "steam_libraries", return_value=[lib]):
                self.assertIsNone(games.find_dota2())

    def test_the_setting_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIs(Config(Path(tmp) / "config.json").data["games"]["dota"], False)


class Connecting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = steam(self.tmp.name, dota=True, cs2=True)
        self.patch = mock.patch.object(games, "steam_libraries", return_value=[self.lib])
        self.patch.start()
        self.changed = []
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: self.changed.append(1))
        self.dota_dir = self.lib / "steamapps" / "common" / "dota 2 beta" / "game" / "dota" / "cfg" / "gamestate_integration"
        self.cs2_cfg = self.lib / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg" / games.CFG_NAME

    def tearDown(self):
        self.g.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def post(self, data, token=None):
        port = self.g._server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/dota2", data=json.dumps(data).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=3).status

    def test_connecting_makes_the_folder_and_writes_the_file_and_disconnecting_removes_it(self):
        self.g.connect_dota()
        path = self.dota_dir / games.CFG_NAME
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn(self.g.settings["token"], text)
        self.assertIn(f":{self.g.settings['port']}/dota2", text)
        self.assertTrue(self.g.settings["dota"])
        d = self.g.describe()["dota"]
        self.assertTrue(d["found"] and d["connected"] and d["fileThere"])
        self.assertFalse(self.cs2_cfg.exists())                           # CS2 isn't touched
        self.g.disconnect_dota()
        self.assertFalse(path.exists())
        self.assertFalse(self.g.settings["dota"])
        self.assertFalse(self.g.describe()["dota"]["connected"])
        self.assertIsNone(self.g._server)                                 # nobody needs the listener any more

    def test_connecting_without_dota_says_so(self):
        with mock.patch.object(games, "find_dota2", return_value=None):
            with self.assertRaises(ValueError):
                self.g.connect_dota()
        self.assertFalse(self.g.settings["dota"])

    def test_both_games_share_one_listener_and_one_token(self):
        self.g.connect()
        server = self.g._server
        self.g.connect_dota()
        self.assertIs(self.g._server, server)
        dota_text = (self.dota_dir / games.CFG_NAME).read_text(encoding="utf-8")
        cs2_text = self.cs2_cfg.read_text(encoding="utf-8")
        token, port = self.g.settings["token"], self.g.settings["port"]
        for text in (dota_text, cs2_text):
            self.assertIn(token, text)
            self.assertIn(f":{port}/", text)
        self.g.disconnect()                                               # CS2 goes, Dota keeps listening
        self.assertFalse(self.cs2_cfg.exists())
        self.assertIsNotNone(self.g._server)
        self.assertTrue((self.dota_dir / games.CFG_NAME).is_file())
        self.g.disconnect_dota()
        self.assertIsNone(self.g._server)

    def test_messages_are_routed_by_the_appid(self):
        self.g.connect()
        self.g.connect_dota()
        token = self.g.settings["token"]
        data = msg(kills=0)
        data["auth"] = {"token": token}
        self.assertEqual(self.post(data), 200)
        self.assertTrue(self.g.dota.snapshot(time.monotonic())["inMatch"])
        self.assertEqual(self.g.dota.snapshot(time.monotonic())["team"], "radiant")
        self.assertFalse(self.g.cs2.in_match)                             # nothing went to CS2
        cs2 = cs2_msg()
        cs2["auth"] = {"token": token}
        self.assertEqual(self.post(cs2), 200)
        self.assertTrue(self.g.cs2.in_match)
        self.assertEqual(self.g.cs2.team, "CT")
        self.assertEqual(self.g.cs2.health, 100)
        self.assertEqual(self.g.dota.snapshot(time.monotonic())["team"], "radiant")   # ... and nothing to Dota
        self.assertTrue(self.changed)                                     # the window was told a match began

    def test_a_message_with_the_wrong_token_is_refused(self):
        self.g.connect_dota()
        data = msg()
        data["auth"] = {"token": "not-it"}
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(data)
        self.assertEqual(e.exception.code, 403)
        self.assertFalse(self.g.dota.snapshot(time.monotonic())["inMatch"])

    def test_a_message_without_a_provider_still_goes_to_cs2(self):
        self.g.connect()
        data = cs2_msg()
        data.pop("provider")
        data["auth"] = {"token": self.g.settings["token"]}
        self.post(data)
        self.assertTrue(self.g.cs2.in_match)

    def test_the_describe_has_all_three_games(self):
        d = self.g.describe()
        self.assertEqual(set(d), {"cs2", "lol", "dota"})
        self.assertEqual(set(d["dota"]), {"found", "connected", "fileThere", "receiving", "inMatch", "team", "health"})

    def test_describe_follows_the_match(self):
        self.g.connect_dota()
        self.g._route({**msg(hp=40), "auth": {}})
        d = self.g.describe()["dota"]
        self.assertEqual((d["receiving"], d["inMatch"], d["team"], d["health"]), (True, True, "radiant", 40))
        self.assertTrue(self.g.dota_active(time.monotonic()))
        self.g.disconnect_dota()
        self.assertFalse(self.g.dota_active(time.monotonic()))

    def test_the_effect_is_known(self):
        self.assertIn("game_dota", EFFECTS)
        self.assertEqual(EFFECTS["game_dota"].group, "game")
        self.assertEqual(EFFECTS["game_dota"].name, "Dota 2")


class SlowDisk(unittest.TestCase):
    """A Steam library on a hard disk that spun down: the window must never wait for it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "gamestate_integration"
        self._find, self._find_cs2 = games.find_dota2, games.find_cs2
        games.find_dota2 = self.slow_find
        games.find_cs2 = lambda: None
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: None)

    def tearDown(self):
        games.find_dota2, games.find_cs2 = self._find, self._find_cs2
        self.tmp.cleanup()

    def slow_find(self):
        time.sleep(1.0)
        return self.folder

    def test_the_window_doesnt_wait(self):
        started_at = time.monotonic()
        first = self.g.describe()["dota"]
        self.assertLess(time.monotonic() - started_at, 0.2)
        self.assertFalse(first["found"])                                  # not known yet: looked up in the background
        time.sleep(1.4)
        started_at = time.monotonic()
        self.assertTrue(self.g.describe()["dota"]["found"])
        self.assertLess(time.monotonic() - started_at, 0.2)

    def test_it_is_looked_up_again_in_the_background_when_old(self):
        self.g.dota_folder(fresh=True)
        at, folder = self.g._found_dota
        self.g._found_dota = (at - 61, folder)
        started_at = time.monotonic()
        self.g.dota_folder()
        self.assertLess(time.monotonic() - started_at, 0.2)
        time.sleep(1.4)
        self.assertGreater(self.g._found_dota[0], at)

    def test_connecting_looks_right_away(self):
        self.g._listen = lambda: None
        self.g.settings.update(port=1, token="t")
        self.g.connect_dota()
        d = self.g.describe()["dota"]
        self.assertTrue(d["found"] and d["connected"] and d["fileThere"])
        self.assertTrue((self.folder / games.CFG_NAME).is_file())
        self.g.disconnect_dota()
        self.assertFalse(self.g.describe()["dota"]["fileThere"])

    def test_starting_never_waits_for_the_disk(self):
        started_at = time.monotonic()
        self.g.start()
        self.assertLess(time.monotonic() - started_at, 0.3)
        self.g.stop()


# ---- the automatic look --------------------------------------------------------------------------------

class Manager(FakeManager):
    def __init__(self):
        super().__init__()
        self.games.settings["dota"] = True

    def game_lights(self, effect="game_cs2"):
        self.look = {"lamp": {"on": True, "mode": "effect", "effect": effect}}


class Automatic(unittest.TestCase):
    def setUp(self):
        self.m = Manager()
        self.a = Automations(self.m)
        self.t = 0

    def run_for(self, seconds, dota=False, lol=False, cs2=False, kind=None):
        for _ in range(seconds):
            self.a.step(self.t, self.a.rules(), kind, 0.0, cs2, frozenset(), False, lol, dota)
            self.t += 1

    def effect(self):
        return self.m.look["lamp"].get("effect") or self.m.look["lamp"].get("color")

    def test_the_rule_is_there_right_after_league_and_only_when_connected(self):
        self.assertEqual(self.a.rules()["dota"], "game:dota")
        self.assertEqual(list(self.a.rules())[:3], ["cs2", "lol", "dota"])
        self.m.games.settings["dota"] = False
        self.assertEqual(self.a.rules()["dota"], "")

    def test_a_match_changes_the_lights_and_the_end_puts_them_back(self):
        self.run_for(1, dota=True)
        self.assertEqual(self.effect(), "#ffffff")                        # not yet: a match has to last a second
        self.run_for(2, dota=True)
        self.assertEqual(self.effect(), "game_dota")
        self.assertEqual(self.a.active, "dota")
        self.run_for(4, dota=False)
        self.assertEqual(self.effect(), "game_dota")                      # a short gap in the messages: still playing along
        self.run_for(4, dota=False)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_nothing_happens_when_not_connected(self):
        self.m.games.settings["dota"] = False
        self.run_for(10, dota=True)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_cs2_and_league_go_before_dota_and_dota_before_a_full_screen_game(self):
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.m.games.settings.update(cs2=True, lol=True)
        self.run_for(10, dota=True, kind="game")
        self.assertEqual(self.a.active, "dota")
        self.assertEqual(self.effect(), "game_dota")
        self.run_for(10, dota=True, lol=True, kind="game")
        self.assertEqual(self.a.active, "lol")
        self.run_for(10, dota=True, lol=True, cs2=True, kind="game")
        self.assertEqual(self.a.active, "cs2")
        self.assertEqual(self.effect(), "game_cs2")

    def test_a_match_keeps_music_from_taking_over(self):
        self.m.config.data["settings"]["auto_show"] = True
        self.run_for(5, dota=True)
        self.assertTrue(self.a._dota)
        self.a._listen(self.t, self.a.rules(), 1.0)
        self.assertEqual(self.a._checks, 0)

    def test_a_change_by_hand_is_left_alone(self):
        self.run_for(3, dota=True)
        self.m.look = {"lamp": {"on": True, "mode": "color", "color": "#123456"}}
        self.run_for(10, dota=True)
        self.assertEqual(self.effect(), "#123456")

    def test_the_loop_tells_step_about_a_dota_match(self):
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
        self.m.games.dota_active = lambda now: True
        self.a._stop = Stop(2)
        with mock.patch.object(Automations, "step", lambda s, *args: seen.append(args)):
            self.a._run()
        self.assertTrue(seen and all(args[-1] is True for args in seen))


if __name__ == "__main__":
    unittest.main()
