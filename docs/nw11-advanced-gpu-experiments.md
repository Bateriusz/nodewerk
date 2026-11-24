# NW-11 Advanced GPU Feature Experiments

NW-11 evaluates optional GPU features only through bounded, costed
experiments layered on the completed NW-07 graph-derived production/export
path. It starts from immutable base
`dc8edbafc3f5928861f14b0fbb932f7d42e81c8b`; the older planning base is
provenance only. NW-01A issue records are accepted as prerequisite completion,
and NW-01B experiment/evidence semantics are reused rather than replaced.

The initial implementation deliberately contains **exactly two experiments**:

1. conventional indexed/instanced rendering versus mesh shaders for the
   existing NW-07 torus workload;
2. an analytic reflection baseline versus DXR inline ray queries through the
   same graph, geometry, quality, capture, and present architecture.

No second production backend, hand-authored renderer, or additional prototype
enters this task.

## 1. Common profile and optional profiles

The common profile is inherited unchanged from NW-01/NW-03..NW-07:

| Item | Value |
|---|---|
| Profile ID | `nw.common.d3d12.fl12_0.sm6_0.v1` |
| Requirements digest | `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a` |
| API | D3D12 Feature Level 12_0, shader model 6_0 |
| Fallback | none (this *is* the common artifact profile) |
| Distribution size | Measured and reported; no NW-11 experimental byte ceiling |
| Evidence status | `pending` until RTX 4060 Ti and RTX 5070 Ti common-artifact correctness is physically verified |

The common requirements digest is the **inherited NW-07 identity**
`sha256:v1:d2c53aea…`. It is not re-hashed from the NW-11 presentation of the
same requirements; `nw11_capability.compute_requirements_digest` returns that
frozen identity for the common profile and a content hash for optional
profiles. Both the published profiles and `profile_manifest_template` are
validated against the same rule, so the template cannot generate a profile the
validator rejects.

The common profile never promotes pending coverage: while
`benchmarks/nw11/evidence-status.v1.json` records common correctness and
required physical coverage as pending, the common profile's device coverage
and `evidence_status` remain `pending`.

The two optional profiles are separately exported and separately tested:

| Profile | ID | Requirements digest | Shader targets | Fallback |
|---|---|---|---|---|
| Mesh shaders | `nw.optional.d3d12.mesh_shaders.ms6_5.v1` | `sha256:v1:cc0636631f844cca1840c848849fd0056a5c688d6228f97cec0a70fb78f237b8` | `as_6_5`, `ms_6_5`, `ps_6_0` | `nw11.fallback.common.d3d12.fl12_0.sm6_0.v1` |
| Ray queries | `nw.optional.d3d12.ray_query.ps6_5.v1` | `sha256:v1:efe3d0e61d837c57bf102267bbfc7afd60c4a2fbaf401ea000b7fa9174e77ebd` | `ps_6_5` | `nw11.fallback.common.d3d12.fl12_0.sm6_0.v1` |

Every profile document (`benchmarks/nw11/profiles/`) declares:

- exact queried D3D12 capabilities and members;
- accepted values and shader-model order;
- toolchain/runtime requirements, including the Agility SDK decision;
- complete dependency ledger with optional/counted flags;
- artifact budget and fallback artifact identity;
- device coverage status for the two representative desktop tiers;
- pass/fail/pending/unsupported evidence status.

### Queried capabilities

The common queries are:

| Query ID | D3D12 query | Member |
|---|---|---|
| `d3d_feature_level` | `D3D12_FEATURE_FEATURE_LEVELS` | requested feature levels |
| `resource_binding_tier` | `D3D12_FEATURE_D3D12_OPTIONS` | `ResourceBindingTier` |
| `resource_heap_tier` | `D3D12_FEATURE_D3D12_OPTIONS` | `ResourceHeapTier` |
| `highest_shader_model` | `D3D12_FEATURE_SHADER_MODEL` | `HighestShaderModel` |

The optional profiles add:

| Profile | Query | Required value |
|---|---|---|
| Mesh shaders | `D3D12_FEATURE_D3D12_OPTIONS7` | `MeshShaderTier >= D3D12_MESH_SHADER_TIER_1` |
| Mesh shaders | `D3D12_FEATURE_SHADER_MODEL` | `HighestShaderModel >= D3D12_SHADER_MODEL_6_5` |
| Ray queries | `D3D12_FEATURE_D3D12_OPTIONS5` | `RaytracingTier >= D3D12_RAYTRACING_TIER_1_1` |
| Ray queries | `D3D12_FEATURE_SHADER_MODEL` | `HighestShaderModel >= D3D12_SHADER_MODEL_6_5` |

