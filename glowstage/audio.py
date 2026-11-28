"""Listens to what the PC is playing (WASAPI loopback of the default output device, so no
microphone) and turns it into what the music effects need: loudness, 16 frequency bands from
bass to treble, beats, snare/hi-hat hits and the tempo. Runs only while a music effect is in use.

Only when the person chooses it, it listens to a microphone instead (for music that doesn't come
from the PC: a record player, a party, a band). It is the same analysis, on sound that is thrown
away as it is analyzed; the stream is only open while something needs the sound."""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import warnings

import numpy as np

from . import harmony
from .impacts import Impacts

log = logging.getLogger(__name__)

RATE = 48000
BLOCK = 1024                 # ~21 ms per analysis step
BLOCK_RATE = RATE / BLOCK
FFT_SIZE = 2048              # longer window for usable bass resolution (~23 Hz per bin)
BANDS = 16
BAND_EDGES = np.geomspace(40, 16000, BANDS + 1)
SILENCE_RMS = 2e-5           # (-94 dBFS: what the PC plays is digital silence between songs; music
                             # played very softly, e.g. with the player turned far down, still counts)
KEEP_SECONDS = 4.5           # raw sound kept for the genre detector (it looks at up to 4 s)
TEMPO_SECONDS = 8.0          # onset history the tempo is estimated from

# A microphone hears the room: much quieter than what the PC plays, and never really silent.
MIC_TARGET = 0.1             # the level (rms) the analysis gets to see, about what music from the PC has
MIC_FLOOR = 2e-3             # (-54 dBFS) below this the microphone only hears the room: it counts as silence
                             # and isn't turned up, so a quiet room doesn't become noise
MIC_MAX_GAIN = MIC_TARGET / MIC_FLOOR
MIC_MEMORY = 0.995           # the loudest recent level fades by this much per block: ~4 s
FOLLOW_SECONDS = 2.0         # how often it looks for a switch of output or microphone
DEFAULT_MIC = "default"      # the setting's value that follows Windows' default microphone


def quiet() -> dict:
    """What effects see when nothing is playing (or before the first block arrives)."""
    return {"level": 0.0, "bands": [0.0] * BANDS, "bass": 0.0, "mid": 0.0, "treble": 0.0,
            "since_beat": 99.0, "beats_ago": [], "beat_count": 0, "silent": True,
            "rms": 0.0, "bass_raw": 0.0, "high_raw": 0.0, "bpm": 0.0, "bpm_conf": 0.0,
            "grid_ago": None, "grid_conf": 0.0, "hit_ago": 99.0, "hat_ago": 99.0,
            "chord": None, "chord_ago": 99.0, "key": None, "key_conf": 0.0, "centroid": 0.0, "impacts": []}


