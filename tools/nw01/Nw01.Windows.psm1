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

    $windowsSdkVersion = [string]$pins.windows_sdk_version
    $msvcCompilerVersion = [string]$msvc.compiler_version
    $msvcLinkerVersion = [string]$msvc.linker_version
    # The Visual Studio toolset version is the first three numeric components of
    # the MSVC linker (14.40.33821.0 -> 14.40.33821). It is derived from the
    # authoritative toolchain manifest rather than re-pinned in provisioning
    # sources so the vcvars selector and the expected binary versions cannot drift.
    $msvcToolsetVersion = ""
    if ($msvcLinkerVersion -match '^([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.[0-9]+)?$') {
        $msvcToolsetVersion = "$($matches[1]).$($matches[2]).$($matches[3])"
    } elseif ($msvcCompilerVersion -match '^19\.([0-9]+)\.([0-9]+)$') {
        $msvcToolsetVersion = "14.$($matches[1]).$($matches[2])"
    } else {
        throw "Toolchain manifest does not expose a usable MSVC toolset version (compiler='$msvcCompilerVersion', linker='$msvcLinkerVersion')."
    }
    $sdkParts = @($windowsSdkVersion -split '\.')
    if ($sdkParts.Count -lt 3 -or $sdkParts[2] -notmatch '^[0-9]+$') {
        throw "Toolchain manifest pins an invalid Windows SDK version: '$windowsSdkVersion'."
    }

    [pscustomobject]@{
        RepoRoot = $root
        CMakeVersion = [string]$pins.cmake_version
        NinjaVersion = [string]$pins.ninja_version
        LlvmVersion = [string]$clang.compiler_version
        DxcVersion = [string]$pins.dxc_version
        MsvcCompilerVersion = $msvcCompilerVersion
        MsvcLinkerVersion = $msvcLinkerVersion
        MsvcToolsetVersion = $msvcToolsetVersion
        MsvcToolsetMajorMinor = (($msvcToolsetVersion -split '\.')[0..1] -join '.')
        WindowsSdkVersion = $windowsSdkVersion
        WindowsSdkBuild = [string]$sdkParts[2]
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

function Get-NwToolVersionIdentities {
    <#
        Repository-owned mapping from a semantic pinned version to the exact
        binary identity reported by the distributed executable. The pin in the
        toolchain manifest is a contract version; some upstream release archives
        report an additional build component (for example DXC 1.8.2407 reports
        "1.8.2407.12"). This file is the single place where that identity is
        recorded so the verifier does not accept an arbitrary nearby version.
    #>
    [CmdletBinding()]
    param([string]$RepoRoot = "")
    $root = Get-NwRepoRoot -RepoRoot $RepoRoot
    $path = Join-Path $root "tools/nw01/tool-version-identities.json"
    if (-not (Test-Path $path -PathType Leaf)) {
        throw "Tool version identity mapping not found: $path"
    }
    return Get-Content -Raw $path | ConvertFrom-Json
}

function Get-NwReportedToolVersions {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Tool,
        [Parameter(Mandatory)][string]$Expected,
        [string]$RepoRoot = ""
    )
    $mapping = Get-NwToolVersionIdentities -RepoRoot $RepoRoot
    $toolProperty = $mapping.PSObject.Properties[$Tool]
    if ($null -eq $toolProperty -or $null -eq $toolProperty.Value) {
        throw "Tool version identity mapping has no entry for '$Tool'."
    }
    $versionProperty = $toolProperty.Value.PSObject.Properties[$Expected]
    if ($null -eq $versionProperty -or $null -eq $versionProperty.Value) {
        throw "Tool version identity mapping has no exact identity for '$Tool' pinned version '$Expected'."
    }
    $reported = @($versionProperty.Value.reported_versions)
    if ($reported.Count -eq 0 -or @($reported | Where-Object { [string]::IsNullOrWhiteSpace([string]$_) }).Count -gt 0) {
        throw "Tool version identity mapping for '$Tool' pinned version '$Expected' does not list non-empty reported_versions."
    }
    return $reported
}

