"""Racing game lighting: rev lights, start lights and flags for Forza, the F1 games, iRacing and
Assetto Corsa (with Competizione). Forza and F1 have an official setting that sends the car's telemetry
over UDP to an address you type in (the game's own "Data out" or "Telemetry" settings). iRacing and
Assetto Corsa publish theirs in Windows shared memory, which is their documented way for dashboards
and wheels: simracing.py reads that, read-only. Glowstage listens on this PC (127.0.0.1) and looks for
those blocks only while the switch in Setup -> Games is on; nothing reads the game's own memory and no
game file is touched, so anti-cheat has nothing to say about it.

This file holds what is specific to the racing games: the packet formats (pure functions, below),
what a race looks like (RacingState, which the "game_racing" effect in effects.py reads) and the
listener threads (RacingListener, which also owns the shared memory reader of simracing.py). Games
(games.py) owns the listener.

---- Forza (Motorsport 7, Motorsport 2023, Horizon 4, Horizon 5): "Data out" -------------------
One UDP packet per frame (about 60 a second), little-endian, no header. What the game sends is told
by the packet's LENGTH:

    232  "Sled" (Motorsport 7): only the first part below
    311  "Dash" (Motorsport 7): the sled part, then the dash part
    324  Horizon 4 and 5: the sled part, 12 bytes nobody has a name for, then the dash part (so
         every dash offset is 12 higher), and one byte at the very end
    331  Motorsport (2023): the dash, then TireWear x4 (f32) and TrackOrdinal (s32) at the end

    sled part  offset  type
        0   s32 IsRaceOn          (1 while you drive, 0 in menus and while paused)
        4   u32 TimestampMS
        8   f32 EngineMaxRpm
       12   f32 EngineIdleRpm
       16   f32 CurrentEngineRpm
       20   f32 AccelerationX, Y, Z       32  f32 VelocityX, Y, Z       44  f32 AngularVelocityX, Y, Z
       56   f32 Yaw, Pitch, Roll          68  f32 NormalizedSuspensionTravel FL FR RL RR
       84   f32 TireSlipRatio x4         100  f32 WheelRotationSpeed x4  116  s32 WheelOnRumbleStrip x4
      132   f32 WheelInPuddleDepth x4    148  f32 SurfaceRumble x4       164  f32 TireSlipAngle x4
      180   f32 TireCombinedSlip x4      196  f32 SuspensionTravelMeters x4
      212   s32 CarOrdinal, CarClass, CarPerformanceIndex, DrivetrainType, NumCylinders    (232 bytes)
    dash part (offsets for 311 and 331; add 12 for 324)
      232   f32 PositionX, Y, Z
      244   f32 Speed (meters per second)
      248   f32 Power      252  f32 Torque      256  f32 TireTemp FL FR RL RR
      272   f32 Boost      276  f32 Fuel        280  f32 DistanceTraveled
      284   f32 BestLap    288  f32 LastLap     292  f32 CurrentLap     296  f32 CurrentRaceTime
      300   u16 LapNumber
      302   u8 RacePosition, Accel, Brake, Clutch, HandBrake, Gear      (302 .. 307)
      308   s8 Steer, NormalizedDrivingLine, NormalizedAIBrakeDifference        (ends at 311)

Glowstage reads IsRaceOn, the three rpm values and, when there is a dash part, Speed and Gear. The
sled offsets, the 232/311 lengths and the dash offsets are the ones of Forza's published Data Out
documentation (sure). The 324 layout (12 bytes between the parts) and the 331 one (extra fields only
at the end) are what the community parsers use for Horizon and Motorsport 2023 (fairly sure: they
only matter for Speed and Gear, which nothing important hangs on). Gear: 0 is reverse, 1 and up are
the gears (a few cars report a higher number in neutral, which is shown as no gear).

---- F1 (EA Sports F1 23, F1 24, F1 25): "Telemetry" -------------------------------------------
UDP format 2023, 2024 or 2025 (the "UDP Format" setting). Every packet starts with a 29-byte header,
little-endian, packed:

    u16 packetFormat (2023 | 2024 | 2025)    u8 gameYear    u8 gameMajorVersion    u8 gameMinorVersion
    u8 packetVersion    u8 packetId    u64 sessionUID    f32 sessionTime    u32 frameIdentifier
    u32 overallFrameIdentifier    u8 playerCarIndex    u8 secondaryPlayerCarIndex          (29 bytes)

Packets used (all others are ignored):
    3 Event: header, then a 4-letter code and 12 bytes of details. SSTA session started, SEND session
        ended, FTLP fastest lap (u8 vehicleIdx, f32 lapTime), DRSE / DRSD DRS enabled / disabled,
        CHQF chequered flag, LGOT lights out, STLG start lights (u8 numLights, 1 to 5), SCAR safety
        car (u8 safetyCarType: 1 full, 2 virtual; u8 eventType: 0 deployed, 1 returning, 2 returned,
        3 resume race), RCWN race winner (u8 vehicleIdx).
    6 Car telemetry: header, 22 cars of 60 bytes, then 3 bytes. One car:
        u16 speed (km/h)  f32 throttle  f32 steer  f32 brake  u8 clutch  s8 gear  u16 engineRPM
        u8 drs (0 or 1)  u8 revLightsPercent  u16 revLightsBitValue  u16 brakesTemperature[4]
        u8 tyresSurfaceTemperature[4]  u8 tyresInnerTemperature[4]  u16 engineTemperature
        f32 tyresPressure[4]  u8 surfaceType[4]                                         (60 bytes)
        Our car is the one at header.playerCarIndex.
    7 Car status: header, 22 cars of 55 bytes, in which the byte at offset 28 is s8 vehicleFiaFlags
        (-1 unknown, 0 none, 1 green, 2 blue, 3 yellow). Only read when the packet is exactly
        29 + 22 * 55 = 1239 bytes, the size of the 2023 and 2024 formats: if a newer format changes
        the layout the length changes with it, and the flag is then left to the events.

How sure: the header, the event codes and the car telemetry layout are the published ones for 2023
and 2024 (sure). Format 2025 is believed to keep all of them (fairly sure); nothing here is checked
against a real game, so every packet is size-checked before it is read and anything else is ignored."""

