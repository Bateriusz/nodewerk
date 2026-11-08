# ADR-0005: Downstream Task Contracts (NW-02 through NW-12)

- **Status:** Accepted; NW-01A contract and task-mapping correction applied 2026-11-08
- **Task:** NW-01 (original contracts), NW-01A (capability-policy realignment)
- **Date:** 2026-11-06 (original), 2026-11-08 (NW-01A correction)

## Context

NW-01 has no upstream task dependencies and serves as the architectural, schema, toolchain, and capability foundation for tasks **NW-02 through NW-12**. Establishing explicit boundaries and contracts now prevents downstream tasks from introducing hidden runtime dependencies, violating the D3D12 Feature Level `12_0` / Shader Model `6.0` baseline, or drifting from the size-ledger and benchmark schemas.

NW-01A establishes the durable GPU support, correctness, optional-feature, and performance-evidence policy in [`ADR-0006`](0006-capability-based-gpu-support-policy.md). The task and subsystem mapping below is corrected against the actual GitHub issue identities. The former table mislabeled NW-03 through NW-12; hardware contracts must not be applied to a misidentified task.

## Execution Order and Prerequisite Gate

`NW-01 -> NW-01A -> NW-01B -> NW-02..NW-12`

- NW-01A unblocks **NW-01B only**.
- Both NW-01A and NW-01B must complete before implementation of NW-02 through NW-12 begins.
- Existing dependencies among NW-02 through NW-12 are preserved.
- NW-01's frozen TaskSpec, historical evidence, and separately scoped operator-authorized workaround are unchanged.

## Downstream Contracts by Task

