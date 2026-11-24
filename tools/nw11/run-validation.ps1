<#
.SYNOPSIS
    One-command NW-11 physical validation launcher.

.DESCRIPTION
    Runs entirely from an immutable validation bundle.  It verifies bundle
    identities and hashes before touching measured artifacts, records the
    actual adapter/driver/OS and the queried D3D12 capability snapshot, decides
    common and optional-profile eligibility from those queries, executes the
    common/baseline, the eligible optional variant, and the separately exported
    common fallback under identical frozen settings, compares fixed-tick
    captures independently, preserves raw results, and invokes the
    repository-owned verifier before reporting anything as verified.

    Capability absence exits 3; a required query that was not answered exits 11
    (pending); initialization or execution failures exit 4.  No measured
    artifact is launched when the optional profile is unsupported or pending,
    and no effect is ever substituted.
#>
param(
    [string]$BundleRoot = "",
    [string]$ExpectedCommit = "",
    [string]$OutputDirectory = "",
    [string]$DeviceTier = "",
    [switch]$VerifyOnly,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$env:PYTHONDONTWRITEBYTECODE = "1"

if (-not $BundleRoot) {
    $BundleRoot = $PSScriptRoot
}
if (-not $DeviceTier) {
    $DeviceTier = "$env:NW11_DEVICE_TIER"
}

$knownTiers = @("desktop_ada_representative", "desktop_blackwell_representative")

function Write-Nw11Error {
    param([int]$Code, [string]$Kind, [string]$Message)
    [Console]::Error.WriteLine("[NW11-LAUNCH-ERR][exit_code=$Code][kind=$Kind] $Message")
}

function Resolve-Nw11Python {
    foreach ($candidate in @("python3", "python", "py")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            if ($candidate -eq "py") {
                return @{ Command = $command.Source; Prefix = @("-3") }
            }
            return @{ Command = $command.Source; Prefix = @() }
        }
    }
    return $null
}

function Invoke-Nw11Python {
    param(
        [hashtable]$Python,
        [string[]]$Arguments
    )
    $all = @()
    $all += $Python.Prefix
    $all += $Arguments
    & $Python.Command @all
    return $LASTEXITCODE
}

function Read-Nw11Json {
    param([string]$Path)
    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
}

function Write-Nw11Json {
    param([string]$Path, $Value)
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $Value | ConvertTo-Json -Depth 64 | Set-Content -LiteralPath $Path -Encoding utf8
}

function Get-ArtifactPath {
    param($Manifest, [string]$Role)
    $entry = $Manifest.artifacts | Where-Object { $_.role -eq $Role } | Select-Object -First 1
    if ($null -eq $entry) {
        throw "bundle has no artifact for role '$Role'"
    }
    return (Join-Path $root $entry.path)
}

function Assert-Nw11ArtifactHash {
    param($Manifest, [string]$RelativePath)
    $entry = $Manifest.artifacts | Where-Object { $_.path -eq $RelativePath } | Select-Object -First 1
    if ($null -eq $entry) {
        throw "bundle manifest does not record '$RelativePath'"
    }
    $target = Join-Path $root $RelativePath
    if (-not (Test-Path -LiteralPath $target)) {
        throw "bundle artifact '$RelativePath' is missing before execution"
    }
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne [string]$entry.sha256) {
        throw "bundle artifact '$RelativePath' changed after bundle verification"
    }
}