def estimate_tempo(onsets: np.ndarray) -> tuple[float, float, float, float]:
    """Tempo and beat grid from an onset-strength curve sampled at BLOCK_RATE.

    Tempo: the autocorrelation peak between 70 and 190 BPM, weighted toward ~120 BPM so a song
    isn't read at double or half speed, refined to a fraction of a block. Grid: a comb at that
    period slid over the whole curve finds where the beats fall, using every beat of the last
    seconds instead of a single kick. Returns (bpm, confidence 0..1, blocks since the last beat
    of the grid, how clearly the grid stands out 0..1)."""
    env = onsets - onsets.mean()
    if len(env) < BLOCK_RATE * 4 or not env.any():
        return 0.0, 0.0, 0.0, 0.0
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    if not ac[0] > 0:
        return 0.0, 0.0, 0.0, 0.0
    ac /= ac[0]
    lo, hi = int(BLOCK_RATE * 60 / 190), int(BLOCK_RATE * 60 / 70) + 1
    lags = np.arange(lo, hi)
    bpms = 60 * BLOCK_RATE / lags
    weight = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    k = int(np.argmax(ac[lo:hi] * weight)) + lo
    a, b, c = ac[k - 1], ac[k], ac[k + 1] if k + 1 < len(ac) else ac[k]
    if not b > 0:   # nothing repeats (e.g. the first onsets after silence): no tempo yet
        return 0.0, 0.0, 0.0, 0.0
    # (the peak's top between blocks; within half a block, or a flat top threw it anywhere, even
    # below zero, and the grid search below crashed: that stopped listening at every song start)
    shift = float(np.clip(0.5 * (a - c) / (a - 2 * b + c), -0.5, 0.5)) if (a - 2 * b + c) < 0 else 0.0
    period = min(max(k + shift, lo), hi - 1)   # (and inside 70..190 BPM)
    offsets = np.arange(0.0, period, 0.5)
    teeth = np.arange(int((len(onsets) - 1) // period))
    where = len(onsets) - 1 - offsets[:, None] - period * teeth[None, :]
    score = np.interp(where, np.arange(len(onsets)), onsets).sum(axis=1)
    best = float(offsets[int(np.argmax(score))])
    clarity = float(min(1.0, max(0.0, (score.max() / max(score.mean(), 1e-9) - 1) / 1.5)))
    return float(60 * BLOCK_RATE / period), float(max(0.0, min(1.0, b * 2))), best, clarity


def output_kind(speaker) -> str:
    """"bluetooth", "remote" (Remote Desktop) or "local": how the sound gets to your ears.
    Bluetooth headphones and speakers play the sound 150-300 ms after the PC sends it."""
    if "remote audio" in str(speaker.name).lower():
        return "remote"
    try:
        import struct

        from soundcard import mediafoundation as mf
        store = mf._ffi.new("IPropertyStore **")
        ptr = speaker._device_ptr()
        hr = ptr[0][0].lpVtbl.OpenPropertyStore(ptr[0], 0, store)
        mf._com.release(ptr)
        mf._com.check_error(hr)
        value = mf._PropVariant()
        # PKEY_Device_EnumeratorName: "BTHENUM" (and BTHHFENUM, BTHLEDevice) for Bluetooth.
        key = mf._ffi.new("PROPERTYKEY *", [[0xa45c254e, 0xdf1c, 0x4efd,
                                             [0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0]], 24])
        hr = store[0][0].lpVtbl.GetValue(store[0], key, value.ptr)
        mf._com.release(store)
        mf._com.check_error(hr)
        if value.ptr[0].vt != 31:   # not a string
            return "local"
        data = mf._ffi.cast("short*", value.ptr[0].data)
        n = next((i for i in range(256) if data[i] == 0), 256)
        name = struct.pack("h" * n, *data[0:n]).decode("utf-16")
        return "bluetooth" if name.upper().startswith("BTH") else "local"
    except Exception as e:   # soundcard internals changed, or no such property: assume wired
        log.debug("Could not tell how %s is connected: %s", speaker.name, e)
        return "local"


class MicGain:
    """Automatic gain for a microphone: brings what it hears to a level the analysis can use.

    It follows the loudest recent level (it turns down at once when it gets loud, and turns up
    slowly over a few seconds when it gets quiet), so a quiet party and a loud band both arrive at
    about MIC_TARGET. Below MIC_FLOOR it does nothing: that is the room, not the music."""

    def __init__(self):
        self.env = MIC_FLOOR
        self.heard = False       # did the last block hold more than the room's noise?

    def apply(self, data: np.ndarray) -> np.ndarray:
        rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64)))) if data.size else 0.0
        self.heard = rms > MIC_FLOOR
        self.env = max(self.env * MIC_MEMORY, rms, MIC_FLOOR)
        if not self.heard:
            return data
        gain = min(MIC_MAX_GAIN, MIC_TARGET / self.env)
        return np.clip(data * gain, -1.0, 1.0).astype(np.float32)


def clean_source(value) -> str:
    """The setting "show_input": "" (what the PC plays), "default" or a microphone's id."""
    return str(value or "").strip()[:300]


def list_microphones(sc) -> dict:
    """The PC's microphones as {"inputs": [{"id", "name"}], "default": id of Windows' default or ""}.
    What the speakers play ("loopback") is left out. Needs the audio thread (see microphones)."""
    inputs = [{"id": str(m.id), "name": str(m.name)} for m in sc.all_microphones(include_loopback=False)]
    try:
        default = str(sc.default_microphone().id)
    except Exception:       # no microphone at all
        default = ""
    return {"inputs": inputs, "default": default}


