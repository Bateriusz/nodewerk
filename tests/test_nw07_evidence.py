from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as contract  # noqa: E402
import nw07_evidence as evidence  # noqa: E402

PERF_REF = REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"
EVIDENCE = REPO_ROOT / "benchmarks" / "nw07" / "hardware-evidence.json"


def _identity() -> dict:
    return {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "executable_bytes": 120000,
        "link_manifest_sha256": "2" * 64,
    }


def _correctness(tier: str) -> dict:
    target = contract.CORRECTNESS_COVERAGE_TARGETS[tier]
    adapter = (
        "NVIDIA GeForce RTX 4060 Ti"
        if tier == "ada"
        else "NVIDIA GeForce RTX 5070 Ti"
    )
    identity = _identity()
    return {
        "capture_id": f"{tier}-capture",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": target,
        "evidence_role": contract.FIXED_TICK_CORRECTNESS_ROLE,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "adapter_description": adapter,
        "device": adapter,
        "driver_version": "566.36",
        "os_version": "10.0.26100.2314",
        "toolchain_identity": "msvc-ltcg",
        "queried_capabilities": {"feature_level": "12_0"},
        "clock_information": {"core_mhz": 2310},
        "power_information": {"power_limit_w": 160},
        "thermal_information": {"temperature_c": 62},
        "cache_state": {"kind": "warm"},
        "capture_protocol": contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "renderer": "exported_player",
        "execution_environment": {"kind": "physical"},
        "fixed_ticks": list(contract.REQUIRED_FIXED_TICKS),
        "capture_artifacts": [
            {
                "kind": contract.CAPTURE_KIND,
                "format": contract.CAPTURE_FORMAT,
                "path": f"tick_{tick}.rgba32f",
                "sha256": hashlib.sha256(f"{tier}-{tick}".encode()).hexdigest(),
                "bytes": contract.OUTPUT_WIDTH
                * contract.OUTPUT_HEIGHT
                * contract.CAPTURE_BYTES_PER_PIXEL,
                "tick": tick,
                "width": contract.OUTPUT_WIDTH,
                "height": contract.OUTPUT_HEIGHT,
            }
            for tick in contract.REQUIRED_FIXED_TICKS
        ],
        "reference_execution": {
            "execution_id": f"{tier}-preview",
            "renderer": "diagnostic_preview",
            "package_sha256": identity["package_sha256"],
            "executable_sha256": identity["executable_sha256"],
            "manifest_sha256": identity["manifest_sha256"],
            "capture_protocol": contract.CORRECTNESS_CAPTURE_PROTOCOL,
        },
        "comparison": {
            "per_tick": [
                {
                    "tick": tick,
                    "artifact_sha256": hashlib.sha256(
                        f"{tier}-{tick}".encode()
                    ).hexdigest(),
                    "reference_artifact_sha256": hashlib.sha256(
                        f"{tier}-{tick}-ref".encode()
                    ).hexdigest(),
                    "renderer": "exported_player",
                    "reference_renderer": "diagnostic_preview",
                    "numeric": {
                        "maximum_error": 1e-8,
                        "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                        "maximum_relative_error": 1e-8,
                        "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
                    },
                    "image": {
                        "maximum_error": 1e-7,
                        "tolerance": contract.IMAGE_ABSOLUTE_TOLERANCE,
                        "maximum_relative_error": 1e-7,
                        "relative_tolerance": contract.IMAGE_RELATIVE_TOLERANCE,
                    },
                }
                for tick in contract.REQUIRED_FIXED_TICKS
            ],
            "loop_boundary": {
                "verified": True,
                "start_tick": 0,
                "boundary_tick": contract.LOOP_TICKS,
                "state_digest_equal_modulo_turn": True,
                "numeric": {
                    "maximum_error": 0.0,
                    "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                },
            },
        },
        "startup": {
            "protocol": contract.STARTUP_PROTOCOL,
            "process_start_to_first_present_ms": 800.0,
            "cache_state": {
                "kind": "cold_driver_cache",
                "application_pipeline_cache_used": False,
            },
            "cold_driver_cache_preparation": "Driver cache cleared.",
            "warm_preparation": "Artifact pre-run once.",
        },
        "memory": {
            "peak_engine_committed_gpu_bytes": 200 * 1024 * 1024,
            "measurement_source": "engine accounting + driver query",
        },
        "measurements": {
            "process_ram_bytes": 300 * 1024 * 1024,
            "driver_memory_bytes": 220 * 1024 * 1024,
            "startup_pipeline_ms": 400.0,
            "generation_ms": 10.0,
            "image_quality_metrics": {"psnr_db": 52.0},
            "scaling_1440p": {"p99_gpu_ms": 12.0},
        },
        "unavailable_telemetry": [],
    }


def _perf() -> dict:
    identity = _identity()
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=identity
    )
    samples = {
        "samples": [10.0 + (index % 3) * 0.001 for index in range(600)],
        "sample_count": 600,
    }
    samples["reported_p99_ms"] = evidence.nearest_rank_percentile(samples["samples"])
    return {
        "capture_id": "perf-capture",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.PERF_CONTRACT_ID,
        "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
        "contract_id": contract.PERF_CONTRACT_ID,
        "execution_environment": {"kind": "physical"},
        "reference": copy.deepcopy(bound["reference"]),
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "tools": copy.deepcopy(contract_record["tools"]),
        "protocol": {
            "protocol_id": contract.PERF_RUN_PROTOCOL,
            "warmup_seconds": contract.WARMUP_SECONDS,
            "runs": contract.PERF_RUN_COUNT,
            "run_seconds": contract.PERF_RUN_SECONDS,
            "uncapped": True,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
        },
        "runs": [
            {
                "run_index": index,
                "warmup_seconds": contract.WARMUP_SECONDS,
                "duration_seconds": contract.PERF_RUN_SECONDS,
                "uncapped": True,
                "fresh_process": True,
                "gpu_frame_time_ms": samples,
                "cpu_submit_ms": samples,
                "displayed_frame_intervals_ms": {"samples": [16.6] * 60, "gate": False},
            }
            for index in range(1, 4)
        ],
    }


