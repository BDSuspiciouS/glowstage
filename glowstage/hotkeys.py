"""Global hotkeys (off until switched on in Setup): from any program,

    Ctrl+Alt+Shift+L     all lights on / off
    Ctrl+Alt+Shift+S     the light show on / off
    Ctrl+Alt+Shift+1..9  the first nine presets

Three modifiers, so they don't take a shortcut from another program (Ctrl+Alt+L, say, is a
command in several editors, and a global hotkey would take it from them everywhere).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
KEYS = [("L", "All lights on or off"), ("S", "The light show on or off")] + \
       [(str(n), f"Preset {n}") for n in range(1, 10)]
LABEL = "Ctrl+Alt+Shift+"


class Hotkeys:
    def __init__(self, manager):
        self.manager = manager
        self.failed: list[str] = []          # keys another program already has
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._lock = threading.Lock()

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            if on and self._thread is None:
                ready = threading.Event()
                self._thread = threading.Thread(target=self._run, args=(ready,), name="hotkeys", daemon=True)
                self._thread.start()
                ready.wait(2.0)
            elif not on and self._thread is not None:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
                self._thread.join(2.0)
                self._thread, self.failed = None, []

    def _run(self, ready: threading.Event) -> None:
        # A hotkey belongs to the thread that registered it: register, listen and unregister here.
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        mods = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT
        ids, failed = [], []
        for i, (key, _) in enumerate(KEYS, start=1):
            if user32.RegisterHotKey(None, i, mods, ord(key)):
                ids.append(i)
            else:
                failed.append(LABEL + key)
        self.failed = failed
        if failed:
            log.warning("Hotkeys another program already has: %s", ", ".join(failed))
        ready.set()
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._pressed(KEYS[msg.wParam - 1][0])
        finally:
            for i in ids:
                user32.UnregisterHotKey(None, i)

    def _pressed(self, key: str) -> None:
        m = self.manager
        try:
            if key == "L":
                m.set_power_all(not m.any_on())
            elif key == "S":
                m.toggle_show()
            else:
                names = m.preset_names()
                n = int(key) - 1
                if n < len(names):
                    m.apply_preset(names[n])
        except Exception:
            log.exception("Hotkey %s failed", LABEL + key)
