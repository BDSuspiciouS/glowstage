"""Presets in turn: you pick a few presets and Glowstage switches between them by itself, one after
the other, every few minutes (Home and the Presets page): an evening that slowly changes from Cozy to
Reading to Late night, or a room that never looks the same for long.

It is a "this evening" thing: nothing of it is saved, so after a restart it is off. Only the last
choice (which presets, how often, shuffle) is kept in the settings, as the default for next time.

It stops by itself when you take over: a light, a preset, switching on or off, from the window, the
tray, the phone or a hotkey. Like the automatic looks (automations.py) it sees that by comparing the
lights with how they looked right after its own last turn. The sleep timer or a wake-up starting
stops it too, and so does switching every light off (that is a change by hand as well).

An automatic look (a game, a video, music, an app, a call) wins while it lasts and nothing is
paused: when a turn comes and one is on, that turn is skipped and the next comes `minutes` later, so
the lights are never fought over. When the automatic look ends it puts the lights back as they
were, which is how they looked after our last turn: the cycle simply goes on.
"""

from __future__ import annotations

import logging
import random
import threading
import time

log = logging.getLogger(__name__)

CHOICES = (1, 5, 10, 15, 30, 60)   # minutes between two turns, as offered
POLL = 1.0                         # seconds between two looks at whether you took over


def _same(now: dict, then: dict) -> bool:
    """The lights still look as we left them (lights that came or went since don't count)."""
    return all(now[i] == then[i] for i in now.keys() & then.keys())


class PresetCycle:
    def __init__(self, manager, rng: random.Random | None = None):
        self.manager = manager
        self.names: list[str] = []          # the presets in turn; empty when it doesn't run
        self.minutes = 0
        self.shuffle = False
        self.next_at = 0.0                  # time.time() of the next turn
        self._rng = rng or random.Random()
        self._queue: list[str] = []         # the rest of this round, in the order they come
        self._current = ""                  # the preset that is on now
        self._set: dict | None = None       # how the lights looked right after our last turn
        self._preset = ""                   # active_preset right after it
        self._alt = ""                      # ... or the name it had a moment ago (a rename)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return bool(self.names)

    def describe(self) -> dict | None:
        with self._lock:
            if not self.active:
                return None
            return {"names": list(self.names), "minutes": self.minutes, "shuffle": self.shuffle,
                    "now": self._current, "next": self._queue[0] if self._queue else "", "at": self.next_at}

    def start(self, names, minutes, shuffle=False, now: float | None = None) -> None:
        m = self.manager
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        if minutes not in CHOICES:
            raise ValueError("Choose 1, 5, 10, 15, 30 or 60 minutes")
        names = list(dict.fromkeys(str(n) for n in (names or [])))
        have = m.preset_names()
        if any(n not in have for n in names):
            raise ValueError("One of those presets is gone")
        if len(names) < 2:
            raise ValueError("Choose at least two presets")
        now = time.time() if now is None else now
        m.wallpaper.cancel()            # these don't both decide what the lights look like
        m.focus.cancel()
        with self._lock:
            self.names, self.minutes, self.shuffle = names, minutes, bool(shuffle)
            self._queue, self._current = [], ""
            log.info("Presets in turn: %s, every %d min%s", ", ".join(names), minutes, ", shuffled" if shuffle else "")
            self._turn(now)
            self._save_last()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="preset-cycle", daemon=True)
            self._thread.start()
        self._wake.set()

    def cancel(self) -> None:
        """Stop: the lights stay as they are now."""
        with self._lock:
            if self.active:
                self._end("stopped")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def rename(self, old: str, new: str) -> None:
        """A preset got a new name (manager.rename_preset calls this just before it changes the
        settings, so for a moment the settings still say `old`: both are accepted)."""
        with self._lock:
            def swap(names):
                return [new if n == old else n for n in names]
            self.names, self._queue = swap(self.names), swap(self._queue)
            if self._current == old:
                self._current = new
            if self._preset == old:
                self._preset, self._alt = new, old
            settings = self.manager.config["settings"]
            last = settings.get("cycle_last")
            if last and old in last.get("names", []):
                settings["cycle_last"] = {**last, "names": swap(last["names"])}

    def forget(self, name: str) -> None:
        """A preset was deleted: out of the list (and the last choice); with fewer than two left
        there is nothing to take turns."""
        with self._lock:
            settings = self.manager.config["settings"]
            last = settings.get("cycle_last")
            if last and name in last.get("names", []):
                settings["cycle_last"] = {**last, "names": [n for n in last["names"] if n != name]}
            if name not in self.names:
                return
            self.names = [n for n in self.names if n != name]
            self._queue = [n for n in self._queue if n != name]
            if len(self.names) < 2:
                self._end("fewer than two presets left")
                return
            if not self._queue:
                self._refill()
        self.manager._changed()

    # ---- the turns ---------------------------------------------------------------------------

    def _refill(self) -> None:
        """The next round: in the list's order, or shuffled. A round never starts with the preset
        that is on now, so the same one doesn't come twice in a row."""
        queue = list(self.names)
        if self.shuffle:
            self._rng.shuffle(queue)
        if len(queue) > 1 and queue[0] == self._current:
            queue[0], queue[-1] = queue[-1], queue[0]
        self._queue = queue

    def _turn(self, now: float) -> None:
        """Switch to the next preset. (Not remember(): a turn isn't something you did, so Undo
        keeps going back to the lights from before you started.)"""
        m = self.manager
        if not self._queue:
            self._refill()
        name = self._queue.pop(0)
        m.apply_preset(name)
        self._current = name
        self._set = m.looks()           # right after, so the change doesn't look like yours
        self._preset, self._alt = m.config["settings"].get("active_preset", ""), ""
        self.next_at = now + self.minutes * 60
        if not self._queue:
            self._refill()              # (so Home can say what comes next)
        m._changed()

    def tick(self, now: float) -> bool:
        """One look: did you take over (then it's over), is it time for the next preset? False
        once it doesn't run."""
        m = self.manager
        looks = m.looks()
        preset = m.config["settings"].get("active_preset", "")
        with self._lock:
            if not self.active:
                return False
            if m.automations.active or m.away.holding:
                # An automatic look is on, or you're away and the lights are off for you: what
                # changes them is not you. Skip the turn.
                if now >= self.next_at:
                    self.next_at = now + self.minutes * 60
                    log.info("Presets in turn: skipped a turn (%s)", "an automatic look" if m.automations.active else "you're away")
                    m._changed()
                return True
            if (self._set is not None and not _same(looks, self._set)) or preset not in (self._preset, self._alt):
                self._end("you changed the lights")     # (switching every light off counts)
                return False
            if now >= self.next_at:
                self._turn(now)
            return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(POLL)
            self._wake.clear()
            try:
                if not self.tick(time.time()):
                    return
            except Exception:
                log.exception("Presets in turn: a step failed")

    def _end(self, why: str) -> None:
        log.info("Presets in turn over: %s", why)
        self.names, self._queue, self._current, self._set = [], [], "", None
        self.minutes, self.next_at = 0, 0.0
        self.manager._changed()

    def _save_last(self) -> None:
        self.manager.config["settings"]["cycle_last"] = {"names": list(self.names), "minutes": self.minutes,
                                                         "shuffle": self.shuffle}
        self.manager.config.save_soon()
