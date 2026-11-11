# NW-03 Compiler-to-Player Contract

- **Status:** Implemented
- **Task:** NW-03
- **Base:** `deb671b20703b9616a7f7cc34489c124bd0d504e`

## Scope

NW-03 lowers reachable NW-02 `ShaderExpr` DAGs through a small typed expression
IR into fused, specialized HLSL entry points for the `math_noise` fixture. It
compiles offline with pinned DXC (`1.8.2407`), validates and strips the DXIL,
caches complete shader blobs, and writes a reproducible `.nwp` v1 package. The
same pass also emits a development sidecar containing source maps, reflection,
compiler/profile fingerprints, and the source graph. None of that authoring
metadata enters the release package.

## Pipeline

1. Parse and validate the `.nwgraph` document and consume NW-02's canonical
   semantic digest (the Python frontend builds/uses the authoritative
   `nw_graph_cli digest` helper when a host compiler is available). If the
   authoritative helper is unavailable, compilation fails explicitly rather
   than substituting a JSON or otherwise incompatible digest. Tests may opt
   into an explicit non-production test identity with
   `NODEWERK_NW03_TEST_DIGEST=1`.
2. Find reachable `ShaderExpr` entry points from GPU pass `shader` inputs (or
   terminal standalone expressions when there is no GPU pass).
3. Evaluate the immutable snapshot and fuse the reachable expression DAG.
4. Emit one deterministic HLSL compute entry point per unique specialization.
5. Compile offline with pinned DXC using
   `-T cs_6_0 -E CSMain -HV 2021 -O2 -Qstrip_reflect -Qstrip_debug`.
6. Validate the resulting DXIL container structurally and against the declared
   shader target.
7. Deduplicate identical complete shader blobs and root signatures.
8. Assemble the `.nwp` v1 capability record, resources, primitive instructions,
   parameters/timeline data, and raw payloads.
9. Validate the package and scan release bytes for authoring/debug leakage.
10. Atomically publish the package. A failed compile or validation leaves the
    previous destination untouched.

The HLSL lowering uses a small expression IR with explicit literals,
variables (`uv`, `time`), unary/binary arithmetic, vector constructors, swizzles,
and the declared math functions. `const(x)` is lowered to its argument (it is a
semantic identity, not an HLSL intrinsic). It does not emit per-node function
calls, reflection metadata, or source paths. `texture2d`/`sample` are rejected
for the initial compute family because that family has no texture binding.

## Source-linked diagnostics

Every lowering or compilation failure carries a development diagnostic with:

- the originating graph `node_id` and `operator_id`;
- the expression/port id;
- the generated source-map line/column when available;
- the DXC stderr text for compilation failures.

Release packages contain no source map. Development sidecars map generated line
10 (`float4 nw_value = ...`) back to every transitive graph node contributing to
the fused expression. Reflection in the sidecar is explicit and static; the
player never performs runtime reflection.

## Numerical tolerances

The CPU oracle evaluates the same expression in double precision and in a
float32-quantized mode that rounds every literal, variable, and intermediate
operation result (not only the leaves) to IEEE-754 binary32. The generated HLSL
`float4 nw_value = ...` expression is also evaluated in float32 mode and
compared against the double-precision reference for each reachable
specialization, so fusion/lowering changes are exercised rather than re-running
the source expression. Cancellation-sensitive cases such as
`(16777216 + 1) - 16777216` therefore evaluate to `0` under the documented
float32 model. Comparisons use
absolute tolerance `1e-5` and relative tolerance `1e-4` (documented in
`tools/nw03_oracle.py`). The fixture
`fixtures/nw03/math_noise.nwgraph` uses a fused math/noise-like expression;
`fixtures/graphs/valid_shader_composition.nwgraph` remains the shape-only
composition fixture.

## Shader cache

The cache is content-addressed. Keys include the graph semantic digest, profile
ID and requirements digest, compiler settings, complete HLSL hash, entry point,
target, pass family, thread group, fixed binding convention identity, DXC
identity/version, optimization, and toolchain identity. A changed semantic
input, binding, profile, option, or toolchain identity produces a different key;
corrupt or mismatched entries are misses/invalidations, never silent reuse.

## Reproducibility

