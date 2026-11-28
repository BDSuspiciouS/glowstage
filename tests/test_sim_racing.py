import ctypes
import struct
import sys
import threading
import time
import unittest
import uuid

from glowstage import racing, simracing
from glowstage.effects import EFFECTS
from glowstage.racing import RacingListener, RacingState
from glowstage.simracing import (AC_FLAG_AT, AC_MAXRPM_AT, AC_MAPS, IDLE, IR_VAR_SIZE, IRACING_MAP, LET_GO, POLL, SEEK, STALL,
                                 AssettoCorsa, IRacing, Mapping, SimReader, ac_sample, iracing_bar, iracing_variables,
                                 open_mapping)

# ---- shared memory, built the way the games write it (no real block is ever opened here) -------------

FLOAT, INT, BOOL, BITS = 4, 2, 1, 3
IR_FORMAT = {FLOAT: "f", INT: "i", BOOL: "?", BITS: "I"}


class Fake:
    """A block of memory that can be read like the real one (and watched: how often, and was it closed)."""

    def __init__(self, data: bytes, on_read=None):
        self.data = bytearray(data)
        self.size = len(data)
        self.closed = False
        self.reads = 0
        self.on_read = on_read

    def read(self, offset, length):
        self.reads += 1
        if offset < 0 or length < 0 or offset + length > self.size:
            return None
        out = bytes(self.data[offset:offset + length])
        if self.on_read:
            self.on_read(self, offset, length)
        return out

    def close(self):
        self.closed = True


EXTRA = ("Pad1", INT), ("Lat", FLOAT)                    # (variables we don't use: they must not confuse us)


def iracing(values=None, ticks=(100, 90, 80), status=1, skew=0, drop=(), n_buf=None, bufs_at_gap=64):
    """An iRacing block: header, the variable table, then `n_buf` buffers. `values` says what the car does
    (the newest buffer holds it; the others hold other numbers, so reading the wrong one shows)."""
    v = {"RPM": (FLOAT, 6000.0), "Gear": (INT, 3), "Speed": (FLOAT, 50.0), "IsOnTrack": (BOOL, True), "SessionFlags": (BITS, 0),
         "PlayerCarSLFirstRPM": (FLOAT, 5000.0), "PlayerCarSLShiftRPM": (FLOAT, 6800.0), "PlayerCarSLLastRPM": (FLOAT, 7000.0),
         "PlayerCarSLBlinkRPM": (FLOAT, 7200.0)}
    for k, x in (values or {}).items():
        v[k] = x if isinstance(x, tuple) else (v[k][0], x)
    for k in drop:
        v.pop(k, None)
    table = [(name, kind) for name, kind in EXTRA] + [(k, x[0]) for k, x in v.items()]
    offsets, at = {}, 0
    for name, kind in table:
        offsets[name] = at
        at += 8
    buf_len = at
    n_buf = n_buf or len(ticks)
    var_at = 112
    bufs_at = var_at + len(table) * IR_VAR_SIZE + bufs_at_gap
    data = bytearray(bufs_at + buf_len * n_buf)
    struct.pack_into("<12i", data, 0, 2, status, 60, 5, 0, 0, len(table), var_at, n_buf, buf_len, 0, 0)
    for i, (name, kind) in enumerate(table):
        h = var_at + i * IR_VAR_SIZE
        struct.pack_into("<iiiB3x", data, h, kind, offsets[name], 1, 0)
        data[h + 16:h + 16 + len(name)] = name.encode()
        data[h + 48:h + 52] = b"desc"
    newest = max(range(n_buf), key=lambda i: ticks[i])
    for i in range(n_buf):
        struct.pack_into("<4i", data, 48 + i * 16, ticks[i], bufs_at + i * buf_len, 0, 0)
        for name, (kind, x) in v.items():
            if i != newest and name == "RPM":
                x = 1234.0                              # (an older buffer says something else)
            struct.pack_into("<" + IR_FORMAT[kind], data, bufs_at + i * buf_len + offsets[name], x)
    return bytes(data)


def physics(packet=1, gear=3, rpms=6000, speed=120.0):
    return struct.pack("<ifffiiff", packet, 1.0, 0.0, 30.0, gear, rpms, 0.0, speed) + bytes(600)


def graphics(status=2, flag=0, packet=1, size=1580):
    b = bytearray(size)
    struct.pack_into("<iii", b, 0, packet, status, 2)
    if size >= AC_FLAG_AT + 4:
        struct.pack_into("<i", b, AC_FLAG_AT, flag)
    return bytes(b)


