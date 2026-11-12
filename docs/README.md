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
- Upstream Measured-Evidence Status: [`benchmarks/evidence-status.json`](../benchmarks/evidence-status.json) (complete for recorded Windows/RTX captures)
