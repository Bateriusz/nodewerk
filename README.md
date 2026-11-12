# Nodewerk

Nodewerk is a size-conscious procedural demoscene authoring and playback system targeting Windows 11 x64 and Direct3D 12.

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
