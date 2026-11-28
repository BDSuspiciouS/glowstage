"""App-driven effects. The engine renders these for every device on a shared clock, so
the PC, the WLED strips and the Nanoleaf panels animate in sync."""

from __future__ import annotations

import bisect
import math
import random

import numpy as np

from . import keys, show, sun
from .audio import quiet
from .colors import RGB, hex_to_rgb, hsv, lerp, scale
from .games import DOTA_LENGTH
from .lol import LENGTH
from .racing import LENGTH as RACING_LENGTH
from .rocket_league import BLUE as _RL_BLUE_RGB, LENGTH as RL_LENGTH, ORANGE as _RL_ORANGE_RGB
from .state import LightState


def rate(speed: int) -> float:
    """Map speed 1..100 to cycles per second on a log scale (~0.03 Hz .. 3 Hz)."""
    return 0.03 * (100 ** (speed / 100))


def _hash01(i: int, seed: int) -> float:
    h = (i * 374761393 + seed * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h ^ (h >> 16)) / 0xFFFFFFFF


def _noise(x: float, seed: int) -> float:
    """Smooth 1-D value noise in 0..1."""
    i = math.floor(x)
    f = x - i
    a, b = _hash01(i, seed), _hash01(i + 1, seed)
    f = f * f * (3 - 2 * f)
    return a + (b - a) * f


def _places(n: int, ctx: dict) -> list[float]:
    """Where each LED sits, 0..1: along the light itself, or with "Flow across all lights"
    (ctx["flow"]) along the stage, so one animation runs from light to light."""
    if ctx.get("flow"):
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        return [start + (end - start) * (i + 0.5) / n for i in range(n)]
    return [i / max(n, 1) for i in range(n)]


class Effect:
    id = ""
    name = ""
    group = "animation"   # "animation" or "music"
    uses: tuple[str, ...] = ("speed",)
    flows = False         # can run from light to light along the stage (_places)

    def render(self, t: float, n: int, st: LightState, mem: dict, ctx: dict) -> list[RGB]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "uses": list(self.uses), "group": self.group, "flows": self.flows}


class Rainbow(Effect):
    id, name = "rainbow", "Rainbow wave"
    flows = True

    def render(self, t, n, st, mem, ctx):
        shift = t * rate(st.speed) * 0.5
        return [hsv(u - shift) for u in _places(n, ctx)]


class Spectrum(Effect):
    id, name = "spectrum", "Spectrum cycle"
    flows = True

    def render(self, t, n, st, mem, ctx):
        hue = t * rate(st.speed) * 0.25
        if ctx.get("flow"):                 # each light a step behind the one on its left
            return [hsv(hue - 0.5 * u) for u in _places(n, ctx)]
        return [hsv(hue)] * n


class Breathing(Effect):
    id, name = "breathing", "Breathing"
    uses = ("speed", "color", "color2")
    flows = True

    def _one(self, phase: float, st) -> RGB:
        level = ((1 - math.cos(2 * math.pi * phase)) / 2) ** 1.5
        base = hex_to_rgb(st.color if math.floor(phase) % 2 == 0 else st.color2)
        return scale(base, 0.04 + 0.96 * level)

    def render(self, t, n, st, mem, ctx):
        phase = t * rate(st.speed) * 0.5
        if ctx.get("flow"):                 # a breath that rolls across the room
            return [self._one(phase - 0.6 * u, st) for u in _places(n, ctx)]
        return [self._one(phase, st)] * n


class Wave(Effect):
    id, name = "wave", "Color wave"
    uses = ("speed", "color", "color2")
    flows = True

    def render(self, t, n, st, mem, ctx):
        a, b = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        shift = t * rate(st.speed) * 0.5
        return [lerp(a, b, (math.sin(2 * math.pi * (u - shift)) + 1) / 2) for u in _places(n, ctx)]


# OKLab (Björn Ottosson, 2020): a color space where equal steps look equal. A gradient goes
# from one color to the other through the hues between them on the color wheel (the shorter
# way round), with lightness and colorfulness changing evenly: red to blue passes a clear
# magenta, not the dark, muddy purple a blend of the RGB numbers gives, and not the grey that
# a straight line through OKLab gives between colors far apart on the wheel.
_LMS = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                 [0.2119034982, 0.6806995451, 0.1073969566],
                 [0.0883024619, 0.2817188376, 0.6299787005]])
_LAB = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                 [1.9779984951, -2.4285922050, 0.4505937099],
                 [0.0259040371, 0.7827717662, -0.8086757660]])
_LMS_INV, _LAB_INV = np.linalg.inv(_LMS), np.linalg.inv(_LAB)
GREY = 0.02     # colorfulness below which a color (white, grey, black) has no hue of its own


def _oklch(rgb: RGB) -> tuple[float, float, float]:
    c = np.array(rgb, float) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    lab = _LAB @ np.cbrt(_LMS @ lin)
    return float(lab[0]), float(np.hypot(lab[1], lab[2])), float(np.arctan2(lab[2], lab[1]))


def blend(a: RGB, b: RGB, u) -> np.ndarray:
    """Colors from a (u = 0) to b (u = 1) as an (n, 3) array of 0..255 (see OKLab above)."""
    u = np.clip(np.asarray(u, float), 0.0, 1.0)
    (la, ca, ha), (lb, cb, hb) = _oklch(a), _oklch(b)
    if ca < GREY:           # white to red: only the red has a hue, so the white takes it on
        ha = hb
    if cb < GREY:
        hb = ha
    dh = (hb - ha + math.pi) % (2 * math.pi) - math.pi
    lum, chroma, hue = la + (lb - la) * u, ca + (cb - ca) * u, ha + dh * u
    lab = np.stack([lum, chroma * np.cos(hue), chroma * np.sin(hue)], axis=1)
    lin = np.clip((lab @ _LAB_INV.T) ** 3 @ _LMS_INV.T, 0.0, 1.0)
    return np.rint(np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055) * 255)


class Gradient(Effect):
    """A still blend from the first color to the second along each light; with Flow one blend
    across the whole stage, every light taking the part where it stands (a bulb its one color).
    It doesn't move, so a light's colors are worked out once and kept until something changes."""
    id, name = "gradient", "Gradient"
    uses = ("color", "color2")
    flows = True

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        if ctx.get("flow"):
            places = tuple(_places(n, ctx))
        else:   # end to end: the last LED is the second color (a bulb takes the first)
            places = tuple(i / (n - 1) for i in range(n)) if n > 1 else (0.0,)
        key = (st.color, st.color2, places)
        if mem.get("key") != key:
            mem["key"], mem["colors"] = key, _rgb(blend(hex_to_rgb(st.color), hex_to_rgb(st.color2), places))
        return mem["colors"]


class Comet(Effect):
    id, name = "comet", "Comet"
    uses = ("speed", "color", "color2")
    flows = True

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        head = t * rate(st.speed) * 0.5 % 1.0
        tail = 0.3 if ctx.get("flow") else max(2.0, n * 0.3) / n
        color, bg = hex_to_rgb(st.color), scale(hex_to_rgb(st.color2), 0.06)
        return [lerp(bg, color, max(0.0, 1 - ((head - u) % 1.0) / tail) ** 2) for u in _places(n, ctx)]


class Twinkle(Effect):
    id, name = "twinkle", "Starlight"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        stars = mem.get("stars")
        if stars is None or len(stars) != n:
            rnd = random.Random(n)
            stars = mem["stars"] = [(rnd.uniform(0.5, 1.5), rnd.random()) for _ in range(n)]
        r = rate(st.speed)
        color, bg = hex_to_rgb(st.color), scale(hex_to_rgb(st.color2), 0.12)
        return [lerp(bg, color, max(0.0, math.sin(2 * math.pi * (t * r * k + p))) ** 6) for k, p in stars]


FIRE = [(0, 0, 0), (120, 8, 0), (255, 50, 0), (255, 130, 10), (255, 210, 80)]


class Fire(Effect):
    id, name = "fire", "Fire"

    def render(self, t, n, st, mem, ctx):
        r = rate(st.speed) * 4
        out = []
        for i in range(n):
            heat = 0.35 + 0.65 * (0.6 * _noise(t * r + i * 3.7, 1) + 0.4 * _noise(t * r * 2.3 + i * 5.1, 2))
            pos = heat * (len(FIRE) - 1)
            k = min(int(pos), len(FIRE) - 2)
            out.append(lerp(FIRE[k], FIRE[k + 1], pos - k))
        return out


class Aurora(Effect):
    id, name = "aurora", "Aurora"
    flows = True

    def render(self, t, n, st, mem, ctx):
        r = rate(st.speed)
        out = []
        for u in _places(n, ctx):
            x = u * 3
            hue = 0.33 + 0.42 * _noise(x + t * r, 3)            # green .. violet
            val = 0.25 + 0.75 * _noise(x * 1.7 - t * r * 1.3, 4)
            out.append(hsv(hue, 0.85, val))
        return out


# ---- ambient looks: calm ones for a room in the evening ------------------------------------

def _hash_np(i: np.ndarray, seed: int) -> np.ndarray:
    """_hash01 for a whole array of integers at once (same numbers, int64 instead of Python ints)."""
    h = (i * 374761393 + seed * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h ^ (h >> 16)) / 0xFFFFFFFF


def _noise_np(x: np.ndarray, seed: int) -> np.ndarray:
    """_noise for a whole array: smooth 1-D value noise in 0..1."""
    i = np.floor(x)
    f = x - i
    i = i.astype(np.int64)
    a, b = _hash_np(i, seed), _hash_np(i + 1, seed)
    f = f * f * (3 - 2 * f)
    return a + (b - a) * f


