"""The light show: a virtual lighting operator for the whole room.

Every frame the director reads the music analysis (audio.py) and the AI's genre (genre.py)
and decides what all lights do together, the way a festival lighting jockey would:

- A beat clock, locked to the kick drum, counts beats, bars and 8-bar phrases. The lights run
  LEAD seconds ahead of the analysis, so flashes land on the beat instead of just after it.
  Sync shifts that per sound output: Bluetooth plays the sound ~0.2 s late, so the frames wait.
- Sections: groove, break (the bass drops out), build-up (energy rising without bass), gap
  (the silence right before a drop), drop (the bass slams back in, or a chorus jumps up in
  energy), calm and silent. A section only changes once the music has stayed that way for a
  beat or a few, so the show doesn't flicker between them. While a riser keeps climbing the
  lights already lean into the build-up before it is certain.
- Looks ("scenes") change on phrase boundaries and section changes, with a white hit on the
  first beat of a phrase. Each style has its own color pairs (a main color and an accent) and
  hardness: techno gets red and white with strobes, chill music slow washes and no flashes.
  Or the person's own colors: 2 to 5 of them take the place of every color pair (set_colors).

The lights in front of you form one stage from left to right (stage_spans): chases run from
the left of the monitor over the keyboard to the PC. Lights behind you (the Nanoleaf on the
back wall) answer the front: the front hits on the kick, the back on the snare.
"""

from __future__ import annotations

import cmath
import collections
import colorsys
import copy
import logging
import math
import random
import re
from dataclasses import dataclass, field

import numpy as np

from . import harmony
from .colors import hex_to_rgb

log = logging.getLogger(__name__)

LEAD = 0.08            # seconds the lights run ahead of the analysis: capture + network delay
BUILD_ON, BUILD_OFF = 0.55, 0.25  # build-up score to start one / below which it fizzled out
                                  # (tuned on real tracks: real build-ups 0.6-0.8, busy drops 0.45)
WHITE = np.ones(3, dtype=np.float32)


def _c(h: str) -> np.ndarray:
    return np.array(hex_to_rgb(h), dtype=np.float32) / 255


# ---- the person's own colors ---------------------------------------------------------------------

MIN_SHOW_COLORS, MAX_SHOW_COLORS = 2, 5
DARKEST_PEAK = 0.65     # the brightest channel of a color is lifted to at least this (as in palette.py)
WHITE_BELOW = 0.12      # saturation under this is a grey or a white, not a color (as in palette.py)
WARM_WHITE = (255, 214, 170)   # what a grey becomes


def clean_show_colors(colors) -> list[str]:
    """The colors a person picked for the show, checked: none (follow the music) or 2 to 5 different
    #rrggbb colors, lower case. Anything else is refused."""
    if colors is None or (isinstance(colors, (list, tuple)) and not colors):
        return []
    if not isinstance(colors, (list, tuple)):
        raise ValueError("Pick colors from the list")
    out: list[str] = []
    for c in colors:
        c = str(c).strip().lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", c):
            raise ValueError("Use hex colors like #ff8800")
        if c not in out:
            out.append(c)
    if len(out) < MIN_SHOW_COLORS:
        raise ValueError(f"Pick at least {MIN_SHOW_COLORS} different colors")
    if len(out) > MAX_SHOW_COLORS:
        raise ValueError(f"Pick up to {MAX_SHOW_COLORS} colors")
    return out


def led_color(h: str) -> np.ndarray:
    """A picked color made for LEDs, keeping what the person chose as far as it can: a grey
    can't be shown (a grey LED is a dim white), so it becomes a soft warm white, and a dark
    color is brightened until its brightest channel is at a good level. The hue stays."""
    r, g, b = (float(x) for x in _c(h))
    _, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < WHITE_BELOW:
        if v >= 0.85:
            return WHITE.copy()
        return np.array(WARM_WHITE, dtype=np.float32) / 255 * max(v, 0.6)
    rgb = np.array([r, g, b], dtype=np.float32)
    return rgb * (DARKEST_PEAK / v) if v < DARKEST_PEAK else rgb


def _luma(c: np.ndarray) -> float:
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


