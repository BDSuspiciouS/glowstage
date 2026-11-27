"""Follow the sun: the light of the day where you live, for the "Follow the sun" look.

Where you live comes from Windows' region setting (Settings → Time & language → Region), so it
works without location access or the internet: the country's middle, and for countries that span
several time zones, the longitude of your time zone. That puts sunrise and sunset within a few
minutes of the real ones, which is plenty for a light that changes over hours.

The light follows how high the sun is: cool and bright by day, warmer as it sinks, golden at
sunset, warm and dimmer in the evening, and from about 23:00 a low amber glow for the night that
lifts again at 06:00 (by the clock, since the sun rises later than most people get up in winter).
"""

from __future__ import annotations

import ctypes
import logging
import math
import time
from datetime import datetime

from .colors import RGB, kelvin_to_rgb

log = logging.getLogger(__name__)

SUNRISE = -0.833    # degrees: the sun's top edge on the horizon, with the air's refraction
# Countries that span several time zones: the middle of the country can be hours of sun away
# from where you are, so the longitude comes from your time zone instead.
WIDE = {"US", "CA", "RU", "BR", "AU", "MX", "ID", "KZ", "CD", "MN", "CN"}

# How the light follows the sun's height (degrees): color temperature and how bright. (RGB LEDs
# make low color temperatures look orange rather than warm white, so the evening stays at 2500 K.)
KELVIN = ((-6, 2500), (0, 2800), (4, 3300), (10, 4200), (20, 5200), (35, 6000))
LEVEL = ((-6, 0.62), (0, 0.75), (6, 0.9), (15, 1.0))
NIGHT_KELVIN, NIGHT_LEVEL = 1900, 0.2
GLOW = (255, 128, 40)   # the golden glow around sunrise and sunset, strongest as the sun touches the horizon
NIGHT = (22.5, 23.5, 5.5, 6.5)   # local hours: night fades in 22:30-23:30 and out 05:30-06:30


def _interp(points, x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _smooth(a: float, b: float, x: float) -> float:
    k = min(1.0, max(0.0, (x - a) / (b - a)))
    return k * k * (3 - 2 * k)


def elevation(lat: float, lon: float, ts: float) -> float:
    """How high the sun stands (degrees above the horizon) at a place and a Unix time.
    The US Naval Observatory's short formula: within about a tenth of a degree."""
    d = ts / 86400 + 2440587.5 - 2451545.0            # days since 1 January 2000, noon
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    ecl = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    tilt = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(tilt) * math.sin(ecl), math.cos(ecl))
    dec = math.asin(math.sin(tilt) * math.sin(ecl))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    hour_angle = math.radians(gmst * 15 + lon) - ra
    la = math.radians(lat)
    s = math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def _region() -> tuple[str, str, float, float] | None:
    """Windows' region setting: (country, two-letter code, latitude, longitude) of its middle."""
    try:
        k32 = ctypes.windll.kernel32
        geo = k32.GetUserGeoID(16)          # GEOCLASS_NATION
        if geo in (-1, 0):
            return None

        def info(kind: int) -> str:
            n = k32.GetGeoInfoW(geo, kind, None, 0, 0)
            buf = ctypes.create_unicode_buffer(max(n, 1))
            k32.GetGeoInfoW(geo, kind, buf, n, 0)
            return buf.value

        lat, lon = (float(info(k).replace(",", ".")) for k in (2, 3))   # GEO_LATITUDE, GEO_LONGITUDE
        return info(8) or info(4), info(4).upper(), lat, lon             # GEO_FRIENDLYNAME, GEO_ISO2
    except (AttributeError, OSError, ValueError):
        return None


