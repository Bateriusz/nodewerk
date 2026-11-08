# NW-01 Measured Evidence Capture

NW-01 remains **unverified** until the commands below are executed on the required physical
Windows 11 x64 machines. Nothing in this repository fabricates or substitutes measured data.

## Required Physical Targets

| Tier | Required hardware class | Required recorded environment |
| :--- | :--- | :--- |
| `rtx_4060_desktop` | Desktop NVIDIA GeForce RTX 4060 Ti (not Laptop GPU) | Exact Windows 11 build/UBR, NVIDIA driver `NNN.NN`, WDDM version, measured CPU model, GPU architecture |
| `rtx_50_series_desktop` | Desktop NVIDIA RTX 50-series (not Laptop GPU) | Exact Windows 11 build/UBR, NVIDIA driver `NNN.NN`, WDDM version, measured CPU model, GPU architecture |

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
results from older binaries cannot certify newly built artifacts. A desktop GPU name is not
sufficient by itself: CPU metadata must be recorded, but a specific CPU family or model
whitelist is not imposed. Ryzen Threadripper, Intel Xeon W, Core, Ryzen, and other valid
desktop workstation processors are acceptable when paired with the required desktop GPU.

## Current Status

No measured Windows, RTX 4060 Ti, RTX 50-series, reproducibility, compiler-comparison, offline
GPU, or clean-system results are present in this workspace. The checked-in hardware baseline
and clean-system JSON files are unverified templates with null measurement fields, not
measurement records. Those acceptance requirements remain explicitly unverified. The pinned
minimum Windows build and selected clang-cl/lld-link toolchain are therefore *targets*, not
tested claims, until this evidence is captured.
