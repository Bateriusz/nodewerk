# ADR-0006: Capability-Based GPU Support, Correctness, Optional-Feature, and Performance-Evidence Policy

- **Status:** Accepted
- **Task:** NW-01A (refined issue #18; planning source issue #15)
- **Date:** 2026-11-08
- **Planning base:** `743a4c1aec9e016af8c76cf08f9dd1696017270c`
- **Frozen NW-01A source requirements:** `sha256:v1:4b7a4e828d9ce4835d3f002bdd2960d7af00d99aefdc418317bc8592f753116d`
- **Verified final NW-01 baseline:** `benchmarks/capability-manifest.v1.json`, `benchmarks/hardware/rtx-4060-baseline.json`, `benchmarks/hardware/rtx-50-series-baseline.json`, `src/player/capability_check.h`

## Context

The NW-01 backlog conflated five different questions:

1. Which hardware generations does Nodewerk intend to support?
2. Which exact capabilities make a device eligible to run a particular package?
3. Which representative hardware is sufficient to qualify correctness?
4. Which exact hardware, environment, and protocol may certify a hard performance gate?
5. Which measurements and environment metadata were actually observed?

Those questions must not be collapsed into an exact GPU model. An RTX 4060 Ti, another Ada device, and an RTX 5070 Ti are not interchangeable performance references, yet any of them may be representative correctness hardware when it satisfies the common capability profile. Conversely, a marketing generation name, coverage-tier label, or PCI device whitelist cannot prove that a required D3D12 feature is present.

This ADR establishes the durable policy. It is an architecture, documentation, and backlog contract; it introduces no renderer feature, package-format redesign, executable API, GPU benchmark, or normalization research.

## Decision

### 1. Separated concerns

Nodewerk keeps these concerns independent in every downstream task:

| Concern | Question answered | Authoritative representation |
| :--- | :--- | :--- |
| **Declared support scope** | Which device classes are in qualification scope? | ADR text and evidence coverage targets |
| **Exact capability requirements** | May this package run on this adapter? | Queried D3D12 capabilities plus emitted package requirements and capability-profile identity |
| **Correctness coverage** | Has the identical common artifact been proven correct on representative hardware? | Evidence records with coverage target, evidence role, observed hardware, artifact provenance, and result status |
| **Optional artifacts** | Which exact additional capabilities, dependencies, and budgets does an optional feature require? | Named optional profile with exact requirements, separate export, and separately tested common fallback |
| **Performance references** | Which exact machine and protocol may certify a hard timing gate? | Versioned performance contract, e.g. `nw07_perf_ref_v1` |
| **Measured environment metadata / benchmark provenance** | What was actually observed, and which required telemetry is unavailable? | Evidence environment fields, capability query results, artifact hashes, raw samples, and explicit `unavailable_telemetry` |

Measured environment metadata is orthogonal to runtime eligibility and is always recorded rather than inferred.

### 2. Declared support scope (qualification scope, never a runtime predicate)

- **Compatibility floor:** desktop NVIDIA GeForce RTX 40-series / Ada Lovelace.
- **Current-generation development and performance target:** desktop NVIDIA GeForce RTX 50-series / Blackwell.
- These statements define the hardware generations for which Nodewerk intends to produce and review representative correctness evidence. They are **not runtime feature predicates**, do not create exhaustive SKU coverage, and do not imply equal performance.
- Nodewerk makes **no support commitment** to mobile/laptop GPUs, workstation GPUs, data-center GPUs, pre-Ada generations, post-Blackwell or future devices, Windows 10, Linux, or macOS. A device outside the declared scope may still execute if it independently satisfies the exact package requirements, but that fact creates no support or qualification claim.
- A representative compliant desktop Ada device and a representative compliant desktop Blackwell device are sufficient to qualify generation correctness when both exercise the identical common artifact. When a representative device is a diagnostic sibling of a different exact SKU, the sibling relationship and exact identities are recorded; the sibling does not become an exact-SKU prerequisite.
- RTX 4060 Ti, RTX 5070 Ti, device IDs, and board names in existing evidence are **observed metadata**. They do not create model-name compatibility requirements.

### 3. Common capability profile

The common profile is the canonical requirement vector that every common artifact targets and every common export validates.

- **Stable profile ID:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Canonical requirements digest:** `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`
- **Machine-readable record:** [`benchmarks/capability-profile.common.v1.json`](../../benchmarks/capability-profile.common.v1.json)
- **Existing projection:** `nw::player::BaselineCapabilityProfile` in [`src/player/capability_check.h`](../../src/player/capability_check.h)
- **Existing requirement source:** [`benchmarks/capability-manifest.v1.json`](../../benchmarks/capability-manifest.v1.json)

The digest is computed over the profile's `requirements` object only. Support-scope prose, representative evidence, observed adapters, and historical records are excluded so that adding a measurement cannot silently change the semantic profile identity. Canonicalization is UTF-8 JSON with lexicographically sorted object keys at every level, declaration-order arrays, no insignificant whitespace, separators `,` and `:`, no trailing newline, and the `sha256:v1:` prefix.

The final NW-01 contracts were cross-checked against the planning base before this ADR was written. The following reported requirements are verified:

| Category | Exact common requirement |
| :--- | :--- |
| **Queried capabilities** | D3D12; `D3D_FEATURE_LEVEL_12_0` (`0xc000`); `D3D_SHADER_MODEL_6_0` (`0x60`); resource binding tier `>= 2`; resource heap tier `>= 2`; required format support for `DXGI_FORMAT_R8G8B8A8_UNORM`, `DXGI_FORMAT_R32G32B32A32_FLOAT`, `DXGI_FORMAT_R32_UINT`, and `DXGI_FORMAT_D32_FLOAT` with the manifest's declared usages. |
| **Shader targets** | `cs_6_0`, `vs_6_0`, `ps_6_0`; offline compilation only; no SM 6.6 `ResourceDescriptorHeap`/`SamplerDescriptorHeap` indexing; DXIL container validation retained. |
| **Pinned build tools** | DXC `1.8.2407` with `-Qstrip_reflect -Qstrip_debug`; clang-cl/lld-link and MSVC/LTCG profiles; reproducible-build contract retained. |
| **Runtime requirements** | Windows 11 x64; minimum tested build `10.0.26200.9711`; intended lower runtime target `10.0.22631.4317` (unverified); inbox `d3d12.dll`; no mandatory Agility SDK redistribution; no runtime DXC/Slang; no proprietary vendor SDK; no executable packer. |
| **Architectural constraints** | Bounded descriptor tables; ordinary `TRANSITION`/`UAV`/`ALIASING` barriers (no enhanced barriers); exactly one direct queue (zero compute/copy queues); `max_cbv_srv_uav_descriptors_per_frame = 4096`; `max_sampler_descriptors_per_frame = 64`; `max_root_signature_dword_cost = 16`; `max_compute_threads_per_group = 1024`; `max_texture_2d_dimension = 8192`; `swap_chain_buffer_count = 2`; no mesh shaders, ray tracing, or runtime shader compilation. Existing platform security mitigations remain required. |

Optional features such as `WaveOps`, `Int64ShaderOps`, `Native16BitShaderOpsSupported`, and `TypedUAVLoadAdditionalFormats` are **not** part of the common requirement vector. They are queried only when emitted content declares them.

### 4. Exact capability eligibility

- Runtime and export eligibility is derived only from applicable D3D12 queries and the exact requirements emitted by the package, player link manifest, and capability-profile identity.
- Adapter description strings, marketing generation names (`RTX 40`, `RTX 50`, `Ada`, `Blackwell`), coverage-tier labels, benchmark tier identifiers, and PCI device IDs are **never** feature predicates. They may be recorded as observed metadata.
- A missing or unknown required capability produces the existing capability-failure category and identifies the unmet requirement before bulk resource allocation. An adapter name, tier label, or previous measurement cannot repair missing evidence.
- Historical legacy tier labels remain readable but grant no new qualification claim. In particular, `rtx_4060_desktop` remains a backward-readable label for the recorded RTX 4060 Ti evidence; it is not proof that the measured device was an RTX 4060.

### 5. Optional profiles and fallback artifacts

An optional profile is a separately versioned contract that declares:

- a stable optional-profile ID and canonical requirements digest;
- exact additional queried D3D12 capabilities and minimum shader model;
- exact shader targets, DXC version, and any Agility SDK/runtime/toolchain requirement;
- all additional dependencies and their complete distributed-byte and startup costs;
- an explicit artifact budget and the coverage targets on which the profile is claimed;
- the identity of the separately exported and tested common fallback artifact.

Rules:

- Optional features must not add mandatory code, shaders, or dependencies to common exports or common fallback exports.
- Unused optional implementations are excluded from the common artifact by reachability, not merely disabled at runtime.
- An optional artifact that cannot satisfy its contract exits diagnostically. It does not silently substitute another effect or elevate the common baseline.
- Fallback is an explicitly selected, separately exported, and separately tested common artifact, identified in evidence. There is no automatic runtime quality downgrade.
- No optional feature is presumed available on every device in either declared generation.

A minimal optional-profile record has this shape (field names are illustrative; NW-01B owns the schema):

```json
{
  "profile_id": "optional.mesh_shader.ada_blackwell.v1",
  "profile_version": "1.0.0",
  "requirements_digest": "sha256:v1:<canonical-digest>",
  "additional_queried_capabilities": {
    "mesh_shader_tier_min": 1,
    "shader_model_min": "D3D_SHADER_MODEL_6_5"
  },
  "shader_targets": ["ms_6_5", "as_6_5", "cs_6_5"],
  "pinned_build_tools": {"dxc_version": "1.8.2407"},
  "runtime_requirements": {"agility_sdk_redistribution": false},
  "dependencies": [],
  "artifact_budget": {"max_distributed_bytes": 0, "max_peak_gpu_bytes": 0},
  "coverage_targets": ["ada_desktop", "blackwell_desktop"],
  "fallback_artifact": "common:nw.common.d3d12.fl12_0.sm6_0.v1"
}
```

The `artifact_budget` values must be measured for the candidate profile; the zeroes above are placeholders, not claims. A profile with unavailable or unsupported declared coverage remains `pending` or `unsupported`; it never silently becomes part of the common artifact.

#### Review examples for NW-11

| Observation | Correct result |
| :--- | :--- |
| Identical common artifact passes fixed-tick correctness on representative desktop Ada and Blackwell | Common correctness coverage `pass` for both generations |
| Optional profile declared for Ada and Blackwell; matched baseline/variant pair on a capability-qualified Ada device improves GPU time by at least 10 percent at matched quality and stays within budget | That device/environment pair `pass`; improvement is evaluated independently, not aggregated across SKUs |
| Same profile and pair on a capability-qualified Blackwell device | Independent Blackwell pair result; does not inherit or certify the Ada result |
| Declared optional coverage on a device whose queried capabilities do not satisfy the profile | `unsupported`; diagnostic exit; no effect substitution |
| Required coverage on a declared target was not executed | `pending`; not a pass |
| Optional artifact cannot satisfy its contract | Diagnostic exit; the separately exported and tested common fallback is identified and used only by explicit selection |

### 6. Representative correctness coverage

- Correctness coverage requires the **identical common artifact** to pass fixed-tick and package-driven validation on one representative compliant desktop Ada device and one representative compliant desktop Blackwell device.
- Any representative device that satisfies the common profile may qualify its generation for correctness. Exact model, board, device ID, driver, OS, capability query results, artifact hashes, clocks/power/thermal conditions, cache state, and unavailable telemetry are recorded.
- A diagnostic sibling relationship is recorded when representative coverage uses a different exact SKU from a historical or intended reference. The sibling is evidence provenance, not an exact-SKU requirement.
- Missing coverage is `pending` or `unverified`, never `pass`.
- Correctness coverage does not establish performance equivalence, exhaustive SKU coverage, or any support commitment beyond the declared scope.

### 7. Performance reference contract `nw07_perf_ref_v1`

NW-07's existing hard budgets are preserved with their original operators and applicability:

- complete distribution size `<= 262,144` bytes, with `131,072` bytes as a stretch measurement only;
- process-start-to-first-present `<= 2.0` s;
- peak engine-committed GPU resources `<= 512 MiB`;
- at `1920x1080`, p99 GPU frame time `< 16.67` ms **and** p99 CPU submission time `< 16.67` ms, each strictly less than 16.67 ms;
- five-second warm-up followed by three uncapped 60-second runs; **every** run must independently satisfy both strict p99 gates;
- raw per-run samples and the percentile methodology retained;
- clean-start measurements without an application pipeline cache, with cold driver-cache and warm results distinguished;
- existing image-quality and resolution requirements preserved; no silent quality or resolution reduction.

The reference is a **versioned performance contract named `nw07_perf_ref_v1`**, not the meaning of Ada support:

- **Reference GPU:** desktop NVIDIA GeForce RTX 4060. The exact board, VBIOS, device ID, VRAM, clock/power limits, and cooling configuration must be designated before certification.
- **Reference CPU/host:** exact CPU model, motherboard/chipset, RAM, storage class, OS build, and host environment must be designated before certification.
- **Reference environment:** exact Windows build/UBR, NVIDIA driver, WDDM version, power plan, NVIDIA power-management mode, PCIe power/link settings, ambient/thermal conditions, display mode, and measurement tools.
- **Reference protocol:** `1920x1080`, declared quality settings, uncapped presentation, five-second warm-up, three uncapped 60-second runs, per-run evaluation, retained raw samples, named percentile method, and declared cache conditions.
- **Reference artifact:** exact artifact hashes, capability-profile ID/digest, shader hashes, toolchain identity, and package identity.

Certification rules:

- Only a matching RTX 4060 reference measurement satisfying the designated host, environment, artifact, and protocol may evaluate `nw07_perf_ref_v1`.
- RTX 4060 Ti, any other Ada device, any Blackwell device, a stale artifact, a mismatched host, or a mismatched protocol may remain available as informational or correctness evidence but **cannot certify v1**. Faster hardware does not substitute.
- The historical NW-01 RTX 4060 Ti capture is **not** a qualifying NW-07 RTX 4060 result.
- Missing reference hardware leaves the performance gate **pending**. It does not block NW-01A completion and does not create a pass.
- A separate Blackwell matched-environment development/performance series is maintained, but **no new absolute Blackwell performance gate** is introduced.
- A replacement reference or any normalized method requires a new contract version with independently justified thresholds, documented validation uncertainty, and bridge measurements where feasible. It never retroactively passes v1.
- The proposed `13.5` ms and `12.00` ms surrogate thresholds, theoretical-throughput equivalence, and unvalidated normalization formulas are rejected.

#### Review examples for `nw07_perf_ref_v1`

| Observation | Correct result |
| :--- | :--- |
| Exact designated RTX 4060 board, CPU/host, environment, artifact, and protocol; five-second warm-up; all three uncapped 60-second runs independently have p99 GPU `< 16.67` ms and p99 CPU submission `< 16.67` ms; raw samples and percentile method retained | `pass` for `nw07_perf_ref_v1` |
| Same reference configuration, but one of the three runs exceeds either strict 16.67 ms gate | `fail` for the contract; the failing run and samples are retained |
| Same- or higher-tier Ada device other than the designated RTX 4060 reference (including RTX 4060 Ti) | `informational_regression` or correctness evidence; cannot certify v1; v1 remains `pending` if the reference is absent |
| RTX 5070 Ti or any Blackwell device | Correctness or informational performance series; cannot certify v1; no new absolute Blackwell gate |
| Exact RTX 4060 board but a stale artifact, mismatched host, mismatched driver/OS/power/cache state, or mismatched protocol | `unverified` or `informational`; cannot certify v1 |
| Historical NW-01 RTX 4060 Ti capture with legacy `rtx_4060_desktop` label | `legacy_unclassified`; retained and readable; not an RTX 4060 result |
| No qualifying reference hardware available | `pending`; NW-01A completion is not blocked |

### 8. Measured environment and evidence contract

Every new or corrected evidence record separates the following backward-readable fields:

| Field group | Required content |
| :--- | :--- |
| `coverage_target` | `common`, `ada_desktop`, `blackwell_desktop`, or `optional:<profile-id>` |
| `evidence_role` | `correctness_qualification`, `performance_certification`, `informational_regression`, `pending`, or `unsupported` |
| `observed_hardware` | exact GPU model/architecture, vendor ID, device ID, adapter/LUID, VRAM; exact CPU model and RAM; desktop/mobile classification |
| `capability_profile` | profile ID, canonical requirements digest, and query results for every required and emitted optional capability |
| `artifact_provenance` | artifact hashes, package hash, shader hashes, size-ledger identity, toolchain identity, build/reproducibility identity |
| `environment` | OS name/build/UBR, driver/WDDM/runtime, toolchain versions, power/thermal/clocks, cache state, display/resolution/quality |
| `performance_contract_identity` | named contract and version (for example `nw07_perf_ref_v1`), exact reference GPU/CPU/host designation, thresholds and inequality operators, resolution/quality, and matching rules |
| `protocol_identity` | metric definitions, run count/duration/warm-up, sampling and percentile method, cache conditions, measurement tools, and raw-sample references |
| `result_status` | `pass`, `fail`, `pending`, `unsupported`, or `unverified` |
| `unavailable_telemetry` | explicit list of required fields that could not be measured, with reason |

Rules:

- Missing required telemetry is recorded explicitly as unavailable; it is never zero-filled or inferred from a different device.
- A non-reference, stale, mismatched-host, or mismatched-protocol measurement remains available but cannot certify the named performance gate.
- An executed failure remains `fail`. Unavailable required evidence remains `pending`. Optional features outside a profile's declared coverage remain `unsupported`. Aggregate reporting cannot convert any of these into `pass`.
- Reference or threshold changes create a new contract version and preserve earlier results and unresolved statuses.

### 9. Historical NW-01 records and backward-readable migration

- NW-01's frozen TaskSpec, historical evidence, and operator-authorized verification workaround remain unchanged.
- Existing records remain readable with their original interpretation:
  - `benchmarks/hardware/rtx-4060-baseline.json` records an NVIDIA GeForce RTX 4060 Ti (Ada Lovelace `AD106`, device ID `0x2803`), not an RTX 4060.
  - `benchmarks/hardware/rtx-50-series-baseline.json` records an NVIDIA GeForce RTX 5070 Ti (Blackwell, device ID `0x2C05`).
  - The legacy tier label `rtx_4060_desktop` remains readable for compatibility but never becomes proof that its recorded device was an RTX 4060 or that it may certify `nw07_perf_ref_v1`.
- New evidence may add `coverage_target`, `evidence_role`, `capability_profile`, and `result_status`. Absence of those fields means `legacy_unclassified`; such a record remains readable but cannot certify a new gate.
- Observed `gpu_model`, `gpu_device_id`, and `gpu_architecture` remain authoritative over any legacy tier label.
- No historical record is rewritten, and no historical result acquires a new qualification claim.

Backward-readable migration examples:

| Historical record | Reading |
| :--- | :--- |
| `hardware_tier: "rtx_4060_desktop"`, `gpu_model: "NVIDIA GeForce RTX 4060 Ti"`, `gpu_device_id: "0x2803"` | Legitimate historical Ada correctness/measurement evidence for the recorded RTX 4060 Ti; it is **not** an RTX 4060 v1 performance result |
| `hardware_tier: "rtx_50_series_desktop"`, `gpu_model: "NVIDIA GeForce RTX 5070 Ti"`, `gpu_device_id: "0x2C05"` | Legitimate representative Blackwell correctness/measurement evidence; not the `nw07_perf_ref_v1` reference |
| Record with no `coverage_target`, `evidence_role`, or `result_status` | `legacy_unclassified`; readable, cannot certify a new gate |
| Record with `result_status: "fail"` | Executed failure; remains `fail` |
| Record with `result_status: "pending"` or missing required telemetry | Pending/unverified; not a pass |

### 10. Recovery and failure semantics

- Missing or unknown required capabilities produce the existing capability-failure category with the unmet requirement before bulk resource allocation.
- An unsupported optional artifact exits diagnostically; a separately exported fallback must be explicitly selected and identified.
- Non-reference, stale, mismatched-host, or mismatched-protocol measurements remain available but cannot certify the named performance gate.
- A failed benchmark remains a failure; unavailable hardware remains pending.
- Reference or threshold changes create a new contract version and preserve earlier results and unresolved statuses.
- Issue updates retain original bodies and per-issue completion records so interrupted updates can be reconciled without overwriting concurrent edits. Completion requires readback verification of every intended update, including recomputed section offsets.

### 11. Execution order

`NW-01 -> NW-01A -> NW-01B -> NW-02..NW-12`

NW-01A unblocks NW-01B only. Implementation of NW-02 through NW-12 remains blocked until NW-01B also completes. Existing dependencies among NW-02 through NW-12 are preserved.

### 12. Downstream dispositions

The complete section-by-section audit is in [`ADR-0007`](0007-nw02-nw12-section-audit.md). Every task NW-02 through NW-12 has a disposition for Final technical design, Scope, Required behavior, State/configuration/API changes, Recovery semantics, Compatibility and safety constraints, Test plan, Regressions, Acceptance criteria, and Dependencies/boundaries.

| Task | Issue | Disposition |
| :--- | :--- | :--- |
| NW-02 | #3 | Capability-profile identity and canonical digest drive target identity and semantic hashing; compiler acceptance stays GPU-independent. |
| NW-03 | #4 | Exact emitted capability requirements and validation-boundary rejection of unknown/malformed requirements; no package redesign. |
| NW-04 | #5 | Exact startup capability validation and diagnostics; identical common package on representative Ada and Blackwell; missing coverage pending. |
| NW-05 | #6 | Export identity and reachable-content closure by capability profile; unused optional implementations excluded; representative schedule/aliasing coverage. |
| NW-06 | #7 | Representative Ada/Blackwell fixed-tick captures with artifact, capability, environment, and pending-coverage semantics; tolerances unchanged. |
| NW-07 | #8 | Correctness coverage separated from `nw07_perf_ref_v1`; full reference host/protocol contract; all hard budgets preserved. |
| NW-08 | #9 | Device generation means D3D12 recreation epoch; compiler targets keyed by capability profile; device-resident realizations retain adapter identity. |
| NW-09 | #10 | Checkpoint compatibility by semantic/state-layout and capability profile; device-lifetime binding; representative cross-device tolerance coverage without portable GPU-state replay. |
| NW-10 | #11 | Inherits `nw07_perf_ref_v1`; faster substitutes cannot certify; matched before/after environments; representative common-artifact correctness. |
| NW-11 | #12 | Representative common coverage on both generations; capability-qualified matched baseline/variant pairs per device; unsupported versus pending; tested common fallback. |
| NW-12 | #13 | Hardware-neutral audio contract retained; no GPU-policy edit; repository mapping corrected; global prerequisite note added. |

## Non-goals and boundaries

- No new GPU feature, renderer, package-format, editor, simulation, audio, or experiment redesign.
- No executable API, schema, verifier, capture-tool, runtime, or compiler implementation in NW-01A.
- No hardware acquisition, physical benchmark execution, or mandatory normalization research required to complete this policy task.
- No modification of NW-01's frozen TaskSpec or historical evidence.
- No new absolute Blackwell performance gate.
- No commitments to mobile, workstation, data-center, older, or future devices.
- No retroactive v1 performance pass through reference substitution.

## Consequences

- Downstream tasks can use representative desktop Ada and Blackwell hardware for correctness without inventing an exact-SKU compatibility requirement.
- Performance certification remains strict and reproducible under a versioned reference contract, even when reference hardware is unavailable and the gate remains pending.
- Optional features remain separately exportable and costed, with tested fallbacks and no common-profile inflation.
- Evidence remains backward-readable and distinguishes observed hardware, capability eligibility, coverage role, performance certification, and unavailable telemetry.