| Task ID | Issue | Actual Subsystem Scope | Contract Established by NW-01 and Corrected by NW-01A |
| :--- | :--- | :--- | :--- |
| **NW-02** | #3 | Versioned operator schemas, typed DAGs, and deterministic evaluation | Consumes the three-product boundary (`ADR-0001`). Graph serialization remains strictly authoring/compiler-side (`nodewerkc` input); no JSON/graph parser is linked into `nodewerk-player`. Target-profile identity and semantic hashing use the stable capability-profile ID and canonical requirements digest, never GPU generation/model metadata. Compiler acceptance remains GPU-independent. |
| **NW-03** | #4 | Offline shader lowering and bounded release-package format | Must compile all generated shaders offline via pinned DXC (`1.8.2407`) targeting Shader Model `6.0` (`cs_6_0`, `vs_6_0`, `ps_6_0`) with `-Qstrip_reflect -Qstrip_debug`. The `.nwp v1` capability record must express exact emitted features, formats, limits, shader/runtime requirements, and profile identity/digest. Unknown or malformed requirements fail through existing validation boundaries; no package-format redesign. No SM 6.6 `ResourceDescriptorHeap` indexing, mesh shaders, or ray tracing. Runtime DXC redistribution remains forbidden. |
| **NW-04** | #5 | Minimal package-driven D3D12 player | Validates exact startup capabilities and package/profile requirements via `ID3D12Device::CheckFeatureSupport` and the existing capability contract before bulk allocation; diagnostics identify unmet requirements. Adapter names and marketing generations are metadata only. Qualifies the identical common package on representative desktop Ada and Blackwell hardware; missing coverage remains pending. One direct queue, ordinary barriers, bounded descriptor tables. |
| **NW-05** | #6 | Whole-graph specialized players and explicit resource schedules | Export target profiles are selected by capability-profile ID/digest. The generated player link manifest contains only reachable feature queries; unused optional implementations, shaders, and dependencies are excluded from common exports. Optional profiles are separate artifacts and may not raise common capability, dependency, or size baselines. Existing resource-schedule and aliasing validation is qualified on representative desktop Ada and Blackwell hardware. |
| **NW-06** | #7 | Procedural mesh, texture, scene, and integer-timeline operators | Uses the existing bounded descriptor tables, root-constant limit (`max_root_signature_dword_cost = 16`), and size-ledger schema. Fixed-tick correctness captures use the identical common artifact on representative desktop Ada and Blackwell hardware, record exact artifact/capability/environment provenance, and preserve existing numeric/image tolerances and feature exclusions. Missing coverage remains pending. |
| **NW-07** | #8 | Deliver and benchmark the 256 KiB architectural proof | Correctness coverage uses the identical common artifact on representative desktop Ada and Blackwell hardware. Performance certification is separate and versioned as `nw07_perf_ref_v1`: `1920x1080` p99 GPU time and p99 CPU submission time each strictly `< 16.67 ms` on the designated desktop RTX 4060 reference, five-second warm-up plus three uncapped 60-second runs, each run passing, with raw samples and percentile method retained. Existing size, startup, memory, image-quality, and resolution requirements are preserved. Non-reference, stale, or mismatched measurements cannot certify v1. Missing reference hardware remains pending. |
| **NW-08** | #9 | Minimal authoring editor with incremental GPU caching | Strictly separated from `nodewerk-player` (`ADR-0001`). May use the C++20 standard library and `dev` profile validation sidecars (`d3d12sdklayers.dll`, PIX), and exports versioned typed graphs for `nodewerkc`. Device generation means a D3D12 device-instance recreation epoch, not an RTX marketing generation. Compiler targets are keyed by capability-profile identity; device-resident realizations retain actual adapter/device identity. Existing cache ownership and recovery design is preserved. |
| **NW-09** | #10 | Explicit temporal state and replayable simulation | Checkpoint compatibility is governed by checkpoint version, tick, semantic/state-layout hash, and capability-profile identity/digest. Device-resident state remains tied to its device lifetime/epoch. Existing cross-device tolerance tests run on representative desktop Ada and Blackwell hardware without claiming portable GPU-state replay. |
| **NW-10** | #11 | Compact exports and true 64 KiB feasibility | Consumes `schemas/size-ledger.schema.json` (including `decoder_overhead.decoder_overhead_bytes`) to measure specialized player dead-stripping and package compression. Inherits `nw07_perf_ref_v1`; faster substitute devices cannot certify preservation; before/after measurements use matched hardware, environment, and protocol. **NW-10 is the first task authorized to evaluate whether a 64 KiB complete demo budget is achievable based on measured evidence.** |
| **NW-11** | #12 | Advanced RTX features through bounded, costed experiments | Maintains one common capability profile qualified on representative desktop Ada and Blackwell hardware plus separately exported optional profiles. Each optional profile declares exact additional capabilities, shader/runtime/toolchain requirements, dependencies, and artifact budget; fallback is a separately exported and tested common artifact. Matched baseline/variant pairs run only on capability-qualified declared targets, with the existing `>= 10%` improvement criterion evaluated separately per matched device/environment. Unsupported features are distinguished from pending required coverage. |
| **NW-12** | #13 | Optional procedural audio through the authoritative clock boundary | Hardware-neutral synthesis, clock, WASAPI/PCM, offline-rendering, and audio acceptance contract. Consumes the `release` dependency closure (`benchmarks/dependency-inventory.v1.json`): only documented Windows 11 inbox OS DLLs are excluded from `total_distributed_bytes`; any non-inbox audio DLL or payload is counted. Introduces no GPU generation, exact-SKU, capability-profile, or performance-reference requirement. The former repository mapping that described NW-12 as hardware qualification is corrected here. |

## Enforced Scope Boundaries in NW-01

- **No Editor:** `nodewerk-studio` is not implemented in NW-01; the minimal editor is NW-08 / issue #9.
- **No Procedural Scene:** Only minimal `empty_player`, `clear_present`, and `compute_shader` measurement fixtures are implemented.
- **No Second Backend or General RHI:** Only the Windows 11 x64 D3D12 bootstrap is implemented, with zero virtual RHI abstraction layer.
- **No Unmeasured 64 KiB Claim:** All NW-01 ledgers set `"claims_64kib_achievable": false` and `"defer_minimum_size_claim_until_task": "NW-10"`.
- **No Exact-SKU Runtime Requirement:** Desktop Ada/Blackwell are qualification coverage generations. Exact models appear only as justified performance references or observed evidence metadata.

## Correction Record

- The original NW-01 ADR-0005 table mapped NW-03 through NW-12 to different subsystem labels than the actual GitHub issues. The table above is the corrected mapping.
- Original issue bodies and per-issue publication records are retained under [`docs/backlog/`](../backlog/README.md).
- Section-level audit: [`ADR-0007`](0007-nw02-nw12-section-audit.md).
