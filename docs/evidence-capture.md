# NW-01 Measured Evidence Capture

NW-01B turns the Windows setup and evidence workflow used to complete NW-01 into a
repository-owned Windows Evidence Harness. The harness wraps the existing verifier; it does not
replace verifier semantics or make missing physical evidence pass.

The two machine roles remain separate:

- **Full host** — Windows 11 x64 with the pinned compiler/SDK/toolchain. Bootstrap is allowed here.
- **CleanSystem target** — Windows 11 x64 used to prove deployment independence. Do **not** run
  the Full-host bootstrap on this machine before CleanSystem validation.

Pinned versions are read from `benchmarks/toolchain-manifest.v1.json`. Download locations,
integrity hashes, Visual Studio component identifiers, and bounded download retry policy live in
`tools/nw01/toolchain-sources.json`.

## Required Physical Targets

| Tier | Required hardware class | Required recorded environment |
| :--- | :--- | :--- |
| `rtx_4060_desktop` | Desktop NVIDIA GeForce RTX 4060 Ti (not Laptop GPU) | Exact Windows 11 build/UBR, NVIDIA driver, WDDM version, measured CPU model, GPU architecture |
| `rtx_50_series_desktop` | Desktop NVIDIA RTX 50-series (not Laptop GPU) | Exact Windows 11 build/UBR, NVIDIA driver, WDDM version, measured CPU model, GPU architecture |

The historical tier identifiers remain verifier/API compatibility names. GPU eligibility is
capability/hardware-class based; CPU model is recorded evidence and generic host strings are not
accepted as a substitute for a measured CPU model.

## 1. Bootstrap the Full host

From an ordinary PowerShell terminal in the repository:

```powershell
pwsh ./tools/nw01/bootstrap-windows.ps1
```

The bootstrap is idempotent. It first searches for an **exact** pinned installation and reuses it.
Only missing or wrong components are provisioned. Standalone release artifacts are SHA-256
verified before installation.

For transient package-delivery failures (timeout, 429, 5xx, network/CDN failure), downloads use a
bounded retry policy. After retries are exhausted, an exact runner-provided installation may be
reused. A different version is never accepted. A completed download with a checksum, archive, or
version mismatch is a hard failure and does not enter the fallback path.

The bootstrap records whether tools were downloaded or reused from the host/runner.

## 2. Verify or enter the exact build environment

Fast preflight:

```powershell
pwsh ./tools/nw01/doctor.ps1
```

The doctor verifies CMake, Ninja, clang-cl, lld-link, DXC, MSVC compiler/linker, and Windows SDK
against the repository contract.

To open a nested PowerShell containing the exact environment selected through `vcvars64.bat`:

```powershell
pwsh ./tools/nw01/enter-build-env.ps1 -InteractiveShell
```

This explicit nested-shell model is intentional: a child PowerShell process cannot mutate the
environment of its already-running parent process.

The MSVC environment reproduces the same `vcvars64.bat` selection mechanism that completed
NW-01 on the physical RTX 4060 Ti host. The Windows SDK version is read from
`benchmarks/toolchain-manifest.v1.json`; the matching Visual Studio component and vcvars selector
are read from `tools/nw01/toolchain-sources.json`. The documentation therefore does not maintain a
second independent copy of those pins.

## 3. Run Full and preserve its summary

```powershell
pwsh ./tools/nw01/run-full.ps1
```

A timestamped summary JSON is preserved. The wrapper returns the verifier result unchanged:

```text
0 = full baseline verified
1 = concrete verification failure
2 = concrete checks passed but physical evidence incomplete
3 = required build/toolchain coverage unavailable
```

Exit `2` is never converted into a Full PASS.

For CI or scripted use an explicit path may be supplied:

```powershell
pwsh ./tools/nw01/run-full.ps1 `
  -SummaryPath C:\evidence\nw01-full.json
```

## 4. Capture physical GPU evidence

The hardware capture command measures fields that Windows can bind reliably to the executing host:
Windows build + UBR, CPU model, GPU model, NVIDIA driver, WDDM/driver version, refresh rate, and
active Windows power plan.

Settings that cannot be detected reliably remain explicit operator inputs rather than being
fabricated:

```powershell
pwsh ./tools/nw01/capture-hardware.ps1 `
  -Tier rtx_4060_desktop `
  -GpuArchitecture Ada `
  -NvidiaPowerManagementMode "Prefer maximum performance" `
  -PciePowerManagement Off `
  -DriverShaderCacheState warm_primed `
  -OsFileCacheState warm
