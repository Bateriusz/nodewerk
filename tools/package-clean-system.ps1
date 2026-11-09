#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Package a verified physical NW-01 Windows build for CleanSystem execution.

.DESCRIPTION
    This command is intentionally separate from the GPU-less hosted CI build. It requires a
    Full-mode summary produced on a physical Windows 11 x64 runner whose fixtures actually
    executed. Packaging refuses to proceed unless the selected toolchain's ledgers, fixture
    validation, and artifact hashes are verified.

.PARAMETER RepoRoot
    Repository root.

.PARAMETER FullSummary
    Path to the nw01 Full-mode summary JSON produced on the physical runner.

.PARAMETER OutputRoot
    Package output root. A deploy/ and evidence/ directory are created beneath it.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),

    [Parameter(Mandatory = $true)]
    [string]$FullSummary,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $FullSummary -PathType Leaf)) {
    Write-Error "Full-mode summary not found: $FullSummary"
    exit 1
}

$summary = Get-Content -Raw -Path $FullSummary | ConvertFrom-Json
if ($summary.checks.size_ledgers.passed -ne $true) {
    Write-Error "Refusing to package: size-ledger comparison is not verified."
    exit 1
}
if ($summary.checks.fixture_validation.passed -ne $true) {
    Write-Error "Refusing to package: clang-cl fixture validation did not pass."
    exit 1
}
if ($summary.checks.msvc_fixture_validation.passed -ne $true) {
    Write-Error "Refusing to package: MSVC/LTCG fixture validation did not pass."
    exit 1
}

$toolchainManifestPath = Join-Path $RepoRoot "benchmarks/toolchain-manifest.v1.json"
$toolchain = (Get-Content -Raw $toolchainManifestPath | ConvertFrom-Json).selected_toolchain.toolchain_id
switch ($toolchain) {
    "clang-cl-lld" {
        $source = Join-Path $RepoRoot "build/repro-clean-a/deploy"
        $sourceBuildDirectory = "build/repro-clean-a"
    }
    "msvc-ltcg" {
        $source = Join-Path $RepoRoot "build/msvc-release-ltcg-clean-a/deploy"
        $sourceBuildDirectory = "build/msvc-release-ltcg-clean-a"
    }
    default {
        Write-Error "Unknown selected toolchain '$toolchain'."
        exit 1
    }
}
if (-not (Test-Path $source -PathType Container)) {
    Write-Error "Selected toolchain deploy directory is missing: $source"
    exit 1
}

$sourceCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
$sourceCommit = ([string]$sourceCommit).Trim().ToLowerInvariant()
if ($sourceCommit -notmatch '^[0-9a-f]{40}$') {
    Write-Error "Could not resolve a valid checked-out source commit for CleanSystem package provenance."
    exit 1
}
# A stale or mismatched GITHUB_SHA in a local shell must not make the package
# record a revision other than the one that was actually built. The checked-out
# HEAD is authoritative; an explicit CI SHA may only confirm it.
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_SHA)) {
    $ciSha = ([string]$env:GITHUB_SHA).Trim().ToLowerInvariant()
    if ($ciSha -notmatch '^[0-9a-f]{40}$') {
        Write-Error "GITHUB_SHA is not a full Git revision: '$($env:GITHUB_SHA)'."
        exit 1
    }
    if (-not [string]::Equals($ciSha, $sourceCommit, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Error "GITHUB_SHA '$ciSha' does not match checked-out HEAD '$sourceCommit'; refusing to label artifacts with a different revision."
        exit 1
    }
}
# Require and validate the Full summary's source revision before its verified
# checks are used to certify or package this build. A summary without a
# verifiable revision cannot prove it belongs to the checkout being built.
$summarySourceProperty = $summary.PSObject.Properties["source_commit"]
$summaryCommit = if ($null -eq $summarySourceProperty) { "" } else { ([string]$summarySourceProperty.Value).Trim().ToLowerInvariant() }
if ($summaryCommit -notmatch '^[0-9a-f]{40}$') {
    Write-Error "Full summary does not record a valid full source revision; refusing to package unverifiable build evidence."
    exit 1
}
if (-not [string]::Equals($summaryCommit, $sourceCommit, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "Full summary source_commit '$summaryCommit' does not match checked-out HEAD '$sourceCommit'; refusing stale build evidence."
    exit 1
}

$deployRoot = Join-Path $OutputRoot "deploy"
$evidenceRoot = Join-Path $OutputRoot "evidence"
New-Item -ItemType Directory -Force -Path $deployRoot | Out-Null
New-Item -ItemType Directory -Force -Path $evidenceRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $evidenceRoot "ledgers") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $evidenceRoot "reproducibility") | Out-Null

$requiredExecutables = @(
    "nw_empty_player.exe",
    "nw_clear_present_fixture.exe",
    "nw_compute_shader_fixture.exe"
)
$files = @()
foreach ($name in $requiredExecutables) {
    $sourcePath = Join-Path $source $name
    if (-not (Test-Path $sourcePath -PathType Leaf)) {
        Write-Error "Required clean-system executable is missing: $sourcePath"
        exit 1
    }
    $destinationPath = Join-Path $deployRoot $name
    Copy-Item -Force $sourcePath $destinationPath
    $item = Get-Item $destinationPath
    $files += [ordered]@{
        name = $name
        bytes = $item.Length
        sha256 = (Get-FileHash -Algorithm SHA256 $destinationPath).Hash.ToLowerInvariant()
    }
}

$manifest = [ordered]@{
    schema_version = "1"
    source_commit = $sourceCommit
    source_revision_origin = "git-checkout-head"
    source_build_directory = $sourceBuildDirectory
    toolchain = $toolchain
    purpose = "NW-01 CleanSystem physical Windows execution"
    files = $files
}
$manifest | ConvertTo-Json -Depth 4 |
    Set-Content -Encoding utf8 (Join-Path $evidenceRoot "artifact-manifest.json")

Copy-Item -Force $toolchainManifestPath (Join-Path $evidenceRoot "toolchain-manifest.v1.json")
Copy-Item -Force (Join-Path $RepoRoot "shaders/manifest.json") `
    (Join-Path $evidenceRoot "shaders-manifest.json")
$ledgerDestination = Join-Path $evidenceRoot "ledgers"
foreach ($ledger in @(Get-ChildItem -Path (Join-Path $RepoRoot "benchmarks/ledgers") -Filter "*.json" -File)) {
    Copy-Item -LiteralPath $ledger.FullName -Force -Destination $ledgerDestination
}
$reproDestination = Join-Path $evidenceRoot "reproducibility"
foreach ($repro in @(Get-ChildItem -Path (Join-Path $RepoRoot "benchmarks/reproducibility") -Filter "*.json" -File)) {
    Copy-Item -LiteralPath $repro.FullName -Force -Destination $reproDestination
}
Copy-Item -Force $FullSummary (Join-Path $evidenceRoot "build-checks.json")

Write-Host "Packaged verified $toolchain deployment at $OutputRoot"
exit 0
