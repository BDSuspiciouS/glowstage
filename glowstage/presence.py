"""Someone's home (Setup → Schedule → While you're away): while you're on holiday and the PC stays
on, the room's lights come on in the evening and go off at night, at a slightly different time every
day, and now and then one light goes off while another comes on. From outside it looks like someone
is home, and not like a timer, which is one fixed block of light at the same minute every evening.

Each day's plan is worked out from the settings, the date and the sun (plan_for, a pure function):
the lights come on around sunset (or at a time you chose) and go off at the time you chose, both
moved by up to 25 minutes, and in between there are a few changes, 20 to 60 minutes apart. The
plan is seeded with the date, so a restart the same evening works out the same plan and carries on
where it was.

The lights take a plain warm white at a usual brightness: no effects, nothing that flashes. The
PC's own lights are never touched, the PC is the one that's "away". Which lights: the room's lights
(all but the PC's) or the lights of the groups you choose.

It only works while this PC is on and Glowstage runs.

How it lives with the rest, while it is on:
- Schedules are skipped (manager.run_schedules): they switch every light, or a preset, and would
  break the pattern. You're away, so there's nothing a schedule is needed for.
- Automatic looks don't start (automations.py): the same reason, and nobody is playing a game.
- Presets in turn, following the wallpaper, the focus timer, the sleep timer and a wake-up are
  stopped when it starts.
- A change by hand isn't fought: the plan only acts at the moments something should change (a
  light comes on, goes off), so a light you set meanwhile keeps its look until the plan's next
  move for it.
- None of it is saved with the lights: if Glowstage stops, the lights come back as they were
  saved. How the lights looked when you switched it on is kept, and Stop puts it back.
"""

from __future__ import annotations

import datetime
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field

from .colors import kelvin_to_rgb, rgb_to_hex

log = logging.getLogger(__name__)