def _ramp(v: np.ndarray, stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    """Colors along a ramp of (position, color) stops, for an array of positions: n x 3."""
    xs = [p for p, _ in stops]
    return np.stack([np.interp(v, xs, [c[k] for _, c in stops]) for k in range(3)], axis=1)


def _smooth(lo: float, hi: float, x: np.ndarray) -> np.ndarray:
    k = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return k * k * (3 - 2 * k)


CANDLE = [(0.0, (60, 12, 0)), (0.5, (235, 100, 6)), (1.0, (255, 190, 60))]   # deep amber .. warm yellow


class Candle(Effect):
    """A candle flame on every LED: layered noise for the soft flicker, each LED with its own
    phase so a strip looks like a row of candles, and now and then a gust that makes the flames
    dip and recover (it passes along the light, so the flames don't all dip at the same moment).
    Speed: how lively the flame is. Red >= green >= blue always: never white, never green."""
    id, name = "candle", "Candle"

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        k = st.speed / 100
        i = np.arange(n)
        x, off = t * (1.2 + 4.0 * k), i * 97.13
        flame = 0.5 * _noise_np(x + off, 11) + 0.3 * _noise_np(x * 2.7 + off * 1.3, 12) + 0.2 * _noise_np(x * 6.1 + off * 0.7, 13)
        level = np.clip(0.68 + (flame - 0.5) * (0.9 + 0.9 * k), 0.0, 1.0)
        period = 7.0 - 4.0 * k
        slot = math.floor(t / period)
        dip = np.zeros(n)
        for s in (slot - 1, slot):          # a gust can still be recovering from the slot before
            at = (s + 0.15 + 0.7 * _hash01(s, 21)) * period + i / n * 0.25
            d = t - at
            depth = (0.35 + 0.5 * _hash_np(i * 131 + s, 22)) * (_hash_np(i * 131 + s, 23) > 0.2)
            shape = np.where(d < 0, 0.0, np.where(d < 0.12, d / 0.12, np.exp(-(d - 0.12) / 0.7)))
            dip = np.maximum(dip, depth * shape)
        return _rgb(_ramp(level * (1 - dip), CANDLE))


OCEAN = [(0.0, (0, 18, 85)), (0.4, (0, 70, 160)), (0.75, (0, 150, 165))]
FOAM = np.array((190.0, 240.0, 250.0))   # pale cyan


class Ocean(Effect):
    """Slow waves of deep blue and teal rolling along the lights, with a little foam on the
    crests. Three waves of different lengths and a slow swell on top of them."""
    id, name = "ocean", "Ocean"
    flows = True

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        u = np.array(_places(n, ctx))
        r = rate(st.speed) * 0.6
        tau = 2 * math.pi
        w = 0.5 + 0.5 * (0.55 * np.sin(tau * (u * 1.6 - t * r * 0.5)) + 0.3 * np.sin(tau * (u * 2.9 + t * r * 0.35 + 0.3))
                         + 0.15 * np.sin(tau * (u * 5.3 - t * r * 0.8)))
        v = np.clip(w * (0.7 + 0.5 * _noise_np(u * 1.2 - t * r * 0.2, 31)), 0.0, 1.0)
        foam = (_smooth(0.8, 1.0, v) * 0.85)[:, None]
        base = _ramp(v, OCEAN)
        return _rgb(base + (FOAM - base) * foam)


LAVA_BG, LAVA_RED, LAVA_ORANGE, LAVA_MAGENTA = (np.array(c, float) for c in ((58, 8, 8), (170, 8, 24), (255, 105, 8), (225, 20, 120)))


class Lava(Effect):
    """A lava lamp: slow warm blobs that drift, merge and part along each light, on a dark warm
    background. Two noise fields moving in opposite directions make the blobs run into each
    other and pull apart. Own colors (deep red, orange, magenta) and only Speed: the colors are
    what makes it a lava lamp; with the app's usual violet and cyan it would be a different look."""
    id, name = "lava", "Lava lamp"
    flows = True

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        u = np.array(_places(n, ctx))
        r = rate(st.speed) * 0.3
        field = 0.55 * _noise_np(u * 2.4 + t * r, 5) + 0.45 * _noise_np(u * 1.5 - t * r * 0.7 + 7, 6)
        blob = _smooth(0.38, 0.6, field)[:, None]     # (this noise sits mostly between 0.25 and 0.7)
        hot = _smooth(0.48, 0.7, field)[:, None]
        tint = _noise_np(u * 0.8 + t * r * 0.3, 8)[:, None]      # which blobs lean orange, which magenta
        core = LAVA_ORANGE + (LAVA_MAGENTA - LAVA_ORANGE) * tint
        out = LAVA_BG + (LAVA_RED - LAVA_BG) * blob
        return _rgb(out + (core - out) * hot)


STORM_SEED = random.getrandbits(24)    # one per run, shared by every light: the whole room hears the same storm
CLOUD = np.array((26.0, 34.0, 54.0))   # dim blue-grey
BOLT = np.array((225.0, 235.0, 255.0))  # cold white


class Storm(Effect):
    """Dim blue-grey clouds drifting, and every 4 to 15 seconds a lightning strike: two or three
    quick flashes within about 0.4 s, now and then a far one that is dimmer. Speed: how often.
    With Flow a strike has a place: it lights up that part of the stage most and the rest a little.
    The strikes are a fixed schedule from the run's seed, so every light sees the same ones, and
    a given time always gives the same colors.

    Safety: a strike is at least 4 s after the one before, and has at most three flashes, 0.13 s or
    more apart, so there are never more than three flashes in any second (the limit for people
    sensitive to flashing light, the same as impacts.py keeps for Screen + sound).
    Slow lights (Hue, Govee: ctx["fps"] <= SLOW_FPS) update every 0.1 s or slower and could miss a
    50 ms flash, so they get one flash held for 0.3 s instead of the flicker."""
    id, name = "storm", "Thunderstorm"
    flows = True
    HOLD = 0.3

    def _strikes(self, mem: dict, t: float, speed: int) -> list[dict]:
        """The schedule, made as far as t needs: a list of strikes in order."""
        seed = mem.setdefault("seed", STORM_SEED)
        strikes = mem.setdefault("strikes", [])
        span = 15.5 - 12.0 * (speed - 1) / 99      # the gap is 4 s plus up to this much: 4..19 s slowest, 4..7.5 s fastest
        while len(strikes) > 1 and strikes[1]["at"] < t - 2.0:
            strikes.pop(0)      # (a storm left on for days mustn't keep every strike it ever made)
        while not strikes or strikes[-1]["at"] <= t + 1.0:
            k = mem["made"] = mem.get("made", 0) + 1
            at = 2.0 + 4.0 * _hash01(0, seed) if not strikes else strikes[-1]["at"] + 4.0 + span * _hash01(k, seed + 1)
            far = _hash01(k, seed + 2) < 0.3
            if far:     # a far strike: two softer, slower flashes
                offs, power, tau = [0.0, 0.16], [0.35, 0.22], 0.14
            else:
                o1 = 0.13 + 0.04 * _hash01(k, seed + 3)
                o2 = o1 + 0.13 + 0.06 * _hash01(k, seed + 4)
                if _hash01(k, seed + 5) < 0.4:
                    offs, power = [0.0, o1], [1.0, 0.7]
                else:
                    offs, power = [0.0, o1, o2], [1.0, 0.55 + 0.2 * _hash01(k, seed + 6), 0.8]
                tau = 0.07
            strikes.append({"at": at, "offs": offs, "power": power, "tau": tau, "place": _hash01(k, seed + 7), "far": far})
        return strikes

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        u = np.array(_places(n, ctx))
        clouds = 0.55 * _noise_np(u * 1.8 + t * 0.05, 41) + 0.45 * _noise_np(u * 4.1 - t * 0.08, 42)
        base = CLOUD * (0.3 + 0.7 * clouds)[:, None]
        strikes = self._strikes(mem, t, st.speed)
        starts = [s["at"] for s in strikes]
        s = strikes[max(0, bisect.bisect_right(starts, t) - 1)]
        d = t - s["at"]
        if d < 0 or d > 1.5:
            return _rgb(base)
        slow = (ctx.get("fps") or 100) <= show.SLOW_FPS
        if slow:    # one flash held long enough to be seen by a light that updates slowly
            level = s["power"][0] if d < self.HOLD else s["power"][0] * math.exp(-(d - self.HOLD) / 0.12)
        else:
            level = 0.0
            for off, power in zip(s["offs"], s["power"]):
                if d >= off:
                    level = max(level, power * math.exp(-(d - off) / s["tau"]))
        if level < 0.004:
            return _rgb(base)
        # Where it struck gets all of it, the rest of the stage a little. Without Flow the light is one place.
        w = 0.15 + 0.85 * np.exp(-((u - s["place"]) / 0.22) ** 2) if ctx.get("flow") else np.full(n, 0.8 + 0.2 * (1 - s["place"]))
        k = np.clip(level * w, 0.0, 1.0)[:, None]
        return _rgb(base + (BOLT - base) * k)


# ---- looks for the holidays --------------------------------------------------------------------

HOLIDAY_SEED = random.getrandbits(24)   # one per run, shared by every light: the whole room sees the same snow
SNOW_NIGHT = np.array((7.0, 16.0, 52.0))      # deep winter-night blue
SNOW_ICY = np.array((38.0, 78.0, 150.0))      # what a bulb shows between flakes
SNOW_FLAKE = np.array((206.0, 226.0, 255.0))  # a flake: icy white
SNOW_GLINT = np.array((255.0, 255.0, 255.0))  # a flake that catches the light
SNOW_FLAKES = 8


def _places_mid(n: int, ctx: dict) -> np.ndarray:
    """Where each LED sits, 0..1, at the middle of its slot (_places puts a light's first LED at its start)."""
    if ctx.get("flow"):
        return np.array(_places(n, ctx))
    return (np.arange(n) + 0.5) / n


def _spacing(n: int, ctx: dict) -> float:
    """How far apart the LEDs are in those units (a lone bulb: a small share of the light)."""
    if ctx.get("flow"):
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        return max(end - start, 0.01) / n
    return 1.0 / n if n > 1 else 0.1


class Snow(Effect):
    """Snowfall: a deep winter-night blue with soft white flakes drifting down. Each flake is a
    soft spot that moves slowly along the light, towards its start: strips are normally wired
    from the bottom up, so the start is the bottom (a flat strip just sees flakes drift to the
    left). The flakes have a depth: the bigger and brighter ones are nearer and fall a little
    faster. Now and then a flake catches the light and glints, slowly (about a second), never a flash.
    A bulb stays an icy blue and brightens softly as a flake passes.
    Speed: how fast it snows. A flake's place is a plain sum of time and its seed (no state), so
    every light with the seed sees the same snow, and with Flow the flakes cross from light to light."""
    id, name = "snow", "Snowfall"
    flows = True

    def _flakes(self, mem: dict) -> dict:
        seed = mem.setdefault("seed", HOLIDAY_SEED)
        flakes = mem.get("flakes")
        if flakes is None or flakes["seed"] != seed:
            k = np.arange(SNOW_FLAKES)
            depth = _hash_np(k, seed + 2)
            flakes = mem["flakes"] = {
                "seed": seed, "start": _hash_np(k, seed + 1),
                "fall": 0.025 + 0.035 * depth,                  # of the light's length per second at speed 50
                "sigma": 0.008 + 0.014 * depth, "amp": 0.5 + 0.45 * depth,
                "glints": _hash_np(k, seed + 5) < 0.6, "period": 7.0 + 8.0 * _hash_np(k, seed + 6),
                "offset": _hash_np(k, seed + 7)}
        return flakes

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        f = self._flakes(mem)
        u = _places_mid(n, ctx)
        bulb = n == 1
        few = slice(0, 6) if bulb and not ctx.get("flow") else slice(None)   # (a bulb sees a flake now and then, not a blizzard)
        speed = (rate(st.speed) / 0.3) ** 0.3        # 0.5 .. 2 times: a gentle range, so the fastest snow is still no flicker
        pos = (f["start"][few] - f["fall"][few] * speed * t) % 1.0      # (towards the start; wraps round)
        sigma = np.maximum(f["sigma"][few], 0.8 * _spacing(n, ctx))     # (never thinner than the LEDs: a flake between two would blink)
        d = (u[:, None] - pos[None, :] + 0.5) % 1.0 - 0.5
        spot = np.exp(-0.5 * (d / sigma) ** 2)
        phase = (t / f["period"][few] + f["offset"][few]) % 1.0
        glint = f["glints"][few] * np.sin(np.pi * np.clip(phase / 0.07, 0.0, 1.0)) ** 2     # ~0.5 to 1 s up and down
        amp = f["amp"][few] + (1 - f["amp"][few]) * glint
        lit = 1 - np.prod(1 - amp * spot, axis=1)                        # flakes on top of each other don't add past full
        shine = np.clip((glint * spot).sum(axis=1), 0.0, 1.0) * 0.55
        sky = (SNOW_ICY if bulb else SNOW_NIGHT) * (0.85 + 0.3 * _noise_np(u * 2.2 + t * 0.03, 51))[:, None]
        out = sky + (SNOW_FLAKE - sky) * lit[:, None]
        return _rgb(out + (SNOW_GLINT - out) * shine[:, None])


BULBS = np.array(((240.0, 24.0, 16.0), (24.0, 190.0, 52.0), (255.0, 160.0, 24.0), (24.0, 70.0, 255.0)))   # red, green, gold, blue


class StringLights(Effect):
    """A string of colored bulbs: LEDs in groups of two to four lit in turn red, green, gold and
    blue, with a dark gap between the bulbs (on a light of 10 LEDs or more; a short one is a bulb
    per LED). Each bulb dims and brightens on its own, slowly, never off and never a flash.
    With Flow the bulbs are placed along the whole stage, so the string runs on from light to
    light. A bulb light cycles through the four colors, fading from one to the next.
    Speed: how lively the twinkle is (and how fast a bulb light changes color)."""
    id, name = "string_lights", "Holiday lights"
    flows = True
    STAGE_PITCH = 0.035         # with Flow: a bulb every 3.5 % of the stage

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        seed = mem.setdefault("seed", HOLIDAY_SEED)
        speed = (rate(st.speed) / 0.3) ** 0.5       # 0.3 .. 3.2 times
        depth = 0.15 + 0.35 * st.speed / 100        # how far a bulb dims: a sixth .. half
        if n == 1:      # a bulb: the four colors in turn, a fade after a rest on each
            u = float(_places_mid(1, ctx)[0]) if ctx.get("flow") else 0.0
            pos = t * rate(st.speed) * 0.5 - (4 * u if ctx.get("flow") else 0.0)    # (with Flow each bulb of the room a step ahead)
            i = math.floor(pos)
            mix = float(_smooth(0.55, 1.0, np.array([pos - i]))[0])
            color = blend(tuple(BULBS[i % 4]), tuple(BULBS[(i + 1) % 4]), [mix])[0]
            wave = math.sin(2 * math.pi * (t * (0.08 + 0.2 * _hash01(0, seed + 1)) * speed + _hash01(0, seed + 2)))
            return _rgb((color * (1 - depth * (0.5 + 0.5 * wave) * 0.6))[None, :])
        if ctx.get("flow"):
            x = _places_mid(n, ctx) / self.STAGE_PITCH
            plain = False
        else:
            pitch = 6 if n >= 24 else 3 if n >= 10 else 1      # LEDs per bulb and gap
            x = (np.arange(n) + 0.5) / pitch
            plain = pitch == 1
        bulb = np.floor(x).astype(np.int64)
        frac = x - bulb
        if plain:    # a short light: every LED a bulb, no gaps
            shape = np.ones(n)
        else:           # lit for the first two thirds of the slot, soft at both ends, dark for the rest
            shape = _smooth(0.0, 0.14, frac) * (1 - _smooth(0.54, 0.68, frac))
        freq = (0.08 + 0.2 * _hash_np(bulb, seed + 1)) * speed
        wave = np.sin(2 * math.pi * (t * freq + _hash_np(bulb, seed + 2)))
        return _rgb(BULBS[bulb % 4] * (shape * (1 - depth * (0.5 + 0.5 * wave)))[:, None])


# What the candy cane is when the colors are still the app's usual violet and cyan: red and white.
APP_COLORS = (LightState().color.lower(), LightState().color2.lower())
CANE_RED, CANE_WHITE = (222, 18, 36), (255, 246, 238)


class CandyCane(Effect):
    """Stripes of the first color and the second moving slowly along the light, with soft edges,
    like a candy cane or a barber pole. While the colors are still the app's usual violet and
    cyan it is red and white; pick two colors for green and white, or anything else. A bulb fades
    between the two. With Flow the stripes run on across the whole stage. Speed: how fast they move.
    Nothing here is random, so every light shows the same stripes at the same time."""
    id, name = "candy_cane", "Candy cane"
    uses = ("speed", "color", "color2")
    flows = True
    STAGE_PAIRS = 8         # with Flow: a pair of stripes is an eighth of the stage

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        if (st.color.lower(), st.color2.lower()) == APP_COLORS:
            a, b = np.array(CANE_RED, float), np.array(CANE_WHITE, float)
        else:
            a, b = np.array(hex_to_rgb(st.color), float), np.array(hex_to_rgb(st.color2), float)
        pairs = self.STAGE_PAIRS if ctx.get("flow") else min(max(n / 16, 1.0), 24.0)    # about 8 LEDs a stripe
        x = _places_mid(n, ctx) * pairs - t * rate(st.speed) * 0.35
        wave = np.cos(2 * math.pi * x)
        if n == 1:      # a bulb: a plain fade
            m = 0.5 + 0.5 * wave
        else:           # stripes: a flattened wave, so the colors hold and the edges are soft
            m = _smooth(0.0, 1.0, 0.5 + 0.5 * np.clip(1.8 * wave, -1.0, 1.0))
        return _rgb(a + (b - a) * m[:, None])


# ---- more everyday looks: a summer night, rain, plasma and a sunset -----------------------------

NIGHT_SEED = random.getrandbits(24)    # one per run, shared by every light: the whole room sees the same fireflies and rain
DUSK = np.array((3.0, 20.0, 27.0))            # a summer night, green-blue and dark
DUSK_BULB = np.array((5.0, 28.0, 36.0))
FIREFLY = np.array((176.0, 220.0, 52.0))      # a soft yellow-green
FIREFLIES = 7


def _seed_of(mem: dict) -> int:
    return mem.setdefault("seed", NIGHT_SEED)


def _pace(speed: int, power: float) -> float:
    """1 at speed 50; a gentle range either side (a low power keeps the fastest setting calm)."""
    return (rate(speed) / 0.3) ** power


class Fireflies(Effect):
    """A summer night: a deep green-blue dusk with a handful of soft yellow-green glows. Each one
    slowly lights up, drifts a little along the light and fades, on its own rhythm, then rests and
    lights up again somewhere else. A bulb glows softly now and then. Speed: how quick the rhythms are.
    A firefly's place and brightness are a plain function of time and the run's seed (no state), so
    every light with the seed sees the same fireflies, and with Flow they live on the whole stage.
    Each cycle starts and ends dark, so a firefly never pops in or out."""
    id, name = "fireflies", "Fireflies"
    flows = True

    def _table(self, mem: dict) -> dict:
        seed = _seed_of(mem)
        table = mem.get("fireflies")
        if table is None or table["seed"] != seed:
            k = np.arange(FIREFLIES)
            table = mem["fireflies"] = {
                "seed": seed, "k": k, "period": 9.0 + 8.0 * _hash_np(k, seed + 1), "offset": _hash_np(k, seed + 2),
                "duty": 0.5 + 0.15 * _hash_np(k, seed + 3),         # the lit share of a cycle; the rest is rest
                "sigma": 0.02 + 0.015 * _hash_np(k, seed + 4), "drift": (_hash_np(k, seed + 5) - 0.5) * 0.12}
        return table

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        f = self._table(mem)
        seed = f["seed"]
        bulb = n == 1 and not ctx.get("flow")
        few = slice(0, 2) if bulb else slice(None)      # (a bulb glows now and then, not all the time)
        k, duty = f["k"][few], f["duty"][few]
        phase = t * _pace(st.speed, 0.35) / f["period"][few] + f["offset"][few]
        cycle = np.floor(phase)
        x = (phase - cycle) / duty                      # 0..1 while it is lit, past 1 while it rests
        env = np.sin(np.pi * np.clip(x, 0.0, 1.0)) ** 2
        cell = k * 7919 + cycle.astype(np.int64)        # (a new place and strength every cycle)
        amp = 0.6 + 0.4 * _hash_np(cell, seed + 7)
        if bulb:    # it lights up in only about every other cycle of either of two fireflies
            amp = amp * (_hash_np(cell, seed + 9) < 0.55)
            lit = 1 - np.prod(1 - 0.8 * amp * env)
            return _rgb((DUSK_BULB + (FIREFLY - DUSK_BULB) * lit)[None, :])
        u = _places_mid(n, ctx)
        pos = 0.05 + 0.9 * _hash_np(cell, seed + 6) + f["drift"][few] * (x - 0.5)
        sigma = np.maximum(f["sigma"][few], 0.8 * _spacing(n, ctx))     # (never thinner than the LEDs)
        spot = np.exp(-0.5 * ((u[:, None] - pos[None, :]) / sigma) ** 2)
        lit = 1 - np.prod(1 - amp * env * spot, axis=1)
        sky = DUSK * (0.8 + 0.4 * _noise_np(u * 2.0 + t * 0.02, 61))[:, None]
        return _rgb(sky + (FIREFLY - sky) * lit[:, None])


RAIN_SKY = np.array((13.0, 21.0, 38.0))      # dark slate-blue
RAIN_DROP = np.array((150.0, 190.0, 250.0))
RAIN_SPLASH = np.array((190.0, 222.0, 255.0))
RAIN_LOW, RAIN_HIGH = np.array((22.0, 38.0, 70.0)), np.array((110.0, 158.0, 235.0))     # what a bulb shows, at its dimmest and brightest
RAIN_DROPS = 9


class Rain(Effect):
    """Rain: a dark slate-blue with thin drops running down the light, each a short bright head with
    a tail that fades behind it. They run towards the start of a light, the same way as Snowfall's
    flakes (strips are normally wired from the bottom up), but quicker and thinner, and the nearer
    ones are brighter. Some drops end in a splash: a soft glow at the start that spreads and fades.
    A bulb stays a cool blue and brightens softly now and then as a drop passes. Speed: how hard it
    rains. A drop's place is a plain function of time and the run's seed, so every light with the seed
    sees the same rain, and with Flow the drops run on from light to light."""
    id, name = "rain", "Rain"
    flows = True

    def _drops(self, mem: dict) -> dict:
        seed = _seed_of(mem)
        drops = mem.get("drops")
        if drops is None or drops["seed"] != seed:
            k = np.arange(RAIN_DROPS)
            depth = _hash_np(k, seed + 2)
            drops = mem["drops"] = {
                "seed": seed, "start": _hash_np(k, seed + 1), "fall": 0.07 + 0.07 * depth,   # of the length per second at speed 50
                "sigma": 0.013 + 0.006 * depth, "tail": 0.025 + 0.03 * depth, "amp": 0.45 + 0.35 * depth,
                "splash": (_hash_np(k, seed + 5) < 0.3) * (0.5 + 0.4 * _hash_np(k, seed + 6)),
                "period": 6.0 + 5.0 * _hash_np(k, seed + 7), "offset": _hash_np(k, seed + 8)}
        return drops

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        f = self._drops(mem)
        seed = f["seed"]
        speed = _pace(st.speed, 0.25)
        if n == 1 and not ctx.get("flow"):     # a bulb: a cool blue that drifts, and brightens softly as a drop passes
            phase = (t * speed / f["period"][:4] + f["offset"][:4]) % 1.0
            pulse = f["amp"][:4] * 0.55 * np.sin(np.pi * np.clip(phase / 0.3, 0.0, 1.0)) ** 2
            level = np.clip(0.3 * float(_noise_np(np.array([t * 0.08]), seed + 3)[0]) + pulse.sum(), 0.0, 1.0)
            return _rgb((RAIN_LOW + (RAIN_HIGH - RAIN_LOW) * level)[None, :])
        u = _places_mid(n, ctx)
        space = _spacing(n, ctx)
        # On a short light a drop is a few whole LEDs wide, so fewer of them run at once (the count follows
        # the LED spacing, not the LED count, so with Flow two halves of a stage still show one rain).
        f = {key: v[:int(np.clip(0.28 / (4.5 * space), 2, RAIN_DROPS))] if isinstance(v, np.ndarray) else v for key, v in f.items()}
        pace = f["fall"] * speed
        pos = (f["start"] - pace * t) % 1.0                              # (towards the start; wraps round)
        sigma = np.maximum(f["sigma"], 2 * space)                         # (thin, but never thinner than the LEDs)
        tail = np.maximum(f["tail"], 3 * space)
        d = u[:, None] - pos[None, :]
        head = np.exp(-0.5 * (d / sigma) ** 2)
        behind = np.exp(-np.maximum(d, 0.0) / tail) * (0.3 + 0.7 * head)     # (d > 0: behind the head, further from the start)
        shape = np.where(d < 0, head, behind)
        gate = _smooth(0.0, 0.05, pos) * (1 - _smooth(0.95, 1.0, pos))      # a drop fades out as it lands and in as it starts
        lit = 1 - np.prod(1 - f["amp"] * gate * shape, axis=1)
        # A splash starts when a drop has just run off the start (it comes in again at the far end).
        age = (1.0 - pos) / np.maximum(pace, 1e-6)
        spread = 0.015 + 0.03 * np.minimum(age, 1.5)
        glow = f["splash"] * _smooth(0.0, 0.15, age) * np.exp(-age / 0.45) * (age < 2.5)
        splash = 1 - np.prod(1 - glow * np.exp(-0.5 * (np.maximum(u[:, None] - 0.01, 0.0) / np.maximum(spread, space)) ** 2), axis=1)
        sky = RAIN_SKY * (0.8 + 0.4 * _noise_np(u * 2.0 + t * 0.04, 71))[:, None]
        out = sky + (RAIN_DROP - sky) * lit[:, None]
        return _rgb(out + (RAIN_SPLASH - out) * (splash * 0.6)[:, None])


PLASMA_MAGENTA, PLASMA_CYAN = (255, 30, 170), (0, 215, 255)     # the plasma of the app's usual violet and cyan: these, with violet between


class Plasma(Effect):
    """Smooth flowing fields of color, like an old plasma screensaver: three slow sine fields of
    different lengths and speeds are mixed, and the mix picks a color between the first color and the
    second (through the hues between them, as Gradient does), while another slow wave dims and
    brightens it a little. While the colors are still the app's usual violet and cyan it is magenta,
    violet and cyan; pick two colors for anything else. With Flow one plasma covers the whole stage.
    Speed: how fast the fields move. Only the fields' starting points are random: they come from the
    run's seed, so every light shows the same plasma at the same time."""
    id, name = "plasma", "Plasma"
    uses = ("speed", "color", "color2")
    flows = True

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        seed = _seed_of(mem)
        if (st.color.lower(), st.color2.lower()) == APP_COLORS:
            a, b = PLASMA_MAGENTA, PLASMA_CYAN
        else:
            a, b = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        u = _places_mid(n, ctx)
        r, tau = rate(st.speed) * 0.35, 2 * math.pi
        p = [_hash01(i, seed + 8) for i in range(6)]
        f1 = np.sin(tau * (u * 1.3 + t * r * 0.6 + p[0]))
        f2 = np.sin(tau * (u * 2.1 - t * r * 0.45 + p[1]))
        f3 = np.sin(tau * (u * 0.7 + 0.35 * math.sin(tau * (t * r * 0.3 + p[2])) + t * r * 0.2 + p[3]))
        mix = _smooth(0.1, 0.9, 0.5 + 0.5 * np.clip(1.35 * (0.4 * f1 + 0.35 * f2 + 0.25 * f3), -1.0, 1.0))
        glow = 0.78 + 0.22 * (0.5 + 0.5 * np.sin(tau * (u * 1.7 - t * r * 0.35 + p[4])))
        return _rgb(blend(a, b, mix) * glow[:, None])


SUN_ORANGE, SUN_PINK, SUN_VIOLET = (232, 70, 4), (238, 56, 138), (62, 46, 208)
SUN_GLOW = np.array((255, 150, 30), float)
SUNSET = [(0.0, SUN_ORANGE), (0.5, SUN_PINK), (1.0, SUN_VIOLET)]


class Sunset(Effect):
    """A sunset: a slow gradient from deep orange at the start of a light, through pink, to a
    violet-blue at the end, sliding very slowly to and fro, with a soft golden glow where the sun sits
    low, near the orange end. A bulb slowly breathes through the same colors. With Flow one sunset
    spans the whole stage. Speed: how slowly it drifts. Only where the drift starts is random, and
    that comes from the run's seed, so every light shows the same sky at the same time."""
    id, name = "sunset", "Sunset"
    flows = True

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        seed = _seed_of(mem)
        p, tau = rate(st.speed) * 0.25, 2 * math.pi
        ph = [_hash01(i, seed + 9) for i in range(3)]
        if n == 1 and not ctx.get("flow"):      # a bulb: out through the whole sky and back
            x = np.array([0.5 - 0.5 * math.cos(tau * (t * p * 0.5 + ph[0]))])
            sun = (0.35 * (1 - x) ** 2)[:, None]
            base = _ramp(x, SUNSET)
            return _rgb(base + (SUN_GLOW - base) * sun)
        u = _places_mid(n, ctx)
        x = np.clip(0.07 + 0.86 * u + 0.06 * math.sin(tau * (t * p * 0.5 + ph[0]))
                    + 0.05 * (_noise_np(u * 1.5 + t * p * 0.3, seed + 4) - 0.5), 0.0, 1.0)
        base = _ramp(x, SUNSET) * (0.85 + 0.15 * _noise_np(u * 2.5 - t * p * 0.2, seed + 5))[:, None]
        at = 0.16 + 0.06 * math.sin(tau * (t * p * 0.35 + ph[1]))       # where the sun sits, low at the orange end
        glow = (0.55 * np.exp(-0.5 * ((u - at) / 0.14) ** 2))[:, None]
        return _rgb(base + (SUN_GLOW - base) * glow)


class Typing(Effect):
    """Every key you press lights up and sends a ripple across the keyboard (where its LEDs are
    known: fixture "geometry"); every other light glows where the key sits on the board, left to
    right, with a ripple running both ways. Speed: how quickly it fades. Keys from keys.py."""
    id, name = "typing", "Typing ripples"
    uses = ("speed", "color", "color2")
    SPAN = 10.0     # a strip, a fan chain: as long as ten keys

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        color, bg = np.array(hex_to_rgb(st.color), float), np.array(scale(hex_to_rgb(st.color2), 0.08), float)
        tau = 1.6 * 0.12 ** (st.speed / 100)      # how long a key glows: 1.6 s (slow) .. 0.2 s (fast)
        geo = (ctx.get("fixture") or {}).get("geometry")
        if mem.get("n") != n or mem.get("geo") is not geo:
            if geo and len(geo) == n:            # LED positions in key widths
                mem["pts"] = np.array(geo, float) * (keys.WIDTH, keys.HEIGHT)
            else:                                 # along the light
                mem["pts"] = np.stack([(np.arange(n) + 0.5) / n * self.SPAN, np.zeros(n)], axis=1)
            mem["n"], mem["geo"], mem["board"] = n, geo, bool(geo and len(geo) == n)
        pts = mem["pts"]
        level = np.zeros(n)
        for age, x, y in ctx.get("keys") or ():
            if mem["board"]:
                d = np.hypot(pts[:, 0] - x * keys.WIDTH, pts[:, 1] - y * keys.HEIGHT)
            else:
                d = np.abs(pts[:, 0] - x * self.SPAN)
            glow = np.exp(-(d / 0.6) ** 2) * math.exp(-age / tau)
            ring = 0.6 * np.exp(-((d - 6.0 * age / tau) / 1.0) ** 2) * math.exp(-age / (1.2 * tau))
            level += glow + ring
        level = np.clip(level, 0.0, 1.0)[:, None]
        out = bg + (color - bg) * level
        return [(int(r), int(g), int(b)) for r, g, b in out]


class PcLoad(Effect):
    """How busy the PC is, as a bar along each light: filled up to the load, from the first color
    (idle) towards the second (busy) as the load rises; the rest glows dimly in the first color.
    With "Flow across all lights" one bar runs across the whole stage. The graphics card (ctx["gpu"])
    shows its own load, every other light the processor's. Loads from load.py."""
    id, name = "pc_load", "PC load"
    uses = ("color", "color2")
    flows = True
    GLIDE = 0.15        # seconds: the bar settles on a new load in ~0.4 s, so it never jitters

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        cpu, gpu = ctx.get("load") or (None, None)
        target = gpu if ctx.get("gpu") and gpu is not None else cpu
        target = min(1.0, max(0.0, target or 0.0))
        level = mem.get("level")
        if level is None:
            level = target
        else:
            level += (target - level) * (1 - math.exp(-max(0.0, t - mem.get("t", t)) / self.GLIDE))
        mem["level"], mem["t"] = level, t
        idle, busy = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        fill, dim = lerp(idle, busy, level), scale(idle, 0.12)
        if ctx.get("flow"):                 # the bar spans the stage; each LED is a slice of it
            start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
            width = (end - start) / n
            cover = [(level - (start + i * width)) / width for i in range(n)]
        else:
            cover = [level * n - i for i in range(n)]
        out = []
        for c in cover:                     # the LED at the bar's tip is only partly lit
            out.append(lerp(dim, fill, min(1.0, max(0.0, c))))
        return [(int(r), int(g), int(b)) for r, g, b in out]


class Daylight(Effect):
    """The light of the day where you live (sun.py): cool and bright by day, golden as the sun
    sets, warm in the evening, a low amber glow at night. One calm color for the whole room."""
    id, name = "daylight", "Follow the sun"
    uses = ()

    def render(self, t, n, st, mem, ctx):
        return [sun.color()] * n


class Screen(Effect):
    id, name = "screen", "Screen sync"
    uses = ()

    def render(self, t, n, st, mem, ctx):
        """Every light takes the part of the screen where it sits on the stage: a strip above
        the monitor follows the picture from left to right, a bulb takes the whole screen."""
        shot = ctx.get("screen")
        if shot is None:
            target = np.zeros((n, 3))
        else:
            start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
            target = shot.colors(start, end, n)
        cur = mem.get("colors")
        if cur is None or len(cur) != n:
            cur = target
        else:   # glide (~0.08 s) so the lights don't flicker with every cut and scroll
            k = 1 - math.exp(-max(0.0, t - mem.get("t", t)) / 0.08)
            cur = cur + (target - cur) * k
        mem["colors"], mem["t"] = cur, t
        return [(int(r), int(g), int(b)) for r, g, b in cur]


FLASH = np.array((255.0, 236.0, 210.0))   # a muzzle flash: warm white


class ScreenSound(Screen):
    """Screen sync, and every loud, sudden sound (a shot, an explosion: impacts.py) flashes the
    lights on the side it came from: a shot on your left lights the left of the stage. Lights
    behind you (the "back" of the stage) flash along a little, wherever it came from."""
    id, name = "screen_sound", "Screen + sound"
    uses = ()

    def render(self, t, n, st, mem, ctx):
        base = super().render(t, n, st, mem, ctx)
        hits = [(age, s, p) for age, s, p in (ctx.get("audio") or {}).get("impacts") or () if age < 0.6]
        if not hits or n == 0:
            return base
        start, end, layer = ctx.get("span", (0.0, 1.0, "front"))
        u = start + (end - start) * (np.arange(n) + 0.5) / n          # where each LED sits, 0 = left
        flash = np.zeros(n)
        for age, strength, pan in hits:
            k = strength * math.exp(-age / 0.09)
            if layer == "back":
                w = np.full(n, 0.55)
            else:
                w = 0.2 + 0.8 * np.exp(-((u - (0.5 + 0.5 * pan)) / 0.3) ** 2)
            flash = np.maximum(flash, k * w)
        rgb = np.array(base, float)
        out = rgb + (FLASH - rgb) * np.clip(flash, 0, 1)[:, None]
        return [(int(r), int(g), int(b)) for r, g, b in out]


# ---- music: these react to whatever the PC is playing (see audio.py) -----------------------

def _audio(ctx: dict) -> dict:
    return ctx.get("audio") or quiet()


class MusicEffect(Effect):
    group = "music"


class BeatPulse(MusicEffect):
    id, name = "music_pulse", "Beat pulse"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        a = _audio(ctx)
        if a["silent"]:
            return [scale(hex_to_rgb(st.color), 0.3 + 0.08 * math.sin(t * 1.2))] * n
        base = hex_to_rgb(st.color if a["beat_count"] % 2 == 0 else st.color2)
        decay = 0.08 + 0.45 * (1 - st.speed / 100)        # higher speed = shorter, punchier flash
        flash = math.exp(-a["since_beat"] / decay)
        return [scale(base, max(0.12 + 0.35 * a["level"], flash))] * n


class MusicSpectrum(MusicEffect):
    id, name = "music_spectrum", "Spectrum"

    def render(self, t, n, st, mem, ctx):
        a = _audio(ctx)
        bands = a["bands"]
        if a["silent"]:
            return [hsv(i / max(n, 1) * 0.78, 1, 0.12) for i in range(n)]
        if n == 1:  # one light: the color of the loudest band
            k = max(range(len(bands)), key=bands.__getitem__)
            return [hsv(k / (len(bands) - 1) * 0.78, 1, 0.15 + 0.85 * a["level"])]
        out = []
        for i in range(n):
            p = i / (n - 1)                       # bass on the left, treble on the right
            f = p * (len(bands) - 1)
            k = min(int(f), len(bands) - 2)
            v = bands[k] + (bands[k + 1] - bands[k]) * (f - k)
            out.append(hsv(p * 0.78, 1, 0.05 + 0.95 * v))
        return out


def _glide(mem: dict, key: str, target: float, t: float, rise: float, fall: float | None = None) -> float:
    """Move mem[key] smoothly toward target: time constant `rise` going up, `fall` going down."""
    last_t = mem.get(key + "_t", t)
    value = mem.get(key, target)
    tau = rise if target >= value or fall is None else fall
    value += (target - value) * (1 - math.exp(-max(0.0, t - last_t) / max(tau, 1e-3)))
    mem[key], mem[key + "_t"] = value, t
    return value


class Party(MusicEffect):
    id, name = "music_party", "Party"

    def render(self, t, n, st, mem, ctx):
        a = _audio(ctx)
        punch = st.speed / 100
        if a["silent"]:
            hue = _glide(mem, "hue", mem.get("hue", 0.0) + 0.02, t, 0.5)
            return [hsv(hue + 0.1 * i / max(n, 1), 1, 0.35) for i in range(n)]
        # Each beat moves the color a step around the wheel; the lights glide there instead of jumping.
        step = 0.05 + 0.07 * punch
        hue = _glide(mem, "hue", a["beat_count"] * step, t, 0.6 - 0.4 * punch)
        env = _glide(mem, "env", a["level"], t, 0.08, 0.4)
        swell = math.exp(-a["since_beat"] / (0.25 + 0.35 * (1 - punch)))
        v = min(1.0, 0.4 + 0.35 * env + (0.15 + 0.15 * punch) * swell)
        return [hsv(hue + 0.1 * i / max(n, 1), 1, v) for i in range(n)]


class BassRipple(MusicEffect):
    id, name = "music_ripple", "Bass ripple"

    def render(self, t, n, st, mem, ctx):
        a = _audio(ctx)
        travel = 0.6 + 1.6 * st.speed / 100          # strip lengths per second
        base = 0.05 + (0.0 if a["silent"] else 0.18 * a["level"])
        out = []
        for i in range(n):
            x = i / max(n - 1, 1)
            v = base
            for age in a["beats_ago"]:
                d = abs(x - age * travel)
                if d < 0.12:
                    v += (1 - d / 0.12) * math.exp(-age * 1.2)
            out.append(hsv(t * 0.04 + x * 0.35, 1, min(1.0, v)))
        return out


class VuMeter(MusicEffect):
    id, name = "music_vu", "VU meter"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        a = _audio(ctx)
        low, high = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        level = a["level"]
        peak = mem["peak"] = max(level, mem.get("peak", 0.0) - 0.012)
        if n <= 2:
            return [scale(lerp(low, high, level), 0.1 + 0.9 * level)] * n
        lit, top = level * n, min(n - 1, int(peak * n))
        out = []
        for i in range(n):
            x = i / (n - 1)
            if i < lit:
                out.append(lerp(low, high, x))
            elif i == top and peak > 0.05:
                out.append((255, 255, 255))
            else:
                out.append(scale(low, 0.04))
        return out


class Romantic(MusicEffect):
    id, name = "music_romantic", "Romantic"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        """Your two colors flowing slowly; brightness rides the music, bass leans toward the
        second color, and every beat sends a soft swell outward from the middle. No flashes."""
        a = _audio(ctx)
        c1, c2 = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        env = _glide(mem, "env", 0.3 if a["silent"] else a["level"], t, 0.18, 0.7)
        bass = _glide(mem, "bass", 0.0 if a["silent"] else a["bass"], t, 0.2, 0.8)
        r = rate(st.speed) * 0.25
        out = []
        for i in range(n):
            x = i / max(n - 1, 1)
            mix = 0.5 + 0.35 * math.sin(2 * math.pi * (x * 1.2 - t * r)) + 0.5 * (bass - 0.35)
            swell = 0.0
            for age in a["beats_ago"]:
                d = abs(abs(x - 0.5) * 2 - age * 0.9)   # rings moving out from the center
                if d < 0.35:
                    swell += (1 - d / 0.35) * math.exp(-age * 1.4) * 0.35
            texture = 0.85 + 0.15 * _noise(x * 3 + t * r * 2, 6)
            v = min(1.0, 0.12 + 0.88 * env * texture + swell)
            out.append(scale(lerp(c1, c2, min(1.0, max(0.0, mix))), v))
        return out


class ColorOrgan(MusicEffect):
    id, name = "music_organ", "Color organ"

    def render(self, t, n, st, mem, ctx):
        """The classic: bass lights red, mids green, treble blue. Along a strip the bass end
        sits on the left and the treble end on the right."""
        a = _audio(ctx)
        punch = st.speed / 100
        fall = 0.45 - 0.3 * punch
        b = _glide(mem, "b", a["bass"], t, 0.04, fall)
        m = _glide(mem, "m", a["mid"], t, 0.04, fall)
        h = _glide(mem, "h", a["treble"], t, 0.04, fall)
        if a["silent"]:
            b = m = h = 0.12
        out = []
        for i in range(n):
            x = i / max(n - 1, 1)
            r = b * (0.55 + 0.45 * (1 - x))
            g = m * (0.55 + 0.45 * (1 - abs(x - 0.5) * 2))
            bl = h * (0.55 + 0.45 * x)
            out.append((int(min(1.0, r) * 255), int(min(1.0, g) * 255), int(min(1.0, bl) * 255)))
        return out


class CenterBloom(MusicEffect):
    id, name = "music_bloom", "Center bloom"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        """A bar that grows out from the middle with the volume, color 1 in the center fading
        to color 2 at the edges; the last peak lingers as a soft glow."""
        a = _audio(ctx)
        c1, c2 = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        kick = math.exp(-a["since_beat"] / 0.25)
        target = 0.08 if a["silent"] else min(1.0, 0.75 * a["level"] + 0.35 * kick * a["bass"])
        size = _glide(mem, "size", target, t, 0.05, 0.35 - 0.25 * st.speed / 100)
        peak = mem["peak"] = max(size, mem.get("peak", 0.0) - 0.01)
        if n <= 2:
            return [scale(lerp(c1, c2, size), 0.1 + 0.9 * size)] * n
        out = []
        for i in range(n):
            d = abs(i / (n - 1) - 0.5) * 2               # 0 in the middle, 1 at the ends
            if d <= size:
                out.append(lerp(c1, c2, d))
            elif d <= peak:
                out.append(scale(c2, 0.25 * (1 - (d - size) / max(peak - size, 1e-3))))
            else:
                out.append(scale(c1, 0.04))
        return out


class Sparkle(MusicEffect):
    id, name = "music_sparkle", "Sparkle"

    def render(self, t, n, st, mem, ctx):
        """Every beat throws a shower of colored sparks; the treble adds fine white glitter."""
        a = _audio(ctx)
        rnd = mem.setdefault("rnd", random.Random(n * 7919))
        sparks: dict = mem.setdefault("sparks", {})
        last = mem.get("beat", a["beat_count"])
        decay = 0.25 + 0.6 * (1 - st.speed / 100)
        if not a["silent"]:
            if a["beat_count"] != last:                  # new beat: a burst of sparks
                for _ in range(max(1, int(n * (0.12 + 0.2 * a["bass"])))):
                    sparks[rnd.randrange(n)] = (t, rnd.random(), 1.0)
            if rnd.random() < a["treble"] * 0.9:         # glitter rides the hi-hats
                for _ in range(max(1, n // 40)):
                    sparks[rnd.randrange(n)] = (t, 0.0, 0.0)  # saturation 0 = white
        elif rnd.random() < 0.05:
            sparks[rnd.randrange(n)] = (t, rnd.random(), 0.6)
        mem["beat"] = a["beat_count"]
        base = 0.03 + (0.0 if a["silent"] else 0.06 * a["level"])
        out = [hsv(0.75, 0.6, base)] * n
        for led, (born, hue, sat) in list(sparks.items()):
            v = math.exp(-(t - born) / (decay if sat else decay * 0.5))
            if v < 0.03 or led >= n:
                del sparks[led]
                continue
            out[led] = hsv(hue, sat, max(base, v))
        return out


class Meteor(MusicEffect):
    id, name = "music_meteor", "Meteor"

    def render(self, t, n, st, mem, ctx):
        """Each beat launches a meteor with a tail, alternately from the left and the right."""
        a = _audio(ctx)
        travel = 0.9 + 1.8 * st.speed / 100             # strip lengths per second
        tail = 0.28
        out = [(0, 0, 0)] * n if n else []
        if n == 0:
            return out
        base = scale(hsv(t * 0.02, 0.8, 1), 0.03 + (0.0 if a["silent"] else 0.05 * a["level"]))
        out = [base] * n
        ages = a["beats_ago"]
        first = a["beat_count"] - len(ages) + 1
        for j, age in enumerate(ages):
            index = first + j
            head = age * travel
            if head > 1 + tail:
                continue
            color = hsv(_hash01(index, 11), 0.9, 1)
            fade = math.exp(-age * 0.6)
            for i in range(n):
                x = i / max(n - 1, 1)
                pos = x if index % 2 == 0 else 1 - x       # alternate direction
                d = head - pos
                if 0 <= d < tail:
                    v = (1 - d / tail) ** 2 * fade
                    c = scale(color, v)
                    out[i] = tuple(max(p, q) for p, q in zip(out[i], c))
        return out


class MusicFire(MusicEffect):
    id, name = "music_fire", "Music fire"

    def render(self, t, n, st, mem, ctx):
        """Flames that burn higher and flicker faster the louder the music gets."""
        a = _audio(ctx)
        env = _glide(mem, "env", 0.25 if a["silent"] else a["level"], t, 0.08, 0.5)
        kick = 0.0 if a["silent"] else math.exp(-a["since_beat"] / 0.3) * a["bass"]
        phase = mem["phase"] = mem.get("phase", 0.0) + max(0.0, t - mem.get("t", t)) * rate(st.speed) * (1.5 + 6 * env)
        mem["t"] = t
        glow = 0.35 + 0.65 * min(1.0, max(env, kick))   # the whole fire breathes with the music
        out = []
        for i in range(n):
            flicker = 0.6 * _noise(phase + i * 3.7, 1) + 0.4 * _noise(phase * 2.3 + i * 5.1, 2)
            heat = min(1.0, 0.15 + (0.45 + 0.4 * env) * flicker + 0.25 * kick)
            pos = heat * (len(FIRE) - 1)
            k = min(int(pos), len(FIRE) - 2)
            out.append(scale(lerp(FIRE[k], FIRE[k + 1], pos - k), glow))
        return out


class NorthernLights(MusicEffect):
    id, name = "music_aurora", "Northern lights"

    def render(self, t, n, st, mem, ctx):
        """Aurora that flows faster and glows brighter with the energy of the music."""
        a = _audio(ctx)
        env = _glide(mem, "env", 0.2 if a["silent"] else a["level"], t, 0.15, 0.8)
        swell = 0.0 if a["silent"] else 0.25 * math.exp(-a["since_beat"] / 0.6)
        phase = mem["phase"] = mem.get("phase", 0.0) + max(0.0, t - mem.get("t", t)) * rate(st.speed) * (0.3 + 2.5 * env)
        mem["t"] = t
        out = []
        for i in range(n):
            x = i / max(n, 1) * 3
            hue = 0.33 + 0.42 * _noise(x + phase, 3)
            val = min(1.0, (0.2 + 0.6 * env + swell) * (0.35 + 0.65 * _noise(x * 1.7 - phase * 1.3, 4)))
            out.append(hsv(hue, 0.85, val))
        return out


class Heartbeat(MusicEffect):
    id, name = "music_heart", "Heartbeat"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        """A soft double beat (lub-dub) on every beat in color 1, settling into color 2."""
        a = _audio(ctx)
        c1, c2 = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        if a["silent"]:
            s = t % 1.2                                    # a calm resting pulse without music
        else:
            s = a["since_beat"]
        width = 0.05 + 0.05 * (1 - st.speed / 100)
        lub = math.exp(-((s - 0.04) / width) ** 2)
        dub = 0.65 * math.exp(-((s - 0.04 - 3.2 * width) / width) ** 2)
        v = max(lub, dub)
        rest = 0.12 if a["silent"] else 0.1 + 0.15 * a["level"]
        return [scale(lerp(c2, c1, v), rest + (1 - rest) * v)] * n


class LightShow(MusicEffect):
    id, name = "music_show", "Light show"

    def render(self, t, n, st, mem, ctx):
        """A festival-style show across the whole room, run by show.ShowDirector: the engine
        works out one show frame per frame and every light draws its part of the stage."""
        frame = ctx.get("show")
        if frame is None:  # a still frame (the graphics card): festival colors side by side
            palette = show.STYLES["edm"].colors
            return [tuple(int(c * 255) for c in palette[i % len(palette)]) for i in range(n)]
        return show.render(frame, n, ctx.get("span", (0.0, 1.0, "front")), mem, ctx.get("fixture"),
                           ctx.get("slow", False))


# ---- games: these follow what happens in the game (see games.py) ----------------------------

_CT = np.array([40.0, 120.0, 255.0])      # Counter-Terrorists
_T = np.array([255.0, 135.0, 20.0])       # Terrorists
_NEUTRAL = np.array([170.0, 175.0, 200.0])
_RED = np.array([255.0, 0.0, 0.0])
_WHITE = np.array([255.0, 255.0, 255.0])
_BLAST = np.array([255.0, 190.0, 110.0])
_GOLD = np.array([255.0, 215.0, 120.0])
_FIRE = np.array([255.0, 90.0, 0.0])
_DEFUSE = np.array([170.0, 225.0, 255.0])   # ice white: shows on the blue of the CT side too


def _mix(out: np.ndarray, color: np.ndarray, k) -> np.ndarray:
    k = np.clip(np.asarray(k, dtype=float), 0.0, 1.0)
    if k.ndim == 1:
        k = k[:, None]
    return out * (1 - k) + color * k


class CS2(Effect):
    """Counter-Strike 2, as the game tells it (games.CS2State): the room in your team's color
    with your health as a bar across the stage (the right side goes dark first), a red heartbeat
    when you're low, white when you're flashed, flicker when you burn, dim in a smoke. The bomb
    blinks red ever faster, the blast floods the room, a defuse flashes ice white, the round's winners
    sweep their color across, and every kill gives a short white pop (gold for a headshot)."""
    id, name = "game_cs2", "Counter-Strike 2"
    group = "game"
    uses = ()

    def render(self, t, n, st, mem, ctx):
        g = ctx.get("game") or {}
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        x = start + (end - start) * (np.arange(n) + 0.5) / n      # where each pixel sits: 0 left .. 1 right
        if not g.get("inMatch"):    # in the menu: both sides' colors drift slowly across the room
            u = (0.5 + 0.5 * np.sin(2 * np.pi * (x * 0.8 - t * 0.05)))[:, None]
            return _rgb((_CT * u + _T * (1 - u)) * (0.35 + 0.08 * math.sin(t * 0.8)))
        team = {"CT": _CT, "T": _T}.get(g.get("team"), _NEUTRAL)
        ago = g.get("ago") or {}
        hp = max(0.0, min(1.0, (g.get("health") or 0) / 100))
        if not g.get("me"):                  # dead, watching a teammate
            out = np.tile(team * (0.08 + 0.03 * math.sin(t * 1.5)), (n, 1))
        elif g.get("phase") == "freezetime":  # buy time: the team color breathes
            out = np.tile(team * (0.45 + 0.2 * math.sin(t * 2.4)), (n, 1))
        elif hp <= 0:
            out = np.tile(_RED * 0.06, (n, 1))
        else:
            if n == 1:
                out = np.tile(team * (0.25 + 0.75 * hp), (1, 1))
            else:
                lit = np.clip((hp - x) / 0.04 + 0.5, 0.0, 1.0)
                out = team[None, :] * (0.07 + 0.93 * lit[:, None])
            if hp < 0.3:                     # low: a red heartbeat
                out = _mix(out, _RED, 0.85 * math.exp(-((t * 1.6) % 1.0) * 7))
        bomb = g.get("bomb")
        if bomb is not None:                 # the C4 blinks faster and faster: once a second at the
            k = 0.88 / 40.0                  # plant, about eight a second at the end (the blink period
            e = min(bomb, 40.0)              # shrinks with time, so its phase is the integral of 1/period)
            phase = -math.log(1 - k * e) / k + max(0.0, bomb - 40.0) / 0.12
            if phase % 1.0 < 0.3:
                out = _mix(out, _RED, 0.75)
        if ago.get("exploded", 99) < 2.5:
            out = _mix(out, _BLAST, math.exp(-ago["exploded"] * 1.6))
        if ago.get("defused", 99) < 2.0:
            a = ago["defused"]
            out = _mix(out, _DEFUSE, 0.9 * math.exp(-a * 1.5) * (0.5 + 0.5 * math.cos(a * 14)))
        if ago.get("round_over", 99) < 4.0 and g.get("winTeam") in ("CT", "T"):
            a = ago["round_over"]
            wave = 0.5 + 0.5 * np.cos(2 * np.pi * (x - a * 0.8))
            out = _mix(out, _CT if g["winTeam"] == "CT" else _T, (1 - a / 4.0) * wave)
        if ago.get("headshot", 99) < 0.4:
            out = _mix(out, _GOLD, 0.8 * (1 - ago["headshot"] / 0.4))
        elif ago.get("kill", 99) < 0.35:
            out = _mix(out, _WHITE, 0.55 * (1 - ago["kill"] / 0.35))
        burning = g.get("burning") or 0.0
        if burning > 0:
            flicker = np.array([_noise(i * 0.7 + t * 9, 11) for i in range(n)])
            # (fire over the team color, not mixed with it: blue and orange half-and-half look purple)
            out = _mix(out, _FIRE * (0.55 + 0.45 * flicker[:, None]), min(1.0, burning * 1.3))
        out = out * (1 - 0.65 * (g.get("smoked") or 0.0))
        return _rgb(_mix(out, _WHITE, g.get("flashed") or 0.0))


_ORDER = np.array([30.0, 110.0, 255.0])       # League: the blue side
_CHAOS = np.array([255.0, 40.0, 30.0])        # ... and the red side
_COLD = np.array([90.0, 100.0, 125.0])        # dead: a cold grey
_KILL = np.array([255.0, 200.0, 40.0])        # your kill: gold
_MULTI = np.array([255.0, 240.0, 150.0])      # a multikill: the same gold, brighter
_BARON = np.array([135.0, 30.0, 235.0])
_HERALD = np.array([205.0, 120.0, 255.0])
_LOSS = np.array([110.0, 8.0, 8.0])
_DRAGONS = {                                  # each dragon in its own color
    "fire": (255.0, 70.0, 10.0), "water": (0.0, 200.0, 190.0), "earth": (190.0, 120.0, 30.0),
    "air": (200.0, 230.0, 255.0), "hextech": (0.0, 230.0, 255.0), "chemtech": (60.0, 255.0, 60.0),
    "elder": (225.0, 200.0, 255.0),
}


class LeagueOfLegends(Effect):
    """League of Legends, as the game tells it (lol.LolState): the room in your team's color
    (blue side blue, red side red) with your health as a bar across the stage, a dim cold grey
    while you're dead with the respawn bar filling back up, a gold flash for your kill (brighter
    and longer for a multikill), a sweep when your team gets the ace, your team's dragon in the
    dragon's color, the baron in purple, the herald in violet. The enemy taking an objective
    doesn't flash. The end holds gold for a win and a dim red for a loss.

    Safety: lol.py only starts a flash when the one before is over (and never closer than 0.34 s),
    so there are never more than three flashes in any second, like Storm and Counter-Strike 2.
    Slow lights (Hue, Govee: ctx["fps"] <= SLOW_FPS) get each flash held, not decayed, for at
    least 0.3 s, so they show it at all."""
    id, name = "game_lol", "League of Legends"
    group = "game"
    uses = ()
    HOLD = 0.3
    RESULT_HOLD, RESULT_FADE = 5.0, 1.5

    def render(self, t, n, st, mem, ctx):
        g = ctx.get("lol") or {}
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        x = start + (end - start) * (np.arange(n) + 0.5) / n      # where each pixel sits: 0 left .. 1 right
        ago = g.get("ago") or {}
        slow = (ctx.get("fps") or 100) <= show.SLOW_FPS
        result = next((k for k in ("win", "lose") if ago.get(k, 99) < self.RESULT_HOLD + self.RESULT_FADE), None)
        if not g.get("inMatch") and result is None:   # no match: both sides' colors drift slowly across the room
            u = (0.5 + 0.5 * np.sin(2 * np.pi * (x * 0.8 - t * 0.05)))[:, None]
            return _rgb((_ORDER * u + _CHAOS * (1 - u)) * (0.35 + 0.08 * math.sin(t * 0.8)))
        team = {"ORDER": _ORDER, "CHAOS": _CHAOS}.get(g.get("team"), _NEUTRAL)
        if g.get("dead"):                     # dead: dim grey, and the respawn bar fills with your team's color
            back = np.tile(_COLD * (0.10 + 0.02 * math.sin(t * 1.5)), (n, 1))
            back_up = max(0.0, min(1.0, g.get("respawn") or 0.0))
            if n == 1:
                out = _mix(back, team * 0.5, back_up)
            else:
                out = _mix(back, team * 0.5, np.clip((back_up - x) / 0.04 + 0.5, 0.0, 1.0))
        else:
            hp = max(0.0, min(1.0, g.get("health") if g.get("health") is not None else 1.0))
            if n == 1:
                out = np.tile(team * (0.25 + 0.75 * hp), (1, 1))
            else:
                lit = np.clip((hp - x) / 0.04 + 0.5, 0.0, 1.0)
                out = team[None, :] * (0.07 + 0.93 * lit[:, None])
        for kind, color, power in (("my_kill", _KILL, 0.8), ("multikill", _MULTI, 1.0),
                                   ("dragon", np.array(_DRAGONS.get(str(g.get("dragon") or "").lower(), _DRAGONS["hextech"])), 0.9),
                                   ("baron", _BARON, 0.9), ("herald", _HERALD, 0.85)):
            a, length = ago.get(kind, 99), LENGTH[kind]
            if a < length:
                level = power if slow and a < max(length, self.HOLD) else power * (1 - a / length)
                out = _mix(out, color, level)
        a = ago.get("ace", 99)
        if a < LENGTH["ace"]:
            if n == 1 or slow:                # one light, or one that is too slow to show a sweep: a held flash
                out = _mix(out, team * 0.4 + _WHITE * 0.6, 0.85 if slow else 0.85 * (1 - a / LENGTH["ace"]))
            else:                             # a bright band running from the left across the stage
                p = a / LENGTH["ace"]
                out = _mix(out, team * 0.4 + _WHITE * 0.6, 0.9 * np.exp(-((x - (p * 1.4 - 0.2)) / 0.16) ** 2))
        if result:                            # the end: gold held for a win, a dim red for a loss
            a = ago[result]
            level = 1.0 if a < self.RESULT_HOLD else 1 - (a - self.RESULT_HOLD) / self.RESULT_FADE
            out = _mix(out, _KILL if result == "win" else _LOSS, max(0.0, level) * (0.85 if result == "win" else 0.9))
        return _rgb(out)


_RADIANT = np.array([40.0, 225.0, 70.0])      # Dota: Radiant, a clear green
_DIRE = np.array([255.0, 35.0, 25.0])         # ... and Dire, a clear red
_DAWN = np.array([255.0, 205.0, 130.0])       # the warm light of the day
_DUSK = np.array([50.0, 80.0, 255.0])         # ... and the blue of the night
_MANA = np.array([40.0, 120.0, 255.0])
_ROSHAN = np.array([150.0, 50.0, 255.0])      # Roshan and the aegis: purple, turning gold


class Dota2(Effect):
    """Dota 2, as the game tells it (games.DotaState): the room in your side's color (Radiant a
    clear green, Dire a clear red) that follows the map's day and night (the day brighter and warmer,
    the night dimmer and bluer), your health as a bar across the stage, your mana as a blue bar on
    the PC's own lights, a dim cold grey while you're dead with the respawn bar filling back up,
    a gold flash for your kill, a purple flash turning gold when your team kills Roshan or you pick
    up the aegis, a short desaturated dip when you're stunned or hexed (a dip, not a flash), gold held
    for a win and a dim red for a loss.

    Safety: games.py only starts a flash when the one before is over (and never closer than 0.34 s),
    so there are never more than three flashes in any second, like League and Storm. Slow lights
    (ctx["fps"] <= SLOW_FPS) get each flash held, not decayed, for at least 0.35 s."""
    id, name = "game_dota", "Dota 2"
    group = "game"
    uses = ()
    HOLD = 0.35
    DIP = 0.8
    RESULT_HOLD, RESULT_FADE = 5.0, 1.5
    FLASHES = (("my_kill", _KILL, 0.8), ("aegis", None, 0.9), ("roshan", None, 1.0))

    def render(self, t, n, st, mem, ctx):
        g = ctx.get("dota") or {}
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        x = start + (end - start) * (np.arange(n) + 0.5) / n      # where each pixel sits: 0 left .. 1 right
        ago = g.get("ago") or {}
        slow = (ctx.get("fps") or 100) <= show.SLOW_FPS
        result = next((k for k in ("win", "lose") if ago.get(k, 99) < self.RESULT_HOLD + self.RESULT_FADE), None)
        if not g.get("inMatch") and result is None:   # no match: both sides' colors drift slowly across the room
            u = (0.5 + 0.5 * np.sin(2 * np.pi * (x * 0.8 - t * 0.05)))[:, None]
            return _rgb((_RADIANT * u + _DIRE * (1 - u)) * (0.35 + 0.08 * math.sin(t * 0.8)))
        # Day and night: eased, so the room slowly turns with the map instead of jumping.
        want = 1.0 if g.get("daytime", True) else 0.0
        day = want if "day" not in mem else mem["day"] + (want - mem["day"]) * min(1.0, max(0.0, t - mem["t"]) * 0.8)
        mem["day"], mem["t"] = day, t
        base = {"radiant": _RADIANT, "dire": _DIRE}.get(g.get("team"), _NEUTRAL)
        team = (base * 0.82 + _DAWN * 0.18) * day + (base * 0.78 + _DUSK * 0.22) * 0.6 * (1 - day)
        if g.get("alive") is False:           # dead: dim grey, and the respawn bar fills with your side's color
            back = np.tile(_COLD * (0.10 + 0.02 * math.sin(t * 1.5)), (n, 1))
            back_up = max(0.0, min(1.0, g.get("respawn") or 0.0))
            if n == 1:
                out = _mix(back, team * 0.5, back_up)
            else:
                out = _mix(back, team * 0.5, np.clip((back_up - x) / 0.04 + 0.5, 0.0, 1.0))
        else:
            hp = max(0.0, min(1.0, g.get("health") if g.get("health") is not None else 1.0))
            if n == 1:
                out = np.tile(team * (0.25 + 0.75 * hp), (1, 1))
            else:
                lit = np.clip((hp - x) / 0.04 + 0.5, 0.0, 1.0)
                out = team[None, :] * (0.07 + 0.93 * lit[:, None])
                mana = max(0.0, min(1.0, g.get("mana") if g.get("mana") is not None else 1.0))
                if start >= 0.7:              # the PC's own lights stand at the right: each shows mana as a blue bar
                    along = (x - start) / max(end - start, 1e-6)
                    out = _mix(out, _MANA, 0.55 * np.clip((mana - along) / 0.06 + 0.5, 0.0, 1.0))
        a = ago.get("dip", 99)
        if a < self.DIP:                      # stunned or hexed: the colors drain for a moment and come back
            k = 0.7 if slow else 0.7 * math.sin(math.pi * a / self.DIP)
            grey = out.mean(axis=1, keepdims=True) * 0.7
            out = out * (1 - k) + grey * k
        for kind, color, power in self.FLASHES:
            a, length = ago.get(kind, 99), DOTA_LENGTH[kind]
            if a < length:
                level = power if slow and a < max(length, self.HOLD) else power * (1 - a / length)
                if color is None:             # purple at the start, gold at the end
                    p = min(1.0, a / length)
                    color = _ROSHAN * (1 - p) + _GOLD * p
                out = _mix(out, color, level)
        if result:                            # the end: gold held for a win, a dim red for a loss
            a = ago[result]
            level = 1.0 if a < self.RESULT_HOLD else 1 - (a - self.RESULT_HOLD) / self.RESULT_FADE
            out = _mix(out, _KILL if result == "win" else _LOSS, max(0.0, level) * (0.85 if result == "win" else 0.9))
        return _rgb(out)


_RL_BLUE = np.array(_RL_BLUE_RGB, dtype=float)       # Rocket League: the game says the team colors; these are the fallback and the idle drift
_RL_ORANGE = np.array(_RL_ORANGE_RGB, dtype=float)
_DEMO = np.array([255.0, 50.0, 190.0])                # your demolition: hot pink, which shows on both sides' colors
_SAVE = np.array([170.0, 225.0, 255.0])               # a save: ice white


class RocketLeague(Effect):
    """Rocket League, as the game tells it (rocket_league.RocketLeagueState): the room in your
    team's color (the game's own team colors, made vivid) with your boost as a bar across the stage,
    a slow shimmer along the bar while you're supersonic, a big sweep in your color when your team
    scores (with gold on top when it was you), a short dim in their color when they score, a short
    flash for your saves and demolitions, dim grey while your car is demolished until it's back, a
    slow pulse in overtime, your color held for a win and dim for a loss.

    Safety: rocket_league.py only starts a flash when the one before is over (and never closer than
    0.34 s), so there are never more than three flashes in any second, like League, Dota and Storm.
    The shimmer moves along the bar at 2.5 Hz and only changes the bar by a third: a slow ripple, not
    a flash. Slow lights (ctx["fps"] <= SLOW_FPS) get each flash held, not decayed, for at least 0.35 s."""
    id, name = "game_rl", "Rocket League"
    group = "game"
    uses = ()
    HOLD = 0.35
    DIM = 1.5
    RESULT_HOLD, RESULT_FADE = 5.0, 1.5
    FLASHES = (("demo", _DEMO, 0.8), ("save", _SAVE, 0.8), ("epic_save", _WHITE, 1.0))

    def render(self, t, n, st, mem, ctx):
        g = ctx.get("rl") or {}
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        x = start + (end - start) * (np.arange(n) + 0.5) / n      # where each pixel sits: 0 left .. 1 right
        ago = g.get("ago") or {}
        slow = (ctx.get("fps") or 100) <= show.SLOW_FPS
        result = next((k for k in ("win", "lose") if ago.get(k, 99) < self.RESULT_HOLD + self.RESULT_FADE), None)
        if not g.get("inMatch") and result is None:   # no match: both teams' colors drift slowly across the room
            u = (0.5 + 0.5 * np.sin(2 * np.pi * (x * 0.8 - t * 0.05)))[:, None]
            return _rgb((_RL_BLUE * u + _RL_ORANGE * (1 - u)) * (0.35 + 0.08 * math.sin(t * 0.8)))
        team = np.array(g.get("color") or _NEUTRAL, dtype=float)
        other = np.array(g.get("other") or _NEUTRAL, dtype=float)
        if g.get("demolished"):               # your car is gone: dim grey until it's back
            out = np.tile(_COLD * (0.10 + 0.02 * math.sin(t * 1.5)), (n, 1))
        else:
            boost = max(0.0, min(1.0, g.get("boost") or 0.0))
            if n == 1:
                out = np.tile(team * (0.2 + 0.8 * boost), (1, 1))
                if g.get("supersonic"):
                    out = _mix(out, _WHITE, 0.2 * (0.5 + 0.5 * math.sin(2 * math.pi * t * 1.5)))
            else:
                lit = np.clip((boost - x) / 0.04 + 0.5, 0.0, 1.0)
                out = team[None, :] * (0.12 + 0.88 * lit[:, None])
                if g.get("supersonic"):       # a ripple running along the bar, only where the bar is lit
                    out = _mix(out, _WHITE, 0.35 * (0.5 + 0.5 * np.sin(2 * np.pi * (x * 8 - t * 2.5))) * lit)
        if g.get("overtime"):                 # overtime: the room slowly breathes (easing in over the first seconds)
            ease = min(1.0, ago.get("overtime", 99) / 2.0)
            out = out * (1 - 0.35 * ease * (0.5 - 0.5 * math.sin(2 * math.pi * t * 0.4)))
        a = ago.get("goal_against", 99)
        if a < self.DIM:                      # they scored: a short dim in their color (a dip, not a flash)
            k = 0.7 if slow else 0.7 * math.sin(math.pi * a / self.DIM)
            out = _mix(out * (1 - 0.5 * k), other * 0.3, 0.45 * k)
        for kind, color, power in self.FLASHES:
            a, length = ago.get(kind, 99), RL_LENGTH[kind]
            if a < length:
                level = power if slow and a < max(length, self.HOLD) else power * (1 - a / length)
                out = _mix(out, color, level)
        for kind in ("goal_for", "my_goal"):
            a, length = ago.get(kind, 99), RL_LENGTH[kind]
            if a >= length:
                continue
            sweep = team * 0.6 + _WHITE * 0.4
            if n == 1 or slow:                # one light, or one that is too slow to show a sweep: a held flash
                out = _mix(out, sweep, 0.85 if slow else 0.85 * (1 - a / length))
            else:                             # a bright band running across the stage, the room lifted behind it
                p = a / length
                band = np.maximum(0.95 * np.exp(-((x - (p * 1.5 - 0.25)) / 0.2) ** 2), 0.35 * (1 - p))
                out = _mix(out, sweep, band)
            if kind == "my_goal":             # you scored: gold on top
                out = _mix(out, _GOLD, 0.6 if slow and a < max(length, self.HOLD) else 0.6 * (1 - a / length))
        if result:                            # the end: your color held for a win, dim for a loss
            a = ago[result]
            level = max(0.0, 1.0 if a < self.RESULT_HOLD else 1 - (a - self.RESULT_HOLD) / self.RESULT_FADE)
            if result == "win":
                out = _mix(out, team * 0.85 + _WHITE * 0.15, level * 0.9)
            else:
                out = _mix(out, _COLD * 0.12, level * 0.85)
        return _rgb(out)


_GREEN_LED = np.array([0.0, 255.0, 40.0])             # racing: the rev lights of a real wheel, low to high
_YELLOW_LED = np.array([255.0, 205.0, 0.0])
_RED_LED = np.array([255.0, 15.0, 10.0])
_LIMIT_BLUE = np.array([20.0, 60.0, 255.0])            # at the limiter
_AMBER = np.array([255.0, 140.0, 0.0])                 # a yellow flag, a safety car
_DRS_GREEN = np.array([0.0, 255.0, 80.0])
_PURPLE = np.array([170.0, 40.0, 255.0])               # a fastest lap
_START_RED = np.array([255.0, 0.0, 0.0])


def _rev_colors(x: np.ndarray) -> np.ndarray:
    """The color of the rev lights at each place on the stage (0 left .. 1 right): green, then yellow, then red near the top."""
    to_yellow = np.clip((x - 0.5) / 0.15, 0.0, 1.0)[:, None]
    to_red = np.clip((x - 0.75) / 0.15, 0.0, 1.0)[:, None]
    low = _GREEN_LED * (1 - to_yellow) + _YELLOW_LED * to_yellow
    return low * (1 - to_red) + _RED_LED * to_red


class Racing(Effect):
    """Racing games, as they tell it (racing.RacingState: Forza, the F1 games, iRacing and Assetto Corsa): rev lights like a
    real wheel's, the engine's revs filling the stage from left to right in green, then yellow, then
    red near the top. At the limiter the whole bar pulses blue. F1 start lights: one to five red
    blocks as they come on, all gone together at lights out. A yellow flag or a safety car: an amber
    band pulsing slowly. DRS open: a green edge on both ends. Your fastest lap: a purple sweep. The
    chequered flag: a slow black and white checker scrolling by.

    Safety: racing.py starts a limiter pulse or a sweep only when the one before is over (and never
    closer than 0.34 s), so there are never more than three in any second, like League, Dota and
    Rocket League. The amber band breathes at 0.5 Hz and the checker scrolls so that a place changes
    every second or so: slow, not flashing. Slow lights (ctx["fps"] <= SLOW_FPS) can't show a rev bar
    at 60 Hz: they show the revs as one calm color, green to red, smoothed over a second, and skip the
    limiter pulse."""
    id, name = "game_racing", "Racing"
    group = "game"
    uses = ()
    HOLD = 0.35
    OUT_HOLD = 0.5            # the start lights all gone: dark this long
    CHEQUERED = 15.0          # seconds the checker shows after the flag
    SMOOTH = 0.8              # seconds the calm color of a slow light takes to follow the revs

    def render(self, t, n, st, mem, ctx):
        g = ctx.get("racing") or {}
        start, end, _ = ctx.get("span", (0.0, 1.0, "front"))
        x = start + (end - start) * (np.arange(n) + 0.5) / n      # where each pixel sits: 0 left .. 1 right
        ago = g.get("ago") or {}
        slow = (ctx.get("fps") or 100) <= show.SLOW_FPS
        if not g.get("inRace"):           # no race: a dim red and amber drift, the pit lane waiting
            u = (0.5 + 0.5 * np.sin(2 * np.pi * (x * 0.6 - t * 0.05)))[:, None]
            return _rgb((_START_RED * 0.8 * u + _AMBER * (1 - u)) * (0.22 + 0.05 * math.sin(t * 0.8)))
        lights = int(g.get("startLights") or 0)
        if lights:                        # the start lights: blocks of red, one more each time
            if n == 1 or slow:
                return _rgb(np.tile(_START_RED * (0.2 * lights), (n, 1)))
            block = np.minimum((x * 5).astype(int), 4)
            inside = np.abs((x * 5) % 1.0 - 0.5) < 0.38           # (a dark gap between the blocks)
            return _rgb(_START_RED[None, :] * ((block < lights) & inside)[:, None])
        if ago.get("lights_out", 99) < self.OUT_HOLD:             # lights out: all of them go off at once
            return _rgb(np.zeros((n, 3)))
        if ago.get("chequered", 99) < self.CHEQUERED:             # the flag: a slow checker, black and white
            if n == 1 or slow:
                return _rgb(np.tile(_WHITE * (0.55 + 0.4 * math.sin(2 * math.pi * t * 0.3)), (n, 1)))
            wave = np.clip(np.sin(2 * np.pi * (x * 4 - t * 0.25)) * 3, -1.0, 1.0)
            return _rgb(_WHITE[None, :] * (0.5 + 0.5 * wave)[:, None])
        f = max(0.0, min(1.0, g.get("rpm") or 0.0))
        if n == 1 or slow:                # one light, or one too slow for a bar: the revs as a calm color
            last = mem.get("t")
            k = 1.0 if last is None else 1 - math.exp(-max(0.0, min(1.0, t - last)) / self.SMOOTH)
            mem["f"] = f if "f" not in mem else mem["f"] + (f - mem["f"]) * k
            mem["t"] = t
            level = mem["f"]
            out = np.tile(_rev_colors(np.array([level]))[0] * (0.35 + 0.55 * level), (n, 1))
        else:
            lit = np.clip((f - x) / 0.03 + 0.5, 0.0, 1.0)
            out = _rev_colors(x) * (0.05 + 0.95 * lit)[:, None]
        if g.get("drs") and n > 1 and not slow:                   # DRS is open: a green edge on both ends
            edge = np.clip((0.1 - np.minimum(x, 1 - x)) / 0.03 + 0.5, 0.0, 1.0)
            out = _mix(out, _DRS_GREEN, edge)
        elif g.get("drs"):
            out = _mix(out, _DRS_GREEN, 0.25)
        if g.get("caution"):              # a yellow flag or the safety car: an amber band, breathing slowly
            breathe = 0.5 + 0.5 * math.sin(2 * math.pi * t * 0.5)
            if n == 1 or slow:
                out = _mix(out, _AMBER, 0.55)
            else:
                band = np.clip((0.2 - np.abs(x - 0.5)) / 0.06 + 0.5, 0.0, 1.0)
                out = _mix(out, _AMBER, band * (0.45 + 0.5 * breathe))
        a = ago.get("limiter", 99)
        if not slow and a < RACING_LENGTH["limiter"]:             # at the limiter: the whole bar pulses blue
            out = _mix(out, _LIMIT_BLUE, 0.95)
        a, length = ago.get("fastest", 99), RACING_LENGTH["fastest"]
        if a < length:                    # your fastest lap: a purple band running across, the room lifted behind it
            if n == 1 or slow:
                out = _mix(out, _PURPLE, 0.85)
            else:
                p = a / length
                band = np.maximum(0.95 * np.exp(-((x - (p * 1.5 - 0.25)) / 0.2) ** 2), 0.35 * (1 - p))
                out = _mix(out, _PURPLE, band)
        return _rgb(out)


def _rgb(arr: np.ndarray) -> list[RGB]:
    a = np.clip(arr, 0, 255).astype(int)
    return [(int(r), int(g), int(b)) for r, g, b in a]


EFFECTS: dict[str, Effect] = {e.id: e for e in (
    Rainbow(), Spectrum(), Breathing(), Wave(), Gradient(), Comet(), Twinkle(), Fire(), Aurora(), Candle(), Ocean(), Lava(), Storm(), Snow(), StringLights(), CandyCane(), Fireflies(), Rain(), Plasma(), Sunset(), Typing(), PcLoad(), Daylight(),
    Screen(), ScreenSound(),
    LightShow(), Party(), BassRipple(), MusicSpectrum(), BeatPulse(), VuMeter(), Romantic(),
    ColorOrgan(), CenterBloom(), Sparkle(), Meteor(), MusicFire(), NorthernLights(), Heartbeat(), CS2(), LeagueOfLegends(), Dota2(), RocketLeague(), Racing(),
)}
MUSIC_EFFECTS = {e.id for e in EFFECTS.values() if e.group == "music"}
HEARING_EFFECTS = MUSIC_EFFECTS | {"screen_sound"}        # ask the audio analysis for its picture
SCREEN_EFFECTS = {"screen", "screen_sound"}               # ask for the screen
GAME_EFFECTS = {e.id for e in EFFECTS.values() if e.group == "game"}
