import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from glowstage.devices import elgato
from glowstage.state import LightState

COLOR_LIGHT = {"on": 0, "brightness": 10, "hue": 120, "saturation": 50}
WHITE_LIGHT = {"on": 0, "brightness": 10, "temperature": 200}


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeElgato:
    def __init__(self, light=COLOR_LIGHT, product="Elgato Light Strip", serial="BW12K1A00001"):
        self.light = dict(light)
        self.info = {"productName": product, "serialNumber": serial, "displayName": "Desk strip",
                     "firmwareVersion": "1.0.3", "features": ["lights"]}
        self.puts: list[tuple[int, dict]] = []   # (client port, body)
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/elgato/accessory-info":
                    self._send(fake.info)
                elif self.path == "/elgato/lights":
                    self._send({"numberOfLights": 1, "lights": [fake.light]})
                else:
                    self.send_error(404)

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    fake.puts.append((self.client_address[1], body))
                    fake.light.update(body["lights"][0])
                self._send({"numberOfLights": 1, "lights": [fake.light]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def bodies(self):
        with self.lock:
            return [b for _, b in self.puts]


def light_body(**light):
    return {"numberOfLights": 1, "lights": [light]}


class ElgatoBase(unittest.TestCase):
    def start(self, light=COLOR_LIGHT, **kw):
        fake = FakeElgato(light, **kw)
        self.addCleanup(fake.close)
        dev = elgato.ElgatoDevice("elgato:x", "Elgato", "127.0.0.1", fake.port)
        self.addCleanup(dev._stop_sender)
        return fake, dev


class DeviceTests(ElgatoBase):
    def test_connect_color_light(self):
        fake, dev = self.start()
        dev.connect()
        self.assertTrue(dev.online)
        self.assertEqual(dev.pixel_count, 1)
        self.assertEqual(dev.reported_name, "Desk strip")
        self.assertTrue(dev._color)
        self.assertEqual(dev.extra_info(), {"host": "127.0.0.1", "label": "Elgato · Elgato Light Strip"})
        self.assertEqual(dev.read_state(), {"on": False, "brightness": 10})

    def test_connect_white_light(self):
        fake, dev = self.start(WHITE_LIGHT, product="Elgato Key Light")
        dev.connect()
        self.assertFalse(dev._color)

    def test_connect_unreachable_raises(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(Exception):
            elgato.ElgatoDevice("elgato:x", "E", "127.0.0.1", port).connect()

    def test_show_color_on_color_light(self):
        fake, dev = self.start()
        dev.connect()
        dev.show_color((0, 255, 0), LightState(brightness=80))
        self.assertEqual(fake.bodies(), [light_body(on=1, brightness=80, hue=120, saturation=100)])

    def test_show_color_scales_brightness_by_value(self):
        fake, dev = self.start()
        dev.connect()
        dev.show_color((128, 0, 0), LightState(brightness=100))   # v = 0.502
        body = fake.bodies()[0]["lights"][0]
        self.assertEqual((body["brightness"], body["hue"], body["saturation"]), (50, 0, 100))

    def test_show_color_on_white_light_is_brightness_only(self):
        fake, dev = self.start(WHITE_LIGHT)
        dev.connect()
        dev.show_color((255, 0, 0), LightState(brightness=65))
        self.assertEqual(fake.bodies(), [light_body(on=1, brightness=65)])

    def test_power_off(self):
        fake, dev = self.start()
        dev.apply(LightState(on=False))
        self.assertEqual(fake.bodies(), [light_body(on=0)])


class StreamTests(ElgatoBase):
    def test_streaming_collapses_frames_over_one_connection(self):
        fake, dev = self.start()
        dev.connect()
        dev.apply(LightState(mode="effect"))
        self.assertEqual(fake.bodies(), [light_body(on=1)])   # begin_stream turns it on
        for i in range(1, 60):
            dev.send_frame([(i * 4, 0, 0), (i * 4, 0, 0)])    # last one: (236, 0, 0)
        self.assertTrue(wait_for(lambda: fake.bodies()[-1].get("lights", [{}])[0].get("brightness") == 93))
        stream = fake.puts[1:]
        self.assertLessEqual(len(stream), 4)                   # 59 frames collapsed
        self.assertEqual(stream[-1][1], light_body(on=1, brightness=93, hue=0, saturation=100))
        self.assertEqual(len({port for port, _ in stream}), 1)  # one persistent connection

    def test_rate_is_limited_to_ten_a_second(self):
        fake, dev = self.start()
        dev.connect()
        dev.begin_stream(LightState())
        start = time.monotonic()
        n = 0
        while time.monotonic() - start < 0.55:
            n += 1
            dev.send_frame([(10 + n % 200, 0, 0)])
            time.sleep(0.005)
        time.sleep(0.15)
        self.assertLessEqual(len(fake.puts) - 1, 7)

    def test_frame_is_averaged_and_white_light_gets_brightness(self):
        fake, dev = self.start(WHITE_LIGHT)
        dev.connect()
        dev.begin_stream(LightState())
        dev.send_frame([(255, 255, 255), (0, 0, 0)])           # average 127 -> 50 %
        self.assertTrue(wait_for(lambda: len(fake.puts) >= 2))
        self.assertEqual(fake.bodies()[1], light_body(on=1, brightness=50))

    def test_black_frame_turns_off(self):
        fake, dev = self.start()
        dev.connect()
        dev.begin_stream(LightState())
        dev.send_frame([(0, 0, 0)])
        self.assertTrue(wait_for(lambda: len(fake.puts) >= 2))
        self.assertEqual(fake.bodies()[1], light_body(on=0))

    def test_end_stream_stops_the_sender(self):
        fake, dev = self.start()
        dev.connect()
        dev.begin_stream(LightState())
        thread = dev._thread
        dev.end_stream()
        self.assertFalse(thread.is_alive())
        dev.send_frame([(255, 0, 0)])   # ignored
        time.sleep(0.2)
        self.assertEqual(len(fake.puts), 1)


class DriverTests(unittest.TestCase):
    def test_describe(self):
        d = elgato.Driver().describe()
        self.assertEqual((d["kind"], d["title"], d["beta"]), ("elgato", "Elgato", True))
        self.assertEqual(elgato.Driver.mdns, ("_elg._tcp.local.",))

    def test_from_mdns_reads_accessory_info(self):
        fake = FakeElgato()
        self.addCleanup(fake.close)
        f = elgato.Driver().from_mdns("_elg._tcp.local.", "Elgato Key Light 1A2B", "127.0.0.1", fake.port, {})
        self.assertEqual((f.kind, f.dev_id, f.host, f.port, f.label),
                         ("elgato", "elgato:BW12K1A00001", "127.0.0.1", fake.port, "Desk strip"))
        self.assertEqual(f.extra, {"product": "Elgato Light Strip"})

    def test_from_mdns_unreachable_falls_back(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        f = elgato.Driver().from_mdns("_elg._tcp.local.", "Elgato Key Light", "127.0.0.1", port, {"id": "AA:BB"})
        self.assertEqual((f.dev_id, f.label), ("elgato:AA:BB", "Elgato Key Light"))

    def test_probe(self):
        fake = FakeElgato()
        self.addCleanup(fake.close)
        f = elgato.Driver().probe({"host": f"127.0.0.1:{fake.port}"})
        self.assertEqual((f.dev_id, f.label), ("elgato:BW12K1A00001", "Desk strip"))

    def test_probe_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaisesRegex(ValueError, "No Elgato"):
            elgato.Driver().probe({"host": f"127.0.0.1:{port}"})

    def test_create(self):
        dev = elgato.Driver().create("elgato:1", {"host": "10.0.0.9", "port": 9123, "label": "Key", "product": "Elgato Key Light"})
        self.assertEqual((dev.host, dev.port, dev.max_fps, dev.group), ("10.0.0.9", 9123, 10, "room"))
        self.assertEqual(dev.extra_info()["label"], "Elgato · Elgato Key Light")


if __name__ == "__main__":
    unittest.main()
