# Run one immutable bundle on the current Windows validation host.
param(
    [string]$BundleRoot = "",
    [string]$ExpectedCommit = "",
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
    $manifest = Get-Content -LiteralPath (Join-Path $root "bundle-manifest.json") -Raw | ConvertFrom-Json
    $relative = "runtime/python-runtime.zip"
    $entries = @($manifest.artifacts | Where-Object { $_.path -ceq $relative })
    $archive = Join-Path $root $relative
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($entries.Count -ne 1 -or $hash -ne $entries[0].sha256) {
        throw "Bundled Python runtime hash mismatch"
    }
    # The cleanup target is created by this invocation and never derived from manifest data.
    $temporaryPath = Join-Path ([IO.Path]::GetTempPath()) ("nodewerk-nw11-" + [Guid]::NewGuid().ToString("N"))
    $runtimeRoot = (New-Item -ItemType Directory -Path $temporaryPath).FullName
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot
    $python = Join-Path $runtimeRoot "python.exe"
    $verifier = Join-Path $root "tools/nw11/nw11_verify.py"
    $verifyArgs = @($verifier, "bundle", "--bundle-root", $root,
        "--json-out", (Join-Path $actualOutput "bundle-verification.json"))
    if ($ExpectedCommit) { $verifyArgs += @("--expected-commit", $ExpectedCommit) }
    & $python @verifyArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "Bundle verification failed" }

    $runner = Join-Path $root "tools/nw11/validate-device.py"
    $runnerArgs = @($runner, "--bundle-root", $root, "--output", $actualOutput)
    if ($ExpectedCommit) { $runnerArgs += @("--expected-commit", $ExpectedCommit) }
    if ($DeviceTier) { $runnerArgs += @("--expected-device-tier", $DeviceTier) }
    if ($DryRun) { $runnerArgs += "--dry-run" }
    if ($VerifyOnly) { $runnerArgs += "--verify-only" }
    $code = 4
    & $python @runnerArgs
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
