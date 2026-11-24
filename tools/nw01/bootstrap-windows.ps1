#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$ToolsRoot = "C:\Tools",
    [string]$ProvenancePath = "",
    [string]$DownloadCacheRoot = $env:NW01_DOWNLOAD_CACHE,
    [switch]$SkipLlvm,
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
if (-not [string]::IsNullOrWhiteSpace($DownloadCacheRoot)) {
    $DownloadCacheRoot = [System.IO.Path]::GetFullPath($DownloadCacheRoot)
    New-Item -ItemType Directory -Force -Path $DownloadCacheRoot | Out-Null
}

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

function Get-PinnedPayload(
    [string]$Name,
    [string]$Uri,
    [string]$Sha256,
    [string]$FileName,
    [scriptblock]$FallbackProbe
) {
    $cacheEnabled = -not [string]::IsNullOrWhiteSpace($DownloadCacheRoot)
    $payloadPath = if ($cacheEnabled) {
        Join-Path $DownloadCacheRoot $FileName
    } else {
        Join-Path $tempRoot $FileName
    }

    if (Test-Path -LiteralPath $payloadPath -PathType Leaf) {
        try {
            Assert-NwSha256 -Path $payloadPath -Expected $Sha256
            Write-Host "[NW-BOOTSTRAP] CACHE_HIT $Name $payloadPath"
            return [pscustomobject]@{
                Path = $payloadPath
                CacheHit = $true
                FallbackUsed = $false
                FallbackValue = $null
            }
        } catch {
            Write-Host "[NW-BOOTSTRAP] CACHE_INVALID $Name $payloadPath"
            Remove-Item -LiteralPath $payloadPath -Force -ErrorAction SilentlyContinue
        }
    }

    $download = Invoke-NwDownloadWithFallback -Name $Name -Uri $Uri -OutFile $payloadPath -RetryPolicy $retry -FallbackProbe $FallbackProbe
    if ($download.FallbackUsed) {
        return [pscustomobject]@{
            Path = $null
            CacheHit = $false
            FallbackUsed = $true
            FallbackValue = $download.FallbackValue
        }
    }
    Assert-NwSha256 -Path $payloadPath -Expected $Sha256
    Write-Host "[NW-BOOTSTRAP] CACHE_MISS $Name $payloadPath"
    return [pscustomobject]@{
        Path = $payloadPath
        CacheHit = $false
        FallbackUsed = $false
        FallbackValue = $null
    }
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

    $probeCommand = $Command
    $probeTool = $Tool
    $probeExpected = $Expected
    $probeCandidate = $candidate
    $findExact = ${function:Find-NwExactCommand}
    $fallbackProbe = {
        & $findExact -Name $probeCommand -Tool $probeTool -Expected $probeExpected -AdditionalCandidates @($probeCandidate)
    }.GetNewClosure()
    $payloadName = ("{0}.zip" -f ($Name -replace '[^A-Za-z0-9_.-]', '_'))
    $download = Get-PinnedPayload -Name $Name -Uri $Uri -Sha256 $Sha256 -FileName $payloadName -FallbackProbe $fallbackProbe
    $archive = [string]$download.Path
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
    $payloadSource = if ($download.CacheHit) { "cached-pinned-artifact" } else { "downloaded-pinned-artifact" }
    Record-Selection $Name $Expected $installedExe $payloadSource
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
    $download = Get-PinnedPayload -Name "LLVM" -Uri $uri -Sha256 ([string]$sources.llvm.sha256) -FileName "LLVM-$($contract.LlvmVersion)-win64.exe" -FallbackProbe $fallbackProbe
    $installer = [string]$download.Path
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
    $payloadSource = if ($download.CacheHit) { "cached-pinned-artifact" } else { "downloaded-pinned-artifact" }
    Record-Selection "LLVM clang-cl/lld-link" $contract.LlvmVersion $clang $payloadSource
    return (Split-Path -Parent $clang)
}

function Test-ExistingVisualStudio {
    param([ref]$FailureReason)
    try {
        # Import validates the exact toolset, the selected cl/link versions and
        # their ownership by VCToolsInstallDir. It throws on any mismatch.
        Import-NwVcVarsEnvironment -WindowsSdkVersion $visualStudio.WindowsSdkVersion -VcVarsVersion $visualStudio.VcVarsVersion -VcVarsFallbackVersion $visualStudio.VcVarsFallbackVersion -MsvcCompilerVersion $contract.MsvcCompilerVersion -MsvcLinkerVersion $contract.MsvcLinkerVersion -RepoRoot $RepoRoot | Out-Null
        return $true
    } catch {
        if ($null -ne $FailureReason) {
            $FailureReason.Value = [string]$_.Exception.Message
        }
        return $false
    }
}

