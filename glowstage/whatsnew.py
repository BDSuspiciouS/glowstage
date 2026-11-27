"""What's new: a card on Home, once per version, after an update (Update now doesn't show GitHub's
release notes). Each line can take you to the new thing: "go" is what the window opens.

A new install doesn't get it: the welcome tour shows the app. Someone who updates does, until
the card's "Got it" marks the version as seen.
"""

from __future__ import annotations

from .paths import VERSION

# go: "setup:automatic" / "setup:phone" (that part of Setup), "effect:<id>" (the effects panel, at that look),
# "sleep" (the sleep timer).
NOTES: dict[str, list[dict]] = {
    "0.9.7": [
        {"title": "Phone remote", "go": "setup:phone",
         "text": "Scan a code and control your lights from your phone: on and off, brightness, presets, the sleep timer."},
        {"title": "Screen + sound", "go": "setup:automatic",
         "text": "In any game your lights follow the screen and flash with every shot or explosion, on the side it came from."},
        {"title": "Typing ripples", "go": "effect:typing",
         "text": "Every key you press lights up and sends a ripple across your keyboard."},
        {"title": "A look per app", "go": "setup:automatic",
         "text": "Focus while Photoshop is open, the light show with your DJ app: back to your own look when you close it."},
        {"title": "Sleep timer", "go": "sleep",
         "text": "At bedtime your lights fade out like a sunset and switch off."},
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
