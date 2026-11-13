from __future__ import annotations

import argparse
import json
import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw06_evidence  # noqa: E402
import nwp_package as nwp  # noqa: E402
import verify_nw06  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402


EVIDENCE = REPO_ROOT / "benchmarks" / "nw06-hardware-evidence.json"
SCHEMA = REPO_ROOT / "schemas" / "nw06-hardware-evidence.schema.json"
OPERATOR_SCHEMA = REPO_ROOT / "schemas" / "nw06-operator-contracts.schema.json"
OPERATOR_DOCUMENT = REPO_ROOT / "schemas" / "nw06-operator-contracts.v1.json"


def _complete_capture(tier: str, *, artifact_key: str = "sha256:v1:artifact") -> dict:
    coverage = {
        "ada": "desktop_ada_representative",
        "blackwell": "desktop_blackwell_representative",
    }[tier]
    return {
        "status": "passed",
        "coverage_target": coverage,
        "evidence_role": "fixed_tick_correctness",
        "artifact_key": artifact_key,
        "package_sha256": "1" * 64,
        "executable_sha256": "2" * 64,
        "manifest_sha256": "sha256:v1:" + "3" * 64,
        "profile_id": nwp.COMMON_PROFILE_ID,
        "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
        "adapter_description": f"{tier} adapter",
        "device": f"{tier} device",
        "driver_version": "1.0",
        "os_version": "Windows 11",
        "toolchain_identity": "dxc:1.8.2407",
        "queried_capabilities": {"feature_level": "12_0"},
        "clock_information": {"core_mhz": 2000},
        "power_information": {"limit_w": 300},
        "thermal_information": {"c": 60},
        "cache_state": "cold",
        "capture_protocol": "nw06-fixed-tick-capture.v1",
        "unavailable_telemetry": [],
    }


def test_pending_evidence_is_valid_but_never_verified() -> None:
    validate_file_against_schema(EVIDENCE, SCHEMA)
    record = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert record["verification_status"] == "pending_unverified"
    assert nw06_evidence.validate_evidence_record(record)["status"] == "pending_unverified"
    assert record["profile_id"] == nwp.COMMON_PROFILE_ID
    assert record["requirements_digest"] == nwp.COMMON_REQUIREMENTS_DIGEST


def test_missing_capture_telemetry_cannot_pass() -> None:
    partial = nw06_evidence.validate_capture(
        {
            "status": "passed",
            "profile_id": nwp.COMMON_PROFILE_ID,
            "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
        }
    )
    assert partial["status"] == "pending_unverified"
    assert "adapter_description" in partial["missing_metadata"]
    assert "clock_information" in partial["missing_metadata"]
    assert "capture_protocol" in partial["missing_metadata"]


def test_executed_failure_remains_failure() -> None:
    result = nw06_evidence.validate_evidence_record(
        {
            "captures": [
                {
                    "status": "failed",
                    "profile_id": nwp.COMMON_PROFILE_ID,
                    "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
                }
            ]
        }
    )
    assert result["status"] == "failed"


def test_hardware_classification_is_metadata_only() -> None:
    assert nw06_evidence.classify_hardware_tier("NVIDIA GeForce RTX 4070") == "ada"
    assert nw06_evidence.classify_hardware_tier("NVIDIA GeForce RTX 5070 Ti") == "blackwell"
    assert nw06_evidence.classify_hardware_tier("Unknown adapter") == "unknown"
    record = nw06_evidence.pending_record()
    assert record["qualifications"]["ada"]["evidence_role"] == "fixed_tick_correctness"
    assert "artifact_key" in record["unavailable_telemetry"]


def test_nw06_operator_contract_document_is_schema_valid() -> None:
    validate_file_against_schema(OPERATOR_DOCUMENT, OPERATOR_SCHEMA)
    document = json.loads(OPERATOR_DOCUMENT.read_text(encoding="utf-8"))
    ids = {operator["operator_id"] for operator in document["operators"]}
    assert {
        "nw.texture.noise2d",
        "nw.mesh.torus",
        "nw.scene.instances",
        "nw.material.standard",
        "nw.camera.curve",
        "nw.animation.twist",
        "nw.render.instanced",
        "nw.post.bloom",
        "nw.post.tonemap",
    } <= ids


