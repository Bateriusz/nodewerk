# NW-05 Typed-Graph Export and Procedural-Image Path

- **Status:** Implemented from base `14a4d80b89b05909d1da033e15f60d36e859a36a`
- **Task:** NW-05
- **Formats:** `.nwgraph` v1 (unchanged), `.nwp` v1 (unchanged)
- **Common profile:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Requirements digest:** `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`

The supplied prerequisite completion clarification is authoritative: NW-01A is
treated as complete through its backlog corrections rather than through a
missing code commit, so NW-05 builds on the reported completed baseline.

## Result

A textual typed graph is now the sole source of a standalone procedural-image
demo:

```bash
python tools/nodewerkc.py export fixtures/nw05/procedural_export.nwgraph build/demo \
    --dxc <pinned-dxc> --player-template <specialized-player.exe> \
    --report build/demo-report.json
python tools/nodewerkc.py inspect-size build/demo
```

`export` runs whole-graph typed lowering, constant propagation, dead-node and
dead-output elimination, common-subexpression elimination, bounded expression
fusion, initialisation/frame partitioning, and field-specific reversible
quantization. It then compiles the reachable shaders offline with the existing
NW-03 pinned-DXC pipeline, assembles the established `.nwp` v1 package, adds
the established NW-04 compute/display stream (`frame.present`), proves resource
aliases against queried allocation requirements, and publishes one artifact per
capability profile.

The published directory contains, per profile:

- `<profile>.nwp` — the unchanged `.nwp` v1 execution package;
- `<profile>.manifest.json` — generated used-feature player link manifest;
- `<profile>.schedule.json` — logical single-queue resource schedule;
- `<profile>.aliases.json` — adapter-specific alias realisation evidence;
- `<profile>.size.json` — complete byte/dependency attribution;
- `<profile>.exe` — self-contained specialized player: the generated main is
  compiled and linked in the pinned Windows build, its actual PE import closure
  is verified against the manifest, then package+manifest are embedded by the
  deterministic footer;
- `<profile>.player.cpp` — generated generic main that binds the embedded
  package and calls the existing `PackagePlayer`; it contains no demo content
  or per-demo logic.

The checked-in `nw_procedural_image_player` target is the compiled,
content-free player template; on Windows it is part of the default build and
its post-build dependency/closure check runs like the other fixtures.

`tests` and `tools/verify_nw05.py --mode ContractsOnly` use a structurally
valid synthetic DXIL payload for GPU-less closure/reproducibility checks. That
is not hardware evidence; release export still requires the pinned real DXC.

## Optimised IR

`tools/nw05_optimizer.py` consumes the NW-02 `GraphDocument` produced from
`.nwgraph` v1 text. It does not introduce a second graph format, a second
semantic digest, or a runtime interpreter.

The deterministic textual IR is exposed by `OptimizedGraph.to_text()` and the
structured form by `to_dict()`. `fixtures/nw05/golden_optimized_ir.json` pins
the golden typed lowering for `fixtures/nw05/procedural_image.nwgraph`.

Semantic release identity (`ir_sha256`) covers retained nodes, outputs,
partitions, settings, profile identity, and quantization. Diagnostic fields
such as dead nodes, CSE bookkeeping, fusion decisions, source hashes, and
generation/bake decisions are deliberately outside that identity, so adding an
unreachable operator or unused output cannot change the generated link closure
or the common artifact.

### Constant propagation

Typed CPU operators (`nw.math.*`, `nw.control.*`, deterministic procedural
helpers) are evaluated transitively when all inputs are constant. A folded node
is scheduled at compile time and no longer requires its inputs at runtime.
ShaderExpr ASTs are folded with the NW-03 oracle vocabulary. Correctness is
checked by comparing the original and folded ASTs on deterministic sample
points using the documented `1e-5` absolute / `1e-4` relative tolerance.

### Dead elimination

Reachability starts from present/terminal render outputs, readback outputs, or
terminal ShaderExpr roots, exactly as NW-02/NW-03 semantics define. Nodes that
do not contribute are removed. Output ports that are not consumed are recorded
as dead outputs. Folded nodes become compile-time constants, so their now-dead
producer chains are removed.

### Common-subexpression elimination

Pure typed nodes and identical ShaderExpr roots are keyed by operator,
semantic version, frequency, canonical parameters, and canonical upstream
inputs. Duplicate nodes are merged into one canonical node and all consumers
are rewritten. Resource-producing, render, upload, readback, and present nodes
are never merged.