function Get-MissingVisualStudioComponents {
    param([string]$TargetInstallPath = "")

    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        return @($visualStudio.Components)
    }

    $target = ""
    if (-not [string]::IsNullOrWhiteSpace($TargetInstallPath)) {
        $target = [System.IO.Path]::GetFullPath($TargetInstallPath).TrimEnd('\')
    }

    $missing = @()
    foreach ($component in $visualStudio.Components) {
        # Scope the probe to VS 17.x, then (when a target is supplied) require
        # the component to belong to that exact instance. A component present in
        # Enterprise must not make a separate Build Tools instance look complete,
        # and vice versa.
        $installations = @(& $vswhere -version "[17.0,18.0)" -products * -requires ([string]$component) -property installationPath 2>$null)
        $present = $false
        if ($LASTEXITCODE -eq 0) {
            foreach ($installation in $installations) {
                $candidate = ([string]$installation).Trim()
                if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
                if ([string]::IsNullOrWhiteSpace($target)) {
                    $present = $true
                    break
                }
                try {
                    $normalized = [System.IO.Path]::GetFullPath($candidate).TrimEnd('\')
                    if ([string]::Equals($normalized, $target, [System.StringComparison]::OrdinalIgnoreCase)) {
                        $present = $true
                        break
                    }
                } catch { }
            }
        }
        if (-not $present) {
            $missing += [string]$component
        }
    }
    return @($missing)
}

function Get-VisualStudioModifyTarget {
    # Get-NwVisualStudioInstallPaths already restricts candidates to VS 17.x
    # installations with native x64 VC tools and prefers Build Tools before
    # other editions. On windows-2022 this resolves to the preinstalled
    # Enterprise 2022 instance, avoiding a second split VS installation.
    foreach ($path in @(Get-NwVisualStudioInstallPaths)) {
        $candidate = ([string]$path).Trim()
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            return $candidate
        }
    }
    return $null
}

function Get-VisualStudioChannelId {
    param([Parameter(Mandatory)][string]$InstallPath)

    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        try {
            $raw = (& $vswhere -version "[17.0,18.0)" -products * -format json -utf8 2>$null | Out-String)
            foreach ($instance in @($raw | ConvertFrom-Json)) {
                $candidate = [string]$instance.installationPath
                if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
                $a = [System.IO.Path]::GetFullPath($candidate).TrimEnd('\')
                $b = [System.IO.Path]::GetFullPath($InstallPath).TrimEnd('\')
                if ([string]::Equals($a, $b, [System.StringComparison]::OrdinalIgnoreCase) -and
                    -not [string]::IsNullOrWhiteSpace([string]$instance.channelId)) {
                    return [string]$instance.channelId
                }
            }
        } catch { }
    }
    return "VisualStudio.17.Release"
}

function Invoke-VisualStudioInstanceModify {
    param(
        [Parameter(Mandatory)][string]$InstallPath,
        [Parameter(Mandatory)][string[]]$Components
    )

    if ($Components.Count -eq 0) { return }
    $setup = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\setup.exe"
    if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        throw "Visual Studio Installer setup.exe was not found for in-place modification."
    }
    Assert-NwAuthenticodeSignature -Path $setup -SubjectContains $visualStudio.BootstrapperSubjectContains

    Write-Host "[NW-BOOTSTRAP] VS_MODIFY_TARGET=$InstallPath"
    Write-Host "[NW-BOOTSTRAP] VS_MODIFY_COMPONENTS=$($Components -join ',')"

    # ProcessStartInfo.ArgumentList preserves install-path quoting without relying
    # on Start-Process's string re-serialization. The local installer already
    # owns the target instance, so a modify operation does not need a channel
    # override. Preventing an installer self-update removes an unrelated network
    # dependency from the critical path. Keep the package cache: the runner is
    # ephemeral, so deleting freshly downloaded packages only adds cleanup I/O.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $setup
    $startInfo.UseShellExecute = $false
    foreach ($arg in @("modify", "--installPath", $InstallPath, "--quiet", "--norestart", "--noUpdateInstaller")) {
        [void]$startInfo.ArgumentList.Add([string]$arg)
    }
    foreach ($component in $Components) {
        [void]$startInfo.ArgumentList.Add("--add")
        [void]$startInfo.ArgumentList.Add([string]$component)
    }

    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) { throw "Visual Studio Installer modify process could not be started." }
    $process.WaitForExit()
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "Visual Studio existing-instance modification failed with exit code $($process.ExitCode)"
    }
}