Generated HLSL contains no timestamps, absolute paths, source text, or editor
metadata. Sections and records are sorted deterministically; blob tables are
deduplicated by content hash; package bytes contain only canonical execution
data. Two clean build directories using the same graph, settings, profile, and
pinned DXC produce byte-identical packages and matching hashes.

## Commands

```bash
# Validate a graph and report reachable specializations
python tools/nodewerkc.py validate fixtures/nw03/math_noise.nwgraph

# Validate, compile offline with pinned DXC, validate, and atomically publish
python tools/nodewerkc.py compile-package \
    fixtures/nw03/math_noise.nwgraph build/math_noise.nwp \
    --report build/math_noise-report.json \
    --dev-sidecar build/math_noise.dev.json \
    --cache-dir build/shader-cache

# Inspect a package without GPU submission
python tools/nodewerkc.py inspect build/math_noise.nwp
```

Release `inspect` also runs the leakage scan and exits nonzero if the package
contains authoring names, source paths/text, debug data, or reflection tables.
`--allow-dev` is required for development sidecar sections.

The optional C++ frontend exposes the same three commands as `nodewerkc` after
an explicit `cmake --build <build-dir> --target nodewerkc`. On Windows it is
excluded from the default build so the NW-01 concrete-build gate does not depend
on authoring-tool compilation; POSIX builds include it normally.
`python tools/nodewerkc.py --help` is authoritative for options. Diagnostics
are JSON with `category`, `code`, `message`, and source fields; failures exit
nonzero.

The native frontend intentionally implements a subset of `compile-package`:
`--dxc <path>` only. Build reports (`--report`/`--build-report`), development
sidecars (`--dev-sidecar`/`--sidecar`), shader-cache directories
(`--cache-dir`/`--shader-cache`), and `--optimization` are Python-frontend
features, as is `--allow-unpinned-dxc`. Passing one of those options to the
native binary fails with an explicit "use tools/nodewerkc.py" diagnostic
instead of silently ignoring it.

`--allow-unpinned-dxc` is a developer escape hatch. When the measured DXC
version does not contain the pinned identity, the emitted artifact is marked as
non-release (package flags bit 0 is clear), the capability record and build
report/sidecar carry the measured DXC version and identity, and default
release inspection rejects it.

## Verification

```bash
python tools/verify_nw03.py --mode ContractsOnly
python tools/verify_nw03.py --mode Full --summary-json build/nw03-verify.json
```

`ContractsOnly` runs the fixture/oracle, package round-trip/malformed-input,
release leakage, and bounded Python parser checks without requiring DXC.
`Full` additionally requires real pinned-DXC compilation in two clean
directories and a bounded sanitizer-backed native parser campaign. Full refuses
`.py` mock DXC executables, requires the measured pinned identity, and rejects
synthetic/blake2s test containers and debug/reflection chunk payloads in the
resulting compilation; mock-based pytest cases must opt in explicitly with
`allow_test_payloads=True`/`--allow-test-payloads`. It exits nonzero if a
required check fails or is unavailable. The ordinary repository `pytest` suite
remains part of the required validation.

Full also compiles a dedicated expression fixture that exercises every declared
`ShaderExpr` operation (`const`, vector constructors, swizzles, `sin`, `cos`,
`mix`, `uv`, and `time`) with the real pinned DXC, so an operation that survives
lowering (such as the former unsupported `const(...)` output) fails the gate.

The native parser campaign is reproducible: `tools/verify_nw03.py` builds
`tools/nw03_parser_fuzz.cpp` with
`-fsanitize=address,undefined -fno-sanitize-recover=all`, generates a
deterministic 256-file corpus (the valid package plus 255 mutations) from seed
`0x4E573033`, and executes every file within the recorded 256-iteration budget
(the summary JSON records the compiler, sanitizer flags, corpus size, seed,
budget, and elapsed time). The Python bounded fuzz check uses 512 iterations by
default.

## Explicit boundaries

- No runtime source compilation, runtime DXC/Slang dependency, universal
  shaders, or custom compression.
- No graph parser or operator interpreter in the player.
- No capability inference from GPU model, generation, marketing tier,
  adapter-description text, or PCI identifiers.
- Optional profiles are separate artifacts, never silently folded into the
  common package.
- Checksums detect corruption, not authenticity; parsing is not a hostile
  shader sandbox.
