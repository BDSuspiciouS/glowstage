"""League of Legends lighting. Riot's official Live Client Data API answers on this PC while a
match runs (https://127.0.0.1:2999/liveclientdata/allgamedata): your health, who is dead, and
the events of the match (kills, dragons, the end). Nothing is written into the game and nothing
reads its memory; Glowstage only asks, once a second, the same question the in-game overlays ask.

LolState turns those answers into what the "game_lol" effect (effects.py) needs; LolPoller asks.
Games (games.py) owns the poller and starts it only while the switch in Setup → Games is on."""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)

URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
FAST, SLOW = 1.0, 3.0   # seconds between asks: while a match answers / otherwise (a refused connection is instant, so asking often costs nothing)
ALIVE = 4.0             # seconds without a good answer after which the match counts as gone (a single hiccup shouldn't dim the room)
END_LINGER = 10.0       # the match is over this long after the end: the win or loss shows, then your own look comes back
ABSORB = 5.0            # events older than this (seconds of game time) when we first look are history, not news
STALE = 6.0             # a flash that had to wait longer than this for its turn is no longer news (an ace plus a baron plus a dragon take about five seconds)

# The moments that flash, strongest first, and how long each lasts (seconds). The next flash
# starts only when the one before is over, plus a pause, so there are never more than three
# flashes in any second (the limit for people sensitive to flashing light, like Storm and CS2).
FLASHES = ("ace", "baron", "dragon", "herald", "multikill", "my_kill")
LENGTH = {"my_kill": 0.35, "multikill": 0.6, "ace": 1.6, "dragon": 0.9, "baron": 1.4, "herald": 1.1}
PAUSE = 0.1
MIN_GAP = 0.34          # flashes start at least this far apart: three fit in a second, never four


def _context() -> ssl.SSLContext:
    """The API's certificate is signed by Riot's own root, which Python doesn't trust, so a
    normal check always fails. Turning the check off is safe here only because this context is
    used for one address, 127.0.0.1: the connection never leaves this PC."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_opener = None


def _get_opener():
    """Made at the first look for a match, not at startup: loading the certificates takes a while."""
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                              urllib.request.HTTPSHandler(context=_context()))
    return _opener


def fetch_live(timeout: float = 1.0) -> dict:
    """The match as the game tells it now. Raises (refused, 404, timeout) while there is none."""
    with _get_opener().open(URL, timeout=timeout) as r:
        return json.loads(r.read(8_000_000))


# ---- who is who ---------------------------------------------------------------------------------

def _keys(player: dict) -> set[str]:
    """Every way the game may write a player's name: Name#TAG, Name, the old summoner name."""
    out = set()
    for field in ("riotId", "riotIdGameName", "summonerName"):
        v = player.get(field)
        if isinstance(v, str) and v.strip():
            v = v.strip().lower()
            out.add(v)
            out.add(v.split("#")[0])
    return out


def _is(name, keys: set[str]) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    n = name.strip().lower()
    return n in keys or n.split("#")[0] in keys


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---- what happens in the match ------------------------------------------------------------------

