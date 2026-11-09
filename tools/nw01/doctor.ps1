#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 doctor is Windows-only."
    exit 1
}

Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force
try {
    Enter-NwBuildEnvironment -RepoRoot $RepoRoot | Out-Null
    $results = @(Get-NwDoctorResults -RepoRoot $RepoRoot)
    Write-Host ""
    Write-Host "NW-01 pinned toolchain doctor"
    Write-Host "-----------------------------"
    foreach ($row in $results) {
        $status = if ($row.Pass) { "PASS" } else { "FAIL" }
        Write-Host ("{0,-14} expected={1,-16} {2}  detected={3}" -f $row.Name, $row.Expected, $status, $row.Detected)
    }
    $failures = @($results | Where-Object { -not $_.Pass })
    if ($failures.Count -gt 0) {
        Write-Error "NW-01 doctor found $($failures.Count) pinned toolchain mismatch(es)."
        exit 1
    }
    Write-Host "[NW-DOCTOR] all pinned toolchain requirements match exactly."
    exit 0
} catch {
    Write-Error "[NW-DOCTOR] $($_.Exception.Message)"
    exit 1
}
