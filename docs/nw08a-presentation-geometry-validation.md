# NW-08A Presentation Geometry Correction and Windows Validation

## Defect

NW-06 lowered its render targets from the resolved presentation dimensions but
did not emit the explicit `nw05.present.width` / `nw05.present.height`
initialization parameters that the NW-05 exporter already emitted.  The
PackagePlayer therefore fell back to pixel-count inference.  That inference is
ambiguous for rectangles:

| Requested output | Pixel count | Inferred side |
| :--- | ---: | ---: |
| 256x144 | 36,864 | 192x192 |
| 144x256 | 36,864 | 192x192 |
| 1920x1080 | 2,073,600 | 1440x1440 |
| 256x256 | 65,536 | 256x256 |

`compile_profile_package` now appends exactly one `uint`,
`initialization`-phase parameter per axis, using the existing NW-05 names and
the resolved render dimensions.  Both PackagePlayer frame-plan variants (the
generic validator and the `NW05_FROZEN_EMBEDDED_SPECIALIZATION` release path)
resolve geometry through the same `SelectPresentSourceGeometry` helper, which
prefers those parameters and keeps the square pixel-count fallback only for
older packages that lack them.

## Automated contract checks (no GPU required)

```bash
python -m pytest -q tests/test_nw08a_presentation_cases.py
python -m pytest -q tests/test_nw06_export.py \
  -k "presentation or geometry or deterministic"
python -m pytest -q tests/test_nw08_async_runtime.py \
  -k "capture_dimensions"
python -m pytest -q tests/test_nw05_export.py \
  -k "specialized_player_build"
python tools/verify_nw07.py --mode ContractsOnly
```

The NW-06 tests export and decode 256x144, 144x256, 1920x1080, and 256x256
packages and assert the exact parameter values, uniqueness, phase, type, and
presentation-buffer byte count.  They also export the same case twice and
compare package bytes for determinism.  The player-source regression asserts
that both frame-plan variants call the shared geometry helper and that the
square fallback remains.  The NW-08A regressions additionally assert that the
frozen fixture links `nw05_player_bootstrap` (the implementation generated
players use), that the fixture links the fully validating external-package
decoder rather than the frozen canonical-HD decoder, that the fixture reports
its compiled mode from PackagePlayer, and that the Windows workflow configures
both fixtures with the pinned MSVC toolchain.

## Windows real-player procedure

Run on a Windows 11 x64 host with the pinned MSVC/DXC toolchain from
`tools/nw01/bootstrap-windows.ps1` and
`tools/nw01/enter-build-env.ps1`.  Nothing below claims hardware evidence
unless a physical adapter actually executed the player.

1. Build the canonical NW-07 release player template.  This prerequisite
   creates `build/nw07-release` and must run before every step that consumes
   `$template`:

   ```powershell
   cmake --preset nw07-release -DNW_ENFORCE_PINNED_TOOLCHAIN=ON
   if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
   cmake --build --preset nw07-release --target nw_procedural_image_player --parallel 2
   if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

   $template = Join-Path $PWD "build/nw07-release/staging/procedural_image_player/nw_procedural_image_player.exe"
   if (-not (Test-Path $template -PathType Leaf)) {
     throw "NW-07 release player template was not built: $template"
   }
   $dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
   ```

   Keep `$template` and `$dxc` in the same PowerShell session for the steps
   below.  The CI job performs the same build before it runs any player or
   exporter.