class LolState:
    """The match as the game last described it, plus the moments worth a flash with when they
    started. feed() takes one answer of the API; snapshot() is what the light effect reads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = -1e9                # when the last answer with you in a match came (monotonic)
        self.answered = -1e9            # when the API last answered at all (spectating counts)
        self._reset()

    def _reset(self) -> None:
        """A new match: forget the old one."""
        self.playing = False
        self.team: str | None = None    # "ORDER" (blue side) or "CHAOS" (red side)
        self.health = 1.0               # 0..1
        self.dead = False
        self.respawn_total = 0.0        # seconds the death started with
        self.respawn_left = 0.0         # ... and what was left when it was last read
        self.respawn_at = 0.0
        self.game_time = 0.0
        self.max_id = -1
        self.seen: set = set()          # EventIDs we have dealt with
        self.fresh = True               # first look at this match: older events are history
        self.events: dict[str, float] = {}   # flash name -> when it started
        self.dragon: str | None = None
        self.ended: float | None = None
        self.pending: list[tuple[str, float, str | None]] = []   # flashes waiting for their turn: (name, since, dragon type)
        self.busy_until = 0.0

    # -- reading --

    def feed(self, data, now: float) -> None:
        """One answer of the API (or None / an error: nothing to see)."""
        with self.lock:
            if not isinstance(data, dict) or data.get("errorCode") or not any(k in data for k in ("activePlayer", "allPlayers", "events", "gameData")):
                self._pump(now)
                return          # no match yet, or it's loading: the grace period decides when it counts as gone
            self.answered = now
            me = data.get("activePlayer")
            if not isinstance(me, dict) or not me:
                self.playing = False        # spectating: no you to follow
                self._pump(now)
                return
            if now - self.last > 30:
                self.fresh = True           # we had lost it for a while: whatever happened meanwhile isn't news
            events = _d(data.get("events")).get("Events") or []
            events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
            ids = [e["EventID"] for e in events if isinstance(e.get("EventID"), int)]
            game_time = _num(_d(data.get("gameData")).get("gameTime"), self.game_time)
            top = max(ids, default=-1)
            if self.last > -1e8 and (game_time < self.game_time - 2 or top < self.max_id or (not ids and self.seen)):
                self._reset()               # the clock or the event numbers went back: a new match
            self.last, self.game_time, self.playing = now, game_time, True
            self.max_id = max(self.max_id, top)

            mine = _keys(me)
            players = [p for p in (data.get("allPlayers") if isinstance(data.get("allPlayers"), list) else []) if isinstance(p, dict)]
            my_row = next((p for p in players if _keys(p) & mine), None)
            if my_row:
                team = my_row.get("team")
                if team in ("ORDER", "CHAOS"):
                    self.team = team
            stats = _d(me.get("championStats"))
            top_hp = _num(stats.get("maxHealth"))
            if top_hp > 0:
                self.health = max(0.0, min(1.0, _num(stats.get("currentHealth")) / top_hp))
            dead = bool((my_row or {}).get("isDead"))
            if dead:
                left = max(0.0, _num((my_row or {}).get("respawnTimer")))
                if not self.dead:
                    self.respawn_total = 0.0
                self.respawn_total = max(self.respawn_total, left)
                self.respawn_left, self.respawn_at = left, now
            self.dead = dead

            team_of = {}
            for p in players:
                for k in _keys(p):
                    team_of.setdefault(k, p.get("team"))

            def side(name) -> str | None:
                if not isinstance(name, str) or not name.strip():
                    return None
                n = name.strip().lower()
                return team_of.get(n) or team_of.get(n.split("#")[0])

            batch: list[tuple[str, str | None]] = []
            for e in events:
                eid = e.get("EventID")
                if eid in self.seen:
                    continue
                self.seen.add(eid)
                if self.fresh and _num(e.get("EventTime")) < game_time - ABSORB:
                    continue
                name = e.get("EventName")
                if name == "ChampionKill" and _is(e.get("KillerName"), mine):
                    batch.append(("my_kill", None))
                elif name == "Multikill" and _is(e.get("KillerName"), mine):
                    batch.append(("multikill", None))
                elif name == "Ace" and self.team and e.get("AcingTeam") == self.team:
                    batch.append(("ace", None))
                elif name in ("DragonKill", "BaronKill", "HeraldKill") and self.team and side(e.get("KillerName")) == self.team:
                    kind = {"DragonKill": "dragon", "BaronKill": "baron", "HeraldKill": "herald"}[name]
                    batch.append((kind, str(e.get("DragonType") or "") if kind == "dragon" else None))
                elif name == "GameEnd" and e.get("Result") in ("Win", "Lose"):
                    self.ended = now
                    self.events["win" if e["Result"] == "Win" else "lose"] = now   # held, not a flash: no queue
                    self.pending.clear()
            self.fresh = False
            if any(k == "multikill" for k, _ in batch):
                batch = [b for b in batch if b[0] != "my_kill"]   # the multikill says it all
            self.pending += [(k, now, d) for k, d in batch]
            self._pump(now)

    def _pump(self, now: float) -> None:
        """Start the next waiting flash once the one before is over."""
        self.pending = [p for p in self.pending if now - p[1] <= STALE]
        if not self.pending or now < self.busy_until:
            return
        pick = min(self.pending, key=lambda p: FLASHES.index(p[0]))
        self.pending.remove(pick)
        kind, _, dragon = pick
        self.events[kind] = now
        if kind == "dragon":
            self.dragon = dragon
        self.busy_until = now + max(LENGTH[kind] + PAUSE, MIN_GAP)

    # -- asking --

    def in_match(self, now: float) -> bool:
        """You're in a match and the game is answering."""
        return self.playing and now - self.last < ALIVE and not (self.ended is not None and now - self.ended > END_LINGER)

    def receiving(self, now: float) -> bool:
        return now - self.answered < ALIVE

    def snapshot(self, now: float) -> dict:
        """What the light effect needs, with ages instead of times."""
        with self.lock:
            self._pump(now)
            left = max(0.0, self.respawn_left - (now - self.respawn_at)) if self.dead else 0.0
            return {
                "inMatch": self.in_match(now), "team": self.team, "health": self.health, "dead": self.dead,
                "respawn": min(1.0, 1 - left / self.respawn_total) if self.dead and self.respawn_total > 0 else 0.0,
                "dragon": self.dragon, "ago": {k: now - t for k, t in self.events.items()},
            }


# ---- asking the game ----------------------------------------------------------------------------

class LolPoller:
    """Asks the game about once a second while a match answers, every few seconds otherwise.
    `fetch` is injectable so tests never open a socket to port 2999."""

    def __init__(self, on_change: Callable[[], None] | None = None, fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.state = LolState()
        self.on_change = on_change or (lambda: None)
        self.fetch = fetch or fetch_live
        self.clock = clock
        self.answering = False          # the last ask got a match
        self._was = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self, now: float | None = None) -> bool:
        """One ask. True if a match answered."""
        now = self.clock() if now is None else now
        try:
            data = self.fetch()
        except Exception as e:      # refused, 404, a timeout, half an answer: no match right now
            log.debug("League of Legends isn't answering: %s", e)
            data = None
        self.state.feed(data, now)
        self.answering = self.state.last == now
        if self.state.in_match(now) != self._was:
            self._was = not self._was
            self.on_change()
        return self.answering

    def snapshot(self, now: float) -> dict:
        return self.state.snapshot(now)

    def in_match(self, now: float) -> bool:
        return self.state.in_match(now)

    def receiving(self, now: float) -> bool:
        return self.state.receiving(now)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(stop,), name="lol-poll", daemon=True)
        self._thread.start()
        log.info("Game lighting: listening for League of Legends")

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        self.answering = False

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("League of Legends poll failed")
            stop.wait(FAST if self.answering else SLOW)
