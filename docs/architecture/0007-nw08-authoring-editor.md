# ADR-0007: NW-08 Authoring Editor Scope and Release Isolation

- **Status:** Accepted
- **Task:** NW-08 implementation
- **Date:** 2026-11-15

## Context

ADR-0005 originally placed `nodewerk-studio` in NW-09 and assigned NW-08 to
audio synthesis. The NW-08 implementation task instead asks for the minimal
interactive authoring editor that makes the NW-07 architectural proof editable,
previewable, and exportable. The existing frozen NW-07 build inputs, release
package, player, and performance-reference contract must remain unchanged.

## Decision

Implement `nodewerk-studio` as a separate authoring product layered on the
authoritative NW-02 document model:

1. **Single graph model.** The editor mutates `tools/nw03_compiler.py`
   `GraphDocument` and re-serializes `.nwgraph` v1 for every edit. It does not
   introduce a second graph representation.
2. **Existing preview/export path.** Preview launches the existing
   `PackagePlayer` runtime on Windows. Export calls
   `nw05_export.export_graph`, the same function used by the `nodewerkc export`
   CLI. No new package or export format is created.
3. **Editor metadata isolation.** Layout, selection, undo/redo, timeline
   position, preview state, and cache budget live outside the canonical
   semantic encoding and release output.
4. **Separate build target.** Because every root CMake file is a frozen NW-07
   build input, the native studio preview host is a standalone authoring
   project under `src/studio/` plus an explicit opt-in
   `cmake/Nw08Studio.cmake` hook. It is not part of the default release build.
5. **Honest evidence.** Contract simulations are never reported as physical
   hardware evidence. Real preview and Windows GUI/CLI equivalence require the
   retained Windows integration report.

## Consequences

- The NW-07 pinned build-input digest and release distribution are unchanged.
- The editor can evolve independently of release-player failure policy.
- Audio work originally associated with NW-08 remains out of scope for this
  task and does not alter the three-product architecture.
- Hardware acceptance for preview/GUI execution remains pending until executed
  and retained on Windows.
