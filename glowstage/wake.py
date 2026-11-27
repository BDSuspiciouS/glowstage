"""Wake-up light: the mirror image of the sleep timer. Over the time you choose your room lights
come up slowly like a sunrise, from a dim deep red to a bright warm white, and are fully light at
the time of the schedule.

On the way the lights show a sunrise but nothing is saved: if Glowstage or Windows stops halfway,
the lights come back as they were. At the end they stay on in warm white at full brightness and
that is saved, so you wake up to lights that stay on. A light you (or anything else) change meanwhile
is yours again: the wake-up leaves it alone. When every one of them is changed or switched off, the
wake-up is over.
"""

from __future__ import annotations

import logging
import threading
import time

from .colors import kelvin_to_rgb, lerp, rgb_to_hex

log = logging.getLogger(__name__)

CHOICES = (15, 30, 45)        # minutes, as offered
STEP = 6.0                    # seconds between two steps up
FIRST = (255, 58, 0)          # where the sunrise starts: a deep red-orange, at 1 %
AMBER = 1800                  # K, where the red has turned to amber
WARM = 3000                   # K, the warm white the lights end in
RED_PART = 0.15               # the first part of the time: from FIRST to amber
FINAL = rgb_to_hex(kelvin_to_rgb(WARM))


def wake_minutes(schedule: dict) -> int:
    """The minutes a schedule's wake-up starts before its time (0 for any other action)."""
    action = str(schedule.get("action", ""))
    return int(action[5:]) if action.startswith("wake:") and action[5:].isdigit() else 0


def look(progress: float) -> tuple[str, int]:
    """The color and brightness at `progress` (0 = start, 1 = done): red-orange at 1 %, amber,
    then warm white at 100 %. Brightness follows an ease-in curve: slow at first."""
    p = max(0.0, min(1.0, progress))
    if p < RED_PART:
        rgb = lerp(FIRST, kelvin_to_rgb(AMBER), p / RED_PART)
    else:
        rgb = kelvin_to_rgb(AMBER + (WARM - AMBER) * (p - RED_PART) / (1 - RED_PART))
    return rgb_to_hex(rgb), max(1, round(1 + 99 * p * p))


class WakeUp:
    def __init__(self, manager):
        self.manager = manager
        self.started = 0.0          # time.time() when it started, 0 when no wake-up runs
        self.until = 0.0            # ... when the lights are fully light
        self.minutes = 0
        self._lights: dict[str, int] = {}   # id -> change count after our last step
        self._before: dict[str, dict] = {}  # id -> the light as it was, for Stop
        self._preset = ""                   # the preset that showed before, for Stop
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
        return {"until": self.until, "minutes": self.minutes}

    def start(self, minutes: int) -> None:
        minutes = int(minutes)
        if not 1 <= minutes <= 240:
            raise ValueError("Choose between 1 minute and 4 hours")
        m = self.manager
        m.sleep.cancel()                # a sleep timer and a wake-up don't run at once
        with self._lock:
            with m.lock:
                visible = [i for i in m.visible_ids() if i in m.states]
                ids = [i for i in visible if m.devices[i].group != "pc"] or visible
                self._lights = {i: m.touches(i) for i in ids}
                self._before = {i: m.states[i].to_dict() for i in ids}
                self._preset = m.config["settings"].get("active_preset", "")
            if not ids:
                raise ValueError("There are no lights to wake you with")
            self.started = time.time()
            self.until = self.started + minutes * 60
            self.minutes = minutes
            log.info("Wake-up: %d lights come up in %d min", len(ids), minutes)
            self.tick(self.started)     # the first step: the lights come on, dim and red
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="wake-up", daemon=True)
            self._thread.start()
        self._wake.set()
        m._changed()

    def cancel(self, put_back: bool = True) -> None:
        """Stop: the lights that are still waking go back to how they were before it started (a
        light you changed meanwhile stays as you set it). put_back=False: they stay where they
        are now (the sleep timer takes over from there)."""
        with self._lock:
            if not self.active:
                return
            m = self.manager
            back = {i: self._before[i] for i, n in self._lights.items()
                    if put_back and i in self._before and i in m.states and m.touches(i) == n}
            if back:
                m._apply(back, preset=self._preset, save=False)    # (what's saved is still the "before")
            self._end("stopped" if put_back else "the sleep timer took over")

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
                log.exception("Wake-up step failed")

    def tick(self, now: float) -> bool:
        """One step up (or the end). False once no wake-up runs."""
        m = self.manager
        with self._lock:
            if not self.active:
                return False
            # A light changed by anything else since our last step is yours again.
            self._lights = {i: n for i, n in self._lights.items() if i in m.states and m.touches(i) == n}
            if not self._lights:
                self._end("every light was changed by hand")
                return False
            if now >= self.until:
                done = {"on": True, "mode": "color", "color": FINAL, "brightness": 100}
                m._apply({i: done for i in self._lights}, preset="")    # saved: they stay on
                self._end("fully light")
                return False
            color, bright = look((now - self.started) / (self.until - self.started))
            step = {"on": True, "mode": "color", "color": color, "brightness": bright}
            patches = {i: step for i in self._lights
                       if {k: getattr(m.states[i], k) for k in step} != step}
            if patches:
                m._apply(patches, preset="", save=False)
                for i in patches:
                    self._lights[i] = m.touches(i)
            return True

    def _end(self, why: str) -> None:
        log.info("Wake-up over: %s", why)
        self.started = self.until = 0.0
        self.minutes = 0
        self._lights = {}
        self.manager._changed()
