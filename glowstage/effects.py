"""App-driven effects. The engine renders these for every device on a shared clock, so
the PC, the WLED strips and the Nanoleaf panels animate in sync."""

from __future__ import annotations

import math
import random

import numpy as np

from . import keys, show, sun
from .audio import quiet
from .colors import RGB, hex_to_rgb, hsv, lerp, scale
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


def _rgb(arr: np.ndarray) -> list[RGB]:
    a = np.clip(arr, 0, 255).astype(int)
    return [(int(r), int(g), int(b)) for r, g, b in a]


EFFECTS: dict[str, Effect] = {e.id: e for e in (
    Rainbow(), Spectrum(), Breathing(), Wave(), Gradient(), Comet(), Twinkle(), Fire(), Aurora(), Typing(), PcLoad(), Daylight(),
    Screen(), ScreenSound(),
    LightShow(), Party(), BassRipple(), MusicSpectrum(), BeatPulse(), VuMeter(), Romantic(),
    ColorOrgan(), CenterBloom(), Sparkle(), Meteor(), MusicFire(), NorthernLights(), Heartbeat(), CS2(),
)}
MUSIC_EFFECTS = {e.id for e in EFFECTS.values() if e.group == "music"}
HEARING_EFFECTS = MUSIC_EFFECTS | {"screen_sound"}        # ask the audio analysis for its picture
SCREEN_EFFECTS = {"screen", "screen_sound"}               # ask for the screen
GAME_EFFECTS = {e.id for e in EFFECTS.values() if e.group == "game"}
