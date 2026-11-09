#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackageRoot,
    [string]$RepoRoot = "",
    [string]$SummaryPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# In Windows PowerShell 5.1, $PSScriptRoot is not reliable while default parameter
# expressions are being evaluated. Resolve the repository root after entering the script body.
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw "Unable to resolve run-clean-system.ps1 location."
    }
    $scriptDir = Split-Path -Parent $scriptPath
    $RepoRoot = (Resolve-Path (Join-Path $scriptDir "../..")).Path
}

# Windows PowerShell 5.1 is present on a stock Windows 11 target, while $IsWindows
# exists only in PowerShell Core. Do not require PowerShell 7 on CleanSystem hosts.
$isWindowsVariable = Get-Variable -Name IsWindows -ErrorAction SilentlyContinue
$runningOnWindows = if ($null -ne $isWindowsVariable) {
    [bool]$isWindowsVariable.Value
} else {
    $env:OS -eq "Windows_NT"
}
if (-not $runningOnWindows) {
    Write-Error "NW-01 CleanSystem execution requires Windows."
    exit 1
}

try {
    $binaryDir = Join-Path $PackageRoot "deploy"
    $evidenceDir = Join-Path $PackageRoot "evidence"
    if (-not (Test-Path $binaryDir -PathType Container)) { throw "CleanSystem package deploy directory is missing: $binaryDir" }
    if (-not (Test-Path $evidenceDir -PathType Container)) { throw "CleanSystem package evidence directory is missing: $evidenceDir" }

    $packageManifestPath = Join-Path $evidenceDir "artifact-manifest.json"
    if (-not (Test-Path $packageManifestPath -PathType Leaf)) {
        throw "CleanSystem package artifact manifest is missing: $packageManifestPath"
    }
    $packageManifest = Get-Content -Raw $packageManifestPath | ConvertFrom-Json
    $packageSourceCommit = ([string]$packageManifest.source_commit).Trim()
    if ($packageSourceCommit -notmatch '^[0-9a-fA-F]{40}    # The verifier owns authoritative SDK/DXC/debug/redist absence probes;
    # these preflight checks only catch obvious contamination before fixtures run.
    if (-not [string]::IsNullOrWhiteSpace($env:WindowsSdkDir) -or
        -not [string]::IsNullOrWhiteSpace($env:WindowsSDKVersion)) {
        throw "CleanSystem target has an active Windows SDK environment. Use an uncontaminated target."
    }
    if (Get-Command dxc.exe -ErrorAction SilentlyContinue) {
        throw "CleanSystem target exposes dxc.exe in PATH. Use an uncontaminated target."
    }

    $nt = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    $osBuild = "10.0.$($nt.CurrentBuildNumber).$($nt.UBR)"

    $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) { $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue }
    if (-not $nvidiaSmi) { throw "nvidia-smi is required to bind CleanSystem execution to the physical NVIDIA adapter." }
    $gpuLine = (& $nvidiaSmi.Source --query-gpu=name,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gpuLine)) { throw "nvidia-smi did not return GPU model/driver metadata." }
    $parts = @($gpuLine -split ',', 2 | ForEach-Object { $_.Trim() })
    if ($parts.Count -ne 2) { throw "Unexpected nvidia-smi output: $gpuLine" }
    $gpuModel = $parts[0]
    $nvidiaDriver = $parts[1]

    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $SummaryPath = Join-Path (Split-Path -Parent ([System.IO.Path]::GetFullPath($PackageRoot))) "nw01-clean-system-$stamp.json"
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 is required to run the repository verifier." }

    $args = @(
        (Join-Path $RepoRoot "tools\verify_nw01.py"),
        "--mode", "CleanSystem",
        "--binary-dir", $binaryDir,
        "--evidence-dir", $evidenceDir,
        "--os-build", $osBuild,
        "--gpu-model", $gpuModel,
        "--nvidia-driver-version", $nvidiaDriver,
        "--summary-json", $SummaryPath
    )
    & $python.Source @args
    $code = $LASTEXITCODE

    if (-not (Test-Path $SummaryPath -PathType Leaf)) {
        throw "CleanSystem verifier did not produce summary JSON (exit code $code)."
    }
    if ($code -eq 0) {
        Write-Host "[NW-CLEANSYSTEM] VERIFIED package=$PackageRoot summary=$SummaryPath"
    } else {
        Write-Host "[NW-CLEANSYSTEM] FAILED exit=$code package=$PackageRoot summary=$SummaryPath"
    }
    exit $code
} catch {
    Write-Error "[NW-CLEANSYSTEM] $($_.Exception.Message)"
    exit 1
}
) {
        throw "CleanSystem package source_commit is missing or invalid."
    }
    $checkoutCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    $checkoutCommit = ([string]$checkoutCommit).Trim()
    if ($checkoutCommit -notmatch '^[0-9a-fA-F]{40}    # The verifier owns authoritative SDK/DXC/debug/redist absence probes;
    # these preflight checks only catch obvious contamination before fixtures run.
    if (-not [string]::IsNullOrWhiteSpace($env:WindowsSdkDir) -or
        -not [string]::IsNullOrWhiteSpace($env:WindowsSDKVersion)) {
        throw "CleanSystem target has an active Windows SDK environment. Use an uncontaminated target."
    }
    if (Get-Command dxc.exe -ErrorAction SilentlyContinue) {
        throw "CleanSystem target exposes dxc.exe in PATH. Use an uncontaminated target."
    }

    $nt = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    $osBuild = "10.0.$($nt.CurrentBuildNumber).$($nt.UBR)"

    $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) { $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue }
    if (-not $nvidiaSmi) { throw "nvidia-smi is required to bind CleanSystem execution to the physical NVIDIA adapter." }
    $gpuLine = (& $nvidiaSmi.Source --query-gpu=name,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gpuLine)) { throw "nvidia-smi did not return GPU model/driver metadata." }
    $parts = @($gpuLine -split ',', 2 | ForEach-Object { $_.Trim() })
    if ($parts.Count -ne 2) { throw "Unexpected nvidia-smi output: $gpuLine" }
    $gpuModel = $parts[0]
    $nvidiaDriver = $parts[1]

    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $SummaryPath = Join-Path (Split-Path -Parent ([System.IO.Path]::GetFullPath($PackageRoot))) "nw01-clean-system-$stamp.json"
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 is required to run the repository verifier." }

    $args = @(
        (Join-Path $RepoRoot "tools\verify_nw01.py"),
        "--mode", "CleanSystem",
        "--binary-dir", $binaryDir,
        "--evidence-dir", $evidenceDir,
        "--os-build", $osBuild,
        "--gpu-model", $gpuModel,
        "--nvidia-driver-version", $nvidiaDriver,
        "--summary-json", $SummaryPath
    )
    & $python.Source @args
    $code = $LASTEXITCODE

    if (-not (Test-Path $SummaryPath -PathType Leaf)) {
        throw "CleanSystem verifier did not produce summary JSON (exit code $code)."
    }
    if ($code -eq 0) {
        Write-Host "[NW-CLEANSYSTEM] VERIFIED package=$PackageRoot summary=$SummaryPath"
    } else {
        Write-Host "[NW-CLEANSYSTEM] FAILED exit=$code package=$PackageRoot summary=$SummaryPath"
    }
    exit $code
} catch {
    Write-Error "[NW-CLEANSYSTEM] $($_.Exception.Message)"
    exit 1
}
) {
        throw "Unable to resolve verifier checkout HEAD for package provenance."
    }
    if (-not [string]::Equals($packageSourceCommit, $checkoutCommit, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "CleanSystem package source_commit '$packageSourceCommit' does not match verifier checkout HEAD '$checkoutCommit'. Refusing stale package."
    }

    # No bootstrap or build-environment activation is allowed on this role.
    # The verifier owns authoritative SDK/DXC/debug/redist absence probes;
    # these preflight checks only catch obvious contamination before fixtures run.
    if (-not [string]::IsNullOrWhiteSpace($env:WindowsSdkDir) -or
        -not [string]::IsNullOrWhiteSpace($env:WindowsSDKVersion)) {
        throw "CleanSystem target has an active Windows SDK environment. Use an uncontaminated target."
    }
    if (Get-Command dxc.exe -ErrorAction SilentlyContinue) {
        throw "CleanSystem target exposes dxc.exe in PATH. Use an uncontaminated target."
    }

    $nt = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    $osBuild = "10.0.$($nt.CurrentBuildNumber).$($nt.UBR)"

    $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) { $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue }
    if (-not $nvidiaSmi) { throw "nvidia-smi is required to bind CleanSystem execution to the physical NVIDIA adapter." }
    $gpuLine = (& $nvidiaSmi.Source --query-gpu=name,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gpuLine)) { throw "nvidia-smi did not return GPU model/driver metadata." }
    $parts = @($gpuLine -split ',', 2 | ForEach-Object { $_.Trim() })
    if ($parts.Count -ne 2) { throw "Unexpected nvidia-smi output: $gpuLine" }
    $gpuModel = $parts[0]
    $nvidiaDriver = $parts[1]

    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $SummaryPath = Join-Path (Split-Path -Parent ([System.IO.Path]::GetFullPath($PackageRoot))) "nw01-clean-system-$stamp.json"
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { throw "Python 3 is required to run the repository verifier." }

    $args = @(
        (Join-Path $RepoRoot "tools\verify_nw01.py"),
        "--mode", "CleanSystem",
        "--binary-dir", $binaryDir,
        "--evidence-dir", $evidenceDir,
        "--os-build", $osBuild,
        "--gpu-model", $gpuModel,
        "--nvidia-driver-version", $nvidiaDriver,
        "--summary-json", $SummaryPath
    )
    & $python.Source @args
    $code = $LASTEXITCODE

    if (-not (Test-Path $SummaryPath -PathType Leaf)) {
        throw "CleanSystem verifier did not produce summary JSON (exit code $code)."
    }
    if ($code -eq 0) {
        Write-Host "[NW-CLEANSYSTEM] VERIFIED package=$PackageRoot summary=$SummaryPath"
    } else {
        Write-Host "[NW-CLEANSYSTEM] FAILED exit=$code package=$PackageRoot summary=$SummaryPath"
    }
    exit $code
} catch {
    Write-Error "[NW-CLEANSYSTEM] $($_.Exception.Message)"
    exit 1
}
