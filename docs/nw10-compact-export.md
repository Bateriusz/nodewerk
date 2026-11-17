# NW-10 Compact Export, Restricted Intro & Measured 64 KiB Attempt

- **Status:** Implemented from immutable base
  `c7a73ce64a15973fc18b384543894e68ee2464e8`
- **Planning provenance only:** `270d298f1b037548e0ab4808b2c123c12ddcc4e6`
- **Canonical comparison artifact:** the existing NW-07 proof
  (`fixtures/nw07/production_loop.nwgraph`,
  `benchmarks/nw07/canonical-production.v1.json`)
- **Restricted intro profile:** `nw10.intro.sdf20.v1`
- **Performance contract:** `nw07_perf_ref_v1` (`nw07-perf-run.v1`)
- **Evidence:** `benchmarks/nw10/compact-proof.v1.json`,
  `benchmarks/nw10/intro-profile.v1.json`,
  `benchmarks/nw10/evidence-status.v1.json`, plus the archived
  `proof-export-report.v1.json` and `intro-export-report.v1.json` raw reports.

## Result and decision

The NW-07 graph, quality profile (`nw07.quality.v1`), 1920x1080 output,
fixed-tick replay, numeric/image tolerances, 262,144-byte proof ceiling,
131,072-byte stretch measurement, 1,048,576-byte standard tier, 2,000 ms
startup ceiling, and 536,870,912-byte GPU-memory ceiling are preserved.

Contract-only exports with structural DXIL payloads measure:

| Artifact | Complete distribution | Payload | Status |
| :--- | ---: | ---: | :--- |
| Canonical NW-07 proof | 18,008 bytes | 17,626-byte `.nwp` | contract-only |
| Compact NW-07 proof candidate | 4,475 bytes | 4,093-byte MSZIP container | contract-only |
| Restricted intro canonical | 4,346 bytes | 3,964-byte `.nwp` | contract-only |
| Restricted intro compact | 2,089 bytes | 1,707-byte MSZIP container | contract-only |

These numbers validate the accounting, decoder, and deterministic export
paths. They are **not** Windows release artifacts and are not physical 64 KiB
proof. The exact Windows x64 executable, startup time, clean-system startup,
GPU frame time, CPU submit time, startup/decode memory, Ada/Blackwell visuals,
and dependency closure remain explicitly pending. The decision is
**continue**, not a universal feasibility or impossibility claim.

### Hosted Windows feasibility result

The hosted Windows x64 experiment is treated as a measured feasibility run,
not as a hard 64 KiB gate. The current run measured the inherited NW-07 proof
at 264,390 bytes (above its 262,144-byte proof tier) and the restricted-intro
compact candidate at 219,710 bytes. The compact candidate then failed runtime
validation with package-validation exit code 70, so it is a tested blocker,
not a validated runnable distribution. The workflow preserves and smoke-tests
the canonical restricted-intro fallback, records the smallest validated
runnable artifact separately from the smallest measured candidate, and keeps
the NW-10 decision at **continue** when the fallback remains valid.

A compact candidate that fails runtime validation is never counted as an
achieved size result. Its stdout/stderr, exit code, measured byte count, and
dependency evidence are archived with the Windows CI evidence. Clean-system
and matched physical performance/correctness certification remain separate
pending evidence under the inherited NW-07 protocol.

## Preserved architecture

The implementation reuses:

- the NW-02 typed graph and semantic validator;
- the NW-03 offline DXC compilation and `.nwp` v1 package;
- the NW-05 generated-player embedding and link-manifest path;
- the NW-06 procedural content and fixed-tick scene semantics;
- the NW-07 export, contract, baseline, capture, and verification tooling.

There is no second player, graph model, quality profile, performance contract,
audio path, mesh pipeline, or hardware ray-tracing requirement.

The NW-07 build-input digest changed only because the bounded compact decoder
was added to the executable-producing closure. The superseded digest remains
recorded in `benchmarks/nw07/canonical-production.v1.json` under
`build_inputs_provenance.superseded_digests`.

## Compact delivery container

The canonical `.nwp` bytes remain the semantic format. The compact container is
a delivery encoding that reconstructs the exact canonical bytes before the
existing NW-03 decoder validates them.

