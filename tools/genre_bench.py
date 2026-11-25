"""Genre AI benchmark on real songs (recorded with tools/record_songs.py). Not part of the app.

Each recording is fed to a genre detector the way the app does it (a look every 2 s at the
last 2-4 s of sound, with a fake clock), and the style it settles on is compared with the hand
label in testdata/recordings/genres.json:

    {"songs": {"Artist - Title": {"genre": "afro house", "style": "house", "also": ["edm"]}}}

A song counts as right when the style it spent most of its time in (after the first 10 s) is the
label's style or one of its "also" styles. "drops right" is looser: a right style, or at least
one that has drops when the song's style does (and none when it doesn't), which is what decides
the blinders and the soft looks. "a right style ... of the time" counts every look from the
first verdict on, the song's intro included.

    python tools/genre_bench.py                      # every recording, YAMNet
    python tools/genre_bench.py --model discogs      # the app's detector: Discogs-EffNet when it's
                                                     # in genre.DISCOGS_DIR, else YAMNet
    python tools/genre_bench.py --model ced-mini --model-dir DIR   # CED, tried and not taken
    python tools/genre_bench.py --csv out.csv        # also write one row per look
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time as real_time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from glowstage import audio, genre, show  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from show_bench import RECORDINGS, load  # noqa: E402

R = audio.RATE
SETTLE = 10.0      # seconds before the verdict counts (the show plays a tempo guess until then)


# ---- CED: compared, not taken --------------------------------------------------------------------
# CED (Dinkel et al. 2023, github.com/RicherMans/CED, weights Apache-2.0) knows the same AudioSet
# genres as YAMNet and hears them better (AudioSet mAP 0.48-0.49 against 0.31), but on the recorded
# songs it did no better: AudioSet itself has no afro or melodic house, and calls them Techno.
# ONNX: huggingface.co/k2-fsa/sherpa-onnx-ced-{tiny,mini}-audio-tagging-2024-04-19, model.int8.onnx
# saved as ced-tiny.int8.onnx / ced-mini.int8.onnx, class_labels_indices.csv as audioset_labels.csv.
# Its input is torchaudio's MelSpectrogram + AmplitudeToDB: 16 kHz, a periodic 512-point Hann
# window every 10 ms, centred (reflect-padded) frames, power spectrum, 64 HTK mel bands 0-8 kHz
# without normalisation, in dB (floor 1e-10, at most 120 dB below the loudest).

CED_DIR = genre.MODEL_DIR


def _ced_mel_matrix() -> np.ndarray:
    """(257, 64), as torchaudio.functional.melscale_fbanks(257, 0, 8000, 64, 16000)."""
    htk = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)   # noqa: E731
    freqs = np.linspace(0, 8000, 257)
    pts = 700.0 * (10 ** (np.linspace(htk(0.0), htk(8000.0), 64 + 2) / 2595.0) - 1.0)
    slopes = pts[None, :] - freqs[:, None]
    down, up = -slopes[:, :-2] / np.diff(pts)[:-1], slopes[:, 2:] / np.diff(pts)[1:]
    return np.maximum(0.0, np.minimum(down, up)).astype(np.float32)


_CED_MEL = _ced_mel_matrix()
_HANN512 = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(512) / 512)).astype(np.float32)


def ced_features(wave: np.ndarray) -> np.ndarray:
    """16 kHz mono float waveform -> (1, 64, frames) float32 dB mel spectrogram for CED."""
    x = np.pad(wave, 256, mode="reflect")
    n = 1 + (len(x) - 512) // 160
    frames = x[np.arange(512)[None, :] + 160 * np.arange(n)[:, None]] * _HANN512
    db = 10.0 * np.log10(np.maximum((np.abs(np.fft.rfft(frames, 512)) ** 2).astype(np.float32) @ _CED_MEL, 1e-10))
    return np.maximum(db, db.max() - 120.0).T[None].astype(np.float32)


class CEDDetector(genre.GenreDetector):
    size = "mini"

    def _load(self):
        import csv
        with open(CED_DIR / "audioset_labels.csv", newline="", encoding="utf-8") as f:
            names = [row["display_name"] for row in csv.DictReader(f)]
        m = genre.labels(names, genre.STYLE_GENRES, "Music", genre.MOODS)
        session = genre._session(CED_DIR / f"ced-{self.size}.int8.onnx")
        m.name, m.window = f"CED-{self.size}", 2.0
        m.scores = lambda wave: session.run(None, {"feats": ced_features(wave)})[0][0]
        return m


class CEDTinyDetector(CEDDetector):
    size = "tiny"


def replay(data: np.ndarray, make) -> dict:
    """Feeds the song to a fresh detector, one look every genre.INTERVAL seconds."""
    clock = types.SimpleNamespace(t=0.0)
    fake = types.SimpleNamespace(monotonic=lambda: clock.t, sleep=lambda s: None, perf_counter=real_time.perf_counter)
    genre.time = fake
    try:
        det = make()
        looks = []
        t = genre.INTERVAL
        while t <= len(data) / R:
            clock.t = t
            n = int(det._model.window * R)
            end = int(t * R)
            t0 = real_time.perf_counter()
            det.look(data[max(0, end - n):end] if end >= n else None)
            took = real_time.perf_counter() - t0
            snap = dict(det._snap)
            looks.append({"t": t, "style": snap.get("style"), "conf": snap.get("confidence", 0.0),
                          "top": snap.get("top", []), "ms": took * 1000})
            t += genre.INTERVAL
    finally:
        genre.time = real_time
    return {"looks": looks}


def verdict(looks: list[dict]) -> tuple[str | None, dict[str, float]]:
    counted = [lk["style"] for lk in looks if lk["t"] >= SETTLE]
    shares = collections.Counter(counted)
    total = max(1, len(counted))
    share = {s: n / total for s, n in shares.most_common()}
    best = next((s for s in share if s), None)
    return best, share


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("songs", nargs="*", type=Path)
    ap.add_argument("--model", default="yamnet", help="detector: yamnet, discogs, ced-mini or ced-tiny")
    ap.add_argument("--labels", type=Path, default=RECORDINGS / "genres.json")
    ap.add_argument("--csv", type=Path, help="write every look to this file")
    ap.add_argument("--model-dir", type=Path, help="look for the model files here instead")
    ap.add_argument("--pool", type=int, help="a style scores as its N strongest labels together")
    ap.add_argument("--window", type=float, help="seconds of sound per look (default: the detector's)")
    ap.add_argument("--top", action="store_true", help="print the model's top labels per song")
    args = ap.parse_args()
    labels = json.loads(args.labels.read_text(encoding="utf-8"))["songs"]
    if args.songs:
        songs = args.songs
    else:
        index = json.loads((RECORDINGS / "index.json").read_text(encoding="utf-8"))
        songs = [RECORDINGS / e["file"] for e in index]
    detectors = {**genre.DETECTORS, "ced-mini": CEDDetector, "ced-tiny": CEDTinyDetector}
    cls = detectors[args.model]
    global CED_DIR
    if args.model_dir:
        CED_DIR = args.model_dir
        genre.DISCOGS_MODEL = args.model_dir / genre.DISCOGS_MODEL.name
        genre.DISCOGS_LABELS = args.model_dir / genre.DISCOGS_LABELS.name
    model = None

    def make():
        nonlocal model
        det = cls(types.SimpleNamespace(recent=lambda s: None))
        if model is None:
            t0 = real_time.perf_counter()
            model = det._load()
            if args.window:
                model.window = args.window
            if args.pool:
                model.pool = args.pool
            print(f"{args.model}: {model.name} loaded in {real_time.perf_counter() - t0:.1f} s, "
                  f"{model.window:g} s of sound per look, styles from their {model.pool} strongest labels")
        det._model = model
        return det

    rows, right, drops_ok, n, ms, time_ok = [], 0, 0, 0, [], []
    for path in songs:
        title = path.stem[16:]
        label = labels.get(title)
        data = load(path)
        run = replay(data, make)
        best, share = verdict(run["looks"])
        ms += [lk["ms"] for lk in run["looks"]]
        first = next((lk for lk in run["looks"] if lk["style"]), None)
        if label:
            ok_styles = [label["style"], *label.get("also", [])]
            ok = best in ok_styles
            dok = ok or (best is not None and show.STYLES[best].drops == show.STYLES[label["style"]].drops)
            right += ok
            drops_ok += dok
            styled = [lk["style"] for lk in run["looks"] if lk["style"]]   # from the first verdict on
            time_ok.append(sum(s in ok_styles for s in styled) / max(1, len(styled)))
            n += 1
            mark = "ok " if ok else "-- " if dok else "XX "
            want = "/".join(ok_styles)
        else:
            mark, want = "?? ", "unlabelled"
        mix = ", ".join(f"{s} {v:.0%}" for s, v in list(share.items())[:3])
        print(f"{mark}{title[:44]:44s} want {want:18s} got {mix}"
              + (f"  (first at {first['t']:.0f} s)" if first else "  (no verdict)"))
        if args.top:
            tops = collections.Counter()
            for lk in run["looks"]:
                for name, v in lk["top"]:
                    tops[name] += v
            print("      top:", ", ".join(f"{k} {v / max(1, len(run['looks'])):.3f}" for k, v in tops.most_common(5)))
        for lk in run["looks"]:
            rows.append((title, lk["t"], lk["style"], round(lk["conf"], 3), round(lk["ms"], 1)))
    print(f"\n{args.model}: style right {right}/{n}, drops right {drops_ok}/{n}, "
          f"a right style {np.mean(time_ok):.0%} of the time, {np.median(ms):.0f} ms per look (median, 1 thread), p95 {np.percentile(ms, 95):.0f} ms")
    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["song", "t", "style", "confidence", "ms"])
            w.writerows(rows)


if __name__ == "__main__":
    main()
