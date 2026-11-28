"""Rocket League lighting. Psyonix's official Stats API: switched on in a small text file of the
game (TAGame/Config/DefaultStatsAPI.ini), the game itself then listens on this PC (127.0.0.1) while
a match runs and sends a stream of JSON messages to whoever connects: the cars, the boost, the
score, goals, saves and demolitions. Nothing reads the game's memory and no other game file is
touched, so it's fine with Easy Anti-Cheat; Glowstage only connects, the way stream overlays do.

This file holds what is specific to Rocket League: finding the install folder (Epic here, Steam in
games.py), editing that one ini file, reading the stream (StreamParser), turning it into what the
"game_rl" effect (effects.py) needs (RocketLeagueState) and the reader thread (RocketLeagueReader).
Games (games.py) owns the reader and starts it only while Rocket League is connected."""

from __future__ import annotations

import codecs
import colorsys
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .lol import MIN_GAP, PAUSE, STALE

log = logging.getLogger(__name__)

STEAM_APP = 252950
INI_PARTS = ("TAGame", "Config", "DefaultStatsAPI.ini")
SECTION = "TAGame.MatchStatsExporter_TA"
DEFAULT_PORT = 49123
SEND_RATE = 30          # messages per second we ask the game for (0 = the Stats API is off)
RETRY = 3.0             # seconds between tries while the game isn't there (a refused connection is instant, so trying costs nothing)
READ_WAIT = 1.0         # seconds a read waits, so the thread notices it should stop
ALIVE = 4.0             # seconds without a message after which the match counts as gone (the game sends 30 a second)
END_LINGER = 10.0       # the match is over this long after the end: the win or loss shows, then your own look comes back
MAX_BUFFER = 1_000_000  # characters of one half-received message we keep: a normal one is a few thousand, more is garbage

# The moments that flash, strongest first, and how long each lasts (seconds). The next flash
# starts only when the one before is over, plus a pause, so there are never more than three
# flashes in any second (the limit for people sensitive to flashing light, like League and Dota).
FLASHES = ("my_goal", "goal_for", "epic_save", "save", "demo")
LENGTH = {"demo": 0.4, "save": 0.45, "epic_save": 0.8, "goal_for": 1.5, "my_goal": 1.9}

BLUE, ORANGE = (30, 110, 255), (255, 120, 20)     # Rocket League's own two sides, when the game doesn't say


# ---- finding the game ---------------------------------------------------------------------------

def epic_manifests() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"


def _is_install(folder: Path) -> bool:
    return (folder / INI_PARTS[0]).is_dir()


def find_in_epic(manifests: Path | None = None) -> Path | None:
    """Rocket League's folder from the Epic Games Launcher's manifests (one .item file per
    installed game, plain JSON), or None."""
    try:
        files = sorted((manifests or epic_manifests()).glob("*.item"))
    except OSError:
        return None
    for f in files:
        try:
            item = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        name, app = str(item.get("DisplayName") or ""), str(item.get("AppName") or "")
        if name.strip().lower() != "rocket league" and app != "Sugar":
            continue
        where = item.get("InstallLocation")
        if isinstance(where, str) and where.strip() and _is_install(Path(where)):
            return Path(where)
    return None


def find_in_steam(libraries: list[Path]) -> Path | None:
    """Rocket League's folder from Steam's appmanifest in each library, or None."""
    for lib in libraries:
        manifest = lib / "steamapps" / f"appmanifest_{STEAM_APP}.acf"
        try:
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        folder = lib / "steamapps" / "common" / (m.group(1) if m else "rocketleague")
        if _is_install(folder):
            return folder
    return None


def ini_path(install: Path) -> Path:
    return install.joinpath(*INI_PARTS)


# ---- the ini file -------------------------------------------------------------------------------
# One small file, edited line by line so everything else in it stays exactly as it was: other
# sections and keys, comments, the line endings and the encoding.

