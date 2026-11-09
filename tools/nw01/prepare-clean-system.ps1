#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$FullSummary,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$OutputRoot = "",
    [string]$ZipPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 CleanSystem package preparation requires the Windows Full-build host."
    exit 1
}

try {
    $sha = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    $sha = ([string]$sha).Trim()
    if ($sha -notmatch '^[0-9a-fA-F]{40}$') { throw "Unable to resolve repository HEAD for package provenance." }
    if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_SHA)) {
        $ciSha = ([string]$env:GITHUB_SHA).Trim()
        if ($ciSha -notmatch '^[0-9a-fA-F]{40}$') {
            throw "GITHUB_SHA is not a full Git revision: '$($env:GITHUB_SHA)'."
        }
        if (-not [string]::Equals($ciSha, $sha, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "GITHUB_SHA '$ciSha' does not match checked-out HEAD '$sha'; refusing to package a different revision."
        }
    }
    if (-not (Test-Path $FullSummary -PathType Leaf)) { throw "Full-mode summary not found: $FullSummary" }
    $fullSummaryData = Get-Content -Raw $FullSummary | ConvertFrom-Json
    $fullSourceProperty = $fullSummaryData.PSObject.Properties["source_commit"]
    $fullCommit = if ($null -eq $fullSourceProperty) { "" } else { ([string]$fullSourceProperty.Value).Trim() }
    if ($fullCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Full summary does not record a valid full source revision; refusing to package unverifiable verification evidence."
    }
    if (-not [string]::Equals($fullCommit, $sha, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Full summary source_commit '$fullCommit' does not match checked-out HEAD '$sha'; refusing stale verification evidence."
    }
    if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path (Split-Path -Parent $RepoRoot) "nodewerk-evidence" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $OutputRoot = Join-Path $base "nw01-clean-system-$($sha.Substring(0,12))-$stamp"
    }
    if ([string]::IsNullOrWhiteSpace($ZipPath)) { $ZipPath = "$OutputRoot.zip" }

    if (Test-Path $OutputRoot) { throw "Refusing to overwrite existing CleanSystem output: $OutputRoot" }
    if (Test-Path $ZipPath) { throw "Refusing to overwrite existing CleanSystem ZIP: $ZipPath" }
    & (Join-Path $RepoRoot "tools\package-clean-system.ps1") -RepoRoot $RepoRoot -FullSummary $FullSummary -OutputRoot $OutputRoot
    if ($LASTEXITCODE -ne 0) { throw "package-clean-system.ps1 failed with exit code $LASTEXITCODE." }

    $manifestPath = Join-Path $OutputRoot "evidence\artifact-manifest.json"
    if (-not (Test-Path $manifestPath -PathType Leaf)) { throw "CleanSystem package manifest is missing." }
    $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
    $expectedSource = $sha
    if ([string]$manifest.source_commit -ne [string]$expectedSource) {
        throw "Package source_commit '$($manifest.source_commit)' does not match expected '$expectedSource'."
    }

    Compress-Archive -Path (Join-Path $OutputRoot '*') -DestinationPath $ZipPath -CompressionLevel Optimal
    Write-Host "[NW-CLEANSYSTEM] package=$OutputRoot"
    Write-Host "[NW-CLEANSYSTEM] zip=$ZipPath"
    Write-Host "[NW-CLEANSYSTEM] source_commit=$expectedSource"
    exit 0
} catch {
    Write-Error "[NW-CLEANSYSTEM] $($_.Exception.Message)"
    exit 1
}
