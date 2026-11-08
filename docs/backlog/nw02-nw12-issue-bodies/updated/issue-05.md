# NW-04 — Implement the minimal package-driven D3D12 player

## Goal

Execute initialization and frame programs without carrying authoring infrastructure.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Implement Win32 lifecycle, capability checks, D3D12 device/swapchain, one direct queue, fences, resource/descriptor tables, package loading, parameter uploads, and a bounded pass executor. Use a small number of frames in flight with explicit upload and resource ownership. The compiler provides logical lifetimes and permitted alias groups; startup queries actual resource size/alignment and realizes placements for the current adapter. Texture allocation requirements are adapter/driver-specific, so fixed portable offsets from slot 3 are invalid [GetResourceAllocationInfo documentation](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-getresourceallocationinfo%28uint_uint_constd3d12_resource_desc%29). Initially use non-aliased placement; NW-05 adds proven reuse. Rendering performs no engine-owned heap or descriptor allocations after initialization, except explicit resize/recreation paths. Diagnostic builds expose timing and capture hooks; release linkage excludes them. Startup capability validation is exact: compare queried D3D12 feature level, shader model, resource binding/heap tiers, required formats/limits, and every emitted optional feature/profile requirement against the package's common-profile identity and canonical requirements digest. Adapter names, GPU marketing generations, tier labels, and PCI identifiers are recorded metadata only and never repair missing evidence or grant eligibility. Qualify the identical common package on one representative compliant desktop Ada device and one representative compliant desktop Blackwell device; missing coverage remains pending.

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

- Validate exact startup capabilities and package requirement/profile identity before bulk resource allocation, and produce actionable diagnostics naming each unmet requirement.
- Treat adapter descriptions, marketing names, generation labels, and PCI identifiers as metadata only; they cannot establish capability or repair missing evidence.
- Execute initialization once, followed by ordered frame instructions.
- Avoid graph traversal, runtime reflection, readback, and engine-owned per-frame allocations.
- Distinguish engine memory accounting from driver/process totals.

## State, configuration, and API changes

- Define initialize(package), renderAtTick(tick), resize, and shutdown internal interfaces.
- Define stable exit-code categories for package, capability, allocation, pipeline, and device failures.
- Expose structured capability-validation diagnostics identifying the unmet requirement, required value, observed value, package profile ID/digest, and existing exit-code category.
- Introduce resource-table and per-frame ownership structures.

## Recovery semantics

- Device removal terminates the release player cleanly; automatic recovery is not required.
- Resize waits for affected work and recreates size-dependent resources.
- Zero-sized/minimized windows suspend presentation safely.
- Partial initialization releases successfully created resources.
- Missing or unknown required capabilities fail with the existing capability-failure category before bulk allocation; missing representative Ada/Blackwell evidence remains a pending coverage state, not a runtime failure or pass.

## Compatibility and safety constraints

- No generic RHI, scene graph, ECS, editor library, or plugin loader.
- Descriptor limits and format support are queried and bounded.
- GPU allocations may occur within the driver; the allocation prohibition concerns engine code.
- The identical common package is used for representative Ada and Blackwell correctness qualification; no per-generation package fork is introduced.
- No adapter-name, marketing-generation, tier-label, or PCI-whitelist compatibility predicate is permitted.

## Test plan

- Render a package-only fixture with validation enabled.
- Exercise missing feature level, shader model, binding/heap tier, format/limit, unknown profile/digest, and malformed package capabilities; verify diagnostics name the unmet requirement and occur before bulk allocation.
- Qualify the identical common package on representative desktop Ada and representative desktop Blackwell devices and record exact adapter, driver, OS, capability query, artifact hashes, and unavailable telemetry; missing coverage remains pending.
- Exercise allocation failures, pipeline failures, resize, minimize, and shutdown.
- Verify fence-safe upload reuse and absence of engine-owned per-frame allocations.
- Inspect release imports and linker maps for tooling dependencies.

## Regressions

- Track bootstrap/player byte growth and CPU submission time.
- Validation errors and resource leaks block completion.
- Runtime must not acquire a dependency on compiler-generated source schemas.
- Missing hardware coverage must remain pending and must not be reported as passing; executed failures remain failures.
- Startup validation must not branch on adapter name, generation label, or PCI identifier.

## Acceptance criteria

- The identical NW-03 common package renders from its package on representative desktop Ada and representative desktop Blackwell devices, with exact capability/profile and environment evidence; missing coverage remains pending.
- Startup capability diagnostics identify exact unmet requirements before bulk allocation and never rely on adapter names or marketing generations.
- A sustained run and resize sequence are validation-clean.
- The release player runs without compiler/editor installation.
- Timing and memory reports are available from the diagnostic build.

## Decomposition dependencies and boundaries

Dependencies:
- NW-01 provides the working platform baseline.
- NW-03 provides the package and shader contracts.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No advanced RTX features, audio, asynchronous compute, or device-loss replay.
- No assumption that offline lifetimes determine portable physical memory offsets.
- No exact-SKU correctness prerequisite beyond representative desktop generation coverage.
