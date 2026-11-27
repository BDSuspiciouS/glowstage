"""Which keys you press, for the "Typing ripples" look: only while a light runs it.

It asks Windows every 1/60 s which keys are down (GetAsyncKeyState). That never sits between
your keyboard and your programs, so typing and games feel exactly the same (a keyboard hook
would). What it learns stays in memory for three seconds, as "a key at this spot on the board":
nothing is saved or sent anywhere. Keys typed into a program that runs as administrator, or on
the sign-in screen, aren't seen at all.
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque

# Where each key sits on a tenkeyless keyboard, in key widths from the top left: its center.
# (Virtual-key codes; the punctuation keys are where a US keyboard has them, which is the same
# spot on almost every layout.)
_ROWS = {
    0.6: [(0x1B, 0.5), (0x70, 1.75), (0x71, 2.75), (0x72, 3.75), (0x73, 4.75), (0x74, 6.0), (0x75, 7.0),
          (0x76, 8.0), (0x77, 9.0), (0x78, 10.25), (0x79, 11.25), (0x7A, 12.25), (0x7B, 13.25),
          (0x2C, 15.75), (0x91, 16.75), (0x13, 17.75)],
    2.1: [(0xC0, 0.5), *[(0x31 + i, 1.5 + i) for i in range(9)], (0x30, 10.5), (0xBD, 11.5), (0xBB, 12.5),
          (0x08, 14.0), (0x2D, 15.75), (0x24, 16.75), (0x21, 17.75)],
    3.1: [(0x09, 0.75), *[(ord(c), 2.0 + i) for i, c in enumerate("QWERTYUIOP")], (0xDB, 12.0), (0xDD, 13.0),
          (0xDC, 14.25), (0x2E, 15.75), (0x23, 16.75), (0x22, 17.75)],
    4.1: [(0x14, 0.875), *[(ord(c), 2.25 + i) for i, c in enumerate("ASDFGHJKL")], (0xBA, 11.25),
          (0xDE, 12.25), (0x0D, 13.875)],
    5.1: [(0xA0, 1.125), (0xE2, 1.75), *[(ord(c), 2.75 + i) for i, c in enumerate("ZXCVBNM")], (0xBC, 9.75),
          (0xBE, 10.75), (0xBF, 11.75), (0xA1, 13.625), (0x26, 16.75)],
    6.1: [(0xA2, 0.625), (0x5B, 1.875), (0xA4, 3.125), (0x20, 6.875), (0xA5, 10.625), (0x5C, 11.875),
          (0x5D, 13.125), (0xA3, 14.375), (0x25, 15.75), (0x28, 16.75), (0x27, 17.75)],
}
WIDTH, HEIGHT = 18.25, 6.4          # the board, in key widths
KEYS: dict[int, tuple[float, float]] = {vk: (x / WIDTH, y / HEIGHT) for y, row in _ROWS.items() for vk, x in row}

POLL = 1 / 60
KEEP = 3.0          # seconds a press is remembered
IDLE = 2.0          # stop asking Windows this long after the look last wanted to know


def _async_key_state():
    get = ctypes.WinDLL("user32").GetAsyncKeyState
    get.argtypes = [ctypes.c_int]
    get.restype = ctypes.c_short
    return get


class KeyWatcher:
    def __init__(self, get_state=None):
        self._get = get_state            # (vk) -> GetAsyncKeyState's answer; made on first use
        self._presses: deque[tuple[float, int]] = deque(maxlen=64)
        self._lock = threading.Lock()
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None

    def recent(self, now: float) -> list[tuple[float, float, float]]:
        """The presses of the last few seconds as (age in s, x, y), x and y the key's place on
        the board (0..1). Asking keeps the watcher running; nobody asking stops it."""
        self._wanted_until = now + IDLE
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="keys", daemon=True)
            self._thread.start()
        with self._lock:
            while self._presses and now - self._presses[0][0] > KEEP:
                self._presses.popleft()
            items = list(self._presses)
        return [(now - at, *KEYS[vk]) for at, vk in items]

    def _run(self) -> None:
        if self._get is None:
            self._get = _async_key_state()
        get = self._get
        for vk in KEYS:                  # forget presses from before we started watching
            get(vk)
        down: set[int] = set()
        while time.monotonic() < self._wanted_until:
            self.poll(time.monotonic(), down)
            time.sleep(POLL)
        with self._lock:
            self._presses.clear()

    def poll(self, now: float, down: set[int]) -> None:
        get = self._get
        for vk in KEYS:
            state = get(vk)
            is_down = bool(state & 0x8000)
            # A new press: down now and wasn't before, or down and up again since the last look
            # (the low bit), for a quick tap between two looks.
            if (is_down and vk not in down) or (state & 1 and not is_down):
                with self._lock:
                    self._presses.append((now, vk))
            if is_down:
                down.add(vk)
            else:
                down.discard(vk)
