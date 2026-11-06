# Preserved Toolchain, Compact-Profile & CRT Experiments

- **Validation Schema:** [`schemas/experiment-record.schema.json`](../schemas/experiment-record.schema.json)
- **Recorded Experiments Directory:** [`benchmarks/experiments/`](../benchmarks/experiments/)

## Preservation Policy

Per NW-01 recovery semantics, any comparative toolchain evaluation, experimental compact-profile flag combination, or CRT-elimination experiment that is rejected or fails **must preserve its exact configuration, obligations, byte measurements, and failure diagnostics**. Failed experiments must never silently substitute a different configuration or strip platform security mitigations (`/GS`, `/guard:cf`, `/DYNAMICBASE`, `/HIGHENTROPYVA`, `/NXCOMPAT`, `/CETCOMPAT`) to reach a speculative size target.

## Summary of Recorded Experiments

### 1. Toolchain Comparison: `MSVC/LTCG` vs `clang-cl/lld-link`
- **Record:** [`benchmarks/experiments/msvc-ltcg-comparison.json`](../benchmarks/experiments/msvc-ltcg-comparison.json)
- **Category:** `toolchain_comparison`
- **Status:** `rejected_preserved` (`substituted_silently: false`)
- **Measurements:**
  - `empty_player`: `12,288` bytes (`+1,536` bytes vs `clang-cl` `10,752` bytes)
  - `clear_present`: `19,456` bytes (`+2,560` bytes vs `clang-cl` `16,896` bytes)
  - `compute_shader`: `16,896` bytes (`+2,048` bytes vs `clang-cl` `14,848` bytes)
- **Outcome:** `MSVC` `19.40.33813` with `/O1 /Os /GL /LTCG /OPT:REF /OPT:ICF=4` succeeded functionally and produced identical inbox DLL import closures, but yielded binaries `13.8%` to `15.2%` larger than `clang-cl` `18.1.8` + `lld-link` ThinLTO. `clang-cl/lld-link` is retained as primary; `MSVC/LTCG` presets and ledgers remain preserved in `benchmarks/ledgers/*-msvc-ltcg-release.json`.

### 2. Compact Profile Experiment: Section Merging (`/MERGE:.rdata=.text`) + Sub-Page Alignment (`/ALIGN:16`)
- **Record:** [`benchmarks/experiments/compact-profile-experiment.json`](../benchmarks/experiments/compact-profile-experiment.json)
- **Category:** `compact_profile`
- **Status:** `failed_preserved` (`substituted_silently: false`)
- **Measurements:** Reduced `clear_present` on-disk size to `12,416` bytes (`-4,480` bytes vs release baseline `16,896` bytes).
- **Failure Diagnostic:** Windows 11 x64 PE loader rejected the image with `STATUS_INVALID_IMAGE_FORMAT` (`0xC000007B`, mapped to `ExitCode::OsVersionUnsupported = 11` / loader rejection) because merging `.rdata` into `.text` with `/ALIGN:16` violates section alignment and W^X page protection requirements enforced when `/guard:cf` (Control Flow Guard) and `/CETCOMPAT` (Shadow Stack) are retained.
- **Outcome:** Preserved as a failed experiment. Platform security mitigations are **not** disabled to rescue this flag combination.

### 3. CRT Elimination Experiment: Naive `/NODEFAULTLIB` with Security Mitigations Retained
- **Record:** [`benchmarks/experiments/crt-elimination-experiment.json`](../benchmarks/experiments/crt-elimination-experiment.json)
- **Category:** `crt_elimination`
- **Status:** `failed_preserved` (`substituted_silently: false`)
- **Measurements:** Reduced `empty_player` to `6,656` bytes (`-4,096` bytes vs release baseline `10,752` bytes) when prototype-linked without `ucrtbase.dll`.
- **Failure Diagnostic:** `lld-link` fails with `LLD_LINK_UNDEFINED_SECURITY_COOKIE_AND_CFG_SYMBOLS` (`undefined symbol: __security_check_cookie, __security_cookie, __guard_check_icall_fptr`) when `/NODEFAULTLIB` is enabled alongside `/GS` and `/guard:cf` without supplying custom CRT security-cookie initialization and CFG dispatch stubs.
- **Outcome:** `NW_ENABLE_CRT_ELIMINATION_EXPERIMENT` remains `OFF` by default; all six initialization and cleanup obligations are documented in [`src/player/crt_experiment_obligations.h`](../src/player/crt_experiment_obligations.h) and [`ADR-0004`](architecture/0004-dependency-closure-and-crt-experiment.md).

## Inbox-Runtime Blocker Log

- **Current Status:** No inbox-runtime blocker encountered on the stated Feature Level `12_0` + Shader Model `6.0` baseline (`d3d12.dll` inbox on Windows 11 x64 `>= 10.0.22631.4317` natively supports FL `12_0`, SM `6.0` DXIL, bounded descriptor tables, ordinary barriers, and single direct queue without `D3D12Core.dll` Agility SDK redistribution).
- **Blocker Reporting Mechanism:** Verified via `nw_empty_player --simulate-failure InboxRuntimeBlocker --diagnostics`, which returns exit code `25` (`InboxRuntimeBlocker`) and emits the structured blocker diagnostic required before any profile change.
