# NW-11 optional experiment tooling

This directory contains the optional-profile experiment implementation. It is
built as separate experiment targets that reuse the current production player
and shared runtime. See [ADR 0009](../../docs/architecture/0009-validation-and-evidence-lifecycle.md)
and the [physical-validation playbook](../../docs/physical-validation-playbook.md).
The directory is not an independent renderer or a frozen implementation boundary:

- `capability_probe.cpp` records the real selected adapter, driver, OS build,
  exact D3D12 `CheckFeatureSupport` results, the selected adapter LUID, and
  every enumerated adapter.
- `render/nw11_scene_harness.cpp` uses the recorded canonical production workload through the
  shared production initialization compute shaders and renders the indexed
  baseline, the mesh-shader variant, or the inline DXR ray-query variant.  The
  launcher passes the probe-selected adapter LUID so a measurement can never
  silently move to another GPU.  The baseline consumes the generated indirect
  draw arguments, both arms run the shared bloom/tonemap/display chain.
  Physical runs use real Present and PresentMon GPU/displayed-frame timings;
  hosted CI uses headless WARP command-path checks without Present. Production
  instrumentation measures CPU submission and the engine allocation high-water
  mark.
- `CMakeLists.txt` builds `nw11_common_baseline` (raster scene PS),
  `nw11_common_analytic` (analytic reflection baseline), separately built
  `nw11_fallback_common`/`nw11_fallback_analytic`, `nw11_optional_mesh`,
  `nw11_optional_ray`, and `nw11_capability_probe` with offline
  `as_6_5`/`ms_6_5`/`ps_6_5` shader compilation.

`tools/nw11_graph_scene.py` generates the graph-derived constants consumed by
the harness and shaders. The mesh stage reads the generated vertex buffer and
cannot drift to hard-coded torus geometry.  `Buffer<uint>` SRVs are indexed in
32-bit elements, and one group thread writes each of the 81 wrapped local-patch
vertices.  The generated constants also carry the canonical NW-07 production
contract digest, bloom/tonemap settings, and the NW-06 camera-interpolation
encoding.

Every experiment target compiles the current `src/player/package_player.cpp`
with shared runtime translation units, including the Win32 window layer.
Compile-time `NW11_VARIANT_*` definitions select one optional pipeline per
executable. The common and fallback artifacts are built from the same source
but never reference mesh or DXR shader payload headers, so unused optional
implementations, shaders, models, or vendor SDK components are not linked into
them.

All experiment executables use the static MSVC CRT. `tools/nw11_toolchain_identity.py`,
`tools/nw11_shader_payloads.py`, and `tools/nw11_pe_dependencies.py` are used by
hosted CI to bind the measured compiler/SDK identity, compiled DXIL/header
payload hashes, and inbox-only PE import set into the validation bundle.

The active v3 experiments accept verified execution of the optional RTX feature. Image equivalence, matched relative performance, process startup, resource usage and distribution cost are reported separately; no default adoption is implied. No reboot, process-creation audit or cold/warm startup classification is required. Historical v1/v2 specifications and evidence remain unchanged. Use shared host identity/locale/PCI, telemetry, presentation metrics, measurement and error-evidence helpers rather than
forking them; retain old task-named entry points only for compatibility.
Reproducibility compares independent builds of the same revision, not old
binary hashes. Physical evidence binds the exact measured bundle and revision.
