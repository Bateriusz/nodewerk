# NW-08 — Add a minimal authoring editor with incremental GPU caching

## Goal

Make the proven graph model usable interactively without changing release semantics.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Build a separate editor with graph connections, parameter inspection, timeline scrubbing, preview, compiler diagnostics, pass timings, and size attribution. Use immutable graph snapshots and generation IDs for asynchronous compilation. Cache GPU realizations by full semantic key plus device generation; evict under a configured memory budget only after fences permit destruction. Reuse compiler lowering and runtime pass execution for preview so GUI and CLI exports remain equivalent. UI libraries, shader compiler libraries, profiling tools, and source maps belong exclusively to authoring targets.

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
- Obsolete jobs cannot publish results into a newer graph snapshot.
- Cached resources remain alive until submitted GPU work retires.
- The proof graph can be created and exported without editing serialized text.

## State, configuration, and API changes

- Add editor-only layout, selection, undo, and cache-budget state.
- Define evaluation generation tokens and device-generation cache keys.
- Keep editor metadata outside canonical export semantics.

## Recovery semantics

- Shader errors retain the last valid preview and show source-linked diagnostics.
- Corrupt disk cache entries regenerate.
- Device loss clears GPU realizations and allows explicit preview reinitialization.
- Recovery never silently bypasses or changes graph operators.

## Compatibility and safety constraints

- Editor dependencies must not appear in player imports or link manifests.
- A cache key includes every relevant compiler, target, and semantic input.
- No hard real-time editor latency claim is made without a representative graph benchmark.

## Test plan

- Test snapshot cancellation, stale completion, cache eviction, and fence retirement.
- Compare CLI and GUI export bytes for the same semantic graph.
- Test broken shader edits followed by successful recovery.
- Inspect player dependency closure after editor integration.

## Regressions

- Editor layout edits must not change release output.
- Unchanged graph branches must retain cache hits.
- GPU cache growth must remain bounded.

## Acceptance criteria

- The proof is editable, previewable, and exportable through the GUI.
- GUI and CLI exports are identical under identical settings.
- Failed edits and cancelled jobs cannot replace valid preview state.
- Release byte size remains unchanged by editor-only additions.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 establishes a validated export and rendering reference.
- NW-02 supplies immutable snapshots and semantic caching.

Boundaries:
- No general plugin marketplace, asset browser, scripting console, or elaborate editor polish.
- Editor recovery is independent of the minimal release player's failure policy.