### Initialisation/frame partitioning

The output partitions are `compile-time`, `initialization`, and `frame`.
Dependency order is preserved inside every partition. A producer may not run
after a consumer; the optimizer fails instead of silently reordering across a
frequency transition. The `.nwp` `INIT` and `FRAM` instruction order remains the
NW-03/NW-04 order and is copied into the logical schedule for verification.

### Bounded fusion

Fusion is applied only to reachable ShaderExpr chains. Each decision records
the root, member nodes, expression bytes before/after, estimated instruction
counts, correctness-oracle result, and an explicit `accepted` flag. The default
bounds are 64 nodes and 64 KiB of expression text. If fusion is disabled,
exceeds a bound, fails the correctness oracle, or is not smaller than the
unfused path, the record is not accepted and the correct NW-03 root lowering is
retained. No automatic global fusion heuristic is inferred from GPU names.

### Field-specific reversible quantization

Quantization is configured through the existing graph settings:

```text
setting quantization_policy bits
setting quantization_bits 8
setting quantization_tolerance 0.01
setting quantization_fields "gain.value camera.fov_degrees"
setting quantization_field.gain.value "bits=8;tolerance=0.01"
```

`quantize_document` records the original full-precision tuple, the quantized
tuple, the maximum absolute error, and whether the error is within the declared
tolerance. If any selected field exceeds its tolerance, export fails closed.
`recover_full_precision()` returns the exact source values. Setting
`quantization_policy none` disables quantization. Integer dimensions/seeds are
not quantized implicitly; they must be selected explicitly.

## Generation versus bake

Generation is the default. Bake is never selected by an automatic cost oracle.
The existing `.nwp` v1 player has no runtime consumer for arbitrary baked node
records. Explicit `bake` overrides therefore fail closed with a dependency
path; they are never silently rewritten to generation and never emit
unconsumed parameter blobs. Generation decisions report that shared shader and
resource closure costs are attributed by the package/link-manifest reports, not
per node, and mark per-node measurements as unavailable. There is no automatic
cost oracle and generation remains the default.

## Capability-profile artifacts and budgets

The common profile is always emitted. Optional profiles (currently
`nw.optional.d3d12.wave_ops.v1`) selected with `--optional-profile` (or all of
them with `--allow-optional-profiles`) remain separate artifacts with
independent byte/memory accounting. They never add code, shaders, dependencies,
or budget to the common artifact.

Every artifact is keyed by:

```
sha256(canonical {
  profile_id,
  requirements_digest,
  exact emitted requirements
})
```

The graph/profile/digest must resolve to a known table entry before any
allocation. Unknown profiles fail with a `dependency_path`. Observed GPU model,
marketing generation, adapter name, PCI identifiers, and evidence tier are
metadata only.

Export settings expose `--byte-ceiling`, `--memory-budget`,
`--quantization-tolerance`, `--target-profile`, `--requirements-digest`,
generation/bake overrides, frame-slot count, and the player template. The
existing `compile-package` command accepts the same target-profile, digest,
byte-ceiling, and memory-budget options for compatibility.

The memory budget uses complete peak accounting: adapter allocation padding,
transient frame-slot duplication, and PackagePlayer-owned swap-chain, display,
upload-ring, descriptor, and readback storage are all included. Without an
adapter query a placement-aligned upper bound is used; the generated player
re-checks the same limit after native allocation requirements are queried and
before any bulk GPU allocation.

## Resource schedules and physical aliases

`tools/nw05_schedule.py` emits an explicit single direct-queue logical
schedule. Each resource records every logical read/write, the required state,
its lifetime, and whether it is alias-eligible. The portable logical schedule
contains no physical offsets or heap assumptions.

Physical alias realisation is a separate adapter-specific step:

1. query every resource through the supplied adapter allocation query (the
   Windows player uses `ID3D12Device::GetResourceAllocationInfo`);
2. reject incompatible kind/format/usage/dimension groups;
3. reject overlapping lifetimes;
4. duplicate transient resources once per frame slot (`frames_in_flight`) or
   serialize frames when the runtime uses one frame slot;
5. reuse an offset only within the same frame-slot instance and compatible
   alias group;
6. emit aliasing and UAV barriers in execution order, including the
   frame-boundary activation that lets the next frame reuse a region first
   touched by a different resource. Fence completion does not replace these
   barriers.

