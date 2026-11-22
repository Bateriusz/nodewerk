from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_feasibility as feasibility  # noqa: E402
import nw10_contract as contract  # noqa: E402
from schema_validator import validate_instance  # noqa: E402
import json  # noqa: E402
import pytest  # noqa: E402


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
        "compact_candidate": {
            "executable_bytes": 64_000,
            "executable_sha256": "f" * 64,
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
            "package_bytes": 4_000,
            "manifest_sha256": "d" * 64,
            "manifest_bytes": 6_000,
        },
        "compact_artifact": {
            "executable_bytes": 65_000,
            "executable_sha256": "e" * 64,
            "selected_executable_bytes": 65_000,
            "selected_executable_sha256": "e" * 64,
            "selected_mode": "compact",
            "decoded_package_sha256": "c" * 64,
            "decoded_package_bytes": 4_000,
            "encoded_package_bytes": 1_700,
            "encoded_package_sha256": "f" * 64,
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
        },
        "ledgers": {
            "canonical": {
                "all_required_bytes_accounted": True,
                "dependencies": [
                    {"name": "d3d12", "kind": "windows_inbox", "distributed_bytes": 0}
                ],
            },
            "compact": {
                "all_required_bytes_accounted": False,
                "unaccounted_components": ["decoder_code_bytes"],
                "components": {"decoder_code_bytes": None},
                "required_auxiliary_files": {},
                "dependencies": [
                    {
                        "name": "cabinet.dll",
                        "kind": "windows_inbox",
                        "distributed_bytes": 0,
                    },
                    {
                        "name": "d3d12",
                        "kind": "windows_inbox",
                        "distributed_bytes": 0,
                    },
                ],
                "complete_runnable_distribution_bytes": 65_000,
            },
        },
        "visual_validation": {
            "status": "verified",
            "numeric_absolute": 0.0,
            "image_absolute": 0.0,
        },
    }


def _pe_ledger() -> dict:
    return {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "task": "NW-10",
        "final_artifact": {
            "executable_sha256": "e" * 64,
            "executable_bytes": 65_000,
            "package_sha256": "c" * 64,
            "package_binding_verified": True,
            "manifest_binding_verified": True,
        },
        "linker_elimination": {
            "security_mitigations_preserved": True,
            "canonical_package_identical": True,
            "compact_decoder_preserved": True,
        },
        "compact_decoder": {
            "compact_payload_verified": True,
            "template_bytes_added": 3_500,
        },
        "conclusion": {"byte_ledger_complete": True},
    }


def _dependency_closure() -> dict:
    return {
        "closure_verified": True,
        "binary_sha256": "e" * 64,
        "sidecar_files": [],
        "canonical_imports": ["cabinet.dll", "d3d12.dll", "kernel32.dll"],
    }


def _system_validation() -> dict:
    return {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "final_artifact": {
            "executable_sha256": "e" * 64,
            "package_sha256": "c" * 64,
        },
        "clean_system": {
            "status": "verified",
            "deployment": {
                "only_runtime_executable": True,
                "executable_sha256": "e" * 64,
                "executable_bytes": 65_000,
            },
            "host": {"clean_environment": True},
            "development_dependencies_present_on_path": [],
            "environment_cache_variables": [],
            "scenarios": [
                {"status": "passed", "timed_out": False} for _ in range(6)
            ],
        },
        "startup": {
            "status": "verified",
            "process_start_to_first_present_ms": 100.0,
        },
        "memory": {
            "status": "verified",
            "peak_engine_committed_gpu_bytes": 1_000_000,
            "post_initialization_allocation_violation": 0,
        },
    }


