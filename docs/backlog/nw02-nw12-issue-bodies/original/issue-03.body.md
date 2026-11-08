## Goal

Define the authoring semantics independently of graphics API objects.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Represent projects as immutable snapshots of typed, multi-output DAGs. Schemas declare stable operator and port identifiers, semantic versions, canonical parameters, explicit seeds, frequency, residency, resource descriptors, and lowering hooks. Initially support scalar/vector/matrix values, curves, shader expressions, texture/buffer/mesh recipes, material descriptions, flat scene tables, camera/light values, and render images. Distinguish pure ShaderExpr composition from resource-producing GpuPass operations and CPU control expressions. Frequencies are compile-time, initialization, and frame; history semantics are reserved for NW-09 and rejected in initial exports. Residency conversions and readbacks are explicit. Hash canonical field encodings, versions, upstream hashes, target profile, and compiler settings using a wide content digest; never hash struct padding or raw parameter memory. Semantic floating-point parameters must be finite, with declared normalization rules. Node IDs and editor layout must not influence randomness or exported bytes unless explicitly supplied as semantic seed inputs. Provide CPU reference evaluation for control/math and practical procedural oracles, not a mandatory complete CPU renderer.

## Scope

- Versioned textual .nwgraph schema and canonical semantic encoding.
- Operator registry, validation, topological ordering, and dependency diagnostics.
- CPU memoization, downstream invalidation, and deterministic seed derivation.
- Explicit separation of editor layout from semantic content.

## Affected components

- Proposed graph/schema library
- Proposed operator registry
- Proposed CPU evaluator and cache

## Required behavior

- Reject ordinary cycles, unknown schema versions, incompatible ports, implicit readbacks, and illegal frequency transitions.
- Evaluate identical semantic inputs reproducibly regardless of worker scheduling or serialization order.
- Changing one input invalidates only its dependent closure.
- Extensions register authoring/compiler implementations; the player has no plugin ABI.

## State, configuration, and API changes

- Introduce GraphSnapshot, OperatorSchema, LogicalValue, EvaluationContext, and Diagnostic contracts.
- Record explicit quantization policies separately from full-precision authoring values.
- Define versioned migrations for supported authoring documents.

## Recovery semantics

- Invalid graph edits leave the last valid snapshot available.
- Missing or corrupt cache entries are treated as misses.
- Unknown operator versions require an explicit migration rather than silent reinterpretation.

## Compatibility and safety constraints

- GPU floating-point results have numerical/perceptual tolerances; explicit seeds alone do not guarantee bitwise images.
- No D3D12 resource pointers appear in graph serialization or logical types.
- Shader expressions describe supported operations rather than arbitrary code embedded in release data.

## Test plan

- Test valid and invalid type/frequency connections, multi-output nodes, cycles, and bounded resource dimensions.
- Test canonical serialization, schema migration, stable seed derivation, and cache invalidation.
- Verify node reordering and editor-layout changes leave semantic hashes unchanged.
- Test dependency diagnostics against source node and port identifiers.

## Regressions

- Operator semantic changes require version changes.
- Editor-only edits must not alter exported semantic identity.
- Canonical hashes must not vary with allocator layout or worker completion order.

## Acceptance criteria

- A textual fixture graph validates and evaluates without a GPU.
- Invalid fixtures fail with precise dependency diagnostics.
- Repeated evaluations and canonical serialization are stable across clean runs.
- All initial operator kinds declare frequency, residency, and seed behavior.

## Decomposition dependencies and boundaries

Dependencies:
- NW-01 supplies language, build, and target-profile contracts.

Boundaries:
- No GUI or runtime operator interpreter.
- No executable feedback, mutable scene hierarchy, or general scripting language.

<!-- pairallel-sections:v1 {"sections":[{"end":78,"slug":"goal","start":9},{"end":373,"slug":"source-context","start":99},{"end":1639,"slug":"final-technical-design","start":402},{"end":1943,"slug":"scope","start":1651},{"end":2064,"slug":"affected-components","start":1969},{"end":2468,"slug":"required-behavior","start":2088},{"end":2767,"slug":"state-configuration-api-changes","start":2512},{"end":3007,"slug":"recovery-semantics","start":2792},{"end":3351,"slug":"compatibility-and-safety-constraints","start":3050},{"end":3734,"slug":"test-plan","start":3367},{"end":3950,"slug":"regressions","start":3752},{"end":4260,"slug":"acceptance-criteria","start":3976},{"end":4522,"slug":"decomposition-dependencies-and-boundaries","start":4308}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":1,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:e3da09b716364e8f494b3d02ba184eaea292c90dde73332399ec37218b9ce7c8","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
