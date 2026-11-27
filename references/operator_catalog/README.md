# Pinned Werkkzeug operator catalog

This directory is review metadata. It is **not** a port, fork, compatibility
layer or copy of Farbrausch source code. The `references/farbrausch/fr_public`
submodule remains untouched and unvendored.

## Pinned inputs

`pinned-sources.v1.json` records:

* the `fr_public` upstream repository and the exact submodule commit
  `8c8f82c2ae01133370e649bbc05916e7aceecadd`;
* separately scoped engine entries for Werkkzeug3 and Werkkzeug4;
* the `kkrieger` game variant as an identifiable variant of Werkkzeug3 rather
  than as a Werkkzeug4 operator set;
* the fact that no upstream code or assets are vendored.

The raw submodule is empty in this worktree, so the deterministic regeneration
input is the checked-in declaration export captured from that pinned commit
(`*.declarations.v1.json`). `tools/operator_catalog.py --write` regenerates
`*.inventory.v1.json` from those pinned declarations; `--check` verifies exact
reproduction, declaration identity, ports, types, extractable parameters,
source locations, hidden/obsolete status, extraction limits and variant scope.

## Review records

* `semantic-coverage.v1.json` is the many-to-many map from catalog entries to
  nodewerk implementation candidates.
* `implementation-candidates.v1.json` records for each reviewed use its
  production need, controls, animation, downstream and temporal requirements,
  proposed realization, backend, implementation state and
  `preserve` / `consolidate` / `replace_implementation` / `partial` / `defer` /
  `omit` decision.
* Unreviewed inventory entries are retained. Mesh-topology, selection and UV
  gaps are explicitly recorded in `unresolved_gaps`; they are not silently
  treated as covered.

`nw13-geometry-contracts.v1.json` is the reproducible public contract catalog
for the sixteen NW-13 bounded geometry operators. It is generated from
`tools/nw13_geometry.contract_document()` and references
`schemas/nw13-geometry-graph.v1.json`.

## Licence provenance

Upstream license files were not available in this worktree. Each declaration
therefore records `license: upstream_unknown` unless a reviewer supplies
provenance. No upstream expression, identifier, constant or asset is copied.

