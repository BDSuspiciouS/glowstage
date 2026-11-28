"""Samples a display for the "Screen sync" effect: the vivid colors in each slice of the
screen from left to right, so every light can take the part of the screen where it sits on the
stage (a strip above the monitor runs along with the picture; a bulb takes the screen's color as a
whole). Runs only while some light is using that effect.

The screen is read with one GDI StretchBlt into a 128 x 72 bitmap: half the processor time of the
full-size screenshot it used to take (on a 3456 x 2169 screen ~15 ms per sample against ~34 ms).

Which display: the main one, one picked by its Windows name (DISPLAY2), or "auto" for where a
full-screen game or video is. The cost is the same either way: only the rectangle that is stretched
changes."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes as w
from typing import Callable, NamedTuple

import numpy as np

log = logging.getLogger(__name__)

COLS = 32          # slices of the screen from left to right
W, H = 128, 72     # the size the screen is sampled at
REFRESH = 2.0      # seconds between looks at which displays there are (and where a full-screen window is)
AUTO_HOLD = 5.0    # seconds "auto" stays on a display after its full-screen window went (a quick alt-tab)


# ---- which displays there are ----------------------------------------------------------------------

class Monitor(NamedTuple):
    device: str                       # Windows' name for it, e.g. "\\.\DISPLAY2"
    rect: tuple[int, int, int, int]   # left, top, right, bottom on the virtual screen, in pixels
    primary: bool

    @property
    def size(self) -> tuple[int, int]:
        return self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]


def names(monitors: list[Monitor]) -> dict[str, str]:
    """A name for each display that says where it stands: "Main display", "Right display \u00b7
    2560 \u00d7 1440". (Windows' own numbers, DISPLAY129 and the like, mean nothing to anyone and
    change when a driver is reinstalled.) Two on the same side get a number, left to right."""
    main = next((m for m in monitors if m.primary), None)
    if main is None:
        return {m.device: f"Display {i} \u00b7 {m.size[0]} \u00d7 {m.size[1]}" for i, m in enumerate(monitors, 1)}
    mx, my = (main.rect[0] + main.rect[2]) / 2, (main.rect[1] + main.rect[3]) / 2
    side = {}
    for m in monitors:
        if m is not main:
            dx, dy = (m.rect[0] + m.rect[2]) / 2 - mx, (m.rect[1] + m.rect[3]) / 2 - my
            side[m.device] = ("Right" if dx > 0 else "Left") if abs(dx) >= abs(dy) else ("Lower" if dy > 0 else "Upper")
    out = {main.device: "Main display"}
    others = sorted((m for m in monitors if m is not main), key=lambda m: (m.rect[0], m.rect[1]))
    for m in others:
        same = [x.device for x in others if side[x.device] == side[m.device]]
        count = f" {same.index(m.device) + 1}" if len(same) > 1 else ""
        out[m.device] = f"{side[m.device]} display{count} \u00b7 {m.size[0]} \u00d7 {m.size[1]}"
    return out


class Choice(NamedTuple):
    monitor: Monitor | None   # what is sampled (None: no display found at all)
    missing: str              # the display that was asked for but isn't connected ("" if none)


def choose(setting: str, monitors: list[Monitor], full_screen: str | None = None) -> Choice:
    """The display to sample. "" is the main one; a device name is that display, or the main one if
    it isn't connected (never an error: a monitor that is switched off must not stop the lights);
    "auto" is the display with a full-screen window (`full_screen`, its device name), else the main
    one."""
    main = next((m for m in monitors if m.primary), monitors[0] if monitors else None)
    if setting == "auto":
        return Choice(next((m for m in monitors if m.device == full_screen), main), "")
    if setting:
        found = next((m for m in monitors if m.device == setting), None)
        return Choice(found, "") if found else Choice(main, setting)
    return Choice(main, "")


def describe(setting: str, monitors: list[Monitor], choice: Choice) -> dict:
    """What the window shows for the "Screen" choice: the options (the main display, each other
    display by where it stands and its size, then "where the game or video is"), which one is
    saved, which display is followed now, and a note when the chosen display isn't connected."""
    label = names(monitors)
    options = [{"id": "", "name": "Main display", "primary": True}]
    options += [{"id": m.device, "name": label[m.device], "primary": False} for m in monitors if not m.primary]
    if setting not in ("", "auto") and not any(o["id"] == setting for o in options):
        # (a display that is unplugged stays in the list, so the choice is kept for when it's back)
        options.append({"id": setting, "name": "The display you picked (not connected)", "primary": False,
                        "missing": True})
    options.append({"id": "auto", "name": "Where the game or video is", "primary": False})
    note = "The display you picked isn't connected: following the main display" if choice.missing else ""
    using = choice.monitor
    return {"options": options, "current": setting, "count": len(monitors),
            "using": {"id": using.device, "name": label.get(using.device, "Main display")} if using else None,
            "note": note}


