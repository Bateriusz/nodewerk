# Preserved Toolchain, Compact-Profile & CRT Experiments

- **Validation Schema:** [`schemas/experiment-record.schema.json`](../schemas/experiment-record.schema.json)
- **Recorded Experiments Directory:** [`benchmarks/experiments/`](../benchmarks/experiments/)

## Preservation Policy

Per NW-01 recovery semantics, any comparative toolchain evaluation, experimental compact-profile flag combination, or CRT-elimination experiment that is rejected or fails **must preserve its exact configuration, reproducible CMake command, obligations, byte measurements, and failure diagnostics**. Unexecuted or link-failing experiments on hosts without the Windows 11 x64 toolchain record `null` for unmeasured byte counts and list them in `unavailable_measurements` rather than presenting invented numbers. Failed experiments must never silently substitute a different configuration or strip platform security mitigations (`/GS`, `/guard:cf`, `/DYNAMICBASE`, `/HIGHENTROPYVA`, `/NXCOMPAT`, `/CETCOMPAT`) to reach a speculative size target.

The same rule applies to a failed re-verification after an earlier success: `tools/size_ledger.py` leaves previously measured PE byte counts in place but marks those size ledgers `unverified` with the failure reason, clears the toolchain evidence flags, and keeps the manifest and ledgers consistent. A toolchain whose offline DXIL payload was not measured still retains its measured compute fixture PE bytes in an unverified experiment record instead of dropping the measurement.

Toolchain eligibility also requires `fixture_execution_verified=true`. Both clang-cl/lld-link and
MSVC/LTCG must execute the identical clear/present and compute fixtures successfully (including a
clean D3D12 info queue) before their measured bytes may participate in selection. Build hashes and
PDB evidence alone are not sufficient.

Normal release builds are **not** a CRT-elimination experiment: they use the complete static CRT
(`MSVC_RUNTIME_LIBRARY=MultiThreaded`/`/MT`), which supplies `mainCRTStartup`, UCRT internals,
the floating-point ABI marker, and `/GS` security-cookie support without importing any CRT DLL.
`/NODEFAULTLIB` remains only the explicit `NW_ENABLE_CRT_ELIMINATION_EXPERIMENT` path documented
below.

Failed CleanSystem and Full runs preserve measured exit codes, deployed sizes/hashes, SDK-probe
results, stderr hashes/tails, validation messages, and observed module inventories in their JSON
reports; the overall `verification_status`/`clean_system_passed` remains false instead of
replacing the measurements with nulls. `verify_nw01.py --record-benchmark <tier>` captures timing,
committed memory, and capability metrics into the benchmark schema without interpolating missing
values.

## Summary of Recorded Experiments

### 1. Toolchain Comparison: `MSVC/LTCG` vs `clang-cl/lld-link`
- **Record:** [`benchmarks/experiments/msvc-ltcg-comparison.json`](../benchmarks/experiments/msvc-ltcg-comparison.json)
- **Reproducible Command:** `cmake --preset msvc-release-ltcg && cmake --build --preset msvc-release-ltcg`
- **Category:** `toolchain_comparison`
- **Status:** `rejected_preserved` (`substituted_silently: false`, `verification_status: unverified` on Linux host)
- **Outcome:** The experiment is preserved unverified because no Windows PE byte comparison has been executed. `clang-cl/lld-link` remains only the provisional default in `benchmarks/toolchain-manifest.v1.json`; `tools/size_ledger.py` selects the smaller verified aggregate footprint (or the only verified toolchain) from the recorded ledgers and updates `selected_toolchain` after measurement, including when `MSVC/LTCG` wins. The `MSVC/LTCG` presets (`msvc-dev`, `msvc-release-ltcg`, `msvc-compact-experiment`) and ledger schemas remain preserved in `benchmarks/ledgers/*-msvc-ltcg-release.json` for identical-fixture comparison on Windows 11 x64.

### 2. Compact Profile Experiment: Section Merging (`/MERGE:.rdata=.text`) + Sub-Page Alignment (`/ALIGN:16`)
- **Record:** [`benchmarks/experiments/compact-profile-experiment.json`](../benchmarks/experiments/compact-profile-experiment.json)
- **Reproducible Command:** `cmake --preset clang-cl-compact-experiment -DNW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT=ON && cmake --build --preset clang-cl-compact-experiment`
- **Category:** `compact_profile`
- **Status:** `failed_preserved` (`substituted_silently: false`, `verification_status: unverified` on Linux host)
- **Failure Diagnostic (`CFG_CET_SECTION_MERGE_INCOMPATIBLE`):** Combining `/MERGE:.rdata=.text` and `/ALIGN:16` with `/guard:cf` (Control Flow Guard) and `/CETCOMPAT` (Shadow Stack) violates Windows 11 x64 PE section alignment and W^X page protection requirements (an OS PE loader / linker constraint occurring prior to `main()`, not an application `ExitCode`).
- **Outcome:** Preserved behind the explicit `-DNW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT=ON` CMake option in [`cmake/BuildProfiles.cmake`](../cmake/BuildProfiles.cmake). Platform security mitigations are **not** disabled to rescue this flag combination.

### 3. CRT Elimination Experiment: Naive `/NODEFAULTLIB` with Security Mitigations Retained
- **Record:** [`benchmarks/experiments/crt-elimination-experiment.json`](../benchmarks/experiments/crt-elimination-experiment.json)
- **Reproducible Command:** `cmake --preset clang-cl-compact-experiment -DNW_ENABLE_CRT_ELIMINATION_EXPERIMENT=ON && cmake --build --preset clang-cl-compact-experiment`
- **Category:** `crt_elimination`
- **Status:** `failed_preserved` (`substituted_silently: false`, `verification_status: unverified` on Linux host)
- **Failure Diagnostic (`NODEFAULTLIB_MISSING_CRT_AND_SECURITY_STUBS`):** Enabling `/NODEFAULTLIB /ENTRY:mainCRTStartup` alongside `/GS` and `/guard:cf` without supplying custom `mainCRTStartup`, `__security_cookie`, `__security_check_cookie`, `__guard_check_icall_fptr`, `_fltused`, and freestanding memory intrinsics fails at link time before producing a PE executable.
- **Outcome:** `NW_ENABLE_CRT_ELIMINATION_EXPERIMENT` remains `OFF` by default; all six initialization and cleanup obligations are documented in [`src/player/crt_experiment_obligations.h`](../src/player/crt_experiment_obligations.h) and [`ADR-0004`](architecture/0004-dependency-closure-and-crt-experiment.md).

## Inbox-Runtime Blocker Log

- **Current Status:** No inbox-runtime blocker encountered on the stated Feature Level `12_0` + Shader Model `6.0` baseline (`d3d12.dll` inbox on Windows 11 x64 `>= 10.0.22631.4317` natively supports FL `12_0`, SM `6.0` DXIL, bounded descriptor tables, ordinary barriers, and single direct queue without `D3D12Core.dll` Agility SDK redistribution).
- **Blocker Reporting Mechanism:** Verified via `nw_empty_player --simulate-failure InboxRuntimeBlocker --diagnostics`, which returns exit code `25` (`InboxRuntimeBlocker`) and emits the structured blocker diagnostic required before any profile change.
