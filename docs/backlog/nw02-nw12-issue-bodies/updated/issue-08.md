# NW-07 — Deliver and benchmark the 256 KiB architectural proof

## Goal

Demonstrate the complete operator-to-player path with hard size and performance gates.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Freeze a 20-second looping production using NW-06: 256 seeded torus instances, generated noise material, animated camera and twist, analytic lighting, bloom, and tonemapping. Export one Windows x64 executable with embedded package and precompiled shaders. Set a complete-distribution ceiling of 262,144 bytes, with 131,072 bytes as a stretch measurement only. Require process-start-to-first-present within 2 seconds and peak engine-committed GPU resources within 512 MiB.

Separate correctness coverage from performance certification. Correctness uses the identical common artifact on one representative compliant desktop Ada device and one representative compliant desktop Blackwell device at 1920x1080, recording exact observed hardware, capability-profile ID/digest, artifact hashes, driver/OS/toolchain, queried capabilities, cache state, and clocks/power/thermal. Representative devices qualify their generation; they do not establish equal performance or exhaustive SKU coverage. Flagship or higher-tier results do not substitute for representative generation coverage or the reference gate. Missing generation coverage remains pending.

Performance is certified only under the versioned contract `nw07_perf_ref_v1`: 1920x1080 p99 GPU frame time and p99 CPU submission time each strictly less than 16.67 ms on the designated desktop NVIDIA GeForce RTX 4060 reference. The exact reference board, CPU/host, OS build, driver, power/thermal conditions, clocks, cache conditions, resolution, quality settings, measurement tools, percentile method, and run protocol must be designated and frozen before any certifying measurement. The protocol is a five-second warm-up followed by three uncapped 60-second runs; every run must independently satisfy both strict p99 gates, and raw per-run samples and percentile methodology must be retained. Separately report displayed frame intervals. Run clean-start measurements without an application pipeline cache and distinguish cold driver-cache from warm results.

An RTX 4060 Ti, any other Ada device, or any Blackwell device may provide correctness and its own informational performance series but cannot certify `nw07_perf_ref_v1`. The historical NW-01 RTX 4060 Ti capture is not a qualifying RTX 4060 NW-07 result. Missing reference hardware leaves certification pending; it does not pass by substitution and does not block NW-01A. Maintain a separate matched-environment Blackwell development/performance series with no new absolute Blackwell gate. A replacement reference or normalized method requires a new contract version with independently justified thresholds, documented validation uncertainty, and bridge measurements where feasible; it never retroactively passes v1. Do not adopt 13.5 ms, 12.00 ms, theoretical-throughput equivalence, or an unvalidated normalization formula.

Record process RAM, driver memory, startup pipeline time, generation time, image-quality metrics, and 1440p scaling even where they have no hard ceiling. Preserve existing image-quality and resolution requirements; do not weaken them silently.

## Scope

- Canonical graph, reproducible export recipe, and release artifact.
- Separate representative common-artifact correctness coverage on desktop Ada and Blackwell from `nw07_perf_ref_v1` certification.
- Versioned performance-reference contract, reference host/protocol designation, raw samples, and pending/unverified evidence states.
- Continuous size, visual, startup, memory, and frame-time regression checks.
- Clean-system deployment and fixed-tick capture evidence.
- Architecture feasibility report.

## Affected components

- Proposed sample production
- Proposed benchmark/capture harness
- Proposed performance-reference contract record and evidence-role metadata
- Proposed CI release gates

## Required behavior

- Correctness qualification uses the identical common artifact on representative desktop Ada and Blackwell hardware; performance certification is separately evaluated only under `nw07_perf_ref_v1`.
- The artifact is produced exclusively from the graph and pinned build inputs.
- All required non-system bytes count toward the ceiling.
- A missed gate leaves the milestone incomplete; changing a budget or reference requires an explicit architecture revision and, for the performance reference, a new contract version.
- Non-reference, stale, or mismatched-host measurements remain available but cannot certify the gate.
- Missing reference hardware remains pending and cannot be converted into a pass by another SKU, a legacy label, or a theoretical ratio.
- Nightly GPU measurements complement per-change CPU/compiler/size checks.

