# NW-11 optional experiment tooling

This directory contains the optional-profile experiment implementation. It is
deliberately **not** part of the root NW-07 production build:

- `capability_probe.cpp` records the real selected adapter, driver, OS build,
  exact D3D12 `CheckFeatureSupport` results, the selected adapter LUID, and
  every enumerated adapter.
- `render/nw11_scene_harness.cpp` lowers the frozen NW-07 graph through the
  frozen NW-06 initialization compute shaders and renders the indexed
  baseline, the mesh-shader variant, or the inline DXR ray-query variant.  The
  launcher passes the probe-selected adapter LUID so a measurement can never
  silently move to another GPU.  The baseline consumes the generated indirect
  draw arguments, both arms run the frozen bloom/tonemap/display/present chain,
  GPU time comes from timestamp queries, and VRAM comes from
  `IDXGIAdapter3::QueryVideoMemoryInfo`.
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

Compile-time `NW11_VARIANT_*` definitions select one optional pipeline per
executable. The common and fallback artifacts are built from the same source
but never reference mesh or DXR shader payload headers, so unused optional
implementations, shaders, models, or vendor SDK components are not linked into
them.

All experiment executables use the static MSVC CRT. `tools/nw11_toolchain_identity.py`,
`tools/nw11_shader_payloads.py`, and `tools/nw11_pe_dependencies.py` are used by
hosted CI to bind the measured compiler/SDK identity, compiled DXIL/header
payload hashes, and inbox-only PE import set into the validation bundle.
