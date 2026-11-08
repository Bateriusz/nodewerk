from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from schema_validator import (  # noqa: E402
    SchemaValidationError,
    validate_all_repository_artifacts,
    validate_instance,
    validate_semantic_invariants,
)


def test_all_repository_json_artifacts_validate_against_schemas() -> None:
    summary = validate_all_repository_artifacts(REPO_ROOT)
    assert summary["validated_count"] == 17
    assert all(item["valid"] for item in summary["results"])


def test_capability_manifest_baseline_and_cpp_enforcement_agreement() -> None:
    cap = json.loads((REPO_ROOT / "benchmarks" / "capability-manifest.v1.json").read_text(encoding="utf-8"))
    assert cap["target_platform"]["os"] == "windows_11_x64"
    assert cap["target_platform"]["minimum_tested_windows_build"] == "10.0.26200.9711"
    assert "windows_10" in cap["target_platform"]["unsupported_platforms"]
    assert "linux" in cap["target_platform"]["unsupported_platforms"]
    assert "macos" in cap["target_platform"]["unsupported_platforms"]

    drivers = {d["hardware_tier"]: d for d in cap["target_platform"]["tested_nvidia_drivers"]}
    assert set(drivers.keys()) == {"rtx_4060_desktop", "rtx_50_series_desktop"}
    assert drivers["rtx_4060_desktop"]["gpu_model"] == "NVIDIA GeForce RTX 4060 Ti"
    assert drivers["rtx_4060_desktop"]["driver_version"] == "610.88"
    assert drivers["rtx_4060_desktop"]["verification_status"] == "verified"
    assert drivers["rtx_50_series_desktop"]["gpu_model"] == "NVIDIA GeForce RTX 5070 Ti"
    assert drivers["rtx_50_series_desktop"]["driver_version"] == "610.88"
    assert drivers["rtx_50_series_desktop"]["verification_status"] == "verified"

    api = cap["graphics_api"]
    assert api["api"] == "D3D12"
    assert api["feature_level"] == "12_0"
    assert api["shader_model"] == "6.0"
    assert api["descriptor_model"] == "bounded_descriptor_tables"
    assert api["barrier_model"] == "ordinary_resource_barriers"
    assert api["queue_model"] == {"direct_queue_count": 1, "compute_queue_count": 0, "copy_queue_count": 0}

    req_features = cap["required_features"]
    assert req_features["resource_binding_tier"] == 2
    assert req_features["resource_heap_tier"] == 2

    # Verify C++ runtime capability check header and implementation enforce identical Tier 2 constants
    cap_h = (REPO_ROOT / "src" / "player" / "capability_check.h").read_text(encoding="utf-8")
    cap_cpp = (REPO_ROOT / "src" / "player" / "capability_check.cpp").read_text(encoding="utf-8")
    assert re.search(r"kMinWindowsBuildNumber\s*=\s*22631u", cap_h)
    assert re.search(r"kRequiredResourceBindingTier\s*=\s*2u", cap_h)
    assert re.search(r"kRequiredResourceHeapTier\s*=\s*2u", cap_h)
    assert "BaselineCapabilityProfile::kRequiredResourceBindingTier" in cap_cpp
    assert "BaselineCapabilityProfile::kRequiredResourceHeapTier" in cap_cpp

    excl = cap["excluded_baseline_features"]
    assert excl["sm_6_6_direct_heap_indexing"] is False
    assert excl["enhanced_barriers"] is False
    assert excl["mesh_shaders"] is False
    assert excl["ray_tracing"] is False
    assert excl["runtime_shader_compilation"] is False
    assert excl["mandatory_agility_sdk_redistribution"] is False
    assert excl["proprietary_vendor_sdks"] is False
    assert excl["executable_packers"] is False


