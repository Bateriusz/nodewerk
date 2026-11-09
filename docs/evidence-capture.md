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
`tools/nw01/toolchain-sources.json`. The exact binary identity reported by a pinned upstream
distribution is recorded in `tools/nw01/tool-version-identities.json`; for example the pinned DXC
`1.8.2407` release reports `1.8.2407.7` / `1.8.2407.12`, and any other build suffix or nearby
numeric token is rejected.

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
verified before installation. The Visual Studio Build Tools bootstrapper is validated with its
configured SHA-256 (when present) and its Authenticode signature before it is executed.
Only an Authenticode status of `Valid` is accepted; unknown/indeterminate validation results are
hard failures even when the certificate subject contains `Microsoft`. A downloaded archive that
extracts but does not report the exact pinned binary identity is a hard failure and never enters
the transient-failure fallback path.

The Visual Studio component list uses manifest placeholders (`{msvc_toolset_major_minor}`,
`{windows_sdk_build}`). They are expanded from `benchmarks/toolchain-manifest.v1.json`; a literal
component that disagrees with the manifest is rejected instead of drifting.

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
`benchmarks/toolchain-manifest.v1.json`; `vcvars_ver` selects the derived MSVC major/minor family
(for example `14.40`). Visual Studio's installed package-directory patch is not the compiler/linker
file version: the verified environment may expose `VCToolsVersion=14.40.33807` while `cl.exe`
reports `19.40.33821` and `link.exe` reports `14.40.33821.0`. The harness therefore accepts only
the required toolset family, then requires the active `cl.exe`/`link.exe` to resolve under the
selected `VCToolsInstallDir` and report the exact pinned compiler/linker versions. Doctor inspects
those actually selected executables rather than scanning other PATH candidates for an exact match.
`tools/nw01/toolchain-sources.json` only carries component templates, download locations,
integrity digests and retry policy. There is no second independent copy of the toolset or SDK
version pins.

## 3. Run Full and preserve its summary

```powershell
pwsh ./tools/nw01/run-full.ps1
```

A timestamped, invocation-identified summary JSON is preserved on **every** invocation, including
environment-activation failures, missing Python and verifier crashes without output. An explicit
`-SummaryPath` is cleared before the run so a stale file can never be re-exported, and the written
summary carries a wrapper invocation id. The wrapper returns the verifier result unchanged:

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

Paths containing spaces are supported because every harness command passes paths as native
PowerShell arguments rather than concatenating them into a shell command line.

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

Run `run-full.ps1` successfully before hardware capture. By default the capture helper reads
`selected_toolchain` from the repository toolchain manifest and binds measurement to that
toolchain's freshly built release directory (`msvc-ltcg` -> `build\msvc-release-ltcg-clean-a`,
`clang-cl-lld` -> `build\repro-clean-a`). An explicit `-BinaryDir` override is available for
diagnostic use, but the directory must contain the release fixtures.

The script invokes the existing `verify_nw01.py --mode Full --record-benchmark ... --binary-dir ...`
path. Adapter identity and artifact provenance therefore remain governed by the verifier rather
than by this wrapper.

## 5. Prepare a CleanSystem transfer package

After a successful Full build, use the Full summary:

```powershell
pwsh ./tools/nw01/prepare-clean-system.ps1 `
  -FullSummary C:\evidence\nw01-full.json
```

The command wraps `tools/package-clean-system.ps1`, records a real source commit, verifies the
package manifest, and creates a ZIP. The checked-out `git rev-parse HEAD` is authoritative. A
`GITHUB_SHA` value, when present, may only confirm that commit; a mismatch is rejected. A Full
summary from a different source revision is rejected as stale. A stale package therefore cannot
silently certify a different source revision.

Before copying any executable, packaging compares it byte-for-byte with the selected toolchain's
verified release-ledger hash and size, and with the hashes recorded by the Full summary when
present. A binary replaced after a successful Full run at the same HEAD is rejected rather than
packaged and re-hashed.

By default package output is kept outside the repository working tree.

## 6. Execute CleanSystem on an uncontaminated target

Transfer the prepared package to the CleanSystem machine. Do **not** run
`bootstrap-windows.ps1` there.

Run the CleanSystem-only entry point from the repository checkout. A stock Windows 11
target does not need PowerShell 7 installed; Windows PowerShell 5.1 is supported:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\nw01\run-clean-system.ps1 `
  -PackageRoot <package>
