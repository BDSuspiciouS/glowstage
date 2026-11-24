"""Magic Home / Flux LED: discovery parsing, exact command bytes and checksums, status, reconnects.

Everything runs against fake controllers on 127.0.0.1 with ephemeral ports; no real network is used.
"""

import socket
import threading
import time
import unittest

from glowstage.devices import magichome
from glowstage.devices.registry import Found
from glowstage.state import LightState

REPLY = b"192.168.1.50,ACCF235FFFFF,AK001-ZJ200"


def status_bytes(on=True, rgb=(1, 2, 3)) -> bytes:
    body = bytes([0x81, 0x04, 0x23 if on else 0x24, 0x61, 0x21, 0x10, *rgb, 0x00, 0x04, 0x00, 0xF0])
    return body + bytes([sum(body) & 0xFF])


class FakeController:
    """A TCP controller: parses commands by their opcode, records them, answers the status query."""

    LENGTHS = {0x31: 8, 0x71: 4, 0x81: 4}

    def __init__(self, status=True, answer_status=True):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.server.settimeout(0.1)
        self.port = self.server.getsockname()[1]
        self.status = status_bytes() if status is True else status
        self.answer_status = answer_status
        self.commands: list[tuple[int, bytes]] = []    # (connection number, command bytes)
        self.conns: list[socket.socket] = []
        self._cond = threading.Condition()
        self._stop = False
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._cond:
                self.conns.append(conn)
                number = len(self.conns)
            thread = threading.Thread(target=self._serve, args=(conn, number), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, conn, number):
        conn.settimeout(0.1)
        buf = b""
        while not self._stop:
            try:
                chunk = conn.recv(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while buf and len(buf) >= self.LENGTHS.get(buf[0], len(buf)):
                size = self.LENGTHS.get(buf[0], len(buf))
                command, buf = buf[:size], buf[size:]
                with self._cond:
                    self.commands.append((number, command))
                    self._cond.notify_all()
                if command[0] == 0x81 and self.answer_status:
                    try:
                        conn.sendall(self.status)
                    except OSError:
                        return

    def wait(self, count, timeout=2.0):
        end = time.monotonic() + timeout
        with self._cond:
            while len(self.commands) < count and time.monotonic() < end:
                self._cond.wait(0.05)
            return list(self.commands)

    def only_bytes(self, count, timeout=2.0):
        return [c for _, c in self.wait(count, timeout)]

    def drop_connections(self):
        """Close every connection from the controller's side, like a controller that timed out."""
        with self._cond:
            conns, self.conns_dropped = list(self.conns), True
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop = True
        for conn in list(self.conns):
            conn.close()
        self.server.close()
        for thread in self._threads:
            thread.join(0.5)


class MagicHomeTestCase(unittest.TestCase):
    def controller(self, **kwargs):
        ctl = FakeController(**kwargs)
        self.addCleanup(ctl.close)
        return ctl

    def device(self, ctl, model="AK001-ZJ200"):
        dev = magichome.MagicHomeDevice("magichome:accf235fffff", "Magic Home", "127.0.0.1", ctl.port, model)
        self.addCleanup(dev._close)
        return dev


class TestProtocol(unittest.TestCase):
    def test_checksums(self):
        self.assertEqual(magichome.POWER_ON, bytes([0x71, 0x23, 0x0F, 0xA3]))
        self.assertEqual(magichome.POWER_OFF, bytes([0x71, 0x24, 0x0F, 0xA4]))
        self.assertEqual(magichome.STATUS_QUERY, bytes([0x81, 0x8A, 0x8B, 0x96]))
        cmd = magichome.color_command((255, 0, 128))
        self.assertEqual(cmd, bytes([0x31, 255, 0, 128, 0x00, 0x00, 0x0F, (0x31 + 255 + 128 + 0x0F) & 0xFF]))
        self.assertEqual(len(cmd), 8)

    def test_color_command_clamps(self):
        self.assertEqual(magichome.color_command((300, -5, 7))[1:4], bytes([255, 0, 7]))

    def test_every_command_ends_with_its_checksum(self):
        for cmd in (magichome.POWER_ON, magichome.POWER_OFF, magichome.STATUS_QUERY,
                    magichome.color_command((12, 34, 56))):
            self.assertEqual(cmd[-1], sum(cmd[:-1]) & 0xFF)

    def test_parse_status(self):
        self.assertEqual(magichome.parse_status(status_bytes(True, (10, 20, 30))), {"on": True, "rgb": (10, 20, 30)})
        self.assertFalse(magichome.parse_status(status_bytes(False))["on"])
        self.assertIsNone(magichome.parse_status(b"\x81\x04"))
        self.assertIsNone(magichome.parse_status(b"\x00" * 14))

    def test_parse_discovery_reply(self):
        self.assertEqual(magichome.parse_discovery_reply(REPLY),
                         Found("magichome", "magichome:accf235fffff", "192.168.1.50", 5577,
                               "AK001-ZJ200", {"model": "AK001-ZJ200"}))

    def test_discovery_garbage_is_ignored(self):
        for raw in (b"", b"HF-A11ASSISTHREAD", b"hello,world,x", b"192.168.1.50,ZZZZ,x", b"999,ACCF235FFFFF,x",
                    b"\xff\xfe"):
            self.assertIsNone(magichome.parse_discovery_reply(raw))

    def test_driver_metadata(self):
        driver = magichome.Driver()
        self.assertEqual((driver.kind, driver.title, driver.beta, driver.pairing), ("magichome", "Magic Home", True, ""))


class TestDiscovery(MagicHomeTestCase):
    def fake_responder(self, answer=REPLY):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.1)
        seen = []
        stop = threading.Event()

        def run():
            while not stop.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    return
                seen.append(data)
                sock.sendto(answer, addr)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def cleanup():
            stop.set()
            thread.join(1)
            sock.close()
        self.addCleanup(cleanup)
        return sock.getsockname()[1], seen

    def test_scan(self):
        port, seen = self.fake_responder()
        found = magichome.Driver(discovery_target=("127.0.0.1", port)).scan(0.5)
        self.assertEqual([f.dev_id for f in found], ["magichome:accf235fffff"])
        self.assertEqual((found[0].host, found[0].port, found[0].label), ("192.168.1.50", 5577, "AK001-ZJ200"))
        self.assertEqual(seen[0], b"HF-A11ASSISTHREAD")

    def test_scan_never_raises(self):
        self.assertEqual(magichome.Driver(discovery_target=("not an address", 1)).scan(0.1), [])

    def test_scan_ignores_garbage_replies(self):
        port, _ = self.fake_responder(b"not a controller")
        self.assertEqual(magichome.Driver(discovery_target=("127.0.0.1", port)).scan(0.3), [])

    def test_probe_ok_takes_id_from_discovery(self):
        ctl = self.controller()
        udp_port, _ = self.fake_responder()
        found = magichome.Driver(port=ctl.port, discovery_port=udp_port).probe({"host": "127.0.0.1"})
        self.assertEqual(found.dev_id, "magichome:accf235fffff")
        self.assertEqual((found.host, found.port), ("127.0.0.1", ctl.port))
        self.assertEqual(found.extra, {"model": "AK001-ZJ200"})
        self.assertEqual(ctl.only_bytes(1)[0], magichome.STATUS_QUERY)

    def test_probe_without_discovery_uses_host_as_id(self):
        ctl = self.controller()
        silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        silent.bind(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        found = magichome.Driver(port=ctl.port, discovery_port=silent.getsockname()[1]).probe({"host": "127.0.0.1"})
        self.assertEqual(found.dev_id, "magichome:127.0.0.1")

    def test_probe_unreachable(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        driver = magichome.Driver(port=port)
        with self.assertRaises(ValueError) as ctx:
            driver.probe({"host": "127.0.0.1"})
        self.assertIn("127.0.0.1", str(ctx.exception))
        with self.assertRaises(ValueError):
            driver.probe({"host": " "})

    def test_probe_not_a_magic_home(self):
        ctl = self.controller(answer_status=False)
        with self.assertRaises(ValueError):
            magichome.Driver(port=ctl.port).probe({"host": "127.0.0.1"})

    def test_create(self):
        dev = magichome.Driver().create("magichome:x", {"host": "10.0.0.9", "port": 5577, "model": "AK001-ZJ200"})
        self.assertEqual((dev.host, dev.port, dev.model, dev.kind, dev.group), ("10.0.0.9", 5577, "AK001-ZJ200",
                                                                                "magichome", "room"))
        self.assertEqual(dev.extra_info(), {"host": "10.0.0.9", "label": "Magic Home · AK001-ZJ200"})
        self.assertIsNone(dev._sock)   # create() does no network I/O


class TestControl(MagicHomeTestCase):
    def test_connect_and_read_state(self):
        ctl = self.controller(status=status_bytes(True, (0, 128, 0)))
        dev = self.device(ctl)
        dev.connect()
        self.assertTrue(dev.online)
        self.assertIsNone(dev.error)
        self.assertEqual(dev.pixel_count, 1)
        self.assertEqual(dev.reported_name, "Magic Home AK001-ZJ200")
        self.assertEqual(ctl.only_bytes(1), [magichome.STATUS_QUERY])
        self.assertEqual(dev.read_state(), {"on": True, "brightness": 50, "color": "#00ff00"})

    def test_connect_unreachable_raises(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        dev = magichome.MagicHomeDevice("magichome:x", "Magic Home", "127.0.0.1", port)
        with self.assertRaises(OSError):
            dev.connect()
        self.assertFalse(dev.online)

    def test_show_color_scales_by_brightness(self):
        ctl = self.controller()
        dev = self.device(ctl)
        dev.show_color((200, 100, 50), LightState(brightness=50))
        self.assertEqual(ctl.only_bytes(2), [magichome.POWER_ON, magichome.color_command((100, 50, 25))])

    def test_power_off(self):
        ctl = self.controller()
        dev = self.device(ctl)
        dev.apply(LightState(on=False))
        self.assertEqual(ctl.only_bytes(1), [magichome.POWER_OFF])

    def test_power_off_respects_timeout_when_unreachable(self):
        dev = magichome.MagicHomeDevice("magichome:x", "Magic Home", "127.0.0.1", 1)
        started = time.monotonic()
        with self.assertRaises(ConnectionError):
            dev.power_off(timeout=0.5)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_reconnects_after_the_controller_drops_the_connection(self):
        ctl = self.controller()
        dev = self.device(ctl)
        dev.show_color((10, 20, 30), LightState(brightness=100))
        self.assertEqual(len(ctl.wait(2)), 2)
        ctl.drop_connections()
        time.sleep(0.2)
        dev.show_color((40, 50, 60), LightState(brightness=100))
        sent = ctl.wait(4)
        self.assertEqual([c for _, c in sent[2:]], [magichome.POWER_ON, magichome.color_command((40, 50, 60))])
        self.assertEqual([n for n, _ in sent], [1, 1, 2, 2])

    def test_unsolicited_answers_do_not_pile_up(self):
        ctl = self.controller()
        dev = self.device(ctl)
        dev.connect()                       # leaves the connection open
        ctl.conns[0].sendall(b"\x31\x00\x00\x00")   # something the controller says unasked
        time.sleep(0.1)
        dev.show_color((1, 2, 3), LightState(brightness=100))
        self.assertEqual(len(ctl.wait(3)), 3)
        self.assertEqual({n for n, _ in ctl.commands}, {1})   # same connection, nothing broke


class TestStreaming(MagicHomeTestCase):
    def test_stream_sends_average_color(self):
        ctl = self.controller()
        dev = self.device(ctl)
        self.assertFalse(dev.hardware_brightness)
        self.assertEqual(dev.max_fps, 20)
        dev.apply(LightState(mode="effect"))
        self.assertTrue(dev.streaming)
        dev.send_frame([(100, 0, 0), (200, 0, 100)])
        dev.send_frame([(100, 0, 0), (200, 0, 100)])   # unchanged: not sent again
        dev.send_frame([(0, 0, 0), (10, 10, 10), (20, 20, 20)])
        self.assertEqual(ctl.only_bytes(3), [magichome.POWER_ON, magichome.color_command((150, 0, 50)),
                                              magichome.color_command((10, 10, 10))])
        time.sleep(0.2)
        self.assertEqual(len(ctl.commands), 3)

    def test_empty_frame_is_ignored(self):
        ctl = self.controller()
        dev = self.device(ctl)
        dev.send_frame([])
        time.sleep(0.1)
        self.assertEqual(ctl.commands, [])

    def test_frames_do_not_block_when_the_controller_is_unreachable(self):
        dev = magichome.MagicHomeDevice("magichome:x", "Magic Home", "127.0.0.1", 1)
        started = time.monotonic()
        for i in range(20):
            dev.send_frame([(i, 0, 0)])   # must neither raise nor stall
        self.assertLess(time.monotonic() - started, 0.5)

    def test_frames_resume_after_a_drop(self):
        ctl = self.controller()
        dev = self.device(ctl)
        dev.send_frame([(1, 1, 1)])
        ctl.wait(1)
        ctl.drop_connections()
        time.sleep(0.2)
        dev.send_frame([(2, 2, 2)])
        self.assertEqual(ctl.only_bytes(2)[1], magichome.color_command((2, 2, 2)))

    def test_busy_socket_drops_the_frame(self):
        ctl = self.controller()
        dev = self.device(ctl)
        started = time.monotonic()
        with dev._io:   # another thread is mid-conversation
            dev.send_frame([(5, 5, 5)])
        self.assertLess(time.monotonic() - started, 0.1)
        time.sleep(0.1)
        self.assertEqual(ctl.commands, [])
        dev.send_frame([(5, 5, 5)])   # the dropped frame is not remembered as sent
        self.assertEqual(ctl.only_bytes(1), [magichome.color_command((5, 5, 5))])


if __name__ == "__main__":
    unittest.main()
