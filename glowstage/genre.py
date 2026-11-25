"""AI genre detection for the light show.

Google's YAMNet sound classifier (trained on AudioSet: 521 sound classes, among them ~50 music
genres and six moods) listens to the PC's sound every couple of seconds, fully offline, through
onnxruntime. Its scores are averaged over the last ~15 seconds and mapped to a light-show style.

The model (tools/yamnet/yamnet.fp16.onnx, from huggingface.co/audiojs/yamnet) takes log-mel
patches, computed here exactly like YAMNet's own frontend: 16 kHz mono, 25 ms Hann frames
every 10 ms, a 512-point FFT magnitude, 64 HTK mel bands from 125 to 7500 Hz, ln(mel + 0.001),
and patches of 96 frames every 48 frames.
"""

from __future__ import annotations

import csv
import logging
import threading
import time

import numpy as np

from .audio import RATE, AudioAnalyzer
from .paths import MODEL_DIR

log = logging.getLogger(__name__)

MODEL = MODEL_DIR / "yamnet.fp16.onnx"
CLASS_MAP = MODEL_DIR / "yamnet_class_map.csv"

SR = 16000
INTERVAL = 2.0        # seconds between two looks at the music
WINDOW = 2.0          # seconds of sound per look (3 overlapping YAMNet patches)

# Light-show style -> the AudioSet genres that point to it, with how much each one counts.
# "Electronic music" and "Dance music" are very broad, so they count for less.
STYLE_GENRES: dict[str, dict[str, float]] = {
    "techno": {"Techno": 1.0},
    "house": {"House music": 1.0, "Disco": 1.0, "Funk": 0.8, "Dance music": 0.4},
    "edm": {"Electronic dance music": 1.0, "Trance music": 1.0, "Electronica": 0.7, "Electronic music": 0.5},
    "bass": {"Dubstep": 1.0, "Drum and bass": 1.0},
    "hiphop": {"Hip hop music": 1.0, "Rapping": 0.8},
    "rock": {"Rock music": 1.0, "Heavy metal": 1.0, "Punk rock": 1.0, "Grunge": 1.0, "Progressive rock": 1.0,
             "Rock and roll": 1.0, "Psychedelic rock": 1.0},
    "pop": {"Pop music": 1.0, "Independent music": 0.6, "Christmas music": 0.6, "Music for children": 0.6},
    "latin": {"Music of Latin America": 1.0, "Salsa music": 1.0, "Flamenco": 1.0, "Reggae": 1.0, "Ska": 1.0,
              "Music of Africa": 1.0, "Afrobeat": 1.0, "Middle Eastern music": 0.8, "Music of Bollywood": 0.8,
              "Music of Asia": 0.6},
    "chill": {"Ambient music": 1.0, "New-age music": 1.0, "Jazz": 1.0, "Classical music": 1.0, "Opera": 1.0,
              "Blues": 1.0, "Soul music": 1.0, "Rhythm and blues": 1.0, "Folk music": 1.0, "Country": 0.8,
              "Bluegrass": 0.8, "Swing music": 0.8, "Gospel music": 0.8, "Christian music": 0.6, "Lullaby": 1.0,
              "A capella": 0.8, "Traditional music": 0.6, "Carnatic music": 0.6},
}
MOODS = ("Happy music", "Sad music", "Tender music", "Exciting music", "Angry music", "Scary music")


def _hz_to_mel(f):
    return 1127.0 * np.log1p(np.asarray(f, dtype=np.float64) / 700.0)


def _mel_matrix() -> np.ndarray:
    """(257, 64) weights from FFT bins to mel bands, as tf.signal.linear_to_mel_weight_matrix."""
    bins = _hz_to_mel(np.linspace(0, SR / 2, 257)[1:])[:, None]   # TF leaves out the DC bin
    edges = np.linspace(_hz_to_mel(125.0), _hz_to_mel(7500.0), 64 + 2)
    lower, center, upper = edges[:-2], edges[1:-1], edges[2:]
    weights = np.maximum(0.0, np.minimum((bins - lower) / (center - lower), (upper - bins) / (upper - center)))
    return np.vstack([np.zeros((1, 64)), weights]).astype(np.float32)


_MEL = _mel_matrix()
_HANN = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 400)).astype(np.float32)  # periodic, as tf.signal


def log_mel_patches(wave: np.ndarray) -> np.ndarray:
    """16 kHz mono float waveform -> (n, 96, 64) float32 log-mel patches for YAMNet."""
    if len(wave) < 15600:
        wave = np.pad(wave, (0, 15600 - len(wave)))
    n = 1 + (len(wave) - 400) // 160
    frames = wave[np.arange(400)[None, :] + 160 * np.arange(n)[:, None]] * _HANN
    mel = np.abs(np.fft.rfft(frames, 512)).astype(np.float32) @ _MEL
    logmel = np.log(mel + 0.001)
    count = 1 + (n - 96) // 48
    return np.stack([logmel[i * 48:i * 48 + 96] for i in range(count)]).astype(np.float32)


def _lowpass_taps(cutoff_hz: float, rate: int, taps: int = 95) -> np.ndarray:
    k = np.arange(taps) - (taps - 1) / 2
    fc = cutoff_hz / rate
    h = 2 * fc * np.sinc(2 * fc * k) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


_ANTI_ALIAS = _lowpass_taps(7600, RATE)


