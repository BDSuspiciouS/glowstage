"""AI genre detection for the light show.

Google's YAMNet sound classifier (trained on AudioSet: 521 sound classes, among them ~50 music
genres and six moods) listens to the PC's sound every couple of seconds, fully offline, through
onnxruntime. Its scores are averaged over the last ~15 seconds and mapped to a light-show style.

The model (tools/yamnet/yamnet.fp16.onnx, from huggingface.co/audiojs/yamnet) takes log-mel
patches, computed here exactly like YAMNet's own frontend: 16 kHz mono, 25 ms Hann frames
every 10 ms, a 512-point FFT magnitude, 64 HTK mel bands from 125 to 7500 Hz, ln(mel + 0.001),
and patches of 96 frames every 48 frames.

When the enhanced genre AI (Discogs-EffNet) is on the PC (fetched on request, see DISCOGS_DIR
below), it picks the style instead: it knows dance music far better. YAMNet then still tells
music from speech and gives the mood.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
import types

import numpy as np

from .audio import RATE, AudioAnalyzer
from .paths import DATA_DIR, MODEL_DIR

log = logging.getLogger(__name__)

MODEL = MODEL_DIR / "yamnet.fp16.onnx"
CLASS_MAP = MODEL_DIR / "yamnet_class_map.csv"

SR = 16000
INTERVAL = 2.0        # seconds between two looks at the music
WINDOW = 2.0          # seconds of sound per look (3 overlapping YAMNet patches)
RELEASE_AFTER = 60.0  # seconds without the light show asking, then the model is let go of (memory)

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


def _session(path):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1     # a small model: one core is plenty and leaves the rest free
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def labels(names: list[str], style_genres: dict[str, dict[str, float]], music: str | None = None,
           moods: tuple[str, ...] = ()) -> types.SimpleNamespace:
    """A model's labels, and how much each one points to each style."""
    index = {name: i for i, name in enumerate(names)}
    weights = np.zeros((len(STYLE_GENRES), len(names)), dtype=np.float32)
    for s, style in enumerate(STYLE_GENRES):
        for name, w in style_genres.get(style, {}).items():
            weights[s, index[name]] = w
    return types.SimpleNamespace(
        names=names, shown=list(names), style_weights=weights,
        genre_ids=sorted({index[n] for g in style_genres.values() for n in g}),
        music_id=index[music] if music else None, mood_ids=[index[m] for m in moods], pool=1)


def load_yamnet() -> types.SimpleNamespace:
    """YAMNet, ready to listen: its labels, and scores(16 kHz wave) -> how much the sound sounds
    like each of them (0..1)."""
    with open(CLASS_MAP, newline="", encoding="utf-8") as f:
        names = [row["display_name"] for row in csv.DictReader(f)]
    m = labels(names, STYLE_GENRES, "Music", MOODS)
    session = _session(MODEL)
    m.name, m.window = "YAMNet", WINDOW
    m.scores = lambda wave: session.run(None, {"patches": log_mel_patches(wave)})[0].mean(axis=0)
    return m


