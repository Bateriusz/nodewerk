from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_feasibility as feasibility  # noqa: E402
import nw10_contract as contract  # noqa: E402
from schema_validator import validate_instance  # noqa: E402
import json  # noqa: E402


def _proof() -> dict:
    return {
        "release_grade": True,
        "test_payload": False,
        "measurement_kind": "release_decoder_off",
        "nw10_build_inputs_digest": contract.build_inputs_digest(
            contract.compute_build_inputs()
        ),
        "templates": {
            "canonical_decoder_off": True,
            "compact_decoder_on": True,
        },
        "canonical_artifact": {
            "executable_bytes": 260_000,
            "executable_sha256": "a" * 64,
        },
    }


def _intro() -> dict:
    return {
        "release_grade": True,
        "test_payload": False,
        "measurement_kind": "release_restricted_intro",
        "nw10_build_inputs_digest": contract.build_inputs_digest(
            contract.compute_build_inputs()
        ),
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
        "canonical_fallback_exit_code": None,
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


def test_feasibility_does_not_invent_unknown_artifact_identity() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    assert "artifact_id" not in record["source"]
    assert "artifact_digest" not in record["source"]


def test_feasibility_normalizes_github_artifact_digest_output() -> None:
    raw_digest = "3" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest=raw_digest,
    )
    assert record["source"]["artifact_digest"] == "sha256:" + raw_digest
    schema = _schema()
    validate_instance(record, schema, schema)


def test_feasibility_rejects_malformed_artifact_digest() -> None:
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=_proof(),
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
            artifact_id=2,
            artifact_digest="not-a-digest",
        )
    except ValueError as exc:
        assert "SHA-256 digest" in str(exc)
    else:
        raise AssertionError("malformed artifact digest was accepted")


def test_feasibility_failure_branch_keeps_canonical_fallback_and_continue() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=227_715,
            minimum_validated_runnable_bytes=240_702,
            validated_mode="canonical_fallback",
            compact_candidate_runtime_status="rejected_runtime_failure",
            compact_candidate_exit_code=70,
            canonical_fallback_exit_code=0,
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
    assert record["canonical_fallback"]["exit_code"] == 0


def test_hosted_access_violation_remains_pending_and_schema_valid() -> None:
    access_violation = -1073741819
    intro = _intro()
    intro["ledgers"] = {
        "canonical": {
            "dependencies": [
                {"name": "d3d12", "kind": "windows_inbox", "distributed_bytes": 0},
                {"name": "dxgi", "kind": "windows_inbox", "distributed_bytes": 0},
            ]
        }
    }
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=228_181,
            minimum_validated_runnable_bytes=None,
            validated_mode="canonical_fallback_pending_physical",
            compact_candidate_runtime_status="pending_runtime_validation",
            compact_candidate_exit_code=access_violation,
            canonical_fallback_exit_code=access_violation,
            intro_compact_candidate_bytes=228_181,
            proof_compact_candidate_bytes=238_188,
        ),
        proof=_proof(),
        intro=intro,
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest="sha256:" + "3" * 64,
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is False
    assert record["measurements"]["validated_mode"] == (
        "canonical_fallback_pending_physical"
    )
    assert record["compact_candidate"]["runtime_status"] == "pending_runtime_validation"
    assert record["compact_candidate"]["exit_code"] == access_violation
    assert record["compact_candidate"]["exit_code_hex"] == "0xC0000005"
    assert record["compact_candidate"]["failure_classification"] == (
        "non_certifying_hosted_failure"
    )
    assert record["canonical_fallback"]["runtime_status"] == "pending_runtime_validation"
    assert record["canonical_fallback"]["exit_code"] == access_violation
    assert record["canonical_fallback"]["exit_code_hex"] == "0xC0000005"
    assert record["canonical_fallback"]["dependency_closure"]["canonical_imports"] == [
        "d3d12",
        "dxgi",
    ]
    assert record["blocker"]["status"] == "pending_physical"


def test_windows_exit_code_rejects_values_outside_signed_32_bit() -> None:
    for value in (-(2**31) - 1, 2**31):
        try:
            feasibility._windows_exit_code(value)
        except ValueError as exc:
            assert "signed 32-bit" in str(exc)
        else:
            raise AssertionError("out-of-range Windows exit code was accepted")


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


def test_feasibility_requires_measured_release_decoder_off_proof() -> None:
    proof = _proof()
    proof["measurement_kind"] = "contract_only"
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=proof,
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
        )
    except ValueError as exc:
        assert "decoder-OFF" in str(exc)
    else:
        raise AssertionError("contract-only proof was accepted as feasibility evidence")

    proof = _proof()
    proof["nw10_build_inputs_digest"] = "sha256:v1:" + "0" * 64
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=proof,
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
        )
    except ValueError as exc:
        assert "build inputs" in str(exc)
    else:
        raise AssertionError("stale proof build inputs were accepted")


def test_workflow_binds_feasibility_to_the_uploaded_artifact_identity() -> None:
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "nw10-compact.yml"
    ).read_text(encoding="utf-8")
    assert "--artifact-id" in workflow
    assert "--artifact-digest" in workflow
    assert "steps.nw10_evidence_upload.outputs.artifact-id" in workflow
    assert "steps.nw10_evidence_upload.outputs.artifact-digest" in workflow
    assert "--runtime-status $runtimeStatus" in workflow
    assert "--runtime-exit-code $runtimeExitCode" in workflow
