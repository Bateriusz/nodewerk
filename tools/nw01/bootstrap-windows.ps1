#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$ToolsRoot = "C:\Tools",
    [string]$ProvenancePath = "",
    [switch]$Ci
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $IsWindows) {
    Write-Error "NW-01 Full-host bootstrap is Windows-only. Do not run it on a CleanSystem target."
    exit 1
}

$env:NW_TOOLS_ROOT = $ToolsRoot
Import-Module (Join-Path $PSScriptRoot "Nw01.Windows.psm1") -Force
$contract = Get-NwToolchainContract -RepoRoot $RepoRoot
$sources = Get-NwProvisioningSources -RepoRoot $RepoRoot
$retry = $sources.download_retry
New-Item -ItemType Directory -Force -Path $ToolsRoot | Out-Null
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "nodewerk-nw01-bootstrap"
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

$provenance = [ordered]@{
    schema_version = "1"
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    tools = [ordered]@{}
}

function Record-Selection([string]$Name, [string]$Expected, [string]$Executable, [string]$Source) {
    $provenance.tools[$Name] = [ordered]@{
        expected_version = $Expected
        executable = $Executable
        source = $Source
    }
    Write-Host ("[NW-BOOTSTRAP] {0} {1}: {2} ({3})" -f $Name, $Expected, $Executable, $Source)
}

function Find-Exact([string]$Name, [string]$Tool, [string]$Expected, [string[]]$Candidates) {
    return Find-NwExactCommand -Name $Name -Tool $Tool -Expected $Expected -AdditionalCandidates $Candidates
}

function Download-WithFallback(
    [string]$Name,
    [string]$Uri,
    [string]$OutFile,
    [scriptblock]$FallbackProbe
) {
    $result = Invoke-NwDownload -Uri $Uri -OutFile $OutFile -RetryPolicy $retry
    if ($result.Success) { return $true }
    if (-not $result.TransientFailure) { throw "Non-transient download failure for $Name: $($result.Error)" }
    Write-Warning "[NW-BOOTSTRAP] transient download failure after $($result.Attempts) attempts for $Name; re-checking exact runner-provided installation."
    $fallback = & $FallbackProbe
    if ($fallback) { return $fallback }
    throw "Transient download failure for $Name and no exact pinned runner-provided fallback is available: $($result.Error)"
}

function Ensure-ZipTool(
    [string]$Name,
    [string]$Command,
    [string]$Tool,
    [string]$Expected,
    [string]$Uri,
    [string]$Sha256,
    [string]$InstallRoot,
    [string]$ExecutableRelative,
    [string]$PathRelative
) {
    $candidate = Join-Path $InstallRoot $ExecutableRelative
    $exact = Find-Exact $Command $Tool $Expected @($candidate)
    if ($exact) {
        Record-Selection $Name $Expected $exact "preinstalled-exact"
        return (Split-Path -Parent $exact)
    }

    $archive = Join-Path $tempRoot ("{0}.zip" -f ($Name -replace '[^A-Za-z0-9_.-]', '_'))
    Remove-Item -Force -ErrorAction SilentlyContinue $archive
    $fallbackProbe = {
        $found = Find-Exact $Command $Tool $Expected @($candidate)
        if ($found) {
            Record-Selection $Name $Expected $found "runner-preinstalled-after-transient-download-failure"
            return (Split-Path -Parent $found)
        }
        return $null
    }
    $download = Download-WithFallback $Name $Uri $archive $fallbackProbe
    if ($download -is [string]) { return $download }

    # A completed download is trusted only after the pinned digest matches. Integrity
    # failures are hard failures and never enter the runner-fallback path.
    Assert-NwSha256 -Path $archive -Expected $Sha256
    if (Test-Path $InstallRoot) { Remove-Item -Recurse -Force $InstallRoot }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Expand-Archive -Path $archive -DestinationPath $InstallRoot -Force
    $installedExe = Join-Path $InstallRoot $ExecutableRelative
    if (-not (Test-NwExactVersion -Tool $Tool -Executable $installedExe -Expected $Expected)) {
        throw "$Name archive installed but exact version $Expected was not detected at $installedExe"
    }
    Record-Selection $Name $Expected $installedExe "downloaded-pinned-artifact"
    return (Join-Path $InstallRoot $PathRelative)
}