```

Use `rtx_50_series_desktop` and the corresponding architecture/settings on the representative
RTX 50-series host.

The script invokes the existing `verify_nw01.py --mode Full --record-benchmark ...` path. Adapter
identity and artifact provenance therefore remain governed by the verifier rather than by this
wrapper.

## 5. Prepare a CleanSystem transfer package

After a successful Full build, use the Full summary:

```powershell
pwsh ./tools/nw01/prepare-clean-system.ps1 `
  -FullSummary C:\evidence\nw01-full.json
```

The command wraps `tools/package-clean-system.ps1`, records a real source commit, verifies the
package manifest, and creates a ZIP. Local execution resolves the source commit from
`git rev-parse HEAD`; CI may use its exact `GITHUB_SHA`. A stale package cannot silently certify a
different source revision.

By default package output is kept outside the repository working tree.

## 6. Execute CleanSystem on an uncontaminated target

Transfer the prepared package to the CleanSystem machine. Do **not** run
`bootstrap-windows.ps1` there.

Run the CleanSystem-only entry point from the repository checkout:

```powershell
pwsh ./tools/nw01/run-clean-system.ps1 `
  -PackageRoot <package>
```

The wrapper performs no provisioning or build-environment activation. It rejects obvious active
SDK/DXC contamination, measures the exact Windows build plus executing NVIDIA GPU/driver, and then
invokes the existing `verify_nw01.py --mode CleanSystem` path. The verifier remains authoritative
for SDK/DXC/debug-layer/redistributable absence and deployment closure. The package is transport
evidence, not authority to weaken those checks.

## 7. Render a concise evidence summary

```powershell
pwsh ./tools/nw01/evidence-summary.ps1 `
  -FullSummary C:\evidence\nw01-full.json
```

This prints source commit, selected toolchain, concrete-check state, representative RTX 40/50
coverage, CleanSystem state, and Full-baseline state. It is a convenience view derived from
verifier/benchmark artifacts and is not an independent authority.

## 8. Archive non-repository artifacts

Keep Full summaries and transfer packages outside the Git tree:

```powershell
pwsh ./tools/nw01/archive-artifacts.ps1 `
  -ArchiveRoot D:\nodewerk-evidence-archive `
  -FullSummary C:\evidence\nw01-full.json `
  -CleanSystemPackage D:\nodewerk-evidence\nw01-clean-system-<sha> `
  -CleanSystemZip D:\nodewerk-evidence\nw01-clean-system-<sha>.zip
```

The command refuses an archive root inside the repository and writes an archive manifest bound to
the source commit.

## 9. Prepare repository evidence for Git

After verified evidence files have been generated:

```powershell
pwsh ./tools/nw01/prepare-evidence-commit.ps1
```

Only an explicit evidence allowlist is staged. Unexpected modified/untracked paths cause refusal;
the command never uses `git add .`, never stages build/package/ZIP artifacts, and prints staged
paths plus a diff stat.

To create an ordinary new commit:

```powershell
pwsh ./tools/nw01/prepare-evidence-commit.ps1 -Commit
```

This never amends history, never force-pushes, and never merges. Push remains an explicit operator
action.

## CI

GitHub Actions uses the same repository-owned bootstrap, environment activation, doctor, and
BuildChecks entry points as local execution. The hosted Windows job also invokes bootstrap twice to
exercise the already-correct/idempotent path.

Physical CI, when `NW_PHYSICAL_RUNNER_LABEL` is configured, uses the shared Full and CleanSystem
packaging wrappers. The CleanSystem target itself remains a separate uncontaminated role.

## Current Status

Verified evidence covers Windows `10.0.26200.9711` and `10.0.26300.9818`, NVIDIA driver `610.88`,
desktop RTX 4060 Ti, and desktop RTX 5070 Ti. The actual minimum tested Windows build is
`10.0.26200.9711`; `10.0.22631.4317` remains the lower intended runtime target.

Verified release ledgers, compiler comparison, reproducibility, offline DXIL execution, hardware
captures, and CleanSystem execution remain stored under `benchmarks/` and are still validated by
the existing NW-01 schemas/verifier.
