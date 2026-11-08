# NW-03 — Build offline shader lowering and a bounded release-package format

## Goal

Establish the compiler-to-player contract before expanding rendering features.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Lower ShaderExpr DAGs into a small typed expression IR and generate specialized HLSL entry points. Compile offline using pinned DXC; retain source maps and reflection as development sidecars, while release blobs retain all metadata needed for valid DXIL execution. Deduplicate identical complete shader blobs and root signatures; do not assume functions can be shared across independent blobs without linking. Use a small fixed binding convention per pass family and explicit resource bindings, avoiding runtime reflection. Define .nwp v1 with a little-endian versioned header, checked section directory, capability requirements, resource descriptors, initialization/frame instructions, parameter/timeline data, shader blobs, and integrity checksum. Instructions describe supported GPU/CPU execution primitives rather than authoring nodes. Start with raw payloads; compression requires a later measured net saving including decoder code.

## Scope

- Expression fusion and HLSL generation for a small math/noise fixture.
- Offline DXC compilation, validation, stripping, and shader cache.
- Release package writer, reader, and inspection tool.
- Development source maps and shader diagnostics.

## Affected components

- Proposed shader compiler
- Proposed package format and parser
- Proposed compiler diagnostics and sidecars

## Required behavior

- Only reachable shader entry points and their dependencies are emitted.
- Package parsing validates counts, ranges, checked arithmetic, instruction references, and decoded-size limits before GPU submission.
- Release packages contain no editor names, source paths, reflection tables, or source text.
- Identical inputs and pinned tools yield identical packages.

## State, configuration, and API changes

- Define package version 1 and internal pass/binding contracts.
- Introduce nodewerkc validate, compile-package, and inspect commands.
- Record compiler/profile fingerprints in build reports without unnecessary release strings.

## Recovery semantics

- Failed compilation publishes no partial package.
- Unknown major versions, required sections, instructions, or shader targets are rejected.
- Compilation diagnostics identify the originating expression/node.
- Temporary output is atomically replaced only after validation succeeds.

## Compatibility and safety constraints

- Early package versions may be incompatible; archival reproduction retains source graphs and pinned tools.
- Checksums detect corruption, not authenticity.
- A valid package can contain expensive or faulty shaders; parsing validation is not a sandbox for hostile GPU programs.
- No runtime DXC or Slang dependency.

## Test plan

- Compile and validate every fixture specialization.
- Test expression fusion against CPU mathematical oracles.
- Fuzz malformed package sections, instruction operands, dimensions, and arithmetic.
- Compare package hashes across clean build directories.

## Regressions

- Track shader bytes and specialization counts per fixture.
- Reject debug/reflection leakage into release payloads.
- No parser failure may reach command submission.

## Acceptance criteria

- A generated fused compute shader is packaged reproducibly.
- Malformed-input tests and sanitizer-backed parser fuzzing complete without memory errors.
- Release inspection demonstrates absence of authoring metadata.
- Shader errors map back to graph expressions.

## Decomposition dependencies and boundaries

Dependencies:
- NW-01 provides DXC and capability targets.
- NW-02 provides graph types, expression semantics, and diagnostics.

Boundaries:
- No runtime source compilation, universal shader, or custom compression codec.
- Slang remains a later toolchain experiment, not a second mandatory shader path.

<!-- pairallel-sections:v1 {"sections":[{"end":87,"slug":"goal","start":9},{"end":382,"slug":"source-context","start":108},{"end":1348,"slug":"final-technical-design","start":411},{"end":1604,"slug":"scope","start":1360},{"end":1738,"slug":"affected-components","start":1630},{"end":2124,"slug":"required-behavior","start":1762},{"end":2395,"slug":"state-configuration-api-changes","start":2168},{"end":2703,"slug":"recovery-semantics","start":2420},{"end":3061,"slug":"compatibility-and-safety-constraints","start":2746},{"end":3330,"slug":"test-plan","start":3077},{"end":3514,"slug":"regressions","start":3348},{"end":3804,"slug":"acceptance-criteria","start":3540},{"end":4154,"slug":"decomposition-dependencies-and-boundaries","start":3852}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":2,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:4fd73fd37b1bc7a222ae5aa48d7f6d9ed05e5b9ca9ff769dfff0b7e9b4cf3561","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
