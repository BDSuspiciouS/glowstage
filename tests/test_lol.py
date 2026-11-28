import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

from glowstage import games, lol
from glowstage.automations import Automations
from glowstage.config import Config
from glowstage.effects import EFFECTS
from glowstage.lol import LolPoller, LolState
from tests.test_app_looks import FakeManager


# ---- what the Live Client Data API says (shaped like the real thing, trimmed) -----------------------

def player(name, team, dead=False, respawn=0.0, tag="EUW"):
    return {"riotId": f"{name}#{tag}", "riotIdGameName": name, "summonerName": name, "team": team,
            "isDead": dead, "respawnTimer": respawn, "championName": "Ahri", "level": 9}


def ev(eid, name, at, **more):
    return {"EventID": eid, "EventName": name, "EventTime": at, **more}


def match(t=100.0, hp=500.0, events=(), me="Batikan", team="ORDER", dead=False, respawn=0.0):
    other = "CHAOS" if team == "ORDER" else "ORDER"
    return {
        "activePlayer": {"riotId": f"{me}#EUW", "riotIdGameName": me, "summonerName": me, "level": 9,
                         "championStats": {"currentHealth": hp, "maxHealth": 1000.0, "resourceType": "MANA",
                                           "resourceValue": 300.0, "resourceMax": 600.0}},
        "allPlayers": [player(me, team, dead, respawn), player("Mate", team),
                       player("Foe1", other), player("Foe2", other)],
        "events": {"Events": [ev(0, "GameStart", 0.0), ev(1, "MinionsSpawning", 65.0), *events]},
        "gameData": {"gameMode": "CLASSIC", "gameTime": t, "mapName": "Map11"},
    }


NOT_FOUND = {"errorCode": "RESOURCE_NOT_FOUND", "httpStatus": 404, "message": "Resource not found"}


def started(state, **kw):
    """A state that has seen the match once, so what follows is news."""
    state.feed(match(**kw), 1.0)
    return state


def render(snap, n=10, fps=100, effect="game_lol", t=0.0):
    return EFFECTS[effect].render(t, n, None, {}, {"lol": snap, "fps": fps})


# ---- the state --------------------------------------------------------------------------------------

class Colors(unittest.TestCase):
    def test_the_blue_side_is_blue_and_the_red_side_is_red(self):
        for team, strong in (("ORDER", 2), ("CHAOS", 0)):
            s = started(LolState(), hp=1000.0, team=team)
            c = render(s.snapshot(1.0), n=1)[0]
            self.assertEqual(max(range(3), key=lambda i: c[i]), strong, (team, c))
            self.assertEqual(s.snapshot(1.0)["team"], team)

    def test_health_is_a_bar_across_the_stage(self):
        s = started(LolState(), hp=500.0)
        px = render(s.snapshot(1.0), n=10)
        self.assertGreater(sum(px[0]), 3 * sum(px[-1]))       # the right side goes dark first
        self.assertEqual(s.snapshot(1.0)["health"], 0.5)
        full = render(started(LolState(), hp=1000.0).snapshot(1.0), n=10)
        self.assertGreater(sum(full[-1]), 3 * sum(px[-1]))

    def test_no_match_drifts_in_both_colors_and_stays_dim(self):
        px = render({"inMatch": False, "ago": {}}, n=8)
        self.assertTrue(all(max(p) < 140 for p in px))

    def test_missing_fields_dont_break_it(self):
        s = LolState()
        s.feed({"activePlayer": {"riotId": "A#B"}}, 1.0)
        snap = s.snapshot(1.0)
        self.assertTrue(snap["inMatch"])
        self.assertEqual(len(render(snap, n=4)), 4)
        s.feed({"activePlayer": {"riotId": "A#B", "championStats": "nope"}, "events": [], "allPlayers": {}, "gameData": 3}, 2.0)
        self.assertTrue(s.snapshot(2.0)["inMatch"])


