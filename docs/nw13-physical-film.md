# NW-13 physical film handoff

The Windows job publishes two separate archives, each bound to its own
authenticated Actions step summary:

- `nw13-film-inspection-physical-<measured SHA>`: the real 320×180 native
  inspection graph, not an upscale of the regression raster. CI builds and
  binds common, independently built fallback, and the compile-target-only
  DXR arm but does not render this profile. Only the physical capability
  probe can admit execution of the exact DXR arm. The run may compare other
  arms with the physical common image; **no hosted oracle reference** is
  claimed for this profile.
- `nw13-film-physical-<measured SHA>`: the unchanged tiny hosted-test graph,
  independently measured common/fallback and DXR when the hosted capabilities
  support it, and hosted GPU fixed-tick readbacks checked against the CPU
  oracle. Choose the first archive for higher-detail visual inspection.

Both deliver the players, packages and link manifests, exact source
snapshots, shared capability/host/telemetry tools, a portable Windows Python
runtime, and the pinned, optional PresentMon collector. Neither runs the
physical test in hosted CI. No DXC, SDK, checkout, Python installation or
rebuild is needed on the validation PC.

## Verify before running anything

1. Open the authenticated GitHub Actions run for the intended PR revision. For
   a PR merge-ref run, use the **measured commit and tree**, not the PR head
   unless the CI checkout actually measured it. Download the film artifact
   from that run. Copy the exact **inner archive SHA-256**, **inventory
   SHA-256**, **measured revision**, and **measured tree** from the run's
   **step summary**, not solely from the downloaded receipt. The receipt is
   a convenience copy, not an independent trust anchor.
2. Verify the inner archive using installed Windows PowerShell before
   extracting it or running anything inside. On a Windows 11 physical
   desktop, extract into a new directory. Use the existing desktop session;
   no reboot or cold-start certification.
3. Obtain the selected adapter LUID from a capability query of the verified
   bundled probe. It uses the normal shared Windows capability query and
   never decides the GPU from a model name. If the default is not the device
   you intend to use, add `--adapter-index <n>` to the query and copy the
   returned LUID. Keep all output outside the extracted bundle.

~~~powershell
$zip = 'C:\nw13\download\inspection-physical.zip'  # or film-physical.zip, with that archive's own anchors
$archiveSha = '<SHA-256 from the authenticated CI step summary>'
$inventorySha = '<inventory SHA-256 from the same summary>'
$revision = '<measured commit from the same summary>'
$tree = '<measured tree from the same summary>'
$bundle = 'C:\nw13\verified-film'
$out = 'C:\nw13\physical-film-evidence'
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $archiveSha) {
    throw 'Archive identity differs from the trusted run'
}
if (Test-Path -LiteralPath $bundle) { throw 'Use a fresh extraction directory' }
Expand-Archive -LiteralPath $zip -DestinationPath $bundle
if ((Get-FileHash -LiteralPath "$bundle\film-physical.json" -Algorithm SHA256).Hash -ne $inventorySha) {
    throw 'Extracted inventory differs from the trusted run'
}
$inventory = Get-Content -LiteralPath "$bundle\film-physical.json" -Raw | ConvertFrom-Json
if ($inventory.revision -ne $revision -or $inventory.tree -ne $tree) { throw 'Source identity differs' }
$probe = "$bundle\bin\nw11_capability_probe.exe"
if ((Get-FileHash -LiteralPath $probe -Algorithm SHA256).Hash -ne $inventory.files.'bin/nw11_capability_probe.exe') {
    throw 'Capability probe identity differs'
}
$queried = & $probe | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $queried.status -ne 'ok') { throw 'Capability query failed' }
$adapter = $queried.adapter.adapter_luid
if ((Get-FileHash -LiteralPath "$bundle\run-film.ps1" -Algorithm SHA256).Hash -ne $inventory.files.'run-film.ps1') {
    throw 'Launcher identity differs'
}
powershell -NoProfile -ExecutionPolicy Bypass -File "$bundle\run-film.ps1" -Revision $revision -Tree $tree -ExpectedInventorySha256 $inventorySha -AdapterLuid $adapter -Out $out
~~~

Keep the extracted bundle unchanged. The launcher checks the external CI
inventory digest, commit/tree, file membership and file hashes; it verifies
the embedded PE package/manifest and NWAS identity and rechecks the actual
players, readbacks and probe before use. Output must be a new directory
outside the extraction. PowerShell policy is process-scoped here.

## What the run proves

- Runs a short, visible, maximized playback for each delivered/eligible arm
  on the **same queried hardware adapter**, without allowing software
  adapters. It records native successful Present counters, startup, engine
  committed allocations and DXR AS allocation when emitted.
