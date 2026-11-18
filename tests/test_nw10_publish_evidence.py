from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_publish_evidence as publisher  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_status() -> dict:
    return {
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


def test_artifact_publisher_rejects_invalidated_gradient() -> None:
    with pytest.raises(ValueError, match="invalidated gradient"):
        publisher._artifact_from_intro_report(
            {
                "canonical_artifact": {
                    "executable_sha256": publisher.INVALIDATED_GRADIENT_SHA256,
                    "executable_bytes": 222058,
                    "package_sha256": "a" * 64,
                    "manifest_sha256": "b" * 64,
                }
            }
        )


def test_correctness_publisher_strips_raw_capture_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    source = {
        "format": "nw10-common-artifact-correctness",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": {"package_sha256": "a" * 64},
        "ada": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
                "device_form_factor": "desktop",
                "driver_version": "1.2.3",
            },
            "capture_manifest": [{"file": "tick_0.rgba32f", "sha256": "c" * 64}],
            "loop_boundary": {"status": "passed"},
        },
        "blackwell": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
                "device_form_factor": "desktop",
                "driver_version": "1.2.3",
            },
            "capture_manifest": [{"file": "tick_0.rgba32f", "sha256": "d" * 64}],
            "loop_boundary": {"status": "passed"},
        },
        "comparison": {
            "per_tick": [
                {"tick": tick, "status": "passed", "maximum_error": 0.0}
                for tick in (0, 240000, 480000, 720000, 959999)
            ],
            "worst_absolute_error": 0.0,
            "worst_relative_error": 0.0,
            "absolute_tolerance": 0.0001,
            "relative_tolerance": 0.0001,
            "absolute_required": True,
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
    }
    comparison = tmp_path / "build" / "comparison.json"
    status_path = tmp_path / "benchmarks" / "nw10" / "evidence-status.v1.json"
    _write_json(comparison, source)
    _write_json(status_path, _base_status())

    args = argparse.Namespace(
        comparison=str(comparison),
        destination="benchmarks/nw10/common-artifact-correctness.v1.json",
        status_file="benchmarks/nw10/evidence-status.v1.json",
        implementation_head="1" * 40,
        forbid_token=[],
    )
    public = publisher.publish_correctness(args)
    assert public["status"] == "verified"
    assert "capture_manifest" not in public["ada"]
    assert "per_tick" not in public["comparison"]
    assert public["comparison"]["fixed_ticks"] == [
        0,
        240000,
        480000,
        720000,
        959999,
    ]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["ada_correctness"] == "verified"
    assert status["blackwell_correctness"] == "verified"
    assert status["correctness_evidence"]["package_sha256"] == "a" * 64


