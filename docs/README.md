# Nodewerk NW-01 Architecture Records & Baseline Documentation

This directory contains the Architecture Decision Records (ADRs), dependency inventory, build profile contracts, exit-code recovery semantics, and experiment preservation logs established in **NW-01** to retire deployment and fixed-overhead risks before building the Nodewerk engine.

## Index

1. [ADR-0001: Three-Product Architecture, Data Flow & Backend-Neutral Compiler IR](architecture/0001-three-product-architecture.md)
2. [ADR-0002: Platform Target & Direct3D 12 Capability Baseline](architecture/0002-platform-and-d3d12-baseline.md)
3. [ADR-0003: Language Rationale (C++20) & Toolchain Selection (`clang-cl`/`lld-link` vs `MSVC`/`LTCG`)](architecture/0003-language-and-toolchain-selection.md)
4. [ADR-0004: Build Profiles, Dependency Closures & CRT-Elimination Experiment Obligations](architecture/0004-dependency-closure-and-crt-experiment.md)
5. [ADR-0005: Downstream Task Contracts (NW-02 through NW-12)](architecture/0005-task-contracts-nw02-nw12.md)
6. [Dependency Inventory & Distribution Accounting Policy](dependency-inventory.md)
7. [Initialization Exit Codes, Diagnostics & Inbox-Runtime Blocker Semantics](exit-codes-and-diagnostics.md)
8. [Preserved Toolchain, Compact-Profile & CRT Experiments](experiments-and-blockers.md)
9. [Measured Evidence Capture Requirements](evidence-capture.md)

## Schemas & Recorded Baseline Artifacts

- Capability Manifest: [`benchmarks/capability-manifest.v1.json`](../benchmarks/capability-manifest.v1.json) (validated by [`schemas/capability-manifest.schema.json`](../schemas/capability-manifest.schema.json))
- Reproducible Toolchain Manifest: [`benchmarks/toolchain-manifest.v1.json`](../benchmarks/toolchain-manifest.v1.json) (validated by [`schemas/toolchain-manifest.schema.json`](../schemas/toolchain-manifest.schema.json))
- Dependency Inventory: [`benchmarks/dependency-inventory.v1.json`](../benchmarks/dependency-inventory.v1.json) (validated by [`schemas/dependency-inventory.schema.json`](../schemas/dependency-inventory.schema.json))
- Size Ledgers: [`benchmarks/ledgers/`](../benchmarks/ledgers/) (validated by [`schemas/size-ledger.schema.json`](../schemas/size-ledger.schema.json))
- Hardware Capability & Benchmark Captures: [`benchmarks/hardware/`](../benchmarks/hardware/) (validated by [`schemas/benchmark-result.schema.json`](../schemas/benchmark-result.schema.json))
- Clean-Directory Reproducibility Reports: [`benchmarks/reproducibility/release-build-reproducibility.json`](../benchmarks/reproducibility/release-build-reproducibility.json) (clang-cl/lld-link) and [`benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json`](../benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json) (MSVC/LTCG)
- Clean Physical System Validation Report: [`benchmarks/clean-system/clean-system-validation.json`](../benchmarks/clean-system/clean-system-validation.json)
