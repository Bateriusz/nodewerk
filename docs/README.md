# Nodewerk NW-01 Architecture Records & Baseline Documentation

This directory contains the Architecture Decision Records (ADRs), dependency
inventory, build profile contracts, exit-code recovery semantics, and experiment
preservation logs established in **NW-01**, plus the **NW-02** typed operator
graph semantics and `.nwgraph` schema.

## Index

1. [ADR-0001: Three-Product Architecture, Data Flow & Backend-Neutral Compiler IR](architecture/0001-three-product-architecture.md)
2. [ADR-0002: Platform Target & Direct3D 12 Capability Baseline](architecture/0002-platform-and-d3d12-baseline.md)
3. [ADR-0003: Language Rationale (C++20) & Toolchain Selection (`clang-cl`/`lld-link` vs `MSVC`/`LTCG`)](architecture/0003-language-and-toolchain-selection.md)
4. [ADR-0004: Build Profiles, Dependency Closures & CRT-Elimination Experiment Obligations](architecture/0004-dependency-closure-and-crt-experiment.md)
5. [ADR-0005: Downstream Task Contracts (NW-02 through NW-12)](architecture/0005-task-contracts-nw02-nw12.md)
6. [ADR-0006: NW-02 Versioned Typed Operator Graph Semantics](architecture/0006-nw02-typed-graph-semantics.md)
7. [`.nwgraph` Version 1 Textual Schema](nwgraph-format-v1.md)
8. [Dependency Inventory & Distribution Accounting Policy](dependency-inventory.md)
9. [Initialization Exit Codes, Diagnostics & Inbox-Runtime Blocker Semantics](exit-codes-and-diagnostics.md)
10. [Preserved Toolchain, Compact-Profile & CRT Experiments](experiments-and-blockers.md)
11. [Measured Evidence Capture Requirements](evidence-capture.md)
12. [NW-03 Compiler-to-Player Contract](nw03-compiler-player-contract.md)
13. [`.nwp` Version 1 Container Contract](nwp-format-v1.md)
14. [NW-04 Package Player Runtime](nw04-player-runtime.md)
15. [NW-05 Typed-Graph Export and Procedural-Image Path](nw05-typed-graph-export.md)
16. [NW-06 Procedural Scene, Timeline & Fixed-Tick Evidence](nw06-procedural-scene.md)
17. [NW-07 256 KiB Architectural Proof — Feasibility Report](nw07-256k-architectural-proof.md)
18. [ADR-0007: NW-08 Authoring Editor Scope and Release Isolation](architecture/0007-nw08-authoring-editor.md)
19. [NW-08 `nodewerk-studio` Authoring Editor](nw08-studio-authoring-editor.md)
20. [NW-08A Presentation Geometry Correction and Windows Validation](nw08a-presentation-geometry-validation.md)
21. [ADR-0008: NW-09 Explicit Temporal State Extension](architecture/0008-nw09-temporal-state.md)
22. [NW-09 Explicit Temporal State (Particles and Feedback)](nw09-temporal-state.md)
23. [NW-10 Compact Export, Restricted Intro & Measured 64 KiB Attempt](nw10-compact-export.md)
24. [NW-11 Advanced GPU Feature Experiments](nw11-advanced-gpu-experiments.md)
25. [NW-11 Bounded Follow-Up Matrix](nw11-follow-up-matrix.md)

## Schemas & Recorded Baseline Artifacts

- `.nwgraph` v1 JSON representation: [`schemas/nwgraph-v1.schema.json`](../schemas/nwgraph-v1.schema.json) (textual grammar: [`docs/nwgraph-format-v1.md`](nwgraph-format-v1.md))
- `.nwp` v1 binary contract and inspection schema:
  [`docs/nwp-format-v1.md`](nwp-format-v1.md),
  [`schemas/nwp-v1.schema.json`](../schemas/nwp-v1.schema.json)
