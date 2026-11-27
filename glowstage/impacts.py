"""Loud, sudden sounds in a game (a shot, an explosion, a hit) and where they come from, for the
"Screen + sound" look: the lights flash on the side the sound is on.

Fed with every block of what the PC plays (stereo, before the music analysis mixes it to mono).
It listens to the crack of a sound: 300 to 4000 Hz, above the bass and kick drums, below the
hi-hats. A sound counts when it is
  - sudden: far louder than the quietest moment of the last ~60 ms (so a shot right after
    another can count too),
  - well above everything around it: the level of the last few seconds (so the hi-hats and
    snares of music, footsteps and a steady engine don't), and
  - among the loudest of the last seconds.
Left or right comes from the two channels' share.

Tuned on a made-up scene (shots left, right and far away, a burst of automatic fire, an
explosion, footsteps, hi-hats, wind, a music bed) and 42 minutes of real songs: every single
shot and the explosion, no footsteps, hats or kicks, and music alone about twice a minute. It
never flashes more than three times a second, the limit for people sensitive to flashing light.
"""

from __future__ import annotations

import collections
import math

import numpy as np

JUMP = 4.0          # times louder than the quietest of the last few blocks
BED = 6.0           # times louder than the level of the last few seconds
BED_TIME = 3.0      # s
LOUD = 0.2          # share of the loudest of the last seconds it has to reach
PEAK_TIME = 15.0    # s: how slowly "the loudest" is forgotten
START_PEAK = 0.01   # "the loudest" before anything was heard: quiet sounds don't count at the start
FLOOR_BLOCKS = 3    # ~60 ms
GAP = 0.34          # s: at most three flashes a second, also in a burst of fire (safe for the eyes)
KEEP = 1.0          # s: impacts are remembered this long
RATE = 48000
LOW, HIGH = 300, 4000   # Hz: the crack of a shot, above the bass and kicks, below the hi-hats


class Impacts:
    def __init__(self, block_seconds: float):
        self._bed_a = 1 - math.exp(-block_seconds / BED_TIME)
        self._decay = math.exp(-block_seconds / PEAK_TIME)
        self._bed = 0.0
        self._kept = 1.0                # (1 - a)^blocks: to read the level right from the first block
        self._peak = START_PEAK
        self._prev: collections.deque[float] = collections.deque(maxlen=FLOOR_BLOCKS)
        self._last = -1e9
        self._hits: collections.deque[tuple[float, float, float]] = collections.deque(maxlen=24)
        self._bins: tuple | None = None

    def feed(self, block: np.ndarray, now: float) -> bool:
        """One block of sound (frames x channels). True when it holds an impact."""
        if block.ndim == 1 or block.shape[1] == 1:
            left = right = block.reshape(-1)
        else:
            left, right = block[:, 0], block[:, 1]
        if len(left) < 2:
            return False
        el, er = self._band(left), self._band(right)
        e = math.sqrt((el * el + er * er) / 2)
        self._peak = max(self._peak * self._decay, e)
        floor = min(self._prev) if len(self._prev) == FLOOR_BLOCKS else e
        bed = self._bed / (1 - self._kept) if self._kept < 1 else e
        hit = (e > JUMP * floor and e > BED * bed and e > LOUD * self._peak
               and now - self._last >= GAP)
        if hit:
            strength = min(1.0, 0.35 + 0.65 * e / self._peak)
            pan = (er - el) / (er + el) if er + el > 0 else 0.0
            self._hits.append((now, strength, pan))
            self._last = now
        self._prev.append(e)
        self._bed += (e - self._bed) * self._bed_a
        self._kept *= 1 - self._bed_a
        return hit

    def _band(self, x: np.ndarray) -> float:
        """How loud the block is between LOW and HIGH Hz: where a shot's crack is, above the bass
        and kick drums, below the hi-hats."""
        n = len(x)
        if self._bins is None or self._bins[0] != n:
            f = np.fft.rfftfreq(n, 1 / RATE)
            self._bins = (n, (f >= LOW) & (f <= HIGH), np.hanning(n).astype(np.float32))
        spec = np.abs(np.fft.rfft(x * self._bins[2]))[self._bins[1]]
        return float(np.sqrt(np.mean(spec ** 2)) / n * 4)

    def recent(self, now: float) -> list[tuple[float, float, float]]:
        """The impacts of the last second: (age in s, strength 0..1, -1 left .. 1 right)."""
        return [(now - at, s, p) for at, s, p in tuple(self._hits) if now - at <= KEEP]
