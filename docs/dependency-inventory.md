# Dependency Inventory & Distribution Byte Accounting Policy

- **Machine-Readable Inventory:** [`benchmarks/dependency-inventory.v1.json`](../benchmarks/dependency-inventory.v1.json)
- **Validation Schema:** [`schemas/dependency-inventory.schema.json`](../schemas/dependency-inventory.schema.json)
- **Automated Gate:** [`tools/dependency_check.py`](../tools/dependency_check.py) (invoked via [`cmake/DependencyCheck.cmake`](../cmake/DependencyCheck.cmake))

## 1. Distribution Accounting Rule

To prevent hidden deployment overhead or uncounted redistributables from distorting size budgets:

1. **Excluded Components (Counted as `0` Distributed Bytes):**
   - **Documented Windows 11 Inbox OS DLLs** (inbox components for the intended Windows 11 x64 runtime floor; verified by the recorded CleanSystem capture on Windows `10.0.26200.9711`):
     - `kernel32.dll` — Win32 process, synchronization, timing, and module management
     - `user32.dll` — Win32 window registration and message loop for swapchain presentation
     - `gdi32.dll` — Win32 display subsystem support
     - `d3d12.dll` — Inbox Direct3D 12 runtime loader (Feature Level `12_0` / Shader Model `6.0` baseline)
     - `dxgi.dll` — Inbox DXGI 1.6 adapter enumeration and flip-model swapchain runtime
     - `ucrtbase.dll` — Inbox Universal C Runtime (shipped as part of the Windows OS since Windows 10/11)
   - `api-ms-win-crt-*.dll` — Inbox UCRT API-set forwarders, canonicalized to `ucrtbase.dll` for closure comparison
   - **Installed Display-Driver Components** (installed by the user's graphics driver package and loaded dynamically by `d3d12.dll`):
     - `nvwgf2umx.dll` — NVIDIA D3D12 user-mode driver
     - `nvldumdx.dll` — NVIDIA WDDM user-mode driver loader

2. **Counted Components (100% Included in `total_distributed_bytes`):**
   - The standalone executable itself (`nw_empty_player.exe`, `nw_clear_present_fixture.exe`, `nw_compute_shader_fixture.exe`, or `nodewerk-player.exe`).
   - **Every other required DLL, redistributable, shader payload, demo package, model, or sidecar file**, including nested deployment-tree paths and delay-load dependencies.

The automated gate walks the complete deployment tree recursively; it does not limit accounting
to selected filename suffixes or immediate children. Payloads such as `demo.nwpkg`, shader/model
files, and nested sidecars are counted or rejected. PE delay-load import descriptors are parsed
and validated with the same release-closure rules as ordinary imports.

## 2. Forbidden Release Imports & Sidecars

Release (`release` and `compact_experiment`) builds fail automated dependency verification if the PE import table or output directory contains any of the following:

| Component | Category | Reason Forbidden in Release Baseline |
| :--- | :--- | :--- |
| `dxcompiler.dll`, `dxil.dll`, `d3dcompiler_47.dll` | Runtime Shader Compiler | All HLSL shaders must be compiled offline via pinned DXC (`1.8.2407`) at build time. |
| `D3D12Core.dll` | D3D12 Agility SDK Redistributable | Baseline uses Feature Level `12_0` + Shader Model `6.0` supported by Windows 11 inbox `d3d12.dll`. |
| `d3d12SDKLayers.dll`, `dxgidebug.dll`, `WinPixEventRuntime.dll` | Development Validation / PIX | Permitted only in the `dev` dependency closure; forbidden in `release`. |
| `vcruntime140.dll`, `vcruntime140_1.dll`, `msvcp140.dll` | Non-Inbox MSVC C++ Runtime | Player disables exceptions (`/EHs-c-`), RTTI (`/GR-`), and heavy STL and selects the fully static CRT (`MultiThreaded`/`/MT`), which supplies complete startup and security-cookie support without any CRT DLL import. |
| `ucrtbased.dll`, `vcruntime140d.dll` | Debug CRT | Permitted only in the `dev` dependency closure. |
| `nvapi64.dll`, `amd_ags_x64.dll` | Proprietary Vendor SDKs | Vendor-specific extensions are excluded from the baseline. |

## 3. Protection Against Silent Compiler Closure Changes

Both `clang-cl` (`clang-cl-release`) and `MSVC/LTCG` (`msvc-release-ltcg`) are verified by `tools/dependency_check.py` against `benchmarks/dependency-inventory.v1.json` and per-fixture `expected_release_imports` in `fixtures/*/fixture.json`. Neither compiler is permitted to introduce an extra runtime DLL (`vcruntime140.dll`, `libomp.dll`, etc.) without failing the build immediately.

Fixture executables are built into isolated `staging/<fixture_id>/` directories, so each
post-build check inventories only that fixture's payload tree. A separate
`build/<preset>/deploy/` tree contains the flat clean-system executable set and is scanned
recursively; nested duplicate executables or payloads in that tree are counted and rejected.