def test_operator_contract_lowerings_and_generation_frequencies_match_registry() -> None:
    document = json.loads(OPERATOR_DOCUMENT.read_text(encoding="utf-8"))
    expected_lowering = {
        "nw.texture.noise2d": "nw.lower.cpu.recipe",
        "nw.mesh.torus": "nw.lower.cpu.recipe",
        "nw.scene.instances": "nw.lower.cpu.scene",
        "nw.material.standard": "nw.lower.cpu.material",
        "nw.camera.curve": "nw.lower.cpu.camera",
        "nw.animation.twist": "nw.lower.cpu.animation",
        "nw.render.instanced": "nw.lower.hlsl.render",
        "nw.post.bloom": "nw.lower.hlsl.post",
        "nw.post.tonemap": "nw.lower.hlsl.post",
        "nw.residency.generate_texture": "nw.lower.hlsl.residency",
        "nw.residency.generate_mesh": "nw.lower.hlsl.residency",
        "nw.residency.generate_instances": "nw.lower.hlsl.residency",
    }
    by_id = {operator["operator_id"]: operator for operator in document["operators"]}
    for operator_id, lowering in expected_lowering.items():
        assert by_id[operator_id]["lowering"] == lowering
    for operator_id in (
        "nw.residency.generate_texture",
        "nw.residency.generate_mesh",
        "nw.residency.generate_instances",
    ):
        assert by_id[operator_id]["frequencies"] == ["initialization"]
    registry_source = (
        REPO_ROOT / "src" / "graph" / "nw_graph.cpp"
    ).read_text(encoding="utf-8")
    assert registry_source.count(
        "InitializationFrequency(), Frequency::Initialization"
    ) >= 3


def test_record_capture_writes_schema_valid_qualifications_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    package_bytes = b"nw06-test-package"
    artifact_key = "sha256:v1:test-artifact"
    manifest_sha256 = "sha256:v1:" + "4" * 64
    monkeypatch.setattr(
        nw06_evidence,
        "_artifact_identity",
        lambda directory: (
            directory / "artifact.nwp",
            package_bytes,
            artifact_key,
            manifest_sha256,
        ),
    )
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ" + b"\x00" * 64)
    output = tmp_path / "evidence.json"
    capture_metadata = _complete_capture("ada")
    for name in (
        "coverage_target",
        "evidence_role",
        "artifact_key",
        "package_sha256",
        "executable_sha256",
        "manifest_sha256",
        "profile_id",
        "requirements_digest",
        "status",
        "cache_state",
        "capture_protocol",
        "unavailable_telemetry",
    ):
        capture_metadata.pop(name, None)
    record = nw06_evidence.record_capture(
        artifact_directory=artifact,
        executable=executable,
        tier="ada",
        coverage_target="desktop_ada_representative",
        evidence_role="fixed_tick_correctness",
        status="passed",
        metadata=capture_metadata,
        output_path=output,
    )
    validate_file_against_schema(output, SCHEMA)
    recorded = json.loads(output.read_text(encoding="utf-8"))
    assert recorded["policy"]
    assert recorded["qualifications"]["ada"]["status"] == "verified"
    assert (
        recorded["qualifications"]["blackwell"]["status"]
        == "pending_unverified"
    )
    assert recorded["verification_status"] == "execution_recorded"
    assert record["captures"][0]["package_sha256"] == hashlib.sha256(
        package_bytes
    ).hexdigest()


def test_pending_qualifications_prevent_verified_status() -> None:
    captures = [_complete_capture("ada"), _complete_capture("blackwell")]
    pending = {
        "captures": captures,
        "qualifications": {
            "ada": {
                "coverage_target": "desktop_ada_representative",
                "evidence_role": "fixed_tick_correctness",
                "status": "pending_unverified",
            },
            "blackwell": {
                "coverage_target": "desktop_blackwell_representative",
                "evidence_role": "fixed_tick_correctness",
                "status": "pending_unverified",
            },
        },
    }
    assert (
        nw06_evidence.validate_evidence_record(pending)["status"]
        == "pending_unverified"
    )
    pending["qualifications"]["ada"]["status"] = "verified"
    pending["qualifications"]["blackwell"]["status"] = "verified"
    result = nw06_evidence.validate_evidence_record(pending)
    assert result["status"] == "verified"
    assert result["qualifications"] == {
        "ada": "verified",
        "blackwell": "verified",
    }


def test_wrong_evidence_role_or_coverage_target_is_failed() -> None:
    wrong_role = _complete_capture("ada")
    wrong_role["evidence_role"] = "warmup"
    assert nw06_evidence.validate_capture(wrong_role)["status"] == "failed"
    wrong_target = _complete_capture("ada")
    wrong_target["coverage_target"] = "desktop_unknown_representative"
    assert nw06_evidence.validate_capture(wrong_target)["status"] == "failed"


