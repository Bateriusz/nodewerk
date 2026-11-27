# Operator coverage and implementation roadmap

This is the planning method accepted for [NW-13](https://github.com/Bateriusz/nodewerk/issues/36)
on 2026-11-27. It guides the expansion of nodewerk after NW-11/NW-12; it
does not claim that the planned operators or optional integrations are implemented.
The initial implementation assessment uses nodewerk revision
`1c9e1559c8d43530c7b213461c6719b6aa43ae35`.

## Three linked catalogs

1. **Historical reference.** Preserve the complete inventory within each declared
   source scope, including source revision, variant, operator identity, ports,
   parameter metadata and extraction limitations. Keep historical entries when a
   modern implementation makes the original algorithm unnecessary.
2. **Authoring capabilities.** Describe what a production author can construct,
   control, animate and compose. Map reference operators or whole subgraphs to
   nodewerk operators or subgraphs. A mapping can be many-to-many.
3. **Implementations.** Associate those capabilities with compiler/CPU work,
   compute, rasterization, mesh shaders, DXR and eligible pretrained inference.
   Backend selection belongs to compilation/export and capability profiles.

The machine-readable starting point is in
[`references/operator_catalog`](../references/operator_catalog/README.md):

- [`werkkzeug3.json`](../references/operator_catalog/werkkzeug3.json) and
  [`werkkzeug4.json`](../references/operator_catalog/werkkzeug4.json) preserve
  extracted reference declarations;
- [`capability_map.json`](../references/operator_catalog/capability_map.json)
  separates current nodewerk functionality from proposed semantic additions;
- [`technology_candidates.json`](../references/operator_catalog/technology_candidates.json)
  records eligibility and role of modern implementation candidates.

An inventory of declarations is not a count of enabled operators in every
historical build. It is also not evidence of usage in a production. Read the
scope, conditional-definition and extraction notes in each dataset before
interpreting its counts. RG2, GenThree, audio tools and other reference engines
have their own provenance; they must not be silently counted as Werkkzeug.

## Replacement decisions

Assess a required use at its output boundary and in the downstream graph. A
replacement preserves the required result, editable controls, animation,
composition and temporal behavior. An independently authored modern production
need not reproduce the historical renderer pixel for pixel.

Use these decisions when reviewing a concrete reference entry or subgraph:

| Decision | Meaning |
| --- | --- |
| Preserve capability | Provide the needed authoring function in nodewerk. |
| Consolidate | Several historical variants share a smaller composable vocabulary. |
| Replace implementation | Preserve the function while omitting an obsolete algorithm. |
| Partial coverage | Name the supported uses and the unresolved semantic difference. |
| Defer | Keep the gap visible, with its production need and reason for deferral. |
| Omit | Record why the behavior is unnecessary for the selected product scope. |

Record **functional coverage** separately from **implementation state**.
Unreviewed reference mappings, absent engine functionality, an experimental
backend and a reusable implementation are different facts. A source declaration,
a schema name, or a fixture-specific shader does not establish reusable support.
No coverage percentage is inferred from keyword matches or declaration counts.

The first curated map groups capabilities. It deliberately leaves exact
operator/subgraph equivalence and demo reachability unreviewed. Subsequent
review records should cite the inventory keys and the exact graph/page/shot,
identify the nodewerk subgraph and preserve any limitations. Changes to controls,
camera position or downstream consumers provide useful focused checks of a
replacement; a matching still image alone is insufficient.

## Geometry and composition

Implicit fields/SDFs and polygon meshes may coexist behind shared scene,
material, transform and rendering semantics. Use the representation appropriate
to the required construction and its consumers.

- SDF primitives, CSG and domain deformations cover useful construction patterns.
  They do not automatically preserve polygon selections, topology, UVs, material
  regions, extrusion or subdivision behavior.
- Mesh shaders change geometry execution. Modeling functions still need defined
  inputs, outputs and controls.
- Keep scene composition, mesh aggregation and solid boolean operations
  distinct. Assembling objects must preserve their identities, transforms and
  material bindings; it does not imply a CSG union.
- Preserve procedural texture and material operations even when their execution
  moves to GPU compute. Reconstruction and denoising do not replace authored
  masks, blur, bloom, distortions or feedback.
- Perform static work in the compiler or initialization when appropriate.
  Per-frame execution and temporal state remain explicit.

The required bounded implicit vocabulary in NW-13 is an initial capability
cluster. The reference audit can justify focused mesh additions beside it.
Unselected topology/UV/skeletal capabilities remain explicit gaps or follow-up
work; the catalog does not make a full DCC or a port of every old operator an
NW-13 requirement.

## Modern backends and neural policy

An author specifies shadows, reflection, geometry and lighting behavior.
Mesh shaders, DXR, Work Graphs and vendor SDKs are implementation choices.
Common-profile implementations remain available under the existing NW-13
policy. They can use a contemporary conventional algorithm; they do not have to
reproduce the historical implementation to qualify as a fallback.

The training boundary is **no nodewerk-owned model training**:

- Allow fixed vendor-supplied, production-independent pretrained inference.
- Exclude dataset preparation, backpropagation, fine-tuning, per-production or
  per-asset model/latent fitting, and online/runtime model training.
- Evaluate the concrete model and integration path. The presence of a ready SDK
  or a word such as "neural" does not settle eligibility.
- Ordinary sampling, temporal accumulation and cache updates are not neural
  training. They still need correct history/reset behavior.

DLSS SR/DLAA, Ray Reconstruction and frame generation are separate candidates.
NRC trains during rendering and is excluded. Asset-specific neural texture or
material fitting is excluded. ReSTIR, DDGI, SHaRC and NRD are eligible candidates
without that training requirement. See the technology catalog for primary
sources and feature-specific limitations.

Eligibility does not imply adoption. Retain measured NW-11 outcomes and assess
the selected workload, image behavior, performance, memory and complete
distribution dependencies. Apply only the existing artifact-specific size
requirements. Do not introduce universal byte limits or a new benchmarking gate
through this catalog.

## Implementation order

1. **Composable scene foundation.** Multiple geometry/material bindings, stable
   objects, independent transforms, authored camera poses and shots, local lights,
   images and shared time. Generalize the existing graph/compiler/player path.
2. **Reference-driven construction.** Select representative graph fragments or
   construction patterns and implement their missing geometry, texture,
   deformation and animation capabilities. Count both frequency and blocking
   importance when prioritizing. A rare operation can enable a whole scene.
3. **Shared visibility and lighting.** Shadows and material-aware scene
   reflections consume the same current scene as primary visibility. Add
   eligible advanced lighting only for a demonstrated use.
4. **Temporal image reconstruction.** Supply the selected effect with correct
   depth, motion, camera and material data, including history invalidation at
   cuts, resets and time discontinuities. Frame generation is presentation;
   it does not advance the authored simulation or audio clock.
5. **Targeted optimization.** Evaluate optional geometry/scheduling/ray-tracing
   optimizations on those useful workloads under the existing validation policy.

The initial inventory and useful implementation slices can proceed together.
Completing a deep audit of every historical operator is not a prerequisite for
starting a scene. A new capability should work through public graph schemas,
Studio preview, compilation/export and the shared player. Avoid introducing a
separate renderer or a replacement monolithic shader for each showcase.

## Production references and next review work

Start with independently authored examples of construction patterns seen in
Werkkzeug3/debris and Werkkzeug4/the_cube, plus NW-12A's room/corridor production
needs. Select the exact reference graph/page and reachable dependency chain
before claiming that a shot or demo is covered. Neither declaration extraction
nor viewing a finished video establishes that dependency chain.

The next review pass should:

1. Record selected source productions, pages/shots and their reachable operators.
2. Add explicit many-to-many mapping records with full/partial/missing coverage,
   decisions, production controls and evidence links.
3. Implement the smallest shared capability slice that makes a useful sequence
   authorable, then use that sequence to refine priorities.

The earlier NW-12A showcase continues to use its existing engine vocabulary.
This roadmap expands NW-13 without making completion of NW-12A depend on it.

## Sources and reproducibility

The historical inventory uses the existing Farbrausch submodule at
`8c8f82c2ae01133370e649bbc05916e7aceecadd`. It records declaration metadata and
source locations, not copied generator or renderer implementations. Preserve
the clean-room policy in [`references/README.md`](../references/README.md).

Commands and dataset scope are documented in the
[catalog README](../references/operator_catalog/README.md). The catalogs are
developer/planning artifacts and do not enter runtime packages or player
dependency closures. Updating an inventory baseline is an explicit data refresh,
not a freeze of nodewerk's product architecture.
