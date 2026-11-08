# NW-03 — Build offline shader lowering and a bounded release-package format

## Goal

Establish the compiler-to-player contract before expanding rendering features.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Lower ShaderExpr DAGs into a small typed expression IR and generate specialized HLSL entry points. Compile offline using pinned DXC; retain source maps and reflection as development sidecars, while release blobs retain all metadata needed for valid DXIL execution. Deduplicate identical complete shader blobs and root signatures; do not assume functions can be shared across independent blobs without linking. Use a small fixed binding convention per pass family and explicit resource bindings, avoiding runtime reflection. Define .nwp v1 with a little-endian versioned header, checked section directory, capability requirements, resource descriptors, initialization/frame instructions, parameter/timeline data, shader blobs, and integrity checksum. Instructions describe supported GPU/CPU execution primitives rather than authoring nodes. Package capability requirements are exact and emitted-only: capability-profile ID and canonical requirements digest, D3D12 feature level, shader model, resource binding/heap tiers, required DXGI formats and limits, emitted shader targets, runtime requirements, and any declared optional-profile identity. They are never GPU marketing names, PCI device IDs, coverage-tier labels, or adapter-description substrings. Unknown, missing, or malformed requirement records are rejected by the existing package-validation boundaries before allocation. This is an additive v1 capability record; it does not redesign the package format. Start with raw payloads; compression requires a later measured net saving including decoder code.

## Scope

- Expression fusion and HLSL generation for a small math/noise fixture.
- Offline DXC compilation, validation, stripping, and shader cache.
- Exact emitted capability-requirements section within the existing .nwp v1 header and validation path.
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
- Encode only exact emitted capability requirements (profile identity/digest, feature/format/limit/shader/runtime requirements) and reject unknown, missing, or malformed requirement records through existing validation boundaries before allocation.
- Never infer capability from GPU generation, marketing name, tier label, or PCI device identifier.

## State, configuration, and API changes

- Define package version 1 and internal pass/binding contracts.
- Extend the v1 capability-requirements record with capability-profile ID, canonical requirements digest, exact queried features/formats/limits, shader targets, runtime/toolchain requirements, and declared optional-profile identity; do not change the package container structure.
- Introduce nodewerkc validate, compile-package, and inspect commands.
- Record compiler/profile fingerprints in build reports without unnecessary release strings.

## Recovery semantics

- Failed compilation publishes no partial package.
- Unknown major versions, required sections, instructions, or shader targets are rejected.
- Unknown or malformed capability requirements, profiles, or digests are rejected before bulk allocation through the existing package-validation diagnostic category; no package redesign or silent downgrade is introduced.
- Compilation diagnostics identify the originating expression/node.
- Temporary output is atomically replaced only after validation succeeds.

## Compatibility and safety constraints

- Early package versions may be incompatible; archival reproduction retains source graphs and pinned tools.
- Checksums detect corruption, not authenticity.
- A valid package can contain expensive or faulty shaders; parsing validation is not a sandbox for hostile GPU programs.
- No runtime DXC or Slang dependency.
- Capability requirement encoding is additive within v1 and preserves existing package/schema compatibility; unsupported optional profiles are separate artifacts, not common-package requirements.

## Test plan

- Compile and validate every fixture specialization.
- Test expression fusion against CPU mathematical oracles.
- Test unknown, missing, malformed, and contradictory capability requirement records, profile IDs, and digests at package-validation boundaries before allocation.
- Verify exact emitted feature/format/limit/shader/runtime requirements survive package round-trip and inspection.
- Fuzz malformed package sections, instruction operands, dimensions, and arithmetic.
- Compare package hashes across clean build directories.

## Regressions

- Track shader bytes and specialization counts per fixture.
- Reject debug/reflection leakage into release payloads.
- No parser failure may reach command submission.
- No GPU model, marketing generation, tier label, or PCI identifier may become a package capability requirement or parser branch.

## Acceptance criteria

- A generated fused compute shader is packaged reproducibly.
- Malformed-input tests and sanitizer-backed parser fuzzing complete without memory errors.
- Release inspection demonstrates absence of authoring metadata.
- Shader errors map back to graph expressions.
- Packages express and inspect exact capability requirements with profile identity/digest, and malformed or unknown requirements fail through existing validation boundaries without package-format redesign.

## Decomposition dependencies and boundaries

Dependencies:
- NW-01 provides DXC and capability targets.
- NW-02 provides graph types, expression semantics, and diagnostics.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No runtime source compilation, universal shader, or custom compression codec.
- Slang remains a later toolchain experiment, not a second mandatory shader path.
- No package-format redesign or capability inference from GPU names.
