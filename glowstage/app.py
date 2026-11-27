"""Wires the config, device manager, window and tray icon together."""

from __future__ import annotations

import datetime
import faulthandler
import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler

import webview

from . import webfix, whatsnew, winutil
from .api import Api
from .config import Config
from .hotkeys import Hotkeys
from .online import Online
from .remote import Remote
from .updater import Updater
from .icon import icon_file
from .manager import DeviceManager
from .paths import APP_DIR, APP_NAME, INSTALL_DIR, UI_DIR, VERSION, launch_command
from .tray import Tray

log = logging.getLogger(__name__)


class App:
    def __init__(self, start_minimized: bool, autostart: bool = False):
        self.config = Config()
        self.manager = DeviceManager(self.config)
        self.manager.force_on = autostart and bool(self.config["settings"].get("lights_on_at_startup", True))
        self.tray = Tray(self)
        self.hotkeys = Hotkeys(self.manager)
        self.online = Online(self.config, self.manager._changed)
        self.updater = Updater(self.manager._changed, self.quit)
        self.remote = Remote(self.manager, self.config, self.manager._changed)
        # ("--updated" / "--update-failed": the update that just ran, for a word in the window)
        self.update_result = next((a[2:] for a in sys.argv if a in ("--updated", "--update-failed")), "")
        settings = self.config["settings"]
        if not settings.get("first_run"):
            settings["first_run"] = datetime.date.today().isoformat()
            self.config.save_soon()
        if whatsnew.first_run(settings):
            self.config.save_soon()
        self.window: webview.Window | None = None
        self.start_minimized = start_minimized or bool(self.config["settings"].get("start_minimized"))
        self._quitting = False
        self._preset_names: tuple = ()
        self._watch_stop = threading.Event()
        self.manager.listeners.append(self._on_change)

    def run(self) -> None:
        winutil.set_app_id()
        winutil.watch_windows(self._on_windows_shutdown, self.manager.away.set)
        self.manager.start()
        self.tray.start()
        self.hotkeys.set_enabled(bool(self.config["settings"].get("hotkeys")))
        self.online.start()
        self.remote.start()
        # Straight from the files (file://), not through pywebview's own web server: that one listens
        # on a fixed port, and WebView2 kept its pages in a cache, so after an update the window
        # could get an old index.html with the new scripts, and stay empty.
        self.window = webview.create_window(
            APP_NAME, url=(UI_DIR / "index.html").as_uri(), js_api=Api(self),
            width=1240, height=820, min_size=(980, 680), background_color="#16171b",
            hidden=self.start_minimized,
        )
        self.window.events.closing += self._on_closing
        threading.Thread(target=webfix.watch, args=(self.window, self._watch_stop), name="watchdog", daemon=True).start()
        webview.start(gui="edgechromium", icon=str(icon_file(APP_DIR)), private_mode=False,
                      storage_path=str(APP_DIR / "webview"))
        self._watch_stop.set()
        self.hotkeys.set_enabled(False)
        self.online.stop()
        self.remote.stop()
        self.tray.stop()
        self.manager.stop()

    def _on_change(self) -> None:
        # The tray menu shows the presets, the one that's on and whether the show runs.
        sig = (self.manager.preset_names(), self.config["settings"].get("active_preset", ""),
               self.manager.show_running(), self.manager.sleep.until, self.manager.can_undo())
        if sig != self._preset_names:
            self._preset_names = sig
            self.tray.refresh()

    def _on_windows_shutdown(self) -> None:
        self._quitting = True  # let the window close instead of hiding to the tray
        # If this ever takes too long, write down where every thread is so it can be fixed.
        trace = open(APP_DIR / "shutdown-trace.txt", "w", encoding="utf-8")
        faulthandler.dump_traceback_later(6, exit=False, file=trace)
        try:
            settings = self.config["settings"]
            pc = settings.get("pc_lights_off_at_shutdown", True)
            room = settings.get("room_lights_off_at_shutdown", True)
            if pc or room:
                self.manager.lights_off_now(pc=pc, room=room)
            self.config.save_now()
        finally:
            faulthandler.cancel_dump_traceback_later()
            trace.close()

    def _on_closing(self):
        if self._quitting or winutil.shutting_down() or not self.config["settings"].get("close_to_tray", True):
            self._quitting = True
            return True
        threading.Thread(target=self.hide_window, daemon=True).start()
        return False  # keep running in the tray

    def show_window(self) -> None:
        if self.window:
            self.window.show()
            self.window.restore()
            # Windows may refuse to activate a background app's window; a brief topmost
            # flip still raises it above everything else.
            self.window.on_top = True
            self.window.on_top = False

    def hide_window(self) -> None:
        if self.window:
            self.window.hide()

    def quit(self) -> None:
        self._quitting = True
        if self.window:
            self.window.destroy()

    def restart(self, show: bool = False) -> None:
        """Start a fresh copy that waits for this one to close, then close this one."""
        log.info("Restarting")
        args = launch_command("--after", str(os.getpid()), *([] if show else ["--minimized"]))
        subprocess.Popen(args, cwd=str(INSTALL_DIR),
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.quit()


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(APP_DIR / "glowstage.log", maxBytes=1_000_000,
                                                           backupCount=2, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("pywebview").setLevel(logging.WARNING)
    # pythonw has no console: make sure crashes end up in the log instead of vanishing.
    sys.excepthook = lambda *exc: log.critical("Uncaught error", exc_info=exc)
    threading.excepthook = lambda a: log.critical("Uncaught error in thread %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("%s %s starting (%s)", APP_NAME, VERSION, " ".join(sys.argv[1:]) or "no options")
    webfix.enable_crash_log()
    if "--install-update" in sys.argv:   # the new version, started from the update folder: swap and go
        from . import updater
        updater.run_install(sys.argv)
        return
    if "--after" in sys.argv:  # restarting: let the old copy finish closing first
        winutil.wait_for_exit(int(sys.argv[sys.argv.index("--after") + 1]), timeout=20)

    holder: dict[str, App] = {}
    if not winutil.claim_single_instance(lambda: holder["app"].show_window() if "app" in holder else None):
        log.info("Already running; asked the running copy to show its window")
        return
    winutil.migrate_legacy()
    webfix.install()
    threading.Thread(target=winutil.ensure_shortcut, name="shortcut", daemon=True).start()
    from . import updater
    threading.Thread(target=updater.clean_up, name="update-cleanup", daemon=True).start()
    app = holder["app"] = App(start_minimized="--minimized" in sys.argv, autostart="--autostart" in sys.argv)
    try:
        app.run()
    except Exception:
        log.critical("%s stopped because of an error", APP_NAME, exc_info=True)
        raise
    log.info("%s closed", APP_NAME)
