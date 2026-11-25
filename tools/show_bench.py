"""Light show benchmark on real songs (recorded with tools/record_songs.py). Not part of the app.

Each song is played through the app's own analyzer, AI genre detection and show director as
fast as the PC can, with a fake clock, exactly as if it came out of the speakers. Then it
compares what the show did with where the builds, drops and breaks really are, as marked in
<song>.marks.json next to the recording:

    {"checked": true, "marks": [{"t": 61.9, "kind": "break"}, {"t": 76.9, "kind": "build"},
                                {"t": 91.9, "kind": "drop"}]}

Songs without marks get suggested ones (worked out from the whole song in hindsight, which
the live show can't do) with "checked": false, and a picture to check them against:
<song>.png shows the spectrogram, the bass, the "lift" (highs, brightness and hits rising
together), what the show did, and the marks.

    python tools/show_bench.py                 # every song in testdata/recordings
    python tools/show_bench.py SONG.wav --plot # one song, and draw its picture
    python tools/show_bench.py --no-ai         # style from the tempo, as before the AI decides
"""

from __future__ import annotations

import argparse
import json
import sys
import time as real_time
import types
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from glowstage import audio, genre, show  # noqa: E402

RECORDINGS = ROOT / "testdata" / "recordings"
R = audio.RATE
FPS = 50
KINDS = {"break": ("break", "calm"), "build": ("build",), "drop": ("drop",)}


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getframerate() != R or w.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: expected 48 kHz 16-bit")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32767
        if w.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    return data


# ---- the live show, replayed ------------------------------------------------------------------

def replay(data: np.ndarray, ai: bool = True, style: str | None = None) -> dict:
    """Plays the song through the analyzer, the genre AI and the director. Returns what the show
    did: section changes, a row every 0.25 s, and the styles it chose."""
    clock = types.SimpleNamespace(t=0.0)
    fake = types.SimpleNamespace(monotonic=lambda: clock.t, sleep=lambda s: None, perf_counter=real_time.perf_counter)
    audio.time = genre.time = fake
    an = audio.AudioAnalyzer()
    an._thread = object()            # no capture thread: we feed the blocks
    if style:
        ear = types.SimpleNamespace(request=lambda: {"style": style, "top": []}, error="")
        g = None
    elif ai:
        g = genre.GenreDetector(an)
        g._model = g._load()
        ear = types.SimpleNamespace(request=lambda: dict(g._snap), error="")
    else:
        g, ear = None, types.SimpleNamespace(request=lambda: {}, error="")
    d = show.ShowDirector(ear)
    trace, rows, styles = [], [], []
    last_sec, next_frame, next_look, next_row = None, 0.0, genre.INTERVAL, 0.0
    for i in range(0, len(data) - audio.BLOCK, audio.BLOCK):
        clock.t = (i + audio.BLOCK) / R          # the block has fully arrived
        an._analyze(data[i:i + audio.BLOCK, None])
        if g is not None and clock.t >= next_look:
            next_look += genre.INTERVAL
            g.look(an.recent(genre.WINDOW))
        while clock.t >= next_frame:
            next_frame += 1 / FPS
            flash_before = d.frame.flash
            f = d.update(an.request(), clock.t, 0.6, True)
            if d.section != last_sec:
                trace.append((clock.t, d.section))
                last_sec = d.section
            if f.flash > 0.85 and flash_before < 0.6:
                trace.append((clock.t, "blinder"))
            if not styles or styles[-1][1] != d.style_id:
                styles.append((clock.t, d.style_id))
            if clock.t >= next_row:
                next_row += 0.25
                rows.append((clock.t, d.section, f.tension, f.build, d._score, d._climb_c, d._sweep_c, d._thin_c,
                             d._bass_env / max(d._bass_peak, 1e-6), d.style_id, d._climb_since is not None))
    audio.time = genre.time = real_time
    return {"trace": trace, "rows": rows, "styles": styles, "bpm": d.bpm}


# ---- the truth, in hindsight ----------------------------------------------------------------

