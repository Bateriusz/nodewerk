# nodewerk-studio (NW-08 authoring product)

`nodewerk-studio` is the separate interactive authoring application.  The
authoritative editor model and GUI are implemented in
`tools/nw08_editor.py`, `tools/nw08_preview.py`, `tools/nw08_studio.py`, and
`tools/nw08_gui.py`.

The optional native `nodewerk-studio-preview` host in this directory drives the
unchanged `nw::player::PackagePlayer` runtime at an absolute tick and uses the
existing diagnostic readback seam.  It exists only in the authoring product and
is never linked into `nodewerk-player`.

The host accepts `--package <exported.nwp>`, `--memory-budget <bytes>`, and
`--stats-json <path>`.  It enforces the configured budget through the existing
`PlayerConfig::memory_budget_bytes` preflight, waits for submitted GPU work in
`PackagePlayer::shutdown()`, and writes actual engine allocation statistics
with an explicit `"fence_retired": true` marker.  The Python preview pipeline
uses the process exit as the fence boundary and reserves the estimated
realization before launch, so a preview that cannot fit the configured budget
never starts.

The repository-root `CMakeLists.txt`, `CMakePresets.json`, and every other file
in `tools/nw07_contract.py::PINNED_BUILD_INPUTS` are frozen NW-07 build inputs.
For that reason the studio project is a standalone CMake project rather than a
default target of the root build.  An authoring build may add it explicitly:

```bash
cmake -S src/studio -B build/studio -DNW_STUDIO_ROOT="$PWD"
cmake --build build/studio
```

For a complete authoring build (including the content-free
`nw_procedural_image_player` template), use the wrapper that adds the unchanged
root project plus this optional target set:

```bash
cmake -S tools/nw08_authoring_build -B build/nw08-authoring
cmake --build build/nw08-authoring --target nodewerk-studio-preview
```

The Python GUI is launched with:

```bash
python tools/nw08_studio.py gui --player-executable <diagnostic-player.exe>
```

Editor-only state (layout, selection, undo/redo, timeline position, cache
budget) never changes canonical graph identity or the release distribution.
