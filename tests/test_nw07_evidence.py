from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
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
    capture = {
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
        "device_form_factor": "desktop",
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
        ]
        + [
            {
                "kind": contract.CAPTURE_KIND,
                "format": contract.CAPTURE_FORMAT,
                "path": f"tick_{contract.LOOP_BOUNDARY_TICKS[1]}.rgba32f",
                "sha256": hashlib.sha256(
                    f"{tier}-{contract.LOOP_BOUNDARY_TICKS[1]}".encode()
                ).hexdigest(),
                "bytes": contract.OUTPUT_WIDTH
                * contract.OUTPUT_HEIGHT
                * contract.CAPTURE_BYTES_PER_PIXEL,
                "tick": contract.LOOP_BOUNDARY_TICKS[1],
                "width": contract.OUTPUT_WIDTH,
                "height": contract.OUTPUT_HEIGHT,
            }
        ],
        "reference_execution": {
            "execution_id": f"{tier}-preview",
            "renderer": "diagnostic_preview",
            "artifact_key": identity["artifact_key"],
            "package_sha256": identity["package_sha256"],
            "executable_sha256": "4" * 64,
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
                    "reference_artifact_path": f"{tier}-{tick}-ref.rgba32f",
                    "renderer": "exported_player",
                    "reference_renderer": "diagnostic_preview",
                    "numeric": {
                        "maximum_error": 1e-8,
                        "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                        "maximum_relative_error": 1e-8,
                        "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
                    },
                    "image": {
                        "maximum_error": 0.0,
                        "tolerance": contract.IMAGE_ABSOLUTE_TOLERANCE,
                        "maximum_relative_error": 0.0,
                        "relative_tolerance": contract.IMAGE_RELATIVE_TOLERANCE,
                    },
                }
                for tick in contract.REQUIRED_FIXED_TICKS
            ],
            "loop_boundary": {
                "verified": True,
                "source": "captured_boundary_comparison",
                "start_tick": 0,
                "boundary_tick": contract.LOOP_BOUNDARY_TICKS[1],
                "state_digest_equal_modulo_turn": True,
                "state_digest_source": "verify_canonical_graph",
                "graph_loop_boundary": {"seamless_modulo_turn": True},
                "boundary_artifact_sha256": hashlib.sha256(
                    f"{tier}-{contract.LOOP_BOUNDARY_TICKS[1]}".encode()
                ).hexdigest(),
                "numeric": {
                    "maximum_error": 0.0,
                    "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                    "maximum_relative_error": 0.0,
                    "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
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
            "image_quality_metrics": {
                "psnr_db": 52.0,
                "artifact_sha256": identity["executable_sha256"],
                "environment": {
                    "adapter_description": adapter,
                    "driver_version": "566.36",
                    "os_version": "10.0.26100.2314",
                    "toolchain_identity": "msvc-ltcg",
                },
            },
            "scaling_1440p": {
                "width": 2560,
                "height": 1440,
                "p99_gpu_ms": 12.0,
                "artifact_sha256": identity["executable_sha256"],
                "environment": {
                    "adapter_description": adapter,
                    "driver_version": "566.36",
                    "os_version": "10.0.26100.2314",
                    "toolchain_identity": "msvc-ltcg",
                },
            },
        },
        "unavailable_telemetry": [],
        "release_capture_protocol": contract.RELEASE_CAPTURE_PROTOCOL,
    }
    capture["execution_records"] = [
        {
            "renderer": "exported_player",
            "tick": item["tick"],
            "executable_sha256": identity["executable_sha256"],
            "exit_code": 0,
            "artifact_sha256": item["sha256"],
        }
        for item in capture["capture_artifacts"]
    ]
    return capture