class GenreDetector:
    """Runs on its own thread while the light show is on; request() returns what it heard.

    It listens with YAMNet. Other models only change _load(): what it gives back says which
    labels the model knows, how they point to the styles, and how it scores a stretch of sound;
    the averaging, the settling and what request() returns are shared."""

    def __init__(self, audio: AudioAnalyzer):
        self.audio = audio
        self.error = "" if MODEL.is_file() and CLASS_MAP.is_file() else "AI model not found"
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snap: dict = {}
        self._ema: np.ndarray | None = None
        self._looks = 0
        self._quiet_since: float | None = None
        self._style: int | None = None
        self._challenger: tuple[int, int] = (-1, 0)   # (style, looks in a row it has been clearly ahead)
        self._model: types.SimpleNamespace | None = None
        self._reload = False

    @property
    def model_name(self) -> str:
        """The model listening right now ("" before it has loaded)."""
        return self._model.name if self._model is not None else ""

    def reload(self) -> None:
        """Load the model again at the next look (the enhanced one was just installed or removed)."""
        self._reload = True

    def request(self) -> dict:
        self._wanted_until = time.monotonic() + 3.0
        with self._lock:
            if self._thread is None and not self.error:
                self._thread = threading.Thread(target=self._run, name="genre", daemon=True)
                self._thread.start()
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

    def _load(self) -> types.SimpleNamespace:
        return load_yamnet()

    def _run(self) -> None:
        self._reload = False        # loading now anyway
        try:
            self._model = self._load()
        except Exception as e:
            log.warning("AI genre detection unavailable: %s", e)
            self.error = "AI genre detection unavailable"
            return
        log.info("AI genre detection ready (%s, %d labels)", self._model.name, len(self._model.names))
        while True:
            time.sleep(INTERVAL)
            if self._reload:
                self._swap()
            if time.monotonic() > self._wanted_until:
                if time.monotonic() > self._wanted_until + RELEASE_AFTER and self._release():
                    return
                continue
            self.look(self.audio.recent(self._model.window))

    def _release(self) -> bool:
        """The light show has been off for a while: let go of the model (tens of MB) and end the
        thread. The next request() starts it again and loads the model afresh."""
        with self._lock:
            if time.monotonic() <= self._wanted_until + RELEASE_AFTER:     # (asked again just now)
                return False
            self._model, self._thread = None, None
            self._ema, self._looks, self._style, self._challenger = None, 0, None, (-1, 0)
            self._snap = {}
        log.info("AI genre detection idle: model released")
        return True

    def _swap(self) -> None:
        """Another model: its labels differ, so what was heard so far starts over."""
        self._reload = False
        try:
            model = self._load()
        except Exception as e:
            log.warning("AI genre model didn't load again, keeping %s: %s", self.model_name, e)
            return
        self._model, self._ema, self._looks = model, None, 0
        self._style, self._challenger = None, (-1, 0)
        log.info("AI genre detection now uses %s", model.name)

    def look(self, wave: np.ndarray | None) -> None:
        """One look at the last few seconds of sound (48 kHz mono); updates what request()
        returns. Needs the model loaded (self._model = self._load()); tools/show_bench.py calls
        this directly to replay songs."""
        m = self._model
        if wave is None or float(np.sqrt(np.mean(wave ** 2))) < 3e-5:
            # A pause of a couple of seconds usually means the next song: start over.
            self._quiet_since = self._quiet_since or time.monotonic()
            if time.monotonic() - self._quiet_since > 2.0:
                self._looks = 0
            return
        self._quiet_since = None
        # The models learned from sound at everyday levels: music played with the player turned
        # far down (the PC hears it before the Windows volume) looks like near-silence to them.
        # Bring it up to a normal level first (at most 40 dB).
        level = float(np.sqrt(np.mean(wave ** 2)))
        wave = wave * min(100.0, 0.1 / max(level, 1e-9))
        try:
            t0 = time.perf_counter()
            scores = m.scores(to_16k(wave))
            took = time.perf_counter() - t0
        except Exception as e:
            log.warning("AI genre detection failed: %s", e)
            return
        if m.music_id is not None and scores[m.music_id] < 0.15:
            return  # speech, a game, a video: no music to judge
        # A running average over roughly the last 15 s; right after a pause it starts fresh.
        alpha = max(0.15, 1.0 / (self._looks + 1))
        self._ema = scores if self._ema is None or self._looks == 0 else self._ema + alpha * (scores - self._ema)
        self._looks += 1
        # A style scores as its strongest genre, or its few strongest together when the model
        # knows many close ones (Deep, Tech and Tropical House agreeing say more than one alone;
        # summing them all would favor the styles with the most labels).
        points = m.style_weights * self._ema
        styles = points.max(axis=1) if m.pool == 1 else np.sort(points, axis=1)[:, -m.pool:].sum(axis=1)
        best = int(np.argmax(styles))
        total = float(styles.sum())
        self._settle(best, styles)
        top = sorted(m.genre_ids, key=lambda i: -self._ema[i])[:3]
        mood = max(m.mood_ids, key=lambda i: self._ema[i]) if m.mood_ids else None
        style = self._style if self._style is not None else best
        with self._lock:
            self._snap = {
                "style": list(STYLE_GENRES)[style] if total > 0.01 else None,
                "confidence": float(styles[style] / total) if total > 0 else 0.0,
                "top": [(m.shown[i], round(float(self._ema[i]), 3)) for i in top],
                "mood": m.names[mood].replace(" music", "") if mood is not None and self._ema[mood] > 0.05 else None,
                "music": float(self._ema[m.music_id]) if m.music_id is not None else None,
                "ms": round(took * 1000, 1),
            }


