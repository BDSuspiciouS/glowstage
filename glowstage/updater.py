"""Updating with one click: Glowstage gets the new version from its GitHub release, checks it and
puts it in place of itself.

1. Download the release's zip from github.com (only this project's releases), and check it
   against the SHA-256 that GitHub lists for it, or at least its size.
2. Unpack it into %LOCALAPPDATA%\\Glowstage\\update\\<version>.
3. Start the NEW Glowstage.exe from there with --install-update <folder> --after <pid>, and close.
4. That copy waits for the old one to close, moves the old files aside (into _old), copies itself
   into the folder, and starts it. If anything can't be copied it puts the old files back and
   starts the old version, so a failed update never leaves a broken app.

Settings, lights and presets live in %APPDATA%\\Glowstage and stay as they are. Only the built
app (Glowstage.exe) can update itself; from a source checkout the update note links to GitHub.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from .paths import DATA_DIR, FROZEN, INSTALL_DIR, REPO, VERSION

log = logging.getLogger(__name__)

LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
UPDATE_DIR = DATA_DIR / "update"
EXE = "Glowstage.exe"


def _version(text: str) -> tuple[int, ...]:
    import re
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3]) or (0,)


def can_install(folder: Path = INSTALL_DIR) -> bool:
    """The built app, in a folder it may write to."""
    if not FROZEN:
        return False
    probe = folder / ".glowstage-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


class Updater:
    def __init__(self, on_change, quit_app):
        self.on_change = on_change
        self.quit_app = quit_app          # (show_window: bool) -> None: close the app for the swap
        self.state = "idle"               # idle, downloading, checking, installing, failed
        self.progress = 0.0
        self.message = ""
        self._thread: threading.Thread | None = None

    def describe(self) -> dict:
        return {"state": self.state, "progress": round(self.progress, 3), "message": self.message,
                "canInstall": can_install()}

    def start(self, show_window: bool, latest_url: str = LATEST_URL) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._set("downloading", 0.0, "Getting the new version…")
        self._thread = threading.Thread(target=self._run, args=(show_window, latest_url), name="update", daemon=True)
        self._thread.start()

    def _set(self, state: str, progress: float | None = None, message: str | None = None) -> None:
        self.state = state
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        self.on_change()

    def _run(self, show_window: bool, latest_url: str) -> None:
        try:
            exe = self.prepare(latest_url)
            self._set("installing", 1.0, "Installing: Glowstage restarts in a moment…")
            log.info("Update: starting %s to install it", exe)
            subprocess.Popen([str(exe), "--install-update", str(INSTALL_DIR), "--after", str(os.getpid()),
                              *([] if show_window else ["--minimized"])],
                             cwd=str(exe.parent),
                             creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            time.sleep(0.5)
            self.quit_app()
        except Exception as e:
            log.warning("Update failed: %s", e)
            self._set("failed", 0.0, f"The update didn't work: {e}. Your Glowstage is unchanged.")

    def prepare(self, latest_url: str = LATEST_URL) -> Path:
        """Download, check and unpack the latest release. Returns the new Glowstage.exe."""
        req = urllib.request.Request(latest_url, headers={"User-Agent": f"Glowstage/{VERSION}",
                                                          "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read(500_000))
        version = str(rel.get("tag_name", "")).lstrip("v")
        if _version(version) <= _version(VERSION):
            raise ValueError("there is no newer version")
        name = f"Glowstage-{version}-windows.zip"
        asset = next((a for a in rel.get("assets", []) if a.get("name") == name), None)
        if not asset:
            raise ValueError(f"the release has no {name}")
        url = str(asset.get("browser_download_url", ""))
        if not url.startswith(DOWNLOADS) and not latest_url.startswith("http://127.0.0.1"):
            raise ValueError("the download isn't on Glowstage's GitHub page")
        size, digest = int(asset.get("size") or 0), str(asset.get("digest") or "")

        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = UPDATE_DIR / name
        sha = hashlib.sha256()
        got = 0
        req = urllib.request.Request(url, headers={"User-Agent": f"Glowstage/{VERSION}"})
        with urllib.request.urlopen(req, timeout=30) as r, open(zip_path, "wb") as f:
            while chunk := r.read(256 * 1024):
                f.write(chunk)
                sha.update(chunk)
                got += len(chunk)
                if size:
                    self.progress = min(0.99, got / size)
                    self.message = f"Getting the new version… {got / 1e6:.0f} of {size / 1e6:.0f} MB"
        self._set("checking", 1.0, "Checking the download…")
        problem = ("the download is incomplete" if size and got != size
                   else "the download doesn't match what GitHub lists for it"
                   if digest.startswith("sha256:") and sha.hexdigest() != digest[7:].lower() else "")
        if problem:
            zip_path.unlink(missing_ok=True)
            raise ValueError(problem)

        target = UPDATE_DIR / version
        shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as z:
            root = target.resolve()
            for member in z.namelist():   # nothing may land outside the update folder
                if not (target / member).resolve().is_relative_to(root):
                    raise ValueError("the download has unexpected contents")
            z.extractall(target)
        zip_path.unlink(missing_ok=True)
        exe = target / "Glowstage" / EXE
        if not exe.is_file():
            raise ValueError("the download has no Glowstage.exe")
        return exe


# ---- the new copy's side: put itself in place of the old one ------------------------------------

def _files(folder: Path) -> list[Path]:
    return [p.relative_to(folder) for p in folder.rglob("*") if p.is_file()]


def install(source: Path, target: Path, attempts: int = 10) -> bool:
    """Copy the app in `source` over the one in `target`. The old files move aside into
    target/_old first, and come back if anything fails. True when the new version is in place."""
    old_dir = target / "_old"
    shutil.rmtree(old_dir, ignore_errors=True)
    moved: list[Path] = []      # old files now in _old
    added: list[Path] = []      # new files where there was none
    try:
        for rel in _files(source):
            dst = target / rel
            if dst.exists():
                for i in range(attempts):        # (a virus scanner may hold a file for a moment)
                    try:
                        (old_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                        os.replace(dst, old_dir / rel)
                        moved.append(rel)
                        break
                    except OSError:
                        if i == attempts - 1:
                            raise
                        time.sleep(0.5)
            else:
                added.append(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dst)
    except OSError as e:
        log.error("Update: couldn't install (%s); putting the old version back", e)
        for rel in added:
            (target / rel).unlink(missing_ok=True)
        for rel in moved:
            try:
                os.replace(old_dir / rel, target / rel)
            except OSError as e2:
                log.error("Update: couldn't put back %s: %s", rel, e2)
        if not any(p.is_file() for p in old_dir.rglob("*")):
            shutil.rmtree(old_dir, ignore_errors=True)
        return False
    shutil.rmtree(old_dir, ignore_errors=True)
    return True


def run_install(argv: list[str]) -> None:
    """`Glowstage.exe --install-update <folder> --after <pid> [--minimized]`, run from the update folder."""
    from . import winutil
    target = Path(argv[argv.index("--install-update") + 1])
    if "--after" in argv:
        winutil.wait_for_exit(int(argv[argv.index("--after") + 1]), timeout=30)
    source = Path(sys.executable).resolve().parent
    ok = install(source, target)
    log.info("Update %s: %s", "installed" if ok else "failed, the old version is back", target)
    args = [str(target / EXE), *(["--minimized"] if "--minimized" in argv else []), "--updated" if ok else "--update-failed"]
    subprocess.Popen(args, cwd=str(target), creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def clean_up() -> None:
    """Remove what earlier updates left (the app runs from its own folder again)."""
    try:
        here = Path(sys.executable).resolve()
        if UPDATE_DIR.is_dir() and UPDATE_DIR.resolve() not in here.parents:
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        shutil.rmtree(INSTALL_DIR / "_old", ignore_errors=True)
    except OSError:
        pass
