param(
    [Parameter(Mandatory)][string]$Revision,
    [Parameter(Mandatory)][string]$Tree,
    [Parameter(Mandatory)][string]$ExpectedInventorySha256,
    [Parameter(Mandatory)][string]$AdapterLuid,
    [Parameter(Mandatory)][string]$Out,
    [switch]$CollectTiming
)
$ErrorActionPreference = "Stop"
# Invoke only after the CI ZIP has been independently hash-verified before extraction.
$manifest = Join-Path $PSScriptRoot "film-physical.json"
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $manifest).Hash -ne $ExpectedInventorySha256) {
    throw "The extracted inventory does not match the hash copied from trusted CI."
}
$index = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
if ($index.format -ne "nw13.physical-film-bundle" -or $index.version -ne 1 -or
    $index.revision -cne $Revision -or $index.tree -cne $Tree) {
    throw "The film archive does not match the exact measured CI revision and tree."
}
if ((Get-Item -LiteralPath $PSScriptRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "The extracted bundle cannot be a junction or a symlink."
}
$expected = @{}
foreach ($entry in $index.files.PSObject.Properties) {
    if ($entry.Name -cnotmatch '^[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*$' -or
        $entry.Name -match '(^|/)\.{1,2}(/|$)' -or
        $entry.Value -cnotmatch '^[0-9a-f]{64}$' -or $expected.ContainsKey($entry.Name)) {
        throw "The authenticated inventory contains an invalid or aliased member."
    }
    $expected[$entry.Name] = $entry.Value
}
$remaining = $expected.Count
foreach ($item in Get-ChildItem -LiteralPath $PSScriptRoot -Recurse -Force) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The extracted bundle contains a reparse point."
    }
    if ($item.PSIsContainer -or $item.FullName -eq $manifest) { continue }
    $relative = $item.FullName.Substring($PSScriptRoot.Length + 1).Replace('\', '/')
    if (-not $expected.ContainsKey($relative) -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName).Hash -ne $expected[$relative]) {
        throw "An extracted file differs from the authenticated inventory."
    }
    $remaining--
}
if ($remaining -ne 0) { throw "The extracted bundle is missing authenticated files." }

# The portable runtime, source validator and its imports are verified with
# Windows' host hash tool before the bundled interpreter starts.
$command = @("-I", "-B", (Join-Path $PSScriptRoot "tools/nw13_physical_film.py"), "run",
    "--root", $PSScriptRoot, "--index-sha", $ExpectedInventorySha256, "--revision", $Revision,
    "--tree", $Tree, "--adapter-luid", $AdapterLuid, "--output", $Out)
if ($CollectTiming) { $command += "--collect-timing" }
& (Join-Path $PSScriptRoot "python/python.exe") @command
exit $LASTEXITCODE
