# `.nwp` Version 1 Container Contract

- **Status:** Established by NW-03
- **Format version:** 1.0
- **Endianness:** little-endian for every multi-byte field
- **Owner:** `nodewerkc` writes; `nodewerk-player` reads

The `.nwp` container is the compiler-to-player execution contract. It is not an
authoring document. Release packages contain only execution-required data:
capability requirements, resource descriptors, primitive initialization/frame
instructions, parameter/timeline data, raw shader payloads, raw root-signature
payloads, and an integrity checksum. Source graphs, source text, source paths,
editor layout, source maps, reflection tables, and debug data are *not* present
in release packages. Those development artifacts belong in a separate
`.dev.json` sidecar and build report.

This v1 contract is established once. Later NW-03 stages and downstream tasks
must consume the same section IDs, header layout, checked reader behavior, and
raw payload rules rather than inventing a parallel format.

## File layout

```text
offset  size  field
0       8     magic = "NWPKG1\n\0"
8       2     version_major = 1
10      2     version_minor = 0
12      4     header_size = 64
16      4     flags (bit 0: release package)
20      4     reserved = 0
24      8     section_directory_offset = 64
32      8     total_size
40      4     section_count
44      4     section_entry_size = 32
48      8     checksum_offset = total_size - 32
56      8     reserved = 0
64      ...   section directory entries (32 bytes each)
...           section payloads in directory order
last 32 32    SHA-256 of every byte before the checksum
```

Each directory entry is:

```text
4   four-character section id
4   flags (must be zero in v1)
4   semantic record count (CAP=1, RES resource count, INIT/FRAM instruction
    count, PARM parameter count, RSIG root-signature record count, SHDR
    specialization count; validated against the decoded payload)
8   payload offset
8   payload byte size
4   reserved (must be zero in v1)
```

The directory is canonical: section IDs are unique and payloads are packed in
ascending four-character ID order with no overlaps or gaps. Offsets and sizes
are checked with overflow-safe arithmetic before any payload allocation.

## Sections

| ID | Contents |
| :--- | :--- |
| `CAP ` | Exactly one capability record: profile ID/digest, optional-profile identity, D3D12 feature level, shader model, resource binding/heap tiers, queues, limits, required DXGI formats, emitted shader targets, runtime/toolchain requirements, compiler/profile fingerprints, and exact required instruction opcodes. |
| `RES ` | Bounded resource descriptors: id, semantic name, kind, DXGI format, usage, pass family, fixed binding slot/space, dimensions, byte size, flags. |
| `INIT` | Initialization primitives (`init.create_resource`, `init.create_root_signature`, `init.upload_resource`, `init.create_pipeline`). |
| `FRAM` | Frame primitives (`frame.set_root_signature`, `frame.set_root_constants`, `frame.set_descriptor_table`, `frame.dispatch`, `frame.barrier`, `frame.present`, and the other documented opcodes). |
| `PARM` | Parameter and timeline records: canonical values, phase, component count, frame range, step, interpolation. |
| `SHDR` | Deduplicated complete DXIL shader blob table plus specialization records. |
| `RSIG` | Deduplicated complete root-signature blob table plus fixed pass-family binding records. |
| `DEVC` | Development-only sidecar metadata. It is rejected by the release reader and never written by release compilation. |

All seven release sections (`CAP`, `RES`, `INIT`, `FRAM`, `PARM`, `SHDR`,
`RSIG`) are required exactly once. Unknown section IDs, duplicate sections,
missing required sections, and `DEVC` in release mode are rejected.

## Capability record

`CAP` begins with `u16 section_version = 1`, `u16 flags = 0`, `u32 field_count`,
then sorted key/value fields. Unknown, duplicate, missing, or malformed fields
fail through the `package_validation` diagnostic category before bulk
allocation.

Required fields include:

- `profile_id = nw.common.d3d12.fl12_0.sm6_0.v1`
- `requirements_digest = sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`
- `optional_profile_id` / `optional_requirements_digest` /
  `optional_profile_separate_artifact`