class Death(unittest.TestCase):
    def test_respawn_fills_from_zero_to_one_as_it_counts_down(self):
        s = started(LolState(), hp=0.0)
        s.feed(match(hp=0.0, dead=True, respawn=20.0), 10.0)
        snap = s.snapshot(10.0)
        self.assertTrue(snap["dead"])
        self.assertAlmostEqual(snap["respawn"], 0.0)
        s.feed(match(hp=0.0, dead=True, respawn=10.0), 20.0)
        self.assertAlmostEqual(s.snapshot(20.0)["respawn"], 0.5)
        self.assertAlmostEqual(s.snapshot(25.0)["respawn"], 0.75)   # between two asks it keeps counting
        s.feed(match(hp=900.0, dead=False), 31.0)
        self.assertFalse(s.snapshot(31.0)["dead"])
        self.assertEqual(s.snapshot(31.0)["respawn"], 0.0)

    def test_dead_is_a_dim_cold_grey_and_the_respawn_bar_fills(self):
        s = started(LolState(), hp=0.0)
        s.feed(match(hp=0.0, dead=True, respawn=20.0), 10.0)
        grey = render(s.snapshot(10.0), n=10)
        self.assertTrue(all(max(p) < 40 for p in grey))
        self.assertTrue(all(abs(p[0] - p[2]) < 40 for p in grey))        # no team color yet: cold grey
        s.feed(match(hp=0.0, dead=True, respawn=5.0), 25.0)
        bar = render(s.snapshot(25.0), n=10)
        self.assertGreater(sum(bar[0]), 2 * sum(bar[-1]))                 # filling from the left
        self.assertGreater(bar[0][2], bar[0][0])                          # in the team color (blue side)

    def test_a_second_death_starts_a_new_countdown(self):
        s = started(LolState())
        s.feed(match(dead=True, respawn=8.0), 5.0)
        s.feed(match(dead=False), 14.0)
        s.feed(match(dead=True, respawn=30.0), 40.0)
        s.feed(match(dead=True, respawn=15.0), 55.0)
        self.assertAlmostEqual(s.snapshot(55.0)["respawn"], 0.5)


