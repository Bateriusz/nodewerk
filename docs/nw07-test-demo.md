# Asteroid Tunnel showcase

Asteroid Tunnel is a 24-second seamless 1920x1080 Nodewerk showcase carried
forward from PR #30 and included in the downloadable PR #34 CI bundle.

It remains isolated from the canonical NW-07 proof: the normal NW-06/NW-07
shader files are not replaced.  The export helper temporarily installs three
showcase-specific shader specializations and restores the canonical shader table
after export.

## Visual design

The scene keeps the established NW-07 graph/package/player path:

- 1,024 deterministic instances;
- a 4.6-unit, 96-unit-long repeating tunnel;
- low-poly asteroid/debris hazards;
- five-octave seeded value noise;
- warm directional lighting;
- half-resolution bloom;
- ACES tonemapping;
- a 24-second / 1,152,000-tick stateless loop.

NW-09A extends the original PR #30 visual with two bounded temporal layers while
preserving the existing runtime and package contracts.

At the canonical 1,024-instance budget the showcase uses approximately:

- 384 structural tunnel ribs;
- 384 ordinary asteroid hazards;
- 128 seeded dust/streak particle instances;
- 128 delayed temporal-echo asteroid instances.

The particle positions advance only on 400-timeline-tick boundaries, matching
NW-09's 120 Hz fixed-step cadence at 48,000 timeline ticks per second.  Particle
velocity and drift are deterministic functions of the explicit instance seed.

Temporal echoes duplicate deterministic asteroid records and render the same
hazard several fixed simulation steps in the past.  Stable screen-space
dithering reduces their coverage and the existing bloom pass integrates the
surviving hot pixels into an afterimage-like trail.

These two layers deliberately do **not** introduce a second History
implementation.  The real NW-09 History scheduler, ping-pong state, checkpoint,
and feedback-image contracts continue to be exercised by the canonical
`particles_seeded.nwgraph` and `feedback_rect.nwgraph` examples in the same
CI artifact.  Asteroid Tunnel only visualizes the same fixed-step/seeded/
feedback ideas inside its showcase-only shader specialization.

## Files

- `fixtures/nw07-test-demo/orbital_vortex.nwgraph`
- `shaders/nw07_test_demo/nw07_test_demo_instances_cs.hlsl`
- `shaders/nw07_test_demo/nw07_test_demo_scene_vs.hlsl`
- `shaders/nw07_test_demo/nw07_test_demo_scene_ps.hlsl`
- `tools/nw07_test_demo.py`
- `tests/test_nw07_test_demo.py`

The canonical `fixtures/nw07/production_loop.nwgraph` and
`shaders/nw06/*` remain untouched.

## CI artifact

PR #34 builds and smoke-tests the exact executable that is uploaded.  The
combined GitHub Actions artifact contains:

```text
nw09-examples-<commit>/
  particles/
    nw09-particles.exe
  feedback/
    nw09-feedback.exe
  asteroid-tunnel/
    asteroid-tunnel.exe
  SHA256SUMS.txt
```

The SHA-256 manifest covers all three runnable executables.  Publication occurs
only after all three exports and smoke tests succeed.

## Local validation

From the repository root:

```powershell
python .\tools\nw07_test_demo.py --validate-only
python -m pytest -q .\tests\test_nw07_test_demo.py
```

A local Windows release export still uses the existing generated-player path:

```powershell
python .\tools\nw07_test_demo.py `
  --output .\build\nw07-test-demo\artifact `
  --report .\build\nw07-test-demo\export-report.json `
  --player-template .\build\nw07-release\staging\procedural_image_player\nw_procedural_image_player.exe `
  --dxc C:\Tools\dxc-1.8.2407\bin\x64\dxc.exe `
  --build-root .\build\nw07-test-demo\specialized-build
```

## Scope boundary

This showcase does not add a new operator, graph format, package format, player,
or History ABI.  The special shaders exist only to express the forward tunnel,
seeded dust, and temporal-echo visual direction without expanding canonical
Nodewerk semantics.
