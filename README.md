# Glowstage

**Turn your room into a stage.** Glowstage drives your PC's RGB, your LED strips and your smart lights together from one app, and puts on a festival-style light show to whatever your PC is playing: Spotify, YouTube, games. No microphone, no cloud, no account.

- **A real light show, not a VU meter.** A virtual lighting operator locks onto the beat, counts bars and phrases, hears build-ups coming, holds its breath before the drop and hits it on the one. It learns the song while it plays, so the second chorus looks like the first.
- **It knows what it's listening to.** An offline AI model (Google's YAMNet) recognises the genre, and every genre gets its own choreography: hard step chases for techno, a disco floor for house, everything glowing with the 808 for hip-hop, slow washes for chill. Colors follow the song's chords.
- **Everything moves together.** One clock, one stage: chases run across the whole room, from the monitor strip over the keyboard to the fans in your case, which spin with the beat.
- **Works out of the box.** Lights on your network are found automatically; your PC's RGB is driven through OpenRGB, which Glowstage starts for you.
- **Lives in the tray.** Starts with Windows if you want, switches your lights on at boot and off at shutdown, and stays out of the way.

## Supported lights

| Lights | How |
|---|---|
| Motherboards, RAM, GPUs, coolers, fans, keyboards, mice, mousepads, headsets: the 300+ devices [OpenRGB](https://openrgb.org) supports | OpenRGB's SDK server, started by Glowstage |
| [WLED](https://kno.wled.ge) strips and matrices | Found automatically; UDP realtime streaming at up to 50 fps |
| Nanoleaf (Shapes, Canvas, Light Panels, Lines, Elements) | Found automatically; press Pair and hold the power button |
| ASRock Radeon graphics cards (not supported by OpenRGB) | Directly over the card's I2C bus through the AMD driver |
| QMK keyboards that speak SignalRGB's protocol (e.g. MCHOSE GX87), per key | Directly over USB |

More are on the way: Philips Hue, Govee, LIFX, Yeelight, Elgato, Twinkly, Home Assistant, and DMX stage lights over Art-Net/sACN.

## Install

1. Download the latest `Glowstage-x.y.z-windows.zip` from [Releases](https://github.com/BDSuspiciouS/glowstage/releases).
2. Unzip it anywhere (for example `C:\Program Files\Glowstage` or your Documents) and run `Glowstage.exe`.
3. Glowstage finds your lights and adds itself to the Start menu. Pick the **Festival** preset and play some music.

Windows 10 or 11, 64-bit. Windows may warn that the app is from an unknown publisher: choose *More info → Run anyway* (the app isn't code-signed yet).

### Run from source

```
python -m pip install -r requirements.txt
python -m glowstage
```

Python 3.12. `tools/install-shortcut.ps1` adds a source checkout to the Start menu. To preview the UI in a browser with sample data, serve `glowstage/ui` and open `index.html`.

## Support Glowstage

Glowstage is free and open source. If it lights up your room, you can support its development through [GitHub Sponsors](https://github.com/sponsors/BDSuspiciouS). Thank you!

## More

- [The light show, in detail](docs/LIGHT-SHOW.md): how it hears beats, build-ups, drops, genres and chords.
- [Hardware notes](docs/HARDWARE.md): fans on ARGB headers, RAM lighting, Windows Dynamic Lighting and other things worth knowing.
- Settings, lights and presets are kept in `%APPDATA%\Glowstage\config.json`, the log in `%APPDATA%\Glowstage\glowstage.log`.

## License

Glowstage is licensed under the [GNU General Public License v3.0](LICENSE). It uses OpenRGB (GPL-2.0) as a separate program, and the YAMNet model (Apache-2.0) with AudioSet labels (CC BY 4.0) by Google.