class Moments(unittest.TestCase):
    def flash_once(self, event, key, mine=True, extra=None):
        s = started(LolState())
        data = match(t=101.0, events=[event])
        s.feed(data, 10.0)
        ago = s.snapshot(10.0)["ago"]
        if mine:
            self.assertEqual(ago.get(key), 0.0, key)
            s.feed(data, 11.0)                                   # the next ask says the same
            self.assertAlmostEqual(s.snapshot(11.0)["ago"][key], 1.0)   # ... and doesn't flash again
        else:
            self.assertNotIn(key, ago)
        return s

    def test_your_kill_flashes_once(self):
        self.flash_once(ev(5, "ChampionKill", 100.0, KillerName="Batikan", VictimName="Foe1", Assisters=[]), "my_kill")

    def test_a_kill_by_someone_else_doesnt(self):
        self.flash_once(ev(5, "ChampionKill", 100.0, KillerName="Mate", VictimName="Foe1"), "my_kill", mine=False)
        self.flash_once(ev(5, "ChampionKill", 100.0, KillerName="Foe1", VictimName="Batikan"), "my_kill", mine=False)

    def test_a_multikill_is_its_own_flash_and_replaces_the_kill(self):
        s = started(LolState())
        s.feed(match(t=101.0, events=[ev(5, "ChampionKill", 100.0, KillerName="Batikan", VictimName="Foe1"),
                                      ev(6, "Multikill", 100.0, KillerName="Batikan", KillStreak=2)]), 10.0)
        ago = s.snapshot(10.0)["ago"]
        self.assertEqual(ago.get("multikill"), 0.0)
        self.assertNotIn("my_kill", ago)

    def test_your_teams_ace_flashes_theirs_doesnt(self):
        self.flash_once(ev(5, "Ace", 100.0, Acer="Mate", AcingTeam="ORDER"), "ace")
        self.flash_once(ev(5, "Ace", 100.0, Acer="Foe1", AcingTeam="CHAOS"), "ace", mine=False)

    def test_your_teams_dragon_flashes_in_the_dragons_color(self):
        s = self.flash_once(ev(5, "DragonKill", 100.0, DragonType="Fire", KillerName="Mate", Assisters=[], Stolen="False"), "dragon")
        self.assertEqual(s.snapshot(11.0)["dragon"], "Fire")

    def test_the_enemy_taking_an_objective_doesnt_flash(self):
        for name, key, more in (("DragonKill", "dragon", {"DragonType": "Water"}), ("BaronKill", "baron", {}), ("HeraldKill", "herald", {})):
            s = self.flash_once(ev(5, name, 100.0, KillerName="Foe2", **more), key, mine=False)
            self.assertEqual(s.pending, [])
            self.assertEqual(render(s.snapshot(10.0)), render(started(LolState()).snapshot(1.0)))

    def test_baron_and_herald_flash_for_your_team(self):
        self.flash_once(ev(5, "BaronKill", 100.0, KillerName="Mate", Stolen="False"), "baron")
        self.flash_once(ev(5, "HeraldKill", 100.0, KillerName="Batikan", Stolen="False"), "herald")

    def test_on_the_red_side_the_other_team_is_the_enemy(self):
        s = started(LolState(), team="CHAOS")
        s.feed(match(t=101.0, team="CHAOS", events=[ev(5, "DragonKill", 100.0, KillerName="Foe1", DragonType="Air"),
                                                    ev(6, "BaronKill", 100.0, KillerName="Mate")]), 10.0)
        ago = s.snapshot(10.0)["ago"]
        self.assertNotIn("dragon", ago)
        self.assertIn("baron", ago)

    def test_the_end_holds_gold_for_a_win_and_dim_red_for_a_loss(self):
        for result, key in (("Win", "win"), ("Lose", "lose")):
            s = started(LolState())
            s.feed(match(t=1800.0, events=[ev(9, "GameEnd", 1799.0, Result=result)]), 10.0)
            snap = s.snapshot(12.0)
            self.assertEqual(snap["ago"][key], 2.0)
            px = render(snap, n=6)
            if key == "win":
                self.assertTrue(all(p[0] > 180 and p[1] > 140 and p[2] < 120 for p in px), px)
            else:
                self.assertTrue(all(p[0] > p[1] * 3 and max(p) < 140 for p in px), px)
            # a few seconds later your own look is allowed back
            s.feed(match(t=1805.0, events=[ev(9, "GameEnd", 1799.0, Result=result)]), 14.0)
            self.assertTrue(s.snapshot(14.5)["inMatch"])
            s.feed(match(t=1830.0, events=[ev(9, "GameEnd", 1799.0, Result=result)]), 25.0)
            self.assertFalse(s.snapshot(25.0)["inMatch"])

    def test_joining_late_doesnt_replay_the_match(self):
        s = LolState()
        s.feed(match(t=900.0, events=[ev(5, "ChampionKill", 300.0, KillerName="Batikan", VictimName="Foe1"),
                                      ev(6, "DragonKill", 500.0, KillerName="Mate", DragonType="Earth"),
                                      ev(7, "ChampionKill", 898.0, KillerName="Batikan", VictimName="Foe2")]), 5.0)
        ago = s.snapshot(5.0)["ago"]
        self.assertEqual(set(ago), {"my_kill"})                  # only the one that just happened

    def test_a_new_match_resets(self):
        s = LolState()
        s.feed(match(t=1700.0, team="ORDER", events=[ev(5, "ChampionKill", 1699.0, KillerName="Batikan"),
                                                      ev(6, "GameEnd", 1699.5, Result="Win")]), 5.0)
        self.assertIn("win", s.snapshot(5.0)["ago"])
        s.feed(match(t=12.0, team="CHAOS", events=[]), 400.0)    # the clock went back, the event numbers restart
        snap = s.snapshot(400.0)
        self.assertEqual(snap["ago"], {})
        self.assertEqual(snap["team"], "CHAOS")
        self.assertTrue(snap["inMatch"])
        # the same event numbers again are news in the new match
        s.feed(match(t=40.0, team="CHAOS", events=[ev(5, "ChampionKill", 39.0, KillerName="Batikan")]), 430.0)
        self.assertEqual(s.snapshot(430.0)["ago"].get("my_kill"), 0.0)

    def test_event_numbers_going_back_alone_is_a_new_match_too(self):
        s = LolState()
        s.feed(match(t=500.0, events=[ev(8, "ChampionKill", 499.0, KillerName="Batikan")]), 5.0)
        s.feed(match(t=502.0, events=[]), 7.0)                    # (same clock range, but the events are gone)
        self.assertEqual(s.snapshot(7.0)["ago"], {})


