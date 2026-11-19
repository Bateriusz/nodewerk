from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_feasibility as feasibility  # noqa: E402
from schema_validator import validate_instance  # noqa: E402
import json  # noqa: E402


def _proof() -> dict:
    return {
        "release_grade": True,
        "test_payload": False,
        "canonical_artifact": {
            "executable_bytes": 260_000,
            "executable_sha256": "a" * 64,
        },
    }


def _intro() -> dict:
    return {
        "release_grade": True,
        "test_payload": False,
        "canonical_artifact": {
            "executable_bytes": 240_000,
            "executable_sha256": "b" * 64,
            "package_sha256": "c" * 64,
            "manifest_sha256": "d" * 64,
        },
        "compact_artifact": {
            "selected_executable_bytes": 65_000,
            "selected_executable_sha256": "e" * 64,
        },
    }


def _outcome(**overrides) -> dict:
    value = {
        "minimum_measured_candidate_bytes": 65_000,
        "minimum_validated_runnable_bytes": 65_000,
        "validated_mode": "compact",
        "compact_candidate_runtime_status": "validated_runnable",
        "compact_candidate_exit_code": 0,
        "proof_compact_candidate_bytes": 65_000,
    }
    value.update(overrides)
    return value


def _schema() -> dict:
    return json.loads(
        (
            REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json"
        ).read_text(encoding="utf-8")
    )


def test_feasibility_success_branch_is_schema_valid() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest="sha256:" + "3" * 64,
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is True
    assert record["classification"] == "complete"
    assert record["compact_candidate"]["runtime_status"] == "validated_runnable"
    assert record["acceptance_linkage"]["true_64k_artifact_delivered"] is True


def test_feasibility_failure_branch_keeps_canonical_fallback_and_continue() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=227_715,
            minimum_validated_runnable_bytes=240_702,
            validated_mode="canonical_fallback",
            compact_candidate_runtime_status="rejected_runtime_failure",
            compact_candidate_exit_code=70,
            proof_compact_candidate_bytes=227_715,
        ),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest="sha256:" + "3" * 64,
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["compact_candidate"]["runtime_status"] == "rejected_runtime_failure"
    assert record["canonical_fallback"]["runtime_status"] == "validated_runnable"


def test_feasibility_rejects_canonical_proof_above_the_frozen_tier() -> None:
    proof = _proof()
    proof["canonical_artifact"]["executable_bytes"] = 262_145
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=proof,
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
        )
    except ValueError as exc:
        assert "262,144" in str(exc)
    else:
        raise AssertionError("canonical proof above the tier was accepted")
