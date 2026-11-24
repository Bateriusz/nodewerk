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
| Distribution ceiling | 262,144 bytes complete; 131,072-byte stretch is measurement only |

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

The requirements digest is `sha256:v1:` over canonical JSON of the profile ID,
profile kind, exact query descriptors, shader targets, and frozen
runtime/toolchain requirements. Device names, budgets, fallback paths, and
evidence status are intentionally excluded.

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
- end-to-end frame-time, CPU-submit, distribution, shader-count,
  cold-initialization, and VRAM guardrails;
- a fixed-quality image criterion (`max_abs_channel_error <= 1e-4`,
  `RMSE <= 1e-4`) at frozen fixed ticks;
- incremental byte and shader-count budgets;
- the fallback artifact identity and execution arguments.

### 2.1 Mesh shaders versus indexed rendering

- Graph derivation: `pipeline_lowering_overlay`; the NW-07 graph is unchanged,
  no operators are added, and no second backend exists.
- Baseline: the existing `nw06_scene_vs` + `nw06_scene_ps` indexed/instanced
  path.
- Variant: `as_6_5` amplification + `ms_6_5` mesh stages under
  `shaders/nw11/mesh/`, sharing the common fragment stage and all NW-07
  instance/camera/material/light buffers.
- Meshlets: 8×8 vertices, 32 meshlets per instance × 256 instances.
- Synchronization: `SetMeshOutputCounts` and
  `GroupMemoryBarrierWithGroupSync()` are declared and enforced by static
  bundle checks.
- Incremental budget: 32,768 bytes; shader-count increment ≤ 2.

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
- Incremental budget: 65,536 bytes; shader-count increment ≤ 1.
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
manifests/experiment.json
manifests/follow-up-matrix.json
shaders/...
tools/nw11/*.py
```

The manifest binds repository, commit, experiment/profile, toolchain/runtime
identity, expected artifacts, fallback artifact ID, capability queries,
dependency ledger, and accounting. `tools/nw11_verify.py bundle` recomputes
identities and rejects missing, extra, mutated, or misclassified files.

The bundle manifest also enforces zero-cost stripping:

- common and fallback artifacts carry no mesh, ray-tracing, Agility, DLSS,
  RTX Kit, model-weight, or vendor SDK payload/dependency;
- common/fallback shader targets are restricted to the common profile;
- every optional byte is counted toward the incremental budget.

### One-command physical validation

On a clean validation machine with PowerShell 7 and a Python 3 runtime:

```powershell
pwsh .\run-validation.ps1 -BundleRoot . -ExpectedCommit <tested-commit>
```

The launcher does not need a source checkout, nodewerk build, Visual Studio
changes, CMake configuration, Python package installation, or manual evidence
editing. It:

1. verifies bundle identities and hashes;
2. runs the bundled capability probe and records the actual adapter, driver,
   OS build, inbox `d3d12.dll`/`dxgi.dll` versions, Agility `D3D12Core`
   presence, and queried D3D12 features/shader-model support;
3. determines eligibility from those queries;
4. runs the common/baseline and eligible variant under identical frozen
   settings, warm-up, and repeated fresh processes;
5. preserves raw outputs;
6. invokes the repository-owned verifier locally and writes
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
capability snapshot agrees; it never fabricates or reorders samples.

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
the common distribution ceiling remains the frozen NW-07 256 KiB gate.
