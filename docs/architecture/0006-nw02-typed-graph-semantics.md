# ADR-0006: NW-02 Versioned Typed Operator Graph Semantics

- **Status:** Accepted
- **Task:** NW-02
- **Date:** 2026-11-10

## Context

NW-01 established the three-product boundary and predeclared that the versioned
typed operator graph is authoring/compiler-side only. NW-02 implements that
contract without introducing a graphics API abstraction into logical graph
types, and without linking a graph parser or runtime interpreter into
`nodewerk-player`.

NW-01A and NW-01B are complete on this implementation base. The graph work
consumes their C++20/toolchain, capability-baseline, and dependency-boundary
contracts.

## Decision

1. **Immutable snapshots.** `GraphSnapshot` is an immutable, validated,
   topologically ordered view of a typed multi-output DAG. Invalid edits never
   replace the last valid snapshot.
2. **Stable schema declarations.** `OperatorSchema` declares stable operator and
   port identifiers, semantic versions, canonical parameters, frequency policy,
   seed behavior, residency, bounded resource descriptors, lowering hooks, and
   whether the operator is pure `ShaderExpr` composition, a resource-producing
   `GpuPass`, or a `CpuControl` expression.
3. **Versioned text.** `.nwgraph` version 1 is a deterministic textual schema.
   Version 0 migration is explicit (`nwgraph-v0-to-v1`). Unknown schema and
   operator versions are rejected rather than reinterpreted.
4. **Canonical semantics.** A wide SHA-256 content digest covers canonical field
   encodings, schema/operator versions, upstream structural hashes, compiler
   settings, quantization policy, and the capability-profile identity
   (`nw.common.d3d12.fl12_0.sm6_0.v1` plus its canonical requirements digest).
   Struct padding, raw parameter memory, node ids, and editor layout are not
   hashed. A node id becomes semantic only through an explicit `seed node-id`
   input.
5. **Explicit drift boundaries.** Ordinary cycles, incompatible ports,
   frequency regressions, history semantics (reserved for NW-09), implicit
   readbacks/uploads, malformed/unknown/mismatched capability identities, and
   out-of-bounds resources fail through precise source-linked diagnostics.
6. **CPU reference behavior.** Control/math, curve, scene/camera/light/material,
   ShaderExpr composition, and practical procedural oracles have deterministic
   CPU reference evaluation. Cache invalidation is limited to the dependent
   closure; missing/corrupt entries are misses.
7. **No player runtime graph.** The player has no plugin ABI, graph parser, or
   operator interpreter. Logical types and serialization contain no D3D12
   resource pointers. GPU image correctness remains tolerance-based outside
   this task.

## Explicit non-goals

- GUI/editor implementation (`nodewerk-studio`, NW-09).
- Executable feedback or live GPU preview.
- Mutable scene hierarchies.
- General scripting or arbitrary release-data code.
- A CPU renderer; procedural/image values are deterministic oracles, not pixel
  correctness claims.

## Verification

`tests/test_nw02_graph_semantics.py` builds the C++20 library/CLI, validates and
evaluates fixtures without a GPU, checks clean-run determinism and canonical
golden digests, exercises positive/negative type/frequency/residency/resource
fixtures, and verifies cache closure, migration, profile independence, and
last-valid-snapshot retention.
