# NW-11 — Evaluate advanced RTX features through bounded, costed experiments

## Goal

Adopt modern GPU features only where they improve a representative production enough to justify their total cost.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Maintain one common RTX 40/50 raster/compute profile and separately exported feature profiles. First compare conventional indirect rendering against a mesh-shader implementation of the existing torus scene, then compare an SDF or analytic reflection fixture against a DXR ray-query variant. These are the only initial implementation experiments. Evaluate Vulkan 1.4, asynchronous compute, Work Graphs, SER, opacity micromaps, micromesh/clustered geometry, and Tensor/neural shading through a capability/dependency matrix and content-specific hypotheses; prototype further candidates only through separately scoped follow-up issues. Vulkan feature promotion does not remove the need to query required features [Vulkan extension guidance](https://docs.vulkan.org/spec/latest/chapters/extensions.html). RTX 50-specific paths must declare exact API features and toolchain/runtime requirements, never infer support from a marketing name. DLSS, RTX Kit, model weights, and other SDK components remain optional and fully counted.

## Scope

- Mesh-shader versus indexed/indirect rendering experiment.
- Ray-query versus baseline reflection experiment.
- Feature, deployment, licensing, and byte-cost matrix for other candidates.
- Accept/reject records for measured variants.

## Affected components

- Proposed capability profiles
- Proposed optional geometry and ray-query lowering
- Proposed feature benchmark fixtures

## Required behavior

- Report incremental bytes, shader count, initialization, VRAM, CPU/GPU time, and fixed-quality image results.
- Predeclare each experiment's success criterion; default performance criterion is at least 10 percent lower GPU time at matched quality while remaining within its chosen artifact budget.
- Quality experiments require an explicit measurable image/reference criterion.
- Unused advanced paths contribute no code, shaders, or SDK dependencies to baseline exports.

## State, configuration, and API changes

- Add exact queried-feature manifests for experimental exports.
- Record any required Agility SDK and shader-model versions in the complete dependency ledger.
- Represent fallback as a separately exported tested artifact.

## Recovery semantics

- Missing required capabilities produce an early diagnostic and exit.
- Feature initialization failure does not silently substitute a different effect.
- Unsuccessful experiments remain documented without becoming baseline requirements.

## Compatibility and safety constraints

- No mandatory mesh, RT, SER, Work Graphs, neural shading, or vendor SDK in the common profile.
- No second production backend merely to exercise a feature.
- Microbenchmark gains alone do not justify adoption if end-to-end frame time or distribution cost regresses.

## Test plan

- Run paired baseline/variant captures on RTX 4060 and representative RTX 50 hardware.
- Test absent-feature manifests and zero-cost stripping.
- Measure both cold initialization and steady-state execution.
- Validate synchronization and resource lifetimes for each variant.

## Regressions

- The common artifact's bytes and dependencies must remain unchanged.
- RTX 50 improvements must not silently alter the RTX 40 artifact.
- Any accepted feature must retain a reproducible comparison fixture.

## Acceptance criteria

- Publish measured accept/reject decisions for mesh shaders and ray queries.
- Publish a bounded follow-up matrix for all remaining named technologies.
- Accepted variants have explicit capabilities, budgets, and regression tests.
- Rejected experiments add no mandatory runtime dependency.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 supplies the baseline renderer and benchmark protocol.
- NW-09 is required only for a future temporal/stateful experiment.
- Independent of NW-10's 64 KiB result.

Boundaries:
- No omnibus implementation of every RTX feature.
- No claim that Vulkan, D3D12, FP16, mesh shaders, or neural techniques are universally smaller or faster.

<!-- pairallel-sections:v1 {"sections":[{"end":122,"slug":"goal","start":9},{"end":417,"slug":"source-context","start":143},{"end":1468,"slug":"final-technical-design","start":446},{"end":1714,"slug":"scope","start":1480},{"end":1860,"slug":"affected-components","start":1740},{"end":2356,"slug":"required-behavior","start":1884},{"end":2621,"slug":"state-configuration-api-changes","start":2400},{"end":2882,"slug":"recovery-semantics","start":2646},{"end":3191,"slug":"compatibility-and-safety-constraints","start":2925},{"end":3481,"slug":"test-plan","start":3207},{"end":3705,"slug":"regressions","start":3499},{"end":4021,"slug":"acceptance-criteria","start":3731},{"end":4423,"slug":"decomposition-dependencies-and-boundaries","start":4069}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":10,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:a979c215d0d9c9ea9d8aba495e8ef5686d843cc8c10e13c52b4d6ff35dd9b441","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