Eligibility is decided **only** from these queried values. GPU marketing names,
driver names, and representative models are provenance metadata. A missing
query is `pending`; a present-but-insufficient value is `unsupported`; an
explicit initialization failure is `failed`. None of those states is a pass,
and none triggers a silent effect substitution.

For optional profiles the requirements digest is `sha256:v1:` over canonical
JSON of the profile ID, profile kind, exact query descriptors, shader targets,
and frozen runtime/toolchain requirements. Device names, budgets, fallback
paths, and evidence status are intentionally excluded. The common profile uses
the inherited NW-07 identity described above.

### Runtime and toolchain requirements

- Windows 11 x64, build 22631 or newer.
- NVIDIA driver floor 610.88 for the representative validation targets.
- Windows-inbox D3D12/DXGI; no runtime DXC/D3DCompiler redistribution.
- Offline DXC 1.8.2407 (measured 1.8.2407.7).
- MSVC 19.4x, Windows SDK 10.0.22621 or newer, CMake 3.24+, Ninja.
- **No Agility SDK redistribution is required** for these two experiments. A
  future Agility dependency would be optional, counted, and subject to a new
  profile version.
- No DLSS, RTX Kit, model weights, CUDA, vendor neural SDK, or runtime shader
  compiler is part of any NW-11 artifact.

## 2. Frozen experiments

Definitions live in `benchmarks/nw11/experiments/`. Each is marked
`status: frozen` before measurement and records:

- base graph `fixtures/nw07/production_loop.nwgraph` with the frozen SHA-256
  `093b8429…b7091`;
- 256 seeded torus instances, 1920×1080, 960,000 ticks / 20 s / 60 Hz;
- quality profile `nw07.quality.v1`, capture protocol
  `nw07-correctness-capture.v1`, performance contract `nw07_perf_ref_v1`, and
  protocol `nw07-perf-run.v1`;
- warm-up 5 s, 3 fresh processes, 60 s per run, ≥600 raw samples per run,
  nearest-rank p99, no application pipeline cache;
- at least 10% lower p99 GPU time at matched quality, independently per
  qualified device/environment;
- end-to-end frame-time, CPU-submit, shader-count,
  cold-initialization, and VRAM guardrails;
- a fixed-quality image criterion (`max_abs_channel_error <= 1e-4`,
  `RMSE <= 1e-4`) at frozen fixed ticks;
- actual complete and incremental distribution sizes (informational), and shader-count guardrails;
- the fallback artifact identity and execution arguments.

### 2.0 End-to-end implementation

`src/nw11` builds one renderer harness with four separately linked artifacts:

| Artifact | Pipeline | Optional payload |
|---|---|---|
| `nw11_common_baseline.exe` | indexed/instanced raster + shared `nw06_scene_ps` | none |
| `nw11_common_analytic.exe` | indexed/instanced raster + analytic reflection `ps_6_0` | none |
| `nw11_fallback_common.exe` | separately built common fallback, raster pipeline | none |
| `nw11_fallback_analytic.exe` | separately built common fallback, analytic pipeline | none |
| `nw11_optional_mesh.exe` | `as_6_5` + `ms_6_5` + shared `ps_6_0` | amplification and mesh shaders |
| `nw11_optional_ray.exe` | indexed/instanced raster + `ps_6_5` inline ray query | DXR shader and BLAS/TLAS build code |

`tools/nw11_graph_scene.py` lowers `fixtures/nw07/production_loop.nwgraph`
into generated C++ constants. The harness runs the frozen NW-06
initialization compute shaders (`generate`, `instances`, `material`, `camera`,
`light`) to produce the torus vertex/index buffers, instance table, material,
camera curve, and light, then renders all variants from those same buffers.
The ray-query artifact builds a BLAS from the generated vertex/index buffers
and a TLAS from the generated instance transforms, fences the build with a UAV
barrier and fence wait, transitions both acceleration-structure scratch
buffers to `D3D12_RESOURCE_STATE_UNORDERED_ACCESS` before the build, and keeps
every resource alive until the frame command list has completed. No
hand-authored geometry, second renderer, or second production backend exists.
The probe publishes the selected adapter LUID; the launcher passes that exact
LUID to every measured executable, and the renderer fails initialization
rather than silently measuring a different GPU.

