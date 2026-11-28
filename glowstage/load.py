"""How busy the PC is, for the "PC load" look: only while a light runs it.

The processor load comes from GetSystemTimes (idle, kernel and user time; the change between two
looks says how much of the time the cores were working). The graphics card's load comes from the
same Windows performance counters Task Manager reads ("GPU Engine", the 3D engines), summed per
graphics card; the busiest card counts. If Windows won't give that (an old driver, no counters),
the GPU stays None and the look shows the processor load everywhere. It never raises.

It looks at most twice a second and stops 2 s after the look last asked. Nothing is saved or sent.

Cost of one sample, measured on the development PC: the processor load ~0.1 ms (one system
call); the graphics load ~0.7 ms (one counter query). Opening the graphics counters, once, takes
~0.4 s, which is why all of it runs on its own thread and never in the frame loop.
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from ctypes import wintypes as w

log = logging.getLogger(__name__)

EVERY = 0.5         # seconds between two looks
IDLE = 2.0          # stop looking this long after the look last wanted to know

_PDH_MORE_DATA = 0x800007D2
_PDH_FMT_DOUBLE = 0x200
_COUNTER = r"\GPU Engine(*engtype_3D)\Utilization Percentage"


def cpu_load(prev: tuple[int, int, int] | None, cur: tuple[int, int, int]) -> float | None:
    """The share of time the cores were busy between two GetSystemTimes answers (idle, kernel,
    user; kernel includes the idle time), 0..1. None when there is nothing to compare yet."""
    if prev is None:
        return None
    idle, kernel, user = (c - p for c, p in zip(cur, prev))
    total = kernel + user
    if total <= 0:
        return None
    return min(1.0, max(0.0, (total - idle) / total))


def _filetime(ft: w.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def system_times() -> tuple[int, int, int]:
    idle, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME()
    if not ctypes.WinDLL("kernel32").GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("GetSystemTimes failed")
    return _filetime(idle), _filetime(kernel), _filetime(user)


class _Item(ctypes.Structure):
    _fields_ = [("name", w.LPWSTR), ("status", w.DWORD), ("value", ctypes.c_double)]


def adapter_loads(items: list[tuple[str, float]]) -> float | None:
    """Per graphics card (the "luid" in each engine's name) the sum of its 3D engines, clamped
    to 0..100; the busiest card's share as 0..1. None when there are no engines."""
    per: dict[str, float] = {}
    for name, value in items:
        m = re.search(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", name)
        key = m.group(1) if m else ""
        per[key] = per.get(key, 0.0) + max(0.0, value)
    if not per:
        return None
    return min(100.0, max(per.values())) / 100.0


class GpuReader:
    """The 3D load of the graphics card(s) through PDH. read() gives 0..1, or None if Windows
    can't tell; after one failure it stops trying."""

    def __init__(self):
        self._pdh = None
        self._query = w.HANDLE()
        self._counter = w.HANDLE()
        self._failed = False

    def _open(self) -> None:
        pdh = ctypes.WinDLL("pdh")
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            raise OSError("PdhOpenQueryW failed")
        if pdh.PdhAddEnglishCounterW(self._query, _COUNTER, 0, ctypes.byref(self._counter)) != 0:
            raise OSError("no GPU counters")
        pdh.PdhCollectQueryData(self._query)      # a rate needs two looks; this is the first
        self._pdh = pdh

    def read(self) -> float | None:
        if self._failed:
            return None
        try:
            if self._pdh is None:
                self._open()
                return None
            pdh = self._pdh
            if pdh.PdhCollectQueryData(self._query) != 0:
                raise OSError("PdhCollectQueryData failed")
            size, count = w.DWORD(0), w.DWORD(0)
            r = pdh.PdhGetFormattedCounterArrayW(self._counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                                 ctypes.byref(count), None) & 0xFFFFFFFF
            if r != _PDH_MORE_DATA:
                return None if r == 0 else self._fail("PdhGetFormattedCounterArrayW failed")
            buf = ctypes.create_string_buffer(size.value)
            r = pdh.PdhGetFormattedCounterArrayW(self._counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                                 ctypes.byref(count), buf) & 0xFFFFFFFF
            if r != 0:
                return self._fail("PdhGetFormattedCounterArrayW failed")
            items = ctypes.cast(buf, ctypes.POINTER(_Item))
            return adapter_loads([(items[i].name or "", items[i].value) for i in range(count.value)
                                  if items[i].status in (0, 1)])
        except Exception as e:
            return self._fail(str(e))

    def _fail(self, why: str):
        log.info("GPU load not available: %s", why)
        self._failed = True
        return None


class LoadWatcher:
    def __init__(self, cpu_times=None, gpu=None):
        self._times = cpu_times or system_times      # () -> (idle, kernel, user)
        self._gpu = gpu if gpu is not None else GpuReader()   # anything with read() -> 0..1 | None
        self._cpu: float | None = None
        self._gpu_load: float | None = None
        self._lock = threading.Lock()
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None

    def request(self, now: float) -> tuple[float | None, float | None]:
        """(processor load, graphics load) as 0..1, None where unknown. Asking keeps the watcher
        running; nobody asking stops it."""
        self._wanted_until = now + IDLE
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="load", daemon=True)
            self._thread.start()
        with self._lock:
            return self._cpu, self._gpu_load

    def _run(self) -> None:
        prev = None
        while time.monotonic() < self._wanted_until:
            prev = self.sample(prev)
            time.sleep(EVERY)
        with self._lock:
            self._cpu = self._gpu_load = None        # old numbers shouldn't greet the next start

    def sample(self, prev):
        """One look at both loads; returns the processor times to compare the next look with."""
        try:
            cur = self._times()
            cpu = cpu_load(prev, cur)
        except Exception:
            cur, cpu = None, None
        try:
            gpu = self._gpu.read()
        except Exception:
            gpu = None
        with self._lock:
            if cpu is not None or cur is None:
                self._cpu = cpu
            self._gpu_load = gpu
        return cur
