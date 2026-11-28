"""Methods the window's JavaScript calls through pywebview (window.pywebview.api.*)."""

from __future__ import annotations

import datetime
import functools
import logging
import re
import threading
import webbrowser
from pathlib import Path

import webview

from . import automations, backups, diagnostics, genre_install, online, palette, screen, show, wallpaper, webfix, whatsnew, winutil
from .devices import openrgb_install as installer
from .manager import friendly_error
from .paths import APP_NAME, DONATE, REPO, VERSION
from .away import SCOPES
from .sun import SUN

log = logging.getLogger(__name__)

FILE_TYPES = ("Glowstage settings (*.json)", "All files (*.*)")
PICTURE_TYPES = ("Pictures (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif)", "All files (*.*)")


def _picked(result) -> str:
    """The one path a file dialog gave ("" when cancelled): a tuple or a string, by dialog."""
    if isinstance(result, (tuple, list)):
        return str(result[0]) if result else ""
    return str(result or "")


def _documents() -> str:
    docs = Path.home() / "Documents"
    return str(docs if docs.is_dir() else Path.home())


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
        self._picked_file = None    # the settings file chosen to load (Setup → Backups)

    @property
    def _m(self):
        return self._app.manager

    @_safe
    def get_state(self, since=None):
        """Everything the window shows. With `since` (the revision the window has), and nothing
        changed since, only the parts that change all the time: a small answer every second
        instead of every light, preset and effect."""
        extra = {"settings": {**self._app.config["settings"], "autostart": winutil.autostart_enabled()},
                 "hotkeysTaken": [{"id": a, "label": self._app.hotkeys.label(a), "key": k}
                                  for a, k in self._app.hotkeys.taken.items()],
                 "hotkeyKeys": [{"id": a["id"], "label": a["label"], "key": a["key"]}
                                for a in self._app.hotkeys.actions() if a["key"]],
                 "about": {"name": APP_NAME, "version": VERSION, "repo": f"https://github.com/{REPO}",
                           "donate": self._app.online.donate_links()},
                 "update": self._app.online.update(),
                 "updating": self._app.updater.describe(),
                 "updateResult": self._app.update_result,
                 "recovered": self._app.config.recovered,
                 "canUndo": self._m.can_undo(),
                 "sun": SUN.describe(),
                 "sleep": self._m.sleep.describe(),
                 "wake": self._m.wake.describe(),
                 "cycle": self._m.cycle.describe(),
                 "focus": self._m.focus.describe(),
                 "night": self._m.night.describe(),
                 "wallpaper": self._m.wallpaper.describe(),
                 "askSupport": self._ask_support(),
                 "whatsNew": whatsnew.card(self._app.config["settings"]),
                 "remote": self._app.remote.describe()}
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
    def start_sleep_timer(self, minutes):
        """The lights that are on fade out and switch off after `minutes`."""
        self._m.sleep.start(minutes)

    @_safe
    def cancel_sleep_timer(self):
        self._m.sleep.cancel()

    @_safe
    def start_cycle(self, names, minutes, shuffle=False):
        """Presets in turn: the first one now, then the next every `minutes` (cycle.py)."""
        self._m.remember()      # (the first one is a change by hand: Undo goes back to before it)
        self._m.cycle.start(names, minutes, shuffle)

    @_safe
    def stop_cycle(self):
        self._m.cycle.cancel()

    @_safe
    def start_focus(self, work, rest, rounds, look=""):
        """Focus timer: work, a break the lights show in green, a few rounds (focus.py)."""
        self._m.remember()      # (the work light is a change by hand: Undo goes back to before it)
        self._m.focus.start(work, rest, rounds, look)

    @_safe
    def pause_automatic(self, minutes):
        """No automatic look for an hour, three, or until tomorrow ("tomorrow"): when it's over."""
        return self._m.automations.pause(minutes)

    @_safe
    def resume_automatic(self):
        self._m.automations.resume()

    @_safe
    def set_night_limit(self, on, start, end, top):
        """The night limit: from `start` to `end` no light above `top` percent (night.py)."""
        self._m.night.configure(on, start, end, top)

    @_safe
    def skip_night_limit(self):
        """"Not tonight": no night limit until the morning."""
        self._m.night.skip_tonight()

    @_safe
    def skip_focus(self):
        self._m.focus.skip()

    @_safe
    def stop_focus(self):
        self._m.focus.cancel()

    @_safe
    def cancel_wake(self):
        """Stop the wake-up light: its lights go back to how they were before it started."""
        self._m.wake.cancel()

    @_safe
    def set_app_looks(self, items):
        self._m.set_app_looks(items)

    @_safe
    def open_apps(self):
        """The apps open right now, to pick one for Setup → Automatic → Apps."""
        return automations.open_apps()

    @_safe
    def set_show_excluded(self, ids):
        self._m.set_show_excluded(ids)

    @_safe
    def set_show_colors(self, colors):
        """The light show in your own colors: 2 to 5 as #rrggbb, or none to let the music pick them."""
        self._app.config["settings"]["show_colors"] = show.clean_show_colors(colors)
        self._app.config.save_soon()

    @_safe
    def set_show_sync(self, ms):
        self._m.set_show_sync(ms)

    @_safe
    def show_inputs(self):
        """The PC's microphones for "Listen to" on the Light show page (listing opens none of them)."""
        return self._m.show_inputs()

    @_safe
    def set_show_input(self, value):
        """What the light show listens to: "" (what the PC plays), "default" or a microphone's id."""
        self._m.set_show_input(value)

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
    def remote_set(self, on):
        """Setup → Phone remote: listen on the home network for the phone page, or stop."""
        self._app.remote.set_on(bool(on))

    @_safe
    def remote_new_link(self):
        self._app.remote.new_link()

    @_safe
    def remote_qr(self):
        """The phone remote's link as a QR code (SVG)."""
        return self._app.remote.qr_svg()

    @_safe
    def diagnostics(self):
        """Report a problem: the summary that goes with it, to read before sending."""
        return diagnostics.report(self._app)

    @_safe
    def report_problem(self):
        """A new GitHub issue with the summary filled in (the browser shows it before it's sent)."""
        webbrowser.open(diagnostics.issue_url(diagnostics.report(self._app)))

    @_safe
    def list_backups(self):
        """Setup → Backups: the copies of the settings there are, newest first."""
        return backups.listing(self._app.config.path)

    @_safe
    def restore_backup(self, name):
        """Bring back one of the copies: Glowstage restarts with it (what's here now is kept)."""
        path = backups.find(self._app.config.path, name)
        data = backups.read(path) if path else None
        if not data:
            raise ValueError("That copy can't be read")
        self._load(data)

    @_safe
    def export_settings(self):
        """Save the presets, lights and settings to a file, to take to another PC ("" if cancelled)."""
        path = _picked(self._app.window.create_file_dialog(
            webview.FileDialog.SAVE, directory=_documents(), file_types=FILE_TYPES,
            save_filename=f"Glowstage settings {datetime.date.today().isoformat()}.json"))
        if not path:
            return ""
        Path(path).write_text(self._app.config.text(), encoding="utf-8")
        return Path(path).name

    @_safe
    def pick_settings_file(self):
        """Choose a file of settings to load: what's in it, to confirm first (None if cancelled)."""
        path = _picked(self._app.window.create_file_dialog(
            webview.FileDialog.OPEN, directory=_documents(), file_types=FILE_TYPES))
        if not path:
            return None
        data = backups.read(Path(path))
        if data is None:
            raise ValueError(f"{Path(path).name} doesn't hold Glowstage's settings")
        self._picked_file = Path(path)
        return backups.describe(Path(path), data)

    @_safe
    def load_settings_file(self):
        """Load the file picked with pick_settings_file: Glowstage restarts with it."""
        data = backups.read(self._picked_file) if self._picked_file else None
        if not data:
            raise ValueError("Choose the file again")
        self._load(data)

    def _load(self, data):
        self._app.config.replace(data)
        threading.Timer(0.3, self._app.restart, kwargs={"show": True}).start()

    @_safe
    def whats_new_seen(self):
        """The "what's new" card was read: not again for this version."""
        self._app.config["settings"]["seen_version"] = VERSION
        self._app.config.save_soon()

    @_safe
    def remember_color(self, hex):
        """A color set by hand (wheel, hex field, white slider): kept as one of the last 8."""
        c = str(hex or "").strip().lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", c):
            raise ValueError("Use a hex color like #ff8800")
        s = self._app.config["settings"]
        s["recent_colors"] = ([c] + [x for x in s.get("recent_colors", []) if x != c])[:8]
        self._app.config.save_soon()

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
        # Everything (or a group) on or off, or a new effect: something to undo. (Not every step
        # of a slider, nor one light's own power button.)
        if "mode" in patch or "effect" in patch or (set(patch) <= {"on"} and (not ids or len(ids) > 1)):
            self._m.remember()
        self._m.update(ids or None, patch)

    @_safe
    def undo(self):
        """Back to the lights as they were before the last big change by hand."""
        if not self._m.undo():
            raise ValueError("There's nothing to undo")

    @_safe
    def native_options(self, dev_id):
        return self._m.native_options(dev_id)

    @_safe
    def apply_preset(self, name):
        self._m.remember()
        self._m.apply_preset(name)

    @_safe
    def pick_picture(self):
        """Choose a picture to light the room from: its path (None if cancelled)."""
        path = _picked(self._app.window.create_file_dialog(
            webview.FileDialog.OPEN, directory=_documents(), file_types=PICTURE_TYPES))
        return path or None

    @_safe
    def colors_from_picture(self, path):
        """The picture's main colors, now on the lights (Undo like a preset), with a name to save
        them under and a small copy of the picture for the window."""
        img = palette.read(str(path))
        colors = palette.main_colors(img)
        self._m.remember()
        if not self._m.light_from_colors(colors):
            raise ValueError("Switch a light on first, then try again")
        stem = Path(str(path)).stem.replace("_", " ").strip()[:40]
        return {"colors": colors, "name": stem[:1].upper() + stem[1:], "thumb": palette.thumbnail_url(img)}

    @_safe
    def colors_from_wallpaper(self):
        """The colors of the desktop wallpaper, now on the lights (Undo like a preset): the same
        answer as colors_from_picture."""
        colors, img = self._m.light_from_wallpaper()
        return {"colors": colors, "name": wallpaper.NAME, "thumb": palette.thumbnail_url(img)}

    @_safe
    def start_wallpaper_follow(self):
        """Keep the lights in the colors of the wallpaper as it changes (wallpaper.py)."""
        self._m.wallpaper.start()

    @_safe
    def stop_wallpaper_follow(self):
        self._m.wallpaper.cancel()

    @_safe
    def save_preset(self, name, ids=None):
        self._m.save_preset(name, ids)

    @_safe
    def rename_preset(self, old, new):
        self._m.rename_preset(old, new)

    @_safe
    def move_preset(self, name, step):
        self._m.move_preset(name, step)

    @_safe
    def duplicate_preset(self, name):
        """A copy of a preset right after it: the copy's name."""
        return self._m.duplicate_preset(name)

    # ---- your own groups of lights (Setup → Lights → Your groups, Home) ------------------------

    @_safe
    def create_group(self, name, ids):
        return self._m.create_group(name, ids)

    @_safe
    def rename_group(self, old, new):
        self._m.rename_group(old, new)

    @_safe
    def set_group_lights(self, name, ids):
        self._m.set_group_lights(name, ids)

    @_safe
    def delete_group(self, name):
        self._m.delete_group(name)

    @_safe
    def move_group(self, name, step):
        self._m.move_group(name, max(-1, min(1, int(step))))

    @_safe
    def group_power(self, name, on):
        """A group's lights on or off (Undo takes it back, even for a group of one light)."""
        self._m.set_group_power(name, bool(on))

    @_safe
    def group_brightness(self, name, value):
        """The brightness of a group's lights that are on (a slider: no Undo for each step)."""
        self._m.set_group_brightness(name, value, undo=False)

    @_safe
    def group_look(self, name, patch):
        """A color or an effect for a group's lights, as one change."""
        self._m.set_group_look(name, patch)

    @_safe
    def nudge_brightness(self, delta):
        """Every light that is on a bit brighter or darker (the + and - keys in the window)."""
        self._m.nudge_brightness(max(-100, min(100, int(delta))))

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
    def identify(self, dev_id):
        """Flash one light for a few seconds so you can see which one it is."""
        self._m.identify(dev_id)

    @_safe
    def get_balance(self, dev_id):
        """A light's white balance and what full brightness means for it."""
        return self._m.balance(dev_id)

    @_safe
    def set_balance(self, dev_id, warmth, top):
        self._m.set_balance(dev_id, warmth, top)

    @_safe
    def set_color_order(self, dev_id, order):
        """Which order a light's colors are wired in: RGB, RBG, GRB, GBR, BRG or BGR."""
        self._m.set_color_order(dev_id, order)

    @_safe
    def color_test(self, dev_id, on):
        """Red, green, blue on one light while its Balance window is open (off: back as it was)."""
        self._m.color_test(dev_id, bool(on))

    @_safe
    def color_test_step(self, dev_id):
        """Which color of the test the light shows right now: "red", "green", "blue" or "" (not running)."""
        return self._m.color_test_step(dev_id)

    @_safe
    def show_white(self, dev_id, on):
        """A plain white on one light while its Balance window is open (off: back as it was)."""
        self._m.show_white(dev_id, bool(on))

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
        if key not in self._app.config["settings"] or key in ("hotkey_keys", "show_input"):   # (the keys and the microphone have calls of their own)
            raise ValueError(f"Unknown setting {key}")
        if key in ("auto_game", "auto_video"):   # "", "screen", "screen_sound" or "preset:<name>"
            value = str(value or "")
            if value not in ("", "screen", "screen_sound") and not (value.startswith("preset:")
                                                    and value[7:] in self._app.manager.preset_names()):
                raise ValueError("Choose a look from the list")
        if key == "screen_display":   # "" (main), "auto" or a display's device name, e.g. \\.\DISPLAY2
            value = str(value or "")
            if value not in ("", "auto") and not re.fullmatch(r"\\\\\.\\DISPLAY\d{1,3}", value):
                raise ValueError("Choose a display from the list")
        if key in ("auto_call", "auto_live"):   # "" or "preset:<name>"
            value = str(value or "")
            if value and not (value.startswith("preset:") and value[7:] in self._app.manager.preset_names()):
                raise ValueError("Choose a look from the list")
        if key == "away_lights" and value not in SCOPES:
            raise ValueError("Choose what happens from the list")
        if key == "glide":
            return self._app.manager.set_glide(str(value))
        self._app.config["settings"][key] = value
        self._app.config.save_soon()
        if key == "hotkeys":
            self._app.hotkeys.set_enabled(bool(value))

    # ---- the keys for the hotkeys (Setup → App → Change keys; hotkeys.py) ----------------------

    def _hotkey_list(self):
        hk = self._app.hotkeys
        return {"enabled": bool(self._app.config["settings"].get("hotkeys")), "actions": hk.actions(),
                "presets": self._m.preset_names()}

    @_safe
    def get_hotkeys(self):
        """Every action with its key and whether another program has it, and the presets (for the
        picker that gives a preset a key of its own)."""
        return self._hotkey_list()

    @_safe
    def set_hotkey(self, action, key):
        """A new key for one action: refused with the reason when it can't be one (hotkeys.py)."""
        self._app.hotkeys.set_key(str(action), str(key))
        return self._hotkey_list()

    @_safe
    def clear_hotkey(self, action):
        self._app.hotkeys.clear_key(str(action))
        return self._hotkey_list()

    @_safe
    def add_preset_hotkey(self, name):
        self._app.hotkeys.add_preset(str(name))
        return self._hotkey_list()

    @_safe
    def remove_preset_hotkey(self, name):
        self._app.hotkeys.remove_preset(str(name))
        return self._hotkey_list()

    @_safe
    def reset_hotkeys(self):
        self._app.hotkeys.reset()
        return self._hotkey_list()

    @_safe
    def pause_hotkeys(self):
        """While the key dialog is open: Glowstage's own hotkeys would take the keys before the window sees them."""
        self._app.hotkeys.pause()

    @_safe
    def resume_hotkeys(self):
        self._app.hotkeys.resume()

    @_safe
    def list_displays(self):
        """The choices for what Screen sync follows (Color & effects): the main display, each other
        display by number and size, and "where the game or video is". `current` is the saved choice,
        `using` the name of the display being followed now, `note` says when the chosen one is gone."""
        setting = self._app.config["settings"].get("screen_display") or ""
        sampler, monitors = self._m.engine.screen, screen.list_monitors()
        if sampler.running() and sampler.choice.monitor:   # what it follows right now ("auto" holds a display a while)
            choice = sampler.choice
        else:
            choice = screen.choose(setting, monitors, screen.full_screen_device() if setting == "auto" else None)
        return screen.describe(setting, monitors, choice)

    @_safe
    def install_update(self):
        """Update now: download the new version, check it, put it in place and restart."""
        if not self._app.online.update():
            raise ValueError("There's no newer version")
        self._app.updater.start(show_window=True)

    @_safe
    def games_connect(self):
        """Game lighting: put the small file in CS2's cfg folder and start listening."""
        self._app.manager.games.connect()

    @_safe
    def games_disconnect(self):
        self._app.manager.games.disconnect()

    @_safe
    def games_dota_connect(self):
        """Game lighting: put the small file in Dota 2's cfg folder and start listening."""
        self._app.manager.games.connect_dota()

    @_safe
    def games_dota_disconnect(self):
        self._app.manager.games.disconnect_dota()

    @_safe
    def games_rl_connect(self):
        """Game lighting: switch Rocket League's Stats API on in its ini file and start listening."""
        self._app.manager.games.connect_rl()

    @_safe
    def games_rl_disconnect(self):
        self._app.manager.games.disconnect_rl()

    @_safe
    def games_racing(self, on, forza_port, f1_port):
        """Racing games: listen for what Forza and the F1 games send to this PC (or not), and on which ports."""
        self._app.manager.games.set_racing(on, forza_port, f1_port)

    @_safe
    def games_lol(self, on):
        """League of Legends: switch asking the game's own live data on or off (nothing is installed)."""
        self._app.manager.games.set_lol(bool(on))

    @_safe
    def obs_set(self, on, port, when):
        """Setup → Automatic → OBS: connect to OBS on this PC (or not), the port of its WebSocket
        server, and whether a stream, a recording or either counts."""
        self._app.manager.obs.set_options(on, port, when)

    @_safe
    def obs_set_password(self, password):
        """OBS's password: kept protected for this Windows user, never given back to the window."""
        self._app.manager.obs.set_password(password)

    @_safe
    def obs_clear_password(self):
        self._app.manager.obs.clear_password()

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
