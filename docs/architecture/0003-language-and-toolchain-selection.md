# ADR-0003: Language Rationale (C++20) & Toolchain Selection (`clang-cl`/`lld-link` vs `MSVC`/`LTCG`)

- **Status:** Accepted
- **Task:** NW-01
- **Date:** 2026-11-06

## Context

Selecting the implementation language and Windows x64 compiler/linker toolchain directly affects binary overhead, COM/D3D12 API ergonomics, code sharing across the three Nodewerk products, debugging maturity, and build reproducibility.

## Decision

### 1. Language Selection: C++20 Across All Three Products

We standardize on **C++20** for `nodewerk-studio`, `nodewerkc`, and `nodewerk-player` (with distinct library/feature subsets between tooling and player):

- **Direct Win32/D3D12 COM Integration:** D3D12 (`d3d12.h`) and DXGI (`dxgi1_6.h`) are native C++ COM interfaces (`ID3D12Device`, `IDXGISwapChain3`, `__uuidof`). C++20 consumes them directly without FFI wrappers or vtable marshaling shims.
- **Allocation & Layout Control:** `constexpr` evaluation, `alignas`, placement new, scoped RAII helpers, and zero-overhead strong enums allow exact control over static and heap memory in the player without hidden allocations.
- **Mature Debugging Ecosystem:** Full native support in Visual Studio Debugger, WinDbg, PIX for Windows, and RenderDoc via standard CodeView PDB debug info.
- **Shared Code Across Tooling & Player:** Opcode definitions, package binary headers, capability manifests (`BaselineCapabilityProfile`), exit codes (`ExitCode`), and deterministic math/hash routines are shared directly as header/source modules between `nodewerkc` and `nodewerk-player`.
- **Why Not C:** Introducing C for the player would create an artificial language boundary between C++20 tooling and the player without any demonstrated binary size benefit once C++ exceptions (`/EHs-c-`), RTTI (`/GR-`), and heavy STL components (`<iostream>`, `<locale>`, `<regex>`) are disabled in player builds.
- **Why Not Rust or Zig:** While Rust and Zig are viable systems languages, introducing either here would require maintaining external Win32/D3D12 COM bindings, prevent seamless header/constexpr sharing with C++ tooling, and lacks measured size or workflow advantages over `clang-cl` C++20. Neither C, Rust, nor Zig may be introduced without measured justification.
- **Library Boundary:** Authoring (`nodewerk-studio`) and whole-demo compiler (`nodewerkc`) builds may freely use the C++20 standard library; standalone player builds (`nodewerk-player` and NW-01 fixtures) disable exceptions and RTTI and avoid heavy standard library dependencies.

### 2. Toolchain Comparison & Selection: `clang-cl` + `lld-link` vs `MSVC` + `LTCG`

We configure and maintain identical CMake/Ninja toolchain presets for **LLVM `clang-cl` 18.1.8 + `lld-link` 18.1.8** (ThinLTO `-flto=thin`, `/O1 /Os /Gw /Gy /GF /OPT:REF /OPT:ICF=4`) and **MSVC `cl.exe` 19.40.33813 + `link.exe` 14.40.33813.0** (`/O1 /Os /GL /Gw /Gy /GF /LTCG /OPT:REF /OPT:ICF=4`) across all three identical C++20 measurement fixtures:

| Fixture | `clang-cl` + `lld-link` Ledger | `MSVC` + `LTCG` Ledger | Identical Import Closure | Current Measurement Status |
| :--- | :--- | :--- | :---: | :--- |
| `empty_player` | `empty-player-clang-cl-release.json` | `empty-player-msvc-ltcg-release.json` | Yes (`kernel32.dll`, `ucrtbase.dll`) | `unverified` (`null` on Linux host; populated via `tools/size_ledger.py` on Windows 11 x64) |
| `clear_present` | `clear-present-clang-cl-release.json` | `clear-present-msvc-ltcg-release.json` | Yes (`kernel32.dll`, `user32.dll`, `d3d12.dll`, `dxgi.dll`, `ucrtbase.dll`) | `unverified` (`null` on Linux host; populated via `tools/size_ledger.py` on Windows 11 x64) |
| `compute_shader` | `compute-fixture-clang-cl-release.json` | `compute-fixture-msvc-ltcg-release.json` | Yes (`kernel32.dll`, `d3d12.dll`, `dxgi.dll`, `ucrtbase.dll`) | `unverified` (`null` on Linux host; populated via `tools/size_ledger.py` on Windows 11 x64) |

**Provisional Default Configuration:** `clang-cl` 18.1.8 + `lld-link` 18.1.8 (`cmake/toolchains/clang-cl-x64.cmake`) is configured as the provisional default for the initial baseline because:
1. It supports explicit source-prefix normalization (`-ffile-prefix-map=${CMAKE_SOURCE_DIR}=.`) alongside `/Brepro` and `/PDBALTPATH:%_PDB%` for deterministic bit-for-bit reproducible builds across independent clean directories.
2. It provides aggressive ThinLTO dead-stripping and identical-code folding (`/OPT:REF /OPT:ICF=4`) while preserving full CodeView PDB debugging compatibility and identical inbox Windows 11 import closure (`ucrtbase.dll`, `kernel32.dll`, `user32.dll`, `d3d12.dll`, `dxgi.dll`).
3. Both toolchains are actively version-checked at CMake configure time in [`cmake/ToolchainManifest.cmake`](../../cmake/ToolchainManifest.cmake), and `tools/size_ledger.py` records and compares the exact PE byte ledgers when executed on a Windows 11 x64 toolchain host without ever claiming unmeasured byte counts.

**The default is not a hardcoded winner.** `tools/size_ledger.py` selects the smaller aggregate distributed footprint among toolchains whose CodeView/PDB and two-clean-directory reproducibility evidence are both verified and whose three fixture byte counts are recorded. Selection is applied when measurements are recorded and is reconciled again by `generate_size_comparison_report`, so a measured MSVC/LTCG win updates `selected_toolchain`, its executable/linker metadata, and the per-fixture `status` fields instead of failing the comparison. When only one toolchain is verified, that working toolchain is selected rather than leaving the unverified provisional default in place. Ties retain the currently selected eligible toolchain. The checked-in manifest is therefore provisional while all byte counts are `null`; it does not claim that `clang-cl` is smaller before measurement.

Full-mode verification builds **both** toolchains twice in independent clean directories (`build/repro-clean-a`/`b` for clang-cl and `build/msvc-release-ltcg-clean-a`/`b` for MSVC/LTCG) and records a separate reproducibility report for each (`benchmarks/reproducibility/release-build-reproducibility.json` and `benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json`). It also compiles the `dev` profile with `/DEBUG:FULL` for each toolchain and verifies that every fixture PE contains an `RSDS` CodeView record resolving to an MSF PDB before either toolchain may be marked `verified`. A toolchain with byte measurements but missing reproducibility or CodeView/PDB evidence remains explicitly `unverified`.

The `MSVC/LTCG` configuration (`cmake/toolchains/msvc-x64.cmake`, preset `msvc-release-ltcg`) and its byte ledgers are preserved in [`benchmarks/ledgers/`](../../benchmarks/ledgers/) and [`benchmarks/experiments/msvc-ltcg-comparison.json`](../../benchmarks/experiments/msvc-ltcg-comparison.json).