- Runs fixed ticks from the **same source and native raster** in all arms,
  records actual native Present and RGBA32F readback, requires opaque finite
  pixels, compares fallback/DXR with the independently captured physical
  common frame, and additionally checks the bound hosted reference **only**
  for the tiny regression profile. For the inspection profile the common
  capture is an observation, and relative agreement is not absolute image
  correctness. The
  GPU resource readback is **not** a desktop pixel capture or a simulated
  resize. The actual client extent and desktop pixel capture are reported
  unavailable when the producer does not emit them.
- Add `-CollectTiming` for one longer visible run per arm. The pinned
  PresentMon 2.6.0 collector is restricted to the player PID. Timing requires
  an absolute QPC-millisecond column, the launch window, the native sync
  interval, one swap chain and enough usable samples. The default collector
  supports the combined CPU-submit metric; explicit `--v2_metrics` output
  lacks Present API time, so only the reported GPU/display metrics can be
  analyzed in that mode. The raw CSV, launch
  record, collector metadata and private stdout/stderr are preserved.
  `etw_access_denied` means the collector explicitly reported trace-session
  access denial. Keep the functional result. If timing is needed, explicitly
  open a Windows PowerShell window as administrator for that collection only,
  or use an account that already has Performance Log Users access. The
  runner never elevates itself or changes group membership. Other collector,
  schema, clock and capture problems are reported separately. ETW, VRAM
  usage and performance adoption are not prerequisites for functional success.
- Physical host and operating data are gathered through the shared host and
  GPU tools, including virtualization/VBS; a rejected physical host never
  runs a film player. Unsupported/no-delivered optional arm, functional
  execution, image correctness, performance and adoption remain distinct.
  No distribution-size gate, fixed FPS prescription or automatic adoption.
- `summary.json` is the allowlisted, shareable result. The `private/` folder
  contains actual device and host observations, commands, process identities,
  native output and captures. Do not publish it or treat it as interchangeable
  with another bundle. This does not verify real audio/display cue alignment;
  use the [separate NWAS physical validation path](physical-audio-cues.md)
  for that measurement when the NWAS v2 exporter is present.


## Re-analyze a retained timing run without rerunning on RTX

Use the **new** verified Python tooling to read the **old** verified bundle
and its original private evidence. Re-check the old bundle against its own
CI inventory SHA, measured commit and tree. The command does not run
PresentMon or a player and writes only to stdout/stderr. Save its sanitized
JSON somewhere outside both inputs if desired. It cannot turn a different
revision, an unsuccessful collector or missing identity into a measurement.

The old collector did not record a CSV digest. Supply independently retained
SHA-256 values for the original summary, private capability snapshot, arm
launch record, collector metadata and CSV. If collecting those hashes now,
use the original copies you personally retained or obtained through a trusted
handoff. Hashing untrusted copies does not establish provenance. Historical
player stdout/stderr are also required and compared with the launch record.
Repeat for each arm with a successful collector.

~~~powershell
# Inputs: $newBundle (verified tooling), $oldBundle, $oldInventorySha,
# $oldRevision, $oldTree and $oldEvidence (from the same historical run).
$arm = 'common'
$visible = "$oldEvidence\private\runs\$arm\visible"
$summarySha = (Get-FileHash -Algorithm SHA256 -LiteralPath "$oldEvidence\summary.json").Hash.ToLowerInvariant()
$capabilitySha = (Get-FileHash -Algorithm SHA256 -LiteralPath "$oldEvidence\private\capability.json").Hash.ToLowerInvariant()
$launchSha = (Get-FileHash -Algorithm SHA256 -LiteralPath "$visible\launch.json").Hash.ToLowerInvariant()
$collectorSha = (Get-FileHash -Algorithm SHA256 -LiteralPath "$visible\presentmon-metadata.json").Hash.ToLowerInvariant()
$csvSha = (Get-FileHash -Algorithm SHA256 -LiteralPath "$visible\presentmon.csv").Hash.ToLowerInvariant()
& "$newBundle\python\python.exe" -I -B "$newBundle\tools\nw13_physical_film.py" reanalyze `
  --root $oldBundle --index-sha $oldInventorySha --revision $oldRevision --tree $oldTree `
  --evidence $oldEvidence --arm $arm --summary-sha $summarySha --capability-sha $capabilitySha `
  --launch-sha $launchSha --collector-sha $collectorSha --csv-sha $csvSha
~~~

A successful re-analysis is evidence for that historical artifact and device
only. If the old clock or process cannot be matched, timing remains
unavailable. This does not qualify the new build or change any adoption
decision.
