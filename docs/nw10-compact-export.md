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
  `benchmarks/nw10/common-artifact-correctness.v1.json`,
  `benchmarks/nw10/evidence-status.v1.json`,
  `benchmarks/nw10/system-validation.v1.json`,
  `benchmarks/nw10/reused-physical-evidence.v1.json`,
  `benchmarks/nw10/windows-compact-feasibility.v1.json`, and
  `benchmarks/nw10/windows-pe-ledger.v1.json`, plus the archived
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
proof. Historical physical measurements are retained only as archived
attribution in `benchmarks/nw10/reused-physical-evidence.v1.json`; they were
captured at source head `4d216ddf` and cannot certify the current checkout.
The historical correctness run did not record a separate packed GPU capture
set, the clean-system run was on a different host from the startup/memory
measurements, and the later `063bb45b` artifact exited with `0xC0000005`.
All of those physical claims are therefore **pending**, not verified. The exact
`nw07_perf_ref_v1` reference capture and its bindings remain unavailable, so
performance preservation is still **pending** and no faster substitute hardware
is used to certify it. The decision is **continue**, not a universal
feasibility or impossibility claim.

`windows-pe-ledger.v1.json` keeps the prior valid `6c6fdb40` artifact and its
complete measured ledger as the selected final distribution. The non-runnable
`063bb45b` current-head attempt is retained in `current_attempt` with its
`rejected_runtime_failure`/`0xC0000005` state; its ledger is not promoted to a
current-head final ledger.

### Hosted Windows feasibility result

The hosted Windows x64 experiment is treated as a measured feasibility run,
not as a hard 64 KiB gate. Current run `44542203189` measures the decoder-OFF
canonical NW-07 proof at **260,294 bytes**, inside the frozen 262,144-byte proof
tier. The restricted-intro compact candidate measures **228,181 bytes** and the
canonical fallback **237,118 bytes**. The hosted runner exits both GPU runtime
probes with `0xC0000005`; because that host is non-certifying for this physical
GPU requirement, both outcomes remain `pending_runtime_validation` rather than
being mislabeled as compact-package failures. Dependency closure and the
single-file/link-manifest checks still pass.

The same run measures a PE ledger for the 237,118-byte restricted intro, but
that artifact exited `0xC0000005`. The ledger is archived as a superseded
measurement and cannot be the selected final distribution. The retained prior
valid ledger measures a **3,584-byte** compact-decoder template cost (3,072
bytes in `.text`, 512 bytes in `.rdata`), and the only added runtime import is
`cabinet.dll`. The historical evidence artifact is retained as GitHub Actions
artifact `14540370510` with digest
`sha256:7fb0807a098a4152eae8298fef5984e1410cc7ca14b910226680403d39c876f4`.

Physical correctness and system measurements are represented separately from
this hosted feasibility result. Their historical bindings are retained only as
archived attribution in `reused-physical-evidence.v1.json`; they are not
current-head certification. The exact performance reference capture remains
pending.

The Windows workflow constructs `build/nw10-windows-compact-feasibility.json`
with `tools/nw10_feasibility.py` and schema-validates it in both outcomes: a
validated at-most-65,536-byte artifact yields `target_met=true` and
`classification=complete`; a larger or failed candidate yields
`classification=continue`, a tested blocker, and the minimum measured
candidate/validated sizes. Neither branch is a universal feasibility claim.

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
- codec `nwc.codec.mszip.v1` (Windows release encoding uses the inbox
  Compression API; GPU-less contract measurements use a portable CK-framed
  reference encoder; decoded canonical package SHA-256 is the cross-host identity)
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
- **Linker elimination:** measured in the archived Windows run against an
  otherwise identical pinned control link with `/OPT:NOREF /OPT:NOICF`.
  `/OPT:REF + /OPT:ICF=4` save 723,456 bytes in the complete distribution
  (the same 723,456 bytes in the PE image). The largest raw-section savings are
  592,384 bytes in `.text` and 93,696 bytes in `.rdata`.
- **Compression:** stored, MSZIP, zlib, bzip2, and lzma are measured. Only
  player-supported stored/MSZIP encodings are selectable. Compression that
  increases the complete distributed bytes is rejected and the canonical
  executable remains selected. Candidate-only lzma produced a smaller payload
  but is not selected because no measured player decoder/dependency path exists
  for it; introducing a new compressor library is outside NW-10's measured
  options. MSZIP encoded bytes are intentionally host-specific: the Windows
  release path uses the inbox Compression API, while GPU-less contract evidence
  uses the portable CK-framed reference encoder. Encoded payload SHA-256 values
  are therefore only local provenance. Cross-host identity is the SHA-256 of the
  decoded canonical package, which both decoders must reconstruct exactly.

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

This regeneration is non-destructive: it refreshes the contract evidence and
file hashes and never downgrades an existing tracked physical status.

