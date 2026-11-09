Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NwRepoRoot {
    [CmdletBinding()]
    param([string]$RepoRoot = "")
    if (-not [string]::IsNullOrWhiteSpace($RepoRoot)) {
        return (Resolve-Path $RepoRoot).Path
    }
    return (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
}

function Get-NwToolchainContract {
    [CmdletBinding()]
    param([string]$RepoRoot = "")

    $root = Get-NwRepoRoot -RepoRoot $RepoRoot
    $manifestPath = Join-Path $root "benchmarks/toolchain-manifest.v1.json"
    if (-not (Test-Path $manifestPath -PathType Leaf)) {
        throw "Toolchain manifest not found: $manifestPath"
    }
    $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
    $selected = $manifest.selected_toolchain
    $pins = $manifest.pinned_dependencies
    $clang = $manifest.evaluated_toolchains | Where-Object { $_.toolchain_id -eq "clang-cl-lld" } | Select-Object -First 1
    $msvc = $manifest.evaluated_toolchains | Where-Object { $_.toolchain_id -eq "msvc-ltcg" } | Select-Object -First 1
    if (-not $clang -or -not $msvc) {
        throw "Toolchain manifest does not contain both clang-cl-lld and msvc-ltcg evaluated toolchains."
    }

    [pscustomobject]@{
        RepoRoot = $root
        CMakeVersion = [string]$pins.cmake_version
        NinjaVersion = [string]$pins.ninja_version
        LlvmVersion = [string]$clang.compiler_version
        DxcVersion = [string]$pins.dxc_version
        MsvcCompilerVersion = [string]$msvc.compiler_version
        MsvcLinkerVersion = [string]$msvc.linker_version
        WindowsSdkVersion = [string]$pins.windows_sdk_version
        SelectedToolchain = [string]$selected.toolchain_id
    }
}

function Get-NwProvisioningSources {
    [CmdletBinding()]
    param([string]$RepoRoot = "")
    $root = Get-NwRepoRoot -RepoRoot $RepoRoot
    $path = Join-Path $root "tools/nw01/toolchain-sources.json"
    if (-not (Test-Path $path -PathType Leaf)) {
        throw "Provisioning source manifest not found: $path"
    }
    return Get-Content -Raw $path | ConvertFrom-Json
}

function Expand-NwTemplate {
    param([Parameter(Mandatory)][string]$Template, [Parameter(Mandatory)][string]$Version)
    return $Template.Replace("{version}", $Version)
}

function Get-NwCommandOutput {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [string[]]$Arguments = @("--version")
    )
    try {
        return (& $Executable @Arguments 2>&1 | Out-String).Trim()
    } catch {
        return ""
    }
}

function Test-NwExactVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet("cmake", "ninja", "clang-cl", "lld-link", "dxc", "cl", "link")][string]$Tool,
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$Expected
    )

    if (-not (Test-Path $Executable -PathType Leaf)) { return $false }
    $args = @("--version")
    if ($Tool -in @("cl", "link")) { $args = @() }
    $output = Get-NwCommandOutput -Executable $Executable -Arguments $args
    if ([string]::IsNullOrWhiteSpace($output)) { return $false }

    switch ($Tool) {
        "cmake" { return $output -match ("cmake version\s+" + [regex]::Escape($Expected) + "(?:\s|$)") }
        "ninja" { return (($output -split "`r?`n")[0].Trim() -eq $Expected) }
        "clang-cl" { return $output -match ("clang version\s+" + [regex]::Escape($Expected) + "(?:\s|$)") }
        "lld-link" { return $output -match ("LLD\s+" + [regex]::Escape($Expected) + "(?:\s|$)") }
        "dxc" { return $output -match [regex]::Escape($Expected) }
        "cl" { return $output -match ("Compiler Version\s+" + [regex]::Escape($Expected) + "(?:\s|$)") }
        "link" { return $output -match ("Linker Version\s+" + [regex]::Escape($Expected) + "(?:\s|$)") }
    }
}