@contextmanager
def _physical_pixels():
    """Windows hands out coordinates in the units the calling thread is aware of. The window library
    (pywebview) makes this process "system DPI aware" (SetProcessDPIAware, in its winforms backend;
    this app never sets it itself), which on a second monitor with another scaling gives scaled
    coordinates. Per-monitor awareness for this thread only (-4: per-monitor v2) makes the monitor
    rectangles and the screen DC both physical pixels, whatever the process was told."""
    u32 = ctypes.WinDLL("user32")
    old = None
    try:
        u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        old = u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass   # Windows before 10 1703: the numbers stay as they are
    try:
        yield
    finally:
        if old:
            u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


class _MonitorInfo(ctypes.Structure):   # MONITORINFOEXW
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD),
                ("szDevice", ctypes.c_wchar * 32)]


def _info(u32, handle) -> Monitor | None:
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not u32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return None
    r = info.rcMonitor
    return Monitor(info.szDevice, (r.left, r.top, r.right, r.bottom), bool(info.dwFlags & 1))   # MONITORINFOF_PRIMARY


def list_monitors() -> list[Monitor]:
    """The connected displays from Windows: the main one first, then left to right."""
    u32 = ctypes.WinDLL("user32")
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    u32.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.c_void_p, proc, w.LPARAM]
    u32.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(_MonitorInfo)]
    found: list[Monitor] = []

    def each(handle, _dc, _rect, _data):
        m = _info(u32, handle)
        if m:
            found.append(m)
        return True

    with _physical_pixels():
        u32.EnumDisplayMonitors(None, None, proc(each), 0)
    return sorted(found, key=lambda m: (not m.primary, m.rect[0], m.rect[1]))


def full_screen_device() -> str | None:
    """The display that holds a full-screen window now (its device name), or None. Windows' own test,
    the one automations.full_screen uses: SHQueryUserNotificationState says "busy" (2) or "a
    full-screen Direct3D program" (3); the window in front is the one that holds it."""
    state = ctypes.c_int()
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0 or state.value not in (2, 3):
        return None
    u32 = ctypes.WinDLL("user32")
    u32.GetForegroundWindow.restype = w.HWND
    u32.MonitorFromWindow.restype, u32.MonitorFromWindow.argtypes = w.HANDLE, [w.HWND, w.DWORD]
    u32.GetMonitorInfoW.argtypes = [w.HANDLE, ctypes.POINTER(_MonitorInfo)]
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return None
    m = _info(u32, u32.MonitorFromWindow(hwnd, 2))   # MONITOR_DEFAULTTONEAREST
    return m.device if m else None


