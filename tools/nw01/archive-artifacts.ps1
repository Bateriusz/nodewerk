#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ArchiveRoot,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$FullSummary = "",
    [string]$CleanSystemPackage = "",
    [string]$CleanSystemZip = "",
    [switch]$Replace
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-NwJsonFile([string]$Path) {
    if (-not (Test-Path $Path -PathType Leaf)) { return $null }
    try {
        return (Get-Content -Raw $Path | ConvertFrom-Json)
    } catch {
        throw "Unable to parse JSON artifact '$Path': $($_.Exception.Message)"
    }
}

function Test-NwCommit([string]$Value) {
    return ($Value -match '^[0-9a-fA-F]{40}$')
}

try {
    $worktreeRoot = (& git -C $RepoRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    $worktreeRoot = ([string]$worktreeRoot).Trim()
    if ([string]::IsNullOrWhiteSpace($worktreeRoot)) { $worktreeRoot = $RepoRoot }
    $repo = [System.IO.Path]::GetFullPath($worktreeRoot).TrimEnd([char[]]@('\', '/'))
    $archive = [System.IO.Path]::GetFullPath($ArchiveRoot).TrimEnd([char[]]@('\', '/'))
    $repoWithSeparator = $repo + [System.IO.Path]::DirectorySeparatorChar
    if ($archive -eq $repo -or $archive.StartsWith($repoWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "ArchiveRoot must be outside the Git working tree."
    }

    $checkoutCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    $checkoutCommit = ([string]$checkoutCommit).Trim()
    if (-not (Test-NwCommit $checkoutCommit)) { throw "Unable to resolve repository HEAD." }

    # Label the archive with the revision the supplied evidence was produced
    # from, not merely the revision currently checked out. Historical evidence
    # stays historical and is never relabelled as current certifying evidence.
    $revisions = New-Object System.Collections.Generic.List[object]
    if (-not [string]::IsNullOrWhiteSpace($FullSummary)) {
        $summary = Read-NwJsonFile $FullSummary
        if ($null -eq $summary) { throw "Archive input does not exist: $FullSummary" }
        if ($summary.PSObject.Properties.Name -contains "source_commit") {
            $summaryCommit = ([string]$summary.source_commit).Trim()
            if (-not [string]::IsNullOrWhiteSpace($summaryCommit)) {
                if (-not (Test-NwCommit $summaryCommit)) {
                    throw "FullSummary source_commit '$summaryCommit' is not a full Git revision."
                }
                $revisions.Add([pscustomobject]@{ Artifact = "full-summary"; SourceCommit = $summaryCommit.ToLowerInvariant() })
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($CleanSystemPackage)) {
        $manifestPath = Join-Path $CleanSystemPackage "evidence/artifact-manifest.json"
        $packageManifest = Read-NwJsonFile $manifestPath
        if ($null -eq $packageManifest) {
            throw "CleanSystem package artifact manifest is missing: $manifestPath"
        }
        if ($packageManifest.PSObject.Properties.Name -notcontains "source_commit") {
            throw "CleanSystem package artifact manifest does not record source_commit."
        }
        $packageCommit = ([string]$packageManifest.source_commit).Trim()
        if (-not (Test-NwCommit $packageCommit)) {
            throw "CleanSystem package source_commit '$packageCommit' is not a full Git revision."
        }
        $revisions.Add([pscustomobject]@{ Artifact = "clean-system-package"; SourceCommit = $packageCommit.ToLowerInvariant() })
    }

    $distinctCommits = @($revisions | ForEach-Object { $_.SourceCommit } | Select-Object -Unique)
    if ($distinctCommits.Count -gt 1) {
        $detail = ($revisions | ForEach-Object { "$($_.Artifact)=$($_.SourceCommit)" }) -join ", "
        throw "Archive inputs come from conflicting source revisions: $detail"
    }
    if ($distinctCommits.Count -eq 1) {
        $sourceCommit = [string]$distinctCommits[0]
        $sourceSource = ($revisions | ForEach-Object { $_.Artifact } | Select-Object -Unique) -join "+"
    } else {
        # No artifact exposes provenance (for example a bare ZIP or an older
        # summary). Fall back to the checked-out revision but mark the archive
        # explicitly as checkout-labelled rather than certifying evidence.
        $sourceCommit = $checkoutCommit.ToLowerInvariant()
        $sourceSource = "checkout-fallback"
    }
    $isCurrent = $sourceCommit -eq $checkoutCommit.ToLowerInvariant()
    $evidenceClass = if ($isCurrent -and $sourceSource -ne "checkout-fallback") {
        "current-certifying"
    } elseif ($isCurrent) {
        "checkout-labelled-unverified-provenance"
    } else {
        "historical"
    }

    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $unique = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $dest = Join-Path $archive ("{0}-{1}-{2}" -f $sourceCommit.Substring(0, 12), $stamp, $unique)
    if ((Test-Path $dest) -and -not $Replace) {
        throw "Archive destination already exists: $dest. Pass -Replace to overwrite prior evidence explicitly."
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null

    $copied = @()
    foreach ($item in @(
        @{ Path = $FullSummary; Name = "full-summary.json"; Kind = "file" },
        @{ Path = $CleanSystemPackage; Name = "clean-system-package"; Kind = "directory" },
        @{ Path = $CleanSystemZip; Name = "clean-system-package.zip"; Kind = "file" }
    )) {
        if ([string]::IsNullOrWhiteSpace([string]$item.Path)) { continue }
        if (-not (Test-Path $item.Path)) { throw "Archive input does not exist: $($item.Path)" }
        $target = Join-Path $dest $item.Name
        if ((Test-Path $target) -and -not $Replace) {
            throw "Refusing to overwrite archived artifact '$target' without -Replace."
        }
        Copy-Item -Recurse -Force $item.Path $target
        $copied += $item.Name
    }

    $note = if ($evidenceClass -eq "current-certifying") {
        "Archive copy of artifacts produced from the checked-out revision; repository verifier provenance remains authoritative."
    } else {
        "Historical archive. It is not current certifying evidence for the checked-out revision."
    }
    [ordered]@{
        schema_version = "1"
        source_commit = $sourceCommit
        source_commit_origin = $sourceSource
        checkout_head = $checkoutCommit.ToLowerInvariant()
        evidence_class = $evidenceClass
        archived_at_utc = [DateTime]::UtcNow.ToString("o")
        artifacts = $copied
        source_revisions = @($revisions | ForEach-Object { [ordered]@{ artifact = $_.Artifact; source_commit = $_.SourceCommit } })
        note = $note
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $dest "archive-manifest.json")

    Write-Host "[NW-ARCHIVE] archived source $sourceCommit ($evidenceClass) at $dest"
    exit 0
} catch {
    Write-Error "[NW-ARCHIVE] $($_.Exception.Message)"
    exit 1
}
