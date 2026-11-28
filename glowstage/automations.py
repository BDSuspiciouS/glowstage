"""Automatic looks: Glowstage changes the lights by itself when something happens on the PC, and
puts them back the way they were afterwards (Setup → Automatic; the light show's "Start by
itself" switch is the music one).

- A Counter-Strike 2 match, once CS2 is connected (games.py): the lights play along with it.
- A League of Legends match, once its switch is on (games.py, lol.py): the same.
- A Dota 2 match, once Dota 2 is connected (games.py): the same.
- A Rocket League match, once Rocket League is connected (games.py, rocket_league.py): the same.
- A race in Forza or an F1 game, once the racing games' switch is on (games.py, racing.py): the rev lights.
- An app you chose is open (Setup → Automatic → Apps): its look, e.g. Focus while Photoshop is.
- A game in full screen: the look chosen for games (Screen sync, or a preset such as Gaming).
- A video in full screen, in a browser or a video player: the look chosen for videos.
- Music with a beat: the light show.
- A call: any app using the camera or the microphone (calls.py): the look chosen for calls.
- On air: OBS Studio streams or records (obs.py): the look chosen for it, e.g. a preset that only
  sets one light red. It sits right after the call.

A call wins over everything (it has to last a few seconds, and be over for a while before
the lights go back, so a quick voice message doesn't flick them). Then a CS2, League, Dota 2, Rocket League or racing game match, then your apps (the first in the list), then a full-screen
game or video, then music. When you take over (change a light, pick a preset,
switch off; from the window, the tray, a hotkey or a schedule) Glowstage keeps its hands off
until whatever it reacted to is over.

You can also pause all of them for a while (an hour, three, or until tomorrow morning: Home, the
tray, Ctrl+K, a link): the look that's on ends and puts your lights back, and nothing changes by
itself until the pause is over or you resume. The pause is kept in the settings, so it holds
across a restart (until tomorrow is a long time).

Full screen: Windows itself says when a program holds the whole screen (the same test it uses to
hold back notifications). The program in front then says game or video: browsers and video
players are videos, a few programs that go full screen but are neither (Remote Desktop, a slide
show) don't count, anything else is a game. It has to last a few seconds, so alt-tabbing through
doesn't flick the lights.

Music: waiting costs next to nothing. Windows' own level meter of the sound output (the bar in
the volume mixer) tells whether anything plays at all; only after a few seconds of sound does the
music analysis wake up, to hear whether it's music with a beat: talk in a video, a game's
explosions or a notification sound shouldn't start a show. Measured on recordings: songs reach a
tempo confidence of 0.7 or more within 5 to 9 s (mostly 0.9 to 1.0), spoken word stays under
0.35 and noisy game-like sound under 0.45."""

from __future__ import annotations

import ctypes
import functools
import datetime
import logging
import os
import struct
import threading
import time
import uuid
from ctypes import wintypes as w

from . import calls

log = logging.getLogger(__name__)

RULES = ("live", "call", "cs2", "lol", "dota", "rl", "racing", "game", "video", "music")   # first wins; your apps ("app:<exe>") come after the games
APP_LOOKS_MAX = 30
PAUSE_CHOICES = (60, 180)    # minutes, as offered; or "tomorrow": until MORNING the next morning
MORNING = 6                  # o'clock: when "until tomorrow" is over

LEVEL = 1e-4         # the meter's peak above which something plays (0..1; silence is exactly 0).
                     # Low on purpose: players are often turned far down (peaks of 0.001).