def static(max_rpm=8000, size=800):
    b = bytearray(size)
    b[0:8] = "1.7".encode("utf-16-le").ljust(8, b"\0")
    struct.pack_into("<i", b, AC_MAXRPM_AT, max_rpm)
    return bytes(b)


def opener(blocks):
    """An opener that knows these blocks only (name -> Fake or bytes) and remembers what it was asked."""
    asked = []

    def open_(name):
        asked.append(name)
        b = blocks.get(name)
        return Fake(b) if isinstance(b, (bytes, bytearray)) else b
    open_.asked = asked
    return open_


# ---- iRacing -------------------------------------------------------------------------------------------

class IRacingBlock(unittest.TestCase):
    def read(self, block, now=1.0, **kw):
        src = IRacing()
        assert src.open(opener({IRACING_MAP: Fake(block)}))
        return src.poll(now)

    def test_variables_are_found_by_name_in_the_table(self):
        raw = iracing()
        count = struct.unpack_from("<i", raw, 24)[0]
        found = iracing_variables(raw[112:112 + count * IR_VAR_SIZE], count)
        self.assertEqual(set(found), {"RPM", "Gear", "Speed", "IsOnTrack", "SessionFlags", "PlayerCarSLFirstRPM",
                                      "PlayerCarSLShiftRPM", "PlayerCarSLLastRPM", "PlayerCarSLBlinkRPM"})
        self.assertEqual(found["RPM"][0], FLOAT)
        self.assertEqual(found["Gear"][0], INT)
        self.assertEqual(found["IsOnTrack"][0], BOOL)
        self.assertEqual(found["SessionFlags"][0], BITS)

    def test_the_names_are_found_wherever_they_sit(self):
        a = iracing_variables(iracing()[112:], 11)
        b = iracing_variables(iracing(values={"Zzz": (FLOAT, 1.0)})[112:], 12)      # (one more variable shifts them all)
        self.assertEqual(set(a), set(b))
        self.assertNotEqual(a["RPM"][1], b["RPM"][1] + 1)

    def test_the_header_is_112_bytes_and_a_variable_144(self):
        self.assertEqual(simracing.IR_HEADER, 112)
        self.assertEqual(simracing._IR_HEAD.size + 4 * simracing._IR_BUF.size, 112)
        self.assertEqual(IR_VAR_SIZE, 144)
        self.assertEqual(16 + 32 + 64 + 32, 144)

    def test_a_car_driving(self):
        s = self.read(iracing(values={"Gear": (INT, 4), "Speed": (FLOAT, 50.0)}))
        self.assertTrue(s["live"])
        self.assertEqual(s["gear"], 4)
        self.assertAlmostEqual(s["speed"], 180.0)                                   # (metres a second to km/h)
        self.assertAlmostEqual(s["rpm"], 0.3 + 0.7 * 0.5)                           # (halfway from the first light to the last)
        self.assertFalse(s["limiter"])
        self.assertFalse(s["caution"] or s["chequered"])

    def test_the_newest_buffer_is_the_one_read(self):
        for ticks in ((100, 90, 80), (10, 30, 20), (5, 6, 7), (7, 6, 5)):
            s = self.read(iracing(ticks=ticks))
            self.assertAlmostEqual(s["rpm"], 0.65, msg=ticks)                       # (never the 1234 of the others)
        s = self.read(iracing(ticks=(1, 2, 3, 4)))
        self.assertAlmostEqual(s["rpm"], 0.65)

    def test_the_shift_light_points_give_the_bar(self):
        self.assertAlmostEqual(iracing_bar(0.0, 5000, 6800, 7000), 0.0)
        self.assertAlmostEqual(iracing_bar(2500, 5000, 6800, 7000), 0.15)
        self.assertAlmostEqual(iracing_bar(5000, 5000, 6800, 7000), 0.3)
        self.assertAlmostEqual(iracing_bar(7000, 5000, 6800, 7000), 1.0)
        self.assertAlmostEqual(iracing_bar(9000, 5000, 6800, 7000), 1.0)
        self.assertAlmostEqual(iracing_bar(6000, 5000, 6800, 0), 0.3 + 0.7 * 1000 / 1800)     # (no last light: the shift point stands in)
        self.assertAlmostEqual(iracing_bar(4500, 0, 0, 0), 0.5)                              # (a car that gives no points: a guess)
        self.assertAlmostEqual(iracing_bar(4500, 7000, 5000, 6000), 0.5)                      # (points out of order: the guess too)

    def test_the_bar_through_a_block(self):
        for rpm, want in ((2500.0, 0.15), (5000.0, 0.3), (7000.0, 1.0)):
            self.assertAlmostEqual(self.read(iracing(values={"RPM": (FLOAT, rpm)}))["rpm"], want)

    def test_the_blink_point_is_the_limiter(self):
        self.assertFalse(self.read(iracing(values={"RPM": (FLOAT, 7100.0)}))["limiter"])
        self.assertTrue(self.read(iracing(values={"RPM": (FLOAT, 7250.0)}))["limiter"])
        s = self.read(iracing(values={"RPM": (FLOAT, 7250.0), "PlayerCarSLBlinkRPM": (FLOAT, 0.0)}))
        self.assertIsNone(s["limiter"])                                             # (no blink point: the state decides)

    def test_session_flags_become_flags(self):
        for bits, caution, chequered in ((0, False, False), (0x4, False, False), (0x2 | 0x20, False, False),
                                         (0x8, True, False), (0x100, True, False), (0x4000, True, False), (0x8000, True, False),
                                         (0x10, True, False), (0x1, False, True), (0x1 | 0x8, True, True)):
            s = self.read(iracing(values={"SessionFlags": (BITS, bits)}))
            self.assertEqual((s["caution"], s["chequered"]), (caution, chequered), hex(bits))

    def test_not_connected_is_no_race(self):
        self.assertFalse(self.read(iracing(status=0))["live"])
        self.assertFalse(self.read(iracing(status=2))["live"])                      # (only bit 0 counts)

    def test_off_the_track_is_no_race(self):
        self.assertFalse(self.read(iracing(values={"IsOnTrack": (BOOL, False)}))["live"])

    def test_a_block_without_the_revs_or_with_a_broken_header_is_no_race(self):
        self.assertFalse(self.read(iracing(drop=("RPM",)))["live"])
        for index, bad in ((6, 0), (6, 100000), (8, 0), (8, 9), (9, 0), (9, 1 << 30), (7, 0)):
            raw = bytearray(iracing())
            struct.pack_into("<i", raw, index * 4, bad)
            self.assertFalse(self.read(bytes(raw))["live"], (index, bad))
        self.assertFalse(self.read(iracing()[:200])["live"])                        # (cut short: nothing is read past the end)
        raw = bytearray(iracing())
        struct.pack_into("<f", raw, raw.find(struct.pack("<f", 6000.0)), float("nan"))
        self.assertFalse(self.read(bytes(raw))["live"])

    def test_a_block_too_small_for_a_header_is_not_opened(self):
        fake = Fake(bytes(50))
        self.assertFalse(IRacing().open(opener({IRACING_MAP: fake})))
        self.assertTrue(fake.closed)
        self.assertFalse(IRacing().open(opener({})))

    def test_a_read_torn_by_the_sim_is_thrown_away(self):
        raw = iracing()
        src = IRacing()
        fake = Fake(raw)
        src.open(lambda name: fake)
        self.assertAlmostEqual(src.poll(1.0)["rpm"], 0.65)

        def tear(f, offset, length):                    # (the sim writes a new tick into the buffer while it is being copied)
            if length > 16:
                struct.pack_into("<i", f.data, 48, 999)
        fake.on_read = tear
        self.assertAlmostEqual(src.poll(1.1)["rpm"], 0.65)                          # (the last good read stands, not half of a new one)
        src2 = IRacing()
        src2.open(lambda name: Fake(raw, on_read=tear))
        self.assertFalse(src2.poll(1.0)["live"])                                    # (and nothing before a first good read)

    def test_a_counter_that_stands_still_means_the_game_stopped(self):
        src = IRacing()
        src.open(opener({IRACING_MAP: iracing()}))
        self.assertTrue(src.poll(10.0)["live"])
        self.assertTrue(src.poll(10.0 + STALL - 0.1)["live"])
        self.assertFalse(src.poll(10.0 + STALL + 0.1)["live"])
        fake = src.map
        struct.pack_into("<i", fake.data, 48, 5000)                                 # it ticks again
        self.assertTrue(src.poll(20.0)["live"])