Every command list binds the shader-visible descriptor heap before any root
descriptor table is used. The conventional scene path consumes the generated
`DRAW_INDEXED_INSTANCED` argument record through `ExecuteIndirect`; the mesh
path uses `DispatchMesh`. Both arms then execute the frozen NW-06 post chain
(half-resolution bloom, ACES tonemap to the RGBA32F present resource, display
conversion) and present through a flip-discard swap chain. Post settings,
exposure, gamma, and the canonical production-contract digest are generated
from `benchmarks/nw07/canonical-production.v1.json`, not duplicated literals.
The measured executables use the static MSVC CRT so a clean validation machine
does not need VCRUNTIME/MSVCP redistributables. Hosted CI records the actual
DXC/MSVC/SDK/CMake identities, SHA-256 identities for every compiled
DXIL/header payload, and the PE import DLL set in the bundle.

The CMake project compiles every shader offline with the pinned DXC
(`as_6_5`, `ms_6_5`, `ps_6_5`, and the frozen `cs_6_0`/`vs_6_0`/`ps_6_0`
payloads). Compile-time `NW11_VARIANT_*` definitions select exactly one
optional pipeline per executable, so the common and fallback artifacts do not
link mesh or DXR code, shaders, or payload headers.

### 2.1 Mesh shaders versus indexed rendering

- Graph derivation: `pipeline_lowering_overlay`; the NW-07 graph is unchanged,
  no operators are added, and no second backend exists.
- Baseline: the existing `nw06_scene_vs` + `nw06_scene_ps` indexed/instanced
  path.
- Variant: `as_6_5` amplification + `ms_6_5` mesh stages under
  `shaders/nw11/mesh/`, sharing the common fragment stage and all NW-07
  instance/camera/material/light buffers.
- The mesh stage consumes the vertex buffer generated by the frozen NW-06
  initialization dispatch. Segment counts and radii arrive through
  graph-derived root constants; the shader does not reconstruct a hard-coded
  torus, so a graph parameter change cannot silently drift from the workload.
- Generated buffers are bound as `Buffer<uint>` SRVs, so the mesh stage loads
  `NwVertices` and `NwIndices` in 32-bit element units (eight uint elements per
  32-byte vertex), never as byte offsets. One group thread owns each of the
  81 wrapped local-patch vertices, so every declared mesh vertex is written.
- Camera interpolation uses the same encoding as `nw06_scene_vs`: the generated
  constant is 0 for linear and 1 for step, and the mesh stage calls the same
  `NwCamera.Load(11u)` curve semantics as the raster path.
- Meshlets: 8×8 cells (9×9 including the wrapped boundary vertices), 32
  meshlets per instance × 256 instances.
- Synchronization: `SetMeshOutputCounts` and
  `GroupMemoryBarrierWithGroupSync()` are declared and enforced by static
  bundle checks.
- Shader-count increment ≤ 2; distribution bytes reported.

### 2.2 DXR ray queries versus analytic reflection

- Graph derivation: `fragment_shader_specialization_overlay`; the same NW-07
  production graph, torus mesh, instance buffer, quality profile, captures,
  and present path are used for both arms.
- Baseline: `nw11_analytic_reflection_ps` (`ps_6_0`), which is common-profile
  valid and contains no DXR dependency.
- Variant: `nw11_scene_rayquery_ps` (`ps_6_5`) with inline
  `RayQuery<RAY_FLAG_NONE>` over a BLAS/TLAS built from the same
  graph-generated geometry.
- The acceleration structure and all bound buffers must outlive the frame
  command list; BLAS/TLAS build work is fenced before shading.
- Shader-count increment ≤ 1; distribution bytes reported.
- Missing DXR capability is explicitly unsupported and the separately exported
  common analytic fallback remains available; initialization/execution failure
  is reported as failure, never silently substituted.

## 3. Evidence, decisions, and staleness

`tools/nw11_evidence.py` and `tools/nw11_verify.py` enforce:

- evidence binds commit, bundle ID, bundle-manifest SHA-256, capability profile
  SHA-256, and the hash of every measured executable/package;
- raw per-sample GPU time, CPU submit time, frame time, cold initialization,
  distribution bytes, shader count, VRAM, and fixed-quality image results are
  preserved;
- decisions are isolated by `(device_tier, device_key, environment_key)`; a
  result on one GPU never establishes the other;
- the collector requires exactly three fresh-process reports per role and at
  least 600 raw samples in each report; merging one long report cannot satisfy
  the protocol;
