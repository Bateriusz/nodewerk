# NW-08 `nodewerk-studio` Authoring Editor

- **Task:** NW-08 (implementation worker scope: interactive authoring editor)
- **Formats:** `.nwgraph` v1 and `.nwp` v1 (both unchanged)
- **Common profile:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Requirements digest:**
  `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`
- **Proof graph:** `fixtures/nw07/production_loop.nwgraph`
- **Frozen performance contract:** `nw07_perf_ref_v1` (unchanged)

NW-08 adds a separate, minimal `nodewerk-studio` authoring application. It is
not a second engine: every edit is emitted as `.nwgraph` v1 and validated by the
same `tools/nw03_compiler.py` frontend. Preview and export reuse the existing
package-player runtime and `tools/nw05_export.py` export/package path. The
release player, `.nwp` format, dependency closure, frozen NW-07 graph, and
`nw07_perf_ref_v1` contract are untouched.

## Components

| Component | Responsibility |
| :--- | :--- |
| `tools/nw08_editor.py` | Authoritative graph editing, deterministic serialization, undo/redo, layout/selection, timeline, dependency invalidation |
| `tools/nw08_cache.py` | Compiler/device cache identity, device epochs, asynchronous generation tokens, fence-safe bounded GPU cache |
| `tools/nw08_preview.py` | Preview pipeline, last-valid retention, source-linked diagnostics, pass timings, size attribution |
| `tools/nw08_studio.py` | Headless application model, CLI, export facade |
| `tools/nw08_gui.py` | Minimal Tk GUI: graph, inspector, curves, timeline, preview, diagnostics, timings, size, cache budget |
| `src/studio/nw_studio_preview_host.cpp` | Native authoring preview host over the NW-04 `PackagePlayer` core plus the build-tree-only hot-reload overlay; release sources remain unchanged |
| `tools/verify_nw08.py` | Contract/build/full verification entry point |
| `tools/nw08_windows_integration.py` | Windows integration validation and retained evidence |

The GUI is an authoring-only target. Tk, compiler/DXC, source maps, reflection
data, and profiling state never enter `src/player/` or the generated release
manifest. No second graph model, package/export format, player, plugin
marketplace, asset browser, scripting console, or alternate evidence system is
introduced.

## Editing the NW-07 proof without serialized text

`GraphEditorSession.create_nw07_proof()` loads the frozen graph into an editable
document model. The GUI provides node selection, parameter inspection/editing,
connection editing, curve keyframe editing, timeline scrubbing, preview, undo,
redo, and export. Saving emits a deterministic `.nwgraph` v1 file; the frozen
`fixtures/nw07/` directory is write-protected by the editor.

Editor-only state is explicitly outside canonical identity:

- layout coordinates,
- selection,
- undo/redo history,
- timeline cursor,
- preview/device/cache state.

Changing layout or selection leaves the canonical semantic digest unchanged.
Under identical release inputs, editor additions do not change the pinned
NW-07 build-input digest or the release distribution byte size.

## Incremental invalidation

The editor mirrors the NW-02 dependency contract:

- every node has a structural semantic identity covering operator id/version,
  frequency, residency, canonical parameters, seed identity, and upstream
  structural identities;
- a parameter edit invalidates only the changed node and its transitive
  dependent closure;
- unrelated branches retain cache hits;
- layout/selection changes invalidate nothing.

For the NW-07 proof, editing the texture seed recomputes the texture and the
material/render/bloom/tonemap/present chain while retaining cached torus, mesh,
instance, camera, light, and twist branches.

## Asynchronous preview and generation tokens

Every semantic edit advances an immutable snapshot generation. Preview jobs
carry:

```text
(job id, snapshot generation, device epoch, semantic digest)
```

A completion is accepted only when all of the following hold:

- it was not cancelled;
- it has not already completed;
- its snapshot generation is still current;
- its device epoch is still current;
- no newer job for that snapshot has superseded it;
- the device has not been lost.