class Shot:
    """One sample of the screen: per slice, the color sum of its pixels weighted by how vivid
    they are, and the sum of those weights (so slices can be averaged together exactly)."""

    def __init__(self, sums: np.ndarray, weights: np.ndarray):
        self._cs = np.vstack([np.zeros((1, 3)), np.cumsum(sums, axis=0)])
        self._cw = np.concatenate([[0.0], np.cumsum(weights)])

    def _at(self, cum: np.ndarray, x: np.ndarray) -> np.ndarray:
        p = np.clip(x, 0.0, 1.0) * COLS
        i = np.minimum(p.astype(int), COLS - 1)
        frac = p - i
        if cum.ndim == 2:
            frac = frac[:, None]
        return cum[i] + (cum[i + 1] - cum[i]) * frac

    def colors(self, start: float, end: float, n: int) -> np.ndarray:
        """The colors for a light of n pixels spanning start..end of the stage (0 = left, 1 =
        right; start > end runs it the other way): each pixel the vivid average of its own part
        of the screen, at least one slice wide. An (n, 3) array, 0..255."""
        u = start + (end - start) * (np.arange(n) + 0.5) / n
        half = min(0.5, max(abs(end - start) / n / 2, 0.5 / COLS))
        u = np.clip(u, half, 1 - half)
        lo, hi = u - half, u + half
        rgb = (self._at(self._cs, hi) - self._at(self._cs, lo)) / \
            np.maximum(self._at(self._cw, hi) - self._at(self._cw, lo), 1e-9)[:, None]
        return vivid(rgb)


def vivid(rgb: np.ndarray) -> np.ndarray:
    """Screens look washed out on lights: more saturation (x1.35) and a little more value (x1.15),
    keeping the hue."""
    mx = rgb.max(axis=1, keepdims=True)
    mn = rgb.min(axis=1, keepdims=True)
    span = mx - mn
    f = np.where(span > 1e-6, (rgb - mn) / np.maximum(span, 1e-6), 1.0)   # where each channel sits
    s = np.where(mx > 1e-6, span / np.maximum(mx, 1e-6), 0.0)
    v = np.minimum(1.0, mx / 255 * 1.15)
    s = np.minimum(1.0, s * 1.35)
    return 255 * v * (1 - s * (1 - f))


def weigh(img: np.ndarray) -> Shot:
    """A (H, W, 3) picture into slices. Vivid pixels count more, so a colorful scene wins over
    the gray of a window around it."""
    img = img.astype(np.float32)
    mx, mn = img.max(axis=2), img.min(axis=2)
    wgt = (mx - mn) / 255 * (mx / 255) + 0.02
    h, wd = wgt.shape
    k = wd // COLS
    sums = (img * wgt[:, :, None])[:, :k * COLS].reshape(h, COLS, k, 3).sum(axis=(0, 2))
    weights = wgt[:, :k * COLS].reshape(h, COLS, k).sum(axis=(0, 2))
    return Shot(sums, weights)


