"""Lights off while you're away: when you lock the PC, its screen goes off or it goes to sleep,
the PC's lights (or all of them) switch off, and when you're back they come on as they were.

Windows tells winutil.watch_windows about each of the three; while any of them holds, you're
away. Only lights that were on go off, and that isn't saved: if Windows shuts down meanwhile,
they still come back on next time. Coming back switches on only the lights that are still as
Glowstage left them: one a schedule (or anything else) changed meanwhile keeps its new look.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

SCOPES = ("", "pc", "all")      # the setting "away_lights": nothing, the PC's lights, all lights


class Away:
    def __init__(self, manager):
        self.manager = manager
        self.reasons: set[str] = set()          # "locked", "screen off", "asleep"
        self._off: dict[str, int] | None = None  # the lights switched off, with their change count
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return bool(self.reasons)

    def scope(self) -> str:
        value = self.manager.config["settings"].get("away_lights") or ""
        return value if value in SCOPES else ""

    def set(self, reason: str, on: bool) -> None:
        """Windows says: locked / screen off / asleep, or not any more."""
        with self._lock:
            was = bool(self.reasons)
            (self.reasons.add if on else self.reasons.discard)(reason)
            if self.reasons and not was:
                self._leave(reason)
            elif was and not self.reasons:
                self._back()

    def _leave(self, reason: str) -> None:
        scope = self.scope()
        if not scope:
            return
        self._off = self.manager.away_off(scope)
        log.info("Away (%s): %d lights off", reason, len(self._off))
        if reason == "asleep" and self._off:
            # Windows gives a program about two seconds before it sleeps: let the lights go dark first.
            self.manager.wait_applied(list(self._off), timeout=1.5)

    def _back(self) -> None:
        if self._off is None:
            return
        back = self.manager.away_back(self._off)
        log.info("Back: %d of %d lights on again", len(back), len(self._off))
        self._off = None
