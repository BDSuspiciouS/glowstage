"""Renders app effects and streams frames to every device that is in effect mode."""

from __future__ import annotations

import collections
import logging
import threading
import time

from .audio import AudioAnalyzer
from .colors import scale
from .effects import EFFECTS, GAME_EFFECTS, HEARING_EFFECTS, SCREEN_EFFECTS
from .genre import DiscogsDetector
from .keys import KeyWatcher
from .screen import ScreenSampler
from .show import SLOW_FPS, ShowDirector, stage_spans

log = logging.getLogger(__name__)

SHOW_FPS = 50   # the light show's chases need more frames than the other effects


class EffectEngine:
    def __init__(self, manager, fps: int = 30):
        self.manager = manager
        self.fps = fps
        self.screen = ScreenSampler()
        self.audio = AudioAnalyzer()
        self.keys = KeyWatcher()
        self.show = ShowDirector(DiscogsDetector(self.audio))
        self.show_active = False
        self._epoch = time.monotonic()
        self._mem: dict[tuple[str, str], dict] = {}
        self._last: dict[str, float] = {}
        self._live: dict[str, tuple[float, list]] = {}   # id -> (time, last frame), for the stage view
        self._heard: collections.deque[tuple[float, dict]] = collections.deque(maxlen=40)  # for the sync delay
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="effects", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def live(self, samples: int = 24) -> dict[str, list[str]]:
        """The colors each streaming light shows right now, a few samples along it (for the UI's
        stage view). Lights that aren't running an app effect aren't in it."""
        now = time.monotonic()
        out = {}
        for dev_id, (at, colors) in list(self._live.items()):
            if now - at > 0.6:
                self._live.pop(dev_id, None)
                continue
            n = len(colors)
            if not n:
                continue
            k = min(n, samples)
            picks = [colors[min(n - 1, int((i + 0.5) * n / k))] for i in range(k)]
            out[dev_id] = ["#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c[:3]) for c in picks]
        return out

    def _delayed(self, now: float, heard: dict, sync: float) -> dict:
        """The music analysis as it was `sync` seconds ago, for the music effects: with Bluetooth
        you hear the sound that much later than the PC plays it. (The light show delays its own
        frames: its beat clock keeps running on the live sound.)"""
        if sync <= 0:
            self._heard.clear()
            return heard
        self._heard.append((now, heard))
        while len(self._heard) > 1 and self._heard[1][0] <= now - sync + 0.01:   # (to the nearest frame)
            self._heard.popleft()
        return self._heard[0][1]

    def _run(self) -> None:
        # Paced with time.sleep, which is exact to ~0.1 ms; Event.wait rounds to Windows' 15.6 ms
        # timer ticks, which made frames come unevenly and movement stutter.
        next_at = time.monotonic()
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                self._frame(start)
            except Exception:
                log.exception("effect frame failed")
            next_at += 1 / (SHOW_FPS if self.show_active else self.fps)
            now = time.monotonic()
            if next_at < now - 0.1:  # fell far behind (sleep/hibernate): start a fresh schedule
                next_at = now
            time.sleep(max(0.0, next_at - now))

    def _frame(self, now: float) -> None:
        targets = self.manager.stream_targets()
        show = [st for _, st in targets if st.effect == "music_show"]
        self.show_active = bool(show)
        if not targets:
            return
        t = now - self._epoch
        ctx = {}
        spans = {}
        screen = any(st.effect in SCREEN_EFFECTS for _, st in targets)
        if screen:
            ctx["screen"] = self.screen.request()
        game = any(st.effect in GAME_EFFECTS for _, st in targets)
        if game:
            ctx["game"] = self.manager.games.cs2.snapshot(now)
        if any(st.effect == "typing" for _, st in targets):
            ctx["keys"] = self.keys.recent(now)
        if any(st.effect in HEARING_EFFECTS for _, st in targets):
            heard = self.audio.request()  # one snapshot per frame keeps every light on the same beat
            sync = self.manager.show_sync()["ms"] / 1000
            ctx["audio"] = self._delayed(now, heard, sync)
            if show:  # one show for the whole room; its intensity is the speed of the lights running it
                settings = self.manager.config["settings"]
                intensity = sum(st.speed for st in show) / len(show) / 100
                ctx["show"] = self.show.update(heard, now, intensity, settings.get("show_strobe", True), sync)
        if show or screen or game:   # where each light sits on the stage
            spans = stage_spans(list(self.manager.devices.values()), self.manager.config.data.get("show_layout"))
        for dev, st in targets:
            due = now - self._last.get(dev.id, 0.0) >= 1 / dev.max_fps - 0.004
            # Slow lights in the show are drawn every frame (only sent when due), so that their
            # hold catches the flashes that fall between two of their updates.
            slow = st.effect == "music_show" and dev.max_fps <= SLOW_FPS
            if not due and not slow:
                continue
            ctx["span"] = spans.get(dev.id, (0.0, 1.0, "front"))
            ctx["fixture"] = getattr(dev, "fixture", None)
            ctx["slow"] = slow
            effect = EFFECTS.get(st.effect) or EFFECTS["rainbow"]
            colors = effect.render(t, dev.pixel_count, st, self._mem.setdefault((dev.id, effect.id), {}), ctx)
            if not dev.hardware_brightness and st.brightness < 100:
                f = st.brightness / 100
                colors = [scale(c, f) for c in colors]
            self._live[dev.id] = (now, colors)
            if not due:
                continue
            # Never stall the whole frame on a device that is busy with a slow HTTP call.
            if not dev.lock.acquire(blocking=False):
                continue
            try:
                if dev.streaming and dev.online:
                    dev.send_frame(colors)
                    self._last[dev.id] = now
            except Exception as e:
                self.manager.report_error(dev, e)
            finally:
                dev.lock.release()
