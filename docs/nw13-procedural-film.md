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
499 Werkkzeug3 registrations and 370 Werkkzeug4 declarations from PR #48.
The regeneration commands check the original Git blob identities and Wz4 source path set.

```sh
python tools/operator_catalog.py check --reference-root /path/to/pinned/fr_public
python tools/operator_catalog.py generate --reference-root /path/to/pinned/fr_public
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
repetition, generated primitives and memory before evaluation. The common CPU
and shader marches share conservative field gradient bounds over each clipped
ray interval; strong displacement or deformation exceeding the film profile's
gradient limit is diagnosed before export. A public bounded march can
distinguish exhausted steps from a geometric miss. Reference-point,
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
| fallback | a separately built copy of the common film |

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

The film compiler derives one exact integer timeline for shader action,
camera cuts and audio cue bindings, including authored tick-zero cues. Studio
and the common standalone exporter use the same graph-aware NWAS preparation;
neither substitutes the source note's action tick for its presentation onset.

## 6. Common rendering and inline ray queries

The film graph connects a named geometry library to `nw.shader.film`.
The stateless `nw.render.film` still feeds `nw.render.present` directly;
the feedback-enabled film uses the typed frame producer and History node. The library contains the source
solids and their dependencies. The shared optimizer and offline compiler
specialize the film pass from those nodes. The checked-in graph is the
authoritative editable runtime input. Its geometry and scene companion JSON
files are import inputs, not substitutions during evidence generation.
Studio can preview the graph through
the existing Windows package host or the CPU reference renderer.

The SM 6.0 common compute pass traces bounded solids against the current
transforms. The separately linked SM 6.5 film uses inline ray queries to find
candidate solids from a swept-bounds acceleration structure. Both paths
evaluate detailed intersections and material response at the current tick.
The optional player builds one BLAS and one TLAS at startup and performs no
per-frame acceleration builds. Its runtime checks DXR 1.1 and SM 6.5 before
allocating the acceleration resources.

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

[`schemas/nw13-particles.v1.json`](../schemas/nw13-particles.v1.json)
supports seeded emitters with animated origins and world-space motion. The
film pass reconstructs its bounded particle cycle from the 120 Hz action
tick. A particle samples its emitter at the spawn tick and compares its
positive linear view-space depth with the scene depth at the covered pixel.
The film composites straight-alpha particles before spatial bloom and
tonemapping.

The film's public graph connects `nw.render.film_frame@1.0.0` to
`nw.feedback.scene_image@1.0.0` with three typed views from the same frame:
opaque color, positive linear view-space depth, and camera metadata. The
previous input is owned by the shared PackagePlayer History scheduler and
cannot be authored as a graph edge. The optional ray-query AS belongs only to
the film producer; the feedback pass has its own current and previous SRVs.

This one-tap effect reprojects the preceding raw film sample and blends
within a declared depth tolerance. It does not recursively feed a blended
image back into itself. The scheduler reconstructs at most one previous
120 Hz sample plus the exact presentation frame, including after a backward
seek. At a cut or an authored reversal the feedback pass rejects the prior
epoch. Previous data outside the camera frustum or incompatible with the
current depth is also rejected. The package has no baked checkpoint or
serialized initial pixel buffer for this state type. The two scene passes
are charged to the same per-frame work limit.
Both package readers enforce the versioned command order and bindings: the
prior sample is copied before the current render, and the distinct prior
and current views are sampled by the terminal composite.

Standalone measurement includes a zero-weight control export: the authored
flash must leave a visible echo at tick 150400, while the shot cut at tick
192000 must exactly match the no-feedback capture. The same oracle is used by
Studio reference preview. Native results are distinct from reference and
compiled-shader checks.

## 8. Versioned audio

The public payload is
[`schemas/nw13-instrument-sequence.v2.json`](../schemas/nw13-instrument-sequence.v2.json).
`tools/nw13_audio.py` compiles it to NWAS v2 for the
shared procedural-audio/player path:

* selectable sine, square, saw, triangle and seeded noise waveforms;
* attack/decay/sustain/release envelopes;
* bounded stable one-pole low-pass state;
* gain and equal-power pan;
* separate music/effect tracks and bus automation;
* distinct lead, bass, percussion and event effects.

The player retains NWAS v1 decoding and a 48 kHz authoritative sample clock.
NWAS v2 uses stereo PCM in offline WAV and WASAPI playback. Sequential
refills carry voice and filter state. Seeking reconstructs the active filter
from at most two one-second windows per active voice.
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
The conventional and Work Graphs arms route 1,024 deterministic candidates
through the same visibility and shading functions. Each compares all output
slots with a CPU oracle after explicitly invalidating the previous values
outside the measured GPU interval. The predeclared criterion requires a median GPU
dispatch time below 95% of the conventional arm, exact output and at most
32 MiB of backing memory. The Windows runner logs hosted WARP baseline
execution and leaves Work Graphs adoption pending until a matched device run.

```powershell
cmake -S src/nw13 -B build/nw13-work-graph -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/nw13-work-graph --target nw13_work_graph
python tools/nw13_work_graph.py build/nw13-work-graph/nw13_work_graph.exe --output build/nw13-evidence/work-graph-matched.json
```

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

NW-13 distribution, executable, package and shader byte counts are reported
for comparison only, not used as acceptance limits. The evidence records
actual export file lengths, counts each delivered path once, and separately
attributes the PE image, the embedded payload and the package sections. The
size of an executable is included once: the embedded bytes are a component
of that executable, not an additional file. Explicit reports written into
the export directory are included with their actual finalized byte counts.
A single-file CLI export attributes companions only when their bytes bind to
the selected package or embedded manifest; stale same-stem files do not count.
Captures and evidence reports outside the export folder are not part of the
measured distribution.
Memory and acceleration-structure budgets, package integrity and bounds,
image checks, smoke and reproducibility requirements still apply.
The independent NW-11 common/fallback WARP smoke remains runnable after a
film rejection, without converting the Windows job result to success.

`tools/nw13_evidence.py` also emits matched-arm ledgers. Distribution bytes,
shader bytes, startup, committed memory/VRAM, CPU submission, GPU time and
image error are recorded; unavailable observations stay explicit `null` with
their names, never zero. `tools/nw13_artifact_measure.py` refuses to claim an
unexecuted smoke, and independent rebuild/provenance and validation-bundle
integrity are checked in hosted CI. The artifact runner resolves the measured
commit from the clean checkout and retains the historical implementation base
as a separate field. It compares independently linked outputs and their
manifests across isolated CMake and DXC cache roots. Once the Windows
artifact runs, it retains exact-dimension finite RGBA32F readbacks and a
contact sheet across its fixed cuts, cue and transport states. Native image
checks bind a graph CPU oracle and, when supported, matched common/DXR frames.
Controlled re-exports test moving-shadow and off-camera material response;
two frames at identical frozen action must show the camera move. None of
these hosted checks establishes physical presentation timing or GPU adoption.

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
| catalog regeneration, schemas, graph/scene/particle/audio contracts | portable verification |
| fixed-tick images, region checks, contact sheet | generated and retained; CPU reference evidence |
| independent common/fallback export identities | hosted CI contract |
| DXR and Work Graphs native experiments | executables and runners implemented, device measurements pending |
| Mesh shader, SER, OMM and Micro-Mesh | adoption status recorded per technique and workload |
| real display/audio alignment | pending; use the NW-12 physical validation path |

