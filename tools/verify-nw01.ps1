#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Nodewerk NW-01 Baseline, Reproducibility, Dependency Closure & Hardware Verification Entry Point.

.DESCRIPTION
    Executes deterministic verification for task NW-01 in one of two documented modes:

    - Full:
      1. Validates all 17 JSON baseline manifests, ledgers, hardware captures, reproducibility
         reports, clean-system reports, and preserved experiment records against schemas/*.schema.json.
      2. Verifies offline Shader Model 6.0 (cs_6_0) DXIL container integrity, absence of SM 6.6
         direct heap indexing / enhanced barriers / mesh shaders / ray tracing, and expected
         compute shader output hashes.
      3. Verifies separate 'dev', 'release', and 'compact_experiment' dependency closures,
         distribution byte accounting, and clang-cl/lld-link vs MSVC/LTCG size comparisons.
      4. Builds release fixtures twice in independent clean directories (build/repro-clean-a
         and build/repro-clean-b) with the pinned clang-cl/lld-link configuration, compares
         SHA-256 artifact hashes, and runs clear_present and compute_shader fixtures with
         D3D12 validation enabled (--validation) plus failure-recovery exit-code checks. The
         identical fixtures are also executed from the MSVC/LTCG clean build before either
         toolchain can be selected.
      5. Performs evidence-completeness checks across desktop RTX 4060, representative RTX 50-series,
         reproducibility, and clean-system reports.
      NOTE: Missing prerequisites or unverified physical hardware/clean-system evidence NEVER
      produce a passing Full baseline result. Exit code 2 means no concrete check failed but
      physical hardware/clean-system evidence is still unavailable. Exit code 3 means required
      build/toolchain coverage (including the MSVC/LTCG comparison and executable failure-recovery
      diagnostics) was skipped or unavailable; exit code 1 means a build, schema, dependency,
      reproducibility, or fixture-validation check failed.

    - CleanSystem:
      Executes and records release-fixture validation (nw_empty_player.exe,
      nw_clear_present_fixture.exe, nw_compute_shader_fixture.exe) on a clean physical
      Windows 11 x64 machine without requiring developer SDKs, DXC redistribution, or
      Agility SDK redistribution. Verifies SDK absence, inventories the complete deployment
      tree, observes loaded runtime modules, and preserves measured failure evidence.

.PARAMETER Mode
    Verification mode to execute: 'Full' or 'CleanSystem'.

.PARAMETER RepoRoot
    Repository root path (defaults to parent directory of tools/).

.PARAMETER BinaryDir
    Optional path to pre-built release binaries or a CI CleanSystem package root.

.PARAMETER EvidenceDir
    Optional explicit path to CI build evidence. When BinaryDir contains deploy/ and evidence/,
    the Python verifier discovers evidence/ automatically.

.PARAMETER RecordBenchmark
    Optional measured benchmark capture tier: 'rtx_4060_desktop' or
    'rtx_50_series_desktop'. Captures startup/CPU/GPU time, committed memory, and
    capability metrics into benchmarks/hardware before running the selected mode.

.EXAMPLE
    pwsh ./tools/verify-nw01.ps1 -Mode Full

.EXAMPLE
    pwsh ./tools/verify-nw01.ps1 -Mode CleanSystem -BinaryDir ./build/clang-cl-release

.EXAMPLE
    # Optional measured benchmark capture (requires the target physical GPU host)
    python tools/verify_nw01.py --mode Full --record-benchmark rtx_4060_desktop --binary-dir ./build/clang-cl-release/deploy
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidateSet("Full", "CleanSystem")]
    [string]$Mode = "Full",

    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),

    [Parameter(Mandatory = $false)]
    [string]$BinaryDir = "",

    [Parameter(Mandatory = $false)]
    [string]$EvidenceDir = "",

    [Parameter(Mandatory = $false)]
    [string]$OsBuild = "",

    [Parameter(Mandatory = $false)]
    [string]$GpuModel = "",

    [Parameter(Mandatory = $false)]
    [string]$NvidiaDriverVersion = "",

    [Parameter(Mandatory = $false)]
    [string]$CpuModel = "",

    [Parameter(Mandatory = $false)]
    [string]$GpuArchitecture = "",

    [Parameter(Mandatory = $false)]
    [string]$WddmDriverVersion = "",

    [Parameter(Mandatory = $false)]
    [string]$WindowsPowerPlan = "",

    [Parameter(Mandatory = $false)]
    [string]$NvidiaPowerManagementMode = "",

    [Parameter(Mandatory = $false)]
    [string]$PciePowerManagement = "",

    [Parameter(Mandatory = $false)]
    [int]$RefreshHz = 0,

    [Parameter(Mandatory = $false)]
    [ValidateSet("", "cold_cleared", "warm_primed")]
    [string]$DriverShaderCacheState = "",

    [Parameter(Mandatory = $false)]
    [ValidateSet("", "cold", "warm")]
    [string]$OsFileCacheState = "",

    [Parameter(Mandatory = $false)]
    [ValidateSet("", "rtx_4060_desktop", "rtx_50_series_desktop")]
    [string]$RecordBenchmark = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $PythonCmd = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $PythonCmd) {
    Write-Error "[NW-VERIFY-ERR] Python 3 interpreter is required but was not found in PATH."
    exit 1
}

$VerifyPy = Join-Path $RepoRoot "tools/verify_nw01.py"
if (-not (Test-Path $VerifyPy)) {
    Write-Error "[NW-VERIFY-ERR] Missing verification runner at $VerifyPy"
    exit 1
}

$CmdArgs = @($VerifyPy, "--mode", $Mode, "--repo-root", $RepoRoot)
if ($BinaryDir -ne "") {
    $CmdArgs += @("--binary-dir", $BinaryDir)
}
if ($EvidenceDir -ne "") {
    $CmdArgs += @("--evidence-dir", $EvidenceDir)
}
if ($OsBuild -ne "") {
    $CmdArgs += @("--os-build", $OsBuild)
}
if ($GpuModel -ne "") {
    $CmdArgs += @("--gpu-model", $GpuModel)
}
if ($NvidiaDriverVersion -ne "") {
    $CmdArgs += @("--nvidia-driver-version", $NvidiaDriverVersion)
}
if ($CpuModel -ne "") {
    $CmdArgs += @("--cpu-model", $CpuModel)
}
if ($GpuArchitecture -ne "") {
    $CmdArgs += @("--gpu-architecture", $GpuArchitecture)
}
if ($WddmDriverVersion -ne "") {
    $CmdArgs += @("--wddm-driver-version", $WddmDriverVersion)
}
if ($WindowsPowerPlan -ne "") {
    $CmdArgs += @("--windows-power-plan", $WindowsPowerPlan)
}
if ($NvidiaPowerManagementMode -ne "") {
    $CmdArgs += @("--nvidia-power-management-mode", $NvidiaPowerManagementMode)
}
if ($PciePowerManagement -ne "") {
    $CmdArgs += @("--pcie-power-management", $PciePowerManagement)
}
if ($RefreshHz -gt 0) {
    $CmdArgs += @("--refresh-hz", $RefreshHz)
}
if ($DriverShaderCacheState -ne "") {
    $CmdArgs += @("--driver-shader-cache-state", $DriverShaderCacheState)
}
if ($OsFileCacheState -ne "") {
    $CmdArgs += @("--os-file-cache-state", $OsFileCacheState)
}
if ($RecordBenchmark -ne "") {
    $CmdArgs += @("--record-benchmark", $RecordBenchmark)
}

& $PythonCmd.Source @CmdArgs
$ExitCode = $LASTEXITCODE
if ($ExitCode -ne 0) {
    if ($ExitCode -eq 2) {
        Write-Host "[NW-VERIFY] Mode=$Mode exited 2: concrete checks passed but physical hardware/clean-system evidence is unverified. This is not a passing full-baseline result." -ForegroundColor Yellow
    } elseif ($ExitCode -eq 3) {
        Write-Host "[NW-VERIFY] Mode=$Mode exited 3: required build/toolchain coverage was skipped or unavailable. This is not an evidence-only gap." -ForegroundColor Red
    } else {
        Write-Host "[NW-VERIFY] Mode=$Mode detected a concrete build, schema, dependency, reproducibility, or fixture-validation failure (exit code $ExitCode)." -ForegroundColor Red
    }
    exit $ExitCode
}

Write-Host "[NW-VERIFY] Mode=$Mode completed and verified successfully." -ForegroundColor Green
exit 0