# ---- Assetto Corsa and Competizione -------------------------------------------------------------------

class AssettoCorsaPages(unittest.TestCase):
    def test_the_offsets_are_the_ones_the_published_structs_have(self):
        """The structs of the SDK, packed to 4 bytes with 2-byte wide chars, laid out by ctypes."""
        c = ctypes

        class Physics(c.Structure):
            _pack_ = 4
            _fields_ = [("packetId", c.c_int32), ("gas", c.c_float), ("brake", c.c_float), ("fuel", c.c_float),
                        ("gear", c.c_int32), ("rpms", c.c_int32), ("steerAngle", c.c_float), ("speedKmh", c.c_float)]

        class Graphic(c.Structure):
            _pack_ = 4
            _fields_ = [("packetId", c.c_int32), ("status", c.c_int32), ("session", c.c_int32),
                        ("currentTime", c.c_uint16 * 15), ("lastTime", c.c_uint16 * 15), ("bestTime", c.c_uint16 * 15),
                        ("split", c.c_uint16 * 15), ("completedLaps", c.c_int32), ("position", c.c_int32),
                        ("iCurrentTime", c.c_int32), ("iLastTime", c.c_int32), ("iBestTime", c.c_int32),
                        ("sessionTimeLeft", c.c_float), ("distanceTraveled", c.c_float), ("isInPit", c.c_int32),
                        ("currentSectorIndex", c.c_int32), ("lastSectorTime", c.c_int32), ("numberOfLaps", c.c_int32),
                        ("tyreCompound", c.c_uint16 * 33), ("replayTimeMultiplier", c.c_float),
                        ("normalizedCarPosition", c.c_float), ("activeCars", c.c_int32),
                        ("carCoordinates", (c.c_float * 3) * 60), ("carID", c.c_int32 * 60), ("playerCarID", c.c_int32),
                        ("penaltyTime", c.c_float), ("flag", c.c_int32)]

        class Static(c.Structure):
            _pack_ = 4
            _fields_ = [("smVersion", c.c_uint16 * 15), ("acVersion", c.c_uint16 * 15), ("numberOfSessions", c.c_int32),
                        ("numCars", c.c_int32), ("carModel", c.c_uint16 * 33), ("track", c.c_uint16 * 33),
                        ("playerName", c.c_uint16 * 33), ("playerSurname", c.c_uint16 * 33), ("playerNick", c.c_uint16 * 33),
                        ("sectorCount", c.c_int32), ("maxTorque", c.c_float), ("maxPower", c.c_float), ("maxRpm", c.c_int32)]

        self.assertEqual((Physics.gear.offset, Physics.rpms.offset, Physics.speedKmh.offset), (16, 20, 28))
        self.assertEqual(simracing._AC_PHYSICS.unpack_from(physics(7, 3, 6100, 99.5), 0)[::2][:1], (7,))
        self.assertEqual(Graphic.status.offset, 4)
        self.assertEqual(Graphic.flag.offset, AC_FLAG_AT)
        self.assertEqual(Static.maxRpm.offset, AC_MAXRPM_AT)

    def sample(self, phys=None, gfx=None, stat=None, peak=0.0):
        return ac_sample(phys if phys is not None else physics(), gfx if gfx is not None else graphics(),
                         stat if stat is not None else static(), peak)[0]

    def test_the_revs_and_the_limit(self):
        s = self.sample(physics(rpms=6000), stat=static(8000))
        self.assertTrue(s["live"])
        self.assertAlmostEqual(s["rpm"], (0.75 - 0.3) / 0.7)
        self.assertAlmostEqual(self.sample(physics(rpms=8000))["rpm"], 1.0)
        self.assertAlmostEqual(self.sample(physics(rpms=9000))["rpm"], 1.0)
        self.assertAlmostEqual(self.sample(physics(rpms=1000))["rpm"], 0.0)
        self.assertAlmostEqual(self.sample(physics(rpms=0))["rpm"], 0.0)
        self.assertAlmostEqual(self.sample(physics(rpms=6000), stat=static(12000))["rpm"], (0.5 - 0.3) / 0.7)

    def test_without_a_limit_the_highest_revs_seen_stand_in(self):
        s, peak = ac_sample(physics(rpms=5000), graphics(), static(0), 0.0)
        self.assertAlmostEqual(s["rpm"], 1.0)
        self.assertEqual(peak, 5000.0)
        s, _ = ac_sample(physics(rpms=2500), graphics(), None, 5000.0)
        self.assertAlmostEqual(s["rpm"], (0.5 - 0.3) / 0.7)                          # (half of the 5000 seen)

    def test_gears(self):
        for raw, want in ((0, -1), (1, 0), (2, 1), (3, 2), (7, 6)):
            self.assertEqual(self.sample(physics(gear=raw))["gear"], want, raw)
        self.assertEqual(self.sample(physics(gear=99))["gear"], 0)
        self.assertEqual(self.sample(physics(gear=-5))["gear"], 0)

    def test_the_speed_is_in_kmh_already(self):
        self.assertAlmostEqual(self.sample(physics(speed=123.5))["speed"], 123.5)
        self.assertEqual(self.sample(physics(speed=float("nan")))["speed"], 0.0)

    def test_only_live_counts(self):
        for status, live in ((0, False), (1, False), (2, True), (3, False), (9, False)):
            self.assertEqual(self.sample(gfx=graphics(status=status))["live"], live, status)

    def test_the_flag_where_the_offset_is_known(self):
        for flag, caution, chequered in ((0, False, False), (1, False, False), (2, True, False), (3, False, False),
                                         (4, False, False), (5, False, True), (7, False, False), (8, False, False)):
            s = self.sample(gfx=graphics(flag=flag))
            self.assertEqual((s["caution"], s["chequered"]), (caution, chequered), flag)

    def test_a_flag_that_cant_be_one_is_ignored(self):
        for flag in (9, 255, -1, 1 << 30):
            s = self.sample(gfx=graphics(flag=flag))
            self.assertFalse(s["caution"] or s["chequered"], flag)
        s = self.sample(gfx=graphics(size=1000, flag=5))                            # (a page too short to hold it)
        self.assertTrue(s["live"])
        self.assertFalse(s["chequered"])

    def test_short_or_missing_pages_are_no_race(self):
        self.assertFalse(ac_sample(None, graphics(), static())[0]["live"])
        self.assertFalse(ac_sample(physics(), None, static())[0]["live"])
        self.assertFalse(ac_sample(physics()[:10], graphics(), static())[0]["live"])
        self.assertFalse(ac_sample(physics(), graphics()[:6], static())[0]["live"])

    def blocks(self, **parts):
        return {AC_MAPS[k]: v for k, v in parts.items()}

    def test_the_source_needs_physics_and_graphics_and_closes_what_it_opened_otherwise(self):
        phys = Fake(physics())
        src = AssettoCorsa()
        self.assertFalse(src.open(opener(self.blocks(physics=phys))))
        self.assertTrue(phys.closed)
        self.assertFalse(AssettoCorsa().open(opener({})))
        self.assertFalse(AssettoCorsa().open(opener(self.blocks(physics=Fake(bytes(8)), graphics=Fake(graphics())))))
        src = AssettoCorsa()
        self.assertTrue(src.open(opener(self.blocks(physics=physics(), graphics=graphics()))))     # (no static page: still fine)
        self.assertTrue(src.poll(1.0)["live"])

    def test_the_source_reads_the_three_pages(self):
        src = AssettoCorsa()
        src.open(opener(self.blocks(physics=physics(rpms=6000, gear=4), graphics=graphics(flag=2), static=static(8000))))
        s = src.poll(1.0)
        self.assertTrue(s["live"] and s["caution"])
        self.assertEqual(s["gear"], 3)
        self.assertAlmostEqual(s["rpm"], 0.45 / 0.7)

    def test_the_source_notices_a_game_that_stopped_counting(self):
        src = AssettoCorsa()
        src.open(opener(self.blocks(physics=physics(packet=5), graphics=graphics(), static=static())))
        self.assertTrue(src.poll(10.0)["live"])
        self.assertFalse(src.poll(10.0 + STALL + 0.1)["live"])
        struct.pack_into("<i", src.maps["physics"].data, 0, 6)
        self.assertTrue(src.poll(20.0)["live"])

    def test_closing_closes_every_page(self):
        fakes = [Fake(physics()), Fake(graphics()), Fake(static())]
        src = AssettoCorsa()
        src.open(opener(self.blocks(physics=fakes[0], graphics=fakes[1], static=fakes[2])))
        src.close()
        self.assertTrue(all(f.closed for f in fakes))
        self.assertFalse(src.poll(1.0)["live"])