function ConvertTo-NwToolsetVersion {
    <# Normalize a Visual Studio toolset/compiler version to MAJOR.MINOR.PATCH. #>
    param([string]$Value)
    $trimmed = ([string]$Value).Trim().TrimEnd('\')
    $parts = @($trimmed -split '\.')
    if ($parts.Count -ge 3) { return ($parts[0..2] -join '.') }
    return $trimmed
}

function Expand-NwTemplate {
    param([Parameter(Mandatory)][string]$Template, [Parameter(Mandatory)][string]$Version)
    return $Template.Replace("{version}", $Version)
}

function Get-NwVisualStudioSelection {
    <#
        Resolve the Visual Studio build-tools selector from the authoritative
        toolchain manifest. Component templates in toolchain-sources.json may
        reference {msvc_toolset}, {msvc_toolset_major_minor},
        {windows_sdk_version} and {windows_sdk_build}. Literal component IDs are
        also accepted but are cross-checked against the manifest so a stale
        duplicate pin fails deterministically instead of drifting.
    #>
    [CmdletBinding()]
    param([string]$RepoRoot = "")

    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    $sources = Get-NwProvisioningSources -RepoRoot $RepoRoot
    $vs = $sources.visual_studio
    if ($null -eq $vs) { throw "Provisioning sources do not define visual_studio." }
    if ([string]::IsNullOrWhiteSpace([string]$vs.bootstrapper_url)) {
        throw "Provisioning sources do not define visual_studio.bootstrapper_url."
    }

    $toolsetParts = @($contract.MsvcToolsetVersion -split '\.')
    $toolsetMajorMinor = ($toolsetParts[0..1] -join '.')
    $templates = @($vs.components)
    if ($templates.Count -eq 0) {
        throw "Provisioning sources do not define any Visual Studio components."
    }
    $components = @(
        foreach ($template in $templates) {
            $component = [string]$template
            $component = $component.Replace('{msvc_toolset}', $contract.MsvcToolsetVersion)
            $component = $component.Replace('{msvc_toolset_major_minor}', $toolsetMajorMinor)
            $component = $component.Replace('{windows_sdk_version}', $contract.WindowsSdkVersion)
            $component = $component.Replace('{windows_sdk_build}', $contract.WindowsSdkBuild)
            $component
        }
    )

    foreach ($component in $components) {
        if ($component -match '^Microsoft\.VisualStudio\.Component\.VC\.([0-9]+\.[0-9]+)\.') {
            $componentMajorMinor = $matches[1]
            if ($componentMajorMinor -ne $toolsetMajorMinor) {
                throw "Visual Studio component '$component' does not match pinned MSVC toolset $toolsetMajorMinor (selected $componentMajorMinor)."
            }
        }
        if ($component -match 'Windows11SDK\.([0-9]+)') {
            $componentSdkBuild = $matches[1]
            if ($componentSdkBuild -ne $contract.WindowsSdkBuild) {
                throw "Visual Studio component '$component' does not match pinned Windows SDK $($contract.WindowsSdkVersion) (selected build $componentSdkBuild)."
            }
        }
    }
    if (-not ($components | Where-Object { $_ -match 'Windows11SDK\.' })) {
        throw "Visual Studio component list does not include the pinned Windows SDK build $($contract.WindowsSdkBuild)."
    }

    $sha256 = ""
    if ($vs.PSObject.Properties.Name -contains "bootstrapper_sha256") {
        $sha256 = [string]$vs.bootstrapper_sha256
    }
    $subject = "Microsoft Corporation"
    if ($vs.PSObject.Properties.Name -contains "bootstrapper_authenticode_subject_contains") {
        $subject = [string]$vs.bootstrapper_authenticode_subject_contains
    }

    return [pscustomobject]@{
        WindowsSdkVersion = $contract.WindowsSdkVersion
        WindowsSdkBuild = $contract.WindowsSdkBuild
        # vcvars_ver selects the installed MSVC package family, not the file
        # version reported by cl.exe/link.exe. For the verified NW-01 toolchain
        # the package directory may be 14.40.33807 while the binaries report
        # 19.40.33821 / 14.40.33821.0. Select the 14.40 family here and enforce
        # the exact binary identity after activation.
        VcVarsVersion = $contract.MsvcToolsetMajorMinor
        VcVarsFallbackVersion = ""
        Components = $components
        BootstrapperUrl = [string]$vs.bootstrapper_url
        BootstrapperSha256 = $sha256
        BootstrapperSubjectContains = $subject
    }
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
        [Parameter(Mandatory)][string]$Expected,
        [string]$RepoRoot = ""
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
        # DXC release archives report a binary identity that carries an
        # additional build component (the pinned 1.8.2407 release reports
        # identities such as "1.8.2407.7"/"1.8.2407.12"). The repository-owned
        # mapping is authoritative and the match remains exact: a different
        # build suffix or a longer numeric token is still rejected.
        "dxc" {
            foreach ($identity in @(Get-NwReportedToolVersions -Tool "dxc" -Expected $Expected -RepoRoot $RepoRoot)) {
                if ($output -match ("(?<![0-9.])" + [regex]::Escape([string]$identity) + "(?![0-9.])")) {
                    return $true
                }
            }
            return $false
        }
        # MSVC localizes the surrounding banner text (for example Polish cl.exe
        # prints "wersja kompilatora"), so compiler/linker validation must bind
        # to the exact numeric version token rather than English words.
        "cl" { return $output -match ("(?<![0-9.])" + [regex]::Escape($Expected) + "(?![0-9.])") }
        "link" { return $output -match ("(?<![0-9.])" + [regex]::Escape($Expected) + "(?![0-9.])") }
    }
}

