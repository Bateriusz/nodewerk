# NW-10 transferred clean-system validation.
# PowerShell 5.1 compatible. Requires only Windows 11 x64, Python 3, and the
# files in this transfer bundle. No compiler, CMake, Ninja, DXC, Slang, git,
# SDK activation, or repository checkout is required on the target.
[CmdletBinding()]
param(
    [string]$OutputRoot = ".\result"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Get-RelativePath([string]$Root, [string]$Path) {
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside bundle root: $Path"
    }
    return $pathFull.Substring($rootFull.Length).TrimStart('\','/').Replace('\','/')
}

$bundleManifestPath = Join-Path $here "bundle-manifest.json"
if (-not (Test-Path $bundleManifestPath -PathType Leaf)) {
    throw "bundle-manifest.json is missing"
}
$bundleManifest = Get-Content -Raw $bundleManifestPath | ConvertFrom-Json
if ([string]$bundleManifest.format -ne "nw10-clean-system-transfer" -or [int]$bundleManifest.version -ne 1) {
    throw "Unexpected clean-system transfer manifest"
}
foreach ($row in @($bundleManifest.files)) {
    $path = Join-Path $here ([string]$row.path)
    if (-not (Test-Path $path -PathType Leaf)) {
        throw "Bundle file is missing: $($row.path)"
    }
    $item = Get-Item $path
    if ([int64]$item.Length -ne [int64]$row.bytes) {
        throw "Bundle file size mismatch: $($row.path)"
    }
    $sha = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant()
    if ($sha -ne ([string]$row.sha256).ToLowerInvariant()) {
        throw "Bundle file SHA-256 mismatch: $($row.path)"
    }
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) { $python = Get-Command python3.exe -ErrorAction SilentlyContinue }
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) {
    throw "Python 3 is required by the repository clean-system verifier."
}

$deployment = Join-Path $here "deployment"
$package = Join-Path $here "package"
$fixture = Join-Path $here "fixture\nw_package_player_fixture.exe"
$verifier = Join-Path $here "verifier\tools\nw07_clean_system.py"
foreach ($required in @($deployment, $package, $fixture, $verifier)) {
    if (-not (Test-Path $required)) { throw "Required bundle path is missing: $required" }
}

$deploymentFiles = @(Get-ChildItem $deployment -File)
if ($deploymentFiles.Count -ne 1 -or $deploymentFiles[0].Extension -ne ".exe") {
    throw "Deployment must contain exactly one executable and no sidecars."
}
$expectedExeSha = ([string]$bundleManifest.final_executable_sha256).ToLowerInvariant()
$actualExeSha = (Get-FileHash -Algorithm SHA256 $deploymentFiles[0].FullName).Hash.ToLowerInvariant()
if ($actualExeSha -ne $expectedExeSha) {
    throw "Final executable SHA-256 does not match transfer metadata."
}

$output = [System.IO.Path]::GetFullPath((Join-Path $here $OutputRoot))
$outputMarker = Join-Path $output ".nw10-clean-system-output.v1"
if (Test-Path $output -PathType Container) {
    $existing = @(Get-ChildItem $output -Force)
    if ($existing.Count -gt 0 -and -not (Test-Path $outputMarker -PathType Leaf)) {
        throw "Refusing to reuse non-empty OutputRoot not owned by NW-10: $output"
    }
} else {
    New-Item -ItemType Directory -Force $output | Out-Null
}
if (-not (Test-Path $outputMarker -PathType Leaf)) {
    [System.IO.File]::WriteAllText($outputMarker, "nw10-clean-system-output-v1")
}
# OutputRoot may be operator-selected. Never delete it; a non-empty directory
# is reusable only after this runner has marked ownership.
$ownedOutputPaths = @(
    (Join-Path $output "clean-system-report.json"),
    (Join-Path $output "clean-system-report-artifacts"),
    (Join-Path $output "bundle"),
    (Join-Path $output "nw10-clean-system-result.zip")
)
foreach ($ownedPath in $ownedOutputPaths) {
    Remove-Item $ownedPath -Recurse -Force -ErrorAction SilentlyContinue
}
$report = Join-Path $output "clean-system-report.json"

& $python.Source $verifier --fixture $fixture --package $package --deployment $deployment --execution-kind physical --output $report
$code = $LASTEXITCODE
if (-not (Test-Path $report -PathType Leaf)) {
    throw "Clean-system verifier did not produce a report (exit=$code)."
}
$data = Get-Content -Raw $report | ConvertFrom-Json
if ($code -ne 0 -or [string]$data.verification_status -ne "verified") {
    Write-Host "[NW10 CLEAN] STATUS=$($data.verification_status)"
    Write-Host "[NW10 CLEAN] REPORT=$report"
    exit $(if ($code -ne 0) { $code } else { 1 })
}

$resultRoot = Join-Path $output "bundle"
New-Item -ItemType Directory -Force $resultRoot | Out-Null
Copy-Item $report (Join-Path $resultRoot "clean-system-report.json") -Force
$artifactDir = Join-Path $output "clean-system-report-artifacts"
if (Test-Path $artifactDir -PathType Container) {
    Copy-Item $artifactDir (Join-Path $resultRoot "artifacts") -Recurse -Force
}
$fileRows = @()
foreach ($file in Get-ChildItem $resultRoot -File -Recurse | Sort-Object FullName) {
    $fileRows += [ordered]@{
        path = Get-RelativePath $resultRoot $file.FullName
        bytes = [int64]$file.Length
        sha256 = (Get-FileHash -Algorithm SHA256 $file.FullName).Hash.ToLowerInvariant()
    }
}
$resultManifest = [ordered]@{
    format = "nw10-clean-system-result-bundle"
    version = 1
    task = "NW-10"
    final_executable_sha256 = $expectedExeSha
    verification_status = [string]$data.verification_status
    files = $fileRows
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    (Join-Path $resultRoot "bundle-manifest.json"),
    (($resultManifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
    $utf8NoBom
)
$zip = Join-Path $output "nw10-clean-system-result.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $resultRoot "*") -DestinationPath $zip -CompressionLevel Optimal

$hostInfo = $data.host.virtualization_probe
Write-Host "[NW10 CLEAN] STATUS=verified"
Write-Host "[NW10 CLEAN] MANUFACTURER=$($hostInfo.manufacturer)"
Write-Host "[NW10 CLEAN] MODEL=$($hostInfo.model)"
Write-Host "[NW10 CLEAN] HYPERVISOR_PRESENT=$($hostInfo.hypervisor_present)"
Write-Host "[NW10 CLEAN] VBS_STATUS=$($hostInfo.virtualization_based_security_status)"
Write-Host "[NW10 CLEAN] FINAL_EXE_SHA256=$expectedExeSha"
Write-Host "[NW10 CLEAN] RESULT_BUNDLE=$zip"