def _performance() -> dict:
    return {
        "format": "nw10-performance-preservation",
        "performance_contract": "nw07_perf_ref_v1",
        "performance_protocol": "nw07-perf-run.v1",
        "reference_binding_id": "binding-1",
        "status": "verified_preserved",
        "after": {
            "worst_gpu_p99_ms": 10.0,
            "worst_cpu_submit_p99_ms": 5.0,
            "runs": [{"status": "passed"} for _ in range(3)],
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
        "intro_compact_candidate_bytes": 65_000,
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
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is True
    assert record["classification"] == "complete"
    assert record["compact_candidate"]["runtime_status"] == "validated_runnable"
    assert record["acceptance_linkage"]["true_64k_artifact_delivered"] is True
    assert record["certification"]["status"] == "verified"


def test_feasibility_byte_target_alone_cannot_certify_success() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["acceptance_linkage"]["true_64k_artifact_delivered"] is False
    assert record["certification"]["status"] == "pending"
    assert "clean_system" in record["certification"]["pending_gates"]
    assert "dependency_closure" in record["certification"]["pending_gates"]
    assert "pe_ledger_binding" in record["certification"]["pending_gates"]
    assert any(
        blocker["id"] == "compact_certification_evidence"
        for blocker in record["blockers"]
    )


def test_validated_compact_identity_is_not_taken_from_smaller_proof_candidate() -> None:
    proof = _proof()
    proof["compact_candidate"]["executable_bytes"] = 60_000
    proof["compact_candidate"]["executable_sha256"] = "9" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=60_000,
            intro_compact_candidate_bytes=65_000,
            proof_compact_candidate_bytes=60_000,
            minimum_validated_runnable_bytes=65_000,
            validated_mode="compact",
        ),
        proof=proof,
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    assert record["measurements"]["minimum_measured_candidate_bytes"] == 60_000
    assert record["compact_candidate"]["bytes"] == 65_000
    assert record["compact_candidate"]["sha256"] == "e" * 64
    assert record["selected_artifact"]["bytes"] == 65_000
    assert record["selected_artifact"]["sha256"] == "e" * 64


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


def test_feasibility_records_canonical_proof_above_the_frozen_tier() -> None:
    proof = _proof()
    proof["canonical_artifact"]["executable_bytes"] = 262_145
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=proof,
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["measurements"]["canonical_proof_within_tier"] is False
    assert record["blocker"]["id"] == "canonical_proof_tier"
    assert record["blocker"]["measured_bytes"] == 262_145


def test_feasibility_accepts_decoder_off_size_blocked_report_without_artifact() -> None:
    proof = _proof()
    proof.pop("canonical_artifact")
    proof.update(
        {
            "measurement_kind": "release_decoder_off_size_blocked",
            "success": False,
            "measurement_outcome": "size_target_not_met",
            "measured_distribution_bytes": 270_000,
        }
    )
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_validated_runnable_bytes=None,
            validated_mode="none",
            compact_candidate_runtime_status="pending_runtime_validation",
        ),
        proof=proof,
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    validate_instance(record, _schema(), _schema())
    assert record["classification"] == "continue"
    assert record["measurements"]["proof_measured_bytes"] == 270_000
    assert record["measurements"]["proof_measurement_status"] == (
        "measured_decoder_off_size_blocked"
    )
    assert record["blockers"][0]["id"] == "canonical_proof_tier"


def test_feasibility_reproducibly_selects_prior_valid_ledger_when_current_is_pending() -> None:
    prior = {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "task": "NW-10",
        "final_artifact": {
            "executable_sha256": "9" * 64,
            "executable_bytes": 240_702,
            "runtime_status": "validated_runnable",
        },
        "conclusion": {"byte_ledger_complete": True},
        "source": {"head_sha": "8" * 40},
    }
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_validated_runnable_bytes=None,
            validated_mode="canonical_fallback_pending_physical",
            compact_candidate_runtime_status="pending_runtime_validation",
            compact_candidate_exit_code=-1073741819,
            canonical_fallback_exit_code=-1073741819,
        ),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        prior_valid_ledger=prior,
        prior_valid_ledger_path="benchmarks/nw10/windows-pe-ledger.v1.json",
    )
    validate_instance(record, _schema(), _schema())
    assert record["prior_valid_artifact"]["executable_sha256"] == "9" * 64
    assert record["selected_artifact"]["sha256"] == "9" * 64
    assert record["selected_artifact"]["source"] == "prior_valid_artifact"
    assert record["selected_artifact"]["ledger"] == (
        "benchmarks/nw10/windows-pe-ledger.v1.json"
    )


def test_feasibility_rejects_malformed_or_unattributed_prior_ledger() -> None:
    prior = {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "task": "NW-10",
        "final_artifact": {
            "executable_sha256": "9" * 64,
            "executable_bytes": 240_702,
            "runtime_status": "validated_runnable",
        },
        "conclusion": {"byte_ledger_complete": True},
    }
    kwargs = dict(
        outcome=_outcome(
            minimum_validated_runnable_bytes=None,
            validated_mode="canonical_fallback_pending_physical",
            compact_candidate_runtime_status="pending_runtime_validation",
            compact_candidate_exit_code=-1073741819,
            canonical_fallback_exit_code=-1073741819,
        ),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        prior_valid_ledger=prior,
    )
    with pytest.raises(ValueError, match="provenance path is required"):
        feasibility.build_feasibility(**kwargs)

    prior["format"] = "wrong"
    with pytest.raises(ValueError, match="unexpected format"):
        feasibility.build_feasibility(
            **kwargs,
            prior_valid_ledger_path="benchmarks/nw10/windows-pe-ledger.v1.json",
        )

    prior["format"] = "nw10-windows-pe-ledger"
    prior["final_artifact"]["executable_sha256"] = "g" * 64
    with pytest.raises(ValueError, match="SHA-256 is malformed"):
        feasibility.build_feasibility(
            **kwargs,
            prior_valid_ledger_path="benchmarks/nw10/windows-pe-ledger.v1.json",
        )


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
    assert "--prior-valid-ledger" in workflow
    assert "Bind PE ledger to the uploaded artifact identity" in workflow
    assert "--bind-source-report" in workflow
    assert "nw10-windows-pe-ledger-bound.json" in workflow
