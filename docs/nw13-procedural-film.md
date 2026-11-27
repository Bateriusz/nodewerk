# NW-13 Editable Procedural Film

- **Status:** implemented authoring/compiler contract; physical GPU and audio
  observations remain pending/unavailable as recorded in the evidence files.
- **Actual implementation base:**
  `1c9e1559c8d43530c7b213461c6719b6aa43ae35` (Merge PR #45: NW-12 optional
  procedural audio). The base was created after the merged NW-11 work
  (`8ade0b7`, Merge PR #44) and NW-12. See
  [`benchmarks/nw13/base.v1.json`](../benchmarks/nw13/base.v1.json) for the
  machine-readable record.
- **Planning inputs:** PR #48's pinned catalog and roadmap are planning inputs
  only. Neither the historical planning SHA nor the catalog PR head is an
  implementation base.
- **Clean-room boundary:** no Farbrausch code or asset is copied.
  `references/farbrausch/fr_public` remains the untouched, unvendored
  submodule; only clean-room review metadata lives in
  `references/operator_catalog/`.

## 1. Provenance and inherited decisions

The versioned NW-11 decisions are consumed per workload and device:

| Workload | Device tier | NW-11 versioned decision | NW-13 use |
|---|---|---|---|
| Mesh shaders | desktop Ada representative | `pending` | defer; no promotion without a matched measured workload |
| Mesh shaders | desktop Blackwell representative | `pending` | defer |
| DXR inline ray queries | desktop Ada representative | `pending` | capability-qualified optional path; common fallback remains independent |
| DXR inline ray queries | desktop Blackwell representative | `pending` | capability-qualified optional path; common fallback remains independent |

Pending is not a pass. A capability may be sufficient for an optional profile
while the adoption decision remains deferred. Eligibility is decided only from
queried capabilities; representative device names are provenance metadata.

## 2. Pinned operator catalog

[`references/operator_catalog/`](../references/operator_catalog/) contains the
complete declaration export and regenerated inventories for the separately
scoped Werkkzeug3 and Werkkzeug4 engines. Regeneration is deterministic:

```sh
python tools/operator_catalog.py        # verify
python tools/operator_catalog.py --write  # regenerate inventories
```

The regeneration check preserves declaration identity, engine/variant, types,
ports, extractable parameters, source locations, hidden/obsolete status and
extraction limits. The `kkrieger` game variant remains identifiable and
separately scoped; it is never merged into the Werkkzeug4 inventory.
`semantic-coverage.v1.json` is the many-to-many map to
`implementation-candidates.v1.json`. Every reviewed use records production
need, controls, animation, downstream and temporal requirements, proposed
realization, backend, implementation state and one of
`preserve` / `consolidate` / `replace_implementation` / `partial` / `defer` /
`omit`. Unreviewed entries are retained, and the mesh-topology, selection and
UV gaps remain explicit.

## 3. Public geometry schema

[`schemas/nw13-geometry-graph.v1.json`](../schemas/nw13-geometry-graph.v1.json)
exposes bounded signed-distance operators:

| Family | Operators |
|---|---|
| primitives | `sphere`, `box`, `cylinder`, `capsule`, `torus` |
| rigid transforms | `translate`, `rotate`, `scale` |
| solid CSG | `union`, `intersection`, `subtract`, bounded `smooth_union` |
| finite structure | `repeat` (bounded lattice counts) |
| deformation | `twist`, `bend`, seeded `displace` |

Scene composition, mesh aggregation and solid CSG are distinct:

* `nw13.geometry` evaluates solids and conservative bounds;
* `nw.mesh.torus`/mesh recipes aggregate renderable vertices and indices;
* `nw13.scene` binds stable object identities, materials, lights, parent-child
  transforms, cameras and tracks.

`tools/nw13_geometry.py` is the reference evaluator. Bounds are conservative
axis-aligned boxes: a bound may be larger than the exact surface but never
excludes a reachable point. Admission rejects excessive nodes, recursion,
repetition, generated primitives and memory before evaluation. Reference-point,
CSG identity, clean-run seed and bounds tests live in
`tests/test_nw13_geometry.py`.

## 4. Shared compiler/player path

`tools/nw13_pipeline.py` reuses the NW-02 graph reader and NW-05 whole-graph
optimizer. It does not define a second graph model. The production graph
`fixtures/nw13/hotel_corridor.nwgraph` is backend-neutral and compiles through
the common profile:

| Item | Value |
|---|---|
| profile | `nw.common.d3d12.fl12_0.sm6_0.v1` |
| requirements digest | `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a` |
| API / feature level / shader model | D3D12 / 12_0 / 6_0 |
| fallback | the common artifact itself |

The pipeline tests cover canonical semantic serialization, versioning,
semantic hashing, profile selection, reachability, frequency/residency
partitioning, seed identity in the inherited graph tests, and dependency-local
cache invalidation. Static work stays in the initialization partition;
frame-dependent and History work is identified in every fixed-tick schedule.
An explicit migration handles the short-lived v0 production envelope.

Studio uses the existing authoring session, so a failed compile keeps the last
valid snapshot, diagnostics carry the affected node/port/operator identity and
only the changed node's transitive dependents are invalidated. Geometry bounds
and admission diagnostics are reported before any GPU work.

## 5. Scene, animation, cameras and transport

[`schemas/nw13-scene.v1.json`](../schemas/nw13-scene.v1.json) binds shared
geometry to stable object identities. `tools/nw13_scene.py` provides:

* rigid parent-child transform composition with hierarchy-cycle detection;
* typed position/rotation/scale tracks with linear, step and smoothstep
  interpolation;
* a hinged door and a prop that follows its animated parent;
* camera pose/target/projection, shot cuts and bounded transitions;
* camera motion independent of action time;
* diagnostics for cycles, invalid transforms, degenerate cameras, invalid
  clipping planes and exceeded bounds;
* fixed-tick transport with freeze, reverse, resume, cut notification and a
  declared one-shot cue policy that never duplicates a cue in one generation.

## 6. Bounded common and DXR reference paths

`tools/nw13_render.py` renders shadows and one-bounce reflections from the
current scene using the same geometry/scene documents. The common path is a
bounded CPU/analytic reference; the optional DXR path is capability-qualified
by `tools/nw13_evidence.py`. Tests verify:

* moving an occluder changes the expected shadow region;
* changing an off-camera reflected object's material changes the mirror
  reflection response;
* common and optional reference arms agree within declared image tolerance;
* ray length, samples, bounces and acceleration-structure budgets are bounded;
* startup/memory estimates are reported.

Surface response parameters are effective base color, roughness,
metallic/specular response, emission, local light falloff/range, reflection
roughness and miss radiance. Unsupported requests fail diagnostically before
GPU work.

The reference shading contract is:

| Input | Realization |
|---|---|
| effective base color | scene material RGB(A), multiplied by local light color |
| roughness | bounded `[0,1]`; controls specular exponent and reflection mixing |
| metallic / specular | bounded `[0,1]`; metallic selects the one-bounce reflection even for off-camera objects |
| emission | added after local lighting, so the flash/lamp remain visible in WARP/reference images |
| local light falloff/range | point lights use `(1-d/range)^2 * intensity/(1+d^2)` and are skipped beyond `range` |
| reflection roughness | metallic response is mixed with `(1-roughness*0.5)` |
| miss radiance | deterministic directional sky used when a shadow/reflection ray misses |

## 7. Particles, history and composition

[`schemas/nw13-particles.v1.json`](../schemas/nw13-particles.v1.json) and
`tools/nw13_particles.py` implement seeded spatial particles with animated
emitters, world-space motion, lifetime, size/material controls and
scene-camera depth occlusion. History reconstructs across reset, seek and
rewind under a fixed 120 Hz cadence and bounded checkpoint memory.

Composite images use straight alpha by default and typed depth/camera
metadata. Feedback consumes the current image plus prior scheduled state,
rejects occluded history, and clears unrelated history at cuts and time
discontinuities. Bloom and tonemapping are applied after scene/effect
composition.

## 8. Versioned audio

The public payload is
[`schemas/nw13-instrument-sequence.v2.json`](../schemas/nw13-instrument-sequence.v2.json).
`tools/nw13_audio.py` compiles it to the preserved NWAS v1 container for the
shared procedural-audio/player path:

* selectable sine, square, saw, triangle and seeded noise waveforms;
* attack/decay/sustain/release envelopes;
* bounded stable one-pole low-pass state;
* gain and equal-power pan;
* separate music/effect tracks and bus automation;
* distinct lead, bass, percussion and event effects.

NWAS v1 decoding and the 48 kHz authoritative sample clock are unchanged.
Diagnostics cover event, voice, filter-state and memory overflow. Offline
samples are identical across chunk partitions, audiovisual cues align within
one offline output frame, and transport cues are not duplicated.

## 9. Original production and retained evidence

[`fixtures/nw13/hotel_corridor.production.json`](../fixtures/nw13/hotel_corridor.production.json)
binds the editable graph, geometry, scene, particle emitter and song. It is an
original hotel-room/corridor sequence with an articulated character, held prop,
hinged door, parallax, corridor cut, camera motion around frozen action,
reverse/resume transport states, a synchronized flash/transient,
depth-occluded particles, a moving shadow and a material-aware reflection.

Every visible/audible contribution maps to editable nodes, scene objects and
audio tracks, with catalog provenance or an
`independently_authored` label. The retained evidence is:

* [`fixtures/nw13/evidence/frames.v1.json`](../fixtures/nw13/evidence/frames.v1.json)
  with actual fixed-tick PPM frames, region checks, declared tolerances and
  source hashes;
* [`fixtures/nw13/evidence/contact_sheet.ppm`](../fixtures/nw13/evidence/contact_sheet.ppm)
  covering edits, the shot cut and transport states.

Regenerate with:

```sh
PYTHONPATH=tools python - <<'PY'
from pathlib import Path
import nw13_production as production
root = Path('.').resolve()
document = production.load_production(root, 'fixtures/nw13/hotel_corridor.production.json')
production.render_evidence(root, document, output_dir='build/nw13-evidence')
PY
```

## 10. Optional lowerings and eligibility

The machine-readable matrix is produced by
`tools/nw13_evidence.optional_lowering_matrix()` and recorded in
[`benchmarks/nw13/optional-lowering.v1.json`](../benchmarks/nw13/optional-lowering.v1.json).
Functional execution, correctness proofs, numerical differences, performance
and adoption are reported separately in
[`benchmarks/nw13/evidence-status.v1.json`](../benchmarks/nw13/evidence-status.v1.json).
Statuses are deliberately separate:

| Technique | Status | Decision |
|---|---|---|
| DXR inline ray query | `capability_qualified` | adopt only with queried capability and matched acceptance |
| Mesh shader | `pending_unavailable` | defer (NW-11 per-device decisions pending) |
| Work Graphs | `experimental` | defer until one bounded experiment meets the predeclared criterion |
| SER | `pending_unavailable` | defer; requires measured divergence and end-to-end gain |
| OMM | `not_applicable` | omit; no alpha-tested content |
| Micro-Mesh | `pending_unavailable` | defer; requires stable redistributable toolchain and measured total benefit |

Training-free ReSTIR/RTXDI, DDGI, SHaRC and NRD are distinguished from
separately assessed pretrained DLSS SR/DLAA, RR and FG/MFG. Online NRC and
asset-specific fitting are excluded. No nodewerk-owned dataset preparation,
training, fine-tuning or per-production fitting enters the pipeline. Any
future inference adoption must use fixed externally trained artifacts and
reset history correctly; frame generation preserves simulation and audio
clocks.

The bounded Work Graphs experiment is recorded in
[`benchmarks/nw13/work-graph-experiment.v1.json`](../benchmarks/nw13/work-graph-experiment.v1.json).
Without a matched measurement it is `defer`, not an inferred pass.

## 11. Artifacts, reproducibility and physical qualification

Every optional export binds the selected lowering, queried D3D12 capabilities,
shader model, runtime dependencies and budgets to artifact provenance.
Unsupported requests fail before GPU work. The common fallback is exported and
smoke-tested independently, including after an optional build/validation
failure. When hosted CI has the native template and pinned DXC it builds the
common artifact twice, runs it through the PackagePlayer, and records the
measured sizes/imports/smoke. Unreachable NW-13 paths are asserted to contribute
zero release code, shader bytes, imports, package sections or dependencies only
after a matched same-revision export pair is measured; otherwise those fields
remain explicitly `null`.

Measured distribution, shader and committed-memory observations are compared
with the artifact's declared budgets (including the canonical 262144-byte
distribution ceiling). `validate_bundle()` reports `nw13_budget_exceeded`
instead of accepting an over-budget artifact as a passing measurement.

