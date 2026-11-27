"""Draws the app icon (a lamp lens with a spectrum ring) for the tray and the window."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .colors import hsv

SIZE = 256


GREY_RING = (112, 116, 124)   # the quiet "lights off" look: same shape, no color


@lru_cache(maxsize=None)
def icon_image(size: int = 64, tint: tuple | None = None, off: bool = False) -> Image.Image:
    """The lamp icon. `tint` (r, g, b) colors the ring with the lights' color, `off` greys it out."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    px = img.load()
    c = (SIZE - 1) / 2
    for y in range(SIZE):
        for x in range(SIZE):
            dx, dy = x - c, y - c
            r = math.hypot(dx, dy) / c
            if r > 0.98:
                continue
            if r > 0.58:  # spectrum ring
                hue = (math.atan2(dy, dx) / (2 * math.pi) + 0.25) % 1.0
                if off:
                    # (a faint shade across the ring keeps it from looking flat)
                    f = 0.9 + 0.1 * math.sin(hue * 2 * math.pi)
                    px[x, y] = (*(int(v * f) for v in GREY_RING), 255)
                elif tint is not None:
                    # (a soft shade across the ring, so it still looks like a lit ring)
                    f = 0.84 + 0.16 * (0.5 + 0.5 * math.sin(hue * 2 * math.pi))
                    px[x, y] = (*(min(255, int(v * f)) for v in tint), 255)
                else:
                    px[x, y] = (*hsv(hue, 0.9, 1.0), 255)
            elif r > 0.48:  # bezel
                px[x, y] = (27, 29, 34, 255)
            else:  # lens, lit warm white at the center
                glow = max(0.0, 1 - r / 0.48)
                if off:
                    glow *= 0.45   # dim, no light in it
                v = int(60 + 195 * glow ** 1.4)
                px[x, y] = (v, v, v, 255) if off else (v, int(v * 0.93), int(v * 0.82), 255)
    return img.resize((size, size), Image.LANCZOS)


def icon_file(directory: Path) -> Path:
    """Write a multi-size .ico once and return its path."""
    path = directory / "icon.ico"
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        icon_image(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path
