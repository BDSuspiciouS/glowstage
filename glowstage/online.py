"""What Glowstage asks GitHub once a day (unless switched off in Setup): whether a newer version is
out, and where people can support the project. Nothing about the user is sent: two plain requests
for public files of the project, like a browser opening its page.

The support links come from the project's .github/FUNDING.yml (the same file that makes GitHub's
"Sponsor" button), so a new way to donate reaches everyone without a new version of the app.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request

from .paths import DONATE, REPO, VERSION

log = logging.getLogger(__name__)

LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
FUNDING_URL = f"https://raw.githubusercontent.com/{REPO}/main/.github/FUNDING.yml"
EVERY = 24 * 3600

# Where a donation may go: only these sites are ever opened from the support links.
SITES = {
    "github.com": "GitHub Sponsors", "ko-fi.com": "Ko-fi", "www.ko-fi.com": "Ko-fi",
    "paypal.me": "PayPal", "www.paypal.me": "PayPal", "www.paypal.com": "PayPal", "paypal.com": "PayPal",
    "buy.stripe.com": "Card or iDEAL", "donate.stripe.com": "Card or iDEAL",
    "buymeacoffee.com": "Buy Me a Coffee", "www.buymeacoffee.com": "Buy Me a Coffee",
    "patreon.com": "Patreon", "www.patreon.com": "Patreon", "liberapay.com": "Liberapay",
    "opencollective.com": "Open Collective",
}
PLATFORMS = {   # FUNDING.yml key -> address
    "github": "https://github.com/sponsors/{}", "ko_fi": "https://ko-fi.com/{}",
    "patreon": "https://www.patreon.com/{}", "buy_me_a_coffee": "https://buymeacoffee.com/{}",
    "liberapay": "https://liberapay.com/{}", "open_collective": "https://opencollective.com/{}",
}


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3]) or (0,)


def allowed(url: str) -> bool:
    m = re.match(r"https://([^/]+)/", url + "/")
    return bool(m and m.group(1).lower() in SITES and not (m.group(1) == "github.com" and "/sponsors/" not in url))


def label(url: str) -> str:
    m = re.match(r"https://([^/]+)/", url + "/")
    return SITES.get(m.group(1).lower(), "Donate") if m else "Donate"


def parse_funding(text: str) -> list[dict]:
    """The support links in a FUNDING.yml: `github: [name]`, `ko_fi: name`, `custom: [url, ...]`..."""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        values = [v.strip().strip("'\"") for v in value.strip().strip("[]").split(",") if v.strip()]
        for v in values:
            url = v if key.strip() == "custom" else PLATFORMS.get(key.strip(), "").format(v) if key.strip() in PLATFORMS else ""
            if url and allowed(url) and not any(o["url"] == url for o in out):
                out.append({"name": label(url), "url": url})
    return out


def builtin_links() -> list[dict]:
    return [{"name": name, "url": url} for name, url in DONATE.items() if url and allowed(url)]


class Online:
    def __init__(self, config, on_change):
        self.config = config
        self.on_change = on_change
        self._stop = threading.Event()

    @property
    def cache(self) -> dict:
        return self.config.data.setdefault("online", {})

    def start(self) -> None:
        threading.Thread(target=self._run, name="online", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def check_now(self) -> None:
        threading.Thread(target=self._check, name="online-now", daemon=True).start()

    def _run(self) -> None:
        if self._stop.wait(20):          # let the app start first
            return
        while True:
            if self.config["settings"].get("check_updates", True) and \
                    time.time() - self.cache.get("checked", 0) > EVERY - 60:
                self._check()
            if self._stop.wait(3600):
                return

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": f"Glowstage/{VERSION}",
                                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(200_000)

    def _check(self) -> None:
        changed = ok = False
        try:
            rel = json.loads(self._get(LATEST_URL))
            latest = {"version": str(rel.get("tag_name", "")).lstrip("v"), "url": rel.get("html_url", "")}
            if latest["version"] and latest != self.cache.get("latest"):
                self.cache["latest"], changed = latest, True
            ok = True
        except Exception as e:      # offline, or the project isn't public yet: try again tomorrow
            log.info("No update check: %s", e)
        try:
            links = parse_funding(self._get(FUNDING_URL).decode("utf-8", "replace"))
            if links != self.cache.get("donate"):
                self.cache["donate"], changed = links, True
            ok = True
        except Exception as e:
            log.info("No support links from GitHub: %s", e)
        if ok:      # (when both failed, the hourly loop tries again instead of waiting a day)
            self.cache["checked"] = time.time()
            self.config.save_soon()
        if changed:
            self.on_change()

    # ---- for the window ------------------------------------------------------------------

    def donate_links(self) -> list[dict]:
        return self.cache.get("donate") or builtin_links()

    def update(self) -> dict | None:
        latest = self.cache.get("latest") or {}
        if latest.get("version") and _version(latest["version"]) > _version(VERSION) and \
                str(latest.get("url", "")).startswith(f"https://github.com/{REPO}/releases"):
            return latest
        return None