from __future__ import annotations

import logging
import math
import socket
import struct
import threading
import time
from typing import Callable

from .lol import MIN_GAP, PAUSE, STALE
from .simracing import SimReader

log = logging.getLogger(__name__)

FORZA_PORT, F1_PORT = 5300, 20777
LOCAL = "127.0.0.1"
ALIVE = 3.0             # seconds without a packet after which the race counts as gone (the games send 10 to 60 a second)
END_LINGER = 4.0        # an F1 session that ended still counts this long, so the last moment shows
READ_WAIT = 1.0         # seconds a read waits, so the thread notices it should stop
RETRY = 5.0             # seconds between tries to take a port that is busy
START_HOLD = 12.0       # start lights older than this are forgotten (a start that was aborted)
YELLOW_HOLD = 3.0       # a yellow flag from the car status stays this long without a new one
RECV = 4096             # bytes a read takes: the biggest packet of the F1 games is about 1500

# The moments that pulse, and how long each lasts (seconds). The next one starts only when the one
# before is over, plus a pause, so there are never more than three in any second (the limit for people
# sensitive to flashing light, like League, Dota and Rocket League). The limiter blink runs through
# this too: while you sit at the limiter it pulses at 2.9 times a second at most.
FLASHES = ("fastest",)
LENGTH = {"limiter": 0.17, "fastest": 1.2}


# ---- Forza ---------------------------------------------------------------------------------------