def _diagnostic() -> dict:
    identity = _identity()
    return {
        "capture_id": "diagnostic-capture",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": "diagnostic_sibling_build",
        "evidence_role": contract.DIAGNOSTIC_OVERHEAD_ROLE,
        "protocol": contract.DIAGNOSTIC_OVERHEAD_PROTOCOL,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "release": {
            "executable_sha256": identity["executable_sha256"],
            "contains_capture_controls": False,
        },
        "diagnostic": {
            "build_profile": contract.DIAGNOSTIC_BUILD_PROFILE,
            "executable_sha256": "d" * 64,
        },
        "measurements": {
            "release_size_bytes": 120000,
            "diagnostic_size_bytes": 180000,
            "size_overhead_bytes": 60000,
            "release_p99_gpu_frame_time_ms": 10.0,
            "diagnostic_p99_gpu_frame_time_ms": 10.4,
            "gpu_frame_time_overhead_ms": 0.4,
            "release_p99_cpu_submit_ms": 6.0,
            "diagnostic_p99_cpu_submit_ms": 6.2,
            "cpu_submit_overhead_ms": 0.2,
        },
    }


@pytest.fixture(autouse=True)
def _artifact_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence,
        "_artifact_file_state",
        lambda capture, root, expected_ticks: ("passed", ""),
    )


def test_pending_record_is_not_verified() -> None:
    record = evidence.pending_record()
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity()
    )
    assert result["status"] == "pending_unverified"
    assert result["milestone_complete"] is False


def test_complete_required_coverage_certifies() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    record = evidence.pending_record()
    record["captures"] = [
        _correctness("ada"),
        _correctness("blackwell"),
        _perf(),
        _diagnostic(),
    ]
    result = evidence.validate_evidence_record(
        record,
        artifact_identity=_identity(),
        perf_contract=bound,
    )
    assert result["status"] == "verified"
    assert result["milestone_complete"] is True
    assert all(
        result["qualifications"][name]["status"] == "verified"
        for name in result["required_qualifications"]
    )
    assert result["blackwell_development_series"]["absolute_gate"] is False


def test_missing_blackwell_coverage_stays_pending() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    record = evidence.pending_record()
    record["captures"] = [_correctness("ada"), _perf(), _diagnostic()]
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity(), perf_contract=bound
    )
    assert result["status"] == "pending_unverified"
    assert result["qualifications"]["blackwell_correctness"]["status"] == "pending_unverified"


def test_executed_failure_remains_failed() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    record = evidence.pending_record()
    ada = _correctness("ada")
    ada["comparison"]["per_tick"][0]["image"]["maximum_error"] = 1.0
    record["captures"] = [ada, _correctness("blackwell"), _perf(), _diagnostic()]
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity(), perf_contract=bound
    )
    assert result["status"] == "failed"
    assert result["milestone_complete"] is False


def test_captures_must_bind_the_identical_artifact() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    record = evidence.pending_record()
    blackwell = _correctness("blackwell")
    blackwell["executable_sha256"] = "e" * 64
    record["captures"] = [
        _correctness("ada"),
        blackwell,
        _perf(),
        _diagnostic(),
    ]
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity(), perf_contract=bound
    )
    assert result["status"] == "failed"
    assert "identical" in result["identity_reason"]


