from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
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
from schema_validator import validate_file_against_schema  # noqa: E402


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


def _clean_host() -> dict:
    return {
        "execution_kind": "physical",
        "passthrough_documentation": "",
        "virtualization_probe": {
            "available": True,
            "manufacturer": "ASUSTeK COMPUTER INC.",
            "model": "Physical Desktop",
            "hypervisor_present": False,
            "error": "",
        },
        "virtualization_indicators": [],
        "physical_host_evidence": True,
        "platform": "Windows",
        "release": "11",
        "os_build": "10.0.26100.2314",
        "windows_11_or_newer": True,
        "architecture": "AMD64",
        "x64": True,
        "development_dependency_checks": {
            "nodewerkc": None,
            "dxc": None,
            "cmake": None,
        },
        "development_dependencies_present_on_path": [],
        "environment_cache_variables": [],
        "clean_environment": True,
        "application_pipeline_cache_present": False,
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


def test_shipped_metric_baseline_structure_is_understood() -> None:
    baseline = json.loads(
        (REPO_ROOT / "benchmarks" / "nw07" / "metric-baselines.v1.json").read_text(
            encoding="utf-8"
        )
    )
    for name in (
        "complete_distribution_bytes",
        "process_start_to_first_present_ms",
        "peak_engine_committed_gpu_bytes",
        "p99_gpu_frame_time_ms",
        "p99_cpu_submit_ms",
    ):
        baseline["metrics"][name]["baseline"] = {
            "complete_distribution_bytes": 120000,
            "process_start_to_first_present_ms": 800.0,
            "peak_engine_committed_gpu_bytes": 200 * 1024 * 1024,
            "p99_gpu_frame_time_ms": 10.0,
            "p99_cpu_submit_ms": 5.0,
        }[name]
    baseline["environment"] = dict(ENVIRONMENT)
    current = _record(p99_gpu_frame_time_ms=11.5)
    result = regression.evaluate_regression(baseline, current)
    assert regression._metric(
        baseline, "p99_gpu_frame_time_ms"
    ) == pytest.approx(10.0)
    assert result["status"] == "flagged"
    assert result["size"]["delta_bytes"] == 0


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


def test_nightly_metrics_merge_produces_current_series(tmp_path: Path) -> None:
    artifact = tmp_path / "final.exe"
    artifact.write_bytes(b"MZ" + b"\x00" * 120000)
    metrics = {
        "metadata": {
            "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
            "driver_version": "566.36",
            "os_version": "10.0.26100.2314",
            "toolchain_identity": "msvc-ltcg",
            "cache_state": {"kind": "warm"},
        },
        "measurements": {
            "process_start_to_first_present_ms": 800.0,
            "peak_engine_committed_gpu_bytes": 200 * 1024 * 1024,
        },
    }
    metrics_path = tmp_path / "metrics-ada.json"
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    evidence = {
        "captures": [
            {
                "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
                    "executable_sha256": hashlib.sha256(
                        artifact.read_bytes()
                    ).hexdigest(),
                    "captured_at_utc": "2026-12-02T00:00:00+00:00",
                "validation": {
                        "status": "verified",
                    "runs": [
                        {
                            "gpu_frame_time_ms": {"p99_ms": 10.0},
                            "cpu_submit_ms": {"p99_ms": 5.0},
                        },
                        {
                            "gpu_frame_time_ms": {"p99_ms": 11.0},
                            "cpu_submit_ms": {"p99_ms": 6.0},
                        },
                    ]
                },
            }
        ]
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    written = regression.merge_nightly_metrics(
        [metrics_path],
        evidence_path=evidence_path,
        artifact_path=artifact,
        output_dir=tmp_path / "current",
    )
    current = json.loads(written[0].read_text(encoding="utf-8"))
    assert current["metrics"]["complete_distribution_bytes"] == artifact.stat().st_size
    assert current["metrics"]["process_start_to_first_present_ms"] == 800.0
    assert current["metrics"]["p99_gpu_frame_time_ms"] == 11.0
    assert current["metrics"]["p99_cpu_submit_ms"] == 6.0
    assert current["environment"]["board"] == "NVIDIA GeForce RTX 4060 Ti"
    assert (
        regression.evaluate_regression(
            json.loads(
                (REPO_ROOT / "benchmarks" / "nw07" / "metric-baselines.v1.json").read_text(
                    encoding="utf-8"
                )
            ),
            current,
        )["status"]
        in {"passed", "flagged"}
    )


def test_capture_harness_reads_and_hashes_raw_samples(tmp_path: Path) -> None:
    sample_path = tmp_path / "run1-gpu.txt"
    sample_path.write_text("1\n2\n3\n4\n", encoding="utf-8")
    section = capture._sample_section(sample_path)
    assert section["sample_count"] == 4
    assert section["reported_p99_ms"] == 4
    assert section["sha256"]
    assert section["bytes"] == sample_path.stat().st_size


def test_capture_records_always_emit_a_top_level_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    measurements = tmp_path / "measurements.json"
    measurements.write_text("{}", encoding="utf-8")
    record = capture.record_diagnostic(
        Namespace(
            package=str(tmp_path / "package"),
            measurements=str(measurements),
            diagnostic_sha256="d" * 64,
            capture_id="diagnostic-status",
            evidence=str(tmp_path / "evidence.json"),
        )
    )
    assert record["status"] in {
        "passed",
        "failed",
        "pending_unverified",
        "unverified",
        "informational",
    }
    evidence_path = tmp_path / "evidence.json"
    capture._append_record(evidence_path, record)
    validate_file_against_schema(
        evidence_path,
        REPO_ROOT / "schemas" / "nw07-hardware-evidence.schema.json",
    )


def test_retained_capture_namespacing_refuses_to_overwrite_different_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tick_0.rgba32f"
    source.write_bytes(b"first")
    destination_dir = tmp_path / "nw07-captures" / "ada-capture"
    retained = capture._retain_capture(source, destination_dir)
    assert retained.parent == destination_dir
    assert retained.read_bytes() == b"first"
    source.write_bytes(b"different")
    with pytest.raises(ValueError):
        capture._retain_capture(source, destination_dir)


def test_player_teardown_does_not_wait_out_the_measurement_window() -> None:
    process = capture.subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=capture.subprocess.PIPE,
        stderr=capture.subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    capture._stop_player(process)
    assert time.monotonic() - started < 5.0


def test_failed_collection_is_persisted_and_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = {
        "capture_id": "nightly-failed",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.PERF_CONTRACT_ID,
        "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
        "status": "failed",
        "runs": [],
        "failure": "collector failed",
        "validation": {"status": "failed", "reasons": ["collector failed"]},
    }
    monkeypatch.setattr(capture, "collect_perf", lambda args: failed)
    evidence_path = tmp_path / "evidence.json"
    code = capture.main(
        [
            "collect",
            "--package",
            str(tmp_path / "package"),
            "--executable",
            str(tmp_path / "player.exe"),
            "--samples-dir",
            str(tmp_path / "samples"),
            "--reference",
            str(tmp_path / "reference.json"),
            "--collector-command",
            "collector",
            "--capture-id",
            "nightly-failed",
            "--evidence",
            str(evidence_path),
        ]
    )
    assert code == 1
    record = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert record["captures"][-1]["status"] == "failed"


def test_release_playback_exposes_explicit_fixed_tick_and_capture_protocol() -> None:
    source = verify_nw07.export.nw05_export.generate_player_source(
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        present_sync_interval=0,
    )
    assert '"--tick"' in source
    assert '"--hold-ms"' not in source
    assert "::Sleep(1500u)" in source
    assert "NW05_EXPLICIT_TICK" in source
    assert hasattr(capture, "capture_release_correctness")


def test_nightly_freshness_rejects_historical_captures() -> None:
    stale = {
        "captures": [
            {
                "capture_id": "old",
                "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
                "captured_at_utc": "2020-01-01T00:00:00+00:00",
            }
        ]
    }
    reasons = verify_nw07._evidence_freshness_reasons(stale, 86400)
    assert reasons and "stale" in reasons[0]


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
        base = datetime(2026, 12, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
        (samples_dir / f"run{index}-timing.json").write_text(
            json.dumps(
                {
                    "run_started_utc": base.isoformat(),
                    "run_ended_utc": (
                        base + timedelta(seconds=contract.PERF_RUN_SECONDS)
                    ).isoformat(),
                    "uncapped": True,
                    "fresh_process": True,
                    "application_pipeline_cache": False,
                    "player_pid": 1000 + index,
                    "artifact_sha256": "3" * 64,
                    "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
                    "collector_exit_code": 0,
                    "player_exited_early": False,
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
            warmup={
                "warmup_started_utc": "2026-12-01T00:00:00+00:00",
                "warmup_ended_utc": "2026-12-01T00:00:05+00:00",
                "duration_seconds": contract.WARMUP_SECONDS,
                "player_pid": 999,
                "artifact_sha256": "3" * 64,
                "completed": True,
            },
        )
    )
    for run in record["runs"]:
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


def test_capture_controls_are_diagnostic_only_and_write_fixed_ticks() -> None:
    header = (REPO_ROOT / "src" / "player" / "nw07_capture_controls.h").read_text(
        encoding="utf-8"
    )
    assert "#if defined(NW07_CAPTURE_CONTROLS) && NW07_CAPTURE_CONTROLS" in header
    assert "WriteFixedTickCaptures" in header
    assert "readbackPresentedOutput" in header
    assert ".rgba32f" in header
    fixture = (
        REPO_ROOT / "fixtures" / "nw05_procedural_image" / "main.cpp"
    ).read_text(encoding="utf-8")
    assert "nw07_capture_controls.h" in fixture
    assert "--capture-tick" in fixture
    assert "--capture-dir" in fixture


def test_export_report_with_reproducibility_can_verify_and_malformed_reports_fail_closed(
    tmp_path: Path,
) -> None:
    identity = {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "size_accounting": {
            "complete_distribution_bytes": 120000,
            "within_ceiling": True,
        },
    }
    inputs = verify_nw07.export.verify_pinned_inputs(
        verify_nw07.export.load_canonical_config()
    )
    fields = {
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
    }
    report = {
        "format": "nw07-export-report",
        "version": 1,
        "success": True,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "artifact": {
            **fields,
            "artifact_key": identity["artifact_key"],
        },
        "distribution": {
            "complete_distribution_bytes": 120000,
            "within_ceiling": True,
        },
        "build_inputs": {
            "digest": inputs["digest"],
            "matches_frozen_digest": True,
        },
        "reproducibility": {
            "matching": True,
            "fields": fields,
            "build_inputs_digest": inputs["digest"],
        },
        "release_grade": True,
        "test_payload": False,
    }
    report_path = tmp_path / "export-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    check = verify_nw07._validate_build_report(
        report_path, identity=identity
    )
    assert check.status == "passed"

    reproducibility_report = dict(report)
    reproducibility_report["format"] = "nw07-reproducibility-report"
    report_path.write_text(
        json.dumps(reproducibility_report), encoding="utf-8"
    )
    assert (
        verify_nw07._validate_build_report(report_path, identity=identity).status
        == "passed"
    )
    report_path.write_text(json.dumps(report), encoding="utf-8")

    report_path.write_text(json.dumps([]), encoding="utf-8")
    assert (
        verify_nw07._validate_build_report(report_path, identity=identity).status
        == "failed"
    )

    malformed_evidence = {
        "format": "nw07-verification-summary",
        "mode": "BuildChecks",
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "success": True,
        "checks": [
            {
                "name": "nw07_native_release_export",
                "status": "passed",
                "evidence": "not-an-object",
            }
        ],
    }
    report_path.write_text(json.dumps(malformed_evidence), encoding="utf-8")
    assert (
        verify_nw07._validate_build_report(report_path, identity=identity).status
        == "failed"
    )

    malformed_artifact = dict(report)
    malformed_artifact["artifact"] = "not-an-object"
    report_path.write_text(json.dumps(malformed_artifact), encoding="utf-8")
    assert (
        verify_nw07._validate_build_report(report_path, identity=identity).status
        == "failed"
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
    assert any(
        check["name"] == "nw07_native_release_export"
        and check["status"] == "pending_unverified"
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
    executable = deployment / "nw_common_test.exe"
    executable.write_bytes(b"MZ")
    import hashlib

    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    report = {
        "format": "nw07-clean-system-report",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "host": _clean_host(),
        "deployment": {
            "executable": executable.name,
            "executable_sha256": executable_sha256,
            "executable_bytes": executable.stat().st_size,
            "deployment_files": [executable.name],
            "sidecars": [],
            "only_runtime_executable": True,
            "development_dependencies_present": False,
            "pe_imports": [],
            "forbidden_runtime_dependencies": [],
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


def test_clean_system_verifier_forwards_execution_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "nw_common_test.exe").write_bytes(b"MZ")
    package = tmp_path / "package"
    package.mkdir()
    fixture = tmp_path / "fixture.exe"
    fixture.write_bytes(b"fixture")
    captured: dict = {}

    def fake_run_clean_system(**kwargs):
        captured.update(kwargs)
        return {"format": "nw07-clean-system-report"}

    monkeypatch.setattr(verify_nw07.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        clean_system, "run_clean_system", fake_run_clean_system
    )
    monkeypatch.setattr(
        clean_system,
        "validate_report",
        lambda *args, **kwargs: {"status": "verified", "reasons": [], "scenarios": []},
    )
    code = verify_nw07.main(
        [
            "--mode",
            "CleanSystem",
            "--binary-dir",
            str(deployment),
            "--fixture",
            str(fixture),
            "--package",
            str(package),
            "--execution-kind",
            "documented_gpu_passthrough",
            "--passthrough-documentation",
            "dossier",
        ]
    )
    assert code == 0
    assert captured["execution_kind"] == "documented_gpu_passthrough"
    assert captured["passthrough_documentation"] == "dossier"


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
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    deployed = deployment / "nw_common_d3d12_fl12_0_sm6_0_v1.exe"
    deployed.write_bytes(b"MZ" + b"\x00" * 16)
    deployment_report = clean_system.deployment_report(deployed, deployment)
    clean_report = {
        "format": "nw07-clean-system-report",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "artifact": {
            "artifact_key": identity["artifact_key"],
            "package_sha256": identity["package_sha256"],
            "manifest_sha256": identity["manifest_sha256"],
            "executable_sha256": identity["executable_sha256"],
        },
        "host": _clean_host(),
        "deployment": deployment_report,
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
    clean_report_path = tmp_path / "clean-report.json"
    clean_report_path.write_text(json.dumps(clean_report), encoding="utf-8")
    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text("{}", encoding="utf-8")
    pinned_digest = verify_nw07.export.verify_pinned_inputs(
        verify_nw07.export.load_canonical_config()
    )["digest"]
    build_report = {
        "format": "nw07-verification-summary",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "mode": "BuildChecks",
        "success": True,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "checks": [
            {
                "name": "nw07_native_release_export",
                "required": True,
                "passed": True,
                "status": "passed",
                "detail": "",
                "unavailable": False,
                "evidence": {
                    "artifact_key": identity["artifact_key"],
                    "package_sha256": identity["package_sha256"],
                    "manifest_sha256": identity["manifest_sha256"],
                    "executable_sha256": identity["executable_sha256"],
                    "complete_distribution_bytes": identity["size_accounting"][
                        "complete_distribution_bytes"
                    ],
                    "within_ceiling": True,
                    "release_grade": True,
                    "test_payload": False,
                    "reproducible": True,
                    "build_inputs_digest": pinned_digest,
                    "matches_frozen_digest": True,
                },
            }
        ],
    }
    build_report_path = tmp_path / "build-report.json"
    build_report_path.write_text(json.dumps(build_report), encoding="utf-8")
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
            "--build-report",
            str(build_report_path),
            "--binary-dir",
            str(deployment),
            "--clean-report",
            str(clean_report_path),
            "--summary-json",
            str(summary_path),
        ]
    )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert code == 0
    assert summary["success"] is True
    assert summary["milestone_complete"] is True
    assert summary["hardware_certification"] == "verified"

    build_report["checks"][0]["evidence"]["executable_sha256"] = "9" * 64
    build_report_path.write_text(json.dumps(build_report), encoding="utf-8")
    stale_summary = tmp_path / "full-stale.json"
    stale_code = verify_nw07.main(
        [
            "--mode",
            "Full",
            "--fixture",
            str(executable),
            "--package",
            str(package_dir),
            "--evidence",
            str(evidence_file),
            "--build-report",
            str(build_report_path),
            "--binary-dir",
            str(deployment),
            "--clean-report",
            str(clean_report_path),
            "--summary-json",
            str(stale_summary),
        ]
    )
    assert stale_code == 1
    assert json.loads(stale_summary.read_text(encoding="utf-8"))["success"] is False

    missing_report_summary = tmp_path / "full-missing-report.json"
    missing_code = verify_nw07.main(
        [
            "--mode",
            "Full",
            "--fixture",
            str(executable),
            "--package",
            str(package_dir),
            "--evidence",
            str(evidence_file),
            "--binary-dir",
            str(deployment),
            "--clean-report",
            str(clean_report_path),
            "--summary-json",
            str(missing_report_summary),
        ]
    )
    assert missing_code == 2
    missing_summary = json.loads(
        missing_report_summary.read_text(encoding="utf-8")
    )
    assert missing_summary["milestone_complete"] is False
    assert any(
        check["name"] == "nw07_native_release_export"
        and check["status"] == "pending_unverified"
        for check in missing_summary["checks"]
    )


def test_freshness_requires_both_correctness_tiers_and_reference_perf() -> None:
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "captures": [
            {
                "capture_id": "ada",
                "evidence_role": contract.FIXED_TICK_CORRECTNESS_ROLE,
                "coverage_target": contract.CORRECTNESS_COVERAGE_TARGETS["ada"],
                "captured_at_utc": now,
                "validation": {"status": "verified"},
            },
            {
                "capture_id": "perf",
                "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
                "coverage_target": contract.PERF_CONTRACT_ID,
                "captured_at_utc": now,
                "validation": {"status": "verified"},
            },
        ]
    }
    reasons = verify_nw07._evidence_freshness_reasons(record, 86400)
    assert any("Blackwell correctness" in reason for reason in reasons)
    record["captures"].append(
        {
            "capture_id": "blackwell",
            "evidence_role": contract.FIXED_TICK_CORRECTNESS_ROLE,
            "coverage_target": contract.CORRECTNESS_COVERAGE_TARGETS["blackwell"],
            "captured_at_utc": now,
            "validation": {"status": "verified"},
        }
    )
    assert verify_nw07._evidence_freshness_reasons(record, 86400) == []
    record["captures"][0]["validation"] = {"status": "pending_unverified"}
    assert any(
        "Ada correctness" in reason
        for reason in verify_nw07._evidence_freshness_reasons(record, 86400)
    )


def test_fresh_unverified_capture_cannot_borrow_historical_verification() -> None:
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "captures": [
            {
                "capture_id": "historical-verified",
                "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
                "coverage_target": contract.PERF_CONTRACT_ID,
                "captured_at_utc": "2025-01-01T00:00:00+00:00",
                "validation": {"status": "verified"},
            },
            {
                "capture_id": "fresh-unverified",
                "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
                "coverage_target": contract.PERF_CONTRACT_ID,
                "captured_at_utc": now,
                "validation": {"status": "pending_unverified"},
            },
        ]
    }
    filtered, excluded = verify_nw07._filter_fresh_evidence(record, 86400)
    assert [item["capture_id"] for item in filtered["captures"]] == [
        "fresh-unverified"
    ]
    assert excluded and excluded[0]["capture_id"] == "historical-verified"


def test_fresh_metrics_cannot_fall_back_to_static_workflow_values() -> None:
    identity = {"executable_sha256": "a" * 64}
    with pytest.raises(ValueError, match="metadata"):
        capture._normalize_fresh_correctness_metrics(
            {"measurements": {}, "numeric_comparisons": {}},
            identity=identity,
            tier="ada",
        )


def test_fresh_metrics_preserve_and_reject_foreign_provenance() -> None:
    identity = {"executable_sha256": "a" * 64}
    measured = {
        "metadata": {
            "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
            "device_form_factor": "desktop",
            "driver_version": "566.36",
            "os_version": "10.0.26100.2314",
            "toolchain_identity": "msvc-ltcg",
            "queried_capabilities": {"feature_level": "12_0"},
            "clock_information": {"core_mhz": 2310},
            "power_information": {"power_limit_w": 160},
            "thermal_information": {"temperature_c": 62},
            "cache_state": {"kind": "warm"},
        },
        "measurements": {
            "process_start_to_first_present_ms": 800.0,
            "peak_engine_committed_gpu_bytes": 1024,
            "cache_kind": "warm",
            "cold_driver_cache_preparation": "cleared",
            "warm_preparation": "pre-run",
            "memory_source": "driver query",
            "process_ram_bytes": 2048,
            "driver_memory_bytes": 1024,
            "startup_pipeline_ms": 10.0,
            "generation_ms": 1.0,
            "image_quality_metrics": {
                "psnr_db": 52.0,
                "artifact_sha256": "b" * 64,
            },
            "scaling_1440p": {
                "width": 2560,
                "height": 1440,
                "p99_gpu_ms": 10.0,
            },
        },
        "numeric_comparisons": {
            str(tick): {"maximum_error": 0.0, "maximum_relative_error": 0.0}
            for tick in contract.REQUIRED_FIXED_TICKS
        },
    }
    with pytest.raises(ValueError, match="different artifact"):
        capture._normalize_fresh_correctness_metrics(
            measured, identity=identity, tier="ada"
        )
    measured["measurements"]["image_quality_metrics"]["artifact_sha256"] = identity[
        "executable_sha256"
    ]
    measured["measurements"]["image_quality_metrics"]["environment"] = {
        "driver_version": "999.99"
    }
    with pytest.raises(ValueError, match="environment mismatch"):
        capture._normalize_fresh_correctness_metrics(
            measured, identity=identity, tier="ada"
        )


def test_regression_uses_the_fresh_bound_performance_capture() -> None:
    artifact_sha = "a" * 64

    def perf(capture_id: str, timestamp: str, p99: float) -> dict:
        return {
            "capture_id": capture_id,
            "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
            "executable_sha256": artifact_sha,
            "captured_at_utc": timestamp,
            "reference": {
                "board": {"model": "NVIDIA GeForce RTX 4060 Ti"},
                "environment": {
                    "driver_version": "566.36",
                    "os_build": "10.0.26100.2314",
                },
                "toolchain": {"toolchain_identity": "msvc-ltcg"},
                "host": {"cpu_model": "AMD Ryzen 7 7800X3D"},
            },
            "validation": {
                "status": "verified",
                "runs": [
                    {
                        "gpu_frame_time_ms": {"p99_ms": p99},
                        "cpu_submit_ms": {"p99_ms": p99 / 2},
                    }
                ],
            },
        }

    evidence_record = {
        "captures": [
            perf("historical", "2025-01-01T00:00:00+00:00", 10.0),
            perf("nightly-current", "2026-12-02T00:00:00+00:00", 15.0),
        ]
    }
    selected = regression._perf_p99_from_evidence(
        evidence_record,
        artifact_sha256=artifact_sha,
        environment={
            "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
            "driver_version": "566.36",
            "os_version": "10.0.26100.2314",
            "toolchain_identity": "msvc-ltcg",
            "host": "AMD Ryzen 7 7800X3D",
        },
    )
    assert selected["p99_gpu_frame_time_ms"] == 15.0