# ---- the reader: looking, reading, letting go ----------------------------------------------------------

class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class TheReader(unittest.TestCase):
    def reader(self, blocks):
        self.fed = []
        self.clock = Clock()
        self.open = opener(blocks)
        return SimReader(self.fed.append, opener=self.open, clock=self.clock)

    def test_it_looks_for_all_the_games_blocks_and_waits_a_few_seconds_between_looks(self):
        r = self.reader({})
        self.assertEqual(r.step(), SEEK)
        self.assertEqual(r.seeks, 1)
        self.assertIn(IRACING_MAP, self.open.asked)
        self.assertIn(AC_MAPS["physics"], self.open.asked)
        self.assertEqual(self.fed, [])
        self.assertEqual(r.seen(), [])
        self.assertGreaterEqual(SEEK, 2.0)
        self.assertLessEqual(POLL, 1 / 25)
        self.assertGreaterEqual(POLL, 1 / 70)

    def test_a_game_that_drives_is_read_fast_and_fed(self):
        r = self.reader({IRACING_MAP: iracing()})
        self.assertEqual(r.step(), POLL)
        self.assertEqual(r.seen(), ["iRacing"])
        self.assertEqual(len(self.fed), 1)
        p = self.fed[0]
        self.assertEqual((p["source"], p["game"], p["live"]), ("sim", "iRacing", True))
        asked = len(self.open.asked)
        r.step()
        self.assertEqual(len(self.open.asked), asked)                               # (open: it isn't looked for again)
        self.assertEqual(r.seeks, 1)

    def test_a_game_in_a_menu_is_read_slowly_and_let_go_after_a_while(self):
        block = Fake(iracing(values={"IsOnTrack": (BOOL, False)}))
        r = self.reader({IRACING_MAP: block})
        self.assertEqual(r.step(), IDLE)
        self.assertLess(POLL, IDLE)
        self.assertFalse(self.fed[-1]["live"])                                      # (told, so the lights go back at once)
        self.clock.t += LET_GO - 1
        self.assertEqual(r.step(), IDLE)
        self.assertFalse(block.closed)
        self.clock.t += 2
        self.assertEqual(r.step(), SEEK)
        self.assertTrue(block.closed)
        self.assertEqual(r.seen(), [])

    def test_a_game_that_is_gone_is_looked_for_again(self):
        blocks = {IRACING_MAP: Fake(iracing())}
        r = self.reader(blocks)
        r.step()
        blocks[IRACING_MAP].data[4:8] = struct.pack("<i", 0)                        # (the sim says it is gone)
        self.assertEqual(r.step(), IDLE)
        self.clock.t += LET_GO + 1
        self.assertEqual(r.step(), SEEK)
        asked = len(self.open.asked)
        blocks.clear()
        self.assertEqual(r.step(), SEEK)
        self.assertGreater(len(self.open.asked), asked)
        self.assertEqual(r.seeks, 2)

    def test_a_game_that_comes_back_is_found(self):
        blocks = {}
        r = self.reader(blocks)
        self.assertEqual(r.step(), SEEK)
        blocks[AC_MAPS["physics"]] = physics()
        blocks[AC_MAPS["graphics"]] = graphics()
        self.assertEqual(r.step(), POLL)
        self.assertEqual(r.seen(), ["Assetto Corsa"])
        self.assertEqual(self.fed[-1]["game"], "Assetto Corsa")

    def test_the_game_you_drive_speaks_over_one_in_a_menu(self):
        r = self.reader({IRACING_MAP: iracing(values={"IsOnTrack": (BOOL, False)}),
                         AC_MAPS["physics"]: physics(), AC_MAPS["graphics"]: graphics()})
        self.assertEqual(r.step(), POLL)
        self.assertEqual([p["game"] for p in self.fed], ["Assetto Corsa"])
        self.assertEqual(sorted(r.seen()), ["Assetto Corsa", "iRacing"])

    def test_a_surprise_in_a_read_doesnt_stop_it(self):
        r = self.reader({})

        class Broken:
            game = "Broken"

            def open(self, o):
                return True

            def poll(self, now):
                raise ValueError("boom")

            def close(self):
                pass
        r.sources = [Broken()]
        self.assertEqual(r.step(), IDLE)
        self.assertFalse(self.fed[0]["live"])

    def test_the_thread_starts_and_stops_without_waiting_for_the_next_look(self):
        r = SimReader(lambda p: None, opener=lambda name: None)
        r.start()
        time.sleep(0.05)
        started = time.time()
        r.stop()
        self.assertLess(time.time() - started, 1.5)
        self.assertFalse(r._thread)

    def test_stopping_closes_what_is_open(self):
        fake = Fake(iracing())
        r = SimReader(lambda p: None, opener=lambda name: fake if name == IRACING_MAP else None)
        r.start()
        deadline = time.time() + 3
        while not r.seen() and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(r.seen(), ["iRacing"])
        r.stop()
        deadline = time.time() + 3
        while not fake.closed and time.time() < deadline:
            time.sleep(0.005)
        self.assertTrue(fake.closed)


