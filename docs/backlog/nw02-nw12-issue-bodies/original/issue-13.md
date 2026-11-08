# NW-12 — Add optional procedural audio through the authoritative clock boundary

## Goal

Support synchronized sound without adding audio overhead to silent exports.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Introduce an optional audio provider that produces PCM, reports an authoritative playback sample position, and supports deterministic offline rendering. Convert sample position to the existing 48,000-tick timebase with integer rational arithmetic when device sample rate differs. Use a small synthesis fixture and WASAPI output behind compile/export selection; evaluate any third-party synth for license, CPU cost, and complete byte contribution before adoption. Audio rendering uses its own preallocated buffers and communicates timing through a narrow thread-safe interface. Scene events and audio scheduling share tick semantics, while editor seeking explicitly stops/resets audio and reconstructs the requested state rather than replaying audible side effects.

## Scope

- Clock provider and PCM output integration.
- One bounded procedural synthesis fixture.
- Offline synchronized audio/video capture.
- Audio-specific byte and CPU accounting.

## Affected components

- Proposed optional audio module
- Proposed timeline clock adapter
- Proposed audio export closure

## Required behavior

- Silent exports retain the existing monotonic clock and contain no audio imports, code, or song data.
- Audio-enabled playback follows the authoritative sample clock.
- Offline capture reproduces the same event/sample mapping.
- Real-time audio callbacks perform no allocation or blocking compilation.

## State, configuration, and API changes

- Define PCM format, sample-position, reset, seek, and offline-render interfaces.
- Add optional synthesis/song payload sections and their capability declarations.
- Define underrun and clock-discontinuity reporting.

## Recovery semantics

- Audio initialization failure follows an explicit export policy: fail or enter declared silent playback.
- Underruns are counted; time conversion remains monotonic according to the selected device-clock contract.
- Seek stops output, resets synthesis/event state, and resumes from the requested position.

## Compatibility and safety constraints

- No compulsory external synth, codec, or soundtrack dependency.
- Any reused implementation requires a documented compatible license.
- Audio-thread state is bounded and independent of editor worker jobs.

## Test plan

- Test rational sample/tick conversion across device rates and long durations.
- Compare deterministic offline PCM and event schedules.
- Measure audiovisual alignment using a synchronized impulse/flash fixture.
- Verify silent-export dependency and byte identity.

## Regressions

- Silent artifacts must not grow when audio support is added.
- Timeline semantics must remain unchanged for existing graphs.
- Underrun rates and audio CPU use become tracked metrics.

## Acceptance criteria

- The fixture plays synchronized procedural audio with visual event alignment within one displayed frame.
- Offline event-to-sample mapping is deterministic.
- Silent exports strip the entire audio closure.
- Publish synthesis, song, output-layer byte costs and CPU measurements.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 supplies the stable integer timeline and export closure.
- NW-09 is required only when audio-enabled content uses temporal simulation.

Boundaries:
- Post-proof milestone; audio is not part of NW-07 or the required NW-10 intro.
- No full digital audio workstation, general codec library, or mandatory beat-based clock.
