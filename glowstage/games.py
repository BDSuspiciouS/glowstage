"""Game lighting: Counter-Strike 2 tells Glowstage what happens in the match, through Valve's
official Game State Integration. It's the feature RGB makers and stream overlays use: the game
itself sends its state, nothing reads its memory or touches its files, so it's safe for VAC.

Connecting (Setup → Games) writes one small text file into CS2's cfg folder,
gamestate_integration_glowstage.cfg. It asks the game to send its state to Glowstage on this PC
(127.0.0.1, a port Glowstage picked) with a secret token; Glowstage only listens while CS2 is
connected, and only on this PC. CS2 reads the file when it starts.

What comes in: your team, health, armor, whether you're flashed, smoked or burning, your kills
this round, the round phase, the bomb, and who won the round. The "game_cs2" effect (effects.py)
turns that into light; the automatic looks switch the lights to it while you're in a match."""

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

log = logging.getLogger(__name__)

CFG_NAME = "gamestate_integration_glowstage.cfg"
ALIVE = 15.0            # seconds without a message after which CS2 counts as closed (it sends a heartbeat every 10 s)
BOMB_SECONDS = 40.0     # the C4 timer


# ---- finding CS2 ------------------------------------------------------------------------------

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


# ---- the connection ------------------------------------------------------------------------

class Games:
    def __init__(self, config, on_change):
        self.config = config
        self.on_change = on_change
        self.cs2 = CS2State()
        self._server: ThreadingHTTPServer | None = None
        self._was_in_match = False
        self._found: tuple[float, Path | None] = (-1e9, None)

    def folder(self, fresh: bool = False) -> Path | None:
        """CS2's cfg folder, looked up at most once a minute."""
        at, folder = self._found
        if fresh or time.monotonic() - at > 60:
            folder = find_cs2()
            self._found = (time.monotonic(), folder)
        return folder

    @property
    def settings(self) -> dict:
        return self.config.data.setdefault("games", {})

    # -- lifecycle --

    def start(self) -> None:
        if self.settings.get("cs2"):
            self._listen()

    def stop(self) -> None:
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
        state, games = self.cs2, self

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
                state.feed(data, time.monotonic())
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
            self._write_cfg()
        threading.Thread(target=server.serve_forever, name="cs2-listen", daemon=True).start()
        log.info("Game lighting: listening for CS2 on 127.0.0.1:%d", port)

    def _noticed(self) -> None:
        """Tell the window when a match starts or ends (the rest it reads every second)."""
        now = self.cs2.in_match
        if now != self._was_in_match:
            self._was_in_match = now
            self.on_change()

    # -- connecting --

    def _write_cfg(self) -> Path:
        folder = self.folder(fresh=True)
        if folder is None:
            raise ValueError("Counter-Strike 2 isn't installed on this PC (or Steam can't say where)")
        path = folder / CFG_NAME
        path.write_text(cfg_text(self.settings["port"], self.settings["token"]), encoding="utf-8")
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
            except OSError as e:
                log.warning("Couldn't remove %s: %s", CFG_NAME, e)
        self.stop()
        self.on_change()

    def describe(self) -> dict:
        now = time.monotonic()
        folder = self.folder()
        connected = bool(self.settings.get("cs2"))
        snap = self.cs2.snapshot(now)
        return {"cs2": {
            "found": folder is not None, "connected": connected and folder is not None,
            "fileThere": bool(folder and (folder / CFG_NAME).is_file()),
            "receiving": connected and self.cs2.alive(now), "inMatch": connected and snap["inMatch"],
            "team": snap["team"], "health": snap["health"],
        }}

    def active(self, now: float) -> bool:
        """You're in a CS2 match and the game is sending."""
        return bool(self.settings.get("cs2")) and self.cs2.alive(now) and self.cs2.in_match
