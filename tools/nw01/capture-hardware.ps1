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
    # Query all adapters with a stable identity (index + PCI bus + name + driver).
    # Selecting only the first name match is unsafe on multi-GPU hosts: an
    # unrelated NVIDIA adapter can precede the fixture's adapter and contribute
    # a different driver/refresh pair while the GPU name still looks correct.
    $gpuOutput = @(& $nvidiaSmi.Source --query-gpu=index,name,driver_version,pci.bus_id --format=csv,noheader,nounits)
    if ($LASTEXITCODE -ne 0 -or $gpuOutput.Count -eq 0) { throw "nvidia-smi did not return GPU identity metadata." }
    $gpuRows = @(
        foreach ($line in $gpuOutput) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $parts = @($line -split ',' | ForEach-Object { $_.Trim() })
            if ($parts.Count -lt 4) { throw "Unexpected nvidia-smi output: $line" }
            [pscustomobject]@{
                Index = [string]$parts[0]
                Name = [string]$parts[1]
                Driver = [string]$parts[2]
                PciBusId = [string]$parts[3]
            }
        }
    )
    if ($gpuRows.Count -eq 0) { throw "nvidia-smi did not return any NVIDIA adapters." }

    $nvidiaControllers = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like "*NVIDIA*" })
    $bindingProblem = $null
    $selectedRow = $null
    $selectedVideo = $null
    if ($gpuRows.Count -eq 1) {
        $candidateName = $gpuRows[0].Name
        $matchingControllers = @($nvidiaControllers | Where-Object { [string]::Equals($_.Name, $candidateName, [System.StringComparison]::OrdinalIgnoreCase) })
        if ($matchingControllers.Count -eq 1) {
            $selectedRow = $gpuRows[0]
            $selectedVideo = $matchingControllers[0]
        } else {
            $bindingProblem = "Win32_VideoController exposed $($matchingControllers.Count) adapters matching '$candidateName'"
        }
    } else {
        # More than one NVIDIA adapter is present. Only bind when a single adapter
        # name is unique in both enumerations; otherwise the executing fixture
        # adapter cannot be identified safely from host metadata alone.
        $uniqueNames = @(
            $gpuRows | Group-Object Name | Where-Object { $_.Count -eq 1 } | ForEach-Object { $_.Name }
        )
        $candidateRows = @($gpuRows | Where-Object { $uniqueNames -contains $_.Name })
        if ($candidateRows.Count -eq 1) {
            $candidateName = $candidateRows[0].Name
            $matchingControllers = @($nvidiaControllers | Where-Object { [string]::Equals($_.Name, $candidateName, [System.StringComparison]::OrdinalIgnoreCase) })
            if ($matchingControllers.Count -eq 1) {
                $selectedRow = $candidateRows[0]
                $selectedVideo = $matchingControllers[0]
            } else {
                $bindingProblem = "no unique Win32_VideoController match for '$candidateName'"
            }
        } else {
            $bindingProblem = "$($gpuRows.Count) NVIDIA adapters are present and no unique adapter identity can be selected"
        }
    }

    if ($null -ne $selectedRow) {
        $gpuModel = Get-RequiredText "GPU model" $selectedRow.Name
        $nvidiaDriver = Get-RequiredText "NVIDIA driver" $selectedRow.Driver
        $wddmDriver = Get-RequiredText "WDDM driver version" $selectedVideo.DriverVersion
        $refreshHz = [int]$selectedVideo.CurrentRefreshRate
        if ($refreshHz -le 0) { throw "Unable to measure a positive display refresh rate." }
        Write-Host "[NW-HARDWARE] bound adapter index=$($selectedRow.Index) pci=$($selectedRow.PciBusId)"
    } else {
        # Ambiguous adapter identity: preserve the measured model for conflict
        # detection but do not attribute driver/WDDM/refresh metadata to it. The
        # verifier records these fields as unavailable, so the baseline remains
        # explicitly unverified rather than mixed across adapters.
        $gpuModel = Get-RequiredText "GPU model" $gpuRows[0].Name
        $nvidiaDriver = ""
        $wddmDriver = ""
        $refreshHz = 0
        Write-Warning "[NW-HARDWARE] adapter identity is ambiguous ($bindingProblem); driver/WDDM/refresh metadata will remain unverified."
    }

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
        "--cpu-model", $cpuModel,
        "--gpu-architecture", $GpuArchitecture,
        "--windows-power-plan", $WindowsPowerPlan,
        "--nvidia-power-management-mode", $NvidiaPowerManagementMode,
        "--pcie-power-management", $PciePowerManagement,
        "--driver-shader-cache-state", $DriverShaderCacheState,
        "--os-file-cache-state", $OsFileCacheState,
        "--summary-json", $SummaryPath
    )
    if (-not [string]::IsNullOrWhiteSpace($nvidiaDriver)) { $args += @("--nvidia-driver-version", $nvidiaDriver) }
    if (-not [string]::IsNullOrWhiteSpace($wddmDriver)) { $args += @("--wddm-driver-version", $wddmDriver) }
    if ($refreshHz -gt 0) { $args += @("--refresh-hz", [string]$refreshHz) }
    & $python.Source @args
    $code = $LASTEXITCODE
    if (-not (Test-Path $SummaryPath -PathType Leaf)) { throw "Hardware capture did not produce summary JSON (exit code $code)." }
    Write-Host "[NW-HARDWARE] verifier exit=$code summary=$SummaryPath"
    exit $code
} catch {
    Write-Error "[NW-HARDWARE] $($_.Exception.Message)"
    exit 1
}
