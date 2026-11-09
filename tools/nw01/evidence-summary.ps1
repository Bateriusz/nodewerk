#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$FullSummary = "",
    [string]$JsonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try {
    $sourceCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Unable to resolve repository HEAD."
    }

    $toolchainManifest = Get-Content -Raw (Join-Path $RepoRoot "benchmarks/toolchain-manifest.v1.json") | ConvertFrom-Json
    $status = Get-Content -Raw (Join-Path $RepoRoot "benchmarks/evidence-status.json") | ConvertFrom-Json
    $items = @{}
    foreach ($item in $status.required_measured_evidence) {
        $items[[string]$item.item] = [string]$item.status
    }

    $fullStatus = if ($status.complete -eq $true -and $status.verification_status -eq "verified") { "VERIFIED" } else { "INCOMPLETE" }
    $concreteStatus = "UNKNOWN"
    if (-not [string]::IsNullOrWhiteSpace($FullSummary)) {
        if (-not (Test-Path $FullSummary -PathType Leaf)) { throw "Full summary not found: $FullSummary" }
        $full = Get-Content -Raw $FullSummary | ConvertFrom-Json
        $concreteStatus = if (@($full.concrete_failures).Count -eq 0 -and @($full.missing_build_coverage).Count -eq 0) { "PASS" } else { "FAIL" }
        if ($full.full_baseline_passed -eq $true) { $fullStatus = "VERIFIED" }
        elseif ($concreteStatus -eq "PASS") { $fullStatus = "INCOMPLETE" }
        else { $fullStatus = "FAILED" }
    }

    $summary = [ordered]@{
        schema_version = "1"
        source_commit = $sourceCommit
        selected_toolchain = [string]$toolchainManifest.selected_toolchain.toolchain_id
        concrete_checks = $concreteStatus
        rtx_40_representative_hardware = $(if ($items["desktop-rtx-4060-ti-baseline"] -eq "verified") { "VERIFIED" } else { "PENDING" })
        rtx_50_representative_hardware = $(if ($items["desktop-rtx-50-series-baseline"] -eq "verified") { "VERIFIED" } else { "PENDING" })
        clean_system = $(if ($items["clean-system-execution"] -eq "verified") { "VERIFIED" } else { "PENDING" })
        full_baseline = $fullStatus
        authority = "Derived convenience view only; benchmark/verifier artifacts remain authoritative."
    }

    Write-Host "Source commit: $($summary.source_commit)"
    Write-Host "Selected toolchain: $($summary.selected_toolchain)"
    Write-Host "Concrete checks: $($summary.concrete_checks)"
    Write-Host "RTX 40 representative hardware: $($summary.rtx_40_representative_hardware)"
    Write-Host "RTX 50 representative hardware: $($summary.rtx_50_representative_hardware)"
    Write-Host "CleanSystem: $($summary.clean_system)"
    Write-Host "Full baseline: $($summary.full_baseline)"

    if (-not [string]::IsNullOrWhiteSpace($JsonPath)) {
        $parent = Split-Path -Parent $JsonPath
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        $summary | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $JsonPath
    }
    exit 0
} catch {
    Write-Error "[NW-EVIDENCE-SUMMARY] $($_.Exception.Message)"
    exit 1
}
