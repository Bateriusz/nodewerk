# NW-01 Measured Evidence Capture

The commands below document how NW-01 physical evidence is captured reproducibly. The current
checked-in evidence is verified for the recorded desktop GPU captures; CPU model is stored as
metadata and does not gate GPU tier eligibility. Nothing in this repository fabricates or
substitutes measured data.

## Policy Separation

NW-01A ([ADR-0006](architecture/0006-capability-based-gpu-support-policy.md)) separates five
concerns that must not be collapsed into one GPU model:

1. **Declared support scope** — desktop RTX 40-series/Ada compatibility floor and desktop
   RTX 50-series/Blackwell current-generation target. This is qualification scope, never a
   runtime feature predicate.
2. **Exact capability eligibility** — applicable D3D12 queries plus the emitted package,
   player link manifest, capability-profile ID, and canonical requirements digest.
3. **Correctness coverage** — the identical common artifact on representative compliant
   desktop Ada and Blackwell hardware.
4. **Optional artifacts** — named profiles with exact additional capabilities, shader/runtime/
   toolchain requirements, dependencies, and artifact budgets, plus a separately exported and
   tested common fallback.
5. **Performance certification** — only the designated reference hardware, environment, and
   protocol in the named performance contract, currently `nw07_perf_ref_v1`.

Measured environment metadata is recorded independently of all five.

## Required Physical Targets

| Coverage target / role | Required hardware class | Required recorded environment |
| :--- | :--- | :--- |
| `ada_desktop` / correctness qualification | Any representative compliant desktop NVIDIA RTX 40-series/Ada adapter that satisfies the common profile (not Laptop GPU); exact SKU is not required | Exact adapter/device ID, architecture, VRAM, Windows 11 build/UBR, NVIDIA driver `NNN.NN`, WDDM version, measured CPU/RAM, queried capabilities, artifact hashes, cache/power/thermal metadata, unavailable telemetry |
| `blackwell_desktop` / correctness qualification | Any representative compliant desktop NVIDIA RTX 50-series/Blackwell adapter that satisfies the common profile (not Laptop GPU); exact SKU is not required | Exact adapter/device ID, architecture, VRAM, Windows 11 build/UBR, NVIDIA driver `NNN.NN`, WDDM version, measured CPU/RAM, queried capabilities, artifact hashes, cache/power/thermal metadata, unavailable telemetry |
| `nw07_perf_ref_v1` / performance certification | Exact designated desktop NVIDIA GeForce RTX 4060 reference board, CPU/host, environment, and protocol; no other SKU may substitute | Exact board/VBIOS/device ID/VRAM/clocks/power/cooling, CPU/motherboard/RAM/storage, OS build, driver/WDDM, power/thermal conditions, resolution/quality, cache conditions, measurement tools, raw samples, percentile method, artifact hashes |

The historical `rtx_4060_desktop` tier label remains backward-readable for
`benchmarks/hardware/rtx-4060-baseline.json`, whose recorded adapter is an RTX 4060 Ti
(`AD106`, device ID `0x2803`). It is not RTX 4060 evidence and cannot certify
`nw07_perf_ref_v1`. The historical `rtx_50_series_desktop` label remains readable for the
recorded RTX 5070 Ti evidence.

## Evidence Record Fields

New evidence records separate these backward-readable groups:

- `coverage_target`: `common`, `ada_desktop`, `blackwell_desktop`, or `optional:<profile-id>`.
- `evidence_role`: `correctness_qualification`, `performance_certification`,
  `informational_regression`, `pending`, or `unsupported`.
- `observed_hardware`: exact GPU model/architecture, vendor ID, device ID, adapter/LUID,
  VRAM; exact CPU model and RAM; desktop/mobile classification.
- `capability_profile`: profile ID, canonical requirements digest, and queried capability
  results, distinguishing unqueried from unsupported features.
- `artifact_provenance`: artifact/package/shader hashes, size-ledger identity, toolchain and
  build/reproducibility identity.
- `environment`: OS build/UBR, driver/WDDM/runtime, toolchain versions, clocks, power,
  thermal, cache state, display/resolution/quality.
- `protocol_identity`: performance-contract ID, metric definitions/operators, warm-up and run
  protocol, percentile method, matching rules, measurement tools, and raw samples.
- `result_status`: `pass`, `fail`, `pending`, `unsupported`, or `unverified`.
- `unavailable_telemetry`: explicit list of required fields that could not be measured, with
  reasons. Missing values are never zero-filled or inferred from another device.

An executed failure remains `fail`; unavailable required evidence remains `pending`; optional
features outside a profile's declared coverage remain `unsupported`. Aggregate reporting cannot
convert any of these into `pass`.

## Performance Contract `nw07_perf_ref_v1`

The NW-07 hard gate is certified only by a matching reference measurement under
`nw07_perf_ref_v1`:

- exact designated desktop NVIDIA GeForce RTX 4060 reference board, CPU/host, OS/driver,
  power/thermal/clocks, cache conditions, resolution/quality, measurement tools, artifact
  hashes, and protocol;
- `1920x1080` p99 GPU frame time `< 16.67` ms and p99 CPU submission time `< 16.67` ms;
- five-second warm-up followed by three uncapped 60-second runs, with **every** run required
  to satisfy both strict gates;
- retained raw per-run samples and named percentile methodology;
- clean-start measurements without an application pipeline cache, with cold driver-cache and
  warm results distinguished.

