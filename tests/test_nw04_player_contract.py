from __future__ import annotations

import hashlib
import json
import os
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


def test_host_simulation_planner_accepts_fixture_and_updates_constants() -> None:
    package = nw04_fixture.build_compute_display_package()
    assert contract.validate_frame_plan(package) == []
    first, _ = contract.apply_frame_constants(package, 0)
    last, timeline = contract.apply_frame_constants(package, 7)
    assert first is not None and last is not None
    assert first[:4] != last[:4]
    assert struct.unpack_from("<f", last, 0)[0] == 7.0
    assert timeline == 7


def test_zero_count_root_constant_block_does_not_erase_last_constants() -> None:
    package = nw04_fixture.build_compute_display_package()
    zero_block = nwp.Instruction(
        "frame.set_root_constants",
        (
            nwp.InstructionOperand("u32", 0),
            nwp.InstructionOperand("resource_ref", 1),
            nwp.InstructionOperand("u32", 0),
        ),
    )
    package = nwp.replace(package, frame=package.frame + [zero_block])
    assert contract.validate_frame_plan(package) == []
    first, _ = contract.apply_frame_constants(package, 0)
    last, _ = contract.apply_frame_constants(package, 7)
    assert first
    assert last
    assert first[:4] != last[:4]
    assert struct.unpack_from("<f", last, 0)[0] == 7.0


def test_planner_rejects_bad_barrier_heap_and_descriptor_targets() -> None:
    package = nw04_fixture.build_compute_display_package()

    def rewrite(opcode: str, operand_index: int, value: object) -> nwp.NwpPackage:
        frame = []
        for instruction in package.frame:
            if instruction.opcode == opcode:
                operands = list(instruction.operands)
                operands[operand_index] = nwp.InstructionOperand(
                    operands[operand_index].type, value
                )
                instruction = nwp.Instruction(opcode, tuple(operands))
            frame.append(instruction)
        return nwp.replace(package, frame=tuple(frame))

    failures = contract.validate_frame_plan(rewrite("frame.barrier", 0, 99))
    assert any(
        failure.code == "package_instruction_barrier_state"
        for failure in failures
    )
    failures = contract.validate_frame_plan(rewrite("frame.barrier", 1, 1))
    assert any(
        failure.code == "package_instruction_barrier_usage"
        for failure in failures
    )
    failures = contract.validate_frame_plan(
        rewrite("frame.set_descriptor_table", 1, "unexpected_heap")
    )
    assert any(
        failure.code == "package_instruction_descriptor_heap"
        for failure in failures
    )
    failures = contract.validate_frame_plan(
        rewrite("frame.set_descriptor_table", 2, 1)
    )
    assert any(
        failure.code == "package_instruction_descriptor_target"
        for failure in failures
    )


def test_planner_rejects_unsupported_and_unbound_timeline_parameters() -> None:
    package = nw04_fixture.build_compute_display_package()
    bad_type = nwp.replace(
        package.parameters[0],
        type="double",
    )
    failures = contract.validate_frame_plan(
        nwp.replace(package, parameters=(bad_type,))
    )
    assert any(
        failure.code == "package_parameter_type_unsupported"
        for failure in failures
    )

    extra = nwp.ParameterRecord(
        parameter_id=2,
        name="seed",
        type="uint",
        phase="frame",
        component_count=1,
        value=struct.pack("<I", 0),
    )
    frame = []
    for instruction in package.frame:
        if instruction.opcode == "frame.set_timeline":
            instruction = nwp.Instruction(
                "frame.set_timeline",
                (
                    nwp.InstructionOperand("u32", 2),
                    nwp.InstructionOperand("u64", 0),
                ),
            )
        elif instruction.opcode == "frame.set_root_constants":
            instruction = nwp.Instruction(
                "frame.set_root_constants",
                (
                    nwp.InstructionOperand("u32", 0),
                    nwp.InstructionOperand("resource_ref", 1),
                    nwp.InstructionOperand("u32", 1),
                ),
            )
        frame.append(instruction)
    failures = contract.validate_frame_plan(
        nwp.replace(
            package,
            parameters=tuple(package.parameters) + (extra,),
            frame=tuple(frame),
        )
    )
    assert any(
        failure.code == "frame_timeline_parameter_not_bound"
        for failure in failures
    )