SOUND_FIRST = 4.0    # seconds of sound before the analysis is asked whether it's music
MUSIC_CONF = 0.7     # the analysis' tempo confidence that says: music with a beat
MUSIC_CHECKS = 3     # in this many checks in a row (one a second)
QUIET_END = 30.0     # seconds without sound after which the music is over
GAP = 2.0            # shorter dips in the sound don't count (between songs, a breakdown)
SCREEN_SETTLE = 3.0  # seconds a full-screen game or video has to last, or be gone, to count
CS2_ON, CS2_OFF = 1.0, 6.0   # seconds a CS2 match has to be on / off (CS2 sends at least every 10 s)
DOTA_ON, DOTA_OFF = 1.0, 6.0   # the same for a Dota 2 match (it sends many times a second, and a heartbeat every 10 s)
LOL_ON, LOL_OFF = 1.0, 6.0   # the same for a League of Legends match (the game is asked once a second, so a match shows within a moment)
RL_ON, RL_OFF = 1.0, 6.0     # the same for a Rocket League match (it sends 30 times a second)
RACING_ON, RACING_OFF = 1.0, 6.0   # the same for a race in a racing game (Forza says when you pause: a pause shouldn't flick the lights)
CALL_ON, CALL_OFF = 5.0, 10.0   # seconds the camera or microphone has to be in use / free: a quick voice
                                # message shouldn't flick the lights, a short pause in a call shouldn't end it
LIVE_ON, LIVE_OFF = 1.0, 3.0   # seconds OBS has to be streaming or recording / not: the state comes from OBS itself, so a short settle does
CALL_POLL = 3.0      # seconds between looks at which apps use the camera or microphone

VIDEO_APPS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
    "librewolf.exe", "waterfox.exe", "zen.exe", "iexplore.exe",
    "vlc.exe", "mpv.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe", "potplayer.exe",
    "potplayer64.exe", "potplayermini.exe", "potplayermini64.exe", "kmplayer.exe", "gom.exe",
    "smplayer.exe", "wmplayer.exe", "video.ui.exe", "microsoft.media.player.exe", "kodi.exe",
    "plex.exe", "plex htpc.exe", "jellyfinmediaplayer.exe", "stremio.exe", "netflix.exe",
    "applicationframehost.exe",   # Store apps: Netflix, Disney+, Films & TV
}
NOT_GAMES = {
    "explorer.exe", "powerpnt.exe", "pptview.exe", "mstsc.exe", "msrdc.exe", "vmconnect.exe",
    "vmware.exe", "virtualboxvm.exe", "teamviewer.exe", "anydesk.exe", "rustdesk.exe", "lockapp.exe",
    "logonui.exe", "searchhost.exe", "searchapp.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "screenclippinghost.exe", "snippingtool.exe", "textinputhost.exe",
    "windowsterminal.exe", "code.exe", "devenv.exe", "obs64.exe", "acrord32.exe", "acrobat.exe",
    "sumatrapdf.exe", "excel.exe", "winword.exe", "onenote.exe", "ms-teams.exe", "teams.exe",
    "zoom.exe", "discord.exe", "photos.exe", "microsoft.photos.exe", "glowstage.exe",
}


# ---- what runs in full screen -----------------------------------------------------------------

