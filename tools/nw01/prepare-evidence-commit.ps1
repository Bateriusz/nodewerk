#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [switch]$Commit,
    [string]$CommitMessage = "NW-01: record verified physical evidence"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Explicit recognition, not a directory-wide JSON wildcard. A file such as
# benchmarks/hardware/credentials.json or an unrelated stale capture is never an
# evidence candidate.
$allowed = @(
    '^benchmarks/hardware/rtx-4060-baseline\.json$',
    '^benchmarks/hardware/rtx-50-series-baseline\.json$',
    '^benchmarks/clean-system/clean-system-validation\.json$',
    '^benchmarks/reproducibility/release-build-reproducibility\.json$',
    '^benchmarks/reproducibility/msvc-ltcg-release-reproducibility\.json$',
    '^benchmarks/ledgers/(empty-player|clear-present|compute-fixture)-(msvc-ltcg|clang-cl)-release\.json$',
    '^benchmarks/toolchain-manifest\.v1\.json$',
    '^benchmarks/evidence-status\.json$',
    '^shaders/manifest\.json$',
    '^shaders/compute_fixture\.dxil$',
    '^shaders/generated/compute_fixture_dxil\.h$',
    '^fixtures/compute_shader/expected_output\.json$'
)

function Test-AllowedEvidencePath([string]$Path) {
    $normalized = $Path.Replace('\', '/')
    foreach ($pattern in $allowed) {
        if ($normalized -match $pattern) { return $true }
    }
    return $false
}

function Get-NwPropertyValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Test-NwSha256Hex([string]$Value) {
    return ($Value -match '^[0-9a-fA-F]{64}$')
}

$schemaByRelativePath = @(
    @{ Pattern = '^benchmarks/ledgers/'; Schema = 'schemas/size-ledger.schema.json' },
    @{ Pattern = '^benchmarks/hardware/'; Schema = 'schemas/benchmark-result.schema.json' },
    @{ Pattern = '^benchmarks/clean-system/'; Schema = 'schemas/clean-system-report.schema.json' },
    @{ Pattern = '^benchmarks/reproducibility/'; Schema = 'schemas/reproducibility-report.schema.json' },
    @{ Pattern = '^benchmarks/toolchain-manifest\.v1\.json$'; Schema = 'schemas/toolchain-manifest.schema.json' }
)

function Read-NwJsonDocument([string]$Path) {
    try {
        return Get-Content -Raw $Path | ConvertFrom-Json
    } catch {
        throw "Unable to parse JSON evidence '$Path': $($_.Exception.Message)"
    }
}

function Assert-NwEvidenceSchema([string]$Root, [string]$RelativePath, [string]$FullPath) {
    $mapping = $schemaByRelativePath | Where-Object { $RelativePath -match $_.Pattern } | Select-Object -First 1
    if ($null -eq $mapping) { return }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) {
        throw "Python 3 is required to validate the schema of evidence candidate '$RelativePath'."
    }
    $harnessRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
    $validator = Join-Path $harnessRoot "tools/schema_validator.py"
    $schemaPath = Join-Path $harnessRoot $mapping.Schema
    if (-not (Test-Path $validator -PathType Leaf) -or -not (Test-Path $schemaPath -PathType Leaf)) {
        throw "Repository schema validator or schema is missing for '$RelativePath'."
    }
    & $python.Source $validator --repo-root $Root --document $FullPath --schema $schemaPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Schema validation failed for evidence candidate '$RelativePath'."
    }
}