# ---- the state and the effect ---------------------------------------------------------------------------

def sample(game="iRacing", live=True, rpm=0.5, gear=3, speed=100.0, limiter=False, caution=False, chequered=False):
    return {"source": "sim", "game": game, "live": live, "rpm": rpm, "gear": gear, "speed": speed, "limiter": limiter,
            "caution": caution, "chequered": chequered}


class TheState(unittest.TestCase):
    def test_a_read_makes_a_race(self):
        s = RacingState()
        s.feed(sample(rpm=0.7, gear=4), 10.0)
        snap = s.snapshot(10.0)
        self.assertTrue(snap["inRace"])
        self.assertEqual((snap["source"], snap["game"], snap["gear"]), ("sim", "iRacing", 4))
        self.assertAlmostEqual(snap["rpm"], 0.7)
        self.assertEqual(s.label(), "iRacing")

    def test_not_live_is_no_race_but_the_game_is_still_seen(self):
        s = RacingState()
        s.feed(sample(), 10.0)
        s.feed(sample(live=False), 11.0)
        snap = s.snapshot(11.0)
        self.assertFalse(snap["inRace"])
        self.assertEqual(snap["rpm"], 0.0)
        self.assertTrue(s.alive(11.0))

    def test_when_the_reads_stop_the_race_ends(self):
        s = RacingState()
        s.feed(sample(), 10.0)
        self.assertFalse(s.snapshot(10.0 + racing.ALIVE + 0.5)["inRace"])

    def test_the_limiter_comes_from_the_car_or_from_the_bar(self):
        s = RacingState()
        s.feed(sample(rpm=0.5, limiter=True), 1.0)
        self.assertTrue(s.snapshot(1.0)["limiter"])
        s.feed(sample(rpm=0.99, limiter=False), 2.0)
        self.assertFalse(s.snapshot(2.0)["limiter"])                                # (the car's own point wins)
        s.feed(sample(rpm=0.99, limiter=None), 3.0)
        self.assertTrue(s.snapshot(3.0)["limiter"])
        s.feed(sample(rpm=0.95, limiter=None), 4.0)
        self.assertTrue(s.snapshot(4.0)["limiter"])                                 # (a little give, so it doesn't stutter)
        s.feed(sample(rpm=0.8, limiter=None), 5.0)
        self.assertFalse(s.snapshot(5.0)["limiter"])

    def test_the_limiter_makes_the_blue_pulse_but_never_faster_than_three_a_second(self):
        s = RacingState()
        pulses, last = 0, None
        for i in range(1000):
            now = 10.0 + i * 0.01
            s.feed(sample(limiter=True, rpm=1.0), now)
            at = s.snapshot(now)["ago"].get("limiter")
            if at is not None and (last is None or at < last):
                pulses += 1
            last = at
        self.assertGreater(pulses, 2)
        self.assertLessEqual(pulses, 10 * 3 + 1)

    def test_a_yellow_flag_is_a_caution_while_it_is_up(self):
        s = RacingState()
        s.feed(sample(caution=True), 1.0)
        self.assertTrue(s.snapshot(1.0)["caution"])
        s.feed(sample(caution=False), 2.0)
        self.assertFalse(s.snapshot(2.0)["caution"])

    def test_the_chequered_flag_counts_once(self):
        s = RacingState()
        s.feed(sample(), 1.0)
        self.assertNotIn("chequered", s.snapshot(1.0)["ago"])
        s.feed(sample(chequered=True), 2.0)
        s.feed(sample(chequered=True), 3.0)                                         # (still up: it isn't a new flag)
        self.assertAlmostEqual(s.snapshot(3.0)["ago"]["chequered"], 1.0)
        s.feed(sample(chequered=False), 4.0)
        s.feed(sample(chequered=True), 5.0)
        self.assertAlmostEqual(s.snapshot(5.0)["ago"]["chequered"], 0.0)

    def test_another_game_starts_afresh(self):
        s = RacingState()
        s.feed(sample(chequered=True), 1.0)
        s.feed(sample(game="Assetto Corsa", rpm=0.2, gear=1), 2.0)
        snap = s.snapshot(2.0)
        self.assertEqual(snap["game"], "Assetto Corsa")
        self.assertNotIn("chequered", snap["ago"])
        s.feed(racing.parse_forza(bytes(311)) or {"source": "forza", "race_on": True, "rpm": 100.0, "max": 8000.0, "idle": 800.0}, 3.0)
        self.assertEqual(s.snapshot(3.0)["game"], "Forza")

    def test_the_effect_shows_the_bar_the_state_gives(self):
        def lit(rpm):
            s = RacingState()
            s.feed(sample(rpm=rpm), 1.0)
            px = EFFECTS["game_racing"].render(0.0, 10, None, {}, {"racing": s.snapshot(1.0), "span": (0.0, 1.0, "front")})
            return sum(max(p) > 150 for p in px)
        self.assertEqual(lit(0.0), 0)
        self.assertEqual(lit(1.0), 10)
        self.assertLess(lit(0.4), lit(0.8))

    def test_the_effect_waits_when_not_driving(self):
        s = RacingState()
        s.feed(sample(live=False), 1.0)
        px = EFFECTS["game_racing"].render(0.0, 10, None, {}, {"racing": s.snapshot(1.0), "span": (0.0, 1.0, "front")})
        self.assertTrue(all(max(p) < 250 for p in px))


