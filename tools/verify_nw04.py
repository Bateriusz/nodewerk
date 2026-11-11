#!/usr/bin/env python3
"""NW-04 verification entry point.

Usage:
    python tools/verify_nw04.py --mode ContractsOnly [--summary-json out.json]
    python tools/verify_nw04.py --mode Full [--fixture path/to/player.exe]
    python tools/verify_nw04.py --mode Hardware --fixture path/to/player.exe \
        --record benchmarks/nw04-hardware-evidence.json

ContractsOnly is GPU-less and host-neutral.  Full and Hardware are Windows
qualification modes; when a required native artifact or adapter is unavailable
they report `unavailable` and exit nonzero instead of claiming success.
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
        "UsesOnlyNonAliasedPlacements",
        "SetResourceAllocationQueryForTesting",
        "SetForceDeviceRemovedForTesting",
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
        check_capability_failure_matrix(),
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
        },
    )


def run_native_fixture(
    fixture: Path, package_path: Path, extra: list[str]
) -> CheckResult:
    if not fixture.exists():
        return CheckResult(
            "nw04_native_fixture",
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
        "8",
        "--tick-start",
        "0",
        "--tick-step",
        "1",
        "--resize-sequence",
        "--simulate-minimize",
        "--emit-benchmark-metrics",
        "--validation",
        *extra,
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("ContractsOnly", "Full", "Hardware"),
        default="ContractsOnly",
    )
    parser.add_argument("--fixture", type=Path)
    parser.add_argument(
        "--package",
        type=Path,
        help="Real NW-03 .nwp package required by Full/Hardware modes.",
    )
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()

    results = run_contracts_only()
    if args.mode in ("Full", "Hardware"):
        if args.fixture is None:
            results.append(
                CheckResult(
                    "nw04_native_fixture",
                    True,
                    False,
                    "--fixture is required for Full/Hardware mode",
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
                        "--package is required for Full/Hardware mode; the "
                        "synthetic contract fixture cannot certify hardware",
                        unavailable=True,
                    )
                )
            else:
                package_check = _validate_hardware_package(args.package)
                results.append(package_check)
                if package_check.passed:
                    results.append(
                        run_native_fixture(args.fixture, args.package, [])
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

    passed = all(result.passed for result in results if result.required)
    summary = {
        "task": "NW-04",
        "mode": args.mode,
        "verification_status": "verified" if passed else "failed",
        "checks": [result.to_dict() for result in results],
        "hardware_qualification": {
            "ada": "pending_unverified",
            "blackwell": "pending_unverified",
            "note": (
                "Representative Ada/Blackwell qualification is recorded through "
                "the NW-01B hardware evidence harness; missing coverage is "
                "never reported as passing."
            ),
        },
    }
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
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
