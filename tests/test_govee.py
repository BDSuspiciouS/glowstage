"""Govee LAN API: discovery parsing, the exact commands on the wire, status and streaming.

Everything runs against fake lights on 127.0.0.1 with ephemeral ports; no real network is used.
"""

import json
import socket
import threading
import time
import unittest

from glowstage.devices import govee
from glowstage.devices.registry import Found
from glowstage.state import LightState

SCAN_REPLY = json.dumps({"msg": {"cmd": "scan", "data": {
    "ip": "192.168.1.23", "device": "1F:80:C5:32:32:36:72:4E", "sku": "H618E",
    "bleVersionHard": "3.01.01", "bleVersionSoft": "1.03.01",
    "wifiVersionHard": "1.00.10", "wifiVersionSoft": "1.02.03"}}}).encode()


def status_reply(on=1, brightness=100, color=(1, 2, 3), kelvin=0) -> bytes:
    return json.dumps({"msg": {"cmd": "devStatus", "data": {
        "onOff": on, "brightness": brightness,
        "color": {"r": color[0], "g": color[1], "b": color[2]}, "colorTemInKelvin": kelvin}}}).encode()


def msg(cmd, data):
    return {"msg": {"cmd": cmd, "data": data}}


class FakeGovee:
    """A light: one UDP socket that records what it gets and answers scan / devStatus."""

    def __init__(self, reply_port, status=True, scan_reply=SCAN_REPLY):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.reply_port = reply_port
        self.status = status_reply() if status is True else status
        self.scan_reply = scan_reply
        self.raw: list[bytes] = []
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def messages(self):
        with self._cond:
            return [json.loads(r) for r in self.raw]

    def _run(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            with self._cond:
                self.raw.append(data)
                self._cond.notify_all()
            cmd = json.loads(data)["msg"]["cmd"]
            answer = self.status if cmd == "devStatus" else self.scan_reply if cmd == "scan" else None
            if answer:
                self.sock.sendto(answer, (addr[0], self.reply_port))

    def wait(self, count, timeout=2.0):
        end = time.monotonic() + timeout
        with self._cond:
            while len(self.raw) < count and time.monotonic() < end:
                self._cond.wait(0.05)
            return [json.loads(r) for r in self.raw]

    def close(self):
        self._stop = True
        self._thread.join(1)
        self.sock.close()


class NoListener(govee.Listener):
    def ensure(self):
        return False


class GoveeTestCase(unittest.TestCase):
    def setUp(self):
        self.listener = govee.Listener(port=0, host="127.0.0.1")
        self.assertTrue(self.listener.ensure())
        self.addCleanup(self.listener.close)

    def fake(self, **kwargs):
        light = FakeGovee(self.listener.port, **kwargs)
        self.addCleanup(light.close)
        return light

    def device(self, light, sku="H618E"):
        dev = govee.GoveeDevice("govee:1f80c5323236724e", "Govee", "127.0.0.1", light.port, sku, self.listener)
        self.addCleanup(dev._sock.close)
        return dev


class TestParsing(unittest.TestCase):
    def test_scan_reply(self):
        found = govee.parse_scan_reply(SCAN_REPLY)
        self.assertEqual(found, Found("govee", "govee:1f80c5323236724e", "192.168.1.23", 4003,
                                      "H618E", {"sku": "H618E"}))
        self.assertEqual(found.entry()["sku"], "H618E")

    def test_garbage_is_ignored(self):
        for raw in (b"", b"hello", b"{}", b'{"msg":{}}', status_reply(),
                    b'{"msg":{"cmd":"scan","data":{"device":"AA:BB"}}}'):
            self.assertIsNone(govee.parse_scan_reply(raw))

    def test_status_reply(self):
        self.assertEqual(govee.parse_status_reply(status_reply(brightness=40))["brightness"], 40)
        self.assertIsNone(govee.parse_status_reply(SCAN_REPLY))

    def test_scan_message_constant(self):
        self.assertEqual(json.loads(govee.SCAN_MESSAGE), msg("scan", {"account_topic": "reserve"}))

    def test_driver_metadata(self):
        driver = govee.Driver()
        self.assertEqual((driver.kind, driver.title, driver.beta), ("govee", "Govee", True))
        self.assertIn("LAN Control", driver.pairing)
        self.assertEqual(driver.pair(None), {})


class TestDiscovery(GoveeTestCase):
    def test_scan_finds_light(self):
        light = self.fake()
        driver = govee.Driver(self.listener, scan_target=("127.0.0.1", light.port))
        found = driver.scan(0.4)
        self.assertEqual([f.dev_id for f in found], ["govee:1f80c5323236724e"])
        self.assertEqual((found[0].host, found[0].port, found[0].label), ("192.168.1.23", 4003, "H618E"))
        self.assertEqual(light.wait(1)[0], json.loads(govee.SCAN_MESSAGE))

    def test_scan_never_raises(self):
        driver = govee.Driver(NoListener(), scan_target=("127.0.0.1", 9))
        self.assertEqual(driver.scan(0.1), [])
        broken = govee.Driver(self.listener, scan_target=("not an address", 1))
        self.assertEqual(broken.scan(0.1), [])

    def test_probe_uses_scan_answer(self):
        light = self.fake()
        driver = govee.Driver(self.listener, scan_port=light.port, control_port=light.port)
        found = driver.probe({"host": "127.0.0.1"})
        self.assertEqual(found.dev_id, "govee:1f80c5323236724e")
        self.assertEqual((found.host, found.port), ("127.0.0.1", light.port))

    def test_probe_falls_back_to_host_id(self):
        light = self.fake(scan_reply=None)
        driver = govee.Driver(self.listener, scan_port=light.port, control_port=light.port)
        self.assertEqual(driver.probe({"host": "127.0.0.1"}).dev_id, "govee:127.0.0.1")

    def test_probe_unreachable(self):
        light = self.fake(status=None, scan_reply=None)
        driver = govee.Driver(self.listener, scan_port=light.port, control_port=light.port)
        with self.assertRaises(ValueError) as ctx:
            driver.probe({"host": "127.0.0.1"})
        self.assertIn("LAN Control", str(ctx.exception))
        with self.assertRaises(ValueError):
            driver.probe({"host": ""})
        with self.assertRaises(ValueError):
            driver.probe({"host": "not-an-ip"})

    def test_create(self):
        dev = govee.Driver(self.listener).create("govee:x", {"host": "10.0.0.5", "port": 4003, "sku": "H6159"})
        self.addCleanup(dev._sock.close)
        self.assertEqual((dev.host, dev.port, dev.sku, dev.kind, dev.group), ("10.0.0.5", 4003, "H6159", "govee", "room"))
        self.assertEqual(dev.extra_info(), {"host": "10.0.0.5", "label": "Govee · H6159"})


class TestControl(GoveeTestCase):
    def test_show_color_exact_json(self):
        light = self.fake()
        dev = self.device(light)
        dev.show_color((10, 20, 30), LightState(brightness=40))
        self.assertEqual(light.wait(3), [
            msg("turn", {"value": 1}),
            msg("brightness", {"value": 40}),
            msg("colorwc", {"color": {"r": 10, "g": 20, "b": 30}, "colorTemInKelvin": 0}),
        ])

    def test_brightness_is_at_least_one(self):
        light = self.fake()
        self.device(light).show_color((1, 1, 1), LightState(brightness=0))
        self.assertEqual(light.wait(3)[1], msg("brightness", {"value": 1}))

    def test_wire_format_is_compact_json(self):
        light = self.fake()
        self.device(light).show_color((1, 2, 3), LightState(brightness=100))
        light.wait(3)
        self.assertEqual(light.raw[0], b'{"msg":{"cmd":"turn","data":{"value":1}}}')

    def test_power_off(self):
        light = self.fake()
        started = time.monotonic()
        self.device(light).power_off(timeout=1.0)
        self.assertLess(time.monotonic() - started, 1.0)
        sent = light.wait(1)
        self.assertEqual(sent[0], msg("turn", {"value": 0}))
        self.assertTrue(all(m == msg("turn", {"value": 0}) for m in light.wait(2, 0.5)))

    def test_apply_dispatches_through_device(self):
        light = self.fake()
        dev = self.device(light)
        dev.apply(LightState(on=False))
        self.assertEqual(light.wait(1)[0], msg("turn", {"value": 0}))


class TestStatus(GoveeTestCase):
    def test_connect_round_trip(self):
        light = self.fake(status=status_reply(on=1, brightness=64, color=(255, 128, 0)))
        dev = self.device(light)
        dev.connect()
        self.assertTrue(dev.online)
        self.assertIsNone(dev.error)
        self.assertEqual(dev.pixel_count, 1)
        self.assertEqual(dev.reported_name, "Govee H618E")
        self.assertEqual(light.wait(1)[0], msg("devStatus", {}))
        self.assertEqual(dev.read_state(), {"on": True, "brightness": 64, "color": "#ff8000"})

    def test_read_state_color_temperature(self):
        light = self.fake(status=status_reply(on=0, brightness=10, color=(0, 0, 0), kelvin=2700))
        dev = self.device(light)
        dev.connect()
        state = dev.read_state()
        self.assertFalse(state["on"])
        self.assertTrue(state["color"].startswith("#ff"))

    def test_connect_without_answer_fails(self):
        light = self.fake(status=None)
        dev = self.device(light)
        with self.assertRaises(TimeoutError):
            dev.connect()
        self.assertFalse(dev.online)

    def test_connect_succeeds_when_port_4002_is_unavailable(self):
        light = self.fake()
        dev = govee.GoveeDevice("govee:x", "Govee", "127.0.0.1", light.port, "H618E", NoListener())
        self.addCleanup(dev._sock.close)
        dev.connect()
        self.assertTrue(dev.online)
        self.assertEqual(dev.pixel_count, 1)
        self.assertIn("4002", dev.extra_info()["note"])
        self.assertIsNone(dev.read_state())


class TestStreaming(GoveeTestCase):
    def test_stream_sends_average_color(self):
        light = self.fake()
        dev = self.device(light)
        self.assertFalse(dev.hardware_brightness)
        self.assertEqual(dev.max_fps, 10)
        dev.apply(LightState(mode="effect", brightness=50))
        self.assertTrue(dev.streaming)
        dev.send_frame([(100, 0, 0), (200, 0, 100)])
        sent = light.wait(3)
        self.assertEqual(sent[0], msg("turn", {"value": 1}))
        self.assertEqual(sent[1], msg("brightness", {"value": 100}))
        self.assertEqual(sent[2], msg("colorwc", {"color": {"r": 150, "g": 0, "b": 50}, "colorTemInKelvin": 0}))

    def test_identical_frames_are_not_repeated_but_changes_are_sent(self):
        light = self.fake()
        dev = self.device(light)
        dev.begin_stream(LightState(mode="effect"))
        dev.send_frame([(9, 9, 9)])
        dev.send_frame([(9, 9, 9)])
        dev.send_frame([(8, 8, 8)])
        sent = light.wait(4)
        time.sleep(0.2)
        sent = light.messages
        self.assertEqual([m["msg"]["cmd"] for m in sent], ["turn", "brightness", "colorwc", "colorwc"])
        self.assertEqual(sent[3]["msg"]["data"]["color"], {"r": 8, "g": 8, "b": 8})

    def test_empty_frame_is_ignored(self):
        light = self.fake()
        dev = self.device(light)
        dev.send_frame([])
        time.sleep(0.2)
        self.assertEqual(light.messages, [])

    def test_leaving_the_stream_restores_normal_control(self):
        light = self.fake()
        dev = self.device(light)
        dev.apply(LightState(mode="effect"))
        dev.send_frame([(5, 5, 5)])
        dev.apply(LightState(mode="color", color="#102030", brightness=70))
        self.assertFalse(dev.streaming)
        sent = light.wait(6)
        self.assertEqual(sent[-1], msg("colorwc", {"color": {"r": 16, "g": 32, "b": 48}, "colorTemInKelvin": 0}))
        self.assertEqual(sent[-2], msg("brightness", {"value": 70}))


if __name__ == "__main__":
    unittest.main()
