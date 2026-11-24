import socket
import struct
import unittest
import uuid

from glowstage.devices import dmx
from glowstage.state import LightState


def make_hub(protocol="artnet", universe=0, fixtures=None, host="", **kw):
    entry = {"kind": "dmx", "protocol": protocol, "host": host, "universe": universe,
             "label": "t", "fixtures": fixtures or []}
    hub = dmx.DmxHub(f"dmx:{protocol}:{host or 'broadcast'}:{universe}", entry, **kw)
    hub._build_fixtures(entry["fixtures"])
    return hub


def fixture(hub, i=0):
    return list(hub.devices.values())[i]


class PacketTests(unittest.TestCase):
    def test_artdmx_header(self):
        data = bytes(range(256)) * 2
        p = dmx.build_artdmx(0x1234, 7, data)
        self.assertEqual(len(p), 18 + 512)
        self.assertEqual(p[:8], b"Art-Net\x00")
        self.assertEqual(struct.unpack("<H", p[8:10])[0], 0x5000)
        self.assertEqual(struct.unpack(">H", p[10:12])[0], 14)
        self.assertEqual(p[12], 7)
        self.assertEqual(p[13], 0)
        self.assertEqual(p[14], 0x34)           # SubUni
        self.assertEqual(p[15], 0x12)           # Net
        self.assertEqual(struct.unpack(">H", p[16:18])[0], 512)
        self.assertEqual(p[18:], data)

    def test_sacn_layout(self):
        cid = uuid.uuid5(uuid.NAMESPACE_URL, "x").bytes
        data = bytes([5, 6, 7]) + bytes(509)
        p = dmx.build_sacn(cid, 300, 9, data)
        self.assertEqual(len(p), 638)
        self.assertEqual(struct.unpack(">H", p[0:2])[0], 0x0010)
        self.assertEqual(struct.unpack(">H", p[2:4])[0], 0)
        self.assertEqual(p[4:16], b"ASC-E1.17\x00\x00\x00")
        self.assertEqual(struct.unpack(">H", p[16:18])[0], 0x7000 | 622)
        self.assertEqual(struct.unpack(">I", p[18:22])[0], 4)
        self.assertEqual(p[22:38], cid)
        self.assertEqual(struct.unpack(">H", p[38:40])[0], 0x7000 | 600)
        self.assertEqual(struct.unpack(">I", p[40:44])[0], 2)
        self.assertEqual(p[44:108], b"Glowstage".ljust(64, b"\x00"))
        self.assertEqual(p[108], 100)
        self.assertEqual(struct.unpack(">H", p[109:111])[0], 0)
        self.assertEqual(p[111], 9)
        self.assertEqual(p[112], 0)
        self.assertEqual(struct.unpack(">H", p[113:115])[0], 300)
        self.assertEqual(struct.unpack(">H", p[115:117])[0], 0x7000 | 523)
        self.assertEqual(p[117], 0x02)
        self.assertEqual(p[118], 0xA1)
        self.assertEqual(struct.unpack(">HHH", p[119:125]), (0, 1, 513))
        self.assertEqual(p[125], 0)             # start code
        self.assertEqual(p[126:], data)

    def test_cid_stable_per_hub(self):
        a = make_hub("sacn", 1)
        b = make_hub("sacn", 1)
        c = make_hub("sacn", 2)
        self.assertEqual(a.cid, b.cid)
        self.assertNotEqual(a.cid, c.cid)
        self.assertEqual(len(a.cid), 16)


