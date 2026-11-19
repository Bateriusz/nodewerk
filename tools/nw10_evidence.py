#!/usr/bin/env python3
"""Generate deterministic NW-10 contract evidence from the canonical paths.

The generator runs the repository-owned NW-07 proof export and the restricted
NW-10 intro export with structural test payloads on a GPU-less host.  It never
promotes those numbers to physical Windows evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nw10_intro as intro  # noqa: E402
import nw10_proof as proof  # noqa: E402
import nw10_publish_evidence as publisher  # noqa: E402

EVIDENCE_FORMAT = "nw10-compact-evidence"
EVIDENCE_VERSION = 1


def _sha256(path: Path) -> str:
    # Evidence files are text JSON.  Hash canonical LF bytes so a Windows
    # checkout with CRLF conversion records the same identity as POSIX CI.
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def _sanitize_paths(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_paths(item, root) for item in value]
    if isinstance(value, str):
        text = value.replace(str(root), "<nw10-temporary-root>")
        text = re.sub(r"\.nw10-(?:proof|intro|evidence|export)-[A-Za-z0-9_]+",
                      ".nw10-<temporary-dir>", text)
        text = re.sub(r"nw10ev[0-9]*\.[A-Za-z0-9_]+",
                      "<nw10-temporary-root>", text)
        return text
    return value


def _write_text_lf(path: Path, text: str) -> None:
    # Keep archived evidence byte-identical across Windows and POSIX checkouts.
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def build_evidence(output_directory: Path | str) -> dict[str, Any]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("NODEWERK_NW03_TEST_DIGEST", "1")
    with tempfile.TemporaryDirectory(prefix="nw10-evidence-") as temporary:
        root = Path(temporary)
        proof_report_path = root / "proof-report.json"
        intro_report_path = root / "intro-report.json"
        proof_report = proof.export_proof(
            root / "proof-artifacts",
            contracts_only=True,
            report_path=proof_report_path,
            reproducibility=True,
        )
        intro_report = intro.export_intro(
            root / "intro-artifacts",
            contracts_only=True,
            report_path=intro_report_path,
        )
        archived_proof_report = output / "proof-export-report.v1.json"
        archived_intro_report = output / "intro-export-report.v1.json"
        _write_text_lf(
            archived_proof_report,
            json.dumps(
                _sanitize_paths(
                    json.loads(proof_report_path.read_text(encoding="utf-8")),
                    root,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        _write_text_lf(
            archived_intro_report,
            json.dumps(
                _sanitize_paths(
                    json.loads(intro_report_path.read_text(encoding="utf-8")),
                    root,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        # Archived contract evidence must be byte-identical on every host.
        # Host-proxy compiler measurements are intentionally excluded here;
        # the proof report already records the deterministic pending Windows
        # decoder measurement required by the NW-10 contract.
        decoder = dict(proof_report["decoder_code"])
        nw10_inputs = contract.compute_build_inputs()
        toolchain_path = REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json"
        toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
        toolchain_pins = toolchain.get("pinned_dependencies", {})
        evidence = {
            "format": EVIDENCE_FORMAT,
            "version": EVIDENCE_VERSION,
            "task": contract.TASK,
            "implementation_base": contract.IMPLEMENTATION_BASE,
            "source_revision": contract.IMPLEMENTATION_BASE,
            "planning_provenance": contract.PLANNING_PROVENANCE,
            "historical_nw07_build_inputs_digest": (
                contract.HISTORICAL_NW07_BUILD_INPUTS_DIGEST
            ),
            "nw10_build_inputs_digest": contract.build_inputs_digest(nw10_inputs),
            "nw10_build_inputs": nw10_inputs,
            "toolchain": {
                "manifest": "benchmarks/toolchain-manifest.v1.json",
                "manifest_sha256": _sha256(toolchain_path),
                "pinned_dependencies": toolchain_pins,
            },
            "commands": {
                "contract_evidence": "NODEWERK_NW03_TEST_DIGEST=1 python tools/nw10_evidence.py --output-dir benchmarks/nw10",
                "pytest": "python -m pytest -q tests/test_nw10_*.py",
                "windows_proof": (
                    "python tools/nw10_proof.py --output build/nw10-proof "
                    "--canonical-player-template <decoder-off-template> "
                    "--player-template <decoder-on-template> "
                    "--dxc <pinned-dxc> --reproducibility"
                ),
                "windows_intro": (
                    "python tools/nw10_intro.py --output build/nw10-intro "
                    "--canonical-player-template <decoder-off-template> "
                    "--player-template <decoder-on-template> "
                    "--dxc <pinned-dxc>"
                ),
            },
            "evidence_status": "informational_contract_only",
            "physical_certification": "pending_physical_windows_capture",
            "reference_capture": "pending_reference_capture",
            "codec_encoding_identity": {
                "encoding_id": compact.MSZIP_ENCODING_ID,
                "contract_host_encoder": "python-zlib-raw-deflate-level-9-ck-framing",
                "windows_release_encoder": "windows-inbox-compression-api",
                "cross_platform_byte_parity_claimed": False,
                "decoded_package_sha256_is_cross_host_invariant": True,
                "encoded_payload_hash_scope": "host_specific",
                "reason": (
                    "MSZIP encoded bytes are host-specific and are never used as "
                    "a cross-host identity. Contract-only evidence records its "
                    "local proxy bytes; Windows release evidence records the "
                    "Windows encoder bytes. Cross-host linkage is only through "
                    "the decoded canonical package SHA-256."
                ),
            },
            "correctness_coverage": {
                "desktop_ada_representative": {
                    "status": "pending_unverified",
                    "artifact": "exact final compact executable",
                    "protocol": "nw07-correctness-capture.v1",
                    "packed_unpacked_visual_comparison": "pending_physical",
                },
                "desktop_blackwell_representative": {
                    "status": "pending_unverified",
                    "artifact": "exact final compact executable",
                    "protocol": "nw07-correctness-capture.v1",
                    "packed_unpacked_visual_comparison": "pending_physical",
                },
                "synthetic_substitute_forbidden": True,
            },
            "preserved_contracts": {
                "performance_contract": contract.PERF_CONTRACT_ID,
                "performance_protocol": contract.PERF_PROTOCOL_ID,
                "quality_profile": "nw07.quality.v1",
                "resolution": {"width": 1920, "height": 1080},
                "loop_ticks": contract.LOOP_TICKS,
                "frame_rate_hz": contract.FRAME_RATE_HZ,
                "numeric_image_tolerances": contract.image_tolerances(),
                "runs": contract.PERF_RUN_COUNT,
                "warmup_seconds": contract.WARMUP_SECONDS,
                "run_seconds": contract.PERF_RUN_SECONDS,
                "fresh_process_per_run": True,
                "application_pipeline_cache": False,
            },
            "tiers": contract.budget_tiers(),
            "packaging_modes": [
                {
                    "id": contract.COMPACT_CONTAINER_ID,
                    "codec": proof_report["compact_candidate"]["codec_id"],
                    "layout": proof_report["compact_candidate"]["layout_id"],
                    "status": "selected_contract_only",
                    "clean_system_checked": False,
                },
                {
                    "id": "crinkler",
                    "status": "excluded",
                    "reason": "x64 support is neither assumed nor verified; no executable packer is invoked",
                },
                {
                    "id": "zlib-bzip2-lzma-decoder",
                    "status": "excluded",
                    "reason": "no measured player decoder/dependency closure for these options",
                },
            ],
            "compact_target": {
                "bytes": contract.COMPACT_TARGET_BYTES,
                "universal_feasibility_claim": False,
                "physical_result": None,
                "classification": "continue",
            },
            "minimum_measured_artifact": {
                "bytes": min(
                    proof_report["ledgers"]["compact"][
                        "complete_runnable_distribution_bytes"
                    ],
                    intro_report["ledgers"]["compact"][
                        "complete_runnable_distribution_bytes"
                    ],
                ),
                "proof_compact_bytes": proof_report["ledgers"]["compact"][
                    "complete_runnable_distribution_bytes"
                ],
                "intro_compact_bytes": intro_report["ledgers"]["compact"][
                    "complete_runnable_distribution_bytes"
                ],
                "evidence_status": "informational_contract_only",
                "physical_result": None,
            },
            "proof": {
                "report_path": "benchmarks/nw10/proof-export-report.v1.json",
                "report_sha256": _sha256(archived_proof_report),
                "canonical_graph": contract.CANONICAL_PROOF_GRAPH,
                "canonical_config": contract.CANONICAL_PROOF_CONFIG,
                "canonical_artifact": proof_report["canonical_artifact"],
                "compact_candidate": proof_report["compact_candidate"],
                "ledgers": proof_report["ledgers"],
                "measurements": proof_report["measurements"],
                "reproducibility": {
                    "matching": (
                        proof_report.get("reproducibility", {}).get("matching")
                        if isinstance(proof_report.get("reproducibility"), dict)
                        else None
                    ),
                    "fields": (
                        proof_report.get("reproducibility", {}).get("fields")
                        if isinstance(proof_report.get("reproducibility"), dict)
                        else None
                    ),
                },
                "decoder_code": decoder,
            },
            "intro": {
                "report_path": "benchmarks/nw10/intro-export-report.v1.json",
                "report_sha256": _sha256(archived_intro_report),
                "profile": intro_report["profile"],
                "canonical_artifact": intro_report["canonical_artifact"],
                "compact_artifact": intro_report["compact_artifact"],
                "ledgers": intro_report["ledgers"],
                "measurements": intro_report["measurements"],
                "decision": intro_report["decision"],
            },
            "alternatives": {
                "parameter_packing": proof_report["measurements"]["parameter_packing"],
                "delta_references": proof_report["measurements"]["delta_references"],
                "layouts": proof_report["measurements"]["layouts"],
                "shader_specialization": proof_report["measurements"]["shaders"],
                "compression": proof_report["measurements"]["compression_candidates"],
                "linker_elimination": {
                    "status": "pending_physical_pe",
                    "reason": (
                        "Exact release-PE section and symbol elimination bytes are "
                        "not substituted with source-level estimates."
                    ),
                },
            },
            "dominant_costs": _dominant_costs(proof_report),
            "blockers": [
                {
                    "id": "physical_windows_artifact",
                    "status": "pending",
                    "reason": (
                        "The exact Windows x64 release executable, clean-system "
                        "startup, and dependency checks require the pinned Windows "
                        "toolchain and a clean target host."
                    ),
                },
                {
                    "id": "nw07_reference_capture",
                    "status": "pending",
                    "reason": (
                        "nw07_perf_ref_v1 exact reference capture and bindings are "
                        "unavailable; no substitute hardware can certify preservation."
                    ),
                },
                {
                    "id": "linker_elimination_bytes",
                    "status": "pending",
                    "reason": (
                        "Release PE section/symbol elimination costs are measured only "
                        "on the exact Windows artifact; source-level estimates are not "
                        "substituted."
                    ),
                },
            ],
            "decision": {
                "classification": "continue",
                "reason": (
                    "Contract-only exports, codec fuzzing, byte accounting, and "
                    "deterministic compact payloads are implemented. Physical Windows "
                    "x64 and reference-capture evidence remain pending."
                ),
                "custom_compressor_started": False,
                "existing_compression_selected": (
                    proof_report["compact_candidate"]["codec_id"]
                ),
                "crt_free_experiment": "not_pursued_no_measured_net_benefit",
                "universal_64k_claim": False,
            },
        }
        proof_path = output / "compact-proof.v1.json"
        intro_path = output / "intro-profile.v1.json"
        status_path = output / "evidence-status.v1.json"
        _write_text_lf(
            proof_path,
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        )
        _write_text_lf(
            intro_path,
            json.dumps(
                {
                    "format": "nw10-intro-profile",
                    "version": 1,
                    "task": contract.TASK,
                    "implementation_base": contract.IMPLEMENTATION_BASE,
                    **intro_report["profile"],
                    "artifact": intro_report["compact_artifact"],
                    "ledger": intro_report["ledgers"]["compact"],
                    "evidence_status": "informational_contract_only",
                    "physical_measurements": "pending_physical_windows_capture",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        # Keep the explicit non-certifying records reproducible.  Never
        # overwrite a tracked verified physical record from contract-only
        # regeneration.
        previous_status: dict[str, Any] = {}
        if status_path.is_file():
            try:
                previous_status = json.loads(
                    status_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                previous_status = {}
        head = _git_head()
        preserve_physical = _physical_evidence_is_current(
            previous_status, head, output
        )
        if (
            not preserve_physical
            or _record_status(
                output / "common-artifact-correctness.v1.json"
            )
            != "verified"
        ):
            publisher._write(
                output / "common-artifact-correctness.v1.json",
                publisher.build_pending_correctness(),
            )
        if (
            not preserve_physical
            or _record_status(output / "system-validation.v1.json")
            != "verified"
        ):
            publisher._write(
                output / "system-validation.v1.json",
                publisher.build_pending_system_validation(),
            )
        status_files = [
            {
                "path": "benchmarks/nw10/compact-proof.v1.json",
                "sha256": _sha256(proof_path),
            },
            {
                "path": "benchmarks/nw10/intro-profile.v1.json",
                "sha256": _sha256(intro_path),
            },
            {
                "path": "benchmarks/nw10/proof-export-report.v1.json",
                "sha256": _sha256(archived_proof_report),
            },
            {
                "path": "benchmarks/nw10/intro-export-report.v1.json",
                "sha256": _sha256(archived_intro_report),
            },
        ]
        known = {
            "compact-proof.v1.json",
            "intro-profile.v1.json",
            "proof-export-report.v1.json",
            "intro-export-report.v1.json",
            "evidence-status.v1.json",
        }
        for sibling in sorted(output.glob("*.v1.json")):
            if sibling.name in known:
                continue
            status_files.append(
                {
                    "path": f"benchmarks/nw10/{sibling.name}",
                    "sha256": _sha256(sibling),
                }
            )
        _write_text_lf(
            status_path,
            json.dumps(
                _merge_status(
                    status_path,
                    files=status_files,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    return evidence


_PHYSICAL_STATUS_FIELDS = (
    "source_head_sha",
    "evidence_status",
    "physical_evidence",
    "reference_capture",
    "ada_correctness",
    "blackwell_correctness",
    "clean_system",
    "startup",
    "memory",
    "frame_time",
    "system_validation",
    "performance_preservation",
    "correctness_evidence",
    "compact_feasibility",
    "pe_ledger",
    "superseded_physical_evidence",
)


def _merge_status(status_path: Path, *, files: list[dict[str, str]]) -> dict[str, Any]:
    """Regenerate contract evidence without silently deleting physical claims.

    The documented contract-evidence command is run routinely.  It must never
    downgrade a tracked status that records physical evidence.  It refreshes
    the contract file hashes and keeps the existing physical state; a fresh
    clean checkout with no status file starts from an explicit pending state.
    """
    status: dict[str, Any] = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "evidence_status": "pending_physical_windows_capture",
        "contract_evidence": "verified_by_pytest",
        "physical_evidence": "pending",
        "reference_capture": "pending_reference_capture",
        "ada_correctness": "pending_unverified",
        "blackwell_correctness": "pending_unverified",
        "clean_system": "pending_unverified",
        "startup": "pending_unverified",
        "memory": "pending_unverified",
        "frame_time": "pending_unverified",
    }
    current_head = _git_head()
    status["source_head_sha"] = None
    existing = status_path if status_path.is_file() else None
    if existing is not None:
        previous = json.loads(existing.read_text(encoding="utf-8"))
        # Withdrawal history is attribution, not a physical claim.  Keep it
        # even when the current state is pending so the executed failure and
        # the reason its evidence cannot certify remain visible.
        if isinstance(previous.get("superseded_physical_evidence"), dict):
            status["superseded_physical_evidence"] = previous[
                "superseded_physical_evidence"
            ]
        if _physical_evidence_is_current(
            previous, current_head, status_path.parent
        ):
            for field in _PHYSICAL_STATUS_FIELDS:
                if field in previous:
                    status[field] = previous[field]
        elif previous.get("evidence_status") not in (
            None,
            "pending_physical_windows_capture",
        ):
            status["superseded_physical_evidence"] = {
                "reason": (
                    "The tracked physical evidence was captured at a different "
                    "source head or lacks a source-head binding, so it cannot "
                    "support the current checkout. Contract evidence was "
                    "regenerated and physical status was reset to pending."
                ),
                "previous_source_head_sha": previous.get("source_head_sha"),
                "current_source_head_sha": current_head or None,
            }
    status["files"] = files
    return status


def _physical_evidence_is_current(
    previous: dict[str, Any],
    current_head: str,
    evidence_root: Path | None = None,
) -> bool:
    """Preserve verified physical state only with a complete hash-bound chain.

    A matching source-head string is necessary but never sufficient. Every
    physical claim must have its corresponding published summary, that summary
    must still exist with the recorded canonical-text hash, and the same hash
    must also be present in status.files. This prevents a hand-edited status
    JSON from laundering a verified state through contract regeneration.
    """
    if not current_head or previous.get("source_head_sha") != current_head:
        return False
    if previous.get("reused_physical_evidence"):
        return False

    required: set[str] = set()
    if (
        previous.get("ada_correctness") == "verified"
        or previous.get("blackwell_correctness") == "verified"
    ):
        required.add("correctness_evidence")
    if any(
        previous.get(field) == "verified"
        for field in ("clean_system", "startup", "memory")
    ):
        required.add("system_validation")
    if (
        previous.get("reference_capture") == "verified_reference_capture"
        or previous.get("frame_time") == "verified_preserved"
    ):
        required.add("performance_preservation")
    compact = previous.get("compact_feasibility")
    if isinstance(compact, dict) and compact.get("status") == "verified_windows_runtime":
        required.add("compact_feasibility")
    pe = previous.get("pe_ledger")
    if isinstance(pe, dict) and pe.get("status") == "verified":
        required.add("pe_ledger")

    claims_physical = previous.get("physical_evidence") not in (None, "pending")
    claims_complete = previous.get("evidence_status") not in (
        None,
        "pending_physical_windows_capture",
    )
    if (claims_physical or claims_complete) and not required:
        return False
    if not required:
        return False

    file_hashes: dict[str, str] = {}
    rows = previous.get("files")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        relative = row.get("path")
        digest = row.get("sha256")
        if isinstance(relative, str) and isinstance(digest, str):
            file_hashes[relative.replace("\\", "/")] = digest.lower()

    root = evidence_root or (REPO_ROOT / "benchmarks" / "nw10")
    for field in required:
        entry = previous.get(field)
        if not isinstance(entry, dict) or entry.get("status") not in (
            "verified",
            "verified_preserved",
            "verified_windows_runtime",
        ):
            return False
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            return False
        relative = relative.replace("\\", "/")
        if relative.startswith("benchmarks/nw10/"):
            candidate = root / Path(relative).name
        else:
            candidate = REPO_ROOT / relative
        if not candidate.is_file():
            return False
        actual = _sha256(candidate)
        if actual != digest.lower():
            return False
        if file_hashes.get(relative) != actual:
            return False
    return True

def _record_status(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return str(value.get("status", "")) if isinstance(value, dict) else ""


def _git_head() -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and len(value) == 40 else ""


def _dominant_costs(report: dict[str, Any]) -> list[dict[str, Any]]:
    canonical = report["canonical_artifact"]
    ledger = report["ledgers"]["canonical"]
    measurements = report["measurements"]
    return [
        {
            "component": "player_image",
            "bytes": ledger["components"]["player_image_bytes"],
            "status": "contract_only_template",
        },
        {
            "component": "package",
            "bytes": canonical["package_bytes"],
            "status": "contract_only_structural_dxil",
        },
        {
            "component": "shader_payload",
            "bytes": measurements["package"]["shader_bytes"],
            "status": "contract_only_structural_dxil",
        },
        {
            "component": "parameter_payload",
            "bytes": measurements["package"]["parameter_bytes"],
            "status": "measured",
        },
        {
            "component": "decoder_code",
            "bytes": report.get("decoder_code", {}).get("bytes"),
            "status": report.get("decoder_code", {}).get("status", "pending_physical_windows"),
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "nw10",
    )
    args = parser.parse_args(argv)
    evidence = build_evidence(args.output_dir)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
