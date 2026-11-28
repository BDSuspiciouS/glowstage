"""Night limit (Setup → Schedule): from a time in the evening to a time in the morning, no light goes
above a brightness you choose, whatever switches it on: a preset, a schedule, a game, a call, the
light show, the phone. Late at night a game that starts, or a preset meant for the day, doesn't
light the room up at full.

It is a ceiling, not a change: the brightness you set stays as it is (the sliders show it), and a
light set lower than the limit is left alone. When the night is over every light is back at its
own brightness. A wake-up light (wake.py) is let through, so a sunrise before the limit ends still
comes up to full. "Not tonight" lifts it until the next morning.

How: each light gets `cap` (devices/base.py), which Device.apply, the glides into a new color
(manager.DeviceWorker) and the effects (engine.py) keep to. When the limit starts or ends the
lights that are on are sent their look again, so they glide down (or back up) at once instead of
at their next change.
"""

from __future__ import annotations

import datetime
import logging
import re
import threading
import time

log = logging.getLogger(__name__)

DEFAULT = {"on": False, "from": "23:00", "to": "07:00", "max": 30}
CHOICES = (5, 10, 15, 20, 30, 40, 50, 60, 75)     # percent, as offered
POLL = 20.0                                       # seconds between two looks at the clock
TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def _minutes(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def in_window(start: str, end: str, at: datetime.datetime) -> bool:
    """`at` lies between the evening time and the morning time (over midnight, or within a day)."""
    m, a, b = at.hour * 60 + at.minute, _minutes(start), _minutes(end)
    return a <= m < b if a < b else (m >= a or m < b)


def next_end(end: str, at: datetime.datetime) -> datetime.datetime:
    """The next moment the clock shows `end` after `at`."""
    h, m = (int(x) for x in end.split(":"))
    out = at.replace(hour=h, minute=m, second=0, microsecond=0)
    return out if out > at else out + datetime.timedelta(days=1)


class NightLimit:
    def __init__(self, manager):
        self.manager = manager
        self.applied: int | None = None     # the ceiling the lights have now (None: none)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()

    # ---- the setting ---------------------------------------------------------------------------

    @property
    def settings(self) -> dict:
        s = self.manager.config["settings"].get("night_limit")
        return {**DEFAULT, **s} if isinstance(s, dict) else dict(DEFAULT)

    def configure(self, on, start, end, top) -> None:
        """Save the limit (checked first) and use it right away."""
        start, end = str(start or ""), str(end or "")
        if not TIME.fullmatch(start) or not TIME.fullmatch(end):
            raise ValueError("Times look like 23:00")
        if start == end:
            raise ValueError("The night has to start and end at different times")
        try:
            top = int(top)
        except (TypeError, ValueError):
            top = 0
        if top not in CHOICES:
            raise ValueError("Choose one of the brightness levels offered")
        self.manager.config["settings"]["night_limit"] = {"on": bool(on), "from": start, "to": end, "max": top}
        self.manager.config.save_soon()
        self.tick()

    def skip_tonight(self, now: float | None = None) -> None:
        """"Not tonight": no limit until this night is over."""
        now = time.time() if now is None else now
        s = self.settings
        self.manager.config["settings"]["night_skip_until"] = next_end(s["to"], datetime.datetime.fromtimestamp(now)).timestamp()
        self.manager.config.save_soon()
        log.info("Night limit: not tonight")
        self.tick(now)

    # ---- now ----------------------------------------------------------------------------------

    def ceiling(self, now: float | None = None) -> int | None:
        """The brightness no light goes above right now, None when there's no limit now."""
        now = time.time() if now is None else now
        s = self.settings
        if not s["on"] or now < float(self.manager.config["settings"].get("night_skip_until") or 0):
            return None
        if not in_window(s["from"], s["to"], datetime.datetime.fromtimestamp(now)):
            return None
        if self.manager.wake.active:        # (a sunrise comes up to full)
            return None
        return int(s["max"])

    def describe(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        s = self.settings
        until = next_end(s["to"], datetime.datetime.fromtimestamp(now)).timestamp()
        return {**s, "active": self.applied is not None, "until": until, "choices": list(CHOICES)}

    # ---- keeping the lights to it -----------------------------------------------------------------

    def fit(self, dev) -> None:
        """A light that just came: the ceiling of now."""
        dev.cap = self.applied

    def tick(self, now: float | None = None) -> bool:
        """Look at the clock: when the ceiling changed, give it to every light and send the lights
        that are on their look again. True when it changed."""
        m = self.manager
        with self._lock:
            top = self.ceiling(now)
            if top == self.applied:
                return False
            self.applied = top
            with m.lock:
                for dev in m.devices.values():
                    dev.cap = top
                again = [(m.workers[i], st) for i, st in m.states.items()
                         if st.on and i in m.devices and i in m.workers and m.devices[i].online]
            log.info("Night limit: %s", f"lights at most {top}%" if top is not None else "over")
        for worker, state in again:
            worker.submit(state)            # (glides down, or back up, where a light can)
        m._changed()
        return True

    def start(self) -> None:
        threading.Thread(target=self._run, name="night-limit", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Night limit: a look at the clock failed")
            self._wake.wait(POLL)
            self._wake.clear()