Failed edits, cancelled jobs, out-of-order completions, and jobs from a previous
snapshot or device instance can never replace the last valid preview or publish
into a newer generation.

## Preview and export

- `ReferencePreviewBackend` is a deterministic contract test backend and is
  never reported as hardware evidence.
- `WindowsRuntimePreviewBackend` first exports the document it was given through
  `nw05_export.export_graph` (the same path used by `nodewerkc export`) and then
  launches the existing runtime on those exact bytes: the native
  `nodewerk-studio-preview` host with `--package`, or the generated player in
  `embedded-player` mode. A preview never renders an unrelated frozen embedded
  package.
- Preview requires an actual presented-output readback. A zero process exit
  without a `tick_<tick>.rgba32f` file is a failure and is never reported as
  hardware evidence. Physical evidence additionally requires a real,
  un-injected Windows process and an explicit physical/passthrough execution
  declaration.
- The captured pixels are retained on the accepted frame and displayed in the
  GUI. A failed edit keeps the last accepted image visible.
- GUI preview work runs on a worker thread; the Tk event thread only drains
  generation-checked results, so edits, timeline scrubbing, and cancellation
  remain responsive. Stale or cancelled completions are discarded.
- The native authoring host is built separately from the frozen root project:
  the root build first produces the pinned offline shader headers/template, and
  `src/studio` then compiles a build-tree-only PackagePlayer overlay. No
  authoring target or source modification is added to the NW-07 release graph.
- In `package-host` mode `AuthoringRuntimeSession` keeps the native host
  process and D3D12 device alive across timeline scrubs and layout-compatible
  graph edits. Before an edited package is installed, submitted work is retired;
  unchanged committed resources, root signatures, and PSOs remain live while
  invalidated realizations are rebuilt. Only an incompatible resource layout,
  device loss, dead host process, or explicit reinitialization creates a new
  device instance and advances the device epoch.
- Export calls `nw05_export.export_graph`; the CLI calls the same function.
  GUI and CLI exports of the same semantic graph, settings, pinned toolchain,
  and player inputs are byte-identical.
- The GUI Export Settings dialog updates the live preview exporter and
  toolchain identity immediately. Opening another graph or creating a new
  proof preserves the launch preview mode, execution kind, resolution, and
  export configuration through `StudioApplication.reopen()`.

Preview reports include pass timings and deterministic size attribution.
Measured process/timing values come from the authoring host's
`PlayerInstrumentation`; unavailable measurements are labelled explicitly and
never fabricated.
The native host accepts the configured GPU budget (`--memory-budget`) and
emits actual `PackagePlayer` allocation statistics plus an explicit
`fence_retired` flag after `shutdown()` waits for submitted work. The pipeline
reserves the estimated realization before launch, so a preview that cannot fit
the budget fails closed instead of launching.
No hard real-time editor latency claim is made without a representative graph
benchmark.

## Cache identity

Compiler/semantic keys include:

- canonical semantic digest;
- capability-profile ID and requirements digest;
- target profile and digest;
- compiler settings and semantic/compiler inputs;
- compiler id/version, the measured DXC version (or an explicit `unavailable`
  marker), target, optimization, OS/toolchain identity;
- generated shader/entry-point identity and bindings.

Per-node device-resident realization keys use the node's canonical structural
hash plus the same compiler/target inputs, and additionally include:

- the preview width/height used for size attribution and exported artifacts;
- node-specific compiled shader specialization/blob identities derived from the
  existing export manifest's `root_node_ids`; package-global shader-set and
  resource/root-signature binding identities belong only to the whole-graph
  compiler/device key;
- the full compiler key;
- the D3D12 device-instance recreation epoch;
- capability-profile ID/digest;
- driver version, OS build, and toolchain identity;
- actual adapter/device identity (`vendor_id`, `device_id`, `subsystem_id`,
  adapter LUID).

