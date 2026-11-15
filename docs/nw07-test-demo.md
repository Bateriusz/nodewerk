# NW-07-test-demo — Orbital Vortex

> **Branch-only experiment — DO NOT MERGE INTO `main`.**
>
> This production exists to probe the expressive range of nodewerk exactly as
> it stood after NW-07.  It is not a prerequisite for NW-08 or later work and
> must not change the authoritative NW-07 proof.

## Concept

**Orbital Vortex** is a 24-second seamless 1920x1080 loop built only from the
operator/runtime path already available in NW-07.

The scene uses 4,096 very thin procedural torus instances distributed by the
existing seeded radial instance generator.  The current camera operator cannot
author an arbitrary translation path: it always looks at the origin while
moving on an orbit.  The demo therefore puts the camera **inside** the radial
field at radius 5.2 while the field extends to radius 7.5.  Nearby rings pass
close to the camera during the orbit, producing an abstract orbital fly-through
without adding a camera, placement, or rendering feature.

The visual treatment intentionally pushes the existing stack:

- 32x8 low-poly thin torus geometry;
- 4,096 deterministic instances;
- five-octave seeded value noise;
- cool blue material with low roughness and strong specular response;
- saturated directional light bright enough to feed the existing bloom stage;
- a 720-degree maximum analytic twist;
- half-resolution bloom;
- ACES tonemapping;
- one exact camera revolution over 1,152,000 ticks at 48 kHz.

The first and last camera keys are 0 and 2*pi, and the established twist
function is sinusoidal over the same loop duration.  Playback therefore returns
to the same stateless scene state at the boundary.

## Files

- `fixtures/nw07-test-demo/orbital_vortex.nwgraph` — the production graph.
- `tools/nw07_test_demo.py` — validation and export wrapper around the existing
  NW-03/NW-05/NW-06 path.
- `tests/test_nw07_test_demo.py` — branch-local regression coverage.

The canonical `fixtures/nw07/production_loop.nwgraph` remains untouched.

## Validate without a GPU or DXC

From the repository root:

```bash
python tools/nw07_test_demo.py --validate-only
python tools/nw07_test_demo.py \
  --contracts-only \
  --output build/nw07-test-demo/contracts \
  --report build/nw07-test-demo/contracts-report.json

python -m pytest -q tests/test_nw07_test_demo.py
```

The contracts-only export uses the existing structural test shader payload
path.  It is useful for proving graph lowering, package structure, scheduling,
resource accounting, deterministic timeline parameters, and the one-draw +
bloom + tonemap + present frame plan.  It is not visual/hardware evidence.

## Export the standalone Windows executable

Use the same pinned DXC and specialized player template already used by the
post-NW-07 export path:

```powershell
python tools/nw07_test_demo.py `
  --output build\nw07-test-demo\artifact `
  --report build\nw07-test-demo\export-report.json `
  --player-template build\nw07-release\staging\procedural_image_player\nw_procedural_image_player.exe `
  --dxc C:\Tools\dxc-1.8.2407\bin\x64\dxc.exe `
  --build-root build\nw07-test-demo\specialized-build
```

The helper uses the existing `nw05_export.export_graph` path, the existing
NW-06 procedural lowering and shaders, the common capability profile, an
uncapped present interval, and frozen embedded specialization.  No demo-specific
runtime is generated.

The experiment uses a generous 64 MiB export-accounting guard because this task
is an artistic capability probe, not a new size-certification milestone.  It
retains the existing 512 MiB resource guard.  NW-07's canonical 256 KiB proof
and its evidence contract are not modified or redefined here.

## Fixed-tick visual checks

After producing the release executable, inspect at least these absolute ticks:

- `0`
- `288000` — quarter loop
- `576000` — half loop
- `864000` — three-quarter loop
- `1151999` — last tick before wrap
- `1152000` — loop boundary, equivalent to tick 0

The established release player supports deterministic `--tick
<absolute-tick>` playback.  Repeated runs at the same tick should produce the
same scene state.  Tick 1,152,000 wraps to the same camera/deformation state as
tick 0.

## Deliberate limitations

This branch does **not** add:

- arbitrary camera translation or spline position paths;
- a tunnel-specific instance generator;
- additional geometry primitives;
- particles, SDF/CSG, feedback, temporal simulation, or audio;
- an emissive material model;
- another post-processing stage;
- another package format or player.

If the production needs one of those capabilities to become better art, that is
evidence for future nodewerk work — not justification to expand this
experiment's scope.

## Branch policy

Keep this implementation on `experiment/NW-07-test-demo`.  A pull request may
exist as a review/CI surface, but it is intentionally non-mergeable work:
**do not merge it into `main`**.