- `feature_level = 0xC000` (`D3D_FEATURE_LEVEL_12_0`)
- `shader_model = 0x0060`, `shader_model_text = 6_0`
- `resource_binding_tier = 2`, `resource_heap_tier = 2`
- `direct_queue_count = 1`
- `min_windows_build = 22631`
- `required_dxgi_formats` exactly equal to the sorted set of formats emitted by
  the package resources. The math/noise fixture emits only
  `DXGI_FORMAT_R32G32B32A32_FLOAT` and `DXGI_FORMAT_R32_UINT`; depth/color
  formats are not declared unless emitted.
- `limit_max_texture_2d_dimension`, `limit_max_buffer_bytes`,
  `limit_max_compute_threads_per_group`, `max_cbv_srv_uav_descriptors_per_frame`,
  `max_sampler_descriptors_per_frame`, `max_root_signature_dword_cost`, and
  `limit_swap_chain_buffer_count` are derived from the emitted resources,
  root signatures, shaders, and frame primitives. The profile identity retains
  the full baseline maxima, but the package capability record advertises only
  requirements this artifact actually uses.
- `emitted_shader_targets` exactly matching the shader records
- `runtime_dxc_dependency = false`, `runtime_slang_dependency = false`,
  `agility_sdk_required = false`
- `toolchain_dxc_version`, `toolchain_identity`, `compiler_fingerprint`,
  `profile_fingerprint`
- `required_init_instructions` / `required_frame_instructions` exactly matching
  the emitted primitive opcodes

Release packages require `toolchain_dxc_version` to contain the pinned DXC
identity (`1.8.2407`) and `toolchain_identity` to equal
`dxc:<toolchain_dxc_version>`. Non-release development artifacts may carry a
different measured DXC identity.

`profile_fingerprint` is SHA-256 over the canonical capability fields excluding
the fingerprint itself. Unknown main-profile IDs and digest mismatches fail
closed. Unsupported optional profiles remain separate artifacts; a declared
optional identity must be well-formed, digest-matching, and marked
`optional_profile_separate_artifact = true`.

Observed GPU model, marketing generation, coverage tier, adapter-description
substrings, and PCI vendor/device identifiers are metadata only. They never
become requirements, never control validation branches, and never influence
profile selection.

## Fixed pass-family bindings

NW-03 emits the initial `compute.math_noise` family without runtime reflection:

| Semantic binding | Register | Usage |
| :--- | :--- | :--- |
| `params` | `b0` | CBV root constants / parameter block |
| `input` | `t0` | Optional SRV input |
| `output` | `u0` | UAV output |
| `sampler` | `s0` | Optional sampler |

Root parameters are also fixed: `params` occupies root parameter 0 (root
constants) and `output` occupies root parameter 1 (descriptor table). Every
`frame.set_root_constants` / `frame.set_descriptor_table` operand must select
the same root parameter as the binding it references.

The resource descriptors, root-signature records, and dependent instruction
operands must agree. Duplicate bindings, out-of-contract bindings, missing
required `b0`/`u0` bindings, unknown pass families, and undeclared formats are
rejected. Later pass families must document their own fixed convention in this
file and in `PASS_FAMILY_BINDINGS` sources before emission.

## Shader and root-signature payloads

`SHDR` stores raw complete DXIL containers, not source or intermediate
expressions. Identical complete blobs are stored once and referenced by every
specialization. `RSIG` stores complete serialized D3D12 root-signature payloads
with the same deduplication rule. The payload is a DXBC container with one
`RTS0` chunk. Its 24-byte header is
`version, num_root_parameters, parameters_offset, num_static_samplers,
static_samplers_offset, flags`; each 12-byte root parameter is
`type, shader_visibility, data_offset`; parameter data is either the three
`register, space, num_32bit_values` dwords for 32-bit constants or
`num_ranges, ranges_offset` followed by 20-byte descriptor ranges. Enum values
follow D3D12 (`descriptor table = 0`, `32-bit constants = 1`; `SRV = 0`,
`UAV = 1`, `CBV = 2`, `sampler = 3`). `ID3D12Device::CreateRootSignature`
therefore receives serialized signature bytes; the package never stores a
private binding-list format. Static samplers are not part of the baseline
family. Root-signature `dword_cost` is the total D3D12 cost of all parameters
(one DWORD per descriptor table plus the number of 32-bit constants), not just
the constant count. Cross-blob function sharing is never assumed.

