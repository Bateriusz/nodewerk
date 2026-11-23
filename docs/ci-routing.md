# NW-10A CI routing and ownership

NW-10A keeps one broad Ubuntu contract gate and routes expensive subsystem integration by a repository-owned impact map.

## Dependency direction

`core -> nw01 -> nw07 -> nw08/nw08a/nw10`, with NW-07 feeding NW-08, NW-08A and NW-10, and NW-08 feeding NW-08A. The implementation uses the explicit `propagates_to` graph in `ci/impact-map.v1.json`; it does not infer safety from path prefixes alone. Unknown code, CI-infrastructure changes, an empty/ambiguous diff, and an invalid impact map all expand to the full regression set.

Pull requests always start the routing job and the broad NW-01 Ubuntu contract gate. Heavy Windows jobs are conditionally selected. Pushes to `main`, scheduled runs, and manual dispatch retain full regression behavior.

## Test ownership

`ci/test-ownership.v1.json` assigns every current `tests/test_*.py` namespace to the broad Ubuntu gate and to its subsystem/platform owner. The ownership regression test rejects a new namespace until ownership is declared. NW-01 Windows no longer repeats the entire Python suite: it retains NW-01 harness, architecture/CMake, dependency/accounting, offline shader, and schema/manifest families; dedicated subsystem workflows continue to own their Windows integration suites.

## Pinned Windows bootstrap reuse

GitHub Actions caches only immutable downloaded payloads, not generated products or evidence. The cache key binds runner OS/architecture, the authoritative toolchain manifest identity, the provisioning-source manifest identity, and the pinned CMake/LLVM/DXC versions plus source SHA-256 prefixes. `bootstrap-windows.ps1` re-hashes every cached payload before using it; corrupt/stale entries are deleted and fetched again. Exact post-install compiler/linker/SDK/tool verification remains authoritative.

Visual Studio provisioning no longer requests `--includeRecommended`. It probes the explicit repository-owned component list with `vswhere`, asks the installer only for missing explicit components when possible, and still fails unless the exact pinned MSVC/linker/SDK environment verifies afterwards.

## NW-10 in-job reuse

Correctness transfer preparation reuses the already-built package for the second hardware tier of the same packaging mode. Reuse is permitted only after format, packaging mode, common-artifact identity, executable hash, and file-manifest hashes are verified. The tier-specific transfer metadata and manifest are regenerated. Packed and unpacked modes still build independently, and no NW-07/NW-01 clean-build reproducibility check is deduplicated.

## Performance evidence

The pre-change PR #35 baseline is recorded in `benchmarks/nw10a/ci-baseline.v1.json`. The NW-10-only cold/warm targets require representative runs after this routing implementation is available from a branch or merged baseline; no synthetic timing is recorded as measured evidence.
