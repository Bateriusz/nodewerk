#!/usr/bin/env python3
"""NW-04 verification entry point.

Usage:
    python tools/verify_nw04.py --mode ContractsOnly [--summary-json out.json]
    python tools/verify_nw04.py --mode NativeLifecycle --fixture path/to/player.exe
    python tools/verify_nw04.py --mode Full [--fixture path/to/player.exe]
    python tools/verify_nw04.py --mode Hardware --fixture path/to/player.exe \
        --record benchmarks/nw04-hardware-evidence.json

ContractsOnly is GPU-less and host-neutral. NativeLifecycle exercises the
Windows package/player lifecycle without enabling the D3D12 debug layer and
does not certify validation or hardware qualification. Full and Hardware are
Windows validation modes; when a required native artifact or adapter is
unavailable they report `unavailable` and exit nonzero instead of claiming
success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw04_player_contract as contract  # noqa: E402
import nwp_package as nwp  # noqa: E402


@dataclass
class CheckResult:
    name: str
    required: bool
    passed: bool
    detail: str = ""
    unavailable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "passed": self.passed,
            "unavailable": self.unavailable,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def _recompute_checksum(data: bytes) -> bytes:
    body = bytearray(data)
    digest = hashlib.sha256(bytes(body[:-32])).digest()
    body[-32:] = digest
    return bytes(body)


def _section_offset(data: bytes, section_id: bytes) -> int:
    section_count = struct.unpack_from("<I", data, 40)[0]
    for index in range(section_count):
        entry = 64 + index * 32
        if data[entry : entry + 4] == section_id:
            return struct.unpack_from("<Q", data, entry + 12)[0]
    raise AssertionError(f"section {section_id!r} not found")


def check_fixture_roundtrip() -> CheckResult:
    try:
        data = nw04_fixture.package_bytes()
        package = nwp.decode_package(data)
        inspection = nwp.inspect_package_bytes(data)
        if not inspection.valid:
            return CheckResult(
                "nw04_fixture_roundtrip",
                True,
                False,
                "fixture package is not valid",
                evidence={"diagnostics": list(inspection.diagnostics)},
            )
        leakage = nwp.check_release_leakage(data)
        if leakage:
            return CheckResult(
                "nw04_fixture_roundtrip",
                True,
                False,
                f"release fixture contains authoring metadata: {leakage}",
            )
        frame = [instruction.opcode for instruction in package.frame]
        if "frame.present" not in frame or "frame.dispatch" not in frame:
            return CheckResult(
                "nw04_fixture_roundtrip",
                True,
                False,
                "fixture frame instructions are incomplete",
                evidence={"frame": frame},
            )
        return CheckResult(
            "nw04_fixture_roundtrip",
            True,
            True,
            evidence={
                "package_sha256": hashlib.sha256(data).hexdigest(),
                "package_size_bytes": len(data),
                "frame": frame,
                "release_leakage": leakage,
            },
        )
    except Exception as exc:  # defensive: verification must report failure
        return CheckResult("nw04_fixture_roundtrip", True, False, str(exc))


def check_exact_capability_contract() -> CheckResult:
    package = nw04_fixture.build_compute_display_package()
    failures = contract.validate_package_requirements(package)
    if failures:
        return CheckResult(
            "nw04_exact_capability_contract",
            True,
            False,
            contract.format_failures(failures),
        )
    capability = package.capability
    if capability.profile_id != nwp.COMMON_PROFILE_ID:
        return CheckResult(
            "nw04_exact_capability_contract", True, False, "profile id mismatch"
        )
    if capability.requirements_digest != nwp.COMMON_REQUIREMENTS_DIGEST:
        return CheckResult(
            "nw04_exact_capability_contract",
            True,
            False,
            "profile digest mismatch",
        )
    if (
        capability.feature_level != 0xC000
        or capability.shader_model != 0x60
        or capability.resource_binding_tier != 2
        or capability.resource_heap_tier != 2
        or capability.direct_queue_count != 1
        or capability.limit_max_compute_threads_per_group != 64
        or capability.max_root_signature_dword_cost != 5
        or capability.limit_swap_chain_buffer_count != 2
    ):
        return CheckResult(
            "nw04_exact_capability_contract",
            True,
            False,
            "fixture capability values differ from the emitted contract",
        )
    return CheckResult(
        "nw04_exact_capability_contract",
        True,
        True,
        evidence={"capability": capability.to_dict()},
    )


def check_fixture_planner_contract() -> CheckResult:
    package = nw04_fixture.build_compute_display_package()
    failures = contract.validate_frame_plan(package)
    if failures:
        return CheckResult(
            "nw04_fixture_planner_contract",
            True,
            False,
            contract.format_failures(failures),
        )
    first, _ = contract.apply_frame_constants(package, 0)
    last, timeline = contract.apply_frame_constants(package, 7)
    if first is None or last is None:
        return CheckResult(
            "nw04_fixture_planner_contract",
            True,
            False,
            "fixture does not emit a root-constant block",
        )
    if first[:4] == last[:4]:
        return CheckResult(
            "nw04_fixture_planner_contract",
            True,
            False,
            "frame.set_timeline did not change the emitted constant bytes",
            evidence={"first": first[:16].hex(), "last": last[:16].hex()},
        )
    if timeline is None:
        return CheckResult(
            "nw04_fixture_planner_contract",
            True,
            False,
            "fixture does not exercise frame.set_timeline",
        )
    return CheckResult(
        "nw04_fixture_planner_contract",
        True,
        True,
        evidence={
            "first_constants": first[:16].hex(),
            "last_constants": last[:16].hex(),
            "timeline_value": timeline,
        },
    )


def _assert_failure(
    name: str,
    mutate: Callable[[dict[str, Any]], None],
    expected_code: str,
) -> CheckResult:
    package = nw04_fixture.build_compute_display_package()
    adapter = contract.baseline_adapter()
    mutate(adapter)
    failures = contract.evaluate_package_against_adapter(package, adapter)
    matching = [failure for failure in failures if failure.code == expected_code]
    if not matching:
        return CheckResult(
            name,
            True,
            False,
            f"expected {expected_code}, observed "
            f"{[failure.code for failure in failures]}",
        )
    failure = matching[0]
    for field in (
        "requirement",
        "required",
        "observed",
        "profile_id",
        "requirements_digest",
        "message",
    ):
        if not getattr(failure, field):
            return CheckResult(
                name, True, False, f"diagnostic missing {field}"
            )
    if failure.profile_id != nwp.COMMON_PROFILE_ID:
        return CheckResult(name, True, False, "diagnostic profile id mismatch")
    if failure.requirements_digest != nwp.COMMON_REQUIREMENTS_DIGEST:
        return CheckResult(name, True, False, "diagnostic digest mismatch")
    return CheckResult(
        name,
        True,
        True,
        evidence={"diagnostic": failure.to_dict()},
    )


def check_capability_failure_matrix() -> CheckResult:
    cases = [
        (
            "feature_level",
            lambda adapter: adapter.update(max_feature_level=0xB000),
            "package_capability_feature_level",
        ),
        (
            "shader_model",
            lambda adapter: adapter.update(max_shader_model=0x50),
            "package_capability_shader_model",
        ),
        (
            "binding_tier",
            lambda adapter: adapter.update(resource_binding_tier=1),
            "package_capability_binding_tier",
        ),
        (
            "heap_tier",
            lambda adapter: adapter.update(resource_heap_tier=1),
            "package_capability_heap_tier",
        ),
        (
            "format_unavailable",
            lambda adapter: adapter["formats"].pop("DXGI_FORMAT_R32_UINT"),
            "package_capability_format_unavailable",
        ),
        (
            "format_usage",
            lambda adapter: adapter["formats"]["DXGI_FORMAT_R32_UINT"].update(
                unordered_access=False
            ),
            "package_capability_format_usage",
        ),
        (
            "compute_limit",
            lambda adapter: adapter.update(max_compute_threads_per_group=32),
            "package_capability_compute_limit",
        ),
        (
            "buffer_limit",
            lambda adapter: adapter.update(max_buffer_bytes=1024),
            "package_capability_buffer_limit",
        ),
        (
            "descriptor_limit",
            lambda adapter: adapter.update(
                max_cbv_srv_uav_descriptors_per_heap=0
            ),
            "package_capability_descriptor_limit",
        ),
        (
            "root_signature_limit",
            lambda adapter: adapter.update(max_root_signature_dword_cost=4),
            "package_capability_root_signature_limit",
        ),
        (
            "swap_chain_limit",
            lambda adapter: adapter.update(max_swap_chain_buffer_count=1),
            "package_capability_swap_chain_limit",
        ),
        (
            "windows_build",
            lambda adapter: adapter.update(windows_build_number=22630),
            "package_capability_windows_build",
        ),
        (
            "shader_target",
            lambda adapter: adapter.update(supports_cs_6_0=False),
            "package_capability_shader_target",
        ),
        (
            "query_unavailable",
            lambda adapter: adapter.update(queried=False),
            "package_capability_query_unavailable",
        ),
    ]
    results = [_assert_failure(f"capability_{name}", mutate, code) for name, mutate, code in cases]
    failed = [result for result in results if not result.passed]
    if failed:
        return CheckResult(
            "nw04_capability_failure_matrix",
            True,
            False,
            "; ".join(f"{item.name}: {item.detail}" for item in failed),
            evidence={"cases": [result.to_dict() for result in results]},
        )
    return CheckResult(
        "nw04_capability_failure_matrix",
        True,
        True,
        evidence={"cases": [result.to_dict() for result in results]},
    )


def check_optional_capability_matrix() -> CheckResult:
    package = nw04_fixture.build_compute_display_package()
    capability = nwp.replace(
        package.capability,
        optional_profile_id=nwp.OPTIONAL_PROFILE_WAVE_OPS,
        optional_requirements_digest=nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST,
        optional_profile_separate_artifact=True,
    )
    capability = nwp.replace(
        capability,
        profile_fingerprint=capability.computed_profile_fingerprint(),
    )
    package = nwp.replace(package, capability=capability)
    static_failures = contract.validate_package_requirements(package)
    cases = [
        (
            "wave_ops_unsupported",
            contract.baseline_adapter(wave_ops_supported=False),
            "package_optional_feature_unsupported",
        ),
        (
            "wave_ops_query_unavailable",
            contract.baseline_adapter(wave_ops_queried=False),
            "package_capability_query_unavailable",
        ),
    ]
    if static_failures:
        return CheckResult(
            "nw04_optional_capability_matrix",
            True,
            False,
            contract.format_failures(static_failures),
        )
    observed = []
    for name, adapter, expected_code in cases:
        failures = contract.evaluate_package_against_adapter(package, adapter)
        matching = [
            failure for failure in failures if failure.code == expected_code
        ]
        if not matching:
            return CheckResult(
                "nw04_optional_capability_matrix",
                True,
                False,
                f"{name}: expected {expected_code}, observed "
                f"{[failure.code for failure in failures]}",
            )
        observed.append({"case": name, "diagnostic": matching[0].to_dict()})
    return CheckResult(
        "nw04_optional_capability_matrix",
        True,
        True,
        evidence={"cases": observed},
    )


def check_package_rejection_before_allocation() -> CheckResult:
    data = nw04_fixture.package_bytes()
    mutations: list[tuple[str, bytes, str]] = [
        ("truncated", data[:-1], "package"),
        ("checksum", data[:-1] + bytes([data[-1] ^ 0xFF]), "package"),
    ]
    body = bytearray(data)
    cap_offset = _section_offset(bytes(body), b"CAP ")
    struct.pack_into("<I", body, cap_offset + 4, 0xFFFF)
    mutations.append(
        ("capability_field_count", _recompute_checksum(bytes(body)), "package_")
    )

    body = bytearray(data)
    cap_offset = _section_offset(bytes(body), b"CAP ")
    field_marker = body.find(b"profile_id", cap_offset)
    if field_marker >= 0:
        body[field_marker : field_marker + len(b"profile_id")] = b"profile_xx"
        mutations.append(
            ("capability_unknown_field", _recompute_checksum(bytes(body)), "package_capability")
        )

    body = bytearray(data)
    cap_offset = _section_offset(bytes(body), b"CAP ")
    field_count = struct.unpack_from("<I", body, cap_offset + 4)[0]
    if field_count > 0:
        struct.pack_into("<I", body, cap_offset + 4, field_count - 1)
        mutations.append(
            ("capability_missing_field", _recompute_checksum(bytes(body)), "package_")
        )

    body = bytearray(data)
    directory = body.find(b"PARM")
    if directory >= 0:
        body[directory : directory + 4] = b"XXXX"
        mutations.append(
            ("unknown_section", _recompute_checksum(bytes(body)), "package_")
        )

    observed: list[dict[str, Any]] = []
    for name, mutated, expected_code_prefix in mutations:
        try:
            nwp.decode_package(mutated)
        except nwp.PackageValidationError as exc:
            diagnostics = list(exc.diagnostics)
            if not diagnostics or any(
                item.get("category") != "package_validation"
                for item in diagnostics
            ):
                return CheckResult(
                    "nw04_package_rejection_before_allocation",
                    True,
                    False,
                    f"{name} produced a non-package diagnostic",
                    evidence={"diagnostics": diagnostics},
                )
            if not any(
                item.get("code", "").startswith(expected_code_prefix)
                for item in diagnostics
            ):
                return CheckResult(
                    "nw04_package_rejection_before_allocation",
                    True,
                    False,
                    f"{name} did not produce {expected_code_prefix}",
                    evidence={"diagnostics": diagnostics},
                )
            observed.append({"case": name, "diagnostics": diagnostics})
        except Exception as exc:
            return CheckResult(
                "nw04_package_rejection_before_allocation",
                True,
                False,
                f"{name} raised the wrong exception type: {exc}",
            )
        else:
            return CheckResult(
                "nw04_package_rejection_before_allocation",
                True,
                False,
                f"{name} was accepted",
            )
    return CheckResult(
        "nw04_package_rejection_before_allocation",
        True,
        True,
        evidence={"cases": observed},
    )


def check_player_source_contract() -> CheckResult:
    header = (REPO_ROOT / "src" / "player" / "package_player.h").read_text(
        encoding="utf-8"
    )
    source = (REPO_ROOT / "src" / "player" / "package_player.cpp").read_text(
        encoding="utf-8"
    )
    plan = (REPO_ROOT / "src" / "player" / "resource_plan.cpp").read_text(
        encoding="utf-8"
    )
    required_header = [
        "ExitCode initialize(const package::Package& package)",
        "ExitCode renderAtTick(std::uint64_t tick)",
        "ExitCode resize(std::uint32_t width, std::uint32_t height)",
        "ExitCode shutdown()",
        "initialization_count()",
        "post_initialization_allocation_violation",
    ]
    for token in required_header:
        if token not in header:
            return CheckResult(
                "nw04_player_source_contract",
                True,
                False,
                f"missing header interface: {token}",
            )
    required_source = [
        "GetResourceAllocationInfo",
        "D3D12_HEAP_FLAG_NONE",
        "DXGI_SWAP_EFFECT_FLIP_DISCARD",
        "package::kSwapChainBufferCount",
        "frame.set_descriptor_table",
        "upload_reuse_waits",
        "frame.upload",
        "TryBarrierStateFromCode",
        "package_instruction_barrier_state",
        "package_instruction_descriptor_target",
        "simulate_post_initialization_allocation_",
        "SetSimulatePostInitializationAllocationForTesting",
        "SetForcePipelineCreationFailureForTesting",
        "TryParameterTickLayout",
        "frame_timeline_parameter_not_bound",
        "package::ValidatePackage",
        "package_release_flag_required",
        "UsesOnlyNonAliasedPlacements",
        "SetResourceAllocationQueryForTesting",
        "SetForceDeviceRemovedForTesting",
        # Raw RWByteAddressBuffer UAV realization and texture-dimension views.
        "D3D12_BUFFER_UAV_FLAG_RAW",
        "DXGI_FORMAT_R32_TYPELESS",
        "D3D12_UAV_DIMENSION_TEXTURE2D",
        # Dependent UAV ordering and fullscreen package-output display.
        "D3D12_RESOURCE_BARRIER_TYPE_UAV",
        "nw04.display.fullscreen",
        "display_fixture_dxil.h",
        "CopyResource",
        # Planning-time target-bound validation.
        "package_root_constant_bound",
        "package_root_constant_signature",
        "package_instruction_copy_bounds",
        "package_instruction_copy_readback_source",
        "package_instruction_copy_self",
        "package_present_target",
        # Restore transition and drain failure propagation.
        "SetWindowMinimizedForTesting",
        "An ordinary minimize/restore",
        "CheckD3D12Validation(\"shutdown\")",
    ]
    for token in required_source:
        if token not in source + plan + header:
            return CheckResult(
                "nw04_player_source_contract",
                True,
                False,
                f"missing player source contract: {token}",
            )
    for token in ("NonAliasedCommitted", "GetResourceAllocationInfo"):
        if token not in plan and token not in source:
            return CheckResult(
                "nw04_player_source_contract",
                True,
                False,
                f"missing resource realization contract: {token}",
            )
    forbidden_realization = (
        "CreatePlacedResource",
        "D3D12_RESOURCE_BARRIER_TYPE_ALIASING",
        "D3D12_HEAP_FLAG_ALLOW_ONLY",
        "D3D12_BARRIER_TYPE_",
        "D3D12_COMMAND_LIST_TYPE_COMPUTE",
        "D3D12_COMMAND_LIST_TYPE_COPY",
    )
    for token in forbidden_realization:
        if token in source:
            return CheckResult(
                "nw04_player_source_contract",
                True,
                False,
                f"NW-04 must not infer physical alias reuse: {token}",
            )
    # Required validation order inside initialize(const Package&).
    initialize_at = source.index("ExitCode PackagePlayer::initialize(const package::Package&")
    tail = source[initialize_at:]
    ordered = [
        "ValidateEmittedRequirementsAgainstPackage",
        "OpenDeviceAndWindow",
        "EvaluatePackageCapabilities",
        "BuildNonAliasedResourcePlan",
        "BuildFrameInstructionPlan",
        "CreateSwapChainAndFrames",
        "RunInitializationInstructions",
    ]
    positions = [tail.find(token) for token in ordered]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        return CheckResult(
            "nw04_player_source_contract",
            True,
            False,
            "initialization validation/allocation ordering is not enforced",
            evidence={"order": dict(zip(ordered, positions))},
        )
    return CheckResult("nw04_player_source_contract", True, True)


def check_release_closure() -> CheckResult:
    player_dir = REPO_ROOT / "src" / "player"
    platform_dir = REPO_ROOT / "src" / "platform"
    forbidden_patterns = (
        r"#include\s*[<\"]dxcompiler",
        r"LoadLibrary[^\n]*dxcompiler",
        r"\bD3DCompile\s*\(",
        r"\bnw03_compiler\b",
        r"\bnw_graph\b",
    )
    found: list[str] = []
    source_paths = sorted(player_dir.glob("*")) + sorted(platform_dir.glob("*"))
    for path in source_paths:
        if path.suffix not in {".cpp", ".h"}:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if re.search(pattern, text):
                found.append(f"{path.name}:{pattern}")
    build_profiles = (REPO_ROOT / "cmake" / "BuildProfiles.cmake").read_text(
        encoding="utf-8"
    )
    if 'NW_ENABLE_D3D12_VALIDATION=0' not in build_profiles:
        found.append("release profile does not disable D3D12 diagnostic tooling")
    if 'NW_PROFILE_DEV=1' not in build_profiles:
        found.append("diagnostic profile is not defined separately")
    if found:
        return CheckResult(
            "nw04_release_closure",
            True,
            False,
            "; ".join(found),
        )
    return CheckResult(
        "nw04_release_closure",
        True,
        True,
        evidence={
            "excluded": list(forbidden_patterns),
            "diagnostic_profile": "dev",
            "release_profile": "release",
        },
    )


def run_contracts_only() -> list[CheckResult]:
    return [
        check_fixture_roundtrip(),
        check_exact_capability_contract(),
        check_fixture_planner_contract(),
        check_capability_failure_matrix(),
        check_optional_capability_matrix(),
        check_package_rejection_before_allocation(),
        check_player_source_contract(),
        check_release_closure(),
    ]


def _validate_hardware_package(package_path: Path) -> CheckResult:
    if not package_path.exists():
        return CheckResult(
            "nw04_hardware_package",
            True,
            False,
            f"package is unavailable: {package_path}",
            unavailable=True,
        )
    try:
        data = package_path.read_bytes()
        package = nwp.decode_package(data)
    except Exception as exc:
        return CheckResult("nw04_hardware_package", True, False, str(exc))
    failures = contract.validate_package_requirements(package)
    if failures:
        return CheckResult(
            "nw04_hardware_package",
            True,
            False,
            contract.format_failures(failures),
        )
    plan_failures = contract.validate_frame_plan(package)
    if plan_failures:
        return CheckResult(
            "nw04_hardware_package",
            True,
            False,
            contract.format_failures(plan_failures),
        )
    frame_opcodes = [instruction.opcode for instruction in package.frame]
    if (
        "frame.dispatch" not in frame_opcodes
        or "frame.present" not in frame_opcodes
    ):
        return CheckResult(
            "nw04_hardware_package",
            True,
            False,
            "hardware package must contain a compute/display instruction "
            "stream (frame.dispatch and frame.present); a compute-only package "
            "cannot certify a visible render",
            evidence={"frame": frame_opcodes},
        )
    present_resources = {
        instruction.operands[0].value
        for instruction in package.frame
        if instruction.opcode == "frame.present"
        and instruction.operands
        and instruction.operands[0].type == "resource_ref"
    }
    display_outputs = {
        resource.resource_id
        for resource in package.resources
        if resource.resource_id in present_resources
        and resource.kind == "buffer"
        and resource.usage == "uav"
        and resource.size_bytes >= 16
        and resource.size_bytes % 16 == 0
    }
    if present_resources != display_outputs:
        return CheckResult(
            "nw04_hardware_package",
            True,
            False,
            "frame.present does not reference a displayable package UAV buffer "
            "output",
            evidence={"present_resources": sorted(present_resources)},
        )
    # Hardware qualification must use the real offline-compiled NW-03 package,
    # never the synthetic contract fixture.
    try:
        from offline_shader_pack import validate_dxil_container

        for shader in package.shaders:
            validate_dxil_container(shader.blob, allow_test_container=False)
    except Exception as exc:
        return CheckResult(
            "nw04_hardware_package",
            True,
            False,
            f"package DXIL is not a real offline-compiled artifact: {exc}",
        )
    return CheckResult(
        "nw04_hardware_package",
        True,
        True,
        evidence={
            "package_sha256": hashlib.sha256(data).hexdigest(),
            "package_size_bytes": len(data),
            "profile_id": package.capability.profile_id,
            "requirements_digest": package.capability.requirements_digest,
            "frame": frame_opcodes,
            "present_resources": sorted(present_resources),
        },
    )


def check_hardware_evidence_complete(
    record_path: Path | None = None,
) -> CheckResult:
    """Require executed Ada and Blackwell qualification for identical artifacts."""
    path = record_path or (REPO_ROOT / "benchmarks" / "nw04-hardware-evidence.json")
    if not path.exists():
        return CheckResult(
            "nw04_hardware_qualification",
            True,
            False,
            f"hardware evidence record is unavailable: {path}",
            unavailable=True,
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult(
            "nw04_hardware_qualification", True, False, str(exc)
        )
    qualifications = record.get("qualifications", {})
    missing = [
        tier
        for tier in ("ada", "blackwell")
        if qualifications.get(tier, {}).get("status") != "verified"
    ]
    successful = [
        execution
        for execution in record.get("executions", [])
        if execution.get("execution_status") == "passed"
    ]
    package_hashes = {entry["package"]["sha256"] for entry in successful}
    player_hashes = {entry["player"]["sha256"] for entry in successful}
    verified_tiers = {
        entry.get("hardware_tier") for entry in successful
    }
    failures: list[str] = []
    if record.get("verification_status") != "verified":
        failures.append(
            f"verification_status={record.get('verification_status', 'unavailable')}"
        )
    if missing:
        failures.append("unverified tiers: " + ",".join(missing))
    if not {"ada", "blackwell"} <= verified_tiers:
        failures.append("no passed execution for both Ada and Blackwell")
    if len(package_hashes) != 1 or len(player_hashes) != 1:
        failures.append("qualification did not use identical player/package hashes")
    if any(
        execution.get("d3d12_validation_clean") is not True
        for execution in successful
    ):
        failures.append("a retained execution was not D3D12-validation-clean")
    for execution in successful:
        render = execution.get("render", {})
        try:
            frames = int(render.get("frames", "-1"))
            presents = int(render.get("presents", "-1"))
            resize_count = int(render.get("resize_count", "-1"))
            uav_barriers = int(render.get("uav_barriers", "-1"))
        except (TypeError, ValueError):
            failures.append("execution render counts are unavailable")
            continue
        if frames <= 0 or presents != frames:
            failures.append(
                "execution did not complete the requested visible renders"
            )
        if resize_count < 2:
            failures.append("execution did not exercise resize")
        if uav_barriers < 1:
            failures.append("execution did not record dependent UAV barriers")
        if execution.get("post_initialization_allocation_violation") != "0":
            failures.append("execution reported a post-initialization allocation")
        if execution.get("non_aliased_placements") != "1":
            failures.append("execution did not use non-aliased placements")
    if failures:
        return CheckResult(
            "nw04_hardware_qualification",
            True,
            False,
            "; ".join(failures),
            evidence={"record": str(path)},
        )
    return CheckResult(
        "nw04_hardware_qualification",
        True,
        True,
        evidence={
            "record": str(path),
            "package_sha256": next(iter(package_hashes)),
            "player_sha256": next(iter(player_hashes)),
        },
    )


def run_native_fixture(
    fixture: Path,
    package_path: Path,
    extra: list[str],
    *,
    validation: bool = True,
) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    try:
        package = nwp.decode_package(package_path.read_bytes())
    except Exception as exc:
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            f"native fixture package could not be decoded: {exc}",
        )
    frames = 8
    tick_start = 0
    tick_step = 1
    expected_frames = frames + 2
    last_tick = tick_start + (frames - 1) * tick_step
    expected_upload_reuse = max(
        0,
        sum(
            1
            for instruction in package.initialization
            if instruction.opcode == "init.upload_resource"
        )
        - 1,
    )
    last_constants, expected_timeline = contract.apply_frame_constants(
        package, last_tick
    )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        str(frames),
        "--tick-start",
        str(tick_start),
        "--tick-step",
        str(tick_step),
        "--resize-sequence",
        "--simulate-minimize",
        "--win32-minimize-restore",
        "--emit-benchmark-metrics",
    ]
    if validation:
        command.append("--validation")
    command.extend(extra)
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        if proc.stdout:
            print("[NW04-NATIVE-STDOUT]", file=sys.stderr)
            print(proc.stdout, file=sys.stderr, end="" if proc.stdout.endswith("\n") else "\n")
        if proc.stderr:
            print("[NW04-NATIVE-STDERR]", file=sys.stderr)
            print(proc.stderr, file=sys.stderr, end="" if proc.stderr.endswith("\n") else "\n")
        # A Windows Direct3D debug-layer break can terminate the process with
        # 0x87D (2173) before InfoQueue diagnostics reach stderr.  Keep the
        # full run failed, but isolate which optional lifecycle feature triggers
        # the abnormal native termination so CI remains actionable.
        if validation and proc.returncode == 0x87D:
            probes = (
                ("validation_init_only", ["--frames", "0", "--validation"]),
                (
                    "validation_headless_init",
                    ["--frames", "0", "--validation", "--headless"],
                ),
                (
                    "validation_headless_frame",
                    ["--frames", "1", "--validation", "--headless"],
                ),
                ("validation_only", ["--frames", "1", "--validation"]),
                (
                    "validation_metrics",
                    ["--frames", "1", "--validation", "--emit-benchmark-metrics"],
                ),
                (
                    "validation_resize",
                    ["--frames", "1", "--validation", "--resize-sequence"],
                ),
                (
                    "validation_simulated_minimize",
                    ["--frames", "1", "--validation", "--simulate-minimize"],
                ),
                (
                    "validation_win32_minimize_restore",
                    ["--frames", "1", "--validation", "--win32-minimize-restore"],
                ),
            )
            for probe_name, probe_args in probes:
                probe_command = [
                    str(fixture),
                    "--package",
                    str(package_path),
                    *probe_args,
                ]
                probe = subprocess.run(
                    probe_command,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                print(
                    f"[NW04-NATIVE-PROBE] {probe_name} exit={probe.returncode}",
                    file=sys.stderr,
                )
                if probe.stdout:
                    print(
                        f"[NW04-NATIVE-PROBE-STDOUT] {probe_name}",
                        file=sys.stderr,
                    )
                    print(
                        probe.stdout,
                        file=sys.stderr,
                        end="" if probe.stdout.endswith("\n") else "\n",
                    )
                if probe.stderr:
                    print(
                        f"[NW04-NATIVE-PROBE-STDERR] {probe_name}",
                        file=sys.stderr,
                    )
                    print(
                        probe.stderr,
                        file=sys.stderr,
                        end="" if probe.stderr.endswith("\n") else "\n",
                    )
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            f"native fixture exited {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    output = proc.stdout
    required_lines = (
        "NW_PLAYER_INITIALIZATION_COUNT=1",
        "NW_PLAYER_NON_ALIASED_PLACEMENTS=1",
        "NW_PLAYER_POST_INIT_ALLOCATION_VIOLATION=0",
        "NW_PLAYER_READBACK=not_performed",
        "NW_PLAYER_LAST_TIMELINE_PARAMETER_ID=",
        "NW_PLAYER_MINIMIZE_RESTORE_RENDERED=1",
        "NW_PLAYER_UAV_BARRIERS=",
    )
    for line in required_lines:
        if line not in output:
            return CheckResult(
                "nw04_native_fixture",
                True,
                False,
                f"native fixture did not report {line}",
                evidence={"stdout": output, "stderr": proc.stderr},
            )
    upload_reuse = re.search(r"NW_PLAYER_UPLOAD_REUSE_WAITS=(\d+)", output)
    if upload_reuse is None or int(upload_reuse.group(1)) != expected_upload_reuse:
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            "native fixture upload-ring reuse does not match the package "
            f"(expected {expected_upload_reuse})",
            evidence={"stdout": output, "stderr": proc.stderr},
        )
    uav_barriers = re.search(r"NW_PLAYER_UAV_BARRIERS=(\d+)", output)
    if uav_barriers is None or int(uav_barriers.group(1)) < 2:
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            "native fixture did not report dependent UAV barriers",
            evidence={"stdout": output, "stderr": proc.stderr},
        )
    if expected_timeline is not None:
        expected_line = f"NW_PLAYER_LAST_TIMELINE_VALUE={expected_timeline}"
        if expected_line not in output:
            return CheckResult(
                "nw04_native_fixture",
                True,
                False,
                f"native fixture did not report {expected_line}",
                evidence={"stdout": output, "stderr": proc.stderr},
            )
    frames_match = re.search(r"NW_PLAYER_FRAMES=(\d+)", output)
    presents_match = re.search(r"NW_PLAYER_PRESENTS=(\d+)", output)
    observed_frames = (
        int(frames_match.group(1)) if frames_match is not None else None
    )
    observed_presents = (
        int(presents_match.group(1)) if presents_match is not None else None
    )
    if (
        observed_frames != expected_frames
        or observed_presents != expected_frames
    ):
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            "native fixture did not complete the requested visible renders "
            f"(expected frames={expected_frames}, presents={expected_frames}; "
            f"observed frames={observed_frames}, presents={observed_presents})",
            evidence={"stdout": output, "stderr": proc.stderr},
        )
    present_resource = next(
        (
            instruction.operands[0].value
            for instruction in package.frame
            if instruction.opcode == "frame.present"
            and instruction.operands
        ),
        None,
    )
    if (
        present_resource is None
        or f"NW_PLAYER_PRESENT_RESOURCE_ID={present_resource}" not in output
    ):
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            "native fixture did not resolve the package frame.present output",
            evidence={"stdout": output, "stderr": proc.stderr},
        )
    resize_match = re.search(r"NW_PLAYER_RESIZE_COUNT=(\d+)", output)
    if resize_match is None or int(resize_match.group(1)) < 2:
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            "native fixture did not execute the resize sequence",
            evidence={"stdout": output, "stderr": proc.stderr},
        )
    if "NW_D3D12_VALIDATION_ERROR=" in (output + proc.stderr):
        return CheckResult(
            "nw04_native_fixture",
            True,
            False,
            "D3D12 validation emitted an error during the native run",
            evidence={"stdout": output, "stderr": proc.stderr},
        )
    if last_constants:
        expected_root0 = f"NW_PLAYER_LAST_ROOT_CONSTANT0=0x{struct.unpack_from('<I', last_constants, 0)[0]:08X}"
        if expected_root0 not in output:
            return CheckResult(
                "nw04_native_fixture",
                True,
                False,
                "native fixture root-constant bytes do not match the package "
                f"(expected {expected_root0})",
                evidence={"stdout": output, "stderr": proc.stderr},
            )
    return CheckResult(
        "nw04_native_fixture",
        True,
        True,
        evidence={"stdout": output, "stderr": proc.stderr, "command": command},
    )


def run_device_removal_check(fixture: Path, package_path: Path) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_device_removal_termination",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "1",
        "--simulate-device-removal",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 51:
        return CheckResult(
            "nw04_device_removal_termination",
            True,
            False,
            f"expected DeviceRemoved (51), observed {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    if "NW_PLAYER_DEVICE_REMOVAL_TERMINATED=1" not in proc.stdout:
        return CheckResult(
            "nw04_device_removal_termination",
            True,
            False,
            "device-removal termination evidence line is missing",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    return CheckResult(
        "nw04_device_removal_termination",
        True,
        True,
        evidence={"stdout": proc.stdout, "stderr": proc.stderr, "command": command},
    )


def run_allocation_failure_check(fixture: Path, package_path: Path) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_allocation_failure_cleanup",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "1",
        "--simulate-allocation-failure",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 33:
        return CheckResult(
            "nw04_allocation_failure_cleanup",
            True,
            False,
            f"expected ResourceAllocationFailed (33), observed {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    if "NW_PLAYER_PARTIAL_INITIALIZATION_CLEANUP=1" not in proc.stdout:
        return CheckResult(
            "nw04_allocation_failure_cleanup",
            True,
            False,
            "partial-initialization cleanup evidence line is missing",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    return CheckResult(
        "nw04_allocation_failure_cleanup",
        True,
        True,
        evidence={"stdout": proc.stdout, "stderr": proc.stderr, "command": command},
    )


def run_capability_failure_check(fixture: Path, package_path: Path) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_capability_failure_rejection",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "1",
        "--simulate-capability-failure",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 23:
        return CheckResult(
            "nw04_capability_failure_rejection",
            True,
            False,
            f"expected CapabilityBaselineUnmet (23), observed {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    if "NW_PLAYER_CAPABILITY_FAILURE_REJECTED=1" not in proc.stdout:
        return CheckResult(
            "nw04_capability_failure_rejection",
            True,
            False,
            "capability rejection evidence line is missing",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    return CheckResult(
        "nw04_capability_failure_rejection",
        True,
        True,
        evidence={"stdout": proc.stdout, "stderr": proc.stderr, "command": command},
    )


def run_pipeline_failure_check(fixture: Path, package_path: Path) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_pipeline_failure_cleanup",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "1",
        "--simulate-pipeline-failure",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 42:
        return CheckResult(
            "nw04_pipeline_failure_cleanup",
            True,
            False,
            f"expected PipelineStateCreationFailed (42), observed {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    for line in (
        "NW_PLAYER_PIPELINE_FAILURE_REJECTED=1",
        "NW_PLAYER_PARTIAL_INITIALIZATION_CLEANUP=1",
    ):
        if line not in proc.stdout:
            return CheckResult(
                "nw04_pipeline_failure_cleanup",
                True,
                False,
                f"pipeline-failure evidence line is missing: {line}",
                evidence={"stdout": proc.stdout, "stderr": proc.stderr},
            )
    return CheckResult(
        "nw04_pipeline_failure_cleanup",
        True,
        True,
        evidence={"stdout": proc.stdout, "stderr": proc.stderr, "command": command},
    )


def run_frame_allocation_detection_check(
    fixture: Path, package_path: Path
) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_post_init_allocation_detection",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "2",
        "--simulate-frame-allocation",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return CheckResult(
            "nw04_post_init_allocation_detection",
            True,
            False,
            f"fixture exited {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    if "NW_PLAYER_POST_INIT_ALLOCATION_VIOLATION=1" not in proc.stdout:
        return CheckResult(
            "nw04_post_init_allocation_detection",
            True,
            False,
            "tracked render-path allocation was not reported as a violation",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    return CheckResult(
        "nw04_post_init_allocation_detection",
        True,
        True,
        evidence={"stdout": proc.stdout, "stderr": proc.stderr, "command": command},
    )


def run_planner_self_test(fixture: Path, package_path: Path) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_host_simulation_planner",
            True,
            False,
            f"fixture binary is unavailable: {fixture}",
            unavailable=True,
        )
    try:
        package = nwp.decode_package(package_path.read_bytes())
    except Exception as exc:
        return CheckResult(
            "nw04_host_simulation_planner",
            True,
            False,
            f"planner package could not be decoded: {exc}",
        )
    failures = contract.validate_frame_plan(package)
    if failures:
        return CheckResult(
            "nw04_host_simulation_planner",
            True,
            False,
            contract.format_failures(failures),
        )
    last_constants, expected_timeline = contract.apply_frame_constants(
        package, 7
    )
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "8",
        "--tick-start",
        "0",
        "--tick-step",
        "1",
        "--planner-self-test",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return CheckResult(
            "nw04_host_simulation_planner",
            True,
            False,
            f"planner self-test exited {proc.returncode}",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    if "NW_PLAYER_PLANNER_SELF_TEST=1" not in proc.stdout:
        return CheckResult(
            "nw04_host_simulation_planner",
            True,
            False,
            "planner self-test evidence line is missing",
            evidence={"stdout": proc.stdout, "stderr": proc.stderr},
        )
    if expected_timeline is not None:
        expected_line = f"NW_PLAYER_LAST_TIMELINE_VALUE={expected_timeline}"
        if expected_line not in proc.stdout:
            return CheckResult(
                "nw04_host_simulation_planner",
                True,
                False,
                f"planner self-test did not report {expected_line}",
                evidence={"stdout": proc.stdout, "stderr": proc.stderr},
            )
    if last_constants:
        expected_root0 = (
            "NW_PLAYER_LAST_ROOT_CONSTANT0=0x"
            f"{struct.unpack_from('<I', last_constants, 0)[0]:08X}"
        )
        if expected_root0 not in proc.stdout:
            return CheckResult(
                "nw04_host_simulation_planner",
                True,
                False,
                "planner self-test root-constant bytes do not match the "
                f"expected update ({expected_root0})",
                evidence={"stdout": proc.stdout, "stderr": proc.stderr},
            )
    # The negative mutations below are deliberately tailored to the repository
    # fixture (for example resource id 1). Do not apply those assumptions to an
    # arbitrary valid NW-03 package supplied to Full/Hardware verification.
    canonical_fixture = (
        REPO_ROOT / "fixtures" / "nw04" / "compute_display.nwp"
    ).resolve()
    exercise_negative_mutations = package_path.resolve() == canonical_fixture
    frame_opcodes = {instruction.opcode for instruction in package.frame}
    mutations: list[tuple[str, str]] = []
    if exercise_negative_mutations and "frame.barrier" in frame_opcodes:
        mutations.extend(
            [
                ("barrier-state", "package_instruction_barrier_state"),
                ("barrier-usage", "package_instruction_barrier_usage"),
            ]
        )
    if exercise_negative_mutations and "frame.set_descriptor_table" in frame_opcodes:
        mutations.extend(
            [
                ("descriptor-heap", "package_instruction_descriptor_heap"),
                ("descriptor-target", "package_instruction_descriptor_target"),
            ]
        )
    if (
        exercise_negative_mutations
        and "frame.set_timeline" in frame_opcodes
        and package.parameters
    ):
        mutations.append(
            ("timeline-type", "package_parameter_type_unsupported")
        )
    if (
        exercise_negative_mutations
        and "frame.set_root_constants" in frame_opcodes
    ):
        mutations.append(
            ("root-constant-bound", "package_root_constant_bound")
        )
    if exercise_negative_mutations and "frame.present" in frame_opcodes:
        mutations.append(("present-target", "package_present_target"))
    if exercise_negative_mutations and "frame.dispatch" in frame_opcodes:
        mutations.append(("dispatch-root", "package_instruction_reference"))
    if exercise_negative_mutations:
        mutations.extend(
            [
                ("copy-bounds", "package_instruction_copy_bounds"),
                ("copy-self", "package_instruction_copy_self"),
                ("copy-texture", "package_instruction_copy_kind"),
                ("copy-readback-source",
                 "package_instruction_copy_readback_source"),
            ]
        )
    for mutation, expected_code in mutations:
        mutation_proc = subprocess.run(
            [*command, "--planner-mutation", mutation],
            capture_output=True,
            text=True,
            check=False,
        )
        combined = mutation_proc.stdout + mutation_proc.stderr
        if mutation_proc.returncode == 0:
            return CheckResult(
                "nw04_host_simulation_planner",
                True,
                False,
                f"planner accepted mutation {mutation}",
                evidence={"stdout": combined},
            )
        if (
            "NW_PLAYER_PLANNER_REJECTED=1" not in mutation_proc.stdout
            or expected_code not in combined
        ):
            return CheckResult(
                "nw04_host_simulation_planner",
                True,
                False,
                f"planner mutation {mutation} did not emit {expected_code}",
                evidence={"stdout": combined},
            )
    return CheckResult(
        "nw04_host_simulation_planner",
        True,
        True,
        evidence={
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "command": command,
        },
    )


def build_summary(mode: str, results: list[CheckResult]) -> dict[str, Any]:
    passed = all(result.passed for result in results if result.required)
    hardware_complete = mode == "Hardware" and any(
        result.name == "nw04_hardware_qualification" and result.passed
        for result in results
    )
    hardware_status = "verified" if hardware_complete else "pending_unverified"
    mode_status = {
        "ContractsOnly": "contracts_only_verified",
        "NativeLifecycle": "native_lifecycle_verified",
        "Full": "full_execution_verified",
        "Hardware": "hardware_execution_verified",
    }[mode]
    return {
        "task": "NW-04",
        "mode": mode,
        # Scope the status to what this mode actually executed.
        "verification_status": mode_status if passed else "failed",
        # Representative Ada/Blackwell qualification remains mandatory and is
        # not implied by any GPU-less or single-machine check.
        "overall_status": (
            "verified" if hardware_complete and passed else "pending_unverified"
        ),
        "checks": [result.to_dict() for result in results],
        "hardware_qualification": {
            "ada": hardware_status,
            "blackwell": hardware_status,
            "note": (
                "Representative Ada/Blackwell qualification is recorded through "
                "the NW-01B hardware evidence harness; missing coverage is "
                "never reported as passing."
            ),
        },
    }


def build_evidence_record(
    summary: dict[str, Any], passed: bool
) -> dict[str, Any]:
    """Schema-valid NW-04 evidence envelope for --record output.

    ``--summary-json`` carries the mode-scoped verification summary.  The
    hardware evidence record itself stays explicit about missing representative
    qualification and never upgrades it based on GPU-less or single-host tests.
    """
    return {
        "schema_version": "1.0.0",
        "task": "NW-04",
        "verification_status": (
            "pending_unverified" if passed else "executed_failure"
        ),
        "qualifications": {
            "ada": {
                "hardware_tier": "desktop_ada_representative",
                "status": "pending_unverified",
                "reason": "No representative desktop Ada execution is recorded for the NW-04 player/package artifacts.",
            },
            "blackwell": {
                "hardware_tier": "desktop_blackwell_representative",
                "status": "pending_unverified",
                "reason": "No representative desktop Blackwell execution is recorded for the NW-04 player/package artifacts.",
            },
        },
        "executions": [],
        "unavailable_telemetry": [
            "driver_version",
            "wddm_version",
            "cpu_submit_ms",
            "gpu_ms",
        ],
        "toolchain_manifest_sha256": "unavailable",
        "policy": (
            "Historical NW-01 measurements cannot certify NW-04 artifacts. "
            "Missing coverage stays pending/unverified; executed failures remain failures."
        ),
        "verification_summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("ContractsOnly", "NativeLifecycle", "Full", "Hardware"),
        default="ContractsOnly",
    )
    parser.add_argument("--fixture", type=Path)
    parser.add_argument(
        "--package",
        type=Path,
        help="Real NW-03 .nwp package required by NativeLifecycle/Full/Hardware modes.",
    )
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()

    results = run_contracts_only()
    if args.mode == "ContractsOnly" and args.fixture is not None:
        # Optional GPU-less host-simulation execution of the real planner over
        # the in-repo package fixture.  It requires no device or window.
        results.append(
            run_planner_self_test(
                args.fixture,
                REPO_ROOT / "fixtures" / "nw04" / "compute_display.nwp",
            )
        )
    if args.mode in ("NativeLifecycle", "Full", "Hardware"):
        if args.fixture is None:
            results.append(
                CheckResult(
                    "nw04_native_fixture",
                    True,
                    False,
                    "--fixture is required for NativeLifecycle/Full/Hardware mode",
                    unavailable=True,
                )
            )
        else:
            if args.package is None:
                results.append(
                    CheckResult(
                        "nw04_hardware_package",
                        True,
                        False,
                        "--package is required for NativeLifecycle/Full/Hardware mode; the "
                        "synthetic contract fixture cannot certify hardware",
                        unavailable=True,
                    )
                )
            else:
                package_check = _validate_hardware_package(args.package)
                results.append(package_check)
                if package_check.passed:
                    results.append(
                        run_native_fixture(
                            args.fixture,
                            args.package,
                            [],
                            validation=args.mode != "NativeLifecycle",
                        )
                    )
                    results.append(
                        run_device_removal_check(args.fixture, args.package)
                    )
                    results.append(
                        run_allocation_failure_check(args.fixture, args.package)
                    )
                    results.append(
                        run_capability_failure_check(args.fixture, args.package)
                    )
                    results.append(
                        run_pipeline_failure_check(args.fixture, args.package)
                    )
                    results.append(
                        run_frame_allocation_detection_check(
                            args.fixture, args.package
                        )
                    )
                    results.append(
                        run_planner_self_test(args.fixture, args.package)
                    )
    if args.mode == "Hardware":
        results.append(check_hardware_evidence_complete())

    passed = all(result.passed for result in results if result.required)
    summary = build_summary(args.mode, results)
    if args.summary_json is not None:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    for result in results:
        state = "PASS" if result.passed else ("UNAVAILABLE" if result.unavailable else "FAIL")
        print(f"[{state}] {result.name} {result.detail}")
    if args.record is not None:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(
            json.dumps(
                build_evidence_record(summary, passed),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