def features(data: np.ndarray, hop_s: float = 0.1) -> dict:
    """The whole song in 0.1 s steps: bass, highs, brightness, hits, and a spectrogram."""
    hop, size = int(hop_s * R), 4096
    win = np.hanning(size).astype(np.float32)
    freqs = np.fft.rfftfreq(size, 1 / R)
    edges = np.geomspace(30, 16000, 161)
    # (the lowest bands are narrower than one FFT bin: they take the nearest bin)
    bins = [np.where((freqs >= a) & (freqs < b))[0] if ((freqs >= a) & (freqs < b)).any()
            else np.array([int(np.argmin(np.abs(freqs - (a + b) / 2)))]) for a, b in zip(edges[:-1], edges[1:])]
    bass_b, high_b = (freqs > 35) & (freqs < 120), freqs > 4000
    cent_b = (freqs >= 100) & (freqs <= 16000)
    oct_ = np.log2(np.maximum(freqs[cent_b], 1) / 100)
    spec_rows, bass, high, cent, flux = [], [], [], [], []
    prev = None
    for i in range(0, max(1, len(data) - size), hop):
        s = np.abs(np.fft.rfft(data[i:i + size] * win))
        spec_rows.append([s[b].mean() if len(b) else 0.0 for b in bins])
        bass.append(s[bass_b].mean())
        high.append(s[high_b].mean())
        c = s[cent_b]
        cent.append(float((c * oct_).sum() / max(c.sum(), 1e-9)))
        mid = np.log1p(s[(freqs > 800) & (freqs < 5000)] * 100)
        flux.append(0.0 if prev is None else float(np.maximum(0, mid - prev).mean()))
        prev = mid
    return {"hop": hop_s, "spec": np.array(spec_rows, dtype=np.float32), "bass": np.array(bass),
            "high": np.array(high), "cent": np.array(cent), "flux": np.array(flux)}


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    return np.convolve(x, np.ones(n) / n, "same") if len(x) > n else x


def lift(feat: dict) -> np.ndarray:
    """Highs, brightness and hits together, each relative to the whole song (z-scores), smoothed
    over 2 s: a build-up climbs on it."""
    z = lambda v: (v - v.mean()) / max(v.std(), 1e-9)   # noqa: E731
    return _smooth(z(np.log(feat["high"] + 1e-6)) + z(feat["cent"]) + z(_smooth(feat["flux"], 5)), 20) / 3


def suggest(feat: dict, bpm: float) -> list[dict]:
    """Marks worked out in hindsight: drops where the bass comes back hard after at least 4 s
    without much of it, breaks where it leaves for at least 4 s, and build-ups from where the lift
    starts climbing before a drop. Rounded to the beat; to be checked against the picture."""
    hop = feat["hop"]
    bass = _smooth(feat["bass"], 3)
    level = np.percentile(bass, 90)
    low = bass < 0.3 * level
    marks: list[dict] = []
    k, n = int(8 / hop), len(bass)
    while k < n - 5:
        if low[k] and not low[k - int(4 / hop):k].any() and low[k:k + int(4 / hop)].all():
            marks.append({"t": round(k * hop, 1), "kind": "break"})
            k += int(4 / hop)
        elif (not low[k] and low[k - int(4 / hop):k - 2].mean() > 0.85 and bass[k:k + 5].mean() > 0.6 * level):
            marks.append({"t": round(k * hop, 1), "kind": "drop"})
            k += int(8 / hop)
        else:
            k += 1
    up = lift(feat)
    for m in [m for m in marks if m["kind"] == "drop"]:
        d = int(m["t"] / hop)
        brk = max([int(b["t"] / hop) for b in marks if b["kind"] == "break" and b["t"] < m["t"]] or [0])
        a = max(brk, d - int(40 / hop))
        if d - a < int(4 / hop):
            continue
        start = a + int(np.argmin(up[a:d - int(2 / hop)]))
        if up[d - int(1 / hop)] - up[start] > 0.8:        # it really climbed
            marks.append({"t": round(start * hop, 1), "kind": "build"})
    beat = 60 / bpm if bpm else 0
    for m in marks:
        if beat:
            m["t"] = round(round(m["t"] / beat) * beat, 2)
    return sorted(marks, key=lambda m: m["t"])


# ---- the picture -------------------------------------------------------------------------------

SECTION_COLORS = {"groove": (90, 90, 100), "break": (40, 90, 200), "calm": (30, 140, 150), "build": (240, 150, 20),
                  "gap": (10, 10, 10), "drop": (220, 30, 60), "silent": (30, 30, 34)}
MARK_COLORS = {"break": (90, 160, 255), "build": (255, 190, 60), "drop": (255, 70, 90)}


