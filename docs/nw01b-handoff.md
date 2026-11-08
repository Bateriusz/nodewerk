# NW-01B Handoff: Evidence Contracts and Deferred Implementation Ownership

- **From:** NW-01A (refined issue #18; planning source issue #15)
- **To:** NW-01B, then NW-02 through NW-12 owners
- **Planning base:** `743a4c1aec9e016af8c76cf08f9dd1696017270c`
- **Frozen NW-01A source requirements:** `sha256:v1:4b7a4e828d9ce4835d3f002bdd2960d7af00d99aefdc418317bc8592f753116d`
- **Policy:** [ADR-0006](architecture/0006-capability-based-gpu-support-policy.md)
- **Section audit:** [ADR-0007](architecture/0007-nw02-nw12-section-audit.md)

NW-01A is architecture, documentation, and backlog correction only. It introduces no
executable APIs, GPU features, schema/verifier/capture-tool implementation, package-format
redesign, or renderer/editor/simulation/audio/experiment redesign. The deferred work below is
assigned so downstream owners do not re-invent or contradict the policy.

`tools/nw01a_backlog.py` is a backlog-metadata and publication/readback validator. It is not
the NW-01B evidence schema, capture tool, or GPU evidence verifier.

## 1. Execution gate

`NW-01 -> NW-01A -> NW-01B -> NW-02..NW-12`

- NW-01A completion includes live publication and readback of the affected issue bodies.
  Until every per-issue record in
  [`docs/backlog/nw01a-publication-record.v1.json`](backlog/nw01a-publication-record.v1.json)
  is `published_and_verified`, NW-01A is prepared but incomplete and **NW-01B must not start**.
- Once live publication is verified, NW-01A completion unblocks **NW-01B only**.
- NW-02 through NW-12 implementation remains blocked until **both** NW-01A and NW-01B complete.
- Existing dependencies among NW-02 through NW-12 are preserved.
- The canonical issue bodies in
  [`docs/backlog/nw02-nw12-issue-bodies/updated/*.body.md`](backlog/nw02-nw12-issue-bodies/updated/)
  already contain the global prerequisite note. The `.md` wrappers are human-readable only and
  intentionally omit the body-relative section manifest.

Publish and read back in a networked environment with a valid GitHub token:

```bash
python tools/nw01a_backlog.py publish --apply
```

The command refuses to overwrite a concurrent edit. After `gh issue edit` it reads each issue
back and verifies the normalized body hash, the preserved `pairallel-refinement:v1` marker, and
the body-relative `pairallel-sections:v1` offsets recomputed after newline normalization. A
matching hash alone is not accepted.

## 2. Canonical common profile

- **Stable profile ID:** `nw.common.d3d12.fl12_0.sm6_0.v1`
- **Canonical requirements digest:** `sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a`
- **Machine-readable record:** [`benchmarks/capability-profile.common.v1.json`](../benchmarks/capability-profile.common.v1.json)
- **Existing interfaces to reuse:** `nw::player::BaselineCapabilityProfile`, `AdapterCapabilitySnapshot`, `EmittedContentFeatureRequest`, and `EvaluateCapabilityContract` in [`src/player/capability_check.h`](../src/player/capability_check.h); `benchmarks/capability-manifest.v1.json`.

NW-01B must provide one canonical loader/digest verifier that:

1. Reads the profile's `requirements` object.
2. Serializes it as UTF-8 canonical JSON: lexicographically sorted object keys at every level,
   declaration-order arrays, separators `,` and `:`, no insignificant whitespace, no trailing
   newline, and no escaping beyond required JSON escapes.
3. Hashes the resulting bytes with SHA-256 and prefixes `sha256:v1:`.
4. Rejects an unknown profile ID, unknown requirement category, malformed canonicalization
   metadata, or digest mismatch.
5. Keeps support-scope prose, representative evidence, observed adapters, and historical
   records outside the digest.

The profile is the semantic identity used by target profiles and compiler targets. GPU
generation and model metadata never enter it.

## 3. Evidence record contract

Every new or corrected record must be backward-readable and separate the following groups.
Field names may be added to existing schemas additively; historical records must remain valid.

### `coverage_target`

One of:

- `common`
- `ada_desktop`
- `blackwell_desktop`
- `optional:<profile-id>`

### `evidence_role`

One of:

- `correctness_qualification`
- `performance_certification`
- `informational_regression`
- `pending`
- `unsupported`

### `observed_hardware`

- exact GPU model and architecture;
- vendor ID, device ID, adapter LUID or equivalent adapter identity;
- VRAM;
- exact CPU model and RAM;
- desktop/mobile classification;
- diagnostic sibling relationship when a representative device differs from an intended or
  historical reference.

### `capability_profile`

- profile ID and canonical requirements digest;
- queried capability results for every required feature, format, and limit;
- emitted optional-feature requests and their query results;
- explicit distinction between unqueried, unsupported, and supported.

### `artifact_provenance`

- common/optional artifact hash, package hash, shader hashes;
- size-ledger identity and toolchain ID;
- build/reproducibility identity;
- reference contract identity when the evidence is performance evidence.

### `environment`

- OS name/build/UBR;
- driver, WDDM, and runtime versions;
- toolchain versions;
- clocks, power policy/mode, thermal conditions;
- cache state;
- display/resolution/quality settings;
- measurement tools;
- `unavailable_telemetry` entries for required values that could not be measured.

### `performance_contract_identity`

For performance evidence:

- named contract and version, currently `nw07_perf_ref_v1`;
- exact reference GPU and CPU/host designation;
- thresholds and inequality operators;
- resolution and quality;
- matching rules.

### `protocol_identity`

For performance evidence:

- metric definitions and inequality operators;
- warm-up and run protocol;
- cache conditions;
- measurement tools;
- percentile methodology and raw sample references.

### `result_status`

One of:

- `pass`
- `fail`
- `pending`
- `unsupported`
- `unverified`

Rules:

- `pass` requires all applicable eligibility, coverage, artifact, environment, and protocol
  requirements.
- `fail` records an executed failure and cannot be relabeled unavailable.
- `pending` records unavailable required evidence, including missing reference hardware.
- `unsupported` records an optional feature outside a profile's declared coverage.
- `unverified` records legacy or incomplete evidence that cannot certify a gate.
- Aggregate reporting cannot convert fail, pending, unsupported, or unverified into pass.

## 4. `nw07_perf_ref_v1` contract record

NW-01B must make the following fields mandatory for any certifying record:

- exact desktop NVIDIA GeForce RTX 4060 reference board, VBIOS, device ID, VRAM, clocks,
  power limit, and cooling;
- exact CPU, motherboard/chipset, RAM, storage class, OS build/UBR, NVIDIA driver, WDDM
  version, power plan, NVIDIA power-management mode, PCIe power/link settings, ambient and
  thermal conditions, display mode, and measurement tools;
- `1920x1080` resolution, declared quality settings, uncapped presentation;
- five-second warm-up followed by three uncapped 60-second runs;
- per-run p99 GPU time `< 16.67` ms and p99 CPU submission time `< 16.67` ms, each run
  independently satisfying both operators;
- raw per-run samples and named percentile methodology;
- cold driver-cache and warm results distinguished, plus clean-start cache conditions;
- exact artifact/package/shader hashes and common-profile ID/digest;
- result status per run and per contract.

Non-reference, stale, mismatched-host, or mismatched-protocol records remain readable and may
be marked informational, but the verifier must not let them certify v1. RTX 4060 Ti, other Ada,
and Blackwell measurements cannot substitute. The historical NW-01 RTX 4060 Ti capture is not
an RTX 4060 result.

NW-01B should also represent the separate matched-environment Blackwell development/performance
series with no new absolute Blackwell gate.

## 5. Optional-profile and fallback contract

For each optional profile, NW-01B must require:

- profile ID and requirements digest;
- exact additional D3D12 capabilities and minimum shader model;
- shader targets, DXC version, runtime/toolchain requirements, and any Agility SDK requirement;
- dependencies and complete distributed-byte/startup cost;
- artifact budget;
- declared coverage targets;
- separately exported and tested common fallback artifact identity;
- evidence status `pass`, `fail`, `pending`, or `unsupported`.

The verifier must reject a common export that contains mandatory optional code, shaders, or
dependencies, and must reject silent effect substitution or baseline elevation. An unsupported
optional artifact exits diagnostically; a fallback is explicitly selected and identified.

## 6. Backward-readable migration

NW-01B must not rewrite historical NW-01 evidence. Instead:

- Keep existing `hardware_tier` labels readable.
- Treat `gpu_model`, `gpu_device_id`, and `gpu_architecture` as authoritative over a legacy
  tier label.
- Treat absent `coverage_target`, `evidence_role`, or `result_status` as
  `legacy_unclassified`; such a record is readable but cannot certify a new gate.
- Preserve the distinction between the historical RTX 4060 Ti capture and a qualifying RTX
  4060 `nw07_perf_ref_v1` measurement.
- Preserve original issue bodies and publication records under [`docs/backlog/`](backlog/README.md).

## 7. Deferred implementation ownership

| Area | Owner | Deferred work |
| :--- | :--- | :--- |
| Evidence schema, capture tool, verifier, profile digest loader, durable backlog-publication tooling | **NW-01B** | Implement additive evidence fields, canonical profile verification, raw-sample retention, gate evaluation, result-status rules, and a reusable issue-publication readback/offset verifier. NW-01A contributes the one-time `tools/nw01a_backlog.py publish --apply` utility but does not implement the durable NW-01B tool. |
| Capability-profile identity, target-profile semantic hashing, graph schema | **NW-02** | Consume the profile ID/digest; keep compiler acceptance GPU-independent. |
| Package capability requirements and validation boundaries | **NW-03** | Encode exact emitted requirements in v1; reject unknown/malformed records without package redesign. |
| Player startup capability validation and diagnostics | **NW-04** | Query exact capabilities, identify unmet requirements before bulk allocation, and qualify the identical common package on representative Ada and Blackwell. |
| Compiler export identity, reachable-content closure, resource schedules | **NW-05** | Key export targets by capability profile; exclude unused optional implementations; run representative schedule/aliasing validation. |
| Editor device-epoch and capability-profile cache keys | **NW-08** | Distinguish D3D12 device-recreation epoch from marketing generation; retain actual adapter identity only for device-resident realizations. |
| Checkpoint compatibility and device lifetime | **NW-09** | Bind packaged checkpoints to semantic/state-layout and capability profile; bind device-resident checkpoints to device lifetime. |
| NW-07 performance certification and NW-10 inheritance | **NW-07 / NW-10** | Implement `nw07_perf_ref_v1` evidence and matched before/after preservation without substitute certification. |
| Optional profiles and fallback artifacts | **NW-11** | Implement capability-qualified matched pairs, per-device improvement evaluation, unsupported/pending distinction, and separately tested fallback artifacts. |
| Audio | **NW-12** | Preserve the hardware-neutral audio contract; no GPU-policy implementation is required. |

## 8. Verification expectations for NW-01B

NW-01B should add meaningful automated checks for:

- canonical profile digest recomputation and mismatch rejection;
- additive evidence-field parsing with historical-record fallback;
- exact capability-query diagnostics before allocation;
- `nw07_perf_ref_v1` per-run gate evaluation, non-reference rejection, and pending state;
- optional-profile unsupported/pending/fallback distinction;
- issue-body publication readback, original-body retention, concurrent-edit detection, and
  section-offset recomputation after newline normalization (hash-only comparison is
  insufficient).

Physical benchmark execution, hardware acquisition, and normalization research are not
requirements of NW-01A and must not be used to block its completion.
