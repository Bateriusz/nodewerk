from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as nw07_contract  # noqa: E402
import nw07_evidence as nw07_evidence  # noqa: E402
import nw10_perf_preservation as preservation  # noqa: E402


PERF_REF = REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"


def _identity(*, exe: str, manifest: str, artifact: str, package: str = "1" * 64) -> dict:
    return {
        "artifact_key": "sha256:v1:" + artifact * 64,
        "package_sha256": package,
        "manifest_sha256": manifest * 64,
        "executable_sha256": exe * 64,
        "executable_bytes": 260000 if exe == "3" else 264000,
        "link_manifest_sha256": manifest * 64,
    }


def _contract() -> dict:
    return json.loads(PERF_REF.read_text(encoding="utf-8"))


def _bound(identity: dict) -> dict:
    return nw07_evidence.fully_bound_reference(
        _contract(),
        artifact_identity=identity,
        bound_at_utc="2026-11-30T00:00:00+00:00",
    )


def _samples(value: float, count: int = 800) -> dict:
    values = [value + (index % 5) * 0.0001 for index in range(count)]
    return {
        "samples": values,
        "sample_count": count,
        "reported_p99_ms": nw07_evidence.nearest_rank_percentile(values),
        "gate": False,
    }


def _capture(bound: dict, identity: dict, capture_id: str, base_ms: float) -> dict:
    warmup_start = datetime(2026, 12, 1, tzinfo=timezone.utc)
    runs = []
    for index in range(1, 4):
        started = warmup_start + timedelta(seconds=5, minutes=index)
        timing = {
            "run_started_utc": started.isoformat(),
            "run_ended_utc": (started + timedelta(seconds=60)).isoformat(),
            "uncapped": True,
            "fresh_process": True,
            "application_pipeline_cache": False,
            "player_pid": 1000 + index,
            "artifact_sha256": identity["executable_sha256"],
            "collector_protocol": nw07_contract.PERF_COLLECTION_PROTOCOL,
            "collector_exit_code": 0,
            "player_exited_early": False,
        }
        runs.append(
            {
                "run_index": index,
                "duration_seconds": 60,
                "uncapped": True,
                "fresh_process": True,
                "application_pipeline_cache": False,
                "player_pid": 1000 + index,
                "collector_protocol": nw07_contract.PERF_COLLECTION_PROTOCOL,
                "artifact_sha256": identity["executable_sha256"],
                "collector_exit_code": 0,
                "player_exited_early": False,
                "timing": timing,
                "sample_density_hz": 13.3,
                "gpu_frame_time_ms": _samples(base_ms),
                "cpu_submit_ms": _samples(base_ms - 0.1),
                "displayed_frame_intervals_ms": _samples(13.3),
            }
        )
    capture = {
        "capture_id": capture_id,
        "task": nw07_contract.TASK,
        "implementation_base": nw07_contract.IMPLEMENTATION_BASE,
        "coverage_target": nw07_contract.PERF_CONTRACT_ID,
        "evidence_role": nw07_contract.PERFORMANCE_CERTIFICATION_ROLE,
        "present_sync_interval": 0,
        "presentation_probe": {
            "exit_code": 0,
            "present_sync_interval_line": "NW05_PRESENT_SYNC_INTERVAL=0",
            "stdout": "NW05_PRESENT_SYNC_INTERVAL=0\n",
        },
        "contract_id": nw07_contract.PERF_CONTRACT_ID,
        "execution_environment": {"kind": "physical", "documentation": ""},
        "reference": copy.deepcopy(bound["reference"]),
        "reference_binding_digest": bound["binding_digest"],
        "reference_binding_id": bound["binding_id"],
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": nw07_contract.PROFILE_ID,
        "requirements_digest": nw07_contract.REQUIREMENTS_DIGEST,
        "tools": copy.deepcopy(bound["tools"]),
        "protocol": {
            "protocol_id": nw07_contract.PERF_RUN_PROTOCOL,
            "warmup_seconds": nw07_contract.WARMUP_SECONDS,
            "runs": nw07_contract.PERF_RUN_COUNT,
            "run_seconds": nw07_contract.PERF_RUN_SECONDS,
            "uncapped": True,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
        },
        "warmup": {
            "warmup_started_utc": warmup_start.isoformat(),
            "warmup_ended_utc": (warmup_start + timedelta(seconds=5)).isoformat(),
            "duration_seconds": 5,
            "player_pid": 999,
            "artifact_sha256": identity["executable_sha256"],
            "completed": True,
        },
        "runs": runs,
    }
    capture["validation"] = nw07_evidence.validate_perf_capture(
        capture, bound, artifact_identity=identity
    )
    capture["status"] = capture["validation"]["status"]
    return capture


def _fixture() -> tuple[dict, dict, dict, dict]:
    before_identity = _identity(exe="3", manifest="2", artifact="a")
    after_identity = _identity(exe="4", manifest="5", artifact="b")
    bound = _bound(before_identity)
    before = _capture(bound, before_identity, "before", 10.0)
    after = _capture(bound, after_identity, "after", 10.5)
    candidate = {
        "comparison_role": "after_common_artifact",
        "performance_contract": nw07_contract.PERF_CONTRACT_ID,
        "profile_id": nw07_contract.PROFILE_ID,
        "artifact": {
            "artifact_key": after_identity["artifact_key"],
            "package_sha256": after_identity["package_sha256"],
            "manifest_sha256": after_identity["manifest_sha256"],
            "executable_sha256": after_identity["executable_sha256"],
            "complete_distribution_bytes": after_identity["executable_bytes"],
        },
    }
    return bound, before, after, candidate


def test_matched_after_artifact_preserves_frozen_performance_contract() -> None:
    bound, before, after, candidate = _fixture()
    assert before["validation"]["status"] == "verified"
    assert after["validation"]["status"] == "unverified"
    assert after["validation"]["reasons"] == [
        "capture does not reference the exact final artifact"
    ]
    result = preservation.validate_preservation(before, after, bound, candidate)
    assert result["status"] == "verified_preserved"
    assert result["matched_environment"] is True
    assert result["comparison"]["same_canonical_package_sha256"] is True
    assert result["comparison"]["faster_substitute_used"] is False
    assert result["privacy"]["raw_samples_included"] is False
    assert '"samples"' not in json.dumps(result)


def test_changed_common_package_cannot_claim_preservation() -> None:
    bound, before, after, candidate = _fixture()
    after["package_sha256"] = "9" * 64
    candidate["artifact"]["package_sha256"] = "9" * 64
    result = preservation.validate_preservation(before, after, bound, candidate)
    assert result["status"] == "failed"
    assert any("canonical package hashes differ" in item for item in result["reasons"])


def test_public_evidence_rejects_local_user_profile_paths() -> None:
    with pytest.raises(ValueError, match="user-profile path"):
        preservation._scan_public_payload(
            {"bad": r"C:\Users\alice\Desktop\capture.csv"}, []
        )


def test_public_evidence_rejects_explicit_privacy_tokens() -> None:
    with pytest.raises(ValueError, match="forbidden privacy token"):
        preservation._scan_public_payload({"bad": "private-login"}, ["PRIVATE-LOGIN"])
