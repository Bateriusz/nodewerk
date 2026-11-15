# NW-07 256 KiB Architectural Proof — Feasibility Report

- **Status:** Implemented from immutable base
  `b7e2d11062d4f7bb123ae97c21839631ffc09b93`
- **Task:** NW-07
- **Formats:** `.nwgraph` v1 and `.nwp` v1 (both unchanged)
- **Common profile:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Requirements digest:**
  `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`
- **Canonical graph:** `fixtures/nw07/production_loop.nwgraph`
- **Canonical configuration:** `benchmarks/nw07/canonical-production.v1.json`
- **Performance contract:** `benchmarks/nw07/perf-ref-v1.json` (`nw07_perf_ref_v1`)
- **Evidence record:** `benchmarks/nw07/hardware-evidence.json`
- **Metric baselines:** `benchmarks/nw07/metric-baselines.v1.json`

The older planning base `270d298f1b037548e0ab4808b2c123c12ddcc4e6` is provenance
only. No prerequisite requires `benchmarks/nw06-hardware-evidence.json` to
become verified, and no NW-06 development capture can satisfy NW-07 acceptance.

## Result

NW-07 extends the existing NW-02 typed graph, NW-03 compiler/package,
NW-04 package player, NW-05 export/schedule, and NW-06 procedural-scene path.
It introduces no second graph model, package format, player, hand-authored demo
runtime, or hardware-model compatibility selector.

The frozen production is a **20-second looping 1920x1080** scene containing:

- 256 seeded procedurally placed torus instances;
- generated four-octave value-noise material (256x256 R8G8B8A8);
- an absolute-tick animated camera that completes exactly one revolution per
  loop and a stateless analytic twist animation;
- analytic directional lighting;
- half-resolution bloom;
- ACES tonemapping;
- the existing terminal `frame.present` instruction.

The graph contains only compact procedural parameters and explicit seeds. It
does not bake a texture, vertex buffer, index buffer, instance table, or
material table. Those are produced by five initialization-only GPU
dispatches. The frame stream contains exactly one indexed instanced indirect
draw, bloom and tonemap dispatches, and the terminal present.

## Hard gates

| Gate | Contract | Status in this tree |
| :--- | :--- | :--- |
| Complete runnable distribution | `<= 262,144` bytes, one self-contained Windows x64 executable embedding the `.nwp` package and precompiled shaders | Contracts verified with synthetic test payloads; release export pending Windows/DXC execution |
| Process-start-to-first-present | `<= 2,000 ms` under `nw07-startup.v1` | Pending physical measurement |
| Peak engine-committed GPU resources | `<= 512 MiB` | Pending physical measurement |
| `nw07_perf_ref_v1` GPU frame time | p99 `< 16.67 ms` in each of three independent 60-second runs after a five-second warm-up | Pending matching reference host |
| `nw07_perf_ref_v1` CPU submission time | p99 `< 16.67 ms` in each of three independent 60-second runs after a five-second warm-up | Pending matching reference host |
| Desktop Ada correctness | identical final artifact, fixed ticks, cross-renderer comparison | Pending representative hardware |
| Desktop Blackwell correctness | identical final artifact, fixed ticks, cross-renderer comparison | Pending representative hardware |
| Diagnostic overhead | measured separately from the release artifact | Pending diagnostic sibling-build measurement |

The **131,072-byte stretch value is a measurement only**. It is reported as
`within_stretch` / `measured_above_stretch` and never gates milestone success.

### Contract-only measurements in this tree

The GPU-less contract suite exports the frozen graph twice with a structurally
valid synthetic DXIL payload and a 258-byte synthetic player template. That
exercise produces a **32,318-byte** self-contained executable (17,457-byte
package + 14,511-byte link manifest + footer + template), within both the
262,144-byte hard gate and the 131,072-byte stretch value. The export-time
logical peak physical realization is **192,983,040 bytes** (about 184 MiB),
below the 512 MiB export budget. These numbers validate the accounting and
budget paths only; they are **not** release-artifact or hardware evidence.

## Complete size accounting

The runnable distribution is exactly one executable. The embedded package,
generated link manifest, and deterministic footer are accounted as subsets of
that executable. Auxiliary build/evidence JSON files are not required to
execute and are reported separately; they cannot excuse an over-ceiling
executable. System DLLs (`d3d12`, `dxgi`, `kernel32`, `user32`, `gdi32`) are
the only dependencies, and no runtime DXC/Slang, editor, authoring runtime, or
optional implementation is part of the closure.

`tools/nw07_export.py` verifies:

