from __future__ import annotations

import copy
import json
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
    assert summary["validated_count"] == 16
    assert all(item["valid"] for item in summary["results"])


def test_capability_manifest_baseline_and_exclusions() -> None:
    cap = json.loads((REPO_ROOT / "benchmarks" / "capability-manifest.v1.json").read_text(encoding="utf-8"))
    assert cap["target_platform"]["os"] == "windows_11_x64"
    assert cap["target_platform"]["minimum_tested_windows_build"] == "10.0.22631.4317"
    assert "windows_10" in cap["target_platform"]["unsupported_platforms"]
    assert "linux" in cap["target_platform"]["unsupported_platforms"]
    assert "macos" in cap["target_platform"]["unsupported_platforms"]

    drivers = {d["hardware_tier"]: d for d in cap["target_platform"]["tested_nvidia_drivers"]}
    assert set(drivers.keys()) == {"rtx_4060_desktop", "rtx_50_series_desktop"}
    assert drivers["rtx_4060_desktop"]["gpu_model"] == "NVIDIA GeForce RTX 4060"
    assert drivers["rtx_4060_desktop"]["driver_version"] == "566.03"
    assert drivers["rtx_4060_desktop"]["verification_status"] == "unverified"
    assert drivers["rtx_50_series_desktop"]["gpu_model"] == "NVIDIA GeForce RTX 5080"
    assert drivers["rtx_50_series_desktop"]["driver_version"] == "572.16"
    assert drivers["rtx_50_series_desktop"]["verification_status"] == "unverified"

    api = cap["graphics_api"]
    assert api["api"] == "D3D12"
    assert api["feature_level"] == "12_0"
    assert api["shader_model"] == "6.0"
    assert api["descriptor_model"] == "bounded_descriptor_tables"
    assert api["barrier_model"] == "ordinary_resource_barriers"
    assert api["queue_model"] == {"direct_queue_count": 1, "compute_queue_count": 0, "copy_queue_count": 0}

    excl = cap["excluded_baseline_features"]
    assert excl["sm_6_6_direct_heap_indexing"] is False
    assert excl["enhanced_barriers"] is False
    assert excl["mesh_shaders"] is False
    assert excl["ray_tracing"] is False
    assert excl["runtime_shader_compilation"] is False
    assert excl["mandatory_agility_sdk_redistribution"] is False
    assert excl["proprietary_vendor_sdks"] is False
    assert excl["executable_packers"] is False


def test_hardware_benchmarks_mark_missing_coverage_unverified_and_not_passed() -> None:
    for filename, expected_tier in (
        ("rtx-4060-baseline.json", "rtx_4060_desktop"),
        ("rtx-50-series-baseline.json", "rtx_50_series_desktop"),
    ):
        hw_path = REPO_ROOT / "benchmarks" / "hardware" / filename
        hw = json.loads(hw_path.read_text(encoding="utf-8"))
        assert hw["hardware_tier"] == expected_tier
        assert hw["verification_status"] == "unverified"
        assert hw["benchmark_passed"] is False
        for fx in hw["fixture_results"]:
            assert fx["execution_status"] == "unverified"
            assert fx["startup_time_ms"] is None
            assert fx["cpu_time_ms"] is None
            assert fx["gpu_time_ms"] is None
            assert fx["committed_memory_bytes"] is None


def test_schema_rejects_unverified_benchmark_claiming_pass_or_zero_metrics() -> None:
    hw_path = REPO_ROOT / "benchmarks" / "hardware" / "rtx-4060-baseline.json"
    schema_path = REPO_ROOT / "schemas" / "benchmark-result.schema.json"
    hw = json.loads(hw_path.read_text(encoding="utf-8"))
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


def test_schema_rejects_premature_64kib_claim_in_size_ledger() -> None:
    ledger_path = REPO_ROOT / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json"
    schema_path = REPO_ROOT / "schemas" / "size-ledger.schema.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    bad_ledger = copy.deepcopy(ledger)
    bad_ledger["budget_claim_policy"]["claims_64kib_achievable"] = True
    with pytest.raises(SchemaValidationError, match="claims_64kib_achievable"):
        validate_instance(bad_ledger, schema, schema)
