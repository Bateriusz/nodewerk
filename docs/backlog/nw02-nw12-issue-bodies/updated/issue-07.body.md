## Goal

Exercise persistent generated resources, scene composition, and deterministic animation in the same export architecture.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Add seeded 256x256 Noise2D material generation, a 64x32 parametric torus mesh generator, procedural placement of 256 instances, flat instance/material tables, a camera curve, and analytic per-frame twist deformation. Generate textures and vertex/index buffers on the GPU during initialization; use conventional indexed instanced rasterization with one indirect draw, analytic lighting, half-resolution bloom, and tonemapping. No CPU content readback occurs during playback. Use 48,000 integer ticks per second, checked conversion from a monotonic clock, and rational offline frame steps. Curves sample absolute ticks rather than accumulated floating-point deltas. Events use ordered IDs and intervals (previousTick, currentTick]; replay/reset semantics are explicit. The first scene is stateless, making direct seek equivalent to evaluation at the requested tick. Reserve a clock-provider interface for later audio without linking an audio library. Each fixed-tick capture records the identical common artifact hash, capability-profile ID/digest, exact observed adapter/device, driver/OS/toolchain, queried capabilities, clocks/power/thermal, cache state, and capture-tool protocol. The same common artifact is used for representative desktop Ada and Blackwell correctness coverage; missing coverage is pending, never a pass. Existing numeric/image tolerances and feature exclusions are unchanged.

## Scope

- Small procedural operator library covering texture, geometry, material, instance scene, camera, animation, and post-processing.
- Integer timeline and bounded quantized curve encoding.
- Conventional graphics and indirect-draw path.
- Representative desktop Ada/Blackwell fixed-tick correctness captures with artifact and environment provenance.
- CPU/high-precision oracles where practical.

## Affected components

- Proposed procedural operators and shader library
- Proposed scene/material lowering
- Proposed timeline and raster/post passes

## Required behavior

- Static resources regenerate from compact parameters and explicit seeds.
- Only frame-dependent parameters and deformation execute each frame.
- Scene assembly produces dense tables without runtime hierarchy traversal.
- Curve quantization records maximum error and allows a full-precision escape hatch.
- Use the identical common artifact for representative Ada and Blackwell fixed-tick correctness coverage; record exact device, capability/profile, environment, and artifact provenance.

## State, configuration, and API changes

- Register the initial resource, scene, material, curve, and post operator schemas.
- Define integer tick, rational capture step, clock provider, and event cursor contracts.
- Define geometry and instance encodings from explicit fields with verified layout assertions.
- Capture metadata carries coverage target, evidence role, capability-profile identity/digest, artifact hashes, exact adapter/device, driver/OS/toolchain, queried capabilities, clocks/power/thermal, cache state, and unavailable telemetry.

## Recovery semantics

- Invalid dimensions, indices, curve ranges, and resource budgets fail before dispatch.
- Seek resets event cursors according to documented replay policy.
- Generation or pipeline failures preserve the previous authoring preview and fail standalone initialization.
- Missing representative hardware or telemetry yields pending or unverified coverage; it is never substituted or reported as passing.

## Compatibility and safety constraints

- No mandatory mesh shaders, ray tracing, bindless heap indexing, FP16 arithmetic, or wave-size assumptions.
- GPU float comparisons use measured tolerances; integer seed/control outputs may use exact hashes.
- Only final formats with verified device support are emitted.
- Representative hardware is not an exact-SKU requirement; existing numeric/image tolerances, feature exclusions, and quality/resolution requirements remain unchanged.

## Test plan

- Compare generated noise and mesh positions/normals against practical reference calculations.
- Validate mesh topology, index bounds, instance transforms, and resource estimates.
- Test direct seeking, rational capture, event boundaries, duration overflow, and curve error.
- Capture fixed-tick frames on representative desktop RTX 40-series/Ada and RTX 50-series/Blackwell hardware using the identical common artifact; record exact device/capability/profile/environment/artifact provenance, retain tolerances, and mark missing coverage pending.

## Regressions

- Changing display frame rate must not change stateless timeline results.
- Static generators must not move into the frame schedule accidentally.
- Adding operators must not enlarge exports that do not use them.
- Capture artifact identity, capability-profile identity, and representative coverage metadata changes must not alter numeric/image tolerances or feature exclusions.

## Acceptance criteria

- The specified torus-instance graph exports and renders entirely through NW-05.
- Procedural textures and meshes are absent as baked assets from the baseline package.
- Fixed-tick captures on representative desktop Ada and Blackwell hardware meet the existing declared numeric and image tolerances using the identical common artifact; missing coverage remains pending.
- Per-pass time, initialization time, shader bytes, and memory costs are recorded.

## Decomposition dependencies and boundaries

Dependencies:
- NW-05 supplies the complete export/runtime path.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No SDF-to-mesh extraction, particles, history, path tracing, audio implementation, or asset importers.
- Do not expand the initial operator catalog beyond the proof needs.
- No exact-SKU capture requirement or tolerance change is introduced.

<!-- pairallel-sections:v1 {"sections":[{"end":129,"slug":"goal","start":9},{"end":424,"slug":"source-context","start":150},{"end":1850,"slug":"final-technical-design","start":453},{"end":2255,"slug":"scope","start":1862},{"end":2409,"slug":"affected-components","start":2281},{"end":2922,"slug":"required-behavior","start":2433},{"end":3473,"slug":"state-configuration-api-changes","start":2966},{"end":3896,"slug":"recovery-semantics","start":3498},{"end":4378,"slug":"compatibility-and-safety-constraints","start":3939},{"end":4940,"slug":"test-plan","start":4394},{"end":5335,"slug":"regressions","start":4958},{"end":5813,"slug":"acceptance-criteria","start":5361},{"end":6544,"slug":"decomposition-dependencies-and-boundaries","start":5861}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":5,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:2d007ed5ac0d362c59854705af51336bb50497cc818addc71f6c002457325475","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