FORZA_SLED, FORZA_DASH, FORZA_HORIZON, FORZA_MOTORSPORT = 232, 311, 324, 331
_FORZA_NAMES = {FORZA_SLED: "sled", FORZA_DASH: "dash", FORZA_HORIZON: "horizon", FORZA_MOTORSPORT: "motorsport"}
_FORZA_HEAD = struct.Struct("<iIfff")        # IsRaceOn, TimestampMS, EngineMaxRpm, EngineIdleRpm, CurrentEngineRpm
_FORZA_SPEED, _FORZA_GEAR = 244, 307          # in the dash (add 12 for Horizon)
HORIZON_GAP = 12


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def parse_forza(data: bytes) -> dict | None:
    """One Forza packet, or None when it isn't one (a length that no Forza sends, or numbers that
    can't be right). Returns {"source": "forza", "format", "race_on", "rpm", "max", "idle"} and, when
    the packet has a dash part, "speed" (km/h) and "gear"."""
    fmt = _FORZA_NAMES.get(len(data))
    if fmt is None:
        return None
    race_on, _, top, idle, rpm = _FORZA_HEAD.unpack_from(data, 0)
    if race_on not in (0, 1) or not _finite(top, idle, rpm) or min(top, idle, rpm) < 0 or top > 50_000:
        return None
    out = {"source": "forza", "format": fmt, "race_on": bool(race_on), "rpm": rpm, "max": top, "idle": idle}
    if fmt != "sled":
        shift = HORIZON_GAP if fmt == "horizon" else 0
        speed = struct.unpack_from("<f", data, _FORZA_SPEED + shift)[0]
        gear = data[_FORZA_GEAR + shift]
        out["speed"] = speed * 3.6 if _finite(speed) and 0 <= speed < 1000 else 0.0
        out["gear"] = -1 if gear == 0 else (gear if gear <= 10 else 0)
    return out


def rpm_fraction(rpm: float, idle: float, top: float) -> float:
    """Where the engine is between idle and its limit: 0 at idle, 1 at the limit."""
    if top <= 0:
        return 0.0
    span = top - idle
    if span <= 0:
        return max(0.0, min(1.0, rpm / top))
    return max(0.0, min(1.0, (rpm - idle) / span))


# ---- F1 -------------------------------------------------------------------------------------------

F1_FORMATS = (2023, 2024, 2025)
_F1_HEADER = struct.Struct("<HBBBBBQfIIBB")
HEADER = _F1_HEADER.size                     # 29
CARS = 22
_CAR = struct.Struct("<HfffBbHBBH")            # speed .. revLightsBitValue: the first 22 bytes of the 60 of a car
CAR_SIZE, STATUS_SIZE, FIA_AT = 60, 55, 28
TELEMETRY_MIN = HEADER + CARS * CAR_SIZE       # (3 more bytes follow in the real packet)
STATUS_LENGTH = HEADER + CARS * STATUS_SIZE
EVENTS = ("SSTA", "SEND", "FTLP", "DRSE", "DRSD", "CHQF", "LGOT", "STLG", "SCAR", "RCWN")
PACKET_EVENT, PACKET_TELEMETRY, PACKET_STATUS = 3, 6, 7


def parse_f1(data: bytes) -> dict | None:
    """One F1 packet, or None when it isn't one we use (another game, another format, another packet
    id, too short). Returns {"source": "f1", "game": "F1 24", "kind": "telemetry" | "event" | "status", ...}."""
    if len(data) < HEADER:
        return None
    fmt, year, _, _, _, pid, _, _, _, _, player, _ = _F1_HEADER.unpack_from(data, 0)
    if fmt not in F1_FORMATS or player >= CARS:
        return None
    game = f"F1 {year if 20 <= year <= 99 else fmt % 100}"
    if pid == PACKET_TELEMETRY:
        if len(data) < TELEMETRY_MIN:
            return None
        speed, _, _, _, _, gear, rpm, drs, pct, _ = _CAR.unpack_from(data, HEADER + player * CAR_SIZE)
        if pct > 100 or drs > 1:
            return None
        return {"source": "f1", "game": game, "kind": "telemetry", "speed": speed, "gear": gear, "rpm": rpm,
                "drs": bool(drs), "rev": pct / 100}
    if pid == PACKET_EVENT:
        return _f1_event(data, game, player)
    if pid == PACKET_STATUS:
        if len(data) != STATUS_LENGTH:
            return None
        flag = struct.unpack_from("<b", data, HEADER + player * STATUS_SIZE + FIA_AT)[0]
        return {"source": "f1", "game": game, "kind": "status", "fia": flag}
    return None


