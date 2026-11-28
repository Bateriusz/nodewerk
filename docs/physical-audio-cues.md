# Physical audio/visual alignment for NWAS v2 film exports

A cue is a declared visual onset paired with an effect note in the exact NWAS
payload the player consumes. The exporter writes the eligible cues into the
embedded link manifest. It uses the same presentation-tick schedule as the
film shader after freeze, resume and reverse processing. An action tick or the
list of music notes is not a measurement plan. For example, the hotel
corridor's flash is authored at action tick 144000 and emitted at presentation
tick 148800. If a film has no unambiguous, paired cues it can still play, but
it cannot certify physical A/V alignment.

## Capture an exact build

Use the expanded archive from the NW-13 physical film workflow. First verify
its archive and manifest against the trusted CI build, and retain them
unchanged. The audio launcher takes the independently verified SHA-256 of
`film-physical.json`, not a guessed revision or a hash from an unvetted bundle.
Use the player and package paths listed for the same arm in that manifest:

```powershell
python tools/audio_physical_validation.py run `
  --film-bundle "$bundle/film-physical.json" --film-bundle-sha256 $manifestSha256 `
  --variant common --player $selectedPlayer --package $selectedPackage `
  --output "$private/run.json"
```

The launcher rechecks the selected arm, embedded package and link manifest,
graph and song hashes, and opens the player maximized. No source rebuild or
separate song extraction is required. Start simultaneous external audio and
video capture before launching. Keep the window on one fixed-refresh display
and keep the audio endpoint in use for that capture. The launcher retains
same-run WASAPI and Present telemetry and identifies the playback host,
display and output by hashes. Playback success or zero observed underruns is
not an alignment measurement.

Configure the capture application to retain a finalized, non-fragmented
MP4/MOV with both audio and video tracks, or a finalized MP4/MOV video and a
separate PCM WAV (or MP4/MOV audio) on the same capture timebase. MKV/WebM
and fragmented MP4 are not supported by this verifier. Empty files, arbitrary
bytes renamed as media, and files missing a required track's sample data are
rejected even when hashes match. MP4/MOV must be self-contained: every sample
must map to a chunk and every chunk must fit in a local media payload without
overlapping another chunk. Use only one audio track and one video track for the
measurement. Supported video sample entries include H.264, HEVC, AV1, MPEG-4
Part 2 and ProRes. Supported audio sample entries include MPEG-4 audio (such
as AAC), Opus, ALAC and common QuickTime PCM. For separate WAV use integer
PCM or 32/64-bit IEEE float, including WAV extensible with a valid subtype.
The 48,000 Hz analysis clock does not require resampling the recording.
Native 88.2/96 kHz AAC and 96/192 kHz ALAC can be retained in MP4/MOV when
their codec configuration identifies the native audio rate. A zero legacy
rate field is supported only for a native rate too large for that field.
The verifier checks bounded container structure and retained track time, not
decoded content or cue recognition. Keep the original recording files with
their pre-launch lead-in, without trimming or moving their timestamps.

Analyze the raw synchronized recording. In `observation.json` supply:

- `format: "nodewerk-audio-display-observation"`, `version: 2`, and
  `sample_rate: 48000`;
- `capture_clock` with `audio_origin_tick`, `video_origin_tick`, and
  `playback_start_tick` on the shared 48,000 Hz capture timeline. For muxed
  media the file origins are both 0. For separate files use the capture
  setup's independently established file offsets, setting the earlier file's
  presentation-zero position to 0. If the sources do not share a measurable
  timebase, use one muxed recording. Measure `playback_start_tick` from the
  recorded launch reference at or before the start of playback, not from the
  later flash or audio impulse. For example, a launch at 0.25 seconds is tick
  12000. Both tracks must contain this reference.
- `run_id` and a `binding` object copied from `run.json`'s
  `observation_binding` (source revision/tree, variant, executable, cue plan,
  host, display and endpoint). A missing binding cannot qualify;
- exactly one `impulses` entry for each planned cue, in order. Each row has
  `cue_id`, `presentation_tick`, the independently observed `audio_tick` and
  the independently observed `display_tick`. The observed ticks must use the
  same capture timebase, not values copied from the plan. The planned
  `presentation_tick` is relative to the film, not the capture clock.
  `playback_start_tick + presentation_tick` is the earliest permissible
  onset, not a substitute for measurement. Both recordings must extend
  through that nominal cue instant and each measured onset. The verifier
  subtracts each file's origin and checks its half-open presentation spans,
  including MP4/MOV composition timing and edits or the WAV frame count and
  sample rate. The one-displayed-frame tolerance applies to the difference
  between the two independently measured onsets;
- `capture.audio_sha256` and `capture.video_sha256` for the original media.
  A multiplexed recording can be supplied for both.

```powershell
python tools/audio_physical_validation.py record `
  --player $selectedPlayer --package $selectedPackage --run "$private/run.json" `
  --observation "$private/observation.json" --capture-audio "$private/capture.mp4" `
  --capture-video "$private/capture.mp4" --output "$private/evidence.json"

python tools/audio_physical_validation.py verify --evidence "$private/evidence.json" `
  --executable $selectedPlayer --package $selectedPackage
```

The strict verifier rehashes the original run, observation and raw media,
recomputes the displayed-frame tolerance from measured refresh, and compares
the exact NWAS v2 bytes. A detached report does not qualify. Keep the source
archive and recordings private; `summarize` emits a historical projection
without raw recordings, private host/device identities, or individual onsets.
The summary does not certify physical acceptance. NWAS v1 retains its
[original playback and observation protocol](procedural-audio.md).
