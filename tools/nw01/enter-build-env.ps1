#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [switch]$ExportGitHubEnv,
    [switch]$InteractiveShell,
    [string]$Command = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 build environment is Windows-only."
    exit 1
}

Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force
try {
    $entered = Enter-NwBuildEnvironment -RepoRoot $RepoRoot -ExportGitHubEnv:$ExportGitHubEnv
    Write-Host "[NW-ENV] MSVC/SDK environment initialized."
    Write-Host "[NW-ENV] selected toolchain contract: $($entered.Contract.SelectedToolchain)"

    if (-not [string]::IsNullOrWhiteSpace($Command)) {
        & pwsh -NoProfile -Command $Command
        exit $LASTEXITCODE
    }
    if ($InteractiveShell) {
        Write-Host "[NW-ENV] launching nested PowerShell with the pinned build environment. Exit it to return."
        & pwsh -NoProfile -NoExit
        exit $LASTEXITCODE
    }
    exit 0
} catch {
    Write-Error "[NW-ENV] $($_.Exception.Message)"
    exit 1
}
