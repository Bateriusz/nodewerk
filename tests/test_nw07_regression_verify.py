from __future__ import annotations

import copy
import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_regression as regression  # noqa: E402
import nw07_capture as capture  # noqa: E402
import nw07_clean_system as clean_system  # noqa: E402
import nw07_contract as contract  # noqa: E402
import verify_nw07  # noqa: E402


ENVIRONMENT = {
    "board": "NVIDIA GeForce RTX 4060 Ti",
    "host": "AMD Ryzen 7 7800X3D",
    "driver_version": "566.36",
    "os_build": "10.0.26100.2314",
    "toolchain_identity": "msvc-ltcg",
    "resolution": "1920x1080",
    "quality_profile": "nw07.quality.v1",
    "cache_state": "warm",
}


def _record(**metrics) -> dict:
    base = {
        "complete_distribution_bytes": 120000,
        "process_start_to_first_present_ms": 800.0,
        "peak_engine_committed_gpu_bytes": 200 * 1024 * 1024,
        "p99_gpu_frame_time_ms": 10.0,
        "p99_cpu_submit_ms": 5.0,
    }
    base.update(metrics)
    return {"environment": dict(ENVIRONMENT), "metrics": base}


def test_absolute_gate_exactly_at_threshold_is_allowed_for_ceiling_but_not_frame_time() -> None:
    baseline = _record()
    size = _record(complete_distribution_bytes=262144)
    assert regression.evaluate_regression(baseline, size)["status"] in {"passed", "flagged"}
    over = _record(complete_distribution_bytes=262145)
    assert regression.evaluate_regression(baseline, over)["status"] == "failed"
    frame = _record(p99_gpu_frame_time_ms=16.67)
    assert regression.evaluate_regression(baseline, frame)["status"] == "failed"


def test_size_changes_are_tracked_without_percentage_only_failure() -> None:
    baseline = _record(complete_distribution_bytes=100000)
    doubled = _record(complete_distribution_bytes=200000)
    result = regression.evaluate_regression(baseline, doubled)
    assert result["status"] != "failed"
    assert result["size"]["percentage_only_failure"] is False
    assert result["size"]["delta_percent"] == pytest.approx(100.0)
    smaller = _record(complete_distribution_bytes=50000)
    assert regression.evaluate_regression(baseline, smaller)["status"] != "failed"
    assert regression.evaluate_regression(baseline, smaller)["size"]["delta_bytes"] == -50000


def test_relative_alert_requires_matched_environment() -> None:
    baseline = _record()
    current = _record(p99_gpu_frame_time_ms=11.1)
    matched = regression.evaluate_regression(baseline, current)
    assert matched["status"] == "flagged"
    assert matched["relative_alerts"]
    unmatched = copy.deepcopy(current)
    unmatched["environment"]["driver_version"] = "999.99"
    result = regression.evaluate_regression(baseline, unmatched)
    assert result["status"] != "flagged"
    assert any(
        item["status"] == "informational_unmatched_environment"
        for item in result["informational"]
    )


def test_environment_match_requires_every_frozen_field() -> None:
    baseline = _record()
    for field in regression.MATCHED_ENVIRONMENT_FIELDS:
        other = copy.deepcopy(baseline)
        if field in other["environment"]:
            other["environment"][field] = "different"
            assert regression.environments_match(
                baseline["environment"], other["environment"]
            ) is False


def test_regression_cli_exit_codes(tmp_path: Path) -> None:
    baseline = _record()
    current = _record(p99_gpu_frame_time_ms=11.1)
    baseline_path = tmp_path / "baseline.json"
    current_path = tmp_path / "current.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    current_path.write_text(json.dumps(current), encoding="utf-8")
    assert (
        regression.main(
            ["--baseline", str(baseline_path), "--current", str(current_path)]
        )
        == 0
    )
    current["metrics"]["complete_distribution_bytes"] = 262145
    current_path.write_text(json.dumps(current), encoding="utf-8")
    assert (
        regression.main(
            ["--baseline", str(baseline_path), "--current", str(current_path)]
        )
        == 1
    )


