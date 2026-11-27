"""Copies of your presets, lights and settings (config.json): to go back when something went
wrong, and a file of them to take to another PC (Setup → Backups).

The first save of each day you use Glowstage keeps the file as it was, named after the day it
was last saved, in %APPDATA%\\Glowstage\\backups; the last KEEP of those stay. Bringing one back
or loading a file first keeps what you have now as well (a "-before" copy). A config.json that
can't be read is replaced by the newest copy that can, instead of starting with nothing.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

KEEP = 14           # days
KEEP_BEFORE = 5     # copies made before bringing one back
NAME = re.compile(r"^config-\d{4}-\d\d-\d\d(-\d{6}-before)?\.json$")


def folder_for(config_path: Path) -> Path:
    return config_path.parent / "backups"


def keep_daily(config_path: Path, today: datetime.date | None = None) -> Path | None:
    """Before a save: a copy of config.json as it is, if it was last saved before today and that
    day has no copy yet. The copy made, if any."""
    try:
        saved = datetime.date.fromtimestamp(config_path.stat().st_mtime)
    except OSError:
        return None
    if saved >= (today or datetime.date.today()):
        return None
    target = folder_for(config_path) / f"config-{saved.isoformat()}.json"
    return None if target.exists() else _copy(config_path, target)


def keep_before(config_path: Path) -> Path | None:
    """What you have now, before something else takes its place."""
    if not config_path.is_file():
        return None
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return _copy(config_path, folder_for(config_path) / f"config-{stamp}-before.json")


def _copy(source: Path, target: Path) -> Path | None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)        # (keeps the time it was saved: the list shows it)
    except OSError as e:
        log.warning("Could not keep a copy of the settings: %s", e)
        return None
    prune(target.parent)
    return target


def prune(folder: Path) -> None:
    files = sorted(f for f in folder.glob("config-*.json") if NAME.match(f.name))   # (by day)
    daily = [f for f in files if not f.stem.endswith("-before")]
    before = [f for f in files if f.stem.endswith("-before")]
    for f in daily[:-KEEP] + before[:-KEEP_BEFORE]:
        try:
            f.unlink()
        except OSError:
            pass


def valid(data, loose: bool = False) -> bool:
    """Glowstage's settings: a dict with a list of presets that have names, settings a dict.
    Loose: only a dict (the app's own config.json, which fills in what's missing)."""
    if not isinstance(data, dict):
        return False
    return loose or (isinstance(data.get("presets"), list)
                     and all(isinstance(p, dict) and isinstance(p.get("name"), str) for p in data["presets"])
                     and isinstance(data.get("settings", {}), dict) and isinstance(data.get("states", {}), dict))


def read(path: Path) -> dict | None:
    """The settings in this file, or None when it isn't (all of) Glowstage's settings."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if valid(data) else None


def describe(path: Path, data: dict | None) -> dict:
    try:
        saved = datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")
    except OSError:
        saved = ""
    out = {"file": path.name, "saved": saved, "before": path.stem.endswith("-before"), "ok": data is not None}
    if data is not None:
        out.update(presets=len(data["presets"]), lights=len(data.get("states") or {}))
    return out


def listing(config_path: Path) -> list[dict]:
    """The copies there are, newest first."""
    folder = folder_for(config_path)
    files = [f for f in folder.glob("config-*.json") if NAME.match(f.name)] if folder.is_dir() else []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [describe(f, read(f)) for f in files]


def find(config_path: Path, name: str) -> Path | None:
    """One of the copies, by its file name (only those: never a path from elsewhere)."""
    if not NAME.match(str(name)):
        return None
    path = folder_for(config_path) / name
    return path if path.is_file() else None


def newest_good(config_path: Path) -> tuple[Path, dict] | None:
    """The newest copy that can be read, for a config.json that can't."""
    for item in listing(config_path):
        if item["ok"]:
            path = folder_for(config_path) / item["file"]
            data = read(path)
            if data is not None:
                return path, data
    return None
