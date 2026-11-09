#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$SummaryPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 BuildChecks require Windows."
    exit 1
}

Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force
try {
    Enter-NwBuildEnvironment -RepoRoot $RepoRoot | Out-Null
    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $RepoRoot "artifacts\nw01" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $SummaryPath = Join-Path $base "nw01-build-checks.json"
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SummaryPath) | Out-Null
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 was not found in PATH." }

    & $python.Source (Join-Path $RepoRoot "tools\verify_nw01.py") --mode BuildChecks --summary-json $SummaryPath
    $code = $LASTEXITCODE
    if (-not (Test-Path $SummaryPath -PathType Leaf)) {
        throw "BuildChecks did not produce summary JSON (exit code $code)."
    }
    $summary = Get-Content -Raw $SummaryPath | ConvertFrom-Json
    if ($code -ne 0 -or $summary.build_checks_passed -ne $true) {
        throw "BuildChecks failed (exit code $code, concrete_failures=$($summary.concrete_failures -join ','), missing=$($summary.missing_required_checks -join ','))."
    }
    foreach ($name in @("reproducibility_build", "msvc_ltcg_build", "clang_debug_symbols", "msvc_debug_symbols", "failure_recovery")) {
        if ($summary.checks.$name.passed -ne $true) { throw "Required concrete check '$name' did not pass." }
    }
    Write-Host "[NW-BUILDCHECKS] PASS summary=$SummaryPath"
    exit 0
} catch {
    Write-Error "[NW-BUILDCHECKS] $($_.Exception.Message)"
    exit 1
}
