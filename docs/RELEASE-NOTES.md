Glowstage 0.9.7: a phone remote, games flash along, your keyboard ripples as you type, every app can have its own look, and a sleep timer.

**Install:** download `Glowstage-0.9.7-windows.zip`, unzip it anywhere and run `Glowstage.exe`. It adds itself to the Start menu. Windows may say the publisher is unknown: choose *More info → Run anyway*. **Updating?** From 0.9.4, click *Update now* on Home. From an older version, quit Glowstage from its tray icon and unzip over the old folder: your lights and settings stay.

**New**
- Phone remote (Setup → Phone remote): scan the code with your phone and control your lights on the same Wi-Fi: all on or off, brightness, presets, the light show and the sleep timer, on a page made for thumbs. It's off until you switch it on, and it only answers to the code's link, which carries a secret key (*New link* shuts out the old one). Windows asks once whether Glowstage may use the network: allow it for private networks.
- Screen + sound, a new look for any game: your lights follow the screen, and every shot or explosion flashes them on the side it came from (a shot on your left lights the left of your setup; lights behind you flash along). It listens to the crack of a sound, so footsteps, engines, hi-hats and kick drums don't flash, and it never flashes more than three times a second. Pick it for full-screen games (Setup → Automatic) and it's there whenever you play.
- Typing ripples, a new look: every key you press lights up and sends a ripple across your keyboard (on keyboards whose LED layout Glowstage knows, like the MCHOSE GX87), and your other lights glow where the key sits, left to right. Speed sets how quickly it fades, the two colors the flash and the background. While it runs, Glowstage asks Windows 60 times a second which keys are down: it never sits between your keyboard and your programs, so typing and games feel the same. It keeps a key for three seconds as a spot on the board, and saves and sends nothing.
- A look per app (Setup → Automatic → Apps): pick an app that's open, choose its look (a preset, Screen sync or the light show), and your lights change while it's open and go back when you close it. *Focus* while Photoshop is open, *Gaming* while OBS is. It counts once the app has been open for a few seconds, so switching around doesn't flick the lights; the first app in the list goes first, before full-screen games and videos. Checking costs a fifth of a millisecond a second.
- Sleep timer (Home, or the tray): the lights that are on fade out slowly over 15 minutes to an hour and a half, like a sunset, and switch off at the end. Next time they come on as they were; a light you change meanwhile is left alone, and cancelling puts the brightness back.
- Undo: picked a preset over the colors you'd just made, or switched everything off? *Undo* in the message that pops up puts your lights back as they were (also in the tray, and Ctrl+Alt+Shift+Z). Undo again to redo.
- More hotkeys (Setup → App → Hotkeys): Ctrl+Alt+Shift+Up and Down for brighter and darker (hold to keep going), +N for the sleep timer, +Z to undo.
- Report a problem (Setup → App): shows you a short summary first (your version, Windows, your lights and whether they answer, the last warnings from the log; never a key or a password), then opens a GitHub issue with it filled in, or copies it.
- After an update, Home says what's new in a short card, with a *Show me* for each new thing that takes you right to it.
- Fixed: after an update the window could stay empty. It was loaded through a small web server on a fixed port, and WebView2 kept an old page in its cache next to the new scripts. It now opens straight from its files.
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
