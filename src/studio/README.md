# nodewerk-studio (NW-08 authoring product)

`nodewerk-studio` is the separate interactive authoring application.  The
authoritative editor model and GUI are implemented in
`tools/nw08_editor.py`, `tools/nw08_preview.py`, `tools/nw08_studio.py`, and
`tools/nw08_gui.py`.

The optional native `nodewerk-studio-preview` host in this directory drives the
established `nw::player::PackagePlayer` runtime at an absolute tick and uses the
existing diagnostic readback seam. It exists only in the authoring product and
is never linked into `nodewerk-player`. The standalone studio build compiles
`nw08_player_bootstrap` from the same frozen player sources while replacing
only `package_player.cpp/.h` with a deterministic build-tree overlay that adds
the authoring hot-reload seam. The immutable `.nwp` implementation is compiled
as `nw08_package_player`; both use the same player build profile/CRT settings
as the frozen runtime.

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
They remain byte-identical. A complete authoring build is intentionally
two-stage: first build the frozen runtime/template normally, then compile the
standalone studio runtime against the generated shader headers from that build:

```bash
cmake --preset nw07-release -DNW_ENFORCE_PINNED_TOOLCHAIN=ON
cmake --build --preset nw07-release --target nw_procedural_image_player

cmake -S src/studio -B build/nw08-studio \
  -DCMAKE_BUILD_TYPE=Debug \
  -DNW_BUILD_PROFILE=dev \
  -DCMAKE_TOOLCHAIN_FILE="$PWD/cmake/toolchains/msvc-x64.cmake" \
  -DNW_STUDIO_ROOT="$PWD" \
  -DNW_STUDIO_RUNTIME_BUILD_DIR="$PWD/build/nw07-release"
cmake --build build/nw08-studio --target nodewerk-studio-preview
```

The second build never adds a target to the frozen root graph. It consumes only
the root build's generated offline shader headers and compiles the authoring-only
PackagePlayer overlay into `nw08_player_bootstrap`.

The Python GUI is launched with:

```bash
python tools/nw08_studio.py gui --player-executable <diagnostic-player.exe>
```

Editor-only state (layout, selection, undo/redo, timeline position, cache
budget) never changes canonical graph identity or the release distribution.
