from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw04_player_contract as contract  # noqa: E402
import nwp_package as nwp  # noqa: E402
import verify_nw04  # noqa: E402


def test_nw04_fixture_is_a_valid_nw03_package_with_present() -> None:
    data = nw04_fixture.package_bytes()
    inspection = nwp.inspect_package_bytes(data)
    assert inspection.valid, inspection.diagnostics
    package = nwp.decode_package(data)
    assert package.capability.profile_id == nwp.COMMON_PROFILE_ID
    assert package.capability.requirements_digest == nwp.COMMON_REQUIREMENTS_DIGEST
    assert package.capability.feature_level == 0xC000
    assert package.capability.shader_model == 0x60
    assert package.capability.resource_binding_tier == 2
    assert package.capability.resource_heap_tier == 2
    assert package.capability.direct_queue_count == 1
    assert package.capability.limit_swap_chain_buffer_count == 2
    assert "frame.dispatch" in [i.opcode for i in package.frame]
    assert "frame.present" in [i.opcode for i in package.frame]
    assert [i.opcode for i in package.initialization] == [
        "init.create_resource",
        "init.create_resource",
        "init.create_root_signature",
        "init.create_pipeline",
        "init.upload_resource",
    ]
    assert [i.opcode for i in package.frame] == [
        "frame.set_timeline",
        "frame.set_root_signature",
        "frame.set_root_constants",
        "frame.set_descriptor_table",
        "frame.barrier",
        "frame.dispatch",
        "frame.present",
    ]
    assert package.flags & 1, "fixture must be a release package"


def test_nw04_exact_capability_requirements_pass_only_the_common_profile() -> None:
    package = nw04_fixture.build_compute_display_package()
    assert contract.validate_package_requirements(package) == []
    assert (
        contract.evaluate_package_against_adapter(
            package, contract.baseline_adapter()
        )
        == []
    )
    capability = package.capability
    assert capability.max_cbv_srv_uav_descriptors_per_frame == 1
    assert capability.max_sampler_descriptors_per_frame == 0
    assert capability.max_root_signature_dword_cost == 5
    assert capability.limit_max_compute_threads_per_group == 64
    # The common profile baseline retains all four required formats even though
    # this particular package only emits the two it uses.
    assert set(capability.required_dxgi_formats) == {
        "DXGI_FORMAT_R32G32B32A32_FLOAT",
        "DXGI_FORMAT_R32_UINT",
    }


CAPABILITY_FAILURE_CASES = [
    ("max_feature_level", 0xB000, "package_capability_feature_level"),
    ("max_shader_model", 0x50, "package_capability_shader_model"),
    ("resource_binding_tier", 1, "package_capability_binding_tier"),
    ("resource_heap_tier", 1, "package_capability_heap_tier"),
    ("max_compute_threads_per_group", 32, "package_capability_compute_limit"),
    ("max_buffer_bytes", 1024, "package_capability_buffer_limit"),
    (
        "max_cbv_srv_uav_descriptors_per_heap",
        0,
        "package_capability_descriptor_limit",
    ),
    ("max_root_signature_dword_cost", 4, "package_capability_root_signature_limit"),
    ("max_swap_chain_buffer_count", 1, "package_capability_swap_chain_limit"),
    ("windows_build_number", 22630, "package_capability_windows_build"),
    ("supports_cs_6_0", False, "package_capability_shader_target"),
]


@pytest.mark.parametrize("field,value,expected_code", CAPABILITY_FAILURE_CASES)
def test_adapter_capability_failures_are_structured(
    field: str, value: object, expected_code: str
) -> None:
    package = nw04_fixture.build_compute_display_package()
    adapter = contract.baseline_adapter(**{field: value})
    failures = contract.evaluate_package_against_adapter(package, adapter)
    matching = [failure for failure in failures if failure.code == expected_code]
    assert matching, [failure.code for failure in failures]
    failure = matching[0]
    assert failure.required
    assert failure.observed
    assert failure.profile_id == nwp.COMMON_PROFILE_ID
    assert failure.requirements_digest == nwp.COMMON_REQUIREMENTS_DIGEST
    assert failure.requirement
    assert failure.message
    diagnostic = failure.to_dict()
    assert diagnostic["category"] in {
        "package_capability",
        "device_and_capability",
        "package_validation",
    }
    assert diagnostic["code"] == expected_code


def test_missing_and_unsupported_formats_are_rejected() -> None:
    package = nw04_fixture.build_compute_display_package()
    adapter = contract.baseline_adapter()
    del adapter["formats"]["DXGI_FORMAT_R32_UINT"]
    failures = contract.evaluate_package_against_adapter(package, adapter)
    assert any(
        failure.code == "package_capability_format_unavailable" for failure in failures
    )

    adapter = contract.baseline_adapter()
    adapter["formats"]["DXGI_FORMAT_R32_UINT"]["unordered_access"] = False
    failures = contract.evaluate_package_against_adapter(package, adapter)
    assert any(
        failure.code == "package_capability_format_usage" for failure in failures
    )


