"""Records what the PC plays (WASAPI loopback, no microphone) song by song, for the light show
benchmark (tools/show_bench.py). Not part of the app.

A new file starts when the music player shows another song (Spotify, Tidal, Deezer, or a
YouTube / SoundCloud tab) or after ten seconds of silence. Files are mono 16-bit 48 kHz WAVs in
testdata/recordings (kept out of git: the music isn't ours to share), listed in index.json.

    python tools/record_songs.py            # until Ctrl+C, or a file named STOP in that folder

Prints one line per song started and saved.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import re
import sys
import time
import wave
import warnings
from pathlib import Path

import numpy as np

warnings.simplefilter("ignore")
import soundcard as sc  # noqa: E402

RATE = 48000
OUT = Path(__file__).resolve().parent.parent / "testdata" / "recordings"
SILENCE_RMS = 3e-5        # digital silence between songs (music played softly is still music)
SILENCE_SPLIT = 10.0       # seconds of silence that end a song (a rap pause or a gap
                           # before the next song doesn't: the player's title change does)
MIN_SONG = 30.0            # shorter pieces (ads, skipped songs) are thrown away
MAX_SONG = 15 * 60.0       # a DJ mix is cut into 15-minute files

PLAYERS = {"spotify.exe", "tidal.exe", "deezer.exe", "applemusic.exe", "amazon music.exe"}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "vivaldi.exe"}
SITES = re.compile(r"(.+?)\s+[-–]\s+(YouTube|YouTube Music|SoundCloud|Mixcloud|Beatport)\b")

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32


def _process_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(512), wt.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def now_playing() -> str:
    """The song a music player shows in its window title, or "" (only players and media sites
    are looked at, no other windows)."""
    found: list[str] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        proc = _process_name(hwnd)
        if proc in PLAYERS and " - " in title:
            found.append(title)
        elif proc in BROWSERS:
            m = SITES.search(title)
            if m:
                found.append(re.sub(r"^\(\d+\)\s*", "", m.group(1)))
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else ""


def safe_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()[:80] or "unknown"


class Song:
    def __init__(self, title: str, device: str):
        self.title, self.device = title, device
        self.started = dt.datetime.now()
        self.path = OUT / f"{self.started:%Y%m%d-%H%M%S} {safe_name(title)}.wav"
        self.w = wave.open(str(self.path), "wb")
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(RATE)
        self.frames = 0

    def write(self, mono: np.ndarray) -> None:
        self.w.writeframes((np.clip(mono, -1, 1) * 32767).astype(np.int16).tobytes())
        self.frames += len(mono)

    def close(self, trim_end: float = 0.0) -> None:
        self.w.close()
        seconds = self.frames / RATE - trim_end
        if seconds < MIN_SONG:
            self.path.unlink(missing_ok=True)
            print(f"dropped: {self.title or 'untitled'} ({seconds:.0f} s, too short)", flush=True)
            return
        index_path = OUT / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else []
        index.append({"file": self.path.name, "title": self.title, "seconds": round(seconds, 1),
                      "device": self.device, "recorded": self.started.isoformat(timespec="seconds")})
        index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"saved: {self.title or 'untitled'} ({int(seconds // 60)}:{int(seconds % 60):02d}) -> {self.path.name}",
              flush=True)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # song titles in any script
    OUT.mkdir(parents=True, exist_ok=True)
    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    print(f"listening to {speaker.name}; play your songs", flush=True)
    song: Song | None = None
    quiet_for, title, title_checked = 0.0, "", 0.0
    try:
        with mic.recorder(samplerate=RATE, blocksize=1024) as rec:
            while True:
                if (OUT / "STOP").exists():                  # a clean stop from outside
                    (OUT / "STOP").unlink()
                    break
                data = rec.record(numframes=4800)            # 0.1 s
                mono = data.mean(axis=1) if data.ndim == 2 else data
                loud = float(np.sqrt(np.mean(mono ** 2))) > SILENCE_RMS
                quiet_for = 0.0 if loud else quiet_for + len(mono) / RATE
                if time.monotonic() - title_checked > 0.5:
                    title_checked = time.monotonic()
                    playing = now_playing()
                    if playing and playing != title:
                        title = playing
                        if song is not None:                 # the player moved on: next song
                            song.close(trim_end=quiet_for)
                            song = None
                if song is None:
                    if loud:
                        song = Song(title, speaker.name)
                        print(f"recording: {title or 'untitled'}", flush=True)
                        song.write(mono)
                    continue
                song.write(mono)
                if quiet_for >= SILENCE_SPLIT or song.frames / RATE >= MAX_SONG:
                    song.close(trim_end=quiet_for if quiet_for >= SILENCE_SPLIT else 0.0)
                    song = None
    except KeyboardInterrupt:
        pass
    finally:
        if song is not None:
            song.close()


if __name__ == "__main__":
    sys.exit(main())
