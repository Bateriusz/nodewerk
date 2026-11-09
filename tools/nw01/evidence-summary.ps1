#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$FullSummary = "",
    [string]$JsonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NwPropertyValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Get-NwSelectedLedgerHashes {
    param([string]$Root, [string]$ToolchainId)
    $hashes = @{}
    $sizes = @{}
    foreach ($ledgerFile in @(Get-ChildItem -Path (Join-Path $Root "benchmarks/ledgers") -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
        $ledger = Get-Content -Raw $ledgerFile.FullName | ConvertFrom-Json
        if ((Get-NwPropertyValue $ledger "verification_status") -ne "verified") { continue }
        if ((Get-NwPropertyValue $ledger "build_profile") -ne "release") { continue }
        if ((Get-NwPropertyValue $ledger "toolchain_id") -ne $ToolchainId) { continue }
        $fixtureId = [string](Get-NwPropertyValue $ledger "fixture_id" "")
        $component = @((Get-NwPropertyValue $ledger "distributed_components" @()) |
            Where-Object { (Get-NwPropertyValue $_ "category") -eq "executable" }) | Select-Object -First 1
        if ($null -eq $component) { continue }
        $hash = [string](Get-NwPropertyValue $component "sha256" "")
        if ([string]::IsNullOrWhiteSpace($hash)) { continue }
        $hashes[$fixtureId] = $hash.ToLowerInvariant()
        $sizes[$fixtureId] = [int](Get-NwPropertyValue $component "size_bytes" 0)
    }
    return [pscustomobject]@{ Hashes = $hashes; Sizes = $sizes }
}

function Get-NwFixtureHash {
    param($Document, [string]$FixtureId)
    $provenance = Get-NwPropertyValue $Document "artifact_provenance"
    $artifactHashes = Get-NwPropertyValue $provenance "artifact_sha256"
    $hashProperty = if ($null -ne $artifactHashes) { $artifactHashes.PSObject.Properties[$FixtureId] } else { $null }
    if ($null -ne $hashProperty -and -not [string]::IsNullOrWhiteSpace([string]$hashProperty.Value)) {
        return ([string]$hashProperty.Value).ToLowerInvariant()
    }
    foreach ($result in @((Get-NwPropertyValue $Document "fixture_results" @()))) {
        if ([string](Get-NwPropertyValue $result "fixture_id" "") -eq $FixtureId) {
            $hash = [string](Get-NwPropertyValue $result "sha256" "")
            if (-not [string]::IsNullOrWhiteSpace($hash)) { return $hash.ToLowerInvariant() }
        }
    }
    return $null
}

function Test-NwProvenanceAgainstLedgers {
    param($Document, [string]$ToolchainId, $Ledgers, $RequiredFixtures)
    $provenance = Get-NwPropertyValue $Document "artifact_provenance"
    if ($null -eq $provenance) { return "document does not record artifact provenance" }
    if ([string](Get-NwPropertyValue $provenance "toolchain_id" "") -ne $ToolchainId) {
        return "artifact provenance toolchain does not match selected toolchain '$ToolchainId'"
    }
    if ([string](Get-NwPropertyValue $provenance "source" "") -ne "recorded_ledger_artifact_hash") {
        return "artifact provenance is not bound to a recorded ledger hash"
    }
    foreach ($fixture in $RequiredFixtures) {
        if (-not $Ledgers.Hashes.ContainsKey($fixture)) {
            return "selected toolchain '$ToolchainId' has no verified release-ledger hash for $fixture"
        }
        $evidenceHash = Get-NwFixtureHash -Document $Document -FixtureId $fixture
        if ([string]::IsNullOrWhiteSpace($evidenceHash)) {
            return "$fixture artifact hash is missing"
        }
        if ($evidenceHash -ne $Ledgers.Hashes[$fixture]) {
            return "$fixture artifact hash $evidenceHash is stale; expected $($Ledgers.Hashes[$fixture])"
        }
    }
    return $null
}

function Get-NwHardwareStatus {
    param([string]$Root, [string]$RelativePath, [string]$Tier, [string]$ToolchainId, $Ledgers, [string[]]$EvidenceGaps)
    $path = Join-Path $Root $RelativePath
    if (-not (Test-Path $path -PathType Leaf)) { return [pscustomobject]@{ Status = "PENDING"; Reason = "evidence file is absent" } }
    try {
        $document = Get-Content -Raw $path | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "evidence file is not valid JSON" }
    }
    if ((Get-NwPropertyValue $document "verification_status") -ne "verified" -or
        (Get-NwPropertyValue $document "benchmark_passed") -ne $true) {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "benchmark is not verified" }
    }
    if ([string](Get-NwPropertyValue $document "hardware_tier" "") -ne $Tier) {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "benchmark hardware tier does not match $Tier" }
    }
    $problem = Test-NwProvenanceAgainstLedgers -Document $document -ToolchainId $ToolchainId -Ledgers $Ledgers -RequiredFixtures @("clear_present", "compute_shader")
    if ($null -ne $problem) { return [pscustomobject]@{ Status = "PENDING"; Reason = $problem } }
    if (@((Get-NwPropertyValue $document "unavailable_measurements" @())).Count -gt 0) {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "measured metadata is incomplete" }
    }
    $environment = Get-NwPropertyValue $document "environment_metadata"
    foreach ($field in @("gpu_model", "gpu_architecture", "gpu_vendor_id", "gpu_device_id", "vram_mb", "cpu_model", "os_build", "nvidia_driver_version", "wddm_driver_version")) {
        $value = Get-NwPropertyValue $environment $field
        if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value) -or [string]$value -eq "unverified") {
            return [pscustomobject]@{ Status = "PENDING"; Reason = "measured environment metadata '$field' is missing" }
        }
    }
    $resolution = Get-NwPropertyValue $environment "resolution"
    if ($null -eq (Get-NwPropertyValue $resolution "refresh_hz")) {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "measured refresh rate is missing" }
    }
    foreach ($gap in $EvidenceGaps) {
        if ($gap -like "*$RelativePath*" -or ($Tier -eq "rtx_4060_desktop" -and $gap -like "*rtx_4060_desktop*") -or ($Tier -eq "rtx_50_series_desktop" -and $gap -like "*rtx_50_series_desktop*")) {
            return [pscustomobject]@{ Status = "PENDING"; Reason = "the supplied Full verifier still reports this evidence as unmet" }
        }
    }
    return [pscustomobject]@{ Status = "VERIFIED"; Reason = $null }
}