def test_planner_requires_timeline_before_a_matching_root_constant_upload() -> None:
    package = nw04_fixture.build_compute_display_package()
    timeline = next(
        instruction
        for instruction in package.frame
        if instruction.opcode == "frame.set_timeline"
    )
    frame = [
        instruction
        for instruction in package.frame
        if instruction.opcode != "frame.set_timeline"
    ]
    root_index = next(
        index
        for index, instruction in enumerate(frame)
        if instruction.opcode == "frame.set_root_constants"
    )
    frame.insert(root_index + 1, timeline)
    failures = contract.validate_frame_plan(
        nwp.replace(package, frame=tuple(frame))
    )
    assert any(
        failure.code == "frame_timeline_parameter_not_bound"
        for failure in failures
    )


def test_timeline_mirror_saturates_u64_like_native_player() -> None:
    package = nw04_fixture.build_compute_display_package()
    max_u64 = (1 << 64) - 1
    frame = []
    for instruction in package.frame:
        if instruction.opcode == "frame.set_timeline":
            instruction = nwp.Instruction(
                "frame.set_timeline",
                (
                    instruction.operands[0],
                    nwp.InstructionOperand("u64", max_u64 - 2),
                ),
            )
        frame.append(instruction)
    _, timeline = contract.apply_frame_constants(
        nwp.replace(package, frame=tuple(frame)), 7
    )
    assert timeline == max_u64


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


@pytest.mark.parametrize(
    "queried_key",
    (
        "feature_level_queried",
        "shader_model_queried",
        "binding_tier_queried",
        "heap_tier_queried",
        "windows_build_queried",
        "limits_queried",
    ),
)
def test_contract_mirrors_per_field_queried_flags(queried_key: str) -> None:
    package = nw04_fixture.build_compute_display_package()
    adapter = contract.baseline_adapter(**{queried_key: False})
    failures = contract.evaluate_package_against_adapter(package, adapter)
    assert any(
        failure.code == "package_capability_query_unavailable"
        for failure in failures
    )


def test_static_and_adapter_categories_match_native_diagnostics() -> None:
    package = nw04_fixture.build_compute_display_package()
    capability = nwp.replace(package.capability, feature_level=0xB000)
    capability = nwp.replace(
        capability,
        profile_fingerprint=capability.computed_profile_fingerprint(),
    )
    static_failures = contract.validate_package_requirements(
        nwp.replace(package, capability=capability)
    )
    static = [
        failure
        for failure in static_failures
        if failure.code == "package_capability_feature_level"
    ]
    assert static and static[0].category == "package_validation"

    adapter_failures = contract.evaluate_package_against_adapter(
        package, contract.baseline_adapter(max_feature_level=0xB000)
    )
    adapter = [
        failure
        for failure in adapter_failures
        if failure.code == "package_capability_feature_level"
    ]
    assert adapter and adapter[0].category == "device_and_capability"


