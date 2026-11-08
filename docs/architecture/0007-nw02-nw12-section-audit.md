# ADR-0007: NW-02 through NW-12 Section-Level Audit

- **Status:** Accepted
- **Task:** NW-01A (refined issue #18; planning source issue #15)
- **Date:** 2026-11-08
- **Planning base:** `743a4c1aec9e016af8c76cf08f9dd1696017270c`
- **Frozen NW-01A source requirements:** `sha256:v1:4b7a4e828d9ce4835d3f002bdd2960d7af00d99aefdc418317bc8592f753116d`
- **Common profile:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Canonical requirements digest:** `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`

## Source verification

The actual downstream issue identities and source revisions were verified before any body or mapping correction:

- Planning run: `plan-1e6c8c83858cb79fbd34a36a`
- Source issue: #1
- Planning base: `270d298f1b037548e0ab4808b2c123c12ddcc4e6`
- Frozen source requirements: `sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f`
- Method: Rendered from the durable planning lead result with the recorded rendering version; the resulting body hash matched planning_publication_intents.body_hash for issue #3 through #13. Titles matched planning_outputs and the inspected backlog.

The final NW-01 capability contract was cross-checked against the planning base:

- Source: `benchmarks/capability-manifest.v1.json` (`sha256:v1:2ebf9bfd2c994f1845cd9df2d36d58a134b59e1af19b6e484b3ac658e5ef4ed2`)
- Feature level `12_0`; shader model `6.0`; resource binding tier `2`; resource heap tier `2`.
- Descriptor model `bounded_descriptor_tables`; barrier model `ordinary_resource_barriers`; queue model `{"compute_queue_count": 0, "copy_queue_count": 0, "direct_queue_count": 1}`.
- Baseline limits: `{"max_cbv_srv_uav_descriptors_per_frame": 4096, "max_compute_threads_per_group": 1024, "max_root_signature_dword_cost": 16, "max_sampler_descriptors_per_frame": 64, "max_texture_2d_dimension": 8192, "swap_chain_buffer_count": 2}`.

The repository mapping in `docs/architecture/0005-task-contracts-nw02-nw12.md` did not match the actual issue titles from NW-03 onward. It is corrected from the verified titles below. No hardware edit is applied to a misidentified task.

## Audit legend

- **Changed** — the section has exact replacement text below and a stated reason.
- **Unchanged** — the section is explicitly reviewed and requires no change; the reason is stated.
- **Reviewed / no policy change** — an ancillary section was checked for indirect hardware assumptions.

The nine required sections are Final technical design, Scope, Required behavior, State/configuration/API changes, Compatibility and safety constraints, Test plan, Regressions, Acceptance criteria, and Dependencies/boundaries. Recovery semantics is audited separately for consistency. Affected components is reviewed for indirect assumptions. Concrete pass/fail/pending/unsupported review examples are recorded in ADR-0006.

The canonical issue bodies are the `.body.md` files; they carry body-relative `pairallel-sections:v1` offsets. The `.md` wrappers begin with a `# title` line and deliberately omit the machine manifest so body-relative offsets cannot be misread as document-relative offsets.

Publication status is tracked in [`docs/backlog/nw01a-publication-record.v1.json`](../backlog/nw01a-publication-record.v1.json). This audit is prepared and locally verified; it is not complete until every intended live issue update has been published and read back with section offsets recomputed.

## NW-02 — issue #3

**Actual title:** NW-02 — Implement versioned operator schemas, typed DAGs, and deterministic evaluation

- Source revision: `github.com/bateriusz/nodewerk#3` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:e3da09b716364e8f494b3d02ba184eaea292c90dde73332399ec37218b9ce7c8`
- Original body hash: `sha256:v1:5c3e56399192470729cdd9fede7041832698f1ed35c08e943f637e01bf96709e`
- Updated body hash: `sha256:v1:63375a23d9b9c149452f88dba3b80c44ec19f6442b1a046990960c0d19ea9356`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-03.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-03.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-03.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-03.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-03.md`](../backlog/nw02-nw12-issue-bodies/original/issue-03.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-03.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-03.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Replaces the vague `target profile` hash input with the stable capability-profile ID and canonical requirements digest, and explicitly excludes GPU generation, marketing model, adapter description, and PCI device ID from semantic hashing; states that target identity and compiler acceptance are GPU-independent.

**Exact replacement text:**

```markdown
Represent projects as immutable snapshots of typed, multi-output DAGs. Schemas declare stable operator and port identifiers, semantic versions, canonical parameters, explicit seeds, frequency, residency, resource descriptors, and lowering hooks. Initially support scalar/vector/matrix values, curves, shader expressions, texture/buffer/mesh recipes, material descriptions, flat scene tables, camera/light values, and render images. Distinguish pure ShaderExpr composition from resource-producing GpuPass operations and CPU control expressions. Frequencies are compile-time, initialization, and frame; history semantics are reserved for NW-09 and rejected in initial exports. Residency conversions and readbacks are explicit. Hash canonical field encodings, versions, upstream hashes, target-profile identity (the stable capability-profile ID and canonical requirements digest), and compiler settings using a wide content digest; never hash struct padding, raw parameter memory, GPU generation, marketing model name, adapter description, or PCI device ID. Semantic floating-point parameters must be finite, with declared normalization rules. Node IDs and editor layout must not influence randomness or exported bytes unless explicitly supplied as semantic seed inputs. Provide CPU reference evaluation for control/math and practical procedural oracles, not a mandatory complete CPU renderer. The common profile identity is `nw.common.d3d12.fl12_0.sm6_0.v1` with requirements digest `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`. A target profile is identified only by that capability contract, never by GPU generation or model metadata; semantic hashing and compiler acceptance therefore remain GPU-independent.
```

### Scope — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds rules that target-profile identity comes only from the capability-profile ID/digest and that identical semantic hashes and compiler acceptance must be produced on a GPU-less host.

**Exact replacement text:**

```markdown
- Reject ordinary cycles, unknown schema versions, incompatible ports, implicit readbacks, and illegal frequency transitions.
- Evaluate identical semantic inputs reproducibly regardless of worker scheduling or serialization order.
- Changing one input invalidates only its dependent closure.
- Resolve target-profile identity from the stable capability-profile ID and canonical requirements digest; never derive it from GPU generation, marketing name, adapter description, or PCI device ID.
- Produce identical semantic hashes and compiler acceptance on a GPU-less host for identical capability-profile identity and semantic inputs.
- Extensions register authoring/compiler implementations; the player has no plugin ABI.
```

### State, configuration, and API changes — changed

**Reason:** Adds the capability-profile ID/digest as the stored target-profile identity and keeps observed adapter and GPU-generation metadata outside canonical semantic encodings.

**Exact replacement text:**

```markdown
- Introduce GraphSnapshot, OperatorSchema, LogicalValue, EvaluationContext, and Diagnostic contracts.
- Record explicit quantization policies separately from full-precision authoring values.
- Store the capability-profile ID and canonical requirements digest as the target-profile identity record; keep observed adapter and GPU-generation metadata outside canonical semantic encodings.
- Define versioned migrations for supported authoring documents.
```

### Recovery semantics — changed

**Reason:** Adds rejection of unknown or malformed capability-profile IDs/digests through the existing schema-version/migration path instead of interpreting GPU generation or model names.

**Exact replacement text:**

```markdown
- Invalid graph edits leave the last valid snapshot available.
- Missing or corrupt cache entries are treated as misses.
- Unknown operator versions require an explicit migration rather than silent reinterpretation.
- Unknown or malformed capability-profile IDs or digests are rejected through the existing schema-version/migration path, not reinterpreted by GPU generation or model name.
```

### Compatibility and safety constraints — changed

**Reason:** Adds the constraint that compiler acceptance requires no GPU execution and that hardware qualification belongs to downstream evidence tasks.

**Exact replacement text:**

```markdown
- GPU floating-point results have numerical/perceptual tolerances; explicit seeds alone do not guarantee bitwise images.
- No D3D12 resource pointers appear in graph serialization or logical types.
- Shader expressions describe supported operations rather than arbitrary code embedded in release data.
- Compiler acceptance requires no GPU execution; hardware execution and qualification belong to downstream evidence tasks.
```

### Test plan — changed

**Reason:** Adds tests that changing only GPU model/generation metadata does not change semantic hashes or target-profile selection, and that compiler acceptance works on a GPU-less host.

**Exact replacement text:**

```markdown
- Test valid and invalid type/frequency connections, multi-output nodes, cycles, and bounded resource dimensions.
- Test canonical serialization, schema migration, stable seed derivation, and cache invalidation.
- Verify node reordering and editor-layout changes leave semantic hashes unchanged.
- Verify that changing only GPU model/generation metadata, adapter description, or observed hardware does not change canonical semantic hashes or target-profile selection.
- Verify identical compiler acceptance and semantic hashes on a GPU-less host for the common capability-profile identity and digest.
- Test dependency diagnostics against source node and port identifiers.
```

### Regressions — changed

**Reason:** Adds a regression guard that marketing names, generation labels, adapter descriptions, and PCI identifiers cannot alter semantic artifact identity or target-profile selection.

**Exact replacement text:**

```markdown
- Operator semantic changes require version changes.
- Editor-only edits must not alter exported semantic identity.
- Canonical hashes must not vary with allocator layout or worker completion order.
- GPU marketing names, generation labels, adapter descriptions, or PCI identifiers must not alter semantic artifact identity or target-profile selection.
```

### Acceptance criteria — changed

**Reason:** Adds an acceptance clause requiring semantic hashes and compiler acceptance to derive from the capability-profile ID/digest and to pass with no GPU present.

**Exact replacement text:**

```markdown
- A textual fixture graph validates and evaluates without a GPU.
- Invalid fixtures fail with precise dependency diagnostics.
- Repeated evaluations and canonical serialization are stable across clean runs.
- All initial operator kinds declare frequency, residency, and seed behavior.
- Semantic hashes and compiler acceptance derive from the capability-profile ID and canonical requirements digest, not GPU generation or model metadata, and pass with no GPU present.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B ownership and the global prerequisite, and records that no GPU-execution or exact-SKU compiler prerequisite is introduced.

**Exact replacement text:**

```markdown
Dependencies:
- NW-01 supplies language, build, and target-profile contracts.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No GUI or runtime operator interpreter.
- No executable feedback, mutable scene hierarchy, or general scripting language.
- No GPU execution requirement or exact-SKU compiler prerequisite is introduced.
```

---

## NW-03 — issue #4

**Actual title:** NW-03 — Build offline shader lowering and a bounded release-package format

- Source revision: `github.com/bateriusz/nodewerk#4` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:4fd73fd37b1bc7a222ae5aa48d7f6d9ed05e5b9ca9ff769dfff0b7e9b4cf3561`
- Original body hash: `sha256:v1:be156db25c9b4bdd6d8abc2653a3979ad286d7969cc6f005dc537e5b56c0c6da`
- Updated body hash: `sha256:v1:24cd84cc0e141c538da8d13278ea59be7fd497ebdbda2e6d3d7dea7ac2e65f66`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-04.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-04.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-04.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-04.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-04.md`](../backlog/nw02-nw12-issue-bodies/original/issue-04.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-04.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-04.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Replaces generic capability requirements with exact emitted-only fields: profile ID/digest, feature level, shader model, binding/heap tiers, required DXGI formats and limits, emitted shader targets, runtime requirements, and optional-profile identity; rejects unknown or malformed records at existing validation boundaries without redesigning the package.

**Exact replacement text:**

```markdown
Lower ShaderExpr DAGs into a small typed expression IR and generate specialized HLSL entry points. Compile offline using pinned DXC; retain source maps and reflection as development sidecars, while release blobs retain all metadata needed for valid DXIL execution. Deduplicate identical complete shader blobs and root signatures; do not assume functions can be shared across independent blobs without linking. Use a small fixed binding convention per pass family and explicit resource bindings, avoiding runtime reflection. Define .nwp v1 with a little-endian versioned header, checked section directory, capability requirements, resource descriptors, initialization/frame instructions, parameter/timeline data, shader blobs, and integrity checksum. Instructions describe supported GPU/CPU execution primitives rather than authoring nodes. Package capability requirements are exact and emitted-only: capability-profile ID and canonical requirements digest, D3D12 feature level, shader model, resource binding/heap tiers, required DXGI formats and limits, emitted shader targets, runtime requirements, and any declared optional-profile identity. They are never GPU marketing names, PCI device IDs, coverage-tier labels, or adapter-description substrings. Unknown, missing, or malformed requirement records are rejected by the existing package-validation boundaries before allocation. This is an additive v1 capability record; it does not redesign the package format. Start with raw payloads; compression requires a later measured net saving including decoder code.
```

### Scope — changed

**Reason:** Adds the exact emitted capability-requirements section inside the existing `.nwp` v1 header and validation path, avoiding a package-format redesign.

**Exact replacement text:**

```markdown
- Expression fusion and HLSL generation for a small math/noise fixture.
- Offline DXC compilation, validation, stripping, and shader cache.
- Exact emitted capability-requirements section within the existing .nwp v1 header and validation path.
- Release package writer, reader, and inspection tool.
- Development source maps and shader diagnostics.
```

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds exact emitted-requirement encoding, validation-boundary rejection of unknown, missing, or malformed records, and a prohibition on inferring capability from GPU names, tiers, or PCI identifiers.

**Exact replacement text:**

```markdown
- Only reachable shader entry points and their dependencies are emitted.
- Package parsing validates counts, ranges, checked arithmetic, instruction references, and decoded-size limits before GPU submission.
- Release packages contain no editor names, source paths, reflection tables, or source text.
- Identical inputs and pinned tools yield identical packages.
- Encode only exact emitted capability requirements (profile identity/digest, feature/format/limit/shader/runtime requirements) and reject unknown, missing, or malformed requirement records through existing validation boundaries before allocation.
- Never infer capability from GPU generation, marketing name, tier label, or PCI device identifier.
```

### State, configuration, and API changes — changed

**Reason:** Extends the v1 capability-requirements record with profile identity/digest, exact queried features, formats and limits, shader targets, runtime and toolchain requirements, and optional-profile identity without changing the container structure.

**Exact replacement text:**

```markdown
- Define package version 1 and internal pass/binding contracts.
- Extend the v1 capability-requirements record with capability-profile ID, canonical requirements digest, exact queried features/formats/limits, shader targets, runtime/toolchain requirements, and declared optional-profile identity; do not change the package container structure.
- Introduce nodewerkc validate, compile-package, and inspect commands.
- Record compiler/profile fingerprints in build reports without unnecessary release strings.
```

### Recovery semantics — changed

**Reason:** Adds rejection of unknown or malformed requirements, profiles, or digests before bulk allocation through the existing package-validation diagnostic category, with no silent downgrade.

**Exact replacement text:**

```markdown
- Failed compilation publishes no partial package.
- Unknown major versions, required sections, instructions, or shader targets are rejected.
- Unknown or malformed capability requirements, profiles, or digests are rejected before bulk allocation through the existing package-validation diagnostic category; no package redesign or silent downgrade is introduced.
- Compilation diagnostics identify the originating expression/node.
- Temporary output is atomically replaced only after validation succeeds.
```

### Compatibility and safety constraints — changed

**Reason:** Adds that requirement encoding is additive within v1 and that unsupported optional profiles remain separate artifacts rather than common-package requirements.

**Exact replacement text:**

```markdown
- Early package versions may be incompatible; archival reproduction retains source graphs and pinned tools.
- Checksums detect corruption, not authenticity.
- A valid package can contain expensive or faulty shaders; parsing validation is not a sandbox for hostile GPU programs.
- No runtime DXC or Slang dependency.
- Capability requirement encoding is additive within v1 and preserves existing package/schema compatibility; unsupported optional profiles are separate artifacts, not common-package requirements.
```

### Test plan — changed

**Reason:** Adds tests for unknown, missing, malformed, and contradictory requirement records at package-validation boundaries and for exact requirement round-trip and inspection.

**Exact replacement text:**

```markdown
- Compile and validate every fixture specialization.
- Test expression fusion against CPU mathematical oracles.
- Test unknown, missing, malformed, and contradictory capability requirement records, profile IDs, and digests at package-validation boundaries before allocation.
- Verify exact emitted feature/format/limit/shader/runtime requirements survive package round-trip and inspection.
- Fuzz malformed package sections, instruction operands, dimensions, and arithmetic.
- Compare package hashes across clean build directories.
```

### Regressions — changed

**Reason:** Adds a guard that no GPU model, generation, tier label, or PCI identifier becomes a package capability requirement or parser branch.

**Exact replacement text:**

```markdown
- Track shader bytes and specialization counts per fixture.
- Reject debug/reflection leakage into release payloads.
- No parser failure may reach command submission.
- No GPU model, marketing generation, tier label, or PCI identifier may become a package capability requirement or parser branch.
```

### Acceptance criteria — changed

**Reason:** Adds acceptance that packages express and inspect exact capability requirements and that malformed or unknown requirements fail through existing validation boundaries without package-format redesign.

**Exact replacement text:**

```markdown
- A generated fused compute shader is packaged reproducibly.
- Malformed-input tests and sanitizer-backed parser fuzzing complete without memory errors.
- Release inspection demonstrates absence of authoring metadata.
- Shader errors map back to graph expressions.
- Packages express and inspect exact capability requirements with profile identity/digest, and malformed or unknown requirements fail through existing validation boundaries without package-format redesign.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds the NW-01A/NW-01B prerequisite chain and records that no package-format redesign or GPU-name capability inference is introduced.

**Exact replacement text:**

```markdown
Dependencies:
- NW-01 provides DXC and capability targets.
- NW-02 provides graph types, expression semantics, and diagnostics.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No runtime source compilation, universal shader, or custom compression codec.
- Slang remains a later toolchain experiment, not a second mandatory shader path.
- No package-format redesign or capability inference from GPU names.
```

---

## NW-04 — issue #5

**Actual title:** NW-04 — Implement the minimal package-driven D3D12 player

- Source revision: `github.com/bateriusz/nodewerk#5` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:e2cc8ebe078f74aaa171ff65f808915de1ca5cb8f8b3139b67eee565b9030c89`
- Original body hash: `sha256:v1:5c1ae6888a9f7f4ed2f66114c87aca306a71b853312daf29be1c1663e526fe31`
- Updated body hash: `sha256:v1:11b29f30f1d423602e2e2f66177c02c5445591964869c95ec6499f680df50680`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-05.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-05.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-05.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-05.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-05.md`](../backlog/nw02-nw12-issue-bodies/original/issue-05.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-05.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-05.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Replaces generic startup checks with exact validation of feature level, shader model, binding/heap tiers, formats and limits, and every emitted optional or profile requirement against the package profile ID/digest; adds representative Ada and Blackwell qualification of the identical common package.

**Exact replacement text:**

```markdown
Implement Win32 lifecycle, capability checks, D3D12 device/swapchain, one direct queue, fences, resource/descriptor tables, package loading, parameter uploads, and a bounded pass executor. Use a small number of frames in flight with explicit upload and resource ownership. The compiler provides logical lifetimes and permitted alias groups; startup queries actual resource size/alignment and realizes placements for the current adapter. Texture allocation requirements are adapter/driver-specific, so fixed portable offsets from slot 3 are invalid [GetResourceAllocationInfo documentation](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-getresourceallocationinfo%28uint_uint_constd3d12_resource_desc%29). Initially use non-aliased placement; NW-05 adds proven reuse. Rendering performs no engine-owned heap or descriptor allocations after initialization, except explicit resize/recreation paths. Diagnostic builds expose timing and capture hooks; release linkage excludes them. Startup capability validation is exact: compare queried D3D12 feature level, shader model, resource binding/heap tiers, required formats/limits, and every emitted optional feature/profile requirement against the package's common-profile identity and canonical requirements digest. Adapter names, GPU marketing generations, tier labels, and PCI identifiers are recorded metadata only and never repair missing evidence or grant eligibility. Qualify the identical common package on one representative compliant desktop Ada device and one representative compliant desktop Blackwell device; missing coverage remains pending.
```

### Scope — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Replaces the generic capability check with exact startup validation and diagnostics naming each unmet requirement, and makes adapter names, marketing generations, tier labels, and PCI identifiers metadata only.

**Exact replacement text:**

```markdown
- Validate exact startup capabilities and package requirement/profile identity before bulk resource allocation, and produce actionable diagnostics naming each unmet requirement.
- Treat adapter descriptions, marketing names, generation labels, and PCI identifiers as metadata only; they cannot establish capability or repair missing evidence.
- Execute initialization once, followed by ordered frame instructions.
- Avoid graph traversal, runtime reflection, readback, and engine-owned per-frame allocations.
- Distinguish engine memory accounting from driver/process totals.
```

### State, configuration, and API changes — changed

**Reason:** Adds structured capability-validation diagnostics carrying the unmet requirement, required and observed values, package profile ID/digest, and the existing exit-code category.

**Exact replacement text:**

```markdown
- Define initialize(package), renderAtTick(tick), resize, and shutdown internal interfaces.
- Define stable exit-code categories for package, capability, allocation, pipeline, and device failures.
- Expose structured capability-validation diagnostics identifying the unmet requirement, required value, observed value, package profile ID/digest, and existing exit-code category.
- Introduce resource-table and per-frame ownership structures.
```

### Recovery semantics — changed

**Reason:** Adds the existing capability-failure category before bulk allocation for missing or unknown requirements and distinguishes missing representative coverage as pending, not a runtime failure or pass.

**Exact replacement text:**

```markdown
- Device removal terminates the release player cleanly; automatic recovery is not required.
- Resize waits for affected work and recreates size-dependent resources.
- Zero-sized/minimized windows suspend presentation safely.
- Partial initialization releases successfully created resources.
- Missing or unknown required capabilities fail with the existing capability-failure category before bulk allocation; missing representative Ada/Blackwell evidence remains a pending coverage state, not a runtime failure or pass.
```

### Compatibility and safety constraints — changed

**Reason:** Adds single-common-package Ada and Blackwell qualification and prohibits adapter-name, marketing-generation, tier-label, and PCI-whitelist predicates.

**Exact replacement text:**

```markdown
- No generic RHI, scene graph, ECS, editor library, or plugin loader.
- Descriptor limits and format support are queried and bounded.
- GPU allocations may occur within the driver; the allocation prohibition concerns engine code.
- The identical common package is used for representative Ada and Blackwell correctness qualification; no per-generation package fork is introduced.
- No adapter-name, marketing-generation, tier-label, or PCI-whitelist compatibility predicate is permitted.
```

### Test plan — changed

**Reason:** Adds tests for exact missing feature, format, limit, and profile failures and diagnostics before allocation, plus representative Ada/Blackwell common-package coverage with pending status when absent.

**Exact replacement text:**

```markdown
- Render a package-only fixture with validation enabled.
- Exercise missing feature level, shader model, binding/heap tier, format/limit, unknown profile/digest, and malformed package capabilities; verify diagnostics name the unmet requirement and occur before bulk allocation.
- Qualify the identical common package on representative desktop Ada and representative desktop Blackwell devices and record exact adapter, driver, OS, capability query, artifact hashes, and unavailable telemetry; missing coverage remains pending.
- Exercise allocation failures, pipeline failures, resize, minimize, and shutdown.
- Verify fence-safe upload reuse and absence of engine-owned per-frame allocations.
- Inspect release imports and linker maps for tooling dependencies.
```

### Regressions — changed

**Reason:** Adds guards that missing coverage remains pending, executed failures remain failures, and startup validation never branches on adapter names, generation labels, or PCI identifiers.

**Exact replacement text:**

```markdown
- Track bootstrap/player byte growth and CPU submission time.
- Validation errors and resource leaks block completion.
- Runtime must not acquire a dependency on compiler-generated source schemas.
- Missing hardware coverage must remain pending and must not be reported as passing; executed failures remain failures.
- Startup validation must not branch on adapter name, generation label, or PCI identifier.
```

### Acceptance criteria — changed

**Reason:** Replaces the generic two-generation smoke test with identical common-package rendering on representative Ada and Blackwell with exact capability and environment evidence, and adds exact startup-diagnostic acceptance.

**Exact replacement text:**

```markdown
- The identical NW-03 common package renders from its package on representative desktop Ada and representative desktop Blackwell devices, with exact capability/profile and environment evidence; missing coverage remains pending.
- Startup capability diagnostics identify exact unmet requirements before bulk allocation and never rely on adapter names or marketing generations.
- A sustained run and resize sequence are validation-clean.
- The release player runs without compiler/editor installation.
- Timing and memory reports are available from the diagnostic build.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B prerequisites and records that no exact-SKU correctness prerequisite exists beyond representative generation coverage.

**Exact replacement text:**

```markdown
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
```

---

## NW-05 — issue #6

**Actual title:** NW-05 — Compile whole graphs into specialized players and explicit resource schedules

- Source revision: `github.com/bateriusz/nodewerk#6` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:dd8620adac2b708eb6282fa0d743a5844a2487087563752d45ea7dc6b9371631`
- Original body hash: `sha256:v1:ddac60a648ffb052dc2b425892d3eb1188a0cccbf4f9805b13fb64ba91f08c5f`
- Updated body hash: `sha256:v1:00ded3ba9c6406478df27d932f4c8944f0e8820784d2e29f5a6d87e4817c985f`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-06.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-06.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-06.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-06.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-06.md`](../backlog/nw02-nw12-issue-bodies/original/issue-06.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-06.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-06.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Keys export target profiles by capability-profile ID/digest instead of GPU generation or model, and requires the player link manifest to contain only reachable feature queries with unused optional implementations, shaders, and dependencies excluded from the common artifact.

**Exact replacement text:**

```markdown
Implement typed lowering, constant propagation, dead-node/dead-output elimination, common-subexpression elimination, initialization/frame partitioning, profitable expression fusion, shader reachability, and generated player linkage. Logical resources declare read/write access and lifetime; the compiler emits transitions and alias eligibility. Startup realizes compatible alias groups using queried requirements, with required aliasing barriers and fence-safe lifetimes. Per-frame transient resources are duplicated or serialized where frames overlap. Generate-versus-bake decisions compare incremental compressed content plus uniquely introduced code/shaders, startup cost, and peak memory; initially use explicit documented choices rather than an unsupported automatic cost oracle. Quantization is field-specific, tolerance-bounded, and reversible to full precision. Build one artifact for one capability profile; alternatives are separately exported artifacts. `nodewerkc export` target profiles are selected by stable capability-profile identity (common profile ID and canonical requirements digest), never by GPU generation or model name. The generated player link manifest contains only feature queries reachable from the exported graph; unused optional implementations, shaders, and dependencies are excluded from the common artifact.
```

### Scope — changed

**Reason:** Adds capability-profile export identity and reachable-content closure as explicit scope for common and optional artifacts.

**Exact replacement text:**

```markdown
- Whole-demo optimization and scheduling.
- Resource lifetime analysis and conservative aliasing.
- Capability-profile export identity and reachable-content closure for common and optional artifacts.
- Generated used-feature link manifest and embedded package.
- Export reports explaining retained code, shaders, data, and dependencies.
```

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds profile-ID/digest export identity, exact reachable requirement emission, exclusion of unused optional content from common exports, and representative schedule and aliasing qualification.

**Exact replacement text:**

```markdown
- The graph is the only source of demo content; no hand-edited package or per-demo player logic.
- Adding an unreachable operator does not change semantic release content.
- Export budgets fail closed while retaining a diagnostic report.
- Fusion is accepted only when correctness is preserved and measured size/performance tradeoffs are recorded.
- Resolve export target identity from the capability-profile ID and canonical requirements digest; emit exact reachable feature/format/limit/shader/runtime requirements into the player link manifest.
- Exclude unused optional implementations, shaders, and dependencies from common exports; no optional profile may raise common capability, dependency, or size baselines.
- Qualify existing resource-schedule and aliasing validation on representative desktop Ada and Blackwell hardware using the identical common artifact.
```

### State, configuration, and API changes — changed

**Reason:** Adds profile ID/digest to export settings and reports in place of GPU generation or model selectors and records the reachable feature-query closure and excluded optional implementations.

**Exact replacement text:**

```markdown
- Add nodewerkc export and inspect-size commands.
- Define export settings for byte ceiling, memory budget, quantization tolerance, target profile, and generation/bake overrides.
- Add capability-profile ID and canonical requirements digest to export settings and reports in place of GPU generation/model selectors; preserve the existing target-profile interface and byte-budget settings.
- Record reachable feature-query closure and explicitly excluded optional implementations in export reports.
- Define logical alias groups rather than serialized adapter-specific offsets.
```

### Recovery semantics — changed

**Reason:** Changes export failure to cover unsupported or unknown capability profiles with a dependency path, and adds pending coverage without changing a successful artifact.

**Exact replacement text:**

```markdown
- Failed optimization, shader compilation, linking, or budget validation leaves the prior complete artifact intact.
- Unsupported or unknown capability profiles fail at export with a dependency path.
- Missing representative hardware coverage remains pending and does not change a successful export artifact.
- Failed specialized runtime initialization exits rather than silently changing rendering quality.
```

### Compatibility and safety constraints — changed

**Reason:** Adds that optional profiles are separate artifacts with exact requirements and budgets and that common exports never contain mandatory optional code, shaders, or dependencies.

**Exact replacement text:**

```markdown
- Single-queue scheduling first; no unproven cross-queue alias reuse.
- A checksum or successful export does not remove runtime bounds checks.
- Build-time extensions are trusted tooling and are never loaded by the player.
- Optional feature profiles are separate artifacts with exact additional requirements and budgets; common exports never contain mandatory optional code, shaders, or dependencies.
```

### Test plan — changed

**Reason:** Adds tests that export identity is profile-based rather than GPU-name-based, that representative Ada and Blackwell aliasing validation is recorded, and that optional additions do not change the common link closure.

**Exact replacement text:**

```markdown
- Golden optimized-IR tests for folding, elimination, frequency partitioning, and quantization.
- Property tests for overlapping lifetimes, compatible alias groups, and frames-in-flight reuse.
- Verify export target selection is determined by capability-profile ID/digest and not by GPU model, generation, tier label, or adapter name.
- Compare aliased and non-aliased fixture outputs under GPU validation on representative desktop Ada and Blackwell hardware; record exact device/capability/artifact metadata and mark missing coverage pending.
- Verify unused optional operator/profile additions do not change the common link closure.
- Export identical graphs in clean directories and compare package and PE hashes.
```

### Regressions — changed

**Reason:** Adds guards that optional additions do not enlarge common exports or requirements and that GPU marketing metadata cannot alter export identity or byte-identical results.

**Exact replacement text:**

```markdown
- Unused feature additions must not increase the fixture's linked feature closure.
- Quantization must remain within declared tolerances.
- Scheduling changes must preserve initialization and frame ordering.
- Optional profile additions must not enlarge common exports or common capability requirements.
- GPU marketing metadata must not alter export target identity or byte-identical results.
```

### Acceptance criteria — changed

**Reason:** Replaces the generic aliasing acceptance with representative Ada and Blackwell schedule and aliasing validation on the identical common artifact and adds profile-keyed export identity and common-content exclusion.

**Exact replacement text:**

```markdown
- A textual graph exports a self-contained executable that generates and displays a procedural image.
- Repeated clean exports are byte-identical under the pinned environment.
- Reports attribute all distributed bytes and identify dependency costs.
- Resource aliasing and schedule validation runs on representative desktop Ada and Blackwell hardware without changing common-artifact output; missing coverage remains pending.
- Export identity is keyed by capability-profile ID/digest, and common exports exclude unused optional implementations, shaders, and dependencies.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B prerequisites and records that no optional implementation or GPU-name predicate may enter the common export.

**Exact replacement text:**

```markdown
Dependencies:
- NW-02 supplies semantic graph evaluation.
- NW-03 supplies shader/package generation.
- NW-04 supplies the runtime and actual allocation requirements.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No general-purpose runtime graph interpreter.
- No universal bake heuristic, unrestricted pass fusion, or advanced renderer.
- No optional implementation or GPU-name predicate may enter the common export.
```

---

## NW-06 — issue #7

**Actual title:** NW-06 — Add procedural mesh, texture, scene, and integer-timeline operators

- Source revision: `github.com/bateriusz/nodewerk#7` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:2d007ed5ac0d362c59854705af51336bb50497cc818addc71f6c002457325475`
- Original body hash: `sha256:v1:1b5d30e943f5b3b15a3e9e29d6521119f21e479ff1d59aafa75b61533ccfa8fc`
- Updated body hash: `sha256:v1:075f4624bdb3476a9bb95be0afd9bfefdc55cfd376f429c84a4fba4663baaf12`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-07.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-07.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-07.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-07.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-07.md`](../backlog/nw02-nw12-issue-bodies/original/issue-07.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-07.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-07.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Adds fixed-tick capture metadata for common artifact hash, profile ID/digest, exact adapter and device, driver, OS and toolchain, queried capabilities, clocks, power, thermal state, cache state, and protocol; adds representative Ada and Blackwell common-artifact coverage with pending missing coverage while preserving tolerances and exclusions.

**Exact replacement text:**

```markdown
Add seeded 256x256 Noise2D material generation, a 64x32 parametric torus mesh generator, procedural placement of 256 instances, flat instance/material tables, a camera curve, and analytic per-frame twist deformation. Generate textures and vertex/index buffers on the GPU during initialization; use conventional indexed instanced rasterization with one indirect draw, analytic lighting, half-resolution bloom, and tonemapping. No CPU content readback occurs during playback. Use 48,000 integer ticks per second, checked conversion from a monotonic clock, and rational offline frame steps. Curves sample absolute ticks rather than accumulated floating-point deltas. Events use ordered IDs and intervals (previousTick, currentTick]; replay/reset semantics are explicit. The first scene is stateless, making direct seek equivalent to evaluation at the requested tick. Reserve a clock-provider interface for later audio without linking an audio library. Each fixed-tick capture records the identical common artifact hash, capability-profile ID/digest, exact observed adapter/device, driver/OS/toolchain, queried capabilities, clocks/power/thermal, cache state, and capture-tool protocol. The same common artifact is used for representative desktop Ada and Blackwell correctness coverage; missing coverage is pending, never a pass. Existing numeric/image tolerances and feature exclusions are unchanged.
```

### Scope — changed

**Reason:** Adds representative Ada and Blackwell fixed-tick correctness captures with artifact and environment provenance to the task scope.

**Exact replacement text:**

```markdown
- Small procedural operator library covering texture, geometry, material, instance scene, camera, animation, and post-processing.
- Integer timeline and bounded quantized curve encoding.
- Conventional graphics and indirect-draw path.
- Representative desktop Ada/Blackwell fixed-tick correctness captures with artifact and environment provenance.
- CPU/high-precision oracles where practical.
```

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds the identical common artifact as the representative Ada and Blackwell correctness input and requires exact device, capability, environment, and artifact provenance.

**Exact replacement text:**

```markdown
- Static resources regenerate from compact parameters and explicit seeds.
- Only frame-dependent parameters and deformation execute each frame.
- Scene assembly produces dense tables without runtime hierarchy traversal.
- Curve quantization records maximum error and allows a full-precision escape hatch.
- Use the identical common artifact for representative Ada and Blackwell fixed-tick correctness coverage; record exact device, capability/profile, environment, and artifact provenance.
```

### State, configuration, and API changes — changed

**Reason:** Adds capture metadata fields for coverage target, evidence role, profile identity/digest, artifact hashes, exact device, driver, OS and toolchain, queried capabilities, clocks, power, thermal state, cache state, and unavailable telemetry.

**Exact replacement text:**

```markdown
- Register the initial resource, scene, material, curve, and post operator schemas.
- Define integer tick, rational capture step, clock provider, and event cursor contracts.
- Define geometry and instance encodings from explicit fields with verified layout assertions.
- Capture metadata carries coverage target, evidence role, capability-profile identity/digest, artifact hashes, exact adapter/device, driver/OS/toolchain, queried capabilities, clocks/power/thermal, cache state, and unavailable telemetry.
```

### Recovery semantics — changed

**Reason:** Adds that missing representative hardware or telemetry yields pending or unverified coverage and is never substituted or reported as passing.

**Exact replacement text:**

```markdown
- Invalid dimensions, indices, curve ranges, and resource budgets fail before dispatch.
- Seek resets event cursors according to documented replay policy.
- Generation or pipeline failures preserve the previous authoring preview and fail standalone initialization.
- Missing representative hardware or telemetry yields pending or unverified coverage; it is never substituted or reported as passing.
```

### Compatibility and safety constraints — changed

**Reason:** Adds that representative hardware is not an exact-SKU requirement and that existing tolerances, exclusions, quality, and resolution requirements remain unchanged.

**Exact replacement text:**

```markdown
- No mandatory mesh shaders, ray tracing, bindless heap indexing, FP16 arithmetic, or wave-size assumptions.
- GPU float comparisons use measured tolerances; integer seed/control outputs may use exact hashes.
- Only final formats with verified device support are emitted.
- Representative hardware is not an exact-SKU requirement; existing numeric/image tolerances, feature exclusions, and quality/resolution requirements remain unchanged.
```

### Test plan — changed

**Reason:** Replaces RTX 40 and RTX 50 fixed-tick captures with representative desktop Ada and Blackwell captures using the identical common artifact and exact provenance, retaining tolerances and pending coverage.

**Exact replacement text:**

```markdown
- Compare generated noise and mesh positions/normals against practical reference calculations.
- Validate mesh topology, index bounds, instance transforms, and resource estimates.
- Test direct seeking, rational capture, event boundaries, duration overflow, and curve error.
- Capture fixed-tick frames on representative desktop RTX 40-series/Ada and RTX 50-series/Blackwell hardware using the identical common artifact; record exact device/capability/profile/environment/artifact provenance, retain tolerances, and mark missing coverage pending.
```

### Regressions — changed

**Reason:** Adds a guard that new capture identity and provenance metadata cannot alter existing numeric or image tolerances or feature exclusions.

**Exact replacement text:**

```markdown
- Changing display frame rate must not change stateless timeline results.
- Static generators must not move into the frame schedule accidentally.
- Adding operators must not enlarge exports that do not use them.
- Capture artifact identity, capability-profile identity, and representative coverage metadata changes must not alter numeric/image tolerances or feature exclusions.
```

### Acceptance criteria — changed

**Reason:** Replaces the generic fixed-tick tolerance check with representative Ada and Blackwell captures of the identical common artifact that meet the existing tolerances, with missing coverage pending.

**Exact replacement text:**

```markdown
- The specified torus-instance graph exports and renders entirely through NW-05.
- Procedural textures and meshes are absent as baked assets from the baseline package.
- Fixed-tick captures on representative desktop Ada and Blackwell hardware meet the existing declared numeric and image tolerances using the identical common artifact; missing coverage remains pending.
- Per-pass time, initialization time, shader bytes, and memory costs are recorded.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B prerequisites and records that no exact-SKU capture or tolerance change is introduced.

**Exact replacement text:**

```markdown
Dependencies:
- NW-05 supplies the complete export/runtime path.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No SDF-to-mesh extraction, particles, history, path tracing, audio implementation, or asset importers.
- Do not expand the initial operator catalog beyond the proof needs.
- No exact-SKU capture requirement or tolerance change is introduced.
```

---

## NW-07 — issue #8

**Actual title:** NW-07 — Deliver and benchmark the 256 KiB architectural proof

- Source revision: `github.com/bateriusz/nodewerk#8` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:479b0778fd2e7d635ce8171c5c3a7614e2b42dfb5db3a4147b895f6e49fb0b74`
- Original body hash: `sha256:v1:4774c208d7461228090e4a81639cd89667b4d719d4321986036518c1746e057f`
- Updated body hash: `sha256:v1:16ce897c050f4f6c0f8960d9675280828aadbb108140c3e6b9c617868fdb5e8e`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-08.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-08.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-08.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-08.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-08.md`](../backlog/nw02-nw12-issue-bodies/original/issue-08.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-08.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-08.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Separates representative Ada and Blackwell correctness from versioned `nw07_perf_ref_v1` certification, adds the reference board, CPU, host, environment, and protocol designation plus per-run strict gates, preserves all existing size, startup, memory, image-quality, and resolution budgets, and rejects non-reference substitution and surrogate thresholds.

**Exact replacement text:**

```markdown
Freeze a 20-second looping production using NW-06: 256 seeded torus instances, generated noise material, animated camera and twist, analytic lighting, bloom, and tonemapping. Export one Windows x64 executable with embedded package and precompiled shaders. Set a complete-distribution ceiling of 262,144 bytes, with 131,072 bytes as a stretch measurement only. Require process-start-to-first-present within 2 seconds and peak engine-committed GPU resources within 512 MiB.

Separate correctness coverage from performance certification. Correctness uses the identical common artifact on one representative compliant desktop Ada device and one representative compliant desktop Blackwell device at 1920x1080, recording exact observed hardware, capability-profile ID/digest, artifact hashes, driver/OS/toolchain, queried capabilities, cache state, and clocks/power/thermal. Representative devices qualify their generation; they do not establish equal performance or exhaustive SKU coverage. Flagship or higher-tier results do not substitute for representative generation coverage or the reference gate. Missing generation coverage remains pending.

Performance is certified only under the versioned contract `nw07_perf_ref_v1`: 1920x1080 p99 GPU frame time and p99 CPU submission time each strictly less than 16.67 ms on the designated desktop NVIDIA GeForce RTX 4060 reference. The exact reference board, CPU/host, OS build, driver, power/thermal conditions, clocks, cache conditions, resolution, quality settings, measurement tools, percentile method, and run protocol must be designated and frozen before any certifying measurement. The protocol is a five-second warm-up followed by three uncapped 60-second runs; every run must independently satisfy both strict p99 gates, and raw per-run samples and percentile methodology must be retained. Separately report displayed frame intervals. Run clean-start measurements without an application pipeline cache and distinguish cold driver-cache from warm results.

An RTX 4060 Ti, any other Ada device, or any Blackwell device may provide correctness and its own informational performance series but cannot certify `nw07_perf_ref_v1`. The historical NW-01 RTX 4060 Ti capture is not a qualifying RTX 4060 NW-07 result. Missing reference hardware leaves certification pending; it does not pass by substitution and does not block NW-01A. Maintain a separate matched-environment Blackwell development/performance series with no new absolute Blackwell gate. A replacement reference or normalized method requires a new contract version with independently justified thresholds, documented validation uncertainty, and bridge measurements where feasible; it never retroactively passes v1. Do not adopt 13.5 ms, 12.00 ms, theoretical-throughput equivalence, or an unvalidated normalization formula.

Record process RAM, driver memory, startup pipeline time, generation time, image-quality metrics, and 1440p scaling even where they have no hard ceiling. Preserve existing image-quality and resolution requirements; do not weaken them silently.
```

### Scope — changed

**Reason:** Adds the separation of representative common-artifact correctness from `nw07_perf_ref_v1` certification and the reference host, protocol, raw-sample, and pending evidence states.

**Exact replacement text:**

```markdown
- Canonical graph, reproducible export recipe, and release artifact.
- Separate representative common-artifact correctness coverage on desktop Ada and Blackwell from `nw07_perf_ref_v1` certification.
- Versioned performance-reference contract, reference host/protocol designation, raw samples, and pending/unverified evidence states.
- Continuous size, visual, startup, memory, and frame-time regression checks.
- Clean-system deployment and fixed-tick capture evidence.
- Architecture feasibility report.
```

### Affected components — changed

**Reason:** Adds the performance-reference contract record and evidence-role metadata as components, separating certification provenance from correctness coverage.

**Exact replacement text:**

```markdown
- Proposed sample production
- Proposed benchmark/capture harness
- Proposed performance-reference contract record and evidence-role metadata
- Proposed CI release gates
```

### Required behavior — changed

**Reason:** Separates correctness coverage from performance certification, adds per-run strict gate and non-reference or mismatched rejection, and makes missing reference evidence pending rather than a pass or substitution.

**Exact replacement text:**

```markdown
- Correctness qualification uses the identical common artifact on representative desktop Ada and Blackwell hardware; performance certification is separately evaluated only under `nw07_perf_ref_v1`.
- The artifact is produced exclusively from the graph and pinned build inputs.
- All required non-system bytes count toward the ceiling.
- A missed gate leaves the milestone incomplete; changing a budget or reference requires an explicit architecture revision and, for the performance reference, a new contract version.
- Non-reference, stale, or mismatched-host measurements remain available but cannot certify the gate.
- Missing reference hardware remains pending and cannot be converted into a pass by another SKU, a legacy label, or a theoretical ratio.
- Nightly GPU measurements complement per-change CPU/compiler/size checks.
```

### State, configuration, and API changes — changed

**Reason:** Replaces the generic metric baseline record with explicit `nw07_perf_ref_v1` contract identity, reference host, protocol, metric, operator, cache, and matching fields, raw samples, percentile method, result status, and coverage-role metadata.

**Exact replacement text:**

```markdown
- Freeze the proof graph and benchmark configuration.
- Store performance-contract identity (`nw07_perf_ref_v1`), reference GPU and CPU/host designation, exact metric definitions/operators, resolution/quality, warm-up and run protocol, cache conditions, matching rules, raw samples, percentile method, and result status separately from correctness-coverage metadata.
- Store metric baselines, per-device image tolerance definitions, and coverage-target/evidence-role metadata.
- Provide capture controls in a diagnostic sibling build; measure its overhead separately from release.
```

### Recovery semantics — changed

**Reason:** Makes executed failures remain failures, absent reference hardware remain pending regardless of other measurements, and reference or threshold changes create a new contract version while preserving earlier results.

**Exact replacement text:**

```markdown
- Benchmark failures retain reports and failing captures; an executed failure remains a failure.
- Absent reference hardware yields a pending result; non-reference, stale, or mismatched measurements cannot change that state.
- Corrupt packages, unsupported capabilities, and initialization failures produce actionable exit codes.
- Reference or threshold changes create a new contract version and preserve earlier results and unresolved statuses.
```

### Compatibility and safety constraints — changed

**Reason:** Adds that theoretical throughput is insufficient, that the exact RTX 4060 appears only as the justified `nw07_perf_ref_v1` reference or observed metadata, and that no new absolute Blackwell gate is introduced.

**Exact replacement text:**

```markdown
- Use physical hardware or documented GPU passthrough for GPU acceptance; a generic VM or theoretical throughput ratio is insufficient.
- No developer runtime, runtime shader compiler, or uncounted optional dependency.
- Do not weaken image quality or resolution silently to pass performance.
- An exact model appears only as the explicitly justified `nw07_perf_ref_v1` performance reference or as recorded observed metadata, never as a general compatibility predicate.
- No new Blackwell absolute performance requirement is introduced because Blackwell is the current-generation target.
```

### Test plan — changed

**Reason:** Replaces the generic three-pass test with the verified five-second warm-up plus three uncapped 60-second reference runs, per-run strict p99 gates, raw-sample and percentile retention, representative correctness coverage, and non-reference rejection checks.

**Exact replacement text:**

```markdown
- Run clean-system execution and repeated clean-export hash comparisons.
- Capture representative ticks and compare diagnostic preview with exported-player rendering.
- Run the verified protocol on the designated `nw07_perf_ref_v1` host: five-second warm-up then three uncapped 60-second runs; each run independently must satisfy p99 GPU < 16.67 ms and p99 CPU submission < 16.67 ms, with raw samples and percentile method retained.
- Run the identical common artifact for fixed-tick correctness on representative desktop Ada and Blackwell devices; mark missing coverage pending.
- Verify that RTX 4060 Ti, other Ada, Blackwell, stale-artifact, mismatched-host, and mismatched-protocol captures remain informational or unverified and cannot certify v1.
- Exercise corrupt payload, missing-feature, resize, and device-removal failure paths.
```

### Regressions — changed

**Reason:** Adds guards that faster SKUs, theoretical ratios, legacy labels, and non-reference measurements cannot certify v1, and that missing coverage and executed failures are never relabeled as passes.

**Exact replacement text:**

```markdown
- Fail absolute size/performance gates.
- A faster SKU, theoretical ratio, legacy tier label, or non-reference measurement must not certify `nw07_perf_ref_v1`.
- Flag sustained performance or memory regressions above 10 percent against a matched environment.
- Track smaller size changes without an arbitrary percentage-only failure rule.
- Changes to visual tolerances require explained evidence.
- Missing coverage must not be reported as passing, and executed failures must not be relabeled unavailable.
```

### Acceptance criteria — changed

**Reason:** Replaces the generic 16.67 ms acceptance with per-run strict p99 gates under `nw07_perf_ref_v1`, adds representative Ada and Blackwell correctness, requires reference designation before certification, and keeps missing reference evidence pending without blocking NW-01A.

**Exact replacement text:**

```markdown
- One complete distributable is at most 262,144 bytes.
- First present occurs within 2 seconds under the specified startup protocol.
- Peak engine-committed GPU memory is at most 512 MiB.
- Under `nw07_perf_ref_v1`, 1920x1080 p99 GPU time and CPU submission time are each strictly less than 16.67 ms on the designated desktop RTX 4060 reference in each of three uncapped 60-second runs after a five-second warm-up, with raw samples and percentile methodology retained.
- The identical common artifact reproduces equivalent fixed-tick output on representative desktop Ada and Blackwell hardware without editor dependencies; missing coverage remains pending.
- The exact reference board, CPU/host, environment, protocol, artifact hashes, and result status are designated before certification; RTX 4060 Ti, other Ada, Blackwell, stale, or mismatched measurements cannot substitute.
- Missing reference hardware leaves `nw07_perf_ref_v1` certification pending without blocking NW-01A completion.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B prerequisites, records that the RTX 4060 performance reference does not narrow Ada compatibility, and requires any replacement or normalized reference to be a new contract rather than a retroactive v1 pass.

**Exact replacement text:**

```markdown
Dependencies:
- NW-06 completes the proof content.
- NW-01 measurement contracts and NW-05 export reproducibility remain mandatory.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- This closes the first implementation milestone.
- Audio, full editor UX, temporal simulation, advanced RTX features, and 64 KiB exports are later work.
- The RTX 4060 performance reference does not narrow Ada compatibility or require an exact SKU for correctness coverage.
- A replacement or normalized performance reference is a new contract, never a retroactive v1 pass.
```

---

## NW-08 — issue #9

**Actual title:** NW-08 — Add a minimal authoring editor with incremental GPU caching

- Source revision: `github.com/bateriusz/nodewerk#9` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:555a165d08e2e04173f9a191f40612fe062d8d1718a082565669926a9e983384`
- Original body hash: `sha256:v1:4950160c4c169b5b1a5633855a056015371367d323e6f64ed484f57e3bb0524e`
- Updated body hash: `sha256:v1:24e67d430a06d855e7ee78174654739edbd2e7fedddf0cb1430d30f36d902028`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-09.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-09.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-09.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-09.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-09.md`](../backlog/nw02-nw12-issue-bodies/original/issue-09.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-09.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-09.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Defines device generation as the D3D12 device-instance recreation epoch and adds capability-profile ID/digest plus actual adapter and device identity only where device-resident realizations need it; compiler targets are keyed by capability profile, not RTX marketing generation.

**Exact replacement text:**

```markdown
Build a separate editor with graph connections, parameter inspection, timeline scrubbing, preview, compiler diagnostics, pass timings, and size attribution. Use immutable graph snapshots and generation IDs for asynchronous compilation. Cache GPU realizations by full semantic key plus a device-generation key defined as the D3D12 device-instance recreation epoch. The epoch key additionally includes the capability-profile ID/digest, driver/OS/toolchain identity where relevant, and actual adapter/device identity required for device-resident realizations; it is never an RTX marketing generation. Evict under a configured memory budget only after fences permit destruction. Compiler targets are keyed by capability-profile identity, not GPU model. Reuse compiler lowering and runtime pass execution for preview so GUI and CLI exports remain equivalent. UI libraries, shader compiler libraries, profiling tools, and source maps belong exclusively to authoring targets.
```

### Scope — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds that stale jobs cannot publish across device-recreation epochs and explicitly distinguishes the D3D12 device epoch from GPU marketing generation.

**Exact replacement text:**

```markdown
- A parameter change recomputes only affected resources and dependent passes.
- Obsolete jobs cannot publish results into a newer graph snapshot or a later device-recreation epoch.
- Distinguish device generation (D3D12 device-instance recreation epoch) from GPU marketing generation; stale jobs from a prior epoch cannot publish results into a new device instance.
- Cached resources remain alive until submitted GPU work retires.
- The proof graph can be created and exported without editing serialized text.
```

### State, configuration, and API changes — changed

**Reason:** Adds device-generation cache keys keyed by D3D12 recreation epoch, capability-profile ID/digest, and actual adapter identity where required, while compiler targets stay profile-keyed.

**Exact replacement text:**

```markdown
- Add editor-only layout, selection, undo, and cache-budget state.
- Define evaluation generation tokens and device-generation cache keys keyed by D3D12 device-instance recreation epoch, capability-profile ID/digest, and actual adapter/device identity where device-resident state requires it.
- Keep compiler target keys identified by capability-profile identity, not GPU generation or model metadata.
- Keep editor metadata outside canonical export semantics.
```

### Recovery semantics — changed

**Reason:** Adds that device recreation increments the epoch and invalidates device-resident realizations, while existing disk and compiler-cache ownership is unchanged.

**Exact replacement text:**

```markdown
- Shader errors retain the last valid preview and show source-linked diagnostics.
- Corrupt disk cache entries regenerate.
- Device loss clears GPU realizations and allows explicit preview reinitialization; device recreation increments the device-generation epoch and invalidates device-resident realizations.
- Recovery never silently bypasses or changes graph operators.
- Disk-cache and compiler-cache ownership semantics remain unchanged.
```

### Compatibility and safety constraints — changed

**Reason:** Adds that adapter identity may appear only in device-resident realization keys and that RTX marketing-generation strings cannot become cache, compiler, or capability predicates.

**Exact replacement text:**

```markdown
- Editor dependencies must not appear in player imports or link manifests.
- A cache key includes every relevant compiler, target, and semantic input.
- Cache keys may include actual adapter/device identity only for device-resident realizations; semantic and compiler artifacts remain hardware-model independent except for declared capability-profile identity.
- No RTX marketing generation string may become a cache, compiler, or capability predicate.
- No hard real-time editor latency claim is made without a representative graph benchmark.
```

### Test plan — changed

**Reason:** Adds stale-completion testing across device epochs, profile-change invalidation, and verification that marketing-name metadata does not affect compiler or cache targets and adapter identity is retained only for device-resident keys.

**Exact replacement text:**

```markdown
- Test snapshot cancellation, stale completion across device-recreation epochs, cache eviction, and fence retirement.
- Verify capability-profile changes invalidate compiler/cache targets while GPU marketing-name metadata does not.
- Verify actual adapter/device identity is retained only for device-resident realization keys.
- Compare CLI and GUI export bytes for the same semantic graph.
- Test broken shader edits followed by successful recovery.
- Inspect player dependency closure after editor integration.
```

### Regressions — changed

**Reason:** Adds a guard that device-epoch or capability-profile changes are not conflated with RTX marketing generations.

**Exact replacement text:**

```markdown
- Editor layout edits must not change release output.
- Unchanged graph branches must retain cache hits.
- GPU cache growth must remain bounded.
- Device-generation epoch or capability-profile changes must not be conflated with RTX marketing generations.
```

### Acceptance criteria — changed

**Reason:** Adds acceptance that device generation means a D3D12 recreation epoch, compiler targets are profile-keyed, and device-resident realizations retain actual adapter and device identity.

**Exact replacement text:**

```markdown
- The proof is editable, previewable, and exportable through the GUI.
- GUI and CLI exports are identical under identical settings.
- Failed edits and cancelled jobs cannot replace valid preview state.
- Release byte size remains unchanged by editor-only additions.
- Device generation means a D3D12 device-instance recreation epoch, compiler targets are keyed by capability-profile identity, and device-resident realizations retain actual adapter/device identity.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B prerequisites and records that no GPU marketing generation may define a device epoch or compiler target.

**Exact replacement text:**

```markdown
Dependencies:
- NW-07 establishes a validated export and rendering reference.
- NW-02 supplies immutable snapshots and semantic caching.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No general plugin marketplace, asset browser, scripting console, or elaborate editor polish.
- Editor recovery is independent of the minimal release player failure policy.
- No GPU marketing generation may define a device epoch or compiler target.
```

---

## NW-09 — issue #10

**Actual title:** NW-09 — Introduce explicit temporal state and replayable simulation

- Source revision: `github.com/bateriusz/nodewerk#10` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:1110fb91593dc8018b0995c64550b059ec199396e78fcb1a95b46e5dfa4bdbec`
- Original body hash: `sha256:v1:1cea6bfca133ce5ae45ff8d6b9dcf64bd86f3854aee80ecc64b5ea62f0af9a65`
- Updated body hash: `sha256:v1:2d3f9e03ec00386300c36241bdbe9733b665ae234dfbed40db9a9bb33738daf0`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-10.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-10.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-10.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-10.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-10.md`](../backlog/nw02-nw12-issue-bodies/original/issue-10.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-10.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-10.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Adds checkpoint compatibility over version, tick, semantic/state-layout hash, and capability-profile ID/digest, binds device-resident checkpoints to D3D12 device lifetime and epoch, and limits cross-device correctness to representative Ada and Blackwell tolerances without promising portable GPU-state replay.

**Exact replacement text:**

```markdown
Implement explicit History read/write boundaries: previous-tick state is an external input to the current-tick DAG, writes become visible only after the tick completes, and ordinary cycles remain invalid. Use fixed 120 Hz simulation ticks, exactly 400 timeline ticks each, with ping-pong resources and declared reset values. Add one seeded particle fixture and a small feedback-image fixture. Seeking restores an editor checkpoint or resets and replays from tick zero; release checkpoints are optional packaged data whose byte cost must be reported. A delayed frame processes all required simulation ticks in order; when a work cap is reached, presentation may wait or lag, but simulation ticks are never silently discarded. Checkpoint compatibility is deterministic over checkpoint version, tick, semantic/state-layout hash, and capability-profile ID/digest; device-resident editor checkpoints additionally bind to the actual D3D12 device lifetime/epoch and are invalidated on device recreation. Packaged or disk checkpoints do not promise portable GPU-state replay across devices; cross-device correctness uses representative Ada and Blackwell hardware and declared tolerances.
```

### Scope — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds the distinction between semantic/state-layout compatibility and device-resident lifetime and forbids portable GPU-state replay claims across adapters.

**Exact replacement text:**

```markdown
- Initialization, tick advancement, loop reset, seek, and event replay have explicit semantics.
- Different render frame rates produce equivalent state at the same simulation tick.
- History resources never alias concurrently live current/previous state.
- Stateless exports link no history/checkpoint support.
- Distinguish semantic/state-layout compatibility from device-resident lifetime; checkpoint replay never claims portable GPU-state replay across adapters.
```

### State, configuration, and API changes — changed

**Reason:** Replaces the generic checkpoint hash and device field with version, tick, semantic/state-layout hash, capability-profile ID/digest, and device-lifetime binding.

**Exact replacement text:**

```markdown
- Add History declarations with initial value, state type, reset policy, and fixed-step rate.
- Define checkpoint version, tick, semantic/state-layout hash, capability-profile ID/digest, and device-lifetime binding for device-resident checkpoints.
- Extend the package only through an explicit versioned change.
```

### Recovery semantics — changed

**Reason:** Adds that device recreation invalidates device-resident checkpoints while packaged-checkpoint compatibility remains semantic/state-layout and capability-profile based.

**Exact replacement text:**

```markdown
- Invalid checkpoints are discarded and replay starts from the initial state.
- Backward seek resets event cursors and rebuilds state.
- Device recreation in the editor invalidates device-resident checkpoints; semantic/state-layout and capability-profile compatibility remain the packaged-checkpoint contract.
- Replay errors identify the stateful operator and tick.
```

### Compatibility and safety constraints — changed

**Reason:** Adds representative Ada and Blackwell cross-device tolerance coverage and device-lifetime binding while explicitly avoiding portable GPU-state replay claims.

**Exact replacement text:**

```markdown
- No floating-point atomic reduction whose unspecified order is relied upon for exact reproducibility.
- Cross-device results use declared tolerances on representative desktop Ada and Blackwell hardware; no portable GPU-state replay claim is made.
- Temporal state cannot be disguised as a pure cached output.
- Device-resident state remains tied to its device lifetime.
```

### Test plan — changed

**Reason:** Adds checkpoint compatibility tests across state-layout and profile changes, device-recreation rejection, and representative Ada and Blackwell tolerance coverage with exact metadata and pending missing coverage.

**Exact replacement text:**

```markdown
- Compare direct replay and checkpoint replay at matching ticks.
- Test checkpoint compatibility across semantic/state-layout and capability-profile changes, and verify device-resident checkpoints are rejected after device recreation.
- Qualify existing cross-device tolerance tests on representative desktop Ada and Blackwell hardware with exact artifact/capability/environment metadata; missing coverage remains pending.
- Compare state under 30, 60, and irregular presentation rates.
- Test loop boundaries, backward seeks, event duplication, and long-frame catch-up.
- Run synchronization validation with multiple frames in flight.
```

### Regressions — changed

**Reason:** Adds a guard that checkpoint compatibility rules cannot drive semantic behavior or imply portable GPU-state replay.

**Exact replacement text:**

```markdown
- The stateless proof must retain its original bytes and behavior when history is unused.
- Seeking must not duplicate externally observable events.
- State resources must remain within declared memory budgets.
- Checkpoint compatibility rules must not drive semantic product behavior or imply portable GPU-state replay.
```

### Acceptance criteria — changed

**Reason:** Adds acceptance that checkpoints identify state-layout and capability-profile requirements, device-resident state is lifetime-bound, and cross-device tolerance coverage runs on representative Ada and Blackwell without portable replay claims.

**Exact replacement text:**

```markdown
- Particle and feedback fixtures reproduce equivalent state at fixed ticks.
- Checkpoint and full replay agree within the declared contract.
- Long-frame handling preserves every simulation tick.
- History-free exports contain no stateful execution handlers.
- Checkpoint compatibility identifies semantic/state-layout and capability-profile requirements, device-resident state is tied to its device lifetime, and existing cross-device tolerance coverage runs on representative Ada and Blackwell hardware without portable GPU-state replay claims.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds NW-01A/NW-01B prerequisites and records that no portable GPU-state replay or exact-SKU cross-device prerequisite is introduced.

**Exact replacement text:**

```markdown
Dependencies:
- NW-07 provides the validated player and timeline.
- NW-08 supplies authoring seek and checkpoint integration.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No general asynchronous simulation graph or distributed execution.
- No audio side effects in replay; NW-12 defines their separate policy.
- No portable GPU-state replay or exact-SKU cross-device prerequisite.
```

---

## NW-10 — issue #11

**Actual title:** NW-10 — Engineer compact exports and determine true 64 KiB feasibility

- Source revision: `github.com/bateriusz/nodewerk#11` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:40f2cad601def67d17d7ebad5cf63f05d1214929537a7e68e5302c79c8f183a8`
- Original body hash: `sha256:v1:ec79b7b05aadfd867f8854284a25d3e4413add7bcc6391863307e4429cc9a577`
- Updated body hash: `sha256:v1:3183eaca3fb1ee33c83541c630dc4938f05de9f02969d9ee6507298ebcc67cd0`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-11.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-11.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-11.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-11.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-11.md`](../backlog/nw02-nw12-issue-bodies/original/issue-11.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-11.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-11.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Adds the inherited `nw07_perf_ref_v1` requirement: before and after compact-export measurements use matched hardware, environment, and protocol; faster substitutes cannot certify preservation; missing reference evidence remains pending; and common-artifact correctness retains representative Ada and Blackwell coverage.

**Exact replacement text:**

```markdown
Optimize the existing 256 KiB proof using measured parameter packing, delta references, AoS-versus-SoA layouts, shader specialization/fusion choices, linker elimination, and payload compression. Compare encoded bytes plus decoder code and startup memory/time against raw storage. Validate CRT-free startup only if it produces a net benefit with correct compiler-helper and initialization behavior. Then freeze a deliberately restricted 20-second SDF intro: sphere/torus composition, seeded procedural material, camera curves, and tonemapping, with no audio, mesh pipeline, or hardware RT requirement. Attempt a complete single-file export of at most 65,536 bytes using offline shaders and an explicitly verified x64 packaging toolchain. Do not depend on runtime DXC or assumed x64 Crinkler support. Failure produces the smallest measured artifact and tested blockers, not a proof that 64 KiB is impossible. Preserve larger profiles: 128 KiB compact is a stretch tier, 256 KiB is the proven small-demo tier, 1 MiB is a standard production tier, and larger configured budgets remain supported. Preserve the inherited `nw07_perf_ref_v1` performance contract: every before/after comparison uses matched hardware, environment, and protocol; a faster substitute (including RTX 4060 Ti, other Ada, or Blackwell) cannot certify preservation; missing reference evidence remains pending. Common-artifact correctness retains representative desktop Ada and Blackwell coverage.
```

### Scope — changed

**Reason:** Adds matched before and after performance-evidence inheritance from `nw07_perf_ref_v1` to the task scope.

**Exact replacement text:**

```markdown
- Measured byte reductions and decoder selection.
- Optional freestanding player experiment.
- Matched before/after performance-evidence inheritance from `nw07_perf_ref_v1`.
- Restricted SDF intro and 64 KiB feasibility report.
- Archival reproduction of each compared artifact.
```

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Requires before and after performance comparisons to use matched hardware, environment, and protocol and adds that `nw07_perf_ref_v1` preservation cannot be certified by an RTX 4060 Ti, other Ada, Blackwell, or faster substitute and that missing reference evidence is pending.

**Exact replacement text:**

```markdown
- Every optimization reports before/after distribution size, startup, frame time, and visual error, with performance comparisons measured on matched hardware, environment, and protocol.
- Preserve `nw07_perf_ref_v1`; an RTX 4060 Ti, other Ada, Blackwell, or other faster substitute cannot certify preservation. Missing reference evidence is pending.
- Required auxiliary files and dependencies remain counted.
- Shader validation and package bounds checks remain intact.
- The general profile remains usable when the restricted tier cannot meet its budget.
```

### State, configuration, and API changes — changed

**Reason:** Adds recording of the inherited contract identity, reference host and protocol identity, matching keys, artifact hashes, and result status for every before and after comparison.

**Exact replacement text:**

```markdown
- Add explicit codec and compact-layout identifiers.
- Add a restricted intro profile with a frozen supported operator set.
- Record quantization tolerances and packaging-tool versions.
- Record the inherited performance-contract identity, reference host/protocol identity, matching keys, artifact hashes, and result status for every before/after comparison.
```

### Recovery semantics — changed

**Reason:** Adds that unmatched, stale, or non-reference evidence remains available but cannot certify v1 preservation, with executed failures remaining failures and missing reference evidence pending.

**Exact replacement text:**

```markdown
- An unsuccessful compact export retains the prior valid artifact and byte ledger.
- Decoder corruption or expansion-limit violations fail before GPU use.
- Unsupported packers are excluded rather than changing architecture silently.
- Unmatched, stale, or non-reference performance evidence remains available but cannot certify `nw07_perf_ref_v1` preservation; executed failures remain failures and missing reference evidence remains pending.
```

### Compatibility and safety constraints — changed

**Reason:** Adds a prohibition on unvalidated cross-SKU scaling, theoretical-throughput equivalence, and the 13.5 ms and 12.00 ms surrogate thresholds.

**Exact replacement text:**

```markdown
- No uncounted compiler, SDK, soundtrack, model weights, or external asset.
- No speculative compression ratios or fixed PE-size claims.
- Executable packing must be tested on clean target Windows systems.
- No automatic switch to x86 or removal of security mitigations.
- No unvalidated cross-SKU scaling, theoretical-throughput equivalence, or 13.5 ms/12.00 ms surrogate threshold.
```

### Test plan — changed

**Reason:** Adds matched-hardware, environment, and protocol before and after comparison and representative Ada and Blackwell common-artifact correctness verification.

**Exact replacement text:**

```markdown
- Fuzz codec decoders and test worst-case expansion limits.
- Compare packed/unpacked visuals and runtime metrics.
- Compare before/after compact exports on matched hardware, environment, and protocol and verify common-artifact correctness on representative Ada and Blackwell hardware.
- Reproduce artifacts from clean pinned environments.
- Run clean-system startup and dependency checks for every proposed packaging mode.
```

### Regressions — changed

**Reason:** Extends the NW-07 performance and visual preservation guard to name `nw07_perf_ref_v1` and reject faster-substitute or mismatched-evidence certification.

**Exact replacement text:**

```markdown
- Compact changes must preserve the NW-07 performance and visual contracts, including `nw07_perf_ref_v1`; faster substitute or mismatched evidence cannot certify preservation.
- Compression that increases total distributed bytes is rejected.
- A small-tier failure must not reduce larger-profile functionality.
```

### Acceptance criteria — changed

**Reason:** Adds acceptance that preservation evidence names `nw07_perf_ref_v1`, uses matched before and after environments, retains representative common-artifact correctness, and cannot use a faster substitute.

**Exact replacement text:**

```markdown
- Publish an attributed byte ledger for the optimized proof and restricted intro.
- Either deliver a true artifact of at most 65,536 bytes or publish the minimum measured size, dominant costs, tested alternatives, and a continue/defer decision.
- Any achieved 64 KiB result runs without developer tools or additional uncounted files.
- No claim of universal 64 KiB feasibility is made from the restricted result.
- Performance-preservation evidence names `nw07_perf_ref_v1`, uses matched before/after environments, retains common-artifact correctness coverage, and does not use a faster substitute to certify.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds the `nw07_perf_ref_v1` inheritance, NW-01A/NW-01B prerequisites, and a boundary that compact optimization introduces no new or relaxed performance reference.

**Exact replacement text:**

```markdown
Dependencies:
- NW-07 provides the measured size/performance baseline and `nw07_perf_ref_v1` contract.
- Independent of NW-08 and NW-09.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- 64 KiB is a research outcome, not a prerequisite for completing the engine proof.
- No custom compressor project unless existing measured options demonstrably fail.
- No new or relaxed performance reference is introduced by compact optimization.
```

---

## NW-11 — issue #12

**Actual title:** NW-11 — Evaluate advanced RTX features through bounded, costed experiments

- Source revision: `github.com/bateriusz/nodewerk#12` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:a979c215d0d9c9ea9d8aba495e8ef5686d843cc8c10e13c52b4d6ff35dd9b441`
- Original body hash: `sha256:v1:789be1b8c685ef70647ee1acb7a590fde68e2a4e61b9b89fac2cf4327de73475`
- Updated body hash: `sha256:v1:5e299a03f767825597678d6772ac0953e39b8fd5bb65728aa5d728ec4dbd4de3`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-12.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-12.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-12.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-12.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-12.md`](../backlog/nw02-nw12-issue-bodies/original/issue-12.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-12.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-12.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Replaces the one-common-RTX-40/50-profile wording with a common profile qualified on representative Ada and Blackwell plus separately exported optional profiles, each declaring exact capabilities, shader, runtime, and toolchain requirements, dependencies, and budgets with a separately exported and tested common fallback.

**Exact replacement text:**

```markdown
Maintain one common capability profile qualified on representative desktop Ada and Blackwell hardware and separately exported optional feature profiles. A common-profile export excludes unused optional implementations, shaders, and dependencies. First compare conventional indirect rendering against a mesh-shader implementation of the existing torus scene, then compare an SDF or analytic reflection fixture against a DXR ray-query variant. These are the only initial implementation experiments. Evaluate Vulkan 1.4, asynchronous compute, Work Graphs, SER, opacity micromaps, micromesh/clustered geometry, and Tensor/neural shading through a capability/dependency matrix and content-specific hypotheses; prototype further candidates only through separately scoped follow-up issues. Vulkan feature promotion does not remove the need to query required features [Vulkan extension guidance](https://docs.vulkan.org/spec/latest/chapters/extensions.html). Optional Blackwell-specific paths must declare exact API features and toolchain/runtime requirements, never infer support from a marketing name. Each optional profile declares its exact additional D3D12 capabilities, shader targets, runtime/toolchain requirements, dependencies, and artifact budget. Its fallback is a separately exported and tested common-profile artifact; unsupported optional artifacts exit diagnostically without effect substitution or baseline elevation. DLSS, RTX Kit, model weights, and other SDK components remain optional and fully counted.
```

### Scope — changed

**Reason:** Adds capability-qualified per-device baseline and variant pairs and separately tested common fallback artifacts to the task scope.

**Exact replacement text:**

```markdown
- Mesh-shader versus indexed/indirect rendering experiment.
- Ray-query versus baseline reflection experiment.
- Capability-qualified per-device baseline/variant pairs and separately tested common fallback artifacts.
- Feature, deployment, licensing, and byte-cost matrix for other candidates.
- Accept/reject records for measured variants.
```

### Affected components — changed

**Reason:** Renames capability profiles to optional profiles with exact requirements and budgets and adds common fallback artifacts to the benchmark-fixture component.

**Exact replacement text:**

```markdown
- Proposed optional capability profiles with exact requirements and artifact budgets
- Proposed optional geometry and ray-query lowering
- Proposed feature benchmark fixtures and common fallback artifacts
```

### Required behavior — changed

**Reason:** Requires matched baseline and variant pairs only on capability-qualified declared targets, evaluates the improvement criterion per device and environment, distinguishes unsupported from pending coverage, and excludes optional content from common and fallback exports.

**Exact replacement text:**

```markdown
- Report incremental bytes, shader count, initialization, VRAM, CPU/GPU time, and fixed-quality image results.
- Predeclare each experiment's success criterion; default performance criterion is at least 10 percent lower GPU time at matched quality while remaining within its chosen artifact budget, evaluated separately for each capability-qualified matched device and environment.
- Quality experiments require an explicit measurable image/reference criterion.
- Common-artifact correctness runs on representative desktop Ada and Blackwell hardware; optional profiles run matched baseline/variant pairs only on declared targets whose queried capabilities satisfy the profile.
- Unsupported feature coverage is identified as unsupported; unavailable required coverage remains pending; no optional feature is presumed available on every device.
- Unused advanced paths contribute no code, shaders, or SDK dependencies to common or fallback exports.
```

### State, configuration, and API changes — changed

**Reason:** Adds optional-profile ID/digest, artifact budgets, dependencies, fallback-artifact identity, and pass, fail, pending, or unsupported evidence status.

**Exact replacement text:**

```markdown
- Add exact queried-feature manifests for experimental exports.
- Define optional-profile ID and requirements digest, artifact budgets, dependencies, fallback artifact identity, and evidence status (pass/fail/pending/unsupported).
- Record any required Agility SDK and shader-model versions in the complete dependency ledger.
- Represent fallback as a separately exported tested artifact.
```

### Recovery semantics — changed

**Reason:** Adds that unsupported optional artifacts exit diagnostically without effect substitution or common-baseline elevation.

**Exact replacement text:**

```markdown
- Missing required capabilities produce an early diagnostic and exit.
- Unsupported optional artifacts exit diagnostically without substituting a different effect or elevating the common baseline.
- Feature initialization failure does not silently substitute a different effect.
- Unsuccessful experiments remain documented without becoming baseline requirements.
```

### Compatibility and safety constraints — changed

**Reason:** Adds that no optional feature is presumed available across a generation and that common and fallback artifacts remain mandatory and separately tested.

**Exact replacement text:**

```markdown
- No mandatory mesh, RT, SER, Work Graphs, neural shading, or vendor SDK in the common profile.
- No second production backend merely to exercise a feature.
- Microbenchmark gains alone do not justify adoption if end-to-end frame time or distribution cost regresses.
- No optional feature is presumed available on every device in either generation; common exports and fallback artifacts remain mandatory and separately tested.
```

### Test plan — changed

**Reason:** Replaces the exact RTX 4060 paired-test prerequisite with common-artifact Ada and Blackwell runs, capability-qualified matched optional pairs, per-device improvement evaluation, and unsupported, pending, and fallback checks.

**Exact replacement text:**

```markdown
- Run the common artifact on representative desktop Ada and Blackwell hardware. For each optional profile, run matched baseline/variant pairs on declared targets whose queried capabilities satisfy the profile; evaluate the improvement criterion separately per matched device/environment.
- Verify unsupported-feature diagnostics, pending unavailable required coverage, and separately exported and tested common fallback artifacts.
- Test absent-feature manifests and zero-cost stripping.
- Measure both cold initialization and steady-state execution.
- Validate synchronization and resource lifetimes for each variant.
```

### Regressions — changed

**Reason:** Replaces the RTX 40 and RTX 50 artifact guard with one that prevents optional-profile improvements from altering common or fallback artifacts and prevents unsupported or pending outcomes being reported as passes.

**Exact replacement text:**

```markdown
- The common artifact's bytes and dependencies must remain unchanged.
- Optional-profile improvements must not silently alter the common or fallback artifacts, and unsupported or pending outcomes must not be reported as passes.
- Any accepted feature must retain a reproducible comparison fixture.
```

### Acceptance criteria — changed

**Reason:** Adds acceptance for representative common coverage on both generations, capability-qualified per-device optional claims, unsupported-versus-pending distinction, and separately tested common fallback artifacts.

**Exact replacement text:**

```markdown
- Publish measured accept/reject decisions for mesh shaders and ray queries.
- Publish a bounded follow-up matrix for all remaining named technologies.
- Accepted variants have explicit capabilities, budgets, and regression tests.
- Rejected experiments add no mandatory runtime dependency.
- Representative common-artifact coverage exists for both desktop Ada and Blackwell; every optional-profile claim is capability-qualified and evaluated per matched device/environment.
- Unsupported features are distinguished from pending required coverage, and every optional path retains a separately tested common fallback artifact.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds `nw07_perf_ref_v1`, NW-01A/NW-01B prerequisites, and a boundary against cross-SKU performance equivalence or optional-feature baseline elevation.

**Exact replacement text:**

```markdown
Dependencies:
- NW-07 supplies the baseline renderer, benchmark protocol, and `nw07_perf_ref_v1`.
- NW-09 is required only for a future temporal/stateful experiment.
- Independent of NW-10's 64 KiB result.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No omnibus implementation of every RTX feature.
- No claim that Vulkan, D3D12, FP16, mesh shaders, or neural techniques are universally smaller or faster.
- No cross-SKU performance equivalence or optional-feature baseline elevation.
```

---

## NW-12 — issue #13

**Actual title:** NW-12 — Add optional procedural audio through the authoritative clock boundary

- Source revision: `github.com/bateriusz/nodewerk#13` in `plan-1e6c8c83858cb79fbd34a36a`
- Source refinement hash: `sha256:v1:db2a021490f80e68d5fe0f0d42b1d2e4c9c2bacff9aa6508e2396cc7952968ac`
- Original body hash: `sha256:v1:731b912c3196b8d3a082381a7e68c188fae4a6fd1a7e3a99a2eb0cccdab62348`
- Updated body hash: `sha256:v1:546cf64bd24fc57ed9218894e6ff28a78b7689b6c081cf7b88f709dacaa77cd2`
- Original body (exact): [`docs/backlog/nw02-nw12-issue-bodies/original/issue-13.body.md`](../backlog/nw02-nw12-issue-bodies/original/issue-13.body.md)
- Updated body (exact): [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-13.body.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-13.body.md)
- Original document: [`docs/backlog/nw02-nw12-issue-bodies/original/issue-13.md`](../backlog/nw02-nw12-issue-bodies/original/issue-13.md)
- Updated document: [`docs/backlog/nw02-nw12-issue-bodies/updated/issue-13.md`](../backlog/nw02-nw12-issue-bodies/updated/issue-13.md)

### Goal — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Source context — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Final technical design — changed

**Reason:** Adds an explicit GPU-policy note that synthesis, the authoritative clock, WASAPI, PCM and offline rendering, and audio acceptance are hardware-neutral and introduce no GPU generation, exact-SKU, capability-profile, or performance-reference requirement.

**Exact replacement text:**

```markdown
Introduce an optional audio provider that produces PCM, reports an authoritative playback sample position, and supports deterministic offline rendering. Convert sample position to the existing 48,000-tick timebase with integer rational arithmetic when device sample rate differs. Use a small synthesis fixture and WASAPI output behind compile/export selection; evaluate any third-party synth for license, CPU cost, and complete byte contribution before adoption. Audio rendering uses its own preallocated buffers and communicates timing through a narrow thread-safe interface. Scene events and audio scheduling share tick semantics, while editor seeking explicitly stops/resets audio and reconstructs the requested state rather than replaying audible side effects. GPU-policy note: this task's synthesis, authoritative clock, WASAPI output, PCM/offline rendering, and audio acceptance requirements are hardware-neutral. It introduces no GPU generation, exact-SKU, capability-profile, or performance-reference requirement; its visual alignment check inherits the validated common renderer and is bounded by display refresh. No GPU-policy change beyond the global prerequisite is required.
```

### Scope — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Affected components — reviewed-no-policy-change

**Reason:** Reviewed, no policy change required: the affected components were checked for indirect exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumptions; the existing component list remains correct.

### Required behavior — changed

**Reason:** Adds a hardware-neutrality rule forbidding GPU generation, exact-SKU, capability-profile, or performance-reference predicates in audio.

**Exact replacement text:**

```markdown
- Silent exports retain the existing monotonic clock and contain no audio imports, code, or song data.
- Audio-enabled playback follows the authoritative sample clock.
- Offline capture reproduces the same event/sample mapping.
- Real-time audio callbacks perform no allocation or blocking compilation.
- Preserve hardware neutrality: audio introduces no GPU generation, exact-SKU, capability-profile, or performance-reference predicate.
```

### State, configuration, and API changes — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Recovery semantics — unchanged

**Reason:** No change required: this section contains no exact-model, generation-name, performance-reference, capability-identity, or optional-artifact assumption; its existing text remains correct under ADR-0006.

### Compatibility and safety constraints — changed

**Reason:** Adds that audio adds no GPU capability, generation, SKU, or performance-contract requirement to common or fallback artifacts.

**Exact replacement text:**

```markdown
- No compulsory external synth, codec, or soundtrack dependency.
- Any reused implementation requires a documented compatible license.
- Audio-thread state is bounded and independent of editor worker jobs.
- Audio remains hardware-neutral; it does not add GPU capability, generation, SKU, or performance-contract requirements to common or fallback artifacts.
```

### Test plan — changed

**Reason:** Adds verification that audio changes introduce no GPU model, generation, capability-profile, or performance-reference predicate.

**Exact replacement text:**

```markdown
- Test rational sample/tick conversion across device rates and long durations.
- Compare deterministic offline PCM and event schedules.
- Measure audiovisual alignment using a synchronized impulse/flash fixture.
- Verify silent-export dependency and byte identity.
- Verify audio additions introduce no GPU model, generation, capability-profile, or performance-reference predicate.
```

### Regressions — changed

**Reason:** Adds a guard that audio additions cannot introduce GPU-specific capability, generation, or performance dependencies.

**Exact replacement text:**

```markdown
- Silent artifacts must not grow when audio support is added.
- Timeline semantics must remain unchanged for existing graphs.
- Underrun rates and audio CPU use become tracked metrics.
- Audio additions must not introduce GPU-specific capability, generation, or performance dependencies.
```

### Acceptance criteria — changed

**Reason:** Adds acceptance that audio remains hardware-neutral and needs no GPU-policy qualification beyond the existing common-artifact representative coverage.

**Exact replacement text:**

```markdown
- The fixture plays synchronized procedural audio with visual event alignment within one displayed frame.
- Offline event-to-sample mapping is deterministic.
- Silent exports strip the entire audio closure.
- Publish synthesis, song, output-layer byte costs and CPU measurements.
- Audio acceptance remains hardware-neutral and requires no GPU-policy qualification beyond the common artifact's existing representative coverage.
```

### Decomposition dependencies and boundaries — changed

**Reason:** Adds the hardware-neutral audio contract statement, NW-01A/NW-01B prerequisites, and records that NW-12 makes no GPU qualification claim and that its former hardware-qualification mapping is corrected in ADR-0005.

**Exact replacement text:**

```markdown
Dependencies:
- NW-07 supplies the stable integer timeline and export closure.
- NW-09 is required only when audio-enabled content uses temporal simulation.
- This task confirms the hardware-neutral audio contract and introduces no GPU-policy requirement.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- Post-proof milestone; audio is not part of NW-07 or the required NW-10 intro.
- No full digital audio workstation, general codec library, or mandatory beat-based clock.
- No GPU qualification claim is made by NW-12; the repository mapping that labeled it a hardware-qualification task is corrected in ADR-0005.
```

---