function Get-NwCleanSystemStatus {
    param([string]$Root, [string]$ToolchainId, $Ledgers, [string[]]$EvidenceGaps)
    $relativePath = "benchmarks/clean-system/clean-system-validation.json"
    $path = Join-Path $Root $relativePath
    if (-not (Test-Path $path -PathType Leaf)) { return [pscustomobject]@{ Status = "PENDING"; Reason = "evidence file is absent" } }
    try {
        $document = Get-Content -Raw $path | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "evidence file is not valid JSON" }
    }
    if ((Get-NwPropertyValue $document "verification_status") -ne "verified" -or
        (Get-NwPropertyValue $document "clean_system_passed") -ne $true) {
        return [pscustomobject]@{ Status = "PENDING"; Reason = "clean-system execution is not verified" }
    }
    $problem = Test-NwProvenanceAgainstLedgers -Document $document -ToolchainId $ToolchainId -Ledgers $Ledgers -RequiredFixtures @("empty_player", "clear_present", "compute_shader")
    if ($null -ne $problem) { return [pscustomobject]@{ Status = "PENDING"; Reason = $problem } }
    $absence = Get-NwPropertyValue $document "sdk_absence_checks"
    foreach ($name in @("windows_sdk_absent", "d3d12_debug_layer_absent", "dxc_redist_absent", "agility_sdk_redist_absent", "msvc_debug_crt_absent")) {
        if ((Get-NwPropertyValue $absence $name) -ne $true) {
            return [pscustomobject]@{ Status = "PENDING"; Reason = "clean-system absence check '$name' is not verified" }
        }
    }
    foreach ($gap in $EvidenceGaps) {
        if ($gap -like "*$relativePath*" -or $gap -like "*Clean-system*" -or $gap -like "*clean-system*") {
            return [pscustomobject]@{ Status = "PENDING"; Reason = "the supplied Full verifier still reports CleanSystem evidence as unmet" }
        }
    }
    return [pscustomobject]@{ Status = "VERIFIED"; Reason = $null }
}