### Windows release export

```powershell
pwsh ./tools/nw01/bootstrap-windows.ps1 -Ci
pwsh ./tools/nw01/enter-build-env.ps1
cmake --preset nw07-release -DNW10_COMPACT_PACKAGE=ON -DNW_ENFORCE_PINNED_TOOLCHAIN=ON
cmake --build --preset nw07-release --target nw_procedural_image_player --parallel 2

# Canonical NW-07 baseline: decoder OFF so the proof tier excludes NW-10 code.
cmake --preset nw07-release -B build/nw07-release-decoder-off \
  -DNW10_COMPACT_PACKAGE=OFF -DNW_ENFORCE_PINNED_TOOLCHAIN=ON
cmake --build build/nw07-release-decoder-off --target nw_procedural_image_player --parallel 2

$template = "build/nw07-release/staging/procedural_image_player/nw_procedural_image_player.exe"
$canonicalTemplate = "build/nw07-release-decoder-off/staging/procedural_image_player/nw_procedural_image_player.exe"
$dxc = (Get-Command dxc.exe -ErrorAction Stop).Source

python tools/nw10_proof.py --output build/nw10-proof `
  --canonical-player-template $canonicalTemplate `
  --player-template $template --dxc $dxc `
  --report build/nw10-proof/report.json

python tools/nw10_intro.py --output build/nw10-intro `
  --canonical-player-template $canonicalTemplate `
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
proof closure and dependency accounting are preserved unchanged. The canonical
proof/intro baselines are measured from the decoder-OFF template; only the
compact candidates are linked from the decoder-ON template. A canonical proof
above 262,144 bytes fails the export instead of silently redefining the tier.

No executable packer is used. In particular, Crinkler is not invoked and its
x64 support is not assumed. The only compression mode considered for the
artifact is MSZIP. Windows release artifacts are encoded and decoded through
the Windows inbox Compression API; GPU-less contract measurements use the
portable reference codec and do not claim byte-identical encoded payloads; candidate-only zlib/bzip2/lzma payloads remain measured but
unselected because their decoder/dependency cost is not part of the verified
closure.

### Physical capture and clean-system checks

Use the existing NW-07 capture tooling and evidence schema. Every before/after
performance record must name `nw07_perf_ref_v1`, the exact reference board,
host, OS/driver, toolchain, artifact hashes, resolution/quality, cache state,
tools, and protocol. The five-second warm-up and three independent uncapped
60-second runs in fresh processes are mandatory. A failed run cannot be hidden
by aggregation.

The exact RTX 4060 Ti reference capture is **not bound**: the frozen contract
still records `binding_status: pending_reference_capture` and device identity
`10245`. A different device identity is a new reference and requires a fresh
exact-board capture and contract revision. Representative desktop Ada and
Blackwell packed/unpacked correctness is also pending; a different host,
another Ada GPU, faster hardware, stale artifacts, or synthetic tests cannot
substitute for the matched `nw07_perf_ref_v1` evidence.

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

### Scripted physical-evidence publication

Physical capture output stays under `build/` or in transfer ZIPs. Publication is
fail-closed and writes only privacy-safe summaries under `benchmarks/nw10/`.

Matched Ada performance remains the first step. The publication tooling
supports schema validation, controlled staging, commit, and push, but it cannot
create the missing exact reference capture. The frozen contract is still
unbound, so publication remains pending until a fresh exact-board capture is
available:

```powershell
pwsh ./tools/nw10/run-performance-evidence.ps1 -Commit
```

After Ada and Blackwell correctness bundles have been captured, compare and
publish them in one command. Raw RGBA32F captures and capture manifests are not
tracked:

```powershell
pwsh ./tools/nw10/run-correctness-evidence.ps1 -Compare -AdaRecord build/nw10-correctness-scripted/ada/capture.json -AdaCaptures build/nw10-correctness-scripted/ada/captures -BlackwellRecord build/nw10-correctness-scripted/blackwell/capture.json -BlackwellCaptures build/nw10-correctness-scripted/blackwell/captures -Publish -Commit
```

Packed/unpacked correctness requires a separately rendered packed capture set
plus its capture manifests; compositional decoder parity alone is rejected:

```powershell
# Capture the same common package through an independent unpacked diagnostic
# sibling and a decoder-ON diagnostic sibling with the compact payload embedded.
pwsh ./tools/nw10/run-correctness-evidence.ps1 -Tier ada -PackagingMode unpacked -Bundle
pwsh ./tools/nw10/run-correctness-evidence.ps1 -Tier ada -PackagingMode packed -Bundle
pwsh ./tools/nw10/run-correctness-evidence.ps1 -Tier blackwell -PackagingMode unpacked -Bundle
pwsh ./tools/nw10/run-correctness-evidence.ps1 -Tier blackwell -PackagingMode packed -Bundle
```