Header (64 bytes, little endian):

```text
magic[8] = "NWC1\n\0\0\0"
u16 version      = 1
u16 header_size  = 64
u16 codec
u16 layout
u32 flags
u32 decoded_size
u32 payload_size
u32 container_size
u8  decoded_sha256[32]
```

Supported identifiers:

- codec `nwc.codec.stored.v1`
- codec `nwc.codec.mszip.v1` (Windows inbox MSZIP decompressor)
- layout `nwc.layout.canonical.v1`
- layout `nwc.layout.aos.v1`
- layout `nwc.layout.soa.v1`
- parameter layouts `nwc.params.canonical.v1`,
  `nwc.params.varint-stringtable.v1`, `nwc.params.float16.v1`,
  `nwc.params.q16.v1`

The uncompressed container payload is a bounded record table plus raw chunks.
Record kinds are `raw` and backward `reference`. References must target an
earlier raw record, must match its length, and cannot form cycles. AoS records
are 14 bytes; SoA columns are 13 bytes per record. Unknown codecs, layouts,
versions, flags, record kinds, forward references, arithmetic overflow,
truncation, checksum mismatch, or expansion beyond the declared bounds fail
before the package reaches the GPU.

The decoder is bounded by:

- container size `<= 64 MiB`;
- decoded canonical package size `<= 256 MiB`;
- record count `<= 1,048,576`;
- declared expansion ratio `<= 256x`;
- the existing `.nwp` per-section and aggregate decode budgets.

## Measured alternatives

`tools/nw10_compact.py` measures every alternative on the exact encoded
package:

- **Parameter packing:** canonical, varint/string-table, float16, and signed
  Q16. Packing is eligible only when measured maximum absolute error remains
  within the frozen `1e-5` numeric tolerance. Float16/Q16 were rejected in the
  contract measurement.
- **Delta references:** duplicate chunks become backward references. The
  record table prevents inferred or forward references.
- **AoS versus SoA:** both layouts are measured; SoA saves one byte per record
  but is not assumed to win for every payload.
- **Shader specialization/fusion:** the established NW-05 fusion decisions are
  retained. No unmeasured extra fusion is applied and no new shader semantics
  are required.
- **Linker elimination:** release PE section/symbol elimination is pending on
  the exact Windows artifact; source-level estimates are not substituted for
  bytes.
- **Compression:** stored, MSZIP, zlib, bzip2, and lzma are measured. Only
  player-supported stored/MSZIP encodings are selectable. Compression that
  increases the complete distributed bytes is rejected and the canonical
  executable remains selected. Candidate-only lzma produced a smaller payload
  but is not selected because no measured player decoder/dependency path exists
  for it; introducing a new compressor library is outside NW-10's measured
  options.

## Restricted intro

The frozen intro is a 20-second (960,000 ticks at 48 kHz) 1920x1080 loop:

- sphere and torus signed-distance composition;
- seeded procedural material sampled from the existing generated texture;
- absolute-tick camera curves (no accumulated frame deltas);
- ACES tonemapping;
- offline-compiled shader only;
- no audio, mesh pipeline, hardware ray tracing, external assets, or runtime
  DXC.

The graph uses only the existing fullscreen/present path and the frozen shader
specialization `shaders/nw10/nw10_sdf_intro_cs.hlsl`.

## Reproducible commands

### Contract-only (GPU-less host)

```bash
NODEWERK_NW03_TEST_DIGEST=1 python tools/nw10_evidence.py \
  --output-dir benchmarks/nw10

python -m pytest -q tests/test_nw10_*.py
```

### Windows release export

```powershell
pwsh ./tools/nw01/bootstrap-windows.ps1 -Ci
pwsh ./tools/nw01/enter-build-env.ps1
cmake --preset nw07-release -DNW10_COMPACT_PACKAGE=ON -DNW_ENFORCE_PINNED_TOOLCHAIN=ON
cmake --build --preset nw07-release --target nw_procedural_image_player --parallel 2

$template = "build/nw07-release/staging/procedural_image_player/nw_procedural_image_player.exe"
$dxc = (Get-Command dxc.exe -ErrorAction Stop).Source

python tools/nw10_proof.py --output build/nw10-proof `
  --player-template $template --dxc $dxc `
  --report build/nw10-proof/report.json

python tools/nw10_intro.py --output build/nw10-intro `
  --player-template $template --dxc $dxc `
  --report build/nw10-intro/report.json
```