try {
    $sourceCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    $sourceCommit = ([string]$sourceCommit).Trim()
    if ($sourceCommit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Unable to resolve repository HEAD."
    }

    $toolchainManifest = Get-Content -Raw (Join-Path $RepoRoot "benchmarks/toolchain-manifest.v1.json") | ConvertFrom-Json
    $selectedToolchain = [string]$toolchainManifest.selected_toolchain.toolchain_id
    $ledgers = Get-NwSelectedLedgerHashes -Root $RepoRoot -ToolchainId $selectedToolchain

    $fullStatus = "NOT_EVALUATED"
    $concreteStatus = "UNKNOWN"
    $fullSummarySourceCommit = $null
    $fullSummaryMatchesCheckout = $true
    $evidenceGaps = @()
    if (-not [string]::IsNullOrWhiteSpace($FullSummary)) {
        if (-not (Test-Path $FullSummary -PathType Leaf)) { throw "Full summary not found: $FullSummary" }
        $full = Get-Content -Raw $FullSummary | ConvertFrom-Json
        $fullSummarySourceCommit = ([string](Get-NwPropertyValue $full "source_commit" "")).Trim()
        if ([string]::IsNullOrWhiteSpace($fullSummarySourceCommit)) {
            $fullSummaryMatchesCheckout = $false
        } else {
            $fullSummaryMatchesCheckout = [string]::Equals(
                $fullSummarySourceCommit,
                $sourceCommit,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        }
        $concreteFailures = @((Get-NwPropertyValue $full "concrete_failures" @()))
        $missingCoverage = @((Get-NwPropertyValue $full "missing_build_coverage" @()))
        $concreteStatus = if ($concreteFailures.Count -eq 0 -and $missingCoverage.Count -eq 0) { "PASS" } else { "FAIL" }
        if (-not $fullSummaryMatchesCheckout) {
            # A summary from another revision never certifies the current checkout.
            $fullStatus = "HISTORICAL"
            $concreteStatus = "HISTORICAL"
        } elseif ((Get-NwPropertyValue $full "full_baseline_passed") -eq $true -and
                  $concreteStatus -eq "PASS") {
            $fullStatus = "VERIFIED"
        } elseif ($concreteStatus -eq "PASS") {
            $fullStatus = "INCOMPLETE"
        } else {
            $fullStatus = "FAILED"
        }
        $unmet = @((Get-NwPropertyValue $full "unmet_evidence" @()))
        if ($unmet.Count -eq 0) { $unmet = @((Get-NwPropertyValue $full "evidence_gaps" @())) }
        if ($fullSummaryMatchesCheckout) { $evidenceGaps = @($unmet) }
    }

    $rtx40 = Get-NwHardwareStatus -Root $RepoRoot -RelativePath "benchmarks/hardware/rtx-4060-baseline.json" -Tier "rtx_4060_desktop" -ToolchainId $selectedToolchain -Ledgers $ledgers -EvidenceGaps $evidenceGaps
    $rtx50 = Get-NwHardwareStatus -Root $RepoRoot -RelativePath "benchmarks/hardware/rtx-50-series-baseline.json" -Tier "rtx_50_series_desktop" -ToolchainId $selectedToolchain -Ledgers $ledgers -EvidenceGaps $evidenceGaps
    $clean = Get-NwCleanSystemStatus -Root $RepoRoot -ToolchainId $selectedToolchain -Ledgers $ledgers -EvidenceGaps $evidenceGaps

    # A supplied Full summary only certifies the checkout while the evidence it
    # depends on still reconciles against the current checkout. Reusing a
    # previously successful summary after ledger hashes or captured artifacts
    # change at the same HEAD must downgrade the Full baseline instead of
    # advertising stale physical evidence as verified.
    $reconciliationGaps = @()
    if ($rtx40.Status -ne "VERIFIED") { $reconciliationGaps += "rtx_40_representative_hardware=$($rtx40.Status)" }
    if ($rtx50.Status -ne "VERIFIED") { $reconciliationGaps += "rtx_50_representative_hardware=$($rtx50.Status)" }
    if ($clean.Status -ne "VERIFIED") { $reconciliationGaps += "clean_system=$($clean.Status)" }
    $fullBaselineReason = $null
    if ($fullStatus -eq "VERIFIED" -and $reconciliationGaps.Count -gt 0) {
        $fullStatus = "INCOMPLETE"
        $fullBaselineReason = "current evidence reconciliation is not verified: " + ($reconciliationGaps -join ", ")
    }

    $summary = [ordered]@{
        schema_version = "1"
        source_commit = $sourceCommit
        full_summary_source_commit = $fullSummarySourceCommit
        full_summary_matches_checkout = $fullSummaryMatchesCheckout
        selected_toolchain = $selectedToolchain
        concrete_checks = $concreteStatus
        rtx_40_representative_hardware = $rtx40.Status
        rtx_40_representative_hardware_reason = $rtx40.Reason
        rtx_50_representative_hardware = $rtx50.Status
        rtx_50_representative_hardware_reason = $rtx50.Reason
        clean_system = $clean.Status
        clean_system_reason = $clean.Reason
        full_baseline = $fullStatus
        full_baseline_reason = $fullBaselineReason
        authority = "Derived convenience view only; benchmark/verifier artifacts remain authoritative."
    }

    Write-Host "Source commit: $($summary.source_commit)"
    Write-Host "Selected toolchain: $($summary.selected_toolchain)"
    Write-Host "Concrete checks: $($summary.concrete_checks)"
    Write-Host "RTX 40 representative hardware: $($summary.rtx_40_representative_hardware)"
    if ($rtx40.Reason) { Write-Host "  reason: $($rtx40.Reason)" }
    Write-Host "RTX 50 representative hardware: $($summary.rtx_50_representative_hardware)"
    if ($rtx50.Reason) { Write-Host "  reason: $($rtx50.Reason)" }
    Write-Host "CleanSystem: $($summary.clean_system)"
    if ($clean.Reason) { Write-Host "  reason: $($clean.Reason)" }
    Write-Host "Full baseline: $($summary.full_baseline)"
    if ($fullBaselineReason) { Write-Host "  reason: $($fullBaselineReason)" }
    if (-not $fullSummaryMatchesCheckout) {
        Write-Host "Full summary source: $fullSummarySourceCommit (historical; does not match checkout $sourceCommit)"
    }

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
