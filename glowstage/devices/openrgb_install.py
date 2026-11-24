"""Gets OpenRGB for the user: the official portable build, straight from the OpenRGB project
(codeberg.org), unpacked into %LOCALAPPDATA%\\Glowstage\\OpenRGB. Only when the user asks for it
(the setup wizard or Settings → PC lighting): OpenRGB drives the PC's own RGB, so it's needed
for motherboards, RAM, coolers and peripherals, but not for network lights.

Glowstage doesn't ship OpenRGB itself: the user gets it from its makers, under its own license
(GPL-2.0), and can update or remove it like any portable app.
"""

from __future__ import annotations

import logging
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from ..paths import APP_NAME, DATA_DIR, VERSION

log = logging.getLogger(__name__)

OPENRGB_VERSION = "1.1"
OPENRGB_URL = ("https://codeberg.org/OpenRGB/OpenRGB/releases/download/release_1.1/"
               "OpenRGB_1.1_Windows_64_8e16de2.zip")
TARGET = DATA_DIR / "OpenRGB"
MIN_SIZE = 5_000_000              # anything smaller than this is an error page, not OpenRGB

_lock = threading.Lock()
_status = {"state": "idle", "progress": 0.0, "message": ""}   # idle | downloading | unpacking | done | failed


def describe() -> dict:
    with _lock:
        return {**_status, "version": OPENRGB_VERSION}


def _set(**kw) -> None:
    with _lock:
        _status.update(kw)


def start(on_done=None) -> None:
    """Download and unpack OpenRGB on a background thread; on_done() runs after a success."""
    with _lock:
        if _status["state"] in ("downloading", "unpacking"):
            return
        _status.update(state="downloading", progress=0.0, message="Downloading OpenRGB…")
    threading.Thread(target=_run, args=(on_done,), name="openrgb-install", daemon=True).start()


def _run(on_done) -> None:
    tmp = DATA_DIR / "OpenRGB-download.zip"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(OPENRGB_URL, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(256 * 1024):
                out.write(chunk)
                done += len(chunk)
                if total:
                    _set(progress=min(0.99, done / total),
                         message=f"Downloading OpenRGB… {done / 1e6:.0f} of {total / 1e6:.0f} MB")
        if tmp.stat().st_size < MIN_SIZE or not zipfile.is_zipfile(tmp):
            raise ValueError("the download isn't the OpenRGB zip")
        _set(state="unpacking", progress=1.0, message="Unpacking OpenRGB…")
        fresh = DATA_DIR / "OpenRGB-new"
        shutil.rmtree(fresh, ignore_errors=True)
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():   # never write outside the target folder
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    raise ValueError(f"unexpected path in the zip: {name}")
            z.extractall(fresh)
        if not any(fresh.glob("**/OpenRGB.exe")):
            raise ValueError("OpenRGB.exe isn't in the zip")
        shutil.rmtree(TARGET, ignore_errors=True)
        fresh.rename(TARGET)
        log.info("Installed OpenRGB %s into %s", OPENRGB_VERSION, TARGET)
        _set(state="done", message=f"OpenRGB {OPENRGB_VERSION} is installed")
        if on_done:
            on_done()
    except Exception as e:
        log.warning("Installing OpenRGB failed: %s", e)
        _set(state="failed", message=f"Couldn't install OpenRGB: {e}")
    finally:
        tmp.unlink(missing_ok=True)