```

PowerShell 7 (`pwsh`) may also be used when it is already present, but it is not a CleanSystem
prerequisite and should not be installed solely for this validation.

The wrapper performs no provisioning or build-environment activation. It first requires the
package `artifact-manifest.json` source commit to match the verifier checkout HEAD exactly, so a
package prepared from an earlier source revision cannot certify a later checkout. It then rejects
obvious active SDK/DXC contamination, measures the exact Windows build plus executing NVIDIA
GPU/driver, and invokes the existing `verify_nw01.py --mode CleanSystem` path. The verifier remains
authoritative for SDK/DXC/debug-layer/redistributable absence and deployment closure. The package
is transport evidence, not authority to weaken those checks.

## 7. Render a concise evidence summary

```powershell
pwsh ./tools/nw01/evidence-summary.ps1 `
  -FullSummary C:\evidence\nw01-full.json
```

This prints source commit, selected toolchain, concrete-check state, representative RTX 40/50
coverage, CleanSystem state, and Full-baseline state. It is a convenience view derived from
verifier/benchmark artifacts and is not an independent authority. Hardware and CleanSystem
states are recomputed from the current hardware/CleanSystem files by reconciling their artifact
hashes against the selected toolchain's verified release ledgers; stale or incomplete evidence is
reported as `PENDING` with a reason. Without a supplied Full result the Full baseline is
`NOT_EVALUATED`, never `VERIFIED`. An exit-2 Full summary cannot promote stale physical evidence
to verified.

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
the source revision recorded by the supplied Full summary and CleanSystem package artifact
manifest. It rejects conflicting revisions and verifies every recorded package/ZIP deploy
executable against its size and SHA-256. Labels are `current-certifying`, `current-non-certifying`
or `historical`: a current source revision only certifies when the supplied Full summary reports
`full_baseline_passed` and the package/ZIP toolchain manifest and verified release-ledger hashes
match the deployed executables. Historical evidence is never relabelled as current certifying
evidence. Prior archive directories are preserved; `-Replace` is required for an explicit
overwrite. A supplied CleanSystem ZIP is inspected for its embedded
`evidence/artifact-manifest.json`, `build-checks.json`, toolchain manifest and ledgers; missing or
tampered deploy entries fail, and a ZIP without a usable manifest is explicitly recorded in
`unverified_artifacts` instead of receiving current certifying provenance.

## 9. Prepare repository evidence for Git

After verified evidence files have been generated:

```powershell
pwsh ./tools/nw01/prepare-evidence-commit.ps1
```

Only an explicit evidence allowlist is staged. Unexpected modified/untracked paths cause refusal;
the command never uses `git add .`, never stages build/package/ZIP artifacts, and prints staged
paths plus a diff stat. A path with both staged and unstaged changes is rejected rather than
silently replacing pre-staged content. Before the index changes, each candidate's JSON shape,
verification status and applicable provenance/hash relationship are validated. Hardware,
CleanSystem and reproducibility evidence must match the selected toolchain's verified release
ledgers; reproducibility hash pairs are tied to those ledgers rather than merely matching each
other, and `benchmarks/evidence-status.json` must reference current, validated evidence files.
For example `benchmarks/hardware/credentials.json`, an unrelated `benchmarks/hardware/rtx.json`
capture, a stale reproducibility report, or a status file referencing missing evidence is
rejected.

To create an ordinary new commit:

```powershell
pwsh ./tools/nw01/prepare-evidence-commit.ps1 -Commit
```

This never amends history, never force-pushes, and never merges. Push remains an explicit operator
action. If a merge, rebase, cherry-pick, revert or sequencer operation is in progress, the command
refuses to stage or commit.

## CI

GitHub Actions uses the same repository-owned bootstrap, environment activation, doctor, and
BuildChecks entry points as local execution. The hosted Windows job also invokes bootstrap twice to
exercise the already-correct/idempotent path. The Windows harness behavioural pytest suite is a
required gate (not `continue-on-error`); it executes bootstrap no-op reuse, exact environment
selection, download retry/fallback, DXC release identity, Authenticode failure, MSVC ownership and
archive validation in addition to source-level contract checks.

Physical CI, when `NW_PHYSICAL_RUNNER_LABEL` is configured, uses the shared Full and CleanSystem
packaging wrappers. The CleanSystem target itself remains a separate uncontaminated role.

## Current Status

Verified evidence covers Windows `10.0.26200.9711` and `10.0.26300.9818`, NVIDIA driver `610.88`,
desktop RTX 4060 Ti, and desktop RTX 5070 Ti. The actual minimum tested Windows build is
`10.0.26200.9711`; `10.0.22631.4317` remains the lower intended runtime target.

Verified release ledgers, compiler comparison, reproducibility, offline DXIL execution, hardware
captures, and CleanSystem execution remain stored under `benchmarks/` and are still validated by
the existing NW-01 schemas/verifier.
