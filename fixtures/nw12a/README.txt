NEON BREACH / ROOM 07
An original 48-second nodewerk pulp science-fiction concept demo.

Extract the entire zip on supported Windows 11 x64. Run
PLAY-NEON-BREACH.cmd for the story and the three mission archive chapters, or
double-click neon-breach.exe for the story directly. The default story has
built-in procedural audio and needs an active WASAPI render endpoint, Direct3D
12 feature level 12_0 / Shader Model 6.0, and a display; it does not need a
network, codec, recording, or separate soundtrack. The three archive players
are silent. Alt+F4 closes a player. To restart at tick zero, launch it again.
A normal story playback stops at the end of its score.

The heroine teleports from a mothership to a neon hotel room, kicks a security
door open, and fights the agents with a non-graphic stylized beam. One agent
uses the chaos to escape through a service hatch on his own. From the corridor,
he draws a fictional disruptor and incapacitates her in a non-graphic flash.
The original four-line finale is included; it is not song lyrics. The primary story
is continuous and opens independently of the operator archives.

The scene slows to quarter speed (22-25 s), freezes (25-27 s), briefly reverses
selected motion (27-29 s), and resumes. The authoritative audio clock and the
score continue forward throughout. Sound effects are scheduled once at
absolute sample positions, including during the cinematic time remap.
CUE-TABLE.json records the event-to-60-fps-frame mapping.

Music and graphics: original nodewerk NW-12A development assets. This package
does not include a recording, transcription, arrangement or sound-alike of
Clutch. "In Walks Barbarella" is the intended reference; no notes or timing of
that track were verified. There is no audio-file import. When separately
authorized, a bounded note-event score can replace the development score at
build time and is synthesized through NW-12, never played as a recording.
An original or authorized four-line finale text can also be supplied at build time.

The archive chapters are real baseline packages: Orbit Atlas uses the
procedural geometry/material/camera/post chain; Portal Tracker presents real
seeded particle History; Hotel Echo presents real image feedback History.
They use their own forward playback and are not hidden passes of the story.

CI builds and headlessly checks fixed-tick GPU readbacks and the cue indicator.
It does not claim physical display alignment or real audio endpoint playback.
SHA256SUMS.txt lists the staged executable and accompanying file identities.