def test_hardware_benchmark_coverage_matches_recorded_physical_evidence() -> None:
    rtx4060_path = REPO_ROOT / "benchmarks" / "hardware" / "rtx-4060-baseline.json"
    rtx4060 = json.loads(rtx4060_path.read_text(encoding="utf-8"))
    assert rtx4060["hardware_tier"] == "rtx_4060_desktop"
    assert rtx4060["verification_status"] == "verified"
    assert rtx4060["benchmark_passed"] is True
    assert rtx4060["environment_metadata"]["gpu_model"] == "NVIDIA GeForce RTX 4060 Ti"
    assert rtx4060["environment_metadata"]["os_build"] == "10.0.26300.9818"
    assert rtx4060["environment_metadata"]["nvidia_driver_version"] == "610.88"
    for fx in rtx4060["fixture_results"]:
        assert fx["execution_status"] == "passed"
        assert fx["total_distributed_bytes"] is not None
        assert fx["startup_time_ms"] is not None
        assert fx["cpu_time_ms"] is not None
        assert fx["gpu_time_ms"] is not None
        assert fx["committed_memory_bytes"] is not None

    rtx50_path = REPO_ROOT / "benchmarks" / "hardware" / "rtx-50-series-baseline.json"
    rtx50 = json.loads(rtx50_path.read_text(encoding="utf-8"))
    assert rtx50["hardware_tier"] == "rtx_50_series_desktop"
    assert rtx50["verification_status"] == "verified"
    assert rtx50["benchmark_passed"] is True
    assert rtx50["environment_metadata"]["gpu_model"] == "NVIDIA GeForce RTX 5070 Ti"
    assert rtx50["environment_metadata"]["cpu_model"] is not None
    assert rtx50["artifact_provenance"]["toolchain_id"] == "msvc-ltcg"
    assert rtx50["artifact_provenance"]["source"] == "recorded_ledger_artifact_hash"
    for fx in rtx50["fixture_results"]:
        assert fx["execution_status"] == "passed"
        assert fx["exit_code"] == 0
        assert fx["total_distributed_bytes"] is not None
        assert fx["startup_time_ms"] is not None
        assert fx["cpu_time_ms"] is not None
        assert fx["gpu_time_ms"] is not None
        assert fx["committed_memory_bytes"] is not None

    clean = json.loads(
        (
            REPO_ROOT / "benchmarks" / "clean-system" / "clean-system-validation.json"
        ).read_text(encoding="utf-8")
    )
    assert clean["verification_status"] == "verified"
    assert clean["clean_system_passed"] is True
    assert all(value is True for value in clean["sdk_absence_checks"].values())
    assert clean["deployed_dependency_inventory"]["inbox_dlls_resolved"]
    assert clean["system_environment"]["os_build"] == "10.0.26200.9711"
    assert clean["system_environment"]["gpu_model"] == "NVIDIA GeForce RTX 5070 Ti"
    assert clean["system_environment"]["nvidia_driver_version"] == "610.88"


def test_schema_rejects_unverified_benchmark_claiming_pass_or_zero_metrics() -> None:
    hw_path = REPO_ROOT / "benchmarks" / "hardware" / "rtx-4060-baseline.json"
    schema_path = REPO_ROOT / "schemas" / "benchmark-result.schema.json"
    hw = json.loads(hw_path.read_text(encoding="utf-8"))
    hw["verification_status"] = "unverified"
    hw["benchmark_passed"] = False
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # Case 1: unverified hardware must never have benchmark_passed=True
    bad_pass = copy.deepcopy(hw)
    bad_pass["benchmark_passed"] = True
    with pytest.raises(SchemaValidationError, match="benchmark_passed=false"):
        validate_semantic_invariants(bad_pass, schema_path.name, hw_path)

    # Case 2: unavailable metric recorded as 0 instead of null must fail schema exclusiveMinimum / semantic check
    bad_zero = copy.deepcopy(hw)
    bad_zero["fixture_results"][0]["startup_time_ms"] = 0
    with pytest.raises(SchemaValidationError):
        validate_instance(bad_zero, schema, schema)

    # Case 3: unverified reproducibility report claiming all_artifacts_match=True must fail semantic check
    repro_path = REPO_ROOT / "benchmarks" / "reproducibility" / "release-build-reproducibility.json"
    repro = json.loads(repro_path.read_text(encoding="utf-8"))
    bad_repro = copy.deepcopy(repro)
    bad_repro["verification_status"] = "unverified"
    bad_repro["all_artifacts_match"] = True
    with pytest.raises(SchemaValidationError, match="all_artifacts_match=false"):
        validate_semantic_invariants(bad_repro, "reproducibility-report.schema.json", repro_path)


