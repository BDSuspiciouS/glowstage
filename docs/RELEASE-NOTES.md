Glowstage 0.9.7: your keyboard ripples as you type.

**Install:** download `Glowstage-0.9.7-windows.zip`, unzip it anywhere and run `Glowstage.exe`. It adds itself to the Start menu. Windows may say the publisher is unknown: choose *More info → Run anyway*. **Updating?** From 0.9.4, click *Update now* on Home. From an older version, quit Glowstage from its tray icon and unzip over the old folder: your lights and settings stay.

**New**
- Typing ripples, a new look: every key you press lights up and sends a ripple across your keyboard (on keyboards whose LED layout Glowstage knows, like the MCHOSE GX87), and your other lights glow where the key sits, left to right. Speed sets how quickly it fades, the two colors the flash and the background. While it runs, Glowstage asks Windows 60 times a second which keys are down: it never sits between your keyboard and your programs, so typing and games feel the same. It keeps a key for three seconds as a spot on the board, and saves and sends nothing.
- The log now names an error a light reports without a message (like OpenRGB dropping its connection).

**New in 0.9.6**
- Lights off when you step away (Setup → Automatic): when you lock the PC, its screen goes off or it goes to sleep, the PC's lights (or all of them, your choice) switch off, and they come back as they were when you return. A light that a schedule changes meanwhile keeps its new look, and if Windows shuts down while you're away, your lights still come back next time. Off until you switch it on.
- No automatic looks (games, videos, music) while you're away.
- Fixed: the window that hears Windows shut down was set up with a cut-off handle, which could write needless errors into the crash log.

**New in 0.9.5**
- Follow the sun: a new look and preset that lights your room like the day outside. Cool and bright at noon, golden as the sun sets, warm in the evening, and from 23:00 a low amber glow for the night. Sunrise and sunset come from where you live (Windows' region setting), worked out on your PC: no location access, no internet. Use it as a preset, from the tray, or on a schedule.
- The speed slider hides for looks that don't use it (Screen sync, Follow the sun).

**New in 0.9.4**
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
- Presets that know your PC from your room (Follow the sun, Focus, Reading, Cozy, Gaming, Movie and more), from the tray, a hotkey or a schedule.
- A live stage view of every light in your room; choose which lights join the show.
- Slow lights (Hue, Govee, Home Assistant, Elgato) get calm versions of the fast looks.
- PC RGB through OpenRGB, WLED, Nanoleaf, ASRock Radeon cards and SignalRGB-protocol QMK keyboards; in beta: Philips Hue, LIFX, Govee, Yeelight, Elgato, Twinkly, Magic Home, Home Assistant and DMX (Art-Net / sACN).
- Lives in the tray, lights on at boot and off at shutdown or when you step away, updates itself with one click.

**Needs:** Windows 10/11 64-bit. For PC RGB, OpenRGB: the welcome tour (or Setup → PC lighting) gets it for you.
