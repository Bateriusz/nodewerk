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
square fallback remains.

## Windows real-player procedure

Run on a Windows 11 x64 host with the pinned MSVC/DXC toolchain from
`tools/nw01/bootstrap-windows.ps1` and
`tools/nw01/enter-build-env.ps1`.  Nothing below claims hardware evidence
unless a physical adapter actually executed the player.

1. Build the diagnostic player fixture twice so the generic validator and the
   frozen-specialized branch both accept an external `.nwp` package:

   ```powershell
   cmake -S . -B build/nw08a-generic -G Ninja `
     -DCMAKE_BUILD_TYPE=MinSizeRel -DNW_BUILD_PROFILE=release `
     -DNW_ENFORCE_PINNED_TOOLCHAIN=ON `
     -DNW05_FROZEN_EMBEDDED_SPECIALIZATION=OFF
   cmake --build build/nw08a-generic --target nw_package_player_fixture --parallel 2

   cmake -S . -B build/nw08a-frozen -G Ninja `
     -DCMAKE_BUILD_TYPE=MinSizeRel -DNW_BUILD_PROFILE=release `
     -DNW_ENFORCE_PINNED_TOOLCHAIN=ON `
     -DNW05_FROZEN_EMBEDDED_SPECIALIZATION=ON
   cmake --build build/nw08a-frozen --target nw_package_player_fixture --parallel 2
   ```

2. Export a release package for each case.  The graph settings are the
   existing `nw07_output_width` / `nw07_output_height` keys, so no new graph
   or package model is introduced:

   ```powershell
   $template = "build/nw07-release/staging/procedural_image_player/nw_procedural_image_player.exe"
   $dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
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

3. Run the same package through both frame-plan variants at several window
   sizes.  `--headless` keeps the run deterministic; the geometry plan is
   built before presentation, so the printed source geometry is the value the
   display pass consumes:

   ```powershell
   $cases = @("landscape", "portrait", "canonical_hd", "square")
   $windows = @(@(320,180), @(640,360), @(1280,720))
   foreach ($mode in @("generic", "frozen")) {
     $player = (Get-ChildItem "build/nw08a-$mode" -Recurse `
       -Filter nw_package_player_fixture.exe | Select-Object -First 1).FullName
     foreach ($case in $cases) {
       $package = "build/nw08a-cases/$case/$case.nwp"
       foreach ($size in $windows) {
         $out = & $player --package $package --headless `
           --frames 1 --tick-start 0 --tick-step 1 `
           --width $size[0] --height $size[1]
         if ($LASTEXITCODE -ne 0) { throw "$mode/$case/$($size -join 'x') failed" }
         $out | Select-String "NW_PLAYER_PRESENT_SOURCE_(WIDTH|HEIGHT|EXPLICIT)"
       }
     }
   }
   ```

   Expected `NW_PLAYER_PRESENT_SOURCE_WIDTH/HEIGHT` are the authored case
   dimensions for every window size, and `NW_PLAYER_PRESENT_SOURCE_EXPLICIT`
   must be `1`.  A 256x144 source in a 1280x720 window therefore exercises
   rectangular scaling rather than square inference.

4. Confirm the canonical NW-07 release path still satisfies its hard gates and
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

## Recorded results

This worktree was prepared on a Linux host.  No Windows or physical GPU
execution was available, so the real-player rows are deliberately
**unexecuted**, not passing evidence:

| Check | Environment | Result |
| :--- | :--- | :--- |
| 256x144 / 144x256 / 1920x1080 / 256x256 package parameters | Linux, test payloads | passed (pytest) |
| Deterministic repeated export | Linux, test payloads | passed (pytest) |
| Generic frame-plan geometry | Linux source contract | passed (shared helper) |
| Frozen frame-plan geometry | Linux source contract | passed (shared helper) |
| Windows generic player, 4 cases x 3 window sizes | No Windows host | unexecuted |
| Windows frozen player, 4 cases x 3 window sizes | No Windows host | unexecuted |
| Windows canonical NW-07 BuildChecks (two clean dirs, 262144-byte gate) | No Windows host | unexecuted |

Attach the `NW_PLAYER_PRESENT_SOURCE_*` output, adapter/device/driver lines,
and the NW-07 BuildChecks JSON to the validation record before treating any
Windows row as passed.
