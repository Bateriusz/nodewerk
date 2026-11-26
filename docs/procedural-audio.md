# Optional procedural audio (NW-12)

This is the initial audio implementation on the post-NW-11 shared architecture.
It adds no audio code or link dependency to existing player targets. Enable it
explicitly with `-DNW_BUILD_AUDIO=ON`. NWAS v1 is a bounded, little-endian song
payload (20-byte header: magic, uint32 version, uint64 duration in 48 kHz ticks,
uint32 event count; 20 bytes per event: uint64 tick, uint32 duration, uint32
frequency in millihertz, uint32 gain). Maximum 64 events, one hour, sorted by
absolute tick. It uses a deterministic integer triangle oscillator with bounded
attack/release and saturating mono PCM16 mixing. No external synth or codec.
It is a separate payload, not yet a section of the existing NWP container.

Sample-to-tick is floor(sample * 48000 / rate); event-to-sample is
ceil(tick * rate / 48000), using checked quotient/remainder arithmetic. Seeking
restarts audio output and synthesizes directly at the absolute sample origin,
without replaying previous events. WASAPI shared mode uses the endpoint clock's
reported frequency, not submitted-buffer length. The worker owns COM and
writes directly into endpoint-provided buffers; the renderer observes atomic
tick snapshots. Initialization and runtime failures fail explicitly in the
diagnostic player. Zero-padding observations are not asserted to be measured
underruns; the physical endpoint test must establish actual behavior.

## Commands

```sh
python tools/audio_song.py fixtures/audio/sync_song.json song.nwas
cmake -S . -B build/audio -DNW_BUILD_AUDIO=ON -DNW_ENFORCE_PINNED_TOOLCHAIN=OFF
cmake --build build/audio --target nw_audio_contract nw_audio_offline
```

Run the contract executable, then `nw_audio_offline song.nwas audio.wav frames.csv`
(with the platform executable suffix/path). The CSV is the exact 60 fps
frame/tick/sample schedule, not a rendered video; use it to drive fixed-tick
image capture. The Linux hosted pytest suite compiles and exercises the native
synthesis and offline writer, checks malformed inputs, and compares repeated
WAV and CSV byte output.

On a Windows build configured with the normal pinned toolchain and
`NW_BUILD_AUDIO=ON`, build `nw_audio_player` and run it with a compatible
exported scene NWP and song NWAS. It uses the existing PackagePlayer renderer.
The diagnostic program reports synthesis CPU time and clock discontinuities.
The playback fixture is not an embedded production export.

## Outstanding acceptance

The production export/package capability and Studio integration, synchronized
video capture, final byte attribution and same-revision silent closure proof,
and real impulse/flash display measurements remain open. The JSON fixture
contains audio impulses at integer seconds; a matching visual flash production
and physical measurement are still required. No physical evidence is claimed.
