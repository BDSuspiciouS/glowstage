# Hardware notes

## PC lighting through OpenRGB

- **Fans and pumps on motherboard ARGB headers** each get their own fader. ARGB fans can't report how many LEDs they have, so set the count per header under *Settings → Lights* (for example: three 8-LED fans on one header = 24). Many fans have 8 LEDs (e.g. Corsair RS120 ARGB); others have 12-24.
- **Windows Dynamic Lighting** (*Settings → Personalization → Dynamic Lighting*) should be off. Some motherboard RGB chips also offer themselves to Windows, and while Windows owns them, nothing else can drive the ARGB headers.
- **RAM lighting** (e.g. Corsair Vengeance RGB) needs OpenRGB running as administrator with the PawnIO driver. Under *Settings → PC lighting*, press **Restart as administrator** and approve the prompt once: Glowstage then registers a scheduled task (`Glowstage\OpenRGB`, "run with highest privileges") and starts OpenRGB through it without asking again.
- **Corsair Commander Core**: just before starting OpenRGB, Glowstage asks it how many LEDs each RGB port has and sizes its channels to match (OpenRGB on its own leaves them at 0, which leaves fans dark or glitching).
- **Gigabyte boards with an ITE IT5711 RGB chip**: if the ARGB headers stop responding and the chip shows up as USB `048D:57DB` ("ITE Upgrade Mode"), it is stuck in its firmware bootloader; Gigabyte's "IT5711 RGB lighting controller firmware upgrade package" from the board's support page restores it.
- **Lights still on after shutdown**: Glowstage switches every light off when Windows shuts down. If fans or the pump still glow afterwards, the board powers its ARGB headers in standby with its own lighting: set the BIOS option for LEDs in sleep/soft-off to Off (Gigabyte: "LEDs in Sleep, Hibernation, and Soft Off States"), or enable ErP.

## ASRock Radeon graphics cards

OpenRGB doesn't support them; Glowstage drives them directly over the card's I2C bus through the AMD driver, with the commands SignalRGB's ASRock plugin uses (address `0x36` on the OEM line). Every color command makes the card's LEDs blink briefly, so the card shows a still frame of an effect instead of animating it (its fader says "· still"), and only gets new commands when the color, brightness or power changes.

## Keyboards

QMK keyboards that speak SignalRGB's lighting protocol show up by themselves when connected by cable (not over 2.4 GHz or Bluetooth), with every key on its own. Keys are ordered left to right so waves and chases sweep across them; *Built-in effects → Keyboard's own lighting* hands the keyboard back to its own effects. Don't run SignalRGB at the same time.

## WLED

Synced effects stream over UDP (DNRGB). The stream never times out on the strip: if WiFi drops a few frames, the strip holds the last frame instead of falling back to its own look. A weak WiFi signal shows up as stutter; moving the controller or its antenna helps.

## Nanoleaf

Press **Pair** on its fader, hold the controller's power button for 5-7 seconds until the panels flash, then press **Pair** in the dialog.
