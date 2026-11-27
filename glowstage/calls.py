r"""Is a camera or microphone in use? (Setup → Automatic → "On a call", automations.py)

Windows keeps a list of the apps that use the camera and the microphone, under
HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam (and
\microphone). Store apps have a subkey of their own; other programs sit under "NonPackaged", one
subkey per program with "#" for "\" in its path. An app uses the device right now when its
LastUsedTimeStart is set and its LastUsedTimeStop is 0.

Glowstage only reads those two numbers per app, never what the camera or microphone picks up.
"""

from __future__ import annotations

import logging
import winreg

log = logging.getLogger(__name__)

BASE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
DEVICES = {"webcam": "camera", "microphone": "microphone"}
IGNORED = ("glowstage",)    # our own window (a speech test, say) is not a call


class Registry:
    """Reads the registry (read-only). Tests give in_use() a stand-in with the same two methods."""

    def subkeys(self, path: str) -> list[str]:
        """The names under HKCU\\path ([] if it isn't there)."""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                return [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
        except OSError:
            return []

    def value(self, path: str, name: str) -> int | None:
        """A number under HKCU\\path (None if it isn't there)."""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                return int(winreg.QueryValueEx(key, name)[0])
        except (OSError, TypeError, ValueError):
            return None


def _app_name(key: str) -> str:
    """"C:#Program Files#Zoom#bin#Zoom.exe" -> "Zoom.exe"; "Microsoft.WindowsCamera_8wekyb3d8bbwe" -> "Microsoft.WindowsCamera"."""
    if "#" in key or key.lower().endswith(".exe"):
        return key.replace("#", "\\").rsplit("\\", 1)[-1]
    return key.split("_", 1)[0]


def _using(reg, path: str) -> bool:
    return bool(reg.value(path, "LastUsedTimeStart")) and not reg.value(path, "LastUsedTimeStop")


def in_use(reg=None) -> dict[str, list[str]]:
    """{"camera": ["Zoom.exe"], "microphone": [...]} for what's in use now; {} if nothing is.
    Never raises: a missing key is normal (nothing has used that device yet)."""
    reg = reg or Registry()
    found: dict[str, list[str]] = {}
    for cap, device in DEVICES.items():
        base = f"{BASE}\\{cap}"
        try:
            keys = [(k, f"{base}\\{k}") for k in reg.subkeys(base) if k != "NonPackaged"]
            keys += [(k, f"{base}\\NonPackaged\\{k}") for k in reg.subkeys(f"{base}\\NonPackaged")]
            names = sorted({_app_name(k) for k, path in keys if _using(reg, path)})
        except Exception as e:      # (never in the way of the automations loop)
            log.debug("Couldn't read %s use: %s", device, e)
            continue
        names = [n for n in names if not n.lower().startswith(IGNORED)]
        if names:
            found[device] = names
    return found


def describe(found: dict[str, list[str]]) -> str:
    """For the log: "camera (Zoom.exe), microphone (Zoom.exe)"."""
    return ", ".join(f"{d} ({', '.join(a)})" for d, a in found.items())