```powershell
pwsh ./tools/nw10/run-correctness-evidence.ps1 -Compare `
  -AdaRecord build/nw10-correctness-scripted/ada/capture.json `
  -AdaCaptures build/nw10-correctness-scripted/ada/captures `
  -BlackwellRecord build/nw10-correctness-scripted/blackwell/capture.json `
  -BlackwellCaptures build/nw10-correctness-scripted/blackwell/captures `
  -PackedAdaRecord build/nw10-correctness-scripted/ada-packed/capture.json `
  -PackedAdaCaptures build/nw10-correctness-scripted/ada-packed/captures `
  -PackedBlackwellRecord build/nw10-correctness-scripted/blackwell-packed/capture.json `
  -PackedBlackwellCaptures build/nw10-correctness-scripted/blackwell-packed/captures `
  -PackedPackageSha256 <decoded-package-sha256> `
  -UnpackedPackageSha256 <decoded-package-sha256> `
  -Publish -Commit
```

The transfer-bundle preparation path accepts the same `-PackagingMode`
selection, so remote capture hosts need no compiler, CMake, DXC, Python, or
repository checkout. Both modes record the exact diagnostic executable hash,
encoded-container hash (packed mode), capture-manifest hashes, and runtime exit
status. The compare command rejects mismatched packaging modes, hardware,
manifests, or canonical package identities.

The correctness publisher validates the cross-hardware comparison, removes raw
capture material, audits user/profile paths and forbidden tokens, validates the
published summary against
`schemas/nw10-common-artifact-correctness.schema.json`, updates
`evidence-status.v1.json`, stages only the two allowed tracked evidence files,
and optionally pushes when `-Push` is explicitly supplied.

The comparison must include a verified packed/unpacked artifact comparison and
a measured desktop chassis for both tiers. The publisher rejects asserted
desktop values, pending packed/unpacked comparisons, redefined fixed ticks,
non-frozen tolerances, and test-payload or non-release reports.

Clean-system/system publication consumes the transferred physical result ZIP,
fresh physical metrics JSON, and the exact final intro export report. It verifies
every result-bundle file against the embedded manifest before publishing:

```powershell
pwsh ./tools/nw10/publish-system-evidence.ps1 -CleanSystemBundle build/nw10-clean-system-result.zip -Metrics build/nw10-system-metrics.json -IntroReport build/nw10-intro/report.json -Commit
```

The metrics JSON is the fresh collector output used by the NW-07/NW-10 physical
capture path. Its `measurements` object must bind startup and engine-committed
memory to the exact physical run and retain
`post_initialization_allocation_violation=0`; missing or out-of-budget values
fail publication. Its `metadata` must also name the exact
`executable_sha256`, `package_sha256`, `execution_kind`, `windows_build`,
`adapter_description`, and `driver_version`, all matching the clean-system
result bundle. The system publisher extracts only sanitized host/GPU
identity and scenario results from the clean-system ZIP. Raw stdout/stderr,
local paths, and the result ZIP itself are never tracked.

Both publishers require a clean tracked tree (build outputs are allowed), the
expected `agent/NW-10` branch, schema-valid output, `git diff --check`, and
an allowlist of staged evidence paths. `-Push` always requires `-Commit`.

When physical evidence is absent, the checked-in pending summaries are
reproducible without inventing measurements:

```bash
python tools/nw10_publish_evidence.py pending-correctness
python tools/nw10_publish_evidence.py pending-system
```

## Byte-accounting policy

A complete runnable distribution is exactly one executable. The package,
compact payload, manifest binding, and footer are subsets of that executable.
Auxiliary reports are not required at runtime and are reported separately.
System DLLs are not counted as distributed application bytes but are recorded
as dependencies. Every compared artifact retains its source revision, frozen
configuration, toolchain identity, build command, hashes, byte ledger, raw
measurements, and result status.

The compact ledger always records `cabinet.dll` as a required inbox dependency
and `decoder_code_bytes` as a subset of the player image. When the exact
MSVC/x64 decoder contribution has not been measured, the ledger sets
`all_required_bytes_accounted=false` and names `decoder_code_bytes` in
`unaccounted_components`; it never reports a complete ledger with an unknown
decoder cost.

## Regression coverage

`tests/test_nw10_*.py` covers:

- container round-trips for stored/MSZIP and canonical/AoS/SoA layouts;
- malformed, truncated, unsupported, checksum, reference, overflow, and
  worst-case expansion failures;
- NW-07 proof and intro deterministic export;
- byte ledgers, tier preservation, and compression rejection;
- restricted intro profile boundaries and excluded features;
- performance-evidence classification and physical-pending semantics;
- tracked Ada/Blackwell correctness summary and privacy boundaries;
- static evidence/schema validation and archived artifact hashes.

The full repository test suite and the existing NW-07 contract verification
must pass without relaxing frozen gates.
