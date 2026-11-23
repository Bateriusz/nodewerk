from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_feasibility as feasibility  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nw05_export  # noqa: E402
import nw10_compact as compact  # noqa: E402
from schema_validator import validate_instance  # noqa: E402
import json  # noqa: E402
import pytest  # noqa: E402


_PACKAGE_BYTES = b"nw10-feasibility-package-fixture-" * 32
_PACKAGE_SHA = hashlib.sha256(_PACKAGE_BYTES).hexdigest()
_MANIFEST_DIGEST = b"\x11" * 32
_MANIFEST_SHA = hashlib.sha256(_MANIFEST_DIGEST).hexdigest()
_UNPACKED_EXE = nw05_export.embed_package(
    b"MZ" + b"\x00" * 128, _PACKAGE_BYTES, _MANIFEST_DIGEST
)
_COMPACT_CONTAINER = compact.encode_compact(
    _PACKAGE_BYTES,
    codec_id=contract.CODEC_MSZIP_ID,
    layout_id=contract.LAYOUT_SOA_ID,
)
_PACKED_EXE = nw05_export.embed_package(
    b"MZ" + b"\x00" * 128, _COMPACT_CONTAINER.bytes, _MANIFEST_DIGEST
)
_UNPACKED_SHA = hashlib.sha256(_UNPACKED_EXE).hexdigest()
_PACKED_SHA = hashlib.sha256(_PACKED_EXE).hexdigest()
_ENCODED_SHA = hashlib.sha256(_COMPACT_CONTAINER.bytes).hexdigest()


def _correctness_evidence() -> dict:
    return {
        "ada": {"unpacked": _UNPACKED_EXE, "packed": _PACKED_EXE},
        "blackwell": {"unpacked": _UNPACKED_EXE, "packed": _PACKED_EXE},
    }


def _proof() -> dict:
    return {
        "release_grade": True,
        "test_payload": False,
        "measurement_kind": "release_decoder_off",
        "nw10_build_inputs_digest": contract.build_inputs_digest(
            contract.compute_build_inputs()
        ),
        "templates": {
            "canonical_decoder_off": True,
            "compact_decoder_on": True,
        },
        "canonical_artifact": {
            "executable_bytes": 260_000,
            "executable_sha256": "a" * 64,
        },
        "compact_candidate": {
            "executable_bytes": 64_000,
            "executable_sha256": "f" * 64,
        },
    }


def _intro() -> dict:
    return {
        "release_grade": True,
        "test_payload": False,
        "measurement_kind": "release_restricted_intro",
        "nw10_build_inputs_digest": contract.build_inputs_digest(
            contract.compute_build_inputs()
        ),
        "canonical_artifact": {
            "executable_bytes": 240_000,
            "executable_sha256": "b" * 64,
            "package_sha256": _PACKAGE_SHA,
            "package_bytes": len(_PACKAGE_BYTES),
            "manifest_sha256": _MANIFEST_SHA,
            "manifest_bytes": 6_000,
        },
        "compact_artifact": {
            "executable_bytes": 65_000,
            "executable_sha256": "e" * 64,
            "selected_executable_bytes": 65_000,
            "selected_executable_sha256": "e" * 64,
            "selected_mode": "compact",
            "decoded_package_sha256": _PACKAGE_SHA,
            "decoded_package_bytes": len(_PACKAGE_BYTES),
            "encoded_package_bytes": len(_COMPACT_CONTAINER.bytes),
            "encoded_package_sha256": _ENCODED_SHA,
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
        },
        "ledgers": {
            "canonical": {
                "all_required_bytes_accounted": True,
                "dependencies": [
                    {"name": "d3d12", "kind": "windows_inbox", "distributed_bytes": 0}
                ],
            },
            "compact": {
                "all_required_bytes_accounted": False,
                "unaccounted_components": ["decoder_code_bytes"],
                "codec_id": contract.CODEC_MSZIP_ID,
                "layout_id": contract.LAYOUT_SOA_ID,
                "components": {
                    "decoder_code_bytes": None,
                    "encoded_package_bytes": len(_COMPACT_CONTAINER.bytes),
                },
                "required_auxiliary_files": {},
                "dependencies": [
                    {
                        "name": "cabinet.dll",
                        "kind": "windows_inbox",
                        "distributed_bytes": 0,
                    },
                    {
                        "name": "d3d12",
                        "kind": "windows_inbox",
                        "distributed_bytes": 0,
                    },
                ],
                "complete_runnable_distribution_bytes": 65_000,
            },
        },
        "visual_validation": {
            "status": "verified",
            "numeric_absolute": 0.0,
            "image_absolute": 0.0,
        },
    }


