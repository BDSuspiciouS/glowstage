"""Color helpers shared by the devices, the effect engine and the UI bridge."""

from __future__ import annotations

import colorsys
import math

RGB = tuple[int, int, int]


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def hex_to_rgb(value: str) -> RGB:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return (255, 255, 255)
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def rgb_to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(clamp(c, 0, 255)) for c in rgb))


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    """h, s, v in 0..1 (h wraps)."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, clamp(s), clamp(v))
    return (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))


def rgb_to_hsv(rgb: RGB) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def scale(rgb: RGB, f: float) -> RGB:
    f = clamp(f)
    return (int(rgb[0] * f + 0.5), int(rgb[1] * f + 0.5), int(rgb[2] * f + 0.5))


def lerp(a: RGB, b: RGB, t: float) -> RGB:
    t = clamp(t)
    return (
        int(a[0] + (b[0] - a[0]) * t + 0.5),
        int(a[1] + (b[1] - a[1]) * t + 0.5),
        int(a[2] + (b[2] - a[2]) * t + 0.5),
    )


def kelvin_to_rgb(kelvin: float) -> RGB:
    """Approximate black-body color (Tanner Helland's fit), 1000K..40000K."""
    t = clamp(kelvin, 1000, 40000) / 100
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    return (int(clamp(r, 0, 255)), int(clamp(g, 0, 255)), int(clamp(b, 0, 255)))
