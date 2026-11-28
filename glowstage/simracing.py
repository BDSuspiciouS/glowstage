"""Rev lights for the sims that publish their telemetry through Windows shared memory: iRacing, and
Assetto Corsa with Assetto Corsa Competizione. That is the way these games ship for dashboards, wheels
and lights: the game fills a named, memory-mapped block on purpose and any program may look at it. This
file opens those blocks read-only (FILE_MAP_READ: it cannot write a byte), copies what it needs and
closes them again. It never reads the game's own memory and touches no game file, so anti-cheat has
nothing to say about it. racing.py owns the reader (SimReader) and feeds RacingState with what comes out.

---- iRacing: "Local\\IRSDKMemMapFileName" (irsdk_defines.h, the published SDK) ---------------------
Header (irsdk_header), little-endian, 112 bytes:

    0 s32 ver              4 s32 status (bit 0 = connected: the sim is running)   8 s32 tickRate
   12 s32 sessionInfoUpdate  16 s32 sessionInfoLen  20 s32 sessionInfoOffset   (the YAML: not used)
   24 s32 numVars          28 s32 varHeaderOffset   32 s32 numBuf   36 s32 bufLen   40 s32 pad[2]
   48 varBuf[4], each 16 bytes: s32 tickCount, s32 bufOffset, s32 pad[2]

Variable header (irsdk_varHeader), 144 bytes each, numVars of them from varHeaderOffset:

    0 s32 type (0 char, 1 bool, 2 int, 3 bitfield, 4 float, 5 double)   4 s32 offset (in a buffer)
    8 s32 count   12 u8 countAsTime + 3 pad   16 char name[32]   48 char desc[64]   112 char unit[32]

The telemetry is double-buffered over numBuf (3 or 4) buffers: the newest is the one with the highest
tickCount, and its tickCount is read again after the copy: when it moved, the sim overwrote it
meanwhile and this read is thrown away (the SDK's own recipe). Variables are found by NAME in the
table, never by a fixed offset, because the table differs per sim version. Used: RPM (float), Gear
(int, -1 reverse, 0 neutral), Speed (float, m/s), IsOnTrack (bool), SessionFlags (bitfield) and the
car's own shift light points PlayerCarSLFirstRPM, PlayerCarSLShiftRPM, PlayerCarSLLastRPM and
PlayerCarSLBlinkRPM (float, rpm). SessionFlags bits used: checkered 0x1, yellow 0x8, red 0x10,
yellowWaving 0x100, caution 0x4000, cautionWaving 0x8000 (the rest of the list isn't needed).

---- Assetto Corsa and Competizione: "Local\\acpmf_physics", "_graphics", "_static" ---------------
Structs packed to 4 bytes, wide chars are 2 bytes (UTF-16). The offsets below were computed from the
published structs with ctypes (the test file does that again, as a check) and only the leading part of
each page is read:

    physics  0 s32 packetId  4 f32 gas  8 f32 brake  12 f32 fuel  16 s32 gear (0 reverse, 1 neutral,
             2 first ...)  20 s32 rpms  24 f32 steerAngle  28 f32 speedKmh
    graphics 0 s32 packetId  4 s32 status (0 off, 1 replay, 2 live, 3 pause)  8 s32 session
             ... 1224 s32 flag (AC_FLAG_TYPE: 0 none, 1 blue, 2 yellow, 3 black, 4 white, 5 checkered,
             6 penalty, 7 green, 8 orange)
    static   412 s32 maxRpm  (after smVersion, acVersion, numberOfSessions, numCars, carModel, track,
             playerName, playerSurname, playerNick, sectorCount, maxTorque, maxPower)

How sure: physics, the graphics status and static maxRpm are the well known ones (sure). The flag's
offset follows from the published field order (packetId .. tyreCompound, replayTimeMultiplier,
normalizedCarPosition, activeCars, carCoordinates[60][3], carID[60], playerCarID, penaltyTime, flag),
which Assetto Corsa and Competizione share up to there (fairly sure, not seen on a real game): the flag
is only read when the page is long enough and the value is 0 to 8, and it only ever adds the amber band
and the chequered checker, never a rev. Assetto Corsa and Competizione share the block names and these
fields, so they can't be told apart here and both are called "Assetto Corsa". Nothing here has run
against a real game."""