2. Build the diagnostic player fixture twice with the same pinned MSVC
   toolchain and release cache settings as the canonical preset.  The frozen
   configuration builds `nw05_player_bootstrap`, the implementation used by
   generated players, and links the fixture against it.  The fixture always
   links `nw_package_player_diagnostic`, a fully validating decoder compiled
   without `NW05_FROZEN_EMBEDDED_SPECIALIZATION`.  The frozen release decoder
   substitutes canonical-HD CAP limits and is valid only for the exact-SHA
   generated artifact, so using it for the diagnostic fixture would reject
   smaller landscape/portrait/square packages during package load.  Generated
   release targets continue to link `nw_package_player`; only the diagnostic
   fixture uses the full decoder.

   ```powershell
   $toolchain = Join-Path $PWD "cmake/toolchains/msvc-x64.cmake"

   cmake -S . -B build/nw08a-generic -G Ninja `
     "-DCMAKE_TOOLCHAIN_FILE=$toolchain" `
     -DCMAKE_BUILD_TYPE=MinSizeRel -DNW_BUILD_PROFILE=release `
     -DNW_ENFORCE_PINNED_TOOLCHAIN=ON `
     -DNW_ENABLE_CRT_ELIMINATION_EXPERIMENT=OFF -DNW_USE_INBOX_UCRT=ON `
     -DNW07_CAPTURE_CONTROLS=OFF `
     -DNW05_FROZEN_EMBEDDED_SPECIALIZATION=OFF
   cmake --build build/nw08a-generic --target nw_package_player_fixture --parallel 2

   cmake -S . -B build/nw08a-frozen -G Ninja `
     "-DCMAKE_TOOLCHAIN_FILE=$toolchain" `
     -DCMAKE_BUILD_TYPE=MinSizeRel -DNW_BUILD_PROFILE=release `
     -DNW_ENFORCE_PINNED_TOOLCHAIN=ON `
     -DNW_ENABLE_CRT_ELIMINATION_EXPERIMENT=OFF -DNW_USE_INBOX_UCRT=ON `
     -DNW07_CAPTURE_CONTROLS=OFF `
     -DNW05_FROZEN_EMBEDDED_SPECIALIZATION=ON
   cmake --build build/nw08a-frozen --target nw_package_player_fixture --parallel 2
   ```

   Do not omit `CMAKE_TOOLCHAIN_FILE`.  Without it CMake chooses the default
   compiler on the bootstrapped runner (observed as `clang++.exe`), which links
   dynamic MSVC runtime dependencies and fails the release dependency check.

   The fixture prints `NW_PLAYER_FROZEN_SPECIALIZATION=0` or `=1` from
   `PackagePlayer::compiled_with_frozen_specialization()`.  That value comes
   from the linked player object, not from the fixture `main`, so it proves
   which frame-plan implementation actually runs.  Require `0` for the generic
   build and `1` for the frozen build before accepting any geometry result.

3. Export a release package for each case.  The graph settings are the
   existing `nw07_output_width` / `nw07_output_height` keys, so no new graph
   or package model is introduced:

   ```powershell
   python tools/nw08a_presentation_cases.py `
     --work build/nw08a-cases `
     --dxc $dxc `
     --player-template $template `
     --report build/nw08a-cases/report.json
   ```

   The tool writes one graph and package per case.  Each package must decode
   with exactly one `nw05.present.width` and one `nw05.present.height`
   initialization parameter; the report records the package digest, size, and
   retained executable for each case.  Omitting `--dxc` and
   `--player-template` runs the same geometry checks against the shared test
   payload path on a GPU-less host.

