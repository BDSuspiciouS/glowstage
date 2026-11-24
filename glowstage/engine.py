"""Renders app effects and streams frames to every device that is in effect mode."""

from __future__ import annotations

import logging
import threading
import time

from .audio import AudioAnalyzer
from .colors import scale
from .effects import EFFECTS, MUSIC_EFFECTS
from .genre import GenreDetector
from .screen import ScreenSampler
from .show import ShowDirector, stage_spans

log = logging.getLogger(__name__)

SHOW_FPS = 50   # the light show's chases need more frames than the other effects


class EffectEngine:
    def __init__(self, manager, fps: int = 30):
        self.manager = manager
        self.fps = fps
        self.screen = ScreenSampler()
        self.audio = AudioAnalyzer()
        self.show = ShowDirector(GenreDetector(self.audio))
        self.show_active = False
        self._epoch = time.monotonic()
        self._mem: dict[tuple[str, str], dict] = {}
        self._last: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="effects", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

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
        if any(st.effect == "screen" for _, st in targets):
            ctx["screen"] = self.screen.request()
        if any(st.effect in MUSIC_EFFECTS for _, st in targets):
            ctx["audio"] = self.audio.request()  # one snapshot per frame keeps every light on the same beat
        if show:  # one show for the whole room; its intensity is the speed of the lights running it
            settings = self.manager.config["settings"]
            intensity = sum(st.speed for st in show) / len(show) / 100
            ctx["show"] = self.show.update(ctx["audio"], now, intensity, settings.get("show_strobe", True))
            spans = stage_spans(list(self.manager.devices.values()), self.manager.config.data.get("show_layout"))
        for dev, st in targets:
            if now - self._last.get(dev.id, 0.0) < 1 / dev.max_fps - 0.004:
                continue
            ctx["span"] = spans.get(dev.id, (0.0, 1.0, "front"))
            ctx["fixture"] = getattr(dev, "fixture", None)
            effect = EFFECTS.get(st.effect) or EFFECTS["rainbow"]
            colors = effect.render(t, dev.pixel_count, st, self._mem.setdefault((dev.id, effect.id), {}), ctx)
            if not dev.hardware_brightness and st.brightness < 100:
                f = st.brightness / 100
                colors = [scale(c, f) for c in colors]
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