$root = (Resolve-Path -LiteralPath $BundleRoot).Path
if (-not $OutputDirectory) {
    $bundleLeaf = Split-Path -Leaf $root
    $OutputDirectory = Join-Path (Split-Path -Parent $root) ("nw11-evidence-" + $bundleLeaf)
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$python = Resolve-Nw11Python
if ($null -eq $python) {
    Write-Nw11Error 2 "python_runtime" "A Python 3.11+ runtime is required for the repository-owned verifier."
    exit 2
}

$verifier = Join-Path $root "tools/nw11/nw11_verify.py"
if (-not (Test-Path -LiteralPath $verifier)) {
    Write-Nw11Error 6 "bundle_identity" "The bundle does not contain tools/nw11/nw11_verify.py."
    exit 6
}

$bundleArgs = @($verifier, "bundle", "--bundle-root", $root, "--json-out", (Join-Path $OutputDirectory "bundle-verification.json"))
if ($ExpectedCommit) {
    $bundleArgs += @("--expected-commit", $ExpectedCommit)
}
$bundleCode = Invoke-Nw11Python -Python $python -Arguments $bundleArgs
if ($bundleCode -ne 0) {
    Write-Nw11Error $bundleCode "bundle_verification" "Bundle identity/hash verification failed."
    exit $bundleCode
}

$manifest = Read-Nw11Json (Join-Path $root "bundle-manifest.json")
$profilePath = Join-Path $root $manifest.capability_profile.path
$commonProfilePath = Join-Path $root "manifests/common-capability-profile.json"
$experimentPath = Join-Path $root $manifest.experiment_definition.path
foreach ($requiredManifest in @($profilePath, $commonProfilePath, $experimentPath)) {
    if (-not (Test-Path -LiteralPath $requiredManifest)) {
        Write-Nw11Error 6 "bundle_identity" "A bundle capability or experiment manifest is missing."
        exit 6
    }
}
$experiment = Read-Nw11Json $experimentPath

if ($DryRun) {
    $dry = [ordered]@{
        status = "dry_run"
        bundle_id = $manifest.bundle_id
        commit = $manifest.commit
        experiment_id = $manifest.experiment_id
        profile_id = $manifest.profile_id
        artifacts = @($manifest.artifacts | ForEach-Object { $_.path })
        policy = "No measured artifact was executed. Dry-run results never satisfy completion."
    }
    Write-Nw11Json (Join-Path $OutputDirectory "summary.json") $dry
    $dry | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputDirectory "summary.txt") -Encoding utf8
    exit 0
}

$probe = Join-Path $root "bin/nw11_capability_probe.exe"
if (-not (Test-Path -LiteralPath $probe)) {
    Write-Nw11Error 3 "capability_probe" "No capability probe is present in the bundle; capability coverage is unsupported."
    Write-Nw11Json (Join-Path $OutputDirectory "summary.json") ([ordered]@{
        status = "unsupported"
        reason = "capability_probe_missing"
        completion_satisfied = $false
    })
    exit 3
}

$capabilitySnapshot = Join-Path $OutputDirectory "capability-snapshot.json"
$probeArgs = @("--json", $capabilitySnapshot)
if ($env:NW11_ADAPTER_LUID) {
    $probeArgs += @("--adapter-luid", $env:NW11_ADAPTER_LUID)
}
& $probe @probeArgs | Out-Null
$probeCode = $LASTEXITCODE
if ($probeCode -eq 3) {
    Write-Nw11Error 3 "capability_probe" "The capability probe reported an unsupported platform or device."
    exit 3
}
if ($probeCode -ne 0 -or -not (Test-Path -LiteralPath $capabilitySnapshot)) {
    Write-Nw11Error 4 "capability_probe" "The capability probe failed to execute."
    exit 4
}

$capabilitySnapshotDocument = Read-Nw11Json $capabilitySnapshot
$adapterLuid = ""
if ($null -ne $capabilitySnapshotDocument.PSObject.Properties["adapter"]) {
    $adapterRecord = $capabilitySnapshotDocument.adapter
    if ($null -ne $adapterRecord -and $null -ne $adapterRecord.PSObject.Properties["adapter_luid"]) {
        $adapterLuid = [string]$adapterRecord.adapter_luid
    }
}

$commonEligibilityPath = Join-Path $OutputDirectory "common-eligibility.json"
$commonEligibilityCode = Invoke-Nw11Python -Python $python -Arguments @(
    $verifier, "capability",
    "--profile", $commonProfilePath,
    "--observation", $capabilitySnapshot,
    "--json-out", $commonEligibilityPath
)
if ($commonEligibilityCode -ne 0) {
    Write-Nw11Error $commonEligibilityCode "common_eligibility" "The common profile is not eligible on this device."
    exit $commonEligibilityCode
}