class Sun:
    def __init__(self):
        self._place: tuple[str, float, float] | None = None
        self._days: dict[str, tuple[str, str]] = {}
        self._look: tuple[float, dict] = (0.0, {})

    def place(self) -> tuple[str, float, float]:
        """(name, latitude, longitude), worked out once."""
        if self._place is None:
            zone = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone) / 3600
            standard = -time.timezone / 3600 * 15      # the time zone's own longitude
            region = _region()
            if region:
                name, code, lat, lon = region
                if code in WIDE:
                    lon = standard
                self._place = (name, lat, lon)
            else:   # no region set: the time zone's longitude, halfway up the northern half
                self._place = ("your time zone", 45.0, standard)
            log.info("Follow the sun: %s (%.1f, %.1f), UTC%+g", *self._place, zone)
        return self._place

    def times(self, day: datetime) -> tuple[str, str]:
        """Sunrise and sunset ("HH:MM", local time) on a day; "" when the sun doesn't rise or set."""
        key = day.strftime("%Y-%m-%d")
        if key not in self._days:
            _, lat, lon = self.place()
            start = datetime(day.year, day.month, day.day).timestamp()
            up = lambda ts: elevation(lat, lon, ts) - SUNRISE   # noqa: E731
            rise = set_ = ""
            prev_t, prev = start, up(start)
            for k in range(1, 145):                          # every 10 minutes, then narrow it down
                t = start + k * 600
                v = up(t)
                if (prev < 0) != (v < 0):
                    a, b = prev_t, t
                    for _ in range(12):
                        m = (a + b) / 2
                        if (up(m) < 0) == (prev < 0):
                            a = m
                        else:
                            b = m
                    when = datetime.fromtimestamp((a + b) / 2).strftime("%H:%M")
                    if prev < 0 and not rise:
                        rise = when
                    elif prev >= 0:
                        set_ = when
                prev_t, prev = t, v
            self._days = {key: (rise, set_)}
        return self._days[key]

    def look(self, ts: float | None = None) -> dict:
        """The light for a moment (now by default): rgb at full strength, level 0..1, kelvin,
        what part of the day it is. Worked out at most every 5 s (the sun moves slowly)."""
        now = time.time() if ts is None else ts
        at, cached = self._look
        if ts is None and cached and now - at < 5:
            return cached
        _, lat, lon = self.place()
        e = elevation(lat, lon, now)
        local = datetime.fromtimestamp(now)
        hour = local.hour + local.minute / 60 + local.second / 3600
        a, b, c, d = NIGHT
        night = _smooth(a, b, hour) if hour >= 12 else 1 - _smooth(c, d, hour)
        night *= 1 - _smooth(-6, 0, e)                       # never while the sun is up
        kelvin = _interp(KELVIN, e) + (NIGHT_KELVIN - _interp(KELVIN, e)) * night
        level = _interp(LEVEL, e) + (NIGHT_LEVEL - _interp(LEVEL, e)) * night
        rising = elevation(lat, lon, now + 600) > e
        phase = ("night" if night > 0.5 else "evening" if e < -6 and not rising
                 else "dawn" if e < -0.833 and rising else "twilight" if e < -0.833
                 else ("sunrise" if rising else "sunset") if e < 4
                 else "golden hour" if e < 10 else "daylight")
        glow = 0.5 * math.exp(-((e - 1) / 3.5) ** 2) * (1 - night)
        rgb = tuple(int(round(v + (g - v) * glow)) for v, g in zip(kelvin_to_rgb(kelvin), GLOW))
        out = {"rgb": rgb, "level": round(level, 3), "kelvin": int(round(kelvin, -1)),
               "phase": phase, "elevation": round(e, 1)}
        if ts is None:
            self._look = (now, out)
        return out

    def describe(self) -> dict:
        name = self.place()[0]
        rise, set_ = self.times(datetime.now())
        look = self.look()
        return {"place": name, "sunrise": rise, "sunset": set_, "phase": look["phase"],
                "kelvin": look["kelvin"], "level": look["level"]}


SUN = Sun()


def color(ts: float | None = None) -> RGB:
    """The look's color right now, with its level in it."""
    look = SUN.look(ts)
    r, g, b = look["rgb"]
    k = look["level"]
    return (int(r * k), int(g * k), int(b * k))