- the package is a release `.nwp` v1 package with no development section;
- the executable embeds the exact published package and manifest bytes;
- every required non-system byte is accounted (`player image + package +
  manifest + footer == executable`);
- the dependency closure contains no forbidden runtime compiler/authoring
  component;
- two exports from clean directories produce matching artifact, package,
  manifest, executable, and link-manifest hashes;
- the frozen graph digest and pinned build-input digest match.

## Startup and memory protocol

`nw07-startup.v1` measures from process creation to the first successful
present. Clean starts use no application pipeline cache. Cold driver-cache and
warm runs are distinguished, and the preparation of each is documented in the
canonical configuration and the capture. Missing telemetry stays pending; an
observed value above either gate remains failed.

## Performance reference (`nw07_perf_ref_v1`)

Before certifying any measurement the contract freezes:

- the exact desktop **NVIDIA GeForce RTX 4060 Ti** reference board metadata
  (vendor/device/subsystem/VRAM) and the reference host identity;
- Windows build, driver, WDDM, toolchain, DXC pin, artifact hashes, quality
  settings, resolution, cache state, power/thermal/clocks conditions, and
  measurement tools;
- metric definitions, the strict `<` operators, nearest-rank p99 methodology,
  and matching rules;
- a five-second warm-up followed by three uncapped 60-second runs, each in a
  fresh process.

Each run is evaluated independently. Raw GPU and CPU samples are retained and
the p99 values are recomputed by `tools/nw07_evidence.py`. A single failed run
fails the contract; an aggregate cannot rescue it. Displayed frame intervals
are reported separately and never substitute for the gate metrics.
`tools/nw07_capture.py perf` retains each run's `run<N>-timing.json` warm-up and
measurement start/end timestamps. Validation derives the actual warm-up and run
durations from those timestamps and rejects short, capped, or reused-process
runs even when their declared protocol fields claim compliance.

The contract's methodology is frozen, but host/artifact binding is currently
`pending_reference_capture`. Certification remains pending until a matching
capture binds the exact board, host, environment, toolchain, and final artifact.
A different Ada device, a Blackwell device, a faster/higher-tier SKU, a legacy
tier label, a stale artifact, a mismatched host/environment/protocol, or
incomplete provenance cannot certify `nw07_perf_ref_v1`; such measurements are
retained as informational or unverified.
The capture's own execution environment must also be `physical` or a
`documented_gpu_passthrough` with retained documentation; a generic VM cannot
certify performance.

## Correctness coverage

Correctness and performance are separate. Each representative target (desktop
Ada and desktop Blackwell) must execute the **identical final 1920x1080 common
artifact** at the frozen fixed ticks and compare the exported player with a
separately identified diagnostic-preview execution. Captures retain observed
hardware, coverage target, evidence role, capability profile and digest,
executable/package/manifest hashes, driver/OS/toolchain identities, queried
capabilities, cache state, clocks/power/thermal conditions, capture protocol,
raw RGBA32F artifacts, reference provenance, and validated numeric/image
comparisons.
The same physical-or-documented-passthrough rule applies to correctness
captures; a generic VM is never sufficient for GPU acceptance.

Numeric acceptance preserves the NW-06 semantics (`1e-5` absolute **or** `1e-4`
relative). Image-channel acceptance retains the `1e-4` absolute bound and
cannot be rescued by a relative result. Resolution, quality, and tolerances
cannot be silently weakened.

Loop-boundary evidence verifies that tick `960,000` is equivalent to tick `0`
modulo one camera revolution and that the deterministic scene state is
unchanged across the boundary. `tools/nw07_capture.py correctness` performs an
actual image comparison when `tick_0.rgba32f` and `tick_960000.rgba32f` are
supplied. Without the boundary capture it records only canonical-graph
provenance and leaves verification pending. Fixed-tick replay is stateless.

## Matched-environment Blackwell development series

`benchmarks/nw07/metric-baselines.v1.json` and the evidence record reserve a
separate matched-environment Blackwell development/performance series. It has
**no absolute Blackwell gate** and cannot be used as Ada or reference evidence.
The rejected shortcuts `13.5 ms`, `12.00 ms`, theoretical-throughput
equivalence, and unvalidated normalization are not adopted. A replacement
reference or normalized method would require a new contract version,
independently justified thresholds, documented validation uncertainty, and
bridge measurements; it cannot retroactively pass `nw07_perf_ref_v1`.

## Diagnostic sibling build