def _f1_event(data: bytes, game: str, player: int) -> dict | None:
    if len(data) < HEADER + 4:
        return None
    try:
        code = data[HEADER:HEADER + 4].decode("ascii")
    except UnicodeDecodeError:
        return None
    if code not in EVENTS:
        return None
    out = {"source": "f1", "game": game, "kind": "event", "code": code, "player": player}
    detail = data[HEADER + 4:]
    if code == "STLG":
        if not detail or not 1 <= detail[0] <= 5:
            return None
        out["lights"] = detail[0]
    elif code == "FTLP":
        if not detail:
            return None
        out["vehicle"] = detail[0]
    elif code == "SCAR":
        if len(detail) < 2:
            return None
        out["car"], out["change"] = detail[0], detail[1]
    return out


# ---- what is happening in the race ---------------------------------------------------------------

class RacingState:
    """The race as the game last described it, plus the moments worth showing with when they
    started. feed() takes one parsed packet (or one read of a sim's shared memory); snapshot() is what the light effect reads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = -1e9                # when the last packet came (monotonic)
        self._reset()

    def _reset(self) -> None:
        """A new session: forget the old one."""
        self.source = ""                # "forza" | "f1" | "sim" (iRacing, Assetto Corsa: shared memory)
        self.game = ""                  # "Forza" | "F1 24"
        self.race_on = False            # Forza: IsRaceOn; the sims: you're driving (on track, live)
        self.chequered_seen = False     # the sims: the flag is up now (so it counts once, not every read)
        self.rpm = 0.0                  # 0..1, idle to limit (F1: the game's own rev lights)
        self.gear = 0
        self.speed = 0.0                # km/h
        self.limiter = False
        self.drs_open = False
        self.drs_allowed = False
        self.lights = 0                 # start lights on now, 0 to 5
        self.lights_at = -1e9
        self.safety = False             # a (virtual) safety car is out
        self.yellow = False             # the FIA flag at your car
        self.yellow_at = -1e9
        self.ended: float | None = None     # F1: the session ended
        self.events: dict[str, float] = {}  # moments -> when
        self.pending: list[tuple[str, float]] = []
        self.busy_until = 0.0

    # -- reading --

    def feed(self, p: dict, now: float) -> None:
        """One packet, as parse_forza() or parse_f1() gave it."""
        with self.lock:
            if p.get("source") == "forza":
                self._forza(p, now)
            elif p.get("source") == "f1":
                self._f1(p, now)
            elif p.get("source") == "sim":
                self._sim(p, now)
            self._pump(now)

    def _forza(self, p: dict, now: float) -> None:
        if self.source != "forza":
            self._reset()
            self.source, self.game = "forza", "Forza"
        self.last = now
        self.race_on = p["race_on"]
        if not self.race_on:
            self.limiter, self.rpm = False, 0.0
            return
        self.rpm = rpm_fraction(p["rpm"], p["idle"], p["max"])
        self.gear, self.speed = p.get("gear", self.gear), p.get("speed", self.speed)
        # (with a little give, so an engine bouncing off its limit doesn't start and stop the blink)
        self.limiter = self.rpm >= 0.985 or (self.limiter and self.rpm >= 0.96)

    def _sim(self, p: dict, now: float) -> None:
        """One read of iRacing's or Assetto Corsa's shared memory (simracing.py). They send nothing like
        start lights or a fastest lap, only the revs, the gear and the flag at your car."""
        if self.source != "sim" or self.game != p["game"]:
            self._reset()
            self.source, self.game = "sim", p["game"]
        self.last = now
        self.race_on = p["live"]
        if not self.race_on:
            self.limiter, self.rpm, self.yellow, self.chequered_seen = False, 0.0, False, False
            return
        self.rpm = p["rpm"]
        self.gear, self.speed = p["gear"], p["speed"]
        if p.get("limiter") is None:          # (the car gave no limiter point: a little below the top of the bar, with give)
            self.limiter = self.rpm >= 0.97 or (self.limiter and self.rpm >= 0.94)
        else:
            self.limiter = p["limiter"]
        if p["caution"]:
            self.yellow, self.yellow_at = True, now
        else:
            self.yellow = False
        if p["chequered"] and not self.chequered_seen:
            self.events["chequered"] = now
        self.chequered_seen = p["chequered"]

    def _f1(self, p: dict, now: float) -> None:
        if self.source != "f1":
            self._reset()
            self.source = "f1"
        self.game = p["game"]
        kind = p["kind"]
        if kind == "telemetry":
            self.last = now
            self.rpm, self.gear, self.speed = p["rev"], p["gear"], float(p["speed"])
            self.limiter = self.rpm >= 1.0 or (self.limiter and self.rpm >= 0.95)
            self.drs_open = p["drs"]
        elif kind == "status":
            if p["fia"] == 3:
                self.yellow, self.yellow_at = True, now
            else:
                self.yellow = False
        elif kind == "event":
            self._event(p, now)

    def _event(self, p: dict, now: float) -> None:
        code = p["code"]
        if code == "SSTA":
            self._reset()
            self.source, self.game = "f1", p["game"]
            self.events["session"] = now
            return
        if code == "SEND":
            self.ended = now
            self.pending.clear()
            self.limiter = False
            self.lights = 0
            return
        if code == "STLG":
            self.lights, self.lights_at = p["lights"], now
        elif code == "LGOT":
            self.lights = 0
            self.events["lights_out"] = now
        elif code == "FTLP":
            if p["vehicle"] == p["player"]:         # (only your own: the others' don't concern your lights)
                self._queue("fastest", now)
        elif code == "DRSE":
            self.drs_allowed = True
            self.events["drs_enabled"] = now
        elif code == "DRSD":
            self.drs_allowed = False
            self.events["drs_disabled"] = now
        elif code == "CHQF":
            self.events["chequered"] = now
        elif code == "RCWN":
            self.events["winner"] = now
        elif code == "SCAR":
            if p["car"] in (1, 2) and p["change"] in (0, 1):
                self.safety = True
            elif p["change"] in (2, 3):
                self.safety = False

    def _queue(self, kind: str, now: float) -> None:
        if self.ended is None:
            self.pending.append((kind, now))

    def _pump(self, now: float) -> None:
        """Start the next waiting moment once the one before is over; the limiter blink gets its turn
        when nothing else is waiting, and only while you sit at the limit."""
        self.pending = [p for p in self.pending if now - p[1] <= STALE]
        if now < self.busy_until:
            return
        if self.pending:
            pick = min(self.pending, key=lambda p: FLASHES.index(p[0]))
            self.pending.remove(pick)
            kind = pick[0]
        elif self.limiter and self.in_race(now) and self.lights == 0:
            kind = "limiter"
        else:
            return
        self.events[kind] = now
        self.busy_until = now + max(LENGTH[kind] + PAUSE, MIN_GAP)

    # -- asking --

    def in_race(self, now: float) -> bool:
        """You're in a race (or on the track at all) and the game is sending. Forza says when you
        stop driving (IsRaceOn); F1 says when the session ends; both stop when the packets do."""
        if now - self.last >= ALIVE:
            return False
        if self.source in ("forza", "sim"):
            return self.race_on
        if self.source == "f1":
            return self.ended is None or now - self.ended <= END_LINGER
        return False

    def alive(self, now: float) -> bool:
        return now - self.last < ALIVE

    def label(self) -> str:
        return self.game

    def snapshot(self, now: float) -> dict:
        """What the light effect needs, with ages instead of times."""
        with self.lock:
            self._pump(now)
            racing = self.in_race(now)
            return {
                "inRace": racing, "source": self.source, "game": self.game,
                "rpm": self.rpm if racing else 0.0, "gear": self.gear, "speed": self.speed,
                "limiter": self.limiter and racing, "drs": self.drs_open and racing, "drsAllowed": self.drs_allowed,
                "startLights": self.lights if racing and now - self.lights_at < START_HOLD else 0,
                "caution": racing and (self.safety or (self.yellow and now - self.yellow_at < YELLOW_HOLD)),
                "safetyCar": self.safety, "ended": self.ended is not None,
                "ago": {k: now - t for k, t in self.events.items()},
            }


# ---- listening to the game -----------------------------------------------------------------------

def _bind(port: int):
    """The real thing: a UDP socket on this PC's own address. Nothing else on the network can reach it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((LOCAL, port))
    except OSError:
        sock.close()
        raise
    return sock