`tools/nw13_evidence.py` also emits matched-arm ledgers. Distribution bytes,
shader bytes, startup, committed memory/VRAM, CPU submission, GPU time and
image error are recorded; unavailable observations stay explicit `null` with
their names, never zero. `tools/nw13_artifact_measure.py` refuses to claim an
unexecuted smoke, and independent rebuild/provenance and validation-bundle
integrity are checked in hosted CI.

Physical GPU/audio qualification continues to use
[ADR 0009](architecture/0009-validation-and-evidence-lifecycle.md), the
[physical-validation playbook](physical-validation-playbook.md) and the shared
functional evidence helpers. The ordinary path uses the existing desktop
session, one measured feature execution and short baseline/fallback smokes.
Repeated matched runs are reserved for explicit statistical goals. Missing
hardware is `pending`, missing telemetry is `unavailable`, and startup is
recorded only as observed.

## 12. Evidence status

| Observation | Status |
|---|---|
| catalog regeneration, schemas, graph/scene/particle/audio contracts | implemented and exercised by hosted tests |
| fixed-tick images, region checks, contact sheet | generated and retained; CPU reference evidence |
| independent common/fallback export identities | hosted CI contract |
| DXR/mesh/Work-Graph/SER/OMM/Micro-Mesh physical measurements | pending/unavailable as recorded per item |
| real display/audio alignment | pending; use the NW-12 physical validation path |

