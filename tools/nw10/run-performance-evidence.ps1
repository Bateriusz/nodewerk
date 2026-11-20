#!/usr/bin/env pwsh
#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$ExpectedBranch = "agent/NW-10",
    [string]$BeforeEvidence = "build/nw07-reference-perf-evidence.json",
    [string]$BeforeCaptureId = "nw07-ref-4060ti-20261117",
    [string]$PerfContract = "build/nw07-perf-ref-bound.json",
    [string]$PerfReference = "build/nw07-reference-bound.json",
    [string]$PresentMon = "build/tools/presentmon/PresentMon-2.6.0-x64.exe",
    [string]$CaptureId = "",
    [string[]]$ForbidToken = @(),
    [switch]$Commit,
    [switch]$Push,
    [string]$CommitMessage = "NW-10: record matched RTX 4060 Ti performance evidence"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "NW-10 physical performance evidence capture is Windows-only."
}
if ($Push -and -not $Commit) {
    throw "-Push requires -Commit so only validated, explicitly staged evidence can be pushed."
}

$repo = [System.IO.Path]::GetFullPath($RepoRoot)
$originalLocation = Get-Location

function Invoke-NwNative {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureMessage
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit $LASTEXITCODE)"
    }
}

function Resolve-NwRepoPath([string]$Value) {
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repo $Value))
}

function Get-NwJsonStrings($Value) {
    if ($null -eq $Value) { return }
    if ($Value -is [string]) {
        Write-Output ([string]$Value)
        return
    }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            Get-NwJsonStrings $key
            Get-NwJsonStrings $Value[$key]
        }
        return
    }
    if ($Value -is [pscustomobject]) {
        foreach ($property in $Value.PSObject.Properties) {
            Get-NwJsonStrings $property.Name
            Get-NwJsonStrings $property.Value
        }
        return
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        foreach ($item in $Value) {
            Get-NwJsonStrings $item
        }
    }
}

