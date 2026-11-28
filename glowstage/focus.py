"""Focus timer: work for a while, then a short break, a few rounds (Home, the tray and Ctrl+K). The
break is something you see from the corner of your eye: the room turns a calm soft green, so you
don't need a sound or a pop-up to know it is time to get up.

It is a "this session" thing: nothing of it is saved, so after a restart it is off. Only the last
choice (work, break, rounds, work light) is kept in the settings, as the default for next time.

Work: your lights show the work light you chose (one of your presets, or "Keep my lights": nothing
changes). A change by hand meanwhile simply becomes your work light. Break: how the lights look right
then is remembered, and the lights that are on turn soft green at their own brightness (they glide
into it, like any other change). When the break is over they get that look back, so a change you
made while working survives it. After the last round of work there is no break: the lights go back
to how they looked before you started, and the timer is over.

It never fights you. A change by hand during a break stays until the break ends. An automatic look
(a game, a call, a video) wins while it lasts and nothing is paused: the light change of a turn is
skipped and only the time counts (like presets in turn, cycle.py). Switching every light off ends
the timer, because you left. Locking the PC doesn't (away.py may switch the lights off meanwhile):
a break is when you walk away, and the timer goes on; a break or work that began while you were
away shows in the lights when you're back. The sleep timer or a wake-up starting ends it too, and
starting it stops presets in turn and following the wallpaper.

Stop leaves the lights as they are, except in a break: then the work look comes back, so you are
not left with a green room.
"""

from __future__ import annotations

import logging
import math
import threading
import time

log = logging.getLogger(__name__)

WORK_CHOICES = (15, 25, 45, 50)   # minutes of work, as offered
REST_CHOICES = (5, 10, 15)        # minutes of break, as offered
ROUNDS = (1, 8)                   # how many rounds you can ask for
POLL = 1.0                        # seconds between two looks at the clock and the lights
BREAK_COLOR = "#4fd1a5"           # a calm soft green-teal: clearly not a work light
DEFAULT_WORK, DEFAULT_REST, DEFAULT_ROUNDS = 25, 5, 4
DEFAULT_LOOK = "Focus"            # the work light when you have a preset by that name


def defaults(preset_names) -> dict:
    """The choice Home starts from when nothing was chosen before."""
    return {"work": DEFAULT_WORK, "rest": DEFAULT_REST, "rounds": DEFAULT_ROUNDS,
            "look": DEFAULT_LOOK if DEFAULT_LOOK in preset_names else ""}