- each run is bound to the bundle role (`common_baseline`, `optional_variant`,
  `fallback_common`) and to the verified bundle's distribution-byte and shader
  inventory;
- the separately exported common fallback must be executed on the same device
  and is compared against the baseline captures before completion can be
  claimed;
- pending and unsupported coverage never count as passing;
- rejected variants are recorded with explicit reasons;
- stale commits/hashes and malformed evidence are non-zero exits.

Evidence status values are `accepted`, `rejected`, `failed`, `pending`, and
`unsupported`. Tracked decision documents in `benchmarks/nw11/decisions/`
currently record both device tiers as `pending`, which is the honest state
until matched physical runs exist.

## 4. Validation bundles

`tools/nw11_build_bundle.py` creates a deterministic, self-contained bundle:

```
run-validation.ps1
bundle-manifest.json
bin/common_baseline.exe
bin/optional_variant.exe
bin/fallback_common.exe
bin/nw11_capability_probe.exe
manifests/capability-profile.json
manifests/common-capability-profile.json
manifests/experiment.json
manifests/follow-up-matrix.json
manifests/shader-manifest.json
manifests/graph-scene.json
schemas/*.json
shaders/nw06/... (shared payloads)
shaders/nw11/...
tools/nw11/*.py
runtime/python-runtime.zip
```

The manifest binds repository, commit, experiment/profile, toolchain/runtime
identity, expected artifacts, fallback artifact ID, capability queries,
dependency ledger, accounting, and the per-role shader inventory.
`tools/nw11_verify.py bundle` recomputes identities, schema-validates the
capability profile and experiment definition, and rejects missing, extra,
mutated, or misclassified files.

Bundles are built by hosted CI from the executables compiled by the same
workflow run. `tools/nw11_build_bundle.py --require-head` refuses a `--commit`
that does not equal the checked-out repository HEAD, so hashing an arbitrary
operator-supplied file cannot be presented as same-revision evidence.
The same workflow provisions the pinned NW-01 toolchain, records the measured
DXC/MSVC/SDK/CMake identity, hashes every compiled DXIL/header payload, parses
the measured PE import tables (rejecting redistributable VC runtimes), and
carries the canonical NW-07 production contract, so the bundle binds the
actual toolchain, shader payloads, dependencies, and production contract rather
than restating static profile values.

Hosted Windows CI also runs the common baseline on the software adapter through
the real D3D12 command list and presentation path, with a short warm-up and
sample loop, so descriptor-heap binding, `ExecuteIndirect`, post-processing,
resource transitions, timestamp queries, and swap-chain presentation are
exercised before physical evidence is requested.

The bundle manifest also enforces zero-cost stripping:

- common and fallback artifacts carry no mesh, ray-tracing, Agility, DLSS,
  RTX Kit, model-weight, or vendor SDK payload/dependency;
- common/fallback shader targets are restricted to the common profile;
- every optional byte is counted in the distribution report.

### One-command physical validation

On a clean validation machine with PowerShell 7; no system Python installation is required:

```powershell
pwsh .\run-validation.ps1 -BundleRoot . -ExpectedCommit <tested-commit>
```

The launcher verifies the bundled `runtime/python-runtime.zip`, rehydrates it
into a fresh temporary directory for each validation, and uses only that
runtime for the repository-owned verifier and evidence tools. It does not need
a source checkout, nodewerk build, Visual Studio changes, CMake configuration,
system Python installation, Python package installation, or manual evidence
editing. It:

1. verifies bundle identities and hashes;
2. runs the bundled capability probe and records the actual adapter, driver,
   OS build, inbox `d3d12.dll`/`dxgi.dll` versions, Agility `D3D12Core`
   presence, and queried D3D12 features/shader-model support;
3. determines eligibility from those queries;
4. re-checks each measured artifact SHA-256 immediately before execution and
   passes the probe-selected adapter LUID to every measured executable;
5. blocks the optional variant with an explicit unsupported (`3`) or
   pending (`11`) decision when its required capability is absent or
   unqueried, while still executing the common baseline and separately exported
   common fallback;
6. runs the common baseline, the variant only when eligible, and the separately
   exported common fallback under identical frozen settings, warm-up, and
   repeated fresh processes, taking GPU time from timestamp queries and CPU
   submit time from the `ExecuteCommandLists` call;
7. preserves raw outputs and compares fixed-tick captures with the
   repository-owned comparator;