def plot(path: Path, feat: dict, run: dict, marks: list[dict], t0: float = 0.0, t1: float | None = None) -> Path:
    """Draws <song>.png (or <song> 1m10-1m40.png for a stretch, drawn wider to see the beats)."""
    from PIL import Image, ImageDraw

    hop = feat["hop"]
    seconds = len(feat["bass"]) * hop
    t1 = min(seconds, t1 if t1 is not None else seconds)
    span = t1 - t0
    pps = 8 if span > 120 else max(8, int(1600 / span))     # pixels per second
    i0, i1 = int(t0 / hop), int(t1 / hop)
    x0, top = 50, 24
    W = int(span * pps) + x0 + 10
    rows = {"spec": 200, "bass": 60, "lift": 60, "show": 26, "lean": 40}
    H = top + sum(rows.values()) + 6 * len(rows) + 20
    img = Image.new("RGB", (W, H), (16, 16, 20))
    dr = ImageDraw.Draw(img)
    X = lambda t: int(x0 + (t - t0) * pps)   # noqa: E731
    y = top
    # spectrogram (dB, low at the bottom)
    spec = 20 * np.log10(feat["spec"].T + 1e-7)
    lo, hi = np.percentile(spec, 5), np.percentile(spec, 99.5)
    v = np.clip((spec[:, i0:i1] - lo) / (hi - lo), 0, 1)[::-1]
    rgb = np.stack([v ** 0.8 * 255, v ** 1.6 * 220, (1 - v) * v * 4 * 180], axis=-1).astype(np.uint8)
    img.paste(Image.fromarray(rgb).resize((int(span * pps), rows["spec"])), (x0, y))
    dr.text((4, y + 2), "spec", fill=(200, 200, 200))
    y += rows["spec"] + 6

    def curve(times, vals, h, color, label):
        nonlocal y
        vals = np.asarray(vals, dtype=float)
        a, b = np.percentile(vals, 1), np.percentile(vals, 99.5)
        b = max(b, a + 1e-9)
        pts = [(X(t), y + h - 1 - int((min(max(v, a), b) - a) / (b - a) * (h - 2)))
               for t, v in zip(times, vals) if t0 <= t <= t1]
        dr.rectangle([x0, y, X(t1), y + h], fill=(24, 24, 30))
        if len(pts) > 1:
            dr.line(pts, fill=color, width=1)
        dr.text((4, y + 2), label, fill=(200, 200, 200))
        y += h + 6

    times = np.arange(len(feat["bass"])) * hop
    curve(times, _smooth(feat["bass"], 3), rows["bass"], (255, 90, 110), "bass")
    curve(times, lift(feat), rows["lift"], (255, 200, 80), "lift")
    # what the show did
    trace = [(t, s) for t, s in run["trace"] if s != "blinder"] + [(seconds, None)]
    for (t, s), (t2, _) in zip(trace, trace[1:]):
        if t2 >= t0 and t <= t1:
            dr.rectangle([X(max(t, t0)), y, X(min(t2, t1)), y + rows["show"]], fill=SECTION_COLORS.get(s, (60, 60, 60)))
    for t, s in run["trace"]:
        if s == "blinder" and t0 <= t <= t1:
            dr.line([X(t), y, X(t), y + rows["show"]], fill=(255, 255, 255), width=2)
    dr.text((4, y + 6), "show", fill=(200, 200, 200))
    y += rows["show"] + 6
    curve([r[0] for r in run["rows"]] + [-1, -1], [r[2] for r in run["rows"]] + [0, 1], rows["lean"], (240, 150, 20), "lean")
    # marks over everything, and a time axis
    for m in marks:
        if t0 <= m["t"] <= t1:
            c = MARK_COLORS.get(m["kind"], (255, 255, 255))
            dr.line([X(m["t"]), top, X(m["t"]), y], fill=c, width=1)
            dr.text((X(m["t"]) + 3, 4), m["kind"], fill=c)
    step = 10 if span > 60 else 2 if span <= 30 else 5
    for s in range(int(np.ceil(t0 / step)) * step, int(t1) + 1, step):
        dr.line([X(s), y, X(s), y + 5], fill=(150, 150, 150))
        dr.text((X(s) - 8, y + 6), f"{s // 60}:{s % 60:02d}", fill=(150, 150, 150))
    name = path.stem + ("" if (t0, t1) == (0.0, seconds) else f" {int(t0 // 60)}m{int(t0 % 60):02d}-{int(t1 // 60)}m{int(t1 % 60):02d}")
    out = path.with_name(name + ".png")
    img.save(out)
    return out


# ---- comparing ---------------------------------------------------------------------------------

