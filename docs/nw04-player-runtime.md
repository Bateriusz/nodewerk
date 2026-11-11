# NW-04 Package Player Runtime

- **Status:** Implemented from immutable base `ab60451a2c5d5a921fce054b2267934e059d4638`
- **Task:** NW-04
- **Format contract:** NW-03 `.nwp` v1 (unchanged)
- **Common profile:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Requirements digest:** `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`

## Public Lifetime

`nw::player::PackagePlayer` exposes the minimal runtime contract:

```cpp
ExitCode initialize(const package::Package& package);
ExitCode initialize(const std::uint8_t* bytes, std::size_t size);
ExitCode initializeFromFile(const char* path);
ExitCode renderAtTick(std::uint64_t tick);
ExitCode resize(std::uint32_t width, std::uint32_t height);
ExitCode shutdown();
```

Initialization runs the package's `INIT` instructions exactly once. `renderAtTick`
executes the package's `FRAM` instructions in emitted order on every call. `resize`
waits for all affected frame work before recreating swap-chain render targets.
`shutdown` is idempotent and releases every COM/Win32 resource.

## Startup Order

The player enforces this order and the verification suite checks it:

1. bounded `.nwp` v1 decode, integrity checksum, structure/reference checks;
2. exact emitted requirement validation against the capability record;
3. Win32 window + DXGI adapter + `D3D12CreateDevice(D3D_FEATURE_LEVEL_12_0)`;
4. actual capability queries (`D3D12_FEATURE_LEVELS`, `SHADER_MODEL`,
   `D3D12_OPTIONS`, `D3D12_OPTIONS1`, `D3D12_OPTIONS4`,
   `GPU_VIRTUAL_ADDRESS_SUPPORT`, per-format `FORMAT_SUPPORT`);
5. exact requirement comparison against those queried values;
6. adapter-specific `GetResourceAllocationInfo` for every package resource;
7. bounded frame-instruction planning (no graph traversal or reflection);
8. one direct queue, a double-buffered `FLIP_DISCARD` swap chain, fences, bounded
   descriptor heaps, and a small frame-upload ring;
9. ordered `INIT` instruction execution;
10. ordered `FRAM` instruction execution from `renderAtTick`.

No bulk package resource is allocated before step 5 succeeds.

## Resource Ownership and Placements

NW-03 carries logical resource lifetimes and alias-group intent. NW-04 derives
conservative lifetimes from the ordered frame instruction stream, but it never
treats an offline lifetime or alias group as a portable physical offset. Every
resource receives an independent non-aliased committed allocation sized and
aligned by the current adapter's `GetResourceAllocationInfo`. `ResourcePlan`
records the queried size/alignment, the non-aliased placement kind, and the
logical lifetime as evidence. Physical alias reuse is deferred to NW-05.

There are two frames in flight by default (clamped to 1..3). Each slot owns one
command allocator, command list, upload buffer, and fence value. Reusing a slot's
upload buffer waits on that slot's fence. Rendering performs no engine-owned heap
or descriptor allocation after initialization; resize/recreation is the only
explicit allocation path.

## Display and Barriers

Compute work is package-driven. A `frame.present` instruction drives the visible
frame: the player transitions the current swap-chain back buffer from `PRESENT` to
`RENDER_TARGET`, clears it fullscreen, transitions it back to `PRESENT`, executes
the frame command list, and presents with the single direct queue. Ordinary
resource barriers only; enhanced barriers are not used.

`frame.barrier` operand 0 uses this stable player mapping:

| Value | State |
| ---: | :--- |
| 0 | `COMMON` |
| 1 | `VERTEX_AND_CONSTANT_BUFFER` |
| 2 | `INDEX_BUFFER` |
| 3 | `RENDER_TARGET` |
| 4 | `UNORDERED_ACCESS` |
| 5 | `DEPTH_WRITE` |
| 6 | `DEPTH_READ` |
| 7 | `NON_PIXEL_SHADER_RESOURCE` |
| 8 | `PIXEL_SHADER_RESOURCE` |
| 9 | `COPY_DEST` |
| 10 | `COPY_SOURCE` |
| 11 | `PRESENT` |
| 12 | `GENERIC_READ` |

Unknown state codes are rejected during frame planning; they never silently
degrade to `COMMON`. A requested state is also checked against the target
resource's heap and usage (for example, a readback resource may only remain
`COPY_DEST`, and `UNORDERED_ACCESS` requires UAV usage). Descriptor-table
targets must resolve to a descriptor that actually exists in the named heap.

`frame.set_timeline` selects a package parameter by id and advances it from the
emitted tick base by the tick supplied to `renderAtTick`. The bounded constant
block is updated in place from the pre-built frame plan; no allocation,
reflection, or re-planning occurs per frame.

## Failure Behavior

- Missing, unknown, malformed, or contradictory capability fields:
  `PackageValidationFailed` (`70`).
- Unmet exact adapter requirement: `CapabilityBaselineUnmet` (`23`).
- Allocation, pipeline, submission, resize, and device failures use their
  existing stable categories.
- Minimized or zero-sized windows suspend presentation and return success
  without resizing the swap chain.
- Device removal returns `DeviceRemoved` (`51`) and shuts down cleanly. There is
  no automatic device-loss replay.

## Instrumentation

Only the `dev` profile defines `NW_PLAYER_DIAGNOSTICS`. It exposes CPU submission
timestamps, GPU timestamp queries, capture hooks, and engine memory accounting
(package resource/upload bytes, descriptors, heaps) separately from process
private bytes and driver-local GPU usage. The release profile defines
`NW_ENABLE_D3D12_VALIDATION=0` and excludes the diagnostic surface. No runtime
DXC/Slang, graph parser, source schema, reflection table, or authoring metadata
is linked into the player.

## Fixtures and Verification

- `fixtures/nw04_package_player/main.cpp` is the package-only compute +
  fullscreen-display fixture.
- `fixtures/nw04/compute_display.nwp` is a deterministic NW-03 test-container
  fixture used for cross-platform contract checks. It is **not** hardware
  evidence.
- `python tools/verify_nw04.py --mode ContractsOnly` runs GPU-less contract,
  capability-failure, rejection-before-allocation, lifecycle/ownership, and
  release-closure checks.
- `python tools/verify_nw04.py --mode Full --fixture <player.exe> --package
  <real.nwp>` is the Windows qualification path. It refuses the test container
  for hardware evidence.
- `python tools/nw04_evidence.py` records executions and leaves missing Ada or
  Blackwell coverage explicitly `pending_unverified`.
- `pwsh tools/nw01/capture-nw04-player.ps1 -Fixture <player.exe> -Package
  <real.nwp> -Tier ada|blackwell` is the NW-01B evidence-harness entry point
  for a physical host.