The reader checks DXBC magic, container size, chunk count/offsets/sizes, SFI0
feature flags, PSV0 presence, DXIL program version (`cs_6_0`, `vs_6_0`, or
`ps_6_0`), DXIL version, and LLVM 3.7 bitcode framing. Every permitted target
is structurally validated and must match the declared shader target; unknown
targets are rejected. `frame.dispatch` additionally requires a `cs_6_0` shader
and all three group dimensions in `1..65535`. Release packages reject the
`ILDB`, `ILDN`, `STAT`, `RDEF`, `RDAT`, `SPDB`, and `SPDN` chunks explicitly,
so debug/reflection metadata cannot hide merely because its payload lacks a
scanned ASCII string. Release inspection and leakage tests verify that source
paths, source text, editor names, debug data, and reflection tables are absent
while execution-required DXIL metadata (`DXIL`, `PSV0`, `SFI0`, `HASH`) remains.

## Validation ordering

The reader validates, in order:

1. magic, major/minor version, header size, reserved fields, total size;
2. section count, entry size, directory range, checksum offset;
3. SHA-256 integrity checksum;
4. unique known section IDs, flags, per-section decoded-size limits;
5. section offset/size ranges and overlap/gap checks;
6. required sections;
7. bounded section record counts, string lengths, blob lengths, and checked
   arithmetic;
8. capability identity/digest and optional-profile identity are rejected before
   bulk payload decoding;
9. capability fields, profile identity/digest, contradictions, exact emitted
   requirements, emitted targets and instruction opcodes;
10. resource dimensions/formats/bindings;
11. parameter dimensions/timeline ranges;
12. instruction operands, opcodes, phases, and resource/shader/root-signature
    references;
13. shader DXIL structure, forbidden debug/reflection chunks in release mode,
    and serialized root-signature constraints.

Section payloads must be contiguous in directory order. Padding inserted
between sections, descending section IDs, overlap, and non-contiguous payloads
are rejected so a checksum-correct package always re-encodes to the same bytes.
The aggregate decoded-memory budget prevents a small deduplicated blob table
from being copied into thousands of specialization records.

No parser failure reaches GPU submission. Unknown major versions, required
sections, instructions, operand types, and shader targets are rejected.

## Integrity and hostile input

The trailing SHA-256 detects accidental corruption. It does **not** establish
authenticity, provenance, or trust: an attacker can recompute it. Parsing is
also **not** a sandbox for hostile shaders or hostile DXIL; the package reader
enforces bounded allocations and structural invariants, but shader execution
trust must come from the platform and deployment policy.

Malformed-input tests and a bounded AddressSanitizer/UndefinedBehaviorSanitizer
parser campaign cover sections, capability records, instruction operands,
dimensions, lengths, counts, offsets, and arithmetic. The campaign
configuration, corpus generation seed, executed file count, and budget are
recorded by `tools/verify_nw03.py`.

## Excluded from v1

- Runtime source compilation, runtime DXC/Slang dependencies, and Agility SDK
  redistribution.
- Universal/uber shaders; output is specialized per reachable entry point.
- Custom package compression. Any later compression must prove measured net
  savings including decoder code.
- Runtime graph parsing, editor metadata, reflection tables, and debug data in
  release packages.
- Inference of capabilities from GPU marketing names, generations, coverage
  tiers, adapter-description substrings, or PCI identifiers.

## Archival reproduction

Reproducing an archived demo requires the source graph, pinned DXC/toolchain
identity, build report, development sidecar (source maps/reflection), and the
recorded package hash. Release blobs alone are sufficient to execute, not to
re-author.
