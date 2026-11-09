#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ArchiveRoot,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path,
    [string]$FullSummary = "",
    [string]$CleanSystemPackage = "",
    [string]$CleanSystemZip = "",
    [switch]$Replace
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-NwJsonFile([string]$Path) {
    if (-not (Test-Path $Path -PathType Leaf)) { return $null }
    try {
        return (Get-Content -Raw $Path | ConvertFrom-Json)
    } catch {
        throw "Unable to parse JSON artifact '$Path': $($_.Exception.Message)"
    }
}

function Test-NwCommit([string]$Value) {
    return ($Value -match '^[0-9a-fA-F]{40}$')
}

function Read-NwZipTextEntry([string]$ZipPath, [string]$EntryName) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entry = @($zip.Entries | Where-Object { $_.FullName.Replace('\', '/') -eq $EntryName }) | Select-Object -First 1
        if ($null -eq $entry) { return $null }
        $reader = New-Object System.IO.StreamReader($entry.Open())
        try {
            return $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
        }
    } finally {
        $zip.Dispose()
    }
}

function Test-NwSha256Hex([string]$Value) {
    return ($Value -match '^[0-9a-fA-F]{64}$')
}

function Get-NwValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $Default
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Get-NwCertificationProblems(
    [string]$Kind,
    $BuildChecks,
    $ToolchainManifest,
    [object[]]$Ledgers,
    $DeployHashes,
    [string]$Toolchain,
    [string]$ExpectedSourceCommit
) {
    $problems = New-Object System.Collections.Generic.List[string]
    if ([string]::IsNullOrWhiteSpace($Toolchain)) {
        $problems.Add("$Kind does not record a toolchain.")
        return $problems.ToArray()
    }
    if ($Toolchain -notin @("msvc-ltcg", "clang-cl-lld")) {
        $problems.Add("$Kind records unknown toolchain '$Toolchain'.")
        return $problems.ToArray()
    }
    if ($null -eq $BuildChecks) {
        $problems.Add("$Kind Full build evidence is missing.")
    } else {
        $buildChecksCommit = [string](Get-NwValue $BuildChecks "source_commit" "")
        if ([string]::IsNullOrWhiteSpace($buildChecksCommit)) {
            $problems.Add("$Kind build-checks.json does not record source_commit.")
        } elseif (-not [string]::Equals($buildChecksCommit, $ExpectedSourceCommit, [System.StringComparison]::OrdinalIgnoreCase)) {
            $problems.Add("$Kind build-checks.json source_commit '$buildChecksCommit' does not match artifact source_commit '$ExpectedSourceCommit'.")
        }
        if ((Get-NwValue $BuildChecks "full_baseline_passed" $false) -ne $true) {
            $problems.Add("$Kind Full build evidence did not pass the full baseline.")
        }
    }
    $selected = [string](Get-NwValue (Get-NwValue $ToolchainManifest "selected_toolchain" $null) "toolchain_id" "")
    if ($selected -ne $Toolchain) {
        $problems.Add("$Kind toolchain manifest selection '$selected' does not match '$Toolchain'.")
    }
    $fixtureExecutables = [ordered]@{
        empty_player = "nw_empty_player.exe"
        clear_present = "nw_clear_present_fixture.exe"
        compute_shader = "nw_compute_shader_fixture.exe"
    }
    $safeLedgers = @($Ledgers | Where-Object { $null -ne $_ })
    foreach ($fixture in $fixtureExecutables.Keys) {
        $executable = $fixtureExecutables[$fixture]
        $deployHash = [string]$DeployHashes[$executable.ToLowerInvariant()]
        if (-not (Test-NwSha256Hex $deployHash)) {
            $problems.Add("$Kind deploy hash for '$executable' is missing or invalid.")
            continue
        }
        $ledger = $safeLedgers | Where-Object { [string](Get-NwValue $_ "fixture_id" "") -eq $fixture } | Select-Object -First 1
        if ($null -eq $ledger) {
            $problems.Add("$Kind has no ledger for $fixture.")
            continue
        }
        if ((Get-NwValue $ledger "verification_status" "") -ne "verified" -or
            (Get-NwValue $ledger "toolchain_id" "") -ne $Toolchain -or
            (Get-NwValue $ledger "build_profile" "") -ne "release") {
            $problems.Add("$Kind ledger for $fixture is not a verified release ledger for $Toolchain.")
            continue
        }
        $component = @((Get-NwValue $ledger "distributed_components" @()) |
            Where-Object { (Get-NwValue $_ "category" "") -eq "executable" }) | Select-Object -First 1
        $ledgerHash = ([string](Get-NwValue $component "sha256" "")).ToLowerInvariant()
        if (-not (Test-NwSha256Hex $ledgerHash) -or $ledgerHash -ne $deployHash) {
            $problems.Add("$Kind ledger hash for $fixture does not match the verified deploy executable hash.")
        }
    }
    return $problems.ToArray()
}

function Assert-NwManifestFileEntries($Manifest, [string]$Kind) {
    $files = @(Get-NwValue $Manifest "files" @())
    if ($files.Count -eq 0) { throw "$Kind artifact manifest does not list executable files." }
    $hashes = @{}
    foreach ($item in $files) {
        $name = [string]$item.name
        $sha256 = ([string]$item.sha256).ToLowerInvariant()
        $bytes = 0
        try { $bytes = [long]$item.bytes } catch { $bytes = 0 }
        if ([string]::IsNullOrWhiteSpace($name) -or $name -match '[\\/]') {
            throw "$Kind artifact manifest contains an invalid executable name '$name'."
        }
        if (-not (Test-NwSha256Hex $sha256)) {
            throw "$Kind artifact manifest contains an invalid SHA-256 for '$name'."
        }
        if ($bytes -le 0) {
            throw "$Kind artifact manifest contains an invalid byte count for '$name'."
        }
        $hashes[$name.ToLowerInvariant()] = [ordered]@{ name = $name; sha256 = $sha256; bytes = $bytes }
    }
    foreach ($required in @("nw_empty_player.exe", "nw_clear_present_fixture.exe", "nw_compute_shader_fixture.exe")) {
        if (-not $hashes.ContainsKey($required.ToLowerInvariant())) {
            throw "$Kind artifact manifest does not record required executable '$required'."
        }
    }
    return $hashes
}

function Assert-NwPackageDeployHashes([string]$PackageRoot, $Manifest) {
    $expected = Assert-NwManifestFileEntries $Manifest "CleanSystem package"
    $actualHashes = @{}
    $expectedEntryNames = @{}
    $deployRoot = [System.IO.Path]::GetFullPath((Join-Path $PackageRoot "deploy"))
    foreach ($key in $expected.Keys) {
        $expectedEntryNames["deploy/$(Get-NwValue $expected[$key] 'name' '')"] = $true
    }
    foreach ($key in $expected.Keys) {
        $entry = $expected[$key]
        $entryName = [string](Get-NwValue $entry "name" "")
        $entryBytes = [long](Get-NwValue $entry "bytes" 0)
        $entrySha256 = [string](Get-NwValue $entry "sha256" "")
        $deployPath = Join-Path $deployRoot $entryName
        if (-not (Test-Path $deployPath -PathType Leaf)) {
            throw "CleanSystem package is missing deploy executable '$entryName'."
        }
        $item = Get-Item $deployPath
        if ($item.Length -ne $entryBytes) {
            throw "CleanSystem package executable '$entryName' size $($item.Length) does not match recorded $entryBytes."
        }
        $actual = (Get-FileHash -Algorithm SHA256 -Path $deployPath).Hash.ToLowerInvariant()
        if ($actual -ne $entrySha256) {
            throw "CleanSystem package executable '$entryName' hash $actual does not match recorded $entrySha256."
        }
        $actualHashes[$key] = $actual
    }
    $subdirectories = @(Get-ChildItem -Path $deployRoot -Directory -Recurse -Force -ErrorAction SilentlyContinue)
    if ($subdirectories.Count -gt 0) {
        throw "CleanSystem package deploy tree contains unrecorded subdirectory payloads: $($subdirectories.Name -join ', ')."
    }
    $unexpected = @(Get-ChildItem -Path $deployRoot -File -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $relative = $_.FullName.Substring($deployRoot.Length).TrimStart('\', '/').Replace('\', '/')
            -not $expectedEntryNames.ContainsKey("deploy/$relative")
        })
    if ($unexpected.Count -gt 0) {
        throw "CleanSystem package deploy tree contains unrecorded file(s): $($unexpected.Name -join ', ')."
    }
    return $actualHashes
}

function Assert-NwZipDeployHashes([string]$ZipPath, $Manifest) {
    $expected = Assert-NwManifestFileEntries $Manifest "CleanSystem ZIP"
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entries = @{}
        foreach ($entry in $zip.Entries) {
            $entries[$entry.FullName.Replace('\', '/')] = $entry
        }
        $actualHashes = @{}
        foreach ($key in $expected.Keys) {
            $recorded = $expected[$key]
            $recordedName = [string](Get-NwValue $recorded "name" "")
            $recordedBytes = [long](Get-NwValue $recorded "bytes" 0)
            $recordedSha256 = [string](Get-NwValue $recorded "sha256" "")
            $entryName = "deploy/$recordedName"
            if (-not $entries.ContainsKey($entryName)) {
                throw "CleanSystem ZIP is missing deploy executable '$recordedName'."
            }
            $entry = $entries[$entryName]
            if ([long]$entry.Length -ne $recordedBytes) {
                throw "CleanSystem ZIP executable '$recordedName' size $($entry.Length) does not match recorded $recordedBytes."
            }
            $stream = $entry.Open()
            try {
                $sha = [System.Security.Cryptography.SHA256]::Create()
                try {
                    $digest = $sha.ComputeHash($stream)
                } finally {
                    $sha.Dispose()
                }
            } finally {
                $stream.Dispose()
            }
            $actual = -join ($digest | ForEach-Object { $_.ToString("x2") })
            if ($actual -ne $recordedSha256) {
                throw "CleanSystem ZIP executable '$recordedName' hash $actual does not match recorded $recordedSha256."
            }
            $actualHashes[$key] = $actual
        }
        $expectedEntryNames = @{}
        foreach ($key in $expected.Keys) { $expectedEntryNames["deploy/$(Get-NwValue $expected[$key] 'name' '')"] = $true }
        $unexpectedEntries = @($entries.Keys | Where-Object {
            $_.StartsWith("deploy/", [System.StringComparison]::OrdinalIgnoreCase) -and
            -not $expectedEntryNames.ContainsKey($_)
        })
        if ($unexpectedEntries.Count -gt 0) {
            throw "CleanSystem ZIP deploy entries are unrecorded: $($unexpectedEntries -join ', ')."
        }
        return $actualHashes
    } finally {
        $zip.Dispose()
    }
}

try {
    $worktreeRoot = (& git -C $RepoRoot rev-parse --show-toplevel 2>$null | Select-Object -First 1)
    $worktreeRoot = ([string]$worktreeRoot).Trim()
    if ([string]::IsNullOrWhiteSpace($worktreeRoot)) { $worktreeRoot = $RepoRoot }
    $repo = [System.IO.Path]::GetFullPath($worktreeRoot).TrimEnd([char[]]@('\', '/'))
    $archive = [System.IO.Path]::GetFullPath($ArchiveRoot).TrimEnd([char[]]@('\', '/'))
    $repoWithSeparator = $repo + [System.IO.Path]::DirectorySeparatorChar
    if ($archive -eq $repo -or $archive.StartsWith($repoWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "ArchiveRoot must be outside the Git working tree."
    }

    $checkoutCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    $checkoutCommit = ([string]$checkoutCommit).Trim()
    if (-not (Test-NwCommit $checkoutCommit)) { throw "Unable to resolve repository HEAD." }

    # Label the archive with the revision the supplied evidence was produced
    # from, not merely the revision currently checked out. Historical evidence
    # stays historical and is never relabelled as current certifying evidence.
    $revisions = New-Object System.Collections.Generic.List[object]
    $packageManifest = $null
    $packageDeployHashes = $null
    $zipDeployHashes = $null
    $hasUnverifiedProvenance = $false
    $unverifiedArtifacts = New-Object System.Collections.Generic.List[string]
    $summaryCertifies = $false
    $packageCertifies = $false
    $zipCertifies = $false
    $certificationProblems = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($FullSummary)) {
        $summary = Read-NwJsonFile $FullSummary
        if ($null -eq $summary) { throw "Archive input does not exist: $FullSummary" }
        $summaryCertifies = ((Get-NwValue $summary "full_baseline_passed" $false) -eq $true)
        if (-not $summaryCertifies) {
            $certificationProblems.Add("full-summary did not pass the full baseline.")
        }
        $summarySourceProperty = $summary.PSObject.Properties["source_commit"]
        $summaryCommit = if ($null -eq $summarySourceProperty) { "" } else { ([string]$summarySourceProperty.Value).Trim() }
        if ([string]::IsNullOrWhiteSpace($summaryCommit)) {
            $hasUnverifiedProvenance = $true
            $summaryCertifies = $false
            $unverifiedArtifacts.Add("full-summary")
            $certificationProblems.Add("full-summary does not record source_commit.")
        } else {
            if (-not (Test-NwCommit $summaryCommit)) {
                throw "FullSummary source_commit '$summaryCommit' is not a full Git revision."
            }
            $revisions.Add([pscustomobject]@{ Artifact = "full-summary"; SourceCommit = $summaryCommit.ToLowerInvariant() })
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($CleanSystemPackage)) {
        $manifestPath = Join-Path $CleanSystemPackage "evidence/artifact-manifest.json"
        $packageManifest = Read-NwJsonFile $manifestPath
        if ($null -eq $packageManifest) {
            throw "CleanSystem package artifact manifest is missing: $manifestPath"
        }
        if ($packageManifest.PSObject.Properties.Name -notcontains "source_commit") {
            throw "CleanSystem package artifact manifest does not record source_commit."
        }
        $packageCommit = ([string]$packageManifest.source_commit).Trim()
        if (-not (Test-NwCommit $packageCommit)) {
            throw "CleanSystem package source_commit '$packageCommit' is not a full Git revision."
        }
        $revisions.Add([pscustomobject]@{ Artifact = "clean-system-package"; SourceCommit = $packageCommit.ToLowerInvariant() })
        $packageDeployHashes = Assert-NwPackageDeployHashes -PackageRoot $CleanSystemPackage -Manifest $packageManifest
        $packageEvidence = Join-Path $CleanSystemPackage "evidence"
        $packageBuildChecks = Read-NwJsonFile (Join-Path $packageEvidence "build-checks.json")
        $packageToolchainManifest = Read-NwJsonFile (Join-Path $packageEvidence "toolchain-manifest.v1.json")
        $packageLedgers = @()
        foreach ($ledgerFile in @(Get-ChildItem -Path (Join-Path $packageEvidence "ledgers") -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
            $packageLedgers += Read-NwJsonFile $ledgerFile.FullName
        }
        $packageProblems = @(Get-NwCertificationProblems `
            -Kind "clean-system-package" `
            -BuildChecks $packageBuildChecks `
            -ToolchainManifest $packageToolchainManifest `
            -Ledgers $packageLedgers `
            -DeployHashes $packageDeployHashes `
            -Toolchain ([string](Get-NwValue $packageManifest "toolchain" "")) `
            -ExpectedSourceCommit $packageCommit)
        if ($packageProblems.Count -eq 0) {
            $packageCertifies = $true
        } else {
            foreach ($problem in $packageProblems) { $certificationProblems.Add($problem) }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($CleanSystemZip)) {
        if (-not (Test-Path $CleanSystemZip -PathType Leaf)) { throw "Archive input does not exist: $CleanSystemZip" }
        $zipManifestText = Read-NwZipTextEntry -ZipPath $CleanSystemZip -EntryName "evidence/artifact-manifest.json"
        if ([string]::IsNullOrWhiteSpace($zipManifestText)) {
            $hasUnverifiedProvenance = $true
            $unverifiedArtifacts.Add("clean-system-zip")
            $certificationProblems.Add("clean-system-zip has no embedded artifact manifest.")
            Write-Warning "[NW-ARCHIVE] CleanSystem ZIP has no embedded evidence/artifact-manifest.json; classifying that artifact as unverified provenance."
        } else {
            try {
                $zipManifest = $zipManifestText | ConvertFrom-Json
            } catch {
                throw "Unable to parse the CleanSystem ZIP artifact manifest: $($_.Exception.Message)"
            }
            if ($zipManifest.PSObject.Properties.Name -notcontains "source_commit") {
                $hasUnverifiedProvenance = $true
                $unverifiedArtifacts.Add("clean-system-zip")
                $certificationProblems.Add("clean-system-zip artifact manifest does not record source_commit.")
                Write-Warning "[NW-ARCHIVE] CleanSystem ZIP artifact manifest does not record source_commit; classifying that artifact as unverified provenance."
            } else {
                $zipCommit = ([string]$zipManifest.source_commit).Trim()
                if (-not (Test-NwCommit $zipCommit)) {
                    throw "CleanSystem ZIP source_commit '$zipCommit' is not a full Git revision."
                }
                $revisions.Add([pscustomobject]@{ Artifact = "clean-system-zip"; SourceCommit = $zipCommit.ToLowerInvariant() })
                $zipDeployHashes = Assert-NwZipDeployHashes -ZipPath $CleanSystemZip -Manifest $zipManifest
                $zipToolchain = [string](Get-NwValue $zipManifest "toolchain" "")
                $zipBuildChecks = $null
                $zipBuildChecksText = Read-NwZipTextEntry -ZipPath $CleanSystemZip -EntryName "evidence/build-checks.json"
                if (-not [string]::IsNullOrWhiteSpace($zipBuildChecksText)) {
                    try {
                        $zipBuildChecks = $zipBuildChecksText | ConvertFrom-Json
                    } catch {
                        $certificationProblems.Add("clean-system-zip build-checks.json is malformed: $($_.Exception.Message)")
                    }
                }
                $zipToolchainManifest = $null
                $zipToolchainManifestText = Read-NwZipTextEntry -ZipPath $CleanSystemZip -EntryName "evidence/toolchain-manifest.v1.json"
                if (-not [string]::IsNullOrWhiteSpace($zipToolchainManifestText)) {
                    try {
                        $zipToolchainManifest = $zipToolchainManifestText | ConvertFrom-Json
                    } catch {
                        $certificationProblems.Add("clean-system-zip toolchain-manifest.v1.json is malformed: $($_.Exception.Message)")
                    }
                }
                $zipLedgers = @()
                $fixtureFileNames = [ordered]@{
                    empty_player = "empty-player"
                    clear_present = "clear-present"
                    compute_shader = "compute-fixture"
                }
                $ledgerSuffix = if ($zipToolchain -eq "clang-cl-lld") { "clang-cl" } else { $zipToolchain }
                foreach ($fixture in $fixtureFileNames.Keys) {
                    $ledgerEntry = "evidence/ledgers/{0}-{1}-release.json" -f $fixtureFileNames[$fixture], $ledgerSuffix
                    $ledgerText = Read-NwZipTextEntry -ZipPath $CleanSystemZip -EntryName $ledgerEntry
                    if ([string]::IsNullOrWhiteSpace($ledgerText)) { continue }
                    try {
                        $zipLedgers += $ledgerText | ConvertFrom-Json
                    } catch {
                        $certificationProblems.Add("clean-system-zip ledger '$ledgerEntry' is malformed: $($_.Exception.Message)")
                    }
                }
                $zipProblems = @(Get-NwCertificationProblems `
                    -Kind "clean-system-zip" `
                    -BuildChecks $zipBuildChecks `
                    -ToolchainManifest $zipToolchainManifest `
                    -Ledgers $zipLedgers `
                    -DeployHashes $zipDeployHashes `
                    -Toolchain $zipToolchain `
                    -ExpectedSourceCommit $zipCommit)
                if ($zipProblems.Count -eq 0 -and $certificationProblems.Count -eq 0) {
                    $zipCertifies = $true
                } else {
                    foreach ($problem in $zipProblems) { $certificationProblems.Add($problem) }
                }
                if ($null -ne $packageManifest) {
                    if ($zipDeployHashes.Count -ne $packageDeployHashes.Count) {
                        throw "CleanSystem ZIP manifest does not match the supplied package manifest (file count differs)."
                    }
                    foreach ($name in $packageDeployHashes.Keys) {
                        if (-not $zipDeployHashes.ContainsKey($name) -or $zipDeployHashes[$name] -ne $packageDeployHashes[$name]) {
                            throw "CleanSystem ZIP manifest does not match the supplied package manifest for '$name'."
                        }
                    }
                }
            }
        }
    }

    $distinctCommits = @($revisions | ForEach-Object { $_.SourceCommit } | Select-Object -Unique)
    if ($distinctCommits.Count -gt 1) {
        $detail = ($revisions | ForEach-Object { "$($_.Artifact)=$($_.SourceCommit)" }) -join ", "
        throw "Archive inputs come from conflicting source revisions: $detail"
    }
    if ($distinctCommits.Count -eq 1) {
        $sourceCommit = [string]$distinctCommits[0]
        $sourceSource = ($revisions | ForEach-Object { $_.Artifact } | Select-Object -Unique) -join "+"
    } else {
        # No artifact exposes provenance (for example a bare ZIP or an older
        # summary). Fall back to the checked-out revision but mark the archive
        # explicitly as checkout-labelled rather than certifying evidence.
        $sourceCommit = $checkoutCommit.ToLowerInvariant()
        $sourceSource = "checkout-fallback"
        $certificationProblems.Add("No supplied artifact exposes a source revision.")
    }
    $isCurrent = $sourceCommit -eq $checkoutCommit.ToLowerInvariant()
    $inputCertifications = @()
    if (-not [string]::IsNullOrWhiteSpace($FullSummary)) { $inputCertifications += $summaryCertifies }
    if (-not [string]::IsNullOrWhiteSpace($CleanSystemPackage)) { $inputCertifications += $packageCertifies }
    if (-not [string]::IsNullOrWhiteSpace($CleanSystemZip)) { $inputCertifications += $zipCertifies }
    $allInputsCertify = ($inputCertifications.Count -gt 0) -and ($inputCertifications -notcontains $false)
    $evidenceClass = if ($isCurrent -and $sourceSource -ne "checkout-fallback" -and -not $hasUnverifiedProvenance -and $allInputsCertify) {
        "current-certifying"
    } elseif ($isCurrent) {
        "current-non-certifying"
    } else {
        "historical"
    }

    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $unique = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $dest = Join-Path $archive ("{0}-{1}-{2}" -f $sourceCommit.Substring(0, 12), $stamp, $unique)
    if ((Test-Path $dest) -and -not $Replace) {
        throw "Archive destination already exists: $dest. Pass -Replace to overwrite prior evidence explicitly."
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null

    $copied = @()
    foreach ($item in @(
        @{ Path = $FullSummary; Name = "full-summary.json"; Kind = "file" },
        @{ Path = $CleanSystemPackage; Name = "clean-system-package"; Kind = "directory" },
        @{ Path = $CleanSystemZip; Name = "clean-system-package.zip"; Kind = "file" }
    )) {
        if ([string]::IsNullOrWhiteSpace([string]$item.Path)) { continue }
        if (-not (Test-Path $item.Path)) { throw "Archive input does not exist: $($item.Path)" }
        $target = Join-Path $dest $item.Name
        if ((Test-Path $target) -and -not $Replace) {
            throw "Refusing to overwrite archived artifact '$target' without -Replace."
        }
        if (Test-Path $item.Path -PathType Container) {
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            Copy-Item -Recurse -Force (Join-Path $item.Path '*') $target
        } else {
            Copy-Item -Force $item.Path $target
        }
        $copied += $item.Name
    }

    $note = if ($evidenceClass -eq "current-certifying") {
        "Archive copy of artifacts produced from the checked-out revision; repository verifier provenance remains authoritative."
    } elseif ($evidenceClass -eq "current-non-certifying") {
        "Archive copy of current-revision artifacts that do not certify the full baseline; see certification_problems."
    } else {
        "Historical archive. It is not current certifying evidence for the checked-out revision."
    }
    [ordered]@{
        schema_version = "1"
        source_commit = $sourceCommit
        source_commit_origin = $sourceSource
        checkout_head = $checkoutCommit.ToLowerInvariant()
        evidence_class = $evidenceClass
        certifying = ($evidenceClass -eq "current-certifying")
        certification_problems = @($certificationProblems)
        archived_at_utc = [DateTime]::UtcNow.ToString("o")
        artifacts = $copied
        source_revisions = @($revisions | ForEach-Object { [ordered]@{ artifact = $_.Artifact; source_commit = $_.SourceCommit } })
        unverified_artifacts = @($unverifiedArtifacts)
        note = $note
    } | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $dest "archive-manifest.json")

    Write-Host "[NW-ARCHIVE] archived source $sourceCommit ($evidenceClass) at $dest"
    exit 0
} catch {
    Write-Error "[NW-ARCHIVE] $($_.Exception.Message)"
    exit 1
}