def _read_ini(path: Path) -> tuple[str, str, str]:
    """The file's text, its encoding and its line ending. A missing file is empty text."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", "utf-8", "\r\n"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        enc = "utf-16"
    elif raw.startswith(codecs.BOM_UTF8):
        enc = "utf-8-sig"
    else:
        enc = "utf-8"
    text = raw.decode(enc, errors="surrogateescape") if enc != "utf-16" else raw.decode(enc, errors="replace")
    return text, enc, "\r\n" if "\r\n" in text else ("\n" if "\n" in text else "\r\n")


def _write_ini(path: Path, text: str, enc: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)       # (inside TAGame/Config only)
    data = text.encode(enc, errors="surrogateescape") if enc != "utf-16" else text.encode(enc)
    path.write_bytes(data)


def _section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Where the Stats API section's lines are: (header index, index after its last line)."""
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None:
                return start, i
            if s[1:-1].strip().lower() == SECTION.lower():
                start = i
    return (start, len(lines)) if start is not None else None


def _key_of(line: str) -> str | None:
    s = line.strip()
    if not s or s[0] in ";#[" or "=" not in s:
        return None
    return s.split("=", 1)[0].strip().lower()


def read_stats_api(path: Path) -> dict[str, str | None]:
    """The values now in the file: {"PacketSendRate": "30" | None, "Port": ...} (None: not there)."""
    text, _, _ = _read_ini(path)
    lines = text.splitlines()
    out: dict[str, str | None] = {"PacketSendRate": None, "Port": None}
    bounds = _section_bounds(lines)
    if bounds:
        for ln in lines[bounds[0] + 1:bounds[1]]:
            k = _key_of(ln)
            for name in out:
                if k == name.lower():
                    out[name] = ln.split("=", 1)[1].strip()    # (the last one wins, like the game's own reader)
    return out


def write_stats_api(path: Path, values: dict[str, str | int]) -> None:
    """Sets these keys in the Stats API section: an existing line gets its value, a missing key is
    added at the end of the section, a missing section at the end of the file. Nothing else changes."""
    text, enc, eol = _read_ini(path)
    lines = text.splitlines(keepends=True)
    bounds = _section_bounds(lines)
    if bounds is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += eol
        if lines and lines[-1].strip():
            lines.append(eol)
        lines.append(f"[{SECTION}]{eol}")
        lines += [f"{k}={v}{eol}" for k, v in values.items()]
    else:
        start, end = bounds
        todo = dict(values)
        for i in range(start + 1, end):
            k = _key_of(lines[i])
            for name in list(todo):
                if k == name.lower():
                    ending = lines[i][len(lines[i].rstrip("\r\n")):] or eol
                    lines[i] = f"{name}={todo.pop(name)}{ending}"
        if todo:
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1                                    # (before the blank lines that end the section)
            if at > 0 and not lines[at - 1].endswith(("\n", "\r")):
                lines[at - 1] += eol
            lines[at:at] = [f"{k}={v}{eol}" for k, v in todo.items()]
    _write_ini(path, "".join(lines), enc)


def switch_on(path: Path) -> tuple[dict[str, str | None], int]:
    """Asks the game to send (30 a second) and says where. Returns what the file held before and the
    port to connect to. A port already in the file stays (somebody may rely on it); a send rate that
    is already on stays too, so another tool using the Stats API isn't slowed down."""
    before = read_stats_api(path)
    port = _port(before["Port"]) or DEFAULT_PORT
    rate = _rate(before["PacketSendRate"])
    write_stats_api(path, {"PacketSendRate": before["PacketSendRate"] if rate > 0 else SEND_RATE, "Port": port})
    return before, port


def switch_off(path: Path, original: dict | None) -> None:
    """Puts the send rate back the way it was (0, off, when it wasn't there)."""
    was = (original or {}).get("PacketSendRate")
    write_stats_api(path, {"PacketSendRate": was if was not in (None, "") else 0})


def is_on(path: Path) -> bool:
    return _rate(read_stats_api(path)["PacketSendRate"]) > 0