$eligibilityPath = Join-Path $OutputDirectory "eligibility.json"
$eligibilityCode = Invoke-Nw11Python -Python $python -Arguments @(
    $verifier, "capability",
    "--profile", $profilePath,
    "--observation", $capabilitySnapshot,
    "--json-out", $eligibilityPath
)
if ($eligibilityCode -eq 3) {
    Write-Nw11Error 3 "unsupported" "The queried D3D12 capabilities do not satisfy the optional profile; no optional artifact was executed."
    Write-Nw11Json (Join-Path $OutputDirectory "summary.json") ([ordered]@{
        status = "unsupported"
        profile_id = $manifest.profile_id
        fallback_artifact_id = $manifest.fallback_artifact_id
        completion_satisfied = $false
        policy = "No effect substitution occurred; the separate common fallback artifact remains available."
    })
    exit 3
}
if ($eligibilityCode -eq 11) {
    Write-Nw11Error 11 "pending" "A required D3D12 capability query was not answered; the optional artifact must not run."
    Write-Nw11Json (Join-Path $OutputDirectory "summary.json") ([ordered]@{
        status = "pending"
        profile_id = $manifest.profile_id
        fallback_artifact_id = $manifest.fallback_artifact_id
        completion_satisfied = $false
    })
    exit 11
}
if ($eligibilityCode -ne 0) {
    Write-Nw11Error $eligibilityCode "eligibility" "Eligibility evaluation failed."
    exit $eligibilityCode
}
$eligibility = Read-Nw11Json $eligibilityPath
$commonEligibility = Read-Nw11Json $commonEligibilityPath

if ($VerifyOnly) {
    Write-Nw11Json (Join-Path $OutputDirectory "summary.json") ([ordered]@{
        status = $eligibility.status
        bundle_id = $manifest.bundle_id
        completion_satisfied = $false
        policy = "Verification-only runs never satisfy completion."
    })
    exit 0
}

if (-not $DeviceTier) {
    Write-Nw11Error 11 "device_tier" "A qualified device tier is required; coverage stays pending."
    Write-Nw11Json (Join-Path $OutputDirectory "summary.json") ([ordered]@{
        status = "pending"
        reason = "device_tier_not_declared"
        completion_satisfied = $false
    })
    exit 11
}
if ($knownTiers -notcontains $DeviceTier) {
    Write-Nw11Error 2 "device_tier" "Unknown device tier '$DeviceTier'."
    exit 2
}

$baselineExe = Get-ArtifactPath $manifest "common_baseline"
$variantExe = Get-ArtifactPath $manifest "optional_variant"
$fallbackExe = Get-ArtifactPath $manifest "fallback_common"
foreach ($requiredExe in @($baselineExe, $variantExe, $fallbackExe)) {
    if (-not (Test-Path -LiteralPath $requiredExe)) {
        Write-Nw11Error 6 "bundle_identity" "A measured bundle executable is missing."
        exit 6
    }
}

$execution = $null
if ($null -ne $experiment.PSObject.Properties["execution"]) {
    $execution = $experiment.execution
}
if ($null -eq $execution) {
    Write-Nw11Error 2 "experiment" "The frozen experiment definition has no execution block."
    exit 2
}
$arguments = @($execution.arguments)
$runs = [int]$experiment.protocol.runs
$deviceKey = [string]$eligibility.device_key
$environmentKey = [string]$eligibility.environment_key
$referenceKind = [string]$experiment.criteria.image.reference_kind

$baselineIds = @($manifest.shader_inventory.roles.baseline)
$variantIds = @($manifest.shader_inventory.roles.variant)
$fallbackIds = @($manifest.shader_inventory.roles.fallback)
$baselineBytes = [int]$manifest.accounting.baseline_distribution_bytes
$variantBytes = [int]$manifest.accounting.variant_distribution_bytes
$fallbackBytes = [int]$manifest.accounting.fallback_distribution_bytes