def test_full_verification_rejects_mismatched_artifact_and_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_bytes = b"nw06-test-package"
    artifact_key = "sha256:v1:expected-artifact"
    manifest_sha256 = "sha256:v1:" + "5" * 64
    monkeypatch.setattr(
        nw06_evidence,
        "_artifact_identity",
        lambda directory: (
            directory / "artifact.nwp",
            package_bytes,
            artifact_key,
            manifest_sha256,
        ),
    )
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ" + b"\x00" * 64)
    evidence = {
        "schema_version": "1.0.0",
        "task": "NW-06",
        "verification_status": "verified",
        "profile_id": nwp.COMMON_PROFILE_ID,
        "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
        "ticks_per_second": 48_000,
        "qualifications": {
            "ada": {
                "coverage_target": "desktop_ada_representative",
                "evidence_role": "fixed_tick_correctness",
                "status": "verified",
            },
            "blackwell": {
                "coverage_target": "desktop_blackwell_representative",
                "evidence_role": "fixed_tick_correctness",
                "status": "verified",
            },
        },
        "captures": [
            dict(
                _complete_capture("ada"),
                artifact_key="sha256:v1:stale-artifact",
                package_sha256="f" * 64,
            )
        ],
        "policy": "fixed-tick coverage",
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checks = verify_nw06.full_or_hardware(
        argparse.Namespace(
            fixture=str(executable),
            package=str(tmp_path / "artifact"),
            evidence=str(evidence_path),
        )
    )
    by_name = {check.name: check for check in checks}
    assert by_name["nw06_artifact_identity"].status == "failed"
    assert by_name["nw06_hardware_capture"].status == "failed"


def test_record_capture_rejects_tier_coverage_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        nw06_evidence,
        "_artifact_identity",
        lambda directory: (
            directory / "artifact.nwp",
            b"package",
            "sha256:v1:artifact",
            "sha256:v1:" + "6" * 64,
        ),
    )
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ" + b"\x00" * 32)
    with pytest.raises(ValueError, match="coverage target"):
        nw06_evidence.record_capture(
            artifact_directory=tmp_path,
            executable=executable,
            tier="blackwell",
            coverage_target="desktop_ada_representative",
            evidence_role="fixed_tick_correctness",
            status="passed",
            metadata=_complete_capture("ada"),
        )


def test_record_capture_merges_complete_qualification_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        nw06_evidence,
        "_artifact_identity",
        lambda directory: (
            directory / "artifact.nwp",
            b"package",
            "sha256:v1:artifact",
            "sha256:v1:" + "7" * 64,
        ),
    )
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ" + b"\x00" * 32)
    output = tmp_path / "evidence.json"
    # Simulate a pre-existing file with an empty qualifications object.
    output.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "task": "NW-06",
                "verification_status": "pending_unverified",
                "profile_id": nwp.COMMON_PROFILE_ID,
                "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
                "ticks_per_second": 48_000,
                "qualifications": {"ada": {"status": "pending_unverified"}},
                "policy": "fixed-tick coverage",
            }
        ),
        encoding="utf-8",
    )
    metadata = _complete_capture("ada")
    for name in (
        "coverage_target",
        "evidence_role",
        "artifact_key",
        "package_sha256",
        "executable_sha256",
        "manifest_sha256",
        "profile_id",
        "requirements_digest",
        "status",
    ):
        metadata.pop(name, None)
    nw06_evidence.record_capture(
        artifact_directory=tmp_path,
        executable=executable,
        tier="ada",
        coverage_target="desktop_ada_representative",
        evidence_role="fixed_tick_correctness",
        status="passed",
        metadata=metadata,
        output_path=output,
    )
    validate_file_against_schema(output, SCHEMA)
    recorded = json.loads(output.read_text(encoding="utf-8"))
    assert set(recorded["qualifications"]) == {"ada", "blackwell"}
    assert (
        recorded["qualifications"]["blackwell"]["status"]
        == "pending_unverified"
    )


def test_failed_qualification_without_captures_remains_failed() -> None:
    result = nw06_evidence.validate_evidence_record(
        {
            "captures": [],
            "qualifications": {
                "ada": {
                    "coverage_target": "desktop_ada_representative",
                    "evidence_role": "fixed_tick_correctness",
                    "status": "failed",
                },
                "blackwell": {
                    "coverage_target": "desktop_blackwell_representative",
                    "evidence_role": "fixed_tick_correctness",
                    "status": "pending_unverified",
                },
            },
        }
    )
    assert result["status"] == "failed"