function Ensure-Llvm {
    $installRoot = Join-Path $ToolsRoot ("LLVM-{0}" -f $contract.LlvmVersion)
    $clang = Join-Path $installRoot "bin\clang-cl.exe"
    $lld = Join-Path $installRoot "bin\lld-link.exe"
    $existingClang = Find-Exact "clang-cl.exe" "clang-cl" $contract.LlvmVersion @($clang)
    $existingLld = Find-Exact "lld-link.exe" "lld-link" $contract.LlvmVersion @($lld)
    if ($existingClang -and $existingLld) {
        Record-Selection "LLVM clang-cl/lld-link" $contract.LlvmVersion $existingClang "preinstalled-exact"
        return (Split-Path -Parent $existingClang)
    }

    $uri = Expand-NwTemplate -Template ([string]$sources.llvm.url_template) -Version $contract.LlvmVersion
    $installer = Join-Path $tempRoot "LLVM-$($contract.LlvmVersion)-win64.exe"
    Remove-Item -Force -ErrorAction SilentlyContinue $installer
    $fallbackProbe = {
        $c = Find-Exact "clang-cl.exe" "clang-cl" $contract.LlvmVersion @($clang)
        $l = Find-Exact "lld-link.exe" "lld-link" $contract.LlvmVersion @($lld)
        if ($c -and $l) {
            Record-Selection "LLVM clang-cl/lld-link" $contract.LlvmVersion $c "runner-preinstalled-after-transient-download-failure"
            return (Split-Path -Parent $c)
        }
        return $null
    }
    $download = Download-WithFallback "LLVM" $uri $installer $fallbackProbe
    if ($download -is [string]) { return $download }
    Assert-NwSha256 -Path $installer -Expected ([string]$sources.llvm.sha256)
    if (Test-Path $installRoot) { Remove-Item -Recurse -Force $installRoot }
    $process = Start-Process -Wait -PassThru -FilePath $installer -ArgumentList '/S', "/D=$installRoot"
    if ($process.ExitCode -ne 0) { throw "LLVM installer failed with exit code $($process.ExitCode)" }
    if (-not (Test-NwExactVersion -Tool "clang-cl" -Executable $clang -Expected $contract.LlvmVersion) -or
        -not (Test-NwExactVersion -Tool "lld-link" -Executable $lld -Expected $contract.LlvmVersion)) {
        throw "LLVM installation completed but clang-cl/lld-link do not match pinned $($contract.LlvmVersion)."
    }
    Record-Selection "LLVM clang-cl/lld-link" $contract.LlvmVersion $clang "downloaded-pinned-artifact"
    return (Split-Path -Parent $clang)
}

function Test-ExistingVisualStudio {
    try {
        Import-NwVcVarsEnvironment -WindowsSdkVersion $contract.WindowsSdkVersion -VcVarsVersion ([string]$sources.visual_studio.vcvars_version) | Out-Null
        $cl = (Get-Command cl.exe -ErrorAction Stop).Source
        $link = (Get-Command link.exe -ErrorAction Stop).Source
        return ((Test-NwExactVersion -Tool "cl" -Executable $cl -Expected $contract.MsvcCompilerVersion) -and
                (Test-NwExactVersion -Tool "link" -Executable $link -Expected $contract.MsvcLinkerVersion))
    } catch {
        return $false
    }
}