from __future__ import annotations

import ctypes
import logging
import math
import struct
import sys
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

SEEK = 3.0          # seconds between looks for a game's block while there is none (a failed open is instant)
POLL = 1 / 30       # seconds between reads while a game is live (about 30 a second: plenty for rev lights)
IDLE = 0.25         # seconds between reads while a game's block is open but you aren't driving (menu, pause, garage)
LET_GO = 10.0       # seconds not driving after which the block is closed (the game may be gone: while we hold it, it stays)
STALL = 2.0         # seconds a game's counter may stand still before we say it stopped (a crashed game leaves its last data)
MAX_COPY = 1 << 20  # the most one read may take: nothing we use is anywhere near this

IRACING_MAP = "Local\\IRSDKMemMapFileName"
AC_MAPS = {"physics": "Local\\acpmf_physics", "graphics": "Local\\acpmf_graphics", "static": "Local\\acpmf_static"}

FILE_MAP_READ = 0x0004


# ---- reading a named block of memory ------------------------------------------------------------

class _MemoryInfo(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", ctypes.c_uint32),
                ("PartitionId", ctypes.c_uint16), ("RegionSize", ctypes.c_size_t), ("State", ctypes.c_uint32),
                ("Protect", ctypes.c_uint32), ("Type", ctypes.c_uint32)]


_kernel_cache: list = []


def _kernel():
    if not _kernel_cache:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        k.OpenFileMappingW.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.UnmapViewOfFile.restype = ctypes.c_int
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = ctypes.c_int
        k.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MemoryInfo), ctypes.c_size_t]
        k.VirtualQuery.restype = ctypes.c_size_t
        _kernel_cache.append(k)
    return _kernel_cache[0]


class Mapping:
    """A named block of shared memory, open for reading only. read() copies bytes out and never goes
    past the end of the block."""

    def __init__(self, handle, view, size: int):
        self._handle, self._view, self.size = handle, view, size

    def read(self, offset: int, length: int) -> bytes | None:
        if self._view is None or offset < 0 or length < 0 or length > MAX_COPY or offset + length > self.size:
            return None
        return ctypes.string_at(self._view + offset, length)

    def close(self) -> None:
        k = _kernel()
        if self._view is not None:
            k.UnmapViewOfFile(self._view)
            self._view = None
        if self._handle is not None:
            k.CloseHandle(self._handle)
            self._handle = None


def open_mapping(name: str) -> Mapping | None:
    """The block called `name` (as "Local\\Name"), or None when no program has made it: that is how
    a game that isn't running looks, and the answer is instant."""
    if sys.platform != "win32":
        return None
    k = _kernel()
    handle = k.OpenFileMappingW(FILE_MAP_READ, 0, name)
    if not handle:
        return None
    view = k.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
    if not view:
        k.CloseHandle(handle)
        return None
    info = _MemoryInfo()
    if not k.VirtualQuery(view, ctypes.byref(info), ctypes.sizeof(info)):
        k.UnmapViewOfFile(view)
        k.CloseHandle(handle)
        return None
    return Mapping(handle, view, int(info.RegionSize))


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


class _Moving:
    """Tells whether a counter that should tick on every frame is still ticking."""

    def __init__(self):
        self.value, self.at = None, 0.0

    def ticking(self, value, now: float) -> bool:
        if value != self.value:
            self.value, self.at = value, now
        return now - self.at < STALL


NOT_LIVE = {"live": False}


# ---- iRacing -----------------------------------------------------------------------------------

_IR_HEAD = struct.Struct("<12i")                 # ver .. bufLen, pad[2]
_IR_BUF = struct.Struct("<4i")                   # tickCount, bufOffset, pad[2]
IR_HEADER, IR_BUFS_AT, IR_BUF_SIZE = 112, 48, 16
IR_VAR_SIZE = 144
_IR_TYPES = {0: ("c", 1), 1: ("?", 1), 2: ("i", 4), 3: ("I", 4), 4: ("f", 4), 5: ("d", 8)}
_IR_WANT = ("RPM", "Gear", "Speed", "IsOnTrack", "SessionFlags",
            "PlayerCarSLFirstRPM", "PlayerCarSLShiftRPM", "PlayerCarSLLastRPM", "PlayerCarSLBlinkRPM")
