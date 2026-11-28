"""Game lighting: Counter-Strike 2 tells Glowstage what happens in the match, through Valve's
official Game State Integration. It's the feature RGB makers and stream overlays use: the game
itself sends its state, nothing reads its memory or touches its files, so it's safe for VAC.

Connecting (Setup → Games) writes one small text file into CS2's cfg folder,
gamestate_integration_glowstage.cfg. It asks the game to send its state to Glowstage on this PC
(127.0.0.1, a port Glowstage picked) with a secret token; Glowstage only listens while CS2 is
connected, and only on this PC. CS2 reads the file when it starts.

What comes in: your team, health, armor, whether you're flashed, smoked or burning, your kills
this round, the round phase, the bomb, and who won the round. The "game_cs2" effect (effects.py)
turns that into light; the automatic looks switch the lights to it while you're in a match.

Dota 2 works the same way (same listener, same token; the messages say which game they come from):
its file goes in dota 2 beta/game/dota/cfg/gamestate_integration, and Dota only sends once you add
the launch option -gamestateintegration in Steam (Glowstage never changes Steam's settings). The
"game_dota" effect plays along: your side's color, the day and night of the map, your health.

League of Legends needs no file: Riot's own Live Client Data API answers on this PC while a match
runs, and lol.py asks it once a switch in Setup → Games is on.

Rocket League has its own Stats API (rocket_league.py): connecting switches it on in one line of the
game's DefaultStatsAPI.ini (disconnecting puts the line back), and the game then sends its state to
whoever connects to it on this PC, which is what the reader there does while a match runs."""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import rocket_league
from .lol import ABSORB, END_LINGER, MIN_GAP, PAUSE, STALE, LolPoller
from .rocket_league import RocketLeagueReader

log = logging.getLogger(__name__)

CFG_NAME = "gamestate_integration_glowstage.cfg"
ALIVE = 15.0            # seconds without a message after which CS2 counts as closed (it sends a heartbeat every 10 s)
BOMB_SECONDS = 40.0     # the C4 timer
CS2_APP, DOTA_APP = 730, 570   # Steam's app ids: what a message's provider says (and how the listener tells the games apart)

# Dota's flashes, strongest first, and how long each lasts (seconds). Like League's (lol.py): the next
# one starts only when the one before is over, plus a pause, so never more than three in any second.
DOTA_FLASHES = ("roshan", "aegis", "my_kill")
DOTA_LENGTH = {"my_kill": 0.35, "roshan": 1.3, "aegis": 1.0}
DIP_EVERY = 1.0         # a stun or a hex dips the room at most once a second (a chain of stuns is one long dip, not a flicker)


# ---- finding CS2 and Dota 2 ------------------------------------------------------------------------------

