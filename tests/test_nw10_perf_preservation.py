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
from schema_validator import validate_instance  # noqa: E402
import nw10_perf_candidate as candidate  # noqa: E402
import nw04_fixture  # noqa: E402


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


def test_after_candidate_retains_over_proof_ceiling_without_relaxing_it(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    template = toolchain / "template.exe"
    template.write_bytes(b"MZ" + b"\\0" * 262144)
    dxc = toolchain / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\\n"
        "from pathlib import Path\\n"
        "if '--version' in sys.argv:\\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\\n"
        "    sys.exit(0)\\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\\n",
        encoding="utf-8",
    )
    report = candidate.build_candidate(
        tmp_path / "after",
        player_template=template,
        dxc_executable=str(dxc),
        report_path=tmp_path / "after-report.json",
        allow_test_payloads=True,
    )
    assert report["comparison_role"] == "after_common_artifact"
    assert report["size_classification"]["within_nw07_proof_ceiling"] is False
    assert report["size_classification"]["nw07_proof_ceiling_is_not_relaxed"] is True
    assert report["size_classification"]["retained_for_performance_comparison_only"] is True
    assert report["size_classification"]["within_standard_tier"] is True
    assert (tmp_path / "after" / "nw_common_d3d12_fl12_0_sm6_0_v1.exe").is_file()


def test_publish_rejects_private_path_and_promotes_only_performance_status(
    tmp_path: Path,
) -> None:
    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public), encoding="utf-8")
    destination = tmp_path / "repo" / "benchmarks" / "nw10" / "performance.json"
    status_path = tmp_path / "repo" / "benchmarks" / "nw10" / "evidence-status.v1.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "format": "nw10-evidence-status",
                "version": 1,
                "task": "NW-10",
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
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    old_root = preservation.REPO_ROOT
    preservation.REPO_ROOT = tmp_path / "repo"
    try:
        status = preservation.publish_public_evidence(
            source, destination, status_path
        )
    finally:
        preservation.REPO_ROOT = old_root
    assert status["evidence_status"] == "performance_verified_correctness_pending"
    assert status["physical_evidence"] == "performance_verified"
    assert status["reference_capture"] == "verified_reference_capture"
    assert status["frame_time"] == "verified_preserved"
    assert status["ada_correctness"] == "pending_unverified"
    assert status["blackwell_correctness"] == "pending_unverified"
    assert status["startup"] == "pending_unverified"
    assert status["memory"] == "pending_unverified"
    assert destination.is_file()

    poisoned = copy.deepcopy(public)
    poisoned["reference_environment"]["host"]["motherboard"] = r"C:\Users\alice\secret"
    source.write_text(json.dumps(poisoned), encoding="utf-8")
    with pytest.raises(ValueError, match="user-profile path"):
        preservation.publish_public_evidence(source, destination, status_path)


def test_preservation_schema_accepts_float_relative_alert_threshold() -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-performance-preservation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    assert public["comparison"]["relative_alert_threshold_percent"] == 10.0
    validate_instance(public, schema, schema)
