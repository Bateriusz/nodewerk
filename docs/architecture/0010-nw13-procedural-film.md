# ADR-0010: NW-13 Editable Procedural Film on the Existing Architecture

- **Status:** Accepted
- **Implementation base:** `1c9e1559c8d43530c7b213461c6719b6aa43ae35`
- **Supersedes:** none

## Context

NW-11 and NW-12 completed optional GPU experiments and optional procedural
audio. NW-13 must let authors build an editable procedural film without
introducing a second graph, package, player, timeline or capability system.

## Decision

1. Geometry is a new public, backend-neutral signed-distance document that is
   distinct from mesh aggregation and from scene composition.
2. Scene composition binds stable object identities, materials, local lights,
   transforms, typed tracks, shots and cuts. Hierarchies are cycle-checked and
   rigid.
3. The existing NW-02 graph reader and NW-05 optimizer remain authoritative.
   Admission bounds are checked before optimizer/GPU work.
4. Common raster and capability-qualified DXR paths consume the same scene.
   Optional artifacts bind provenance and retain an independently exported
   common fallback.
5. Particles and feedback continue the NW-09 120 Hz History contract.
6. Audio authors a versioned instrument/sequence payload that compiles to the
   preserved NWAS v1 container and the authoritative 48 kHz clock.
7. Optional lowerings are recorded separately. Pending or unavailable
   measurements remain pending/unavailable; they are never inferred as passes.

## Consequences

The production remains editable at graph, scene and song level, and the same
backend-neutral documents drive the shared compiler, exporter, PackagePlayer
and optional render paths. No Farbrausch code or asset enters the repository.

