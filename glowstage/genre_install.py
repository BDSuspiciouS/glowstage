"""Gets the enhanced genre AI for the user: the Discogs-EffNet model by the Music Technology
Group (Universitat Pompeu Fabra, Barcelona), straight from its makers (essentia.upf.edu), into
%LOCALAPPDATA%\\Glowstage\\models. Only when the user asks for it (Settings → Genre AI).

Glowstage doesn't ship the model itself: its licence (CC BY-NC-SA 4.0) is for non-commercial
use, which fits people lighting their room, but not shipping it inside a GPL program that anyone
may sell. The files are checked against the SHA-256 of the published ones before they're used.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.request

from . import genre
from .paths import APP_NAME, VERSION

log = logging.getLogger(__name__)

BASE = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/"
FILES = (   # (path, size in bytes, SHA-256), as published on 16 May 2023
    (genre.DISCOGS_LABELS, 14_986, "a2e85b2e7372d5f8e0f35bdd6aeae1139f101087d183d0b2fb60b0ea0f01a0ff"),
    (genre.DISCOGS_MODEL, 18_027_718, "a280825b334797cf677939db8cd5762c0392aedd0ca6415dbc1cd083f045e43c"),
)
INFO = {
    "name": "Discogs-EffNet",
    "maker": "Music Technology Group, Universitat Pompeu Fabra",
    "license": "CC BY-NC-SA 4.0",
    "licenseUrl": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "aboutUrl": "https://essentia.upf.edu/models.html",
    "mb": round(sum(size for _, size, _ in FILES) / 1e6),
}

_lock = threading.Lock()
_status = {"state": "idle", "progress": 0.0, "message": ""}   # idle | downloading | done | failed


def installed() -> bool:
    return all(path.is_file() for path, _, _ in FILES)


def describe() -> dict:
    with _lock:
        return {**_status, **INFO, "installed": installed()}


def _set(**kw) -> None:
    with _lock:
        _status.update(kw)


def start(on_done=None) -> None:
    """Download the model on a background thread; on_done() runs after a success."""
    with _lock:
        if _status["state"] == "downloading":
            return
        _status.update(state="downloading", progress=0.0, message="Downloading the enhanced genre AI…")
    threading.Thread(target=_run, args=(on_done,), name="genre-install", daemon=True).start()


def _run(on_done) -> None:
    parts = []
    try:
        genre.DISCOGS_DIR.mkdir(parents=True, exist_ok=True)
        total, done = sum(size for _, size, _ in FILES), 0
        for path, size, sha in FILES:
            part = path.with_name(path.name + ".part")
            parts.append((part, path))
            digest = hashlib.sha256()
            req = urllib.request.Request(BASE + path.name, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
                got = 0
                while chunk := resp.read(256 * 1024):
                    if got + len(chunk) > size:
                        raise ValueError(f"{path.name} is bigger than the published file")
                    out.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    _set(progress=min(0.99, (done + got) / total),
                         message=f"Downloading the enhanced genre AI… {(done + got) / 1e6:.0f} of {total / 1e6:.0f} MB")
            if got != size or digest.hexdigest() != sha:
                raise ValueError(f"{path.name} isn't the published file (size or checksum differs)")
            done += size
        for part, path in parts:    # only once every file checked out
            os.replace(part, path)
        log.info("Installed the enhanced genre AI (%s) into %s", INFO["name"], genre.DISCOGS_DIR)
        _set(state="done", progress=1.0, message="The enhanced genre AI is installed")
        if on_done:
            on_done()
    except Exception as e:
        log.warning("Installing the enhanced genre AI failed: %s", e)
        _set(state="failed", message=f"Couldn't get the enhanced genre AI: {e}")
    finally:
        for part, _ in parts:
            part.unlink(missing_ok=True)


def remove(on_done=None) -> None:
    """Delete the model again: the light show goes back to the built-in AI."""
    with _lock:
        if _status["state"] == "downloading":
            raise RuntimeError("The enhanced genre AI is still downloading")
    for path, _, _ in FILES:
        path.unlink(missing_ok=True)
    log.info("Removed the enhanced genre AI")
    _set(state="idle", progress=0.0, message="")
    if on_done:
        on_done()