function Assert-NwExactToolVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Tool,
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$Expected,
        [string]$Label = "",
        [string]$RepoRoot = ""
    )
    if (Test-NwExactVersion -Tool $Tool -Executable $Executable -Expected $Expected -RepoRoot $RepoRoot) {
        return
    }
    $detected = Get-NwCommandOutput -Executable $Executable -Arguments $(if ($Tool -in @("cl", "link")) { @() } else { @("--version") })
    if ([string]::IsNullOrWhiteSpace($detected)) { $detected = "<no version banner>" }
    $name = if ([string]::IsNullOrWhiteSpace($Label)) { $Tool } else { $Label }
    throw "$name exact version $Expected was not detected at ${Executable} (detected: $detected)"
}

function Find-NwExactCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Tool,
        [Parameter(Mandatory)][string]$Expected,
        [string[]]$AdditionalCandidates = @()
    )

    $resolved = Resolve-NwToolCandidate -Name $Name -Tool $Tool -Expected $Expected -AdditionalCandidates $AdditionalCandidates
    if ($null -ne $resolved -and $resolved.Exact) { return [string]$resolved.Executable }
    return $null
}

function Resolve-NwToolCandidate {
    <#
        Return the best candidate for a tool as an object with Executable,
        Detected and Exact fields. An exact pinned match always wins; otherwise
        the first executable that produced a version banner is returned with
        Exact=$false so doctor can report expected-versus-detected.
    #>
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

    $firstAvailable = $null
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path $candidate -PathType Leaf)) { continue }
        $args = if ($Tool -in @("cl", "link")) { @() } else { @("--version") }
        $output = Get-NwCommandOutput -Executable $candidate -Arguments $args
        if ([string]::IsNullOrWhiteSpace($output)) { continue }
        $detected = (($output -split "`r?`n")[0]).Trim()
        if (Test-NwExactVersion -Tool $Tool -Executable $candidate -Expected $Expected) {
            return [pscustomobject]@{ Executable = $candidate; Detected = $detected; Exact = $true }
        }
        if ($null -eq $firstAvailable) {
            $firstAvailable = [pscustomobject]@{ Executable = $candidate; Detected = $detected; Exact = $false }
        }
    }
    return $firstAvailable
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

