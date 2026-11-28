"""Finds network lights: over mDNS for brands that announce themselves (WLED, Nanoleaf, Hue,
Elgato, Home Assistant), and with each brand's own broadcast scan once a minute for the rest
(Govee, LIFX, Yeelight, Twinkly, Magic Home, Art-Net nodes)."""

from __future__ import annotations

import logging
import socket
import threading

from .registry import Driver, Found, drivers

log = logging.getLogger(__name__)

SCAN_EVERY = 60.0


class Discovery:
    def __init__(self, on_found):
        self.on_found = on_found  # (Found) -> None
        self._zc = None           # the zeroconf.Zeroconf, once the mDNS thread has made it
        self._browsers: list = []
        self._by_service: dict[str, list[Driver]] = {}
        self._stop = threading.Event()
        self._zc_lock = threading.Lock()

    def start(self) -> None:
        for d in drivers().values():
            for service in d.mdns:
                self._by_service.setdefault(service, []).append(d)
        # zeroconf is slow to import and to start, and the tray icon and the lights don't wait for
        # it: it starts on its own thread.
        threading.Thread(target=self._start_mdns, name="mdns", daemon=True).start()
        threading.Thread(target=self._scan_loop, name="scan", daemon=True).start()

    def _start_mdns(self) -> None:
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

            discovery = self

            class Listener(ServiceListener):     # (zeroconf wants its own base class; this one hands over)
                def add_service(self, zc, type_, name):
                    discovery.add_service(zc, type_, name)

                def update_service(self, zc, type_, name):
                    discovery.update_service(zc, type_, name)

                def remove_service(self, zc, type_, name):
                    discovery.remove_service(zc, type_, name)

            with self._zc_lock:
                if self._stop.is_set():
                    return
                self._zc = Zeroconf()
                listener = Listener()
                self._browsers = [ServiceBrowser(self._zc, t, listener) for t in self._by_service]
        except OSError as e:
            log.warning("mDNS discovery unavailable: %s", e)

    def stop(self) -> None:
        self._stop.set()
        with self._zc_lock:
            zc, self._zc = self._zc, None
        if zc:
            zc.close()

    def scan_now(self) -> None:
        """Run every brand's broadcast scan once, right away (the setup wizard's "Search")."""
        threading.Thread(target=self._scan_once, name="scan-now", daemon=True).start()

    # ---- mDNS ----------------------------------------------------------------------------

    def add_service(self, zc, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def update_service(self, zc, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def remove_service(self, zc, type_: str, name: str) -> None:
        pass

    def _resolve(self, zc, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info or not info.addresses:
            return
        host = socket.inet_ntoa(info.addresses[0])
        props = {k.decode(): (v.decode(errors="replace") if v else "") for k, v in info.properties.items()}
        label = name[: -len(type_) - 1] if name.endswith(type_) else name
        for d in self._by_service.get(type_, []):
            try:
                found = d.from_mdns(type_, label, host, info.port, props)
                if found:
                    self.on_found(found)
            except Exception:
                log.exception("handling discovered %s %s failed", d.kind, name)

    # ---- broadcast scans ---------------------------------------------------------------------

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            self._scan_once()
            self._stop.wait(SCAN_EVERY)

    def _scan_once(self) -> None:
        threads = [threading.Thread(target=self._scan_driver, args=(d,), name=f"scan:{d.kind}", daemon=True)
                   for d in drivers().values() if type(d).scan is not Driver.scan]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

    def _scan_driver(self, d: Driver) -> None:
        try:
            found: list[Found] = d.scan(2.0)
        except Exception:
            log.exception("%s scan failed", d.kind)
            return
        for f in found:
            try:
                self.on_found(f)
            except Exception:
                log.exception("handling discovered %s %s failed", f.kind, f.dev_id)