The realisation record is marked `"portable": false` and separates
adapter-specific offsets from the logical plan. Missing adapter allocation
information fails closed. Cross-queue alias reuse is impossible because the
schedule exposes exactly one direct queue.

The native `src/player/resource_plan.*` API adds
`BuildAliasedResourcePlan(..., frames_in_flight)` for startup realisation while
keeping the NW-04 non-aliased plan available. On Windows the player calls this
plan with the real adapter query; when the proof finds an actual fence-safe
alias it creates one placed heap, realizes resources at the proven offsets, and
inserts the recorded aliasing/UAV barriers. When no alias opportunity exists,
the player preserves the established NW-04 committed non-aliased plan. The
64 KiB simulation is never selected silently; host tests must request it
explicitly.

An export that does not supply an adapter allocation query publishes the
logical schedule with an explicit `pending_adapter_query` realization: no
offsets, aliases, or barriers are invented, and startup performs the real
`GetResourceAllocationInfo` proof before reuse. Memory budgeting uses the
logical resident-byte upper bound until adapter evidence is available.

## Link manifest

The generated manifest lists exactly:

- reachable typed nodes, operators, outputs, and partitions;
- shader specialisations and emitted targets;
- required formats, limits, init/frame instructions, and runtime requirements;
- feature queries, including `GetResourceAllocationInfo` for alias proof;
- retained code/shaders/data/dependencies;
- explicitly excluded optional implementations, optional profiles, authoring
  infrastructure, and GPU-name predicates.

Runtime DXC/Slang dependencies are always false. Release packages remain
leakage-clean.

## Failure behavior

Optimisation, DXC compilation, DXIL validation, package validation, linking,
embedding, budget validation, and publication are transactional. A failed
export leaves the previous complete artifact directory untouched and writes a
diagnostic report when a report path was supplied. Budget violations fail
closed after writing the diagnostic. Initialization failures in the generated
player return the existing stable exit codes rather than changing rendering
quality. Runtime bounds checks remain active; trusted build-time extensions are
never loaded by the player.

## Verification

```bash
python tools/verify_nw05.py --mode ContractsOnly --summary-json build/nw05.json
```

Contracts-only verification covers the golden IR, constants, dead elimination,
CSE, partitioning, quantization bounds/recovery, schedule property tests
(overlap, incompatible groups, fence-safe reuse, frames in flight), export
closure, optional-profile isolation, byte/memory budgets, failure recovery,
size attribution, and two-clean-directory reproducibility. Windows native and
Ada/Blackwell GPU checks are reported distinctly as `passed`, `failed`, or
`pending`; missing coverage is never treated as success and executed failures
are never hidden.

`benchmarks/nw05-hardware-evidence.json` remains `pending_unverified` until the
identical common artifact is exercised through the established NW-01B evidence
harness on representative desktop Ada and Blackwell hardware.



## Executable semantic boundary

NW-05 v1 is fail-closed at the executable boundary. The exporter currently
publishes only reachable ShaderExpr-family roots whose semantics are emitted by
the established NW-03 compute compiler. Texture/material/mesh/camera/light and
richer render operators remain valid NW-02 authoring vocabulary and optimizer
inputs, but export rejects them until an actual package/runtime consumer exists.
They are never emitted as inert parameters and called implemented.

The checked-in rich `procedural_image.nwgraph` remains the optimizer/golden
fixture. `procedural_export.nwgraph` is the bounded executable acceptance
fixture: two terminal composed shader DAGs exercise the real multi-dispatch
unfused fallback. Their equal-sized multi-region UAVs give a real disjoint
lifetime for alias qualification, with the canonical final root presented as
the procedural image.

Production Windows export compiles the generated specialized main. Optional
D3D12 OPTIONS1/OPTIONS4 query code is selected at compile time from the link
manifest; the generated executable is inspected for its actual imports and
rebuilt against that exact dependency list before embedding. Hardware evidence
uses the real D3D12 debug-layer path via `--validation`; no metadata-only
validation flag is accepted.


Rejected, unmeasured, disabled, or bound-exceeding shader fusion does not fall
back to recursive expression inlining. Each ShaderExpr dependency is compiled
as its own compute pipeline; ordered dispatches exchange values through
deterministic image-sized regions of the root UAV with explicit UAV barriers.
The root writes region zero, so presentation and readback expose only the
canonical image while scratch regions remain private to execution.
