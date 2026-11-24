"""LIFX driver: wire format, parsing and a fake bulb on 127.0.0.1 (no real network)."""

import socket
import struct
import threading
import time
import unittest

from glowstage.devices import lifx
from glowstage.state import LightState

MAC = "d073d5123456"


def wait_for(cond, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeLifx:
    """A LIFX light on 127.0.0.1: answers Get* messages and records every Set* it receives."""

    def __init__(self, zones=0, label="Kitchen", product=22, answer_unhandled=True, silent=False, ack=True):
        self.zones, self.label, self.product = zones, label, product
        self.answer_unhandled, self.silent, self.ack = answer_unhandled, silent, ack
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.records: list[tuple[int, bytes]] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        self._thread.join(1)
        self.sock.close()

    def payloads(self, msg_type):
        return [p for t, p in list(self.records) if t == msg_type]

    def _reply(self, msg, addr, msg_type, payload):
        self.sock.sendto(lifx.build_message(msg_type, payload, source=msg.source, mac=MAC,
                                            sequence=msg.sequence), addr)

    def _run(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            msg = lifx.parse_message(data)
            if msg is None or self.silent:
                continue
            if msg.type == lifx.GET_SERVICE:
                self._reply(msg, addr, lifx.STATE_SERVICE, struct.pack("<BI", 1, self.port))
            elif msg.type == lifx.GET_LABEL:
                self._reply(msg, addr, lifx.STATE_LABEL, self.label.encode().ljust(32, b"\0"))
            elif msg.type == lifx.GET_VERSION:
                self._reply(msg, addr, lifx.STATE_VERSION, struct.pack("<III", 1, self.product, 0))
            elif msg.type == lifx.GET_EXTENDED_COLOR_ZONES:
                if self.zones:
                    n = min(self.zones, 82)
                    colors = b"".join(struct.pack("<HHHH", 0, 0, 0, 3500) for _ in range(n))
                    colors = colors.ljust(82 * 8, b"\0")
                    self._reply(msg, addr, lifx.STATE_EXTENDED_COLOR_ZONES,
                                struct.pack("<HHB", self.zones, 0, n) + colors)
                elif self.answer_unhandled:
                    self._reply(msg, addr, lifx.STATE_UNHANDLED, struct.pack("<H", msg.type))
            else:
                self.records.append((msg.type, msg.payload))
                if msg.flags & 2 and self.ack:
                    self._reply(msg, addr, lifx.ACKNOWLEDGEMENT, b"")


class WireTests(unittest.TestCase):
    def test_get_service_broadcast_bytes(self):
        data = lifx.build_message(lifx.GET_SERVICE, source=1, tagged=True)
        expected = (bytes([0x24, 0x00, 0x00, 0x34, 0x01, 0x00, 0x00, 0x00])   # size, protocol, source
                    + bytes(8) + bytes(6)                                       # target, reserved
                    + bytes([0x00, 0x00])                                       # flags, sequence
                    + bytes(8) + bytes([0x02, 0x00]) + bytes(2))               # reserved, type, reserved
        self.assertEqual(data, expected)

    def test_unicast_header(self):
        data = lifx.build_message(lifx.GET_LABEL, source=0xAABBCCDD, mac=MAC, res_required=True, sequence=7)
        self.assertEqual(len(data), 36)
        self.assertEqual(data[0:2], b"\x24\x00")
        self.assertEqual(data[2:4], b"\x00\x14")                  # 1024 | addressable, not tagged
        self.assertEqual(data[4:8], bytes([0xDD, 0xCC, 0xBB, 0xAA]))
        self.assertEqual(data[8:16], bytes.fromhex(MAC) + b"\0\0")
        self.assertEqual(data[22], 1)                              # res_required
        self.assertEqual(data[23], 7)
        self.assertEqual(data[32:34], b"\x17\x00")                 # type 23
        msg = lifx.parse_message(data)
        self.assertEqual((msg.mac, msg.type, msg.source, msg.tagged, msg.sequence), (MAC, 23, 0xAABBCCDD, False, 7))

    def test_ack_flag_and_payload_size(self):
        data = lifx.build_message(lifx.LIGHT_SET_POWER, lifx.set_power_payload(True), source=5, mac=MAC, ack_required=True)
        self.assertEqual(data[22], 2)
        self.assertEqual(struct.unpack("<H", data[0:2])[0], 36 + 6)
        self.assertEqual(data[36:], b"\xff\xff\x00\x00\x00\x00")

    def test_parse_rejects_garbage(self):
        self.assertIsNone(lifx.parse_message(b"short"))
        self.assertIsNone(lifx.parse_message(bytes(40)))

    def test_hsbk_conversion(self):
        self.assertEqual(lifx.rgb_to_hsbk((255, 0, 0)), (0, 65535, 65535, 3500))
        self.assertEqual(lifx.rgb_to_hsbk((0, 255, 0)), (21845, 65535, 65535, 3500))
        self.assertEqual(lifx.rgb_to_hsbk((0, 0, 255)), (43691, 65535, 65535, 3500))
        self.assertEqual(lifx.rgb_to_hsbk((255, 255, 255)), (0, 0, 65535, 3500))
        self.assertEqual(lifx.rgb_to_hsbk((0, 0, 0)), (0, 0, 0, 3500))
        self.assertEqual(lifx.rgb_to_hsbk((255, 0, 0), 0.5)[2], 32768)

    def test_set_color_payload(self):
        payload = lifx.set_color_payload((1, 2, 3, 3500), 250)
        self.assertEqual(payload, struct.pack("<BHHHHI", 0, 1, 2, 3, 3500, 250))

    def test_parse_state_service(self):
        self.assertEqual(lifx.parse_state_service(b"\x01\x7c\xdd\x00\x00"), (1, 56700))
        self.assertIsNone(lifx.parse_state_service(b"\x01"))

    def test_parse_state_label(self):
        self.assertEqual(lifx.parse_state_label(b"Living room\0" + b"\0" * 20), "Living room")
        self.assertEqual(lifx.parse_state_label("Café".encode().ljust(32, b"\0")), "Café")

    def test_parse_state_version(self):
        self.assertEqual(lifx.parse_state_version(struct.pack("<III", 1, 32, 0)), (1, 32))

    def test_parse_state_extended_zones(self):
        colors = [(i, 2, 3, 3500) for i in range(3)]
        payload = struct.pack("<HHB", 16, 0, 3) + b"".join(struct.pack("<HHHH", *c) for c in colors)
        payload = payload.ljust(5 + 82 * 8, b"\0")
        self.assertEqual(lifx.parse_state_extended_zones(payload), (16, 0, colors))

    def test_set_zones_payload_padding(self):
        colors = [(1, 1, 1, 3500)] * 3
        payload = lifx.set_zones_payload(colors, 0, 1)
        self.assertEqual(len(payload), 8 + 82 * 8)
        self.assertEqual(payload[:8], struct.pack("<IBHB", 0, 1, 0, 3))
        self.assertEqual(payload[-8:], bytes(8))


class DeviceTests(unittest.TestCase):
    def make(self, bulb, **kw):
        dev = lifx.LifxDevice(f"lifx:{MAC}", "Test", "127.0.0.1", bulb.port, MAC)
        dev.timeout = kw.get("timeout", 0.3)
        self.addCleanup(dev._sock.close)
        return dev

    def setUp(self):
        self.bulbs = []

    def tearDown(self):
        for b in self.bulbs:
            b.close()

    def bulb(self, **kw):
        b = FakeLifx(**kw)
        self.bulbs.append(b)
        return b

    def test_connect_bulb(self):
        bulb = self.bulb(label="Desk", product=22)
        dev = self.make(bulb)
        dev.connect()
        self.assertEqual(dev.pixel_count, 1)
        self.assertTrue(dev.online)
        self.assertIsNone(dev.error)
        self.assertEqual(dev.reported_name, "Desk")
        self.assertEqual(dev.extra_info()["label"], "LIFX · product 22")

    def test_connect_bulb_without_unhandled_answer(self):
        bulb = self.bulb(answer_unhandled=False)
        dev = self.make(bulb, timeout=0.1)
        dev.connect()
        self.assertEqual(dev.pixel_count, 1)

    def test_connect_strip(self):
        bulb = self.bulb(zones=16, product=32)
        dev = self.make(bulb)
        dev.connect()
        self.assertEqual(dev.pixel_count, 16)
        self.assertEqual(dev.extra_info()["label"], "LIFX · 16 zones")
        self.assertEqual(dev.extra_info()["host"], "127.0.0.1")

    def test_connect_unreachable_raises(self):
        bulb = self.bulb(silent=True)
        dev = self.make(bulb, timeout=0.1)
        with self.assertRaises(Exception):
            dev.connect()
        self.assertFalse(dev.online)

    def test_show_color_and_power_off(self):
        bulb = self.bulb()
        dev = self.make(bulb)
        dev.connect()
        dev.show_color((255, 0, 0), LightState(brightness=50))
        self.assertEqual(bulb.payloads(lifx.LIGHT_SET_POWER), [struct.pack("<HI", 65535, 0)])
        color = bulb.payloads(lifx.SET_COLOR)
        self.assertEqual(len(color), 1)
        _, h, s, b, k, duration = struct.unpack("<BHHHHI", color[0])
        self.assertEqual((h, s, b, k, duration), (0, 65535, 32768, 3500, 0))
        dev.power_off()
        self.assertEqual(bulb.payloads(lifx.LIGHT_SET_POWER)[-1], struct.pack("<HI", 0, 0))

    def test_show_color_scales_by_value(self):
        bulb = self.bulb()
        dev = self.make(bulb)
        dev.show_color((0, 0, 128), LightState(brightness=100))
        _, h, s, b, k, _ = struct.unpack("<BHHHHI", bulb.payloads(lifx.SET_COLOR)[0])
        self.assertEqual(b, round(128 / 255 * 65535))

    def test_power_off_without_answer_raises(self):
        bulb = self.bulb(ack=False)
        dev = self.make(bulb, timeout=0.1)
        with self.assertRaises(TimeoutError):
            dev.power_off(0.2)

    def test_apply_off_and_color(self):
        bulb = self.bulb()
        dev = self.make(bulb)
        dev.connect()
        dev.apply(LightState(on=True, brightness=100, color="#00ff00"))
        self.assertEqual(len(bulb.payloads(lifx.SET_COLOR)), 1)
        dev.apply(LightState(on=False))
        self.assertEqual(bulb.payloads(lifx.LIGHT_SET_POWER)[-1], struct.pack("<HI", 0, 0))

    def test_stream_bulb(self):
        bulb = self.bulb()
        dev = self.make(bulb)
        dev.connect()
        dev.begin_stream(LightState(mode="effect"))
        dev.send_frame([(0, 0, 255)])
        self.assertTrue(wait_for(lambda: len(bulb.payloads(lifx.SET_COLOR)) == 1))
        _, h, s, b, k, duration = struct.unpack("<BHHHHI", bulb.payloads(lifx.SET_COLOR)[0])
        self.assertEqual((h, s, b, duration), (43691, 65535, 65535, 0))
        self.assertEqual(bulb.payloads(lifx.LIGHT_SET_POWER)[0], struct.pack("<HI", 65535, 0))

    def test_stream_strip(self):
        bulb = self.bulb(zones=10)
        dev = self.make(bulb)
        dev.connect()
        dev.begin_stream(LightState(mode="effect"))
        dev.send_frame([(255, 0, 0)] * 5 + [(0, 255, 0)] * 5)
        self.assertTrue(wait_for(lambda: len(bulb.payloads(lifx.SET_EXTENDED_COLOR_ZONES)) == 1))
        payload = bulb.payloads(lifx.SET_EXTENDED_COLOR_ZONES)[0]
        self.assertEqual(len(payload), 8 + 82 * 8)
        self.assertEqual(struct.unpack("<IBHB", payload[:8]), (0, 1, 0, 10))
        zones = [struct.unpack("<HHHH", payload[8 + i * 8:16 + i * 8]) for i in range(10)]
        self.assertEqual(zones[0], (0, 65535, 65535, 3500))
        self.assertEqual(zones[9], (21845, 65535, 65535, 3500))
        self.assertEqual(payload[8 + 10 * 8:], bytes(72 * 8))

    def test_stream_long_strip_is_chunked(self):
        bulb = self.bulb(zones=100)
        dev = self.make(bulb)
        dev.connect()
        self.assertEqual(dev.pixel_count, 100)
        dev.send_frame([(255, 255, 255)] * 100)
        self.assertTrue(wait_for(lambda: len(bulb.payloads(lifx.SET_EXTENDED_COLOR_ZONES)) == 2))
        first, second = sorted(bulb.payloads(lifx.SET_EXTENDED_COLOR_ZONES),
                               key=lambda p: struct.unpack("<IBHB", p[:8])[2])
        self.assertEqual(struct.unpack("<IBHB", first[:8]), (0, 0, 0, 82))    # apply only on the last chunk
        self.assertEqual(struct.unpack("<IBHB", second[:8]), (0, 1, 82, 18))


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.bulb = FakeLifx(label="Hall", product=32, zones=8)

    def tearDown(self):
        self.bulb.close()

    def test_metadata(self):
        d = lifx.Driver()
        self.assertEqual((d.kind, d.title, d.beta, d.pairing), ("lifx", "LIFX", True, ""))

    def test_scan_finds_bulb(self):
        found = lifx.Driver().scan(0.6, port=self.bulb.port, broadcast="127.0.0.1")
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual((f.kind, f.dev_id, f.host, f.port, f.label), ("lifx", f"lifx:{MAC}", "127.0.0.1", self.bulb.port, "Hall"))
        self.assertEqual(f.extra, {"mac": MAC, "product": 32})

    def test_scan_never_raises(self):
        self.assertEqual(lifx.Driver().scan(0.2, port=self.bulb.port, broadcast="not a host name"), [])

    def test_probe(self):
        f = lifx.Driver().probe({"host": "127.0.0.1"}, port=self.bulb.port)
        self.assertEqual(f.dev_id, f"lifx:{MAC}")
        self.assertEqual(f.label, "Hall")
        self.assertEqual(f.port, self.bulb.port)

    def test_probe_unreachable(self):
        self.bulb.silent = True
        with self.assertRaises(ValueError):
            lifx.Driver().probe({"host": "127.0.0.1"}, port=self.bulb.port)
        with self.assertRaises(ValueError):
            lifx.Driver().probe({"host": ""})

    def test_create(self):
        dev = lifx.Driver().create(f"lifx:{MAC}", {"host": "127.0.0.1", "port": 5000, "label": "Hall", "mac": MAC})
        self.addCleanup(dev._sock.close)
        self.assertEqual((dev.host, dev.port, dev.mac, dev.name, dev.group), ("127.0.0.1", 5000, MAC, "Hall", "room"))
        self.assertEqual(dev.max_fps, 20)
        self.assertFalse(dev.hardware_brightness)


if __name__ == "__main__":
    unittest.main()