class AudioAnalyzer:
    def __init__(self):
        self.device = ""
        self.device_kind = "local"      # "local", "bluetooth", "remote", or "microphone"
        self.source = ""                # what it listens to: "" the PC's sound, "default" or a microphone's id
        self.missing = False            # the chosen microphone isn't there
        self._pc_until = 0.0            # asked for the PC's own sound until then, whatever the source (see request)
        self._agc: MicGain | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._last_failure = ""      # the last reason listening failed (logged once)
        self._retry = 2.0
        self._snap = quiet()
        self._wanted_until = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1 / RATE)
        self._band_bins = [np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(BAND_EDGES[:-1], BAND_EDGES[1:])]
        # (above the bass and the kick: their coming and going must not move it)
        self._centroid_bins = np.where((freqs >= 400) & (freqs <= 16000))[0]
        self._centroid_oct = np.log2(freqs[self._centroid_bins] / 400.0)
        self._band_max = np.full(BANDS, 1e-3)
        self._bands = np.zeros(BANDS)
        self._level_max = 1e-3
        self._level = 0.0
        self._bass_hist: list[float] = []
        self._beats: list[float] = []
        self._beat_count = 0
        self._last_sound = 0.0
        self._prev_raw = np.zeros(BANDS)
        self._onsets: collections.deque[float] = collections.deque(maxlen=int(BLOCK_RATE * TEMPO_SECONDS))
        self._mid_flux: collections.deque[float] = collections.deque(maxlen=47)
        self._high_flux: collections.deque[float] = collections.deque(maxlen=47)
        self._hits: list[float] = []      # snares and claps
        self._crack_peak = 1e-6
        self._hats: list[float] = []      # hi-hats and other high ticks
        self._tempo = (0.0, 0.0)
        self._grid: tuple[float, float] | None = None    # (time of the grid's last beat, clarity)
        self._blocks_since_tempo = 0
        self._recent: collections.deque[np.ndarray] = collections.deque(maxlen=int(KEEP_SECONDS * BLOCK_RATE) + 1)
        # Harmony: chroma over ~0.25 s for the chord, over ~20 s for the key.
        self._chroma_w = harmony.chroma_matrix(RATE)
        self._bass_w = harmony.bass_matrix(RATE)
        self._hann_long = np.hanning(harmony.FFT).astype(np.float32)
        self._chroma = np.zeros(12)
        self._bass_chroma = np.zeros(12)
        self._chroma_long = np.zeros(12)
        self._chord: tuple[int, bool] | None = None
        self._chord_at = 0.0
        self._chord_cand: tuple[tuple[int, bool] | None, int] = (None, 0)
        self._key: tuple[tuple[int, bool] | None, float] = (None, 0.0)
        self._blocks_since_chroma = 0
        self._impacts = Impacts(BLOCK / RATE)   # shots and explosions, left or right (impacts.py)

    # ---- used by the effect engine ----------------------------------------------------------

    def set_source(self, source, name: str = "") -> None:
        """Listen to the PC's sound ("") or a microphone ("default" or its id; `name` is what to show
        until the stream says it). The stream follows on its own within a moment."""
        source = clean_source(source)
        self.missing = False
        self._last_failure = ""
        if source == self.source and not name:
            return
        self.source = source
        self.device = name if source else ""
        self.device_kind = "microphone" if source else "local"

    def pc_requested(self) -> bool:
        return time.monotonic() < self._pc_until

    def request(self, pc_sound: bool = False) -> dict:
        """Keep listening for the next few seconds and return the latest analysis. `pc_sound` asks
        for the PC's own sound even while a microphone is chosen: the automatic start by music
        uses it, so the room's sound never starts the show."""
        now = time.monotonic()
        self._wanted_until = now + 3.0
        self._pc_until = now + 3.0 if pc_sound else 0.0
        self._ensure_thread()
        with self._lock:
            snap = dict(self._snap)
        now = time.monotonic()
        snap["beats_ago"] = [now - b for b in self._beats]
        snap["since_beat"] = snap["beats_ago"][-1] if self._beats else 99.0
        snap["hit_ago"] = now - self._hits[-1] if self._hits else 99.0
        snap["hat_ago"] = now - self._hats[-1] if self._hats else 99.0
        grid = self._grid
        snap["grid_ago"] = now - grid[0] if grid and not snap["silent"] else None
        snap["grid_conf"] = grid[1] if grid else 0.0
        snap["chord_ago"] = now - self._chord_at if self._chord else 99.0
        snap["impacts"] = self._impacts.recent(now)
        return snap

    def recent(self, seconds: float) -> np.ndarray | None:
        """The last few seconds of sound as mono float32 at RATE (None while too little is in)."""
        with self._lock:
            blocks = list(self._recent)
        if sum(len(b) for b in blocks) < seconds * RATE:
            return None
        return np.concatenate(blocks)[-int(seconds * RATE):]

    @property
    def active(self) -> bool:
        return time.monotonic() < self._wanted_until

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
            self._thread.start()

    def microphones(self, timeout: float = 5.0) -> dict:
        """The PC's microphones (see list_microphones). The audio thread lists them: it is the one
        that set up soundcard's Windows COM. Listing does not open any of them."""
        done, box = threading.Event(), {}

        def job(sc):
            try:
                box["result"] = list_microphones(sc)
            except Exception as e:
                box["error"] = e
            done.set()

        self._jobs.put(job)
        self._ensure_thread()
        if not done.wait(timeout):
            raise OSError("The microphones can't be listed right now")
        if "error" in box:
            raise OSError(f"The microphones can't be listed: {box['error']}")
        return box["result"]

    def _serve_jobs(self, sc) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            job(sc)

    def _source_now(self) -> str:
        return "" if self.pc_requested() else self.source

    # ---- capture -----------------------------------------------------------------------------

    def _run(self) -> None:
        # One long-lived thread: soundcard sets up Windows COM for the thread that imports it.
        warnings.simplefilter("ignore")  # soundcard warns about tiny gaps in the stream
        try:
            import soundcard as sc
        except Exception as e:
            log.warning("Music effects unavailable: %s", e)
            return
        while True:
            self._step(sc)

    def _step(self, sc) -> None:
        """One turn of the audio thread: answer questions, and listen only while something needs
        the sound (when nothing does, no stream is open and a microphone is let go)."""
        self._serve_jobs(sc)
        if not self.active:
            with self._lock:
                self._snap = quiet()
            time.sleep(0.3)
            return
        try:
            self._listen_once(sc)
        except Exception as e:
            # (No sound output at all, e.g. a Remote Desktop session that was closed: say so
            # once, then retry ever more calmly. A line every 2 s filled the log in a day and
            # pushed out everything else in it.)
            mic = bool(self._source_now())
            failed = str(e)
            if failed != self._last_failure:
                log.warning("Listening to the %s failed: %s", "microphone" if mic else "PC's audio", failed)
                self._last_failure, self._retry = failed, 2.0
            else:
                self._retry = min(30.0, self._retry * 1.5)
            if mic:
                self.missing = True      # (a microphone that stops answering is gone, as far as we can tell)
            else:
                self.device = ""
            time.sleep(self._retry)
            return
        self._last_failure = ""

    def _listen_once(self, sc) -> None:
        """One stretch of listening: until nothing needs the sound, or the output, the microphone or
        the choice between them changes. The stream is closed when it returns."""
        source = self._source_now()
        if source:
            self._listen_microphone(sc, source)
        else:
            self._listen_pc(sc)

    def _listen_pc(self, sc) -> None:
        speaker = sc.default_speaker()
        mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        self.device_kind = output_kind(speaker)
        self.device = speaker.name
        self.missing = False
        self._agc = None
        log.info("Listening to %s (%s) for music effects", speaker.name, self.device_kind)
        self._reset()
        checked = time.monotonic()
        with mic.recorder(samplerate=RATE, blocksize=BLOCK) as rec:
            while self.active and not self._source_now():
                self._serve_jobs(sc)
                self._analyze(rec.record(numframes=BLOCK))
                if time.monotonic() - checked > FOLLOW_SECONDS:  # follow a switch to speakers/headphones
                    checked = time.monotonic()
                    if sc.default_speaker().id != speaker.id:
                        break

    def _listen_microphone(self, sc, source: str) -> None:
        wanted = list_microphones(sc)["default"] if source == DEFAULT_MIC else source
        mic = next((m for m in sc.all_microphones(include_loopback=False) if str(m.id) == wanted), None) if wanted else None
        if mic is None:
            # Gone (unplugged, or none at all): say so and wait for it. Never switch to another one.
            if not self.missing:
                log.info("The microphone for the light show isn't there")
            self.missing = True
            with self._lock:
                self._snap = quiet()
            time.sleep(2.0)
            return
        self.missing = False
        self.device_kind = "microphone"
        self.device = str(mic.name)
        self._agc = MicGain()
        log.info("Listening to the microphone %s for music effects", mic.name)
        self._reset()
        checked = time.monotonic()
        with mic.recorder(samplerate=RATE, blocksize=BLOCK) as rec:
            while self.active and self._source_now() == source:
                self._serve_jobs(sc)
                self._analyze(rec.record(numframes=BLOCK))
                if time.monotonic() - checked > FOLLOW_SECONDS:   # follow Windows' default, notice an unplugged one
                    checked = time.monotonic()
                    now = list_microphones(sc)
                    if str(mic.id) not in {m["id"] for m in now["inputs"]} or (
                            source == DEFAULT_MIC and now["default"] != str(mic.id)):
                        break

    def _analyze(self, data: np.ndarray) -> None:
        if self._agc is not None:
            data = self._agc.apply(data)       # (a microphone: brought to a usable level, room noise left alone)
        mono = data.mean(axis=1).astype(np.float32) if data.ndim == 2 else data.astype(np.float32)
        n = len(mono)
        if n == 0:
            return
        self._buf = np.roll(self._buf, -n)
        self._buf[-n:] = mono[-FFT_SIZE:]
        now = time.monotonic()
        self._impacts.feed(data, now)       # (stereo: which side a shot is on)

        rms = float(np.sqrt(np.mean(mono ** 2)))
        if self._agc.heard if self._agc is not None else rms > SILENCE_RMS:
            self._last_sound = now
        silent = now - self._last_sound > 0.6

        spectrum = np.abs(np.fft.rfft(self._buf * self._window))
        raw = np.array([spectrum[b].mean() if len(b) else 0.0 for b in self._band_bins])
        # Automatic gain per band: the loudest recent value maps to 1, decaying over a few seconds.
        self._band_max = np.maximum(self._band_max * 0.996, raw)
        target = np.clip(raw / np.maximum(self._band_max, 1e-3), 0, 1) ** 1.3
        self._bands = np.where(target > self._bands, target, self._bands * 0.82 + target * 0.18)

        self._level_max = max(self._level_max * 0.997, rms, 1e-3)
        level = min(1.0, rms / self._level_max)
        self._level = level if level > self._level else self._level * 0.9 + level * 0.1

        # Beats: bass energy jumping well above its average of the last second.
        bass = float(raw[:3].mean())
        hist = self._bass_hist
        avg = (sum(hist) / len(hist)) if hist else bass
        hist.append(bass)
        if len(hist) > 47:
            hist.pop(0)
        since = now - self._beats[-1] if self._beats else 99.0
        if not silent and since > 0.25 and bass > avg * 1.35 and bass > self._band_max[:3].mean() * 0.35:
            self._beats = (self._beats + [now])[-6:]
            self._beat_count += 1

        # Onsets: how much each band got louder since the last block (spectral flux). The whole
        # spectrum, the bass counting most, feeds tempo and beat grid. A snare or clap is a burst
        # of "crack" (0.8-5.5 kHz) that a bass note doesn't have; a hi-hat only reaches above that.
        rise = np.maximum(0.0, np.log1p(raw * 1e3) - np.log1p(self._prev_raw * 1e3))
        # The same jump in absolute terms: a soft hi-hat can double a quiet band (a big relative
        # jump) but never carries as much crack as the song's snares and claps.
        jump = np.maximum(0.0, raw - self._prev_raw)
        self._prev_raw = raw
        self._onsets.append(0.0 if silent else float(1.5 * rise[:4].sum() + rise[4:].sum()))
        crack, air = float(rise[8:13].sum()), float(rise[13:].sum())
        crack_e = float(jump[8:13].sum())
        self._crack_peak = max(crack_e, self._crack_peak * 0.995)    # the loudest crack of the last few s
        crack_avg = (sum(self._mid_flux) / len(self._mid_flux)) if self._mid_flux else crack
        air_avg = (sum(self._high_flux) / len(self._high_flux)) if self._high_flux else air
        self._mid_flux.append(crack)
        self._high_flux.append(air)
        last_hit = now - self._hits[-1] if self._hits else 99.0
        last_hat = now - self._hats[-1] if self._hats else 99.0
        if not silent:
            if (last_hit > 0.1 and crack > crack_avg * 1.7 and crack > 1.2 and crack > 0.5 * air
                    and crack_e > 0.3 * self._crack_peak):
                self._hits = (self._hits + [now])[-4:]
            elif last_hat > 0.06 and air > air_avg * 1.8 and air > 0.6:
                self._hats = (self._hats + [now])[-4:]
        self._blocks_since_tempo += 1
        if self._blocks_since_tempo >= BLOCK_RATE / 2:
            self._blocks_since_tempo = 0
            bpm, conf, beat_blocks, clarity = estimate_tempo(np.array(self._onsets))
            self._tempo = (bpm, conf)
            # The newest onset belongs to the block that ended `now`; its middle is half a block back.
            self._grid = (now - (beat_blocks + 0.5) / BLOCK_RATE, clarity) if bpm else None

        self._blocks_since_chroma += 1
        if self._blocks_since_chroma >= 4 and not silent:
            self._blocks_since_chroma = 0
            self._harmony(now)

        b = self._bands
        with self._lock:
            self._recent.append(mono)
            self._snap = {
                "level": 0.0 if silent else self._level,
                "bands": [0.0] * BANDS if silent else [float(x) for x in b],
                "bass": 0.0 if silent else float(b[:4].mean()),
                "mid": 0.0 if silent else float(b[4:10].mean()),
                "treble": 0.0 if silent else float(b[10:].mean()),
                "beat_count": self._beat_count,
                "silent": silent,
                # Not auto-gained, so the light show can tell a quiet break from a loud drop.
                "rms": 0.0 if silent else rms,
                "bass_raw": 0.0 if silent else bass,
                "high_raw": 0.0 if silent else float(raw[10:].mean()),
                "bpm": self._tempo[0],
                "bpm_conf": self._tempo[1],
                "chord": None if silent else self._chord,
                "key": self._key[0],
                "key_conf": self._key[1],
                # Where the weight of the sound above the bass sits, in octaves above 400 Hz: a
                # riser sweeping up (and a filter opening) moves it up by an octave or more.
                "centroid": 0.0 if silent else self._centroid(spectrum),
            }

    def _centroid(self, spectrum: np.ndarray) -> float:
        band = spectrum[self._centroid_bins]
        total = float(band.sum())
        return float((band * self._centroid_oct).sum() / total) if total > 1e-9 else 0.0

    def _harmony(self, now: float) -> None:
        """Chord and key from the last 8192 samples. A new chord has to win six looks in a row
        (~half a second) before it counts, so passing notes and drum hits don't flip it."""
        blocks = list(self._recent)
        if sum(len(b) for b in blocks) < harmony.FFT:
            return
        wave = np.concatenate(blocks)[-harmony.FFT:]
        spectrum = np.abs(np.fft.rfft(wave * self._hann_long))
        c = harmony.chroma(spectrum, self._chroma_w)
        self._chroma = self._chroma * 0.6 + c * 0.4
        self._bass_chroma = self._bass_chroma * 0.6 + harmony.chroma(spectrum, self._bass_w) * 0.4
        self._chroma_long = self._chroma_long * 0.996 + c * 0.004
        best, clarity = harmony.chord(self._chroma, self._bass_chroma)
        if best is not None and clarity < 0.15:
            best = self._chord               # too close to call: keep the current chord
        cand, count = self._chord_cand
        count = count + 1 if best == cand else 1
        self._chord_cand = (best, count)
        if best != self._chord and count >= 6:
            self._chord, self._chord_at = best, now - 0.45   # it really changed about this long ago
        self._key = harmony.key(self._chroma_long)
