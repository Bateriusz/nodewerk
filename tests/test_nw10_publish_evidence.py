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
from schema_validator import validate_instance  # noqa: E402


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


def test_refresh_status_tracks_compact_progress_before_system_validation() -> None:
    status = _base_status()
    status["performance_preservation"] = {"status": "verified_preserved"}
    status["compact_feasibility"] = {"status": "verified_windows_runtime"}

    refreshed = publisher._refresh_status(status)
    assert (
        refreshed["evidence_status"]
        == "performance_and_compact_verified_correctness_pending"
    )
    assert refreshed["physical_evidence"] == "performance_verified"

    status["correctness_evidence"] = {"status": "verified"}
    refreshed = publisher._refresh_status(status)
    assert (
        refreshed["evidence_status"]
        == "performance_compact_and_correctness_verified_system_pending"
    )
    assert refreshed["physical_evidence"] == (
        "performance_and_correctness_verified"
    )


def test_refresh_status_supports_correctness_and_system_before_performance() -> None:
    status = _base_status()
    status["correctness_evidence"] = {"status": "verified"}
    status["system_validation"] = {"status": "verified"}

    refreshed = publisher._refresh_status(status)
    assert (
        refreshed["evidence_status"]
        == "correctness_and_system_reused_performance_pending"
    )
    assert refreshed["physical_evidence"] == "correctness_and_system_reused"
    assert refreshed["reference_capture"] == "pending_reference_capture"
    assert refreshed["frame_time"] == "pending_unverified"
    assert refreshed["ada_correctness"] == "verified"
    assert refreshed["blackwell_correctness"] == "verified"
    assert refreshed["clean_system"] == "verified"
    assert refreshed["startup"] == "verified"
    assert refreshed["memory"] == "verified"


def test_artifact_publisher_rejects_invalidated_gradient() -> None:
    with pytest.raises(ValueError, match="invalidated gradient"):
        publisher._artifact_from_intro_report(
            {
                "release_grade": True,
                "test_payload": False,
                "canonical_artifact": {
                    "executable_sha256": publisher.INVALIDATED_GRADIENT_SHA256,
                    "executable_bytes": 222058,
                    "package_sha256": "a" * 64,
                    "manifest_sha256": "b" * 64,
                }
            }
        )


