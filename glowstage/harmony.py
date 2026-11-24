"""Harmony from the PC's sound: which notes are sounding (chroma), the chord and the key.

Every ~85 ms a long FFT (8192 samples, ~6 Hz per bin) is folded onto the 12 pitch classes from
80 Hz to 4 kHz. The chord is the best match among the 24 major and minor triads; the key is the
best match of the chroma of the last ~20 s with the Krumhansl-Kessler key profiles.
"""

from __future__ import annotations

import numpy as np

FFT = 8192
NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Krumhansl-Kessler probe-tone profiles: how well each scale degree fits a major / minor key.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def chroma_matrix(rate: int, lo: float = 80.0, hi: float = 2000.0) -> np.ndarray:
    """(12, FFT/2+1) weights folding FFT bins between lo and hi Hz onto pitch classes; a bin
    counts most when it sits right on a note, so the leakage between neighbouring notes stays
    small."""
    freqs = np.fft.rfftfreq(FFT, 1 / rate)
    weights = np.zeros((12, len(freqs)), dtype=np.float32)
    use = np.where((freqs >= lo) & (freqs <= hi))[0]
    midi = 69 + 12 * np.log2(freqs[use] / 440.0)
    nearest = np.round(midi)
    weights[nearest.astype(int) % 12, use] = np.clip(1 - np.abs(midi - nearest) * 2, 0, 1)
    return weights


def bass_matrix(rate: int) -> np.ndarray:
    """The same for the bass register only (40-200 Hz): the bass note usually names the chord."""
    return chroma_matrix(rate, 40.0, 200.0)


def _templates() -> tuple[np.ndarray, list[tuple[int, bool]]]:
    rows, names = [], []
    for minor in (False, True):
        for root in range(12):
            t = np.zeros(12)
            t[[root, (root + (3 if minor else 4)) % 12, (root + 7) % 12]] = 1.0
            rows.append(t / np.linalg.norm(t))
            names.append((root, minor))
    return np.array(rows), names


_CHORDS, CHORD_NAMES = _templates()
_KEYS = np.array([np.roll(p, r) for p in (_MAJOR, _MINOR) for r in range(12)])
_KEYS = (_KEYS - _KEYS.mean(axis=1, keepdims=True)) / _KEYS.std(axis=1, keepdims=True)
KEY_NAMES = [(r, minor) for minor in (False, True) for r in range(12)]


def chroma(spectrum: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """12 pitch-class strengths, scaled so the strongest is 1 (all 0 when there is nothing).
    Linear magnitudes: the fundamentals outweigh their overtones (the 3rd harmonic of a C is a G)."""
    c = matrix @ spectrum
    peak = c.max()
    return c / peak if peak > 1e-6 else c


def chord(c: np.ndarray, bass: np.ndarray | None = None) -> tuple[tuple[int, bool] | None, float]:
    """The best-fitting triad and how clearly it wins (0..1); None when nothing fits. A chord
    whose root is the bass note gets a head start."""
    norm = np.linalg.norm(c)
    if norm < 1e-6:
        return None, 0.0
    scores = _CHORDS @ (c / norm)
    if bass is not None and bass.max() > 1e-6:
        roots = np.array([r for r, _ in CHORD_NAMES])
        scores = scores + 0.12 * (bass / bass.max())[roots]
    order = np.argsort(scores)
    best, second = scores[order[-1]], scores[order[-2]]
    if best < 0.55:
        return None, 0.0
    return CHORD_NAMES[int(order[-1])], float(min(1.0, (best - second) * 8 + (best - 0.55)))


def key(c_long: np.ndarray) -> tuple[tuple[int, bool] | None, float]:
    """The key that fits the longer-term chroma best, and the correlation (0..1)."""
    if c_long.std() < 1e-6:
        return None, 0.0
    z = (c_long - c_long.mean()) / c_long.std()
    corr = _KEYS @ z / 12
    i = int(np.argmax(corr))
    return KEY_NAMES[i], float(max(0.0, corr[i]))


def name(ch: tuple[int, bool] | None) -> str:
    return "" if ch is None else NOTES[ch[0]] + ("m" if ch[1] else "")
