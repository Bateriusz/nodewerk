from __future__ import annotations

import copy
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as contract  # noqa: E402
import nw07_evidence as evidence  # noqa: E402

PERF_REF = REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"


def _identity() -> dict:
    return {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "executable_bytes": 120000,
        "link_manifest_sha256": "2" * 64,
    }


def _contract() -> dict:
    return json.loads(PERF_REF.read_text(encoding="utf-8"))


def _bound() -> dict:
    return evidence.fully_bound_reference(
        _contract(),
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
    )


def _samples(value: float, count: int = 600) -> dict:
    values = [value + (index % 5) * 0.0001 for index in range(count)]
    return {
        "samples": values,
        "sample_count": count,
        "reported_p99_ms": evidence.nearest_rank_percentile(values),
        "gate": False,
    }


def _timing(index: int) -> dict:
    base = datetime(2026, 12, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    return {
        "run_started_utc": base.isoformat(),
        "run_ended_utc": (base + timedelta(seconds=contract.PERF_RUN_SECONDS)).isoformat(),
        "uncapped": True,
        "fresh_process": True,
        "application_pipeline_cache": False,
        "player_pid": 1000 + index,
        "artifact_sha256": _identity()["executable_sha256"],
        "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
        "collector_exit_code": 0,
        "player_exited_early": False,
    }


def _warmup() -> dict:
    base = datetime(2026, 12, 1, tzinfo=timezone.utc)
    return {
        "warmup_started_utc": base.isoformat(),
        "warmup_ended_utc": (base + timedelta(seconds=contract.WARMUP_SECONDS)).isoformat(),
        "duration_seconds": contract.WARMUP_SECONDS,
        "player_pid": 999,
        "artifact_sha256": _identity()["executable_sha256"],
        "completed": True,
    }


def _capture() -> dict:
    bound = _bound()
    return {
        "capture_id": "perf-1",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.PERF_CONTRACT_ID,
        "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
        "present_sync_interval": 0,
        "presentation_probe": {
            "exit_code": 0,
            "present_sync_interval_line": "NW05_PRESENT_SYNC_INTERVAL=0",
            "stdout": "NW05_PRESENT_SYNC_INTERVAL=0\n",
        },
        "contract_id": contract.PERF_CONTRACT_ID,
        "execution_environment": {"kind": "physical"},
        "reference": copy.deepcopy(bound["reference"]),
        "reference_binding_digest": bound["binding_digest"],
        "reference_binding_id": bound["binding_id"],
        "artifact_key": _identity()["artifact_key"],
        "package_sha256": _identity()["package_sha256"],
        "manifest_sha256": _identity()["manifest_sha256"],
        "executable_sha256": _identity()["executable_sha256"],
        "executable_bytes": _identity()["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "tools": copy.deepcopy(_contract()["tools"]),
        "protocol": {
            "protocol_id": contract.PERF_RUN_PROTOCOL,
            "warmup_seconds": contract.WARMUP_SECONDS,
            "runs": contract.PERF_RUN_COUNT,
            "run_seconds": contract.PERF_RUN_SECONDS,
            "uncapped": True,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
        },
        "warmup": _warmup(),
        "runs": [
            {
                "run_index": index,
                "duration_seconds": contract.PERF_RUN_SECONDS,
                "uncapped": True,
                "fresh_process": True,
                "application_pipeline_cache": False,
                "player_pid": 1000 + index,
                "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
                "artifact_sha256": _identity()["executable_sha256"],
                "collector_exit_code": 0,
                "player_exited_early": False,
                "timing": _timing(index),
                "sample_density_hz": 10.0,
                "gpu_frame_time_ms": _samples(10.0),
                "cpu_submit_ms": _samples(6.0),
                "displayed_frame_intervals_ms": _samples(16.6),
            }
            for index in range(1, 4)
        ],
    }


def test_nearest_rank_percentile_is_exact() -> None:
    assert evidence.nearest_rank_percentile([1, 2, 3, 4, 5], 99.0) == 5
    assert evidence.nearest_rank_percentile([1, 2, 3, 4], 75.0) == 3
    samples = list(range(1, 101))
    assert evidence.nearest_rank_percentile(samples, 99.0) == 99
    with pytest.raises(ValueError):
        evidence.nearest_rank_percentile([], 99.0)
    with pytest.raises(ValueError):
        evidence.nearest_rank_percentile([1.0, math.nan], 99.0)


def test_frozen_contract_is_methodologically_complete_but_unbound() -> None:
    result = evidence.validate_perf_contract(_contract())
    assert result["status"] == "pending_binding"
    assert result["missing_bindings"]
    assert "host.cpu_model" in result["missing_bindings"]


def test_bound_contract_is_frozen() -> None:
    result = evidence.validate_perf_contract(_bound())
    assert result["status"] == "frozen"
    assert result["reasons"] == []


@pytest.mark.parametrize(
    "mutation",
    (
        lambda c: c["metric_definitions"]["gpu_frame_time_ms"].__setitem__("threshold", 20.0),
        lambda c: c["metric_definitions"]["gpu_frame_time_ms"].__setitem__("threshold", 13.5),
        lambda c: c["metric_definitions"]["cpu_submit_ms"].__setitem__("threshold", 12.0),
        lambda c: c["metric_definitions"]["gpu_frame_time_ms"].__setitem__("operator", "<="),
        lambda c: c["metric_definitions"]["gpu_frame_time_ms"].__setitem__(
            "percentile_method", "linear"
        ),
        lambda c: c["protocol"].__setitem__("warmup_seconds", 0),
        lambda c: c["protocol"].__setitem__("runs", 1),
        lambda c: c["protocol"].__setitem__("run_seconds", 10),
        lambda c: c["protocol"].__setitem__("uncapped", False),
        lambda c: c["reference"]["quality"].__setitem__("vsync", True),
        lambda c: c["reference"]["quality"]["resolution"].__setitem__("width", 1280),
        lambda c: c["matching_rules"].__setitem__("reject_other_ada_devices", False),
        lambda c: c["reference"]["board"].__setitem__("model", "NVIDIA GeForce RTX 4070"),
    ),
)
def test_contract_cannot_be_weakened(mutation) -> None:
    record = _contract()
    mutation(record)
    assert evidence.validate_perf_contract(record)["status"] == "invalid"


def test_fully_matching_three_run_capture_certifies() -> None:
    result = evidence.validate_perf_capture(
        _capture(), _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "verified"
    assert len(result["runs"]) == 3
    assert all(run["status"] == "passed" for run in result["runs"])


def test_strict_operator_rejects_exactly_16_67_ms() -> None:
    capture = _capture()
    capture["runs"][1]["gpu_frame_time_ms"] = _samples(16.67)
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
    assert result["runs"][1]["status"] == "failed"


def test_single_failed_run_cannot_be_hidden_by_aggregate() -> None:
    capture = _capture()
    capture["runs"][2]["cpu_submit_ms"] = _samples(16.68)
    capture["aggregate_p99_cpu_submit_ms"] = 1.0
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
    assert result["runs"][2]["status"] == "failed"
    assert result["aggregate_percentiles_are_informational"] is True


@pytest.mark.parametrize(
    "mutation",
    (
        lambda c: c["reference"]["board"].__setitem__("model", "NVIDIA GeForce RTX 4070"),
        lambda c: c["reference"]["board"].__setitem__("model", "NVIDIA GeForce RTX 5070 Ti"),
        lambda c: c["reference"]["board"].__setitem__("model", "NVIDIA GeForce RTX 4090"),
        lambda c: c["reference"]["host"].__setitem__("cpu_model", "Intel Core i9-14900K"),
        lambda c: c["reference"]["environment"].__setitem__("driver_version", "999.99"),
        lambda c: c["reference"]["toolchain"].__setitem__("toolchain_identity", "clang-cl"),
        lambda c: c.__setitem__("executable_sha256", "9" * 64),
        lambda c: c.__setitem__("requirements_digest", "sha256:v1:" + "0" * 64),
        lambda c: c["protocol"].__setitem__("warmup_seconds", 4),
        lambda c: c["reference"]["board"].__setitem__(
            "tier_label", "rtx_40_series_legacy"
        ),
        lambda c: c.__setitem__("implementation_base", "23bf4b36c978ff2660a873d3aee293633a53ddac"),
    ),
)
def test_mismatched_or_stale_capture_cannot_certify(mutation) -> None:
    capture = _capture()
    mutation(capture)
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] != "verified"


def test_unbound_contract_leaves_certification_pending() -> None:
    result = evidence.validate_perf_capture(
        _capture(), _contract(), artifact_identity=_identity()
    )
    assert result["status"] == "pending_unverified"
    assert result["missing_contract_bindings"]


def test_missing_run_or_incomplete_provenance_stays_unverified() -> None:
    capture = _capture()
    capture["runs"].pop()
    assert (
        evidence.validate_perf_capture(
            capture, _bound(), artifact_identity=_identity()
        )["status"]
        == "unverified"
    )
    incomplete = _capture()
    del incomplete["reference"]["toolchain"]["toolchain_identity"]
    assert (
        evidence.validate_perf_capture(
            incomplete, _bound(), artifact_identity=_identity()
        )["status"]
        != "verified"
    )


def test_displayed_intervals_are_required_separately_but_never_gate() -> None:
    capture = _capture()
    capture["runs"][0].pop("displayed_frame_intervals_ms")
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
    # A displayed-frame interval p99 above 16.67 ms does not itself fail the
    # GPU/CPU gate metrics.
    capture = _capture()
    for run in capture["runs"]:
        run["displayed_frame_intervals_ms"] = _samples(25.0)
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "verified"
    assert all(
        run["displayed_frame_interval_ms"]["gate"] is False
        for run in result["runs"]
    )


def test_reported_p99_must_match_recomputed_samples() -> None:
    capture = _capture()
    capture["runs"][0]["gpu_frame_time_ms"]["reported_p99_ms"] = 1.0
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"


def test_file_backed_samples_are_hashed_and_tampering_fails(tmp_path: Path) -> None:
    capture = _capture()
    sample_path = tmp_path / "gpu-run1.txt"
    sample_path.write_text(
        "\n".join(str(value) for value in capture["runs"][0]["gpu_frame_time_ms"]["samples"]),
        encoding="utf-8",
    )
    capture["runs"][0]["gpu_frame_time_ms"] = {
        "path": sample_path.name,
        "sha256": evidence.sha256_file(sample_path),
        "bytes": sample_path.stat().st_size,
        "sample_count": 600,
        "reported_p99_ms": evidence.nearest_rank_percentile(
            capture["runs"][0]["gpu_frame_time_ms"]["samples"]
        ),
    }
    assert (
        evidence.validate_perf_capture(
            capture,
            _bound(),
            artifact_identity=_identity(),
            artifact_root=tmp_path,
        )["status"]
        == "verified"
    )
    sample_path.write_text("1\n2\n3\n", encoding="utf-8")
    assert (
        evidence.validate_perf_capture(
            capture,
            _bound(),
            artifact_identity=_identity(),
            artifact_root=tmp_path,
        )["status"]
        == "failed"
    )


def test_performance_capture_rejects_generic_vm_and_undocumented_passthrough() -> None:
    capture = _capture()
    capture["execution_environment"] = {"kind": "generic_vm"}
    assert (
        evidence.validate_perf_capture(
            capture, _bound(), artifact_identity=_identity()
        )["status"]
        == "failed"
    )
    capture = _capture()
    capture["execution_environment"] = {
        "kind": "documented_gpu_passthrough",
        "documentation": "",
    }
    assert (
        evidence.validate_perf_capture(
            capture, _bound(), artifact_identity=_identity()
        )["status"]
        != "verified"
    )
    capture["execution_environment"]["documentation"] = "PCI passthrough dossier"
    assert (
        evidence.validate_perf_capture(
            capture, _bound(), artifact_identity=_identity()
        )["status"]
        == "verified"
    )


def test_malformed_execution_environment_returns_unverified_not_exception() -> None:
    capture = _capture()
    capture["execution_environment"] = "physical"
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] != "verified"
    assert result["execution_environment"] == {"invalid_type": "str"}


def test_performance_capture_requires_retained_60_second_run_timing() -> None:
    capture = _capture()
    capture["runs"][0]["timing"]["run_ended_utc"] = (
        datetime.fromisoformat(capture["runs"][0]["timing"]["run_started_utc"])
        + timedelta(seconds=10)
    ).isoformat()
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
    assert result["runs"][0]["status"] == "failed"

    capture = _capture()
    capture["runs"][1]["timing"]["fresh_process"] = False
    assert (
        evidence.validate_perf_capture(
            capture, _bound(), artifact_identity=_identity()
        )["status"]
        == "failed"
    )


def test_runs_recorded_before_the_frozen_binding_cannot_certify() -> None:
    capture = _capture()
    capture["warmup"]["warmup_started_utc"] = "2026-11-01T00:00:00+00:00"
    capture["warmup"]["warmup_ended_utc"] = "2026-11-01T00:00:05+00:00"
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
    assert "retroactive" in " ".join(result["reasons"])

    capture = _capture()
    capture["runs"][2].pop("timing")
    assert (
        evidence.validate_perf_capture(
            capture, _bound(), artifact_identity=_identity()
        )["status"]
        == "failed"
    )


def test_bound_reference_payload_is_cryptographically_immutable() -> None:
    bound = _bound()
    bound["reference"]["host"]["cpu_model"] = "tampered host"
    result = evidence.validate_perf_contract(bound)
    assert result["status"] == "invalid"
    assert any("binding digest" in reason for reason in result["reasons"])


def test_perf_protocol_uses_one_warmup_and_distinct_measured_processes() -> None:
    capture = _capture()
    capture["runs"][1]["player_pid"] = capture["runs"][0]["player_pid"]
    capture["runs"][1]["timing"]["player_pid"] = capture["runs"][0]["player_pid"]
    result = evidence.validate_perf_capture(
        capture, _bound(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
    assert any(
        "distinct fresh process" in reason
        for reason in result["runs"][1]["reasons"]
    )


def test_populated_but_unfrozen_binding_cannot_certify() -> None:
    bound = _bound()
    unfrozen = copy.deepcopy(bound)
    unfrozen["binding_status"] = "pending_reference_capture"
    unfrozen.pop("binding_digest", None)
    unfrozen.pop("binding_id", None)
    unfrozen.pop("bound_at_utc", None)
    assert evidence.validate_perf_contract(unfrozen)["status"] == "pending_binding"
    assert (
        evidence.validate_perf_capture(
            _capture(), unfrozen, artifact_identity=_identity()
        )["status"]
        != "verified"
    )


def test_executed_failure_is_not_hidden_by_an_unbound_contract() -> None:
    capture = _capture()
    capture["runs"][1]["cpu_submit_ms"] = _samples(16.68)
    result = evidence.validate_perf_capture(
        capture, _contract(), artifact_identity=_identity()
    )
    assert result["status"] == "failed"
