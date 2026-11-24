# NW-11 optional tooling

This directory contains optional-profile validation tooling. It is deliberately
**not** part of the root NW-07 production build:

- `capability_probe.cpp` records the real adapter, driver, OS build, and exact
  D3D12 `CheckFeatureSupport` results used to decide optional-profile
  eligibility.
- `CMakeLists.txt` builds the probe as a standalone executable for validation
  bundles only.

The common and fallback artifacts remain the existing graph-derived NW-07
production players. No mesh-shader, ray-query, Agility SDK, DLSS, RTX Kit,
model-weight, or vendor SDK byte is linked into them.