class Names(unittest.TestCase):
    def kill_by(self, name, me="Batikan"):
        s = started(LolState(), me=me)
        s.feed(match(t=101.0, me=me, events=[ev(5, "ChampionKill", 100.0, KillerName=name, VictimName="Foe1")]), 3.0)
        return "my_kill" in s.snapshot(3.0)["ago"]

    def test_names_match_loosely(self):
        for name in ("Batikan", "batikan", "BATIKAN", "Batikan#EUW", "batikan#euw", " Batikan "):
            self.assertTrue(self.kill_by(name), name)
        for name in ("Mate", "Batik", "", None):
            self.assertFalse(self.kill_by(name), name)

    def test_the_old_summoner_name_alone_works(self):
        data = match(t=101.0, events=[ev(5, "ChampionKill", 100.0, KillerName="Old Name")])
        me = data["activePlayer"]
        for k in ("riotId", "riotIdGameName"):
            me.pop(k)
            data["allPlayers"][0].pop(k)
        me["summonerName"] = data["allPlayers"][0]["summonerName"] = "Old Name"
        s = LolState()
        s.feed(match(t=100.0), 1.0)
        s.feed(data, 2.0)
        self.assertEqual(s.snapshot(2.0)["ago"].get("my_kill"), 0.0)
        self.assertEqual(s.snapshot(2.0)["team"], "ORDER")

    def test_a_name_with_a_hash_in_a_different_tag_still_matches_the_game_name(self):
        self.assertTrue(self.kill_by("Batikan#1234"))


class FlashRate(unittest.TestCase):
    def starts(self, s, seconds=12.0, step=0.05, begin=10.0):
        """When each flash started, found by watching its age jump back to nothing."""
        out, last, t = [], {}, begin
        while t <= begin + seconds:
            for k, a in s.snapshot(t)["ago"].items():
                if k in last and a < last[k] - 1e-9 or k not in last:
                    out.append(round(t - a, 3))
                last[k] = a
            t += step
        return sorted(set(out))

    def test_never_more_than_three_flashes_in_a_second(self):
        s = started(LolState())
        events = [ev(10 + i, "ChampionKill", 100.0, KillerName="Batikan", VictimName="Foe1") for i in range(5)]
        events += [ev(20, "DragonKill", 100.0, KillerName="Mate", DragonType="Hextech"), ev(21, "BaronKill", 100.0, KillerName="Mate"),
                   ev(22, "HeraldKill", 100.0, KillerName="Mate"), ev(23, "Ace", 100.0, AcingTeam="ORDER"),
                   ev(24, "Multikill", 100.0, KillerName="Batikan", KillStreak=5)]
        s.feed(match(t=101.0, events=events), 10.0)
        starts = self.starts(s)
        self.assertGreaterEqual(len(starts), 4)
        for a in starts:
            self.assertLessEqual(sum(1 for b in starts if a <= b < a + 1.0), 3)
        self.assertTrue(all(b - a >= lol.MIN_GAP - 1e-6 for a, b in zip(starts, starts[1:])))

    def test_the_strongest_goes_first_and_old_news_is_dropped(self):
        s = started(LolState())
        s.feed(match(t=101.0, events=[ev(10, "ChampionKill", 100.0, KillerName="Batikan"), ev(11, "Ace", 100.0, AcingTeam="ORDER")]), 10.0)
        self.assertEqual(set(s.snapshot(10.0)["ago"]), {"ace"})
        self.assertIn("my_kill", s.snapshot(12.0)["ago"])         # its turn came when the ace was over
        s2 = started(LolState())
        s2.feed(match(t=101.0, events=[ev(10, "ChampionKill", 100.0, KillerName="Batikan"), ev(11, "Ace", 100.0, AcingTeam="ORDER")]), 10.0)
        s2.snapshot(10.0)
        self.assertNotIn("my_kill", s2.snapshot(30.0)["ago"])     # asked far too late: no longer news

    def test_a_slow_light_holds_the_flash_for_at_least_a_third_of_a_second(self):
        s = started(LolState(), hp=1000.0)
        s.feed(match(t=101.0, hp=1000.0, events=[ev(5, "ChampionKill", 100.0, KillerName="Batikan")]), 10.0)
        quiet = render(started(LolState(), hp=1000.0).snapshot(1.0), n=1, fps=10)[0]
        for age in (0.0, 0.1, 0.2, 0.3):
            px = render(s.snapshot(10.0 + age), n=1, fps=10)[0]
            self.assertGreater(px[0], quiet[0] + 100, age)       # gold on top of the blue, all the way
        self.assertEqual(render(s.snapshot(10.6), n=1, fps=10)[0], quiet)
        fast = [render(s.snapshot(10.0 + a), n=1, fps=100)[0][0] for a in (0.0, 0.15, 0.3)]
        self.assertGreater(fast[0], fast[1])
        self.assertGreater(fast[1], fast[2])

    def test_the_result_is_held_not_flickered(self):
        s = started(LolState())
        s.feed(match(t=1800.0, events=[ev(9, "GameEnd", 1799.0, Result="Win")]), 10.0)
        seen = {tuple(render(s.snapshot(10.0 + k * 0.5), n=1)[0]) for k in range(8)}
        self.assertEqual(len(seen), 1)


