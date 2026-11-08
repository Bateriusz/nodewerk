# NW-10 — Engineer compact exports and determine true 64 KiB feasibility

## Goal

Reduce measured total bytes without concealing dependencies or distorting the general engine.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Optimize the existing 256 KiB proof using measured parameter packing, delta references, AoS-versus-SoA layouts, shader specialization/fusion choices, linker elimination, and payload compression. Compare encoded bytes plus decoder code and startup memory/time against raw storage. Validate CRT-free startup only if it produces a net benefit with correct compiler-helper and initialization behavior. Then freeze a deliberately restricted 20-second SDF intro: sphere/torus composition, seeded procedural material, camera curves, and tonemapping, with no audio, mesh pipeline, or hardware RT requirement. Attempt a complete single-file export of at most 65,536 bytes using offline shaders and an explicitly verified x64 packaging toolchain. Do not depend on runtime DXC or assumed x64 Crinkler support. Failure produces the smallest measured artifact and tested blockers, not a proof that 64 KiB is impossible. Preserve larger profiles: 128 KiB compact is a stretch tier, 256 KiB is the proven small-demo tier, 1 MiB is a standard production tier, and larger configured budgets remain supported.

## Scope

- Measured byte reductions and decoder selection.
- Optional freestanding player experiment.
- Restricted SDF intro and 64 KiB feasibility report.
- Archival reproduction of each compared artifact.

## Affected components

- Proposed exporter encodings
- Proposed release player/link configuration
- Proposed compression and size tooling
- Proposed SDF expression operators

## Required behavior

- Every optimization reports before/after distribution size, startup, frame time, and visual error.
- Required auxiliary files and dependencies remain counted.
- Shader validation and package bounds checks remain intact.
- The general profile remains usable when the restricted tier cannot meet its budget.

## State, configuration, and API changes

- Add explicit codec and compact-layout identifiers.
- Add a restricted intro profile with a frozen supported operator set.
- Record quantization tolerances and packaging-tool versions.

## Recovery semantics

- An unsuccessful compact export retains the prior valid artifact and byte ledger.
- Decoder corruption or expansion-limit violations fail before GPU use.
- Unsupported packers are excluded rather than changing architecture silently.

## Compatibility and safety constraints

- No uncounted compiler, SDK, soundtrack, model weights, or external asset.
- No speculative compression ratios or fixed PE-size claims.
- Executable packing must be tested on clean target Windows systems.
- No automatic switch to x86 or removal of security mitigations.

## Test plan

- Fuzz codec decoders and test worst-case expansion limits.
- Compare packed/unpacked visuals and runtime metrics.
- Reproduce artifacts from clean pinned environments.
- Run clean-system startup and dependency checks for every proposed packaging mode.

## Regressions

- Compact changes must preserve the NW-07 performance and visual contracts.
- Compression that increases total distributed bytes is rejected.
- A small-tier failure must not reduce larger-profile functionality.

## Acceptance criteria

- Publish an attributed byte ledger for the optimized proof and restricted intro.
- Either deliver a true artifact of at most 65,536 bytes or publish the minimum measured size, dominant costs, tested alternatives, and a continue/defer decision.
- Any achieved 64 KiB result runs without developer tools or additional uncounted files.
- No claim of universal 64 KiB feasibility is made from the restricted result.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 provides the measured size/performance baseline.
- Independent of NW-08 and NW-09.

Boundaries:
- 64 KiB is a research outcome, not a prerequisite for completing the engine proof.
- No custom compressor project unless existing measured options demonstrably fail.