def _pe_ledger() -> dict:
    return {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "source": {
            "measurement_tool": "tools/nw10_pe_ledger.py",
            "head_sha": "1" * 40,
        },
        "final_artifact": {
            "executable_sha256": "e" * 64,
            "executable_bytes": 65_000,
            "package_sha256": _PACKAGE_SHA,
            "manifest_sha256": _MANIFEST_SHA,
            "pe_image_file_bytes": 64_000,
            "overlay_bytes": 1_000,
            "embedded_payload_kind": "compact_container",
            "embedded_payload_bytes": len(_COMPACT_CONTAINER.bytes),
            "embedded_payload_sha256": _ENCODED_SHA,
            "decoded_package_sha256": _PACKAGE_SHA,
            "package_binding_verified": True,
            "manifest_binding_verified": True,
            "dll_characteristics": "0x4060",
            "security_mitigations": {"guard_cf": True},
            "runtime_status": "validated_runnable",
            "runtime_exit_code": 0,
            "sections": [{"name": ".text", "raw_size_bytes": 63_000}],
        },
        "linker_elimination": {
            "status": "verified",
            "optimized_options": ["/OPT:REF"],
            "control_options": ["/OPT:NOREF"],
            "security_mitigations_preserved": True,
            "security_mitigation_bits": {
                "final": "0x4060",
                "linker_control": "0x4060",
                "decoder_on_template": "0x4060",
                "required": "0x4160",
                "control_required_bits_present": True,
                "decoder_on_required_bits_present": True,
            },
            "canonical_package_identical": True,
            "compact_decoder_preserved": True,
            "ltgc_preserved": True,
            "ltgc_evidence": {
                "decoder_on_template": {"status": "verified_build_tree_flags"},
                "decoder_off_template": {"status": "verified_build_tree_flags"},
            },
            "embedded_payload_sha256_identical": True,
            "embedded_payload_size_identical": True,
            "final_payload_kind": "compact_container",
            "control_payload_kind": "compact_container",
            "control_executable_bytes": 70_000,
            "control_pe_image_file_bytes": 69_000,
            "complete_distribution_bytes_saved": 5_000,
            "pe_image_bytes_saved": 5_000,
            "section_bytes_saved": [{"section": ".text", "bytes_saved": 5_000}],
        },
        "compact_decoder": {
            "status": "verified",
            "decoder_on_template_bytes": 510_464,
            "decoder_off_template_bytes": 506_880,
            "template_bytes_added": 3_584,
            "pe_image_bytes_added": 3_584,
            "runtime_import_added": "cabinet.dll",
            "runtime_imports_added": ["cabinet.dll"],
            "compact_payload_verified": True,
            "final_imports_cabinet": True,
            "final_payload_kind": "compact_container",
            "final_payload_sha256": _ENCODED_SHA,
            "final_decoded_package_sha256": _PACKAGE_SHA,
        },
        "conclusion": {
            "byte_ledger_complete": True,
            "linker_elimination_measured_on_exact_windows_artifact": True,
            "decoder_cost_measured_on_pinned_windows_template": True,
            "universal_64k_claim": False,
        },
        "privacy": {
            "local_paths_included": False,
            "raw_linker_map_included": False,
            "pdb_paths_included": False,
        },
    }


def _dependency_closure() -> dict:
    return {
        "closure_verified": True,
        "binary_sha256": "e" * 64,
        "sidecar_files": [],
        "canonical_imports": ["cabinet.dll", "d3d12.dll", "kernel32.dll"],
    }


def _system_validation() -> dict:
    hardware = {
        "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
        "driver_version": "32.0.16.1088",
    }
    windows_build = "10.0.22631"
    scenarios = [
        {
            "scenario": name,
            "expected_exit_code": code,
            "observed_exit_code": code,
            "status": "passed",
            "timed_out": False,
        }
        for name, code in feasibility.clean_system_contract.EXPECTED_SCENARIO_EXIT_CODES.items()
    ]
    return {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "source_head_sha": "1" * 40,
        "final_artifact": {
            "executable_sha256": "e" * 64,
            "executable_bytes": 65_000,
            "package_sha256": _PACKAGE_SHA,
            "manifest_sha256": _MANIFEST_SHA,
        },
        "windows_build": windows_build,
        "clean_system": {
            "status": "verified",
            "execution_kind": "physical",
            "deployment": {
                "only_runtime_executable": True,
                "sidecars": [],
                "forbidden_runtime_dependencies": [],
                "executable_sha256": "e" * 64,
                "executable_bytes": 65_000,
            },
            "host": {
                "clean_environment": True,
                "windows_build": windows_build,
                "architecture": "AMD64",
                "virtualization_probe": {
                    "hypervisor_present": False,
                    "hypervisor_explained_by_vbs": False,
                    "vm_markers": [],
                },
            },
            "hardware": hardware,
            "development_dependencies_present_on_path": [],
            "environment_cache_variables": [],
            "scenarios": scenarios,
        },
        "startup": {
            "status": "verified",
            "execution_kind": "physical",
            "process_start_to_first_present_ms": 100.0,
            "ceiling_ms": 2_000.0,
            "cache_state": {
                "kind": "cold_driver_cache",
                "application_pipeline_cache_used": False,
            },
            "hardware": {**hardware, "windows_build": windows_build},
        },
        "memory": {
            "status": "verified",
            "execution_kind": "physical",
            "peak_engine_committed_gpu_bytes": 1_000_000,
            "ceiling_bytes": 536_870_912,
            "package_sha256": _PACKAGE_SHA,
            "post_initialization_allocation_violation": 0,
            "hardware": {**hardware, "windows_build": windows_build},
        },
        "privacy": {
            "local_paths_included": False,
            "raw_stdout_stderr_included": False,
            "user_profile_paths_included": False,
            "raw_result_bundle_tracked": False,
        },
    }