function Ensure-VisualStudio {
    $initialFailure = ""
    if (Test-ExistingVisualStudio -FailureReason ([ref]$initialFailure)) {
        Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "preinstalled-exact"
        return
    }

    # Prefer modifying a compatible existing VS 2022 instance. This is the fast
    # path on windows-2022: the image already owns VS2022, x64 VC tools and SDK
    # 26100, so normally only the pinned legacy MSVC component is added.
    $modifyTarget = Get-VisualStudioModifyTarget
    if (-not [string]::IsNullOrWhiteSpace([string]$modifyTarget)) {
        try {
            $missing = @(Get-MissingVisualStudioComponents -TargetInstallPath $modifyTarget)
            $individualMissing = @($missing | Where-Object { [string]$_ -like "Microsoft.VisualStudio.Component.*" })
            if ($individualMissing.Count -eq 0) {
                # Exact selection can fail even when vswhere reports every
                # individual component. Re-assert the explicit individual set
                # before escalating to workload-level repair.
                $individualMissing = @($visualStudio.Components | Where-Object { [string]$_ -like "Microsoft.VisualStudio.Component.*" })
            }
            Invoke-VisualStudioInstanceModify -InstallPath $modifyTarget -Components $individualMissing

            $modifyFailure = ""
            if (Test-ExistingVisualStudio -FailureReason ([ref]$modifyFailure)) {
                Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "modified-existing-vs-instance"
                return
            }

            # If the individual-component repair was insufficient, add only the
            # still-missing explicit entries (which may include the workload)
            # to the same instance. Never request the broad recommended-component set.
            $remaining = @(Get-MissingVisualStudioComponents -TargetInstallPath $modifyTarget)
            if ($remaining.Count -gt 0) {
                Invoke-VisualStudioInstanceModify -InstallPath $modifyTarget -Components $remaining
                $modifyFailure = ""
                if (Test-ExistingVisualStudio -FailureReason ([ref]$modifyFailure)) {
                    Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "modified-existing-vs-instance"
                    return
                }
            }
            Write-Warning "[NW-BOOTSTRAP] existing VS2022 instance remained non-exact after modify: $modifyFailure"
        } catch {
            Write-Warning "[NW-BOOTSTRAP] existing VS2022 modification failed; falling back to isolated Build Tools provisioning: $($_.Exception.Message)"
        }
    }

    # Fallback for hosts without a compatible VS2022 instance. A new Build Tools
    # instance must receive the complete explicit component set so its contract
    # does not depend on components owned by another side-by-side VS instance.
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

    $componentsToInstall = @($visualStudio.Components)
    Write-Host "[NW-BOOTSTRAP] VS_BOOTSTRAP_COMPONENTS=$($componentsToInstall -join ',')"
    $args = @("--quiet", "--wait", "--norestart", "--nocache")
    foreach ($component in $componentsToInstall) { $args += @("--add", [string]$component) }
    $process = Start-Process -Wait -PassThru -FilePath $bootstrapper -ArgumentList $args
    if ($process.ExitCode -notin @(0, 3010)) { throw "Visual Studio Build Tools installation failed with exit code $($process.ExitCode)" }

    $finalFailure = ""
    if (-not (Test-ExistingVisualStudio -FailureReason ([ref]$finalFailure))) {
        throw "Visual Studio provisioning completed, but exact MSVC $($contract.MsvcCompilerVersion), linker $($contract.MsvcLinkerVersion), and SDK $($contract.WindowsSdkVersion) were not selected: $finalFailure"
    }
    Record-Selection "MSVC/Windows SDK" "$($contract.MsvcCompilerVersion) / $($contract.WindowsSdkVersion)" ((Get-Command cl.exe).Source) "installed-vs-buildtools-components"
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

    $llvmBin = $null
    if (-not $SkipLlvm) {
        $llvmBin = Ensure-Llvm
    } else {
        Write-Host "[NW-BOOTSTRAP] LLVM provisioning skipped for MSVC-only consumer"
        $provenance.tools["LLVM clang-cl/lld-link"] = [ordered]@{
            expected_version = $contract.LlvmVersion
            executable = $null
            source = "not-required-msvc-only"
        }
    }

    $dxcUri = Expand-NwTemplate -Template ([string]$sources.dxc.url_template) -Version $contract.DxcVersion
    $dxcRoot = Join-Path $ToolsRoot ("dxc-{0}" -f $contract.DxcVersion)
    $dxcBin = Ensure-ZipTool "DXC" "dxc.exe" "dxc" $contract.DxcVersion $dxcUri ([string]$sources.dxc.sha256) $dxcRoot "bin\x64\dxc.exe" "bin\x64"

    foreach ($path in @($cmakeBin, $ninjaBin, $llvmBin, $dxcBin)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$path)) {
            Add-NwPathEntry -Path $path -ExportGitHubPath:$Ci
        }
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