$rawDirectory = Join-Path $OutputDirectory "raw"
$captureDirectory = Join-Path $OutputDirectory "captures"
New-Item -ItemType Directory -Force -Path $rawDirectory, $captureDirectory | Out-Null
$runReports = @()

foreach ($pair in @(
    @{ Role = "baseline"; Exe = $baselineExe; Artifact = "bin/common_baseline.exe"; Bytes = $baselineBytes; Ids = $baselineIds; Decision = $commonEligibilityPath },
    @{ Role = "variant"; Exe = $variantExe; Artifact = "bin/optional_variant.exe"; Bytes = $variantBytes; Ids = $variantIds; Decision = $eligibilityPath },
    @{ Role = "fallback"; Exe = $fallbackExe; Artifact = "bin/fallback_common.exe"; Bytes = $fallbackBytes; Ids = $fallbackIds; Decision = $commonEligibilityPath }
)) {
    for ($index = 1; $index -le $runs; $index++) {
        $reportPath = Join-Path $rawDirectory ("{0}-run-{1}.json" -f $pair.Role, $index)
        $captureRoot = Join-Path $captureDirectory ("{0}-run-{1}" -f $pair.Role, $index)
        $captureManifest = Join-Path $captureRoot "captures.json"
        $resolved = @()
        foreach ($argument in $arguments) {
            $resolved += $argument.Replace("{output}", $reportPath)
        }
        $resolved += @(
            "--role", $pair.Role,
            "--artifact-path", $pair.Artifact,
            "--device-tier", $DeviceTier,
            "--device-key", $deviceKey,
            "--environment-key", $environmentKey,
            "--capability-decision", $pair.Decision,
            "--reference-kind", $referenceKind,
            "--distribution-bytes", [string]$pair.Bytes,
            "--shader-ids", ($pair.Ids -join ";")
        )
        if ($index -eq $runs) {
            New-Item -ItemType Directory -Force -Path $captureRoot | Out-Null
            $resolved += @("--capture-dir", $captureRoot, "--capture-manifest", $captureManifest)
        }
        Write-Host ("[NW11] {0} run {1}/{2}: {3}" -f $pair.Role, $index, $runs, $pair.Exe)
        try {
            Assert-Nw11ArtifactHash $manifest $pair.Artifact
        } catch {
            Write-Nw11Error 6 "bundle_identity" $_.Exception.Message
            exit 6
        }
        if ($adapterLuid) {
            $resolved += @("--adapter-luid", $adapterLuid)
        }
        & $pair.Exe @resolved
        $executionCode = $LASTEXITCODE
        if ($executionCode -ne 0) {
            Write-Nw11Error 4 "execution" ("{0} run {1} exited {2}; failures are preserved, never substituted." -f $pair.Role, $index, $executionCode)
            exit 4
        }
        if (-not (Test-Path -LiteralPath $reportPath)) {
            Write-Nw11Error 4 "execution" ("{0} run {1} produced no raw report." -f $pair.Role, $index)
            exit 4
        }
        $runReports += $reportPath
    }
}

$imageResult = Join-Path $OutputDirectory "image-result.json"
$fallbackImageResult = Join-Path $OutputDirectory "fallback-image-result.json"
$baselineCapture = Join-Path $captureDirectory ("baseline-run-{0}/captures.json" -f $runs)
$variantCapture = Join-Path $captureDirectory ("variant-run-{0}/captures.json" -f $runs)
$fallbackCapture = Join-Path $captureDirectory ("fallback-run-{0}/captures.json" -f $runs)
$comparator = Join-Path $root "tools/nw11/compare-captures.py"