class Dragons(unittest.TestCase):
    COLORS = {   # which channel leads, for each dragon (on the blue side's blue)
        "Fire": lambda r, g, b: r > g > b, "Water": lambda r, g, b: g > r and b > r, "Earth": lambda r, g, b: r > g > b and g > 2 * b,
        "Air": lambda r, g, b: min(r, g, b) > 150 and b >= max(r, g), "Hextech": lambda r, g, b: g > r and b > r and r < 60,
        "Chemtech": lambda r, g, b: g > 2 * r and g > 2 * b, "Elder": lambda r, g, b: min(r, g, b) > 150 and r < b}

    def test_each_dragon_has_its_color(self):
        for kind, ok in self.COLORS.items():
            s = started(LolState(), hp=1000.0)
            s.feed(match(t=101.0, hp=1000.0, events=[ev(5, "DragonKill", 100.0, DragonType=kind, KillerName="Mate")]), 10.0)
            c = render(s.snapshot(10.0), n=1)[0]
            self.assertTrue(ok(*c), (kind, c))

    def test_baron_is_purple_and_herald_violet(self):
        for name, key in (("BaronKill", "baron"), ("HeraldKill", "herald")):
            s = started(LolState(), hp=1000.0, team="CHAOS")
            s.feed(match(t=101.0, hp=1000.0, team="CHAOS", events=[ev(5, name, 100.0, KillerName="Mate")]), 10.0)
            r, g, b = render(s.snapshot(10.0), n=1)[0]
            self.assertTrue(b > g and r > g, (key, r, g, b))

    def test_an_ace_sweeps_across_the_stage(self):
        s = started(LolState(), hp=1000.0)
        s.feed(match(t=101.0, hp=1000.0, events=[ev(5, "Ace", 100.0, AcingTeam="ORDER")]), 10.0)
        early = render(s.snapshot(10.2), n=20)
        late = render(s.snapshot(11.2), n=20)
        bright = lambda px: max(range(len(px)), key=lambda i: sum(px[i]))
        self.assertLess(bright(early), 8)
        self.assertGreater(bright(late), 10)


# ---- asking the game ----------------------------------------------------------------------------------

