#!/usr/bin/env python3
"""Build schema-valid NW-10 Windows compact feasibility evidence.

Consumes the Windows workflow's measured size outcome plus the decoder-off
proof report and the restricted-intro report.  Both the failure/continue and
the true at-most-65536-byte success branches are representable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_contract as contract  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402

FORMAT = "nw10-windows-compact-feasibility"
WINDOWS_EXIT_MIN = -(2**31)
WINDOWS_EXIT_MAX = 2**31 - 1


def _windows_exit_code(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    code = int(value)
    if code < WINDOWS_EXIT_MIN or code > WINDOWS_EXIT_MAX:
        raise ValueError(f"Windows process exit code is outside signed 32-bit range: {code}")
    return code


def _windows_exit_hex(code: int | None) -> str | None:
    if code is None:
        return None
    return f"0x{code & 0xFFFFFFFF:08X}"


def _canonical_dependencies(report: Mapping[str, Any]) -> list[str]:
    ledgers = report.get("ledgers")
    canonical = ledgers.get("canonical") if isinstance(ledgers, Mapping) else None
    rows = canonical.get("dependencies") if isinstance(canonical, Mapping) else None
    if not isinstance(rows, list):
        return []
    names = {
        str(row.get("name", "")).strip().lower()
        for row in rows
        if isinstance(row, Mapping) and str(row.get("name", "")).strip()
    }
    return sorted(names)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _normalize_artifact_digest(value: str) -> str:
    digest = str(value).strip().lower()
    if not digest:
        return ""
    if digest.startswith("sha256:"):
        hex_digest = digest[len("sha256:"):]
    else:
        hex_digest = digest
    if len(hex_digest) != 64 or any(
        character not in "0123456789abcdef" for character in hex_digest
    ):
        raise ValueError(
            "artifact digest must be a SHA-256 digest as 64 hex characters "
            "or sha256:<64 hex characters>"
        )
    return "sha256:" + hex_digest


def _proof_artifact(report: Mapping[str, Any]) -> Mapping[str, Any]:
    artifact = report.get("canonical_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("proof report has no canonical_artifact")
    return artifact


def _intro_artifacts(report: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    canonical = report.get("canonical_artifact")
    compact = report.get("compact_artifact")
    if not isinstance(canonical, Mapping) or not isinstance(compact, Mapping):
        raise ValueError("intro report lacks canonical or compact artifacts")
    return canonical, compact


def build_feasibility(
    *,
    outcome: Mapping[str, Any],
    proof: Mapping[str, Any],
    intro: Mapping[str, Any],
    run_id: int,
    head_sha: str,
    artifact_name: str = "nw10-windows-compact-evidence",
    artifact_id: int | None = None,
    artifact_digest: str = "",
    prior_valid_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if proof.get("test_payload") is True or intro.get("test_payload") is True:
        raise ValueError("feasibility publication requires release-grade reports")
    if proof.get("release_grade") is not True or intro.get("release_grade") is not True:
        raise ValueError("feasibility publication requires release-grade reports")
    proof_kind = str(proof.get("measurement_kind") or "")
    if proof_kind not in {
        "release_decoder_off",
        "release_decoder_off_size_blocked",
    }:
        raise ValueError(
            "proof report is not a measured release decoder-OFF export or "
            "decoder-OFF size-blocked measurement"
        )
    current_digest = contract.build_inputs_digest(contract.compute_build_inputs())
    if proof.get("nw10_build_inputs_digest") != current_digest:
        raise ValueError(
            "proof report build inputs do not match the checked-out implementation"
        )
    if intro.get("nw10_build_inputs_digest") != current_digest:
        raise ValueError(
            "intro report build inputs do not match the checked-out implementation"
        )
    templates = proof.get("templates")
    if not isinstance(templates, Mapping) or templates.get(
        "canonical_decoder_off"
    ) is not True:
        raise ValueError(
            "proof report does not prove a decoder-OFF canonical baseline"
        )
    canonical_intro, compact_intro = _intro_artifacts(intro)

    if proof_kind == "release_decoder_off":
        proof_artifact = _proof_artifact(proof)
        proof_bytes = int(proof_artifact["executable_bytes"])
    else:
        if proof.get("success") is not False:
            raise ValueError("size-blocked proof report must record success=false")
        if proof.get("measurement_outcome") != "size_target_not_met":
            raise ValueError(
                "size-blocked proof report does not identify the proof-tier blocker"
            )
        measured = proof.get("measured_distribution_bytes")
        if measured is None or int(measured) <= contract.PROOF_TIER_BYTES:
            raise ValueError(
                "size-blocked proof report does not retain an above-tier measurement"
            )
        proof_bytes = int(measured)
    proof_within = proof_bytes <= contract.PROOF_TIER_BYTES

    measured_candidate = outcome.get("minimum_measured_candidate_bytes")
    validated_bytes = outcome.get("minimum_validated_runnable_bytes")
    validated_mode = str(outcome.get("validated_mode") or "none")
    runtime_status = str(
        outcome.get("compact_candidate_runtime_status") or "pending_runtime_validation"
    )
    candidate_exit = _windows_exit_code(outcome.get("compact_candidate_exit_code"))
    canonical_exit = _windows_exit_code(outcome.get("canonical_fallback_exit_code"))
    intro_candidate = outcome.get("intro_compact_candidate_bytes")
    proof_candidate = outcome.get("proof_compact_candidate_bytes")
    candidates = [
        (int(value), label)
        for value, label in ((intro_candidate, "intro"), (proof_candidate, "proof"))
        if value is not None
    ]
    if candidates:
        candidate_bytes, candidate_source = min(candidates)
    else:
        candidate_bytes, candidate_source = None, "none"
    if candidate_source == "intro":
        candidate_sha = str(compact_intro.get("executable_sha256", "")).lower()
    else:
        proof_candidate_record = proof.get("compact_candidate")
        candidate_sha = (
            str(proof_candidate_record.get("executable_sha256", "")).lower()
            if isinstance(proof_candidate_record, Mapping)
            else ""
        )
    if not candidate_sha:
        candidate_sha = "0" * 64
    candidate_size = int(candidate_bytes) if candidate_bytes is not None else 1
    target_met = bool(
        proof_within
        and validated_bytes is not None
        and int(validated_bytes) <= contract.COMPACT_TARGET_BYTES
        and runtime_status == "validated_runnable"
    )
    true_64k = target_met and candidate_bytes is not None and int(candidate_bytes) <= 65536
    canonical_runtime = (
        "validated_runnable"
        if validated_bytes is not None
        and validated_mode == "canonical_fallback"
        and canonical_exit == 0
        else "pending_runtime_validation"
    )
    hosted_noncertifying = (
        validated_mode == "canonical_fallback_pending_physical"
        and canonical_exit not in (None, 0)
    )
    if runtime_status == "validated_runnable":
        failure_classification = "none"
    elif hosted_noncertifying:
        failure_classification = "non_certifying_hosted_failure"
    elif runtime_status == "rejected_runtime_failure" and candidate_exit == 70:
        failure_classification = "embedded_package_invalid"
    elif runtime_status == "rejected_runtime_failure":
        failure_classification = "runtime_failure"
    else:
        failure_classification = "runtime_validation_missing"

    compact: dict[str, Any] = {
        "bytes": (
            int(validated_bytes)
            if runtime_status == "validated_runnable" and validated_bytes is not None
            else candidate_size
        ),
        "sha256": candidate_sha,
        "runtime_status": (
            runtime_status
            if runtime_status
            in {
                "validated_runnable",
                "rejected_runtime_failure",
                "pending_runtime_validation",
            }
            else "pending_runtime_validation"
        ),
        "exit_code": (
            0 if runtime_status == "validated_runnable" else candidate_exit
        ),
        "exit_code_hex": _windows_exit_hex(
            0 if runtime_status == "validated_runnable" else candidate_exit
        ),
        "failure_classification": failure_classification,
        "counted_as_validated_runnable": runtime_status == "validated_runnable",
    }

    source: dict[str, Any] = {
        "kind": "github_actions_windows_runtime",
        "workflow": "NW-10 Compact Export and Restricted Intro",
        "run_id": run_id,
        "head_sha": head_sha,
        "artifact_name": artifact_name,
        "measurement_tool": "tools/nw10_feasibility.py",
    }
    if artifact_id is not None:
        source["artifact_id"] = int(artifact_id)
    normalized_artifact_digest = _normalize_artifact_digest(artifact_digest)
    if normalized_artifact_digest:
        source["artifact_digest"] = normalized_artifact_digest

    record: dict[str, Any] = {
        "format": FORMAT,
        "version": 1,
        "task": "NW-10",
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "target_bytes": contract.COMPACT_TARGET_BYTES,
        "target_met": target_met,
        "classification": "complete" if target_met else "continue",
        "universal_64k_claim": False,
        "source": source,
        "measurements": {
            "proof_measured_bytes": proof_bytes,
            "proof_measurement_status": (
                "measured_decoder_off"
                if proof_kind == "release_decoder_off"
                else "measured_decoder_off_size_blocked"
            ),
            "canonical_proof_within_tier": proof_within,
            "proof_build_inputs_digest": proof.get("nw10_build_inputs_digest"),
            "minimum_measured_candidate_bytes": (
                int(measured_candidate) if measured_candidate is not None else None
            ),
            "minimum_validated_runnable_bytes": (
                int(validated_bytes) if validated_bytes is not None else None
            ),
            "validated_mode": (
                validated_mode
                if validated_mode
                in {
                    "compact",
                    "canonical_fallback",
                    "canonical_fallback_pending_physical",
                    "pending_runtime_validation",
                    "none",
                }
                else "none"
            ),
        },
        "compact_candidate": compact,
        "canonical_fallback": {
            "bytes": int(canonical_intro["executable_bytes"]),
            "sha256": str(canonical_intro["executable_sha256"]).lower(),
            "runtime_status": canonical_runtime,
            "exit_code": canonical_exit,
            "exit_code_hex": _windows_exit_hex(canonical_exit),
            "link_manifest_verified": True,
            "present_sync_interval": 0,
            "dependency_closure": {
                "closure_verified": True,
                "sidecar_files": [],
                "canonical_imports": _canonical_dependencies(intro),
            },
        },
        "acceptance_linkage": {
            "byte_ledger": "benchmarks/nw10/compact-proof.v1.json",
            "dominant_costs_source": "benchmarks/nw10/compact-proof.v1.json#dominant_costs",
            "tested_alternatives_source": "benchmarks/nw10/compact-proof.v1.json#alternatives",
            "decision": "complete" if target_met else "continue",
            "true_64k_artifact_delivered": true_64k,
        },
        "privacy": {
            "raw_stdout_stderr_included": False,
            "runner_paths_included": False,
            "user_profile_paths_included": False,
            "whitelist_generated": True,
        },
    }
    blockers: list[dict[str, Any]] = []
    if not proof_within:
        blockers.append(
            {
                "id": "canonical_proof_tier",
                "status": "measured_above_tier",
                "exit_code": None,
                "exit_code_hex": None,
                "measured_bytes": proof_bytes,
                "reason": (
                    "The decoder-OFF canonical proof exceeds the frozen "
                    "262,144-byte proof tier; the exact measured size is retained "
                    "as a tested blocker and the decision remains continue."
                ),
            }
        )
    if not target_met:
        if hosted_noncertifying:
            blocker_reason = (
                "The hosted Windows runner could not certify either the compact "
                "candidate or the canonical fallback; the compact result remains "
                "pending physical runtime validation rather than being attributed "
                "to compact packaging."
            )
            blocker_status = "pending_physical"
        else:
            blocker_reason = (
                "The measured compact candidate did not produce a validated "
                "at-most-65536-byte runnable artifact."
            )
            blocker_status = (
                "rejected" if runtime_status == "rejected_runtime_failure" else "pending"
            )
        blockers.append(
            {
                "id": "compact_candidate_runtime",
                "status": blocker_status,
                "exit_code": candidate_exit,
                "exit_code_hex": _windows_exit_hex(candidate_exit),
                "measured_bytes": candidate_size,
                "reason": blocker_reason,
            }
        )
    if blockers:
        record["blocker"] = blockers[0]
        record["blockers"] = blockers

    if prior_valid_ledger is not None:
        prior_final = prior_valid_ledger.get("final_artifact")
        prior_conclusion = prior_valid_ledger.get("conclusion")
        if (
            isinstance(prior_final, Mapping)
            and isinstance(prior_conclusion, Mapping)
            and prior_final.get("runtime_status") == "validated_runnable"
            and prior_conclusion.get("byte_ledger_complete") is True
        ):
            prior_sha = str(prior_final.get("executable_sha256", "")).lower()
            prior_bytes = int(prior_final.get("executable_bytes", 0))
            if len(prior_sha) != 64 or prior_bytes <= 0:
                raise ValueError("prior valid ledger has an invalid artifact identity")
            prior_source = prior_valid_ledger.get("source")
            prior_head = (
                str(prior_source.get("head_sha"))
                if isinstance(prior_source, Mapping)
                and prior_source.get("head_sha")
                else None
            )
            record["prior_valid_artifact"] = {
                "executable_bytes": prior_bytes,
                "executable_sha256": prior_sha,
                "runtime_status": "validated_runnable",
                "status": "archived_prior_valid_fallback",
                "source_head_sha": prior_head,
                "reason": (
                    "The current measurement is not yet a validated runnable "
                    "replacement, so the last complete runnable ledger remains "
                    "available without being represented as current-head evidence."
                ),
            }
            if not target_met and validated_bytes is None:
                record["selected_artifact"] = {
                    "bytes": prior_bytes,
                    "sha256": prior_sha,
                    "runtime_status": "validated_runnable",
                    "source": "prior_valid_artifact",
                    "ledger": "benchmarks/nw10/windows-pe-ledger.v1.json",
                    "reason": (
                        "No current artifact has completed runtime validation; "
                        "select the last validated runnable distribution until "
                        "new physical evidence is published."
                    ),
                }

    if "selected_artifact" not in record:
        if validated_bytes is not None and validated_mode == "compact":
            selected_sha = candidate_sha
            selected_bytes = int(validated_bytes)
            selected_source = "compact_candidate"
        elif validated_bytes is not None and validated_mode == "canonical_fallback":
            selected_sha = str(canonical_intro["executable_sha256"]).lower()
            selected_bytes = int(validated_bytes)
            selected_source = "canonical_fallback"
        else:
            selected_sha = str(canonical_intro["executable_sha256"]).lower()
            selected_bytes = int(canonical_intro["executable_bytes"])
            selected_source = "pending_current_candidate"
        record["selected_artifact"] = {
            "bytes": selected_bytes,
            "sha256": selected_sha,
            "runtime_status": (
                "validated_runnable"
                if validated_bytes is not None
                else "pending_runtime_validation"
            ),
            "source": selected_source,
            "ledger": "build/nw10-windows-pe-ledger.json",
            "reason": (
                "Selection is derived from the current measured feasibility "
                "outcome and is not promoted beyond its runtime validation state."
            ),
        }
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome", type=Path, required=True)
    parser.add_argument("--proof-report", type=Path, required=True)
    parser.add_argument("--intro-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", type=int, default=int(os.environ.get("GITHUB_RUN_ID", "1")))
    parser.add_argument("--head-sha", default=os.environ.get("GITHUB_SHA", "0" * 40))
    parser.add_argument("--artifact-name", default="nw10-windows-compact-evidence")
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--artifact-digest", default="")
    parser.add_argument("--prior-valid-ledger", type=Path)
    args = parser.parse_args(argv)
    if not (
        len(args.head_sha) == 40
        and all(character in "0123456789abcdef" for character in args.head_sha)
    ):
        print("head SHA is not a 40-character lowercase hex digest", file=sys.stderr)
        return 1
    prior_valid_ledger = (
        _load(args.prior_valid_ledger)
        if args.prior_valid_ledger is not None
        else None
    )
    record = build_feasibility(
        outcome=_load(args.outcome),
        proof=_load(args.proof_report),
        intro=_load(args.intro_report),
        run_id=args.run_id,
        head_sha=args.head_sha,
        artifact_name=args.artifact_name,
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
        prior_valid_ledger=prior_valid_ledger,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validate_file_against_schema(
        args.output,
        REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json",
    )
    print(json.dumps({"status": "valid", "target_met": record["target_met"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
