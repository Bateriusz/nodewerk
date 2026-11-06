# ADR-0004: Build Profiles, Dependency Closures & CRT-Elimination Experiment Obligations

- **Status:** Accepted
- **Task:** NW-01
- **Date:** 2026-11-06

## Context

Demoscene players require strict control over binary footprint and runtime dependencies, while day-to-day engine development requires full Direct3D 12 debug layer validation, symbol information, and structured diagnostics. Mixing development and release dependency closures—or assuming `/NODEFAULTLIB` CRT elimination without satisfying Windows x64 ABI and security obligations—introduces fragile builds or silent runtime crashes.

## Decision

### 1. Three Build Profiles (`cmake/BuildProfiles.cmake` & `CMakePresets.json`)

Nodewerk defines three explicit build profiles across both `clang-cl`/`lld-link` (selected primary) and `MSVC`/`LTCG` (comparison):

| Profile | CMake Value | Purpose | Exceptions / RTTI | Security Mitigations | D3D12 Validation | Dependency Closure |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Development** | `dev` | Local debugging, D3D12 debug layer validation, failure recovery testing | Disabled (`/EHs-c-`, `/GR-`) | Retained (`/GS`, `/guard:cf`, ASLR, NX, CET) | Enabled (`NW_ENABLE_D3D12_VALIDATION=1`) | `dev` (`ucrtbased.dll`, `d3d12sdklayers.dll`, `dxgidebug.dll`, PDBs) |
| **Release Baseline** | `release` | Reproducible standalone player baseline & clean-system validation | Disabled (`/EHs-c-`, `/GR-`) | Retained (`/GS`, `/guard:cf`, ASLR, NX, CET) | Disabled (`NW_ENABLE_D3D12_VALIDATION=0`) | `release` (inbox Windows 11 DLLs only; zero sidecars) |
| **Experimental Compact** | `compact_experiment` | Isolated sandbox for aggressive LTO/ICF and `/NODEFAULTLIB` CRT experiments | Disabled (`/EHs-c-`, `/GR-`) | Retained (`/GS`, `/guard:cf`, ASLR, NX, CET) | Disabled (`NW_ENABLE_D3D12_VALIDATION=0`) | `compact_experiment` (inbox Windows 11 DLLs only; zero sidecars) |

### 2. Separate Development and Release Dependency Closures

- **Release Closure (`release`):** May import **only** documented Windows 11 inbox OS DLLs (`kernel32.dll`, `user32.dll`, `gdi32.dll`, `d3d12.dll`, `dxgi.dll`, `ucrtbase.dll`). Installed display-driver user-mode DLLs (`nvwgf2umx.dll`, `nvldumdx.dll`) are loaded dynamically by `d3d12.dll`. Every other DLL (`dxcompiler.dll`, `dxil.dll`, `d3dcompiler_47.dll`, `D3D12Core.dll`, `vcruntime140.dll`, `msvcp140.dll`, `nvapi64.dll`), payload, model, or sidecar is either forbidden or counted byte-for-byte in `total_distributed_bytes`.
- **Development Closure (`dev`):** Permits debug CRT imports (`ucrtbased.dll`, `vcruntime140d.dll`), optional D3D12/DXGI validation layers (`d3d12sdklayers.dll`, `dxgidebug.dll`, `winpixeventruntime.dll`), and `.pdb` symbol sidecars.
- **Automated Enforcement:** `cmake/DependencyCheck.cmake` invokes `tools/dependency_check.py` as a `POST_BUILD` step on every fixture executable and fails the build if any unexpected DLL import or sidecar appears, or if compiler selection silently alters deployment requirements.

### 3. CRT Elimination (`/NODEFAULTLIB`) as an Explicit Experiment with Obligations

CRT elimination is **never** a baseline assumption. In `release`, executables link dynamically against the Windows 11 inbox Universal CRT (`ucrtbase.dll`), which is a documented OS component present on every Windows 11 installation.

Enabling `/NODEFAULTLIB` requires opting into `-DNW_BUILD_PROFILE=compact_experiment -DNW_ENABLE_CRT_ELIMINATION_EXPERIMENT=ON` and satisfying all six obligations codified in [`src/player/crt_experiment_obligations.h`](../../src/player/crt_experiment_obligations.h):

1. **16-Byte Stack Alignment & Entry Point:** Custom `mainCRTStartup` / `WinMainCRTStartup` must align `RSP` to a 16-byte boundary before executing any SSE2/AVX2 instructions.
2. **Security Cookie & CFG Initialization:** Retaining `/GS` and `/guard:cf` under `/NODEFAULTLIB` requires providing `__security_cookie`, `__security_init_cookie()`, `__security_check_cookie()`, and `__guard_check_icall_fptr`.
3. **Floating-Point ABI Symbol:** Must define `extern "C" int _fltused = 0x9875;` to satisfy the Windows x64 floating-point ABI.
4. **Freestanding Memory Intrinsics:** Must provide freestanding `memcpy`, `memset`, and `memcmp` compiled with `-fno-builtin` / `/Oi-` so LTO does not synthesize recursive calls.
5. **Static Initializers & TLS:** Must prohibit dynamic C++ global constructors (`.CRT$XCU`) and `thread_local` variables, or explicitly walk `.CRT$XCA`..`.CRT$XCZ` on startup and teardown.
6. **Deterministic GPU & OS Cleanup:** Must wait on the D3D12 direct queue fence, release all COM interfaces, destroy the Win32 `HWND`, and terminate explicitly via `ExitProcess(static_cast<UINT>(exit_code))`.
