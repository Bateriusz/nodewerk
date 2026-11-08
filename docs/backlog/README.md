# NW-02 through NW-12 Backlog Correction Bundle

- **Task:** NW-01A (refined issue #18; planning source issue #15)
- **Planning base:** `743a4c1aec9e016af8c76cf08f9dd1696017270c`
- **Frozen NW-01A source requirements:** `sha256:v1:4b7a4e828d9ce4835d3f002bdd2960d7af00d99aefdc418317bc8592f753116d`
- **Policy:** [ADR-0006](../architecture/0006-capability-based-gpu-support-policy.md)
- **Section audit:** [ADR-0007](../architecture/0007-nw02-nw12-section-audit.md)
- **NW-01B handoff:** [NW-01B handoff](../nw01b-handoff.md)

## What this bundle contains

| Path | Purpose |
| :--- | :--- |
| [`nw01a-issue-audit.v1.json`](nw01a-issue-audit.v1.json) | Machine-readable per-issue source identity, hashes, section dispositions, exact replacement text, and recomputed section offsets. |
| [`nw01a-publication-record.v1.json`](nw01a-publication-record.v1.json) | Per-issue original/updated body hashes, completion status, GitHub publication status, and source revision. |
| [`nw02-nw12-issue-bodies/original/`](nw02-nw12-issue-bodies/original/) | Verbatim original issue bodies reconstructed from the durable planning output and verified against their recorded body hashes. |
| [`nw02-nw12-issue-bodies/updated/`](nw02-nw12-issue-bodies/updated/) | Complete intended issue bodies. The `.body.md` files are canonical and carry preserved `pairallel-refinement:v1` provenance markers plus recomputed body-relative `pairallel-sections:v1` offsets. The `.md` files are human-readable `# title` wrappers and intentionally carry no machine metadata. |

Issue mapping:

| Task | Issue | Actual title |
| :--- | :--- | :--- |
| NW-02 | #3 | NW-02 — Implement versioned operator schemas, typed DAGs, and deterministic evaluation |
| NW-03 | #4 | NW-03 — Build offline shader lowering and a bounded release-package format |
| NW-04 | #5 | NW-04 — Implement the minimal package-driven D3D12 player |
| NW-05 | #6 | NW-05 — Compile whole graphs into specialized players and explicit resource schedules |
| NW-06 | #7 | NW-06 — Add procedural mesh, texture, scene, and integer-timeline operators |
| NW-07 | #8 | NW-07 — Deliver and benchmark the 256 KiB architectural proof |
| NW-08 | #9 | NW-08 — Add a minimal authoring editor with incremental GPU caching |
| NW-09 | #10 | NW-09 — Introduce explicit temporal state and replayable simulation |
| NW-10 | #11 | NW-10 — Engineer compact exports and determine true 64 KiB feasibility |
| NW-11 | #12 | NW-11 — Evaluate advanced RTX features through bounded, costed experiments |
| NW-12 | #13 | NW-12 — Add optional procedural audio through the authoritative clock boundary |

## Source verification

The original bodies and titles were reconstructed from the durable planning lead output for
`plan-1e6c8c83858cb79fbd34a36a` and verified against the recorded
`planning_publication_intents.body_hash` values for issues #3 through #13. The titles match the
actual backlog inspected during planning. No hardware contract is applied to a misidentified
task.

The `pairallel-refinement:v1` marker is preserved byte-for-byte in every updated body. That
marker is historical provenance for the original refinement and still carries the original
refinement hash. NW-01A intends to amend the issue content, not to rewrite its refinement
history; the `pairallel-sections:v1` manifest is the metadata recomputed for the amended body.
The audit and publication records carry both original and updated body hashes.

The canonical `.body.md` files are the exact GitHub issue bodies and carry body-relative
`pairallel-sections:v1` offsets. The `.md` wrappers start with a `# title` line, which would
shift every body-relative offset, so the wrappers deliberately omit the machine manifest.
Parsing a wrapper for offsets is therefore invalid by construction; parse the `.body.md`
file instead.

NW-01A's own source identity is recorded as:

- refined issue #18 from planning source issue #15;
- planning run `plan-3952f366ef2f607f355cb098`;
- planning base `743a4c1aec9e016af8c76cf08f9dd1696017270c`;
- frozen source requirements `sha256:v1:4b7a4e828d9ce4835d3f002bdd2960d7af00d99aefdc418317bc8592f753116d`;
- refinement hash `sha256:v1:8498891e588744b796ba4e1cf88c4908cd65b3b24c2dca78e2248a2b05e5804b`;
- body hash `sha256:v1:0d3941a663b71a9f602224f70b4ec65ea2ebb9731c3ee5f5d1cacc1905a62379`.

## Publication status

The bundle is prepared and locally verified, but it is **not published**. The implementation
sandbox has no DNS/network access to GitHub and the configured `gh` token is invalid, so live
`gh issue edit` publication and readback could not be executed here. Every per-issue record is
`prepared_verified_publication_pending` / `pending_live_publication`; none claims that GitHub
already contains the updated body.

NW-01A completion is contingent on live publication. Run the following in an environment with
GitHub API access and a valid token:

```bash
python tools/nw01a_backlog.py publish --apply
```

The publisher must:

1. Verify the local bundle before contacting GitHub.
2. Fetch each live issue and accept only the recorded original or updated normalized body hash;
   abort with a concurrent-edit result on any other body or title.
3. Publish only the exact recorded updated `.body.md` body.
4. Read back the live body and verify the normalized body hash, the preserved
   `pairallel-refinement:v1` marker, and the body-relative `pairallel-sections:v1` offsets
   recomputed after newline normalization.
5. Retain the original body and atomically update the per-issue completion record only after
   readback succeeds.
6. Mark the bundle `published` only when every issue is `published_and_verified`.

Do not declare NW-01A complete, and do not unblock NW-01B, while any issue remains
`pending_live_publication`.

## Validation

Run:

```bash
python tools/nw01a_backlog.py verify
```

The verifier checks hashes, section offsets, preserved refinement markers, the recorded NW-01
source digest, every common-profile requirement category (including format usages, shader
targets, pinned toolchain/runtime requirements, and `/CETCOMPAT`), audit text against the exact
bodies, specific changed-section reasons, ADR-0007 consistency, task titles, required sections,
and the NW-07/NW-11 policy invariants. It does not require network access and does not execute
benchmarks. The `publish` command is the only operation that contacts GitHub; it verifies live
offsets, not just the normalized body hash.
