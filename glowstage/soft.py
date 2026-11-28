"""Soften flashes (Setup → App): for people who are sensitive to flashing light. Every look already
keeps to the limit of three flashes a second; with this on, no light jumps at all. Each LED may
brighten from dark to full no faster than RISE seconds, and dim no faster than FALL: a lightning
strike, a kill in a game, a beat in the light show becomes a soft swell instead of a flash. The
light show also leaves out its strobes.

It works on what the effects draw, light by light, just before it is sent (engine.py), so every
look is covered, the ones to come too. A light that wasn't drawn for a moment (it showed a plain
color, or it just joined) starts at its look: switching to a new look is something you did, not a
flash."""

from __future__ import annotations

import numpy as np

RISE = 0.3      # seconds an LED takes at least from dark to full brightness
FALL = 0.2      # ... and from full brightness to dark (a little quicker: a glow that fades is calm)
STALE = 1.0     # a light not drawn for this long starts where its look is


class Softener:
    def __init__(self):
        self._last: dict[str, tuple[float, np.ndarray]] = {}    # light -> (when, what it showed)

    def apply(self, dev_id: str, colors: list, now: float) -> list[tuple[int, int, int]]:
        """The colors this light may show now, on their way to `colors`."""
        target = np.asarray(colors, dtype=float).reshape(-1, 3)
        seen = self._last.get(dev_id)
        dt = now - seen[0] if seen is not None else -1.0
        if seen is None or not 0 <= dt <= STALE or seen[1].shape != target.shape:
            out = target
        else:       # (dt 0: drawn twice in one moment, so no time to move)
            out = seen[1] + np.clip(target - seen[1], -255.0 * dt / FALL, 255.0 * dt / RISE)
        self._last[dev_id] = (now, out)
        return [tuple(row) for row in np.rint(out).astype(int).tolist()]

    def forget(self) -> None:
        self._last.clear()
