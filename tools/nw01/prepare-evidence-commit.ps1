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
