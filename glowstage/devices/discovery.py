"""Finds WLED and Nanoleaf lights on the local network over mDNS."""

from __future__ import annotations

import logging
import socket

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

log = logging.getLogger(__name__)

SERVICES = {
    "_wled._tcp.local.": "wled",
    "_nanoleafapi._tcp.local.": "nanoleaf",
}


class Discovery(ServiceListener):
    def __init__(self, on_found):
        self.on_found = on_found  # (kind, dev_id, host, port, name)
        self._zc: Zeroconf | None = None
        self._browsers: list[ServiceBrowser] = []

    def start(self) -> None:
        try:
            self._zc = Zeroconf()
            self._browsers = [ServiceBrowser(self._zc, t, self) for t in SERVICES]
        except OSError as e:
            log.warning("mDNS discovery unavailable: %s", e)

    def stop(self) -> None:
        if self._zc:
            self._zc.close()
            self._zc = None

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def _resolve(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info or not info.addresses:
            return
        kind = SERVICES[type_]
        host = socket.inet_ntoa(info.addresses[0])
        props = {k.decode(): (v.decode(errors="replace") if v else "") for k, v in info.properties.items()}
        label = name[: -len(type_) - 1] if name.endswith(type_) else name
        if kind == "wled":
            mac = props.get("mac", "").lower().replace(":", "")
            dev_id = f"wled:{mac}" if mac else f"wled:{host}"
        else:
            dev_id = f"nanoleaf:{props.get('id', host).upper()}"
        try:
            self.on_found(kind, dev_id, host, info.port, label)
        except Exception:
            log.exception("handling discovered device %s failed", name)