8. invokes the repository-owned verifier locally and writes
   `evidence.json`, `decisions.json`, and a short `summary.txt` into a sibling
   output directory (never into the immutable bundle root, so unrecorded
   payload detection stays strict).

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | verified/accepted |
| 2 | malformed manifest/profile/experiment/evidence |
| 3 | capability absence: explicitly unsupported |
| 4 | initialization or execution failure |
| 5 | stale evidence |
| 6 | bundle identity mismatch |
| 10 | measured decision rejected |
| 11 | required device/environment coverage still pending |

### Raw run report contract

Each measured executable writes one JSON report per fresh process. The report
declares `role` (`baseline`/`variant`), `artifact_path` matching the bundle
manifest, the capability decision obtained before launch, the device/environment
keys, and a `measurement` object containing raw GPU/CPU/frame-time samples,
cold-initialization milliseconds, distribution bytes, shader count, peak
engine-committed GPU bytes, and the fixed-quality image result. The launcher
merges repeated processes for the same role only when every identity and
capability snapshot agrees, including the probe-selected adapter LUID; it
never fabricates or reorders samples. The collector replaces baseline,
variant, and fallback image metrics with results computed directly from the
fixed-tick capture bytes, so a measured binary cannot self-report a passing
quality result.

## 5. Physical coverage and published decisions

Required acceptance coverage is:

- desktop Ada representative: NVIDIA GeForce RTX 4060 Ti;
- desktop Blackwell representative: NVIDIA GeForce RTX 5070 Ti.

Each optional-profile claim must use matched baseline/variant runs on the same
qualified device and environment. Cross-SKU equivalence is not assumed. The
tracked `evidence-status.v1.json` keeps physical evidence
`pending_physical` until both tiers have verified matched bundles. Contract
implementation is verified by pytest and does not substitute for physical
evidence.

After ordinary CI is green and implementation is stable, validation bundles for
the tested commit are attached to the PR (or linked from it) together with
`evidence.json`, `decisions.json`, and the bundle manifest SHA-256. Pending
coverage does not count as a completed acceptance.

## 6. Preserved contracts and stripping

- The NW-01 capability manifest is unchanged; mesh shaders, ray tracing,
  runtime shader compilation, and mandatory Agility SDK redistribution remain
  excluded from the common baseline.
- NW-07 canonical production, quality, resolution, capture, reproducibility,
  distribution, metric-baseline, and `nw07_perf_ref_v1` semantics remain
  intact.
- NW-07 pinned build inputs do not include any `src/nw11`, `shaders/nw11`, or
  `tools/nw11` file, so the common artifact bytes and dependencies are
  unchanged.
- NW-11 experiment records in `benchmarks/experiments/` reuse the NW-01B
  `experiment-record.schema.json` semantics via the additive
  `advanced_gpu_feature` category and `pending_evaluation` status.

## 7. Follow-up matrix

`benchmarks/nw11/follow-up-matrix.v1.json` covers Vulkan 1.4, asynchronous
compute, Work Graphs, SER, opacity micromaps, micromesh/clustered geometry, and
tensor/neural shading. Each entry records content-specific hypotheses, exact
feature queries, API/shader/toolchain/runtime requirements, deployment and
licensing constraints, dependencies, byte costs (measured or an explicit
unmeasured estimate), and disposition. DLSS, RTX Kit, model weights, and
generic vendor SDK components are optional and fully counted wherever relevant.
No further feature prototype enters NW-11.

## 8. Explicit non-dependencies

NW-09 is required only for future temporal/stateful experiments; the two NW-11
experiments are stateless and do not depend on it. NW-11 is independent of
NW-10's 64 KiB result: it neither raises nor lowers that compact target, and
the original NW-07 256 KiB proof and its exact-byte regression gate remain unchanged.


### Frozen source isolation and informational distribution accounting

NW-11 generates its specialized translation unit from the original production
`src/player/package_player.cpp` using a source-SHA- and context-checked overlay.
NW-07 and NW-10 continue to compile their original frozen input; historical
NW-10 reports and physical evidence are not rewritten. If the shared source
changes later, the generator fails until the overlay is explicitly rebased.

Bundle integrity and adoption are separate results. Complete and incremental
experimental distribution bytes are reported without a size acceptance gate.
The original NW-07 common production export must still remain byte-identical,
and unused optional code, shaders and dependencies must remain excluded.
Performance, quality, resource, shader-count and capability checks are unchanged.
