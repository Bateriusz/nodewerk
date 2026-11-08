# ADR-0001: Three-Product Architecture, Data Flow & Backend-Neutral Compiler IR

- **Status:** Accepted
- **Task:** NW-01
- **Date:** 2026-11-06

## Context

Demoscene engines face a fundamental tension between rich interactive authoring workflows and ultra-compact standalone player executables. Coupling the authoring application or an all-purpose runtime rendering abstraction into the shipped player inflates fixed binary overhead and complicates dead-code elimination.

Prior to implementation, we inspected the tracked reference documentation in `references/README.md` (which documents the inclusion of `references/farbrausch/fr_public` for conceptual study only). Nodewerk is a clean-room modern implementation; no Farbrausch source code is copied or mechanically translated.

## Decision

Nodewerk is partitioned into **three separate products** with strict compilation and dependency boundaries:

1. **Authoring Application (`nodewerk-studio`)**
   - Interactive operator-graph editor, timeline tool, live preview, and profiling host.
   - Permitted to use the C++20 standard library, UI toolkits, debug layers, and development sidecars (`d3d12sdklayers.dll`, `dxgidebug.dll`, PIX).
   - *Scope Boundary:* Implementing the authoring application is outside NW-01 (the minimal editor is NW-08 / issue #9; broader editor UX is later work).

2. **Whole-Demo Compiler (`nodewerkc`)**
   - Ahead-of-time command-line compiler that consumes a **versioned typed operator graph**, lowers parameters into **optimized expressions**, lowers operator evaluation into **scheduled GPU passes** (raster, compute, and resource transitions), invokes offline HLSL/DXC shader compilation, and emits:
     - a **compact binary package** (opcodes, constant tables, pass schedule, and offline DXIL shader bytecode), plus
     - a **generated player link manifest** (enumerating only the operator kernels, shader permutations, and optional D3D12 capability queries actually referenced by the demo).
   - *Backend Neutrality in Compiler IR:* Backend neutrality is preserved strictly in `nodewerkc`'s intermediate representations (the Typed Expression IR and Scheduled Pass IR). We explicitly reject introducing a general runtime Rendering Hardware Interface (RHI) virtual dispatch layer in the player.
   - *Scope Boundary:* Implementing `nodewerkc` is outside NW-01 (NW-02 schemas, NW-03 shader/package lowering, NW-05 whole-graph specialization/scheduling, and NW-10 compact exports; NW-04 owns the player and NW-06 owns the procedural operator set).

3. **Specialized Standalone Player (`nodewerk-player`)**
   - Minimal Windows 11 x64 Direct3D 12 executable compiled against the generated player link manifest so unreferenced operators and features are dead-stripped by the linker (`/OPT:REF`, `/OPT:ICF`, LTO).
   - Compiles with C++ exceptions disabled (`/EHs-c-`, `_HAS_EXCEPTIONS=0`), RTTI disabled (`/GR-`), platform security mitigations retained, and zero heavy standard library dependencies.

## End-to-End Data Flow

```text
[Authoring Application (nodewerk-studio)]
        |
        v  (serializes)
[Versioned Typed Operator Graph]
        |
        v  (nodewerkc frontend & optimizer)
[Optimized Expressions IR (Backend-Neutral)]
        |
        v  (nodewerkc pass scheduler & barrier planner)
[Scheduled GPU Passes IR (Backend-Neutral)]
        |
        +---> [Offline HLSL Codegen + Offline DXC (SM 6.0 DXIL)]
        |
        v  (nodewerkc emitter)
[Compact Demo Package] + [Generated Player Link Manifest]
        |
        v  (clang-cl + lld-link dead-stripped player build)
[Specialized Standalone Player (nodewerk-player.exe)]
```

## Explicit Non-Claim on 64 KiB Budget

NW-01 establishes the reproducible empty-player, clear/present, and offline compute-shader byte-ledger schemas (`schemas/size-ledger.schema.json`) and now carries verified baseline ledgers (`benchmarks/ledgers/*.json`) for the measured Windows/MSVC and clang-cl fixture builds. **No claim is made that a 64 KiB complete demo budget is achievable before NW-10 supplies whole-demo package and compression evidence.**