function Ensure-VisualStudio {
    if (Test-ExistingVisualStudio) {
        Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "preinstalled-exact"
        return
    }

    $bootstrapper = Join-Path $tempRoot "vs_buildtools.exe"
    Remove-Item -Force -ErrorAction SilentlyContinue $bootstrapper
    $download = Invoke-NwDownload -Uri ([string]$sources.visual_studio.bootstrapper_url) -OutFile $bootstrapper -RetryPolicy $retry
    if (-not $download.Success) {
        if ($download.TransientFailure -and (Test-ExistingVisualStudio)) {
            Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "runner-preinstalled-after-transient-download-failure"
            return
        }
        throw "Visual Studio Build Tools bootstrapper download failed: $($download.Error)"
    }

    # Microsoft's aka.ms VS bootstrapper is a rolling signed bootstrapper rather than a
    # versioned immutable release artifact. The installed compiler/linker/SDK are therefore
    # the fail-closed contract boundary and are verified exactly after installation.
    $args = @("--quiet", "--wait", "--norestart", "--nocache")
    foreach ($component in $sources.visual_studio.components) { $args += @("--add", [string]$component) }
    $args += "--includeRecommended"
    $process = Start-Process -Wait -PassThru -FilePath $bootstrapper -ArgumentList $args
    if ($process.ExitCode -notin @(0, 3010)) { throw "Visual Studio Build Tools installation failed with exit code $($process.ExitCode)" }
    if (-not (Test-ExistingVisualStudio)) {
        throw "Visual Studio installation completed, but exact MSVC $($contract.MsvcCompilerVersion), linker $($contract.MsvcLinkerVersion), and SDK $($contract.WindowsSdkVersion) were not selected."
    }
    Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "installed-vs-components"
}

try {
    Ensure-VisualStudio

    $cmakeUri = Expand-NwTemplate -Template ([string]$sources.cmake.url_template) -Version $contract.CMakeVersion
    $cmakeRoot = Join-Path $ToolsRoot ("CMake-{0}" -f $contract.CMakeVersion)
    $cmakeRel = "cmake-$($contract.CMakeVersion)-windows-x86_64\bin\cmake.exe"
    $cmakeBin = Ensure-ZipTool "CMake" "cmake.exe" "cmake" $contract.CMakeVersion $cmakeUri ([string]$sources.cmake.sha256) $cmakeRoot $cmakeRel "cmake-$($contract.CMakeVersion)-windows-x86_64\bin"

    $ninjaUri = Expand-NwTemplate -Template ([string]$sources.ninja.url_template) -Version $contract.NinjaVersion
    $ninjaRoot = Join-Path $ToolsRoot ("Ninja-{0}" -f $contract.NinjaVersion)
    $ninjaBin = Ensure-ZipTool "Ninja" "ninja.exe" "ninja" $contract.NinjaVersion $ninjaUri ([string]$sources.ninja.sha256) $ninjaRoot "ninja.exe" "."

    $llvmBin = Ensure-Llvm

    $dxcUri = Expand-NwTemplate -Template ([string]$sources.dxc.url_template) -Version $contract.DxcVersion
    $dxcRoot = Join-Path $ToolsRoot ("dxc-{0}" -f $contract.DxcVersion)
    $dxcBin = Ensure-ZipTool "DXC" "dxc.exe" "dxc" $contract.DxcVersion $dxcUri ([string]$sources.dxc.sha256) $dxcRoot "bin\x64\dxc.exe" "bin\x64"

    foreach ($path in @($cmakeBin, $ninjaBin, $llvmBin, $dxcBin)) {
        Add-NwPathEntry -Path $path -ExportGitHubPath:$Ci
    }

    if ([string]::IsNullOrWhiteSpace($ProvenancePath)) {
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $RepoRoot "artifacts\nw01" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $ProvenancePath = Join-Path $base "bootstrap-provenance.json"
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ProvenancePath) | Out-Null
    }
    $provenance | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $ProvenancePath
    Write-Host "[NW-BOOTSTRAP] provenance: $ProvenancePath"
    exit 0
} catch {
    Write-Error "[NW-BOOTSTRAP] $($_.Exception.Message)"
    exit 1
}
