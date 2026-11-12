#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Fixture,
    [Parameter(Mandatory)][string]$Package,
    [ValidateSet("ada", "blackwell", "auto")]
    [string]$Tier = "ada",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$Output = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-04 hardware evidence capture requires a physical Windows 11 x64 host."
    exit 1
}
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $RepoRoot "benchmarks/nw04-hardware-evidence.json"
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) { throw "Python 3 was not found in PATH." }

& $python.Source (Join-Path $RepoRoot "tools/nw04_evidence.py") `
    --fixture $Fixture `
    --package $Package `
    --tier $Tier `
    --output $Output
if ($LASTEXITCODE -ne 0) {
    Write-Error "NW-04 execution evidence failed (exit code $LASTEXITCODE). A failed run remains failed evidence."
    exit $LASTEXITCODE
}
Write-Host "[NW04-EVIDENCE] tier=$Tier output=$Output"
exit 0
