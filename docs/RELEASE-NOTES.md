Glowstage 0.9.4: updates with one click.

**Install:** download `Glowstage-0.9.4-windows.zip`, unzip it anywhere and run `Glowstage.exe`. It adds itself to the Start menu. Windows may say the publisher is unknown: choose *More info → Run anyway*. **Updating from an older version?** Quit Glowstage from its tray icon, then unzip over the old folder: your lights and settings stay. This is the last time you have to do that by hand.

**New**
- Updates with one click: when a new version is out, Home says so with an *Update now* button. Glowstage downloads the new version from its GitHub release, checks it against the SHA-256 checksum GitHub lists for it, puts it in place of itself and restarts, in a few seconds. If anything can't be replaced (a virus scanner holding a file), it puts the old version back, so a failed update never leaves a broken app.

**New in 0.9.3**
- Counter-Strike 2 lighting (Setup → Games → Connect): your team's color, your health as a bar across the room, white when you're flashed, the bomb blinking ever faster, a pop for every kill. It uses Valve's own game state integration, so nothing touches the game and it's safe for VAC.

**New in 0.9.2**
- Automatic looks (Setup → Automatic): a full-screen game or video can switch your lights to Screen sync or a preset (*Gaming*, *Movie*), and they go back afterwards. Change anything yourself and Glowstage leaves your lights alone until it's over.

**New in 0.9.1**
- The light show can start by itself when music plays (*Start by itself* on the Light show page) and puts your lights back 30 seconds after the music stops. It waits for music with a beat, so talk, games and notification sounds don't set it off.
- Screen sync follows the picture: every light takes the part of the screen where it sits on the stage. It takes half the processor time it used to.
- Fixed: when a song started after a pause, listening could stop for two seconds, so the light show missed the first beats.

**Everything in Glowstage**
- A festival-style light show to whatever your PC plays: beat-locked, hears build-ups and drops, learns the song while it plays, checked against an hour of real songs.
- Offline AI genre detection with its own choreography per genre, colors that follow the chords; one click away, an enhanced genre AI that knows 400 music styles.
- Sync for your speakers: Bluetooth headphones and speakers are detected and the lights wait for their sound.
- Presets that know your PC from your room (Focus, Reading, Cozy, Gaming, Movie and more), from the tray, a hotkey or a schedule.
- A live stage view of every light in your room; choose which lights join the show.
- Slow lights (Hue, Govee, Home Assistant, Elgato) get calm versions of the fast looks.
- PC RGB through OpenRGB, WLED, Nanoleaf, ASRock Radeon cards and SignalRGB-protocol QMK keyboards; in beta: Philips Hue, LIFX, Govee, Yeelight, Elgato, Twinkly, Magic Home, Home Assistant and DMX (Art-Net / sACN).
- Lives in the tray, lights on at boot and off at shutdown, tells you when a new version is out.

**Needs:** Windows 10/11 64-bit. For PC RGB, OpenRGB: the welcome tour (or Setup → PC lighting) gets it for you.
