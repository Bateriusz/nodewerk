# ADR-0002: Platform Target & Direct3D 12 Capability Baseline

- **Status:** Accepted
- **Task:** NW-01
- **Date:** 2026-11-06

## Context

Shipping a standalone player without redistributing compiler or runtime DLLs requires targeting a well-defined OS and GPU baseline whose inbox Direct3D 12 runtime supports all required pipeline and shader capabilities out of the box.

## Decision

### 1. Target Operating System & Hardware Tiers

- **Target OS:** Windows 11 x64 (`x86_64-pc-windows-msvc`). The pinned minimum *target* build is `10.0.22631.4317` (Windows 11 23H2), with `10.0.26100.2033` (Windows 11 24H2) as the current capture target. Both builds are recorded as `target_pinned_unverified` until the Full/CleanSystem evidence gates execute on physical hardware; this ADR does not claim they have been tested yet.
- **Non-Commitments:** Nodewerk makes **no commitment** to Windows 10, Linux, macOS, or legacy pre-Feature-Level-12_0 GPUs. Only one Windows x64 D3D12 backend is implemented initially.
- **Target Hardware Matrix:**
  1. **Desktop NVIDIA GeForce RTX 4060** (Ada Lovelace `AD107`, Device ID `0x2882`, pinned driver baseline `566.03` / WDDM `32.0.15.6603`).
  2. **Representative Desktop NVIDIA RTX 50-Series** (`NVIDIA GeForce RTX 5080`, Blackwell `GB203`, Device ID `0x2C02`, pinned driver baseline `572.16` / WDDM `32.0.15.7216`).
- **Missing Coverage Policy:** Any missing or unexecuted physical hardware coverage is recorded explicitly as `"verification_status": "unverified"` with `"benchmark_passed": false` and `null` runtime metrics (accompanied by `unavailable_measurements` explanations)—never as a passing benchmark or zeroed measurement. Toolchain selection likewise remains provisional until both identical-fixture builds and the physical captures contain matching artifact hashes and toolchain provenance.

### 2. Direct3D 12 Baseline Capability Profile

Defined formally in [`benchmarks/capability-manifest.v1.json`](../../benchmarks/capability-manifest.v1.json) and enforced in [`src/player/capability_check.h`](../../src/player/capability_check.h):

- **Feature Level:** `D3D_FEATURE_LEVEL_12_0`
- **Shader Model:** `6.0` (`D3D_SHADER_MODEL_6_0`, offline DXC profiles `cs_6_0`, `vs_6_0`, `ps_6_0`)
- **Descriptor Binding:** Bounded descriptor tables (`CBV_SRV_UAV` and `SAMPLER` descriptor heaps with explicit bounded root descriptor tables)
- **Synchronization:** Ordinary resource barriers (`D3D12_RESOURCE_BARRIER_TYPE_TRANSITION`, `D3D12_RESOURCE_BARRIER_TYPE_UAV`, `D3D12_RESOURCE_BARRIER_TYPE_ALIASING`) and direct queue fence synchronization
- **Command Queue Topology:** One direct command queue (`D3D12_COMMAND_LIST_TYPE_DIRECT`) for raster, compute, and copy commands
- **Conditional Feature Queries:** Additional optional features (such as `WaveOps`, `Int64ShaderOps`, `Native16BitShaderOpsSupported`, or `TypedUAVLoadAdditionalFormats`) are **required only when emitted content in the generated player link manifest declares that it uses them**. Benchmark capture may query them for measurement; the report records whether each optional feature was queried separately from whether it was supported.
- **Capability Measurement:** Feature-level probing includes `12_2`/`12_1`/`12_0`/`11_1`/`11_0`; shader-model probing walks the highest available model rather than assuming 6.0. Baseline-support booleans, queried maxima, and unqueried optional features are kept distinct in the benchmark record.
- **Runtime Module Evidence:** CleanSystem emits full module paths. `D3D12Core.dll` under the Windows system directory is classified as an inbox component; only a copy loaded from outside the system directory is an Agility SDK redistributable. Basename-only evidence is treated as unclassified rather than as a redistributable.
- **Benchmark Provenance:** A benchmark is certifiable only when the executing fixture's reported adapter matches the requested desktop tier and every executed artifact hash matches a recorded size-ledger entry whose toolchain identifies the build. Missing environment fields, mismatched tiers, and unmatched artifact hashes remain explicitly unverified.

### 3. Explicit Baseline Exclusions

The following features and dependencies are **excluded** from the NW-01 baseline:

- **No Shader Model 6.6 Direct Heap Indexing:** Shaders must not require `ResourceDescriptorHeap` or `SamplerDescriptorHeap`.
- **No Enhanced Barriers:** The player does not require `D3D12_FEATURE_D3D12_OPTIONS12::EnhancedBarriersSupported`.
- **No Mesh Shaders:** The player does not require `D3D12_MESH_SHADER_TIER`.
- **No Ray Tracing (DXR):** The player does not require `D3D12_RAYTRACING_TIER`.
- **No Runtime Shader Compilation:** Neither `dxcompiler.dll`, `dxil.dll`, nor `d3dcompiler_47.dll` may be loaded or redistributed in release builds. All shaders are compiled offline at build time using pinned DXC (`1.8.2407`).
- **No Mandatory D3D12 Agility SDK Redistribution:** Release builds run directly against the Windows 11 inbox `d3d12.dll` without shipping `D3D12Core.dll`.
- **No Proprietary Vendor SDKs:** NVAPI, AMD AGS, DLSS, and XeSS are excluded.
- **No Executable Packers in Baseline:** Crinkler, kkrunchy, UPX, and custom PE packers are excluded from the NW-01 baseline so raw compiler/linker section sizes and platform security mitigations remain transparent.
- **Platform Security Mitigations Retained:** `/GS`, `/guard:cf` (Control Flow Guard), `/DYNAMICBASE`, `/HIGHENTROPYVA` (64-bit ASLR), `/NXCOMPAT` (DEP), and `/CETCOMPAT` (Intel/AMD CET Shadow Stack) remain enabled across all profiles.
