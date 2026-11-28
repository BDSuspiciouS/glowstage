"""What's new: a card on Home, once per version, after an update (Update now doesn't show GitHub's
release notes). Each line can take you to the new thing: "go" is what the window opens.

A new install doesn't get it: the welcome tour shows the app. Someone who updates does, until
the card's "Got it" marks the version as seen.
"""

from __future__ import annotations

import re

from .paths import VERSION

# go: "setup:<section>" (that part of Setup: automatic, phone, schedule, backups), "effect:<id>"
# (the effects panel, at that look), "sleep" (the sleep timer), "search" (the quick search).
GOES = ("sleep", "search", "setup:automatic", "setup:phone", "setup:schedule", "setup:backups")

NOTES: dict[str, list[dict]] = {
    "0.9.8": [
        {"title": "PC load", "go": "effect:pc_load",
         "text": "Your lights fill up like a bar as your PC gets busy, and the graphics card shows its own load."},
        {"title": "Gradient", "go": "effect:gradient",
         "text": "Two colors blended along each light, through the colors between them, or one blend across your whole setup."},
        {"title": "Use from other apps", "go": "setup:phone",
         "text": "Stream Deck, AutoHotkey and Home Assistant can switch your lights, pick a preset or start the light show with a link."},
        {"title": "Screen sync on any display", "go": "effect:screen",
         "text": "Follow your main display, another one, or the one where your game or video is full screen."},
    ],
    "0.9.7": [
        {"title": "Quick search", "go": "search",
         "text": "Press Ctrl+K and type a few letters: switch on a preset, open a light, jump to any setting. Press ? for every shortcut."},
        {"title": "Phone remote", "go": "setup:phone",
         "text": "Scan a code and control every light from your phone, on the same Wi-Fi."},
        {"title": "Wake up with a sunrise", "go": "setup:schedule",
         "text": "Your room's lights come up slowly before you get up, and schedules can follow sunset all year."},
        {"title": "Screen + sound", "go": "setup:automatic",
         "text": "In any game your lights follow the screen and flash with every shot or explosion, on the side it came from."},
        {"title": "Typing ripples", "go": "effect:typing",
         "text": "Every key you press lights up and sends a ripple across your keyboard."},
        {"title": "A look per app, and for calls", "go": "setup:automatic",
         "text": "Focus while Photoshop is open, a soft light while you're on a call: back to your own look afterwards."},
        {"title": "Backups", "go": "setup:backups",
         "text": "A copy of your presets and settings for each day, and a file to take them to another PC."},
    ],
}


LIMIT = 7       # lines on the card: more and it stops being a glance


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def card(settings: dict, version: str = VERSION) -> dict | None:
    """The card to show now, or None: {"version", "items"}, and "since" when it covers more than
    one version. Someone who skipped a version (0.9.6 straight to 0.9.8) sees what's new in both,
    the newest first."""
    seen = settings.get("seen_version") or ""
    if seen == version:
        return None
    if seen:
        versions = sorted((v for v in NOTES if _key(seen) < _key(v) <= _key(version)), key=_key, reverse=True)
    else:   # from before versions were noted: only this one's news
        versions = [version] if version in NOTES else []
    items = [item for v in versions for item in NOTES[v]][:LIMIT]
    if not items:
        return None
    out = {"version": version, "items": items}
    if len(versions) > 1:
        out["since"] = seen
    return out


def first_run(settings: dict, version: str = VERSION) -> bool:
    """A new install (the welcome tour still to come): nothing is new to it. True when that
    changed the settings."""
    if not settings.get("seen_version") and not settings.get("setup_done"):
        settings["seen_version"] = version
        return True
    return False
