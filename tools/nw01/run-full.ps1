#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$SummaryPath = "",
    [switch]$ExportGitHubEnv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 Full verification requires Windows."
    exit 1
}

Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force
try {
    Enter-NwBuildEnvironment -RepoRoot $RepoRoot | Out-Null
    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path (Split-Path -Parent $RepoRoot) "nodewerk-evidence" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $SummaryPath = Join-Path $base "nw01-full-$stamp.json"
    } else {
        $parent = Split-Path -Parent $SummaryPath
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 was not found in PATH." }

    & $python.Source (Join-Path $RepoRoot "tools\verify_nw01.py") --mode Full --summary-json $SummaryPath
    $code = $LASTEXITCODE
    if (-not (Test-Path $SummaryPath -PathType Leaf)) {
        throw "Full verification did not produce summary JSON (exit code $code)."
    }

    if ($ExportGitHubEnv) {
        if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) { throw "GITHUB_ENV is unavailable." }
        Add-Content -Path $env:GITHUB_ENV -Value "NW_FULL_SUMMARY=$SummaryPath"
    }

    switch ($code) {
        0 { Write-Host "[NW-FULL] VERIFIED: complete NW-01 baseline. summary=$SummaryPath" }
        1 { Write-Host "[NW-FULL] FAILED: concrete verification failure. summary=$SummaryPath" }
        2 { Write-Host "[NW-FULL] INCOMPLETE: concrete checks passed but physical evidence is incomplete. summary=$SummaryPath" }
        3 { Write-Host "[NW-FULL] UNAVAILABLE: required build/toolchain coverage was unavailable. summary=$SummaryPath" }
        default { Write-Host "[NW-FULL] FAILED: unexpected verifier exit code $code. summary=$SummaryPath" }
    }
    # Preserve the verifier contract exactly. In particular, exit 2 is never converted to PASS.
    exit $code
} catch {
    Write-Error "[NW-FULL] $($_.Exception.Message)"
    exit 1
}