- Capability Manifest: [`benchmarks/capability-manifest.v1.json`](../benchmarks/capability-manifest.v1.json) (validated by [`schemas/capability-manifest.schema.json`](../schemas/capability-manifest.schema.json))
- Reproducible Toolchain Manifest: [`benchmarks/toolchain-manifest.v1.json`](../benchmarks/toolchain-manifest.v1.json) (validated by [`schemas/toolchain-manifest.schema.json`](../schemas/toolchain-manifest.schema.json))
- Dependency Inventory: [`benchmarks/dependency-inventory.v1.json`](../benchmarks/dependency-inventory.v1.json) (validated by [`schemas/dependency-inventory.schema.json`](../schemas/dependency-inventory.schema.json))
- Size Ledgers: [`benchmarks/ledgers/`](../benchmarks/ledgers/) (validated by [`schemas/size-ledger.schema.json`](../schemas/size-ledger.schema.json))
- Hardware Capability & Benchmark Captures: [`benchmarks/hardware/`](../benchmarks/hardware/) (validated by [`schemas/benchmark-result.schema.json`](../schemas/benchmark-result.schema.json))
- Clean-Directory Reproducibility Reports: [`benchmarks/reproducibility/release-build-reproducibility.json`](../benchmarks/reproducibility/release-build-reproducibility.json) (clang-cl/lld-link) and [`benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json`](../benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json) (MSVC/LTCG)
- Clean Physical System Validation Report: [`benchmarks/clean-system/clean-system-validation.json`](../benchmarks/clean-system/clean-system-validation.json)
- NW-04 Player Hardware Evidence: [`benchmarks/nw04-hardware-evidence.json`](../benchmarks/nw04-hardware-evidence.json) (validated by [`schemas/nw04-player-evidence.schema.json`](../schemas/nw04-player-evidence.schema.json))
- NW-05 Alias/GPU Evidence: [`benchmarks/nw05-hardware-evidence.json`](../benchmarks/nw05-hardware-evidence.json) (validated by [`schemas/nw05-hardware-evidence.schema.json`](../schemas/nw05-hardware-evidence.schema.json)); remains pending until identical-artifact Ada/Blackwell execution is recorded.
- NW-05 Link Manifest, Export Report & Size Attribution Schemas: [`schemas/nw05-link-manifest.schema.json`](../schemas/nw05-link-manifest.schema.json), [`schemas/nw05-export-report.schema.json`](../schemas/nw05-export-report.schema.json), [`schemas/nw05-size-report.schema.json`](../schemas/nw05-size-report.schema.json)
- NW-06 Fixed-Tick Hardware Evidence: [`benchmarks/nw06-hardware-evidence.json`](../benchmarks/nw06-hardware-evidence.json) (validated by [`schemas/nw06-hardware-evidence.schema.json`](../schemas/nw06-hardware-evidence.schema.json)); pending until identical-artifact Ada/Blackwell captures are recorded.
- NW-06 Operator Contract Schema: [`schemas/nw06-operator-contracts.schema.json`](../schemas/nw06-operator-contracts.schema.json) with [`schemas/nw06-operator-contracts.v1.json`](../schemas/nw06-operator-contracts.v1.json)
- NW-07 Canonical Production: [`benchmarks/nw07/canonical-production.v1.json`](../benchmarks/nw07/canonical-production.v1.json) (validated by [`schemas/nw07-production-config.schema.json`](../schemas/nw07-production-config.schema.json)); graph [`fixtures/nw07/production_loop.nwgraph`](../fixtures/nw07/production_loop.nwgraph)
- NW-07 Performance Reference Contract: [`benchmarks/nw07/perf-ref-v1.json`](../benchmarks/nw07/perf-ref-v1.json) (validated by [`schemas/nw07-perf-ref.schema.json`](../schemas/nw07-perf-ref.schema.json)); methodology frozen, reference-host binding pending
- NW-07 Metric Baselines & Per-Device Tolerances: [`benchmarks/nw07/metric-baselines.v1.json`](../benchmarks/nw07/metric-baselines.v1.json) (validated by [`schemas/nw07-metric-baselines.schema.json`](../schemas/nw07-metric-baselines.schema.json))
- NW-07 Correctness/Performance Evidence: [`benchmarks/nw07/hardware-evidence.json`](../benchmarks/nw07/hardware-evidence.json) (validated by [`schemas/nw07-hardware-evidence.schema.json`](../schemas/nw07-hardware-evidence.schema.json)); pending until the final artifact is captured on representative Ada/Blackwell hardware and the matching RTX 4060 Ti reference host
- NW-07 Export Report Schema: [`schemas/nw07-export-report.schema.json`](../schemas/nw07-export-report.schema.json)
- NW-07 Clean-System Report Schema: [`schemas/nw07-clean-system-report.schema.json`](../schemas/nw07-clean-system-report.schema.json)
- NW-07 Diagnostic Sibling-Build Overhead: [`benchmarks/nw07/diagnostic-overhead.v1.json`](../benchmarks/nw07/diagnostic-overhead.v1.json) (validated by [`schemas/nw07-diagnostic-overhead.schema.json`](../schemas/nw07-diagnostic-overhead.schema.json)); pending separate diagnostic measurement
- NW-08 Studio Report Schema: [`schemas/nw08-studio-report.schema.json`](../schemas/nw08-studio-report.schema.json)
- NW-08 Verification Summary Schema: [`schemas/nw08-verification.schema.json`](../schemas/nw08-verification.schema.json)
- NW-09 Checkpoint Schema: [`schemas/nw09-checkpoint.schema.json`](../schemas/nw09-checkpoint.schema.json)
- NW-09 Export Report Schema: [`schemas/nw09-export-report.schema.json`](../schemas/nw09-export-report.schema.json)
- NW-09 Cross-Device Temporal-State Evidence: [`benchmarks/nw09/hardware-evidence.json`](../benchmarks/nw09/hardware-evidence.json) (validated by [`schemas/nw09-hardware-evidence.schema.json`](../schemas/nw09-hardware-evidence.schema.json)); pending until identical stateful artifacts are captured on representative Ada/Blackwell hardware.
- NW-10 Compact Evidence: [`benchmarks/nw10/compact-proof.v1.json`](../benchmarks/nw10/compact-proof.v1.json) (validated by [`schemas/nw10-compact-evidence.schema.json`](../schemas/nw10-compact-evidence.schema.json)); contract-only until the exact Windows x64 artifact and reference capture are available
- NW-10 Restricted Intro Profile: [`benchmarks/nw10/intro-profile.v1.json`](../benchmarks/nw10/intro-profile.v1.json) (validated by [`schemas/nw10-intro-profile.schema.json`](../schemas/nw10-intro-profile.schema.json))
- NW-10 Evidence Status: [`benchmarks/nw10/evidence-status.v1.json`](../benchmarks/nw10/evidence-status.v1.json) (validated by [`schemas/nw10-evidence-status.schema.json`](../schemas/nw10-evidence-status.schema.json))
- Upstream Measured-Evidence Status: [`benchmarks/evidence-status.json`](../benchmarks/evidence-status.json) (complete for recorded Windows/RTX captures)
- NW-11 Capability Profiles: [`benchmarks/nw11/profiles/`](../benchmarks/nw11/profiles/) (validated by [`schemas/nw11-capability-profile.schema.json`](../schemas/nw11-capability-profile.schema.json))
- NW-11 Frozen Experiments: [`benchmarks/nw11/experiments/`](../benchmarks/nw11/experiments/) (validated by [`schemas/nw11-experiment.schema.json`](../schemas/nw11-experiment.schema.json))
- NW-11 Evidence Status and Decisions: [`benchmarks/nw11/evidence-status.v1.json`](../benchmarks/nw11/evidence-status.v1.json), [`benchmarks/nw11/decisions/`](../benchmarks/nw11/decisions/) (validated by [`schemas/nw11-evidence-status.schema.json`](../schemas/nw11-evidence-status.schema.json) and [`schemas/nw11-decision.schema.json`](../schemas/nw11-decision.schema.json))
- NW-11 Follow-Up Matrix: [`benchmarks/nw11/follow-up-matrix.v1.json`](../benchmarks/nw11/follow-up-matrix.v1.json) (validated by [`schemas/nw11-follow-up-matrix.schema.json`](../schemas/nw11-follow-up-matrix.schema.json))
- NW-11 Bundle and Evidence Schemas: [`schemas/nw11-bundle-manifest.schema.json`](../schemas/nw11-bundle-manifest.schema.json), [`schemas/nw11-evidence.schema.json`](../schemas/nw11-evidence.schema.json)

- [ADR 0009 — Validation and evidence lifecycle](architecture/0009-validation-and-evidence-lifecycle.md)
