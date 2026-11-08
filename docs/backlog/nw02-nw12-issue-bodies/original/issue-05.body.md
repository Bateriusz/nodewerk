## Goal

Execute initialization and frame programs without carrying authoring infrastructure.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Implement Win32 lifecycle, capability checks, D3D12 device/swapchain, one direct queue, fences, resource/descriptor tables, package loading, parameter uploads, and a bounded pass executor. Use a small number of frames in flight with explicit upload and resource ownership. The compiler provides logical lifetimes and permitted alias groups; startup queries actual resource size/alignment and realizes placements for the current adapter. Texture allocation requirements are adapter/driver-specific, so fixed portable offsets from slot 3 are invalid [GetResourceAllocationInfo documentation](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-getresourceallocationinfo%28uint_uint_constd3d12_resource_desc%29). Initially use non-aliased placement; NW-05 adds proven reuse. Rendering performs no engine-owned heap or descriptor allocations after initialization, except explicit resize/recreation paths. Diagnostic builds expose timing and capture hooks; release linkage excludes them.

## Scope

- Package-driven compute and fullscreen display.
- Resource realization, descriptor setup, fences, and barriers.
- Minimal errors, resize/minimize handling, and orderly shutdown.
- Development CPU/GPU timestamps and memory accounting.

## Affected components

- Proposed player
- Proposed D3D12 backend
- Proposed platform and allocation helpers

## Required behavior

- Check capabilities and package validity before bulk resource allocation.
- Execute initialization once, followed by ordered frame instructions.
- Avoid graph traversal, runtime reflection, readback, and engine-owned per-frame allocations.
- Distinguish engine memory accounting from driver/process totals.

## State, configuration, and API changes

- Define initialize(package), renderAtTick(tick), resize, and shutdown internal interfaces.
- Define stable exit-code categories for package, capability, allocation, pipeline, and device failures.
- Introduce resource-table and per-frame ownership structures.

## Recovery semantics

- Device removal terminates the release player cleanly; automatic recovery is not required.
- Resize waits for affected work and recreates size-dependent resources.
- Zero-sized/minimized windows suspend presentation safely.
- Partial initialization releases successfully created resources.

## Compatibility and safety constraints

- No generic RHI, scene graph, ECS, editor library, or plugin loader.
- Descriptor limits and format support are queried and bounded.
- GPU allocations may occur within the driver; the allocation prohibition concerns engine code.

## Test plan

- Render a package-only fixture with validation enabled.
- Exercise missing capabilities, allocation failures, pipeline failures, resize, minimize, and shutdown.
- Verify fence-safe upload reuse and absence of engine-owned per-frame allocations.
- Inspect release imports and linker maps for tooling dependencies.

## Regressions

- Track bootstrap/player byte growth and CPU submission time.
- Validation errors and resource leaks block completion.
- Runtime must not acquire a dependency on compiler-generated source schemas.

## Acceptance criteria

- The NW-03 fixture renders from its package on both target GPU generations.
- A sustained run and resize sequence are validation-clean.
- The release player runs without compiler/editor installation.
- Timing and memory reports are available from the diagnostic build.

## Decomposition dependencies and boundaries

Dependencies:
- NW-01 provides the working platform baseline.
- NW-03 provides the package and shader contracts.

Boundaries:
- No advanced RTX features, audio, asynchronous compute, or device-loss replay.
- No assumption that offline lifetimes determine portable physical memory offsets.

<!-- pairallel-sections:v1 {"sections":[{"end":93,"slug":"goal","start":9},{"end":388,"slug":"source-context","start":114},{"end":1427,"slug":"final-technical-design","start":417},{"end":1673,"slug":"scope","start":1439},{"end":1784,"slug":"affected-components","start":1699},{"end":2115,"slug":"required-behavior","start":1808},{"end":2418,"slug":"state-configuration-api-changes","start":2159},{"end":2733,"slug":"recovery-semantics","start":2443},{"end":3005,"slug":"compatibility-and-safety-constraints","start":2776},{"end":3334,"slug":"test-plan","start":3021},{"end":3548,"slug":"regressions","start":3352},{"end":3843,"slug":"acceptance-criteria","start":3574},{"end":4179,"slug":"decomposition-dependencies-and-boundaries","start":3891}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":3,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:e2cc8ebe078f74aaa171ff65f808915de1ca5cb8f8b3139b67eee565b9030c89","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
