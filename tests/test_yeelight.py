import json
import socket
import socketserver
import threading
import time
import unittest

from glowstage.devices import yeelight
from glowstage.state import LightState

SSDP_ANSWER = (
    b"HTTP/1.1 200 OK\r\nCache-Control: max-age=3584\r\nDate: \r\nExt: \r\n"
    b"Location: yeelight://192.168.1.239:55443\r\nServer: POSIX UPnP/1.0 YGLC/1\r\n"
    b"id: 0x000000000015243f\r\nmodel: color\r\nfw_ver: 18\r\n"
    b"support: get_prop set_default set_power toggle set_bright start_cf stop_cf set_scene "
    b"cron_add cron_get cron_del set_ct_abx set_rgb set_music\r\n"
    b"power: on\r\nbright: 100\r\ncolor_mode: 2\r\nct: 4000\r\nrgb: 16711680\r\nhue: 100\r\nsat: 35\r\n"
    b"name: Desk\r\n"
)

COLOR_SUPPORT = "get_prop set_power toggle set_bright set_ct_abx set_rgb set_music"
WHITE_SUPPORT = "get_prop set_power toggle set_bright set_ct_abx"


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeBulb:
    """A bulb on 127.0.0.1: records every control line and connects back for music mode."""

    def __init__(self, props=("on", "50", "16711680", "Desk"), notify=False):
        self.props = list(props)
        self.notify = notify
        self.lines: list[bytes] = []
        self.music_lines: list[bytes] = []
        self.music_closed = threading.Event()
        self.lock = threading.Lock()
        bulb = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    bulb._on_line(raw, self.wfile)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def commands(self):
        with self.lock:
            return [json.loads(line) for line in self.lines]

    def _on_line(self, raw, out):
        with self.lock:
            self.lines.append(raw)
        msg = json.loads(raw)
        result = ["ok"]
        if msg["method"] == "get_prop":
            result = self.props
        if msg["method"] == "set_music" and msg["params"][0] == 1:
            threading.Thread(target=self._music, args=(msg["params"][1], msg["params"][2]), daemon=True).start()
        if self.notify:
            out.write(b'{"method":"props","params":{"power":"on"}}\r\n')
        out.write(json.dumps({"id": msg["id"], "result": result}).encode() + b"\r\n")

    def _music(self, ip, port):
        s = socket.create_connection((ip, port), timeout=3)
        try:
            for line in s.makefile("rb"):
                with self.lock:
                    self.music_lines.append(line)
        finally:
            s.close()
            self.music_closed.set()