IR_CHECKERED, IR_YELLOW, IR_RED, IR_YELLOW_WAVING, IR_CAUTION, IR_CAUTION_WAVING = 0x1, 0x8, 0x10, 0x100, 0x4000, 0x8000
IR_CAUTION_FLAGS = IR_YELLOW | IR_RED | IR_YELLOW_WAVING | IR_CAUTION | IR_CAUTION_WAVING


def iracing_variables(table: bytes, count: int) -> dict[str, tuple[int, int]]:
    """{name: (type, offset)} for the variables we use, from the raw variable table."""
    out: dict[str, tuple[int, int]] = {}
    for i in range(count):
        at = i * IR_VAR_SIZE
        if at + IR_VAR_SIZE > len(table):
            break
        kind, offset = struct.unpack_from("<ii", table, at)
        name = table[at + 16:at + 48].split(b"\0", 1)[0].decode("ascii", "ignore")
        if name in _IR_WANT:
            out[name] = (kind, offset)
    return out


def iracing_bar(rpm: float, first: float, shift: float, last: float) -> float:
    """The rev bar from the car's own shift light points: the first 30 % of the bar is the climb from
    nothing to the first light, the rest runs from the first light to the last. A car whose points
    aren't given (0, or out of order) has a plain guess: the revs of a 9000 rpm engine."""
    top = last if last > 0 else shift
    if first > 0 and top > first:
        if rpm < first:
            return 0.3 * max(0.0, rpm) / first
        return min(1.0, 0.3 + 0.7 * (rpm - first) / (top - first))
    return max(0.0, min(1.0, rpm / 9000.0))


def iracing_sample(values: dict, now_ticking: bool) -> dict:
    """What the rest of Glowstage needs from the variables of one iRacing buffer."""
    rpm, gear, speed = values.get("RPM"), values.get("Gear"), values.get("Speed")
    if rpm is None or not _finite(rpm):
        return NOT_LIVE
    on_track = values.get("IsOnTrack")
    if on_track is None:
        on_track = True
    flags = int(values.get("SessionFlags") or 0)
    first, shift, last, blink = (float(values.get(k) or 0.0) for k in
                                 ("PlayerCarSLFirstRPM", "PlayerCarSLShiftRPM", "PlayerCarSLLastRPM", "PlayerCarSLBlinkRPM"))
    if not _finite(first, shift, last, blink):
        first = shift = last = blink = 0.0
    out = {"live": bool(on_track and now_ticking), "rpm": iracing_bar(float(rpm), first, shift, last),
           "gear": int(gear) if gear is not None and -1 <= gear <= 12 else 0,
           "speed": float(speed) * 3.6 if speed is not None and _finite(speed) and 0 <= speed < 400 else 0.0,
           "limiter": (rpm >= blink) if blink > 0 else None,
           "caution": bool(flags & IR_CAUTION_FLAGS), "chequered": bool(flags & IR_CHECKERED)}
    return out


