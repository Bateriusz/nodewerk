# nodewerk-studio (NW-08 authoring product)

`nodewerk-studio` is the separate interactive authoring application.  The
authoritative editor model and GUI are implemented in
`tools/nw08_editor.py`, `tools/nw08_preview.py`, `tools/nw08_studio.py`, and
`tools/nw08_gui.py`.

The optional native `nodewerk-studio-preview` host in this directory drives the
unchanged `nw::player::PackagePlayer` runtime at an absolute tick and uses the
existing diagnostic readback seam.  It exists only in the authoring product and
is never linked into `nodewerk-player`.  The target links both
`nw_player_bootstrap` (which contains the `PackagePlayer` implementation) and
`nw_package_player` (the package format), plus the platform libraries and the
player build profile so the Windows CRT settings match.

The host accepts `--package <exported.nwp>`, `--memory-budget <bytes>`,
`--stats-json <path>`, and `--session`. In session mode it keeps one D3D12
device alive and consumes `render <tick>` plus authoring-only `load <nwp>`
commands from stdin. Compatible edited packages wait for GPU retirement and
reuse unchanged physical resources/root signatures/PSOs in the same device
epoch; only incompatible layouts or explicit/lost-device recovery recreate the
device. `lose-device` exercises the existing `PackagePlayer` device-removal
seam.

The host enforces the configured budget through the existing
`PlayerConfig::memory_budget_bytes` preflight, waits for submitted GPU work in
`PackagePlayer::shutdown()`, and writes actual engine allocation statistics
with an explicit `"fence_retired": true` marker together with the adapter
vendor/device IDs and driver version queried from the selected D3D12 device.
It also reports the measured `PlayerInstrumentation` CPU/GPU timings when the
diagnostic profile provides them; unavailable values are labelled rather than
fabricated.  The Python preview pipeline uses the process exit as the fence
boundary and reserves the estimated realization before launch, so a preview
that cannot fit the configured budget never starts.

The repository-root `CMakeLists.txt`, `CMakePresets.json`, and every other file
in `tools/nw07_contract.py::PINNED_BUILD_INPUTS` are frozen NW-07 build inputs.
They remain byte-identical.  A complete authoring build keeps the repository
root as CMake's top-level source directory and injects the optional NW-08 target
set after the frozen project has defined its player/runtime targets:

```bash
cmake -S . -B build/nw08-authoring \
  -DCMAKE_PROJECT_INCLUDE="$PWD/tools/nw08_authoring_build/inject-studio.cmake"
cmake --build build/nw08-authoring --target nodewerk-studio-preview nw_procedural_image_player
```

Keeping the repository root as `CMAKE_SOURCE_DIR` is required by the frozen
offline-shader rules.  The injection hook defers `nw_add_studio_authoring_targets()`
until the end of the root configure, so it adds authoring targets without
changing the NW-07 build graph or release closure.

The Python GUI is launched with:

```bash
python tools/nw08_studio.py gui --player-executable <diagnostic-player.exe>
```

Editor-only state (layout, selection, undo/redo, timeline position, cache
budget) never changes canonical graph identity or the release distribution.