def _process_name(hwnd) -> str:
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = w.DWORD()
    u32.GetWindowThreadProcessId(w.HWND(hwnd), ctypes.byref(pid))
    k32.OpenProcess.restype = w.HANDLE
    handle = k32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(w.HANDLE(handle), 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(w.HANDLE(handle))


def classify(state: int, exe: str) -> str | None:
    """"game", "video" or None, from Windows' notification state and the program in front."""
    if state not in (2, 3):     # QUNS_BUSY (a full-screen window), QUNS_RUNNING_D3D_FULL_SCREEN
        return None
    if exe in VIDEO_APPS:
        return "video"
    if exe in NOT_GAMES or not exe:
        return None
    return "game"


def full_screen() -> tuple[str | None, str]:
    """What holds the screen now ("game", "video" or None), and the program in front."""
    state = ctypes.c_int()
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return None, ""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    exe = _process_name(hwnd) if hwnd else ""
    return classify(state.value, exe), exe


# ---- which apps are open ------------------------------------------------------------------------

_WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
# Windows' own parts, and programs that are always there: not something to give a look to.
SYSTEM_APPS = NOT_GAMES - {"powerpnt.exe", "code.exe", "devenv.exe", "obs64.exe", "excel.exe", "winword.exe",
                           "onenote.exe", "ms-teams.exe", "teams.exe", "zoom.exe", "discord.exe", "acrobat.exe",
                           "acrord32.exe", "sumatrapdf.exe", "windowsterminal.exe", "photos.exe",
                           "microsoft.photos.exe", "vmware.exe", "virtualboxvm.exe"} | {
    "applicationframehost.exe", "systemsettings.exe", "taskmgr.exe", "nvidia overlay.exe", "rundll32.exe",
    "msedgewebview2.exe", "pythonw.exe", "python.exe", "dwm.exe", "ctfmon.exe"}


@functools.lru_cache(maxsize=1)
def _dlls():
    u32, k32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    u32.EnumWindows.argtypes = [_WNDENUMPROC, w.LPARAM]
    for name in ("IsWindowVisible", "GetWindowTextLengthW"):
        getattr(u32, name).argtypes = [w.HWND]
    u32.GetWindow.argtypes, u32.GetWindow.restype = [w.HWND, w.UINT], w.HWND
    u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    k32.OpenProcess.argtypes, k32.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k32.CloseHandle.argtypes = [w.HANDLE]
    dwm = ctypes.WinDLL("dwmapi")
    dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    return u32, k32, dwm


def _window_pids(u32, dwm) -> set[int]:
    """The programs with a window of their own: on screen or minimized, not hidden in the tray."""
    hwnds: list[int] = []

    def each(hwnd, _):
        hwnds.append(hwnd)
        return True

    u32.EnumWindows(_WNDENUMPROC(each), 0)
    pids: set[int] = set()
    pid, cloaked = w.DWORD(), ctypes.c_int()
    for hwnd in hwnds:
        if not u32.IsWindowVisible(hwnd) or u32.GetWindow(hwnd, 4) or not u32.GetWindowTextLengthW(hwnd):
            continue
        cloaked.value = 0               # (a Store app's window that isn't really there)
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        if not cloaked.value:
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
    return pids


def _image_path(k32, pid: int) -> str:
    handle = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        k32.CloseHandle(handle)


_paths: dict[int, str] = {}     # pid -> its program, for the windows seen last time
_paths_lock = threading.Lock()


def _open_paths() -> dict[int, str]:
    """pid -> program path of everything with a window open (cheap: once per new pid)."""
    u32, k32, dwm = _dlls()
    with _paths_lock:
        pids = _window_pids(u32, dwm)
        for pid in pids - _paths.keys():
            _paths[pid] = _image_path(k32, pid)
        for pid in _paths.keys() - pids:
            del _paths[pid]
        return {p: path for p, path in _paths.items() if path}


def open_exes() -> set[str]:
    """The programs (exe names, lower case) that have a window open right now."""
    return {os.path.basename(path).lower() for path in _open_paths().values()}


def _file_description(path: str) -> str:
    """The name a program gives itself ("Adobe Photoshop 2025"), from its version info."""
    ver = ctypes.WinDLL("version")
    ver.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
    ver.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p]
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.UINT)]
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return ""
    ptr, n = ctypes.c_void_p(), w.UINT()
    langs = [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)]
    if ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) and n.value >= 4:
        langs.insert(0, struct.unpack("<HH", ctypes.string_at(ptr.value, 4)))
    for lang, cp in langs:
        key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
        if ver.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(n)) and n.value > 1:
            text = ctypes.wstring_at(ptr.value, n.value).rstrip("\x00").strip()
            if text:
                return text
    return ""


def _nice(exe: str) -> str:
    stem = exe[:-4] if exe.endswith(".exe") else exe
    return stem[:1].upper() + stem[1:]


def open_apps() -> list[dict]:
    """The apps open now that can get a look of their own: [{exe, name}], by name."""
    seen: dict[str, dict] = {}
    for path in _open_paths().values():
        exe = os.path.basename(path).lower()
        if exe not in SYSTEM_APPS and exe not in seen:
            seen[exe] = {"exe": exe, "name": _file_description(path) or _nice(exe)}
    return sorted(seen.values(), key=lambda a: a["name"].lower())


def clean_app_looks(items, presets: list[str]) -> list[dict]:
    """The "Apps" list as the window sends it, checked: [{exe, name, action}]."""
    out, seen = [], set()
    for item in list(items or [])[:APP_LOOKS_MAX]:
        exe = str(item.get("exe", "")).strip().lower()
        if not exe.endswith(".exe") or "\\" in exe or "/" in exe or exe in seen:
            continue
        action = str(item.get("action", ""))
        if action not in ("screen", "screen_sound", "show") and not (action.startswith("preset:") and action[7:] in presets):
            raise ValueError("Choose a look for every app")
        seen.add(exe)
        out.append({"exe": exe, "name": str(item.get("name") or _nice(exe))[:80], "action": action})
    return out