class FixtureTests(unittest.TestCase):
    def test_rgb_brightness_and_fixed(self):
        hub = make_hub(fixtures=[{"id": "f1", "name": "Head", "start": 10, "pixels": 1, "layout": "RGB",
                                  "fixed": {"6": 255, "5": 7}}])
        f = fixture(hub)
        f.connect()
        self.assertTrue(f.online)
        self.assertEqual(f.reported_name, "Head")
        self.assertEqual(f.pixel_count, 1)
        f.show_color((200, 100, 50), LightState(brightness=50))
        buf = hub.snapshot()
        self.assertEqual(list(buf[9:12]), [100, 50, 25])
        self.assertEqual(buf[9 + 4], 7)
        self.assertEqual(buf[9 + 5], 255)
        self.assertEqual(f.extra_info()["label"], "DMX · universe 0 · channel 10-15")

    def test_grb_order(self):
        hub = make_hub(fixtures=[{"id": "a", "name": "A", "start": 1, "pixels": 2, "layout": "GRB"}])
        f = fixture(hub)
        f.send_frame([(1, 2, 3), (4, 5, 6)])
        self.assertEqual(list(hub.snapshot()[:6]), [2, 1, 3, 5, 4, 6])

    def test_rgbw_extraction(self):
        hub = make_hub(fixtures=[{"id": "a", "name": "A", "start": 1, "pixels": 1, "layout": "RGBW"}])
        f = fixture(hub)
        f.show_color((200, 150, 100), LightState())
        self.assertEqual(list(hub.snapshot()[:4]), [100, 50, 0, 100])

    def test_drgb_dimmer_and_drgbw(self):
        hub = make_hub(fixtures=[
            {"id": "a", "name": "A", "start": 1, "pixels": 1, "layout": "DRGB"},
            {"id": "b", "name": "B", "start": 11, "pixels": 1, "layout": "DRGBW"}])
        a, b = list(hub.devices.values())
        a.show_color((10, 20, 30), LightState())
        b.show_color((10, 20, 30), LightState())
        buf = hub.snapshot()
        self.assertEqual(list(buf[:4]), [255, 10, 20, 30])
        self.assertEqual(list(buf[10:15]), [255, 0, 10, 20, 10])

    def test_multi_pixel_short_frame_and_cutoff(self):
        hub = make_hub(fixtures=[{"id": "a", "name": "A", "start": 511, "pixels": 3, "layout": "RGB"}])
        f = fixture(hub)
        f.send_frame([(1, 2, 3)])
        buf = hub.snapshot()
        self.assertEqual(len(buf), 512)
        self.assertEqual(list(buf[510:512]), [1, 2])

    def test_power_off_zeroes_only_colour_channels(self):
        hub = make_hub(fixtures=[{"id": "a", "name": "A", "start": 1, "pixels": 1, "layout": "RGB",
                                  "fixed": {"4": 200}}])
        f = fixture(hub)
        f.show_color((255, 255, 255), LightState())
        f.power_off()
        self.assertEqual(list(hub.snapshot()[:4]), [0, 0, 0, 200])
        f.power_off(timeout=1.0)

    def test_apply_off_and_on(self):
        hub = make_hub(fixtures=[{"id": "a", "name": "A", "start": 1, "pixels": 1, "layout": "RGB"}])
        f = fixture(hub)
        f.apply(LightState(on=True, color="#ff0000"))
        self.assertEqual(list(hub.snapshot()[:3]), [255, 0, 0])
        f.apply(LightState(on=False))
        self.assertEqual(list(hub.snapshot()[:3]), [0, 0, 0])

    def test_set_fixtures_rebuilds(self):
        hub = make_hub(fixtures=[{"id": "a", "name": "A", "start": 1, "pixels": 1}])
        hub.set_fixtures([{"id": "x", "name": "X", "start": 5, "pixels": 1}, {"id": "y", "name": "Y"}])
        self.assertEqual(len(hub.devices), 2)
        self.assertEqual(hub.entry["fixtures"][0]["id"], "x")


