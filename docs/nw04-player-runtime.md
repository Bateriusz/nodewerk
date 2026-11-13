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
logical lifetime as evidence. Physical alias reuse is implemented by NW-05's
`BuildAliasedResourcePlan`/`tools/nw05_schedule.py`, which proves compatibility
against those queried sizes/alignments, duplicates transients across overlapping
frames, and records required aliasing/UAV barriers. NW-04's non-aliased path
remains available.

There are two frames in flight by default (clamped to 1..3). Each slot owns one
command allocator, command list, upload buffer, and fence value. Reusing a slot's
upload buffer waits on that slot's fence. Rendering performs no engine-owned heap
or descriptor allocation after initialization; resize/recreation is the only
explicit allocation path.

## Display and Barriers

Compute work is package-driven. A `frame.present` instruction names the package
output it displays. The player validates during planning that the operand is a
UAV buffer containing complete RGBA32F pixels. At execution it binds that raw
`RWByteAddressBuffer` (R32_TYPELESS raw UAV) and a player-owned
R8G8B8A8_UNORM display texture to an offline-compiled fullscreen compute
shader, writes the display texture, copies it to the current swap-chain back
buffer, transitions the back buffer to `PRESENT`, and presents with the single
direct queue. Changing the compute output or the supplied tick therefore
changes the displayed image. Ordinary resource barriers only; enhanced barriers
are not used, and no frame instruction performs a CPU readback.

The display pass temporarily rebinds the compute root signature, root
constants, and descriptor tables. After the display dispatch it restores the
latest package binding state, so a package stream such as
`dispatch, present, dispatch` cannot accidentally execute the second dispatch
with the display signature.

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
| 13 | `INDIRECT_ARGUMENT` |
| 14 | `ALL_SHADER_RESOURCE` |

Unknown state codes are rejected during frame planning; they never silently
degrade to `COMMON`. A requested state is also checked against the target
resource's heap and usage (for example, a readback resource may only remain
`COPY_DEST`, and `UNORDERED_ACCESS` requires UAV usage). Descriptor-table
targets must resolve to a descriptor that actually exists in the named heap.
When a `frame.barrier` requests `UNORDERED_ACCESS` while the resource already
remains in that state, the player emits a UAV barrier. Dependent dispatches
that reuse a UAV in the same command list also get a UAV barrier before the
second access.

Planning validates instruction operands against their actual targets before
any initialization upload or frame submission: root-constant counts are bounded
by the active serialized root signature's 32-bit constants width, descriptor
tables must match the active root binding slot/space, dispatches must reference
the active root signature, and `frame.copy_buffer` rejects self-copies,
non-buffer operands, readback sources, and byte counts larger than either
resource.

`frame.set_timeline` selects a package parameter by id and advances it from the
emitted tick base by the tick supplied to `renderAtTick`. The bounded constant
block is updated in place from the pre-built frame plan; no allocation,
reflection, or re-planning occurs per frame. Frame planning rejects parameter
types outside `float`/`float2`/`float3`/`float4`/`uint`/`int`, rejects values
whose component width does not match the declared type, and rejects timeline
parameters that are not fully present in a bounded root-constant block. The
tick update writes the parameter's complete declared width.

## Failure Behavior

- Missing, unknown, malformed, or contradictory capability fields:
  `PackageValidationFailed` (`70`).
- Unmet exact adapter requirement: `CapabilityBaselineUnmet` (`23`).
- Allocation, pipeline, submission, resize, and device failures use their
  existing stable categories.
- Minimized or zero-sized windows suspend presentation and return success
  without resizing the swap chain. An ordinary Win32 restore clears suspension
  even when the client size is unchanged, so the next tick resumes rendering
  without a needless `ResizeBuffers`.
- Device removal returns `DeviceRemoved` (`51`) and shuts down cleanly. There is
  no automatic device-loss replay.
- `shutdown()` drains the final asynchronous frame and returns any fence,
  device-removal, or D3D12-validation failure it observed while still
  completing cleanup.
- If `Present` reports occlusion or a non-device-removal failure after the
  command list was submitted, the player still signals and records a
  retirement fence before returning the presentation diagnostic. Cleanup and
  `shutdown()` therefore wait for the submitted work instead of releasing
  resources still in GPU use.

## Instrumentation

Only the `dev` profile defines `NW_PLAYER_DIAGNOSTICS`. It exposes CPU submission
timestamps, GPU timestamp queries, capture hooks, and engine memory accounting
(package resource/upload bytes, descriptors, heaps) separately from process
private bytes and driver-local GPU usage. The release profile defines
`NW_ENABLE_D3D12_VALIDATION=0` and excludes the diagnostic surface. No runtime
DXC/Slang, graph parser, source schema, reflection table, or authoring metadata
is linked into the player.

`engine_resource_bytes` is live engine-resource accounting. Resize/recreation
removes the released display texture's adapter-queried allocation size before
adding its replacement. `engine_cumulative_resource_bytes` separately records
every resource allocation ever made, so repeated resize cycles cannot inflate
the live figure while allocation history remains inspectable.

## Fixtures and Verification

- `fixtures/nw04_package_player/main.cpp` is the package-only compute +
  fullscreen-display fixture.
- `fixtures/nw04/compute_display.nwp` is a deterministic NW-03 test-container
  fixture used for cross-platform contract checks. It is **not** hardware
  evidence.
- `python tools/nw04_fixture.py --from-package <real.nwp> --output
  <display.nwp>` composes the display stream onto a real offline-compiled
  NW-03 package without changing the container format.
- `python tools/verify_nw04.py --mode ContractsOnly` runs GPU-less contract,
  capability-failure, rejection-before-allocation, lifecycle/ownership, and
  release-closure checks.
- `python tools/verify_nw04.py --mode Full --fixture <player.exe> --package
  <real.nwp>` is the Windows qualification path. It refuses the test container
  for hardware evidence.
- `python tools/nw04_evidence.py` records executions and leaves missing Ada or
  Blackwell coverage explicitly `pending_unverified`.
- Hardware qualification is bound to the exact supplied player/package SHA-256
  values. Historical executions for different artifacts remain historical and
  cannot satisfy the current qualification check.
- `pwsh tools/nw01/capture-nw04-player.ps1 -Fixture <player.exe> -Package
  <real.nwp> -Tier ada|blackwell` is the NW-01B evidence-harness entry point
  for a physical host.
