from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw06_evidence  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402


EVIDENCE = REPO_ROOT / "benchmarks" / "nw06-hardware-evidence.json"
SCHEMA = REPO_ROOT / "schemas" / "nw06-hardware-evidence.schema.json"
OPERATOR_SCHEMA = REPO_ROOT / "schemas" / "nw06-operator-contracts.schema.json"
OPERATOR_DOCUMENT = REPO_ROOT / "schemas" / "nw06-operator-contracts.v1.json"


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