`nw07-release` builds the distribution with `NW07_CAPTURE_CONTROLS=OFF` and the
release profile. `nw07-diagnostics` builds the dev-profile sibling with
`NW07_CAPTURE_CONTROLS=ON`. Configure-time checks reject capture controls in
any non-dev profile. Diagnostic size and frame-time overhead is recorded in a
separate evidence record bound to the same final release artifact. The release
artifact never contains capture hooks, timing queries, or diagnostic tooling.

## Continuous regression policy

Per-change checks cover CPU graph/evidence contracts, compiler/export closure,
two-clean-directory reproducibility, and size. Nightly GPU checks cover visual
output, startup, memory, and frame time. The policy is:

- absolute gate violations **fail**;
- matched-environment performance or memory regressions **above 10 %** are
  flagged, not failed;
- unmatched-environment comparisons are informational;
- size changes of any magnitude are tracked, with **no percentage-only size
  failure rule**;
- smaller size changes are reported normally.

Baselines and per-device image tolerances live in
`benchmarks/nw07/metric-baselines.v1.json`. Missing baselines never become zero.

## Recorded informational metrics

Even where no hard ceiling applies, qualifying captures record process RAM,
driver memory, startup pipeline time, generation time, image-quality metrics,
and 1440p scaling. Missing values are recorded as unavailable and keep the
record pending rather than being fabricated.

## Verification entry points

```bash
python tools/verify_nw07.py --mode ContractsOnly --summary-json build/nw07.json
python tools/verify_nw07.py --mode BuildChecks \
    --player-template <specialized-player.exe> --dxc <pinned-dxc> \
    --summary-json build/nw07-build-checks.json
python tools/verify_nw07.py --mode Full \
    --fixture <player.exe> --package <export-dir> --evidence <evidence.json> \
    --build-report build/nw07-build-checks.json
python tools/verify_nw07.py --mode CleanSystem --binary-dir <deployment-dir>
```

- `ContractsOnly` is GPU-less and never claims hardware acceptance.
- `BuildChecks` performs concrete Windows build/export checks when a pinned
  DXC and player template are available; otherwise it reports pending and
  exits `2`.
- `Full` distinguishes an executed failure from pending evidence. It reports
  milestone success only when every hard gate and required coverage target is
  verified. Release-export provenance is mandatory: Full either performs the
  native BuildChecks export when `--player-template` and `--dxc` are supplied,
  or validates a passed BuildChecks summary supplied with `--build-report`.
  The report must bind the same executable, package, manifest, size, pinned
  build-input digest, and clean-directory reproducibility as the final
  artifact. Without either source the native export gate remains pending.
- `CleanSystem` requires a physical Windows 11 x64 host and a clean-system
  execution report covering corrupt payloads, missing required features,
  initialization failures, resize, and device removal with actionable exit
  codes and retained failing captures. `--binary-dir` discovers a retained
  `nw07-clean-system.json` in the deployment directory, or `--clean-report`
  names it explicitly. The report's executable name/hash, byte size, file set,
  sidecars, and dependency state are recomputed from the actual deployment;
  a stale or foreign report cannot certify a different executable.

`tools/nw07_clean_system.py` performs the clean-system run on Windows by
extracting the package embedded in the final executable and driving the
established package-player fixture through:

| Scenario | Expected exit code |
| :--- | ---: |
| Corrupt payload | `70` (`PackageValidationFailed`) |
| Missing required feature | `23` (`CapabilityBaselineUnmet`) |
| Initialization pipeline failure | `42` (`PipelineStateCreationFailed`) |
| Resize sequence | `0` |
| Device removal | `51` (`DeviceRemoved`) |

Every scenario retains stdout/stderr captures; an executed mismatch remains
failed, and a missing execution remains pending. The deployment must contain
only the self-contained release executable and must not import development or
authoring dependencies.

Exit codes: `0` success for the mode, `1` executed failure or contract
violation, `2` required evidence pending/unavailable.

## Unresolved limitations

- Physical Ada and Blackwell correctness captures are pending.
- The reference-host `nw07_perf_ref_v1` runs are pending.
- Startup, peak GPU memory, and diagnostic-overhead measurements are pending.
- The release executable must be exported on Windows with the pinned DXC and
  the verified MSVC/LTCG player template; synthetic test payloads validate
  contract behavior only.
- Representative coverage does not imply exhaustive SKU coverage or equal
  performance.
- Audio, full editor UX, temporal simulation, advanced RTX features, and
  64 KiB exports remain outside NW-07 scope.

The milestone is therefore **not complete** in this tree: all implementation
contracts pass, but the physical hard gates and required hardware coverage are
honestly pending.
