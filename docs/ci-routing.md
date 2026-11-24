# NW-10A CI routing and ownership

NW-10A keeps one broad Ubuntu contract gate and routes expensive subsystem integration by a repository-owned impact map.

## Dependency direction

`core -> nw01 -> nw07 -> nw08/nw08a/nw10`, with NW-07 feeding NW-08, NW-08A and NW-10, and NW-08 feeding NW-08A. The implementation uses the explicit `propagates_to` graph in `ci/impact-map.v1.json`; it does not infer safety from path prefixes alone. Unknown code, CI-infrastructure changes, an empty/ambiguous diff, and an invalid impact map all expand to the full regression set.

Pull requests always start the routing job and the broad NW-01 Ubuntu contract gate. Heavy Windows jobs are conditionally selected. Pushes to `main`, scheduled runs, and manual dispatch retain full regression behavior.

## Test ownership

`ci/test-ownership.v1.json` assigns every current `tests/test_*.py` namespace to the broad Ubuntu gate and to its subsystem/platform owner. The ownership regression test rejects a new namespace until ownership is declared. NW-01 Windows no longer repeats the entire Python suite: it retains NW-01 harness, architecture/CMake, dependency/accounting, offline shader, and schema/manifest families; dedicated subsystem workflows continue to own their Windows integration suites.

## Pinned Windows bootstrap reuse

GitHub Actions caches only immutable downloaded payloads, not generated products or evidence. The cache key binds runner OS/architecture, the authoritative toolchain manifest identity, the provisioning-source manifest identity, and the pinned CMake/LLVM/DXC versions plus source SHA-256 prefixes. `bootstrap-windows.ps1` re-hashes every cached payload before using it; corrupt/stale entries are deleted and fetched again. Exact post-install compiler/linker/SDK/tool verification remains authoritative.

Visual Studio provisioning no longer requests `--includeRecommended`. It probes the explicit repository-owned component list with `vswhere` against one concrete VS 2022 instance. When a compatible VS 2022 installation already exists, the bootstrap modifies that instance in place and adds only the missing explicit components; it does not create a second partial Build Tools instance whose SDK/toolset ownership is split across side-by-side products. Hosts without a compatible VS 2022 instance fall back to a self-contained Build Tools installation with the full explicit component set. Both paths still fail unless the exact pinned MSVC/linker/SDK environment verifies afterwards.

Hosted Windows jobs use a capability-aware split rather than the moving `windows-latest` alias. Build/reproducibility work that only needs the pinned VS2022 toolchain runs on `windows-2022`, whose image already carries Visual Studio 2022, the x64 VC tools workload component, and Windows SDK 10.0.26100.0; bootstrap normally adds only the repository-pinned legacy MSVC 14.40 component. Runtime gates that execute nodewerk players keep `windows-2025`, because the product runtime floor is Windows build 22631 while the Windows Server 2022 hosted image reports build 20348. This includes NW-04 hosted WARP, NW-08 native preview/integration, and NW-08A real-player geometry; NW-07's hosted release/build job remains on `windows-2022` because it does not require the newer runtime floor. NW-01 therefore performs the compiler-dependent NW-05/NW-09 specialization and example staging on the VS2022 build job, but defers every player launch to Windows 2025. The build phase emits a same-run/current-head runtime bundle (NW-05 baseline/changed executables, the NW-09 stateful player, showcase examples, player template and pinned DXC); the runtime phase re-hashes the entire bundle, re-verifies DXC identity, and executes the original runtime behavior assertions without recompiling. Exact compiler, linker, SDK and portable-tool verification remains mandatory, so runner images and transfer artifacts are optimization inputs rather than trust boundaries.

## NW-10 in-job reuse

Correctness transfer preparation reuses the already-built package for the second hardware tier of the same packaging mode. Reuse is permitted only after format, packaging mode, common-artifact identity, executable hash, and file-manifest hashes are verified. The tier-specific transfer metadata and manifest are regenerated. Packed and unpacked modes still build independently, and no NW-07/NW-01 clean-build reproducibility check is deduplicated.

## Performance evidence

The pre-change PR #35 baseline is recorded in `benchmarks/nw10a/ci-baseline.v1.json`. The NW-10-only cold/warm targets require representative runs after this routing implementation is available from a branch or merged baseline; no synthetic timing is recorded as measured evidence.