def test_verified_benchmark_requires_recorded_cpu_metadata_without_family_whitelist() -> None:
    hw_path = REPO_ROOT / "benchmarks" / "hardware" / "rtx-50-series-baseline.json"
    hw = json.loads(hw_path.read_text(encoding="utf-8"))
    hw["verification_status"] = "verified"
    hw["benchmark_passed"] = True
    hw["environment_metadata"].update(
        gpu_model="NVIDIA GeForce RTX 5070 Ti",
        gpu_architecture="Blackwell",
        gpu_vendor_id="0x10DE",
        gpu_device_id="0x2C05",
        vram_mb=16384,
        cpu_model="AMD Ryzen Threadripper PRO 7995WX 96-Core Processor",
        os_build="10.0.26200.9711",
        nvidia_driver_version="610.88",
        wddm_driver_version="32.0.16.1088",
    )
    hw["environment_metadata"]["toolchain"].update(
        toolchain_id="clang-cl-lld",
        compiler_name="LLVM clang-cl",
        compiler_executable="clang-cl.exe",
        compiler_version="18.1.8",
        linker_name="LLVM lld-link",
        linker_executable="lld-link.exe",
        linker_version="18.1.8",
        windows_sdk_version="10.0.26100.0",
        dxc_version="1.8.2407",
    )
    hw["environment_metadata"]["power_settings"].update(
        windows_power_plan="High performance",
        nvidia_power_management_mode="Prefer maximum performance",
        pci_express_link_state_power_management="Off",
    )
    hw["environment_metadata"]["resolution"].update(
        width=1280, height=720, refresh_hz=60
    )
    hw["environment_metadata"]["cache_conditions"].update(
        driver_shader_cache_state="warm_primed",
        os_file_cache_state="warm",
    )
    hw["artifact_provenance"] = {
        "toolchain_id": "clang-cl-lld",
        "artifact_sha256": {},
        "source": "recorded_ledger_artifact_hash",
    }
    validate_semantic_invariants(hw, "benchmark-result.schema.json", hw_path)

    hw["environment_metadata"]["cpu_model"] = None
    with pytest.raises(SchemaValidationError, match="environment_metadata.cpu_model"):
        validate_semantic_invariants(
            hw, "benchmark-result.schema.json", hw_path
        )


def test_schema_rejects_premature_64kib_claim_in_size_ledger() -> None:
    ledger_path = REPO_ROOT / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json"
    schema_path = REPO_ROOT / "schemas" / "size-ledger.schema.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    bad_ledger = copy.deepcopy(ledger)
    bad_ledger["budget_claim_policy"]["claims_64kib_achievable"] = True
    with pytest.raises(SchemaValidationError, match="claims_64kib_achievable"):
        validate_instance(bad_ledger, schema, schema)


def test_unverified_toolchain_cannot_claim_pdb_or_reproducibility_evidence() -> None:
    manifest_path = REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # An unverified toolchain must never retain a PDB success flag after a partial
    # evidence gate: otherwise bytes/status and the evidence flags contradict each other.
    bad = copy.deepcopy(manifest)
    entry = bad["evaluated_toolchains"][0]
    entry["verification_status"] = "unverified"
    for field in ("empty_player_bytes", "clear_present_bytes", "compute_fixture_bytes"):
        entry[field] = None
    entry["pdb_debugging_verified"] = True
    entry["deterministic_repro_verified"] = False
    with pytest.raises(SchemaValidationError, match="pdb_debugging_verified=true"):
        validate_semantic_invariants(
            bad, "toolchain-manifest.schema.json", manifest_path
        )

    bad_repro = copy.deepcopy(manifest)
    entry = bad_repro["evaluated_toolchains"][0]
    entry["verification_status"] = "unverified"
    for field in ("empty_player_bytes", "clear_present_bytes", "compute_fixture_bytes"):
        entry[field] = None
    entry["pdb_debugging_verified"] = False
    entry["deterministic_repro_verified"] = True
    with pytest.raises(SchemaValidationError, match="deterministic_repro_verified=true"):
        validate_semantic_invariants(
            bad_repro, "toolchain-manifest.schema.json", manifest_path
        )


def test_verified_clean_report_requires_real_observed_readback_hash() -> None:
    good_hash = "a" * 64
    report = {
        "verification_status": "verified",
        "clean_system_passed": True,
        "system_environment": {
            "os_name": "Windows 11 x64",
            "os_build": "10.0.26100.3194",
            "architecture": "x86_64",
            "gpu_model": "NVIDIA GeForce RTX 5080",
            "nvidia_driver_version": "572.16",
        },
        "deployed_dependency_inventory": {
            "deployed_files": [{"size_bytes": 1024}],
            "total_distributed_bytes": 1024,
            "unexpected_sidecars_found": [],
        },
        "sdk_absence_checks": {
            "windows_sdk_absent": True,
            "d3d12_debug_layer_absent": True,
            "dxc_redist_absent": True,
            "agility_sdk_redist_absent": True,
            "msvc_debug_crt_absent": True,
        },
        "offline_shader_execution_evidence": {
            "compiled_offline_with_dxc": True,
            "runtime_dxcompiler_loaded": False,
            "runtime_d3dcompiler_loaded": False,
            "agility_d3d12core_redist_loaded": False,
            "expected_output_sha256": good_hash,
            "observed_output_sha256": "b" * 64,
            "output_verified": True,
        },
        "fixture_runs": [{"execution_status": "passed", "exit_code": 0}],
    }
    with pytest.raises(SchemaValidationError, match="observed_output_sha256"):
        validate_semantic_invariants(
            report, "clean-system-report.schema.json", REPO_ROOT / "clean.json"
        )
