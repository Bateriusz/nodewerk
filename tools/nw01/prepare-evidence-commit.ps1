#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [switch]$Commit,
    [string]$CommitMessage = "NW-01: record verified physical evidence"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$allowed = @(
    '^benchmarks/(hardware|clean-system|reproducibility|ledgers)/[^/]+\.json$',
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

try {
    $root = (& git -C $RepoRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    $root = ([string]$root).Trim()
    if ([string]::IsNullOrWhiteSpace($root)) { throw "RepoRoot is not a Git working tree." }

    $preStaged = @(& git -C $root diff --cached --name-only)
    $unexpectedStaged = @($preStaged | Where-Object { -not (Test-AllowedEvidencePath $_) })
    if ($unexpectedStaged.Count -gt 0) {
        throw "Refusing: non-evidence paths are already staged: $($unexpectedStaged -join ', ')"
    }

    $statusLines = @(& git -C $root status --porcelain=v1 --untracked-files=all)
    $changed = @()
    foreach ($line in $statusLines) {
        if ($line.Length -lt 4) { continue }
        $path = $line.Substring(3)
        if ($path -match ' -> ') { throw "Refusing rename in evidence preparation: $path" }
        $path = $path.Trim('"')
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
