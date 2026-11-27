"""What's new: a card on Home, once per version, after an update (Update now doesn't show GitHub's
release notes). Each line can take you to the new thing: "go" is what the window opens.

A new install doesn't get it: the welcome tour shows the app. Someone who updates does, until
the card's "Got it" marks the version as seen.
"""

from __future__ import annotations

from .paths import VERSION

# go: "setup:<section>" (that part of Setup: automatic, phone, schedule, backups), "effect:<id>"
# (the effects panel, at that look), "sleep" (the sleep timer), "search" (the quick search).
GOES = ("sleep", "search", "setup:automatic", "setup:phone", "setup:schedule", "setup:backups")

NOTES: dict[str, list[dict]] = {
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


def card(settings: dict, version: str = VERSION) -> dict | None:
    """The card to show now, or None: {"version", "items"}."""
    items = NOTES.get(version)
    if not items or settings.get("seen_version") == version:
        return None
    return {"version": version, "items": items}


def first_run(settings: dict, version: str = VERSION) -> bool:
    """A new install (the welcome tour still to come): nothing is new to it. True when that
    changed the settings."""
    if not settings.get("seen_version") and not settings.get("setup_done"):
        settings["seen_version"] = version
        return True
    return False
