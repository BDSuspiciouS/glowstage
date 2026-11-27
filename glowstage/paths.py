"""The app's name, version and where things live, the same whether it runs from a source
checkout (python -m glowstage) or as the installed app built by PyInstaller."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Glowstage"
APP_ID = "Glowstage.App"            # Windows AppUserModelID: taskbar grouping and Start menu pins
VERSION = "0.9.4"
REPO = "BDSuspiciouS/glowstage"
DONATE = {                          # shown as "Support Glowstage" in the app when set
    "GitHub Sponsors": "https://github.com/sponsors/BDSuspiciouS",
    "Ko-fi": "",
}

FROZEN = bool(getattr(sys, "frozen", False))
# Where the program is: the folder of Glowstage.exe, or the source checkout.
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]
# Where read-only resources are unpacked (PyInstaller puts them next to the exe in _internal).
RES_DIR = Path(getattr(sys, "_MEIPASS", INSTALL_DIR))
PKG_DIR = Path(__file__).resolve().parent
UI_DIR = PKG_DIR / "ui"
MODEL_DIR = RES_DIR / "tools" / "yamnet"

_roaming = Path(os.environ.get("APPDATA", Path.home()))
_local = Path(os.environ.get("LOCALAPPDATA", _roaming))
APP_DIR = _roaming / APP_NAME           # settings, lights, presets, log
DATA_DIR = _local / APP_NAME            # downloads the app manages itself (OpenRGB)
LEGACY_APP_DIR = _roaming / "RGBControl"   # where the app kept its settings before it was renamed


def launch_command(*args: str) -> list[str]:
    """How to start this app again (autostart, restart), without a console window."""
    if FROZEN:
        return [sys.executable, *args]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else sys.executable), str(INSTALL_DIR / "Glowstage.pyw"), *args]