def test_duplicate_requirements_and_toolchain_identity_are_rejected() -> None:
    package = nw04_fixture.build_compute_display_package()
    capability = package.capability
    formats = tuple(capability.required_dxgi_formats)
    duplicate_formats = nwp.replace(
        capability,
        required_dxgi_formats=(formats[0], formats[0]),
    )
    duplicate_formats = nwp.replace(
        duplicate_formats,
        profile_fingerprint=duplicate_formats.computed_profile_fingerprint(),
    )
    rules = contract.validate_package_requirements(
        nwp.replace(package, capability=duplicate_formats)
    )
    assert any(
        failure.code == "package_capability_contradictory"
        and failure.requirement == "required_dxgi_formats"
        for failure in rules
    )

    init_ops = tuple(capability.required_init_instructions)
    duplicate_instructions = nwp.replace(
        capability,
        required_init_instructions=(init_ops[0], init_ops[0]),
    )
    duplicate_instructions = nwp.replace(
        duplicate_instructions,
        profile_fingerprint=duplicate_instructions.computed_profile_fingerprint(),
    )
    rules = contract.validate_package_requirements(
        nwp.replace(package, capability=duplicate_instructions)
    )
    assert any(
        failure.code == "package_capability_contradictory"
        and failure.requirement == "required_init_instructions"
        for failure in rules
    )

    duplicate_targets = nwp.replace(
        capability,
        emitted_shader_targets=("cs_6_0", "cs_6_0"),
    )
    duplicate_targets = nwp.replace(
        duplicate_targets,
        profile_fingerprint=duplicate_targets.computed_profile_fingerprint(),
    )
    rules = contract.validate_package_requirements(
        nwp.replace(package, capability=duplicate_targets)
    )
    assert any(
        failure.code == "package_capability_contradictory"
        and failure.requirement == "emitted_shader_targets"
        for failure in rules
    )

    toolchain = nwp.replace(
        capability,
        toolchain_dxc_version="9.9.9999",
        toolchain_identity="dxc:9.9.9999",
        compiler_fingerprint="",
    )
    toolchain = nwp.replace(
        toolchain, profile_fingerprint=toolchain.computed_profile_fingerprint()
    )
    rules = contract.validate_package_requirements(
        nwp.replace(package, capability=toolchain)
    )
    codes = {failure.code for failure in rules}
    assert "package_toolchain_identity" in codes
    assert "package_capability_contradictory" in codes


def test_optional_digest_without_profile_is_rejected() -> None:
    package = nw04_fixture.build_compute_display_package()
    capability = nwp.replace(
        package.capability,
        optional_profile_id="",
        optional_requirements_digest=nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST,
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
        and failure.requirement == "optional_requirements_digest"
        for failure in failures
    )


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
        "--simulate-pipeline-failure",
        "--simulate-root-signature-failure",
        "--simulate-frame-allocation",
        "--planner-self-test",
        "--planner-mutation",
    ):
        assert flag in fixture_source
    source = (REPO_ROOT / "src" / "player" / "package_player.cpp").read_text(
        encoding="utf-8"
    )
    assert "SetForcePipelineCreationFailureForTesting" in source
    assert "simulated_pipeline_failure" in source
    assert "frame.upload" in source
    assert "TryBarrierStateFromCode" in source
    assert "allocation_calls_before_frame" in source


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


def test_verify_summary_is_scoped_to_the_selected_mode() -> None:
    passed = verify_nw04.CheckResult("demo", True, True)
    summary = verify_nw04.build_summary("ContractsOnly", [passed])
    assert summary["verification_status"] == "contracts_only_verified"
    assert summary["overall_status"] == "pending_unverified"
    assert summary["hardware_qualification"]["ada"] == "pending_unverified"
    assert summary["hardware_qualification"]["blackwell"] == "pending_unverified"


def test_native_host_simulation_planner_when_fixture_available() -> None:
    candidates = []
    override = os.environ.get("NW04_FIXTURE")
    if override:
        candidates.append(Path(override))
    candidates.extend(
        sorted((REPO_ROOT / "build").glob("**/nw_package_player_fixture*"))
    )
    fixture = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file()
            and candidate.suffix.lower() in ("", ".exe")
            and "CMakeFiles" not in candidate.parts
        ),
        None,
    )
    if fixture is None:
        pytest.skip("native NW-04 fixture is not built on this host")
    result = verify_nw04.run_planner_self_test(
        fixture, REPO_ROOT / "fixtures" / "nw04" / "compute_display.nwp"
    )
    assert result.passed, result.detail


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
