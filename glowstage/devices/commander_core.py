"""Reads the LED count of every RGB port on a Corsair Commander Core.

The controller detects the fans plugged into it and reports how many LEDs each port has,
but OpenRGB doesn't pass that on (its fan channels start at 0 LEDs). We read it ourselves
over HID, using the same commands as liquidctl, so the fan channels can be sized for you.

Only call this while OpenRGB is NOT running: two programs talking to the controller at once
corrupt each other's messages.
"""

from __future__ import annotations

import logging

from .hidio import HidDevice, hid_paths

log = logging.getLogger(__name__)

CORSAIR_VID = 0x1B1C
COMMANDER_CORE_PIDS = {0x0C1C, 0x0C32}

CMD_WAKE = bytes((0x01, 0x03, 0x00, 0x02))
CMD_SLEEP = bytes((0x01, 0x03, 0x00, 0x01))
CMD_OPEN_ENDPOINT = bytes((0x0D, 0x00))
CMD_READ_INITIAL = bytes((0x08, 0x00, 0x01))
CMD_CLOSE_ENDPOINT = bytes((0x05, 0x01, 0x00))
MODE_LED_COUNT = bytes((0x20,))
DATA_TYPE_LED_COUNT = bytes((0x0F, 0x00))


def find_commander_cores() -> list[str]:
    """Device paths of the Commander Core's control interface (MI_00)."""
    return [p for p in hid_paths()
            if f"vid_{CORSAIR_VID:04x}" in p.lower() and "mi_00" in p.lower()
            and any(f"pid_{pid:04x}" in p.lower() for pid in COMMANDER_CORE_PIDS)]


class _CommanderCore(HidDevice):
    def command(self, cmd: bytes, data: bytes = b"") -> bytes:
        self.flush()
        self.write(b"\x08" + cmd + data)          # [report id 0][0x08][command][data]...
        for _ in range(8):
            resp = self.read(1000)
            if resp is None:
                break
            if resp and resp[0] == 0x00:          # other values are unsolicited notifications
                if resp[1] != cmd[0]:
                    raise OSError(0, f"Unexpected Commander Core reply {resp[:4].hex()}")
                return resp
        raise TimeoutError("Commander Core sent no matching reply")


def read_led_counts(path: str) -> dict:
    """{"pump": n | None, "ports": [n | None, ...]} where None means nothing is connected."""
    dev = _CommanderCore(path)
    try:
        dev.command(CMD_WAKE)
        try:
            dev.command(CMD_OPEN_ENDPOINT, MODE_LED_COUNT)
            raw = dev.command(CMD_READ_INITIAL)
            dev.command(CMD_CLOSE_ENDPOINT)
        finally:
            dev.command(CMD_SLEEP)                # hand the lights back to the controller
    finally:
        dev.close()
    if raw[3:5] != DATA_TYPE_LED_COUNT:
        raise OSError(0, f"Unexpected LED count data type {raw[3:5].hex()}")
    data = raw[5:]
    counts = []
    for i in range(data[0]):
        connected = int.from_bytes(data[1 + i * 4:3 + i * 4], "little") == 2
        leds = int.from_bytes(data[3 + i * 4:5 + i * 4], "little")
        counts.append(leds if connected else None)
    return {"pump": counts[0] if counts else None, "ports": counts[1:]}


def detect() -> dict | None:
    """LED counts of the first Commander Core found, or None if there isn't one / it can't be read."""
    for path in find_commander_cores():
        try:
            return read_led_counts(path)
        except (OSError, TimeoutError) as e:
            log.warning("Reading Commander Core LED counts failed: %s", e)
    return None
