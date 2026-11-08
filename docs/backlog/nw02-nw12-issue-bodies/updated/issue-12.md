# NW-11 — Evaluate advanced RTX features through bounded, costed experiments

## Goal

Adopt modern GPU features only where they improve a representative production enough to justify their total cost.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Maintain one common capability profile qualified on representative desktop Ada and Blackwell hardware and separately exported optional feature profiles. A common-profile export excludes unused optional implementations, shaders, and dependencies. First compare conventional indirect rendering against a mesh-shader implementation of the existing torus scene, then compare an SDF or analytic reflection fixture against a DXR ray-query variant. These are the only initial implementation experiments. Evaluate Vulkan 1.4, asynchronous compute, Work Graphs, SER, opacity micromaps, micromesh/clustered geometry, and Tensor/neural shading through a capability/dependency matrix and content-specific hypotheses; prototype further candidates only through separately scoped follow-up issues. Vulkan feature promotion does not remove the need to query required features [Vulkan extension guidance](https://docs.vulkan.org/spec/latest/chapters/extensions.html). Optional Blackwell-specific paths must declare exact API features and toolchain/runtime requirements, never infer support from a marketing name. Each optional profile declares its exact additional D3D12 capabilities, shader targets, runtime/toolchain requirements, dependencies, and artifact budget. Its fallback is a separately exported and tested common-profile artifact; unsupported optional artifacts exit diagnostically without effect substitution or baseline elevation. DLSS, RTX Kit, model weights, and other SDK components remain optional and fully counted.

## Scope

- Mesh-shader versus indexed/indirect rendering experiment.
- Ray-query versus baseline reflection experiment.
- Capability-qualified per-device baseline/variant pairs and separately tested common fallback artifacts.
- Feature, deployment, licensing, and byte-cost matrix for other candidates.
- Accept/reject records for measured variants.

## Affected components

- Proposed optional capability profiles with exact requirements and artifact budgets
- Proposed optional geometry and ray-query lowering
- Proposed feature benchmark fixtures and common fallback artifacts

## Required behavior

- Report incremental bytes, shader count, initialization, VRAM, CPU/GPU time, and fixed-quality image results.
- Predeclare each experiment's success criterion; default performance criterion is at least 10 percent lower GPU time at matched quality while remaining within its chosen artifact budget, evaluated separately for each capability-qualified matched device and environment.
- Quality experiments require an explicit measurable image/reference criterion.
- Common-artifact correctness runs on representative desktop Ada and Blackwell hardware; optional profiles run matched baseline/variant pairs only on declared targets whose queried capabilities satisfy the profile.
- Unsupported feature coverage is identified as unsupported; unavailable required coverage remains pending; no optional feature is presumed available on every device.
- Unused advanced paths contribute no code, shaders, or SDK dependencies to common or fallback exports.

## State, configuration, and API changes

- Add exact queried-feature manifests for experimental exports.
- Define optional-profile ID and requirements digest, artifact budgets, dependencies, fallback artifact identity, and evidence status (pass/fail/pending/unsupported).
- Record any required Agility SDK and shader-model versions in the complete dependency ledger.
- Represent fallback as a separately exported tested artifact.

## Recovery semantics

- Missing required capabilities produce an early diagnostic and exit.
- Unsupported optional artifacts exit diagnostically without substituting a different effect or elevating the common baseline.
- Feature initialization failure does not silently substitute a different effect.
- Unsuccessful experiments remain documented without becoming baseline requirements.

## Compatibility and safety constraints

- No mandatory mesh, RT, SER, Work Graphs, neural shading, or vendor SDK in the common profile.
- No second production backend merely to exercise a feature.
- Microbenchmark gains alone do not justify adoption if end-to-end frame time or distribution cost regresses.
- No optional feature is presumed available on every device in either generation; common exports and fallback artifacts remain mandatory and separately tested.

## Test plan

- Run the common artifact on representative desktop Ada and Blackwell hardware. For each optional profile, run matched baseline/variant pairs on declared targets whose queried capabilities satisfy the profile; evaluate the improvement criterion separately per matched device/environment.
- Verify unsupported-feature diagnostics, pending unavailable required coverage, and separately exported and tested common fallback artifacts.
- Test absent-feature manifests and zero-cost stripping.
- Measure both cold initialization and steady-state execution.
- Validate synchronization and resource lifetimes for each variant.

## Regressions

- The common artifact's bytes and dependencies must remain unchanged.
- Optional-profile improvements must not silently alter the common or fallback artifacts, and unsupported or pending outcomes must not be reported as passes.
- Any accepted feature must retain a reproducible comparison fixture.

## Acceptance criteria

- Publish measured accept/reject decisions for mesh shaders and ray queries.
- Publish a bounded follow-up matrix for all remaining named technologies.
- Accepted variants have explicit capabilities, budgets, and regression tests.
- Rejected experiments add no mandatory runtime dependency.
- Representative common-artifact coverage exists for both desktop Ada and Blackwell; every optional-profile claim is capability-qualified and evaluated per matched device/environment.
- Unsupported features are distinguished from pending required coverage, and every optional path retains a separately tested common fallback artifact.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 supplies the baseline renderer, benchmark protocol, and `nw07_perf_ref_v1`.
- NW-09 is required only for a future temporal/stateful experiment.
- Independent of NW-10's 64 KiB result.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No omnibus implementation of every RTX feature.
- No claim that Vulkan, D3D12, FP16, mesh shaders, or neural techniques are universally smaller or faster.
- No cross-SKU performance equivalence or optional-feature baseline elevation.

<!-- pairallel-sections:v1 {"sections":[{"end":122,"slug":"goal","start":9},{"end":417,"slug":"source-context","start":143},{"end":1962,"slug":"final-technical-design","start":446},{"end":2314,"slug":"scope","start":1974},{"end":2544,"slug":"affected-components","start":2340},{"end":3515,"slug":"required-behavior","start":2568},{"end":3947,"slug":"state-configuration-api-changes","start":3559},{"end":4335,"slug":"recovery-semantics","start":3972},{"end":4804,"slug":"compatibility-and-safety-constraints","start":4378},{"end":5438,"slug":"test-plan","start":4820},{"end":5753,"slug":"regressions","start":5456},{"end":6404,"slug":"acceptance-criteria","start":5779},{"end":7268,"slug":"decomposition-dependencies-and-boundaries","start":6452}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":10,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:a979c215d0d9c9ea9d8aba495e8ef5686d843cc8c10e13c52b4d6ff35dd9b441","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
