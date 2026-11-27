# Reference operator catalog and nodewerk coverage

This directory preserves reference declarations and the first capability-level
assessment for [NW-13](https://github.com/Bateriusz/nodewerk/issues/36).
Start with the generated [overview](overview.md) and the
[implementation roadmap](../../docs/operator-roadmap.md).

## Contents

| File | Purpose |
| --- | --- |
| [werkkzeug3.json](werkkzeug3.json) | Extracted desktop Werkkzeug3 and kkrieger-branch registrations, animation commands and Nova effects. |
| [werkkzeug4.json](werkkzeug4.json) | Declarations from every `.ops` file under `altona_wz4` at the recorded reference revision. |
| [capability_map.json](capability_map.json) | 25 semantic families with reviewed current nodewerk scope, limitations, proposed additions, sources and priorities. |
| [technology_candidates.json](technology_candidates.json) | 14 implementation-candidate groups and the no-owned-training eligibility policy. |
| [overview.md](overview.md) | Generated count/state tables for review; not a compatibility certificate. |

The reference revision is the existing submodule pin
`8c8f82c2ae01133370e649bbc05916e7aceecadd`. The current nodewerk assessment is
pinned to `1c9e1559c8d43530c7b213461c6719b6aa43ae35`; its proposed work also
references the explicitly dated, mutable NW-13 issue. Refresh that assessment
when implementation changes rather than treating it as a permanent restriction.

## Inventory scope

Werkkzeug3 extraction covers the complete `WerkClasses`, `AnimOpCmds` and
`EffectList` tables in both desktop source variants:

| Variant | Graph operators | Animation commands | Nova effects |
| --- | ---: | ---: | ---: |
| `werkkzeug3` | 218 | 41 | 5 |
| `werkkzeug3_kkrieger` | 193 | 37 | 5 |

The two variants contain overlapping functionality. Their 499 registrations
are not 499 unique capabilities. Derived texture/mobile tools and export-player
handler subsets are outside this declared inventory scope; see the JSON scope
notes. RG2, GenThree and the separate synthesizer are not counted as Werkkzeug3.

Werkkzeug4 extraction covers all **33 `.ops` files** under `altona_wz4`: the two
core library files and 31 `wz4frlib` files, including production extensions.
They contain **370 source declarations**, excluding commented-out operators.
Hidden, obsolete, conversion, test and proof-of-concept roles are retained as
tags, which may overlap. The inventory does not evaluate build configurations
or establish which declarations are enabled in a specific executable.

Numeric legacy IDs are preserved where the source declares them. Werkkzeug4's
DSL does not declare such IDs here, so its keys use source path, output type and
name instead of inventing numbers.

## What is and is not reviewed

Source files and declaration boundaries are verified. Each entry retains a
pinned source URL, original type/family and available signature metadata.

Parameter extraction is **partial and static**. Wz3 records direct UI/control
declarations without executing C++ branches, loops, delegated editors or default
assignments. Wz4 records declared parameter/control fields, array scopes and GUI
conditions; exact runtime behavior, ranges/defaults, flag encoding and animation
bindings still require semantic review. These expressions are declaration
metadata, not translated generator or renderer implementations.

Individual reference equivalence remains **unreviewed**. The capability map
records current engine functionality separately; it is not an automatically
verified mapping from all reference entries. Search hints are only discovery
aids. Demo usage and reachable graph/page dependencies are
`pending_graph_extraction`; no usage count or demo-coverage percentage is
inferred from an operator name or source count.

This separation matters for current nodewerk: the scene exporter is bounded,
the SDF intro is a specialized shader, and the NW-11 optional paths are
experiments. Their existence does not establish general SDF authoring,
heterogeneous scene composition or material-aware reflection. The capability
map records the specific implementation sources and limits.

## Regeneration and checks

Use Python 3.10 or newer and the repository's existing reference submodule.
The scripts use only the standard library and do not build the historical tools,
run their code, download SDKs or change runtime exports.

From the repository root:

```bash
git -c core.autocrlf=false submodule update --init references/farbrausch/fr_public
python tools/operator_catalog.py generate
python tools/operator_catalog.py check
python tools/operator_catalog.py summary
python -m unittest discover -s tests -p 'test_werkkzeug*_catalog.py'
```

`generate` writes the two reference inventories and overview; curated capability
and technology decisions are maintained separately. `check` reconstructs all
generated outputs and compares their exact bytes. `summary` validates the saved
metadata and prints counts without requiring the reference checkout; it does
not reverify source bytes.

Each extractor checks its input files against the original Git blob hashes
before attaching the recorded revision. Wz4 also checks the complete `.ops`
path set. Preserve the original source encoding and line endings; an existing
checkout modified by automatic CRLF conversion may need a separate checkout
with conversion disabled. Do not normalize source files or bypass a failed hash
check. To refresh to another reference revision, intentionally review/update the
source manifest and regenerate the datasets.

Individual commands are also available:

```bash
python tools/werkkzeug3_catalog.py --check
python tools/werkkzeug4_catalog.py --check
```

Both accept `--reference-root PATH` and `--output PATH`. The combined command
accepts `--reference-root PATH` and `--output-dir PATH`. A temporary output
directory can be used to inspect an independent regeneration.

Generated JSON uses one operator per line while keeping provenance readable.
GitHub marks the generated inventories as generated files so reviewers can
focus on extraction logic, scope and curated decisions. The catalogs remain
fully searchable and are not embedded in release players.

## Completing the mapping

For each selected reference operator or reachable subgraph, add a reviewed
relation identifying its inventory keys, production/page/shot, required controls,
nodewerk operator/subgraph, implementation, coverage and evidence. Use explicit
preserve/consolidate/replace/partial/defer/omit decisions. Keep the reference
entry after an obsolete implementation is replaced.

Select the next implementation slice from actual production requirements and
the capability priorities. A full historical port, a general DCC, and integration
of every eligible SDK are not prerequisites for a useful NW-13 production.

The independent-implementation and licensing guidance in
[Reference Sources](../README.md) continues to apply. No upstream source files
are duplicated by this catalog change.

## NW-13 production review

The [production coverage map](semantic-coverage.v1.json) connects exact
declaration keys to [implementation candidates](implementation-candidates.v1.json).
Each link identifies the production use and the pinned declaration. The
candidate records describe controls, animation and temporal requirements,
realization, backend, implementation state and disposition.
