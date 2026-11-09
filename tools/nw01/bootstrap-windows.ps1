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
$visualStudio = Get-NwVisualStudioSelection -RepoRoot $RepoRoot
$retry = $sources.download_retry
New-Item -ItemType Directory -Force -Path $ToolsRoot | Out-Null
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "nodewerk-nw01-bootstrap"
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

$git = Get-Command git -ErrorAction Stop
$sourceCommit = (& $git.Source -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
$sourceCommit = ([string]$sourceCommit).Trim()
if ($sourceCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Unable to resolve repository HEAD for bootstrap provenance."
}

$provenance = [ordered]@{
    schema_version = "1"
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    source_commit = $sourceCommit
    visual_studio = [ordered]@{
        vcvars_version = $visualStudio.VcVarsVersion
        windows_sdk_version = $visualStudio.WindowsSdkVersion
        components = @($visualStudio.Components)
    }
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
    $probeCommand = $Command
    $probeTool = $Tool
    $probeExpected = $Expected
    $probeCandidate = $candidate
    $findExact = ${function:Find-NwExactCommand}
    $fallbackProbe = {
        & $findExact -Name $probeCommand -Tool $probeTool -Expected $probeExpected -AdditionalCandidates @($probeCandidate)
    }.GetNewClosure()
    $download = Invoke-NwDownloadWithFallback -Name $Name -Uri $Uri -OutFile $archive -RetryPolicy $retry -FallbackProbe $fallbackProbe
    if ($download.FallbackUsed) {
        $found = [string]$download.FallbackValue
        Record-Selection $Name $Expected $found "runner-preinstalled-after-transient-download-failure"
        return (Split-Path -Parent $found)
    }

    # A completed download is trusted only after the pinned digest matches. Integrity
    # failures are hard failures and never enter the runner-fallback path.
    Assert-NwSha256 -Path $archive -Expected $Sha256
    if (Test-Path $InstallRoot) { Remove-Item -Recurse -Force $InstallRoot }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Expand-Archive -Path $archive -DestinationPath $InstallRoot -Force
    $installedExe = Join-Path $InstallRoot $ExecutableRelative
    Assert-NwExactToolVersion -Tool $Tool -Executable $installedExe -Expected $Expected -Label "$Name archive installed" -RepoRoot $RepoRoot
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
    $probeClang = $clang
    $probeLld = $lld
    $probeVersion = $contract.LlvmVersion
    $findExact = ${function:Find-NwExactCommand}
    $fallbackProbe = {
        $c = & $findExact -Name "clang-cl.exe" -Tool "clang-cl" -Expected $probeVersion -AdditionalCandidates @($probeClang)
        $l = & $findExact -Name "lld-link.exe" -Tool "lld-link" -Expected $probeVersion -AdditionalCandidates @($probeLld)
        if ($c -and $l) { return (Split-Path -Parent $c) }
        return $null
    }.GetNewClosure()
    $download = Invoke-NwDownloadWithFallback -Name "LLVM" -Uri $uri -OutFile $installer -RetryPolicy $retry -FallbackProbe $fallbackProbe
    if ($download.FallbackUsed) {
        $foundClangDir = [string]$download.FallbackValue
        Record-Selection "LLVM clang-cl/lld-link" $contract.LlvmVersion (Join-Path $foundClangDir "clang-cl.exe") "runner-preinstalled-after-transient-download-failure"
        return $foundClangDir
    }
    Assert-NwSha256 -Path $installer -Expected ([string]$sources.llvm.sha256)
    if (Test-Path $installRoot) { Remove-Item -Recurse -Force $installRoot }
    $process = Start-Process -Wait -PassThru -FilePath $installer -ArgumentList '/S', "/D=$installRoot"
    if ($process.ExitCode -ne 0) { throw "LLVM installer failed with exit code $($process.ExitCode)" }
    Assert-NwExactToolVersion -Tool "clang-cl" -Executable $clang -Expected $contract.LlvmVersion -Label "LLVM clang-cl installation" -RepoRoot $RepoRoot
    Assert-NwExactToolVersion -Tool "lld-link" -Executable $lld -Expected $contract.LlvmVersion -Label "LLVM lld-link installation" -RepoRoot $RepoRoot
    Record-Selection "LLVM clang-cl/lld-link" $contract.LlvmVersion $clang "downloaded-pinned-artifact"
    return (Split-Path -Parent $clang)
}

function Test-ExistingVisualStudio {
    try {
        # Import validates the exact toolset, the selected cl/link versions and
        # their ownership by VCToolsInstallDir. It throws on any mismatch.
        Import-NwVcVarsEnvironment -WindowsSdkVersion $visualStudio.WindowsSdkVersion -VcVarsVersion $visualStudio.VcVarsVersion -VcVarsFallbackVersion $visualStudio.VcVarsFallbackVersion -MsvcCompilerVersion $contract.MsvcCompilerVersion -MsvcLinkerVersion $contract.MsvcLinkerVersion -RepoRoot $RepoRoot | Out-Null
        return $true
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
    $probeSdk = $visualStudio.WindowsSdkVersion
    $probeVcVars = $visualStudio.VcVarsVersion
    $probeVcVarsFallback = $visualStudio.VcVarsFallbackVersion
    $probeCompiler = $contract.MsvcCompilerVersion
    $probeLinker = $contract.MsvcLinkerVersion
    $probeRepoRoot = $RepoRoot
    $importVcVars = ${function:Import-NwVcVarsEnvironment}
    $fallbackProbe = {
        try {
            & $importVcVars -WindowsSdkVersion $probeSdk -VcVarsVersion $probeVcVars -VcVarsFallbackVersion $probeVcVarsFallback -MsvcCompilerVersion $probeCompiler -MsvcLinkerVersion $probeLinker -RepoRoot $probeRepoRoot | Out-Null
            $cl = (Get-Command cl.exe -ErrorAction Stop).Source
            return $cl
        } catch {
            return $null
        }
    }.GetNewClosure()
    $download = Invoke-NwDownloadWithFallback -Name "Visual Studio Build Tools" -Uri $visualStudio.BootstrapperUrl -OutFile $bootstrapper -RetryPolicy $retry -FallbackProbe $fallbackProbe
    if ($download.FallbackUsed) {
        Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ([string]$download.FallbackValue) "runner-preinstalled-after-transient-download-failure"
        return
    }

    # Microsoft's aka.ms VS bootstrapper is a rolling signed bootstrapper rather than a
    # versioned immutable release artifact. Integrity is established before execution:
    # a configured SHA-256 must match, and the Authenticode signature must be valid and
    # issued to Microsoft. The installed compiler/linker/SDK are then verified exactly.
    if (-not [string]::IsNullOrWhiteSpace($visualStudio.BootstrapperSha256)) {
        Assert-NwSha256 -Path $bootstrapper -Expected $visualStudio.BootstrapperSha256
    }
    Assert-NwAuthenticodeSignature -Path $bootstrapper -SubjectContains $visualStudio.BootstrapperSubjectContains

    $args = @("--quiet", "--wait", "--norestart", "--nocache")
    foreach ($component in $visualStudio.Components) { $args += @("--add", [string]$component) }
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
        $base = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path (Split-Path -Parent $RepoRoot) "nodewerk-evidence" }
        New-Item -ItemType Directory -Force -Path $base | Out-Null
        $shortSha = (& $git.Source -C $RepoRoot rev-parse --short=12 HEAD 2>$null | Select-Object -First 1)
        $sha = ([string]$shortSha).Trim()
        if ([string]::IsNullOrWhiteSpace($sha)) { $sha = "unknown" }
        $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
        $ProvenancePath = Join-Path $base "nw01-bootstrap-$sha-$stamp.json"
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
