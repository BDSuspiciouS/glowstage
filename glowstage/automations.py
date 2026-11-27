"""Automatic looks: Glowstage changes the lights by itself when something happens on the PC, and
puts them back the way they were afterwards (Setup → Automatic; the light show's "Start by
itself" switch is the music one).

- A Counter-Strike 2 match, once CS2 is connected (games.py): the lights play along with it.
- A game in full screen: the look chosen for games (Screen sync, or a preset such as Gaming).
- A video in full screen, in a browser or a video player: the look chosen for videos.
- Music with a beat: the light show.

A CS2 match wins over everything; a full-screen game or video wins over music. When you take over (change a light, pick a preset,
switch off; from the window, the tray, a hotkey or a schedule) Glowstage keeps its hands off
until whatever it reacted to is over.

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
import logging
import os
import threading
import time
import uuid
from ctypes import wintypes as w

log = logging.getLogger(__name__)

RULES = ("cs2", "game", "video", "music")   # first wins

LEVEL = 1e-4         # the meter's peak above which something plays (0..1; silence is exactly 0).
                     # Low on purpose: players are often turned far down (peaks of 0.001).
SOUND_FIRST = 4.0    # seconds of sound before the analysis is asked whether it's music
MUSIC_CONF = 0.7     # the analysis' tempo confidence that says: music with a beat
MUSIC_CHECKS = 3     # in this many checks in a row (one a second)
QUIET_END = 30.0     # seconds without sound after which the music is over
GAP = 2.0            # shorter dips in the sound don't count (between songs, a breakdown)
SCREEN_SETTLE = 3.0  # seconds a full-screen game or video has to last, or be gone, to count
CS2_ON, CS2_OFF = 1.0, 6.0   # seconds a CS2 match has to be on / off (CS2 sends at least every 10 s)

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
        self._sound_since: float | None = None
        self._last_sound = -1e9
        self._checks = 0                    # checks in a row that heard music
        self._music = False
        self._stop = threading.Event()

    def rules(self) -> dict[str, str]:
        """What each rule does: "" (nothing), "screen", "show" or "preset:<name>"."""
        s = self.manager.config["settings"]
        return {"cs2": "game:cs2" if self.manager.games.settings.get("cs2") else "",
                "game": s.get("auto_game") or "", "video": s.get("auto_video") or "",
                "music": "show" if s.get("auto_show") else ""}

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
        while not self._stop.wait(1.0):
            rules = self.rules()
            now = time.monotonic()
            kind, peak = None, 0.0
            cs2 = bool(rules["cs2"]) and self.manager.games.active(now)
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
                self.step(now, rules, kind, peak, cs2)
            except Exception:
                log.exception("Automatic look failed")
        if meter:
            meter.close()

    # (separate from the thread, for tests)
    def step(self, now: float, rules: dict[str, str], kind: str | None, peak: float, cs2: bool = False) -> None:
        m = self.manager
        if not any(rules.values()):     # all switched off: forget, leave the lights as they are
            self.active, self._saved, self._set, self._hands_off = None, None, None, False
            self._music, self._checks, self._screen, self._cs2 = False, 0, None, False
            return

        if cs2 != self._cs2_seen[0]:
            self._cs2_seen = (cs2, now)
        if now - self._cs2_seen[1] >= (CS2_ON if cs2 else CS2_OFF):
            self._cs2 = cs2

        if kind != self._screen_kind:
            self._screen_kind, self._screen_since = kind, now
        if now - self._screen_since >= SCREEN_SETTLE:
            self._screen = self._screen_kind
        self._listen(now, rules, peak)

        holds = {"cs2": self._cs2, "game": self._screen == "game", "video": self._screen == "video", "music": self._music}
        want = next((r for r in RULES if rules[r] and holds[r]), None)
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
        busy = self._cs2 or (self._screen is not None and rules.get(self._screen))   # a game or video goes first
        if self._sound_since is None or now - self._sound_since < SOUND_FIRST or busy or self._hands_off:
            self._checks = 0
            return
        heard = self.manager.engine.audio.request()   # wakes the music analysis (it listens 3 s per ask)
        self._checks = self._checks + 1 if not heard["silent"] and heard["bpm_conf"] >= MUSIC_CONF else 0
        if self._checks >= MUSIC_CHECKS:
            log.info("Music at %.0f BPM", heard["bpm"])
            self._music, self._checks = True, 0

    def _do(self, action: str) -> bool:
        m = self.manager
        if action == "show":
            m.start_show()
        elif action == "game:cs2":
            m.game_lights("game_cs2")
        elif action == "screen":
            m.screen_sync()
        elif action.startswith("preset:") and action[7:] in m.preset_names():
            m.apply_preset(action[7:])
        else:
            return False   # e.g. a preset that has been deleted since
        return True


def _same(now: dict, then: dict) -> bool:
    """The lights still look as we left them (lights that came or went since don't count)."""
    return all(now[i] == then[i] for i in now.keys() & then.keys())