def _performance() -> dict:
    reference_environment = (
        feasibility.nw10_perf_preservation._technical_reference(
            _PERF_REFERENCE
        )
    )
    binding_id = _perf_contract()["binding_id"]
    binding_digest = _perf_contract()["binding_digest"]

    def capture(capture_id: str, executable_sha: str, bytes_: int) -> dict:
        return {
            "capture_id": capture_id,
            "artifact": {
                "artifact_key": "sha256:v1:" + "a" * 64,
                "package_sha256": _PACKAGE_SHA,
                "manifest_sha256": _MANIFEST_SHA,
                "executable_sha256": executable_sha,
                "complete_distribution_bytes": bytes_,
            },
            "runs": [
                {
                    "run_index": index,
                    "status": "passed",
                    "duration_seconds": 60,
                    "gpu_p99_ms": 10.0,
                    "gpu_sample_count": 100,
                    "cpu_p99_ms": 5.0,
                    "cpu_sample_count": 100,
                }
                for index in range(1, 4)
            ],
            "worst_gpu_p99_ms": 10.0,
            "worst_cpu_submit_p99_ms": 5.0,
        }
    return {
        "format": "nw10-performance-preservation",
        "version": 1,
        "task": "NW-10",
        "performance_contract": "nw07_perf_ref_v1",
        "performance_protocol": "nw07-perf-run.v1",
        "reference_binding_id": binding_id,
        "reference_binding_digest": binding_digest,
        "status": "verified_preserved",
        "reasons": [],
        "matched_environment": True,
        "reference_environment": reference_environment,
        "before": capture("before", "b" * 64, 65_001),
        "after": capture("after", "e" * 64, 65_000),
        "comparison": {
            "same_canonical_package_sha256": True,
            "gpu_worst_p99_delta_percent": 0.0,
            "cpu_worst_p99_delta_percent": 0.0,
            "relative_alert_threshold_percent": 10,
            "relative_regression_alert": False,
            "absolute_gate_operator": "<",
            "absolute_gate_ms": 16.67,
            "faster_substitute_used": False,
        },
        "privacy": {
            "raw_samples_included": False,
            "process_ids_included": False,
            "stdout_stderr_included": False,
            "local_paths_included": False,
            "whitelist_generated": True,
        },
    }


_PERF_REFERENCE = {
    "board": {
        "model": "NVIDIA GeForce RTX 4060 Ti",
        "vendor_id": 4318,
        "device_id": 10245,
        "subsystem_vendor_id": 4318,
        "subsystem_device_id": 10245,
        "vram_mib": 8192,
        "architecture": "Ada",
    },
    "host": {
        "cpu_model": "AMD Ryzen 7 7800X3D",
        "physical_cores": 8,
        "logical_processors": 16,
        "motherboard": "X670E",
        "memory": "32 GiB DDR5",
    },
    "environment": {
        "os": "Windows 11 x64",
        "execution_kind": "physical",
        "os_build": "10.0.22631",
        "driver_version": "32.0.16.1088",
        "wddm_driver_version": "3.1",
        "windows_power_plan": "Ultimate Performance",
        "nvidia_power_management_mode": "Prefer maximum performance",
        "pcie_power_management": "off",
        "gpu_clocks": "default",
        "temperature_c": 25,
    },
    "toolchain": {
        "toolchain_identity": "msvc-19.40.33821-lld",
        "toolchain_manifest_sha256": "a" * 64,
        "dxc_version": "1.8.2407",
        "msvc_version": "19.40.33821",
        "windows_sdk": "10.0.26100.0",
    },
    "quality": {
        "profile_id": "nw07.quality.v1",
        "resolution": {"width": 1920, "height": 1080},
        "vsync": False,
        "presentation_mode": "flip_discard",
        "msaa_samples": 1,
        "filtering": "linear",
        "anisotropy": 1,
        "texture_format": "r8g8b8a8_unorm",
        "intermediate_format": "rgba32f",
        "bloom_resolution_scale": 0.5,
        "tonemap_operator": "aces",
        "tone_exposure": 1.1,
        "tone_gamma": 2.2,
        "feature_level": "12_0",
        "shader_model": "6_0",
    },
    "cache": {
        "clean_start_application_pipeline_cache": False,
        "accepted_states": ["cold_driver_cache", "warm"],
        "cold_preparation": "Clear the driver shader cache or reboot the host",
        "warm_preparation": "Pre-run the identical artifact once",
    },
    "artifact": {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": _PACKAGE_SHA,
        "manifest_sha256": _MANIFEST_SHA,
        "executable_sha256": "b" * 64,
        "complete_distribution_bytes": 65_001,
    },
}


def _perf_contract() -> dict:
    return {
        "format": "nw07-performance-reference",
        "version": 1,
        "task": "NW-07",
        "contract_id": "nw07_perf_ref_v1",
        "contract_status": "frozen",
        "binding_status": "bound",
        "binding_id": "nw07_perf_ref_v1@sha256:v1:" + "a" * 64,
        "binding_digest": "sha256:v1:" + "a" * 64,
        "reference": _PERF_REFERENCE,
    }