The compact executable is a single self-contained Windows x64 file. It embeds
the compact package and the 32-byte manifest binding. `embedded_package.cpp`
expands the compact package to canonical bytes before calling the existing
`PackagePlayer` package decoder. The generated player verifies the canonical
package SHA-256 and the manifest binding before GPU initialization.

The compact decoder is opt-in via `-DNW10_COMPACT_PACKAGE=ON`. The canonical
NW-07 release build leaves it at the default `OFF`, so the existing 256 KiB
proof closure and dependency accounting are preserved unchanged. NW-10 compact
proof/intro artifacts build the template with the option enabled.

No executable packer is used. In particular, Crinkler is not invoked and its
x64 support is not assumed. The only compression mode considered for the
artifact is the Windows inbox MSZIP decoder; candidate-only zlib/bzip2/lzma
payloads remain measured but unselected because their decoder/dependency cost
is not part of the verified closure.

### Physical capture and clean-system checks

Use the existing NW-07 capture tooling and evidence schema. Every before/after
performance record must name `nw07_perf_ref_v1`, the exact reference board,
host, OS/driver, toolchain, artifact hashes, resolution/quality, cache state,
tools, and protocol. The five-second warm-up and three independent uncapped
60-second runs in fresh processes are mandatory. A failed run cannot be hidden
by aggregation.

Missing exact reference capture remains pending. An RTX 4060 Ti on another
host, another Ada GPU, Blackwell hardware, faster hardware, stale artifacts, or
synthetic tests cannot certify preservation; such results are informational or
unverified.

Clean-system deployment commands (exact final artifact; separate from the
synthetic pytest gates):

```powershell
python tools/nw07_clean_system.py `
  --fixture build/nw10-diagnostics/nw_package_player_fixture.exe `
  --package build/nw10-proof/compact `
  --deployment build/nw10-clean-deployment `
  --execution-kind physical `
  --output build/nw10-clean-system.json

python tools/nw07_capture.py release-correctness `
  --package build/nw10-proof/compact `
  --executable build/nw10-proof/compact/nw_common_d3d12_fl12_0_sm6_0_v1.exe `
  --diagnostic-executable build/nw10-diagnostics/nw_procedural_image_player.exe `
  --capture-id nw10-ada-<run-id> --tier ada `
  --capture-command "<external RGBA32F capture adapter>" `
  --captures-dir nw10-captures --reference-captures-dir nw10-reference-captures `
  --reference-execution-output build/nw10-reference-ada.json `
  --metrics-command "<startup/memory/frame-time collector>" `
  --metrics-output build/nw10-metrics-ada.json `
  --evidence build/nw10-evidence-ada.json
```

Repeat the correctness command with `--tier blackwell`. A virtual machine is
not sufficient; use `physical` or documented GPU passthrough semantics from the
NW-07 contract.

## Byte-accounting policy

A complete runnable distribution is exactly one executable. The package,
compact payload, manifest binding, and footer are subsets of that executable.
Auxiliary reports are not required at runtime and are reported separately.
System DLLs are not counted as distributed application bytes but are recorded
as dependencies. Every compared artifact retains its source revision, frozen
configuration, toolchain identity, build command, hashes, byte ledger, raw
measurements, and result status.

## Regression coverage

`tests/test_nw10_*.py` covers:

- container round-trips for stored/MSZIP and canonical/AoS/SoA layouts;
- malformed, truncated, unsupported, checksum, reference, overflow, and
  worst-case expansion failures;
- NW-07 proof and intro deterministic export;
- byte ledgers, tier preservation, and compression rejection;
- restricted intro profile boundaries and excluded features;
- performance-evidence classification and physical-pending semantics;
- static evidence/schema validation and archived artifact hashes.

The full repository test suite and the existing NW-07 contract verification
must pass without relaxing frozen gates.
