"""Colors from a picture: the few main colors of a photo, spread over the lights the way they
stand on the stage, so the room takes on the picture (a sunset over the desk, a forest in the
corners). Pure numbers: the window's "From a picture" button reads a file with `read()`, takes the
colors with `main_colors()` and lets the manager hand them out with `spread()`."""

from __future__ import annotations

import base64
import colorsys
import io
import os

import numpy as np
from PIL import Image, ImageOps

from .colors import rgb_to_hex
from .screen import vivid

MAX_BYTES = 40 * 1024 * 1024    # a bigger file is not a snapshot of a room, and takes too long to read
SAMPLE = 160                    # the picture is looked at this small: plenty for its main colors
THUMB = 240                     # the preview in the window
COLORS = 5
MERGE = 48                      # two colors closer than this (0..441) are one color to a light
WHITE_BELOW = 0.12              # saturation under this is a grey or a white, not a color
WARM_WHITE = (255, 214, 170)    # what a grey becomes: LEDs can't show grey, a soft white they can
NOT_A_PICTURE = "That file isn't a picture Glowstage can read"


def read(path: str) -> Image.Image:
    """The picture as a small RGB image (longest side THUMB), the right way up. Huge files are
    never decoded at full size: JPEGs are shrunk while they are read, the rest is refused when
    Pillow itself finds it too big."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            raise ValueError("That picture is too big for Glowstage (over 40 MB)")
        with Image.open(path) as img:
            img.draft("RGB", (THUMB * 2, THUMB * 2))       # (JPEG: decode at 1/2, 1/4 or 1/8 size)
            img = ImageOps.exif_transpose(img)             # (a phone photo held sideways)
            img = img.convert("RGB")
            img.thumbnail((THUMB, THUMB))
            return img
    except ValueError:
        raise
    except Exception as e:      # not an image, cut off, a bomb, no such file: all the same to the person
        raise ValueError(NOT_A_PICTURE) from e


def thumbnail_url(img: Image.Image) -> str:
    """The small picture for the window, as a data: URL."""
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


# ---- the main colors -----------------------------------------------------------------------------

def _kmeans(pix: np.ndarray, wgt: np.ndarray, k: int, iters: int = 10) -> tuple[np.ndarray, np.ndarray]:
    """Weighted k-means in RGB. Fixed seed: the same picture always gives the same colors."""
    rng = np.random.default_rng(7)
    p = wgt / wgt.sum()
    centers = [pix[rng.choice(len(pix), p=p)]]
    for _ in range(min(k, len(pix)) - 1):      # (k-means++: the next start is far from those taken)
        d = np.min([((pix - c) ** 2).sum(axis=1) for c in centers], axis=0) * wgt
        if d.sum() <= 0:
            break
        centers.append(pix[rng.choice(len(pix), p=d / d.sum())])
    centers = np.array(centers, dtype=np.float64)
    for _ in range(iters):
        dist = ((pix[:, None, :] - centers[None]) ** 2).sum(axis=2)
        belong = dist.argmin(axis=1)
        for i in range(len(centers)):
            m = belong == i
            if m.any():
                centers[i] = (pix[m] * wgt[m, None]).sum(axis=0) / wgt[m].sum()
    dist = ((pix[:, None, :] - centers[None]) ** 2).sum(axis=2)
    return centers, dist.argmin(axis=1)


def _led(rgb: np.ndarray) -> tuple[int, int, int]:
    """One color made for LEDs. Greys can't be shown (a grey LED is just a dim white), so they
    become a soft warm white; real whites stay white; colors get the screen sync's boost
    (more saturation, a little more light) and are never dark."""
    r, g, b = (float(c) / 255 for c in rgb)
    _, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < WHITE_BELOW:
        if v >= 0.85:
            return (255, 255, 255)
        return tuple(int(c * max(v, 0.6) + 0.5) for c in WARM_WHITE)
    boosted = vivid(np.array([rgb], dtype=np.float64))[0]
    peak = float(boosted.max())
    if peak < 0.65 * 255:
        boosted = boosted * (0.65 * 255 / peak)
    return tuple(int(min(255, c) + 0.5) for c in boosted)


def main_colors(img: Image.Image, count: int = COLORS) -> list[str]:
    """Up to `count` colors (hex) of the picture, from the left of it to the right. Vivid pixels
    weigh more than dull ones (a bright lamp beats the large grey wall), near-black is ignored.
    Colors that look alike are one; so a picture of two colors gives two."""
    small = img.convert("RGB")
    small.thumbnail((SAMPLE, SAMPLE))
    arr = np.asarray(small, dtype=np.float64)
    h, w, _ = arr.shape
    pix = arr.reshape(-1, 3)
    xs = np.tile(np.linspace(0.0, 1.0, w), h)
    mx, mn = pix.max(axis=1), pix.min(axis=1)
    wgt = (mx - mn) / 255 * (mx / 255) + 0.02
    wgt[mx < 0.1 * 255] = 0.0                    # near-black: a dark corner is no color
    keep = wgt > 0
    if not keep.any():
        raise ValueError("That picture is too dark to take colors from")
    pix, xs, wgt = pix[keep], xs[keep], wgt[keep]
    centers, belong = _kmeans(pix, wgt, count)
    groups = []                                  # [color, weight, weight x where it sits (0 left, 1 right)]
    for i, c in enumerate(centers):
        m = belong == i
        if m.any():
            groups.append([c, float(wgt[m].sum()), float((wgt[m] * xs[m]).sum())])
    merged = True
    while merged and len(groups) > 1:            # look-alikes into one
        merged = False
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                if np.linalg.norm(groups[a][0] - groups[b][0]) < MERGE:
                    ga, gb = groups[a], groups[b]
                    total = ga[1] + gb[1]
                    ga[0] = (ga[0] * ga[1] + gb[0] * gb[1]) / total
                    ga[1], ga[2] = total, ga[2] + gb[2]
                    del groups[b]
                    merged = True
                    break
            if merged:
                break
    total = sum(g[1] for g in groups)
    big = [g for g in groups if g[1] >= 0.03 * total]       # (a speck of color is not one of the main ones)
    groups = big or groups
    groups.sort(key=lambda g: g[2] / g[1])
    out = []
    for g in groups:
        hexed = rgb_to_hex(_led(g[0]))
        if hexed not in out:
            out.append(hexed)
    return out


# ---- over the lights -----------------------------------------------------------------------------

def _pick(colors: list[str], x: float) -> int:
    """Which color the picture has at x (0 = left, 1 = right): the colors share it in equal parts."""
    return min(len(colors) - 1, max(0, int(x * len(colors))))


def spread(colors: list[str], lights: list[tuple[str, int, tuple[float, float, str]]]) -> dict[str, dict]:
    """A look for each light: (id, LEDs, stage span) in, {id: patch} out. A bulb gets the one color
    of its place on the stage; a strip, keyboard or fan gets the Gradient from the color at one end
    of its place to the color at the other (two next to each other if it is too short to tell:
    the picture's neighbours blend). Lights that share a place (three bulbs in the room, all of
    "the whole stage") divide it between them, so they don't all take the middle color."""
    if not colors or not lights:
        return {}
    by_place: dict[tuple[float, float], list[int]] = {}
    for i, (_, _, (a, b, _layer)) in enumerate(lights):
        by_place.setdefault((round(a, 3), round(b, 3)), []).append(i)
    patches = {}
    for members in by_place.values():
        for j, i in enumerate(members):
            dev_id, pixels, (start, end, _layer) = lights[i]
            s, e = start + (end - start) * j / len(members), start + (end - start) * (j + 1) / len(members)
            lo, hi = min(s, e), max(s, e)
            if len(colors) == 1:
                patches[dev_id] = {"mode": "color", "color": colors[0]}
            elif pixels <= 1:
                patches[dev_id] = {"mode": "color", "color": colors[_pick(colors, (lo + hi) / 2)]}
            else:
                first, last = _pick(colors, lo + (hi - lo) * 0.15), _pick(colors, hi - (hi - lo) * 0.15)
                if first == last:      # (a short place: the neighbour on the side it runs to)
                    last = first + 1 if first + 1 < len(colors) else first - 1
                    first, last = min(first, last), max(first, last)
                if s > e:              # (a strip laid out the other way round)
                    first, last = last, first
                patches[dev_id] = {"mode": "effect", "effect": "gradient",
                                   "color": colors[first], "color2": colors[last]}
    return patches
