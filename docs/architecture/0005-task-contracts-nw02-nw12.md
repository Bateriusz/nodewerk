# ADR-0005: Downstream Task Contracts (NW-02 through NW-12)

- **Status:** Accepted
- **Task:** NW-01
- **Date:** 2026-11-06

## Context

NW-01 has no upstream task dependencies and serves as the architectural, schema, toolchain, and capability foundation for tasks **NW-02 through NW-12**. Establishing explicit boundaries and contracts now prevents downstream tasks from introducing hidden runtime dependencies, violating the D3D12 Feature Level `12_0` / Shader Model `6.0` baseline, or drifting from the size-ledger and benchmark schemas.

## Downstream Contracts by Task

| Task ID | Subsystem Scope | Contract Established by NW-01 |
| :--- | :--- | :--- |
| **NW-02** | Versioned Typed Operator Graph & Schema | Consumes the three-product boundary (`ADR-0001`). Graph serialization remains strictly authoring/compiler-side (`nodewerkc` input); no JSON/graph parser is linked into `nodewerk-player`. |
| **NW-03** | Expression Lowering & Constant/Timeline Optimizer | Operates on backend-neutral Expression IR in `nodewerkc`. May use the C++20 standard library in compiler builds; emitted player evaluation tables must remain exception/RTTI-free. |
| **NW-04** | Pass Scheduler, Resource Lifetime & Barrier Planner | Schedules passes against the NW-01 D3D12 baseline (`benchmarks/capability-manifest.v1.json`): 1 direct command queue (`D3D12_COMMAND_LIST_TYPE_DIRECT`), ordinary resource transition/UAV/aliasing barriers (no enhanced barriers), and bounded descriptor tables (`max_cbv_srv_uav_descriptors_per_frame = 4096`). |
| **NW-05** | Offline HLSL Codegen & DXC Compilation Pipeline | Must compile all generated shaders offline via pinned DXC (`1.8.2407`) targeting Shader Model `6.0` (`cs_6_0`, `vs_6_0`, `ps_6_0`) with `-Qstrip_reflect -Qstrip_debug`. Must not emit SM 6.6 `ResourceDescriptorHeap` indexing, mesh shaders, or ray tracing. Runtime DXC redistribution remains forbidden. |
| **NW-06** | Procedural Texture & Compute Operator Kernels | Must bind resources via bounded descriptor tables (`CBV_SRV_UAV` / `SAMPLER`) and root constants within `max_root_signature_dword_cost = 16`. Every new operator kernel must publish a delta against `benchmarks/ledgers/compute-fixture-clang-cl-release.json` using `schemas/size-ledger.schema.json`. |
| **NW-07** | Procedural Mesh & Raster Pipeline Operators | Must target Feature Level `12_0` rasterization using required formats (`DXGI_FORMAT_R8G8B8A8_UNORM`, `DXGI_FORMAT_R32G32B32A32_FLOAT`, `DXGI_FORMAT_R32_UINT`, `DXGI_FORMAT_D32_FLOAT`). Any optional feature usage must be declared in `EmittedContentFeatureRequest` (`src/player/capability_check.h`). |
| **NW-08** | Audio Synthesis & Demo Timeline Synchronization | Must obey the `release` dependency closure (`benchmarks/dependency-inventory.v1.json`): only documented Windows 11 inbox OS DLLs are excluded from `total_distributed_bytes`. Any non-inbox audio DLL or payload is counted. |
| **NW-09** | Authoring Application (`nodewerk-studio`) | Strictly separated from `nodewerk-player` (`ADR-0001`). May use the C++20 standard library and `dev` profile validation sidecars (`d3d12sdklayers.dll`, PIX), and exports versioned typed graphs for `nodewerkc`. |
| **NW-10** | Compact Package Format, Generated Player Link Manifest & Compression | Consumes `schemas/size-ledger.schema.json` (including `decoder_overhead.decoder_overhead_bytes`) to measure specialized player dead-stripping and package compression. **NW-10 is the first task authorized to evaluate whether a 64 KiB complete demo budget is achievable based on measured evidence.** |
| **NW-11** | End-to-End Demo Assembly & Determinism Verification | Reuses the two-clean-directory reproducibility workflow (`schemas/reproducibility-report.schema.json`) and distinct exit-code diagnostics (`src/player/exit_codes.h`). |
| **NW-12** | Final Hardware Qualification & Release Sign-Off | Executes against both pinned GPU baselines (`benchmarks/hardware/rtx-4060-baseline.json` and `benchmarks/hardware/rtx-50-series-baseline.json`) and clean physical Windows 11 validation (`schemas/clean-system-report.schema.json`) via `tools/verify-nw01.ps1`. |

## Enforced Scope Boundaries in NW-01

- **No Editor:** `nodewerk-studio` is not implemented in NW-01.
- **No Procedural Scene:** Only minimal `empty_player`, `clear_present`, and `compute_shader` measurement fixtures are implemented.
- **No Second Backend or General RHI:** Only the Windows 11 x64 D3D12 bootstrap is implemented, with zero virtual RHI abstraction layer.
- **No Unmeasured 64 KiB Claim:** All NW-01 ledgers set `"claims_64kib_achievable": false` and `"defer_minimum_size_claim_until_task": "NW-10"`.