function Find-NwExactCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Tool,
        [Parameter(Mandatory)][string]$Expected,
        [string[]]$AdditionalCandidates = @()
    )

    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $AdditionalCandidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) { $candidates.Add($candidate) }
    }
    foreach ($cmd in @(Get-Command $Name -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source) { $candidates.Add([string]$cmd.Source) }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-NwExactVersion -Tool $Tool -Executable $candidate -Expected $Expected) {
            return $candidate
        }
    }
    return $null
}

function Add-NwPathEntry {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$ExportGitHubPath
    )
    if (-not (Test-Path $Path -PathType Container)) {
        throw "PATH directory does not exist: $Path"
    }

    # vcvars64.bat prepends Windows SDK tool directories, and recent Windows SDK
    # images may carry a newer dxc.exe. Merely detecting that our pinned path is
    # already somewhere in PATH is therefore insufficient: deterministic
    # execution requires the repository-selected standalone directory to be
    # moved to the front every time the build environment is entered.
    $normalized = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $parts = @(
        $env:PATH -split ';' |
            Where-Object {
                if ([string]::IsNullOrWhiteSpace($_)) { return $false }
                try {
                    return [System.IO.Path]::GetFullPath($_).TrimEnd('\') -ne $normalized
                } catch {
                    return $_ -ne $Path
                }
            }
    )
    $env:PATH = (@($Path) + $parts) -join ';'

    if ($ExportGitHubPath) {
        if ([string]::IsNullOrWhiteSpace($env:GITHUB_PATH)) {
            throw "GITHUB_PATH is not available."
        }
        Add-Content -Path $env:GITHUB_PATH -Value $Path
    }
}

function Test-NwTransientDownloadException {
    param([Parameter(Mandatory)]$Exception)
    $message = [string]$Exception.Message
    $statusCode = $null
    if ($Exception.PSObject.Properties.Name -contains "Response" -and $Exception.Response) {
        try { $statusCode = [int]$Exception.Response.StatusCode } catch { }
    }
    if ($statusCode -in @(408, 425, 429) -or ($statusCode -ge 500 -and $statusCode -le 599)) {
        return $true
    }
    return $message -match "(?i)(timed? out|timeout|connection.*(closed|reset|refused)|temporar|name resolution|dns|network.*unreachable|5\d\d|429)"
}

function Invoke-NwDownload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile,
        [Parameter(Mandatory)]$RetryPolicy
    )

    $attempts = [int]$RetryPolicy.max_attempts
    $delay = [double]$RetryPolicy.initial_delay_seconds
    $multiplier = [double]$RetryPolicy.backoff_multiplier
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            Write-Host "[NW-BOOTSTRAP] download attempt $attempt/${attempts}: $Uri"
            Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
            return [pscustomobject]@{ Success = $true; TransientFailure = $false; Attempts = $attempt; Error = $null }
        } catch {
            $transient = Test-NwTransientDownloadException -Exception $_.Exception
            if (-not $transient) { throw }
            if ($attempt -eq $attempts) {
                return [pscustomobject]@{ Success = $false; TransientFailure = $true; Attempts = $attempt; Error = $_.Exception.Message }
            }
            Start-Sleep -Seconds ([int][math]::Ceiling($delay))
            $delay *= $multiplier
        }
    }
    throw "Unreachable download retry state for $Uri"
}

function Assert-NwSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected
    )
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) {
        throw "SHA-256 mismatch for $Path. expected=$Expected actual=$actual"
    }
}

function Get-NwVisualStudioInstallPath {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere -PathType Leaf)) { return $null }
    $path = & $vswhere -products Microsoft.VisualStudio.Product.BuildTools -version "[17.0,18.0)" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath -latest
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($path | Out-String))) { return $null }
    return ([string]($path | Select-Object -First 1)).Trim()
}

