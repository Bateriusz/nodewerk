# NW-12A: Neon Breach / Room 07

An original 48-second pulp science-fiction concept demo from issue [#46](https://github.com/Bateriusz/nodewerk/issues/46), built on merged nodewerk main
`1c9e1559c8d43530c7b213461c6719b6aa43ae35` (tree `4b73007a5321e9c8ad89eac70de2f0219a542080`).
Barbarella teleports from a mothership, kicks in a neon hotel's security door,
and fires a stylized beam. The third agent takes advantage of the chaos and
escapes through a service hatch on his own. She does not escort or release him.
Already outside the room, he draws a fictional disruptor and incapacitates her
in a stylized, non-graphic counterattack. The finale uses four original lines,
not song lyrics. The HUD, actors, geometry, glyphs and sound code are original procedural work.

## Download and run

The core workflow's existing pinned Windows build job produces the four
specialized players and their embedded packages with `tools/audio_demo.py`.
The latest Windows 11 runtime job verifies the transfer SHA manifest, launches
the exact shipped bytes at fixed ticks under WARP without a window or endpoint,
then uploads **nodewerk-nw12a-demo-windows** for the PR. Use the artifact from the
successful PR run, not an earlier build. The zip contains EXEs, the menu,
short README, cue table and checksums; it is not a source or toolchain bundle.

On Windows 11 x64, extract the full directory. Double-click
`PLAY-NEON-BREACH.cmd` (or `neon-breach.exe` for the continuous story). The menu
offers the three optional in-universe mission archives; it starts the main film
automatically after five seconds. Quit with Alt+F4. Relaunch starts at tick zero.
The story requires the common D3D12 profile and an active WASAPI render endpoint.
No codec SDK, separate recording, or network is needed. The archive views are
silent.

## One clock, separate song and motion

The actual NW-12 `AUDI`/`NWAS` capability is on the main generated player:
WASAPI's absolute 48 kHz clock supplies the render tick and its own fixed-point
procedural synthesis produces every note and transient. A single JSON cue
table generates the shader cue constants and synthesizer event ticks. Representative
events are committed on exact 60 fps frame boundaries (800 samples). At each
cue hosted CI checks the rendered diegetic red timing LED is off on the prior
frame and on at the boundary; the same cue dispatcher drives the corresponding
flash, line, hit or door marker. The portable test executes the real NW-12
offline PCM writer twice (music alone and effects mix) and confirms sample
onsets and matching frame plan for every event.

| Cue | Tick / first PCM sample | 60 fps frame |
| --- | ---: | ---: |
| Teleport | 384000 | 480 |
| Door impact | 720000 | 900 |
| First shot | 1008000 | 1260 |
| First impact | 1032000 | 1290 |
| Second shot | 1401600 | 1752 |
| Second impact | 1425600 | 1782 |
| Agent slips out | 1512000 | 1890 |
| Hatch seals / agent outside | 1872000 | 2340 |
| Agent fires the fictional disruptor | 1920000 | 2400 |
| Barbarella is incapacitated | 1944000 | 2430 |
| Original end card | 2064000 | 2580 |

The presentation clock does not move backwards. The authored action time is
reconstructed by a continuous piecewise integer map: forward at 1×, quarter
speed from 22–25 s, frozen from 25–27 s, reversing at half speed from 27–29 s,
then forward at 1×. The transport HUD and absolute audio score remain live.
Effects are never replayed during the visual reverse. The continuous story is
stateless in this respect. The independent History chapters play forward only;
they do not claim native backward History seek.

The bundled score (`fixtures/nw12a/development-score.json`) is the original
development accompaniment, not an arrangement, transcription or recording of
Clutch's *In Walks Barbarella*. Approved note data can replace the music part
without modifying the visuals/effects. Supply an authorized local note-event
JSON **at build time**, not an audio file:

```text
python tools/audio_demo.py build --player-template <configured-Windows-EXE> --dxc <pinned-DXC> --score <authorized-score.json> --output <new-dir>
```

Use fields `version: 1`, `title`, `kind: "authorized_arrangement"`,
`duration_ticks: 2304000`, and `notes` (rows of absolute start tick, duration
ticks, frequency in millihertz, positive gain up to 32767). Note and cue effects
together must fit the native NWAS 64-event bound; remix the authorized excerpt
within that bound rather than silently truncating it. Regenerate cue positions
only when permitted musical timing data is available. Invalid, absent explicitly
requested, or recording inputs fail closed; the default build uses only the
original repository score. No actual Clutch notes, recording or beats have
been verified for this demo. Do not redistribute a replacement without the
appropriate rights. The four display lines are read at build time from
`fixtures/nw12a/finale.txt`. Use `--finale-text <file>` to substitute four
original/authorized uppercase lines (up to 46 characters in the built-in font).
The requested lyrics are not bundled or reproduced; changing the text does
not modify the procedural audio or cue timings.

## Implemented operator inventory

These IDs are present on the stated baseline. Each chapter is reached from the
same menu and has an actual embedded native runtime package; the main film is a
continuous stateless artwork, not a misleading substitute for executing these
operators. The chapter assets are story-linked engineering views of the same
locations (approach map, portal telemetry and hotel afterimage).

| Baseline source | Versioned operator ID | Actual visible use |
| --- | --- | --- |
| NW-06 operator contracts | `nw.texture.noise2d@1.0.0` | Orbit atlas generated alloy noise |
| NW-06 | `nw.residency.generate_texture@1.0.0` | Atlas GPU material texture |
| NW-06 | `nw.mesh.torus@1.0.0` | Atlas ring structure |
| NW-06 | `nw.residency.generate_mesh@1.0.0` | Atlas GPU torus topology |
| NW-06 | `nw.scene.instances@1.0.0` | Approach ring array |
| NW-06 | `nw.residency.generate_instances@1.0.0` | GPU-generated atlas instances and indirect arguments |
| NW-06 | `nw.material.standard@1.0.0` | Atlas cyan alloy treatment |
| NW-06 | `nw.camera.curve@1.0.0` | Atlas absolute-tick camera flyaround |
| NW-06 | `nw.animation.twist@1.0.0` | Atlas twisting ring orientations |
| NW-06 | `nw.render.instanced@1.0.0` | Atlas renders the real geometry |
| NW-06 | `nw.post.bloom@1.0.0` | Atlas half-resolution highlight bloom |
| NW-06 | `nw.post.tonemap@1.0.0` | Atlas ACES output mapping |
| NW-09 temporal state | `nw.particles.system@1.0.0` | Portal tracker seeded, fixed-step particle-state readout |
| NW-09 temporal state | `nw.feedback.image@1.0.0` | Hotel echo accumulated image-history readout |
| NW-12 audio | `AUDI` / `NWAS` v1, not a graph operator ID | Main story procedural score, cues and authoritative clock |

The main also reaches the baseline `nw.shader.expr`, `nw.texture.procedural`,
`nw.residency.upload_texture`, `nw.render.fullscreen` and `nw.render.present`
operators, with a precompiled authored shader specialization under the ordinary
fullscreen package ABI. NW-11 introduced optional realization experiments, not
authoring operator IDs; none are falsely claimed as used here. Planned NW-13
(#36) bounded SDF/CSG, transforms, repetition/deformation/displacement and
visibility/reflection semantics are **not** baseline nodes. Analytic screen
primitives in this artwork are not claimed as NW-13 integration.

## Evidence limits

The actual offline PCM test uses a native synth executable. Portable package
contract tests use test DXIL, not a claim of compiled Windows visuals. Hosted
Windows smoke is a real headless rendering check, not a desktop A/V observation.
No physical Windows playback/alignment, GPU timing or licensing clearance for
the intended song is asserted. Confirm the end-user desktop result separately.