function Assert-NwCleanTrackedTree {
    $lines = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "git status failed." }
    $unexpected = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $path = ([string]$line).Substring(3).Trim('"').Replace('\', '/')
        if ($path.StartsWith("build/", [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $unexpected += [string]$line
    }
    if ($unexpected.Count -gt 0) {
        throw "Working tree has non-build changes before capture: $($unexpected -join '; ')"
    }
}

function Assert-NwOnlyEvidenceChanges {
    $allowed = @(
        "benchmarks/nw10/evidence-status.v1.json",
        "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json"
    )
    $lines = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "git status failed after publication." }
    $unexpected = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $path = ([string]$line).Substring(3).Trim('"').Replace('\', '/')
        if ($path.StartsWith("build/", [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        if ($allowed -notcontains $path) {
            $unexpected += [string]$line
        }
    }
    if ($unexpected.Count -gt 0) {
        throw "Publication changed an unexpected path: $($unexpected -join '; ')"
    }
}

try {
    Set-Location $repo

    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne $ExpectedBranch) {
        throw "Expected branch '$ExpectedBranch', current branch is '$branch'."
    }
    Assert-NwCleanTrackedTree

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if ($null -eq $pythonCommand) {
        throw "Python 3 is required."
    }
    $python = $pythonCommand.Source

    $requiredFiles = @(
        $BeforeEvidence,
        $PerfContract,
        $PerfReference,
        $PresentMon,
        "tools/nw07_presentmon_collector.py",
        "tools/nw10_perf_candidate.py",
        "tools/nw10_perf_preservation.py",
        "schemas/nw10-performance-preservation.schema.json",
        "schemas/nw10-evidence-status.schema.json"
    )
    foreach ($required in $requiredFiles) {
        $resolved = Resolve-NwRepoPath $required
        if (-not (Test-Path $resolved -PathType Leaf)) {
            throw "Required file is missing: $required"
        }
    }

    Write-Host "[NW10 PERF] entering pinned MSVC/SDK/DXC environment"
    Import-Module (Join-Path $repo "tools/nw01/Nw01.Windows.psm1") -Force
    $entered = Enter-NwBuildEnvironment -RepoRoot $repo
    Write-Host "[NW10 PERF] MSVC toolset=$($entered.VisualStudio.VcVarsVersion) SDK=$($entered.VisualStudio.WindowsSdkVersion)"

    $templateRoot = Join-Path $repo "build/nw10-perf-after-template"
    $candidateRoot = Join-Path $repo "build/nw10-perf-after"
    $candidateReport = Join-Path $repo "build/nw10-perf-after-report.json"
    $samplesDir = Join-Path $repo "build/nw10-perf-after-samples"
    $rawEvidence = Join-Path $repo "build/nw10-perf-after-raw.json"
    $publicLocal = Join-Path $repo "build/nw10-perf-preservation-public.json"
    $publicTracked = Join-Path $repo "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json"
    $statusTracked = Join-Path $repo "benchmarks/nw10/evidence-status.v1.json"

    Write-Host "[NW10 PERF] rebuilding matched after candidate"
    foreach ($path in @($templateRoot, $candidateRoot, (Join-Path $repo "build/nw10-perf-after-build"))) {
        Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
    }
    foreach ($path in @($candidateReport, $rawEvidence, $publicLocal)) {
        Remove-Item $path -Force -ErrorAction SilentlyContinue
    }

    Invoke-NwNative "cmake" @(
        "--preset", "nw07-release",
        "-B", $templateRoot,
        "-DNW10_COMPACT_PACKAGE=ON",
        "-DNW_ENFORCE_PINNED_TOOLCHAIN=ON"
    ) "NW-10 after template configure failed"
    Invoke-NwNative "cmake" @(
        "--build", $templateRoot,
        "--target", "nw_procedural_image_player",
        "--parallel", "2"
    ) "NW-10 after template build failed"

    $templateExe = Join-Path $templateRoot "staging/procedural_image_player/nw_procedural_image_player.exe"
    if (-not (Test-Path $templateExe -PathType Leaf)) {
        throw "Release template executable was not produced: $templateExe"
    }
    $dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
    Invoke-NwNative $python @(
        "tools/nw10_perf_candidate.py",
        "--output", $candidateRoot,
        "--report", $candidateReport,
        "--player-template", $templateExe,
        "--dxc", $dxc
    ) "NW-10 matched after-candidate export failed"

    $beforeRecord = Get-Content -Raw (Resolve-NwRepoPath $BeforeEvidence) | ConvertFrom-Json
    $beforeCapture = @($beforeRecord.captures | Where-Object { $_.capture_id -eq $BeforeCaptureId }) | Select-Object -First 1
    if ($null -eq $beforeCapture) {
        throw "Before capture '$BeforeCaptureId' is absent from $BeforeEvidence."
    }
    $candidate = Get-Content -Raw $candidateReport | ConvertFrom-Json
    if ($beforeCapture.package_sha256 -ne $candidate.artifact.package_sha256) {
        throw "After candidate changed the canonical package; physical capture is refused."
    }
    if ($candidate.runtime_probe.status -ne "passed") {
        throw "After candidate runtime probe did not pass."
    }
    Write-Host "[NW10 PERF] SAME_PACKAGE=True RUNTIME_PROBE=passed AFTER_BYTES=$($candidate.artifact.complete_distribution_bytes)"

    if ([string]::IsNullOrWhiteSpace($CaptureId)) {
        $CaptureId = "nw10-after-4060ti-" + [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    }
    Remove-Item $samplesDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $samplesDir | Out-Null

    $privacyTokens = New-Object System.Collections.Generic.List[string]
    foreach ($token in @($env:USERNAME, $env:USERPROFILE, $ForbidToken)) {
        $value = [string]$token
        if (-not [string]::IsNullOrWhiteSpace($value) -and -not $privacyTokens.Contains($value)) {
            $privacyTokens.Add($value)
        }
    }

    $candidateExe = Join-Path $candidateRoot "nw_common_d3d12_fl12_0_sm6_0_v1.exe"
    if (-not (Test-Path $candidateExe -PathType Leaf)) {
        throw "After candidate executable is missing: $candidateExe"
    }
    $collectorCommand = "python tools/nw07_presentmon_collector.py --presentmon build/tools/presentmon/PresentMon-2.6.0-x64.exe"
    $collectArgs = @(
        "tools/nw10_perf_preservation.py", "collect-after",
        "--package", $candidateRoot,
        "--executable", $candidateExe,
        "--contract", (Resolve-NwRepoPath $PerfContract),
        "--reference", (Resolve-NwRepoPath $PerfReference),
        "--before-evidence", (Resolve-NwRepoPath $BeforeEvidence),
        "--before-capture-id", $BeforeCaptureId,
        "--candidate-report", $candidateReport,
        "--samples-dir", $samplesDir,
        "--collector-command", $collectorCommand,
        "--player-args", "--frames 0",
        "--capture-id", $CaptureId,
        "--execution-kind", "physical",
        "--raw-evidence", $rawEvidence,
        "--public-evidence", $publicLocal
    )
    foreach ($token in $privacyTokens) {
        $collectArgs += @("--forbid-token", $token)
    }

    Write-Host "[NW10 PERF] starting 5 s warmup + 3 x 60 s matched capture; do not close player windows"
    Invoke-NwNative $python $collectArgs "NW-10 physical performance capture failed"

    $publishArgs = @(
        "tools/nw10_perf_preservation.py", "publish",
        "--public-evidence", $publicLocal,
        "--destination", $publicTracked,
        "--status-file", $statusTracked
    )
    foreach ($token in $privacyTokens) {
        $publishArgs += @("--forbid-token", $token)
    }
    Invoke-NwNative $python $publishArgs "NW-10 public evidence publication failed"

    Write-Host "[NW10 PERF] validating tracked evidence schemas"
    Invoke-NwNative $python @(
        "tools/schema_validator.py",
        "--repo-root", $repo,
        "--document", $publicTracked,
        "--schema", (Join-Path $repo "schemas/nw10-performance-preservation.schema.json")
    ) "Performance evidence schema validation failed"
    Invoke-NwNative $python @(
        "tools/schema_validator.py",
        "--repo-root", $repo,
        "--document", $statusTracked,
        "--schema", (Join-Path $repo "schemas/nw10-evidence-status.schema.json")
    ) "Evidence-status schema validation failed"

    $published = Get-Content -Raw $publicTracked | ConvertFrom-Json
    if ($published.status -ne "verified_preserved") { throw "Published performance status is not verified_preserved." }
    if ($published.matched_environment -ne $true) { throw "Published environment is not matched." }
    if ($published.comparison.same_canonical_package_sha256 -ne $true) { throw "Published evidence changed the canonical package." }
    if ($published.comparison.faster_substitute_used -ne $false) { throw "Published evidence used a faster substitute." }
    if ($published.privacy.raw_samples_included -ne $false -or $published.privacy.local_paths_included -ne $false) {
        throw "Published privacy declaration is unsafe."
    }

    $strings = @(Get-NwJsonStrings $published)
    $privacyHits = New-Object System.Collections.Generic.List[string]
    foreach ($value in $strings) {
        if ($value -match '(?i)^[a-z]:\\users\\' -or $value -match '(?i)^/home/') {
            $privacyHits.Add($value)
            continue
        }
        foreach ($token in $privacyTokens) {
            if ($value.IndexOf($token, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $privacyHits.Add($value)
                break
            }
        }
    }
    if ($privacyHits.Count -gt 0) {
        throw "Independent privacy audit found forbidden public strings: $($privacyHits -join '; ')"
    }

    Invoke-NwNative "git" @("diff", "--check") "git diff --check failed"
    Assert-NwOnlyEvidenceChanges

    $status = Get-Content -Raw $statusTracked | ConvertFrom-Json
    Write-Host "[NW10 PERF] PERFORMANCE=$($published.status)"
    Write-Host "[NW10 PERF] PRIVACY=PASS"
    Write-Host "[NW10 PERF] SCHEMAS=PASS"
    Write-Host "[NW10 PERF] EVIDENCE_STATUS=$($status.evidence_status)"
    Write-Host "[NW10 PERF] RAW_EVIDENCE=$rawEvidence"
    Write-Host "[NW10 PERF] PUBLIC_EVIDENCE=$publicTracked"

    if ($Commit) {
        $stage = @(
            "benchmarks/nw10/evidence-status.v1.json",
            "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json"
        )
        Invoke-NwNative "git" (@("add", "--") + $stage) "staging NW-10 public evidence failed"
        Invoke-NwNative "git" @("diff", "--cached", "--check") "staged evidence diff check failed"
        $staged = @(& git diff --cached --name-only)
        if ($LASTEXITCODE -ne 0) { throw "Unable to inspect staged evidence." }
        $unexpectedStaged = @($staged | Where-Object { $stage -notcontains $_ })
        if ($unexpectedStaged.Count -gt 0) {
            throw "Unexpected staged paths: $($unexpectedStaged -join ', ')"
        }
        if ($staged.Count -eq 0) {
            Write-Host "[NW10 PERF] COMMIT=SKIPPED_NO_CHANGES"
        } else {
            Invoke-NwNative "git" @("commit", "-m", $CommitMessage) "NW-10 evidence commit failed"
            $commitSha = (& git rev-parse HEAD).Trim()
            Write-Host "[NW10 PERF] COMMIT=$commitSha"
            if ($Push) {
                Invoke-NwNative "git" @("push", "origin", $ExpectedBranch) "NW-10 evidence push failed"
                Write-Host "[NW10 PERF] PUSH=PASS"
            }
        }
    } else {
        Write-Host "[NW10 PERF] COMMIT=NOT_REQUESTED"
    }
}
finally {
    Set-Location $originalLocation
}