def test_artifact_publisher_rejects_test_payload_and_non_release_intro() -> None:
    artifact = {
        "executable_sha256": "a" * 64,
        "executable_bytes": 100,
        "package_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
    }
    with pytest.raises(ValueError, match="test payload"):
        publisher._artifact_from_intro_report(
            {
                "release_grade": True,
                "test_payload": True,
                "canonical_artifact": artifact,
            }
        )
    with pytest.raises(ValueError, match="release-grade"):
        publisher._artifact_from_intro_report(
            {
                "release_grade": False,
                "test_payload": False,
                "canonical_artifact": artifact,
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
                "device_form_factor_source": "win32_system_enclosure_and_battery",
                "classified_tier": "ada",
                "driver_version": "1.2.3",
            },
            "capture_manifest": [{"file": "tick_0.rgba32f", "sha256": "c" * 64}],
            "loop_boundary": {"status": "passed"},
        },
        "blackwell": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
                "device_form_factor": "desktop",
                "device_form_factor_source": "win32_system_enclosure_and_battery",
                "classified_tier": "blackwell",
                "driver_version": "1.2.3",
            },
            "capture_manifest": [{"file": "tick_0.rgba32f", "sha256": "d" * 64}],
            "loop_boundary": {"status": "passed"},
        },
        "comparison": {
            "fixed_ticks": [0, 240000, 480000, 720000, 959999],
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
        "packed_unpacked_comparison": {
            "status": "verified",
            "direct_separate_packed_gpu_capture_recorded": True,
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
            "packed_package_sha256": "a" * 64,
            "unpacked_package_sha256": "a" * 64,
            "worst_absolute_error": 0.0,
            "runtime": {
                "ada": {
                    "status": "passed",
                    "unpacked_executable_sha256": "1" * 64,
                    "packed_executable_sha256": "2" * 64,
                    "encoded_container_sha256": "3" * 64,
                    "codec_id": "nwc.codec.mszip.v1",
                    "layout_id": "nwc.layout.soa.v1",
                    "device_form_factor_source": "win32_system_enclosure_and_battery",
                    "frames_per_tick": 1,
                    "present_sync_interval": 0,
                },
                "blackwell": {
                    "status": "passed",
                    "unpacked_executable_sha256": "4" * 64,
                    "packed_executable_sha256": "5" * 64,
                    "encoded_container_sha256": "6" * 64,
                    "codec_id": "nwc.codec.mszip.v1",
                    "layout_id": "nwc.layout.soa.v1",
                    "device_form_factor_source": "win32_system_enclosure_and_battery",
                    "frames_per_tick": 1,
                    "present_sync_interval": 0,
                },
            },
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
        "source": {
            "mode": "compare",
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": "1" * 40,
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
    assert public["packed_unpacked_comparison"]["status"] == "verified"


def test_correctness_publisher_rejects_asserted_desktop_and_pending_packed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    source = {
        "format": "nw10-common-artifact-correctness",
        "status": "verified",
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": {"package_sha256": "a" * 64},
        "ada": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
                "device_form_factor": "desktop",
                "device_form_factor_source": "asserted",
                "classified_tier": "ada",
            },
            "loop_boundary": {"status": "passed"},
        },
        "blackwell": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
                "device_form_factor": "desktop",
                "device_form_factor_source": "asserted",
                "classified_tier": "blackwell",
            },
            "loop_boundary": {"status": "passed"},
        },
        "comparison": {
            "fixed_ticks": [0, 240000, 480000, 720000, 959999],
            "per_tick": [
                {"tick": tick, "status": "passed"} for tick in (0, 240000, 480000, 720000, 959999)
            ],
            "worst_absolute_error": 0.0,
            "worst_relative_error": 0.0,
            "absolute_tolerance": 0.0001,
            "relative_tolerance": 0.0001,
            "absolute_required": True,
        },
        "packed_unpacked_comparison": {
            "status": "pending_physical",
            "worst_absolute_error": None,
            "runtime": {},
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
        "source": {
            "mode": "compare",
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": "1" * 40,
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
    with pytest.raises(ValueError, match="packed/unpacked"):
        publisher.publish_correctness(args)


def test_correctness_publisher_rejects_compositional_packed_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    package = "a" * 64
    source = {
        "format": "nw10-common-artifact-correctness",
        "status": "verified",
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": {"package_sha256": package},
        "ada": {"loop_boundary": {"status": "passed"}},
        "blackwell": {"loop_boundary": {"status": "passed"}},
        "comparison": {
            "fixed_ticks": [0, 240000, 480000, 720000, 959999],
            "per_tick": [
                {"tick": tick, "status": "passed"}
                for tick in (0, 240000, 480000, 720000, 959999)
            ],
            "worst_absolute_error": 0.0,
            "worst_relative_error": 0.0,
            "absolute_tolerance": 0.0001,
            "relative_tolerance": 0.0001,
            "absolute_required": True,
        },
        "packed_unpacked_comparison": {
            "status": "verified",
            "direct_separate_packed_gpu_capture_recorded": False,
            "packed_package_sha256": package,
            "unpacked_package_sha256": package,
            "worst_absolute_error": 0.0,
            "runtime": {
                "ada": {
                    "status": "passed",
                    "unpacked_executable_sha256": "1" * 64,
                    "packed_executable_sha256": "2" * 64,
                    "encoded_container_sha256": "3" * 64,
                    "codec_id": "nwc.codec.mszip.v1",
                    "layout_id": "nwc.layout.soa.v1",
                    "device_form_factor_source": "win32_system_enclosure_and_battery",
                    "frames_per_tick": 1,
                    "present_sync_interval": 0,
                },
                "blackwell": {
                    "status": "passed",
                    "unpacked_executable_sha256": "4" * 64,
                    "packed_executable_sha256": "5" * 64,
                    "encoded_container_sha256": "6" * 64,
                    "codec_id": "nwc.codec.mszip.v1",
                    "layout_id": "nwc.layout.soa.v1",
                    "device_form_factor_source": "win32_system_enclosure_and_battery",
                    "frames_per_tick": 1,
                    "present_sync_interval": 0,
                },
            },
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
        },
        "source": {
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": "1" * 40,
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
    with pytest.raises(ValueError, match="direct separate packed GPU capture"):
        publisher.publish_correctness(args)


def test_correctness_publisher_rejects_reused_inventory_form_factor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    package = "a" * 64
    source = {
        "format": "nw10-common-artifact-correctness",
        "status": "verified",
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": {"package_sha256": package},
        "ada": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
                "device_form_factor": "desktop",
                "device_form_factor_source": "physical host inventory: MSI B550",
                "classified_tier": "ada",
            },
            "loop_boundary": {"status": "passed"},
        },
        "blackwell": {
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
                "device_form_factor": "desktop",
                "device_form_factor_source": "win32_system_enclosure_and_battery",
                "classified_tier": "blackwell",
            },
            "loop_boundary": {"status": "passed"},
        },
        "comparison": {
            "fixed_ticks": [0, 240000, 480000, 720000, 959999],
            "per_tick": [
                {"tick": tick, "status": "passed"}
                for tick in (0, 240000, 480000, 720000, 959999)
            ],
            "worst_absolute_error": 0.0,
            "worst_relative_error": 0.0,
            "absolute_tolerance": 0.0001,
            "relative_tolerance": 0.0001,
            "absolute_required": True,
        },
        "packed_unpacked_comparison": {
            "status": "verified",
            "direct_separate_packed_gpu_capture_recorded": True,
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
            "packed_package_sha256": package,
            "unpacked_package_sha256": package,
            "worst_absolute_error": 0.0,
            "runtime": {
                "ada": {
                    "status": "passed",
                    "unpacked_executable_sha256": "1" * 64,
                    "packed_executable_sha256": "2" * 64,
                    "encoded_container_sha256": "3" * 64,
                    "codec_id": "nwc.codec.mszip.v1",
                    "layout_id": "nwc.layout.soa.v1",
                    "device_form_factor_source": "win32_system_enclosure_and_battery",
                    "frames_per_tick": 1,
                    "present_sync_interval": 0,
                },
                "blackwell": {
                    "status": "passed",
                    "unpacked_executable_sha256": "4" * 64,
                    "packed_executable_sha256": "5" * 64,
                    "encoded_container_sha256": "6" * 64,
                    "codec_id": "nwc.codec.mszip.v1",
                    "layout_id": "nwc.layout.soa.v1",
                    "device_form_factor_source": "win32_system_enclosure_and_battery",
                    "frames_per_tick": 1,
                    "present_sync_interval": 0,
                },
            },
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
        },
        "source": {
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": "1" * 40,
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
    with pytest.raises(ValueError, match="chassis probe"):
        publisher.publish_correctness(args)


def test_compact_selection_requires_explicit_post_validation_reason() -> None:
    artifact = {
        "executable_sha256": "a" * 64,
        "executable_bytes": 100,
        "package_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
    }
    compact = {
        "executable_sha256": "d" * 64,
        "executable_bytes": 90,
        "selected_mode": "compact",
        "selected_reason": "runtime_validation_pending",
        "selected_executable_sha256": "d" * 64,
        "selected_executable_bytes": 90,
        "decoded_package_sha256": "b" * 64,
        "codec_id": "nwc.codec.mszip.v1",
        "layout_id": "nwc.layout.soa.v1",
    }
    with pytest.raises(ValueError, match="post-validation"):
        publisher._artifact_from_intro_report(
            {
                "release_grade": True,
                "test_payload": False,
                "canonical_artifact": artifact,
                "compact_artifact": compact,
            }
        )
    compact["selected_reason"] = "explicit_compact_selection_after_validation"
    selected = publisher._artifact_from_intro_report(
        {
            "release_grade": True,
            "test_payload": False,
            "canonical_artifact": artifact,
            "compact_artifact": compact,
        }
    )
    assert selected["selected_mode"] == "compact"
    assert selected["executable_sha256"] == "d" * 64


def test_pending_physical_records_are_reproducible_and_fail_closed() -> None:
    correctness = publisher.build_pending_correctness()
    system = publisher.build_pending_system_validation()
    correctness_schema = json.loads(
        (
            REPO_ROOT / "schemas" / "nw10-common-artifact-correctness.schema.json"
        ).read_text(encoding="utf-8")
    )
    system_schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-system-validation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate_instance(correctness, correctness_schema, correctness_schema)
    validate_instance(system, system_schema, system_schema)
    assert correctness["status"] == "pending_unverified"
    assert correctness["source"]["implementation_head"] is None
    assert system["source_head_sha"] is None
    assert (
        correctness["packed_unpacked_comparison"][
            "direct_separate_packed_gpu_capture_recorded"
        ]
        is False
    )
    assert correctness["common_artifact"]["package_sha256"] is None
    assert system["status"] == "pending_unverified"
    assert system["final_artifact"] is None
    assert system["startup"] is None
    assert system["memory"] is None
    assert system["clean_system"] is None
    assert system["windows_build"] is None


    with pytest.raises(ValueError, match="cannot bind an implementation head"):
        publisher.build_pending_correctness(implementation_head="1" * 40)
    with pytest.raises(ValueError, match="cannot bind an implementation head"):
        publisher.build_pending_system_validation(implementation_head="1" * 40)


def test_system_publisher_verifies_bundle_and_can_complete_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(publisher, "_git_head", lambda: "1" * 40)
    artifact = {
        "executable_sha256": "1" * 64,
        "executable_bytes": 240702,
        "package_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
    }
    intro_report = tmp_path / "build" / "intro-report.json"
    _write_json(
        intro_report,
        {
            "release_grade": True,
            "test_payload": False,
            "canonical_artifact": artifact,
        },
    )

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
                "product_vendor": "ASUSTeK COMPUTER INC.",
                "product_name": "NUC13ANH-B",
                "bios_manufacturer": "American Megatrends Inc.",
                "bios_version": "NUC13ANBi.0058.2023.0613.1143",
                "baseboard_manufacturer": "ASUSTeK COMPUTER INC.",
                "baseboard_product": "NUC13ANH-B",
                "hypervisor_present": False,
                "virtualization_based_security_status": 0,
                "hypervisor_explained_by_vbs": False,
                "vm_markers": [],
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
                "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
                "driver_version": "32.0.16.1088",
                "executable_sha256": artifact["executable_sha256"],
                "package_sha256": artifact["package_sha256"],
                "execution_kind": "physical",
                "windows_build": "10.0.26200",
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
        "NVIDIA GeForce RTX 5070 Ti"
    )
    assert public["memory"]["peak_engine_committed_gpu_bytes"] == 42_926_080
    assert public["startup"]["hardware"]["windows_build"] == "10.0.26200"
    assert public["memory"]["hardware"]["windows_build"] == "10.0.26200"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["evidence_status"] == "verified_complete"
    assert status["physical_evidence"] == (
        "performance_correctness_and_system_verified"
    )
    system_path = tmp_path / "benchmarks" / "nw10" / "system-validation.v1.json"
    system_sha = publisher._text_sha256(system_path)
    system_rows = [
        row
        for row in status["files"]
        if row.get("path") == "benchmarks/nw10/system-validation.v1.json"
    ]
    assert system_rows == [
        {
            "path": "benchmarks/nw10/system-validation.v1.json",
            "sha256": system_sha,
        }
    ]


def test_blackwell_transfer_bundle_is_runtime_only_and_published_by_ci() -> None:
    wrapper = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    transferred = (
        REPO_ROOT / "tools" / "nw10" / "run-transferred-correctness-capture.ps1"
    ).read_text(encoding="utf-8")
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "nw10-compact.yml"
    ).read_text(encoding="utf-8")

    assert '"--preset", "nw07-diagnostics"' in wrapper
    assert "TARGET_DEBUG_CRT_REQUIRED=$($debugRuntimeDependencies.Count -gt 0)" in wrapper
    assert "TARGET_DEBUG_CRT_DEPENDENCIES=" in wrapper
    assert "Requires Windows/D3D12 and the debug CRT used by" in transferred
    assert "No compiler, CMake, DXC, Python, git, or repo." in transferred
    assert "Package Blackwell correctness transfer bundle" in workflow
    assert "nw10-correctness-transfer-blackwell" in workflow
    assert "transfer-blackwell/transfer-package/**" in workflow


def test_clean_summary_rejects_vm_markers_and_redefined_exit_codes() -> None:
    artifact = {
        "executable_sha256": "1" * 64,
        "executable_bytes": 100,
    }
    report = {
        "verification_status": "verified",
        "host": {
            "execution_kind": "physical",
            "clean_environment": True,
            "architecture": "AMD64",
            "windows_build": "10.0.26200",
            "development_dependencies_present_on_path": [],
            "environment_cache_variables": [],
            "virtualization_indicators": ["virtual machine"],
            "virtualization_probe": {
                "manufacturer": "Microsoft Corporation",
                "model": "Virtual Machine",
                "hypervisor_present": True,
                "virtualization_based_security_status": 2,
                "hypervisor_explained_by_vbs": True,
                "vm_markers": ["virtual machine"],
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
            for name, code in publisher.EXPECTED_SCENARIO_EXIT_CODES.items()
        ],
    }
    with pytest.raises(ValueError, match="virtualization markers"):
        publisher._clean_summary(
            report,
            bundle_sha256="a" * 64,
            artifact=artifact,
            runtime_hardware={"adapter_description": "NVIDIA GeForce RTX 5070 Ti"},
        )
    report["host"]["virtualization_indicators"] = []
    report["host"]["virtualization_probe"]["vm_markers"] = []
    report["scenarios"][0]["expected_exit_code"] = 0
    with pytest.raises(ValueError, match="frozen expected exit code"):
        publisher._clean_summary(
            report,
            bundle_sha256="a" * 64,
            artifact=artifact,
            runtime_hardware={"adapter_description": "NVIDIA GeForce RTX 5070 Ti"},
        )