def test_capture_harness_reads_and_hashes_raw_samples(tmp_path: Path) -> None:
    sample_path = tmp_path / "run1-gpu.txt"
    sample_path.write_text("1\n2\n3\n4\n", encoding="utf-8")
    section = capture._sample_section(sample_path)
    assert section["sample_count"] == 4
    assert section["reported_p99_ms"] == 4
    assert section["sha256"]
    assert section["bytes"] == sample_path.stat().st_size


def test_perf_capture_derives_run_timing_from_retained_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    for index in range(1, contract.PERF_RUN_COUNT + 1):
        for name in ("gpu", "cpu", "displayed"):
            (samples_dir / f"run{index}-{name}.txt").write_text(
                "\n".join(["10.0"] * contract.MIN_RAW_SAMPLES_PER_RUN),
                encoding="utf-8",
            )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
        (samples_dir / f"run{index}-timing.json").write_text(
            json.dumps(
                {
                    "warmup_started_utc": base.isoformat(),
                    "run_started_utc": (
                        base + timedelta(seconds=contract.WARMUP_SECONDS)
                    ).isoformat(),
                    "run_ended_utc": (
                        base
                        + timedelta(
                            seconds=contract.WARMUP_SECONDS
                            + contract.PERF_RUN_SECONDS
                        )
                    ).isoformat(),
                    "uncapped": True,
                    "fresh_process": True,
                    "application_pipeline_cache": False,
                }
            ),
            encoding="utf-8",
        )
    identity = {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "executable_bytes": 120000,
    }
    monkeypatch.setattr(
        capture.export, "inspect_artifact_directory", lambda path: identity
    )
    reference = tmp_path / "reference.json"
    reference.write_text("{}", encoding="utf-8")
    record = capture.record_perf(
        Namespace(
            package=str(tmp_path / "package"),
            contract=str(REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"),
            samples_dir=str(samples_dir),
            reference=str(reference),
            capture_id="perf-timing",
            execution_kind="physical",
            evidence=str(tmp_path / "evidence.json"),
        )
    )
    for run in record["runs"]:
        assert run["warmup_seconds"] == pytest.approx(contract.WARMUP_SECONDS)
        assert run["duration_seconds"] == pytest.approx(contract.PERF_RUN_SECONDS)
        assert run["uncapped"] is True
        assert run["fresh_process"] is True
        assert run["application_pipeline_cache"] is False
        assert run["sample_density_hz"] == pytest.approx(10.0)


def test_perf_capture_does_not_fabricate_run_timing_without_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    for index in range(1, contract.PERF_RUN_COUNT + 1):
        for name in ("gpu", "cpu", "displayed"):
            (samples_dir / f"run{index}-{name}.txt").write_text(
                "10.0\n", encoding="utf-8"
            )
    identity = {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "executable_bytes": 120000,
    }
    monkeypatch.setattr(
        capture.export, "inspect_artifact_directory", lambda path: identity
    )
    reference = tmp_path / "reference.json"
    reference.write_text("{}", encoding="utf-8")
    record = capture.record_perf(
        Namespace(
            package=str(tmp_path / "package"),
            contract=str(REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"),
            samples_dir=str(samples_dir),
            reference=str(reference),
            capture_id="perf-no-timing",
            execution_kind="physical",
            evidence=str(tmp_path / "evidence.json"),
        )
    )
    for run in record["runs"]:
        assert run["warmup_seconds"] is None
        assert run["duration_seconds"] is None
        assert run["uncapped"] is None
        assert run["fresh_process"] is None
        assert run["application_pipeline_cache"] is None


def test_loop_boundary_evidence_is_measured_only_from_boundary_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_evidence = {"loop_boundary": {"seamless_modulo_turn": True}}
    unavailable = capture._loop_boundary_evidence(tmp_path, graph_evidence)
    assert unavailable["verified"] is False
    assert unavailable["source"] == "graph-derived-provenance"
    assert unavailable["state_digest_equal_modulo_turn"] is True
    (tmp_path / "tick_0.rgba32f").write_bytes(b"start")
    boundary = tmp_path / f"tick_{contract.LOOP_BOUNDARY_TICKS[1]}.rgba32f"
    boundary.write_bytes(b"boundary")
    monkeypatch.setattr(
        capture,
        "_compare_images",
        lambda captured, reference: {
            "maximum_error": 0.0,
            "maximum_relative_error": 0.0,
        },
    )
    measured = capture._loop_boundary_evidence(tmp_path, graph_evidence)
    assert measured["verified"] is True
    assert measured["source"] == "captured_boundary_comparison"
    assert measured["numeric"]["maximum_error"] == 0.0
    assert measured["boundary_artifact_sha256"] == (
        capture._sha256_file(boundary)
    )


def test_verify_contracts_only_never_claims_certification(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    code = verify_nw07.main(
        ["--mode", "ContractsOnly", "--summary-json", str(summary_path)]
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert summary["success"] is True
    assert summary["milestone_complete"] is False
    assert summary["hardware_certification"] == "pending_unverified"
    assert summary["contracts_only_success_does_not_imply_hardware_acceptance"] is True
    assert summary["required_failures"] == 0
    assert summary["pending_checks"] == 0


def test_verify_full_without_evidence_is_pending(tmp_path: Path) -> None:
    summary_path = tmp_path / "full.json"
    code = verify_nw07.main(["--mode", "Full", "--summary-json", str(summary_path)])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 2
    assert summary["success"] is False
    assert summary["milestone_complete"] is False
    assert summary["hardware_certification"] == "pending_unverified"
    assert summary["pending_checks"] >= 1
    assert all(
        check["name"] != "nw07_native_release_export"
        for check in summary["checks"]
    )


def test_verify_clean_system_requires_windows(tmp_path: Path) -> None:
    summary_path = tmp_path / "clean.json"
    code = verify_nw07.main(["--mode", "CleanSystem", "--summary-json", str(summary_path)])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if sys.platform.startswith("win"):
        assert code == 2
    else:
        assert code == 2
    assert summary["milestone_complete"] is False
    assert summary["hardware_certification"] == "pending_unverified"


def test_clean_system_binary_dir_discovers_retained_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "nw_common_test.exe").write_bytes(b"MZ")
    report = {
        "format": "nw07-clean-system-report",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "deployment": {
            "only_runtime_executable": True,
            "development_dependencies_present": False,
        },
        "scenarios": [
            {
                "scenario": name,
                "executed": True,
                "expected_exit_code": expected,
                "observed_exit_code": expected,
                "status": "passed",
            }
            for name, expected in clean_system.EXPECTED_SCENARIO_EXIT_CODES.items()
        ],
        "verification_status": "verified",
        "policy": "Executed failures remain failed.",
    }
    (deployment / "nw07-clean-system.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    monkeypatch.setattr(verify_nw07.platform, "system", lambda: "Windows")
    summary_path = tmp_path / "clean.json"
    code = verify_nw07.main(
        [
            "--mode",
            "CleanSystem",
            "--binary-dir",
            str(deployment),
            "--summary-json",
            str(summary_path),
        ]
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert summary["checks"][-1]["name"] == "nw07_clean_system"
    assert summary["checks"][-1]["status"] == "passed"


def test_full_reports_milestone_only_when_every_gate_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "executable_bytes": 120000,
        "size_accounting": {
            "complete_distribution_bytes": 120000,
            "within_ceiling": True,
            "within_stretch": True,
        },
    }
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ" + b"\x00" * 16)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(verify_nw07, "contracts_only", lambda: [])
    monkeypatch.setattr(
        verify_nw07,
        "build_checks",
        lambda args: [],
    )
    monkeypatch.setattr(
        verify_nw07.export, "inspect_artifact_directory", lambda path: identity
    )
    monkeypatch.setattr(
        verify_nw07.evidence, "sha256_file", lambda path: identity["executable_sha256"]
    )
    monkeypatch.setattr(
        verify_nw07,
        "validate_file_against_schema",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        verify_nw07.evidence,
        "validate_evidence_record",
        lambda *args, **kwargs: {
            "status": "verified",
            "milestone_complete": True,
            "qualifications": {},
            "required_qualifications": [],
            "captures": [],
        },
    )
    summary_path = tmp_path / "full.json"
    code = verify_nw07.main(
        [
            "--mode",
            "Full",
            "--fixture",
            str(executable),
            "--package",
            str(package_dir),
            "--evidence",
            str(evidence_file),
            "--summary-json",
            str(summary_path),
        ]
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert summary["success"] is True
    assert summary["milestone_complete"] is True
    assert summary["hardware_certification"] == "verified"
