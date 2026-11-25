# The light show, in detail

Pick **Light show** under *Synced effects → Music*, or the **Festival** preset. A virtual lighting operator (`glowstage/show.py`) then runs every light at once. Speed sets how wild it gets; strobe flashes can be switched off under *Settings → App*.

## Listening

Glowstage listens to the PC's default sound output through Windows (WASAPI loopback), so it hears exactly what you hear, without a microphone, and follows a switch between headphones and speakers. It only listens while a music effect is on. Every ~21 ms it measures loudness, 16 frequency bands, kicks, snares and claps, hi-hats, the sound's centre of weight (in octaves) and, every ~85 ms, the notes being played.

## Beat, bar and phrase

- The tempo comes from the autocorrelation of the rhythm over the last 8 s (~±1 BPM). Slips to 1/2 or 2/3 of the tempo during a snare roll are ignored; within a song the tempo only changes when the new one holds for 8 s.
- A beat clock locks onto the beat grid of the last 8 s, kept on the kick rather than the offbeat bass and hi-hats (within ~5-20 ms once settled).
- The "one" of the bar: snares and claps on 2 and 4 narrow it to two beats; chord changes, which land on the one, and drops decide.
- The lights run 80 ms ahead of the analysis to make up for capture and network delay, so they hit on the beat, not after it.
- Looks change on 8-bar phrases, with a hit in the new color on the first beat. The show runs at an even 50 frames per second.

## Sections

The show only switches after the music has stayed that way for a beat or two.

- **Groove**: dark between the beats, full on the kick.
- **Break**: the bass drops out: slow washes, glitter on the hi-hats.
- **Build-up**: heard the way you hear one: a riser sweeping up, brighter highs, a snare roll getting tighter, the bass being filtered away, rising volume. The light closes in from both ends and follows how far the riser has swept up; the strobe speeds up from 1 to 4 flashes per beat over the last two bars, counting toward the next 8-bar line.
- **The breath**: the silence just before a drop goes black.
- **Drop**: fired within a few hundredths of a second of the bass slamming back in (also right out of a previous drop after a tenth-of-a-second cut), or exactly on the "one" after the breath. One white blinder, then the style's hardest look. A louder chorus in other music gets the same treatment, in color.
- **Calm**: quiet passages.

White is used only for the build-up strobe and the drop's blinder; all other accents are in color.

## Genre, mood and chords

- **AI genre**: Google's YAMNet sound classifier (AudioSet, 521 sound classes, ~50 of them genres, 6 moods) runs offline every 2 s via onnxruntime (~5 ms). A new style comes in at the next phrase once it stays clearly ahead for ~6 s. Sad or tender music softens the show, angry or exciting music makes it harder.
- **Better dance-music genres (optional)**: AudioSet has no afro, melodic or tech house, so YAMNet often hears those as jazz, folk or techno. MTG's Discogs-EffNet knows the 400 styles record shops use (Deep House, Tech House, Trap, Reggaeton, ...) and picks the style instead when it is on the PC; YAMNet still tells music from speech and gives the mood (~10 ms every 2 s together). On 26 recorded songs it got the style right for 17, YAMNet for 13 (`tools/genre_bench.py`). Its licence is CC BY-NC-SA 4.0 (non-commercial), so Glowstage doesn't ship it: put `discogs-effnet-bsdynamic-1.onnx` and `discogs-effnet-bsdynamic-1.json` from [essentia.upf.edu/models](https://essentia.upf.edu/models.html) (feature-extractors/discogs-effnet) in `%LOCALAPPDATA%\Glowstage\models` to use it.
- **Each style has its own choreography**:
  - *Techno*: short mono pulses on every kick, 16th-note step chases, color flashes jumping around.
  - *House / Disco*: a light-up dance floor stepping on each beat, lights on the offbeat, mirror-ball glitter.
  - *EDM*: moving-head scanners, waves, rings; the full build-up / breath / drop treatment.
  - *Dubstep / DnB*: the stage breathes with the bass wobble; glitchy random zones.
  - *Hip-hop*: everything glows with the 808 and fades with it, the rap flow lights the middle, the snare swaps sides, trap hi-hats tick on the back wall.
  - *Rock*: pulses on the real kick and snare, color sweeps; the chorus gets chases.
  - *Pop*: bouncing color blocks, mirrored rings.
  - *Latin / Afro / Reggae*: a warm pool of light swaying left and right, accents on the offbeat.
  - *Chill*: colors drifting with the melody, no hits at all.
- **Half time**: trap and dubstep at ~140 BPM are felt at 70. The show measures it per song from where the snares land and then moves at half tempo.
- **Chords** (`glowstage/harmony.py`): an 8192-point FFT folded onto the 12 notes, matched against the 24 major and minor triads (the bass note gives the root a head start); the key from the Krumhansl-Kessler profiles over ~20 s. Once the key is stable, each chord of the key gets its own color pair of the style, so the light changes with the progression.

## The song's own show

- It **learns the song while it plays**: per kind of part (verse, chorus/drop, break, calm) it keeps a cue list of looks and colors, phrase by phrase, and plays it again when that part comes back.
- A **master fader**: verses a bit down, choruses and drops full at once.
- **Transitions**: a new look wipes in across the stage over one beat; drops, build-ups and the breath cut hard.

## The stage

- Lights in front of you form one stage from left to right, so chases run across the room. Lights behind you (e.g. Nanoleaf panels on the back wall) answer the front: the front hits on the kick, the back on the snare.
- Fans and pumps on ARGB headers (8-LED rings) get an arc spinning with the beat, neighbouring fans turning opposite ways. Keyboards with a known key layout get rings rippling out from the middle keys.
- `show_layout` in the config (light id → `[start, end]` or `[start, end, "back"]`; 0 = left, 1 = right; start > end reverses a strip) places the lights; a visual stage editor is coming.