$compareCode = Invoke-Nw11Python -Python $python -Arguments @(
    $comparator, "--baseline-manifest", $baselineCapture,
    "--variant-manifest", $variantCapture,
    "--reference-kind", $referenceKind,
    "--out", $imageResult
)
if ($compareCode -eq 2) {
    Write-Nw11Error 2 "image_comparison" "The fixed-tick capture comparison is malformed."
    exit 2
}
$fallbackCompareCode = Invoke-Nw11Python -Python $python -Arguments @(
    $comparator, "--baseline-manifest", $baselineCapture,
    "--variant-manifest", $fallbackCapture,
    "--reference-kind", $referenceKind,
    "--out", $fallbackImageResult
)
if ($fallbackCompareCode -eq 2) {
    Write-Nw11Error 2 "image_comparison" "The fallback fixed-tick capture comparison is malformed."
    exit 2
}

$evidencePath = Join-Path $OutputDirectory "evidence.json"
$collector = Join-Path $root "tools/nw11/collect-evidence.py"
$collectArgs = @(
    $collector, "--bundle-root", $root, "--out", $evidencePath,
    "--evidence-name", "evidence.json",
    "--eligibility", $eligibilityPath,
    "--common-eligibility", $commonEligibilityPath,
    "--image-result", $imageResult,
    "--fallback-image-result", $fallbackImageResult,
    "--device-tier", $DeviceTier
)
if ($ExpectedCommit) {
    $collectArgs += @("--expected-commit", $ExpectedCommit)
}
foreach ($report in $runReports) {
    $collectArgs += @("--run-report", $report)
}
$collectCode = Invoke-Nw11Python -Python $python -Arguments $collectArgs
if ($collectCode -ne 0) {
    Write-Nw11Error $collectCode "evidence_collection" "Evidence collection failed."
    exit $collectCode
}

$decisionPath = Join-Path $OutputDirectory "decisions.json"
$verifyArgs = @(
    $verifier, "evidence",
    "--bundle-root", $root,
    "--evidence", $evidencePath,
    "--require-accepted",
    "--json-out", $decisionPath
)
if ($ExpectedCommit) {
    $verifyArgs += @("--expected-commit", $ExpectedCommit)
}
$verifyCode = Invoke-Nw11Python -Python $python -Arguments $verifyArgs
$decisions = $null
if (Test-Path -LiteralPath $decisionPath) {
    $decisions = Read-Nw11Json $decisionPath
}
$summary = [ordered]@{
    status = if ($verifyCode -eq 0) { "verified" } elseif ($verifyCode -eq 2) { "malformed" } elseif ($verifyCode -eq 3) { "unsupported" } elseif ($verifyCode -eq 4) { "failed" } elseif ($verifyCode -eq 5) { "stale" } elseif ($verifyCode -eq 6) { "identity_mismatch" } elseif ($verifyCode -eq 10) { "rejected" } elseif ($verifyCode -eq 11) { "pending" } else { "invalid" }
    bundle_id = $manifest.bundle_id
    commit = $manifest.commit
    experiment_id = $manifest.experiment_id
    profile_id = $manifest.profile_id
    device_tier = $DeviceTier
    device_key = $deviceKey
    environment_key = $environmentKey
    fallback_artifact_id = $manifest.fallback_artifact_id
    capability_snapshot = "capability-snapshot.json"
    common_eligibility = "common-eligibility.json"
    eligibility = "eligibility.json"
    evidence = "evidence.json"
    decisions = "decisions.json"
    completion_satisfied = if ($null -ne $decisions) { [bool]$decisions.satisfies_completion } else { $false }
    exit_code = $verifyCode
}
Write-Nw11Json (Join-Path $OutputDirectory "summary.json") $summary
@(
    "NW-11 validation summary",
    "status: $($summary.status)",
    "bundle: $($summary.bundle_id)",
    "commit: $($summary.commit)",
    "experiment: $($summary.experiment_id)",
    "profile: $($summary.profile_id)",
    "device_tier: $($summary.device_tier)",
    "device_key: $($summary.device_key)",
    "environment_key: $($summary.environment_key)",
    "fallback: $($summary.fallback_artifact_id)",
    "completion_satisfied: $($summary.completion_satisfied)",
    "exit_code: $($summary.exit_code)"
) | Set-Content -LiteralPath (Join-Path $OutputDirectory "summary.txt") -Encoding utf8

exit $verifyCode
