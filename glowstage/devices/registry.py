"""Every kind of network light the app knows: how it is found, added by hand, paired and made.

Adding a brand is one module with a Driver subclass (plus its Device, or a Hub for bridges that
control many lights) and one line in DRIVERS at the bottom of this file. The manager, the
discovery, the "Add a light" dialog and pairing then all work for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import Device
from .hub import Hub


@dataclass
class Found:
    """A light (or a hub) seen on the network, or checked after adding it by hand."""
    kind: str
    dev_id: str                    # stable id, e.g. "lifx:d073d5123456" (survives IP changes)
    host: str
    port: int = 0
    label: str = ""                # its name, as the device reports it
    extra: dict = field(default_factory=dict)   # anything else worth keeping in the config entry

    def entry(self) -> dict:
        return {"kind": self.kind, "host": self.host, "port": self.port, "label": self.label, **self.extra}


class Driver:
    kind = ""                      # short id: config entries, device ids ("hue:..."), the UI
    title = ""                     # shown in the UI: "Philips Hue"
    hub = False                    # one entry controls many lights (create() returns a Hub)
    mdns: tuple[str, ...] = ()     # mDNS service types that announce it, e.g. "_hue._tcp.local."
    pairing: str = ""              # what to do before pressing Pair; "" = no pairing needed
    # Fields for "Add a light by hand" (key, label, placeholder, and "secret": True for tokens).
    fields: tuple[dict, ...] = ({"key": "host", "label": "IP address", "placeholder": "192.168.1.40"},)
    beta = False                   # written to the protocol but not yet confirmed on real hardware

    def from_mdns(self, service: str, name: str, host: str, port: int, props: dict) -> Found | None:
        """Turn an mDNS announcement into a Found (None to ignore it)."""
        return None

    def scan(self, timeout: float = 2.0) -> list[Found]:
        """Look for devices that don't announce themselves over mDNS (UDP broadcast etc.).
        Called every minute on a background thread; must not raise."""
        return []

    def probe(self, values: dict) -> Found:
        """"Add a light by hand": check the values from `fields` against the device and return
        what to add. Raise ValueError with a readable message when it can't be reached."""
        raise ValueError(f"{self.title} lights can't be added by hand")

    def create(self, dev_id: str, entry: dict) -> Device | Hub:
        """Make the Device (or the Hub) for a config entry. Don't touch the network here:
        connect() is called afterwards on a background thread."""
        raise NotImplementedError

    def pair(self, target: Device | Hub) -> dict:
        """Pair with the device (after the user did what `pairing` says). Return what to store
        in its config entry, e.g. {"token": "..."}. Raise NotPairedError with a readable
        message when it isn't in pairing mode."""
        return {}

    def describe(self) -> dict:
        return {"kind": self.kind, "title": self.title, "hub": self.hub, "pairing": self.pairing,
                "fields": list(self.fields), "beta": self.beta}


MODULES = ("wled", "nanoleaf", "hue", "govee", "lifx", "yeelight", "wiz", "elgato", "twinkly", "magichome",
           "homeassistant", "dmx")


def _drivers() -> dict[str, Driver]:
    import importlib
    import logging

    out: dict[str, Driver] = {}
    for name in MODULES:
        try:
            driver = importlib.import_module(f"{__package__}.{name}").Driver()
        except Exception as e:   # one broken driver must not take the others down
            logging.getLogger(__name__).warning("Light driver %s unavailable: %s", name, e)
            continue
        out[driver.kind] = driver
    return out


DRIVERS: dict[str, Driver] = {}


def drivers() -> dict[str, Driver]:
    if not DRIVERS:
        DRIVERS.update(_drivers())
    return DRIVERS