## State, configuration, and API changes

- Freeze the proof graph and benchmark configuration.
- Store performance-contract identity (`nw07_perf_ref_v1`), reference GPU and CPU/host designation, exact metric definitions/operators, resolution/quality, warm-up and run protocol, cache conditions, matching rules, raw samples, percentile method, and result status separately from correctness-coverage metadata.
- Store metric baselines, per-device image tolerance definitions, and coverage-target/evidence-role metadata.
- Provide capture controls in a diagnostic sibling build; measure its overhead separately from release.

## Recovery semantics

- Benchmark failures retain reports and failing captures; an executed failure remains a failure.
- Absent reference hardware yields a pending result; non-reference, stale, or mismatched measurements cannot change that state.
- Corrupt packages, unsupported capabilities, and initialization failures produce actionable exit codes.
- Reference or threshold changes create a new contract version and preserve earlier results and unresolved statuses.

## Compatibility and safety constraints

- Use physical hardware or documented GPU passthrough for GPU acceptance; a generic VM or theoretical throughput ratio is insufficient.
- No developer runtime, runtime shader compiler, or uncounted optional dependency.
- Do not weaken image quality or resolution silently to pass performance.
- An exact model appears only as the explicitly justified `nw07_perf_ref_v1` performance reference or as recorded observed metadata, never as a general compatibility predicate.
- No new Blackwell absolute performance requirement is introduced because Blackwell is the current-generation target.

## Test plan

- Run clean-system execution and repeated clean-export hash comparisons.
- Capture representative ticks and compare diagnostic preview with exported-player rendering.
- Run the verified protocol on the designated `nw07_perf_ref_v1` host: five-second warm-up then three uncapped 60-second runs; each run independently must satisfy p99 GPU < 16.67 ms and p99 CPU submission < 16.67 ms, with raw samples and percentile method retained.
- Run the identical common artifact for fixed-tick correctness on representative desktop Ada and Blackwell devices; mark missing coverage pending.
- Verify that RTX 4060 Ti, other Ada, Blackwell, stale-artifact, mismatched-host, and mismatched-protocol captures remain informational or unverified and cannot certify v1.
- Exercise corrupt payload, missing-feature, resize, and device-removal failure paths.

## Regressions

- Fail absolute size/performance gates.
- A faster SKU, theoretical ratio, legacy tier label, or non-reference measurement must not certify `nw07_perf_ref_v1`.
- Flag sustained performance or memory regressions above 10 percent against a matched environment.
- Track smaller size changes without an arbitrary percentage-only failure rule.
- Changes to visual tolerances require explained evidence.
- Missing coverage must not be reported as passing, and executed failures must not be relabeled unavailable.

## Acceptance criteria

- One complete distributable is at most 262,144 bytes.
- First present occurs within 2 seconds under the specified startup protocol.
- Peak engine-committed GPU memory is at most 512 MiB.
- Under `nw07_perf_ref_v1`, 1920x1080 p99 GPU time and CPU submission time are each strictly less than 16.67 ms on the designated desktop RTX 4060 reference in each of three uncapped 60-second runs after a five-second warm-up, with raw samples and percentile methodology retained.
- The identical common artifact reproduces equivalent fixed-tick output on representative desktop Ada and Blackwell hardware without editor dependencies; missing coverage remains pending.
- The exact reference board, CPU/host, environment, protocol, artifact hashes, and result status are designated before certification; RTX 4060 Ti, other Ada, Blackwell, stale, or mismatched measurements cannot substitute.
- Missing reference hardware leaves `nw07_perf_ref_v1` certification pending without blocking NW-01A completion.

## Decomposition dependencies and boundaries

Dependencies:
- NW-06 completes the proof content.
- NW-01 measurement contracts and NW-05 export reproducibility remain mandatory.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- This closes the first implementation milestone.
- Audio, full editor UX, temporal simulation, advanced RTX features, and 64 KiB exports are later work.
- The RTX 4060 performance reference does not narrow Ada compatibility or require an exact SKU for correctness coverage.
- A replacement or normalized performance reference is a new contract, never a retroactive v1 pass.
