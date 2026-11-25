"""Starts the light show by itself when music plays, and puts the lights back the way they were
after the music stops (the "Start by itself when music plays" switch).

Waiting costs next to nothing: Windows' own level meter of the sound output (the bar in the
volume mixer) tells whether anything plays at all. Only after a few seconds of sound does the
music analysis wake up, to hear whether it's music with a beat: talk in a video, a game's
explosions or a notification sound shouldn't start a show. Measured on recordings: songs reach
a tempo confidence of 0.7 or more within 5 to 9 s (mostly 0.9 to 1.0), spoken word stays under
0.35 and noisy game-like sound under 0.45.

When you take over (stop the show, choose a preset, switch the lights off), Glowstage keeps its
hands off until the music has stopped for a while."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import uuid
from ctypes import wintypes as w

log = logging.getLogger(__name__)

LEVEL = 1e-4         # the meter's peak above which something plays (0..1; silence is exactly 0).
                     # Low on purpose: players are often turned far down (peaks of 0.001).
SOUND_FIRST = 4.0    # seconds of sound before the analysis is asked whether it's music
MUSIC_CONF = 0.7     # the analysis' tempo confidence that says: music with a beat
MUSIC_CHECKS = 3     # in this many checks in a row (one a second)
QUIET_END = 30.0     # seconds without sound after which an automatic show ends
GAP = 2.0            # shorter dips in the sound don't count (between songs, a breakdown)


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

class AutoShow:
    def __init__(self, manager):
        self.manager = manager
        self.running = False        # a show this started is on
        self._saved: dict | None = None      # how the lights looked before it, to put back
        self._saved_preset = ""
        self._armed = True          # may start a show (not after you stopped one, until it's quiet)
        self._sound_since: float | None = None
        self._last_sound = -1e9
        self._music = 0             # checks in a row that heard music
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.manager.config["settings"].get("auto_show"))

    def start(self) -> None:
        threading.Thread(target=self._run, name="autoshow", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        meter, meter_at, failed = None, 0.0, ""
        while not self._stop.wait(1.0):
            if not self.enabled:
                if meter:
                    meter.close()
                    meter = None
                self.running, self._saved, self._music = False, None, 0
                continue
            now = time.monotonic()
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
                meter, peak = None, 0.0
            try:
                self.step(now, peak)
            except Exception:
                log.exception("Automatic light show failed")
        if meter:
            meter.close()

    def step(self, now: float, peak: float) -> None:
        """One second of deciding, from the sound level now (separate from the thread, for tests)."""
        m = self.manager
        if peak > LEVEL:
            self._last_sound = now
            if self._sound_since is None:
                self._sound_since = now
        elif now - self._last_sound > GAP:
            self._sound_since = None
        quiet = now - self._last_sound

        if self.running:
            if not m.show_running() or not m.any_on():   # you stopped it, chose a look, or switched off
                log.info("Automatic light show: taken over by hand")
                self.running, self._saved, self._armed = False, None, False
            elif quiet >= QUIET_END:
                log.info("Automatic light show: the music stopped, back to how it was")
                m.restore_looks(self._saved or {}, self._saved_preset)
                self.running, self._saved = False, None
            return
        if m.show_running():        # started by hand, a schedule or a hotkey: not ours to end
            self._music = 0
            return
        if not self._armed:
            if quiet >= QUIET_END:
                self._armed = True
            return
        if self._sound_since is None or now - self._sound_since < SOUND_FIRST or not m.any_on():
            self._music = 0
            return
        heard = m.engine.audio.request()   # wakes the music analysis (it listens 3 s per ask)
        music = not heard["silent"] and heard["bpm_conf"] >= MUSIC_CONF
        self._music = self._music + 1 if music else 0
        if self._music >= MUSIC_CHECKS:
            log.info("Automatic light show: music at %.0f BPM, starting the show", heard["bpm"])
            self._saved, self._saved_preset = m.looks(), m.config["settings"].get("active_preset", "")
            m.start_show()
            self.running, self._music = True, 0