class TheListener(unittest.TestCase):
    def test_the_listener_feeds_the_state_from_the_sims_and_tells_the_window(self):
        changes = []
        now = [50.0]
        lis = RacingListener(on_change=lambda: changes.append(1), clock=lambda: now[0],
                             sim_opener=opener({IRACING_MAP: iracing(values={"RPM": (FLOAT, 7000.0)})}))
        lis.sim.step()
        self.assertTrue(lis.in_race(50.0))
        self.assertEqual(lis.label(), "iRacing")
        self.assertEqual(lis.seen(), "iRacing")
        self.assertAlmostEqual(lis.snapshot(50.0)["rpm"], 1.0)
        self.assertTrue(lis.receiving(50.0))
        self.assertEqual(changes, [1])
        self.assertFalse(lis.sim._thread)                                           # (nothing runs until start())

    def test_start_looks_for_the_sims_and_stop_lets_go(self):
        fake = Fake(iracing())
        lis = RacingListener(sim_opener=lambda name: fake if name == IRACING_MAP else None, make_socket=lambda port: FakeSock())
        lis.start(5300, 20777)
        deadline = time.time() + 3
        while not lis.in_race(time.monotonic()) and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(lis.label(), "iRacing")
        lis.stop()
        deadline = time.time() + 3
        while not fake.closed and time.time() < deadline:
            time.sleep(0.005)
        self.assertTrue(fake.closed)
        self.assertEqual(lis.seen(), "")