def _correctness() -> dict:
    def runtime() -> dict:
        return {
            "status": "passed",
            "unpacked_executable_sha256": _UNPACKED_SHA,
            "packed_executable_sha256": _PACKED_SHA,
            "encoded_container_sha256": _ENCODED_SHA,
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
            "device_form_factor_source": "win32_system_enclosure_and_battery",
            "frames_per_tick": 1,
            "present_sync_interval": 0,
        }

    common = {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": _PACKAGE_SHA,
        "manifest_sha256": _MANIFEST_SHA,
        "profile_id": "nw.common.d3d12.fl12_0.sm6_0.v1",
        "requirements_digest": "sha256:v1:" + "b" * 64,
        "final_executable_sha256": "e" * 64,
    }
    return {
        "format": "nw10-common-artifact-correctness-summary",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "source_head_sha": "1" * 40,
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": common,
        "ada": {
            "status": "verified",
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 4060 Ti",
                "classified_tier": "ada",
                "device_form_factor": "desktop",
                "device_form_factor_source": "win32_system_enclosure_and_battery",
                "driver_version": "32.0.16.1088",
            },
            "artifact": {
                "unpacked_executable_sha256": _UNPACKED_SHA,
                "packed_executable_sha256": _PACKED_SHA,
                "encoded_container_sha256": _ENCODED_SHA,
            },
        },
        "blackwell": {
            "status": "verified",
            "hardware": {
                "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
                "classified_tier": "blackwell",
                "device_form_factor": "desktop",
                "device_form_factor_source": "win32_system_enclosure_and_battery",
                "driver_version": "32.0.16.1088",
            },
            "artifact": {
                "unpacked_executable_sha256": _UNPACKED_SHA,
                "packed_executable_sha256": _PACKED_SHA,
                "encoded_container_sha256": _ENCODED_SHA,
            },
        },
        "comparison": {
            "fixed_ticks": [0, 240_000, 480_000, 720_000, 959_999],
            "fixed_tick_status": "passed",
            "loop_boundary_ticks": [0, 960_000],
            "ada_loop_boundary_status": "passed",
            "blackwell_loop_boundary_status": "passed",
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
            "packed_package_sha256": _PACKAGE_SHA,
            "unpacked_package_sha256": _PACKAGE_SHA,
            "worst_absolute_error": 0.0,
            "runtime": {
                "ada": runtime(),
                "blackwell": runtime(),
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
            "raw_capture_manifests_tracked": False,
        },
    }


def _outcome(**overrides) -> dict:
    value = {
        "minimum_measured_candidate_bytes": 65_000,
        "minimum_validated_runnable_bytes": 65_000,
        "validated_mode": "compact",
        "compact_candidate_runtime_status": "validated_runnable",
        "compact_candidate_exit_code": 0,
        "canonical_fallback_exit_code": None,
        "intro_compact_candidate_bytes": 65_000,
        "proof_compact_candidate_bytes": 65_000,
    }
    value.update(overrides)
    return value


def _schema() -> dict:
    return json.loads(
        (
            REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json"
        ).read_text(encoding="utf-8")
    )