class _Grabber:
    """Reads a display into a small bitmap with GDI (kept for the thread's lifetime)."""

    def __init__(self):
        self.u32, self.g32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
        u32, g32 = self.u32, self.g32
        u32.GetDC.restype, u32.GetDC.argtypes = w.HDC, [w.HWND]
        u32.ReleaseDC.argtypes = [w.HWND, w.HDC]
        g32.CreateCompatibleDC.restype, g32.CreateCompatibleDC.argtypes = w.HDC, [w.HDC]
        g32.CreateDIBSection.restype = w.HBITMAP
        g32.CreateDIBSection.argtypes = [w.HDC, ctypes.c_void_p, w.UINT, ctypes.POINTER(ctypes.c_void_p),
                                         w.HANDLE, w.DWORD]
        g32.SelectObject.restype, g32.SelectObject.argtypes = w.HGDIOBJ, [w.HDC, w.HGDIOBJ]
        g32.DeleteObject.argtypes = [w.HGDIOBJ]
        g32.DeleteDC.argtypes = [w.HDC]
        g32.SetStretchBltMode.argtypes = [w.HDC, ctypes.c_int]
        g32.StretchBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

        self.src = u32.GetDC(None)
        self.dst = g32.CreateCompatibleDC(self.src)
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down
        bits = ctypes.c_void_p()
        self.bmp = g32.CreateDIBSection(self.dst, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not (self.src and self.dst and self.bmp):
            self.close()
            raise OSError("could not set up screen capture")
        self.old = g32.SelectObject(self.dst, self.bmp)
        g32.SetStretchBltMode(self.dst, 3)   # COLORONCOLOR: fast; the slices average it out anyway
        self.pixels = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)), (H, W, 4))

    def grab(self, rect: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """`rect` is left, top, right, bottom on the virtual screen; without it the main display."""
        if rect is None:
            rect = (0, 0, self.u32.GetSystemMetrics(0), self.u32.GetSystemMetrics(1))
        left, top, right, bottom = rect
        if not self.g32.StretchBlt(self.dst, 0, 0, W, H, self.src, left, top, right - left, bottom - top,
                                   0x00CC0020):   # SRCCOPY
            raise OSError("screen capture failed")   # e.g. the lock screen
        return self.pixels[:, :, 2::-1].copy()        # BGRA -> RGB

    def close(self) -> None:
        if getattr(self, "old", None):
            self.g32.SelectObject(self.dst, self.old)
        if self.bmp:
            self.g32.DeleteObject(self.bmp)
        if self.dst:
            self.g32.DeleteDC(self.dst)
        if self.src:
            self.u32.ReleaseDC(None, self.src)


class ScreenSampler:
    def __init__(self, fps: float = 15, display: Callable[[], str] = lambda: "",
                 monitors: Callable[[], list[Monitor]] = list_monitors,
                 full_screen: Callable[[], str | None] = full_screen_device,
                 grabber: Callable[[], object] = _Grabber, clock: Callable[[], float] = time.monotonic):
        self.interval = 1 / fps
        self.shot: Shot | None = None
        self._display = display          # the setting: "", "auto" or a display's device name
        self._monitors, self._full_screen, self._make_grabber, self._clock = monitors, full_screen, grabber, clock
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None
        self._list: list[Monitor] = []
        self._looked = float("-inf")
        self._held: str | None = None    # "auto": the display of the last full-screen window ...
        self._held_at = float("-inf")    # ... and when it was last seen
        self.choice = Choice(None, "")   # what is being sampled (for the window to say so)

    def request(self) -> Shot | None:
        """Keep sampling for the next couple of seconds and return the latest sample (None until
        the first one)."""
        self._wanted_until = self._clock() + 2.0
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="screen", daemon=True)
            self._thread.start()
        return self.shot

    def running(self) -> bool:
        return self._clock() < self._wanted_until

    def pick(self, force: bool = False) -> Choice:
        """The display to sample now. The displays (and for "auto" the full-screen window) are
        looked up every REFRESH seconds, or at once with `force` (a grab failed: a display may have
        gone, at most twice a second).

        "auto" is meant to be calm: it stays on the main display and only moves when a full-screen
        window appears on another display (the game or film you play there). Clicking in another
        window never moves it, and it stays on that display AUTO_HOLD seconds after the full-screen
        window goes, so a quick alt-tab doesn't send the lights back and forth."""
        now = self._clock()
        setting = self._display() or ""
        if now - self._looked >= (0.5 if force else REFRESH):   # (a lock screen fails every sample: not 15 looks a second)
            self._looked = now
            try:
                self._list = self._monitors()
            except Exception as e:   # (never a reason to stop the lights)
                log.debug("Can't list the displays: %s", e)
            if setting == "auto":
                try:
                    seen = self._full_screen()
                except Exception as e:
                    log.debug("Can't tell where the full-screen window is: %s", e)
                    seen = None
                if seen:
                    self._held, self._held_at = seen, now
        held = self._held if setting == "auto" and now - self._held_at < AUTO_HOLD else None
        self.choice = choose(setting, self._list, held)
        return self.choice

    def _run(self) -> None:
        try:
            with _physical_pixels():   # (this thread only)
                grabber = self._make_grabber()
                self._loop(grabber)
        except Exception as e:
            log.warning("Screen sync unavailable: %s", e)
            time.sleep(5)   # (request() starts a new try after this one ends)

    def _loop(self, grabber) -> None:
        try:
            force = True
            while self._clock() < self._wanted_until:
                start = self._clock()
                choice = self.pick(force)
                force = False
                try:
                    self.shot = weigh(grabber.grab(choice.monitor.rect if choice.monitor else None))
                except OSError:
                    force = True   # the lock screen, or a display just unplugged: keep the last picture, look again
                time.sleep(max(0.0, self.interval - (self._clock() - start)))
        finally:
            grabber.close()