DEFAULT = {"on": False, "lights": "room", "groups": [], "when": "sunset", "at": "17:30", "off": "23:30"}
SPREAD = 25             # minutes: the on and off time of a day move by this much at most
GAP = (20, 60)          # minutes between two moments something could change in the evening
CHANCE = 0.7            # how often such a moment really changes a light ("now and then")
MAX_CHANGES = 3         # changes in one evening, at most
MIN_EVENING = 90        # minutes between on and off, at least (before the spread)
BRIGHTNESS = 70         # percent: a usual brightness for a room in the evening
WARM = rgb_to_hex(kelvin_to_rgb(2700))     # a plain warm white
POLL = 20.0             # seconds between two looks at the clock
TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def _minutes(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def _clock(at: datetime.datetime) -> str:
    return at.strftime("%H:%M")


@dataclass
class Plan:
    day: datetime.date
    on: datetime.datetime
    off: datetime.datetime
    start: list[str]                                             # the lights that come on first
    changes: list[tuple[datetime.datetime, str, bool]] = field(default_factory=list)   # (when, light, on?)

    def lit(self, at: datetime.datetime) -> set[str]:
        """The lights that are on at `at` (none outside the evening)."""
        if not self.on <= at < self.off:
            return set()
        out = set(self.start)
        for when, dev_id, on in self.changes:
            if when <= at:
                (out.add if on else out.discard)(dev_id)
        return out


def plan_for(day: datetime.date, settings: dict, sunset: str, ids: list[str]) -> Plan:
    """The evening that begins on `day`. `sunset` is that day's sunset ("HH:MM", "" when there is
    none: then the time from the settings is used). Same inputs, same plan."""
    ids = sorted(ids)
    rng = random.Random(f"someone's home {day.isoformat()} {','.join(ids)}")
    at = sunset if settings.get("when") == "sunset" and TIME.fullmatch(sunset or "") else settings["at"]
    start = _minutes(at)
    end = _minutes(settings["off"])
    if end <= start + MIN_EVENING:
        # An off time in the small hours is after midnight; one that is in the evening but too
        # close to the start (a late sunset) gives a short evening instead of a day-long one.
        end = end + 1440 if end < 720 else start + MIN_EVENING
    midnight = datetime.datetime.combine(day, datetime.time())
    on = midnight + datetime.timedelta(minutes=start + rng.randint(-SPREAD, SPREAD))
    off = midnight + datetime.timedelta(minutes=end + rng.randint(-SPREAD, SPREAD))
    if off < on + datetime.timedelta(minutes=MIN_EVENING - 2 * SPREAD):    # (can't happen with the above; kept as a guard)
        off = on + datetime.timedelta(minutes=MIN_EVENING - 2 * SPREAD)
    n = len(ids)
    first = sorted(rng.sample(ids, rng.randint((n + 1) // 2, n))) if n else []
    plan = Plan(day, on, off, first)
    if n < 2:
        return plan             # (one light can only be on or off: no changes to make)
    lit = set(first)
    t = on + datetime.timedelta(minutes=rng.randint(*GAP))
    while t <= off - datetime.timedelta(minutes=GAP[0]) and len(plan.changes) < MAX_CHANGES:
        if rng.random() < CHANCE:
            can_off, can_on = len(lit) > 1, len(lit) < n
            turn_off = can_off and (not can_on or rng.random() < 0.5)
            dev_id = rng.choice(sorted(lit) if turn_off else sorted(set(ids) - lit))
            (lit.discard if turn_off else lit.add)(dev_id)
            plan.changes.append((t, dev_id, not turn_off))
        t += datetime.timedelta(minutes=rng.randint(*GAP))
    return plan


class Presence:
    def __init__(self, manager):
        self.manager = manager
        self._last: dict[str, bool] = {}       # what the plan last asked of each light
        self._plans: dict[tuple, Plan] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- the setting ---------------------------------------------------------------------------

    @property
    def settings(self) -> dict:
        """The saved setting. The file can be edited by hand: anything that isn't right is the default."""
        s = self.manager.config["settings"].get("presence")
        s = {**DEFAULT, **s} if isinstance(s, dict) else dict(DEFAULT)
        for key in ("at", "off"):
            if not (isinstance(s[key], str) and TIME.fullmatch(s[key])):
                s[key] = DEFAULT[key]
        if s["when"] not in ("sunset", "time"):
            s["when"] = DEFAULT["when"]
        if s["lights"] not in ("room", "groups"):
            s["lights"] = "room"
        s["groups"] = [g for g in s["groups"] if isinstance(g, str)] if isinstance(s["groups"], list) else []
        s["on"] = bool(s["on"])
        return s

    @property
    def active(self) -> bool:
        return self.settings["on"]

    def configure(self, on, lights, groups, when, at, off, now: float | None = None) -> None:
        """Save the setting (checked first). Switching it on keeps how the lights look now, and
        stops what would change them; switching it off puts them back."""
        m = self.manager
        if lights not in ("room", "groups"):
            raise ValueError("Choose the room's lights or your groups")
        if when not in ("sunset", "time"):
            raise ValueError("Choose around sunset or a time")
        at, off = str(at or ""), str(off or "")
        if not TIME.fullmatch(at) or not TIME.fullmatch(off):
            raise ValueError("Times look like 23:30")
        names = []
        if lights == "groups":
            for name in groups if isinstance(groups, (list, tuple)) else []:
                found = m.find_group(name)
                if found is None:
                    raise ValueError("One of those groups is gone")
                if found not in names:
                    names.append(found)
            if not names:
                raise ValueError("Choose at least one group")
        was = self.active
        with self._lock:
            m.config["settings"]["presence"] = {"on": bool(on), "lights": lights, "groups": names,
                                               "when": when, "at": at, "off": off}
            m.config.save_soon()
        if on and not was:
            self._begin(now)
        elif was and not on:
            self._end()
        else:
            self._last = {}                 # (a new plan: the lights take what it asks of them)
            self._plans.clear()
            m._changed()
            if on:
                self.tick(now)

    def stop_now(self) -> None:
        """Stop (Home): the mode is off and the lights are as they were before it began."""
        s = self.settings
        if s["on"]:
            self.manager.config["settings"]["presence"] = {**s, "on": False}
            self.manager.config.save_soon()
            self._end()

    def _begin(self, now: float | None) -> None:
        m = self.manager
        m.cycle.cancel()
        m.wallpaper.cancel()
        m.focus.cancel()
        m.sleep.cancel()
        m.wake.cancel()
        with self._lock:
            ids = self.lights()
            looks = m.looks()
            m.config["settings"]["presence_before"] = {
                "looks": {i: looks[i] for i in ids if i in looks},
                "preset": m.config["settings"].get("active_preset", "")}
            m.config.save_soon()
            self._last, self._plans = {}, {}
        log.info("Someone's home: on, %d lights", len(ids))
        self._ensure_thread()
        self.tick(now)
        m._changed()

    def _end(self) -> None:
        m = self.manager
        with self._lock:
            before = m.config["settings"].get("presence_before")
            m.config["settings"]["presence_before"] = None
            m.config.save_soon()
            self._last, self._plans = {}, {}
        if isinstance(before, dict) and isinstance(before.get("looks"), dict):
            m.restore_looks(before["looks"], before.get("preset") or "")
        log.info("Someone's home: off, the lights are back as they were")
        m._changed()

    # ---- the plan ------------------------------------------------------------------------------

    def lights(self) -> list[str]:
        """The lights it plays with: the room's, or the groups'. Never the PC's own."""
        m, s = self.manager, self.settings
        with m.lock:
            if s["lights"] == "groups":
                ids = set()
                for name in s["groups"]:
                    try:
                        ids.update(m.group_lights(name))
                    except ValueError:      # (a group that was deleted)
                        pass
            else:
                ids = set(m.visible_ids())
            return sorted(i for i in ids if i in m.devices and m.devices[i].group != "pc")

    def plan(self, day: datetime.date) -> Plan:
        s, ids = self.settings, self.lights()
        sunset = self.manager.sun_times(datetime.datetime.combine(day, datetime.time()))[1]
        key = (day, tuple(sorted((k, str(v)) for k, v in s.items() if k != "on")), tuple(ids), sunset)
        if key not in self._plans:
            if len(self._plans) > 8:
                self._plans.clear()
            self._plans[key] = plan_for(day, s, sunset, ids)
        return self._plans[key]

    def current(self, at: datetime.datetime) -> tuple[Plan, bool]:
        """The evening that counts at `at`, and whether it is going on now. An evening that began
        yesterday goes on past midnight. When none is on: today's evening if it hasn't been yet,
        else tomorrow's."""
        today = at.date()
        for day in (today - datetime.timedelta(days=1), today):
            p = self.plan(day)
            if p.on <= at < p.off:
                return p, True
        p = self.plan(today)
        return (p, False) if at < p.on else (self.plan(today + datetime.timedelta(days=1)), False)

    def describe(self, now: float | None = None) -> dict:
        at = datetime.datetime.fromtimestamp(time.time() if now is None else now)
        s = self.settings
        out = {**s, "plan": None, "lit": 0}
        if not self.lights():
            return out
        p, going = self.current(at)
        day = "tomorrow" if p.day > at.date() else "tonight"
        out["plan"] = {"on": _clock(p.on), "off": _clock(p.off), "day": day, "changes": len(p.changes)}
        out["going"] = going
        out["lit"] = len(p.lit(at)) if going else 0
        return out

    # ---- keeping the lights to it -----------------------------------------------------------------

    def tick(self, now: float | None = None) -> bool:
        """Look at the clock: the lights that should change now do. True when any light was sent
        something."""
        m = self.manager
        with self._lock:
            if not self.active:
                return False
            if m.away.holding:              # (the lights are off because you locked the PC: when
                self._last = {}             # you're back they get what the plan asks again)
                return False
            at = datetime.datetime.fromtimestamp(time.time() if now is None else now)
            ids = self.lights()
            lit = self.current(at)[0].lit(at)
            patches = {}
            with m.lock:
                for dev_id in ids:
                    want = dev_id in lit
                    if self._last.get(dev_id) == want:
                        continue
                    self._last[dev_id] = want
                    st = m.states.get(dev_id)
                    if want:
                        patches[dev_id] = {"on": True, "mode": "color", "color": WARM, "brightness": BRIGHTNESS}
                    elif st is not None and st.on:
                        patches[dev_id] = {"on": False}
            if not patches:
                return False
            log.info("Someone's home: %d on, %d off", sum(1 for p in patches.values() if p["on"]),
                     sum(1 for p in patches.values() if not p["on"]))
        # (not saved: if Glowstage stops, the lights come back as they were saved)
        m._apply(patches, preset="", save=False)
        return True

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="presence", daemon=True)
            self._thread.start()
        self._wake.set()

    def start(self) -> None:
        """With the app: carry on if it was on when Glowstage stopped."""
        if self.active:
            self._ensure_thread()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Someone's home: a look at the clock failed")
            self._wake.wait(POLL)
            self._wake.clear()
