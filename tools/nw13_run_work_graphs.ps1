# Start only after checking the original archive against independent GitHub CI evidence.
param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][string]$TrustRecord,
    [Parameter(Mandatory=$true)][string]$ExpectedCommit,
    [string]$AdapterLuid,
    [string]$OutputDirectory,
    [switch]$VerifyOnly
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$trustedPath = (Resolve-Path -LiteralPath $TrustRecord).Path
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$manifestPath = Join-Path $root "bundle-manifest.json"
$prefix = $root.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
foreach ($external in @($trustedPath, $archivePath)) {
    if ($external.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The separately obtained GitHub trust record and archive must be outside the bundle."
    }
}
$trusted = Get-Content -LiteralPath $trustedPath -Raw | ConvertFrom-Json
if ($trusted.format -cne "nw13.work-graph-ci-trust" -or $trusted.version -ne 1 -or
    $trusted.commit -cne $ExpectedCommit -or $trusted.commit -cnotmatch '^[0-9a-f]{40}$' -or
    $trusted.tree -cnotmatch '^[0-9a-f]{40}$' -or $trusted.archive_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
    $trusted.manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
    (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $trusted.archive_sha256 -or
    (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $trusted.manifest_sha256) {
    throw "CI commit, archive, or manifest verification failed."
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format -cne "nw13.work-graph-physical-bundle" -or $manifest.version -ne 1 -or
    $manifest.source.commit -cne $ExpectedCommit -or $manifest.source.tree -cne $trusted.tree) {
    throw "Bundle source does not match independently anchored CI source."
}
$verified = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
[void]$verified.Add("bundle-manifest.json")
foreach ($entry in $manifest.files.PSObject.Properties) {
    $relative = $entry.Name
    if ($relative -cnotmatch '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$' -or
        ($relative -split '/') -contains '..' -or ($relative -split '/') -contains '.' -or
        -not $verified.Add($relative)) { throw "Invalid or duplicate bundle member." }
    $file = Get-Item -LiteralPath (Join-Path $root $relative)
    if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $file.Length -ne $entry.Value.bytes -or
        (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value.sha256) {
        throw "Bundle member echoes different bytes: $relative"
    }
}
foreach ($item in Get-ChildItem -LiteralPath $root -Force -Recurse) {
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Bundle contains a reparse point." }
    $relative = $item.FullName.Substring($prefix.Length).Replace([char]92, [char]47)
    if (-not $item.PSIsContainer -and -not $verified.Contains($relative)) { throw "Unexpected file: $relative" }
}
foreach ($required in @("run-work-graphs.ps1","runtime/python-runtime.zip","tools/nw13_work_graph.py")) {
    if (-not $verified.Contains($required)) { throw "Missing launch dependency: $required" }
}
if (-not $VerifyOnly -and ($AdapterLuid -notmatch '^[0-9a-f]{8}:[0-9a-f]{8}$' -or -not $OutputDirectory)) {
    throw "Specify the physical -AdapterLuid and a new -OutputDirectory outside the bundle."
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("nodewerk-nw13-work-graphs-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    Expand-Archive -LiteralPath (Join-Path $root "runtime/python-runtime.zip") -DestinationPath $temporary
    $python = Join-Path $temporary "python.exe"
    $runner = Join-Path $root "tools/nw13_work_graph.py"
    $mode = if ($VerifyOnly) { "verify" } else { "physical" }
    $runtimeArgs = @($runner, $mode, "--bundle", $root, "--trust", $trustedPath, "--archive", $archivePath, "--revision", $ExpectedCommit)
    if (-not $VerifyOnly) { $runtimeArgs += @("--adapter-luid", $AdapterLuid, "--output", $OutputDirectory) }
    & $python -I -B -S @runtimeArgs
    $code = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force
}
exit $code
