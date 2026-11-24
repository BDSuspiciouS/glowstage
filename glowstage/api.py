"""Methods the window's JavaScript calls through pywebview (window.pywebview.api.*)."""

from __future__ import annotations

import functools
import logging
import threading
import webbrowser

from . import winutil
from .manager import friendly_error
from .paths import APP_NAME, DONATE, REPO, VERSION

log = logging.getLogger(__name__)


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *args):
        try:
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
    def get_state(self):
        snap = self._m.snapshot()
        snap["settings"] = {**self._app.config["settings"], "autostart": winutil.autostart_enabled()}
        snap["about"] = {"name": APP_NAME, "version": VERSION, "repo": f"https://github.com/{REPO}",
                         "donate": [{"name": k, "url": v} for k, v in DONATE.items() if v]}
        return snap

    @_safe
    def open_link(self, url):
        """Open the project page or a donation page in the browser (only those, nothing else)."""
        allowed = {f"https://github.com/{REPO}", f"https://github.com/{REPO}/releases", *DONATE.values()}
        if url in allowed and url:
            webbrowser.open(url)

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
    def pair_nanoleaf(self, dev_id):
        self._m.pair_nanoleaf(dev_id)

    @_safe
    def add_device(self, kind, host):
        return self._m.add_manual(kind, host)

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
    def openrgb_stop(self):
        self._m.openrgb.kill()

    @_safe
    def set_openrgb_option(self, key, value):
        if key not in ("auto_launch", "path"):
            raise ValueError(f"Unknown OpenRGB option {key}")
        self._app.config["openrgb"][key] = value
        self._app.config.save_soon()

    @_safe
    def set_setting(self, key, value):
        if key == "autostart":
            winutil.set_autostart(bool(value))
            return
        if key not in self._app.config["settings"]:
            raise ValueError(f"Unknown setting {key}")
        self._app.config["settings"][key] = value
        self._app.config.save_soon()

    @_safe
    def hide_window(self):
        self._app.hide_window()

    @_safe
    def restart_app(self):
        # Answer the page first; the window closes a moment later.
        threading.Timer(0.3, self._app.restart, kwargs={"show": True}).start()