class Asking(unittest.TestCase):
    def poller(self, *answers):
        """A poller whose fetch gives the answers in turn (an exception is raised)."""
        queue = list(answers)

        def fetch():
            a = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(a, BaseException):
                raise a
            return a

        return LolPoller(fetch=fetch)

    def test_a_match_answers(self):
        p = self.poller(match())
        self.assertTrue(p.poll_once(1.0))
        self.assertTrue(p.in_match(1.0))
        self.assertTrue(p.answering)

    def test_nothing_that_goes_wrong_counts_as_a_match(self):
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        for answer in (NOT_FOUND, urllib.error.HTTPError(lol.URL, 404, "Not Found", {}, None), ConnectionRefusedError(),
                       refused, TimeoutError(), ValueError("half an answer"), [], None, "oops", {}):
            p = self.poller(answer)
            self.assertFalse(p.poll_once(5.0), answer)
            self.assertFalse(p.in_match(5.0), answer)
            self.assertFalse(p.receiving(5.0), answer)
            self.assertFalse(p.snapshot(5.0)["inMatch"])

    def test_spectating_is_not_playing(self):
        data = match()
        data.pop("activePlayer")
        p = self.poller(data)
        self.assertFalse(p.poll_once(1.0))
        self.assertFalse(p.in_match(1.0))
        self.assertTrue(p.receiving(1.0))                        # the game answers, there's just no you
        self.assertEqual(p.snapshot(1.0)["ago"], {})

    def test_spectating_after_playing_stops_the_match_at_once(self):
        spectator = match()
        spectator.pop("activePlayer")
        p = self.poller(match(), spectator)
        p.poll_once(1.0)
        p.poll_once(2.0)
        self.assertFalse(p.in_match(2.0))

    def test_one_missed_ask_doesnt_end_the_match_but_a_lasting_one_does(self):
        p = self.poller(match(), ConnectionRefusedError())
        p.poll_once(1.0)
        p.poll_once(2.0)
        self.assertTrue(p.in_match(2.0))
        self.assertFalse(p.answering)                            # (so it asks again in three seconds)
        p.poll_once(2.0 + lol.ALIVE + 0.5)
        self.assertFalse(p.in_match(2.0 + lol.ALIVE + 0.5))

    def test_the_window_hears_when_a_match_starts_and_ends(self):
        heard = []
        p = LolPoller(on_change=lambda: heard.append(1), fetch=lambda: match())
        p.poll_once(1.0)
        p.poll_once(2.0)
        self.assertEqual(len(heard), 1)
        p.fetch = lambda: NOT_FOUND
        p.poll_once(2.0 + lol.ALIVE + 1)
        self.assertEqual(len(heard), 2)

    def test_the_thread_asks_only_while_started_and_never_opens_a_socket_here(self):
        calls = []
        p = LolPoller(fetch=lambda: calls.append(1) or NOT_FOUND)
        time.sleep(0.15)
        self.assertEqual(calls, [])                              # nothing asks before start()
        p.start()
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        p.stop()
        self.assertTrue(calls)
        time.sleep(0.1)
        n = len(calls)
        time.sleep(0.3)
        self.assertEqual(len(calls), n)                          # stopped means stopped

    def test_the_real_fetch_only_ever_goes_to_this_pc_and_skips_the_certificate_check(self):
        self.assertTrue(lol.URL.startswith("https://127.0.0.1:"))
        import ssl
        ctx = lol._context()
        self.assertFalse(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)


class TheSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._find, self._find_dota = games.find_cs2, games.find_dota2
        games.find_cs2 = games.find_dota2 = lambda: None
        self.calls = []
        self.g = games.Games(Config(Path(self.tmp.name) / "config.json"), lambda: None)
        self.g.lol.fetch = lambda: self.calls.append(1) or match()

    def tearDown(self):
        self.g.stop()
        games.find_cs2, games.find_dota2 = self._find, self._find_dota
        self.tmp.cleanup()

    def test_off_until_switched_on(self):
        self.assertIs(Config(Path(self.tmp.name) / "other.json").data["games"]["lol"], False)
        self.g.start()
        time.sleep(0.2)
        self.assertEqual(self.calls, [])
        d = self.g.describe()["lol"]
        self.assertFalse(d["on"] or d["receiving"] or d["inMatch"])
        self.assertFalse(self.g.lol_active(time.monotonic()))

    def test_on_asks_and_describes_the_match(self):
        self.g.set_lol(True)
        deadline = time.monotonic() + 3
        while not self.g.describe()["lol"]["inMatch"] and time.monotonic() < deadline:
            time.sleep(0.02)
        d = self.g.describe()["lol"]
        self.assertEqual((d["on"], d["receiving"], d["inMatch"], d["team"], d["health"]), (True, True, True, "ORDER", 50))
        self.assertTrue(self.g.lol_active(time.monotonic()))
        self.g.set_lol(False)
        self.assertFalse(self.g.lol_active(time.monotonic()))
        self.assertFalse(self.g.describe()["lol"]["on"])
        time.sleep(0.1)
        n = len(self.calls)
        time.sleep(0.3)
        self.assertEqual(len(self.calls), n)

    def test_the_effect_is_known_and_the_cs2_description_is_still_there(self):
        self.assertIn("game_lol", EFFECTS)
        self.assertEqual(EFFECTS["game_lol"].group, "game")
        self.assertIn("cs2", self.g.describe())


# ---- the automatic look --------------------------------------------------------------------------------

class Manager(FakeManager):
    def __init__(self):
        super().__init__()
        self.games.settings["lol"] = True

    def game_lights(self, effect="game_cs2"):
        self.look = {"lamp": {"on": True, "mode": "effect", "effect": effect}}


class Automatic(unittest.TestCase):
    def setUp(self):
        self.m = Manager()
        self.a = Automations(self.m)
        self.t = 0

    def run_for(self, seconds, lol=False, cs2=False, kind=None):
        for _ in range(seconds):
            self.a.step(self.t, self.a.rules(), kind, 0.0, cs2, frozenset(), False, lol)
            self.t += 1

    def effect(self):
        return self.m.look["lamp"].get("effect") or self.m.look["lamp"].get("color")

    def test_the_rule_is_there_right_after_cs2_and_only_with_the_switch_on(self):
        self.assertEqual(self.a.rules()["lol"], "game:lol")
        self.assertEqual(list(self.a.rules())[:2], ["cs2", "lol"])
        self.m.games.settings["lol"] = False
        self.assertEqual(self.a.rules()["lol"], "")

    def test_a_match_changes_the_lights_and_the_end_puts_them_back(self):
        self.run_for(1, lol=True)
        self.assertEqual(self.effect(), "#ffffff")                # not yet: a match has to last a second
        self.run_for(2, lol=True)
        self.assertEqual(self.effect(), "game_lol")
        self.assertEqual(self.a.active, "lol")
        self.run_for(4, lol=False)
        self.assertEqual(self.effect(), "game_lol")               # a short gap in the answers: still playing along
        self.run_for(4, lol=False)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)
        self.assertEqual(self.m.config.data["settings"]["active_preset"], "Mine")

    def test_nothing_happens_with_the_switch_off(self):
        self.m.games.settings["lol"] = False
        self.run_for(10, lol=True)
        self.assertEqual(self.effect(), "#ffffff")
        self.assertIsNone(self.a.active)

    def test_cs2_goes_before_league_and_league_before_a_full_screen_game(self):
        self.m.config.data["settings"]["auto_game"] = "preset:Gaming"
        self.m.games.settings["cs2"] = True
        self.run_for(10, lol=True, kind="game")
        self.assertEqual(self.a.active, "lol")
        self.assertEqual(self.effect(), "game_lol")
        self.run_for(10, lol=True, cs2=True, kind="game")
        self.assertEqual(self.a.active, "cs2")
        self.assertEqual(self.effect(), "game_cs2")

    def test_a_change_by_hand_is_left_alone(self):
        self.run_for(3, lol=True)
        self.m.look = {"lamp": {"on": True, "mode": "color", "color": "#123456"}}
        self.run_for(10, lol=True)
        self.assertEqual(self.effect(), "#123456")


if __name__ == "__main__":
    unittest.main()
