# Nodewerk — a public pAIrallel showcase project

**Nodewerk is a public showcase repository for [pAIrallel](https://github.com/Bateriusz/pAIrallel).** Its purpose is not only to build a demoscene engine, but also to demonstrate how a non-trivial software project can be designed, decomposed, implemented, reviewed, verified, and evolved by the pAIrallel workflow.

pAIrallel is an AI software-development orchestration system. It defines and coordinates a team of specialized **architects**, a **lead architect**, **implementation agents/programmers**, and independent **reviewers**, assigning different models and roles to planning, implementation, verification, revision, and final review instead of relying on a single agent to perform the whole development process. The system also supports and verifies implementation results through automated tests run locally or through CI, so code review and acceptance can be tied to reproducible build, test, hardware-evidence, and regression results rather than to source inspection alone.

Nodewerk was designed end-to-end by the architects working through pAIrallel. The initial project brief was supplied as a single architecture task; the architects turned that brief into a concrete technical design, and the lead architect decomposed the resulting architecture into the ordered implementation roadmap **NW-01 through NW-12**, with explicit dependencies, acceptance criteria, measurement requirements, and subsystem boundaries.

The starting task asked the architects to design a modern, size-conscious, operator-graph-based demoscene engine inspired by the ideas behind Farbrausch's Werkkzeug tools and the public `fr_public` codebase, but redesigned for contemporary Windows systems and NVIDIA GeForce RTX 40-series and RTX 50-series GPUs rather than porting the historical implementation. From that brief, the lead architect chose a Windows 11 x64 / Direct3D 12 / C++20 architecture split into **`nodewerk-studio`**, the **`nodewerkc` whole-demo compiler**, and a highly specialized **`nodewerk-player`**. The NW-01…NW-12 roadmap then builds the system progressively: establish the toolchain and measurable hardware/runtime baseline; define versioned typed operator DAGs and deterministic evaluation; create offline shader lowering and the compact `.nwp` package contract; implement the minimal D3D12 player; specialize whole graphs and resource schedules; add procedural textures, meshes, scenes, animation, and the integer timeline; deliver and benchmark a complete 256 KiB-class architectural proof; add the interactive authoring editor and incremental GPU caching; introduce explicit temporal state for particles and feedback; investigate true 64 KiB-class exports; evaluate advanced RTX features only where measurements justify their cost; and finally add optional deterministic procedural audio. The intended end result is a modern operator-based demoscene production system in which a rich authoring graph can be compiled, specialized, stripped of unused functionality, and exported as a very small standalone GPU-driven demo for current RTX hardware.

The original NW-01…NW-12 decomposition also became part of the showcase experiment itself. The lead architect initially expressed some hardware acceptance requirements too narrowly, binding validation to an exact RTX 4060 SKU even though the architectural intent was capability- and generation-based RTX 40-series/Ada support. Because the project had physical access to an RTX 4060 Ti and an RTX 5070 Ti rather than that exact SKU, the backlog required a planning-level correction. This resulted in **NW-01A**, which introduced a durable capability-based GPU support policy and realigned the downstream NW-02…NW-12 requirements while preserving explicit performance-reference contracts where an exact device really matters. The physical NW-01 validation also exposed how much of the Windows toolchain setup, Full/CleanSystem verification, hardware evidence capture, artifact provenance, and staging process still depended on handwritten operator steps. That experience led to **NW-01B**, which turned the validation workflow into repository-owned PowerShell tooling, made the process reproducible and fail-closed, and required local execution and GitHub Actions to share the same provisioning and verification logic. As a result, the scripts created to automate the physical validation workflow became part of the project's CI path as well, reducing drift between local hardware qualification and automated CI verification.

## Architecture Overview (Three-Product Split)

1. **`nodewerk-studio` (Authoring Application):** Interactive operator-graph editor and profiling host.
2. **`nodewerkc` (Whole-Demo Compiler):** Compiles a versioned typed operator graph through backend-neutral optimized expressions and scheduled GPU passes into a compact demo package and generated player link manifest, compiling shaders offline via HLSL/DXC (Shader Model 6.0).
3. **`nodewerk-player` (Specialized Standalone Player):** Exception-free, RTTI-free Windows 11 x64 Direct3D 12 standalone player linked against the generated player link manifest with platform security mitigations retained.

See [`docs/README.md`](docs/README.md) for the full NW-01 Architecture Decision Records, dependency inventory, capability and toolchain manifests, size ledgers, and verification entry points.

## NW-03 Compiler-to-Player Contract

`nodewerkc` validates a typed graph, lowers reachable `ShaderExpr` DAGs to fused
specialized HLSL, compiles offline with pinned DXC (`1.8.2407`), validates and
strips DXIL, and atomically publishes a reproducible `.nwp` v1 package. Source
maps, reflection tables, compiler/profile fingerprints, and the original graph
remain development sidecars/build reports.

```bash
python tools/nodewerkc.py validate fixtures/nw03/math_noise.nwgraph
python tools/nodewerkc.py compile-package fixtures/nw03/math_noise.nwgraph build/math_noise.nwp \
    --report build/math_noise-report.json --dev-sidecar build/math_noise.dev.json
python tools/nodewerkc.py inspect build/math_noise.nwp
python tools/verify_nw03.py --mode Full --summary-json build/nw03-verify.json
```

See [`docs/nw03-compiler-player-contract.md`](docs/nw03-compiler-player-contract.md)
and [`docs/nwp-format-v1.md`](docs/nwp-format-v1.md). `verify_nw03.py --mode Full`
requires real pinned-DXC compilation and sanitizer-backed parser fuzzing and
returns nonzero if any required check is unavailable.

## Verification Entry Point (`NW-01`)

- **Cross-platform schema, dependency closure, reproducibility, and fixture contract suite:**
  ```bash
  pytest -v
  ```
- **Windows 11 x64 PowerShell verification runner (`Full` and `CleanSystem` modes):**
  ```powershell
  pwsh ./tools/verify-nw01.ps1 -Mode Full
  pwsh ./tools/verify-nw01.ps1 -Mode CleanSystem
  ```
- **GPU-less CI concrete build gate (no physical-evidence claim):**
  ```powershell
  python tools/verify_nw01.py --mode BuildChecks --summary-json build-checks.json
  ```

`Full` records two-clean-directory reproducibility results, validated offline DXC DXIL
evidence, measured size ledgers, and D3D12-validation fixture runs. `CleanSystem` records
SDK-absence checks and the observed GPU compute readback hash on the target machine.

`Full` exits `0` only when the complete baseline and physical evidence are verified, `2` when
concrete checks pass but RTX 4060 Ti/RTX 50-series or clean-system evidence remains unavailable,
`3` when required build/toolchain coverage (including MSVC/LTCG and failure-recovery
diagnostics) was skipped or unavailable, and `1` when a build, schema, dependency,
reproducibility, or fixture-validation check fails. Physical evidence is accepted only when
its recorded executable hashes and toolchain provenance match the selected, verified release
ledgers. `BuildChecks` performs the concrete build/reproducibility/dependency/failure-recovery
checks without claiming physical coverage; it exits non-zero if any required build check was
skipped.
Measured benchmark capture (timing, committed memory, capabilities) is available through
`python tools/verify_nw01.py --record-benchmark <tier> --binary-dir <deployment-dir>`.

## NW-04 Package Player

`src/player/package_player.*` consumes the finalized NW-03 `.nwp` v1 package without
redefining its capability identity. It validates exact emitted requirements against
live D3D12 queries before bulk allocation, queries adapter-specific resource sizes
and alignments, executes package initialization instructions once, and executes
ordered frame instructions at supplied ticks. The package-only compute and
fullscreen-display fixture is `nw_package_player_fixture`.

```bash
python tools/verify_nw04.py --mode ContractsOnly
python tools/nw04_fixture.py --output fixtures/nw04/compute_display.nwp
```

`Full` and `Hardware` qualification require a real offline-compiled NW-03 package
and the native Windows player. Missing Ada or Blackwell execution coverage remains
`pending_unverified`; the synthetic contract fixture is never accepted as hardware
evidence. See [`docs/nw04-player-runtime.md`](docs/nw04-player-runtime.md).

## NW-05 Typed-Graph Export

`tools/nw05_optimizer.py` performs whole-graph typed lowering with constant
propagation, dead-node/dead-output elimination, common-subexpression
elimination, bounded correctness-checked fusion, initialization/frame
partitioning, and field-specific reversible quantization.
`tools/nw05_schedule.py` emits explicit single-queue logical schedules and proves
adapter-specific alias groups at startup without serializing portable physical
offsets. `tools/nw05_export.py` publishes one artifact per capability profile,
with the common profile kept free of optional code, shaders, dependencies, and
budget accounting.

```bash
python tools/nodewerkc.py export fixtures/nw05/procedural_image.nwgraph build/demo \
    --dxc <pinned-dxc> --player-template <specialized-player.exe> \
    --report build/demo-report.json
python tools/nodewerkc.py inspect-size build/demo
python tools/verify_nw05.py --mode ContractsOnly --summary-json build/nw05.json
```

The exported graph is the sole source of the procedural demo content. The
generated executable embeds the unchanged `.nwp` v1 package and drives the
existing `PackagePlayer`; there is no hand-edited package or per-demo player
logic. Windows native and Ada/Blackwell alias/output-equality checks remain
`pending` until executed through the NW-01B evidence harness. See
[`docs/nw05-typed-graph-export.md`](docs/nw05-typed-graph-export.md).

## NW-06 Procedural Scene & Integer Timeline

The NW-06 proof exports a seeded 256x256 Noise2D material, a 64x32 parametric
torus, and 256 procedurally placed instances through the existing typed-graph,
NW-05 export, and `.nwp` v1 PackagePlayer path. GPU initialization generates the
texture, vertex/index buffers, dense instance/material tables, and one indirect
argument record; the frame schedule contains exactly one conventional indexed
instanced indirect draw, analytic lighting/twist deformation, an absolute-tick
camera curve, half-resolution bloom, and tonemapping.

```bash
python tools/nodewerkc.py export fixtures/nw06/procedural_scene.nwgraph build/nw06 \
    --dxc <pinned-dxc> --player-template <specialized-player.exe>
python tools/verify_nw06.py --mode ContractsOnly --summary-json build/nw06.json
```

Timeline time is 48,000 integer ticks per second with checked monotonic-clock
conversion and rational capture steps. Curves record bounded quantization error
and retain a full-precision escape hatch; event delivery uses ordered IDs over
`(previousTick, currentTick]` with explicit seek/replay resets. Ada/Blackwell
fixed-tick evidence remains `pending_unverified` until the identical common
artifact is executed on both representative hardware tiers. See
[`docs/nw06-procedural-scene.md`](docs/nw06-procedural-scene.md).