function Get-NwSelectedLedgerHashes([string]$Root, [string]$ToolchainId) {
    $hashes = @{}
    foreach ($ledgerFile in @(Get-ChildItem -Path (Join-Path $Root "benchmarks/ledgers") -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
        $ledger = Read-NwJsonDocument $ledgerFile.FullName
        if ((Get-NwPropertyValue $ledger "verification_status") -ne "verified") { continue }
        if ((Get-NwPropertyValue $ledger "build_profile") -ne "release") { continue }
        if ((Get-NwPropertyValue $ledger "toolchain_id") -ne $ToolchainId) { continue }
        $fixtureId = [string](Get-NwPropertyValue $ledger "fixture_id" "")
        $component = @((Get-NwPropertyValue $ledger "distributed_components" @()) |
            Where-Object { (Get-NwPropertyValue $_ "category") -eq "executable" }) | Select-Object -First 1
        if ($null -eq $component) { continue }
        $hash = [string](Get-NwPropertyValue $component "sha256" "")
        if (Test-NwSha256Hex $hash) { $hashes[$fixtureId] = $hash.ToLowerInvariant() }
    }
    return $hashes
}

function Get-NwDocumentFixtureHash($Document, [string]$FixtureId) {
    $provenance = Get-NwPropertyValue $Document "artifact_provenance"
    $artifactHashes = Get-NwPropertyValue $provenance "artifact_sha256"
    $property = if ($null -ne $artifactHashes) { $artifactHashes.PSObject.Properties[$FixtureId] } else { $null }
    if ($null -ne $property -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        return ([string]$property.Value).ToLowerInvariant()
    }
    foreach ($result in @((Get-NwPropertyValue $Document "fixture_results" @()))) {
        if ([string](Get-NwPropertyValue $result "fixture_id" "") -eq $FixtureId) {
            $hash = [string](Get-NwPropertyValue $result "sha256" "")
            if (-not [string]::IsNullOrWhiteSpace($hash)) { return $hash.ToLowerInvariant() }
        }
    }
    return $null
}

function Assert-NwProvenance([string]$Kind, $Document, [string]$ToolchainId, $LedgerHashes, [string[]]$Fixtures) {
    $provenance = Get-NwPropertyValue $Document "artifact_provenance"
    if ($null -eq $provenance) { throw "$Kind does not record artifact provenance." }
    if ([string](Get-NwPropertyValue $provenance "toolchain_id" "") -ne $ToolchainId) {
        throw "$Kind artifact provenance does not match selected toolchain '$ToolchainId'."
    }
    if ([string](Get-NwPropertyValue $provenance "source" "") -ne "recorded_ledger_artifact_hash") {
        throw "$Kind artifact provenance is not bound to a recorded ledger hash."
    }
    foreach ($fixture in $Fixtures) {
        if (-not $LedgerHashes.ContainsKey($fixture)) {
            throw "Selected toolchain '$ToolchainId' has no verified release-ledger hash for $fixture."
        }
        $evidenceHash = Get-NwDocumentFixtureHash -Document $Document -FixtureId $fixture
        if ([string]::IsNullOrWhiteSpace($evidenceHash)) { throw "$Kind does not record the $fixture artifact hash." }
        if ($evidenceHash -ne $LedgerHashes[$fixture]) {
            throw "$Kind $fixture artifact hash $evidenceHash is stale; expected $($LedgerHashes[$fixture])."
        }
    }
}

function Assert-NwEvidenceContent([string]$Root, [string]$RelativePath, [string]$SelectedToolchain, $LedgerHashes) {
    $normalized = $RelativePath.Replace('\', '/')
    $fullPath = Join-Path $Root $normalized
    if (-not (Test-Path $fullPath -PathType Leaf)) { throw "Evidence candidate is missing: $normalized" }
    Assert-NwEvidenceSchema -Root $Root -RelativePath $normalized -FullPath $fullPath

    if ($normalized -match '^benchmarks/ledgers/') {
        $ledger = Read-NwJsonDocument $fullPath
        if ((Get-NwPropertyValue $ledger "verification_status") -ne "verified") { throw "Ledger '$normalized' is not verified." }
        if ((Get-NwPropertyValue $ledger "build_profile") -ne "release") { throw "Ledger '$normalized' is not a release ledger." }
        if ([string](Get-NwPropertyValue $ledger "toolchain_id" "") -notin @("msvc-ltcg", "clang-cl-lld")) {
            throw "Ledger '$normalized' records an unknown toolchain."
        }
        $executable = @((Get-NwPropertyValue $ledger "distributed_components" @()) |
            Where-Object { (Get-NwPropertyValue $_ "category") -eq "executable" }) | Select-Object -First 1
        if ($null -eq $executable) { throw "Ledger '$normalized' has no executable component." }
        if (-not (Test-NwSha256Hex ([string](Get-NwPropertyValue $executable "sha256" "")))) {
            throw "Ledger '$normalized' executable SHA-256 is invalid."
        }
        if ([int](Get-NwPropertyValue $executable "size_bytes" 0) -le 0) {
            throw "Ledger '$normalized' executable size is invalid."
        }
        return
    }

    if ($normalized -match '^benchmarks/hardware/') {
        $document = Read-NwJsonDocument $fullPath
        $expectedTier = if ($normalized -like "*rtx-4060-baseline.json") { "rtx_4060_desktop" } else { "rtx_50_series_desktop" }
        if ([string](Get-NwPropertyValue $document "hardware_tier" "") -ne $expectedTier) {
            throw "Hardware evidence '$normalized' does not record tier $expectedTier."
        }
        if ((Get-NwPropertyValue $document "verification_status") -ne "verified" -or
            (Get-NwPropertyValue $document "benchmark_passed") -ne $true) {
            throw "Hardware evidence '$normalized' is not verified."
        }
        if (@((Get-NwPropertyValue $document "unavailable_measurements" @())).Count -gt 0) {
            throw "Hardware evidence '$normalized' has unavailable measurements."
        }
        Assert-NwProvenance -Kind "Hardware evidence '$normalized'" -Document $document -ToolchainId $SelectedToolchain -LedgerHashes $LedgerHashes -Fixtures @("clear_present", "compute_shader")
        return
    }

    if ($normalized -match '^benchmarks/clean-system/') {
        $document = Read-NwJsonDocument $fullPath
        if ((Get-NwPropertyValue $document "verification_status") -ne "verified" -or
            (Get-NwPropertyValue $document "clean_system_passed") -ne $true) {
            throw "CleanSystem evidence '$normalized' is not verified."
        }
        Assert-NwProvenance -Kind "CleanSystem evidence '$normalized'" -Document $document -ToolchainId $SelectedToolchain -LedgerHashes $LedgerHashes -Fixtures @("empty_player", "clear_present", "compute_shader")
        $absence = Get-NwPropertyValue $document "sdk_absence_checks"
        foreach ($name in @("windows_sdk_absent", "d3d12_debug_layer_absent", "dxc_redist_absent", "agility_sdk_redist_absent", "msvc_debug_crt_absent")) {
            if ((Get-NwPropertyValue $absence $name) -ne $true) { throw "CleanSystem evidence '$normalized' does not verify '$name'." }
        }
        return
    }

    if ($normalized -match '^benchmarks/reproducibility/') {
        $document = Read-NwJsonDocument $fullPath
        if ((Get-NwPropertyValue $document "verification_status") -ne "verified" -or
            (Get-NwPropertyValue $document "all_artifacts_match") -ne $true) {
            throw "Reproducibility evidence '$normalized' is not verified."
        }
        if (@((Get-NwPropertyValue $document "missing_files" @())).Count -gt 0) {
            throw "Reproducibility evidence '$normalized' is missing artifacts."
        }
        foreach ($artifact in @((Get-NwPropertyValue $document "artifacts" @()))) {
            if ((Get-NwPropertyValue $artifact "hash_match") -ne $true) {
                throw "Reproducibility evidence '$normalized' contains a non-matching artifact."
            }
            $hashA = [string](Get-NwPropertyValue $artifact "sha256_a" "")
            $hashB = [string](Get-NwPropertyValue $artifact "sha256_b" "")
            if (-not (Test-NwSha256Hex $hashA) -or $hashA -ne $hashB) {
                throw "Reproducibility evidence '$normalized' contains an invalid artifact hash pair."
            }
        }
        return
    }

    if ($normalized -eq "benchmarks/toolchain-manifest.v1.json") {
        $document = Read-NwJsonDocument $fullPath
        $selected = Get-NwPropertyValue $document "selected_toolchain"
        if ($null -eq $selected -or [string]::IsNullOrWhiteSpace([string](Get-NwPropertyValue $selected "toolchain_id" ""))) {
            throw "Toolchain manifest does not record a selected toolchain."
        }
        if (@((Get-NwPropertyValue $document "evaluated_toolchains" @())).Count -eq 0) {
            throw "Toolchain manifest does not record evaluated toolchains."
        }
        return
    }

    if ($normalized -eq "benchmarks/evidence-status.json") {
        $document = Read-NwJsonDocument $fullPath
        if (@((Get-NwPropertyValue $document "required_measured_evidence" @())).Count -eq 0) {
            throw "Evidence status does not list required measured evidence."
        }
        return
    }

    if ($normalized -eq "shaders/manifest.json") {
        $document = Read-NwJsonDocument $fullPath
        if ((Get-NwPropertyValue $document "verification_status") -ne "verified") {
            throw "Shader manifest is not verified."
        }
        $shaders = @((Get-NwPropertyValue $document "shaders" @()))
        if ($shaders.Count -eq 0) { throw "Shader manifest does not list shaders." }
        foreach ($shader in $shaders) {
            if ((Get-NwPropertyValue $shader "compiled_offline_with_dxc") -ne $true) {
                throw "Shader manifest entry is not marked as offline DXC compilation."
            }
            if (-not (Test-NwSha256Hex ([string](Get-NwPropertyValue $shader "offline_dxil_sha256" "")))) {
                throw "Shader manifest entry has an invalid DXIL SHA-256."
            }
        }
        $dxilPath = Join-Path $Root "shaders/compute_fixture.dxil"
        if (Test-Path $dxilPath -PathType Leaf) {
            $expected = [string](Get-NwPropertyValue $shaders[0] "offline_dxil_sha256" "")
            $actual = (Get-FileHash -Algorithm SHA256 $dxilPath).Hash.ToLowerInvariant()
            if ($actual -ne $expected.ToLowerInvariant()) {
                throw "shaders/compute_fixture.dxil hash $actual does not match shader manifest $expected."
            }
        }
        return
    }

    if ($normalized -eq "shaders/compute_fixture.dxil") {
        $manifest = Read-NwJsonDocument (Join-Path $Root "shaders/manifest.json")
        $expected = [string](Get-NwPropertyValue (@((Get-NwPropertyValue $manifest "shaders" @()))[0]) "offline_dxil_sha256" "")
        $actual = (Get-FileHash -Algorithm SHA256 $fullPath).Hash.ToLowerInvariant()
        if (-not (Test-NwSha256Hex $expected) -or $actual -ne $expected.ToLowerInvariant()) {
            throw "shaders/compute_fixture.dxil hash $actual does not match verified shader manifest $expected."
        }
        return
    }

    if ($normalized -eq "shaders/generated/compute_fixture_dxil.h") {
        if ((Get-Item $fullPath).Length -le 0) { throw "Embedded shader header is empty." }
        return
    }

    if ($normalized -eq "fixtures/compute_shader/expected_output.json") {
        $document = Read-NwJsonDocument $fullPath
        if (-not (Test-NwSha256Hex ([string](Get-NwPropertyValue $document "output_sha256" "")))) {
            throw "Compute fixture expected output has an invalid output_sha256."
        }
        return
    }

    throw "Unrecognized evidence candidate: $normalized"
}

function Assert-NwNoHistoryOperationInProgress([string]$GitRoot) {
    # Never create a commit while a merge, rebase, cherry-pick, revert or
    # sequencer is in progress. Those states would turn `git commit` into a
    # merge/resolution commit or allow committing mid-rebase, which violates the
    # guarantee that this tool only creates one ordinary new evidence commit.
    $gitDir = (& git -C $GitRoot rev-parse --git-dir 2>$null | Select-Object -First 1)
    $gitDir = ([string]$gitDir).Trim()
    if ([string]::IsNullOrWhiteSpace($gitDir)) { throw "Unable to resolve the Git directory." }
    if (-not [System.IO.Path]::IsPathRooted($gitDir)) {
        $gitDir = Join-Path $GitRoot $gitDir
    }
    $gitDir = [System.IO.Path]::GetFullPath($gitDir)
    foreach ($marker in @("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")) {
        if (Test-Path (Join-Path $gitDir $marker)) {
            throw "Refusing: Git operation marker '$marker' is present. Complete or abort the in-progress operation first."
        }
    }
    $unmerged = @(& git -C $GitRoot ls-files -u 2>$null)
    if ($unmerged.Count -gt 0) {
        throw "Refusing: unresolved merge conflicts are present in the index."
    }
}

try {
    $root = (& git -C $RepoRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    $root = ([string]$root).Trim()
    if ([string]::IsNullOrWhiteSpace($root)) { throw "RepoRoot is not a Git working tree." }

    Assert-NwNoHistoryOperationInProgress -GitRoot $root

    $toolchainManifest = Read-NwJsonDocument (Join-Path $root "benchmarks/toolchain-manifest.v1.json")
    $selectedToolchain = [string](Get-NwPropertyValue (Get-NwPropertyValue $toolchainManifest "selected_toolchain") "toolchain_id" "")
    if ([string]::IsNullOrWhiteSpace($selectedToolchain)) { throw "Unable to resolve the selected toolchain for evidence validation." }
    $ledgerHashes = Get-NwSelectedLedgerHashes -Root $root -ToolchainId $selectedToolchain

    $preStaged = @(& git -C $root diff --cached --name-only)
    $unexpectedStaged = @($preStaged | Where-Object { -not (Test-AllowedEvidencePath $_) })
    if ($unexpectedStaged.Count -gt 0) {
        throw "Refusing: non-evidence paths are already staged: $($unexpectedStaged -join ', ')"
    }

    $statusLines = @(& git -C $root status --porcelain=v1 --untracked-files=all)
    $changed = @()
    foreach ($line in $statusLines) {
        if ($line.Length -lt 4) { continue }
        $statusCode = $line.Substring(0, 2)
        $path = $line.Substring(3)
        if ($path -match ' -> ') { throw "Refusing rename in evidence preparation: $path" }
        $path = $path.Trim('"')
        # A path with both staged (index) and unstaged (worktree) changes would
        # have its pre-staged content silently replaced by `git add -- path`.
        # Reject that conflicting state instead of rewriting staged evidence.
        $stagedMark = if ($statusCode.Length -eq 2) { $statusCode.Substring(0, 1) } else { "" }
        $worktreeMark = if ($statusCode.Length -eq 2) { $statusCode.Substring(1, 1) } else { "" }
        if ($stagedMark -ne ' ' -and $worktreeMark -ne ' ' -and
            $stagedMark -ne '?' -and $worktreeMark -ne '?') {
            throw "Refusing: '$path' has both staged and unstaged changes ($statusCode); review or reset the pre-staged content first."
        }
        $changed += $path
    }
    $changed = @($changed | Select-Object -Unique)
    $unexpected = @($changed | Where-Object { -not (Test-AllowedEvidencePath $_) })
    if ($unexpected.Count -gt 0) {
        throw "Refusing to stage while unexpected paths are modified/untracked: $($unexpected -join ', ')"
    }
    if ($changed.Count -eq 0 -and $preStaged.Count -eq 0) {
        Write-Host "[NW-EVIDENCE-GIT] no evidence changes to stage."
        exit 0
    }

    # Validate every candidate's schema/provenance/hash relationships before the
    # index is changed. Staging invalid or stale content would make it available
    # to a later commit even if the downstream verifier would reject it.
    foreach ($path in $changed) {
        Assert-NwEvidenceContent -Root $root -RelativePath $path -SelectedToolchain $selectedToolchain -LedgerHashes $ledgerHashes
    }

    foreach ($path in $changed) {
        & git -C $root add -- $path
        if ($LASTEXITCODE -ne 0) { throw "git add failed for allowlisted path: $path" }
    }

    $staged = @(& git -C $root diff --cached --name-only)
    $unexpectedAfter = @($staged | Where-Object { -not (Test-AllowedEvidencePath $_) })
    if ($unexpectedAfter.Count -gt 0) { throw "Unexpected staged path after allowlist staging: $($unexpectedAfter -join ', ')" }

    Write-Host "[NW-EVIDENCE-GIT] staged evidence paths:"
    $staged | ForEach-Object { Write-Host "  $_" }
    & git -C $root diff --cached --stat

    if ($Commit) {
        # Re-check immediately before committing; the working tree could have
        # changed while the evidence was staged.
        Assert-NwNoHistoryOperationInProgress -GitRoot $root
        # Deliberately no amend/force/push. This creates one ordinary new commit only.
        & git -C $root commit -m $CommitMessage
        if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
        $sha = (& git -C $root rev-parse HEAD).Trim()
        Write-Host "[NW-EVIDENCE-GIT] created new commit $sha"
        Write-Host "[NW-EVIDENCE-GIT] push remains an explicit operator action."
    }
    exit 0
} catch {
    Write-Error "[NW-EVIDENCE-GIT] $($_.Exception.Message)"
    exit 1
}
