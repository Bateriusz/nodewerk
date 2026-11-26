# ADR 0009 — Validation and evidence lifecycle

## Decision

A task is a development milestone, not an ownership boundary for code. Product
code and shared verification tooling evolve on the active branch. A historical
measurement, spec revision, or exported artifact remains immutable: its
provenance is not updated merely because a later implementation exists.

### Identity and reproducibility

- Record the actual source revision, tree and build-input digest.
- Retain the old pinned digest as historical provenance; a mismatch is not
  automatically a regression. Never represent a mismatch as equality.
- Rebuild the same clean revision independently and compare actual executable,
  package, manifest and dependency identities. Cross-revision comparisons are
  explicit performance or correctness experiments, not a byte-equality gate.
- Keep existing artifact-specific limits where the relevant spec actually
  requires them. The canonical compact proof's size goal is not a universal
  adoption rule for GPU experiments.
- Published evidence continues to identify exactly the measured executable,
  bundle, environment, protocol and inputs. It does not certify future builds.

### Shared implementation

All player targets compile the common PackagePlayer source. Optional GPU
paths are selected by build configuration; an independently generated copy
of a historically pinned implementation is not a second player.

The common Win32 layer initializes client geometry from GetClientRect, not a
cached request. Fixed-resolution GPU source readback is independent of the
ordinary window geometry. Physical GPU experiment and native composed-release
proofs use explicit borderless placement; the common generated player also
supports ordinary maximization, without changing source resolution. Other
player users retain their ordinary window policy by default. A Full HD monitor
can provide the full 1920×1080 native display proof; physical presentation
captures still require the actual visible measured dimensions. Verify that
proof before expensive sampled measurements; startup preparation is not required for ordinary validation.

Shared tools are named by responsibility:

| Module | Responsibility |
| --- | --- |
| build_attestation.py | Current build/source identity |
| canonical_export_reproducibility.py | Independent same-revision native builds |
| hardware_evidence_host.py | Host qualification, locale and Windows identity |
| hardware_evidence_devices.py | Measured physical GPU identities |
| validation_gpu_telemetry.py | Physical GPU telemetry and power conditions |
| validation_cold_start.py | Cold-start observations and qualification |
| presentation_metrics.py | PresentMon collection and sample processing |
| gpu_experiment_measurements.py | GPU experiment measurement aggregation |
| validation_errors.py | Native renderer error taxonomy and failure evidence |
| compact_export_evidence_publisher.py | Compact export evidence publication |

Old module names in this migration remain compatibility entry points where
older scripts or packages depend on them. Do not fork the implementation under
a new task number. New consumers should import functional modules.

### Experiment policy

The active GPU experiment definitions are revision 3. Verified functional execution of an optional GPU path is a positive experiment outcome, independently of whether a simple fixture demonstrates a performance advantage or pixel equivalence. Preserve all image comparisons, resource observations and matched performance results, including differences and inconclusive comparisons. A functional pass is not a claim of numeric equivalence or a decision to enable the feature by default. Distribution size is reported. Historical v1 and v2 definitions and decisions remain unchanged; version and content digest distinguish semantics.

Hosted CI runs the headless WARP command-path check, independent builds and
bundle integrity tests. It does not assert physical GPU presentation. The
physical policy requires real GPU execution and real presentation. Ordinary physical validation runs in the current desktop session, without restarting Windows, auditing process creation, qualifying driver cache, or distinguishing cold and warm startup. Record per-process startup timing as an observation; only a task explicitly investigating startup behavior may require a specialized startup protocol. A physical outcome is recorded only after the actual test runs.

Native renderer failure evidence preserves the actual process exit code,
symbolic code, diagnostic message and launch/artifact identities in
failure.json. A runner-level failure must not overwrite those facts.

### CI and compatibility

Workflow display names describe their function. Existing workflow paths and
job/check names remain stable in this migration because external check
configuration and links may depend on them. Internal scope IDs remain routing
keys, not module names. A future removal of a compatibility entry point or
change to a required check must migrate its consumers together.

Historical evidence formats, benchmark file paths and published identifiers
are intentionally not renamed. This migration does not declare any old
physical result verified for a new artifact.

### Public evidence

Keep raw evidence unchanged on the measurement host; publish an allowlisted, sanitized projection without user/host names, absolute paths, machine serials or raw audit logs. Identify the measured commit, bundle and available hashes, retain historical decisions and limitations, and never claim a redacted summary was independently reverified as the original raw evidence.
