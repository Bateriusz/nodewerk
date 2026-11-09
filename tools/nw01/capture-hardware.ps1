#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("rtx_4060_desktop", "rtx_50_series_desktop")]
    [string]$Tier,

    [Parameter(Mandatory)]
    [string]$GpuArchitecture,

    [Parameter(Mandatory)]
    [string]$NvidiaPowerManagementMode,

    [Parameter(Mandatory)]
    [string]$PciePowerManagement,

    [Parameter(Mandatory)]
    [ValidateSet("cold_cleared", "warm_primed")]
    [string]$DriverShaderCacheState,

    [Parameter(Mandatory)]
    [ValidateSet("cold", "warm")]
    [string]$OsFileCacheState,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$BinaryDir = "",
    [string]$SummaryPath = "",
    [string]$WindowsPowerPlan = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 physical hardware capture requires Windows."
    exit 1
}

Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force

function Get-RequiredText([string]$Name, $Value) {
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { throw "Unable to measure required host field: $Name" }
    return $text.Trim()
}

try {
    Enter-NwBuildEnvironment -RepoRoot $RepoRoot | Out-Null

    $nt = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    $osBuild = "10.0.$($nt.CurrentBuildNumber).$($nt.UBR)"
    $cpuModel = Get-RequiredText "CPU model" ((Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name))

    $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) { $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue }
    if (-not $nvidiaSmi) { throw "nvidia-smi was not found; GPU model/driver cannot be measured reliably." }
    $gpuLine = (& $nvidiaSmi.Source --query-gpu=name,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gpuLine)) { throw "nvidia-smi did not return GPU model/driver metadata." }
    $gpuParts = @($gpuLine -split ',', 2 | ForEach-Object { $_.Trim() })
    if ($gpuParts.Count -ne 2) { throw "Unexpected nvidia-smi output: $gpuLine" }
    $gpuModel = Get-RequiredText "GPU model" $gpuParts[0]
    $nvidiaDriver = Get-RequiredText "NVIDIA driver" $gpuParts[1]

    $video = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -eq $gpuModel -or $_.Name -like "*NVIDIA*" } | Select-Object -First 1
    if (-not $video) { throw "Win32_VideoController did not expose the executing NVIDIA adapter." }
    $wddmDriver = Get-RequiredText "WDDM driver version" $video.DriverVersion
    $refreshHz = [int]$video.CurrentRefreshRate
    if ($refreshHz -le 0) { throw "Unable to measure a positive display refresh rate." }

    if ([string]::IsNullOrWhiteSpace($WindowsPowerPlan)) {
        $powerText = (powercfg /getactivescheme | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw "powercfg /getactivescheme failed." }
        $match = [regex]::Match($powerText, '\(([^)]+)\)')
        if ($match.Success) { $WindowsPowerPlan = $match.Groups[1].Value.Trim() }
    }
    $WindowsPowerPlan = Get-RequiredText "Windows power plan" $WindowsPowerPlan

    if ([string]::IsNullOrWhiteSpace($BinaryDir)) {
        $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
        switch ([string]$contract.SelectedToolchain) {
            "msvc-ltcg" {
                $BinaryDir = Join-Path $RepoRoot "build\msvc-release-ltcg-clean-a"
            }
            "clang-cl-lld" {
                $BinaryDir = Join-Path $RepoRoot "build\repro-clean-a"
            }
            default {
                throw "Unsupported selected toolchain '$($contract.SelectedToolchain)' for physical capture."
            }
        }
    }
    $BinaryDir = [System.IO.Path]::GetFullPath($BinaryDir)
    $requiredFixtures = @(
        (Join-Path $BinaryDir "deploy\nw_clear_present_fixture.exe"),
        (Join-Path $BinaryDir "deploy\nw_compute_shader_fixture.exe")
    )
    $missingFixtures = @($requiredFixtures | Where-Object { -not (Test-Path $_ -PathType Leaf) })
    if ($missingFixtures.Count -gt 0) {
        throw "Selected-toolchain release fixtures are missing from '$BinaryDir'. Run tools/nw01/run-full.ps1 successfully before capture-hardware.ps1. Missing: $($missingFixtures -join ', ')"
    }

    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path (Split-Path -Parent $RepoRoot) "nodewerk-evidence" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $SummaryPath = Join-Path $base "nw01-hardware-$Tier-$stamp.json"
    }

    Write-Host "[NW-HARDWARE] measured OS=$osBuild CPU=$cpuModel GPU=$gpuModel driver=$nvidiaDriver WDDM=$wddmDriver refresh=$($refreshHz)Hz plan=$WindowsPowerPlan"
    Write-Host "[NW-HARDWARE] selected release fixtures: $BinaryDir"

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 was not found in PATH." }
    $args = @(
        (Join-Path $RepoRoot "tools\verify_nw01.py"),
        "--mode", "Full",
        "--record-benchmark", $Tier,
        "--binary-dir", $BinaryDir,
        "--os-build", $osBuild,
        "--gpu-model", $gpuModel,
        "--nvidia-driver-version", $nvidiaDriver,
        "--cpu-model", $cpuModel,
        "--gpu-architecture", $GpuArchitecture,
        "--wddm-driver-version", $wddmDriver,
        "--windows-power-plan", $WindowsPowerPlan,
        "--nvidia-power-management-mode", $NvidiaPowerManagementMode,
        "--pcie-power-management", $PciePowerManagement,
        "--refresh-hz", [string]$refreshHz,
        "--driver-shader-cache-state", $DriverShaderCacheState,
        "--os-file-cache-state", $OsFileCacheState,
        "--summary-json", $SummaryPath
    )
    & $python.Source @args
    $code = $LASTEXITCODE
    if (-not (Test-Path $SummaryPath -PathType Leaf)) { throw "Hardware capture did not produce summary JSON (exit code $code)." }
    Write-Host "[NW-HARDWARE] verifier exit=$code summary=$SummaryPath"
    exit $code
} catch {
    Write-Error "[NW-HARDWARE] $($_.Exception.Message)"
    exit 1
}
