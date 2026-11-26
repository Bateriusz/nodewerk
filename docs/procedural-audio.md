# Optional procedural audio (NW-12)

Audio is an explicit optional NWP 1.2 extension: the AUDI section carries the bounded
NWAS v1 song (20-byte header, 20-byte events, max 64, duration at most one hour).
See [the NWP specification](nwp-format-v1.md). A silent package retains its existing
version and does not carry the section. Generated silent targets disable the audio
reader and synthesis/output linkage. Audio exports currently use the full reader.

The integer triangle oscillator produces mono PCM16 with bounded envelopes and
saturating mixing. Tick/sample conversion is rational floor/ceil. WASAPI uses
IAudioClock frequency and absolute position, not queued sample count. Endpoint
zero-padding observations are not physical underruns. A clock position beyond the
submitted frontier with an empty queue is an observed stream-coordinate gap:
stop/reset/restart at the fresh absolute tick, preserving monotonic time, and report
the gap count, missing-sample lower bound and discontinuity. Contradictory or
backward clock observations fail explicitly. Exact audible glitch telemetry is
unavailable; synthesis and complete worker CPU time are reported in 100 ns units with availability.

## Use

```sh
python tools/audio_song.py fixtures/audio/sync_song.json song.nwas
python tools/nodewerkc.py export fixtures/audio/sync_flash.nwgraph --output build/audio-export --audio-song fixtures/audio/sync_song.json --player-template <configured-exe>
```

Use the generated executable for playback, or configure the root Windows build with
NW_BUILD_AUDIO=ON and build nw_audio_player, nw_audio_contract and nw_audio_offline.
The diagnostic player takes the exported common.nwp; it reads the embedded song.
The offline writer takes song.nwas, audio.wav and frames.csv. The paired flash
graph and song share absolute onset ticks. For offline video capture run
`python tools/audio_capture.py --offline <nw_audio_offline.exe> --host <nodewerk-studio-preview.exe> --package <common.nwp> --song song.nwas --output <new-dir>`.
The capture validates exact song identity, PCM length/header and frame plan, then
captures RGBA32F images through the existing headless Studio host. It publishes
only a complete bundle; the bundle does not certify real displayed alignment.

Studio supports --audio-song in the gui/export CLI and a song field in Export
Settings. Play/Stop and timeline scrubbing use the existing native package-host
session. The worker reconstructs the target visual state before starting sound;
the native host stops output before rewind/reload/device loss/exit. GUI preview
cadence is not a physical alignment measurement.

## Validation

Linux pytest exercises the native synthesis and stream state machine, package
roundtrip, fixture compile/oracle transitions, and controlled capture. The existing
mandatory Windows real-export gate builds independent silent and audio-enabled
PEs, validates link commands, sections/imports and native package decoding, and
builds/runs the native contract without an audio endpoint or windowed Present.
The existing Windows 2025 runtime transfer gate checks actual headless WARP
image readback hashes at song onsets and after each flash; this is not physical
alignment evidence.
A nonshipping synthesis-only link probe measures marginal synthesis/reader and
WASAPI output PE costs, preserving signed linker/alignment effects. The JSON report
is published with the existing runtime-smoke build inputs. The exact physical
impulse/flash alignment, audio CPU and underrun evidence remain separate hardware-
neutral acceptance observations, not hosted CI obligations.