def _rate(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _port(v) -> int | None:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return None
    return p if 1024 <= p <= 65535 else None


# ---- the stream ---------------------------------------------------------------------------------

class StreamParser:
    """The game sends JSON objects one after the other over plain TCP, with nothing between them. A
    read may end in the middle of one or hold several, so this keeps what hasn't ended yet. Anything
    that isn't a message (noise, a broken object) is skipped; a half message longer than MAX_BUFFER
    is dropped, so a stream that never makes sense can't grow without end."""

    START = re.compile(r'\{\s*"Event"')

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self.dropped = 0                # messages (or pieces) thrown away: for the log and the tests

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        """Everything that has become complete: [(event name, data), ...]. A Data that is itself a
        JSON string is decoded a second time."""
        self._buf += self._decoder.decode(data)
        out: list[tuple[str, object]] = []
        buf, pos = self._buf, 0
        while True:
            at = buf.find("{", pos)
            if at < 0:
                pos = len(buf)
                break
            end = _object_end(buf, at)
            if end is None:                                # not finished: wait for more ...
                nxt = self.START.search(buf, at + 1)
                if nxt:                                    # ... unless another message starts inside it: this one is broken
                    self.dropped += 1
                    pos = nxt.start()
                    continue
                pos = at
                break
            if end - at > MAX_BUFFER:
                self.dropped += 1
                pos = end
                continue
            try:
                obj = json.loads(buf[at:end])
            except ValueError:
                self.dropped += 1
                pos = at + 1                               # not a message: look again from the next brace
                continue
            pos = end
            if isinstance(obj, dict) and isinstance(obj.get("Event"), str):
                out.append((obj["Event"], _data(obj.get("Data"))))
        buf = buf[pos:]
        if len(buf) > MAX_BUFFER:
            self.dropped += 1
            buf = ""
        self._buf = buf
        return out


def _object_end(s: str, at: int) -> int | None:
    """Where the object starting at s[at] == "{" ends (the index after its closing brace), or None if
    it isn't finished. Strings are skipped, so a brace inside one doesn't count."""
    depth, in_str, esc = 0, False, False
    for i in range(at, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _data(v):
    """Data comes as an object or as a JSON string holding one: decode the second kind again."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# ---- who is who, and the colors -----------------------------------------------------------------

def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _team(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n in (0, 1) else None


def vivid(hex_color, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """A team color the way LEDs like it: the game's hue, full strength. Players pick pale, dark or
    almost grey colors for their team that look like nothing on a strip; those fall back to the
    team's own blue or orange."""
    s = str(hex_color or "").strip().lstrip("#")
    if len(s) != 6:
        return fallback
    try:
        r, g, b = (int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return fallback
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    if sat < 0.2 or val < 0.15:
        return fallback
    r, g, b = colorsys.hsv_to_rgb(h, max(sat, 0.85), 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def _same_player(a: dict, b: dict) -> bool:
    """The same player: by the match's number for them (Shortcut) when both have one, else by name."""
    sa, sb = a.get("Shortcut"), b.get("Shortcut")
    if sa is not None and sb is not None:
        return str(sa) == str(sb)
    na, nb = a.get("Name"), b.get("Name")
    return isinstance(na, str) and isinstance(nb, str) and bool(na.strip()) and na.strip().lower() == nb.strip().lower()


# ---- what happens in the match ------------------------------------------------------------------

class RocketLeagueState:
    """The match as the game last described it, plus the moments worth a flash with when they
    started. feed() takes one message of the Stats API; snapshot() is what the light effect reads.

    You are the car the camera follows (the game's "Target"): you, when you play; whoever you watch,
    when you spectate, and the lights then play along with them. During a goal replay the camera
    goes elsewhere, so who you are stays as it was and nothing flashes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = -1e9                # when the last message came (monotonic)
        self._reset()

    def _reset(self) -> None:
        """A new match: forget the old one."""
        self.playing = False
        self.guid = ""
        self.me: dict = {}              # the player we follow: Name, Shortcut, TeamNum
        self.team: int | None = None    # 0 blue, 1 orange
        self.colors: dict[int, tuple[int, int, int]] = {0: BLUE, 1: ORANGE}
        self.boost = 0.0                # 0..1
        self.supersonic = False
        self.demolished = False
        self.overtime = False
        self.replay = False
        self.score = [0, 0]
        self.ended: float | None = None
        self.won: bool | None = None
        self.over = False               # the match was destroyed before it ended (you left)
        self.events: dict[str, float] = {}   # goal_against, overtime, demolished, win, lose and the flashes -> when
        self.pending: list[tuple[str, float]] = []   # flashes waiting for their turn: (name, since)
        self.busy_until = 0.0

    # -- reading --

    def feed(self, event: str, data, now: float) -> None:
        """One message of the Stats API."""
        with self.lock:
            self.last = now
            if event == "MatchCreated":
                self._reset()
            elif event == "UpdateState":
                self._update(_d(data), now)
            elif event == "GoalScored":
                self._goal(_d(data), now)
            elif event == "StatfeedEvent":
                self._statfeed(_d(data), now)
            elif event == "GoalReplayStart":
                self.replay = True
            elif event == "GoalReplayEnd":
                self.replay = False
            elif event == "MatchEnded":
                self._ended(_d(data), now)
            elif event == "MatchDestroyed":
                if self.ended is None:
                    self.over = True        # (after an end the result still shows for a few seconds)
            self._pump(now)

    def _update(self, data: dict, now: float) -> None:
        guid = data.get("MatchGuid")
        if isinstance(guid, str) and guid:
            if self.guid and guid != self.guid:
                self._reset()               # another match on the same connection
            self.guid = guid
        game = _d(data.get("Game"))
        players = [p for p in (data.get("Players") if isinstance(data.get("Players"), list) else []) if isinstance(p, dict)]
        self.playing, self.over = True, False
        replay = bool(game.get("bReplay"))
        self.replay = replay or self.replay and "bReplay" not in game
        for t in (_d(x) for x in (game.get("Teams") if isinstance(game.get("Teams"), list) else [])):
            n = _team(t.get("TeamNum"))
            if n is not None:
                self.colors[n] = vivid(t.get("ColorPrimary"), BLUE if n == 0 else ORANGE)
                self.score[n] = int(_num(t.get("Score"), self.score[n]))
        target = _d(game.get("Target"))
        if game.get("bHasTarget") and target and not self.replay:
            self.me = target
        mine = next((p for p in players if self.me and _same_player(p, self.me)), None)
        if mine is not None:
            team = _team(mine.get("TeamNum"))
            if team is not None:
                self.team = team
            self.boost = max(0.0, min(1.0, _num(mine.get("Boost")) / 100))
            self.supersonic = bool(mine.get("bSupersonic"))
            down = bool(mine.get("bDemolished"))
            if down and not self.demolished:
                self.events["demolished"] = now
            self.demolished = down
        elif self.team is None and self.me and _team(self.me.get("TeamNum")) is not None:
            self.team = _team(self.me.get("TeamNum"))
        over = bool(game.get("bOverTime"))
        if over and not self.overtime:
            self.events["overtime"] = now
        self.overtime = over
        if game.get("bHasWinner") and self.ended is None and _team(game.get("Winner")) is not None:
            self._ended({"WinnerTeamNum": game.get("Winner")}, now)

    def _is_me(self, who) -> bool:
        return bool(self.me) and isinstance(who, dict) and _same_player(who, self.me)

    def _queue(self, kind: str, now: float) -> None:
        if not self.replay and self.ended is None:       # (nothing flashes in a replay or after the end)
            self.pending.append((kind, now))

    def _goal(self, data: dict, now: float) -> None:
        scorer = _d(data.get("Scorer"))
        side = _team(scorer.get("TeamNum"))
        if side is None or self.team is None:
            return
        if side != self.team:
            self.events["goal_against"] = now        # a dim, not a flash: it eases in and out
        elif self._is_me(scorer):
            self._queue("my_goal", now)
        else:
            self._queue("goal_for", now)

    def _statfeed(self, data: dict, now: float) -> None:
        name, main, other = data.get("EventName"), _d(data.get("MainTarget")), _d(data.get("SecondaryTarget"))
        if name == "Demolish":
            if self._is_me(main):
                self._queue("demo", now)
            elif self._is_me(other):
                self.events["demolished"] = now
        elif name in ("Save", "EpicSave") and self._is_me(main):
            self._queue("epic_save" if name == "EpicSave" else "save", now)

    def _ended(self, data: dict, now: float) -> None:
        if self.ended is not None:
            return
        winner = _team(data.get("WinnerTeamNum"))
        self.ended = now
        self.pending.clear()
        if winner is not None and self.team is not None:
            self.won = winner == self.team
            self.events["win" if self.won else "lose"] = now      # held, not a flash: no queue

    def _pump(self, now: float) -> None:
        """Start the next waiting flash once the one before is over (never during a replay)."""
        self.pending = [p for p in self.pending if now - p[1] <= STALE]
        if not self.pending or now < self.busy_until or self.replay:
            return
        pick = min(self.pending, key=lambda p: FLASHES.index(p[0]))
        self.pending.remove(pick)
        self.events[pick[0]] = now
        self.busy_until = now + max(LENGTH[pick[0]] + PAUSE, MIN_GAP)

    # -- asking --

    def in_match(self, now: float) -> bool:
        """You're in a match and the game is sending (the end shows for a few seconds, then the look goes back)."""
        if self.ended is not None:
            return now - self.ended <= END_LINGER
        return self.playing and not self.over and now - self.last < ALIVE

    def alive(self, now: float) -> bool:
        return now - self.last < ALIVE

    def snapshot(self, now: float) -> dict:
        """What the light effect needs, with ages instead of times."""
        with self.lock:
            self._pump(now)
            team = self.team
            return {
                "inMatch": self.in_match(now), "team": team,
                "color": self.colors.get(team) if team is not None else None,
                "other": self.colors.get(1 - team) if team is not None else None,
                "boost": self.boost, "supersonic": self.supersonic and not self.demolished,
                "demolished": self.demolished, "overtime": self.overtime, "replay": self.replay,
                "score": list(self.score), "won": self.won,
                "ago": {k: now - t for k, t in self.events.items()},
            }


# ---- listening to the game ----------------------------------------------------------------------

def _connect(port: int):
    """The real thing: a plain TCP connection to the game on this PC. Refused at once when it isn't there."""
    return socket.create_connection(("127.0.0.1", port), timeout=1.0)


class RocketLeagueReader:
    """Connects to the game while it's running a match and feeds the state; tries again every few
    seconds when it isn't there. `connect` is injectable so tests never open a socket."""

    def __init__(self, on_change: Callable[[], None] | None = None, port: int = DEFAULT_PORT,
                 connect: Callable[[int], object] | None = None, clock: Callable[[], float] = time.monotonic):
        self.state = RocketLeagueState()
        self.on_change = on_change or (lambda: None)
        self.port = port
        self.connect = connect or _connect
        self.clock = clock
        self.connected = False
        self._was = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def session(self, stop: threading.Event | None = None) -> bool:
        """One connection, read until the game closes it (or `stop` is set). False if nobody answered."""
        stop = stop or self._stop
        try:
            sock = self.connect(self.port)
        except OSError as e:                # refused: no match running (or the Stats API is off)
            log.debug("Rocket League isn't answering: %s", e)
            return False
        parser = StreamParser()
        self.connected = True
        try:
            try:
                sock.settimeout(READ_WAIT)
            except (AttributeError, OSError):
                pass
            while not stop.is_set():
                try:
                    data = sock.recv(65536)
                except TimeoutError:        # quiet for a second: look at the clock and the stop switch
                    self._noticed()
                    continue
                if not data:                # the game closed it: the match is over
                    break
                now = self.clock()
                for event, payload in parser.feed(data):
                    self.state.feed(event, payload, now)
                self._noticed()
        except OSError as e:
            log.debug("Rocket League's stream ended: %s", e)
        finally:
            self.connected = False
            try:
                sock.close()
            except OSError:
                pass
            self._noticed()
        return True

    def _noticed(self) -> None:
        """Tell the window when a match starts or ends (the rest it reads every second)."""
        now = self.state.in_match(self.clock())
        if now != self._was:
            self._was = now
            self.on_change()

    def snapshot(self, now: float) -> dict:
        return self.state.snapshot(now)

    def in_match(self, now: float) -> bool:
        return self.state.in_match(now)

    def receiving(self, now: float) -> bool:
        return self.connected and self.state.alive(now)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(stop,), name="rl-read", daemon=True)
        self._thread.start()
        log.info("Game lighting: listening for Rocket League")

    def stop(self) -> None:
        self._stop.set()
        self._thread = None
        self.connected = False

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.session(stop)
            except Exception:
                log.exception("Rocket League reader failed")
            stop.wait(RETRY)