# ---- Discogs-EffNet: 400 record-shop styles ------------------------------------------------------
# YAMNet knows AudioSet's ~50 broad genres, and modern afro, melodic and tech house sound like
# none of them to it (jazz, folk, "Christmas music"). Discogs-EffNet (Alonso-Jimenez, Serra and
# Bogdanov 2022, essentia.upf.edu/models) learned the 400 styles record shops file music under:
# Deep House, Tech House, Tropical House, Trap, Cloud Rap, Reggaeton... On 26 recorded songs
# (tools/genre_bench.py) it picked a right style for 24 against YAMNet's 17, and a right style
# 87% of the time against 54%; drops on or off rightly for all 26 against 21.
#
# Its licence is CC BY-NC-SA 4.0 (non-commercial), which doesn't sit with shipping it inside a
# GPL program, so the user gets it from its makers on request (genre_install.py, Settings → Genre
# AI) into DISCOGS_DIR. YAMNet keeps telling music from speech and gives the mood; without the
# model, YAMNet does everything as before.
#
# Its frontend is Essentia's TensorflowInputMusiCNN: 16 kHz, a 512-point symmetric Hann window
# every 256 samples (the first frame centred on sample 0), power spectrum, 96 Slaney mel bands
# from 0 to 8 kHz with area-normalised triangles, log10(1 + 10000 x), in patches of 128 frames
# (2.05 s) every 62 frames.

DISCOGS_DIR = DATA_DIR / "models"
DISCOGS_MODEL = DISCOGS_DIR / "discogs-effnet-bsdynamic-1.onnx"
DISCOGS_LABELS = DISCOGS_DIR / "discogs-effnet-bsdynamic-1.json"


def _slaney_mel(f):
    f = np.asarray(f, dtype=np.float64)
    return np.where(f >= 1000.0, 15.0 + np.log(np.maximum(f, 1e-9) / 1000.0) / (np.log(6.4) / 27.0), f / (200.0 / 3))


def _slaney_hz(m):
    m = np.asarray(m, dtype=np.float64)
    return np.where(m >= 15.0, 1000.0 * np.exp((m - 15.0) * np.log(6.4) / 27.0), m * 200.0 / 3)


def _discogs_mel_matrix() -> np.ndarray:
    """(257, 96), as Essentia's MelBands(slaneyMel, linear weighting, unit_tri) at 16 kHz."""
    f = _slaney_hz(np.linspace(_slaney_mel(0.0), _slaney_mel(8000.0), 96 + 2))
    scale = 8000.0 / 256
    w = np.zeros((257, 96))
    for i in range(96):
        for j in range(int(np.ceil(f[i] / scale)), int(np.floor(f[i + 2] / scale)) + 1):
            hz = j * scale
            w[j, i] = (hz - f[i]) / (f[i + 1] - f[i]) if hz < f[i + 1] else (f[i + 2] - hz) / (f[i + 2] - f[i + 1])
        w[:, i] /= (f[i + 2] - f[i]) / 2
    return w.astype(np.float32)


