import base64
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from glowstage.devices import twinkly
from glowstage.state import LightState


def token_for(n: int) -> str:
    return base64.b64encode(b"TOKEN%03d" % n).decode()   # 8 bytes, like the real ones


class FakeTwinkly:
    def __init__(self, leds=3, profile="RGB"):
        self.gestalt = {"device_name": "Garland", "number_of_led": leds, "led_profile": profile,
                        "mac": "aa:bb:cc:dd:ee:ff", "product_code": "TWS250STP"}
        self.calls: list[tuple[str, str, str | None, object]] = []   # method, path, token header, body
        self.logins = 0
        self.valid: str | None = None
        self._pending: str | None = None
        self.challenge_ok = True
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status, obj=None):
                body = json.dumps(obj if obj is not None else {"code": 1000}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                fake.calls.append(("GET", self.path, self.headers.get("X-Auth-Token"), None))
                if self.path == "/xled/v1/gestalt":
                    self._reply(200, fake.gestalt)
                else:
                    self._reply(404)

            def do_POST(self):
                body = self._body()
                token = self.headers.get("X-Auth-Token")
                fake.calls.append(("POST", self.path, token, body))
                if self.path == "/xled/v1/login":
                    if len(base64.b64decode(body["challenge"])) != 32:
                        fake.challenge_ok = False
                    fake.logins += 1
                    fake._pending = token_for(fake.logins)
                    self._reply(200, {"authentication_token": fake._pending,
                                      "challenge-response": f"resp{fake.logins}", "code": 1000})
                elif self.path == "/xled/v1/verify":
                    if token == fake._pending and body == {"challenge-response": f"resp{fake.logins}"}:
                        fake.valid = token
                        self._reply(200)
                    else:
                        self._reply(401)
                elif token is None or token != fake.valid:
                    self._reply(401)
                else:
                    self._reply(200)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def expire(self):
        self.valid = None

    def after_connect(self):
        return [c for c in self.calls if c[1] not in ("/xled/v1/login", "/xled/v1/verify", "/xled/v1/gestalt")]


class TwinklyBase(unittest.TestCase):
    def start(self, **kw):
        fake = FakeTwinkly(**kw)
        self.addCleanup(fake.close)
        dev = twinkly.TwinklyDevice("twinkly:127.0.0.1", "Twinkly", "127.0.0.1", fake.port)
        return fake, dev

    def udp_receiver(self, dev):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        self.addCleanup(rx.close)
        dev.udp_port = rx.getsockname()[1]
        return rx


class DiscoveryTests(unittest.TestCase):
    ANSWER = b"\xef\x01\xa8\xc0" + b"OK" + b"Twinkly_AB12\x00\x00"

    def test_parse(self):
        f = twinkly.parse_discover(self.ANSWER)
        self.assertEqual((f.kind, f.dev_id, f.host, f.port, f.label),
                         ("twinkly", "twinkly:192.168.1.239", "192.168.1.239", 80, "Twinkly_AB12"))

    def test_parse_rejects_other_packets(self):
        self.assertIsNone(twinkly.parse_discover(b"\x01discover"))
        self.assertIsNone(twinkly.parse_discover(b"\xef\x01\xa8\xc0NO"))
        self.assertIsNone(twinkly.parse_discover(b""))

    def test_discover_with_fake_responder(self):
        resp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        resp.bind(("127.0.0.1", 0))
        resp.settimeout(3)

        def answer():
            data, addr = resp.recvfrom(100)
            self.assertEqual(data, b"\x01discover")
            resp.sendto(self.ANSWER, addr)

        t = threading.Thread(target=answer, daemon=True)
        t.start()
        try:
            found = twinkly.discover(0.6, ("127.0.0.1", resp.getsockname()[1]))
        finally:
            t.join(2)
            resp.close()
        self.assertEqual([f.dev_id for f in found], ["twinkly:192.168.1.239"])

    def test_scan_never_raises(self):
        orig = twinkly.discover
        twinkly.discover = lambda *a, **k: (_ for _ in ()).throw(OSError("no network"))
        try:
            self.assertEqual(twinkly.Driver().scan(0.1), [])
        finally:
            twinkly.discover = orig


class LoginTests(TwinklyBase):
    def test_login_verify_gestalt(self):
        fake, dev = self.start(leds=250)
        dev.connect()
        login, verify, gestalt = fake.calls
        self.assertEqual((login[0], login[1], login[2]), ("POST", "/xled/v1/login", None))
        self.assertTrue(fake.challenge_ok)
        self.assertEqual(list(login[3]), ["challenge"])
        self.assertEqual((verify[1], verify[2], verify[3]),
                         ("/xled/v1/verify", token_for(1), {"challenge-response": "resp1"}))
        self.assertEqual((gestalt[0], gestalt[1], gestalt[2]), ("GET", "/xled/v1/gestalt", token_for(1)))
        self.assertTrue(dev.online)
        self.assertEqual(dev.pixel_count, 250)
        self.assertEqual(dev.reported_name, "Garland")
        self.assertEqual(dev.extra_info(), {"host": "127.0.0.1", "label": "Twinkly · TWS250STP"})

    def test_connect_unreachable_raises(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        dev = twinkly.TwinklyDevice("twinkly:x", "T", "127.0.0.1", port)
        with self.assertRaises(Exception):
            dev.connect()
        self.assertFalse(dev.online)

    def test_relogin_on_401(self):
        fake, dev = self.start()
        dev.connect()
        fake.expire()   # token timed out on the light
        dev.power_off()
        self.assertEqual(fake.logins, 2)
        self.assertEqual(fake.after_connect()[-2:][0][2], token_for(1))   # first try with the old token: 401
        last = fake.calls[-1]
        self.assertEqual((last[1], last[2], last[3]), ("/xled/v1/led/mode", token_for(2), {"mode": "off"}))
        dev.power_off()   # and the new token keeps working without another login
        self.assertEqual(fake.logins, 2)


class ControlTests(TwinklyBase):
    def test_static_color_calls(self):
        fake, dev = self.start()
        dev.connect()
        dev.show_color((10, 20, 30), LightState(brightness=70))
        self.assertEqual([(m, p, t, b) for m, p, t, b in fake.after_connect()], [
            ("POST", "/xled/v1/led/mode", token_for(1), {"mode": "color"}),
            ("POST", "/xled/v1/led/color", token_for(1), {"red": 10, "green": 20, "blue": 30}),
            ("POST", "/xled/v1/led/out/brightness", token_for(1), {"mode": "enabled", "type": "A", "value": 70}),
        ])

    def test_power_off(self):
        fake, dev = self.start()
        dev.connect()
        dev.apply(LightState(on=False))
        self.assertEqual(fake.after_connect(), [("POST", "/xled/v1/led/mode", token_for(1), {"mode": "off"})])

    def test_begin_stream_sets_rt_mode(self):
        fake, dev = self.start()
        dev.connect()
        dev.apply(LightState(mode="effect"))
        calls = fake.after_connect()
        self.assertIn(("POST", "/xled/v1/led/mode", token_for(1), {"mode": "rt"}), calls)
        self.assertEqual(calls[-1][3], {"mode": "rt"})   # rt comes last
        self.assertTrue(dev.streaming)


class UdpTests(TwinklyBase):
    HEADER = b"\x03" + b"TOKEN001" + b"\x00\x00"

    def test_single_packet_bytes(self):
        fake, dev = self.start(leds=3)
        dev.connect()
        rx = self.udp_receiver(dev)
        dev.send_frame([(1, 2, 3), (4, 5, 6), (255, 0, 128)])
        data, _ = rx.recvfrom(2048)
        self.assertEqual(data, self.HEADER + b"\x00" + bytes([1, 2, 3, 4, 5, 6, 255, 0, 128]))

    def test_fragments_over_300_leds(self):
        fake, dev = self.start(leds=700)
        dev.connect()
        rx = self.udp_receiver(dev)
        colors = [(i % 256, (i * 3) % 256, (i * 7) % 256) for i in range(700)]
        dev.send_frame(colors)
        packets = [rx.recvfrom(4096)[0] for _ in range(3)]
        self.assertEqual([len(p) for p in packets], [12 + 900, 12 + 900, 12 + 300])
        self.assertEqual([p[11] for p in packets], [0, 1, 2])
        self.assertTrue(all(p[:11] == self.HEADER for p in packets))
        data = b"".join(p[12:] for p in packets)
        self.assertEqual(data, bytes(c for rgb in colors for c in rgb))

    def test_exactly_300_leds_is_one_packet(self):
        fake, dev = self.start(leds=300)
        dev.connect()
        rx = self.udp_receiver(dev)
        dev.send_frame([(1, 1, 1)] * 300)
        self.assertEqual(len(rx.recvfrom(4096)[0]), 12 + 900)
        rx.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            rx.recvfrom(4096)

    def test_rgbw_profile(self):
        fake, dev = self.start(leds=2, profile="RGBW")
        dev.connect()
        rx = self.udp_receiver(dev)
        dev.send_frame([(1, 2, 3), (4, 5, 6)])
        data, _ = rx.recvfrom(2048)
        self.assertEqual(data, self.HEADER + b"\x00" + bytes([0, 1, 2, 3, 0, 4, 5, 6]))

    def test_rgbw_fragments_at_225_leds(self):
        fake, dev = self.start(leds=226, profile="RGBW")
        dev.connect()
        rx = self.udp_receiver(dev)
        dev.send_frame([(9, 8, 7)] * 226)
        packets = [rx.recvfrom(4096)[0] for _ in range(2)]
        self.assertEqual([len(p) for p in packets], [12 + 900, 12 + 4])

    def test_packet_uses_new_token_after_relogin(self):
        fake, dev = self.start(leds=1)
        dev.connect()
        fake.expire()
        dev.power_off()
        rx = self.udp_receiver(dev)
        dev.send_frame([(0, 0, 0)])
        data, _ = rx.recvfrom(100)
        self.assertEqual(data[1:9], b"TOKEN002")


class DriverTests(TwinklyBase):
    def test_describe(self):
        d = twinkly.Driver().describe()
        self.assertEqual((d["kind"], d["title"], d["beta"]), ("twinkly", "Twinkly", True))

    def test_probe_without_login(self):
        fake, _ = self.start()
        f = twinkly.Driver().probe({"host": f"127.0.0.1:{fake.port}"})
        self.assertEqual((f.dev_id, f.host, f.port, f.label), ("twinkly:127.0.0.1", "127.0.0.1", fake.port, "Garland"))
        self.assertEqual(f.extra["mac"], "aa:bb:cc:dd:ee:ff")

    def test_probe_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaisesRegex(ValueError, "No Twinkly"):
            twinkly.Driver().probe({"host": f"127.0.0.1:{port}"})

    def test_create(self):
        dev = twinkly.Driver().create("twinkly:1.2.3.4", {"host": "1.2.3.4", "port": 80, "label": "Tree"})
        self.assertEqual((dev.host, dev.port, dev.name, dev.max_fps, dev.group), ("1.2.3.4", 80, "Tree", 25, "room"))


if __name__ == "__main__":
    unittest.main()