RTX 4060 Ti, other Ada, Blackwell, stale-artifact, mismatched-host, and mismatched-protocol
measurements remain available as informational or correctness evidence but cannot certify v1.
Missing reference hardware leaves certification pending. A separate matched-environment
Blackwell development/performance series is maintained without a new absolute Blackwell gate.
Replacement references or normalization require a new contract version with independently
justified thresholds, validation uncertainty, and bridge measurements; they never pass v1
retroactively.

## Backward-Readable Migration

- Existing records keep their original interpretation. Do not rewrite historical files.
- Absence of `coverage_target`, `evidence_role`, or `result_status` means
  `legacy_unclassified`; the record remains readable but cannot certify a new gate.
- `gpu_model`, `gpu_device_id`, and `gpu_architecture` are authoritative over a legacy
  `hardware_tier` label.
- A legacy tier label must never become proof that its recorded RTX 4060 Ti was an RTX 4060.

## 1. Release Ledger, Reproducibility, and Compiler Comparison

On a Windows 11 x64 host with the pinned clang-cl/lld-link, MSVC/LTCG, Windows SDK, DXC,
CMake, and Ninja versions installed:

```powershell
pwsh ./tools/verify-nw01.ps1 -Mode Full
```

This must execute both toolchains' fixtures, produce verified
`benchmarks/ledgers/*.json`, `benchmarks/reproducibility/*.json`, and a verified
`benchmarks/toolchain-manifest.v1.json`. Exit code `2` means concrete checks passed and only
physical evidence is still incomplete; exit code `3` means required build/toolchain coverage
was skipped or unavailable and must not be treated as an evidence-only gap.

CI routes this physical GPU work to the runner selected by the
`NW_PHYSICAL_RUNNER_LABEL` repository variable. The hosted `windows-latest` job performs only
GPU-less BuildChecks and never advertises a CleanSystem package. The packaging command
`tools/package-clean-system.ps1` refuses to run unless the supplied Full summary contains
verified ledgers and successful clang-cl and MSVC fixture execution.

The workflow can be started manually with `workflow_dispatch` after setting
`NW_PHYSICAL_RUNNER_LABEL` to the physical Windows GPU runner label. The resulting physical
job uploads the Full summary, ledgers, reproducibility reports, hardware baselines, and
CleanSystem package for evidence review.

## 2. Clean Physical-System Execution

Copy the `build/<preset>/deploy/` directory to a clean physical Windows 11 x64 machine with
no developer SDKs or DXC installed, then run:

```powershell
pwsh ./tools/verify-nw01.ps1 -Mode CleanSystem `
  -BinaryDir <deployed-directory> `
  -OsBuild <exact measured build e.g. 10.0.26100.3194> `
  -GpuModel "<exact adapter description>" `
  -NvidiaDriverVersion <NNN.NN>
```

The report must show SDK/redistributable absence, complete deployment-tree accounting,
offline DXIL execution, observed module paths, and a matching compute readback hash. Operator
GPU metadata is accepted only when it matches the adapter description emitted by both the
raster and compute fixtures. CleanSystem can pass only when the selected toolchain and all
three release ledgers are already verified by Full; unverified ledgers, including packaged
ledgers awaiting fixture execution, are rejected rather than used to certify provenance.

## 3. Hardware Baseline Capture

Provide every measured environment field; missing fields, registry-only adapter detection,
unknown revision numbers, or unmatched artifact hashes intentionally prevent certification.

```powershell
pwsh ./tools/verify-nw01.ps1 -Mode Full `
  -BinaryDir <deployed-directory> `
  -RecordBenchmark rtx_4060_desktop `
  -OsBuild <exact measured build e.g. 10.0.26100.3194> `
  -GpuModel "NVIDIA GeForce RTX 4060 Ti" `
  -NvidiaDriverVersion <NNN.NN> `
  -CpuModel "<measured CPU model>" `
  -GpuArchitecture "<measured architecture>" `
  -WddmDriverVersion <a.b.c.d> `
  -WindowsPowerPlan "<measured plan>" `
  -NvidiaPowerManagementMode "<measured mode>" `
  -PciePowerManagement "<measured setting>" `
  -RefreshHz <measured refresh rate> `
  -DriverShaderCacheState cold_cleared `
  -OsFileCacheState cold
```

Repeat with `-RecordBenchmark rtx_50_series_desktop` and the corresponding desktop RTX
50-series adapter. The command records `benchmarks/hardware/rtx-4060-baseline.json` or
`benchmarks/hardware/rtx-50-series-baseline.json` only after fixture execution, adapter
tier matching, environment measurement, and artifact-hash provenance all verify. The
evidence-completeness gate additionally requires those recorded executable hashes and the
toolchain ID to match the selected toolchain's verified release ledgers; stale physical
results from older binaries cannot certify newly built artifacts. GPU tier eligibility is
determined from the observed GPU model and desktop/mobile classification. CPU metadata must be
recorded, but CPU family, suffix, or desktop/mobile class is not a certification gate. Generic
host strings such as `x86_64`, `AMD64`, or `Intel64 Family ...` are rejected as missing CPU
metadata.

## Current Status

Verified evidence covers Windows `10.0.26200.9711` and `10.0.26300.9818`, NVIDIA driver
`610.88`, desktop RTX 4060 Ti, and desktop RTX 5070 Ti. The actual minimum tested Windows build
is `10.0.26200.9711`; `10.0.22631.4317` remains only the lower intended runtime target. The
RTX 5070 Ti capture records its i7-1360P host CPU as metadata; CPU class does not gate the
verified desktop GPU evidence.
Verified release ledgers, compiler comparison, reproducibility, offline DXIL execution, and
clean-system execution are recorded in the checked-in benchmark artifacts. Re-run the commands
below after any toolchain, shader, or fixture change.
