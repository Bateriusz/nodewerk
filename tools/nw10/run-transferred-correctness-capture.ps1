# NW-10 transferred diagnostic correctness capture.
# PowerShell 5.1 compatible. Requires Windows/D3D12 and the debug CRT used by
# the bundled diagnostic EXE. No compiler, CMake, DXC, Python, git, or repo.
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

$bundleManifestPath = Join-Path $here "bundle-manifest.json"
if (-not (Test-Path $bundleManifestPath -PathType Leaf)) {
    throw "bundle-manifest.json is missing"
}
$bundleManifest = Get-Content -Raw $bundleManifestPath | ConvertFrom-Json
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

$transfer = Get-Content -Raw (Join-Path $here "transfer.json") | ConvertFrom-Json
if ([string]$transfer.target_tier -ne $Tier) {
    throw "Bundle target tier '$($transfer.target_tier)' does not match requested '$Tier'"
}
$packagingMode = [string]$transfer.packaging_mode
if ($packagingMode -notin @("unpacked", "packed")) {
    throw "Transfer bundle has an unrecognized packaging mode '$packagingMode'"
}
$exe = Join-Path $here ([string]$transfer.diagnostic_sibling.file)
$exeSha = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant()
if ($exeSha -ne ([string]$transfer.diagnostic_sibling.executable_sha256).ToLowerInvariant()) {
    throw "Diagnostic executable SHA-256 does not match transfer metadata"
}

$output = [System.IO.Path]::GetFullPath((Join-Path $here $OutputRoot))
$captures = Join-Path $output "captures"
Remove-Item $output -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $captures | Out-Null
$verificationFile = $null
if ($packagingMode -eq "packed") {
    $verificationFile = "packed-diagnostic.exe"
    $verificationPath = Join-Path $output $verificationFile
    Copy-Item $exe $verificationPath -Force
    $verificationSha = (Get-FileHash -Algorithm SHA256 $verificationPath).Hash.ToLowerInvariant()
    if ($verificationSha -ne ([string]$transfer.diagnostic_sibling.executable_sha256).ToLowerInvariant()) {
        throw "Retained packed diagnostic executable SHA-256 changed"
    }
}

$probeOutput = @(& $exe --frames 1 2>&1)
$probeExit = $LASTEXITCODE
if ($probeExit -ne 0) {
    throw "Diagnostic hardware probe failed with exit $probeExit"
}
$values = @{}
foreach ($line in $probeOutput) {
    $text = [string]$line
    $index = $text.IndexOf("=")
    if ($index -gt 0) {
        $key = $text.Substring(0,$index)
        $value = $text.Substring($index+1)
        if ($key.StartsWith("NW05_")) {
            $values[$key] = $value
        }
    }
}
foreach ($required in @("NW05_ADAPTER","NW05_DRIVER","NW05_VENDOR_ID","NW05_DEVICE_ID","NW05_PRESENT_SYNC_INTERVAL")) {
    if (-not $values.ContainsKey($required)) {
        throw "Hardware probe omitted $required"
    }
}
if ($values["NW05_PRESENT_SYNC_INTERVAL"] -ne "0") {
    throw "Diagnostic sibling is not uncapped"
}
$adapter = [string]$values["NW05_ADAPTER"]
if ($Tier -eq "blackwell" -and $adapter -notmatch "RTX 50") {
    throw "Expected representative Blackwell/RTX 50-series adapter, observed '$adapter'"
}
if ($Tier -eq "ada" -and $adapter -notmatch "RTX 40") {
    throw "Expected representative Ada/RTX 40-series adapter, observed '$adapter'"
}

$chassis = @(Get-CimInstance Win32_SystemEnclosure |
    ForEach-Object { $_.ChassisTypes } | ForEach-Object { [int]$_ })
$battery = $null
try {
    $battery = Get-CimInstance Win32_Battery -ErrorAction Stop
} catch {}
$desktopChassis = @(3, 4, 5, 6, 7, 15, 16, 35, 36)
$laptopChassis = @(8, 9, 10, 11, 14, 30, 31, 32)
$deviceFormFactor = "unknown"
if ($chassis.Count -gt 0 -and -not ($chassis | Where-Object { $desktopChassis -notcontains $_ })) {
    $deviceFormFactor = "desktop"
} elseif ($null -ne $battery -or ($chassis | Where-Object { $laptopChassis -contains $_ })) {
    $deviceFormFactor = "laptop"
}
if ($deviceFormFactor -ne "desktop") {
    throw "Representative desktop correctness requires a measured desktop chassis, observed '$deviceFormFactor'"
}