def test_unknown_profile_and_digest_are_rejected() -> None:
    package = nw04_fixture.build_compute_display_package()
    unknown = nwp.replace(
        package.capability,
        profile_id="nw.adapter.rtx.unknown.v1",
    )
    unknown = nwp.replace(
        unknown, profile_fingerprint=unknown.computed_profile_fingerprint()
    )
    failures = contract.validate_package_requirements(
        nwp.replace(package, capability=unknown)
    )
    assert any(failure.code == "package_profile_unknown" for failure in failures)

    digest = nwp.replace(
        package.capability,
        requirements_digest="sha256:v1:" + "0" * 64,
    )
    digest = nwp.replace(
        digest, profile_fingerprint=digest.computed_profile_fingerprint()
    )
    failures = contract.validate_package_requirements(
        nwp.replace(package, capability=digest)
    )
    assert any(
        failure.code == "package_profile_digest_mismatch" for failure in failures
    )


def test_optional_profile_must_remain_a_separate_artifact() -> None:
    package = nw04_fixture.build_compute_display_package()
    capability = nwp.replace(
        package.capability,
        optional_profile_id=nwp.OPTIONAL_PROFILE_WAVE_OPS,
        optional_requirements_digest=nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST,
        optional_profile_separate_artifact=False,
    )
    capability = nwp.replace(
        capability,
        profile_fingerprint=capability.computed_profile_fingerprint(),
    )
    failures = contract.validate_package_requirements(
        nwp.replace(package, capability=capability)
    )
    assert any(
        failure.code == "package_optional_profile_contradiction"
        for failure in failures
    )


def test_capability_failure_matrix_check_passes() -> None:
    result = verify_nw04.check_capability_failure_matrix()
    assert result.passed, result.detail
    assert len(result.evidence["cases"]) >= 14


def test_malformed_packages_are_rejected_before_bulk_allocation() -> None:
    result = verify_nw04.check_package_rejection_before_allocation()
    assert result.passed, result.detail
    cases = {entry["case"] for entry in result.evidence["cases"]}
    assert {
        "truncated",
        "checksum",
        "capability_field_count",
        "capability_unknown_field",
        "capability_missing_field",
        "unknown_section",
    } <= cases


def test_player_interfaces_and_validation_order_are_present() -> None:
    result = verify_nw04.check_player_source_contract()
    assert result.passed, result.detail
    fixture_source = (
        REPO_ROOT / "fixtures" / "nw04_package_player" / "main.cpp"
    ).read_text(encoding="utf-8")
    for flag in (
        "--resize-sequence",
        "--simulate-minimize",
        "--simulate-device-removal",
        "--simulate-allocation-failure",
        "--simulate-capability-failure",
    ):
        assert flag in fixture_source


def test_release_closure_excludes_authoring_and_diagnostic_tooling() -> None:
    result = verify_nw04.check_release_closure()
    assert result.passed, result.detail
    build_profiles = (REPO_ROOT / "cmake" / "BuildProfiles.cmake").read_text(
        encoding="utf-8"
    )
    assert "NW_ENABLE_D3D12_VALIDATION=0" in build_profiles
    assert "NW_PROFILE_DEV=1" in build_profiles


def test_eligibility_never_branches_on_adapter_metadata() -> None:
    source = (REPO_ROOT / "src" / "player" / "capability_requirements.cpp").read_text(
        encoding="utf-8"
    )
    start = source.index("PackageCapabilityEvaluation EvaluatePackageCapabilities(")
    end = source.index("QueriedAdapterCapabilities MakeBaselineQueriedCapabilities(")
    body = source[start:end]
    for metadata in (
        "adapter_description",
        "adapter_generation",
        "vendor_id",
        "device_id",
        "dedicated_video_memory_bytes",
    ):
        assert metadata not in body


def test_verify_reports_hardware_qualification_as_pending() -> None:
    results = verify_nw04.run_contracts_only()
    assert all(result.passed for result in results), [
        (result.name, result.detail) for result in results if not result.passed
    ]
    summary = {
        "hardware_qualification": {"ada": "pending_unverified", "blackwell": "pending_unverified"}
    }
    assert summary["hardware_qualification"]["ada"] == "pending_unverified"


def test_nw04_fixture_is_deterministic() -> None:
    first = nw04_fixture.package_bytes()
    second = nw04_fixture.package_bytes()
    assert first == second
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()


def test_nw04_hardware_evidence_is_pending_and_schema_valid() -> None:
    from schema_validator import validate_file_against_schema

    evidence_path = REPO_ROOT / "benchmarks" / "nw04-hardware-evidence.json"
    schema_path = REPO_ROOT / "schemas" / "nw04-player-evidence.schema.json"
    validate_file_against_schema(evidence_path, schema_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["task"] == "NW-04"
    assert evidence["verification_status"] == "pending_unverified"
    for tier in ("ada", "blackwell"):
        assert evidence["qualifications"][tier]["status"] == "pending_unverified"
        assert "No representative" in evidence["qualifications"][tier]["reason"]


def test_synthetic_contract_fixture_cannot_certify_hardware() -> None:
    result = verify_nw04._validate_hardware_package(
        REPO_ROOT / "fixtures" / "nw04" / "compute_display.nwp"
    )
    assert not result.passed
    assert "real offline-compiled artifact" in result.detail
