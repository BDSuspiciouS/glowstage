"""The desired lighting state of one device, as set from the UI, tray or a preset."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

MODES = ("color", "effect", "native")


@dataclass
class LightState:
    on: bool = True
    brightness: int = 100          # 0..100
    mode: str = "color"            # color | effect (synced app effect) | native (device's own effect)
    color: str = "#7c4dff"         # primary color
    color2: str = "#00e5ff"        # secondary color used by some effects
    effect: str = "rainbow"        # app effect id, see effects.EFFECTS
    speed: int = 50                # 1..100
    native: str | None = None      # device effect id (WLED fx index, Nanoleaf effect name, OpenRGB mode name)
    palette: int | None = None     # WLED palette index for native effects

    @classmethod
    def from_dict(cls, data: dict | None) -> "LightState":
        known = {f.name for f in fields(cls)}
        state = cls(**{k: v for k, v in (data or {}).items() if k in known})
        return state.normalized()

    def to_dict(self) -> dict:
        return asdict(self)

    def patched(self, patch: dict) -> "LightState":
        data = self.to_dict()
        data.update({k: v for k, v in patch.items() if k in data})
        return LightState.from_dict(data)

    def normalized(self) -> "LightState":
        self.on = bool(self.on)
        self.brightness = int(max(0, min(100, int(self.brightness))))
        self.speed = int(max(1, min(100, int(self.speed))))
        if self.mode not in MODES:
            self.mode = "color"
        return self
