#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ArchiveRoot,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$FullSummary = "",
    [string]$CleanSystemPackage = "",
    [string]$CleanSystemZip = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
try {
    $repo = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
    $archive = [System.IO.Path]::GetFullPath($ArchiveRoot).TrimEnd('\')
    if ($archive.StartsWith($repo + '\', [System.StringComparison]::OrdinalIgnoreCase) -or $archive -eq $repo) {
        throw "ArchiveRoot must be outside the Git working tree."
    }
    $sha = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    $sha = ([string]$sha).Trim()
    if ($sha -notmatch '^[0-9a-fA-F]{40}$') { throw "Unable to resolve repository HEAD." }
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $dest = Join-Path $archive ("{0}-{1}" -f $sha.Substring(0,12), $stamp)
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
        Copy-Item -Recurse -Force $item.Path $target
        $copied += $item.Name
    }

    [ordered]@{
        schema_version = "1"
        source_commit = $sha
        archived_at_utc = [DateTime]::UtcNow.ToString("o")
        artifacts = $copied
        note = "Archive only; certifying evidence remains governed by repository verifier provenance."
    } | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $dest "archive-manifest.json")

    Write-Host "[NW-ARCHIVE] archived source $sha at $dest"
    exit 0
} catch {
    Write-Error "[NW-ARCHIVE] $($_.Exception.Message)"
    exit 1
}