class SsdpTests(unittest.TestCase):
    def test_parse_answer(self):
        f = yeelight.parse_ssdp(SSDP_ANSWER)
        self.assertEqual((f.kind, f.dev_id, f.host, f.port), ("yeelight", "yeelight:0x000000000015243f",
                                                              "192.168.1.239", 55443))
        self.assertEqual(f.label, "Desk")
        self.assertEqual(f.extra["model"], "color")
        self.assertIn("set_rgb", f.extra["support"])

    def test_label_falls_back_to_model(self):
        f = yeelight.parse_ssdp(SSDP_ANSWER.replace(b"name: Desk\r\n", b"name: \r\n"))
        self.assertEqual(f.label, "color")

    def test_ignores_other_answers(self):
        self.assertIsNone(yeelight.parse_ssdp(b"HTTP/1.1 200 OK\r\nLocation: http://192.168.1.2/\r\n"))
        self.assertIsNone(yeelight.parse_ssdp(b"garbage"))

    def test_discover_with_fake_responder(self):
        resp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        resp.bind(("127.0.0.1", 0))
        resp.settimeout(3)

        def answer():
            data, addr = resp.recvfrom(2048)
            self.assertIn(b"ST: wifi_bulb", data)
            self.assertTrue(data.startswith(b"M-SEARCH * HTTP/1.1\r\n"))
            resp.sendto(SSDP_ANSWER, addr)

        t = threading.Thread(target=answer, daemon=True)
        t.start()
        try:
            found = yeelight.discover(0.6, ("127.0.0.1", resp.getsockname()[1]))
        finally:
            t.join(2)
            resp.close()
        self.assertEqual([f.dev_id for f in found], ["yeelight:0x000000000015243f"])

    def test_scan_never_raises(self):
        orig = yeelight.discover
        yeelight.discover = lambda *a, **k: (_ for _ in ()).throw(OSError("no network"))
        try:
            self.assertEqual(yeelight.Driver().scan(0.1), [])
        finally:
            yeelight.discover = orig


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.bulb = FakeBulb()
        self.addCleanup(self.bulb.close)

    def device(self, support=COLOR_SUPPORT):
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", self.bulb.port, "color", support)
        dev.moment = 0                      # (at once: the glide is tested apart)
        return dev

    def test_connect_sends_get_prop_line(self):
        dev = self.device()
        dev.connect()
        self.assertEqual(self.bulb.lines, [b'{"id":1,"method":"get_prop","params":["power","bright","rgb","name"]}\r\n'])
        self.assertTrue(dev.online)
        self.assertEqual(dev.pixel_count, 1)
        self.assertEqual(dev.reported_name, "Desk")
        self.assertEqual(dev.read_state(), {"on": True, "brightness": 50})

    def test_connect_skips_notifications(self):
        self.bulb.notify = True
        dev = self.device()
        dev.connect()
        self.assertEqual(dev.reported_name, "Desk")

    def test_connect_unreachable_raises(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", port)
        with self.assertRaises(OSError):
            dev.connect()
        self.assertFalse(dev.online)

    def test_color_detected_from_rgb_when_support_unknown(self):
        self.bulb.props = ["on", "50", "", "Desk"]
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", self.bulb.port)
        dev.connect()
        dev.show_color((255, 0, 0), LightState(brightness=40))
        self.assertEqual([c["method"] for c in self.bulb.commands()], ["get_prop", "set_power", "set_bright"])

    def test_show_color_exact_lines(self):
        dev = self.device()
        dev.show_color((255, 128, 0), LightState(brightness=80))
        self.assertEqual(self.bulb.lines, [
            b'{"id":1,"method":"set_power","params":["on","sudden",0]}\r\n',
            b'{"id":2,"method":"set_rgb","params":[16744448,"sudden",0]}\r\n',
            b'{"id":3,"method":"set_bright","params":[80,"sudden",0]}\r\n',
        ])

    def test_white_only_gets_brightness_only(self):
        dev = self.device(WHITE_SUPPORT)
        dev.show_color((255, 128, 0), LightState(brightness=30))
        self.assertEqual([c["method"] for c in self.bulb.commands()], ["set_power", "set_bright"])
        self.assertEqual(self.bulb.commands()[1]["params"], [30, "sudden", 0])

    def test_a_glide_is_smooth(self):
        dev = self.device()
        dev.moment = 2.0
        dev.show_color((255, 128, 0), LightState(brightness=80))
        self.assertTrue(all(c["params"][-2:] == ["smooth", 2000] for c in self.bulb.commands()))

    def test_power_off_via_apply(self):
        dev = self.device()
        dev.apply(LightState(on=False))
        self.assertEqual(self.bulb.lines, [b'{"id":1,"method":"set_power","params":["off","sudden",0]}\r\n'])

    def test_error_reply_raises(self):
        class Err(FakeBulb):
            def _on_line(self, raw, out):
                msg = json.loads(raw)
                out.write(json.dumps({"id": msg["id"], "error": {"code": -1, "message": "client quota exceeded"}}).encode() + b"\r\n")

        bulb = Err()
        self.addCleanup(bulb.close)
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", bulb.port, support=COLOR_SUPPORT)
        with self.assertRaisesRegex(RuntimeError, "quota"):
            dev.show_color((1, 2, 3), LightState())


class MusicModeTests(unittest.TestCase):
    def setUp(self):
        self.bulb = FakeBulb()
        self.addCleanup(self.bulb.close)

    def test_music_mode_stream(self):
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", self.bulb.port, support=COLOR_SUPPORT)
        dev.begin_stream(LightState(brightness=60, mode="effect"))
        self.addCleanup(dev._close_music)
        cmds = self.bulb.commands()
        self.assertEqual([c["method"] for c in cmds], ["set_power", "set_bright", "set_music"])
        self.assertEqual(cmds[1]["params"], [60, "sudden", 0])
        self.assertEqual(cmds[2]["params"][:2], [1, "127.0.0.1"])
        self.assertIsInstance(cmds[2]["params"][2], int)

        dev.send_frame([(255, 0, 0)])
        dev.send_frame([(255, 0, 0)])   # unchanged: not sent again
        dev.send_frame([(0, 255, 0)])
        self.assertTrue(wait_for(lambda: len(self.bulb.music_lines) >= 2))
        time.sleep(0.1)
        lines = [json.loads(x) for x in self.bulb.music_lines]
        self.assertEqual([(x["method"], x["params"]) for x in lines], [
            ("set_rgb", [16711680, "sudden", 0]),
            ("set_rgb", [65280, "sudden", 0]),
        ])
        self.assertTrue(all(x.endswith(b"\r\n") for x in self.bulb.music_lines))

        dev.end_stream()
        self.assertTrue(self.bulb.music_closed.wait(3))
        last = self.bulb.commands()[-1]
        self.assertEqual((last["method"], last["params"]), ("set_music", [0]))

    def test_white_light_streams_brightness(self):
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", self.bulb.port, support=WHITE_SUPPORT)
        dev.begin_stream(LightState(brightness=50, mode="effect"))
        self.addCleanup(dev._close_music)
        dev.send_frame([(255, 255, 255)])
        dev.send_frame([(128, 128, 128)])
        self.assertTrue(wait_for(lambda: len(self.bulb.music_lines) >= 2))
        lines = [json.loads(x) for x in self.bulb.music_lines]
        self.assertEqual([(x["method"], x["params"][0]) for x in lines], [("set_bright", 50), ("set_bright", 25)])

    def test_apply_effect_then_color_ends_music(self):
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", self.bulb.port, support=COLOR_SUPPORT)
        dev.apply(LightState(mode="effect"))
        self.assertTrue(dev.streaming)
        dev.apply(LightState(mode="color", color="#0000ff"))
        self.assertTrue(self.bulb.music_closed.wait(3))
        methods = [c["method"] for c in self.bulb.commands()]
        self.assertEqual(methods[-4:], ["set_music", "set_power", "set_rgb", "set_bright"])

    def test_no_connect_back_raises(self):
        class Silent(FakeBulb):
            def _music(self, ip, port):
                pass

        bulb = Silent()
        self.addCleanup(bulb.close)
        dev = yeelight.YeelightDevice("yeelight:x", "Lamp", "127.0.0.1", bulb.port, support=COLOR_SUPPORT)
        with self.assertRaises(ConnectionError):   # waits the full 3 s for the light to connect back
            dev.begin_stream(LightState())


class DriverTests(unittest.TestCase):
    def test_describe(self):
        d = yeelight.Driver().describe()
        self.assertEqual((d["kind"], d["title"], d["beta"]), ("yeelight", "Yeelight", True))
        self.assertIn("LAN Control", d["pairing"])
        self.assertEqual(yeelight.Driver().pair(None), {})

    def test_create_and_extra_info(self):
        dev = yeelight.Driver().create("yeelight:1", {"host": "10.0.0.5", "port": 55443, "label": "Desk",
                                                      "model": "strip1", "support": WHITE_SUPPORT})
        self.assertEqual(dev.host, "10.0.0.5")
        self.assertEqual(dev.extra_info(), {"host": "10.0.0.5", "label": "Yeelight · strip1"})
        self.assertEqual(dev.group, "room")

    def test_probe_by_tcp(self):
        bulb = FakeBulb()
        self.addCleanup(bulb.close)
        f = yeelight.Driver().probe({"host": f"127.0.0.1:{bulb.port}"})
        self.assertEqual((f.dev_id, f.host, f.port, f.label), ("yeelight:127.0.0.1", "127.0.0.1", bulb.port, "Desk"))

    def test_probe_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaisesRegex(ValueError, "LAN Control"):
            yeelight.Driver().probe({"host": f"127.0.0.1:{port}"})


if __name__ == "__main__":
    unittest.main()