function Invoke-NwDownloadWithFallback {
    <#
        Download with bounded retries. Only transient delivery failures may use
        the caller-provided fallback probe, and the probe is responsible for
        validating an exact pinned version. Non-transient delivery failures,
        checksum mismatches and malformed/version-invalid archives never reach
        this fallback path because they are raised by the caller after a
        successful download.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile,
        [Parameter(Mandatory)]$RetryPolicy,
        [scriptblock]$FallbackProbe,
        [scriptblock]$Downloader = { param([string]$TargetUri, [string]$TargetOutFile) Invoke-WebRequest -Uri $TargetUri -OutFile $TargetOutFile -UseBasicParsing }
    )

    $result = Invoke-NwDownload -Uri $Uri -OutFile $OutFile -RetryPolicy $RetryPolicy -Downloader $Downloader
    if ($result.Success) {
        return [pscustomobject]@{
            Downloaded = $true
            FallbackUsed = $false
            FallbackValue = $null
            Attempts = $result.Attempts
            Error = $null
        }
    }
    if (-not $result.TransientFailure) {
        throw "Non-transient download failure for ${Name}: $($result.Error)"
    }

    Write-Warning "[NW-BOOTSTRAP] transient download failure after $($result.Attempts) attempts for $Name; re-checking exact runner-provided installation."
    $fallback = $null
    if ($null -ne $FallbackProbe) { $fallback = & $FallbackProbe }
    if ($null -ne $fallback -and $fallback -ne $false) {
        return [pscustomobject]@{
            Downloaded = $false
            FallbackUsed = $true
            FallbackValue = $fallback
            Attempts = $result.Attempts
            Error = $result.Error
        }
    }
    throw "Transient download failure for $Name and no exact pinned runner-provided fallback is available: $($result.Error)"
}

function Invoke-NwDownload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile,
        [Parameter(Mandatory)]$RetryPolicy,
        [scriptblock]$Downloader = { param([string]$TargetUri, [string]$TargetOutFile) Invoke-WebRequest -Uri $TargetUri -OutFile $TargetOutFile -UseBasicParsing }
    )

    $attempts = [int]$RetryPolicy.max_attempts
    $delay = [double]$RetryPolicy.initial_delay_seconds
    $multiplier = [double]$RetryPolicy.backoff_multiplier
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            Write-Host "[NW-BOOTSTRAP] download attempt $attempt/${attempts}: $Uri"
            & $Downloader $Uri $OutFile
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

function Assert-NwAuthenticodeSignature {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$SubjectContains = ""
    )
    if (-not (Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue)) {
        throw "Get-AuthenticodeSignature is unavailable; refusing to trust $Path without integrity validation."
    }
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($null -eq $signature -or $null -eq $signature.SignerCertificate) {
        throw "Authenticode signature validation failed for $Path (not signed)."
    }
    $status = [string]$signature.Status
    $subject = [string]$signature.SignerCertificate.Subject
    if (-not [string]::IsNullOrWhiteSpace($SubjectContains) -and $subject -notlike "*$SubjectContains*") {
        throw "Authenticode signer for $Path ('$subject') does not contain '$SubjectContains'."
    }
    if ($status -ne "Valid") {
        # A certificate subject containing "Microsoft" is not publisher
        # authenticity. Any non-Valid result (including an unknown/indeterminate
        # chain or revocation result) is a hard failure unless the caller
        # supplied an independently verified pinned digest.
        throw "Authenticode signature validation failed for $Path (status=$status, signer='$subject')."
    }
}

function Get-NwVisualStudioInstallPaths {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere -PathType Leaf)) { return @() }

    $query = @(
        "-version", "[17.0,18.0)",
        "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "-property", "installationPath"
    )
    # Prefer a Build Tools installation because bootstrap provisions through
    # that product, but reuse an already-correct Community/Professional/
    # Enterprise installation rather than downloading or replacing it.
    $results = New-Object System.Collections.Generic.List[string]
    foreach ($products in @("Microsoft.VisualStudio.Product.BuildTools", "*")) {
        $paths = & $vswhere -products $products @query
        if ($LASTEXITCODE -ne 0) { continue }
        foreach ($path in @($paths)) {
            $trimmed = ([string]$path).Trim()
            if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
            $duplicate = $false
            foreach ($existing in $results) {
                if ([string]::Equals($existing, $trimmed, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $duplicate = $true
                    break
                }
            }
            if (-not $duplicate) { $results.Add($trimmed) }
        }
    }
    return $results.ToArray()
}

