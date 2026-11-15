# NW-07-test-demo — Asteroid Tunnel

> **Branch-only experiment — DO NOT MERGE INTO `main`.**
>
> This production is a visual showcase built on the post-NW-07 graph/package/player
> path.  It is intentionally isolated from the canonical NW-07 proof.

## Concept

**Asteroid Tunnel** is a 24-second seamless 1920x1080 loop inspired by the feel
of classic Amiga bonus-tunnel sequences: fast forward motion, dark red/orange
industrial ribs, bright segmented panels, strong bloom, and asteroid-like
hazards crossing the flight path.

The scene keeps the NW-07 operator vocabulary and normal `.nwgraph` authoring
surface, but the branch-only export helper installs three **demo-specific shader
specializations** during this export only:

- `nw07_test_demo_instances_cs.hlsl` — divides 1,536 instances into repeating
  tunnel ribs and deterministic asteroid/debris hazards;
- `nw07_test_demo_scene_vs.hlsl` — converts the absolute-tick loop into true
  forward travel through a 96-unit repeating tunnel segment, with periodic
  weave and bank;
- `nw07_test_demo_scene_ps.hlsl` — gives the tunnel hot red/orange segmented
  lighting and the obstacles warm faceted rock shading.

The normal NW-06/NW-07 shader files are not modified or replaced.  The helper
temporarily swaps the three fixed shader records only while exporting this demo
and restores the canonical tuple in a `finally` block.

## Scene structure

The graph uses:

- 1,536 deterministic instances;
- a 4.6-unit tunnel radius;
- 1,024 thin repeating tunnel rings;
- 512 much smaller asteroid-like hazards/debris pieces;
- a low-poly near-closed 24x8 torus as the legal NW-07 source topology;
- five-octave seeded value noise;
- a warm directional light;
- deterministic asteroid rotation driven by the existing twist parameter;
- half-resolution bloom;
- ACES tonemapping;
- a 24-second / 1,152,000-tick stateless loop.

The source torus is deliberately almost closed (`major_radius=0.75`,
`minor_radius=0.68`). Tunnel rings are reconstructed analytically from the
mesh UVs with a slightly thicker 0.0095 local tube cross-section. At the 4.6
world scale this nearly fills the 96/1024 ring spacing without fully collapsing
adjacent rings into one sheet, giving the tunnel more visual mass while keeping
the repeating torus rhythm readable. Hazards keep the chunky
low-poly topology and receive seeded multi-frequency deformation and rotation.

The hazard scale is limited to 0.06–0.22. About 92% of hazards are biased into
the outer 56–88% of the tunnel radius; only 8% enter a narrower central band.
This keeps a readable flight corridor while still allowing occasional hazards
to cross the player's line of travel.

## Loop model

The static generated content occupies one 96-unit segment.  The specialized
vertex shader advances the camera phase by one complete segment over exactly
1,152,000 ticks, then wraps object depth around the camera.

At tick 1,152,000 the shader evaluates the same wrapped phase as tick 0.  Camera
weave, banking, obstacle rotation and segment placement are all functions of
the same absolute tick, so no frame-delta state is required.

## Files

- `fixtures/nw07-test-demo/orbital_vortex.nwgraph` — production graph; the
  historical path is retained even though the visual concept is now Asteroid
  Tunnel.
- `shaders/nw07_test_demo/nw07_test_demo_instances_cs.hlsl`
- `shaders/nw07_test_demo/nw07_test_demo_scene_vs.hlsl`
- `shaders/nw07_test_demo/nw07_test_demo_scene_ps.hlsl`
- `tools/nw07_test_demo.py` — validation and export wrapper.
- `tests/test_nw07_test_demo.py` — isolation and export regression coverage.

The canonical `fixtures/nw07/production_loop.nwgraph` and
`shaders/nw06/*` remain untouched.

## Validate

From the repository root:

```powershell
python .\tools\nw07_test_demo.py --validate-only
```

Contracts-only export:

```powershell
python .\tools\nw07_test_demo.py --contracts-only --output .\build\nw07-test-demo\contracts --report .\build\nw07-test-demo\contracts-report.json
```

Tests:

```powershell
python -m pytest -q .\tests\test_nw07_test_demo.py
```

## Local Windows release export

Enter the pinned build environment and build the existing release player
template first:

```powershell
pwsh .\tools\nw01\enter-build-env.ps1 -InteractiveShell
```

Inside that shell:

```powershell
cmake --preset nw07-release -DNW_ENFORCE_PINNED_TOOLCHAIN=ON; cmake --build --preset nw07-release --target nw_procedural_image_player --parallel 2
```

Then export the demo:

```powershell
python .\tools\nw07_test_demo.py --output .\build\nw07-test-demo\artifact --report .\build\nw07-test-demo\export-report.json --player-template .\build\nw07-release\staging\procedural_image_player\nw_procedural_image_player.exe --dxc C:\Tools\dxc-1.8.2407\bin\x64\dxc.exe --build-root .\build\nw07-test-demo\specialized-build
```

Run:

```powershell
.\build\nw07-test-demo\artifact\nw_common_d3d12_fl12_0_sm6_0_v1.exe
```

## Scope

This is deliberately not a general nodewerk feature.  It does not add a new
operator, graph format, package format, player, or mergeable runtime API.
Instead, it demonstrates a branch-local production specialization needed
because the current NW-07 camera and instance generator cannot express a true
forward tunnel flight by graph parameters alone.

If this visual direction later becomes a supported nodewerk authoring feature,
the correct follow-up is a separate architectural task with explicit operator
contracts — not merging these demo shaders into `main`.

## Branch policy

Keep this implementation on `experiment/NW-07-test-demo`.  The pull request is
only a review/CI surface.  **Do not merge it into `main`.**