def steam_libraries() -> list[Path]:
    """Every Steam library folder on this PC."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return []
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libs
    for raw in re.findall(r'"path"\s+"([^"]+)"', text):
        p = Path(raw.replace("\\\\", "\\"))
        if p not in libs:
            libs.append(p)
    return libs


def find_dota2() -> Path | None:
    """Dota 2's gamestate_integration folder (…/dota 2 beta/game/dota/cfg/gamestate_integration), or
    None. The folder itself may not exist yet (Dota doesn't ship it): connecting creates it."""
    for lib in steam_libraries():
        manifest = lib / "steamapps" / f"appmanifest_{DOTA_APP}.acf"
        try:
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        game = lib / "steamapps" / "common" / (m.group(1) if m else "dota 2 beta") / "game" / "dota"
        if game.is_dir():
            return game / "cfg" / "gamestate_integration"
    return None


def find_cs2() -> Path | None:
    """CS2's cfg folder (…/Counter-Strike Global Offensive/game/csgo/cfg), or None."""
    for lib in steam_libraries():
        manifest = lib / "steamapps" / "appmanifest_730.acf"
        try:
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        cfg = lib / "steamapps" / "common" / (m.group(1) if m else "Counter-Strike Global Offensive") / "game" / "csgo" / "cfg"
        if cfg.is_dir():
            return cfg
    return None


def find_rocket_league() -> Path | None:
    """Rocket League's install folder (the one with TAGame in it), from Steam or the Epic Games
    Launcher, or None."""
    return rocket_league.find_in_steam(steam_libraries()) or rocket_league.find_in_epic()


def cfg_text(port: int, token: str) -> str:
    return f'''"Glowstage"
{{
\t"uri"\t\t"http://127.0.0.1:{port}/cs2"
\t"timeout"\t"1.1"
\t"buffer"\t"0.0"
\t"throttle"\t"0.0"
\t"heartbeat"\t"10.0"
\t"auth"
\t{{
\t\t"token"\t"{token}"
\t}}
\t"data"
\t{{
\t\t"provider"\t\t"1"
\t\t"map"\t\t\t"1"
\t\t"round"\t\t\t"1"
\t\t"player_id"\t\t"1"
\t\t"player_state"\t"1"
\t}}
}}
'''


def dota_cfg_text(port: int, token: str) -> str:
    """Dota's file: the same format as CS2's. Only what the light effect reads is asked for (the
    abilities and items blocks would only make every message bigger)."""
    return f'''"Glowstage"
{{
\t"uri"\t\t"http://127.0.0.1:{port}/dota2"
\t"timeout"\t"5.0"
\t"buffer"\t"0.1"
\t"throttle"\t"0.1"
\t"heartbeat"\t"10.0"
\t"auth"
\t{{
\t\t"token"\t"{token}"
\t}}
\t"data"
\t{{
\t\t"provider"\t"1"
\t\t"map"\t\t"1"
\t\t"player"\t"1"
\t\t"hero"\t\t"1"
\t\t"events"\t"1"
\t}}
}}
'''


# ---- what happens in the match ------------------------------------------------------------------

class CS2State:
    """The match as CS2 last described it, plus the moments worth a flash (kills, the bomb, the
    end of a round) with when they happened."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = -1e9            # when the last message came (monotonic)
        self.in_match = False
        self.me = True              # the player CS2 describes is you (not someone you spectate)
        self.team: str | None = None
        self.health = 100
        self.armor = 0
        self.flashed = 0            # 0..255, as CS2 sends them
        self.smoked = 0
        self.burning = 0
        self.kills = 0
        self.headshots = 0
        self.phase: str | None = None      # round phase: freezetime, live, over
        self.bomb: str | None = None       # planted, exploded, defused
        self.bomb_at: float | None = None
        self.win_team: str | None = None
        self.events: dict[str, float] = {}   # kill, headshot, exploded, defused, round_over -> when

    def feed(self, data: dict, now: float) -> None:
        with self.lock:
            self.last = now
            provider = data.get("provider") or {}
            player = data.get("player") or {}
            st = player.get("state") or {}
            rnd = data.get("round") or {}
            self.in_match = bool(data.get("map")) and player.get("activity") != "menu"
            me = not provider.get("steamid") or player.get("steamid") == provider.get("steamid")
            if me and st:
                kills, hs = int(st.get("round_kills", 0) or 0), int(st.get("round_killhs", 0) or 0)
                if self.me and kills > self.kills:
                    self.events["headshot" if hs > self.headshots else "kill"] = now
                self.kills, self.headshots = kills, hs
            self.me = me
            if player.get("team") in ("CT", "T") and me:
                self.team = player["team"]
            if st:
                self.health = int(st.get("health", self.health) or 0)
                self.armor = int(st.get("armor", self.armor) or 0)
                self.flashed = int(st.get("flashed", 0) or 0)
                self.smoked = int(st.get("smoked", 0) or 0)
                self.burning = int(st.get("burning", 0) or 0)
            bomb = rnd.get("bomb")
            if bomb != self.bomb:
                if bomb == "planted":
                    self.bomb_at = now
                elif bomb in ("exploded", "defused"):
                    self.events[bomb] = now
                self.bomb = bomb
            phase = rnd.get("phase")
            if phase != self.phase:
                if phase == "over":
                    self.events["round_over"] = now
                    self.win_team = rnd.get("win_team")
                if phase == "freezetime":   # a new round
                    self.kills = self.headshots = 0
                self.phase = phase

    def alive(self, now: float) -> bool:
        return now - self.last < ALIVE

    def snapshot(self, now: float) -> dict:
        """What the light effect needs, with ages instead of times."""
        with self.lock:
            ago = {k: now - t for k, t in self.events.items()}
            return {
                "inMatch": self.in_match and self.alive(now), "me": self.me, "team": self.team,
                "health": self.health, "armor": self.armor,
                "flashed": self.flashed / 255, "smoked": self.smoked / 255, "burning": self.burning / 255,
                "phase": self.phase, "winTeam": self.win_team,
                "bomb": (now - self.bomb_at) if self.bomb == "planted" and self.bomb_at is not None else None,
                "ago": ago,
            }


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _count(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _side(v) -> str | None:
    v = str(v or "").lower()
    return v if v in ("radiant", "dire") else None


class DotaState:
    """The match as Dota 2 last described it (Valve's game state integration), plus the moments
    worth a flash (your kills, Roshan, the aegis) with when they started. Any field can be missing:
    spectating, the hero select and the menu all send less."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = -1e9                    # when the last message came (monotonic)
        self._reset()

    def _reset(self) -> None:
        """A new match: forget the old one."""
        self.playing = False                # you're in a match and the game describes you (not someone you watch)
        self.team: str | None = None        # "radiant" or "dire"
        self.state: str | None = None       # map.game_state, as Dota writes it
        self.health = 1.0                   # 0..1
        self.mana = 1.0
        self.alive = True
        self.respawn_total = 0.0            # seconds the death started with
        self.respawn_left = 0.0             # ... and what was left when it was last read
        self.respawn_at = 0.0
        self.daytime = True
        self.kills: int | None = None
        self.deaths: int | None = None
        self.disabled = False               # stunned or hexed
        self.my_id = None                   # your player id, to tell your aegis from someone else's
        self.win_team: str | None = None
        self.ended: float | None = None
        self.fresh = True                   # first look at this match: older events are history
        self.seen: set = set()              # events we have dealt with
        self.events: dict[str, float] = {}  # my_kill, death, roshan, aegis, dip, win, lose -> when
        self.pending: list[tuple[str, float]] = []   # flashes waiting for their turn: (name, since)
        self.busy_until = 0.0

    def feed(self, data: dict, now: float) -> None:
        with self.lock:
            if now - self.last > 30:
                self.kills = self.deaths = None     # we had lost it for a while: whatever happened meanwhile isn't news
                self.fresh = True
            self.last = now
            data = _d(data)
            provider, map_, player, hero = _d(data.get("provider")), _d(data.get("map")), _d(data.get("player")), _d(data.get("hero"))
            state = map_.get("game_state") if isinstance(map_.get("game_state"), str) else None
            kills, deaths = _count(player.get("kills")), _count(player.get("deaths"))
            early = state is not None and any(k in state for k in ("HERO_SELECTION", "STRATEGY_TIME", "PRE_GAME", "WAIT_FOR_PLAYERS"))
            if (early and self.state is not None and any(k in self.state for k in ("GAME_IN_PROGRESS", "POST_GAME"))) \
                    or (kills is not None and self.kills is not None and kills < self.kills):
                self._reset()                       # the game went back to the start, or kills went down: a new match
            side = _side(player.get("team_name"))
            # Spectating: no player of your own (Dota then sends one per team), or the steam id isn't yours.
            me = bool(map_) and bool(player) and side is not None and player.get("activity") != "menu" \
                and (not provider.get("steamid") or not player.get("steamid") or str(player["steamid"]) == str(provider["steamid"]))
            self.playing = me
            if state:
                self.state = state
            if not me:
                self._pump(now)
                return
            self.team = side
            mine = player.get("player_id", player.get("player_slot"))
            if mine is not None:
                self.my_id = mine
            if "daytime" in map_ or "nightstalker_night" in map_:
                self.daytime = bool(map_.get("daytime", True)) and not map_.get("nightstalker_night")
            if hero:
                if "health_percent" in hero:
                    self.health = max(0.0, min(1.0, _num(hero.get("health_percent"), 100.0) / 100))
                if "mana_percent" in hero:
                    self.mana = max(0.0, min(1.0, _num(hero.get("mana_percent"), 100.0) / 100))
                left = max(0.0, _num(hero.get("respawn_seconds")))
                alive = hero.get("alive")
                dead = alive is False or (alive is None and left > 0)
                if dead:
                    if self.alive:                  # just died: the countdown starts here
                        self.respawn_total = 0.0
                        self.events["death"] = now
                    self.respawn_total = max(self.respawn_total, left)
                    self.respawn_left, self.respawn_at = left, now
                self.alive = not dead
                off = bool(hero.get("stunned") or hero.get("hexed")) and not dead
                if off and not self.disabled and now - self.events.get("dip", -1e9) >= DIP_EVERY:
                    self.events["dip"] = now        # a dip, not a flash: it eases in and out
                self.disabled = off
            if kills is not None:
                if self.kills is not None and kills > self.kills and not self.fresh:
                    self.pending.append(("my_kill", now))
                self.kills = kills
            if deaths is not None:
                if self.deaths is not None and deaths > self.deaths and self.alive:
                    self.events["death"] = now      # (no hero block in this message: the count says it too)
                self.deaths = deaths
            clock = _num(map_.get("game_time"), _num(map_.get("clock_time")))
            for e in (data.get("events") if isinstance(data.get("events"), list) else []):
                if not isinstance(e, dict):
                    continue
                key = json.dumps(e, sort_keys=True, default=str)
                if key in self.seen:
                    continue
                self.seen.add(key)
                if self.fresh and _num(e.get("game_time")) < clock - ABSORB:
                    continue
                kind = e.get("event_type")
                if kind == "roshan_killed" and side and _side(e.get("killed_by_team")) == side:
                    self.pending.append(("roshan", now))
                elif kind == "aegis_picked_up" and e.get("player_id") is not None and e.get("player_id") == self.my_id:
                    self.pending.append(("aegis", now))
            if state and "POST_GAME" in state and self.ended is None and _side(map_.get("win_team")):
                self.win_team = _side(map_.get("win_team"))
                self.ended = now
                if not self.fresh:                  # (joining at the end screen isn't news)
                    self.events["win" if self.win_team == side else "lose"] = now   # held, not a flash: no queue
                    self.pending.clear()
            self.fresh = False
            self._pump(now)

    def _pump(self, now: float) -> None:
        """Start the next waiting flash once the one before is over."""
        self.pending = [p for p in self.pending if now - p[1] <= STALE]
        if not self.pending or now < self.busy_until:
            return
        pick = min(self.pending, key=lambda p: DOTA_FLASHES.index(p[0]))
        self.pending.remove(pick)
        self.events[pick[0]] = now
        self.busy_until = now + max(DOTA_LENGTH[pick[0]] + PAUSE, MIN_GAP)

    def alive_now(self, now: float) -> bool:
        return now - self.last < ALIVE

    def in_match(self, now: float) -> bool:
        """You're in a match and Dota is sending (the end shows for a few seconds, then the look goes back)."""
        return self.playing and self.alive_now(now) and not (self.ended is not None and now - self.ended > END_LINGER)

    def snapshot(self, now: float) -> dict:
        """What the light effect needs, with ages instead of times."""
        with self.lock:
            self._pump(now)
            left = max(0.0, self.respawn_left - (now - self.respawn_at)) if not self.alive else 0.0
            return {
                "inMatch": self.in_match(now), "team": self.team, "health": self.health, "mana": self.mana,
                "alive": self.alive, "daytime": self.daytime, "state": self.state, "winTeam": self.win_team,
                "respawn": min(1.0, 1 - left / self.respawn_total) if not self.alive and self.respawn_total > 0 else 0.0,
                "ago": {k: now - t for k, t in self.events.items()},
            }


# ---- the connection ------------------------------------------------------------------------

class Games:
    def __init__(self, config, on_change):
        self.config = config
        self.on_change = on_change
        self.cs2 = CS2State()
        self.dota = DotaState()
        self.lol = LolPoller(on_change=self._lol_noticed)
        self._server: ThreadingHTTPServer | None = None
        self._was_in_match = False
        self._dota_was_in_match = False
        self._found: tuple[float, Path | None] = (-1e9, None)
        self._file_there = False
        self._looking = threading.Lock()
        self._found_dota: tuple[float, Path | None] = (-1e9, None)
        self._dota_file_there = False
        self._looking_dota = threading.Lock()
        self.rl = RocketLeagueReader(on_change=self._rl_noticed)
        self._found_rl: tuple[float, Path | None] = (-1e9, None)
        self._rl_file_there = False        # the game's ini asks for the Stats API now
        self._looking_rl = threading.Lock()

    def folder(self, fresh: bool = False) -> Path | None:
        """CS2's cfg folder. Fresh: looked up now (you're connecting). Otherwise the last answer,
        looked up again in the background once a minute: a Steam library on a hard disk that has
        spun down takes seconds to answer, and the window mustn't wait for that."""
        if fresh:
            self._look()
        elif time.monotonic() - self._found[0] > 60 and not self._looking.locked():
            threading.Thread(target=self._look, name="find-cs2", daemon=True).start()
        return self._found[1]

    def _look(self) -> None:
        with self._looking:
            folder = find_cs2()
            self._file_there = bool(folder and (folder / CFG_NAME).is_file())
            self._found = (time.monotonic(), folder)

    def dota_folder(self, fresh: bool = False) -> Path | None:
        """Dota 2's gamestate_integration folder, the same way: never makes the window wait."""
        if fresh:
            self._look_dota()
        elif time.monotonic() - self._found_dota[0] > 60 and not self._looking_dota.locked():
            threading.Thread(target=self._look_dota, name="find-dota2", daemon=True).start()
        return self._found_dota[1]

    def _look_dota(self) -> None:
        with self._looking_dota:
            folder = find_dota2()
            self._dota_file_there = bool(folder and (folder / CFG_NAME).is_file())
            self._found_dota = (time.monotonic(), folder)

    def rl_folder(self, fresh: bool = False) -> Path | None:
        """Rocket League's install folder, the same way: never makes the window wait."""
        if fresh:
            self._look_rl()
        elif time.monotonic() - self._found_rl[0] > 60 and not self._looking_rl.locked():
            threading.Thread(target=self._look_rl, name="find-rl", daemon=True).start()
        return self._found_rl[1]

    def _look_rl(self) -> None:
        with self._looking_rl:
            folder = find_rocket_league()
            self._rl_file_there = bool(folder and rocket_league.is_on(rocket_league.ini_path(folder)))
            self._found_rl = (time.monotonic(), folder)

    @property
    def settings(self) -> dict:
        return self.config.data.setdefault("games", {})

    # -- lifecycle --

    def start(self) -> None:
        threading.Thread(target=self._look, name="find-cs2", daemon=True).start()
        threading.Thread(target=self._look_dota, name="find-dota2", daemon=True).start()
        threading.Thread(target=self._look_rl, name="find-rl", daemon=True).start()
        if self.settings.get("cs2") or self.settings.get("dota"):
            self._listen()
        if self.settings.get("lol"):
            self.lol.start()
        if self.settings.get("rl"):
            self._start_rl()

    def stop(self) -> None:
        self.lol.stop()
        self.rl.stop()
        self._unlisten()

    def _unlisten(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _listen(self) -> None:
        """Listen on 127.0.0.1, on the port the cfg file names if it's free (else a new one,
        and the file gets the new port: CS2 picks it up when it starts next)."""
        if self._server:
            return
        token = self.settings.get("token") or secrets.token_hex(16)
        games = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(size, 256_000))
                try:
                    data = json.loads(body)
                except ValueError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if (data.get("auth") or {}).get("token") != token:
                    self.send_response(403)
                    self.end_headers()
                    return
                games._route(data)
                self.send_response(200)
                self.end_headers()
                games._noticed()

            def log_message(self, *args):   # (no line per message)
                pass

        server = None
        for port in (int(self.settings.get("port") or 0), 0):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        if server is None:
            log.warning("Game lighting: no port to listen on")
            return
        server.daemon_threads = True
        self._server = server
        port = server.server_address[1]
        if port != self.settings.get("port") or token != self.settings.get("token"):
            self.settings.update(port=port, token=token)
            self.config.save_soon()
            if self.settings.get("cs2"):
                self._write_cfg()
            if self.settings.get("dota"):
                try:
                    self._write_cfg("dota")
                except (ValueError, OSError) as e:
                    log.warning("Couldn't update the Dota 2 file: %s", e)
        threading.Thread(target=server.serve_forever, name="game-listen", daemon=True).start()
        log.info("Game lighting: listening for CS2 and Dota 2 on 127.0.0.1:%d", port)

    def _route(self, data: dict) -> None:
        """One message from a game: Dota 2 says so in its provider (appid 570); anything else is CS2's."""
        if _num(_d(data.get("provider")).get("appid")) == DOTA_APP:
            self.dota.feed(data, time.monotonic())
        else:
            self.cs2.feed(data, time.monotonic())
        self._noticed()

    def _noticed(self) -> None:
        """Tell the window when a match starts or ends (the rest it reads every second)."""
        now = self.cs2.in_match
        if now != self._was_in_match:
            self._was_in_match = now
            self.on_change()
        now = self.dota.in_match(time.monotonic())
        if now != self._dota_was_in_match:
            self._dota_was_in_match = now
            self.on_change()

    # -- connecting --

    def _write_cfg(self, game: str = "cs2") -> Path:
        if game == "dota":
            folder = self.dota_folder(fresh=True)
            if folder is None:
                raise ValueError("Dota 2 isn't installed on this PC (or Steam can't say where)")
            folder.mkdir(parents=True, exist_ok=True)      # Dota doesn't ship this folder
            path = folder / CFG_NAME
            path.write_text(dota_cfg_text(self.settings["port"], self.settings["token"]), encoding="utf-8")
            self._dota_file_there = True
            return path
        folder = self.folder(fresh=True)
        if folder is None:
            raise ValueError("Counter-Strike 2 isn't installed on this PC (or Steam can't say where)")
        path = folder / CFG_NAME
        path.write_text(cfg_text(self.settings["port"], self.settings["token"]), encoding="utf-8")
        self._file_there = True
        return path

    def connect(self) -> None:
        if self.folder(fresh=True) is None:
            raise ValueError("Counter-Strike 2 isn't installed on this PC (or Steam can't say where)")
        self.settings["cs2"] = True
        self.settings.setdefault("token", secrets.token_hex(16))
        self.config.save_soon()
        self._listen()
        log.info("Game lighting: connected to CS2 (%s)", self._write_cfg())
        self.on_change()

    def disconnect(self) -> None:
        self.settings["cs2"] = False
        self.config.save_soon()
        folder = self.folder(fresh=True)
        if folder is not None:
            try:
                (folder / CFG_NAME).unlink(missing_ok=True)
                self._file_there = False
            except OSError as e:
                log.warning("Couldn't remove %s: %s", CFG_NAME, e)
        if not self.settings.get("dota"):
            self._unlisten()        # (Dota may still be using the listener)
        self.on_change()

    def connect_dota(self) -> None:
        if self.dota_folder(fresh=True) is None:
            raise ValueError("Dota 2 isn't installed on this PC (or Steam can't say where)")
        self.settings["dota"] = True
        self.settings.setdefault("token", secrets.token_hex(16))
        self.config.save_soon()
        self._listen()
        log.info("Game lighting: connected to Dota 2 (%s)", self._write_cfg("dota"))
        self.on_change()

    def disconnect_dota(self) -> None:
        self.settings["dota"] = False
        self.config.save_soon()
        folder = self.dota_folder(fresh=True)
        if folder is not None:
            try:
                (folder / CFG_NAME).unlink(missing_ok=True)
                self._dota_file_there = False
            except OSError as e:
                log.warning("Couldn't remove %s: %s", CFG_NAME, e)
        if not self.settings.get("cs2"):
            self._unlisten()
        self.on_change()

    # -- League of Legends --

    def set_lol(self, on: bool) -> None:
        """The switch in Setup → Games: ask the game's own API only while it's on."""
        self.settings["lol"] = bool(on)
        self.config.save_soon()
        if on:
            self.lol.start()
        else:
            self.lol.stop()
        self.on_change()

    # -- Rocket League --

    def _start_rl(self) -> None:
        self.rl.port = int(self.settings.get("rl_port") or rocket_league.DEFAULT_PORT)
        self.rl.start()

    def connect_rl(self) -> None:
        """Switches the Stats API on in the game's ini (the one file we touch) and starts listening
        for it. The game reads the file when it starts, so a running Rocket League has to restart."""
        folder = self.rl_folder(fresh=True)
        if folder is None:
            raise ValueError("Rocket League isn't installed on this PC (or Steam and Epic can't say where)")
        path = rocket_league.ini_path(folder)
        try:
            before, port = rocket_league.switch_on(path)
        except PermissionError:
            raise ValueError("Glowstage isn't allowed to change Rocket League's settings file. "
                             "Close Glowstage and start it as administrator once, or give your account write access to " + str(path)) from None
        if self.settings.get("rl_original") is None:     # (connecting twice must not keep our own 30 as "the original")
            self.settings["rl_original"] = before
        self.settings.update(rl=True, rl_port=port)
        self.config.save_soon()
        self._rl_file_there = True
        self._start_rl()
        log.info("Game lighting: connected to Rocket League (%s)", path)
        self.on_change()

    def disconnect_rl(self) -> None:
        """Puts the send rate back as it was (off, unless something else had it on) and stops listening."""
        folder = self.rl_folder(fresh=True)
        if folder is not None:
            path = rocket_league.ini_path(folder)
            try:
                rocket_league.switch_off(path, self.settings.get("rl_original"))
                self._rl_file_there = rocket_league.is_on(path)
            except OSError as e:
                log.warning("Couldn't put Rocket League's settings file back: %s", e)
                raise ValueError("Glowstage couldn't change Rocket League's settings file back. Try again, or set "
                                 "PacketSendRate to 0 in " + str(path)) from None
        self.settings.update(rl=False, rl_original=None)
        self.config.save_soon()
        self.rl.stop()
        self.on_change()

    def _rl_noticed(self) -> None:
        """A match starts or ends: tell the window (the rest it reads every second)."""
        self.on_change()

    def rl_active(self, now: float) -> bool:
        """You're in a Rocket League match and the game is sending."""
        return bool(self.settings.get("rl")) and self.rl.in_match(now)

    def _lol_noticed(self) -> None:
        """A match starts or ends: tell the window (the rest it reads every second)."""
        self.on_change()

    def lol_active(self, now: float) -> bool:
        """You're in a League of Legends match and the game is answering."""
        return bool(self.settings.get("lol")) and self.lol.in_match(now)

    def describe(self) -> dict:
        now = time.monotonic()
        folder = self.folder()
        connected = bool(self.settings.get("cs2"))
        snap = self.cs2.snapshot(now)
        on = bool(self.settings.get("lol"))
        lol = self.lol.snapshot(now)
        dota_folder = self.dota_folder()
        dota_on = bool(self.settings.get("dota"))
        dota = self.dota.snapshot(now)
        rl_folder = self.rl_folder()
        rl_on = bool(self.settings.get("rl"))
        rl = self.rl.snapshot(now)
        return {"rl": {
            "found": rl_folder is not None, "connected": rl_on and rl_folder is not None,
            "fileThere": bool(rl_folder) and self._rl_file_there,
            "receiving": rl_on and self.rl.receiving(now), "inMatch": rl_on and rl["inMatch"],
            "team": {0: "blue", 1: "orange"}.get(rl["team"]), "boost": round(rl["boost"] * 100),
        }, "dota": {
            "found": dota_folder is not None, "connected": dota_on and dota_folder is not None,
            "fileThere": bool(dota_folder) and self._dota_file_there,
            "receiving": dota_on and self.dota.alive_now(now), "inMatch": dota_on and dota["inMatch"],
            "team": dota["team"], "health": round(dota["health"] * 100),
        }, "lol": {
            "on": on, "receiving": on and self.lol.receiving(now), "inMatch": on and lol["inMatch"],
            "team": lol["team"], "health": round(lol["health"] * 100),
        }, "cs2": {
            "found": folder is not None, "connected": connected and folder is not None,
            "fileThere": bool(folder) and self._file_there,
            "receiving": connected and self.cs2.alive(now), "inMatch": connected and snap["inMatch"],
            "team": snap["team"], "health": snap["health"],
        }}

    def active(self, now: float) -> bool:
        """You're in a CS2 match and the game is sending."""
        return bool(self.settings.get("cs2")) and self.cs2.alive(now) and self.cs2.in_match

    def dota_active(self, now: float) -> bool:
        """You're in a Dota 2 match and the game is sending."""
        return bool(self.settings.get("dota")) and self.dota.in_match(now)
