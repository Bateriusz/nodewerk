# NW-11 Bounded Follow-Up Matrix

The authoritative machine-readable matrix is
[`benchmarks/nw11/follow-up-matrix.v1.json`](../benchmarks/nw11/follow-up-matrix.v1.json).
This page summarizes its dispositions. No feature below is prototyped in
NW-11; each requires a separately scoped follow-up issue.

| Feature | Disposition | Primary constraint |
|---|---|---|
| Vulkan 1.4 | follow-up issue required | would add a second production backend, forbidden here |
| Asynchronous compute | follow-up issue required | no relevant common-path change may enter NW-11 |
| Work Graphs | follow-up issue required | Agility SDK bytes and profile/fallback required |
| Shader Execution Reordering (SER) | follow-up issue required | depends on the ray-query experiment first |
| Opacity micromaps | not applicable | frozen torus content is fully opaque |
| Micromesh / clustered geometry | blocked by licensing | vendor SDK terms and likely byte-positive torus payload |
| Tensor / neural shading | follow-up issue required | model weights and vendor runtime dominate the budget |

## Vendor-optional cost ledger

DLSS, RTX Kit, model weights, and vendor SDK components are recorded as
optional, fully counted items. NW-11 distributes zero bytes from them; any
future use must count every DLL, data file, model weight, and notice in the
incremental distribution budget and must never appear in a common or fallback
artifact.
