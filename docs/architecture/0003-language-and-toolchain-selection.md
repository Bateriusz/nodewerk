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

### 2. Toolchain Comparison & Selection: `clang-cl` + `lld-link` Retained Over `MSVC` + `LTCG`

We evaluated **LLVM `clang-cl` 18.1.8 + `lld-link` 18.1.8** (with ThinLTO `-flto=thin`, `/O1 /Os /Gw /Gy /GF /OPT:REF /OPT:ICF=4`) against **MSVC `cl.exe` 19.40.33813 + `link.exe` 14.40.33813.0** (with `/O1 /Os /GL /Gw /Gy /GF /LTCG /OPT:REF /OPT:ICF=4`) across identical C++20 source fixtures:

| Fixture | `clang-cl` + `lld-link` (Bytes) | `MSVC` + `LTCG` (Bytes) | Delta (`MSVC` vs `clang-cl`) | Identical Import Closure |
| :--- | ---: | ---: | ---: | :---: |
| `empty_player` | `10,752` | `12,288` | `+1,536` (`+14.3%`) | Yes (`kernel32.dll`, `ucrtbase.dll`) |
| `clear_present` | `16,896` | `19,456` | `+2,560` (`+15.2%`) | Yes (`kernel32.dll`, `user32.dll`, `d3d12.dll`, `dxgi.dll`, `ucrtbase.dll`) |
| `compute_shader` | `14,848` | `16,896` | `+2,048` (`+13.8%`) | Yes (`kernel32.dll`, `d3d12.dll`, `dxgi.dll`, `ucrtbase.dll`) |

**Retained Primary Configuration:** `clang-cl` 18.1.8 + `lld-link` 18.1.8 (`cmake/toolchains/clang-cl-x64.cmake`) is retained as the primary baseline toolchain because:
1. It produces the smaller working executable across all three measurement fixtures (`1,536` to `2,560` bytes smaller per fixture due to tighter `.text` constant folding and `.rdata` COM GUID/unwind table folding).
2. It emits deterministic, bit-for-bit reproducible binaries across independent clean directories via `/Brepro`, `-ffile-prefix-map=${CMAKE_SOURCE_DIR}=.`, and `/PDBALTPATH:%_PDB%`.
3. It preserves full CodeView PDB debugging compatibility and identical inbox Windows 11 import closure (`ucrtbase.dll`, `kernel32.dll`, `user32.dll`, `d3d12.dll`, `dxgi.dll`).

The `MSVC/LTCG` configuration (`cmake/toolchains/msvc-x64.cmake`, preset `msvc-release-ltcg`) and its byte ledgers are preserved in [`benchmarks/ledgers/`](../../benchmarks/ledgers/) and [`benchmarks/experiments/msvc-ltcg-comparison.json`](../../benchmarks/experiments/msvc-ltcg-comparison.json) for ongoing regression tracking.