class SenderTests(unittest.TestCase):
    def _receive(self, protocol, universe):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(rx.close)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(3.0)
        hub = make_hub(protocol, universe, host="127.0.0.1", port=rx.getsockname()[1],
                       fixtures=[{"id": "a", "name": "A", "start": 3, "pixels": 1, "layout": "RGB"}])
        self.addCleanup(hub.close)
        hub.connect()
        self.assertTrue(hub.online)
        f = list(hub.devices.values())[0]
        f.connect()
        f.show_color((9, 8, 7), LightState())
        packets = [rx.recv(1024) for _ in range(3)]
        return hub, packets

    def test_artnet_sender(self):
        hub, packets = self._receive("artnet", 5)
        p = packets[-1]
        self.assertEqual(len(p), 530)
        self.assertEqual(p[:8], b"Art-Net\x00")
        self.assertEqual(p[14], 5)
        seqs = [q[12] for q in packets]
        self.assertTrue(all(1 <= s <= 255 for s in seqs))
        self.assertEqual(list(p[18 + 2:18 + 5]), [9, 8, 7])

    def test_sacn_sender(self):
        hub, packets = self._receive("sacn", 7)
        p = packets[-1]
        self.assertEqual(len(p), 638)
        self.assertEqual(p[22:38], hub.cid)
        self.assertEqual(struct.unpack(">H", p[113:115])[0], 7)
        self.assertEqual(list(p[126 + 2:126 + 5]), [9, 8, 7])

    def test_close_stops_thread(self):
        hub = make_hub(host="127.0.0.1", port=9)
        hub.connect()
        thread = hub._thread
        hub.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(hub.online)

    def test_multicast_destination(self):
        hub = make_hub("sacn", 0x0102)
        hub.connect()
        try:
            self.assertEqual(hub._dest, ("239.255.1.2", 5568))
        finally:
            hub.close()


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.driver = dmx.Driver()

    def test_describe(self):
        d = self.driver.describe()
        self.assertEqual((d["kind"], d["hub"], d["beta"], d["pairing"]), ("dmx", True, True, ""))

    def test_probe_ok(self):
        f = self.driver.probe({"protocol": "sacn", "host": "10.0.0.5", "universe": "3"})
        self.assertEqual(f.dev_id, "dmx:sacn:10.0.0.5:3")
        self.assertEqual(f.port, 5568)
        self.assertEqual(f.label, "DMX universe 3")
        self.assertEqual(f.extra, {"protocol": "sacn", "universe": 3, "fixtures": []})
        g = self.driver.probe({"protocol": "artnet", "host": "", "universe": "0"})
        self.assertEqual((g.dev_id, g.port), ("dmx:artnet:broadcast:0", 6454))

    def test_probe_errors(self):
        bad = [{"protocol": "dmx"}, {"protocol": "artnet", "host": "nope"},
               {"protocol": "artnet", "universe": "32768"}, {"protocol": "artnet", "universe": "-1"},
               {"protocol": "sacn", "universe": "0"}, {"protocol": "sacn", "universe": "64000"},
               {"protocol": "artnet", "universe": "abc"}]
        for values in bad:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    self.driver.probe(values)

    def test_create_does_no_io(self):
        hub = self.driver.create("dmx:artnet:broadcast:0", {"kind": "dmx", "fixtures": []})
        self.assertIsInstance(hub, dmx.DmxHub)
        self.assertIsNone(hub._sock)

    def test_parse_artpoll_reply(self):
        data = bytearray(239)
        data[:8] = b"Art-Net\x00"
        data[8:10] = struct.pack("<H", 0x2100)
        data[10:14] = bytes((192, 168, 1, 60))
        data[26:26 + 7] = b"NodeOne"
        f = dmx.parse_artpoll_reply(bytes(data))
        self.assertEqual((f.kind, f.dev_id, f.host, f.port, f.label),
                         ("dmx", "dmx:artnet:192.168.1.60:0", "192.168.1.60", 6454, "NodeOne"))
        self.assertEqual(f.extra["protocol"], "artnet")
        data[26:44] = bytes(18)
        self.assertEqual(dmx.parse_artpoll_reply(bytes(data)).label, "Art-Net node")
        data[8:10] = struct.pack("<H", 0x2000)
        self.assertIsNone(dmx.parse_artpoll_reply(bytes(data)))
        self.assertIsNone(dmx.parse_artpoll_reply(b"junk"))

    def test_scan_never_raises(self):
        self.assertIsInstance(self.driver.scan(0.05), list)


if __name__ == "__main__":
    unittest.main()
