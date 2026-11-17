# NW-10 standalone portable correctness capture.
# PowerShell 5.1 compatible; no repo, Python, CMake, DXC, or Visual Studio needed.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("ada","blackwell")]
    [string]$Tier,
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

function Verify-Bundle {
    $manifestPath = Join-Path $here "bundle-manifest.json"
    if (-not (Test-Path $manifestPath -PathType Leaf)) {
        throw "bundle-manifest.json is missing"
    }
    $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
    foreach ($row in @($manifest.files)) {
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
}

Verify-Bundle

$transfer = Get-Content -Raw (Join-Path $here "transfer.json") | ConvertFrom-Json
$runner = Join-Path $here ([string]$transfer.portable_runner.file)
$runnerSha = (Get-FileHash -Algorithm SHA256 $runner).Hash.ToLowerInvariant()
if ($runnerSha -ne ([string]$transfer.portable_runner.sha256).ToLowerInvariant()) {
    throw "Portable runner SHA-256 does not match transfer metadata"
}

$output = [System.IO.Path]::GetFullPath((Join-Path $here $OutputRoot))
$captures = Join-Path $output "captures"
Remove-Item $output -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $captures | Out-Null

$args = @(
    "--capture-dir", $captures,
    "--required-mask", [string]$transfer.portable_runner.required_implementation_mask
)
foreach ($tick in @($transfer.capture.ticks)) {
    $args += @("--capture-tick", [string]$tick)
}

$stdoutPath = Join-Path $output "runner-output.txt"
$allOutput = @(& $runner @args 2>&1)
$exitCode = $LASTEXITCODE
$allOutput | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
if ($exitCode -ne 0) {
    throw "Portable correctness runner failed with exit $exitCode. See $stdoutPath"
}

$values = @{}
foreach ($line in $allOutput) {
    $text = [string]$line
    $index = $text.IndexOf("=")
    if ($index -gt 0) {
        $key = $text.Substring(0,$index)
        $value = $text.Substring($index+1)
        if ($key.StartsWith("NW10_")) {
            $values[$key] = $value
        }
    }
}

if ($values["NW10_PACKAGE_SHA"] -ne $transfer.common_artifact.package_sha256) {
    throw "Runtime package SHA does not match transfer metadata"
}
$observedMask = [int]$values["NW10_RUNTIME_IMPLEMENTATION_MASK"]
$requiredMask = [int]$transfer.portable_runner.required_implementation_mask
if (($observedMask -band $requiredMask) -ne $requiredMask) {
    throw "Runtime implementation mask does not satisfy required mask"
}
if ($values["NW10_PRESENT_SYNC_INTERVAL"] -ne "0") {
    throw "Portable correctness runner is not uncapped"
}

$adapter = [string]$values["NW10_ADAPTER"]
if ($Tier -eq "blackwell" -and $adapter -notmatch "RTX 50") {
    throw "Expected representative desktop Blackwell/RTX 50-series adapter, observed '$adapter'"
}
if ($Tier -eq "ada" -and $adapter -notmatch "RTX 40") {
    throw "Expected representative desktop Ada/RTX 40-series adapter, observed '$adapter'"
}

$rows = @()
foreach ($tick in @($transfer.capture.ticks)) {
    $name = "tick_$tick.rgba32f"
    $path = Join-Path $captures $name
    if (-not (Test-Path $path -PathType Leaf)) {
        throw "Missing capture $name"
    }
    $item = Get-Item $path
    if ([int64]$item.Length -ne [int64]$transfer.capture.expected_bytes_per_capture) {
        throw "Unexpected capture size for $name"
    }
    $rows += [ordered]@{
        tick = [int64]$tick
        file = $name
        bytes = [int64]$item.Length
        sha256 = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant()
    }
}

$loopStart = Join-Path $captures ("tick_" + [string]$transfer.capture.loop_boundary_ticks[0] + ".rgba32f")
$loopEnd = Join-Path $captures ("tick_" + [string]$transfer.capture.loop_boundary_ticks[1] + ".rgba32f")
$loopStartSha = (Get-FileHash -Algorithm SHA256 $loopStart).Hash.ToLowerInvariant()
$loopEndSha = (Get-FileHash -Algorithm SHA256 $loopEnd).Hash.ToLowerInvariant()
if ($loopStartSha -ne $loopEndSha) {
    throw "Loop-boundary captures are not bit-identical; preserve result for detailed numeric analysis"
}

$status = if ($Tier -eq "ada") { "captured_reference" } else { "captured_candidate" }
$record = [ordered]@{
    format = "nw10-common-artifact-correctness-capture"
    version = 1
    task = "NW-10"
    coverage_role = "common_artifact_correctness"
    status = $status
    tier = $Tier
    execution_kind = "physical"
    capture_protocol = [string]$transfer.capture_protocol
    common_artifact = $transfer.common_artifact
    diagnostic_sibling = [ordered]@{
        executable_sha256 = [string]$transfer.portable_runner.sha256
        executable_bytes = [int64]$transfer.portable_runner.bytes
        manifest_sha256 = [string]$transfer.portable_runner.diagnostic_manifest_sha256
        dependencies = @($transfer.portable_runner.dependencies)
        separate_from_final_executable = $true
        same_common_package = $true
        release_manifest_reused = $false
        portable_capture_runner = $true
        capture_method = "readbackPresentedOutput"
    }
    hardware = [ordered]@{
        adapter_description = $adapter
        driver_version = [string]$values["NW10_DRIVER"]
        vendor_id = [int]$values["NW10_VENDOR_ID"]
        device_id = [int]$values["NW10_DEVICE_ID"]
        present_sync_interval = 0
        device_form_factor = "desktop"
        classified_tier = $Tier
    }
    fixed_ticks = @($transfer.capture.fixed_ticks)
    loop_boundary_ticks = @($transfer.capture.loop_boundary_ticks)
    captures = $rows
    loop_boundary = [ordered]@{
        maximum_error = 0.0
        maximum_relative_error = 0.0
        absolute_tolerance = 0.0001
        relative_tolerance = 0.0001
        status = "passed"
        verification = "bit_identical_sha256"
    }
    privacy = [ordered]@{
        raw_capture_bytes_embedded = $false
        local_paths_included = $false
        stdout_stderr_included = $false
    }
}
$recordPath = Join-Path $output "capture.json"
$record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $recordPath -Encoding UTF8

$publicOutput = Join-Path $output "runner-output.txt"
Remove-Item $publicOutput -Force -ErrorAction SilentlyContinue

$bundleRoot = Join-Path $output "bundle"
New-Item -ItemType Directory -Force $bundleRoot | Out-Null
Copy-Item $recordPath (Join-Path $bundleRoot "capture.json") -Force
Copy-Item $captures (Join-Path $bundleRoot "captures") -Recurse -Force

$fileRows = @()
foreach ($file in Get-ChildItem $bundleRoot -File -Recurse | Sort-Object FullName) {
    $fileRows += [ordered]@{
        path = Get-RelativePath $bundleRoot $file.FullName
        bytes = [int64]$file.Length
        sha256 = (Get-FileHash -Algorithm SHA256 $file.FullName).Hash.ToLowerInvariant()
    }
}
$resultManifest = [ordered]@{
    format = "nw10-correctness-result-bundle"
    version = 1
    task = "NW-10"
    tier = $Tier
    files = $fileRows
}
$resultManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $bundleRoot "bundle-manifest.json") -Encoding UTF8

$zip = Join-Path $output ("nw10-correctness-" + $Tier + "-bundle.zip")
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $zip -CompressionLevel Optimal

Write-Host "[NW10 PORTABLE] STATUS=$status"
Write-Host "[NW10 PORTABLE] TIER=$Tier"
Write-Host "[NW10 PORTABLE] GPU=$adapter"
Write-Host "[NW10 PORTABLE] DRIVER=$($values["NW10_DRIVER"])"
Write-Host "[NW10 PORTABLE] PACKAGE_SHA=$($values["NW10_PACKAGE_SHA"])"
Write-Host "[NW10 PORTABLE] CAPTURES=$($rows.Count)"
Write-Host "[NW10 PORTABLE] LOOP_BOUNDARY=bit_identical"
Write-Host "[NW10 PORTABLE] RESULT_BUNDLE=$zip"
