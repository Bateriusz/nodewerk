#!/usr/bin/env pwsh
#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$ExpectedBranch = "agent/NW-10",
    [Parameter(Mandatory)][string]$CleanSystemBundle,
    [Parameter(Mandatory)][string]$Metrics,
    [string]$IntroReport = "build/nw10-intro/report.json",
    [string[]]$ForbidToken = @(),
    [switch]$Commit,
    [switch]$Push,
    [string]$CommitMessage = "NW-10: publish physical system evidence"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Push -and -not $Commit) {
    throw "-Push requires -Commit."
}

$repo = [System.IO.Path]::GetFullPath($RepoRoot)
$originalLocation = Get-Location

function Resolve-NwRepoPath([string]$Value) {
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repo $Value))
}

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

function Assert-NwPrivacy {
    param(
        [Parameter(Mandatory)]$JsonObject,
        [Parameter(Mandatory)][System.Collections.Generic.List[string]]$Tokens
    )
    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($value in @(Get-NwJsonStrings $JsonObject)) {
        $text = [string]$value
        if ($text -match '(?i)^[a-z]:\\users\\' -or $text -match '(?i)^/home/') {
            $hits.Add($text)
            continue
        }
        foreach ($token in $Tokens) {
            if (
                -not [string]::IsNullOrWhiteSpace($token) -and
                $text.IndexOf($token, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            ) {
                $hits.Add($text)
                break
            }
        }
    }
    if ($hits.Count -gt 0) {
        throw "Privacy audit found forbidden strings: $($hits -join '; ')"
    }
}

function Assert-NwCleanTrackedTree {
    $lines = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "git status failed." }
    $unexpected = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $path = ([string]$line).Substring(3).Trim('"').Replace('\','/')
        if ($path.StartsWith("build/", [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $unexpected += [string]$line
    }
    if ($unexpected.Count -gt 0) {
        throw "Working tree has non-build changes before publication: $($unexpected -join '; ')"
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
    if ($null -eq $pythonCommand) { $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue }
    if ($null -eq $pythonCommand) { throw "Python 3 is required." }
    $python = $pythonCommand.Source

    $bundle = Resolve-NwRepoPath $CleanSystemBundle
    $metricsPath = Resolve-NwRepoPath $Metrics
    $intro = Resolve-NwRepoPath $IntroReport
    foreach ($required in @($bundle, $metricsPath, $intro)) {
        if (-not (Test-Path $required -PathType Leaf)) {
            throw "Required publication input is missing: $required"
        }
    }

    $privacyTokens = New-Object System.Collections.Generic.List[string]
    foreach ($token in @($env:USERNAME, $env:USERPROFILE, $ForbidToken)) {
        $value = [string]$token
        if (-not [string]::IsNullOrWhiteSpace($value) -and -not $privacyTokens.Contains($value)) {
            $privacyTokens.Add($value)
        }
    }

    $destination = "benchmarks/nw10/system-validation.v1.json"
    $statusFile = "benchmarks/nw10/evidence-status.v1.json"
    $publishArgs = @(
        "tools/nw10_publish_evidence.py", "system",
        "--clean-system-bundle", $bundle,
        "--metrics", $metricsPath,
        "--intro-report", $intro,
        "--destination", $destination,
        "--status-file", $statusFile
    )
    foreach ($token in $privacyTokens) {
        $publishArgs += @("--forbid-token", $token)
    }
    Invoke-NwNative $python $publishArgs "NW-10 system evidence publication failed"

    Invoke-NwNative $python @(
        "tools/schema_validator.py",
        "--repo-root", $repo,
        "--document", (Join-Path $repo $destination),
        "--schema", (Join-Path $repo "schemas/nw10-system-validation.schema.json")
    ) "System validation schema check failed"
    Invoke-NwNative $python @(
        "tools/schema_validator.py",
        "--repo-root", $repo,
        "--document", (Join-Path $repo $statusFile),
        "--schema", (Join-Path $repo "schemas/nw10-evidence-status.schema.json")
    ) "Evidence-status schema check failed"

    $system = Get-Content -Raw (Join-Path $repo $destination) | ConvertFrom-Json
    $status = Get-Content -Raw (Join-Path $repo $statusFile) | ConvertFrom-Json
    Assert-NwPrivacy -JsonObject $system -Tokens $privacyTokens
    Assert-NwPrivacy -JsonObject $status -Tokens $privacyTokens

    Invoke-NwNative "git" @("diff", "--check") "git diff --check failed"
    $allowed = @($destination, $statusFile)
    $lines = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "git status failed after publication." }
    $unexpected = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $path = ([string]$line).Substring(3).Trim('"').Replace('\','/')
        if ($path.StartsWith("build/", [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($allowed -notcontains $path) { $unexpected += [string]$line }
    }
    if ($unexpected.Count -gt 0) {
        throw "Publication changed unexpected paths: $($unexpected -join '; ')"
    }

    Write-Host "[NW10 SYSTEM] PUBLISH=PASS"
    Write-Host "[NW10 SYSTEM] SYSTEM_STATUS=$($system.status)"
    Write-Host "[NW10 SYSTEM] EVIDENCE_STATUS=$($status.evidence_status)"
    Write-Host "[NW10 SYSTEM] PRIVACY=PASS"

    if ($Commit) {
        Invoke-NwNative "git" (@("add", "--") + $allowed) "staging system evidence failed"
        Invoke-NwNative "git" @("diff", "--cached", "--check") "staged system evidence diff check failed"
        $staged = @(& git diff --cached --name-only)
        if ($LASTEXITCODE -ne 0) { throw "Unable to inspect staged system evidence." }
        $unexpectedStaged = @($staged | Where-Object { $allowed -notcontains $_ })
        if ($unexpectedStaged.Count -gt 0) {
            throw "Unexpected staged paths: $($unexpectedStaged -join ', ')"
        }
        if ($staged.Count -eq 0) {
            Write-Host "[NW10 SYSTEM] COMMIT=SKIPPED_NO_CHANGES"
        } else {
            Invoke-NwNative "git" @("commit", "-m", $CommitMessage) "NW-10 system evidence commit failed"
            $commitSha = (& git rev-parse HEAD).Trim()
            Write-Host "[NW10 SYSTEM] COMMIT=$commitSha"
            if ($Push) {
                Invoke-NwNative "git" @("push", "origin", $ExpectedBranch) "NW-10 system evidence push failed"
                Write-Host "[NW10 SYSTEM] PUSH=PASS"
            }
        }
    } else {
        Write-Host "[NW10 SYSTEM] COMMIT=NOT_REQUESTED"
    }
}
finally {
    Set-Location $originalLocation
}
