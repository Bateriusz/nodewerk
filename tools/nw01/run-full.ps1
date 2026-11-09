#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$SummaryPath = "",
    [switch]$ExportGitHubEnv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$invocationId = [guid]::NewGuid().ToString("N")
$startedAtUtc = [DateTime]::UtcNow

function Get-NwFullSourceCommit {
    param([string]$RepositoryRoot)
    try {
        $sha = (& git -C $RepositoryRoot rev-parse HEAD 2>$null | Select-Object -First 1)
        $sha = ([string]$sha).Trim()
        if ($sha -match '^[0-9a-fA-F]{40}$') { return $sha }
    } catch { }
    return $null
}

function Resolve-NwFullSummaryPath {
    param([string]$Requested, [string]$RepositoryRoot, [string]$InvocationId)
    if ([string]::IsNullOrWhiteSpace($Requested)) {
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path (Split-Path -Parent $RepositoryRoot) "nodewerk-evidence" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        return (Join-Path $base ("nw01-full-{0}-{1}.json" -f $stamp, $InvocationId.Substring(0, 8)))
    }
    $parent = Split-Path -Parent $Requested
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    return $Requested
}

function Write-NwFullSummaryFile {
    param($Summary, [string]$Path)
    $Summary | ConvertTo-Json -Depth 12 | Set-Content -Encoding utf8 -Path $Path
}

function Write-NwFullFailureSummary {
    param([string]$Path, [string]$Stage, [string]$Message)
    $report = [ordered]@{
        schema_version = "1"
        mode = "Full"
        wrapper = "run-full.ps1"
        wrapper_invocation_id = $invocationId
        started_at_utc = $startedAtUtc.ToString("o")
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
        source_commit = Get-NwFullSourceCommit -RepositoryRoot $RepoRoot
        full_baseline_passed = $false
        gate_status = "wrapper_failure"
        concrete_failures = @($Stage)
        missing_build_coverage = @()
        checks = [ordered]@{}
        wrapper_error = [ordered]@{ stage = $Stage; message = $Message }
    }
    Write-NwFullSummaryFile -Summary $report -Path $Path
}

function Export-NwFullSummaryEnvironment {
    param([string]$Path)
    if (-not $ExportGitHubEnv) { return }
    if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) { throw "GITHUB_ENV is unavailable." }
    Add-Content -Path $env:GITHUB_ENV -Value "NW_FULL_SUMMARY=$Path"
}

function Assert-NwFullSummaryWrittenByThisInvocation {
    param([string]$Path)
    if (-not (Test-Path $Path -PathType Leaf)) {
        throw "Full verification did not produce summary JSON."
    }
    $item = Get-Item $Path
    if ($item.LastWriteTimeUtc -lt $startedAtUtc.AddSeconds(-2)) {
        throw "Full summary '$Path' was not written by this invocation."
    }
    $summary = Get-Content -Raw $Path | ConvertFrom-Json
    if ($null -eq $summary) { throw "Full summary '$Path' is empty." }
    return $summary
}

# Resolve and clear the summary path before any Windows/environment/toolchain
# work so every invocation, including early failures, leaves a fresh summary
# and can never expose a stale file to the downstream CI gate.
$SummaryPath = Resolve-NwFullSummaryPath -Requested $SummaryPath -RepositoryRoot $RepoRoot -InvocationId $invocationId
$verifierCode = $null
try {
    if (Test-Path $SummaryPath -PathType Container) {
        throw "SummaryPath points at a directory: $SummaryPath"
    }
    if (Test-Path $SummaryPath -PathType Leaf) {
        Remove-Item -Force $SummaryPath
    }
} catch {
    Write-Error "[NW-FULL] $($_.Exception.Message)"
    exit 1
}

try {
    if (-not $IsWindows) {
        throw "NW-01 Full verification requires Windows."
    }

    Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force
    Enter-NwBuildEnvironment -RepoRoot $RepoRoot | Out-Null

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 was not found in PATH." }

    $verifier = Join-Path $RepoRoot "tools/verify_nw01.py"
    if (-not (Test-Path $verifier -PathType Leaf)) { throw "Verifier not found: $verifier" }

    & $python.Source $verifier --mode Full --summary-json $SummaryPath
    $code = $LASTEXITCODE
    $verifierCode = $code
    $summary = Assert-NwFullSummaryWrittenByThisInvocation -Path $SummaryPath

    # Annotate the verifier summary with wrapper provenance. The invocation id
    # proves the exported path belongs to this run rather than a stale file.
    $annotated = [ordered]@{}
    foreach ($property in $summary.PSObject.Properties) {
        $annotated[$property.Name] = $property.Value
    }
    $annotated["wrapper"] = "run-full.ps1"
    $annotated["wrapper_invocation_id"] = $invocationId
    $annotated["wrapper_started_at_utc"] = $startedAtUtc.ToString("o")
    $annotated["wrapper_completed_at_utc"] = [DateTime]::UtcNow.ToString("o")
    Write-NwFullSummaryFile -Summary $annotated -Path $SummaryPath

    Export-NwFullSummaryEnvironment -Path $SummaryPath

    switch ($code) {
        0 { Write-Host "[NW-FULL] VERIFIED: complete NW-01 baseline. summary=$SummaryPath" }
        1 { Write-Host "[NW-FULL] FAILED: concrete verification failure. summary=$SummaryPath" }
        2 { Write-Host "[NW-FULL] INCOMPLETE: concrete checks passed but physical evidence is incomplete. summary=$SummaryPath" }
        3 { Write-Host "[NW-FULL] UNAVAILABLE: required build/toolchain coverage was unavailable. summary=$SummaryPath" }
        default { Write-Host "[NW-FULL] FAILED: unexpected verifier exit code $code. summary=$SummaryPath" }
    }
    # Preserve the verifier contract exactly. In particular, exit 2 is never
    # converted into a passing full-verification result.
    exit $code
} catch {
    $message = $_.Exception.Message
    try {
        Write-NwFullFailureSummary -Path $SummaryPath -Stage "wrapper" -Message $message
        Export-NwFullSummaryEnvironment -Path $SummaryPath
    } catch {
        Write-Warning "[NW-FULL] unable to persist a failure summary at '$SummaryPath': $($_.Exception.Message)"
    }
    # A verifier that claimed success but produced no usable summary is a
    # wrapper-side failure (exit 1); preserve only the verifier's non-zero
    # 1/2/3 contract on the failure path.
    $failureExitCode = if ($null -ne $verifierCode -and $verifierCode -ge 1 -and $verifierCode -le 3) {
        [int]$verifierCode
    } else {
        1
    }
    Write-Error "[NW-FULL] $message" -ErrorAction Continue
    exit $failureExitCode
}
