"""Razer devices through Synapse's Chroma SDK: only ever against a fake SDK on 127.0.0.1."""

import json
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from glowstage import diagnostics
from glowstage.api import Api
from glowstage.colors import scale
from glowstage.config import Config
from glowstage.devices import razer_chroma
from glowstage.devices.razer_chroma import RazerChroma, bgr
from glowstage.manager import DeviceManager
from glowstage.paths import REPO
from glowstage.state import LightState

ALL = ["keyboard", "mouse", "headset", "mousepad", "keypad", "chromalink"]


class FakeSdk(ThreadingHTTPServer):
    """What Synapse serves: register, heartbeat, effects per type, end of session."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.registered: list[dict] = []
        self.heartbeats = 0
        self.effects: list[tuple[str, dict]] = []     # (type, body), in the order they came
        self.results: dict[str, int] = {}             # type -> the result code to answer (default 0)
        self.deleted = 0
        self.broken = False                           # answers 500 to everything, like a session that ended
        self.uri_host = "127.0.0.1"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/razer/chromasdk"

    def sent(self, kind: str) -> list[dict]:
        with self.lock:
            return [b for k, b in self.effects if k == kind]

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_POST(self):
        s, body = self.server, self._body()
        if s.broken:
            return self._answer(500, {})
        with s.lock:
            s.registered.append(body)
            n = len(s.registered)
        self._answer(200, {"sessionid": n, "uri": f"http://{s.uri_host}:{s.server_port}/chromasdk"})

    def do_PUT(self):
        s, body = self.server, self._body()
        if s.broken:
            return self._answer(500, {})
        kind = self.path.rsplit("/", 1)[-1]
        with s.lock:
            if kind == "heartbeat":
                s.heartbeats += 1
                return self._answer(200, {"tick": s.heartbeats})
            s.effects.append((kind, body))
            result = s.results.get(kind, 0)
        self._answer(200, {"result": result})

    def do_DELETE(self):
        with self.server.lock:
            self.server.deleted += 1
        self._answer(200, {"result": 0})


def wait_for(test, what, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if what():
            return
        time.sleep(0.01)
    test.fail("timed out waiting")


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSdk()
        self.addCleanup(self.sdk.close)
        self.changes = 0
        self.saved = 0
        self.settings = {"on": False, "types": ["keyboard", "mouse", "mousepad"]}
        self.made: list[RazerChroma] = []

    def provider(self, **kw) -> RazerChroma:
        def changed():
            self.changes += 1

        def saved():
            self.saved += 1

        kw.setdefault("base", self.sdk.base)
        kw.setdefault("heartbeat", 0.05)
        kw.setdefault("retry", 0.05)
        p = RazerChroma(self.settings, changed, saved, **kw)
        self.made.append(p)
        self.addCleanup(p.stop)
        p.start()
        return p

    def switch_on(self, p, types=None):
        p.set_options(True, types or self.settings["types"])
        wait_for(self, lambda: p.describe()["status"] == "connected")
        return p

    def light(self, p, kind):
        return p.devices[f"razer:{kind}"]


class Colors(unittest.TestCase):
    def test_the_sdk_wants_blue_green_red(self):
        self.assertEqual(bgr((255, 0, 0)), 0x0000FF)
        self.assertEqual(bgr((0, 0, 255)), 0xFF0000)
        self.assertEqual(bgr((1, 2, 3)), 0x030201)
        self.assertEqual(bgr((255, 255, 255)), 0xFFFFFF)

    def test_values_stay_in_range(self):
        self.assertEqual(bgr((300, -5, 0)), 0x0000FF)


class Registering(Base):
    def test_nothing_happens_while_off(self):
        p = self.provider()
        time.sleep(0.2)
        self.assertEqual(self.sdk.registered, [])
        self.assertEqual(p.devices, {})
        self.assertEqual(p.describe()["status"], "off")

    def test_switching_on_registers_a_session(self):
        p = self.switch_on(self.provider())
        self.assertEqual(len(self.sdk.registered), 1)
        hello = self.sdk.registered[0]
        self.assertEqual(hello["title"], "Glowstage")
        self.assertEqual(hello["author"], {"name": "Glowstage", "contact": f"https://github.com/{REPO}"})
        self.assertEqual(hello["category"], "application")
        self.assertEqual(sorted(hello["device_supported"]), sorted(ALL))
        self.assertEqual(p.describe()["text"], "Connected to Razer Synapse")

    def test_the_switch_is_saved(self):
        p = self.provider()
        p.set_options(True, ["keyboard"])
        self.assertEqual(self.settings, {"on": True, "types": ["keyboard"]})
        self.assertEqual(self.saved, 1)

    def test_the_ticked_types_become_lights_in_the_pc_group(self):
        p = self.switch_on(self.provider())
        self.assertEqual([d.name for d in p.devices.values()], ["Razer keyboard", "Razer mouse", "Razer mouse mat"])
        for d in p.devices.values():
            self.assertEqual((d.kind, d.group, d.online, d.error), ("razer", "pc", True, None))
        self.assertEqual({d.pixel_count for d in p.devices.values()}, {132, 63, 15})

    def test_any_type_can_be_ticked_with_the_pixel_counts_of_its_grid(self):
        p = self.switch_on(self.provider(), ALL)
        counts = {k: self.light(p, k).pixel_count for k in ALL}
        self.assertEqual(counts, {"keyboard": 22 * 6, "mouse": 9 * 7, "headset": 5, "mousepad": 15,
                                  "keypad": 4 * 5, "chromalink": 5})
        self.assertEqual([d.name for d in p.devices.values()], ["Razer keyboard", "Razer mouse", "Razer headset",
                         "Razer mouse mat", "Razer keypad", "Razer Chroma Link"])

    def test_the_default_types(self):
        self.assertEqual(razer_chroma.DEFAULT_TYPES, ["keyboard", "mouse", "mousepad"])
        p = self.provider()
        self.settings.pop("types")
        self.assertEqual(p.types(), ["keyboard", "mouse", "mousepad"])

    def test_an_unknown_type_and_an_empty_list_are_refused(self):
        p = self.provider()
        with self.assertRaises(ValueError):
            p.set_options(True, ["toaster"])
        with self.assertRaises(ValueError):
            p.set_options(True, [])
        p.set_options(False, [])         # (off with nothing ticked is fine)
        self.assertFalse(self.settings["on"])

    def test_changing_the_ticks_adds_and_removes_lights(self):
        p = self.switch_on(self.provider())
        p.set_options(True, ["keyboard", "headset"])
        wait_for(self, lambda: list(p.devices) == ["razer:keyboard", "razer:headset"]
                 and p.devices["razer:headset"].online)
        self.assertEqual(len(self.sdk.registered), 1)      # (the same session)

    def test_a_session_address_that_is_not_this_pc_is_not_used(self):
        self.sdk.uri_host = "example.com"
        p = self.provider()
        p.set_options(True, ["keyboard"])
        wait_for(self, lambda: p.describe()["status"] == "waiting")
        self.assertEqual(self.sdk.heartbeats, 0)


class Heartbeat(Base):
    def test_the_session_is_kept_alive(self):
        self.switch_on(self.provider())
        wait_for(self, lambda: self.sdk.heartbeats >= 3)

    def test_the_default_beat_is_well_inside_the_fifteen_seconds(self):
        self.assertLessEqual(razer_chroma.HEARTBEAT, 5)

    def test_a_session_that_ended_is_made_again(self):
        p = self.switch_on(self.provider())
        self.sdk.broken = True
        wait_for(self, lambda: p.describe()["status"] == "waiting")
        self.assertFalse(any(d.online for d in p.devices.values()))
        self.sdk.broken = False
        wait_for(self, lambda: p.describe()["status"] == "connected")
        self.assertEqual(len(self.sdk.registered), 2)
        self.assertTrue(all(d.online for d in p.devices.values()))


class StaticColors(Base):
    def test_a_plain_color_is_a_static_effect_in_bgr(self):
        p = self.switch_on(self.provider(), ALL)
        for kind in ALL:
            light = self.light(p, kind)
            light.show_color((255, 128, 0), LightState())
            light.wait_written(2)
            self.assertEqual(self.sdk.sent(kind)[-1], {"effect": "CHROMA_STATIC", "param": {"color": 0x0080FF}}, kind)

    def test_brightness_is_in_the_color(self):
        p = self.switch_on(self.provider())
        light = self.light(p, "keyboard")
        light.show_color((200, 100, 50), LightState().patched({"brightness": 50}))
        light.wait_written(2)
        self.assertEqual(self.sdk.sent("keyboard")[-1]["param"]["color"], bgr(scale((200, 100, 50), 0.5)))

    def test_off_is_none(self):
        p = self.switch_on(self.provider())
        light = self.light(p, "mouse")
        light.power_off()
        light.wait_written(2)
        self.assertEqual(self.sdk.sent("mouse")[-1], {"effect": "CHROMA_NONE"})


class Frames(Base):
    def test_every_type_gets_its_grid(self):
        p = self.switch_on(self.provider(), ALL)
        shapes = {}
        for kind in ALL:
            light = self.light(p, kind)
            light.send_frame([(10, 20, 30)] * light.pixel_count)
            light.wait_written(2)
            body = self.sdk.sent(kind)[-1]
            param = body["param"]
            shape = (len(param), len(param[0])) if isinstance(param[0], list) else (len(param),)
            shapes[kind] = (body["effect"], shape)
            flat = [v for row in param for v in row] if isinstance(param[0], list) else param
            self.assertEqual(set(flat), {bgr((10, 20, 30))}, kind)
        self.assertEqual(shapes, {
            "keyboard": ("CHROMA_CUSTOM", (6, 22)),
            "mouse": ("CHROMA_CUSTOM2", (9, 7)),
            "headset": ("CHROMA_CUSTOM", (5,)),
            "mousepad": ("CHROMA_CUSTOM", (15,)),
            "keypad": ("CHROMA_CUSTOM", (4, 5)),
            "chromalink": ("CHROMA_CUSTOM", (5,)),
        })

    def test_the_keyboard_is_filled_left_to_right(self):
        p = self.switch_on(self.provider())
        kb = self.light(p, "keyboard")
        # pixel i is column i // 6, row i % 6: an effect that sweeps sweeps across the columns
        frame = [(i % 256, 0, 0) for i in range(kb.pixel_count)]
        kb.send_frame(frame)
        kb.wait_written(2)
        grid = self.sdk.sent("keyboard")[-1]["param"]
        for r in range(6):
            for c in range(22):
                self.assertEqual(grid[r][c], bgr(frame[c * 6 + r]))
        # a sweep: the first pixels are the leftmost column, top to bottom
        self.assertEqual([grid[r][0] for r in range(6)], [bgr(frame[r]) for r in range(6)])

    def test_the_mouse_grid_is_nine_rows_of_seven(self):
        p = self.switch_on(self.provider())
        mouse = self.light(p, "mouse")
        frame = [(i, 0, 0) for i in range(mouse.pixel_count)]
        mouse.send_frame(frame)
        mouse.wait_written(2)
        grid = self.sdk.sent("mouse")[-1]["param"]
        self.assertEqual(grid[8][6], bgr(frame[6 * 9 + 8]))
        self.assertEqual(grid[0][0], bgr(frame[0]))

    def test_one_row_types_are_a_flat_list_in_order(self):
        p = self.switch_on(self.provider())
        mat = self.light(p, "mousepad")
        frame = [(i * 10, 0, 255 - i) for i in range(15)]
        mat.send_frame(frame)
        mat.wait_written(2)
        self.assertEqual(self.sdk.sent("mousepad")[-1]["param"], [bgr(c) for c in frame])

    def test_a_frame_of_the_wrong_size_is_dropped(self):
        p = self.switch_on(self.provider())
        mat = self.light(p, "mousepad")
        mat.send_frame([(1, 1, 1)] * 14)
        mat.wait_written(2)
        self.assertEqual(self.sdk.sent("mousepad"), [])

    def test_the_light_show_gets_where_the_keys_sit(self):
        p = self.switch_on(self.provider())
        geometry = self.light(p, "keyboard").fixture["geometry"]
        self.assertEqual(len(geometry), 132)
        self.assertEqual(geometry[0], (0.0, 0.0))
        self.assertEqual(geometry[-1], (1.0, 1.0))
        self.assertIsNone(self.light(p, "mousepad").fixture)

    def test_it_asks_for_what_the_sdk_takes(self):
        p = self.switch_on(self.provider())
        self.assertLessEqual(self.light(p, "keyboard").max_fps, 30)

    def test_the_desk_items_for_the_stage(self):
        p = self.switch_on(self.provider(), ALL)
        self.assertEqual({k: self.light(p, k).desk_item() for k in ALL}, {
            "keyboard": "KEYBOARD", "mouse": "MOUSE", "headset": "HEADSET", "mousepad": "MOUSEMAT",
            "keypad": "KEYPAD", "chromalink": ""})


class OnlyWhatChanged(Base):
    def test_the_same_frame_is_sent_once(self):
        p = self.switch_on(self.provider())
        mat = self.light(p, "mousepad")
        frame = [(5, 6, 7)] * 15
        for _ in range(4):
            mat.send_frame(list(frame))
            mat.wait_written(2)
        self.assertEqual(len(self.sdk.sent("mousepad")), 1)
        mat.send_frame([(9, 9, 9)] * 15)
        mat.wait_written(2)
        self.assertEqual(len(self.sdk.sent("mousepad")), 2)

    def test_only_the_newest_frame_goes_out(self):
        p = self.switch_on(self.provider())
        mat = self.light(p, "mousepad")
        for i in range(50):
            mat.send_frame([(i, 0, 0)] * 15)
        mat.wait_written(2)
        sent = self.sdk.sent("mousepad")
        self.assertLessEqual(len(sent), 50)
        self.assertEqual(sent[-1]["param"], [bgr((49, 0, 0))] * 15)

    def test_a_static_color_twice_is_sent_once(self):
        p = self.switch_on(self.provider())
        kb = self.light(p, "keyboard")
        for _ in range(3):
            kb.show_color((1, 2, 3), LightState())
            kb.wait_written(2)
        self.assertEqual(len(self.sdk.sent("keyboard")), 1)


class Refused(Base):
    def test_a_type_synapse_refuses_is_marked_as_not_answering(self):
        self.sdk.results["mouse"] = 1167
        p = self.switch_on(self.provider())
        mouse = self.light(p, "mouse")
        mouse.show_color((1, 2, 3), LightState())
        wait_for(self, lambda: not mouse.online)
        self.assertIn("doesn't see one", mouse.error)
        self.assertTrue(self.light(p, "keyboard").online)       # (the others carry on)
        self.assertGreater(self.changes, 0)

    def test_any_other_code_is_shown_too(self):
        self.sdk.results["keyboard"] = 5023
        p = self.switch_on(self.provider())
        kb = self.light(p, "keyboard")
        kb.send_frame([(1, 1, 1)] * 132)
        wait_for(self, lambda: not kb.online)
        self.assertIn("5023", kb.error)

    def test_a_refused_type_is_tried_again_later(self):
        self.sdk.results["mouse"] = 1167
        p = self.switch_on(self.provider(refused_retry=0.1))
        mouse = self.light(p, "mouse")
        mouse.show_color((1, 2, 3), LightState())
        wait_for(self, lambda: not mouse.online)
        self.sdk.results["mouse"] = 0
        wait_for(self, lambda: mouse.online)
        self.assertIsNone(mouse.error)
        mouse.show_color((1, 2, 3), LightState())
        mouse.wait_written(2)
        self.assertEqual(len(self.sdk.sent("mouse")), 2)       # (refused once, then taken)

    def test_an_offline_light_sends_nothing(self):
        self.sdk.results["mouse"] = 1167
        p = self.switch_on(self.provider())
        mouse = self.light(p, "mouse")
        mouse.show_color((1, 2, 3), LightState())
        wait_for(self, lambda: not mouse.online)
        mouse.show_color((4, 5, 6), LightState())
        mouse.wait_written(2)
        self.assertEqual(len(self.sdk.sent("mouse")), 1)


class SynapseNotRunning(Base):
    def test_a_refused_connection_is_a_clear_status(self):
        p = self.provider(base=f"http://127.0.0.1:{closed_port()}/razer/chromasdk")
        p.set_options(True, ["keyboard", "mouse"])
        wait_for(self, lambda: p.describe()["status"] == "waiting")
        d = p.describe()
        self.assertIn("Synapse isn't running", d["text"])
        self.assertEqual(d["lights"], 0)
        for light in p.devices.values():
            self.assertFalse(light.online)
            self.assertEqual(light.error, "Razer Synapse isn't running")
        self.assertEqual(p.summary(), "Synapse not reachable")

    def test_it_keeps_trying_and_connects_when_synapse_starts(self):
        port = closed_port()
        p = self.provider(base=f"http://127.0.0.1:{port}/razer/chromasdk")
        p.set_options(True, ["keyboard"])
        wait_for(self, lambda: p.describe()["status"] == "waiting")
        p.base = self.sdk.base         # (Synapse started)
        wait_for(self, lambda: p.describe()["status"] == "connected")
        self.assertTrue(self.light(p, "keyboard").online)

    def test_writing_to_a_light_without_a_session_does_nothing_bad(self):
        p = self.provider(base=f"http://127.0.0.1:{closed_port()}/razer/chromasdk")
        p.set_options(True, ["keyboard"])
        wait_for(self, lambda: p.describe()["status"] == "waiting")
        light = self.light(p, "keyboard")
        light.send_frame([(1, 1, 1)] * 132)
        self.assertTrue(light.wait_written(2))


class Stopping(Base):
    def test_switching_off_ends_the_session_and_removes_the_lights(self):
        p = self.switch_on(self.provider())
        p.set_options(False, ["keyboard", "mouse", "mousepad"])
        wait_for(self, lambda: self.sdk.deleted == 1)
        wait_for(self, lambda: p.devices == {})
        self.assertEqual(p.describe()["status"], "off")
        self.assertEqual(self.settings["types"], ["keyboard", "mouse", "mousepad"])    # (the ticks are kept)

    def test_it_can_be_switched_on_again(self):
        p = self.switch_on(self.provider())
        p.set_options(False, ["keyboard"])
        wait_for(self, lambda: self.sdk.deleted == 1)
        p.set_options(True, ["keyboard"])
        wait_for(self, lambda: p.describe()["status"] == "connected")
        self.assertEqual(len(self.sdk.registered), 2)
        self.assertTrue(self.light(p, "keyboard").online)

    def test_quitting_ends_the_session(self):
        p = self.switch_on(self.provider())
        p.stop()
        self.assertEqual(self.sdk.deleted, 1)

    def test_quitting_while_off_asks_synapse_nothing(self):
        p = self.provider()
        p.stop()
        self.assertEqual((self.sdk.deleted, self.sdk.registered), (0, []))

    def test_lights_written_before_quitting_get_out(self):
        p = self.switch_on(self.provider())
        mat = self.light(p, "mousepad")
        mat.power_off()
        self.assertTrue(mat.wait_written(2))
        self.assertEqual(self.sdk.sent("mousepad")[-1], {"effect": "CHROMA_NONE"})


class InTheApp(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSdk()
        self.addCleanup(self.sdk.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = DeviceManager(Config(Path(self.tmp.name) / "config.json"))
        self.addCleanup(self.m._stop.set)
        self.m.razer.base = self.sdk.base
        self.m.razer._heartbeat = self.m.razer._retry = 0.05
        self.m.razer.start()
        self.addCleanup(self.m.razer.stop)
        self.api = Api(SimpleNamespace(config=self.m.config, manager=self.m))

    def test_off_by_default_with_the_default_types(self):
        self.assertEqual(self.m.config["razer"], {"on": False, "types": ["keyboard", "mouse", "mousepad"]})
        self.assertEqual(self.m.live_status()["razer"]["status"], "off")
        self.assertFalse([d for d in self.m.devices.values() if d.kind == "razer"])

    def test_the_switch_brings_the_lights_to_home_and_they_take_colors(self):
        self.assertTrue(self.api.razer_set(True, ["keyboard", "mousepad"])["ok"])
        wait_for(self, lambda: {"razer:keyboard", "razer:mousepad"} <= set(self.m.devices)
                 and all(self.m.devices[i].online for i in ("razer:keyboard", "razer:mousepad")))
        snap = self.m.snapshot()
        mine = [d for d in snap["devices"] if d["kind"] == "razer"]
        self.assertEqual([(d["name"], d["group"], d["pixels"], d["online"]) for d in mine],
                         [("Razer keyboard", "pc", 132, True), ("Razer mouse mat", "pc", 15, True)])
        self.assertEqual(snap["razer"]["status"], "connected")
        self.m.workers["razer:mousepad"].submit(LightState().patched({"color": "#ff8000", "brightness": 100}))
        wait_for(self, lambda: self.sdk.sent("mousepad"))
        self.assertEqual(self.sdk.sent("mousepad")[-1]["param"]["color"], 0x0080FF)

    def test_an_effect_streams_frames_to_them(self):
        self.api.razer_set(True, ["mousepad"])
        wait_for(self, lambda: "razer:mousepad" in self.m.devices and self.m.devices["razer:mousepad"].online)
        dev = self.m.devices["razer:mousepad"]
        dev.send_frame([(0, 255, 0)] * 15)
        dev.wait_written(2)
        self.assertEqual(self.sdk.sent("mousepad")[-1]["param"], [bgr((0, 255, 0))] * 15)

    def test_switching_off_takes_them_out_and_gives_the_devices_back(self):
        self.api.razer_set(True, ["keyboard"])
        wait_for(self, lambda: "razer:keyboard" in self.m.devices)
        self.api.razer_set(False, ["keyboard"])
        wait_for(self, lambda: "razer:keyboard" not in self.m.devices)
        wait_for(self, lambda: self.sdk.deleted == 1)

    def test_quitting_the_app_ends_the_session(self):
        self.api.razer_set(True, ["keyboard"])
        wait_for(self, lambda: self.m.razer.describe()["status"] == "connected")
        self.m.stop()
        self.assertEqual(self.sdk.deleted, 1)

    def test_a_bad_choice_is_an_error_for_the_window(self):
        r = self.api.razer_set(True, [])
        self.assertFalse(r["ok"])
        self.assertIn("Tick at least one", r["error"])
        self.assertFalse(self.m.config["razer"]["on"])

    def test_synapse_not_running_shows_the_lights_as_not_answering(self):
        self.m.razer.base = f"http://127.0.0.1:{closed_port()}/razer/chromasdk"
        self.api.razer_set(True, ["keyboard"])
        wait_for(self, lambda: "razer:keyboard" in self.m.devices)
        wait_for(self, lambda: self.m.razer.describe()["status"] == "waiting")
        dev = [d for d in self.m.snapshot()["devices"] if d["id"] == "razer:keyboard"][0]
        self.assertEqual((dev["online"], dev["error"]), (False, "Razer Synapse isn't running"))

    def test_the_report_says_where_it_stands(self):
        app = SimpleNamespace(config=self.m.config, manager=self.m,
                              remote=SimpleNamespace(settings={"on": False}))
        self.assertIn("Razer Synapse: off", diagnostics.report(app))
        self.api.razer_set(True, ["keyboard", "mouse"])
        wait_for(self, lambda: self.m.razer.describe()["status"] == "connected")
        self.assertIn("Razer Synapse: connected", diagnostics.report(app))


if __name__ == "__main__":
    unittest.main()