def test_feasibility_success_branch_is_schema_valid(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest="sha256:" + "3" * 64,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is True
    assert record["classification"] == "complete"
    assert record["compact_candidate"]["runtime_status"] == "validated_runnable"
    assert record["acceptance_linkage"]["true_64k_artifact_delivered"] is True
    assert record["certification"]["status"] == "verified"


def test_feasibility_byte_target_alone_cannot_certify_success() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["acceptance_linkage"]["true_64k_artifact_delivered"] is False
    assert record["certification"]["status"] == "pending"
    assert "clean_system" in record["certification"]["pending_gates"]
    assert "dependency_closure" in record["certification"]["pending_gates"]
    assert "pe_ledger_binding" in record["certification"]["pending_gates"]
    assert any(
        blocker["id"] == "compact_certification_evidence"
        for blocker in record["blockers"]
    )


def test_missing_pe_ledger_leaves_dependent_gates_pending(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=None,
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["status"] == "pending"
    assert record["certification"]["failed_gates"] == []
    assert record["certification"]["clean_system"]["status"] == "verified"
    assert record["certification"]["performance"]["status"] == "verified"
    assert record["certification"]["visual"]["status"] == "verified"
    assert record["certification"]["pe_ledger_binding"]["status"] == "pending"


def test_fabricated_or_anonymous_evidence_cannot_certify_success(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    system = _system_validation()
    system["clean_system"]["scenarios"] = [
        {"status": "passed", "timed_out": False} for _ in range(6)
    ]
    performance = {
        "status": "verified_preserved",
        "reference_binding_id": "binding-1",
        "after": {
            "runs": [{"status": "passed"} for _ in range(3)],
            "worst_gpu_p99_ms": 10.0,
            "worst_cpu_submit_p99_ms": 5.0,
        },
    }
    correctness = {"status": "verified"}
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=system,
        performance=performance,
        correctness=correctness,
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["certification"]["status"] == "failed"
    assert "clean_system" in record["certification"]["failed_gates"]
    assert "performance" in record["certification"]["failed_gates"]
    assert "visual" in record["certification"]["failed_gates"]


def test_partial_system_record_cannot_certify_success(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    system = _system_validation()
    del system["final_artifact"]["manifest_sha256"]
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=system,
        performance=_performance(),
        correctness=_correctness(),
        perf_contract=_perf_contract(),
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is False
    assert record["certification"]["clean_system"]["status"] == "failed"
    assert "manifest_sha256" in record["certification"]["clean_system"][
        "schema_error"
    ]


def test_non_x64_clean_host_cannot_certify_success(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    system = _system_validation()
    system["clean_system"]["host"]["architecture"] = "ARM64"
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=system,
        performance=_performance(),
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["clean_system"]["status"] == "failed"


def test_cross_artifact_manifest_mismatch_cannot_certify_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    system = copy.deepcopy(_system_validation())
    system["final_artifact"]["manifest_sha256"] = "9" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=system,
        performance=_performance(),
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["clean_system"]["status"] == "failed"

    performance = copy.deepcopy(_performance())
    performance["after"]["artifact"]["manifest_sha256"] = "9" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=performance,
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["performance"]["status"] == "failed"

    correctness = copy.deepcopy(_correctness())
    correctness["common_artifact"]["manifest_sha256"] = "9" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=correctness,
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["visual"]["status"] == "failed"


def test_fabricated_diagnostic_evidence_cannot_certify_success(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    evidence = _correctness_evidence()
    evidence["ada"]["packed"] = _UNPACKED_EXE
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=_correctness(),
        correctness_evidence=evidence,
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["visual"]["status"] == "failed"
    assert (
        record["certification"]["visual"][
            "retained_diagnostic_evidence_verified"
        ]
        is False
    )


def test_codec_or_layout_identity_mismatch_cannot_certify_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    stored = compact.encode_compact(
        _PACKAGE_BYTES,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_SOA_ID,
    )
    stored_exe = nw05_export.embed_package(
        b"MZ" + b"\x00" * 128, stored.bytes, _MANIFEST_DIGEST
    )
    stored_exe_sha = hashlib.sha256(stored_exe).hexdigest()
    stored_encoded_sha = hashlib.sha256(stored.bytes).hexdigest()
    correctness = copy.deepcopy(_correctness())
    for tier in ("ada", "blackwell"):
        correctness["packed_unpacked_comparison"]["runtime"][tier][
            "packed_executable_sha256"
        ] = stored_exe_sha
        correctness["packed_unpacked_comparison"]["runtime"][tier][
            "encoded_container_sha256"
        ] = stored_encoded_sha
        correctness[tier]["artifact"][
            "packed_executable_sha256"
        ] = stored_exe_sha
        correctness[tier]["artifact"][
            "encoded_container_sha256"
        ] = stored_encoded_sha
    evidence = _correctness_evidence()
    evidence["ada"]["packed"] = stored_exe
    evidence["blackwell"]["packed"] = stored_exe
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=correctness,
        correctness_evidence=evidence,
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["visual"]["status"] == "failed"
    assert (
        record["certification"]["visual"]["codec_layout_identity_verified"]
        is False
    )


def test_performance_manifest_binding_is_independent_of_correctness(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=None,
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=None,
        correctness_evidence=None,
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["status"] == "pending"
    assert record["certification"]["clean_system"]["status"] == "verified"
    assert record["certification"]["performance"]["status"] == "verified"
    assert record["certification"]["visual"]["status"] == "pending"


def test_failed_performance_manifest_does_not_poison_other_verified_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    performance = copy.deepcopy(_performance())
    performance["status"] = "failed"
    performance["after"]["artifact"]["manifest_sha256"] = "9" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=performance,
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["performance"]["status"] == "failed"
    assert record["certification"]["clean_system"]["status"] == "verified"
    assert record["certification"]["visual"]["status"] == "verified"


@pytest.mark.parametrize(
    ("mutator", "failed_gate"),
    [
        (
            lambda intro, pe: intro["compact_artifact"].__setitem__(
                "encoded_package_sha256", "9" * 64
            ),
            "visual",
        ),
        (
            lambda intro, pe: intro["ledgers"]["compact"]["components"].__setitem__(
                "encoded_package_bytes", len(_COMPACT_CONTAINER.bytes) + 1
            ),
            "visual",
        ),
        (
            lambda intro, pe: pe["final_artifact"].__setitem__(
                "embedded_payload_sha256", "9" * 64
            ),
            "pe_ledger_binding",
        ),
        (
            lambda intro, pe: pe["final_artifact"].__setitem__(
                "embedded_payload_bytes", len(_COMPACT_CONTAINER.bytes) + 1
            ),
            "pe_ledger_binding",
        ),
    ],
)
def test_encoded_payload_identity_is_cross_bound_across_all_evidence(
    monkeypatch, mutator, failed_gate
) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    intro = copy.deepcopy(_intro())
    pe = copy.deepcopy(_pe_ledger())
    mutator(intro, pe)
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=intro,
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=pe,
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=_correctness(),
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"][failed_gate]["status"] == "failed"


def test_retained_diagnostic_manifest_must_match_intro_manifest(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    other_manifest = b"\x22" * 32
    unpacked = nw05_export.embed_package(
        b"MZ" + b"\x00" * 128, _PACKAGE_BYTES, other_manifest
    )
    packed = nw05_export.embed_package(
        b"MZ" + b"\x00" * 128, _COMPACT_CONTAINER.bytes, other_manifest
    )
    unpacked_sha = hashlib.sha256(unpacked).hexdigest()
    packed_sha = hashlib.sha256(packed).hexdigest()
    correctness = copy.deepcopy(_correctness())
    for tier in ("ada", "blackwell"):
        correctness["packed_unpacked_comparison"]["runtime"][tier][
            "unpacked_executable_sha256"
        ] = unpacked_sha
        correctness["packed_unpacked_comparison"]["runtime"][tier][
            "packed_executable_sha256"
        ] = packed_sha
        correctness[tier]["artifact"][
            "unpacked_executable_sha256"
        ] = unpacked_sha
        correctness[tier]["artifact"]["packed_executable_sha256"] = packed_sha
    evidence = {
        "ada": {"unpacked": unpacked, "packed": packed},
        "blackwell": {"unpacked": unpacked, "packed": packed},
    }
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=_performance(),
        correctness=correctness,
        correctness_evidence=evidence,
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["visual"]["status"] == "failed"
    assert (
        record["certification"]["visual"]["retained_manifest_binding_verified"]
        is False
    )


def test_negative_performance_values_cannot_certify_success(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    performance = _performance()
    performance["after"]["runs"][0]["gpu_p99_ms"] = -1.0
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=performance,
        correctness=_correctness(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["performance"]["status"] == "failed"

    performance = _performance()
    performance["after"]["worst_cpu_submit_p99_ms"] = -0.01
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=_pe_ledger(),
        dependency_closure=_dependency_closure(),
        system_validation=_system_validation(),
        performance=performance,
        correctness=_correctness(),
        perf_contract=_perf_contract(),
    )
    assert record["target_met"] is False
    assert record["certification"]["performance"]["status"] == "failed"


def test_validated_canonical_single_file_at_target_can_complete(monkeypatch) -> None:
    monkeypatch.setattr(
        feasibility.nw07_evidence,
        "validate_perf_contract",
        lambda record: {"status": "frozen", "reasons": [], "missing_bindings": []},
    )
    canonical_sha = "b" * 64
    intro = _intro()
    intro["canonical_artifact"]["executable_bytes"] = 65_000
    intro["compact_artifact"]["selected_mode"] = "canonical"
    intro["compact_artifact"]["selected_executable_bytes"] = 65_000
    intro["compact_artifact"]["selected_executable_sha256"] = canonical_sha

    pe = copy.deepcopy(_pe_ledger())
    pe["final_artifact"]["executable_sha256"] = canonical_sha
    pe["final_artifact"]["embedded_payload_kind"] = "canonical_package"
    pe["final_artifact"]["embedded_payload_bytes"] = len(_PACKAGE_BYTES)
    pe["final_artifact"]["embedded_payload_sha256"] = _PACKAGE_SHA
    pe["final_artifact"]["decoded_package_sha256"] = _PACKAGE_SHA
    pe["linker_elimination"]["compact_decoder_preserved"] = False
    pe["linker_elimination"]["final_payload_kind"] = "canonical_package"
    pe["linker_elimination"]["control_payload_kind"] = "canonical_package"
    pe["compact_decoder"]["compact_payload_verified"] = False
    pe["compact_decoder"]["final_imports_cabinet"] = False
    pe["compact_decoder"]["final_payload_kind"] = "canonical_package"
    pe["compact_decoder"]["final_payload_sha256"] = _PACKAGE_SHA
    pe["compact_decoder"]["final_decoded_package_sha256"] = _PACKAGE_SHA

    dependency = _dependency_closure()
    dependency["binary_sha256"] = canonical_sha
    dependency["canonical_imports"] = ["d3d12.dll", "kernel32.dll"]

    system = copy.deepcopy(_system_validation())
    system["final_artifact"]["executable_sha256"] = canonical_sha
    system["clean_system"]["deployment"]["executable_sha256"] = canonical_sha

    performance = copy.deepcopy(_performance())
    performance["after"]["artifact"]["executable_sha256"] = canonical_sha

    correctness = copy.deepcopy(_correctness())
    correctness["common_artifact"]["final_executable_sha256"] = canonical_sha

    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_validated_runnable_bytes=65_000,
            validated_mode="canonical_fallback",
            compact_candidate_runtime_status="rejected_runtime_failure",
            compact_candidate_exit_code=70,
            canonical_fallback_exit_code=0,
        ),
        proof=_proof(),
        intro=intro,
        run_id=1,
        head_sha="1" * 40,
        pe_ledger=pe,
        dependency_closure=dependency,
        system_validation=system,
        performance=performance,
        correctness=correctness,
        correctness_evidence=_correctness_evidence(),
        perf_contract=_perf_contract(),
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is True
    assert record["classification"] == "complete"
    assert record["validated_artifact"]["mode"] == "canonical_fallback"
    assert record["validated_artifact"]["bytes"] == 65_000
    assert record["acceptance_linkage"]["true_64k_artifact_delivered"] is True


def test_validated_compact_identity_is_not_taken_from_smaller_proof_candidate() -> None:
    proof = _proof()
    proof["compact_candidate"]["executable_bytes"] = 60_000
    proof["compact_candidate"]["executable_sha256"] = "9" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=60_000,
            intro_compact_candidate_bytes=65_000,
            proof_compact_candidate_bytes=60_000,
            minimum_validated_runnable_bytes=65_000,
            validated_mode="compact",
        ),
        proof=proof,
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    assert record["measurements"]["minimum_measured_candidate_bytes"] == 60_000
    assert record["compact_candidate"]["bytes"] == 65_000
    assert record["compact_candidate"]["sha256"] == "e" * 64
    assert record["selected_artifact"]["bytes"] == 65_000
    assert record["selected_artifact"]["sha256"] == "e" * 64


def test_feasibility_does_not_invent_unknown_artifact_identity() -> None:
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    assert "artifact_id" not in record["source"]
    assert "artifact_digest" not in record["source"]


def test_feasibility_normalizes_github_artifact_digest_output() -> None:
    raw_digest = "3" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest=raw_digest,
    )
    assert record["source"]["artifact_digest"] == "sha256:" + raw_digest
    schema = _schema()
    validate_instance(record, schema, schema)


def test_feasibility_rejects_malformed_artifact_digest() -> None:
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=_proof(),
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
            artifact_id=2,
            artifact_digest="not-a-digest",
        )
    except ValueError as exc:
        assert "SHA-256 digest" in str(exc)
    else:
        raise AssertionError("malformed artifact digest was accepted")


def test_feasibility_failure_branch_keeps_canonical_fallback_and_continue() -> None:
    intro = _intro()
    intro["compact_artifact"]["selected_mode"] = "canonical"
    intro["compact_artifact"]["selected_executable_bytes"] = 240_000
    intro["compact_artifact"]["selected_executable_sha256"] = "b" * 64
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=227_715,
            minimum_validated_runnable_bytes=240_000,
            validated_mode="canonical_fallback",
            compact_candidate_runtime_status="rejected_runtime_failure",
            compact_candidate_exit_code=70,
            canonical_fallback_exit_code=0,
            proof_compact_candidate_bytes=227_715,
        ),
        proof=_proof(),
        intro=intro,
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest="sha256:" + "3" * 64,
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["compact_candidate"]["runtime_status"] == "rejected_runtime_failure"
    assert record["canonical_fallback"]["runtime_status"] == "validated_runnable"
    assert record["canonical_fallback"]["exit_code"] == 0


def test_hosted_access_violation_remains_pending_and_schema_valid() -> None:
    access_violation = -1073741819
    intro = _intro()
    intro["ledgers"] = {
        "canonical": {
            "dependencies": [
                {"name": "d3d12", "kind": "windows_inbox", "distributed_bytes": 0},
                {"name": "dxgi", "kind": "windows_inbox", "distributed_bytes": 0},
            ]
        }
    }
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_measured_candidate_bytes=228_181,
            minimum_validated_runnable_bytes=None,
            validated_mode="canonical_fallback_pending_physical",
            compact_candidate_runtime_status="pending_runtime_validation",
            compact_candidate_exit_code=access_violation,
            canonical_fallback_exit_code=access_violation,
            intro_compact_candidate_bytes=228_181,
            proof_compact_candidate_bytes=238_188,
        ),
        proof=_proof(),
        intro=intro,
        run_id=1,
        head_sha="1" * 40,
        artifact_id=2,
        artifact_digest="sha256:" + "3" * 64,
    )
    schema = _schema()
    validate_instance(record, schema, schema)
    assert record["target_met"] is False
    assert record["measurements"]["validated_mode"] == (
        "canonical_fallback_pending_physical"
    )
    assert record["compact_candidate"]["runtime_status"] == "pending_runtime_validation"
    assert record["compact_candidate"]["exit_code"] == access_violation
    assert record["compact_candidate"]["exit_code_hex"] == "0xC0000005"
    assert record["compact_candidate"]["failure_classification"] == (
        "non_certifying_hosted_failure"
    )
    assert record["canonical_fallback"]["runtime_status"] == "pending_runtime_validation"
    assert record["canonical_fallback"]["exit_code"] == access_violation
    assert record["canonical_fallback"]["exit_code_hex"] == "0xC0000005"
    assert record["canonical_fallback"]["dependency_closure"]["canonical_imports"] == [
        "d3d12",
        "dxgi",
    ]
    assert record["blocker"]["status"] == "pending_physical"


def test_windows_exit_code_rejects_values_outside_signed_32_bit() -> None:
    for value in (-(2**31) - 1, 2**31):
        try:
            feasibility._windows_exit_code(value)
        except ValueError as exc:
            assert "signed 32-bit" in str(exc)
        else:
            raise AssertionError("out-of-range Windows exit code was accepted")


def test_feasibility_records_canonical_proof_above_the_frozen_tier() -> None:
    proof = _proof()
    proof["canonical_artifact"]["executable_bytes"] = 262_145
    record = feasibility.build_feasibility(
        outcome=_outcome(),
        proof=proof,
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    validate_instance(record, _schema(), _schema())
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["measurements"]["canonical_proof_within_tier"] is False
    assert record["blocker"]["id"] == "canonical_proof_tier"
    assert record["blocker"]["measured_bytes"] == 262_145


def test_feasibility_accepts_decoder_off_size_blocked_report_without_artifact() -> None:
    proof = _proof()
    proof.pop("canonical_artifact")
    proof.update(
        {
            "measurement_kind": "release_decoder_off_size_blocked",
            "success": False,
            "measurement_outcome": "size_target_not_met",
            "measured_distribution_bytes": 270_000,
        }
    )
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_validated_runnable_bytes=None,
            validated_mode="none",
            compact_candidate_runtime_status="pending_runtime_validation",
        ),
        proof=proof,
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
    )
    validate_instance(record, _schema(), _schema())
    assert record["classification"] == "continue"
    assert record["measurements"]["proof_measured_bytes"] == 270_000
    assert record["measurements"]["proof_measurement_status"] == (
        "measured_decoder_off_size_blocked"
    )
    assert record["blockers"][0]["id"] == "canonical_proof_tier"


def test_feasibility_reproducibly_selects_prior_valid_ledger_when_current_is_pending() -> None:
    prior = {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "task": "NW-10",
        "final_artifact": {
            "executable_sha256": "9" * 64,
            "executable_bytes": 240_702,
            "runtime_status": "validated_runnable",
        },
        "conclusion": {"byte_ledger_complete": True},
        "source": {"head_sha": "8" * 40},
    }
    record = feasibility.build_feasibility(
        outcome=_outcome(
            minimum_validated_runnable_bytes=None,
            validated_mode="canonical_fallback_pending_physical",
            compact_candidate_runtime_status="pending_runtime_validation",
            compact_candidate_exit_code=-1073741819,
            canonical_fallback_exit_code=-1073741819,
        ),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        prior_valid_ledger=prior,
        prior_valid_ledger_path="benchmarks/nw10/windows-pe-ledger.v1.json",
    )
    validate_instance(record, _schema(), _schema())
    assert record["prior_valid_artifact"]["executable_sha256"] == "9" * 64
    assert record["selected_artifact"]["sha256"] == "9" * 64
    assert record["selected_artifact"]["source"] == "prior_valid_artifact"
    assert record["selected_artifact"]["ledger"] == (
        "benchmarks/nw10/windows-pe-ledger.v1.json"
    )


def test_feasibility_rejects_malformed_or_unattributed_prior_ledger() -> None:
    prior = {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "task": "NW-10",
        "final_artifact": {
            "executable_sha256": "9" * 64,
            "executable_bytes": 240_702,
            "runtime_status": "validated_runnable",
        },
        "conclusion": {"byte_ledger_complete": True},
    }
    kwargs = dict(
        outcome=_outcome(
            minimum_validated_runnable_bytes=None,
            validated_mode="canonical_fallback_pending_physical",
            compact_candidate_runtime_status="pending_runtime_validation",
            compact_candidate_exit_code=-1073741819,
            canonical_fallback_exit_code=-1073741819,
        ),
        proof=_proof(),
        intro=_intro(),
        run_id=1,
        head_sha="1" * 40,
        prior_valid_ledger=prior,
    )
    with pytest.raises(ValueError, match="provenance path is required"):
        feasibility.build_feasibility(**kwargs)

    prior["format"] = "wrong"
    with pytest.raises(ValueError, match="unexpected format"):
        feasibility.build_feasibility(
            **kwargs,
            prior_valid_ledger_path="benchmarks/nw10/windows-pe-ledger.v1.json",
        )

    prior["format"] = "nw10-windows-pe-ledger"
    prior["final_artifact"]["executable_sha256"] = "g" * 64
    with pytest.raises(ValueError, match="SHA-256 is malformed"):
        feasibility.build_feasibility(
            **kwargs,
            prior_valid_ledger_path="benchmarks/nw10/windows-pe-ledger.v1.json",
        )


def test_feasibility_requires_measured_release_decoder_off_proof() -> None:
    proof = _proof()
    proof["measurement_kind"] = "contract_only"
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=proof,
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
        )
    except ValueError as exc:
        assert "decoder-OFF" in str(exc)
    else:
        raise AssertionError("contract-only proof was accepted as feasibility evidence")

    proof = _proof()
    proof["nw10_build_inputs_digest"] = "sha256:v1:" + "0" * 64
    try:
        feasibility.build_feasibility(
            outcome=_outcome(),
            proof=proof,
            intro=_intro(),
            run_id=1,
            head_sha="1" * 40,
        )
    except ValueError as exc:
        assert "build inputs" in str(exc)
    else:
        raise AssertionError("stale proof build inputs were accepted")


def test_workflow_binds_feasibility_to_the_uploaded_artifact_identity() -> None:
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "nw10-compact.yml"
    ).read_text(encoding="utf-8")
    assert "--artifact-id" in workflow
    assert "--artifact-digest" in workflow
    assert "steps.nw10_evidence_upload.outputs.artifact-id" in workflow
    assert "steps.nw10_evidence_upload.outputs.artifact-digest" in workflow
    assert "--runtime-status $runtimeStatus" in workflow
    assert "--runtime-exit-code $runtimeExitCode" in workflow
    assert "--prior-valid-ledger" in workflow
    assert "Bind PE ledger to the uploaded artifact identity" in workflow
    assert "--bind-source-report" in workflow
    assert "nw10-windows-pe-ledger-bound.json" in workflow
