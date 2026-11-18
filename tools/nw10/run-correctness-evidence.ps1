#!/usr/bin/env pwsh
#requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateSet("ada", "blackwell")]
    [string]$Tier,
    [switch]$Compare,
    [switch]$Bundle,
    [switch]$PrepareTransferBundle,
    [ValidateSet("ada", "blackwell")]
    [string]$TargetTier,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$ExpectedBranch = "agent/NW-10",
    [string]$ReferenceEvidence = "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json",
    [string]$WorkRoot = "build/nw10-correctness-scripted",
    [string[]]$ForbidToken = @(),
    [string]$AdaRecord = "",
    [string]$AdaCaptures = "",
    [string]$BlackwellRecord = "",
    [string]$BlackwellCaptures = "",
    [string]$CompareOutput = "build/nw10-correctness-comparison.json",
    [switch]$Publish,
    [switch]$Commit,
    [switch]$Push,
    [string]$PublishedOutput = "benchmarks/nw10/common-artifact-correctness.v1.json",
    [string]$StatusFile = "benchmarks/nw10/evidence-status.v1.json",
    [string]$CommitMessage = "NW-10: publish Ada/Blackwell correctness evidence"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repo = [System.IO.Path]::GetFullPath($RepoRoot)
$originalLocation = Get-Location

function Invoke-NwNative {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureMessage
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit $LASTEXITCODE)"
    }
}

function Resolve-NwRepoPath([string]$Value) {
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repo $Value))
}

function Get-NwJsonStrings($Value) {
    if ($null -eq $Value) { return }
    if ($Value -is [string]) {
        Write-Output ([string]$Value)
        return
    }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            Get-NwJsonStrings $key
            Get-NwJsonStrings $Value[$key]
        }
        return
    }
    if ($Value -is [pscustomobject]) {
        foreach ($property in $Value.PSObject.Properties) {
            Get-NwJsonStrings $property.Name
            Get-NwJsonStrings $property.Value
        }
        return
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        foreach ($item in $Value) {
            Get-NwJsonStrings $item
        }
    }
}