# ---- Windows' level meter of the default sound output -----------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioMeterInformation = _guid("C02216F6-8C67-4B5B-9D00-D008E73E0064")
CLSCTX_ALL = 23


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    """Method `index` of a COM object's vtable, callable with the object as first argument. The
    HRESULT result type raises OSError on failure."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


class LevelMeter:
    """The peak level (0..1) of what the default sound output plays right now. Create, use and
    close it on one thread (COM)."""

    def __init__(self):
        ole32 = ctypes.OleDLL("ole32")
        try:
            ole32.CoInitializeEx(None, 0)          # multithreaded apartment
        except OSError:
            pass                                   # already initialized on this thread
        self._enum = ctypes.c_void_p()
        self._meter = ctypes.c_void_p()
        device = ctypes.c_void_p()
        try:
            ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                   ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(self._enum))
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia, &device)
            _method(self._enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                self._enum, 0, 1, ctypes.byref(device))
            # IMMDevice::Activate(IID_IAudioMeterInformation, CLSCTX_ALL, NULL, &meter)
            _method(device, 3, ctypes.POINTER(_GUID), w.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                device, ctypes.byref(IID_IAudioMeterInformation), CLSCTX_ALL, None, ctypes.byref(self._meter))
        except OSError:
            self.close()
            raise
        finally:
            _release(device)

    def peak(self) -> float:
        value = ctypes.c_float()
        _method(self._meter, 3, ctypes.POINTER(ctypes.c_float))(self._meter, ctypes.byref(value))   # GetPeakValue
        return float(value.value)

    def close(self) -> None:
        _release(self._meter)
        _release(self._enum)
        self._meter = self._enum = ctypes.c_void_p()


# ---- the decisions ---------------------------------------------------------------------------

class Automations:
    def __init__(self, manager):
        self.manager = manager
        self.active: str | None = None      # the rule whose look is on now: "game", "video", "music"
        self._saved: dict | None = None     # how the lights looked before, to put back
        self._saved_preset = ""
        self._set: dict | None = None       # how the lights looked right after we changed them
        self._hands_off = False             # you took over: wait until it's over
        self._screen_kind: str | None = None    # what's in full screen, as last seen
        self._screen_since = 0.0
        self._screen: str | None = None     # ... once it lasted SCREEN_SETTLE
        self._cs2_seen: tuple[bool, float] = (False, 0.0)   # in a CS2 match, since
        self._cs2 = False                   # ... once that settled
        self._lol_seen: tuple[bool, float] = (False, 0.0)   # in a League of Legends match, since
        self._lol = False                   # ... once that settled
        self._dota_seen: tuple[bool, float] = (False, 0.0)   # in a Dota 2 match, since
        self._dota = False                  # ... once that settled
        self._rl_seen: tuple[bool, float] = (False, 0.0)   # in a Rocket League match, since
        self._rl = False                    # ... once that settled
        self._racing_seen: tuple[bool, float] = (False, 0.0)   # in a race in a racing game, since
        self._racing = False                # ... once that settled
        self._call_seen: tuple[bool, float] = (False, 0.0)   # camera or microphone in use, since
        self._call = False                  # ... once that settled
        self._live_seen: tuple[bool, float] = (False, 0.0)   # streaming or recording in OBS, since
        self._live = False                  # ... once that settled
        self._sound_since: float | None = None
        self._last_sound = -1e9
        self._checks = 0                    # checks in a row that heard music
        self._music = False
        self._app_seen: dict[str, tuple[bool, float]] = {}   # exe -> (open, since)
        self._apps: set[str] = set()        # the apps open long enough to count (SCREEN_SETTLE)
        self._stop = threading.Event()

    def rules(self) -> dict[str, str]:
        """What each rule does: "" (nothing), "screen", "screen_sound", "show" or "preset:<name>"."""
        s = self.manager.config["settings"]
        apps = {f"app:{a['exe']}": a["action"] for a in self.manager.config.data.get("app_looks") or []}
        # (Going live in OBS comes first: you chose a preset for it, often one that sets only one
        # light, an ON AIR sign, and a streamed game is usually full screen; a preset can hold a
        # game look like Screen sync for the other lights too. A call comes after games: with voice
        # chat in a game the microphone is on all the time, and the game's lighting should stay.)
        return {"live": s.get("auto_live") or "",
                "cs2": "game:cs2" if self.manager.games.settings.get("cs2") else "",
                "lol": "game:lol" if self.manager.games.settings.get("lol") else "",
                "dota": "game:dota" if self.manager.games.settings.get("dota") else "",
                "rl": "game:rl" if self.manager.games.settings.get("rl") else "",
                "racing": "game:racing" if (self.manager.games.settings.get("racing") or {}).get("on") else "", **apps,
                "game": s.get("auto_game") or "", "call": s.get("auto_call") or "",
                "video": s.get("auto_video") or "", "music": "show" if s.get("auto_show") else ""}

    # ---- pausing ---------------------------------------------------------------------------

    def paused_until(self, now: float | None = None) -> float:
        """When the pause is over (time.time()), 0 when there is none."""
        until = float(self.manager.config["settings"].get("auto_paused_until") or 0)
        return until if until > (time.time() if now is None else now) else 0.0

    def pause(self, minutes, now: float | None = None) -> float:
        """No automatic look for `minutes` (60 or 180) or until tomorrow morning ("tomorrow").
        The look that's on ends at the next look at the PC (about a second) and puts the lights
        back. Gives when the pause is over."""
        now = time.time() if now is None else now
        if minutes == "tomorrow":
            day = datetime.datetime.fromtimestamp(now)
            morning = day.replace(hour=MORNING, minute=0, second=0, microsecond=0)
            if morning <= day:
                morning += datetime.timedelta(days=1)
            until = morning.timestamp()
        else:
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                minutes = 0
            if minutes not in PAUSE_CHOICES:
                raise ValueError("Pause for an hour, three hours, or until tomorrow")
            until = now + minutes * 60
        self.manager.config["settings"]["auto_paused_until"] = until
        self.manager.config.save_soon()
        log.info("Automatic looks paused until %s", time.strftime("%a %H:%M", time.localtime(until)))
        self.manager._changed()
        return until

    def resume(self) -> None:
        if self.manager.config["settings"].get("auto_paused_until"):
            self.manager.config["settings"]["auto_paused_until"] = 0
            self.manager.config.save_soon()
            log.info("Automatic looks: back on")
            self.manager._changed()

    def start(self) -> None:
        threading.Thread(target=self._run, name="automations", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def release(self) -> None:
        """"Put my lights back": undo the automatic look now and leave the lights alone until
        what it reacted to is over."""
        if self.active and self._saved is not None:
            self.manager.restore_looks(self._saved, self._saved_preset)
        self.active, self._saved, self._set, self._hands_off = None, None, None, True

    def _run(self) -> None:
        meter, meter_at, failed = None, 0.0, ""
        apps: set[str] = set()
        on_call, call_at = False, -1e9
        while not self._stop.wait(1.0):
            if self.manager.away.active:   # nobody's watching: no automatic looks (away.py)
                continue
            rules = self.rules()
            now = time.monotonic()
            kind, peak = None, 0.0
            if any(r.startswith("app:") for r in rules):
                try:
                    apps = open_exes()
                except Exception as e:
                    log.debug("No open apps: %s", e)
            cs2 = bool(rules["cs2"]) and self.manager.games.active(now)
            lol = bool(rules["lol"]) and self.manager.games.lol_active(now)
            dota = bool(rules["dota"]) and self.manager.games.dota_active(now)
            rl = bool(rules["rl"]) and self.manager.games.rl_active(now)
            racing = bool(rules["racing"]) and self.manager.games.racing_active(now)
            if rules["call"]:
                if now - call_at >= CALL_POLL:
                    call_at = now
                    try:
                        found = calls.in_use()
                        if bool(found) != on_call:
                            log.info("Camera or microphone %s", calls.describe(found) if found else "free again")
                        on_call = bool(found)
                    except Exception as e:
                        log.debug("No camera or microphone state: %s", e)
            else:
                on_call = False
            live = bool(rules["live"]) and self.manager.obs.live()    # (the watcher keeps up with OBS; nothing is asked here)
            if rules["game"] or rules["video"]:
                try:
                    kind = full_screen()[0]
                except Exception as e:
                    log.debug("No full-screen state: %s", e)
            if rules["music"]:
                try:
                    # (made again every half minute: it follows the default output, which can change)
                    if meter is None or now - meter_at > 30:
                        if meter:
                            meter.close()
                        meter, meter_at = LevelMeter(), now
                    peak = meter.peak()
                    failed = ""
                except OSError as e:
                    if str(e) != failed:
                        log.info("No sound level for the automatic light show: %s", e)
                        failed = str(e)
                    if meter:
                        meter.close()
                    meter = None
            elif meter:
                meter.close()
                meter = None
            try:
                self.step(now, rules, kind, peak, cs2, apps, on_call, lol, dota, rl, live, racing=racing)
            except Exception:
                log.exception("Automatic look failed")
        if meter:
            meter.close()

    # (separate from the thread, for tests)
    def step(self, now: float, rules: dict[str, str], kind: str | None, peak: float, cs2: bool = False,
             apps: set[str] | frozenset = frozenset(), call: bool = False, lol: bool = False, dota: bool = False, rl: bool = False,
             live: bool = False, racing: bool = False) -> None:
        """One look at the PC: `rules` in order of priority (see rules()), `kind` what's full
        screen, `peak` the sound level, `cs2` in a CS2 match, `apps` the exes with a window open,
        `call` the camera or microphone is in use, `lol` in a League of Legends match, `dota` in a Dota 2 match,
        `rl` in a Rocket League match, `live` OBS streams or records, `racing` in a race in a racing game."""
        m = self.manager
        if not any(rules.values()):     # all switched off: forget, leave the lights as they are
            self.active, self._saved, self._set, self._hands_off = None, None, None, False
            self._music, self._checks, self._screen, self._cs2, self._lol, self._dota, self._rl, self._racing, self._call = False, 0, None, False, False, False, False, False, False
            self._call_seen = self._live_seen = (False, now)
            self._live = False
            self._app_seen, self._apps = {}, set()
            return

        if cs2 != self._cs2_seen[0]:
            self._cs2_seen = (cs2, now)
        if now - self._cs2_seen[1] >= (CS2_ON if cs2 else CS2_OFF):
            self._cs2 = cs2

        if lol != self._lol_seen[0]:
            self._lol_seen = (lol, now)
        if now - self._lol_seen[1] >= (LOL_ON if lol else LOL_OFF):
            self._lol = lol

        if dota != self._dota_seen[0]:
            self._dota_seen = (dota, now)
        if now - self._dota_seen[1] >= (DOTA_ON if dota else DOTA_OFF):
            self._dota = dota

        if rl != self._rl_seen[0]:
            self._rl_seen = (rl, now)
        if now - self._rl_seen[1] >= (RL_ON if rl else RL_OFF):
            self._rl = rl

        if racing != self._racing_seen[0]:
            self._racing_seen = (racing, now)
        if now - self._racing_seen[1] >= (RACING_ON if racing else RACING_OFF):
            self._racing = racing

        if call != self._call_seen[0]:
            self._call_seen = (call, now)
        if now - self._call_seen[1] >= (CALL_ON if call else CALL_OFF):
            self._call = call

        if live != self._live_seen[0]:
            self._live_seen = (live, now)
        if now - self._live_seen[1] >= (LIVE_ON if live else LIVE_OFF):
            self._live = live

        if kind != self._screen_kind:
            self._screen_kind, self._screen_since = kind, now
        if now - self._screen_since >= SCREEN_SETTLE:
            self._screen = self._screen_kind
        for rule in rules:
            if rule.startswith("app:"):
                exe, is_open = rule[4:], rule[4:] in apps
                seen = self._app_seen.get(exe)
                if seen is None or seen[0] != is_open:
                    self._app_seen[exe] = seen = (is_open, now)
                if now - seen[1] >= SCREEN_SETTLE:
                    (self._apps.add if is_open else self._apps.discard)(exe)
        self._listen(now, rules, peak)

        holds = {"call": self._call, "live": self._live, "cs2": self._cs2, "lol": self._lol, "dota": self._dota, "rl": self._rl, "racing": self._racing, "game": self._screen == "game", "video": self._screen == "video", "music": self._music,
                 **{r: r[4:] in self._apps for r in rules if r.startswith("app:")}}
        want = next((r for r in rules if rules[r] and holds.get(r)), None)
        if want is not None and self.paused_until():
            want = None         # paused: no automatic look now (the one that's on ends below)
        if self.active and self._set is not None and not _same(m.looks(), self._set):
            log.info("Automatic look (%s): taken over by hand", self.active)
            self.active, self._saved, self._set, self._hands_off = None, None, None, True
        if self._hands_off:
            if want is None:
                self._hands_off = False
            return
        if want == self.active:
            return
        if want is None:
            log.info("Automatic look (%s) is over: back to how it was", self.active)
            m.restore_looks(self._saved or {}, self._saved_preset)
            self.active, self._saved, self._set = None, None, None
            return
        if self.active is None:
            if not m.any_on() or (want == "music" and m.show_running()):   # off, or a show you started
                return
            saved, saved_preset = m.looks(), m.config["settings"].get("active_preset", "")
        else:
            saved, saved_preset = self._saved, self._saved_preset
        if not self._do(rules[want]):
            return
        log.info("Automatic look: %s for the %s", rules[want], want)
        self.active, self._saved, self._saved_preset, self._set = want, saved, saved_preset, m.looks()

    def _listen(self, now: float, rules: dict[str, str], peak: float) -> None:
        """Keeps self._music: music with a beat plays (until QUIET_END of quiet)."""
        if not rules["music"]:
            self._music, self._checks, self._sound_since = False, 0, None
            return
        if peak > LEVEL:
            self._last_sound = now
            if self._sound_since is None:
                self._sound_since = now
        elif now - self._last_sound > GAP:
            self._sound_since = None
        if now - self._last_sound >= QUIET_END:
            self._music = False
        if self._music:
            return
        busy = (self._call or self._live or self._cs2 or self._lol or self._dota or self._rl or self._racing or (self._screen is not None and rules.get(self._screen))   # a game or video goes first
                or any(rules.get(f"app:{exe}") for exe in self._apps))                # so do your apps
        if self._sound_since is None or now - self._sound_since < SOUND_FIRST or busy or self._hands_off:
            self._checks = 0
            return
        audio = self.manager.engine.audio
        if audio.source and audio.active and not audio.pc_requested():
            # (A microphone is in use right now: the room's sound must not start anything, and the
            # PC's sound can't be heard at the same time. The start by music is tied to the PC's own
            # sound: the meter above reads the output, so noise in the room never gets here.)
            self._checks = 0
            return
        heard = audio.request(pc_sound=True)   # wakes the music analysis (it listens 3 s per ask)
        self._checks = self._checks + 1 if not heard["silent"] and heard["bpm_conf"] >= MUSIC_CONF else 0
        if self._checks >= MUSIC_CHECKS:
            log.info("Music at %.0f BPM", heard["bpm"])
            self._music, self._checks = True, 0

    def _do(self, action: str) -> bool:
        m = self.manager
        if action == "show":
            m.start_show()
        elif action == "game:lol":
            m.game_lights("game_lol")
        elif action == "game:dota":
            m.game_lights("game_dota")
        elif action == "game:rl":
            m.game_lights("game_rl")
        elif action == "game:racing":
            m.game_lights("game_racing")
        elif action == "game:cs2":
            m.game_lights("game_cs2")
        elif action in ("screen", "screen_sound"):
            m.screen_sync(action)
        elif action.startswith("preset:") and action[7:] in m.preset_names():
            m.apply_preset(action[7:])
        else:
            return False   # e.g. a preset that has been deleted since
        return True


def _same(now: dict, then: dict) -> bool:
    """The lights still look as we left them (lights that came or went since don't count)."""
    return all(now[i] == then[i] for i in now.keys() & then.keys())
