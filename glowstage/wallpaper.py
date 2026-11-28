"""Your lights in the colors of your desktop wallpaper, and (for a wallpaper that changes by itself:
a slideshow, Windows Spotlight) following it when it changes.

Which picture is on the desktop: Windows keeps a copy of what it really shows in
%APPDATA%\\Microsoft\\Windows\\Themes\\TranscodedWallpaper (no file extension). That is the one to
read, because it is also right for a slideshow and for Spotlight, where the wallpaper setting only
names the folder or nothing at all. When it isn't there, the path Windows gives for the wallpaper
setting does. A plain color background has no picture: its color is in the registry, and every light
gets that color.

Like the presets in turn (cycle.py) the follow mode is a "this evening" thing: nothing of it is
saved, so after a restart it is off. It stops by itself when you take over (a light, a preset,
switching on or off, from anywhere), which it sees by comparing the lights with how they looked
right after its own last change. The sleep timer, a wake-up or presets in turn starting stop it
too. An automatic look (a game, a video, music, an app, a call) wins while it lasts: a change of
wallpaper in the meantime is picked up when that look has ended.

Every Windows lookup is a small function of its own (_transcoded_path, _desktop_setting_path,
_background_rgb), so tests put files of their own in its place and never touch the real desktop.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from PIL import Image

from . import palette
from .colors import rgb_to_hex
from .cycle import _same

log = logging.getLogger(__name__)

LOOK_EVERY = 15.0       # seconds between two looks at whether the wallpaper changed
POLL = 1.0              # seconds between two looks at whether you took over
NAME = "My wallpaper"
TOO_DARK = "Your wallpaper is too dark to take colors from"
NONE_FOUND = "Glowstage can't find your wallpaper"


# ---- the Windows lookups (each one replaceable in tests) ------------------------------------------

def _transcoded_path() -> str:
    """The copy of the wallpaper Windows actually shows."""
    base = os.environ.get("APPDATA")
    if not base:
        return ""
    return os.path.join(base, "Microsoft", "Windows", "Themes", "TranscodedWallpaper")


def _desktop_setting_path() -> str:
    """The wallpaper setting (SPI_GETDESKWALLPAPER): a path, or "" for a plain color."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.user32.SystemParametersInfoW(0x0073, len(buf), buf, 0)
        return buf.value
    except Exception:
        return ""


def _background_rgb() -> str:
    """HKCU\\Control Panel\\Colors\\Background: "R G B", the color behind (or instead of) the picture."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Colors") as key:
            return str(winreg.QueryValueEx(key, "Background")[0])
    except Exception:
        return ""


# ---- which wallpaper is on -------------------------------------------------------------------------

def _usable(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def picture_path() -> str:
    """The picture on the desktop now, "" when there is none (a plain color)."""
    for path in (_transcoded_path(), _desktop_setting_path()):
        if _usable(path):
            return path
    return ""


def plain_color() -> str | None:
    """The color of a plain background as hex, None when the registry doesn't hold one."""
    try:
        r, g, b = (int(p) for p in _background_rgb().split())
    except ValueError:
        return None
    if not all(0 <= c <= 255 for c in (r, g, b)):
        return None
    return rgb_to_hex((r, g, b))


def stamp() -> tuple:
    """Something that changes when the wallpaper does, cheap enough to look at every few seconds:
    (path, size, time of change) of the picture, or ("color", the color) for a plain background."""
    path = picture_path()
    if path:
        try:
            st = os.stat(path)
            return (path, st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    return ("color", plain_color() or "")


def look() -> tuple[list[str], Image.Image]:
    """The colors to light the room with, and a small picture of the wallpaper for the window."""
    path = picture_path()
    if path:
        img = palette.read(path)
        return palette.main_colors(img), img
    color = plain_color()
    if not color:
        raise ValueError(NONE_FOUND)
    if max(int(color[i:i + 2], 16) for i in (1, 3, 5)) < 0.1 * 255:
        raise ValueError(TOO_DARK)
    return [color], Image.new("RGB", (palette.THUMB, palette.THUMB * 2 // 3), color)


# ---- following it ----------------------------------------------------------------------------------

class WallpaperFollow:
    def __init__(self, manager):
        self.manager = manager
        self.since = 0.0                    # time.time() it started; 0 when it doesn't run
        self.next_look = 0.0
        self._stamp: tuple | None = None    # the wallpaper the lights have taken the colors of
        self._set: dict | None = None       # how the lights looked right after our last change
        self._preset = ""
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self.since > 0

    def describe(self) -> dict | None:
        with self._lock:
            return {"since": self.since} if self.active else None

    def start(self, now: float | None = None) -> None:
        """Follow from now on. The lights are taken as they are (they show the colors of this
        wallpaper already, or the next change of it fixes that): starting doesn't change them."""
        m = self.manager
        if not m.any_on():
            raise ValueError("Switch a light on first, then try again")
        now = time.time() if now is None else now
        m.cycle.cancel()                # these don't both decide what the lights look like
        m.focus.cancel()
        with self._lock:
            self.since, self.next_look = now, now + LOOK_EVERY
            self._stamp = stamp()
            self._remember_look()
            log.info("Following the wallpaper")
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wallpaper-follow", daemon=True)
            self._thread.start()
        self._wake.set()
        m._changed()

    def cancel(self) -> None:
        """Stop: the lights stay as they are now."""
        with self._lock:
            if self.active:
                self._end("stopped")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _remember_look(self) -> None:
        m = self.manager
        self._set = m.looks()           # right after, so our own change doesn't look like yours
        self._preset = m.config["settings"].get("active_preset", "")

    def tick(self, now: float) -> bool:
        """One look: did you take over (then it's over), did the wallpaper change? False once it
        doesn't run."""
        m = self.manager
        looks = m.looks()
        preset = m.config["settings"].get("active_preset", "")
        with self._lock:
            if not self.active:
                return False
            if m.automations.active or m.away.holding:
                return True             # it wins (or you're away), and what changes is not you; the wallpaper waits
            if (self._set is not None and not _same(looks, self._set)) or preset != self._preset:
                self._end("you changed the lights")     # (switching every light off counts)
                return False
            if now >= self.next_look:
                self.next_look = now + LOOK_EVERY
                self._follow()
            return True

    def _follow(self) -> None:
        seen = stamp()
        if seen == self._stamp:
            return
        self._stamp = seen              # (also when reading fails: the next try is for the next change)
        try:
            colors, _ = look()
        except ValueError as e:
            log.info("Following the wallpaper: %s", e)
            return
        lit = self.manager.light_from_colors(colors)
        if lit:
            log.info("Following the wallpaper: %d lights took its colors", lit)
            self._remember_look()
            self.manager._changed()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(POLL)
            self._wake.clear()
            try:
                if not self.tick(time.time()):
                    return
            except Exception:
                log.exception("Following the wallpaper: a step failed")

    def _end(self, why: str) -> None:
        log.info("Following the wallpaper over: %s", why)
        self.since, self.next_look, self._stamp, self._set = 0.0, 0.0, None, None
        self.manager._changed()
