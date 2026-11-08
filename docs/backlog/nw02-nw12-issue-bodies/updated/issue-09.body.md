## Goal

Make the proven graph model usable interactively without changing release semantics.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Build a separate editor with graph connections, parameter inspection, timeline scrubbing, preview, compiler diagnostics, pass timings, and size attribution. Use immutable graph snapshots and generation IDs for asynchronous compilation. Cache GPU realizations by full semantic key plus a device-generation key defined as the D3D12 device-instance recreation epoch. The epoch key additionally includes the capability-profile ID/digest, driver/OS/toolchain identity where relevant, and actual adapter/device identity required for device-resident realizations; it is never an RTX marketing generation. Evict under a configured memory budget only after fences permit destruction. Compiler targets are keyed by capability-profile identity, not GPU model. Reuse compiler lowering and runtime pass execution for preview so GUI and CLI exports remain equivalent. UI libraries, shader compiler libraries, profiling tools, and source maps belong exclusively to authoring targets.

## Scope

- Minimal graph and curve editing.
- Background compilation and last-valid preview.
- Bounded GPU cache and dependency-aware invalidation.
- Export UI invoking the same compiler path as the CLI.

## Affected components

- Proposed editor
- Proposed GPU authoring cache
- Proposed diagnostics/profiling UI

## Required behavior

- A parameter change recomputes only affected resources and dependent passes.
- Obsolete jobs cannot publish results into a newer graph snapshot or a later device-recreation epoch.
- Distinguish device generation (D3D12 device-instance recreation epoch) from GPU marketing generation; stale jobs from a prior epoch cannot publish results into a new device instance.
- Cached resources remain alive until submitted GPU work retires.
- The proof graph can be created and exported without editing serialized text.

## State, configuration, and API changes

- Add editor-only layout, selection, undo, and cache-budget state.
- Define evaluation generation tokens and device-generation cache keys keyed by D3D12 device-instance recreation epoch, capability-profile ID/digest, and actual adapter/device identity where device-resident state requires it.
- Keep compiler target keys identified by capability-profile identity, not GPU generation or model metadata.
- Keep editor metadata outside canonical export semantics.

## Recovery semantics

- Shader errors retain the last valid preview and show source-linked diagnostics.
- Corrupt disk cache entries regenerate.
- Device loss clears GPU realizations and allows explicit preview reinitialization; device recreation increments the device-generation epoch and invalidates device-resident realizations.
- Recovery never silently bypasses or changes graph operators.
- Disk-cache and compiler-cache ownership semantics remain unchanged.

## Compatibility and safety constraints

- Editor dependencies must not appear in player imports or link manifests.
- A cache key includes every relevant compiler, target, and semantic input.
- Cache keys may include actual adapter/device identity only for device-resident realizations; semantic and compiler artifacts remain hardware-model independent except for declared capability-profile identity.
- No RTX marketing generation string may become a cache, compiler, or capability predicate.
- No hard real-time editor latency claim is made without a representative graph benchmark.

## Test plan

- Test snapshot cancellation, stale completion across device-recreation epochs, cache eviction, and fence retirement.
- Verify capability-profile changes invalidate compiler/cache targets while GPU marketing-name metadata does not.
- Verify actual adapter/device identity is retained only for device-resident realization keys.
- Compare CLI and GUI export bytes for the same semantic graph.
- Test broken shader edits followed by successful recovery.
- Inspect player dependency closure after editor integration.

## Regressions

- Editor layout edits must not change release output.
- Unchanged graph branches must retain cache hits.
- GPU cache growth must remain bounded.
- Device-generation epoch or capability-profile changes must not be conflated with RTX marketing generations.

## Acceptance criteria

- The proof is editable, previewable, and exportable through the GUI.
- GUI and CLI exports are identical under identical settings.
- Failed edits and cancelled jobs cannot replace valid preview state.
- Release byte size remains unchanged by editor-only additions.
- Device generation means a D3D12 device-instance recreation epoch, compiler targets are keyed by capability-profile identity, and device-resident realizations retain actual adapter/device identity.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 establishes a validated export and rendering reference.
- NW-02 supplies immutable snapshots and semantic caching.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No general plugin marketplace, asset browser, scripting console, or elaborate editor polish.
- Editor recovery is independent of the minimal release player failure policy.
- No GPU marketing generation may define a device epoch or compiler target.

<!-- pairallel-sections:v1 {"sections":[{"end":93,"slug":"goal","start":9},{"end":388,"slug":"source-context","start":114},{"end":1385,"slug":"final-technical-design","start":417},{"end":1591,"slug":"scope","start":1397},{"end":1701,"slug":"affected-components","start":1617},{"end":2235,"slug":"required-behavior","start":1725},{"end":2739,"slug":"state-configuration-api-changes","start":2279},{"end":3206,"slug":"recovery-semantics","start":2764},{"end":3792,"slug":"compatibility-and-safety-constraints","start":3249},{"end":4320,"slug":"test-plan","start":3808},{"end":4592,"slug":"regressions","start":4338},{"end":5082,"slug":"acceptance-criteria","start":4618},{"end":5891,"slug":"decomposition-dependencies-and-boundaries","start":5130}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":7,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:555a165d08e2e04173f9a191f40612fe062d8d1718a082565669926a9e983384","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
