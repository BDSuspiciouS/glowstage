"""App-driven effects. The engine renders these for every device on a shared clock, so
the PC, the WLED strips and the Nanoleaf panels animate in sync."""

from __future__ import annotations

import math
import random

from . import show
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


class Effect:
    id = ""
    name = ""
    group = "animation"   # "animation" or "music"
    uses: tuple[str, ...] = ("speed",)

    def render(self, t: float, n: int, st: LightState, mem: dict, ctx: dict) -> list[RGB]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "uses": list(self.uses), "group": self.group}


class Rainbow(Effect):
    id, name = "rainbow", "Rainbow wave"

    def render(self, t, n, st, mem, ctx):
        shift = t * rate(st.speed) * 0.5
        return [hsv(i / max(n, 1) - shift) for i in range(n)]


class Spectrum(Effect):
    id, name = "spectrum", "Spectrum cycle"

    def render(self, t, n, st, mem, ctx):
        return [hsv(t * rate(st.speed) * 0.25)] * n


class Breathing(Effect):
    id, name = "breathing", "Breathing"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        phase = t * rate(st.speed) * 0.5
        level = ((1 - math.cos(2 * math.pi * phase)) / 2) ** 1.5
        base = hex_to_rgb(st.color if int(phase) % 2 == 0 else st.color2)
        return [scale(base, 0.04 + 0.96 * level)] * n


class Wave(Effect):
    id, name = "wave", "Color wave"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        a, b = hex_to_rgb(st.color), hex_to_rgb(st.color2)
        shift = t * rate(st.speed) * 0.5
        return [lerp(a, b, (math.sin(2 * math.pi * (i / max(n, 1) - shift)) + 1) / 2) for i in range(n)]


class Comet(Effect):
    id, name = "comet", "Comet"
    uses = ("speed", "color", "color2")

    def render(self, t, n, st, mem, ctx):
        if n == 0:
            return []
        head = (t * rate(st.speed) * 0.5 % 1.0) * n
        tail = max(2.0, n * 0.3)
        color, bg = hex_to_rgb(st.color), scale(hex_to_rgb(st.color2), 0.06)
        out = []
        for i in range(n):
            d = (head - i) % n
            out.append(lerp(bg, color, max(0.0, 1 - d / tail) ** 2))
        return out


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

    def render(self, t, n, st, mem, ctx):
        r = rate(st.speed)
        out = []
        for i in range(n):
            x = i / max(n, 1) * 3
            hue = 0.33 + 0.42 * _noise(x + t * r, 3)            # green .. violet
            val = 0.25 + 0.75 * _noise(x * 1.7 - t * r * 1.3, 4)
            out.append(hsv(hue, 0.85, val))
        return out


class Screen(Effect):
    id, name = "screen", "Screen sync"
    uses = ()

    def render(self, t, n, st, mem, ctx):
        target = ctx.get("screen") or (0, 0, 0)
        cur = mem.get("color", target)
        cur = lerp(cur, target, 0.35)  # smooth out flicker between frames
        mem["color"] = cur
        return [cur] * n


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
        return show.render(frame, n, ctx.get("span", (0.0, 1.0, "front")), mem, ctx.get("fixture"))


EFFECTS: dict[str, Effect] = {e.id: e for e in (
    Rainbow(), Spectrum(), Breathing(), Wave(), Comet(), Twinkle(), Fire(), Aurora(), Screen(),
    LightShow(), Party(), BassRipple(), MusicSpectrum(), BeatPulse(), VuMeter(), Romantic(),
    ColorOrgan(), CenterBloom(), Sparkle(), Meteor(), MusicFire(), NorthernLights(), Heartbeat(),
)}
MUSIC_EFFECTS = {e.id for e in EFFECTS.values() if e.group == "music"}