function Assert-NwPrivacy {
    param(
        [Parameter(Mandatory)]$JsonObject,
        [Parameter(Mandatory)][System.Collections.Generic.List[string]]$Tokens
    )
    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($value in @(Get-NwJsonStrings $JsonObject)) {
        $text = [string]$value
        if (
            $text -match '(?i)^[a-z]:\\users\\' -or
            $text -match '(?i)^/home/'
        ) {
            $hits.Add($text)
            continue
        }
        foreach ($token in $Tokens) {
            if (
                -not [string]::IsNullOrWhiteSpace($token) -and
                $text.IndexOf(
                    $token,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            ) {
                $hits.Add($text)
                break
            }
        }
    }
    if ($hits.Count -gt 0) {
        throw "Privacy audit found forbidden strings: $($hits -join '; ')"
    }
}

function Assert-NwCleanTrackedTree {
    $lines = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "git status failed." }
    $unexpected = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
        $path = ([string]$line).Substring(3).Trim('"').Replace('\', '/')
        if ($path.StartsWith("build/", [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $unexpected += [string]$line
    }
    if ($unexpected.Count -gt 0) {
        throw "Working tree has non-build changes: $($unexpected -join '; ')"
    }
}

function Assert-NwSameValue {
    param(
        [Parameter(Mandatory)][string]$Name,
        $Expected,
        $Observed
    )
    if ($Expected -ne $Observed) {
        throw "$Name mismatch: expected=$Expected observed=$Observed"
    }
}

function Get-NwCurrentBranch {
    $output = @(& git branch --show-current)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to determine current git branch."
    }
    if ($output.Count -eq 0 -or $null -eq $output[0]) {
        return ""
    }
    return ([string]$output[0]).Trim()
}

function Assert-NwExpectedBranch {
    param([AllowEmptyString()][string]$Expected)
    $branch = Get-NwCurrentBranch
    if (
        -not [string]::IsNullOrWhiteSpace($Expected) -and
        $branch -ne $Expected
    ) {
        throw "Expected branch '$Expected', current branch is '$branch'."
    }
    return $branch
}

function New-NwBundle {
    param(
        [Parameter(Mandatory)][string]$TierName,
        [Parameter(Mandatory)][string]$TierRoot,
        [Parameter(Mandatory)][string]$RecordPath,
        [Parameter(Mandatory)][string]$CaptureDirectory,
        [Parameter(Mandatory)][string]$Destination
    )
    $bundleRoot = Join-Path $TierRoot "bundle"
    Remove-Item $bundleRoot -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $bundleRoot | Out-Null
    Copy-Item $RecordPath (Join-Path $bundleRoot "capture.json") -Force
    Copy-Item $CaptureDirectory (Join-Path $bundleRoot "captures") -Recurse -Force

    $manifestRows = @()
    foreach ($file in Get-ChildItem $bundleRoot -File -Recurse | Sort-Object FullName) {
        $relative = [System.IO.Path]::GetRelativePath($bundleRoot, $file.FullName).Replace('\', '/')
        $manifestRows += [ordered]@{
            path = $relative
            bytes = [int64]$file.Length
            sha256 = (Get-FileHash -Algorithm SHA256 $file.FullName).Hash.ToLowerInvariant()
        }
    }
    $manifest = [ordered]@{
        format = "nw10-correctness-transfer-bundle"
        version = 1
        task = "NW-10"
        tier = $TierName
        files = $manifestRows
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $bundleRoot "bundle-manifest.json") -Encoding UTF8

    Remove-Item $Destination -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $Destination -CompressionLevel Optimal
    Write-Host "[NW10 CORRECTNESS] BUNDLE=$Destination"
}

try {
    Set-Location $repo

    if ($PrepareTransferBundle) {
        if ($PSBoundParameters.ContainsKey("Tier") -or $Compare) {
            throw "-PrepareTransferBundle cannot be combined with -Tier or -Compare."
        }
        if (-not $PSBoundParameters.ContainsKey("TargetTier")) {
            throw "-PrepareTransferBundle requires -TargetTier ada or blackwell."
        }
        if (-not $IsWindows) {
            throw "Correctness transfer bundle preparation is Windows-only."
        }

        [void](Assert-NwExpectedBranch -Expected $ExpectedBranch)
        Assert-NwCleanTrackedTree

        $referencePath = Resolve-NwRepoPath $ReferenceEvidence
        $reference = Get-Content -Raw $referencePath | ConvertFrom-Json
        if ($reference.status -ne "verified_preserved") {
            throw "Tracked performance evidence is not verified_preserved."
        }
        $expectedArtifact = $reference.after.artifact

        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
        }
        if ($null -eq $pythonCommand) { throw "Python 3 is required on build host." }
        $python = $pythonCommand.Source

        Write-Host "[NW10 CORRECTNESS] preparing diagnostic transfer bundle on qualified build host"
        Import-Module (Join-Path $repo "tools/nw01/Nw01.Windows.psm1") -Force
        $entered = Enter-NwBuildEnvironment -RepoRoot $repo
        Write-Host "[NW10 CORRECTNESS] MSVC toolset=$($entered.VisualStudio.VcVarsVersion) SDK=$($entered.VisualStudio.WindowsSdkVersion)"

        $root = Resolve-NwRepoPath $WorkRoot
        $transferRoot = Join-Path $root ("transfer-" + $TargetTier)
        $commonTemplate = Join-Path $transferRoot "common-template"
        $commonArtifact = Join-Path $transferRoot "common"
        $commonReport = Join-Path $transferRoot "common-report.json"
        $diagTemplate = Join-Path $transferRoot "diagnostic-template"
        $diagArtifact = Join-Path $transferRoot "diagnostic"
        $diagBuildRoot = Join-Path $transferRoot "diagnostic-build"
        $diagReport = Join-Path $transferRoot "diagnostic-report.json"
        $packageRoot = Join-Path $transferRoot "transfer-package"
        Remove-Item $transferRoot -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force $packageRoot | Out-Null

        Invoke-NwNative "cmake" @(
            "--preset", "nw07-release",
            "-B", $commonTemplate,
            "-DNW10_COMPACT_PACKAGE=ON",
            "-DNW_ENFORCE_PINNED_TOOLCHAIN=ON"
        ) "Common release template configure failed"
        Invoke-NwNative "cmake" @(
            "--build", $commonTemplate,
            "--target", "nw_procedural_image_player",
            "--parallel", "2"
        ) "Common release template build failed"
        $dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
        $commonTemplateExe = Join-Path $commonTemplate "staging/procedural_image_player/nw_procedural_image_player.exe"
        Invoke-NwNative $python @(
            "tools/nw10_perf_candidate.py",
            "--output", $commonArtifact,
            "--report", $commonReport,
            "--player-template", $commonTemplateExe,
            "--dxc", $dxc
        ) "Common NW-10 artifact reproduction failed"

        $common = Get-Content -Raw $commonReport | ConvertFrom-Json
        Assert-NwSameValue "artifact_key" $expectedArtifact.artifact_key $common.artifact.artifact_key
        Assert-NwSameValue "package_sha256" $expectedArtifact.package_sha256 $common.artifact.package_sha256
        Assert-NwSameValue "manifest_sha256" $expectedArtifact.manifest_sha256 $common.artifact.manifest_sha256
        Assert-NwSameValue "executable_sha256" $expectedArtifact.executable_sha256 $common.artifact.executable_sha256
        Assert-NwSameValue "complete_distribution_bytes" $expectedArtifact.complete_distribution_bytes $common.artifact.complete_distribution_bytes
        Write-Host "[NW10 CORRECTNESS] COMMON_ARTIFACT=EXACT_MATCH"

        Invoke-NwNative "cmake" @(
            "--preset", "nw07-diagnostics",
            "-B", $diagTemplate
        ) "Transfer diagnostic template configure failed"
        Invoke-NwNative "cmake" @(
            "--build", $diagTemplate,
            "--target", "nw_procedural_image_player",
            "--parallel", "2"
        ) "Diagnostic template build failed"
        $diagTemplateExe = Join-Path $diagTemplate "staging/procedural_image_player/nw_procedural_image_player.exe"
        Invoke-NwNative $python @(
            "tools/nw10_common_correctness.py", "build-diagnostic",
            "--output", $diagArtifact,
            "--expected-package", $commonArtifact,
            "--player-template", $diagTemplateExe,
            "--dxc", $dxc,
            "--build-root", $diagBuildRoot,
            "--report", $diagReport
        ) "Diagnostic sibling export failed"

        $diag = Get-Content -Raw $diagReport | ConvertFrom-Json
        Assert-NwSameValue "diagnostic package_sha256" $expectedArtifact.package_sha256 $diag.common_artifact.package_sha256
        Assert-NwSameValue "diagnostic artifact_key" $expectedArtifact.artifact_key $diag.common_artifact.artifact_key
        if ($diag.capture_controls_required -ne $true) {
            throw "Diagnostic sibling does not have capture controls."
        }
        $diagnosticDependencies = @(
            foreach ($dependency in @($diag.diagnostic_dependencies)) {
                if ($null -eq $dependency) { continue }
                $dependencyName = ([string]$dependency).Trim().ToLowerInvariant()
                if ([string]::IsNullOrWhiteSpace($dependencyName)) { continue }
                $dependencyName
            }
        )
        $debugRuntimeDependencies = @()
        $knownDebugRuntimeDependencies = @(
            "msvcp140d.dll",
            "vcruntime140d.dll",
            "vcruntime140_1d.dll",
            "ucrtbased.dll"
        )
        foreach ($dependencyName in $diagnosticDependencies) {
            if ($knownDebugRuntimeDependencies -contains $dependencyName) {
                $debugRuntimeDependencies += $dependencyName
            }
        }
        if ($debugRuntimeDependencies.Count -gt 0) {
            Write-Host "[NW10 CORRECTNESS] TARGET_DEBUG_CRT_DEPENDENCIES=$($debugRuntimeDependencies -join ',')"
        }

        $stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
        $diagExeSource = Join-Path $diagArtifact "$stem.exe"
        $diagExeName = "nw10-correctness-diagnostic.exe"
        $diagExeTarget = Join-Path $packageRoot $diagExeName
        Copy-Item $diagExeSource $diagExeTarget -Force
        $diagExeSha = (Get-FileHash -Algorithm SHA256 $diagExeTarget).Hash.ToLowerInvariant()
        Assert-NwSameValue "diagnostic executable sha256" $diag.diagnostic_executable_sha256 $diagExeSha

        $transfer = [ordered]@{
            format = "nw10-correctness-diagnostic-transfer"
            version = 1
            task = "NW-10"
            target_tier = $TargetTier
            capture_protocol = "nw07-correctness-capture.v1"
            common_artifact = [ordered]@{
                artifact_key = [string]$diag.common_artifact.artifact_key
                package_sha256 = [string]$diag.common_artifact.package_sha256
                manifest_sha256 = [string]$diag.common_artifact.manifest_sha256
                profile_id = [string]$diag.common_artifact.profile_id
                requirements_digest = [string]$diag.common_artifact.requirements_digest
                final_executable_sha256 = [string]$diag.final_executable_sha256
            }
            diagnostic_sibling = [ordered]@{
                file = $diagExeName
                executable_sha256 = [string]$diag.diagnostic_executable_sha256
                executable_bytes = [int64]$diag.diagnostic_executable_bytes
                manifest_sha256 = [string]$diag.diagnostic_manifest_sha256
                dependencies = $diagnosticDependencies
                separate_from_final_executable = $true
                same_common_package = $true
                release_manifest_reused = $false
                capture_controls_required = $true
            }
            capture = [ordered]@{
                fixed_ticks = @(0,240000,480000,720000,959999)
                loop_boundary_ticks = @(0,960000)
                ticks = @(0,240000,480000,720000,959999,960000)
                expected_bytes_per_capture = 33177600
                format = "rgba32f"
            }
            privacy = [ordered]@{
                local_paths_included = $false
                raw_capture_bytes_embedded = $false
            }
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            (Join-Path $packageRoot "transfer.json"),
            (($transfer | ConvertTo-Json -Depth 12) + [Environment]::NewLine),
            $utf8NoBom
        )
        Copy-Item (Join-Path $repo "tools/nw10/run-transferred-correctness-capture.ps1") (Join-Path $packageRoot "run-capture.ps1") -Force

        $fileRows = @()
        foreach ($file in Get-ChildItem $packageRoot -File | Sort-Object Name) {
            $fileRows += [ordered]@{
                path = $file.Name
                bytes = [int64]$file.Length
                sha256 = (Get-FileHash -Algorithm SHA256 $file.FullName).Hash.ToLowerInvariant()
            }
        }
        $bundleManifest = [ordered]@{
            format = "nw10-correctness-diagnostic-input-bundle"
            version = 1
            task = "NW-10"
            target_tier = $TargetTier
            files = $fileRows
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $packageRoot "bundle-manifest.json"),
            (($bundleManifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
            $utf8NoBom
        )

        $zip = Join-Path $root ("nw10-correctness-transfer-" + $TargetTier + ".zip")
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $zip -CompressionLevel Optimal
        Write-Host "[NW10 CORRECTNESS] TRANSFER_BUNDLE=$zip"
        Write-Host "[NW10 CORRECTNESS] TARGET_TOOLCHAIN_REQUIRED=False"
        Write-Host "[NW10 CORRECTNESS] TARGET_DEBUG_CRT_REQUIRED=$($debugRuntimeDependencies.Count -gt 0)"
        return
    }

    if ($Compare) {
        if ($PSBoundParameters.ContainsKey("Tier")) {
            throw "-Compare cannot be combined with -Tier."
        }
        foreach ($pair in @(
            @("AdaRecord", $AdaRecord),
            @("AdaCaptures", $AdaCaptures),
            @("BlackwellRecord", $BlackwellRecord),
            @("BlackwellCaptures", $BlackwellCaptures)
        )) {
            if ([string]::IsNullOrWhiteSpace([string]$pair[1])) {
                throw "-Compare requires -$($pair[0])."
            }
        }

        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
        }
        if ($null -eq $pythonCommand) { throw "Python 3 is required." }
        $python = $pythonCommand.Source
        $outputPath = Resolve-NwRepoPath $CompareOutput

        Invoke-NwNative $python @(
            "tools/nw10_common_correctness.py", "compare",
            "--ada-record", (Resolve-NwRepoPath $AdaRecord),
            "--blackwell-record", (Resolve-NwRepoPath $BlackwellRecord),
            "--ada-captures", (Resolve-NwRepoPath $AdaCaptures),
            "--blackwell-captures", (Resolve-NwRepoPath $BlackwellCaptures),
            "--output", $outputPath
        ) "NW-10 Ada/Blackwell correctness comparison failed"

        $comparison = Get-Content -Raw $outputPath | ConvertFrom-Json
        if ($comparison.status -ne "verified") {
            throw "Cross-hardware correctness comparison is not verified."
        }
        if ($comparison.privacy.raw_capture_bytes_embedded -ne $false) {
            throw "Comparison unexpectedly embeds raw capture bytes."
        }
        if ($comparison.privacy.local_paths_included -ne $false) {
            throw "Comparison unexpectedly exposes local paths."
        }
        $comparePrivacyTokens = New-Object System.Collections.Generic.List[string]
        foreach ($token in @($env:USERNAME, $env:USERPROFILE, $ForbidToken)) {
            $value = [string]$token
            if (
                -not [string]::IsNullOrWhiteSpace($value) -and
                -not $comparePrivacyTokens.Contains($value)
            ) {
                $comparePrivacyTokens.Add($value)
            }
        }
        Assert-NwPrivacy -JsonObject $comparison -Tokens $comparePrivacyTokens
        Write-Host "[NW10 CORRECTNESS] PRIVACY=PASS"
        Write-Host "[NW10 CORRECTNESS] STATUS=$($comparison.status)"
        Write-Host "[NW10 CORRECTNESS] WORST_ABS=$($comparison.comparison.worst_absolute_error)"
        Write-Host "[NW10 CORRECTNESS] WORST_REL=$($comparison.comparison.worst_relative_error)"
        Write-Host "[NW10 CORRECTNESS] OUTPUT=$outputPath"

        if ($Push -and -not $Commit) {
            throw "-Push requires -Commit."
        }
        if ($Commit -and -not $Publish) {
            throw "-Commit requires -Publish."
        }
        if ($Publish) {
            [void](Assert-NwExpectedBranch -Expected $ExpectedBranch)
            $publishArgs = @(
                "tools/nw10_publish_evidence.py", "correctness",
                "--comparison", $outputPath,
                "--destination", $PublishedOutput,
                "--status-file", $StatusFile
            )
            foreach ($token in $comparePrivacyTokens) {
                $publishArgs += @("--forbid-token", $token)
            }
            Invoke-NwNative $python $publishArgs "NW-10 correctness publication failed"
            Invoke-NwNative $python @(
                "tools/schema_validator.py",
                "--repo-root", $repo,
                "--document", (Resolve-NwRepoPath $PublishedOutput),
                "--schema", (Join-Path $repo "schemas/nw10-common-artifact-correctness.schema.json")
            ) "Published correctness schema validation failed"
            Invoke-NwNative $python @(
                "tools/schema_validator.py",
                "--repo-root", $repo,
                "--document", (Resolve-NwRepoPath $StatusFile),
                "--schema", (Join-Path $repo "schemas/nw10-evidence-status.schema.json")
            ) "Evidence-status schema validation failed"
            Invoke-NwNative "git" @("diff", "--check") "git diff --check failed"

            $allowed = @($PublishedOutput.Replace('\','/'), $StatusFile.Replace('\','/'))
            $changed = @(& git status --porcelain --untracked-files=all)
            if ($LASTEXITCODE -ne 0) { throw "git status failed after publication." }
            $unexpected = @()
            foreach ($line in $changed) {
                if ([string]::IsNullOrWhiteSpace([string]$line)) { continue }
                $path = ([string]$line).Substring(3).Trim('"').Replace('\','/')
                if ($path.StartsWith("build/", [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                if ($allowed -notcontains $path) { $unexpected += [string]$line }
            }
            if ($unexpected.Count -gt 0) {
                throw "Publication changed unexpected paths: $($unexpected -join '; ')"
            }

            Write-Host "[NW10 CORRECTNESS] PUBLISH=PASS"
            Write-Host "[NW10 CORRECTNESS] TRACKED=$PublishedOutput"
            if ($Commit) {
                Invoke-NwNative "git" (@("add", "--") + $allowed) "staging correctness evidence failed"
                Invoke-NwNative "git" @("diff", "--cached", "--check") "staged correctness diff check failed"
                $staged = @(& git diff --cached --name-only)
                if ($LASTEXITCODE -ne 0) { throw "Unable to inspect staged correctness evidence." }
                $unexpectedStaged = @($staged | Where-Object { $allowed -notcontains $_ })
                if ($unexpectedStaged.Count -gt 0) {
                    throw "Unexpected staged paths: $($unexpectedStaged -join ', ')"
                }
                if ($staged.Count -eq 0) {
                    Write-Host "[NW10 CORRECTNESS] COMMIT=SKIPPED_NO_CHANGES"
                } else {
                    Invoke-NwNative "git" @("commit", "-m", $CommitMessage) "NW-10 correctness evidence commit failed"
                    $commitSha = (& git rev-parse HEAD).Trim()
                    Write-Host "[NW10 CORRECTNESS] COMMIT=$commitSha"
                    if ($Push) {
                        Invoke-NwNative "git" @("push", "origin", $ExpectedBranch) "NW-10 correctness evidence push failed"
                        Write-Host "[NW10 CORRECTNESS] PUSH=PASS"
                    }
                }
            } else {
                Write-Host "[NW10 CORRECTNESS] COMMIT=NOT_REQUESTED"
            }
        } else {
            Write-Host "[NW10 CORRECTNESS] PUBLISH=NOT_REQUESTED"
        }
        return
    }

    if (-not $PSBoundParameters.ContainsKey("Tier")) {
        throw "Capture mode requires -Tier ada or -Tier blackwell."
    }
    if (-not $IsWindows) {
        throw "Physical NW-10 correctness capture is Windows-only."
    }

    [void](Assert-NwExpectedBranch -Expected $ExpectedBranch)
    Assert-NwCleanTrackedTree

    $referencePath = Resolve-NwRepoPath $ReferenceEvidence
    if (-not (Test-Path $referencePath -PathType Leaf)) {
        throw "Tracked performance evidence is missing: $ReferenceEvidence"
    }
    $reference = Get-Content -Raw $referencePath | ConvertFrom-Json
    if ($reference.status -ne "verified_preserved") {
        throw "Tracked performance evidence is not verified_preserved."
    }
    $expectedArtifact = $reference.after.artifact

    $privacyTokens = New-Object System.Collections.Generic.List[string]
    foreach ($token in @($env:USERNAME, $env:USERPROFILE, $ForbidToken)) {
        $value = [string]$token
        if (
            -not [string]::IsNullOrWhiteSpace($value) -and
            -not $privacyTokens.Contains($value)
        ) {
            $privacyTokens.Add($value)
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if ($null -eq $pythonCommand) { throw "Python 3 is required." }
    $python = $pythonCommand.Source

    Write-Host "[NW10 CORRECTNESS] entering pinned MSVC/SDK/DXC environment"
    Import-Module (Join-Path $repo "tools/nw01/Nw01.Windows.psm1") -Force
    $entered = Enter-NwBuildEnvironment -RepoRoot $repo
    Write-Host "[NW10 CORRECTNESS] MSVC toolset=$($entered.VisualStudio.VcVarsVersion) SDK=$($entered.VisualStudio.WindowsSdkVersion)"

    $root = Resolve-NwRepoPath $WorkRoot
    $tierRoot = Join-Path $root $Tier
    $commonTemplate = Join-Path $tierRoot "common-template"
    $commonArtifact = Join-Path $tierRoot "common"
    $commonReport = Join-Path $tierRoot "common-report.json"
    $diagTemplate = Join-Path $tierRoot "diagnostic-template"
    $diagArtifact = Join-Path $tierRoot "diagnostic"
    $diagBuildRoot = Join-Path $tierRoot "diagnostic-build"
    $diagReport = Join-Path $tierRoot "diagnostic-report.json"
    $captureDirectory = Join-Path $tierRoot "captures"
    $captureRecord = Join-Path $tierRoot "capture.json"

    Remove-Item $tierRoot -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $tierRoot | Out-Null

    Write-Host "[NW10 CORRECTNESS] rebuilding tracked common artifact identity"
    Invoke-NwNative "cmake" @(
        "--preset", "nw07-release",
        "-B", $commonTemplate,
        "-DNW10_COMPACT_PACKAGE=ON",
        "-DNW_ENFORCE_PINNED_TOOLCHAIN=ON"
    ) "Common release template configure failed"
    Invoke-NwNative "cmake" @(
        "--build", $commonTemplate,
        "--target", "nw_procedural_image_player",
        "--parallel", "2"
    ) "Common release template build failed"

    $dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
    $commonTemplateExe = Join-Path $commonTemplate "staging/procedural_image_player/nw_procedural_image_player.exe"
    Invoke-NwNative $python @(
        "tools/nw10_perf_candidate.py",
        "--output", $commonArtifact,
        "--report", $commonReport,
        "--player-template", $commonTemplateExe,
        "--dxc", $dxc
    ) "Common NW-10 artifact reproduction failed"

    $common = Get-Content -Raw $commonReport | ConvertFrom-Json
    Assert-NwSameValue "artifact_key" $expectedArtifact.artifact_key $common.artifact.artifact_key
    Assert-NwSameValue "package_sha256" $expectedArtifact.package_sha256 $common.artifact.package_sha256
    Assert-NwSameValue "manifest_sha256" $expectedArtifact.manifest_sha256 $common.artifact.manifest_sha256
    Assert-NwSameValue "executable_sha256" $expectedArtifact.executable_sha256 $common.artifact.executable_sha256
    Assert-NwSameValue "complete_distribution_bytes" $expectedArtifact.complete_distribution_bytes $common.artifact.complete_distribution_bytes
    if ($common.runtime_probe.status -ne "passed") {
        throw "Reproduced common artifact runtime probe failed."
    }
    if ($common.runtime_probe.uncapped_present_verified -ne $true) {
        throw "Reproduced common artifact is not uncapped."
    }
    Write-Host "[NW10 CORRECTNESS] COMMON_ARTIFACT=EXACT_MATCH"
    Write-Host "[NW10 CORRECTNESS] PACKAGE_SHA=$($common.artifact.package_sha256)"

    Write-Host "[NW10 CORRECTNESS] building capture-enabled diagnostic sibling"
    Invoke-NwNative "cmake" @(
        "--preset", "nw07-diagnostics",
        "-B", $diagTemplate
    ) "Diagnostic template configure failed"
    Invoke-NwNative "cmake" @(
        "--build", $diagTemplate,
        "--target", "nw_procedural_image_player",
        "--parallel", "2"
    ) "Diagnostic template build failed"
    $diagTemplateExe = Join-Path $diagTemplate "staging/procedural_image_player/nw_procedural_image_player.exe"

    Invoke-NwNative $python @(
        "tools/nw10_common_correctness.py", "build-diagnostic",
        "--output", $diagArtifact,
        "--expected-package", $commonArtifact,
        "--player-template", $diagTemplateExe,
        "--dxc", $dxc,
        "--build-root", $diagBuildRoot,
        "--report", $diagReport
    ) "Diagnostic sibling export failed"

    $diag = Get-Content -Raw $diagReport | ConvertFrom-Json
    if ($diag.status -ne "built") { throw "Diagnostic sibling did not report built." }
    Assert-NwSameValue "diagnostic package_sha256" $expectedArtifact.package_sha256 $diag.common_artifact.package_sha256
    Assert-NwSameValue "diagnostic artifact_key" $expectedArtifact.artifact_key $diag.common_artifact.artifact_key
    if ($diag.capture_controls_required -ne $true) {
        throw "Diagnostic sibling does not require capture controls."
    }
    if ($diag.final_executable_sha256 -eq $diag.diagnostic_executable_sha256) {
        throw "Diagnostic sibling unexpectedly equals final executable."
    }

    $stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    $diagExe = Join-Path $diagArtifact "$stem.exe"
    Write-Host "[NW10 CORRECTNESS] capturing fixed ticks on tier=$Tier"
    Invoke-NwNative $python @(
        "tools/nw10_common_correctness.py", "capture",
        "--package", $diagArtifact,
        "--expected-package", $commonArtifact,
        "--diagnostic-executable", $diagExe,
        "--capture-dir", $captureDirectory,
        "--tier", $Tier,
        "--output", $captureRecord
    ) "Physical correctness capture failed"

    $record = Get-Content -Raw $captureRecord | ConvertFrom-Json
    $expectedStatus = if ($Tier -eq "ada") { "captured_reference" } else { "captured_candidate" }
    Assert-NwSameValue "capture status" $expectedStatus $record.status
    Assert-NwSameValue "capture tier" $Tier $record.tier
    Assert-NwSameValue "capture package_sha256" $expectedArtifact.package_sha256 $record.common_artifact.package_sha256
    Assert-NwSameValue "capture artifact_key" $expectedArtifact.artifact_key $record.common_artifact.artifact_key
    if ($record.loop_boundary.status -ne "passed") {
        throw "Loop-boundary correctness check did not pass."
    }
    if ($record.privacy.raw_capture_bytes_embedded -ne $false) {
        throw "Capture record unexpectedly embeds raw capture bytes."
    }
    if ($record.privacy.local_paths_included -ne $false) {
        throw "Capture record unexpectedly exposes local paths."
    }
    Assert-NwPrivacy -JsonObject $record -Tokens $privacyTokens

    $captureFiles = @(Get-ChildItem $captureDirectory -Filter "*.rgba32f" -File)
    if ($captureFiles.Count -ne 6) {
        throw "Expected 6 RGBA32F capture files, observed $($captureFiles.Count)."
    }
    $rawBytes = ($captureFiles | Measure-Object -Property Length -Sum).Sum
    Write-Host "[NW10 CORRECTNESS] STATUS=$($record.status)"
    Write-Host "[NW10 CORRECTNESS] TIER=$Tier"
    Write-Host "[NW10 CORRECTNESS] GPU=$($record.hardware.adapter_description)"
    Write-Host "[NW10 CORRECTNESS] DRIVER=$($record.hardware.driver_version)"
    Write-Host "[NW10 CORRECTNESS] CAPTURES=$($captureFiles.Count)"
    Write-Host "[NW10 CORRECTNESS] RAW_BYTES=$rawBytes"
    Write-Host "[NW10 CORRECTNESS] LOOP_MAX_ABS=$($record.loop_boundary.maximum_error)"
    Write-Host "[NW10 CORRECTNESS] PRIVACY=PASS"
    Write-Host "[NW10 CORRECTNESS] RECORD=$captureRecord"
    Write-Host "[NW10 CORRECTNESS] CAPTURE_DIR=$captureDirectory"

    if ($Bundle) {
        $bundlePath = Join-Path $root "nw10-correctness-$Tier-bundle.zip"
        New-NwBundle -TierName $Tier -TierRoot $tierRoot -RecordPath $captureRecord -CaptureDirectory $captureDirectory -Destination $bundlePath
    } else {
        Write-Host "[NW10 CORRECTNESS] BUNDLE=NOT_REQUESTED"
    }
}
finally {
    Set-Location $originalLocation
}