$captureArgs = @("--capture-dir", $captures)
foreach ($tick in @($transfer.capture.ticks)) {
    $captureArgs += @("--capture-tick", [string]$tick)
}
$captureOutput = @(& $exe @captureArgs 2>&1)
$captureExit = $LASTEXITCODE
if ($captureExit -ne 0) {
    throw "Diagnostic fixed-tick capture failed with exit $captureExit"
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

$status = if ($Tier -eq "ada") { "captured_reference" } else { "captured_candidate" }
$record = [ordered]@{
    format = "nw10-common-artifact-correctness-capture"
    version = 1
    task = "NW-10"
    coverage_role = "common_artifact_correctness"
    status = $status
    tier = $Tier
    execution_kind = "physical"
    packaging_mode = $packagingMode
    capture_protocol = [string]$transfer.capture_protocol
    common_artifact = $transfer.common_artifact
    diagnostic_sibling = [ordered]@{
        executable_sha256 = [string]$transfer.diagnostic_sibling.executable_sha256
        executable_bytes = [int64]$transfer.diagnostic_sibling.executable_bytes
        manifest_sha256 = [string]$transfer.diagnostic_sibling.manifest_sha256
        dependencies = @($transfer.diagnostic_sibling.dependencies)
        separate_from_final_executable = $true
        same_common_package = $true
        release_manifest_reused = $false
        packaging_mode = $packagingMode
        encoded_container_sha256 = $transfer.diagnostic_sibling.encoded_container_sha256
        encoded_container_bytes = $transfer.diagnostic_sibling.encoded_container_bytes
        codec_id = $transfer.diagnostic_sibling.codec_id
        layout_id = $transfer.diagnostic_sibling.layout_id
        verification_file = $verificationFile
    }
    hardware = [ordered]@{
        adapter_description = $adapter
        driver_version = [string]$values["NW05_DRIVER"]
        vendor_id = [int]$values["NW05_VENDOR_ID"]
        device_id = [int]$values["NW05_DEVICE_ID"]
        present_sync_interval = 0
        device_form_factor = $deviceFormFactor
        device_form_factor_source = "win32_system_enclosure_and_battery"
        chassis_types = @($chassis)
        battery_present = ($null -ne $battery)
        classified_tier = $Tier
    }
    fixed_ticks = @($transfer.capture.fixed_ticks)
    loop_boundary_ticks = @($transfer.capture.loop_boundary_ticks)
    captures = $rows
    loop_boundary = [ordered]@{
        status = "captured_pending_numeric_validation"
        absolute_tolerance = 0.0001
        relative_tolerance = 0.0001
    }
    runtime = [ordered]@{
        status = "passed"
        capture_exit_code = 0
        frames_per_tick = 1
        present_sync_interval = 0
    }
    privacy = [ordered]@{
        raw_capture_bytes_embedded = $false
        local_paths_included = $false
        stdout_stderr_included = $false
    }
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$recordPath = Join-Path $output "capture.json"
[System.IO.File]::WriteAllText(
    $recordPath,
    (($record | ConvertTo-Json -Depth 12) + [Environment]::NewLine),
    $utf8NoBom
)

$bundleRoot = Join-Path $output "bundle"
New-Item -ItemType Directory -Force $bundleRoot | Out-Null
Copy-Item $recordPath (Join-Path $bundleRoot "capture.json") -Force
Copy-Item $captures (Join-Path $bundleRoot "captures") -Recurse -Force
if ($packagingMode -eq "packed") {
    Copy-Item (Join-Path $output $verificationFile) (Join-Path $bundleRoot $verificationFile) -Force
}
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
[System.IO.File]::WriteAllText(
    (Join-Path $bundleRoot "bundle-manifest.json"),
    (($resultManifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
    $utf8NoBom
)

$zip = Join-Path $output ("nw10-correctness-" + $Tier + "-bundle.zip")
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $zip -CompressionLevel Optimal

$rawBytes = [int64]0
foreach ($row in $rows) {
    $rawBytes += [int64]$row["bytes"]
}

Write-Host "[NW10 TRANSFER] STATUS=$status"
Write-Host "[NW10 TRANSFER] TIER=$Tier"
Write-Host "[NW10 TRANSFER] GPU=$adapter"
Write-Host "[NW10 TRANSFER] DRIVER=$($values["NW05_DRIVER"])"
Write-Host "[NW10 TRANSFER] CAPTURES=$($rows.Count)"
Write-Host "[NW10 TRANSFER] RAW_BYTES=$rawBytes"
Write-Host "[NW10 TRANSFER] RESULT_BUNDLE=$zip"