_DISCOGS_MEL = _discogs_mel_matrix()
_HANN512_SYM = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(512) / 511)).astype(np.float32)


def discogs_patches(wave: np.ndarray) -> np.ndarray:
    """16 kHz mono float waveform -> (n, 128, 96) float32 mel patches for Discogs-EffNet."""
    x = np.pad(wave, (256, 0))
    n = 1 + (len(x) - 512) // 256
    if n < 128:
        x = np.pad(x, (0, (128 - n) * 256))
        n = 128
    frames = x[np.arange(512)[None, :] + 256 * np.arange(n)[:, None]] * _HANN512_SYM
    mel = (np.abs(np.fft.rfft(frames, 512)) ** 2).astype(np.float32) @ _DISCOGS_MEL
    feat = np.log10(1.0 + 10000.0 * mel)
    count = 1 + (n - 128) // 62
    return np.stack([feat[i * 62:i * 62 + 128] for i in range(count)]).astype(np.float32)


# Each Discogs genre's styles go to one light-show style, apart from the styles named below.
_DISCOGS_GENRES = {"Hip Hop": ("hiphop", 1.0), "Rock": ("rock", 1.0), "Pop": ("pop", 1.0), "Latin": ("latin", 1.0),
                   "Reggae": ("latin", 1.0), "Jazz": ("chill", 1.0), "Classical": ("chill", 1.0),
                   "Blues": ("chill", 1.0), "Folk, World, & Country": ("chill", 0.8), "Stage & Screen": ("chill", 0.8),
                   "Children's": ("pop", 0.6)}
_DISCOGS_NAMED = {   # style name: how much it counts (1 when not given)
    "techno": "Techno|Minimal Techno|Hard Techno|Deep Techno|Schranz|Dub Techno:0.8|Acid:0.8|Minimal:0.7|EBM:0.6|"
              "Industrial:0.6",
    "house": "House|Deep House|Tech House|Tribal House|Garage House|Acid House|Ghetto House|Italo House|Nu-Disco|"
             "Disco|Speed Garage|UK Garage|Tribal:0.8|Hip-House:0.8|Hard House:0.8|Latin:0.8|Beatdown:0.8|"
             "Tropical House:0.8|Jazzdance:0.6|Boogie:0.6|Funk:0.5|P.Funk:0.5|Free Funk:0.5",
    "edm": "Electro House|Progressive House|Trance|Progressive Trance|Psy-Trance|Goa Trance|Tech Trance|Hard Trance|"
           "Hardstyle|Hands Up|Eurodance|Euro House|Jumpstyle|Happy Hardcore|Gabber|Makina|Italodance|Donk|"
           "Speedcore|Big Beat:0.8|Electro:0.6|New Beat:0.6",
    "bass": "Dubstep|Drum n Bass|Jungle|Bassline|Halftime|Breakbeat:0.8|Breaks:0.8|Progressive Breaks:0.8|"
            "Breakcore:0.8|Juke:0.8",
    "hiphop": "Hip Hop|Grime|Contemporary R&B:0.8|New Jack Swing:0.6|Swingbeat:0.6|UK Street Soul:0.6",
    "pop": "Dance-pop|Synth-pop|Disco Polo|Eurobeat:0.8|Hi NRG:0.8|Euro-Disco:0.8|Italo-Disco:0.8|Laïkó:0.8|"
           "Synthwave:0.6|Freestyle:0.6|Éntekhno:0.6|Pop Rock|Soft Rock:0.6|New Wave:0.6",
    "latin": "Afrobeat|African|Highlife|Soukous|Zouk|Séga|Raï|Flamenco|Romani:0.8|Pacific:0.6|Afro-Cuban Jazz:0.8|"
             "Latin Jazz:0.6|Bollywood:0.8",
    "chill": "Ambient|Downtempo|Chillwave|Trip Hop|New Age|Modern Classical|Dark Ambient|Drone|Berlin-School:0.8|"
             "Future Jazz:0.6|Acid Jazz:0.6|Vaporwave:0.6|IDM:0.6|Abstract:0.6|Illbient:0.6|Leftfield:0.5|"
             "Soul|Neo Soul|Gospel|Rhythm & Blues|Psychedelic:0.6|Light Music:0.8|Ballad:0.6|Acoustic:0.8|"
             "Lounge:0.8|Dream Pop:0.8|Ethereal:0.8|Post Rock:0.8",
}
# Style names found under two genres that go to different light-show styles.
_DISCOGS_ONLY = {"Electronic---Hardcore": "edm", "Rock---Hardcore": "rock", "Electronic---Industrial": "techno",
                 "Rock---Industrial": "rock", "Hip Hop---Electro": "hiphop", "Electronic---Electro": "edm",
                 "Electronic---Latin": "house", "Electronic---New Wave": "pop", "Rock---New Wave": "rock"}


