## Goal

Demonstrate the complete operator-to-player path with hard size and performance gates.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Freeze a 20-second looping production using NW-06: 256 seeded torus instances, generated noise material, animated camera and twist, analytic lighting, bloom, and tonemapping. Export one Windows x64 executable with embedded package and precompiled shaders. Set a complete-distribution ceiling of 262,144 bytes, with 131,072 bytes as a stretch measurement only. Require process-start-to-first-present within 2 seconds, peak engine-committed GPU resources within 512 MiB, and 1920x1080 p99 GPU frame time and CPU submission time each below 16.67 ms on a desktop RTX 4060. Measure uncapped playback over 60 seconds after a defined five-second warm-up, with three runs and recorded clocks/power conditions; separately report displayed frame intervals. Run clean-start measurements without an application pipeline cache and distinguish cold driver-cache results from warm results. Record process RAM, driver memory, startup pipeline time, generation time, and 1440p scaling even where they have no hard ceiling. Test correctness on an RTX 50 board; flagship results do not substitute for the baseline.

## Scope

- Canonical graph, reproducible export recipe, and release artifact.
- Continuous size, visual, startup, memory, and frame-time regression checks.
- Clean-system deployment and fixed-tick capture evidence.
- Architecture feasibility report.

## Affected components

- Proposed sample production
- Proposed benchmark/capture harness
- Proposed CI release gates

## Required behavior

- The artifact is produced exclusively from the graph and pinned build inputs.
- All required non-system bytes count toward the ceiling.
- A missed gate leaves the milestone incomplete; changing a budget requires an explicit architecture revision.
- Nightly GPU measurements complement per-change CPU/compiler/size checks.

## State, configuration, and API changes

- Freeze the proof graph and benchmark configuration.
- Store metric baselines and per-device image tolerance definitions.
- Provide capture controls in a diagnostic sibling build; measure its overhead separately from release.

## Recovery semantics

- Benchmark failures retain reports and failing captures.
- Absent hardware yields a pending result.
- Corrupt packages, unsupported capabilities, and initialization failures produce actionable exit codes.

## Compatibility and safety constraints

- Use physical hardware or documented GPU passthrough for GPU acceptance; a generic VM is insufficient.
- No developer runtime, runtime shader compiler, or uncounted optional dependency.
- Do not weaken image quality or resolution silently to pass performance.

## Test plan

- Run clean-system execution and repeated clean-export hash comparisons.
- Capture representative ticks and compare diagnostic preview with exported-player rendering.
- Run three timed benchmark passes on the RTX 4060 and a correctness pass on RTX 50.
- Exercise corrupt payload, missing-feature, resize, and device-removal failure paths.

## Regressions

- Fail absolute size/performance gates.
- Flag sustained performance or memory regressions above 10 percent against a matched environment.
- Track smaller size changes without an arbitrary percentage-only failure rule.
- Changes to visual tolerances require explained evidence.

## Acceptance criteria

- One complete distributable is at most 262,144 bytes.
- First present occurs within 2 seconds under the specified startup protocol.
- Peak engine-committed GPU memory is at most 512 MiB.
- 1080p p99 GPU time and CPU submission time are each below 16.67 ms on the RTX 4060.
- The same graph reproduces equivalent fixed-tick output on both target generations without editor dependencies.

## Decomposition dependencies and boundaries

Dependencies:
- NW-06 completes the proof content.
- NW-01 measurement contracts and NW-05 export reproducibility remain mandatory.

Boundaries:
- This closes the first implementation milestone.
- Audio, full editor UX, temporal simulation, advanced RTX features, and 64 KiB exports are later work.

<!-- pairallel-sections:v1 {"sections":[{"end":95,"slug":"goal","start":9},{"end":390,"slug":"source-context","start":116},{"end":1514,"slug":"final-technical-design","start":419},{"end":1766,"slug":"scope","start":1526},{"end":1885,"slug":"affected-components","start":1792},{"end":2231,"slug":"required-behavior","start":1909},{"end":2501,"slug":"state-configuration-api-changes","start":2275},{"end":2731,"slug":"recovery-semantics","start":2526},{"end":3034,"slug":"compatibility-and-safety-constraints","start":2774},{"end":3388,"slug":"test-plan","start":3050},{"end":3683,"slug":"regressions","start":3406},{"end":4095,"slug":"acceptance-criteria","start":3709},{"end":4441,"slug":"decomposition-dependencies-and-boundaries","start":4143}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":6,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:479b0778fd2e7d635ce8171c5c3a7614e2b42dfb5db3a4147b895f6e49fb0b74","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