def _perf() -> dict:
    identity = _identity()
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record,
        artifact_identity=identity,
        bound_at_utc="2026-11-30T00:00:00+00:00",
    )
    samples = {
        "samples": [10.0 + (index % 3) * 0.001 for index in range(600)],
        "sample_count": 600,
    }
    samples["reported_p99_ms"] = evidence.nearest_rank_percentile(samples["samples"])
    base = datetime(2026, 12, 1, tzinfo=timezone.utc)
    return {
        "capture_id": "perf-capture",
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
        "warmup": {
            "warmup_started_utc": base.isoformat(),
            "warmup_ended_utc": (
                base + timedelta(seconds=contract.WARMUP_SECONDS)
            ).isoformat(),
            "duration_seconds": contract.WARMUP_SECONDS,
            "player_pid": 999,
            "artifact_sha256": identity["executable_sha256"],
            "completed": True,
        },
        "runs": [
            {
                "run_index": index,
                "duration_seconds": contract.PERF_RUN_SECONDS,
                "uncapped": True,
                "fresh_process": True,
                "application_pipeline_cache": False,
                "player_pid": 1000 + index,
                "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
                "artifact_sha256": identity["executable_sha256"],
                "collector_exit_code": 0,
                "player_exited_early": False,
                "timing": {
                    "run_started_utc": (
                        base + timedelta(minutes=index)
                    ).isoformat(),
                    "run_ended_utc": (
                        base
                        + timedelta(minutes=index, seconds=contract.PERF_RUN_SECONDS)
                    ).isoformat(),
                    "uncapped": True,
                    "fresh_process": True,
                    "application_pipeline_cache": False,
                    "player_pid": 1000 + index,
                    "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
                    "artifact_sha256": identity["executable_sha256"],
                    "collector_exit_code": 0,
                    "player_exited_early": False,
                },
                "sample_density_hz": 10.0,
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
def _artifact_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        evidence,
        "_artifact_file_state",
        lambda capture, root, expected_ticks: ("passed", ""),
    )
    raw_files: dict[tuple[str, int], Path] = {}

    def fake_artifact(capture, root, tick):
        key = (str(capture.get("capture_id")), int(tick))
        path = raw_files.get(key)
        if path is None:
            path = tmp_path / f"{key[0]}-{key[1]}.rgba32f"
            path.write_bytes(b"raw")
            raw_files[key] = path
        return path

    monkeypatch.setattr(evidence, "_artifact_file_for_tick", fake_artifact)
    monkeypatch.setattr(
        evidence,
        "_capture_paths_for_tick",
        lambda capture, root, tick: (
            fake_artifact(capture, root, tick),
            fake_artifact(capture, root, tick),
        ),
    )
    monkeypatch.setattr(
        evidence,
        "compare_rgba32f_files",
        lambda captured, reference: {
            "maximum_error": 0.0,
            "maximum_relative_error": 0.0,
        },
    )
    real_is_file = Path.is_file

    def fake_is_file(self: Path) -> bool:
        if self.suffix == ".rgba32f" and str(tmp_path) in str(self):
            return True
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    real_sha256_file = evidence.sha256_file

    def fake_sha256_file(path: Path) -> str:
        if path.suffix == ".rgba32f":
            return hashlib.sha256(path.stem.encode()).hexdigest()
        return real_sha256_file(path)

    monkeypatch.setattr(evidence, "sha256_file", fake_sha256_file)
    real_validate_record = evidence.validate_evidence_record
    real_validate_correctness = evidence.validate_correctness_capture

    def validate_record_with_raw_root(record, **kwargs):
        kwargs.setdefault("artifact_root", tmp_path)
        return real_validate_record(record, **kwargs)

    def validate_correctness_with_raw_root(capture, **kwargs):
        kwargs.setdefault("artifact_root", tmp_path)
        return real_validate_correctness(capture, **kwargs)

    monkeypatch.setattr(
        evidence, "validate_evidence_record", validate_record_with_raw_root
    )
    monkeypatch.setattr(
        evidence, "validate_correctness_capture", validate_correctness_with_raw_root
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
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
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
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
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
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
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
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
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
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
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
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
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


def test_nonfinite_authoritative_tolerance_fails_closed() -> None:
    section = {
        "maximum_error": 1e-3,
        "tolerance": 1e-3,
        "maximum_relative_error": 0.0,
        "relative_tolerance": 1e-4,
    }
    for bad in (float("nan"), float("inf"), -1.0):
        status, detail = evidence._validate_tolerance_section(
            section,
            kind="image",
            absolute_tolerance=bad,
            relative_tolerance=1e-4,
            relative_can_satisfy=False,
        )
        assert status == "failed"
        assert "non-finite or negative" in detail


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


def test_diagnostic_overhead_requires_complete_release_binding() -> None:
    record = _diagnostic()
    record.pop("executable_sha256")
    assert (
        evidence.validate_diagnostic_overhead(
            record, artifact_identity=_identity()
        )["status"]
        != "verified"
    )
    record = _diagnostic()
    record["release"]["executable_sha256"] = "e" * 64
    assert (
        evidence.validate_diagnostic_overhead(
            record, artifact_identity=_identity()
        )["status"]
        != "verified"
    )
    record = _diagnostic()
    record["executable_sha256"] = "e" * 64
    assert (
        evidence.validate_diagnostic_overhead(
            record, artifact_identity=_identity()
        )["status"]
        != "verified"
    )


def test_loop_boundary_claims_require_measured_capture_provenance() -> None:
    capture = _correctness("ada")
    capture["comparison"]["loop_boundary"]["source"] = "graph-derived-provenance"
    assert (
        evidence.validate_correctness_capture(
            capture,
            coverage_target=capture["coverage_target"],
            artifact_identity=_identity(),
        )["status"]
        == "failed"
    )
    capture = _correctness("ada")
    del capture["comparison"]["loop_boundary"]["boundary_artifact_sha256"]
    assert (
        evidence.validate_correctness_capture(
            capture,
            coverage_target=capture["coverage_target"],
            artifact_identity=_identity(),
        )["status"]
        == "failed"
    )
    capture = _correctness("ada")
    del capture["comparison"]["loop_boundary"]["graph_loop_boundary"]
    assert (
        evidence.validate_correctness_capture(
            capture,
            coverage_target=capture["coverage_target"],
            artifact_identity=_identity(),
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
        "device_form_factor": "desktop",
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


def test_desktop_generation_classification_rejects_laptop_and_datacenter_parts() -> None:
    assert (
        evidence.classify_hardware_tier(
            "NVIDIA GeForce RTX 4060 Ti", device_form_factor="desktop"
        )
        == "ada"
    )
    assert (
        evidence.classify_hardware_tier(
            "NVIDIA GeForce RTX 5070 Ti", device_form_factor="desktop"
        )
        == "blackwell"
    )
    for unsupported in (
        "NVIDIA GeForce RTX 4090 Laptop GPU",
        "NVIDIA RTX A6000",
        "NVIDIA A40",
        "NVIDIA B200",
        "NVIDIA GB200",
    ):
        assert evidence.classify_hardware_tier(unsupported) == "unknown"
    assert (
        evidence.classify_hardware_tier(
            "NVIDIA GeForce RTX 4060 Ti", device_form_factor="laptop"
        )
        == "unknown"
    )


def test_image_comparisons_are_recomputed_from_raw_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _correctness("ada")
    monkeypatch.setattr(
        evidence,
        "compare_rgba32f_files",
        lambda captured, reference: {
            "maximum_error": 1.0,
            "maximum_relative_error": 1.0,
        },
    )
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "failed"
    assert "does not match the retained raw captures" in " ".join(result["reasons"])


def test_explicit_unverified_and_informational_captures_are_not_upgraded() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
    )
    for status in ("unverified", "informational", "pending_unverified"):
        record = evidence.pending_record()
        ada = _correctness("ada")
        ada["status"] = status
        record["captures"] = [
            ada,
            _correctness("blackwell"),
            _perf(),
            _diagnostic(),
        ]
        result = evidence.validate_evidence_record(
            record, artifact_identity=_identity(), perf_contract=bound
        )
        assert result["status"] != "verified"
        assert result["milestone_complete"] is False


def test_record_level_executed_failure_remains_failed() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record,
        artifact_identity=_identity(),
        bound_at_utc="2026-11-30T00:00:00+00:00",
    )
    record = evidence.pending_record()
    record["verification_status"] = "executed_failure"
    record["captures"] = [
        _correctness("ada"),
        _correctness("blackwell"),
        _perf(),
        _diagnostic(),
    ]
    result = evidence.validate_evidence_record(
        record, artifact_identity=_identity(), perf_contract=bound
    )
    assert result["status"] == "failed"
    assert result["milestone_complete"] is False


def test_reference_binding_is_immutable() -> None:
    contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
    bound = evidence.fully_bound_reference(
        contract_record, artifact_identity=_identity()
    )
    with pytest.raises(ValueError):
        evidence.bind_reference(
            bound,
            board={"subsystem_vendor_id": 1, "subsystem_device_id": 2},
        )


def test_missing_metadata_cannot_hide_an_executed_startup_failure() -> None:
    capture = _correctness("ada")
    capture["startup"]["process_start_to_first_present_ms"] = 3000.0
    del capture["driver_version"]
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "failed"
    assert "driver_version" in result.get("missing_metadata", [])


def test_required_quality_and_scaling_measurements_must_be_measured() -> None:
    capture = _correctness("ada")
    capture["measurements"]["image_quality_metrics"] = {}
    capture["measurements"]["scaling_1440p"] = {}
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "pending_unverified"

    capture = _correctness("ada")
    capture["measurements"]["scaling_1440p"]["width"] = 1280
    capture["measurements"]["scaling_1440p"]["height"] = 720
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "failed"


def test_correctness_requires_explicit_desktop_form_factor() -> None:
    capture = _correctness("ada")
    capture["device_form_factor"] = ""
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=contract.CORRECTNESS_COVERAGE_TARGETS["ada"],
        artifact_identity=_identity(),
    )
    assert result["status"] != "verified"
    assert evidence.classify_hardware_tier(
        "NVIDIA GeForce RTX 4060 Ti", device_form_factor=None
    ) == "unknown"


def test_diagnostic_reference_must_be_a_separate_sibling_executable() -> None:
    capture = _correctness("ada")
    capture["reference_execution"]["executable_sha256"] = capture["executable_sha256"]
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=contract.CORRECTNESS_COVERAGE_TARGETS["ada"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "failed"
    assert any("separate sibling executable" in reason for reason in result["reasons"])


def test_missing_metadata_does_not_short_circuit_a_deterministic_failure() -> None:
    capture = _correctness("ada")
    capture.pop("driver_version", None)
    capture["evidence_role"] = "wrong-role"
    result = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=_identity(),
    )
    assert result["status"] == "failed"
    assert "driver_version" in result.get("missing_metadata", [])
