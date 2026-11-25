"""Samples the main display for the "Screen sync" effect: the vivid colors in each slice of the
screen from left to right, so every light can take the part of the screen where it sits on the
stage (a strip above the monitor runs along with the picture; a bulb takes the screen's color as a
whole). Runs only while some light is using that effect.

The screen is read with one GDI StretchBlt into a 128 x 72 bitmap: half the processor time of the
full-size screenshot it used to take (on a 3456 x 2169 screen ~15 ms per sample against ~34 ms)."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes as w

import numpy as np

log = logging.getLogger(__name__)

COLS = 32          # slices of the screen from left to right
W, H = 128, 72     # the size the screen is sampled at


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
    """Reads the main display into a small bitmap with GDI (kept for the thread's lifetime)."""

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

    def grab(self) -> np.ndarray:
        sw, sh = self.u32.GetSystemMetrics(0), self.u32.GetSystemMetrics(1)
        if not self.g32.StretchBlt(self.dst, 0, 0, W, H, self.src, 0, 0, sw, sh, 0x00CC0020):   # SRCCOPY
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
    def __init__(self, fps: float = 15):
        self.interval = 1 / fps
        self.shot: Shot | None = None
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None

    def request(self) -> Shot | None:
        """Keep sampling for the next couple of seconds and return the latest sample (None until
        the first one)."""
        self._wanted_until = time.monotonic() + 2.0
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="screen", daemon=True)
            self._thread.start()
        return self.shot

    def _run(self) -> None:
        try:
            grabber = _Grabber()
        except Exception as e:
            log.warning("Screen sync unavailable: %s", e)
            time.sleep(5)   # (request() starts a new try after this one ends)
            return
        try:
            while time.monotonic() < self._wanted_until:
                start = time.monotonic()
                try:
                    self.shot = weigh(grabber.grab())
                except OSError:
                    pass  # e.g. secure desktop / lock screen: keep the last picture
                time.sleep(max(0.0, self.interval - (time.monotonic() - start)))
        finally:
            grabber.close()
