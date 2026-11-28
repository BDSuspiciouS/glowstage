"""Sleep timer: the lights that are on fade out over the time you choose, like a sunset, and
switch off at the end (from the tray or Home).

The fading isn't saved: if Glowstage or Windows stops halfway, the lights come back at their own
brightness. When you step away meanwhile (away.py) the lights it was fading go off at their own
brightness too. At the end they switch off and get their own brightness back, so next time they come
on as they were. A light you (or a schedule, a preset, anything) change meanwhile is yours again:
the timer leaves it alone. Cancelling puts the brightness back.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

CHOICES = (15, 30, 60, 90)    # minutes, as offered
STEP = 15.0                   # seconds between two steps down
LOWEST = 3                    # % the lights reach just before they switch off


class SleepTimer:
    def __init__(self, manager):
        self.manager = manager
        self.started = 0.0          # time.time() when it started, 0 when no timer runs
        self.until = 0.0            # ... when the lights go off
        self._lights: dict[str, list[int]] = {}   # id -> [brightness before, change count after our last step]
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self.until > 0

    def describe(self) -> dict | None:
        if not self.active:
            return None
        return {"started": self.started, "until": self.until, "lights": len(self._lights)}

    def start(self, minutes: int) -> None:
        minutes = int(minutes)
        if not 1 <= minutes <= 240:
            raise ValueError("Choose between 1 minute and 4 hours")
        m = self.manager
        m.wake.cancel(put_back=False)   # a sleep timer and a wake-up don't run at once
        m.cycle.cancel()                # nor does it go on switching presets under the fade
        with self._lock:
            if self.active:             # a new time: begin again from the lights as they were
                self._put_back()
            with m.lock:
                ids = [i for i in m.visible_ids() if i in m.states and m.states[i].on]
                before = {i: m.states[i].brightness for i in ids}
            if not ids:
                raise ValueError("Switch some lights on first")
            self._lights = {i: [before[i], m.touches(i)] for i in ids}
            self.started = time.time()
            self.until = self.started + minutes * 60
            log.info("Sleep timer: %d lights off in %d min", len(ids), minutes)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="sleep-timer", daemon=True)
            self._thread.start()
        self._wake.set()
        m._changed()

    def cancel(self) -> None:
        with self._lock:
            if not self.active:
                return
            self._put_back()
            self._end("cancelled")

    def release(self, ids) -> dict[str, int]:
        """Stop fading these lights (something else takes them over, like away.py): their own
        brightness, for the ones the timer was fading."""
        with self._lock:
            own = {i: self._lights.pop(i)[0] for i in list(ids) if i in self._lights}
            if own and not self._lights:
                self._end("the lights were switched off while you were away")
            return own

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(STEP)
            self._wake.clear()
            try:
                if not self.tick(time.time()):
                    return
            except Exception:
                log.exception("Sleep timer step failed")

    def tick(self, now: float) -> bool:
        """One step down (or the end). False once no timer runs."""
        m = self.manager
        with self._lock:
            if not self.active:
                return False
            # A light changed by anything else since our last step is yours again.
            self._lights = {i: v for i, v in self._lights.items() if i in m.states and m.touches(i) == v[1]}
            if not self._lights:
                self._end("every light was changed by hand")
                return False
            preset = m.config["settings"].get("active_preset", "")
            if now >= self.until:
                m._apply({i: {"on": False, "brightness": b} for i, (b, _) in self._lights.items()}, preset=preset)
                self._end("lights off")
                return False
            left = (self.until - now) / (self.until - self.started)
            patches = {}
            for i, (b, _) in self._lights.items():
                target = max(min(b, LOWEST), round(LOWEST + (b - LOWEST) * left))
                if m.states[i].brightness != target:
                    patches[i] = {"brightness": target}
            if patches:
                m._apply(patches, preset=preset, save=False)
                for i in patches:
                    self._lights[i][1] = m.touches(i)
            return True

    def _put_back(self) -> None:
        m = self.manager
        back = {i: {"brightness": b} for i, (b, n) in self._lights.items() if i in m.states and m.touches(i) == n}
        if back:
            m._apply(back, preset=m.config["settings"].get("active_preset", ""))

    def _end(self, why: str) -> None:
        log.info("Sleep timer over: %s", why)
        self.started = self.until = 0.0
        self._lights = {}
        self.manager._changed()