class FakeSock:
    def settimeout(self, t):
        pass

    def recvfrom(self, n):
        time.sleep(0.01)
        raise TimeoutError()

    def close(self):
        pass


# ---- the real memory, with a block of our own ----------------------------------------------------------

@unittest.skipUnless(sys.platform == "win32", "Windows shared memory")
class TheRealThing(unittest.TestCase):
    """A block we make ourselves under a name no game uses: the reader opens, reads and closes it like a game's."""

    def make(self, data: bytes):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileMappingW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p]
        k.CreateFileMappingW.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        name = "Local\\glowstage_test_" + uuid.uuid4().hex
        handle = k.CreateFileMappingW(ctypes.c_void_p(-1), None, 0x04, 0, len(data), name)    # PAGE_READWRITE
        self.assertTrue(handle)
        view = k.MapViewOfFile(handle, 0x0002, 0, 0, 0)                                         # FILE_MAP_WRITE
        self.assertTrue(view)
        ctypes.memmove(view, data, len(data))
        self.addCleanup(lambda: (k.UnmapViewOfFile(view), k.CloseHandle(handle)))
        return name

    def test_a_block_that_isnt_there_is_none(self):
        self.assertIsNone(open_mapping("Local\\glowstage_test_" + uuid.uuid4().hex))

    def test_it_reads_the_bytes_and_stays_inside_the_block(self):
        data = bytes(range(256)) * 20
        m = open_mapping(self.make(data))
        self.assertIsInstance(m, Mapping)
        self.addCleanup(m.close)
        self.assertGreaterEqual(m.size, len(data))
        self.assertEqual(m.read(0, 16), data[:16])
        self.assertEqual(m.read(1000, 100), data[1000:1100])
        self.assertIsNone(m.read(m.size - 4, 8))
        self.assertIsNone(m.read(-1, 4))
        self.assertIsNone(m.read(0, simracing.MAX_COPY + 1))

    def test_a_whole_iracing_read_works_end_to_end_and_close_unmaps(self):
        name = self.make(iracing(values={"Gear": (INT, 5)}))
        src = IRacing()
        self.assertTrue(src.open(lambda n: open_mapping(name)))
        s = src.poll(1.0)
        self.assertTrue(s["live"])
        self.assertEqual(s["gear"], 5)
        m = src.map
        src.close()
        self.assertIsNone(m.read(0, 4))                                             # (closed: nothing is read from it any more)

    def test_the_block_is_open_for_reading_only(self):
        m = open_mapping(self.make(bytes(4096)))
        self.addCleanup(m.close)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(simracing._MemoryInfo), ctypes.c_size_t]
        info = simracing._MemoryInfo()
        k.VirtualQuery(m._view, ctypes.byref(info), ctypes.sizeof(info))
        self.assertEqual(info.Protect, 0x02)                                        # PAGE_READONLY


if __name__ == "__main__":
    unittest.main()