Device-resident keys use the identity reported for the adapter the runtime
actually selected: the authoring host reports vendor/device/subsystem IDs, the
real DXGI adapter LUID, and driver version from the `PackagePlayer` D3D12
device. The fallback Windows CIM inventory is used only when complete runtime
identity is unavailable, and is refreshed for each device instance. On hosts where observation is unavailable the keys
use an explicit all-zero `AdapterDeviceIdentity.unavailable()` sentinel;
production code never substitutes a fabricated NVIDIA/AMD PCI identity. A
marketing name reported by any source is ignored.

GPU marketing names, RTX generation labels, and adapter descriptions are
observed metadata only. They never define a cache predicate, compiler target,
capability predicate, or device epoch.

## Bounded GPU memory and fence retirement

The GPU realization cache is bounded by a configured byte budget.
`live bytes + pending-retirement bytes` never exceeds that budget. A resource
referenced by submitted GPU work is moved to a retirement queue when evicted or
released and is destroyed only after a fence value at least as large as its
submission fence is signalled. If submitted resources consume the whole budget,
new admission fails closed instead of destroying work still in flight.

## Device loss and reinitialization

The device generation is a D3D12 device-instance recreation epoch:

- device loss invalidates GPU realizations and rejects in-flight completions;
- old-epoch resources and jobs are never reused;
- explicit preview reinitialization increments the epoch;
- the GUI exposes an explicit **Reinitialize Device** action;
- recovery never bypasses or changes operators and is independent of the
  release-player failure policy.

An in-flight render is revalidated twice: before work starts and after the
backend returns. Device loss or a semantic edit during rendering causes the
completion to be rejected, the last valid preview is retained only when the
device is still live, and reinitialization increments the epoch.
`DeviceRemoved` is propagated from the runtime host rather than collapsed into
a generic preview failure, so the pipeline enters its lost-device state and
queues/cache entries from the old device are invalidated.

## Disk cache corruption

Disk shader-cache entries remain owned by the existing NW-03 `ShaderCache`.
Corrupt metadata, an unexpected blob hash, or a metadata mismatch is treated as
a cache miss; the entry is regenerated and atomically replaced without
changing the `.dxil`/`.json` ownership convention or package identity.

## Verification

```bash
pytest -q tests/test_nw08_*.py
python tools/verify_nw08.py --mode ContractsOnly --summary-json build/nw08.json
python tools/verify_nw08.py --mode BuildChecks \
    --player-template <specialized-player.exe> --dxc <pinned-dxc> \
    --summary-json build/nw08-build-checks.json
python tools/nw08_windows_integration.py \
    --player-template <specialized-player.exe> --dxc <pinned-dxc> \
    --preview-executable <diagnostic-player.exe> \
    --report build/nw08-windows-integration.json
python tools/verify_nw08.py --mode Full \
    --evidence build/nw08-windows-integration.json \
    --summary-json build/nw08-full.json
```

- `ContractsOnly` is GPU-less and never claims hardware acceptance.
- `BuildChecks` performs concrete GUI/CLI export equivalence when the pinned
  DXC and player template are available; otherwise it reports pending.
- `Full` requires the retained Windows integration report, including a real
  preview execution for hardware evidence.

The Windows CI job exercises GUI/CLI export equivalence, shader-error recovery,
device recreation, and fence-safe caching with the deterministic NW-04 fixture
toolchain. Real preview remains `pending_unverified` unless the opt-in
`windows-real-preview` job is enabled with a physical preview host. Simulated
or contract execution is never relabeled as hardware evidence.

The generated-player compatibility check is skipped unless
`nw08_windows_integration.py --embedded-preview` is explicitly supplied with a
diagnostic player template built with capture controls. It never gates
package-host real-preview evidence.

Outstanding NW-07 hardware captures remain `pending_unverified`. They are not
an implementation prerequisite for the editor and are never relabeled as
verified by NW-08 contract tests.
