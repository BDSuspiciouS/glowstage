"""System tray icon: open the window, switch everything on/off, the light show, presets, the
sleep timer, quit."""

from __future__ import annotations

import threading
import time

import pystray
from pystray import Menu, MenuItem

from .icon import icon_image
from .paths import APP_NAME
from .timer import CHOICES


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = pystray.Icon(APP_NAME, icon_image(64), APP_NAME, menu=Menu(self._items))

    def _items(self):
        m = self.app.manager
        yield MenuItem(f"Open {APP_NAME}", lambda: self.app.show_window(), default=True)
        yield Menu.SEPARATOR
        yield MenuItem("All lights on", lambda: m.set_power_all(True))
        yield MenuItem("All lights off", lambda: m.set_power_all(False))
        yield MenuItem("Stop the light show" if m.show_running() else "Start the light show", lambda: m.toggle_show())
        presets = m.preset_names()
        if presets:
            active = m.config["settings"].get("active_preset", "")
            yield MenuItem("Presets", Menu(*(MenuItem(name, self._preset_action(name), radio=True,
                                                      checked=lambda item, n=name: n == active)
                                             for name in presets)))
        yield MenuItem("Sleep timer", Menu(*self._sleep_items()))
        yield Menu.SEPARATOR
        yield MenuItem(f"Restart {APP_NAME}", lambda: self.app.restart())
        yield MenuItem("Quit", lambda: self.app.quit())

    def _sleep_items(self):
        sleep = self.app.manager.sleep
        if sleep.active:
            yield MenuItem(f"Off at {time.strftime('%H:%M', time.localtime(sleep.until))}: cancel",
                           lambda: sleep.cancel())
            yield Menu.SEPARATOR
        for minutes in CHOICES:
            label = {60: "1 hour", 90: "1½ hours"}.get(minutes, f"{minutes} minutes")
            yield MenuItem(f"Fade out in {label}", self._sleep_action(minutes))

    def _sleep_action(self, minutes: int):
        def start():
            try:
                self.app.manager.sleep.start(minutes)
            except ValueError:
                pass   # no lights on: nothing to fade
        return start

    def _preset_action(self, name: str):
        return lambda: self.app.manager.apply_preset(name)  # pystray wants 0-2 positional args

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def refresh(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
