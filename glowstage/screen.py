"""Samples the main display's dominant color for the "Screen sync" effect.
Runs only while some device is using that effect."""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageGrab

from .colors import RGB, hsv, rgb_to_hsv


class ScreenSampler:
    def __init__(self, fps: float = 15):
        self.interval = 1 / fps
        self.color: RGB = (0, 0, 0)
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None

    def request(self) -> RGB:
        """Keep sampling for the next couple of seconds and return the latest color."""
        self._wanted_until = time.monotonic() + 2.0
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="screen", daemon=True)
            self._thread.start()
        return self.color

    def _run(self) -> None:
        while time.monotonic() < self._wanted_until:
            start = time.monotonic()
            try:
                self.color = self._sample()
            except OSError:
                pass  # e.g. secure desktop / lock screen
            time.sleep(max(0.0, self.interval - (time.monotonic() - start)))

    @staticmethod
    def _sample() -> RGB:
        img = ImageGrab.grab().convert("RGB")
        img = img.reduce(max(1, img.width // 128)).resize((32, 18), Image.BILINEAR)
        # Weight vivid pixels higher so a colorful scene wins over gray UI.
        tr = tg = tb = tw = 0.0
        for r, g, b in img.getdata():
            mx, mn = max(r, g, b), min(r, g, b)
            w = (mx - mn) / 255 * (mx / 255) + 0.02
            tr += r * w
            tg += g * w
            tb += b * w
            tw += w
        avg = (tr / tw, tg / tw, tb / tw)
        h, s, v = rgb_to_hsv(tuple(int(c) for c in avg))
        return hsv(h, min(1.0, s * 1.35), min(1.0, v * 1.15))