@dataclass(frozen=True)
class Style:
    """How a genre is lit: its colors and its choreography, like an LJ's show file per genre."""
    name: str
    pairs: tuple[tuple[str, str], ...]   # (main, accent) color pairs, one per phrase
    hard: float                          # 0 = only soft washes .. 1 = strobes and fast chases
    looks: dict[str, tuple[str, ...]]    # section -> the looks that suit it, the first opens a drop
    drops: bool = True                   # has EDM-style build-ups, blackouts and drops
    pump: str = "clock"                  # what the main hits follow: the beat "clock", the "kick", or "none"
    half: tuple[float, float] | None = None  # tempo range (BPM) usually felt at half time

    def pair(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        main, accent = self.pairs[i % len(self.pairs)]
        return _c(main), _c(accent)

    @property
    def colors(self) -> list[np.ndarray]:
        return [_c(m) for m, _ in self.pairs]


STYLES = {  # no white accents: white is kept for the build-up strobe and the drop's blinder
    # Hard and minimal: short mono pulses on every kick, stepping chases, flashes jumping around.
    "techno": Style("Techno", (("#ff0000", "#ff5a00"), ("#ff1a00", "#7a00ff"), ("#2a00ff", "#ff0030"),
                               ("#ff0030", "#ff8a00")), 1.0, {
        "groove": ("pump", "chase16", "alternate", "strobe_chase"),
        "drop": ("strobe_chase", "chase16", "random_zones", "alternate"),
        "break": ("glow", "drift"), "calm": ("glow",)}),
    # Half-time wobble (dubstep) or fast and busy (drum and bass, 160+ BPM, felt straight).
    "bass": Style("Dubstep / DnB", (("#8a00ff", "#00ff4c"), ("#00ff4c", "#00b3ff"), ("#ff0040", "#8a00ff"),
                                    ("#00b3ff", "#ff00c8")), 1.0, {
        "groove": ("wobble", "random_zones", "pump"),
        "drop": ("wobble", "strobe_chase", "random_zones", "split"),
        "break": ("glow", "drift"), "calm": ("glow",)}, half=(125, 155)),
    # Big and sweeping: moving-head scanners, waves, rings; long build-ups into big drops.
    "edm": Style("EDM", (("#00e5ff", "#ff00c8"), ("#2a4bff", "#00e5ff"), ("#ff00c8", "#ffd000"),
                         ("#00ffb0", "#2a4bff")), 0.8, {
        "groove": ("scanner", "wave", "mirror", "pump"),
        "drop": ("chase", "mirror", "random_zones", "scanner"),
        "break": ("drift", "breathe", "sparkle"), "calm": ("breathe", "drift")}),
    # Live and punchy: pulses on kick and snare, colors sweeping by; the chorus gets the movement.
    "rock": Style("Rock", (("#ff1a00", "#ffb000"), ("#ff8a00", "#ff1a00"), ("#ffcc00", "#ff4a00"),
                           ("#c00000", "#ff8a00")), 0.6, {
        "groove": ("pulse", "sweep", "split"),
        "drop": ("pulse", "chase", "split", "mirror"),
        "break": ("vocal", "breathe"), "calm": ("breathe", "glow")}, drops=False, pump="kick"),
    # Bouncy: a disco floor stepping on every beat, lights on the offbeat, glitter like a mirror ball.
    "house": Style("House / Disco", (("#ff7a00", "#ff006a"), ("#a000ff", "#ffd000"), ("#ff006a", "#00d9ff"),
                                     ("#ffd000", "#ff3c00")), 0.5, {
        "groove": ("disco", "offbeat", "sweep", "pump"),
        "drop": ("disco", "offbeat", "mirror", "split"),
        "break": ("sparkle", "breathe"), "calm": ("sparkle", "breathe")}),
    # Playful: bouncing color blocks, mirrored rings; the chorus gets the most.
    "pop": Style("Pop", (("#ff3ca0", "#00d9ff"), ("#9a4bff", "#ffe000"), ("#00d9ff", "#ff3ca0"),
                         ("#ff6a00", "#9a4bff")), 0.4, {
        "groove": ("bounce", "sweep", "split", "vocal"),
        "drop": ("bounce", "mirror", "chase", "split"),
        "break": ("vocal", "breathe", "sparkle"), "calm": ("breathe", "vocal")}),
    # Laid back, usually felt at half time: everything glows with the 808 and fades with it, the
    # rap flow lights the middle, the snare swaps sides, the trap hi-hats tick on the back wall.
    "hiphop": Style("Hip-hop", (("#ffb000", "#7a00ff"), ("#ff2a2a", "#ffb000"), ("#7a00ff", "#ff007a"),
                                ("#ff007a", "#ffb000")), 0.45, {
        "groove": ("bass_glow", "vocal", "pulse", "split"),
        "drop": ("pulse", "bass_glow", "split"),
        "break": ("vocal", "glow"), "calm": ("vocal", "glow")}, drops=False, pump="kick", half=(115, 160)),
    # Swaying: warm light rocking left and right, accents on the offbeat (the reggae skank).
    "latin": Style("Latin / Afro / Reggae", (("#00d43a", "#ffd000"), ("#ff5a00", "#00d43a"), ("#ffd000", "#ff0040"),
                                             ("#ff0040", "#00b3ff")), 0.3, {
        "groove": ("sway", "offbeat", "bounce"),
        "drop": ("sway", "offbeat", "split"),
        "break": ("breathe", "vocal"), "calm": ("breathe",)}, drops=False),
    # Flowing: colors drifting with the melody, no hits on the beat.
    "chill": Style("Chill", (("#1428ff", "#6a0dc8"), ("#00b3a8", "#1428ff"), ("#6a0dc8", "#ff7a3a"),
                             ("#0038ff", "#00c2c7")), 0.0, {
        "groove": ("flow", "breathe", "drift"),
        "drop": ("flow", "sweep"),
        "break": ("flow", "glow"), "calm": ("flow", "glow")}, drops=False, pump="none"),
}
MOOD_HARDNESS = {"Sad": -0.3, "Tender": -0.3, "Happy": 0.0, "Exciting": 0.15, "Angry": 0.2, "Scary": 0.1}


@dataclass
class Frame:
    """What every light needs to draw one frame of the show."""
    t: float = 0.0
    section: str = "silent"
    scene: str = "idle"
    hard: float = 0.5
    bpm: float = 120.0
    beat: float = 0.0          # beats since the current phrase started (already LEAD ahead)
    seed: int = 0              # changes every phrase: which zones the random looks pick
    main: np.ndarray = field(default_factory=lambda: _c("#00e5ff"))
    accent: np.ndarray = field(default_factory=lambda: _c("#ff00c8"))
    colors: list[np.ndarray] = field(default_factory=lambda: STYLES["edm"].colors)
    kick: float = 0.0          # 1 on a kick drum, fading out within ~0.1 s
    pump: float = 0.0          # 1 on every beat (the clock, or the kick when not locked)
    offbeat: float = 0.0       # 1 halfway between the beats (the "and")
    hit: float = 0.0           # 1 on a snare or clap
    hits: int = 0              # how many snares so far (to alternate sides)
    hat: float = 0.0           # 1 on a hi-hat
    level: float = 0.0         # smoothed loudness 0..1
    bass: float = 0.0          # the bass line right now 0..1 (808s, wobbles)
    mid: float = 0.0           # vocals and melody right now 0..1
    treble: float = 0.0
    half: bool = False         # felt at half time: beat and bpm above are the half-time ones
    build: float = 0.0         # build-up progress 0..1 (1 = the drop is due now)
    tension: float = 0.0       # before a build-up is certain: how much the lights already lean into it
    blackout: bool = False     # the held breath on the last half beat before a drop
    flash: float = 0.0         # blinder, fading
    flash_color: np.ndarray = field(default_factory=lambda: WHITE)
    white: np.ndarray = field(default_factory=lambda: WHITE)   # the "white" of the riser and the glitter: the lightest of your colors with those
    strobe: bool = False       # this frame is a white strobe flash (build-ups only)
    master: float = 1.0        # the master fader: verses dimmer, choruses and drops full
    prev_scene: str = "idle"   # the look being wiped away ...
    trans: float = 1.0         # ... and how far the new one has wiped in (1 = done)
    wipe: int = 0              # how: 0 left to right, 1 right to left, 2 from the middle out

    @property
    def beat_i(self) -> int:
        return int(math.floor(self.beat))

    @property
    def bar(self) -> int:
        return self.beat_i // 4

    @property
    def beat_s(self) -> float:
        return 60.0 / self.bpm


def _ema(prev: float, x: float, dt: float, tau: float) -> float:
    return prev + (x - prev) * (1 - math.exp(-dt / tau))


def _climbing(vals: list[float], full: float, step: float) -> float:
    """How clearly a series (one value per second) climbs steadily, 0..1: `full` = the rise over
    the whole series that counts as all the way, `step` = the least each second must add. 0 when
    it fell anywhere or when one or two seconds did most of the rising (a step, not a climb)."""
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    total = vals[-1] - vals[0]
    if total <= 0 or max(diffs) > 0.45 * total:
        return 0.0
    return max(0.0, min(1.0, total / full, min(diffs) / step))


def _hash(i: int, seed: int) -> float:
    h = (i * 374761393 + seed * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h ^ (h >> 16)) / 0xFFFFFFFF


class ShowDirector:
    def __init__(self, genre):
        self.genre = genre
        self.frame = Frame()
        self._rng = random.Random()
        self._t: float | None = None
        self.bpm = 120.0
        self._beat = 0.0
        self._lock_err = 0.5           # how far the music's beats land from the clock's (0 = spot on)
        self._tempo_cand: tuple[float, float] | None = None
        self._grid_shift = 0.0         # 0.5 when the grid turned out to sit on the offbeats
        self._kick_vec = 0j            # where the kicks land on the clock (circular average)
        self._kick_n = 0
        self._grid_trust_at = 0.0
        self._phrase_start = 0.0
        self._phrase_idx = 0
        self._last_kick = -1.0
        self.section = "silent"
        self._since = 0.0
        self._entered_beat = 0.0
        self._cand: str | None = None
        self._cand_since = 0.0
        self._gap_since: float | None = None
        self._sound_since = 0.0
        self.style_id = "edm"
        self._pending_style: str | None = None
        self._guessed_for: float | None = None   # the song (its start) the style was guessed from the tempo for
        self._pair_i = 0
        self._prev_pair = STYLES["edm"].pair(0)
        self._pair_changed = -99.0
        # The person's own colors (None: the colors follow the music) and the lightest of them.
        self._mine: list[np.ndarray] | None = None
        self._mine_raw: tuple = ()
        self._lightest = WHITE
        self._strobe = True        # the strobe setting of the last frame (a blinder is white only then)
        self._ai: dict = {}
        self._l_fast = self._l_slow = self._l_mid = self._l_mid_peak = 1e-6
        self._bass_env = self._bass_peak = self._bass_max = 1e-6
        self._bass_seen: collections.deque[float] = collections.deque(maxlen=180)   # every 0.25 s, 45 s
        self._hush_since: float | None = None
        self._hush_from = self._gap_from = "silent"   # the section a silence came out of
        self._quiet_frac = 0.0
        self._gone_since: float | None = None   # since when the bass has been away
        self._was_gone, self._back_at = 0.0, -99.0   # how many beats it was away, and when it came back
        self._blinder_beat = -99.0     # the last white blinder
        self._h_fast = self._h_slow = 1e-6
        self._level = 0.0
        # Every 0.25 s for the last 8 s: (loudness, highs, centre of weight, bass share).
        self._hist: collections.deque[tuple[float, float, float, float]] = collections.deque(maxlen=32)
        self._hist_t = 0.0
        self._cent = 0.0
        self._bass_fast = self._bass_avg = 1e-6
        self._score = 0.0              # how much it sounds like a build-up right now, 0..1
        self._sweep_c = self._thin_c = 0.0   # two of its parts: the sound swept up, the bass thinned
        self._rise_c = 0.0             # ... and the riser part: swept up and still rising
        self._roll_c = 0.0             # ... and the snare roll part
        self._rolls: collections.deque[float] = collections.deque(maxlen=16)   # it, every 0.25 s for 4 s
        self._roll_t = 0.0
        self._rolling = False          # the roll is tightening (or at full speed)
        self._climb_c = 0.0            # ... and the sound still climbing right now (0 for a step up)
        self._climb_since: float | None = None   # since when it has kept climbing clearly
        self._tension = 0.0            # how much the lights already lean into a possible build-up
        self._dip_at = -99.0           # the last moment the music cut out for an instant
        self._hits: collections.deque[float] = collections.deque(maxlen=24)   # snare/clap times
        self._prev_hit_ago = 99.0
        self._last_snare = -99.0
        self._last_snare_beat = 0.0
        self._snare_gaps: collections.deque[int] = collections.deque(maxlen=12)   # beats between snares
        # The song's cue list, learned while it plays: per kind of part (groove, drop, break, calm)
        # the looks and colors used, phrase by phrase, so a part that comes back looks the same.
        self._cues: dict[str, list[tuple[str, int]]] = {}
        self._cue_i = 0
        self._trans_beat = -99.0
        self._master = 1.0
        # Harmony: colors follow the chords once the song has a clear key.
        self._chord_seen: tuple[int, bool] | None = None
        self._key_seen: int | None = None
        self._key_since = 0.0
        self._harmony_until = 0.0
        self._chord_text = ""
        # Drop anticipation: where the build-up started (on a bar line) and when the drop is due.
        self._build_origin = 0.0
        self._drop_at = 0.0
        self._unconfirmed_until: float | None = None
        self._impact: tuple[float, str] | None = None   # a drop on a bass hit, checked until beat ..., from section ...
        self._impact_n = self._impact_bass = self._impact_kicks = self._kicks = 0
        self._fizzle_since: float | None = None
        self._build_cent = 0.0         # where the sound's weight sat when the build-up began
        # The "one" of the bar: votes per beat position (0-3) and the even/odd split from snares.
        self._bar_votes = [0.0, 0.0, 0.0, 0.0]
        self._parity = [0.0, 0.0]
        self._bar_phase: int | None = None
        # Sync: with Bluetooth the sound reaches your ears ~0.2 s after the PC hears it, so the
        # frames wait that long (_out); lights that lag behind the sound get more lead instead.
        self._lead = LEAD
        self._out: collections.deque[tuple[float, Frame]] = collections.deque(maxlen=64)

    # ---- what the UI shows ---------------------------------------------------------------

    def info(self) -> dict:
        ai = self._ai
        return {
            "style": STYLES[self.style_id].name,
            "ai": bool(ai.get("style")),
            "aiError": self.genre.error,
            "aiModel": getattr(self.genre, "model_name", ""),
            "confidence": round(ai.get("confidence", 0.0), 2),
            "genres": [name for name, _ in ai.get("top", [])],
            "mood": ai.get("mood"),
            "section": self.section,
            "scene": self.frame.scene,
            "bpm": round(self.bpm) if self.section != "silent" else 0,
            "half": self.frame.half,
            "chord": self._chord_text if self._t is not None and self._t < self._harmony_until else "",
            "mine": self._mine is not None,
        }

    def set_colors(self, hexes) -> None:
        """Use these colors (2 to 5 as #rrggbb) for everything the show lights up, or none to let
        the music pick them. Cheap to call every frame; a change glides in over a beat."""
        try:
            raw = tuple(hexes or ())
        except TypeError:
            raw = ()
        if raw == self._mine_raw:
            return
        self._mine_raw = raw
        try:
            colors = clean_show_colors(list(raw))
        except ValueError:
            colors = []              # (a damaged setting: follow the music)
        self._mine = [led_color(c) for c in colors] or None
        self._lightest = max(self._mine, key=_luma) if self._mine else WHITE
        if self.frame.flash < 0.01:    # (nothing is flashing: the next flash starts in the right color)
            self.frame.flash_color = self._lightest
        if self._t is not None:
            self._set_pair(self._pair_i)

    def _pair(self, style: Style, i: int) -> tuple[np.ndarray, np.ndarray]:
        """The (main, accent) pair of phrase i. Of your colors each phrase takes the next as its main
        color and, as its accent, another one that moves on once all have had a turn as main (with
        two colors they simply swap); otherwise the style's own pair."""
        mine = self._mine
        if not mine:
            return style.pair(i)
        n = len(mine)
        return mine[i % n], mine[(i % n + 1 + (i // n) % (n - 1)) % n]

    # ---- one frame -------------------------------------------------------------------------

    def update(self, a: dict, now: float, intensity: float = 0.5, strobe: bool = True,
               sync: float = 0.0) -> Frame:
        """The frame to show now. `sync` (seconds) > 0 holds every frame back that long, for sound
        that reaches your ears late (Bluetooth); < 0 runs the beat clock that much further ahead,
        for lights that react late."""
        self._lead = LEAD + max(0.0, -sync)
        f = self._update(a, now, intensity, strobe)
        if sync <= 0:
            self._out.clear()
            return f
        self._out.append((now, copy.copy(f)))
        while len(self._out) > 1 and self._out[1][0] <= now - sync + 0.01:   # (to the nearest frame)
            self._out.popleft()
        return self._out[0][1]

    def _update(self, a: dict, now: float, intensity: float, strobe: bool) -> Frame:
        dt = 0.02 if self._t is None else max(0.0, min(0.1, now - self._t))
        self._t = now
        self._strobe = strobe
        self._ai = self.genre.request()
        self._follow_style(a)
        self._clock(a, now, dt)
        self._features(a, now, dt)
        self._sections(a, now)
        self._phrases()

        f = self.frame
        style = STYLES[self.style_id]
        hard = style.hard + MOOD_HARDNESS.get(self._ai.get("mood") or "", 0.0) + (intensity - 0.5) * 0.8
        f.hard = max(0.0, min(1.0, hard))
        f.t, f.section = now, self.section
        lead = self._lead * self.bpm / 60
        silent = a.get("silent", True)
        hit_ago = a.get("hit_ago", 99.0)
        if hit_ago < self._prev_hit_ago - 0.02:
            self._hits.append(now - hit_ago)    # every hit counts for how tight a snare roll is
            # A snare or clap falls on the beat; a "hit" between beats is a hi-hat or bass note.
            at = self._beat - hit_ago * self.bpm / 60
            if min(at % 1, 1 - at % 1) < 0.15 or self.section == "build":
                self._last_snare = now - hit_ago
                f.hits += 1
                gap = round(at - self._last_snare_beat)
                if 1 <= gap <= 8:
                    self._snare_gaps.append(gap)
                self._last_snare_beat = at
                if self.section in ("groove", "drop"):
                    self._snare_downbeat(round(at), f.half)
        self._prev_hit_ago = hit_ago
        # Half time (trap, dubstep): the looks move at half the tempo, so they breathe with the
        # snare on every fourth beat instead of rushing along with every beat.
        f.half = self._half_time(style)
        div = 2 if f.half else 1
        f.bpm = self.bpm / div
        f.beat = (self._beat + lead - self._phrase_start) / div
        self._colors(f, style)
        f.kick = 0.0 if silent else math.exp(-a.get("since_beat", 99.0) / 0.1)
        clock = math.exp(-(f.beat % 1) * f.beat_s / 0.09)       # the clock's beat, LEAD ahead
        f.offbeat = 0.0 if silent else math.exp(-((f.beat + 0.5) % 1) * f.beat_s / 0.09)
        locked = a.get("bpm_conf", 0.0) > 0.3 and self._lock_err < 0.1
        if silent or style.pump == "none":
            f.pump = 0.0
        elif style.pump == "kick":
            f.pump = max(f.kick, 0.35 * clock)
        else:
            f.pump = clock if locked else max(f.kick, 0.5 * clock)
        f.hit = 0.0 if silent else math.exp(-max(0.0, now - self._last_snare - LEAD / 2) / 0.06)
        f.hat = 0.0 if silent else math.exp(-a.get("hat_ago", 99.0) / 0.04)
        f.treble = a.get("treble", 0.0)
        f.level = self._level
        # The bass line and the vocals, followed closely: an 808 swells and fades, a wobble wobbles.
        f.bass = 0.0 if silent else _ema(f.bass, a.get("bass", 0.0), dt, 0.05)
        f.mid = 0.0 if silent else _ema(f.mid, a.get("mid", 0.0), dt, 0.12)
        f.flash *= math.exp(-dt / 0.3)
        self._harmony(a, now, style)
        # The master fader, ridden like an LJ would: verses a bit down, choruses and drops full.
        loud = min(1.0, self._l_mid / max(self._l_mid_peak, 1e-6))
        target = {"drop": 1.0, "build": 1.0, "gap": 1.0, "groove": 0.55 + 0.45 * loud,
                  "break": 0.7 + 0.3 * loud, "calm": 0.75}.get(self.section, 1.0)
        # Up at once (a drop must slam in at full power), down slowly.
        self._master = target if target > self._master else _ema(self._master, target, dt, 1.0)
        f.master = self._master
        # Before a build-up is certain, the lights already lean into it while the sound keeps
        # climbing (and, in a break, as it sounds more and more like one), so they rise with the
        # riser instead of waking up when it is nearly over. If it doesn't build, they ease back.
        lean = 0.0
        if self.section in ("groove", "break", "calm") and f.hard >= 0.3 and style.drops:
            lean = self._climb_c if self.section == "groove" else max(self._climb_c, (self._score - 0.3) / 0.3)
            lean = max(0.0, min(1.0, (lean - 0.2) / 0.6))
        self._tension = _ema(self._tension, lean, dt, 0.4 if lean > self._tension else 1.2)
        f.tension = self._tension if self.section in ("groove", "break", "calm") else 0.0
        f.trans = min(1.0, max(0.0, (self._beat - self._trans_beat) / 1.0))   # a wipe takes one beat
        f.blackout, f.strobe = False, False
        if self.section in ("build", "gap"):
            self._countdown(f, now, lead, strobe)
        else:
            f.build = 0.0
        return f

    def _countdown(self, f: Frame, now: float, lead: float, strobe: bool) -> None:
        """Build-up: dance music works in blocks of 8 bars, so the drop is due on such a bar line.
        Count down to it: the riser is full exactly then, the strobe speeds up over the last two
        bars, the light holds its breath on the last half beat, and the drop hits on the one.
        A snare roll at full speed means the drop is at the next bar line."""
        cur = self._beat + lead
        per_beat = sum(1 for h in self._hits if now - h < 1.5) / (1.5 * self.bpm / 60)
        roll = min(1.0, max(0.0, math.log2(max(per_beat, 1.0)) / 2))
        while self._drop_at < cur - 0.25:
            self._drop_at += 16                  # it keeps building: four more bars
        if roll >= 0.95 and self._drop_at - cur > 16:
            # The snare roll is at full speed: the drop is at the next four-bar line.
            self._drop_at = self._build_origin + 16 * math.ceil((cur + 0.5 - self._build_origin) / 16)
        span = max(4.0, self._drop_at - self._build_origin)
        count = min(1.0, max(0.0, (cur - self._build_origin) / span))
        # The riser follows what you hear: how far the riser has swept up since the build-up
        # began (1.5 octaves = all the way), how tight the snare roll is, how built-up it sounds
        # overall; the count adds the rest. It never falls back during the build-up.
        sweep = (self._cent - self._build_cent) / 1.5
        sound = min(1.0, max(0.0, sweep, roll, (self._score - 0.25) / 0.5))
        f.build = max(f.build, 0.3 * count + 0.7 * sound)
        remaining = self._drop_at - cur
        rate = 1 if remaining > 8 else 2 if remaining > 4 else 4
        f.strobe = strobe and f.hard >= 0.3 and (cur * rate) % 1 < 0.3 and self.section == "build"
        # Once the music has gone quiet for the drop, the drop is fired exactly on the "one" of
        # the count instead of a moment after hearing it (if the bass then doesn't come within a
        # beat and a half, the show quietly goes back). Otherwise it fires on hearing the bass
        # slam back in, which takes a few hundredths of a second: a wrong count must never fire
        # a blinder in the middle of a build-up.
        sure = self.section == "gap"
        if (sure or (f.build >= 0.85 and sound >= 0.8)) and 0 < remaining <= 0.5 and f.hard >= 0.3:
            f.blackout, f.strobe = True, False            # the held breath on the last half beat
        if sure and remaining <= 0:
            self._enter("drop", now, on_count=True)

    # ---- where the "one" of the bar is --------------------------------------------------------

    def _snare_downbeat(self, beat: int, half: bool) -> None:
        """Snares and claps sit on 2 and 4, so the beat before one is a "one" or a "three"; at
        half time the snare is on the three, which gives the "one" outright."""
        if half:
            self._vote_bar(beat - 2, 1.0)
        else:
            self._parity = [v * 0.95 for v in self._parity]
            self._parity[(beat - 1) % 2] += 1.0

    def _chord_downbeat(self, beat: float) -> None:
        """Chords change on the "one". The change is heard ~0.3 s late and isn't beat-exact,
        but it is good enough to tell the "one" from the "three" the snares left open."""
        b = round(beat)
        if sum(self._parity) > 3:
            p = 0 if self._parity[0] >= self._parity[1] else 1
            options = (p, p + 2)
            dist = [min((b - o) % 4, (o - b) % 4) for o in options]
            self._vote_bar(options[0] if dist[0] <= dist[1] else options[1], 1.0)
        else:
            self._vote_bar(b, 0.5)

    def _vote_bar(self, beat: int, weight: float) -> None:
        self._bar_votes = [v * 0.9 for v in self._bar_votes]
        self._bar_votes[beat % 4] += weight
        best = max(range(4), key=lambda k: self._bar_votes[k])
        if self._bar_votes[best] < 2.5 or best == self._bar_phase:
            return
        self._bar_phase = best
        if self.section in ("groove", "break", "calm", "silent"):
            # Move the phrase grid onto the real bar lines (by the smallest shift).
            off = (best - self._phrase_start) % 4
            self._phrase_start += off if off <= 2 else off - 4
            self._phrase_idx = self._phrase_now()

    def _bar_line(self, beat: float, up: bool = False) -> float:
        """The nearest bar line at or before (or with up=True at or after) a beat."""
        base = self._bar_phase if self._bar_phase is not None else self._phrase_start
        n = (beat - base) / 4
        return base + 4 * (math.ceil(n) if up else math.floor(n))

    def _harmony(self, a: dict, now: float, style: Style) -> None:
        """When the chord changes, so do the colors: each chord of the key gets its own color pair
        (I, IV, V and vi each their own), so the light follows the song's progression."""
        chord, key = a.get("chord"), a.get("key")
        root = key[0] if key else None           # B and Bm share their home note: same colors
        if root != self._key_seen:               # the key has to settle before the colors follow it
            self._key_seen, self._key_since = root, now
        if chord is None or key is None or a.get("key_conf", 0.0) < 0.5 or now - self._key_since < 8:
            return
        if chord == self._chord_seen:
            return
        self._chord_seen = chord
        if self.section not in ("groove", "drop", "break", "calm"):
            return
        self._chord_downbeat(self._beat - a.get("chord_ago", 0.0) * self.bpm / 60)
        if self._mine:                           # (the chords still find the "one", but your colors stay)
            return
        degree = (chord[0] - key[0]) % 12
        pair = {0: 0, 5: 1, 7: 2, 9: 3, 2: 1, 4: 3, 11: 2, 10: 2, 8: 3, 3: 1, 1: 0, 6: 1}[degree]
        self._harmony_until = now + 8.0
        self._chord_text = f"{harmony.name(chord)} in {harmony.name(key)}"
        if pair % len(style.pairs) != self._pair_i % len(style.pairs):
            self._set_pair(pair)

    def _half_time(self, style: Style) -> bool:
        """Half time when the snares come every four beats; until there are enough snares to
        tell, when this genre at this tempo usually is (trap at 140, dubstep at 140)."""
        gaps = sorted(self._snare_gaps)
        if len(gaps) >= 6:
            typical = gaps[len(gaps) // 2]
            if typical in (3, 4, 5):
                return True
            if typical in (1, 2):
                return False
        return bool(style.half) and style.half[0] <= self.bpm <= style.half[1]

    def _colors(self, f: Frame, style: Style) -> None:
        """The phrase's color pair, crossfading from the last one over one beat."""
        main, accent = self._pair(style, self._pair_i)
        k = min(1.0, max(0.0, (self._beat - self._pair_changed) / 1.0))
        pm, pa = self._prev_pair
        f.main, f.accent = pm * (1 - k) + main * k, pa * (1 - k) + accent * k
        f.colors = self._mine or style.colors
        f.white = self._lightest

    def _set_pair(self, i: int) -> None:
        self._prev_pair = (self.frame.main.copy(), self.frame.accent.copy())
        self._pair_i = i
        self._pair_changed = self._beat

    def _follow_style(self, a: dict) -> None:
        style = self._ai.get("style")
        if not style:
            # No AI verdict (yet): guess from the tempo, once per song, after the tempo settled.
            # (Guessing on every reading flipped the style hundreds of times an evening: a tempo
            # estimate wavers between 105 and 130 and the guess between hip-hop and EDM.)
            if (self._guessed_for == self._sound_since or a.get("bpm_conf", 0.0) < 0.3
                    or self._t is None or self._t - self._sound_since < 6.0):
                return
            self._guessed_for = self._sound_since
            bpm = self.bpm
            style = "bass" if bpm >= 160 else "edm" if 118 <= bpm <= 135 else "hiphop" if 80 <= bpm < 110 else "pop"
        if style != self.style_id:
            self._pending_style = style   # switched at the next phrase, like an operator would

    def _clock(self, a: dict, now: float, dt: float) -> None:
        # In breaks and build-ups the clock runs on by itself: the tempo doesn't change there, and
        # snare rolls and missing kicks would only pull it off the beat just before the drop.
        free = self.section in ("break", "build", "gap", "calm")
        if not free:
            self._tempo(a, now, dt)
        self._beat += dt * self.bpm / 60
        if a.get("silent", True) or free:
            if free:
                beats = a.get("beats_ago") or []
                if beats:
                    self._last_kick = max(self._last_kick, now - beats[-1])
            return
        # Pull the clock's phase onto the beat grid (found over the last 8 s of rhythm), or onto
        # single kick drums while there is no clear grid, so beat 1.0 lands on the beat.
        beats = a.get("beats_ago") or []
        new_kick = bool(beats) and now - beats[-1] > self._last_kick + 0.05
        if new_kick:
            self._kick_on_grid(beats[-1])
        grid_ago, clarity = a.get("grid_ago"), a.get("grid_conf", 0.0)
        # Right after a break or build-up the grid still remembers it (a snare roll isn't a beat):
        # go by the kicks until it has heard a few seconds of the groove again.
        if grid_ago is not None and clarity > 0.15 and now > self._grid_trust_at:
            err = (grid_ago * self.bpm / 60 + self._grid_shift - self._beat) % 1
            err = err - 1 if err > 0.5 else err
            self._lock_err += (abs(err) - self._lock_err) * min(1.0, dt * 2)
            self._beat += err * min(1.0, dt * 4)
        elif new_kick:
            at_kick = self._beat - beats[-1] * self.bpm / 60
            err = round(at_kick) - at_kick
            self._lock_err += (abs(err) - self._lock_err) * 0.2
            if abs(err) < 0.3:
                self._beat += err * 0.35
        if new_kick:
            self._last_kick = now - beats[-1]

    def _kick_on_grid(self, kick_ago: float) -> None:
        """The rhythm grid can settle on the offbeats (where bass and hi-hats sit in dance music)
        instead of on the kick drum. When the kicks keep landing half a beat off the clock, move
        the grid over by half a beat."""
        phase = (self._beat - kick_ago * self.bpm / 60) % 1
        self._kick_vec = self._kick_vec * 0.85 + cmath.exp(2j * math.pi * phase) * 0.15
        self._kick_n += 1
        if self._kick_n >= 8 and abs(self._kick_vec) > 0.5 and math.cos(cmath.phase(self._kick_vec)) < -0.3:
            self._grid_shift = (self._grid_shift + 0.5) % 1
            self._kick_vec, self._kick_n = 0j, 0

    def _tempo(self, a: dict, now: float, dt: float) -> None:
        """Follow the tempo estimate, but not its slips: songs hardly ever change tempo, so a
        really different one only wins once it has held for eight seconds (or right after a
        pause, when a new song starts)."""
        bpm, conf = a.get("bpm", 0.0), a.get("bpm_conf", 0.0)
        if not bpm or conf < 0.15:
            return
        fresh = now - self._sound_since < 4.0
        if not fresh:
            # Mid-song, a reading at 1/2, 2/3, 3/4 ... of the tempo is the estimator slipping on
            # a triplet or a half-time part, not the song changing: it counts as the same tempo.
            for m in (2.0, 0.5, 1.5, 2 / 3, 4 / 3, 0.75):
                if abs(bpm * m / self.bpm - 1) < 0.04:
                    bpm *= m
                    break
        if abs(bpm / self.bpm - 1) < 0.06:
            self.bpm += (bpm - self.bpm) * min(1.0, dt * 0.8)
            self._tempo_cand = None
            return
        if self._tempo_cand is None or abs(bpm / self._tempo_cand[0] - 1) > 0.03:
            self._tempo_cand = (bpm, now)
        elif fresh or now - self._tempo_cand[1] > 8.0:
            self.bpm, self._tempo_cand = bpm, None

    def _features(self, a: dict, now: float, dt: float) -> None:
        rms, bass, high = a.get("rms", 0.0), a.get("bass_raw", 0.0), a.get("high_raw", 0.0)
        # The slow averages follow the whole song so far at its start, then the last ~8 s.
        slow = max(0.5, min(8.0, now - self._sound_since))
        # How much of the last few seconds was (nearly) silent: in sparse, staccato music the
        # silences between the notes are part of the sound, not the breath before a drop.
        self._quiet_frac = _ema(self._quiet_frac, 1.0 if rms < 0.15 * self._l_fast else 0.0, dt, 4.0)
        self._l_fast = _ema(self._l_fast, rms, dt, 0.25)
        self._l_mid = _ema(self._l_mid, rms, dt, 1.5)        # evens out the single kicks
        self._l_mid_peak = max(self._l_mid, self._l_mid_peak * math.exp(-dt / 45))
        self._l_slow = _ema(self._l_slow, rms, dt, slow)
        # Bass: jumps up on every kick and only slowly lets go, so the gap between two kicks
        # isn't mistaken for the bass dropping out.
        self._bass_env = max(bass, self._bass_env * math.exp(-dt / 0.8))
        self._bass_avg = _ema(self._bass_avg, bass, dt, 2.0)    # the bass of the last couple of seconds
        # (the slowly falling maximum of that average: one huge bass hit in a break mustn't make
        # the drop after it sound like no bass at all)
        self._bass_max = max(self._bass_avg, self._bass_max * math.exp(-dt / 30))
        self._h_fast = _ema(self._h_fast, high, dt, 0.4)
        self._h_slow = _ema(self._h_slow, high, dt, slow)
        self._cent = _ema(self._cent, a.get("centroid", 0.0), dt, 0.4)
        self._bass_fast = _ema(self._bass_fast, bass, dt, 0.06)
        self._level = _ema(self._level, a.get("level", 0.0), dt, 1.0)
        if now - self._hist_t >= 0.25:
            self._hist_t = now
            # The song's full bass: what it reaches in its top fifth over the last 45 s, not its
            # single loudest moment (the tail of the previous song, or one big hit, would make a
            # whole groove sound like a break). The slowly falling maximum of the bass's average
            # keeps a long break a break. After a second of (near) silence out of a groove or a drop
            # it starts over: that is where one song ends and the next begins (or a drop comes in,
            # which then sets it right away). A pause in a break or a build-up is just a pause:
            # starting over there made the bass-less music after it sound like the bass was back.
            hushed = rms < 0.08 * self._l_slow
            if hushed and self._hush_since is None:
                self._hush_from = self.section if self.section != "gap" else self._gap_from
            self._hush_since = (self._hush_since or now) if hushed else None
            if (self._hush_since is not None and now - self._hush_since >= 1.0 and self._bass_seen
                    and self._hush_from in ("groove", "drop", "silent")):
                self._bass_seen.clear()
                self._bass_max = 1e-6
            self._bass_seen.append(self._bass_env)
            self._bass_peak = max(float(np.percentile(self._bass_seen, 80)), 1.2 * self._bass_max, 1e-6)
            self._hist.append((self._l_fast, self._h_fast, self._cent,
                               self._bass_env / max(self._bass_peak, 1e-6)))
        self._score = _ema(self._score, self._build_score(now), dt, 0.4)

    def _rise(self) -> float:
        """How much louder the last second is than three seconds before it (0 = flat)."""
        h = [e[0] for e in list(self._hist)[-16:]]
        if len(h) < 12:
            return 0.0
        early, late = sum(h[:4]) / 4, sum(h[-4:]) / 4
        return (late - early) / max(sum(h) / len(h), 1e-6)

    def _build_score(self, now: float) -> float:
        """How much this sounds like a build-up, 0..1, the way you hear one: a riser sweeping up
        (the sound's centre of weight climbs, the highs get brighter), a snare roll getting
        tighter, the bass being filtered away, and the whole thing getting louder. Compares the
        last second with the second four seconds ago."""
        h = list(self._hist)
        if len(h) < 20:
            return 0.0
        late, mid, early = h[-4:], h[-8:-4], h[-18:-14]
        avg = lambda rows, k: sum(r[k] for r in rows) / len(rows)   # noqa: E731
        riser = (avg(late, 2) - avg(early, 2)) / 0.6                # +0.6 octave = a full riser
        bright = math.log2((avg(late, 1) + 1e-6) / (avg(early, 1) + 1e-6)) / 1.5
        self._sweep_c = max(0.0, min(1.0, riser))
        # ... but only while it is still rising: the bass leaving for a break (or a chorus coming
        # in) moves the sound up in one step, and a step is no riser once it is over.
        riser = min(riser, (avg(late, 2) - avg(mid, 2)) / 0.15)
        bright = min(bright, math.log2((avg(late, 1) + 1e-6) / (avg(mid, 1) + 1e-6)) / 0.375)
        louder = (avg(late, 0) - avg(early, 0)) / max(avg(early, 0), 1e-6) / 0.5
        thin = (0.75 - avg(late, 3)) / 0.5
        per_beat = sum(1 for t in self._hits if now - t < 1.5) / (1.5 * self.bpm / 60)
        roll = math.log2(max(per_beat, 1.0)) / 2
        clip = lambda v: max(0.0, min(1.0, v))                      # noqa: E731
        # A riser keeps climbing: each of the last five seconds sits higher than the one before,
        # and no single second did most of it. A drop or a chorus coming in is a step up instead,
        # which lands in one or two of those seconds (and must not be taken for a build-up).
        secs = [h[-20:-16], h[-16:-12], h[-12:-8], mid, late]
        climb = _climbing([avg(s, 2) for s in secs], 0.5, 0.08)
        brighter = _climbing([math.log2(avg(s, 1) + 1e-6) for s in secs], 1.2, 0.2)
        self._rise_c, self._thin_c, self._roll_c = clip(max(riser, bright)), clip(thin), clip(roll)
        if now - self._roll_t >= 0.25:
            self._roll_t = now
            self._rolls.append(self._roll_c)
        early_roll = list(self._rolls)[:4]
        self._rolling = self._roll_c >= 0.9 or (len(self._rolls) >= 12 and self._roll_c - min(early_roll) >= 0.15)
        self._climb_c = clip(max(climb, brighter))
        if self._climb_c >= 0.6:
            self._climb_since = self._climb_since if self._climb_since is not None else now
        else:
            self._climb_since = None
        return (0.35 * clip(max(riser, bright)) + 0.25 * clip(roll) + 0.25 * clip(thin)
                + 0.15 * clip(louder))

    def _candidate(self, a: dict, now: float) -> str:
        """The section the music sounds like right now."""
        sec, in_state = self.section, now - self._since
        bass_ratio = self._bass_env / max(self._bass_peak, 1e-6)
        loud_ratio = self._l_mid / max(self._l_mid_peak, 1e-6)
        rise, hi_rise = self._rise(), self._h_fast / max(self._h_slow, 1e-6)
        quiet = loud_ratio < (0.32 if sec == "calm" else 0.2)
        # How long the bass has been away (in beats), and how long it was away before it came back.
        # A break needs it away for a bar (and then two beats more), a drop needs it to have been away for two
        # bars: a bass line that plays every other bar is no string of breaks and drops.
        if bass_ratio < 0.3:
            if self._gone_since is None:
                self._gone_since = now
        elif bass_ratio > 0.55 and self._gone_since is not None:
            self._was_gone, self._back_at = (now - self._gone_since) * self.bpm / 60, now
            self._gone_since = None
        gone = 0.0 if self._gone_since is None else (now - self._gone_since) * self.bpm / 60
        long_gone = gone >= 8 or (now - self._back_at < 0.5 and self._was_gone >= 8)
        if a.get("silent", True):
            return "silent"
        drops = STYLES[self.style_id].drops   # only dance music builds up to drops
        # The drop: the bass slams back in, against the little there was in the last seconds.
        # Measured on a 60 ms average, so it is heard within a few hundredths of a second.
        played = now - self._sound_since > 4         # not the first kick of a song
        slam = (self._bass_fast > 0.45 * self._bass_peak and self._bass_fast > 2.2 * self._bass_avg and played)
        # After a break the bass may also come back over a moment (a filter opening): softer test.
        back = played and self._bass_fast > 0.5 * self._bass_peak and self._bass_fast > 1.6 * self._bass_avg
        thin_before = self._bass_avg < 0.35 * self._bass_peak
        kicks = a.get("beat_count", 0)
        if sec == "drop" and self._impact is not None:   # a drop fired on a bass hit: does the beat stay?
            until, before = self._impact
            self._impact_n += 1
            self._impact_bass += self._bass_fast > 0.25 * self._bass_peak
            if self._beat > until:
                self._impact = None
                # One big hit in a break (an "impact") with the bass gone again, or a bass note
                # held without a beat under it: no drop. A drop brings the beat back.
                if self._impact_bass < 0.35 * self._impact_n or kicks - self._impact_kicks < 2:
                    return "rebuild" if before == "build" else "unbreak"
        self._kicks = kicks
        if sec == "drop" and self._unconfirmed_until is not None:   # a drop fired on the count ...
            if self._bass_fast > 0.4 * self._bass_peak:
                self._unconfirmed_until = None                      # ... the bass came: it was right
            elif self._beat > self._unconfirmed_until:
                self._unconfirmed_until = None
                return "rebuild"                                    # ... it didn't: back to the build-up
            return "drop"
        if sec == "gap":              # (only the bass coming back counts: not the fading tail of what was)
            return "drop" if slam or back else "break" if in_state > 2.5 else "gap"
        # The breath before a drop: the music suddenly goes (nearly) silent, from anywhere. Even a
        # cut of a tenth of a second followed by the bass slamming in is a new drop ("redrop").
        if drops and sec in ("groove", "drop", "build") and played and self._quiet_frac < 0.1:
            sudden_quiet = a.get("rms", 0.0) < 0.15 * self._l_fast
            self._gap_since = (self._gap_since or now) if sudden_quiet else None
            if self._gap_since is not None and now - self._gap_since > 0.06:
                self._dip_at = now
            if self._gap_since is not None and now - self._gap_since > 0.15:
                return "gap"
            # (A new drop out of a drop only after it has run four bars: a bass line with a short
            # gap in every bar is one drop, not a new one every bar.)
            if sec in ("groove", "drop") and slam and now - self._dip_at < 1.5 and \
                    (sec == "groove" or self._beat - self._entered_beat >= 16):
                self._dip_at = -99.0
                return "redrop"
        if sec == "build":
            # (A kick running through the build-up is no drop: only a slam, after the bass has been
            # away for a couple of bars or the build-up has run that long. A bass line that plays
            # every other bar is no build-up and drop; a bass filtered back in before the drop is.)
            if slam and (long_gone or self._beat - self._entered_beat >= 8):
                return "drop"
            # It fizzled out when it stopped sounding like a build-up and the riser fell back down
            # (after a riser has swept all the way up, the score drops while the build goes on).
            # (Or when nothing in it has risen for a while: no riser, no climb, no snare roll. The
            # bass gone alone keeps the score up, but a break is no build-up.)
            overdue = self._beat + self._lead * self.bpm / 60 - self._drop_at
            # (The snare roll part counts only while it tightens: a busy beat full of hi-hats and
            # snares reads as a steady "roll" too.)
            stale = self._rise_c < 0.2 and self._climb_c < 0.2 and not self._rolling
            fizzled = (self._score < BUILD_OFF or stale) and (self._cent - self._build_cent < 0.5 or overdue > 16)
            self._fizzle_since = (self._fizzle_since or now) if fizzled else None
            wait = 3 if self._score < BUILD_OFF else 6
            if in_state > 40 or (self._fizzle_since is not None and now - self._fizzle_since > wait):
                return "groove" if bass_ratio > 0.5 else "break"    # it didn't build to anything
            return "build"
        # A build-up: from a break, sounding like one is enough; out of a groove or a drop it also
        # needs a riser still climbing, or the bass being filtered away while something rises (the
        # bass just leaving is a break; a busy drop full of snares and bright synths scores high
        # too, but it came in as a step up, not a climb).
        # A riser that keeps climbing for a second or two is a build-up by itself, before the
        # snare roll and the missing bass make it sound like one all round.
        # (Out of a groove or drop only once it has run 5 s: the score looks 4 s back, and right
        # after a drop that is still the quieter, darker stretch before it.)
        settled = sec in ("break", "calm") or in_state > 5.0
        climbing = self._climb_since is not None and now - self._climb_since >= (1.0 if sec in ("break", "calm") else 2.5)
        # (A clearly built-up score with the sound still rising counts too.)
        if drops and sec in ("groove", "break", "calm", "drop") and settled and (
                climbing or (self._score > BUILD_ON and (sec in ("break", "calm") or self._climb_c >= 0.6
                                                         or (self._thin_c >= 0.5 and self._rise_c >= 0.5)
                                                         or (self._rise_c >= 0.6 and self._score > 0.65)))):
            return "build"
        if drops and sec in ("groove", "break", "calm") and (slam or (back and sec != "groove")) and \
                (thin_before or sec != "groove") and long_gone:
            return "drop"             # the bass slams back in after a stretch without much of it
        if bass_ratio < 0.3 and (gone >= 4 or sec in ("break", "calm")):   # the bass is gone
            return "calm" if quiet else "break"
        if sec in ("break", "calm") and bass_ratio > 0.55 and in_state > 2.0:
            return "drop" if drops and long_gone else "groove"   # the bass is back after a break
        if sec == "drop":
            beats = self._beat - self._entered_beat
            return "drop" if beats < 32 or (loud_ratio > 0.8 and beats < 128) else "groove"
        if (sec == "groove" and in_state > 4 and now - self._sound_since > 12
                and self._l_mid > 0.9 * self._l_mid_peak and self._l_mid > 1.25 * self._l_slow):
            return "drop"             # a chorus or a second drop: the energy jumps up
        return "calm" if quiet else "groove"

    def _sections(self, a: dict, now: float) -> None:
        sec = self.section
        cand = self._candidate(a, now)
        if cand == "redrop":           # a new drop right out of the last one: hit it again
            self._enter("drop", now)
            return
        if cand == "rebuild":          # a drop fired on the count that didn't come: back at once
            self._enter("build", now)
            return
        if cand == "unbreak":          # a drop fired on a single bass hit in a break: back at once
            self._enter("break", now)
            return
        if cand == sec:
            self._cand = None
            return
        if self._cand is None:
            self._cand_since = now     # the music has moved away from this section since now
        self._cand = cand              # (flipping between break and calm doesn't restart the wait)
        # How long the music must stay this way before the show follows, in beats.
        immediate = cand in ("silent", "gap") or (cand == "drop" and sec in ("build", "gap", "break", "calm", "groove"))
        # (between calm and groove more patiently: an outro that dips every other bar flickered)
        wait = 0.0 if immediate else {"drop": 1, "build": 1, "groove": 4 if sec == "calm" else 2, "break": 2,
                                      "calm": 8}.get(cand, 2)
        if cand == "break" and not STYLES[self.style_id].drops:
            wait = 4                  # an 808 or bass line pausing for a bar isn't a break yet
        if not immediate and cand != "build" and now - self._since < 2.0:
            return                    # sections last at least two seconds (build-ups may start any time)
        if now - self._cand_since < wait * 60 / self.bpm:
            return
        self._enter(cand, now)

    def _enter(self, cand: str, now: float, on_count: bool = False) -> None:
        sec = self.section
        resumed = cand == "build" and sec == "drop"   # back from a drop called on the count
        if sec == "silent":
            self._sound_since = now
            self._snare_gaps.clear()   # probably a new song: start learning it from scratch
            self._cues.clear()
            self._bass_seen.clear()
            self._bass_max = 1e-6
        if sec in ("break", "build", "gap", "calm") and cand in ("groove", "drop"):
            self._grid_trust_at = now + 5.0
        if cand == "gap":
            self._gap_from = sec
        self.section, self._since, self._entered_beat, self._cand = cand, now, self._beat, None
        self._gap_since = None
        lead = self._lead * self.bpm / 60
        if cand == "build" and not resumed:
            # The build-up started on a bar line about a bar before it could be heard as one;
            # the drop is due eight bars after that.
            self._build_origin = self._bar_line(self._beat + lead - 4)
            self._drop_at = self._build_origin + 32
            self.frame.build = 0.0
            self._build_cent = min(e[2] for e in list(self._hist)[-16:]) if self._hist else self._cent
            self._fizzle_since = None
        if resumed:
            self._drop_at += 16       # the count was early: four more bars
        if cand == "drop":
            # The one white blinder is for dance drops, and not every few bars (a drop on the
            # count always gets it); a chorus in other music, or a drop soon after the last one,
            # hits in color.
            style = STYLES[self.style_id]
            blinder = style.drops and (on_count or self._beat - self._blinder_beat >= 16)
            self.frame.flash = 1.0 if blinder else 0.7 if not style.drops else 0.6
            if self._mine:     # your colors: the blinder is white only when strobes are wanted
                self.frame.flash_color = WHITE if blinder and self._strobe else self._lightest
            else:
                self.frame.flash_color = WHITE if blinder else style.pair(self._pair_i + 1)[0]
            if blinder:
                self._blinder_beat = self._beat
            if on_count:
                self._phrase_start = round(self._drop_at)
                self._unconfirmed_until = self._beat + 1.5
            else:
                self._phrase_start = round(self._beat + lead - 0.15)   # the drop is a "one"
                self._vote_bar(int(self._phrase_start), 2.0)
                if sec in ("build", "break", "calm"):     # the bass must stay for a bar (see _candidate)
                    self._impact, self._impact_n, self._impact_bass = (self._beat + 4.0, sec), 0, 0
                    self._impact_kicks = self._kicks
        cut = cand in ("drop", "gap", "build", "silent") or sec in ("gap", "build", "silent")
        self._next_scene(entry=True, cut=cut)
        self._phrase_idx = self._phrase_now()

    def _phrase_now(self) -> int:
        length = 16 if self.section in ("break", "calm") else 32
        return max(0, int((self._beat + self._lead * self.bpm / 60 - self._phrase_start) // length))

    def _phrases(self) -> None:
        idx = self._phrase_now()
        if idx != self._phrase_idx:
            self._phrase_idx = idx
            self._next_scene()
            style = STYLES[self.style_id]
            if self.section in ("groove", "drop") and style.pump != "none":  # hit the new phrase's downbeat
                f = self.frame
                f.flash, f.flash_color = max(f.flash, 0.4 + 0.3 * f.hard), self._pair(style, self._pair_i)[0]

    def _next_scene(self, entry: bool = False, cut: bool = False) -> None:
        """The next look: from the song's cue list when this part was heard before (so a second
        chorus or drop looks like the first), otherwise a fresh choice that is then remembered."""
        if self._pending_style:
            self.style_id, self._pending_style = self._pending_style, None
            self._cues.clear()
            ai = self._ai
            log.info("Light show style: %s (%s)", STYLES[self.style_id].name,
                     ", ".join(f"{n} {s:.2f}" for n, s in ai.get("top", [])) if ai.get("style")
                     else f"from the tempo, {self.bpm:.0f} BPM")
        f, sec, style = self.frame, self.section, STYLES[self.style_id]
        f.seed = self._rng.randrange(1 << 30)
        previous = f.scene
        if sec in ("silent", "gap", "build"):
            f.scene = {"silent": "idle", "gap": "blackout", "build": "riser"}[sec]
            self._set_pair(self._pair_i + 1)
        else:
            self._cue_i = 0 if entry else self._cue_i + 1
            cues = self._cues.setdefault(sec, [])
            if self._cue_i < len(cues):
                f.scene, pair = cues[self._cue_i]
            else:
                options = list(style.looks.get(sec) or style.looks["groove"])
                if entry and sec == "drop" and not cues:
                    f.scene = options[0]          # the first drop opens with the style's signature look
                else:
                    f.scene = self._rng.choice([s for s in options if s != previous] or options)
                pair = self._pair_i + 1
                cues.append((f.scene, pair))
            if self._t is None or self._t >= self._harmony_until:   # unless the chords lead the colors
                self._set_pair(pair)
        # Wipe the new look in over a beat, except on the hard cuts (drop, blackout, build-up).
        f.prev_scene = previous
        f.wipe = self._rng.randrange(3)
        self._trans_beat = -99.0 if cut or previous == f.scene else self._beat


# ---- stage layout ------------------------------------------------------------------------------

# Where the things on the desk stand, in front of the monitor: the keyboard, the mouse mat under
# it and the mouse, the headset stand on the right.
DESK = {"KEYBOARD": (0.22, 0.52), "KEYPAD": (0.12, 0.2), "MOUSEMAT": (0.2, 0.66), "MOUSE": (0.56, 0.62),
        "HEADSET_STAND": (0.64, 0.7), "HEADSET": (0.64, 0.7)}


def stage_spans(devices, overrides: dict | None = None) -> dict[str, tuple[float, float, str]]:
    """Where each light sits: (start, end, layer) with 0 = left and 1 = right as seen from the
    desk chair, layer "front" or "back". The WLED strips hang above the monitor (left and
    middle), the keyboard, mouse mat and mouse sit in front of it, the PC's own lights (in the
    case) stand to the right of the monitor, and the Nanoleaf is on the wall behind. `overrides`
    (id -> [start, end] or [start, end, layer]) wins; start > end runs a strip the other way."""
    spans: dict[str, tuple[float, float, str]] = {}
    strips = sorted((d for d in devices if d.kind == "wled"), key=lambda d: -d.pixel_count)
    for i, d in enumerate(strips):
        spans[d.id] = (*[(0.0, 0.68), (0.12, 0.6), (0.05, 0.65), (0.2, 0.5)][min(i, 3)], "front")
    desk = {d.id: DESK[d.desk_item()] for d in devices if getattr(d, "desk_item", None) and d.desk_item() in DESK}
    for dev_id, (a, b) in desk.items():
        spans[dev_id] = (a, b, "front")
    pc = sorted((d for d in devices if d.group == "pc" and d.id not in desk), key=lambda d: d.name.lower())
    for i, d in enumerate(pc):
        spans[d.id] = (0.72 + 0.28 * i / len(pc), 0.72 + 0.28 * (i + 1) / len(pc), "front")
    for d in devices:
        if d.kind == "nanoleaf":
            spans[d.id] = (0.0, 1.0, "back")
    for d in devices:   # anything else: bulbs wash the room from behind, strips and bars span the front
        if d.id not in spans:
            spans[d.id] = (0.0, 1.0, "back" if d.pixel_count <= 1 else "front")
    for dev_id, span in (overrides or {}).items():
        if isinstance(span, (list, tuple)) and len(span) in (2, 3):
            spans[dev_id] = (float(span[0]), float(span[1]), str(span[2]) if len(span) == 3 else "front")
    return spans


# ---- rendering ---------------------------------------------------------------------------------

def _fill(n: int, color: np.ndarray, v: float) -> np.ndarray:
    return np.tile(color * v, (n, 1))


def _gradient(colors: list[np.ndarray], u: np.ndarray) -> np.ndarray:
    pal = np.stack(colors)
    u = (u % 1) * len(pal)
    i = u.astype(int) % len(pal)
    frac = (u - np.floor(u))[:, None]
    return pal[i] * (1 - frac) + pal[(i + 1) % len(pal)] * frac


def _pump(f: Frame, x, mem):
    """The whole front on every beat; the snare lights every other zone in the accent color."""
    out = _fill(len(x), f.main, 0.03 + 0.97 * f.pump)
    odd = ((x * 8).astype(int) % 2 == 1).astype(np.float32)
    return out + odd[:, None] * (f.accent * 0.9 * f.hit)[None, :]


def _chase(f: Frame, x, mem):
    """A soft block gliding back and forth, once across per two beats."""
    u = (f.beat / 2) % 2
    pos = u if u < 1 else 2 - u
    pos = 0.5 - 0.5 * math.cos(math.pi * pos)           # eased at the ends
    k = np.clip(1 - np.abs(x - (0.06 + 0.88 * pos)) / 0.16, 0, 1) ** 1.5
    out = k[:, None] * (f.main * (0.55 + 0.45 * f.pump))[None, :]
    return out + (1 - k)[:, None] * (f.accent * 0.35 * f.hit)[None, :]


def _chase16(f: Frame, x, mem):
    """A block stepping across the stage on every 16th note, back the other way each bar."""
    s = math.floor(f.beat * 4)
    pos = ((s % 16) + 0.5) / 16
    if (s // 16) % 2:
        pos = 1 - pos
    k = np.clip(1 - np.abs(x - pos) / 0.12, 0, 1)
    color = f.main if s % 4 else f.accent        # the step on the beat in the accent color
    return k[:, None] * (color * (0.7 + 0.3 * f.pump))[None, :]


def _scanner(f: Frame, x, mem):
    """Two beams sweeping in opposite directions over four beats, like moving heads."""
    p = 0.5 - 0.5 * math.cos(math.pi * f.beat / 2)
    a = np.exp(-((x - (0.06 + 0.88 * p)) / 0.1) ** 2)
    b = np.exp(-((x - (0.94 - 0.88 * p)) / 0.1) ** 2)
    v = 0.45 + 0.55 * f.pump
    return (a[:, None] * f.main[None, :] + b[:, None] * f.accent[None, :]) * v


def _wave(f: Frame, x, mem):
    """Waves of the main color rolling across the stage, one per beat."""
    w = (0.5 + 0.5 * np.sin(2 * np.pi * (x * 1.5 - f.beat))) ** 3
    return w[:, None] * (f.main * (0.35 + 0.65 * f.pump))[None, :] + (f.accent * 0.25 * f.hit)[None, :]


def _alternate(f: Frame, x, mem):
    """Six zones taking turns on every beat; the snare fills the dark ones with the accent."""
    lit = (((x * 6).astype(int) + f.beat_i) % 2 == 0).astype(np.float32)[:, None]
    return lit * (f.main * (0.2 + 0.8 * f.pump))[None, :] + (1 - lit) * (f.accent * f.hit)[None, :]


def _mirror(f: Frame, x, mem):
    """A ring opening from the middle of the stage on every beat; the snare blooms in the center."""
    d = np.abs(x - 0.5) * 2
    k = np.exp(-((d - (f.beat % 1)) / 0.12) ** 2)
    return k[:, None] * f.main[None, :] + np.clip(1 - d, 0, 1)[:, None] * (f.accent * 0.7 * f.hit)[None, :]


def _random_zones(f: Frame, x, mem):
    """Every 8th note a random zone flares up (the same zone on every light) and fades."""
    zone = (x * 8).astype(int)
    pos = f.beat * 2
    s = math.floor(pos)
    out = np.zeros((len(x), 3), dtype=np.float32)
    for back in range(3):
        age = (pos - (s - back)) * f.beat_s / 2
        z = int(_hash(s - back, f.seed) * 8)
        color = f.main if _hash(s - back, f.seed + 1) < 0.65 else f.accent
        out += (zone == z)[:, None] * (color * math.exp(-age / 0.12))[None, :]
    return out


def _strobe_chase(f: Frame, x, mem):
    """Hard drops: a bright flash of the accent color jumping somewhere new on every 16th note,
    over a pumping base."""
    pos = f.beat * 4
    s = math.floor(pos)
    age = (pos - s) * f.beat_s / 4
    k = (np.abs(x - _hash(s, f.seed)) < 0.09).astype(np.float32) * math.exp(-age / 0.05)
    return k[:, None] * f.accent[None, :] + (f.main * 0.3 * f.pump)[None, :]


def _split(f: Frame, x, mem):
    """Left and right in the two colors, swapping sides on every beat."""
    left = (x < 0.5) ^ bool(f.beat_i % 2)
    v = 0.1 + 0.9 * f.pump
    return np.where(left[:, None], (f.main * v)[None, :], (f.accent * v * 0.8)[None, :])


def _sweep(f: Frame, x, mem):
    return _gradient(f.colors, x + f.beat / 8) * (0.15 + 0.85 * f.pump)


def _drift(f: Frame, x, mem):
    return _gradient(f.colors, x * 0.6 + f.t * 0.03) * (0.12 + 0.45 * f.level)


def _breathe(f: Frame, x, mem):
    k = 0.5 - 0.5 * math.cos(math.pi * (f.beat / 8 % 2))
    return _fill(len(x), f.main * (1 - k) + f.accent * k, 0.1 + 0.5 * f.level)


def _glow(f: Frame, x, mem):
    w = 0.6 + 0.4 * np.sin(2 * np.pi * (x - f.t * 0.08))
    return w[:, None] * (f.main * (0.06 + 0.3 * f.level))[None, :]


def _sparkle(f: Frame, x, mem):
    n = len(x)
    spark = mem.get("spark")
    if spark is None or len(spark) != n:
        spark = mem["spark"] = np.zeros(n, dtype=np.float32)
        mem["rng"] = np.random.default_rng()
    spark *= 0.8
    spark[mem["rng"].random(n) < 0.01 + 0.15 * f.treble * f.treble + 0.12 * f.hat + 0.2 * f.hit] = 1.0
    return _fill(n, f.main, 0.06) + spark[:, None] * (f.white * 0.6 + f.accent * 0.4)[None, :]


def _riser(f: Frame, x, mem):
    """Build-up: the light closes in from both ends and turns white; snare hits flash it all."""
    d = np.abs(x - 0.5) * 2
    filled = (d >= 1 - max(0.06, f.build)).astype(np.float32)[:, None]
    color = f.main * (1 - 0.45 * f.build) + f.white * 0.45 * f.build
    out = filled * (color * (0.35 + 0.65 * max(f.build, f.hit)))[None, :] + (1 - filled) * (f.accent * 0.04)[None, :]
    return out + (color * 0.7 * f.hit * f.build)[None, :]


def _pulse(f: Frame, x, mem):
    """Rock and hip-hop: the whole front punches on the kick drum itself; each snare lights one
    side in the accent color, the other side the next time."""
    out = _fill(len(x), f.main, 0.04 + 0.96 * f.pump)
    side = (x < 0.5) if f.hits % 2 else (x >= 0.5)
    return out + side.astype(np.float32)[:, None] * (f.accent * f.hit)[None, :]


def _bass_glow(f: Frame, x, mem):
    """Hip-hop: the room glows with the 808 and fades with it, brightest in the middle; the snare
    flashes the outer ends in the accent color."""
    d = np.abs(x - 0.5) * 2
    v = f.bass ** 1.4
    core = np.clip(1.15 - d * (1.2 - 0.6 * v), 0, 1)
    out = core[:, None] * (f.main * (0.03 + 0.97 * v))[None, :]
    return out + (d > 0.55).astype(np.float32)[:, None] * (f.accent * f.hit)[None, :]


def _vocal(f: Frame, x, mem):
    """The voice (rap flow, singing) lights the stage from the middle out, as wide as it is loud."""
    width = 0.06 + 0.55 * f.mid
    k = np.clip(1 - np.abs(x - 0.5) / width, 0, 1) ** 0.7
    return k[:, None] * (f.main * (0.2 + 0.8 * f.mid))[None, :] + (f.accent * 0.5 * f.hit)[None, :] * k[:, None]


def _wobble(f: Frame, x, mem):
    """Dubstep: the stage breathes with the bass wobble, in width and color."""
    b = f.bass
    k = np.clip(1 - np.abs(x - 0.5) / (0.08 + 0.5 * b), 0, 1)
    color = f.main * (1 - b) + f.accent * b
    return k[:, None] * (color * (0.15 + 0.85 * b))[None, :] + (f.accent * 0.6 * f.hit)[None, :]


def _disco(f: Frame, x, mem):
    """House and disco: a light-up dance floor, every segment its own color, stepping on each
    beat; the hi-hats make it twinkle."""
    pal = np.stack(f.colors)
    seg = (x * 10).astype(int)
    out = pal[(seg * 3 + f.beat_i) % len(pal)] * (0.3 + 0.7 * f.pump)
    return out * (1 + 0.25 * f.hat)


def _offbeat(f: Frame, x, mem):
    """House and reggae: the lights hit on the "and" between the beats, alternating halves."""
    left = (x < 0.5) ^ bool(f.beat_i % 2)
    v = 0.05 + 0.95 * f.offbeat
    return np.where(left[:, None], (f.main * v)[None, :], (f.accent * v * 0.7)[None, :]) + (f.main * 0.15 * f.pump)[None, :]


def _bounce(f: Frame, x, mem):
    """Pop: a colored block bouncing from end to end, landing on every beat, a new color each time."""
    pos = abs(math.sin(math.pi * f.beat / 2))       # at an end on every beat
    k = np.clip(1 - np.abs(x - (0.08 + 0.84 * pos)) / 0.2, 0, 1) ** 1.2
    color = f.colors[f.beat_i % len(f.colors)]
    return k[:, None] * (color * (0.55 + 0.45 * f.pump))[None, :] + (f.accent * 0.3 * f.hit)[None, :]


def _sway(f: Frame, x, mem):
    """Latin and reggae: a warm pool of light rocking left and right over two beats, the offbeat
    adding the accent color."""
    pos = 0.5 + 0.32 * math.sin(math.pi * f.beat)
    k = np.exp(-((x - pos) / 0.28) ** 2)
    return k[:, None] * (f.main * (0.45 + 0.55 * f.pump) + f.accent * 0.6 * f.offbeat)[None, :]


def _flow(f: Frame, x, mem):
    """Chill: the colors drift slowly and follow the melody, brighter as it swells."""
    u = x * 0.7 + f.t * 0.015 + f.mid * 0.25
    return _gradient(f.colors, u) * (0.08 + 0.55 * f.mid + 0.15 * f.level)


def _blackout(f: Frame, x, mem):
    return np.zeros((len(x), 3), dtype=np.float32)


def _idle(f: Frame, x, mem):
    return _gradient(f.colors, x * 0.6 + f.t * 0.02) * 0.06


SCENES = {
    "pump": _pump, "chase": _chase, "chase16": _chase16, "scanner": _scanner, "wave": _wave,
    "alternate": _alternate, "mirror": _mirror, "random_zones": _random_zones, "strobe_chase": _strobe_chase,
    "split": _split, "sweep": _sweep, "drift": _drift, "breathe": _breathe, "glow": _glow, "sparkle": _sparkle,
    "riser": _riser, "blackout": _blackout, "idle": _idle, "pulse": _pulse, "bass_glow": _bass_glow,
    "vocal": _vocal, "wobble": _wobble, "disco": _disco, "offbeat": _offbeat, "bounce": _bounce, "sway": _sway,
    "flow": _flow,
}


def _back(f: Frame, n: int, mem, slow: bool = False) -> np.ndarray:
    """The wall behind you answers the front: it hits on the snare while the front hits the kick."""
    sec = f.section
    if sec == "gap":
        return np.zeros((n, 3), dtype=np.float32)
    if sec == "build":
        color = f.main * (1 - 0.45 * f.build) + f.white * 0.45 * f.build
        return _fill(n, color, 0.15 + 0.85 * max(f.hit, f.build * f.pump))
    if sec in ("groove", "drop"):
        if f.scene in ("flow", "breathe", "drift"):        # chill music: the back wall just glows along
            return _fill(n, f.accent, 0.05 + 0.35 * f.mid)
        # Snare in the accent color, hi-hats ticking softly (trap hat rolls), the beat underneath.
        # (Bulbs can't tick that fast: they leave out the hi-hats.)
        return _fill(n, f.accent, f.hit + (0.0 if slow else 0.25 * f.hat)) + _fill(n, f.main, 0.12 * f.pump)
    if sec in ("break", "calm"):
        return _fill(n, f.accent, 0.06 + 0.3 * f.level * (0.5 - 0.5 * math.cos(math.pi * f.beat / 4)))
    return _fill(n, f.accent, 0.04)


# Lights that take only ~10 updates a second (Hue, Home Assistant, Govee, Elgato: behind a bridge,
# a hub or the cloud) can't step on every 16th note: they get the calmer look of the same kind.
SLOW_FPS = 12
SLOW_LOOKS = {"chase16": "chase", "strobe_chase": "pump", "random_zones": "alternate", "sparkle": "breathe"}
HOLD_S = 0.2     # how long a slow light holds a short flash, fading, so its next update still shows it


def _wiped(f: Frame, x: np.ndarray, mem: dict, slow: bool = False) -> np.ndarray:
    """The current look; while a new one comes in, wiped over the old one across the stage."""
    scene, prev = (SLOW_LOOKS.get(f.scene, f.scene), SLOW_LOOKS.get(f.prev_scene, f.prev_scene)) if slow \
        else (f.scene, f.prev_scene)
    new = SCENES.get(scene, _idle)(f, x, mem)
    if f.trans >= 1.0 or prev == scene:
        return new
    old = SCENES.get(prev, _idle)(f, x, mem.setdefault("prev", {}))
    edge = {0: x, 1: 1 - x, 2: np.abs(x - 0.5) * 2}[f.wipe]
    k = np.clip((f.trans * 1.25 - edge) / 0.25, 0, 1)[:, None]
    return old * (1 - k) + new * k


def _spin(f: Frame, rgb: np.ndarray, ring: int) -> np.ndarray:
    """Fans are rings of LEDs: a bright arc spins round each fan with the beat, the fans side
    by side turning opposite ways and swapping direction every bar."""
    n = len(rgb)
    i = np.arange(n)
    fan, pos = i // ring, (i % ring) / ring
    moving = f.section in ("groove", "drop", "build")
    speed = (1.0 if f.hard >= 0.6 else 0.5) if moving else 0.12
    direction = np.where((fan + f.bar) % 2 == 0, 1.0, -1.0)
    head = (f.beat * speed * direction + fan / max(1, n // ring)) % 1
    d = np.abs(pos - head)
    d = np.minimum(d, 1 - d)
    return rgb * (0.25 + 0.75 * np.exp(-(d / 0.16) ** 2))[:, None]


RIPPLE_LOOKS = {"pump", "pulse", "bass_glow", "offbeat", "split", "disco", "wobble"}


def _ripple(f: Frame, rgb: np.ndarray, geo: np.ndarray) -> np.ndarray:
    """On the keyboard, beat-filling looks become rings rippling out from the middle keys on
    every beat; the snare drops an accent splash in the centre."""
    dist = np.hypot(geo[:, 0] - 0.5, (geo[:, 1] - 0.5) * 0.3) * 2
    ring = np.exp(-((dist - (f.beat % 1) * 1.15) / 0.1) ** 2)
    out = rgb * (0.15 + 0.85 * ring)[:, None]
    return out + (dist < 0.25).astype(np.float32)[:, None] * (f.accent * 0.8 * f.hit)[None, :]


def _hold(rgb: np.ndarray, t: float, mem: dict) -> np.ndarray:
    """For slow lights (rendered every frame, sent ~10 times a second): each pixel keeps the
    brightest of what it shows now and what it showed lately, fading. A 60 ms snare flash then
    reaches the light as a short pulse instead of slipping between two updates."""
    held, at = mem.get("hold"), mem.get("hold_t")
    if held is None or held.shape != rgb.shape or at is None or not 0 <= t - at < 1.0:
        mem["hold"], mem["hold_t"] = rgb, t
        return rgb
    faded = held * math.exp(-(t - at) / HOLD_S)
    keep = (faded.max(axis=1) > rgb.max(axis=1))[:, None]
    out = np.where(keep, faded, rgb)
    mem["hold"], mem["hold_t"] = out, t
    return out


def render(f: Frame, n: int, span: tuple, mem: dict, fixture: dict | None = None,
           slow: bool = False) -> list[tuple[int, int, int]]:
    """One light's part of the show frame. `slow`: a light that takes only ~10 updates a second
    (it is rendered every frame all the same, so the hold catches the short flashes)."""
    if n <= 0:
        return []
    a, b = span[0], span[1]
    back = len(span) > 2 and span[2] == "back"
    x = (a + (b - a) * (np.arange(n) + 0.5) / n).astype(np.float32)
    if f.blackout:
        rgb = np.zeros((n, 3), dtype=np.float32)
    elif back:
        rgb = _back(f, n, mem, slow)
    else:
        rgb = _wiped(f, x, mem, slow)
        fixture = fixture or {}
        geo = fixture.get("geometry")
        if geo is not None and len(geo) == n and f.scene in RIPPLE_LOOKS and f.section in ("groove", "drop"):
            rgb = _ripple(f, rgb, np.asarray(geo, dtype=np.float32))
        ring = fixture.get("ring")
        if ring and n % ring == 0 and f.scene not in ("blackout", "idle"):
            rgb = _spin(f, rgb, ring)
    if f.tension > 0.02 and not f.blackout:
        # Leaning into a possible build-up: part of the riser look, as far as the tension goes.
        pre = copy.copy(f)
        pre.section, pre.build = "build", 0.5 * f.tension
        lean = _back(pre, n, mem, slow) if back else _riser(pre, x, mem)
        k = 0.6 * f.tension
        rgb = rgb * (1 - k) + lean * k
    rgb = rgb * f.master
    if f.strobe and not slow:   # a bulb can't strobe: it builds up with the rest of the riser
        rgb = np.ones((n, 3), dtype=np.float32)
    elif f.flash > 0.01:
        rgb = rgb * (1 - f.flash) + (f.flash_color * f.flash)[None, :]
    if slow and not f.blackout and f.section != "gap":   # (the breath before a drop goes dark at once)
        rgb = _hold(rgb, f.t, mem)
    elif slow:
        mem.pop("hold", None)
    return [tuple(px) for px in (np.clip(rgb, 0, 1) * 255).astype(np.uint8).tolist()]