class IRacing:
    game = "iRacing"

    def __init__(self):
        self.map: Mapping | None = None
        self._vars: dict[str, tuple[int, int]] = {}
        self._key: tuple[int, int] | None = None
        self._moving = _Moving()
        self._prev: dict = NOT_LIVE

    def open(self, opener) -> bool:
        m = opener(IRACING_MAP)
        if m is None:
            return False
        if m.size < IR_HEADER:
            m.close()
            return False
        self.map, self._vars, self._key, self._moving, self._prev = m, {}, None, _Moving(), NOT_LIVE
        return True

    def close(self) -> None:
        if self.map is not None:
            self.map.close()
            self.map = None

    def poll(self, now: float) -> dict:
        m = self.map
        raw = m.read(0, IR_HEADER) if m else None
        if raw is None:
            return NOT_LIVE
        head = _IR_HEAD.unpack_from(raw, 0)
        status, n_vars, var_at, n_buf, buf_len = head[1], head[6], head[7], head[8], head[9]
        if not status & 1:
            return NOT_LIVE
        if not (0 < n_vars <= 8192 and 1 <= n_buf <= 4 and 0 < buf_len <= MAX_COPY and var_at > 0):
            return NOT_LIVE
        if self._key != (var_at, n_vars) or "RPM" not in self._vars:
            table = m.read(var_at, n_vars * IR_VAR_SIZE)
            if table is None:
                return NOT_LIVE
            self._vars, self._key = iracing_variables(table, n_vars), (var_at, n_vars)
            if "RPM" not in self._vars:
                return NOT_LIVE
        bufs = [_IR_BUF.unpack_from(raw, IR_BUFS_AT + i * IR_BUF_SIZE) for i in range(n_buf)]
        index = max(range(n_buf), key=lambda i: bufs[i][0])
        tick, at = bufs[index][0], bufs[index][1]
        data = m.read(at, buf_len)
        again = m.read(IR_BUFS_AT + index * IR_BUF_SIZE, 4)
        if data is None or again is None:
            return NOT_LIVE
        if struct.unpack("<i", again)[0] != tick:       # the sim wrote to it while we copied: this read is no good
            return self._prev
        values = {}
        for name, (kind, offset) in self._vars.items():
            fmt = _IR_TYPES.get(kind)
            if fmt and offset >= 0 and offset + fmt[1] <= len(data):
                values[name] = struct.unpack_from("<" + fmt[0], data, offset)[0]
        self._prev = iracing_sample(values, self._moving.ticking(tick, now))
        return self._prev


# ---- Assetto Corsa / Competizione ----------------------------------------------------------------

_AC_PHYSICS = struct.Struct("<ifffiiff")         # packetId gas brake fuel gear rpms steerAngle speedKmh
_AC_GRAPHIC = struct.Struct("<iii")              # packetId status session
AC_FLAG_AT, AC_MAXRPM_AT = 1224, 412
AC_LIVE = 2
AC_YELLOW, AC_CHECKERED = 2, 5
AC_BAR_FROM = 0.3       # the bar starts at 30 % of the engine's limit (the idle isn't in the data)


def ac_sample(physics: bytes | None, graphics: bytes | None, static: bytes | None, peak: float = 0.0) -> tuple[dict, float]:
    """(what the rest needs, the highest rpm seen). `peak` stands in for the limit when the static
    page doesn't give a maxRpm."""
    if not physics or not graphics or len(physics) < _AC_PHYSICS.size or len(graphics) < _AC_GRAPHIC.size:
        return NOT_LIVE, peak
    _, _, _, _, gear, rpms, _, speed = _AC_PHYSICS.unpack_from(physics, 0)
    _, status, _ = _AC_GRAPHIC.unpack_from(graphics, 0)
    peak = max(peak, float(rpms), 3000.0)
    top = 0
    if static and len(static) >= AC_MAXRPM_AT + 4:
        top = struct.unpack_from("<i", static, AC_MAXRPM_AT)[0]
    top = float(top) if 1000 <= top <= 30000 else peak
    flag = 0
    if len(graphics) >= AC_FLAG_AT + 4:
        flag = struct.unpack_from("<i", graphics, AC_FLAG_AT)[0]
        flag = flag if 0 <= flag <= 8 else 0
    bar = max(0.0, min(1.0, (rpms / top - AC_BAR_FROM) / (1 - AC_BAR_FROM))) if rpms > 0 else 0.0
    out = {"live": status == AC_LIVE, "rpm": bar, "gear": gear - 1 if 0 <= gear <= 12 else 0,
           "speed": float(speed) if _finite(speed) and 0 <= speed < 1000 else 0.0, "limiter": None,
           "caution": flag == AC_YELLOW, "chequered": flag == AC_CHECKERED}
    return out, peak