function Get-NwVisualStudioInstallPath {
    $paths = @(Get-NwVisualStudioInstallPaths)
    if ($paths.Count -eq 0) { return $null }
    return [string]$paths[0]
}

function Assert-NwMsvcSelectedExecutables {
    <#
        Validate the cl.exe/link.exe that PATH actually selects after vcvars.
        Searching every PATH candidate for *some* exact match is not sufficient:
        a wrong 14.40 patch can be active while another installation happens to
        expose the exact binary. Both executables must live under the selected
        VCToolsInstallDir and report the exact pinned versions.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$MsvcCompilerVersion,
        [Parameter(Mandatory)][string]$MsvcLinkerVersion
    )
    $toolsDirRaw = [string]$env:VCToolsInstallDir
    if ([string]::IsNullOrWhiteSpace($toolsDirRaw)) {
        throw "VCToolsInstallDir is not set; cannot verify the selected MSVC toolchain."
    }
    $toolsDir = [System.IO.Path]::GetFullPath($toolsDirRaw.TrimEnd('\'))
    $toolsPrefix = $toolsDir + [System.IO.Path]::DirectorySeparatorChar
    $failures = New-Object System.Collections.Generic.List[string]
    foreach ($spec in @(
        @{ Name = "cl.exe"; Tool = "cl"; Expected = $MsvcCompilerVersion },
        @{ Name = "link.exe"; Tool = "link"; Expected = $MsvcLinkerVersion }
    )) {
        $cmd = Get-Command ([string]$spec.Name) -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $cmd -or [string]::IsNullOrWhiteSpace([string]$cmd.Source)) {
            $failures.Add("$($spec.Name) is not resolvable from the activated PATH")
            continue
        }
        $selectedPath = [System.IO.Path]::GetFullPath([string]$cmd.Source)
        if (-not $selectedPath.StartsWith($toolsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $failures.Add("$($spec.Name) '$selectedPath' is not owned by VCToolsInstallDir '$toolsDir'")
            continue
        }
        if (-not (Test-NwExactVersion -Tool ([string]$spec.Tool) -Executable $selectedPath -Expected ([string]$spec.Expected))) {
            $detected = Get-NwCommandOutput -Executable $selectedPath -Arguments @()
            if ([string]::IsNullOrWhiteSpace($detected)) { $detected = "<no version banner>" }
            $failures.Add("$($spec.Name) '$selectedPath' reports '$detected', expected exact $($spec.Expected)")
        }
    }
    if ($failures.Count -gt 0) {
        throw ("Selected MSVC executables are not the pinned toolchain: " + ($failures -join "; "))
    }
}

function Import-NwVcVarsEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$WindowsSdkVersion,
        [Parameter(Mandatory)][string]$VcVarsVersion,
        [string]$VcVarsFallbackVersion = "",
        [string]$MsvcCompilerVersion = "",
        [string]$MsvcLinkerVersion = "",
        [string]$RepoRoot = "",
        [switch]$ExportGitHubEnv
    )

    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    if ([string]::IsNullOrWhiteSpace($MsvcCompilerVersion)) { $MsvcCompilerVersion = $contract.MsvcCompilerVersion }
    if ([string]::IsNullOrWhiteSpace($MsvcLinkerVersion)) { $MsvcLinkerVersion = $contract.MsvcLinkerVersion }

    $installs = @(Get-NwVisualStudioInstallPaths)
    if ($installs.Count -eq 0) { throw "Visual Studio 2022 installation with VC tools was not found." }

    $required = @("Path", "INCLUDE", "LIB", "LIBPATH", "VCINSTALLDIR", "VCToolsInstallDir", "VCToolsVersion", "WindowsSdkDir", "WindowsSDKVersion", "WindowsSdkVerBinPath", "UCRTVersion", "UniversalCRTSdkDir")
    $requiredPresence = @("Path", "INCLUDE", "LIB", "LIBPATH", "VCToolsInstallDir", "VCToolsVersion", "WindowsSDKVersion")
    $expectedMajorMinor = ((ConvertTo-NwToolsetVersion $VcVarsVersion) -split '\.')[0..1] -join '.'
    $versionsToTry = @($VcVarsVersion)
    $discoveredVersions = New-Object System.Collections.Generic.List[string]
    foreach ($vsInstall in $installs) {
        $toolsRoot = Join-Path $vsInstall "VC\Tools\MSVC"
        if (-not (Test-Path $toolsRoot -PathType Container)) { continue }
        foreach ($toolsetDir in @(Get-ChildItem -Path $toolsRoot -Directory -ErrorAction SilentlyContinue)) {
            if ($toolsetDir.Name -match ('^' + [regex]::Escape($expectedMajorMinor) + '\.[0-9]+$')) {
                $discoveredVersions.Add([string]$toolsetDir.Name)
            }
        }
    }
    foreach ($discovered in @($discoveredVersions | Sort-Object -Descending -Unique)) {
        if (-not ($versionsToTry -contains $discovered)) { $versionsToTry += $discovered }
    }
    if (-not [string]::IsNullOrWhiteSpace($VcVarsFallbackVersion) -and
        -not [string]::Equals($VcVarsFallbackVersion, $VcVarsVersion, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not ($versionsToTry -contains $VcVarsFallbackVersion)) {
        $versionsToTry += $VcVarsFallbackVersion
    }
    $failures = New-Object System.Collections.Generic.List[string]
    $selected = $null
    $selectedVcVars = $null
    $selectedEnvironment = $null
    $selectedVcVarsVersion = $null

    # The authoritative contract pins the active cl.exe/link.exe file versions,
    # not Visual Studio's package-directory patch number. Try the requested
    # major/minor family first, then each installed package directory in that
    # family. Every candidate is accepted only if the activated executables
    # belong to VCToolsInstallDir and report the exact pinned binary versions.
    foreach ($vcvarsVersion in $versionsToTry) {
        foreach ($vsInstall in $installs) {
            $vcvars = Join-Path $vsInstall "VC\Auxiliary\Build\vcvars64.bat"
            if (-not (Test-Path $vcvars -PathType Leaf)) {
                $failures.Add("${vsInstall}: vcvars64.bat missing")
                continue
            }

            $cmdFile = Join-Path ([System.IO.Path]::GetTempPath()) ("nw01-vcvars-{0}.cmd" -f [guid]::NewGuid().ToString("N"))
            $envDump = @()
            $vcvarsExit = 1
            $vcvarsError = $null
            try {
                @"
@echo off
call "$vcvars" $WindowsSdkVersion -vcvars_ver=$vcvarsVersion
if errorlevel 1 exit /b %errorlevel%
set
"@ | Set-Content -Path $cmdFile -Encoding ascii
                    $envDump = & $env:ComSpec /d /c $cmdFile 2>&1
                    $vcvarsExit = $LASTEXITCODE
                } catch {
                    $vcvarsError = $_.Exception.Message
                    $envDump = @()
                    $vcvarsExit = 1
                } finally {
                    Remove-Item -Force -ErrorAction SilentlyContinue $cmdFile
                }

                $captured = @{}
                foreach ($line in $envDump) {
                    if ($line -match "^([^=]+)=(.*)$") {
                        $name = $matches[1]
                        if ($required -contains $name) { $captured[$name] = $matches[2] }
                    }
                }
                # Publish the last observed values even on a mismatch so doctor
                # can report expected-versus-detected instead of only "missing".
                foreach ($name in $captured.Keys) { Set-Item -Path "Env:$name" -Value $captured[$name] }

                if ($vcvarsExit -ne 0) {
                    $detail = if ([string]::IsNullOrWhiteSpace($vcvarsError)) { "exit $vcvarsExit" } else { $vcvarsError }
                    $failures.Add("${vsInstall} ($vcvarsVersion): vcvars64.bat $detail")
                    continue
                }
                $missing = @($requiredPresence | Where-Object { -not $captured.ContainsKey($_) -or [string]::IsNullOrWhiteSpace([string]$captured[$_]) })
                if ($missing.Count -gt 0) {
                    $failures.Add("${vsInstall} ($vcvarsVersion): missing $($missing -join ',')")
                    continue
                }
                $sdkDetected = ([string]$captured["WindowsSDKVersion"]).TrimEnd('\')
                if (-not [string]::Equals($sdkDetected, $WindowsSdkVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $failures.Add("${vsInstall} ($vcvarsVersion): SDK '$sdkDetected' != '$WindowsSdkVersion'")
                    continue
                }
                $toolsetDetected = ConvertTo-NwToolsetVersion ([string]$captured["VCToolsVersion"])
                $toolsetDetectedParts = @($toolsetDetected -split '\.')
                $toolsetDetectedMajorMinor = if ($toolsetDetectedParts.Count -ge 2) {
                    ($toolsetDetectedParts[0..1] -join '.')
                } else {
                    $toolsetDetected
                }
                if (-not [string]::Equals($toolsetDetectedMajorMinor, $expectedMajorMinor, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $failures.Add("${vsInstall} ($vcvarsVersion): toolset '$toolsetDetected' is outside required MSVC family $expectedMajorMinor")
                    continue
                }
                try {
                    Assert-NwMsvcSelectedExecutables -MsvcCompilerVersion $MsvcCompilerVersion -MsvcLinkerVersion $MsvcLinkerVersion
                } catch {
                    $failures.Add("${vsInstall} ($vcvarsVersion): $($_.Exception.Message)")
                    continue
                }
                $selected = $vsInstall
                $selectedVcVars = $vcvars
                $selectedEnvironment = $captured
                $selectedVcVarsVersion = $vcvarsVersion
                break
            }
            if ($null -ne $selected) { break }
        }

    if ($null -eq $selected) {
        throw ("Unable to select exact pinned Visual Studio environment (SDK $WindowsSdkVersion, MSVC family $expectedMajorMinor, cl $MsvcCompilerVersion, link $MsvcLinkerVersion): " + ($failures -join "; "))
    }

    if ($ExportGitHubEnv) {
        if ([string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) { throw "GITHUB_ENV is not available." }
        foreach ($name in $required) {
            if ($selectedEnvironment.ContainsKey($name)) { Add-Content -Path $env:GITHUB_ENV -Value "$name=$($selectedEnvironment[$name])" }
        }
    }
    return [pscustomobject]@{
        VisualStudioInstall = $selected
        VcVars = $selectedVcVars
        VcVarsVersion = $selectedVcVarsVersion
        Environment = $selectedEnvironment
    }
}

function Get-NwStandaloneToolPaths {
    [CmdletBinding()]
    param([string]$RepoRoot = "")
    $contract = Get-NwToolchainContract -RepoRoot $RepoRoot
    $toolsRoot = if ($env:NW_TOOLS_ROOT) { $env:NW_TOOLS_ROOT } else { "C:\Tools" }
    $cmakeRoot = Join-Path $toolsRoot ("CMake-{0}" -f $contract.CMakeVersion)
    $cmakeNested = Join-Path $cmakeRoot ("cmake-{0}-windows-x86_64" -f $contract.CMakeVersion) "bin"
    [pscustomobject]@{
        CMakeBin = $cmakeNested
        NinjaBin = Join-Path $toolsRoot ("Ninja-{0}" -f $contract.NinjaVersion)
        LlvmBin = Join-Path $toolsRoot ("LLVM-{0}" -f $contract.LlvmVersion) "bin"
        DxcBin = Join-Path $toolsRoot ("dxc-{0}" -f $contract.DxcVersion) "bin" "x64"
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
    $visualStudio = Get-NwVisualStudioSelection -RepoRoot $RepoRoot
    # Import vcvars first because it prepends SDK locations. Then resolve each
    # standalone tool by exact version (canonical C:\Tools copy or exact
    # runner-provided fallback) and deliberately move that selected directory
    # ahead of the SDK. This prevents Windows SDK's newer dxc.exe from
    # shadowing the pinned DXC 1.8.2407 used by NW-01.
    Import-NwVcVarsEnvironment -WindowsSdkVersion $visualStudio.WindowsSdkVersion -VcVarsVersion $visualStudio.VcVarsVersion -VcVarsFallbackVersion $visualStudio.VcVarsFallbackVersion -MsvcCompilerVersion $contract.MsvcCompilerVersion -MsvcLinkerVersion $contract.MsvcLinkerVersion -RepoRoot $RepoRoot | Out-Null
    $selection = Get-NwExactStandaloneToolSelection -RepoRoot $RepoRoot

    foreach ($entry in @($selection.CMakeBin, $selection.NinjaBin, $selection.LlvmBin, $selection.DxcBin)) {
        Add-NwPathEntry -Path $entry
    }

    # Fail immediately if command resolution still disagrees with the exact
    # pinned DXC path after vcvars/path activation.
    $resolvedDxc = (Get-Command dxc.exe -ErrorAction Stop).Source
    if ([System.IO.Path]::GetFullPath($resolvedDxc) -ne [System.IO.Path]::GetFullPath($selection.DxcExecutable)) {
        throw "Pinned DXC precedence failure: command resolution selected '$resolvedDxc', expected '$($selection.DxcExecutable)'"
    }

    # Re-assert the active compiler/linker after standalone PATH entries are
    # prepended. The build environment is only entered when the selected
    # executables are owned by the pinned VCToolsInstallDir and report exact
    # manifest versions.
    Assert-NwMsvcSelectedExecutables -MsvcCompilerVersion $contract.MsvcCompilerVersion -MsvcLinkerVersion $contract.MsvcLinkerVersion

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

    return [pscustomobject]@{ Contract = $contract; VisualStudio = $visualStudio; Paths = $selection }
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
        # Doctor must inspect the executable the activated environment actually
        # selects. Scanning every PATH candidate could pass because an unrelated
        # installation exposes an exact version while a wrong one is active.
        $command = Get-Command ([string]$spec.Command) -ErrorAction SilentlyContinue | Select-Object -First 1
        $exe = if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace([string]$command.Source)) {
            [string]$command.Source
        } elseif (-not [string]::IsNullOrWhiteSpace([string]$spec.Candidate)) {
            [string]$spec.Candidate
        } else {
            $null
        }
        $ok = $false
        $detected = "not found"
        if (-not [string]::IsNullOrWhiteSpace($exe) -and (Test-Path $exe -PathType Leaf)) {
            $args = if ([string]$spec.Tool -in @("cl", "link")) { @() } else { @("--version") }
            $output = Get-NwCommandOutput -Executable $exe -Arguments $args
            if (-not [string]::IsNullOrWhiteSpace($output)) {
                $detected = (($output -split "`r?`n")[0]).Trim()
            } else {
                $detected = "no version banner"
            }
            $ok = Test-NwExactVersion -Tool ([string]$spec.Tool) -Executable $exe -Expected ([string]$spec.Expected)
        }
        [pscustomobject]@{ Name = $spec.Name; Expected = [string]$spec.Expected; Detected = $detected; Pass = $ok; Executable = $exe }
    }
    $sdkOk = $false
    $sdkDetected = if ($env:WindowsSDKVersion) { $env:WindowsSDKVersion.TrimEnd('\') } else { "missing" }
    if ([string]::Equals($sdkDetected, $contract.WindowsSdkVersion, [System.StringComparison]::OrdinalIgnoreCase)) { $sdkOk = $true }
    $results += [pscustomobject]@{ Name = "Windows SDK"; Expected = $contract.WindowsSdkVersion; Detected = $sdkDetected; Pass = $sdkOk; Executable = $env:WindowsSdkDir }
    $toolsetDetected = if ($env:VCToolsVersion) { ConvertTo-NwToolsetVersion $env:VCToolsVersion } else { "missing" }
    $toolsetExpected = $contract.MsvcToolsetMajorMinor
    $toolsetDetectedParts = @($toolsetDetected -split '\.')
    $toolsetDetectedMajorMinor = if ($toolsetDetectedParts.Count -ge 2) {
        ($toolsetDetectedParts[0..1] -join '.')
    } else {
        $toolsetDetected
    }
    $toolsetOk = [string]::Equals($toolsetDetectedMajorMinor, $toolsetExpected, [System.StringComparison]::OrdinalIgnoreCase)
    $results += [pscustomobject]@{ Name = "MSVC toolset family"; Expected = $toolsetExpected; Detected = $toolsetDetected; Pass = $toolsetOk; Executable = $env:VCToolsInstallDir }
    return $results
}

Export-ModuleMember -Function *-Nw*
