# Glowstage

**Turn your room into a stage.** Glowstage drives your PC's RGB, your LED strips and your smart lights together from one app, and puts on a festival-style light show to whatever your PC is playing: Spotify, YouTube, games. No microphone, no cloud, no account.

- **A real light show, not a VU meter.** A virtual lighting operator locks onto the beat, counts bars and phrases, hears build-ups coming, holds its breath before the drop and hits it on the one. It learns the song while it plays, so the second chorus looks like the first.
- **It knows what it's listening to.** An offline AI model (Google's YAMNet, or with one click the enhanced genre AI that knows 400 music styles, from deep house to trap) recognises the genre, and every genre gets its own choreography: hard step chases for techno, a disco floor for house, everything glowing with the 808 for hip-hop, slow washes for chill. Colors follow the song's chords.
- **Everything moves together.** One clock, one stage: chases run across the whole room, from the monitor strip over the keyboard to the fans in your case, which spin with the beat.
- **Works out of the box.** A short welcome tour finds your lights, fetches OpenRGB for your PC's RGB if you want it, and shows you where everything stands on the stage.
- **See the show on screen.** The stage view draws every light where it is in your room, glowing live in the colors it shows; drag lights into place so chases run the right way.
- **Lives in the tray.** Starts with Windows if you want, switches your lights on at boot and off at shutdown, and stays out of the way.

## Supported lights

| Lights | How |
|---|---|
| Motherboards, RAM, GPUs, coolers, fans, keyboards, mice, mousepads, headsets: the 300+ devices [OpenRGB](https://openrgb.org) supports | OpenRGB's SDK server, started by Glowstage |
| [WLED](https://kno.wled.ge) strips and matrices | Found automatically; UDP realtime streaming at up to 50 fps |
| Nanoleaf (Shapes, Canvas, Light Panels, Lines, Elements) | Found automatically; press Pair and hold the power button |
| ASRock Radeon graphics cards (not supported by OpenRGB) | Directly over the card's I2C bus through the AMD driver |
| QMK keyboards that speak SignalRGB's protocol (e.g. MCHOSE GX87), per key | Directly over USB |
| Philips Hue *(beta)* | Bridge found automatically; press the link button, then Pair |
| LIFX *(beta)* | Found automatically on the local network |
| Govee *(beta)* | Found automatically once LAN Control is on in the Govee Home app |
| Yeelight *(beta)* | Found automatically once LAN Control is on in the Yeelight app |
| Elgato Key Light and Light Strip *(beta)* | Found automatically |
| Twinkly *(beta)* | Found automatically; realtime streaming |
| Magic Home / Flux LED WiFi controllers *(beta)* | Found automatically |
| Anything in Home Assistant *(beta)* | Add your Home Assistant address and a long-lived access token |
| DMX stage lights over Art-Net or sACN *(beta)* | Art-Net nodes found automatically; patch your fixtures in Setup |

Lights marked *beta* are new and haven't been tested on every model yet. If one misbehaves, please [open an issue](https://github.com/BDSuspiciouS/glowstage/issues).

## Install

1. Download the latest `Glowstage-x.y.z-windows.zip` from [Releases](https://github.com/BDSuspiciouS/glowstage/releases).
2. Unzip it anywhere (for example `C:\Program Files\Glowstage` or your Documents) and run `Glowstage.exe`.
3. The welcome tour finds your lights and, if you want, gets OpenRGB for the RGB inside your PC. Glowstage adds itself to the Start menu. Press **Start the light show** and play some music.

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

Glowstage is licensed under the [GNU General Public License v3.0](LICENSE). It uses OpenRGB (GPL-2.0) as a separate program, and the YAMNet model (Apache-2.0) with AudioSet labels (CC BY 4.0) by Google. The enhanced genre AI, Discogs-EffNet by the Music Technology Group (Universitat Pompeu Fabra), is not part of Glowstage: when you ask for it, Glowstage downloads it from its makers, under its own license (CC BY-NC-SA 4.0, non-commercial use).