class FocusTimer:
    def __init__(self, manager):
        self.manager = manager
        self.phase = ""                     # "work" or "break"; empty when it doesn't run
        self.round = 0                      # the round that is on now, from 1
        self.rounds = 0
        self.work = 0                       # minutes
        self.rest = 0
        self.look = ""                      # the work light: a preset's name, "" = keep my lights
        self.at = 0.0                       # time.time() when this phase ends
        self._before: tuple[dict, str] | None = None     # the lights and preset from before we started
        self._captured: tuple[dict, str] | None = None   # the lights and preset when the break began
        self._missed = False                # a phase began while you were away: show it when you're back
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return bool(self.phase)

    def describe(self, now: float | None = None) -> dict | None:
        with self._lock:
            if not self.active:
                return None
            now = time.time() if now is None else now
            left = max(1, math.ceil((self.at - now) / 60))
            if self.phase == "work":
                label = f"Focus · {left} min left" + (f" · round {self.round} of {self.rounds}" if self.rounds > 1 else "")
                text = ("Then your lights go back to how they were." if self.round >= self.rounds
                        else f"Then a break of {self.rest} minutes: your lights turn soft green.")
            else:
                label = f"Break · {left} min left"
                text = "Your lights go back to your work light when it's over."
            return {"phase": self.phase, "round": self.round, "rounds": self.rounds, "work": self.work,
                    "rest": self.rest, "look": self.look, "at": self.at, "left": left, "label": label, "text": text}

    def last(self) -> dict:
        """The choice made last time (or the defaults), with a work light that still exists."""
        names = self.manager.preset_names()
        base = {**defaults(names), **(self.manager.config["settings"].get("focus_last") or {})}
        if base["look"] not in names:
            base["look"] = ""
        return base

    def start(self, work, rest, rounds, look="", now: float | None = None) -> None:
        m = self.manager
        work, rest, rounds = self._number(work), self._number(rest), self._number(rounds)
        if work not in WORK_CHOICES:
            raise ValueError("Choose 15, 25, 45 or 50 minutes of work")
        if rest not in REST_CHOICES:
            raise ValueError("Choose a break of 5, 10 or 15 minutes")
        if not ROUNDS[0] <= rounds <= ROUNDS[1]:
            raise ValueError("Choose between 1 and 8 rounds")
        look = str(look or "")
        if look and look not in m.preset_names():
            raise ValueError("That preset is gone")
        now = time.time() if now is None else now
        m.cycle.cancel()                # presets in turn would change the lights under the timer
        m.wallpaper.cancel()            # ... and so would following the wallpaper
        with self._lock:
            if self.active:             # a new start begins again (from the work look, not a green room)
                self.cancel()
            if not look and not self._any_on():
                raise ValueError("Switch some lights on first")
            self._before = (m.looks(), m.config["settings"].get("active_preset", ""))
            self._captured, self._missed = None, False
            self.work, self.rest, self.rounds, self.look = work, rest, rounds, look
            self.phase, self.round, self.at = "work", 1, now + work * 60
            log.info("Focus timer: %d min of work, %d min of break, %d rounds, work light %s",
                     work, rest, rounds, look or "(my lights)")
            if look:
                m.apply_preset(look)
            self._save_last()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="focus-timer", daemon=True)
            self._thread.start()
        self._wake.set()
        m._changed()

    def start_last(self) -> None:
        """Start with the last choice (the tray)."""
        c = self.last()
        self.start(c["work"], c["rest"], c["rounds"], c["look"])

    def skip(self, now: float | None = None) -> None:
        """To the next phase now: work → break, break → work (after the last work: the end)."""
        with self._lock:
            if self.active:
                self._next(time.time() if now is None else now)

    def cancel(self) -> None:
        """Stop: the lights stay as they are, except in a break (then the work look comes back)."""
        with self._lock:
            if not self.active:
                return
            if self.phase == "break":
                self._put_back()
            self._end("stopped")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def rename(self, old: str, new: str) -> None:
        """A preset got a new name: the running work light and the last choice follow it."""
        with self._lock:
            if self.look == old:
                self.look = new
            last = self.manager.config["settings"].get("focus_last")
            if last and last.get("look") == old:
                self.manager.config["settings"]["focus_last"] = {**last, "look": new}

    def forget(self, name: str) -> None:
        """A preset was deleted: it is no longer the last choice (a running timer is not affected,
        the work light was applied when it started)."""
        with self._lock:
            settings = self.manager.config["settings"]
            last = settings.get("focus_last")
            if last and last.get("look") == name:
                settings["focus_last"] = {**last, "look": ""}
            if self.look == name:
                self.look = ""

    # ---- the phases --------------------------------------------------------------------------

    @staticmethod
    def _number(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _any_on(self) -> bool:
        m = self.manager
        with m.lock:
            return any(m.states[i].on for i in m.visible_ids() if i in m.states)

    def _next(self, now: float) -> None:
        """Into the next phase. An automatic look that is on wins: then only the time moves on.
        While you're away the lights are left alone too, and the phase shows when you're back."""
        m = self.manager
        auto = bool(m.automations.active)
        away = not auto and m.away.holding
        if self.phase == "work":
            if self.round >= self.rounds:
                if not (auto or away) and self._before:
                    m.restore_looks(*self._before)
                self._end("all rounds done")
                return
            self._captured = None
            if not (auto or away):
                self._green()
            self._missed = away
            self.phase, self.at = "break", now + self.rest * 60
            log.info("Focus timer: break, round %d of %d done", self.round, self.rounds)
        else:
            if not (auto or away):
                self._put_back()
            self._missed = away
            self.phase, self.round, self.at = "work", self.round + 1, now + self.work * 60
            log.info("Focus timer: work, round %d of %d", self.round, self.rounds)
        m._changed()

    def _green(self) -> None:
        """The break: how the lights look now is kept for after it, the ones that are on go green."""
        m = self.manager
        self._captured = (m.looks(), m.config["settings"].get("active_preset", ""))
        with m.lock:
            ids = [i for i in m.visible_ids() if i in m.states and m.states[i].on]
        # (not saved: if Glowstage stops in the break, the lights come back as they were)
        m._apply({i: {"mode": "color", "color": BREAK_COLOR} for i in ids}, preset="", save=False)

    def _put_back(self) -> None:
        """The lights as they were when the break began (if an automatic look is on, it has them)."""
        captured, self._captured = self._captured, None
        if captured and not self.manager.automations.active:
            self.manager.restore_looks(*captured)

    def tick(self, now: float) -> bool:
        """One look: did you switch everything off (then it's over), is this phase over? False
        once it doesn't run."""
        m = self.manager
        with self._lock:
            if not self.active:
                return False
            if not (m.automations.active or m.away.holding):
                if self._missed:            # back: the phase that began meanwhile shows now
                    self._missed = False
                    self._green() if self.phase == "break" else self._put_back()
                if not self._any_on():
                    self._end("you switched the lights off")
                    return False
            if now >= self.at:
                self._next(now)
            return self.active

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(POLL)
            self._wake.clear()
            try:
                if not self.tick(time.time()):
                    return
            except Exception:
                log.exception("Focus timer: a step failed")

    def _end(self, why: str) -> None:
        log.info("Focus timer over: %s", why)
        self.phase, self.round, self.rounds, self.work, self.rest = "", 0, 0, 0, 0
        self.look, self.at = "", 0.0
        self._before = self._captured = None
        self._missed = False
        self.manager._changed()

    def _save_last(self) -> None:
        self.manager.config["settings"]["focus_last"] = {"work": self.work, "rest": self.rest,
                                                         "rounds": self.rounds, "look": self.look}
        self.manager.config.save_soon()
