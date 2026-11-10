# `.nwgraph` Version 1 Textual Schema

The `.nwgraph` file is the authoring/compiler interchange form for NW-02. It is
parsed only by `nodewerkc`-side code (`src/graph/`). It is never linked into
`nodewerk-player`, which has no graph parser, plugin ABI, or runtime operator
interpreter.

## Grammar

```text
document      := header, profile, setting*, (node | layout)*
header        := "nwgraph", version
profile       := "profile", capability-id, requirements-digest
setting       := "setting", setting-key, value
node          := "node", node-id, operator-id, "@", semantic-version,
                 ("frequency=", frequency)?, ("residency=", residency)?
                parameter* | input* | seed?
parameter     := "param", parameter-id, value-token+
input         := "input", input-port, source-node, ".", source-port
seed          := "seed", (uint64 | seed-behavior |
                          "explicit", uint64 |
                          "node-id", node-id)
layout        := "layout", node-id, x, y, ("collapsed=true")?
frequency     := "compile-time" | "initialization" | "frame"
residency     := "logical" | "cpu" | "gpu" | "readback"
```

Quoted values use double quotes with `\\`, `\"`, `\n`, `\r`, and `\t` escapes.
Comments begin with `#` outside quoted strings. Numeric vector/matrix parameters
accept whitespace- or comma-separated components.

`profile` is required. A document without a `profile` directive is rejected
rather than receiving the default capability identity implicitly.

`layout` records editor-only positions. It is parsed into `EditorLayout` and is
excluded from canonical semantic bytes, semantic hashes, seed derivation, and
exported content.

## Versioning and migrations

- Schema version `1` is current.
- Schema version `0` is legacy and is **rejected** by default with
  `migration_required`.
- `nw_graph_cli migrate` and `validate --migrate` apply the explicit
  `nwgraph-v0-to-v1` migration, which adds the registered semantic operator
  version to legacy node declarations. The migration preserves an explicitly
  declared legacy residency so validation can reject it if it is incompatible
  with the operator; it never silently repairs authoring semantics.
- Unknown future schema versions are rejected with `unknown_schema_version`.
- Unknown operator ids and unknown operator versions are rejected. Operator
  semantic changes require a new semantic version; operator contract hashes
  include the version and every port/parameter/resource/lowering declaration.

## Frequency and residency

Frequency ordering is `compile-time` < `initialization` < `frame`. A consumer
may not run earlier than any producer (`frequency_transition`).

`history` is parsed only so it can be rejected explicitly. It is reserved for
NW-09 and is invalid in initial graph exports.

Residency conversions are explicit. Render images are produced in `gpu`
residency. CPU readback requires `nw.residency.gpu_to_cpu` or
`nw.readback.scalar`; a direct GPU-to-CPU connection is `implicit_readback`.
Logical recipes require an explicit upload (`nw.residency.upload_*`); a direct
logical-to-GPU connection is `implicit_upload`.

## Shader expressions

`nw.shader.expr` accepts only identifiers and functions declared in the
operator's `shader_operations` whitelist. Numeric literals, swizzles (`.x`,
`.xyzw`, `.rgba`), parentheses, commas, and arithmetic composition operators are
syntax, not implicitly declared operations. Unknown function calls or
identifiers fail with `invalid_expression` and identify the node and
`expression` parameter.

## Canonical semantic identity

Semantic identity is SHA-256 over length-prefixed, type-tagged canonical fields:

- schema version,
- capability-profile id and canonical requirements digest,
- compiler settings,
- quantization policy (recorded separately from full-precision authoring
  values), and
- sorted structural node hashes.

Each structural node hash covers the stable operator id, semantic version,
frequency, operator contract hash, seed behavior, canonical parameters, and
sorted upstream structural hashes with their source ports. Node ids are not
included unless a node explicitly uses `seed node-id <node-id>`. Editor layout is
never included.

Canonical floating-point fields are finite IEEE-754 doubles, treat `-0.0` as
`+0.0`, and are encoded as big-endian bit patterns. Struct padding and raw
parameter memory are never hashed.

The common capability-profile identity is:

```text
nw.common.d3d12.fl12_0.sm6_0.v1
sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a
```

Observed GPU model, generation, marketing name, adapter description, and PCI
identifiers are metadata only. They are never inputs to target-profile
selection, semantic hashes, seed derivation, or compiler acceptance.

## CPU reference evaluation and caching

`GraphEvaluator` executes a deterministic topological order and memoizes each
node's typed multi-output values. Cache keys include the frequency domain and
frame/seed context. Missing and corrupt entries are cache misses.
`ApplyInputChange` invalidates only the changed node and its transitive
dependent closure. Evaluation failures return diagnostics and never publish a
replacement snapshot.

Curve values carry their declared `linear` or `step` interpolation mode, and
`nw.curve.sample` honors that mode. Texture resource bounds are enforced against
the selected format's bytes-per-element as well as width/height limits; buffer
and mesh recipes enforce their declared byte/vertex/index descriptors.

`AuthoringSession::ApplyEdit` validates a complete new document before replacing
the last valid immutable `GraphSnapshot`; invalid edits leave that snapshot and
its editor layout untouched.