def compare(run: dict, marks: list[dict], seconds: float) -> dict:
    """How late the show caught each mark, when it leaned into each build-up, and what it did
    that wasn't there (a build-up or drop without one in the song)."""
    trace = run["trace"]
    out = []
    ms = sorted(marks, key=lambda m: m["t"])
    for k, m in enumerate(ms):
        until = ms[k + 1]["t"] if k + 1 < len(ms) else seconds
        hit = next((t for t, s in trace if m["t"] - 1.0 <= t <= max(until, m["t"] + 4) and s in KINDS[m["kind"]]), None)
        row = {"kind": m["kind"], "t": m["t"], "late": None if hit is None else round(hit - m["t"], 2)}
        if m["kind"] == "build":
            lean = next((t for t, sec, ten, *_ in run["rows"] if m["t"] <= t <= until and (ten >= 0.3 or sec == "build")), None)
            row["lean"] = None if lean is None else round(lean - m["t"], 2)
        out.append(row)
    spans = [(m["t"] - 2, next((n["t"] for n in ms if n["kind"] == "drop" and n["t"] > m["t"]), m["t"] + 40) + 1)
             for m in ms if m["kind"] == "build"]
    false_builds = [round(t, 1) for t, s in trace if s == "build" and not any(a <= t <= b for a, b in spans)]
    false_drops = [round(t, 1) for t, s in trace if s == "drop"
                   and not any(m["kind"] == "drop" and abs(t - m["t"]) <= 3 for m in ms)]
    return {"marks": out, "false_builds": false_builds, "false_drops": false_drops}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # song titles in any script
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("songs", nargs="*", type=Path)
    ap.add_argument("--plot", action="store_true", help="draw <song>.png")
    ap.add_argument("--zoom", help="with --plot: only this stretch, e.g. 60-100 (seconds)")
    ap.add_argument("--no-ai", action="store_true", help="style from the tempo instead of the genre AI")
    ap.add_argument("--style", help="force a style (edm, techno, house, bass, pop, ...)")
    args = ap.parse_args()
    if args.songs:
        songs = args.songs
    else:   # every finished recording (the one being recorded isn't in the index yet)
        index = RECORDINGS / "index.json"
        done = {e["file"] for e in json.loads(index.read_text(encoding="utf-8"))} if index.is_file() else set()
        songs = [p for p in sorted(RECORDINGS.glob("*.wav")) if p.name in done]
    totals = {"late": [], "lean": [], "missed": 0, "false": 0}
    for path in songs:
        data = load(path)
        seconds = len(data) / R
        run = replay(data, ai=not args.no_ai, style=args.style)
        marks_path = path.with_suffix(".marks.json")
        feat = None
        if marks_path.is_file():
            info = json.loads(marks_path.read_text(encoding="utf-8"))
        else:
            feat = features(data)
            info = {"checked": False, "marks": suggest(feat, run["bpm"])}
            marks_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        if args.plot:
            feat = feat or features(data)
            t0, t1 = (float(v) for v in args.zoom.split("-")) if args.zoom else (0.0, None)
            print("  picture:", plot(path, feat, run, info["marks"], t0, t1).name)
        res = compare(run, info["marks"], seconds)
        style = " > ".join(dict.fromkeys(s for _, s in run["styles"]))
        flag = "" if info.get("checked") else "  (marks not checked yet)"
        blinders = sum(1 for _, s in run["trace"] if s == "blinder")
        print(f"{path.stem[16:] or path.stem}: {run['bpm']:.0f} BPM, style {style}, "
              f"{blinders} white blinders in {int(seconds // 60)}:{int(seconds % 60):02d}{flag}")
        totals["blinders"] = totals.get("blinders", 0) + blinders
        for r in res["marks"]:
            late = "MISSED" if r["late"] is None else f"{r['late']:+.2f} s"
            lean = f", leans in {r['lean']:+.2f} s" if r.get("lean") is not None else ""
            print(f"   {r['kind']:5s} at {int(r['t'] // 60)}:{r['t'] % 60:05.2f}  caught {late}{lean}")
            if r["late"] is None:
                totals["missed"] += 1
            else:
                totals["late"].append(r["late"])
            if r.get("lean") is not None:
                totals["lean"].append(r["lean"])
        if res["false_builds"] or res["false_drops"]:
            print(f"   not in the song: build-up at {res['false_builds']} s, drop at {res['false_drops']} s")
            totals["false"] += len(res["false_builds"]) + len(res["false_drops"])
    if len(songs) > 1 and totals["late"]:
        print(f"\nall songs: median {np.median(totals['late']):+.2f} s late, {totals['missed']} missed, "
              f"{totals['false']} not in the song, {totals.get('blinders', 0)} blinders"
              + (f", leaning into build-ups after a median {np.median(totals['lean']):+.2f} s" if totals["lean"] else ""))


if __name__ == "__main__":
    main()
