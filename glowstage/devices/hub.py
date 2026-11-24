"""A bridge that controls many lights: a Philips Hue bridge, Home Assistant, a DMX node.

The hub itself isn't a light. connect() talks to it and fills `devices` with one Device per
light; the manager registers those like any other light and calls connect() on each.
"""

from __future__ import annotations

from .base import Device


class NotPairedError(Exception):
    """The device or hub needs pairing (or new credentials) first; the message says how."""


class Hub:
    kind = ""

    def __init__(self, hub_id: str, entry: dict):
        self.id = hub_id
        self.entry = entry                 # its config entry (host, token, ...), kept up to date
        self.host = entry.get("host", "")
        self.name = entry.get("label") or self.kind
        self.devices: dict[str, Device] = {}
        self.online = False
        self.error: str | None = None

    @property
    def paired(self) -> bool:
        """False while it still needs pairing (no token yet)."""
        return True

    def connect(self) -> None:
        """Talk to the hub and (re)fill `devices`. Raise NotPairedError when it needs pairing,
        anything else when it can't be reached."""
        raise NotImplementedError

    def close(self) -> None:
        """Stop any background thread (e.g. a DMX sender)."""

    def describe(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "host": self.host,
                "online": self.online, "error": self.error, "paired": self.paired,
                "lights": len(self.devices)}