def to_16k(wave48k: np.ndarray) -> np.ndarray:
    """48 kHz -> 16 kHz: low-pass below the new Nyquist, then keep every third sample."""
    return np.convolve(wave48k, _ANTI_ALIAS, mode="valid")[::RATE // SR].astype(np.float32)


class GenreDetector:
    """Runs on its own thread while the light show is on; request() returns what it heard."""

    def __init__(self, audio: AudioAnalyzer):
        self.audio = audio
        self.error = "" if MODEL.is_file() else "AI model not found"
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snap: dict = {}
        self._ema: np.ndarray | None = None
        self._looks = 0
        self._quiet_since: float | None = None
        self._style: int | None = None
        self._challenger: tuple[int, int] = (-1, 0)   # (style, looks in a row it has been clearly ahead)

    def request(self) -> dict:
        self._wanted_until = time.monotonic() + 3.0
        if self._thread is None and not self.error:
            self._thread = threading.Thread(target=self._run, name="genre", daemon=True)
            self._thread.start()
        with self._lock:
            return dict(self._snap)

    def _settle(self, best: int, styles: np.ndarray) -> None:
        """Keep the current style unless another one stays clearly ahead (by 30%) for three looks
        in a row (~6 s). Right after a pause (a new song) the first verdict counts at once."""
        if self._style is None or self._looks <= 2:
            self._style, self._challenger = best, (-1, 0)
            return
        if best == self._style or styles[best] < 1.3 * styles[self._style]:
            self._challenger = (-1, 0)
            return
        count = self._challenger[1] + 1 if self._challenger[0] == best else 1
        self._challenger = (best, count)
        if count >= 3:
            self._style, self._challenger = best, (-1, 0)

    def _load(self):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1     # a small model: one core is plenty and leaves the rest free
        opts.inter_op_num_threads = 1
        session = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
        with open(CLASS_MAP, newline="", encoding="utf-8") as f:
            names = [row["display_name"] for row in csv.DictReader(f)]
        index = {name: i for i, name in enumerate(names)}
        style_weights = np.zeros((len(STYLE_GENRES), len(names)), dtype=np.float32)
        for s, genres in enumerate(STYLE_GENRES.values()):
            for name, w in genres.items():
                style_weights[s, index[name]] = w
        genre_ids = sorted({index[n] for g in STYLE_GENRES.values() for n in g})
        return session, names, index, style_weights, genre_ids

    def _run(self) -> None:
        try:
            self._model = self._load()
        except Exception as e:
            log.warning("AI genre detection unavailable: %s", e)
            self.error = "AI genre detection unavailable"
            return
        log.info("AI genre detection ready (YAMNet, %d sound classes)", len(self._model[1]))
        while True:
            time.sleep(INTERVAL)
            if time.monotonic() > self._wanted_until:
                continue
            self.look(self.audio.recent(WINDOW))

    def look(self, wave: np.ndarray | None) -> None:
        """One look at the last couple of seconds of sound (48 kHz mono); updates what request()
        returns. Needs the model loaded (self._model = self._load()); tools/show_bench.py calls
        this directly to replay songs."""
        session, names, index, style_weights, genre_ids = self._model
        music_id, mood_ids = index["Music"], [index[m] for m in MOODS]
        if wave is None or float(np.sqrt(np.mean(wave ** 2))) < 3e-5:
            # A pause of a couple of seconds usually means the next song: start over.
            self._quiet_since = self._quiet_since or time.monotonic()
            if time.monotonic() - self._quiet_since > 2.0:
                self._looks = 0
            return
        self._quiet_since = None
        # YAMNet learned from sound at everyday levels: music played with the player turned far
        # down (the PC hears it before the Windows volume) looks like near-silence to it. Bring
        # it up to a normal level first (at most 40 dB).
        level = float(np.sqrt(np.mean(wave ** 2)))
        wave = wave * min(100.0, 0.1 / max(level, 1e-9))
        try:
            t0 = time.perf_counter()
            scores = session.run(None, {"patches": log_mel_patches(to_16k(wave))})[0].mean(axis=0)
            took = time.perf_counter() - t0
        except Exception as e:
            log.warning("AI genre detection failed: %s", e)
            return
        if scores[music_id] < 0.15:
            return  # speech, a game, a video: no music to judge
        # A running average over roughly the last 15 s; right after a pause it starts fresh.
        alpha = max(0.15, 1.0 / (self._looks + 1))
        self._ema = scores if self._ema is None or self._looks == 0 else self._ema + alpha * (scores - self._ema)
        self._looks += 1
        # A style scores as its strongest genre (summing would favor styles with many genres).
        styles = (style_weights * self._ema).max(axis=1)
        best = int(np.argmax(styles))
        total = float(styles.sum())
        self._settle(best, styles)
        top = sorted(genre_ids, key=lambda i: -self._ema[i])[:3]
        mood = max(mood_ids, key=lambda i: self._ema[i])
        style = self._style if self._style is not None else best
        with self._lock:
            self._snap = {
                "style": list(STYLE_GENRES)[style] if total > 0.01 else None,
                "confidence": float(styles[style] / total) if total > 0 else 0.0,
                "top": [(names[i], round(float(self._ema[i]), 3)) for i in top],
                "mood": names[mood].replace(" music", "") if self._ema[mood] > 0.05 else None,
                "music": float(self._ema[music_id]),
                "ms": round(took * 1000, 1),
            }