class AssettoCorsa:
    game = "Assetto Corsa"

    def __init__(self):
        self.maps: dict[str, Mapping] = {}
        self._moving = _Moving()
        self._peak = 0.0
        self._static: tuple[float, bytes | None] = (-1e9, None)

    def open(self, opener) -> bool:
        found = {}
        for key in ("physics", "graphics"):             # (both of these or nothing; the static page may come late)
            m = opener(AC_MAPS[key])
            if m is None:
                for other in found.values():
                    other.close()
                return False
            found[key] = m
        if found["physics"].size < _AC_PHYSICS.size or found["graphics"].size < _AC_GRAPHIC.size:
            for other in found.values():
                other.close()
            return False
        self.maps = found
        self._moving, self._peak, self._static = _Moving(), 0.0, (-1e9, None)
        static = opener(AC_MAPS["static"])
        if static is not None:
            self.maps["static"] = static
        return True

    def close(self) -> None:
        for m in self.maps.values():
            m.close()
        self.maps = {}

    def poll(self, now: float) -> dict:
        if "physics" not in self.maps:
            return NOT_LIVE
        physics = self.maps["physics"].read(0, _AC_PHYSICS.size)
        graphics = self.maps["graphics"].read(0, min(self.maps["graphics"].size, AC_FLAG_AT + 4))
        if now - self._static[0] >= 2.0:                # (the car changes only between sessions)
            static = self.maps.get("static")
            self._static = (now, static.read(0, AC_MAXRPM_AT + 4) if static and static.size >= AC_MAXRPM_AT + 4 else None)
        out, self._peak = ac_sample(physics, graphics, self._static[1], self._peak)
        if physics and not self._moving.ticking(struct.unpack_from("<i", physics, 0)[0], now):
            out = NOT_LIVE
        return out


# ---- looking for the games and reading them ------------------------------------------------------

class SimReader:
    """Looks for the sims' blocks of shared memory every few seconds, reads an open one about 30 times
    a second while you drive and lets it go again when the game is gone. Everything it sees goes to
    `feed(sample)`; `opener(name)` and `clock` are injectable so tests never touch a real block.
    step() does one round and says how long to wait before the next: that is the whole rate policy."""

    def __init__(self, feed: Callable[[dict], None], opener: Callable[[str], Mapping | None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.feed = feed
        self.opener = opener or open_mapping
        self.clock = clock
        self.sources = [IRacing(), AssettoCorsa()]
        self.active: list = []
        self.dark: dict[str, float] = {}      # game -> since when it isn't live
        self.seeks = 0                        # how many times we looked for a block: for the tests
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def seen(self) -> list[str]:
        """The games whose block is open now."""
        return [s.game for s in list(self.active)]

    def step(self) -> float:
        now = self.clock()
        if not self.active:
            self.seeks += 1
            for src in self.sources:
                try:
                    if src.open(self.opener):
                        self.active.append(src)
                        self.dark.pop(src.game, None)
                        log.info("Racing games: %s is running", src.game)
                except Exception:
                    log.exception("Racing games: looking for %s failed", src.game)
            if not self.active:
                return SEEK
        samples = []
        for src in list(self.active):
            try:
                p = dict(src.poll(now))
            except Exception:
                log.exception("Racing games: reading %s failed", src.game)
                p = dict(NOT_LIVE)
            p["source"], p["game"] = "sim", src.game
            if p["live"]:
                self.dark.pop(src.game, None)
            else:
                since = self.dark.setdefault(src.game, now)
                if now - since >= LET_GO:               # not driving for a while: let go, so a game that quit frees its block
                    src.close()
                    self.active.remove(src)
                    self.dark.pop(src.game, None)
                    log.info("Racing games: let go of %s", src.game)
            samples.append(p)
        live = [p for p in samples if p["live"]]
        for p in live or samples[:1]:                   # (a game that sits in a menu doesn't talk over one you drive)
            self.feed(p)
        if not self.active:
            return SEEK
        return POLL if live else IDLE

    def start(self) -> None:
        self.stop()
        self._stop = stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(stop,), name="racing-shm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(2)

    def _run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    delay = self.step()
                except Exception:
                    log.exception("Racing games: shared memory round failed")
                    delay = SEEK
                stop.wait(delay)
        finally:
            for src in self.sources:
                src.close()
            self.active = []
            self.dark = {}
