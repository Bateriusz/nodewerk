# NW-11 optional experiment tooling

This directory contains the optional-profile experiment implementation. It is
deliberately **not** part of the root NW-07 production build:

- `capability_probe.cpp` records the real selected adapter, driver, OS build,
  exact D3D12 `CheckFeatureSupport` results, and every enumerated adapter.
- `render/nw11_scene_harness.cpp` lowers the frozen NW-07 graph through the
  frozen NW-06 initialization compute shaders and renders the indexed
  baseline, the mesh-shader variant, or the inline DXR ray-query variant.
- `CMakeLists.txt` builds `nw11_common_baseline` (raster scene PS),
  `nw11_common_analytic` (analytic reflection baseline), separately built
  `nw11_fallback_common`/`nw11_fallback_analytic`, `nw11_optional_mesh`,
  `nw11_optional_ray`, and `nw11_capability_probe` with offline
  `as_6_5`/`ms_6_5`/`ps_6_5` shader compilation.

`tools/nw11_graph_scene.py` generates the graph-derived constants consumed by
the harness and shaders. The mesh stage reads the generated vertex buffer and
cannot drift to hard-coded torus geometry.

Compile-time `NW11_VARIANT_*` definitions select one optional pipeline per
executable. The common and fallback artifacts are built from the same source
but never reference mesh or DXR shader payload headers, so unused optional
implementations, shaders, models, or vendor SDK components are not linked into
them.
