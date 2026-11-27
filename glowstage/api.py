"""Methods the window's JavaScript calls through pywebview (window.pywebview.api.*)."""

from __future__ import annotations

import datetime
import functools
import logging
import threading
import webbrowser

from . import genre_install, online, webfix, winutil
from .devices import openrgb_install as installer
from .manager import friendly_error
from .paths import APP_NAME, DONATE, REPO, VERSION

log = logging.getLogger(__name__)


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
            with webfix.track(fn.__name__):
                result = fn(self, *args)
            return {"ok": True, "result": result}
        except Exception as e:
            log.warning("api %s failed: %s", fn.__name__, e)
            return {"ok": False, "error": friendly_error(e) if isinstance(e, OSError) else str(e)}
    return wrapper


class Api:
    def __init__(self, app):
        self._app = app  # underscore: pywebview must not expose this to JS

    @property
    def _m(self):
        return self._app.manager

    @_safe
    def get_state(self, since=None):
        """Everything the window shows. With `since` (the revision the window has), and nothing
        changed since, only the parts that change all the time: a small answer every second
        instead of every light, preset and effect."""
        extra = {"settings": {**self._app.config["settings"], "autostart": winutil.autostart_enabled()},
                 "hotkeysTaken": list(self._app.hotkeys.failed),
                 "about": {"name": APP_NAME, "version": VERSION, "repo": f"https://github.com/{REPO}",
                           "donate": self._app.online.donate_links()},
                 "update": self._app.online.update(),
                 "askSupport": self._ask_support()}
        if since is not None and since == self._m.revision:
            return {**self._m.live_status(), **extra, "same": True}
        return {**self._m.snapshot(), **extra}

    @_safe
    def get_live(self):
        """What each light running an app effect shows right now (the stage view polls this)."""
        return self._m.engine.live()

    @_safe
    def set_stage(self, dev_id, span):
        self._m.set_stage(dev_id, span)

    @_safe
    def reset_stage(self):
        self._m.reset_stage()

    @_safe
    def set_schedules(self, items):
        self._m.set_schedules(items)

    @_safe
    def set_show_excluded(self, ids):
        self._m.set_show_excluded(ids)

    @_safe
    def set_show_sync(self, ms):
        self._m.set_show_sync(ms)

    def _ask_support(self) -> bool:
        """Show the support card? Once, after five days of use (and again a month after "Later")."""
        s = self._app.config["settings"]
        asked, first = s.get("support_asked", ""), s.get("first_run", "")
        today = datetime.date.today().isoformat()
        if asked in ("never", "done") or not first or not self._app.online.donate_links():
            return False
        if asked.startswith("later:"):
            return today >= asked[6:]
        return (datetime.date.today() - datetime.date.fromisoformat(first)).days >= 5

    @_safe
    def support_answer(self, answer):
        """The support card: "later" (ask again in a month), "never", or "done" (they gave)."""
        s = self._app.config["settings"]
        if answer == "later":
            s["support_asked"] = "later:" + (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        elif answer in ("never", "done"):
            s["support_asked"] = answer
        self._app.config.save_soon()

    @_safe
    def open_link(self, url):
        """Open the project page, its releases, a support page or the enhanced genre AI's model
        and license pages in the browser (only those, nothing else)."""
        url = str(url or "")
        fixed = {f"https://github.com/{REPO}", f"https://github.com/{REPO}/releases",
                 f"https://github.com/{REPO}/issues/new/choose", *DONATE.values(),
                 genre_install.INFO["aboutUrl"], genre_install.INFO["licenseUrl"]}
        update = self._app.online.update() or {}
        if url and (url in fixed or url == update.get("url")
                    or (online.allowed(url) and any(d["url"] == url for d in self._app.online.donate_links()))):
            webbrowser.open(url)
            return
        raise ValueError("That link isn't one Glowstage opens")

    @_safe
    def update(self, ids, patch):
        self._m.update(ids or None, patch)

    @_safe
    def native_options(self, dev_id):
        return self._m.native_options(dev_id)

    @_safe
    def apply_preset(self, name):
        self._m.apply_preset(name)

    @_safe
    def save_preset(self, name):
        self._m.save_preset(name)

    @_safe
    def delete_preset(self, name):
        self._m.delete_preset(name)

    @_safe
    def pair(self, dev_id):
        self._m.pair(dev_id)

    @_safe
    def pair_nanoleaf(self, dev_id):   # older UI name
        self._m.pair(dev_id)

    @_safe
    def search(self):
        self._m.search()

    @_safe
    def set_dmx_fixtures(self, hub_id, fixtures):
        self._m.set_dmx_fixtures(hub_id, fixtures)

    @_safe
    def add_device(self, kind, values):
        return self._m.add_manual(kind, values)

    @_safe
    def rename(self, dev_id, name):
        self._m.rename(dev_id, name)

    @_safe
    def set_hidden(self, dev_id, hidden):
        self._m.set_hidden(dev_id, bool(hidden))

    @_safe
    def forget(self, dev_id):
        self._m.forget(dev_id)

    @_safe
    def resize_zone(self, dev_id, index, size):
        self._m.resize_zone(dev_id, int(index), int(size))

    @_safe
    def openrgb_restart(self, elevated):
        self._app.config["openrgb"]["elevated"] = bool(elevated)
        self._app.config.save_soon()
        threading.Thread(target=self._m.openrgb.restart, args=(bool(elevated),), daemon=True).start()

    @_safe
    def openrgb_install(self):
        """Download OpenRGB from its makers (the user pressed the button), then start it."""
        provider = self._m.openrgb
        elevated = bool(self._app.config["openrgb"].get("elevated"))
        installer.start(on_done=lambda: provider.restart(elevated))

    @_safe
    def genre_ai_install(self):
        """Download the enhanced genre AI from its makers (the user pressed the button); the light
        show switches to it at its next look."""
        genre_install.start(on_done=self._m.engine.show.genre.reload)

    @_safe
    def genre_ai_remove(self):
        """Back to the built-in genre AI."""
        genre_install.remove(on_done=self._m.engine.show.genre.reload)

    @_safe
    def openrgb_stop(self):
        self._m.openrgb.kill()

    @_safe
    def set_openrgb_option(self, key, value):
        if key not in ("auto_launch", "path"):
            raise ValueError(f"Unknown OpenRGB option {key}")
        self._app.config["openrgb"][key] = value
        self._app.config.save_soon()

    @_safe
    def check_online(self):
        self._app.online.check_now()

    @_safe
    def set_setting(self, key, value):
        if key == "autostart":
            winutil.set_autostart(bool(value))
            return
        if key not in self._app.config["settings"]:
            raise ValueError(f"Unknown setting {key}")
        if key in ("auto_game", "auto_video"):   # "", "screen" or "preset:<name>"
            value = str(value or "")
            if value not in ("", "screen") and not (value.startswith("preset:")
                                                    and value[7:] in self._app.manager.preset_names()):
                raise ValueError("Choose a look from the list")
        self._app.config["settings"][key] = value
        self._app.config.save_soon()
        if key == "hotkeys":
            self._app.hotkeys.set_enabled(bool(value))

    @_safe
    def games_connect(self):
        """Game lighting: put the small file in CS2's cfg folder and start listening."""
        self._app.manager.games.connect()

    @_safe
    def games_disconnect(self):
        self._app.manager.games.disconnect()

    @_safe
    def stop_automation(self):
        """"Put my lights back": undo the automatic look (game, video, music) now."""
        self._app.manager.automations.release()

    @_safe
    def hide_window(self):
        self._app.hide_window()

    @_safe
    def restart_app(self):
        # Answer the page first; the window closes a moment later.
        threading.Timer(0.3, self._app.restart, kwargs={"show": True}).start()
