# Nodewerk

Nodewerk is a size-conscious procedural demoscene authoring and playback system targeting Windows 11 x64 and Direct3D 12.

## Architecture Overview (Three-Product Split)

1. **`nodewerk-studio` (Authoring Application):** Interactive operator-graph editor and profiling host.
2. **`nodewerkc` (Whole-Demo Compiler):** Compiles a versioned typed operator graph through backend-neutral optimized expressions and scheduled GPU passes into a compact demo package and generated player link manifest, compiling shaders offline via HLSL/DXC (Shader Model 6.0).
3. **`nodewerk-player` (Specialized Standalone Player):** Exception-free, RTTI-free Windows 11 x64 Direct3D 12 standalone player linked against the generated player link manifest with platform security mitigations retained.

See [`docs/README.md`](docs/README.md) for the full NW-01 Architecture Decision Records, dependency inventory, capability and toolchain manifests, size ledgers, and verification entry points.

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

`Full` records two-clean-directory reproducibility results, validated offline DXC DXIL
evidence, measured size ledgers, and D3D12-validation fixture runs. `CleanSystem` records
SDK-absence checks and the observed GPU compute readback hash on the target machine.

`Full` exits `0` only when the complete baseline and physical evidence are verified, `2` when
concrete checks pass but RTX 4060/RTX 50-series or clean-system evidence remains unavailable,
and `1` when a build, schema, dependency, reproducibility, or fixture-validation check fails.
Measured benchmark capture (timing, committed memory, capabilities) is available through
`python tools/verify_nw01.py --record-benchmark <tier> --binary-dir <deployment-dir>`.