4. Collect planner/headless evidence.  This is geometry and frame-plan
   evidence only, not display evidence.  `--planner-self-test` builds the exact
   frame plan without creating a window or swapchain.  `--headless` executes
   package work but, with no swapchain, `frame.present` exits before dispatching
   the display shader:

   ```powershell
   $cases = @("landscape", "portrait", "canonical_hd", "square")
   $windows = @(@(320,180), @(640,360), @(1280,720))
   $expectedModes = @{ generic = 0; frozen = 1 }
   foreach ($mode in @("generic", "frozen")) {
     $player = Join-Path $PWD `
       "build/nw08a-$mode/staging/package_player/nw_package_player_fixture.exe"
     foreach ($case in $cases) {
       $package = "build/nw08a-cases/$case/$case.nwp"
       foreach ($size in $windows) {
         $out = & $player --package $package --planner-self-test `
           --frames 1 --tick-start 0 --tick-step 1 `
           --width $size[0] --height $size[1] 2>&1
         if ($LASTEXITCODE -ne 0) { throw "$mode/$case/$($size -join 'x') failed" }
         $modeLine = $out | Select-String "^NW_PLAYER_FROZEN_SPECIALIZATION=(\d+)$" |
           Select-Object -First 1
         if (-not $modeLine) {
           throw "$mode/$case/$($size -join 'x') missing compiled mode"
         }
         if ([int]($modeLine.Matches[0].Groups[1].Value) -ne $expectedModes[$mode]) {
           throw "$mode fixture linked the wrong player implementation"
         }
         $out | Select-String "NW_PLAYER_PRESENT_SOURCE_(WIDTH|HEIGHT|EXPLICIT)"

         $headless = & $player --package $package --headless `
           --frames 1 --tick-start 0 --tick-step 1 `
           --width $size[0] --height $size[1] 2>&1
         if ($LASTEXITCODE -ne 0) { throw "$mode/$case/$($size -join 'x') headless failed" }
         $headless | Select-String "NW_PLAYER_PRESENT_SOURCE_(WIDTH|HEIGHT|EXPLICIT)"
       }
     }
   }
   ```

   Expected `NW_PLAYER_PRESENT_SOURCE_WIDTH/HEIGHT` are the authored case
   dimensions for every window size, and `NW_PLAYER_PRESENT_SOURCE_EXPLICIT`
   must be `1`.  A 256x144 source in a 1280x720 window therefore exercises
   rectangular scaling rather than square inference.  Treat both
   `--planner-self-test` and `--headless` as **planner/rendering evidence only**;
   neither command proves that a display shader or swapchain Present executed.

   The Windows CI job runs the same fixture with `--planner-self-test` and
   fails unless every mode/case/window tuple reports `frozen=0` for generic,
   `frozen=1` for frozen, the authored geometry, and `EXPLICIT=1`.

5. Windowed hardware validation (display evidence).  Run this on a Windows 11
   x64 host with a physical adapter attached to a display, using the pinned
   bootstrap environment.  Do not pass `--headless`: that path creates no
   swapchain and returns before the display shader.  This procedure executes
   the display path, requires one successful Present per frame, and reads back
   the exact source resource passed to `frame.present`:

   ```powershell
   function Get-NwValue($lines, $name) {
     $line = $lines | Where-Object { $_ -match "^$name=(\d+)$" } |
       Select-Object -First 1
     if (-not $line) { throw "missing $name" }
     return [int]($line -replace "^$name=", "")
   }

   $cases = @("landscape", "portrait", "canonical_hd", "square")
   $windows = @(@(320,180), @(640,360), @(1280,720))
   $expectedModes = @{ generic = 0; frozen = 1 }
   $expected = @{
     landscape = @(256, 144)
     portrait = @(144, 256)
     canonical_hd = @(1920, 1080)
     square = @(256, 256)
   }
   $referenceHashes = @{}
   foreach ($mode in @("generic", "frozen")) {
     $player = Join-Path $PWD `
       "build/nw08a-$mode/staging/package_player/nw_package_player_fixture.exe"
     foreach ($case in $cases) {
       $package = "build/nw08a-cases/$case/$case.nwp"
       foreach ($size in $windows) {
         $label = "$mode/$case/$($size -join 'x')"
         $out = & $player --package $package --frames 3 `
           --tick-start 0 --tick-step 1 --width $size[0] --height $size[1] `
           --emit-image-hash 2>&1
         if ($LASTEXITCODE -ne 0) { throw "$label failed with $LASTEXITCODE" }
         $lines = @($out | ForEach-Object { "$_" })
         $compiledFrozen = Get-NwValue $lines "NW_PLAYER_FROZEN_SPECIALIZATION"
         $sourceWidth = Get-NwValue $lines "NW_PLAYER_PRESENT_SOURCE_WIDTH"
         $sourceHeight = Get-NwValue $lines "NW_PLAYER_PRESENT_SOURCE_HEIGHT"
         $explicit = Get-NwValue $lines "NW_PLAYER_PRESENT_SOURCE_EXPLICIT"
         $frames = Get-NwValue $lines "NW_PLAYER_FRAMES"
         $presents = Get-NwValue $lines "NW_PLAYER_PRESENTS"
         $imageBytes = Get-NwValue $lines "NW_PLAYER_IMAGE_BYTES"
         $nonzeroBytes = Get-NwValue $lines "NW_PLAYER_IMAGE_NONZERO_BYTES"
         $hashLine = $lines | Where-Object { $_ -match "^NW_PLAYER_IMAGE_FNV64=([0-9a-f]+)$" } |
           Select-Object -First 1
         if (-not $hashLine) { throw "$label missing NW_PLAYER_IMAGE_FNV64" }
         $imageHash = ($hashLine -replace "^NW_PLAYER_IMAGE_FNV64=", "")
         if ($compiledFrozen -ne $expectedModes[$mode] -or
             $sourceWidth -ne $expected[$case][0] -or
             $sourceHeight -ne $expected[$case][1] -or $explicit -ne 1) {
           throw "$label frozen=$compiledFrozen geometry ${sourceWidth}x${sourceHeight} explicit=$explicit"
         }
         if ($frames -ne 3 -or $presents -ne 3) {
           throw "$label display path did not present every frame (frames=$frames presents=$presents)"
         }
         if ($imageBytes -ne $expected[$case][0] * $expected[$case][1] * 16 -or
             $nonzeroBytes -eq 0) {
           throw "$label invalid presented image bytes=$imageBytes nonzero=$nonzeroBytes"
         }
         $key = "$case/$($size[0])x$($size[1])"
         if ($referenceHashes.ContainsKey($key)) {
           if ($referenceHashes[$key] -ne $imageHash) {
             throw "$label presented image hash disagrees with the other player mode"
           }
         } else {
           $referenceHashes[$key] = $imageHash
         }
       }
     }
   }
   ```

   `NW_PLAYER_PRESENTS=3` proves the windowed `frame.present` path reached
   `IDXGISwapChain::Present`; the nonzero readback and identical image hashes
   across generic and frozen modes show both implementations produced the same
   authored rectangular image.  Record the adapter/driver lines and the raw
   output with the results.  A machine without an attached display can only
   produce the step-4 evidence and must mark this section **unexecuted**.

6. Confirm the canonical NW-07 release path still satisfies its hard gates and
   two-clean-directory reproducibility:

   ```powershell
   python tools/verify_nw07.py --mode BuildChecks `
     --player-template $template --dxc $dxc `
     --native-output build/nw08a-nw07-verification `
     --summary-json build/nw08a-nw07-build-checks.json
   ```

   The check must report `matches_frozen_digest: true`, identical
   package/manifest/executable hashes from both clean build directories, and
   `complete_distribution_bytes <= 262144`.  The ceiling is unchanged.

7. Validate and record the BuildChecks evidence.  This turns the two clean
   export reports into a deterministic artifact that asserts hash equality and
   the 262,144-byte hard limit:

   ```powershell
   python tools/nw08a_windows_build_checks.py `
     --summary build/nw08a-nw07-build-checks.json `
     --verification-root build/nw08a-nw07-verification `
     --output build/nw08a-windows-build-checks-record.json
   ```

   The Windows workflow `.github/workflows/nw08a-presentation-geometry.yml`
   runs steps 1-4, 6, and 7 on `windows-latest` in the job
   `windows-canonical-build-checks` and uploads the raw BuildChecks JSON plus
   this record as the `nw08a-windows-build-checks` artifact.  The hosted runner
   has no physical display, so step 5 is deliberately not part of that job.
   The job is the repeatable record for the canonical two-directory check;
   this Linux worktree did not execute it.

## Recorded results

This worktree was prepared on a Linux host.  No Windows or physical GPU
execution was available, so the real-player rows are deliberately
**unexecuted**, not passing evidence:

| Check | Environment | Result |
| :--- | :--- | :--- |
| 256x144 / 144x256 / 1920x1080 / 256x256 package parameters | Linux, test payloads | passed (pytest) |
| Deterministic repeated export | Linux, test payloads | passed (pytest) |
| Generic frame-plan geometry | Linux source contract and fixture-link contract | passed (shared helper; fixture links generic bootstrap) |
| Frozen frame-plan geometry | Linux source contract and fixture-link contract | passed (shared helper; frozen fixture links `nw05_player_bootstrap`) |
| Frozen external-package loading for 256x144 / 144x256 / 1920x1080 / 256x256 | Linux source and CMake link contract | passed (fixture links `nw_package_player_diagnostic`, not the frozen decoder); native execution unexecuted |
| Generic/frozen compiled-mode assertion | Linux source contract | passed (fixture reports `compiled_with_frozen_specialization()`) |
| Subprocess non-ASCII / invalid UTF-8 decoding (0x88) | Linux, mocked CMake output | passed (pytest) |
| Windows generic planner, 4 cases x 3 window sizes | No Windows host | unexecuted |
| Windows frozen planner, 4 cases x 3 window sizes | No Windows host | unexecuted |
| Windowed hardware display, generic and frozen, 4 cases x 3 window sizes | No Windows host or physical display | unexecuted |
| Windows canonical NW-07 BuildChecks (two clean dirs, 262144-byte gate) | Workflow target: GitHub Actions `windows-latest`, job `windows-canonical-build-checks` in `nw08a-presentation-geometry.yml` | unexecuted in this Linux worktree; the workflow uploads `nw08a-nw07-build-checks.json` and `nw08a-windows-build-checks-record.json` as the `nw08a-windows-build-checks` artifact |

Attach the `NW_PLAYER_FROZEN_SPECIALIZATION` and
`NW_PLAYER_PRESENT_SOURCE_*` output, the windowed `NW_PLAYER_PRESENTS` /
`NW_PLAYER_IMAGE_*` results, adapter/device/driver lines, and the NW-07
BuildChecks JSON/record to the validation record before treating any Windows
row as passed.
