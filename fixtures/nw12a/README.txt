NEON BREACH / ROOM 07
An original 48-second nodewerk procedural 3D science-fiction demo.

Extract the entire zip on supported Windows 11 x64. Run
PLAY-NEON-BREACH.cmd for the story and the three mission archive chapters, or
double-click neon-breach.exe for the story directly. The default story has
built-in procedural audio and needs an active WASAPI render endpoint, Direct3D
12 feature level 12_0 / Shader Model 6.0, and a display; it does not need a
network, codec, recording, or separate soundtrack. The three archive players
are silent. Alt+F4 closes a player. To restart at tick zero, launch it again.
A normal story playback stops at the end of its score. The main film renders
at 1920 x 1080 with perspective, depth, lighting and bloom. It uses rasterized
3D geometry; hardware ray tracing is not enabled or required.

The scene is built from rings: an orbital mothership, a ribbed neon hotel and
mechanical characters. Cyan marks the heroine, rose the guards and amber the
agent who escapes. The heroine teleports to the hotel, kicks a security
door open, and fights the agents with a non-graphic stylized beam. One agent
uses the chaos to escape through a service hatch on his own. From the corridor,
he draws a fictional disruptor and incapacitates her in a non-graphic flash.
The original four-line finale is included; it is not song lyrics. The primary
3D story is continuous and opens independently of the optional archives.

The scene slows to quarter speed (22-25 s), freezes (25-27 s), briefly reverses
authored action (27-29 s), and resumes. The camera continues moving during the
freeze. The authoritative audio clock and score continue forward throughout.
Sound effects are scheduled once at absolute sample positions, including
during the cinematic time remap.
CUE-TABLE.json records the event-to-60-fps-frame mapping.

Music and graphics: original nodewerk NW-12A development assets. This package
does not include a recording, transcription, arrangement or sound-alike of
Clutch. "In Walks Barbarella" is the intended reference; no notes or timing of
that track were verified. There is no audio-file import. When separately
authorized, a bounded note-event score can replace the development score at
build time and is synthesized through NW-12, never played as a recording.
An original or authorized four-line finale text can also be supplied at build time.

The optional archives are separate demonstrations: Orbit Atlas is a procedural
3D ring scene; Portal Tracker is a 2D seeded-particle history view; Hotel Echo
is a 2D image-feedback history view. They are silent and play forward. The main
film's sparks and debris use animated 3D geometry, not those History operators.

CI builds and headlessly checks fixed-tick GPU readbacks and the cue indicator.
It does not establish physical display alignment or real audio endpoint playback.
A separate nodewerk-nw12a-demo-preview artifact is generated in Windows CI
from the same embedded scene package for visual inspection.
SHA256SUMS.txt lists the staged executable and accompanying file identities.