function Import-NwVcVarsEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$WindowsSdkVersion,
        [Parameter(Mandatory)][string]$VcVarsVersion,
        [switch]$ExportGitHubEnv
    )

    $vsInstall = Get-NwVisualStudioInstallPath
    if (-not $vsInstall) { throw "Visual Studio 2022 Build Tools installation was not found." }
    $vcvars = Join-Path $vsInstall "VC\Auxiliary\Build\vcvars64.bat"
    if (-not (Test-Path $vcvars -PathType Leaf)) { throw "vcvars64.bat not found at $vcvars" }

    $cmdFile = Join-Path ([System.IO.Path]::GetTempPath()) ("nw01-vcvars-{0}.cmd" -f [guid]::NewGuid().ToString("N"))
    try {
        @"
@echo off
call "$vcvars" $WindowsSdkVersion -vcvars_ver=$VcVarsVersion
if errorlevel 1 exit /b %errorlevel%
set
"@ | Set-Content -Path $cmdFile -Encoding ascii
        $envDump = & $env:ComSpec /d /c $cmdFile
        if ($LASTEXITCODE -ne 0) { throw "vcvars64.bat failed with exit code $LASTEXITCODE" }
    } finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $cmdFile
    }

    $required = @("Path", "INCLUDE", "LIB", "LIBPATH", "VCINSTALLDIR", "VCToolsInstallDir", "VCToolsVersion", "WindowsSdkDir", "WindowsSDKVersion", "WindowsSdkVerBinPath", "UCRTVersion", "UniversalCRTSdkDir")
    $captured = @{}
    foreach ($line in $envDump) {
        if ($line -match "^([^=]+)=(.*)$") {
            $name = $matches[1]
            if ($required -contains $name) {
                $value = $matches[2]
                $captured[$name] = $value
                Set-Item -Path "Env:$name" -Value $value
            }
        }
    }
    foreach ($name in @("Path", "INCLUDE", "LIB", "LIBPATH", "VCToolsInstallDir", "VCToolsVersion", "WindowsSDKVersion")) {
        if (-not $captured.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$captured[$name])) {
            throw "vcvars64.bat did not export required environment variable $name"
        }
    }
    if (($env:WindowsSDKVersion.TrimEnd('\')) -ne $WindowsSdkVersion) {
        throw "vcvars selected Windows SDK '$env:WindowsSDKVersion', expected '$WindowsSdkVersion'"
    }
    if (-not $env:VCToolsVersion.StartsWith("14.40", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "vcvars selected VCToolsVersion '$env:VCToolsVersion', expected 14.40.x"
    }

    if ($ExportGitHubEnv) {
        if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) { throw "GITHUB_ENV is not available." }
        foreach ($name in $required) {
            if ($captured.ContainsKey($name)) { Add-Content -Path $env:GITHUB_ENV -Value "$name=$($captured[$name])" }
        }
    }
    return [pscustomobject]@{ VisualStudioInstall = $vsInstall; VcVars = $vcvars; Environment = $captured }
}

function Get-NwStandaloneToolPaths {
    [CmdletBinding()]
    param([string]$RepoRoot = "")
    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    $toolsRoot = if ($env:NW_TOOLS_ROOT) { $env:NW_TOOLS_ROOT } else { "C:\Tools" }
    $cmakeRoot = Join-Path $toolsRoot ("CMake-{0}" -f $contract.CMakeVersion)
    $cmakeNested = Join-Path $cmakeRoot ("cmake-{0}-windows-x86_64\bin" -f $contract.CMakeVersion)
    [pscustomobject]@{
        CMakeBin = $cmakeNested
        NinjaBin = Join-Path $toolsRoot ("Ninja-{0}" -f $contract.NinjaVersion)
        LlvmBin = Join-Path $toolsRoot ("LLVM-{0}\bin" -f $contract.LlvmVersion)
        DxcBin = Join-Path $toolsRoot ("dxc-{0}\bin\x64" -f $contract.DxcVersion)
    }
}

function Get-NwExactStandaloneToolSelection {
    [CmdletBinding()]
    param([string]$RepoRoot = "")

    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    $paths = Get-NwStandaloneToolPaths -RepoRoot $RepoRoot
    $specs = @(
        @{ Key = "CMake"; Name = "cmake.exe"; Tool = "cmake"; Expected = $contract.CMakeVersion; Canonical = (Join-Path $paths.CMakeBin "cmake.exe") },
        @{ Key = "Ninja"; Name = "ninja.exe"; Tool = "ninja"; Expected = $contract.NinjaVersion; Canonical = (Join-Path $paths.NinjaBin "ninja.exe") },
        @{ Key = "Clang"; Name = "clang-cl.exe"; Tool = "clang-cl"; Expected = $contract.LlvmVersion; Canonical = (Join-Path $paths.LlvmBin "clang-cl.exe") },
        @{ Key = "Lld"; Name = "lld-link.exe"; Tool = "lld-link"; Expected = $contract.LlvmVersion; Canonical = (Join-Path $paths.LlvmBin "lld-link.exe") },
        @{ Key = "Dxc"; Name = "dxc.exe"; Tool = "dxc"; Expected = $contract.DxcVersion; Canonical = (Join-Path $paths.DxcBin "dxc.exe") }
    )

    $resolved = [ordered]@{}
    foreach ($spec in $specs) {
        $exe = Find-NwExactCommand -Name ([string]$spec.Name) -Tool ([string]$spec.Tool) -Expected ([string]$spec.Expected) -AdditionalCandidates @([string]$spec.Canonical)
        if ([string]::IsNullOrWhiteSpace([string]$exe)) {
            throw "Exact pinned standalone tool '$($spec.Key)' version '$($spec.Expected)' is not available."
        }
        $resolved[$spec.Key] = [System.IO.Path]::GetFullPath([string]$exe)
    }

    $clangDir = Split-Path -Parent $resolved.Clang
    $lldDir = Split-Path -Parent $resolved.Lld
    if ([System.IO.Path]::GetFullPath($clangDir) -ne [System.IO.Path]::GetFullPath($lldDir)) {
        throw "Pinned clang-cl and lld-link were resolved from different tool directories: '$clangDir' vs '$lldDir'"
    }

    return [pscustomobject]@{
        CMakeExecutable = $resolved.CMake
        NinjaExecutable = $resolved.Ninja
        ClangExecutable = $resolved.Clang
        LldExecutable = $resolved.Lld
        DxcExecutable = $resolved.Dxc
        CMakeBin = (Split-Path -Parent $resolved.CMake)
        NinjaBin = (Split-Path -Parent $resolved.Ninja)
        LlvmBin = $clangDir
        DxcBin = (Split-Path -Parent $resolved.Dxc)
    }
}

function Enter-NwBuildEnvironment {
    [CmdletBinding()]
    param(
        [string]$RepoRoot = "",
        [switch]$ExportGitHubEnv
    )
    if (-not $IsWindows) { throw "NW-01 Windows build environment can only be entered on Windows." }
    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    $sources = Get-NwProvisioningSources -RepoRoot $RepoRoot
    # Import vcvars first because it prepends SDK locations. Then resolve each
    # standalone tool by exact version (canonical C:\Tools copy or exact
    # runner-provided fallback) and deliberately move that selected directory
    # ahead of the SDK. This prevents Windows SDK's newer dxc.exe from
    # shadowing the pinned DXC 1.8.2407 used by NW-01.
    Import-NwVcVarsEnvironment -WindowsSdkVersion $contract.WindowsSdkVersion -VcVarsVersion ([string]$sources.visual_studio.vcvars_version) | Out-Null
    $selection = Get-NwExactStandaloneToolSelection -RepoRoot $RepoRoot

    foreach ($entry in @($selection.CMakeBin, $selection.NinjaBin, $selection.LlvmBin, $selection.DxcBin)) {
        Add-NwPathEntry -Path $entry
    }

    if ($ExportGitHubEnv) {
        if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
            throw "GITHUB_ENV is not available."
        }
        foreach ($name in @("Path", "INCLUDE", "LIB", "LIBPATH", "VCINSTALLDIR", "VCToolsInstallDir", "VCToolsVersion", "WindowsSdkDir", "WindowsSDKVersion", "WindowsSdkVerBinPath", "UCRTVersion", "UniversalCRTSdkDir")) {
            $value = [Environment]::GetEnvironmentVariable($name, "Process")
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                Add-Content -Path $env:GITHUB_ENV -Value "$name=$value"
            }
        }
    }

    # Fail immediately if command resolution still disagrees with the exact
    # pinned DXC path after vcvars/path activation.
    $resolvedDxc = (Get-Command dxc.exe -ErrorAction Stop).Source
    if ([System.IO.Path]::GetFullPath($resolvedDxc) -ne [System.IO.Path]::GetFullPath($selection.DxcExecutable)) {
        throw "Pinned DXC precedence failure: command resolution selected '$resolvedDxc', expected '$($selection.DxcExecutable)'"
    }

    return [pscustomobject]@{ Contract = $contract; Paths = $selection }
}

