# NW-05 — Compile whole graphs into specialized players and explicit resource schedules

## Goal

Complete the first small graph-to-standalone path and make unused features disappear.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Implement typed lowering, constant propagation, dead-node/dead-output elimination, common-subexpression elimination, initialization/frame partitioning, profitable expression fusion, shader reachability, and generated player linkage. Logical resources declare read/write access and lifetime; the compiler emits transitions and alias eligibility. Startup realizes compatible alias groups using queried requirements, with required aliasing barriers and fence-safe lifetimes. Per-frame transient resources are duplicated or serialized where frames overlap. Generate-versus-bake decisions compare incremental compressed content plus uniquely introduced code/shaders, startup cost, and peak memory; initially use explicit documented choices rather than an unsupported automatic cost oracle. Quantization is field-specific, tolerance-bounded, and reversible to full precision. Build one artifact for one capability profile; alternatives are separately exported artifacts.

## Scope

- Whole-demo optimization and scheduling.
- Resource lifetime analysis and conservative aliasing.
- Generated used-feature link manifest and embedded package.
- Export reports explaining retained code, shaders, data, and dependencies.

## Affected components

- Proposed nodewerkc exporter
- Proposed scheduler/resource planner
- Proposed specialized-player build generation

## Required behavior

- The graph is the only source of demo content; no hand-edited package or per-demo player logic.
- Adding an unreachable operator does not change semantic release content.
- Export budgets fail closed while retaining a diagnostic report.
- Fusion is accepted only when correctness is preserved and measured size/performance tradeoffs are recorded.

## State, configuration, and API changes

- Add nodewerkc export and inspect-size commands.
- Define export settings for byte ceiling, memory budget, quantization tolerance, target profile, and generation/bake overrides.
- Define logical alias groups rather than serialized adapter-specific offsets.

## Recovery semantics

- Failed optimization, shader compilation, linking, or budget validation leaves the prior complete artifact intact.
- Unsupported capabilities fail at export with a dependency path.
- Failed specialized runtime initialization exits rather than silently changing rendering quality.

## Compatibility and safety constraints

- Single-queue scheduling first; no unproven cross-queue alias reuse.
- A checksum or successful export does not remove runtime bounds checks.
- Build-time extensions are trusted tooling and are never loaded by the player.

## Test plan

- Golden optimized-IR tests for folding, elimination, frequency partitioning, and quantization.
- Property tests for overlapping lifetimes, compatible alias groups, and frames-in-flight reuse.
- Compare aliased and non-aliased fixture outputs under GPU validation.
- Export identical graphs in clean directories and compare package and PE hashes.

## Regressions

- Unused feature additions must not increase the fixture's linked feature closure.
- Quantization must remain within declared tolerances.
- Scheduling changes must preserve initialization and frame ordering.

## Acceptance criteria

- A textual graph exports a self-contained executable that generates and displays a procedural image.
- Repeated clean exports are byte-identical under the pinned environment.
- Reports attribute all distributed bytes and identify dependency costs.
- Resource aliasing tests and validation runs pass without changing output.

## Decomposition dependencies and boundaries

Dependencies:
- NW-02 supplies semantic graph evaluation.
- NW-03 supplies shader/package generation.
- NW-04 supplies the runtime and actual allocation requirements.

Boundaries:
- No general-purpose runtime graph interpreter.
- No universal bake heuristic, unrestricted pass fusion, or advanced renderer.