def test_declared_failure_is_never_upgraded_and_pending_stays_pending() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    record = evidence.pending_record()
    ada = _correctness("ada")
    ada["status"] = "failed"
    record["captures"] = [ada, _correctness("blackwell"), _perf(), _diagnostic()]
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity(), perf_contract=bound
    )
    assert result["status"] == "failed"

    record = evidence.pending_record()
    ada = _correctness("ada")
    ada["status"] = "pending_unverified"
    record["captures"] = [ada, _correctness("blackwell"), _perf(), _diagnostic()]
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity(), perf_contract=bound
    )
    assert result["status"] != "verified"


def test_per_run_and_per_tick_declared_failures_are_retained() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    perf = _perf()
    perf["runs"][1]["status"] = "failed"
    assert (
        evidence.validate_perf_capture(
            perf, bound, artifact_identity=_identity()
        )["status"]
        == "failed"
    )
    ada = _correctness("ada")
    ada["comparison"]["per_tick"][0]["status"] = "failed"
    assert (
        evidence.validate_correctness_capture(
            ada,
            coverage_target=ada["coverage_target"],
            artifact_identity=_identity(),
        )["status"]
        == "failed"
    )


def test_nw06_evidence_cannot_satisfy_nw07() -> None:
    capture = _correctness("ada")
    capture["task"] = "NW-06"
    capture["implementation_base"] = "23bf4b36c978ff2660a873d3aee293633a53ddac"
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "failed"


def test_startup_and_memory_gates_are_hard() -> None:
    capture = _correctness("ada")
    capture["startup"]["process_start_to_first_present_ms"] = 2000.001
    assert (
        evidence.validate_startup_memory(capture)["status"] == "failed"
    )


def test_generic_vm_is_insufficient_and_passthrough_requires_documentation() -> None:
    capture = _correctness("ada")
    capture["execution_environment"] = {"kind": "generic_vm"}
    assert (
        evidence.validate_correctness_capture(
            capture,
            coverage_target=capture["coverage_target"],
            artifact_identity=_identity(),
        )["status"]
        == "failed"
    )
    capture = _correctness("ada")
    capture["execution_environment"] = {
        "kind": "documented_gpu_passthrough",
        "documentation": "",
    }
    assert (
        evidence.validate_correctness_capture(
            capture,
            coverage_target=capture["coverage_target"],
            artifact_identity=_identity(),
        )["status"]
        == "pending_unverified"
    )
    capture = _correctness("ada")
    capture["memory"]["peak_engine_committed_gpu_bytes"] = 512 * 1024 * 1024 + 1
    assert (
        evidence.validate_startup_memory(capture)["status"] == "failed"
    )
    capture = _correctness("ada")
    capture["startup"]["cache_state"]["application_pipeline_cache_used"] = True
    assert (
        evidence.validate_startup_memory(capture)["status"] == "failed"
    )


def test_informational_measurements_missing_keep_record_pending() -> None:
    capture = _correctness("ada")
    del capture["measurements"]["process_ram_bytes"]
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "pending_unverified"


def test_diagnostic_overhead_must_be_a_separate_artifact() -> None:
    record = _diagnostic()
    record["diagnostic"]["executable_sha256"] = _identity()["executable_sha256"]
    assert (
        evidence.validate_diagnostic_overhead(
            record, artifact_identity=_identity()
        )["status"]
        == "failed"
    )


def test_blackwell_development_series_is_informational_and_has_no_gate() -> None:
    identity = _identity()
    capture = {
        "capture_id": "blackwell-dev-1",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.BLACKWELL_DEVELOPMENT_TARGET,
        "evidence_role": contract.DEVELOPMENT_SERIES_ROLE,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
        "matched_environment_id": "blackwell-dev-host-1",
        "metrics": {"p99_gpu_frame_time_ms": 8.0, "p99_cpu_submit_ms": 4.0},
    }
    result = evidence.validate_development_series_capture(
        capture, artifact_identity=identity
    )
    assert result["status"] == "informational"
    assert result["absolute_gate"] is False
    capture["matched_environment_id"] = ""
    assert (
        evidence.validate_development_series_capture(
            capture, artifact_identity=identity
        )["status"]
        == "unverified"
    )
    record = _diagnostic()
    record["release"]["contains_capture_controls"] = True
    assert (
        evidence.validate_diagnostic_overhead(
            record, artifact_identity=_identity()
        )["status"]
        == "failed"
    )