def discogs_style_genres(classes: list[str]) -> dict[str, dict[str, float]]:
    """Light-show style -> the Discogs classes ("Electronic---Deep House") that point to it."""
    named: dict[str, list[tuple[str, float]]] = {}
    for style, spec in _DISCOGS_NAMED.items():
        for item in spec.split("|"):
            name, _, w = item.partition(":")
            named.setdefault(name, []).append((style, float(w or 1.0)))
    out: dict[str, dict[str, float]] = {s: {} for s in STYLE_GENRES}
    for c in classes:
        genre_, _, sub = c.partition("---")
        if c in _DISCOGS_ONLY:
            style = _DISCOGS_ONLY[c]
            out[style][c] = next((w for s, w in named.get(sub, []) if s == style), 1.0)
        elif sub in named:
            for style, w in named[sub]:
                out[style][c] = w
        elif genre_ in _DISCOGS_GENRES:
            style, w = _DISCOGS_GENRES[genre_]
            out[style][c] = w
    return out


def load_discogs() -> types.SimpleNamespace:
    """Discogs-EffNet for the style, YAMNet (on the same sound) for music-or-not and the mood:
    the scores are Discogs' 400 styles followed by YAMNet's 521 sounds."""
    import json

    classes = json.loads(DISCOGS_LABELS.read_text(encoding="utf-8"))["classes"]
    session = _session(DISCOGS_MODEL)
    yamnet = load_yamnet()
    m = labels(classes + yamnet.names, discogs_style_genres(classes), "Music", MOODS)
    m.shown = [c.partition("---")[2] for c in classes] + yamnet.names
    m.name, m.window = "Discogs-EffNet", 4.0      # a patch is 2.05 s; 4 s gives two
    m.pool = 2      # a style scores as its two strongest Discogs styles together (24/26 against 22/26 for one)

    def scores(wave: np.ndarray) -> np.ndarray:
        styles = session.run(["activations"], {"melspectrogram": discogs_patches(wave)})[0].mean(axis=0)
        return np.concatenate([styles, yamnet.scores(wave[-int(WINDOW * SR):])])

    m.scores = scores
    return m


class DiscogsDetector(GenreDetector):
    """Styles from Discogs-EffNet when its model is in DISCOGS_DIR; YAMNet alone otherwise, or
    when it doesn't load."""

    def _load(self) -> types.SimpleNamespace:
        if DISCOGS_MODEL.is_file() and DISCOGS_LABELS.is_file():
            try:
                return load_discogs()
            except Exception as e:
                log.warning("Discogs-EffNet didn't load, YAMNet listens instead: %s", e)
        return load_yamnet()


# Detectors by name, for tools/genre_bench.py.
DETECTORS: dict[str, type[GenreDetector]] = {"yamnet": GenreDetector, "discogs": DiscogsDetector}