function Get-NwDoctorResults {
    [CmdletBinding()]
    param([string]$RepoRoot = "")
    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    $paths = Get-NwStandaloneToolPaths -RepoRoot $RepoRoot
    $specs = @(
        @{ Name = "CMake"; Tool = "cmake"; Expected = $contract.CMakeVersion; Command = "cmake.exe"; Candidate = (Join-Path $paths.CMakeBin "cmake.exe") },
        @{ Name = "Ninja"; Tool = "ninja"; Expected = $contract.NinjaVersion; Command = "ninja.exe"; Candidate = (Join-Path $paths.NinjaBin "ninja.exe") },
        @{ Name = "clang-cl"; Tool = "clang-cl"; Expected = $contract.LlvmVersion; Command = "clang-cl.exe"; Candidate = (Join-Path $paths.LlvmBin "clang-cl.exe") },
        @{ Name = "lld-link"; Tool = "lld-link"; Expected = $contract.LlvmVersion; Command = "lld-link.exe"; Candidate = (Join-Path $paths.LlvmBin "lld-link.exe") },
        @{ Name = "DXC"; Tool = "dxc"; Expected = $contract.DxcVersion; Command = "dxc.exe"; Candidate = (Join-Path $paths.DxcBin "dxc.exe") },
        @{ Name = "MSVC cl"; Tool = "cl"; Expected = $contract.MsvcCompilerVersion; Command = "cl.exe"; Candidate = "" },
        @{ Name = "MSVC link"; Tool = "link"; Expected = $contract.MsvcLinkerVersion; Command = "link.exe"; Candidate = "" }
    )
    $results = foreach ($spec in $specs) {
        $exe = Find-NwExactCommand -Name ([string]$spec.Command) -Tool ([string]$spec.Tool) -Expected ([string]$spec.Expected) -AdditionalCandidates @([string]$spec.Candidate)
        $ok = -not [string]::IsNullOrWhiteSpace([string]$exe)
        $detected = "missing-or-mismatch"
        if ($ok) {
            $args = if ($spec.Tool -in @("cl", "link")) { @() } else { @("--version") }
            $detected = ((Get-NwCommandOutput -Executable $exe -Arguments $args) -split "`r?`n")[0]
        }
        [pscustomobject]@{ Name = $spec.Name; Expected = [string]$spec.Expected; Detected = $detected; Pass = $ok; Executable = $exe }
    }
    $sdkOk = $false
    $sdkDetected = if ($env:WindowsSDKVersion) { $env:WindowsSDKVersion.TrimEnd('\') } else { "missing" }
    if ($sdkDetected -eq $contract.WindowsSdkVersion) { $sdkOk = $true }
    $results += [pscustomobject]@{ Name = "Windows SDK"; Expected = $contract.WindowsSdkVersion; Detected = $sdkDetected; Pass = $sdkOk; Executable = $env:WindowsSdkDir }
    return $results
}

Export-ModuleMember -Function *-Nw*
