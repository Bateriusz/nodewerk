# Run one immutable bundle on the current Windows validation host.
param(
    [string]$BundleRoot = "",
    [string]$ExpectedCommit = "",
    [string]$TrustRecord = "",
    [string]$OutputDirectory = "",
    [string]$DeviceTier = "",
    [switch]$VerifyOnly,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$env:PYTHONDONTWRITEBYTECODE = "1"

if (-not $BundleRoot) { $BundleRoot = $PSScriptRoot }
$root = (Resolve-Path -LiteralPath $BundleRoot).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path -Parent $root) "nw11-evidence"
}
$parent = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
$bundlePrefix = $root.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
$actualOutput = Join-Path $parent ([Guid]::NewGuid().ToString("N"))
if ($actualOutput.StartsWith($bundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Evidence must be written outside the bundle"
}
New-Item -ItemType Directory -Path $actualOutput | Out-Null
$actualOutput = (Resolve-Path -LiteralPath $actualOutput).Path
Write-Host "[NW11] Evidence directory $actualOutput"

$runtimeRoot = $null
$code = 6
try {
    if (-not $TrustRecord) { throw "A separate trusted CI verification record is required" }
    $trusted = Get-Content -LiteralPath $TrustRecord -Raw | ConvertFrom-Json
    if ($trusted.status -ne "verified" -or $trusted.manifest_sha256 -cnotmatch '^[a-f0-9]{64}$') {
        throw "Invalid CI verification record"
    }
    $manifestPath = Join-Path $root "bundle-manifest.json"
    if ((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash -ne $trusted.manifest_sha256) {
        throw "Bundle manifest does not match the trusted CI SHA-256"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.commit -ne $trusted.commit -or $manifest.bundle_id -ne $trusted.bundle_id -or
        ($ExpectedCommit -and $manifest.commit -ne $ExpectedCommit)) { throw "Unexpected bundle identity" }

    $verifiedFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [void]$verifiedFiles.Add("bundle-manifest.json")
    foreach ($entry in $manifest.artifacts) {
        $relative = [string]$entry.path
        if ($relative -notmatch '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$' -or
            ($relative -split '/') -contains '..' -or ($relative -split '/') -contains '.' -or
            $relative -match '(^|/)__pycache__(/|$)|\.py[co]$' -or -not $verifiedFiles.Add($relative)) {
            throw "Invalid or duplicate bundle path '$relative'"
        }
    }
    foreach ($item in Get-ChildItem -LiteralPath $root -Force -Recurse) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Bundle contains a reparse point"
        }
        if (-not $item.PSIsContainer) {
            $relative = [IO.Path]::GetRelativePath($root, $item.FullName).Replace([char]92, [char]47)
            if (-not $verifiedFiles.Contains($relative)) { throw "Unexpected bundle file '$relative'" }
        }
    }
    foreach ($entry in $manifest.artifacts) {
        $file = Get-Item -LiteralPath (Join-Path $root $entry.path)
        if ($file.PSIsContainer -or $file.Length -ne $entry.bytes -or
            (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $entry.sha256) {
            throw "Bundle artifact mismatch '$($entry.path)'"
        }
    }
    foreach ($required in @("runtime/python-runtime.zip", "tools/nw11/nw11_verify.py", "tools/nw11/validate-device.py")) {
        if (-not $verifiedFiles.Contains($required)) { throw "Missing verification dependency '$required'" }
    }
    $archive = Join-Path $root "runtime/python-runtime.zip"
    # The cleanup target is created by this invocation and never derived from manifest data.
    $temporaryPath = Join-Path ([IO.Path]::GetTempPath()) ("nodewerk-nw11-" + [Guid]::NewGuid().ToString("N"))
    $runtimeRoot = (New-Item -ItemType Directory -Path $temporaryPath).FullName
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot
    $python = Join-Path $runtimeRoot "python.exe"
    $verifier = Join-Path $root "tools/nw11/nw11_verify.py"
    $verifyArgs = @($verifier, "bundle", "--bundle-root", $root,
        "--json-out", (Join-Path $actualOutput "bundle-verification.json"))
    if ($ExpectedCommit) { $verifyArgs += @("--expected-commit", $ExpectedCommit) }
    & $python -I -B -S @verifyArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "Bundle verification failed" }

    $runner = Join-Path $root "tools/nw11/validate-device.py"
    $runnerArgs = @($runner, "--bundle-root", $root, "--output", $actualOutput)
    if ($ExpectedCommit) { $runnerArgs += @("--expected-commit", $ExpectedCommit) }
    if ($DeviceTier) { $runnerArgs += @("--expected-device-tier", $DeviceTier) }
    if ($DryRun) { $runnerArgs += "--dry-run" }
    if ($VerifyOnly) { $runnerArgs += "--verify-only" }
    $code = 4
    & $python -I -B -S @runnerArgs
    $code = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath (Join-Path $actualOutput "summary.json"))) {
        $code = 4
        throw "Validation did not write a summary"
    }
} catch {
    $message = $_.Exception.Message
    if ($code -eq 0) { $code = 4 }
    @{ status = "failed"; exit_code = $code; reason = $message; completion_satisfied = $false } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $actualOutput "summary.json") -Encoding utf8
    $message | Set-Content -LiteralPath (Join-Path $actualOutput "summary.txt") -Encoding utf8
    [Console]::Error.WriteLine("[NW11-LAUNCH-ERR] $message")
} finally {
    if ($null -ne $runtimeRoot) { Remove-Item -LiteralPath $runtimeRoot -Recurse -Force }
}
exit $code