def test_system_publisher_verifies_bundle_and_can_complete_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    artifact = {
        "executable_sha256": "1" * 64,
        "executable_bytes": 240702,
        "package_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
    }
    intro_report = tmp_path / "build" / "intro-report.json"
    _write_json(intro_report, {"canonical_artifact": artifact})

    expected_codes = {
        "corrupt_payload": 70,
        "deployed_startup": 0,
        "missing_required_feature": 23,
        "initialization_failure": 42,
        "resize": 0,
        "device_removal": 51,
    }
    clean_report = {
        "verification_status": "verified",
        "captured_at_utc": "2026-11-18T10:00:00+00:00",
        "host": {
            "execution_kind": "physical",
            "clean_environment": True,
            "architecture": "AMD64",
            "windows_build": "10.0.26200",
            "development_dependencies_present_on_path": [],
            "environment_cache_variables": [],
            "virtualization_indicators": [],
            "virtualization_probe": {
                "manufacturer": "ASUSTeK COMPUTER INC.",
                "model": "NUC13ANH-B",
                "hypervisor_present": True,
                "virtualization_based_security_status": 2,
            },
        },
        "deployment": {
            "only_runtime_executable": True,
            "sidecars": [],
            "forbidden_runtime_dependencies": [],
            "executable_sha256": artifact["executable_sha256"],
            "executable_bytes": artifact["executable_bytes"],
        },
        "scenarios": [
            {
                "scenario": name,
                "expected_exit_code": code,
                "observed_exit_code": code,
                "status": "passed",
                "timed_out": False,
            }
            for name, code in expected_codes.items()
        ],
    }
    report_bytes = (json.dumps(clean_report, indent=2, sort_keys=True) + "\n").encode()
    stdout_bytes = (
        "NW_PLAYER_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5070 Ti\n"
        "NW_PLAYER_ADAPTER_GENERATION=blackwell\n"
        "NW_PLAYER_ADAPTER_VENDOR_ID=4318\n"
        "NW_PLAYER_ADAPTER_DEVICE_ID=11269\n"
        "NW_PLAYER_DRIVER_VERSION=32.0.16.1088\n"
    ).encode()
    rows = [
        {
            "path": "clean-system-report.json",
            "bytes": len(report_bytes),
            "sha256": hashlib.sha256(report_bytes).hexdigest(),
        },
        {
            "path": "artifacts/resize.stdout.txt",
            "bytes": len(stdout_bytes),
            "sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        },
    ]
    manifest = {
        "format": "nw10-clean-system-result-bundle",
        "version": 1,
        "task": "NW-10",
        "final_executable_sha256": artifact["executable_sha256"],
        "verification_status": "verified",
        "files": rows,
    }
    bundle = tmp_path / "build" / "clean.zip"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("clean-system-report.json", report_bytes)
        archive.writestr("artifacts/resize.stdout.txt", stdout_bytes)
        archive.writestr(
            "bundle-manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )

    metrics = tmp_path / "build" / "metrics.json"
    _write_json(
        metrics,
        {
            "metadata": {
                "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
                "driver_version": "32.0.16.1088",
            },
            "measurements": {
                "process_start_to_first_present_ms": 250.0,
                "peak_engine_committed_gpu_bytes": 42_926_080,
                "post_initialization_allocation_violation": 0,
                "cache_kind": "warm",
                "warm_preparation": "identical artifact executed once",
                "cold_driver_cache_preparation": "reboot required for cold run",
                "memory_source": "nw_package_player_fixture --emit-benchmark-metrics",
                "engine_resource_bytes": 42_401_792,
                "engine_upload_bytes": 524_288,
                "engine_cumulative_committed_bytes": 42_926_080,
                "frames": 1,
            },
        },
    )

    status = _base_status()
    status.update(
        {
            "performance_preservation": {
                "status": "verified_preserved",
                "path": "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json",
                "sha256": "4" * 64,
                "reference_binding_id": "nw07_perf_ref_v1@sha256:v1:" + "5" * 64,
            },
            "correctness_evidence": {
                "status": "verified",
                "path": "benchmarks/nw10/common-artifact-correctness.v1.json",
                "sha256": "6" * 64,
                "package_sha256": artifact["package_sha256"],
                "worst_absolute_error": 0.0,
                "worst_relative_error": 0.0,
            },
            "compact_feasibility": {
                "status": "verified_windows_runtime",
                "path": "benchmarks/nw10/windows-compact-feasibility.v1.json",
                "sha256": "7" * 64,
            },
            "pe_ledger": {
                "status": "verified",
                "path": "benchmarks/nw10/windows-pe-ledger.v1.json",
                "sha256": "8" * 64,
            },
        }
    )
    status_path = tmp_path / "benchmarks" / "nw10" / "evidence-status.v1.json"
    _write_json(status_path, status)

    args = argparse.Namespace(
        clean_system_bundle=str(bundle),
        metrics=str(metrics),
        intro_report=str(intro_report),
        destination="benchmarks/nw10/system-validation.v1.json",
        status_file="benchmarks/nw10/evidence-status.v1.json",
        forbid_token=[],
    )
    public = publisher.publish_system(args)
    assert public["status"] == "verified"
    assert public["clean_system"]["hardware"]["adapter_description"] == (
        "NVIDIA GeForce RTX 5070 Ti"
    )
    assert public["startup"]["hardware"]["adapter_description"] == (
        "NVIDIA GeForce RTX 4060 Ti"
    )
    assert public["memory"]["peak_engine_committed_gpu_bytes"] == 42_926_080
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["evidence_status"] == "verified_complete"
    assert status["physical_evidence"] == (
        "performance_correctness_and_system_verified"
    )
