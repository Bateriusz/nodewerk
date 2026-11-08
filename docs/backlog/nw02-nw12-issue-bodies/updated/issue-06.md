# NW-05 — Compile whole graphs into specialized players and explicit resource schedules

## Goal

Complete the first small graph-to-standalone path and make unused features disappear.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Implement typed lowering, constant propagation, dead-node/dead-output elimination, common-subexpression elimination, initialization/frame partitioning, profitable expression fusion, shader reachability, and generated player linkage. Logical resources declare read/write access and lifetime; the compiler emits transitions and alias eligibility. Startup realizes compatible alias groups using queried requirements, with required aliasing barriers and fence-safe lifetimes. Per-frame transient resources are duplicated or serialized where frames overlap. Generate-versus-bake decisions compare incremental compressed content plus uniquely introduced code/shaders, startup cost, and peak memory; initially use explicit documented choices rather than an unsupported automatic cost oracle. Quantization is field-specific, tolerance-bounded, and reversible to full precision. Build one artifact for one capability profile; alternatives are separately exported artifacts. `nodewerkc export` target profiles are selected by stable capability-profile identity (common profile ID and canonical requirements digest), never by GPU generation or model name. The generated player link manifest contains only feature queries reachable from the exported graph; unused optional implementations, shaders, and dependencies are excluded from the common artifact.

## Scope

- Whole-demo optimization and scheduling.
- Resource lifetime analysis and conservative aliasing.
- Capability-profile export identity and reachable-content closure for common and optional artifacts.
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
- Resolve export target identity from the capability-profile ID and canonical requirements digest; emit exact reachable feature/format/limit/shader/runtime requirements into the player link manifest.
- Exclude unused optional implementations, shaders, and dependencies from common exports; no optional profile may raise common capability, dependency, or size baselines.
- Qualify existing resource-schedule and aliasing validation on representative desktop Ada and Blackwell hardware using the identical common artifact.

## State, configuration, and API changes

- Add nodewerkc export and inspect-size commands.
- Define export settings for byte ceiling, memory budget, quantization tolerance, target profile, and generation/bake overrides.
- Add capability-profile ID and canonical requirements digest to export settings and reports in place of GPU generation/model selectors; preserve the existing target-profile interface and byte-budget settings.
- Record reachable feature-query closure and explicitly excluded optional implementations in export reports.
- Define logical alias groups rather than serialized adapter-specific offsets.

## Recovery semantics

- Failed optimization, shader compilation, linking, or budget validation leaves the prior complete artifact intact.
- Unsupported or unknown capability profiles fail at export with a dependency path.
- Missing representative hardware coverage remains pending and does not change a successful export artifact.
- Failed specialized runtime initialization exits rather than silently changing rendering quality.

## Compatibility and safety constraints

- Single-queue scheduling first; no unproven cross-queue alias reuse.
- A checksum or successful export does not remove runtime bounds checks.
- Build-time extensions are trusted tooling and are never loaded by the player.
- Optional feature profiles are separate artifacts with exact additional requirements and budgets; common exports never contain mandatory optional code, shaders, or dependencies.

## Test plan

- Golden optimized-IR tests for folding, elimination, frequency partitioning, and quantization.
- Property tests for overlapping lifetimes, compatible alias groups, and frames-in-flight reuse.
- Verify export target selection is determined by capability-profile ID/digest and not by GPU model, generation, tier label, or adapter name.
- Compare aliased and non-aliased fixture outputs under GPU validation on representative desktop Ada and Blackwell hardware; record exact device/capability/artifact metadata and mark missing coverage pending.
- Verify unused optional operator/profile additions do not change the common link closure.
- Export identical graphs in clean directories and compare package and PE hashes.

## Regressions

- Unused feature additions must not increase the fixture's linked feature closure.
- Quantization must remain within declared tolerances.
- Scheduling changes must preserve initialization and frame ordering.
- Optional profile additions must not enlarge common exports or common capability requirements.
- GPU marketing metadata must not alter export target identity or byte-identical results.

## Acceptance criteria

- A textual graph exports a self-contained executable that generates and displays a procedural image.
- Repeated clean exports are byte-identical under the pinned environment.
- Reports attribute all distributed bytes and identify dependency costs.
- Resource aliasing and schedule validation runs on representative desktop Ada and Blackwell hardware without changing common-artifact output; missing coverage remains pending.
- Export identity is keyed by capability-profile ID/digest, and common exports exclude unused optional implementations, shaders, and dependencies.

## Decomposition dependencies and boundaries

Dependencies:
- NW-02 supplies semantic graph evaluation.
- NW-03 supplies shader/package generation.
- NW-04 supplies the runtime and actual allocation requirements.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No general-purpose runtime graph interpreter.
- No universal bake heuristic, unrestricted pass fusion, or advanced renderer.
- No optional implementation or GPU-name predicate may enter the common export.

<!-- pairallel-sections:v1 {"sections":[{"end":94,"slug":"goal","start":9},{"end":389,"slug":"source-context","start":115},{"end":1760,"slug":"final-technical-design","start":418},{"end":2108,"slug":"scope","start":1772},{"end":2248,"slug":"affected-components","start":2134},{"end":3140,"slug":"required-behavior","start":2272},{"end":3760,"slug":"state-configuration-api-changes","start":3184},{"end":4192,"slug":"recovery-semantics","start":3785},{"end":4636,"slug":"compatibility-and-safety-constraints","start":4235},{"end":5368,"slug":"test-plan","start":4652},{"end":5779,"slug":"regressions","start":5386},{"end":6377,"slug":"acceptance-criteria","start":5805},{"end":7173,"slug":"decomposition-dependencies-and-boundaries","start":6425}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":4,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:dd8620adac2b708eb6282fa0d743a5844a2487087563752d45ea7dc6b9371631","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