class RacingListener:
    """One UDP socket per game on 127.0.0.1, open only between start() and stop(). `make_socket(port)`
    is injectable so tests never open a real port."""

    def __init__(self, on_change: Callable[[], None] | None = None, make_socket: Callable[[int], object] | None = None,
                 clock: Callable[[], float] = time.monotonic, sim_opener: Callable | None = None):
        self.state = RacingState()
        self.on_change = on_change or (lambda: None)
        self.make_socket = make_socket or _bind
        self.clock = clock
        self.ports = {"forza": FORZA_PORT, "f1": F1_PORT}
        self.sim = SimReader(self._sim_read, opener=sim_opener, clock=clock)
        self.problems: dict[str, str] = {}      # game -> why its port isn't open
        self.ignored = 0                        # packets from anywhere but this PC, or that no game sends: for the log and the tests
        self._was = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def serve(self, kind: str, sock, stop: threading.Event | None = None) -> None:
        """Reads packets from `sock` until `stop` is set or the socket closes."""
        stop = stop or self._stop
        parse = parse_forza if kind == "forza" else parse_f1
        try:
            sock.settimeout(READ_WAIT)
        except (AttributeError, OSError):
            pass
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(RECV)
            except TimeoutError:                # quiet for a second: look at the clock and the stop switch
                self._noticed()
                continue
            except OSError as e:                # closed under us (or broken)
                log.debug("Racing socket (%s) ended: %s", kind, e)
                break
            if not addr or addr[0] != LOCAL:    # only this PC speaks to us
                self.ignored += 1
                continue
            try:
                packet = parse(data)
            except Exception:                   # (parsers don't raise; a surprise must not stop the listening)
                log.exception("Racing packet failed")
                packet = None
            if packet is None:
                self.ignored += 1
                continue
            self.state.feed(packet, self.clock())
            self._noticed()
        self._noticed()

    def _sim_read(self, sample: dict) -> None:
        """One read of a sim's shared memory: into the state, and tell the window when a race starts or ends."""
        self.state.feed(sample, self.clock())
        self._noticed()

    def seen(self) -> str:
        """The sim whose shared memory is open now ("iRacing", "Assetto Corsa"), or ""."""
        return " · ".join(self.sim.seen())

    def _noticed(self) -> None:
        """Tell the window when a race starts or ends (the rest it reads every second)."""
        now = self.state.in_race(self.clock())
        if now != self._was:
            self._was = now
            self.on_change()

    def snapshot(self, now: float) -> dict:
        return self.state.snapshot(now)

    def in_race(self, now: float) -> bool:
        return self.state.in_race(now)

    def receiving(self, now: float) -> bool:
        return self.state.alive(now)

    def label(self) -> str:
        return self.state.label()

    def start(self, forza_port: int = FORZA_PORT, f1_port: int = F1_PORT) -> None:
        """Opens both ports and starts looking for the sims' shared memory (again, with these numbers, when it was running)."""
        self.stop()
        self.ports = {"forza": int(forza_port), "f1": int(f1_port)}
        self._stop = stop = threading.Event()
        self._threads = [threading.Thread(target=self._run, args=(kind, port, stop), name=f"racing-{kind}", daemon=True)
                         for kind, port in self.ports.items()]
        for t in self._threads:
            t.start()
        self.sim.start()
        log.info("Game lighting: listening for racing games (Forza %s, F1 %s, iRacing and Assetto Corsa in shared memory)", forza_port, f1_port)

    def stop(self) -> None:
        self._stop.set()
        self.sim.stop()
        self._threads = []
        self.problems = {}
        self.state.last = -1e9                  # (no race any more, at once)
        self._was = False

    def _run(self, kind: str, port: int, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                sock = self.make_socket(port)
            except OSError as e:                # busy: another program has the port; try again later
                if self.problems.get(kind) is None:
                    log.warning("Racing games: port %s (%s) isn't free: %s", port, kind, e)
                self.problems[kind] = f"Port {port} is busy"
                stop.wait(RETRY)
                continue
            self.problems.pop(kind, None)
            try:
                self.serve(kind, sock, stop)
            except Exception:
                log.exception("Racing listener failed")
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            stop.wait(RETRY if not stop.is_set() else 0)
