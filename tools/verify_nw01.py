#!/usr/bin/env python3
"""Nodewerk NW-01 Verification Runner backing tools/verify-nw01.ps1.

Supports two documented modes:
- Full:
    Performs build, schema, dependency, reproducibility, fixture-validation,
    and evidence-completeness checks, recording reproducibility results to
    benchmarks/reproducibility/release-build-reproducibility.json when built.
    Missing prerequisites or unverified physical hardware/clean-system evidence
    NEVER produce a passing Full result.
- CleanSystem:
    Probes SDK absence, verifies deployed dependency closure, executes release
    fixtures on a target clean physical Windows 11 x64 machine without requiring
    developer SDKs, DXC, or Agility SDK, and records the verified evidence back
    to benchmarks/clean-system/clean-system-validation.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from dependency_check import (
    DependencyClosureError,
    SIDECAR_SUFFIXES,
    parse_pe64_sections_and_imports,
    verify_all_recorded_ledgers,
    verify_closure,
)
from offline_shader_pack import (
    check_hlsl_source_constraints,
    find_dxc_executable,
    query_dxc_version,
    validate_dxil_container,
)
from schema_validator import validate_all_repository_artifacts, validate_file_against_schema
from size_ledger import generate_size_comparison_report, record_release_ledgers


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


READBACK_LINE_RE = re.compile(r"^NW_READBACK_U32=([0-9A-Fa-f,]+)$", re.MULTILINE)
READBACK_COUNT_RE = re.compile(r"^NW_READBACK_ELEMENT_COUNT=([0-9]+)$", re.MULTILINE)


def load_compute_expected_output(repo_root: Path) -> dict[str, Any]:
    """Load the committed deterministic compute readback expectation."""
    expected_path = repo_root / "fixtures" / "compute_shader" / "expected_output.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if expected.get("output_sha256") is None or not expected.get("elements_u32"):
        raise ValueError(f"Compute expectation {expected_path} is missing measured elements/hash")
    return expected


def parse_observed_compute_readback(stdout: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Parse fixture-emitted GPU readback values and recompute the observed SHA-256.

    The observed digest is derived only from values the executable actually printed after
    D3D12 dispatch + readback. Static expected hashes are never copied into the observed field.
    """
    count_match = READBACK_COUNT_RE.search(stdout)
    values_match = READBACK_LINE_RE.search(stdout)
    if not values_match:
        return {
            "observed_element_count": None,
            "observed_output_sha256": None,
            "expected_output_sha256": expected["output_sha256"],
            "elements_match": False,
            "error": "fixture did not emit NW_READBACK_U32 readback evidence",
        }

    values = [int(token, 16) for token in values_match.group(1).split(",")]
    observed_count = int(count_match.group(1)) if count_match else len(values)
    expected_elements = expected["elements_u32"]
    elements_match = observed_count == expected["element_count"] == len(values) and values == expected_elements
    raw = struct.pack(f"<{len(values)}I", *values)
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    return {
        "observed_element_count": observed_count,
        "observed_output_sha256": observed_sha256,
        "expected_output_sha256": expected["output_sha256"],
        "elements_match": elements_match,
        "hash_match": observed_sha256 == expected["output_sha256"],
    }


def find_embedded_dxil_container(binary_bytes: bytes) -> bytes | None:
    """Locate and validate a DXC-produced DXBC/DXIL container embedded in a player executable."""
    search_from = 0
    while True:
        start = binary_bytes.find(b"DXBC", search_from)
        if start < 0:
            return None
        if start + 28 <= len(binary_bytes):
            total_size = struct.unpack_from("<I", binary_bytes, start + 24)[0]
            end = start + total_size
            if 44 <= total_size and end <= len(binary_bytes):
                candidate = binary_bytes[start:end]
                try:
                    validate_dxil_container(candidate)
                    return candidate
                except ValueError:
                    pass
        search_from = start + 4


def check_shader_manifest_evidence(repo_root: Path) -> dict[str, Any]:
    """Return the recorded offline DXC evidence state from shaders/manifest.json."""
    manifest_path = repo_root / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shader = manifest.get("shaders", [{}])[0]
    verified = (
        manifest.get("verification_status") == "verified"
        and shader.get("compiled_offline_with_dxc") is True
        and shader.get("offline_dxil_bytes") is not None
        and shader.get("offline_dxil_sha256") is not None
    )
    return {
        "verified": verified,
        "manifest_verification_status": manifest.get("verification_status"),
        "compiled_offline_with_dxc": shader.get("compiled_offline_with_dxc"),
        "measured_dxc_version": shader.get("measured_dxc_version"),
        "offline_dxil_bytes": shader.get("offline_dxil_bytes"),
        "offline_dxil_sha256": shader.get("offline_dxil_sha256"),
    }


def record_shader_manifest_evidence(
    repo_root: Path,
    dxil_path: Path,
    dxc_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Record measured offline DXC output into shaders/manifest.json after validating the container."""
    manifest_path = repo_root / "shaders" / "manifest.json"
    hlsl_path = repo_root / "shaders" / "compute_fixture.hlsl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shader = manifest["shaders"][0]

    blob = dxil_path.read_bytes()
    info = validate_dxil_container(blob)
    hlsl_bytes = hlsl_path.read_bytes()

    shader["measured_dxc_version"] = (
        query_dxc_version(str(dxc_executable)) if dxc_executable is not None else "unknown"
    )
    shader["compiled_offline_with_dxc"] = True
    shader["hlsl_source_bytes"] = len(hlsl_bytes)
    shader["hlsl_sha256"] = hashlib.sha256(hlsl_bytes).hexdigest()
    shader["offline_dxil_bytes"] = info["size_bytes"]
    shader["offline_dxil_sha256"] = info["sha256"]
    if dxil_path.is_relative_to(repo_root):
        shader["dxil_build_output_path"] = dxil_path.relative_to(repo_root).as_posix()
        header_path = dxil_path.parent / "generated" / "compute_fixture_dxil.h"
        if header_path.is_relative_to(repo_root):
            shader["embedded_header_build_output_path"] = header_path.relative_to(repo_root).as_posix()
    manifest["verification_status"] = "verified"
    manifest.pop("unverified_reason", None)
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def evaluate_offline_shader(
    repo_root: Path,
    dxc_executable: str | None,
    search_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Validate HLSL constraints, locate a DXC-built DXIL container, and record measured evidence."""
    hlsl_path = repo_root / "shaders" / "compute_fixture.hlsl"
    hlsl_text = hlsl_path.read_text(encoding="utf-8")
    hlsl_violations = check_hlsl_source_constraints(hlsl_text)

    candidate_paths = [
        path / "shaders" / "compute_fixture.dxil"
        for path in (search_dirs or [])
    ]
    candidate_paths.extend(
        [
            repo_root / "build" / "repro-clean-a" / "shaders" / "compute_fixture.dxil",
            repo_root / "build" / "clang-cl-release" / "shaders" / "compute_fixture.dxil",
        ]
    )
    existing_dxil = next((p for p in candidate_paths if p.exists()), None)
    dxil_info: dict[str, Any] | None = None
    pinned_dxc = (
        json.loads((repo_root / "shaders" / "manifest.json").read_text(encoding="utf-8"))["shaders"][0]
        .get("pinned_dxc_version", "")
    )
    dxc_version_matches_pin = False
    if existing_dxil is not None and dxc_executable is not None:
        measured_version = query_dxc_version(str(dxc_executable))
        dxc_version_matches_pin = bool(pinned_dxc) and pinned_dxc in measured_version
        if dxc_version_matches_pin:
            dxil_info = validate_dxil_container(existing_dxil.read_bytes())
            # Record measured DXC output only after validating the real build artifact.
            record_shader_manifest_evidence(repo_root, existing_dxil, dxc_executable)

    evidence = check_shader_manifest_evidence(repo_root)
    passed = (
        not hlsl_violations
        and evidence["verified"]
        and dxil_info is not None
        and dxc_version_matches_pin
    )
    return {
        "passed": passed,
        "verification_status": "verified" if passed else "unverified",
        "hlsl_constraints_passed": not hlsl_violations,
        "dxil_validated": dxil_info is not None,
        "dxc_version_matches_pin": dxc_version_matches_pin,
        "compiled_offline_with_dxc": evidence["compiled_offline_with_dxc"],
        "measured_dxc_version": evidence["measured_dxc_version"],
        "dxil": dxil_info,
        "reason": (
            None
            if passed
            else "Offline DXC evidence is unverified; run on Windows 11 x64 with the pinned dxc "
            f"({pinned_dxc or 'unknown'}) and a validated build artifact to record shaders/manifest.json."
        ),
    }


def compare_two_build_directories(
    dir_a: Path,
    dir_b: Path,
    artifact_names: list[tuple[str, str]] | None = None,
    output_report_path: Path | None = None,
) -> dict[str, Any]:
    """Compare release PE32+ artifacts across two independent clean build directories and optionally record the report."""
    if artifact_names is None:
        artifact_names = [
            ("nw_empty_player.exe", "empty_player"),
            ("nw_clear_present_fixture.exe", "clear_present"),
            ("nw_compute_shader_fixture.exe", "compute_shader"),
        ]

    artifacts_report: list[dict[str, Any]] = []
    mismatches_found = 0
    missing_files: list[str] = []

    for exe_name, fixture_id in artifact_names:
        file_a = dir_a / exe_name
        file_b = dir_b / exe_name
        if not file_a.exists() or not file_b.exists():
            missing_files.append(exe_name)
            artifacts_report.append(
                {
                    "artifact_name": exe_name,
                    "fixture_id": fixture_id,
                    "size_bytes_a": None,
                    "size_bytes_b": None,
                    "sha256_a": None,
                    "sha256_b": None,
                    "hash_match": None,
                }
            )
            continue

        bytes_a = file_a.read_bytes()
        bytes_b = file_b.read_bytes()

        # Verify both files are valid PE32+ x64 images with /Brepro zero timestamp
        pe_a = parse_pe64_sections_and_imports(bytes_a)
        pe_b = parse_pe64_sections_and_imports(bytes_b)
        if pe_a["time_date_stamp"] != 0 or pe_b["time_date_stamp"] != 0:
            raise ValueError(
                f"PE32+ artifact '{exe_name}' violates /Brepro zero TimeDateStamp policy "
                f"(stamp_a={pe_a['time_date_stamp']}, stamp_b={pe_b['time_date_stamp']})"
            )

        hash_a = hashlib.sha256(bytes_a).hexdigest()
        hash_b = hashlib.sha256(bytes_b).hexdigest()
        matched = (len(bytes_a) == len(bytes_b)) and (hash_a == hash_b)
        if not matched:
            mismatches_found += 1
        artifacts_report.append(
            {
                "artifact_name": exe_name,
                "fixture_id": fixture_id,
                "size_bytes_a": len(bytes_a),
                "size_bytes_b": len(bytes_b),
                "sha256_a": hash_a,
                "sha256_b": hash_b,
                "hash_match": matched,
            }
        )

    all_match = (len(missing_files) == 0) and (mismatches_found == 0) and (len(artifacts_report) == len(artifact_names))
    if missing_files:
        verification_status = "unverified"
    elif mismatches_found > 0:
        verification_status = "mismatch_detected"
    else:
        verification_status = "verified"

    unavailable_measurements: list[dict[str, str]] = []
    if missing_files:
        unavailable_measurements = [
            {
                "metric": "artifacts[*].size_bytes_a",
                "reason": f"Missing release build artifacts ({', '.join(missing_files)}) in {dir_a} or {dir_b}.",
            },
            {
                "metric": "artifacts[*].sha256_a",
                "reason": f"Missing release build artifacts ({', '.join(missing_files)}); hashes are recorded as null rather than self-referential placeholders.",
            },
        ]

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "report_id": "nw01-clang-cl-release-reproducibility-v1",
        "toolchain_manifest_ref": "benchmarks/toolchain-manifest.v1.json",
        "build_profile": "release",
        "directory_a": str(dir_a),
        "directory_b": str(dir_b),
        "verification_status": verification_status,
        "all_artifacts_match": all_match,
        "missing_files": missing_files,
        "artifacts": artifacts_report,
        "mismatch_investigation": {
            "mismatches_found": mismatches_found,
            "root_causes_investigated": [
                "PE COFF header TimeDateStamp entropy mitigated via /Brepro on both clang-cl and lld-link.",
                "Embedded absolute build-directory PDB paths mitigated via /PDBALTPATH:%_PDB%.",
                "Compiler __FILE__ and debug prefix paths normalized via -ffile-prefix-map=${CMAKE_SOURCE_DIR}=.",
            ],
            "remediation_applied": (
                "All artifacts matched bit-for-bit across independent clean directories."
                if all_match
                else "Investigate section-by-section PE diffs or missing toolchain prerequisites before claiming reproducibility."
            ),
        },
        "unavailable_measurements": unavailable_measurements,
    }
    if verification_status == "unverified":
        report["unverified_reason"] = (
            f"Missing release build artifacts ({', '.join(missing_files)}); build twice on Windows 11 x64 with pinned clang-cl/lld-link/DXC."
        )

    if output_report_path is not None:
        output_report_path.parent.mkdir(parents=True, exist_ok=True)
        output_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    return report


def build_and_compare_two_clean_directories(
    repo_root: Path,
    dir_a: Path | None = None,
    dir_b: Path | None = None,
    output_report_path: Path | None = None,
    *,
    enforce_pinned_toolchain: bool = True,
) -> dict[str, Any]:
    """Invoke CMake + Ninja + clang-cl/lld-link + DXC in two clean directories and record the reproducibility report."""
    out_dir_a = dir_a or (repo_root / "build" / "repro-clean-a")
    out_dir_b = dir_b or (repo_root / "build" / "repro-clean-b")
    repro_path = output_report_path or (repo_root / "benchmarks" / "reproducibility" / "release-build-reproducibility.json")
    repro_schema = repo_root / "schemas" / "reproducibility-report.schema.json"

    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    clang_cl_bin = shutil.which("clang-cl") or shutil.which("clang-cl.exe")
    lld_link_bin = shutil.which("lld-link") or shutil.which("lld-link.exe")
    dxc_bin = find_dxc_executable()

    missing_tools = []
    if not cmake_bin:
        missing_tools.append("cmake")
    if not ninja_bin:
        missing_tools.append("ninja")
    if not clang_cl_bin:
        missing_tools.append("clang-cl")
    if not lld_link_bin:
        missing_tools.append("lld-link")
    if not dxc_bin:
        missing_tools.append("dxc")

    if missing_tools:
        # Never compare stale artifacts from an earlier local build when the pinned toolchain
        # is missing: remove the dedicated repro directories first so the report stays honest.
        for d in (out_dir_a, out_dir_b):
            if d.exists():
                shutil.rmtree(d)
        report = compare_two_build_directories(out_dir_a, out_dir_b, output_report_path=repro_path)
        report["missing_toolchain_tools"] = missing_tools
        if repro_schema.exists():
            validate_file_against_schema(repro_path, repro_schema)
        return report

    for d in (out_dir_a, out_dir_b):
        if d.exists():
            shutil.rmtree(d)
        subprocess.run(
            [
                cmake_bin,
                "-S",
                str(repo_root),
                "-B",
                str(d),
                "-G",
                "Ninja",
                f"-DCMAKE_TOOLCHAIN_FILE={repo_root / 'cmake/toolchains/clang-cl-x64.cmake'}",
                "-DCMAKE_BUILD_TYPE=MinSizeRel",
                "-DNW_BUILD_PROFILE=release",
                f"-DNW_ENFORCE_PINNED_TOOLCHAIN={'ON' if enforce_pinned_toolchain else 'OFF'}",
            ],
            check=True,
        )
        subprocess.run([cmake_bin, "--build", str(d)], check=True)

    report = compare_two_build_directories(out_dir_a, out_dir_b, output_report_path=repro_path)
    if repro_schema.exists():
        validate_file_against_schema(repro_path, repro_schema)
    return report


def probe_sdk_absence(
    search_dir: Path,
    env: dict[str, str] | None = None,
    system32_dir: Path | None = None,
) -> dict[str, bool]:
    """Actively probe the target machine and deployment directory to verify absence of developer SDKs and redistributables."""
    active_env = env if env is not None else dict(os.environ)
    sys32 = system32_dir or Path("C:/Windows/System32")

    win_sdk_dir = active_env.get("WindowsSdkDir", "")
    win_sdk_ver = active_env.get("WindowsSDKVersion", "")
    default_kits_include = Path("C:/Program Files (x86)/Windows Kits/10/Include")
    windows_sdk_absent = (
        not win_sdk_dir
        and not win_sdk_ver
        and shutil.which("fxc.exe") is None
        and not default_kits_include.exists()
    )

    d3d12_debug_layer_absent = (
        not (search_dir / "d3d12sdklayers.dll").exists()
        and not (search_dir / "d3d12SDKLayers.dll").exists()
        and not (sys32 / "d3d12sdklayers.dll").exists()
        and not (sys32 / "d3d12SDKLayers.dll").exists()
    )

    dxc_redist_absent = (
        not (search_dir / "dxcompiler.dll").exists()
        and not (search_dir / "dxil.dll").exists()
        and not (sys32 / "dxcompiler.dll").exists()
        and not (sys32 / "dxil.dll").exists()
        and shutil.which("dxc.exe") is None
    )

    agility_sdk_redist_absent = (
        not (search_dir / "D3D12Core.dll").exists()
        and not (search_dir / "d3d12core.dll").exists()
        and not (search_dir / "D3D12" / "D3D12Core.dll").exists()
    )

    msvc_debug_crt_absent = (
        not (search_dir / "ucrtbased.dll").exists()
        and not (search_dir / "vcruntime140d.dll").exists()
        and not (sys32 / "ucrtbased.dll").exists()
        and not (sys32 / "vcruntime140d.dll").exists()
    )

    return {
        "windows_sdk_absent": windows_sdk_absent,
        "d3d12_debug_layer_absent": d3d12_debug_layer_absent,
        "dxc_redist_absent": dxc_redist_absent,
        "agility_sdk_redist_absent": agility_sdk_redist_absent,
        "msvc_debug_crt_absent": msvc_debug_crt_absent,
    }


def check_evidence_completeness(repo_root: Path) -> dict[str, Any]:
    """Check whether all physical hardware, reproducibility, and clean-system evidence is verified."""
    unmet_requirements: list[str] = []

    shader_evidence = check_shader_manifest_evidence(repo_root)
    if not shader_evidence["verified"]:
        unmet_requirements.append(
            "Offline DXC shader evidence (shaders/manifest.json) is not verified "
            f"(verification_status={shader_evidence['manifest_verification_status']}, "
            f"compiled_offline_with_dxc={shader_evidence['compiled_offline_with_dxc']})"
        )

    hw_files = [
        ("rtx_4060_desktop", repo_root / "benchmarks" / "hardware" / "rtx-4060-baseline.json"),
        ("rtx_50_series_desktop", repo_root / "benchmarks" / "hardware" / "rtx-50-series-baseline.json"),
    ]
    for tier, hw_path in hw_files:
        data = json.loads(hw_path.read_text(encoding="utf-8"))
        if data.get("verification_status") != "verified" or data.get("benchmark_passed") is not True:
            unmet_requirements.append(
                f"Hardware baseline '{tier}' ({hw_path.name}) is '{data.get('verification_status')}' "
                f"(benchmark_passed={data.get('benchmark_passed')})"
            )

    repro_path = repo_root / "benchmarks" / "reproducibility" / "release-build-reproducibility.json"
    repro_data = json.loads(repro_path.read_text(encoding="utf-8"))
    if repro_data.get("verification_status") != "verified" or repro_data.get("all_artifacts_match") is not True:
        unmet_requirements.append(
            f"Reproducibility report ({repro_path.name}) is '{repro_data.get('verification_status')}' "
            f"(all_artifacts_match={repro_data.get('all_artifacts_match')})"
        )

    clean_path = repo_root / "benchmarks" / "clean-system" / "clean-system-validation.json"
    clean_data = json.loads(clean_path.read_text(encoding="utf-8"))
    if clean_data.get("verification_status") != "verified" or clean_data.get("clean_system_passed") is not True:
        unmet_requirements.append(
            f"Clean physical Windows 11 validation ({clean_path.name}) is '{clean_data.get('verification_status')}' "
            f"(clean_system_passed={clean_data.get('clean_system_passed')})"
        )

    return {
        "complete": len(unmet_requirements) == 0,
        "unmet_requirements": unmet_requirements,
    }


def execute_release_fixture_validation(
    repo_root: Path,
    binary_dir: Path,
    *,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Execute release fixtures with D3D12 validation and verify actual compute readback bytes."""
    expected = load_compute_expected_output(repo_root)
    specs = [
        ("empty_player", "nw_empty_player.exe", ["--no-validation"]),
        ("clear_present", "nw_clear_present_fixture.exe", ["--validation", "--frames", "2"]),
        (
            "compute_shader",
            "nw_compute_shader_fixture.exe",
            ["--validation", "--emit-readback-values"],
        ),
    ]

    runs: list[dict[str, Any]] = []
    all_passed = True
    compute_readback: dict[str, Any] | None = None

    for fixture_id, exe_name, extra_args in specs:
        exe_path = binary_dir / exe_name
        if not exe_path.exists():
            all_passed = False
            runs.append(
                {
                    "fixture_id": fixture_id,
                    "executable": exe_name,
                    "execution_status": "failed",
                    "exit_code": None,
                    "stdout_sha256": None,
                    "stderr_sha256": None,
                    "failure_reason": "release executable missing",
                }
            )
            continue
        try:
            proc = subprocess.run(
                [str(exe_path), *extra_args],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            ret_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            ret_code = None
            stdout = ""
            stderr = str(exc)

        passed = ret_code == 0
        run_record: dict[str, Any] = {
            "fixture_id": fixture_id,
            "executable": exe_name,
            "execution_status": "passed" if passed else "failed",
            "exit_code": ret_code,
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest() if stdout else None,
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest() if stderr else None,
        }

        if fixture_id == "compute_shader":
            compute_readback = parse_observed_compute_readback(stdout, expected)
            run_record["readback"] = compute_readback
            passed = (
                passed
                and compute_readback["elements_match"]
                and compute_readback.get("hash_match") is True
            )
            run_record["execution_status"] = "passed" if passed else "failed"

        if not passed:
            all_passed = False
            if stderr:
                run_record["failure_reason"] = stderr.strip().splitlines()[-1][:500]
        runs.append(run_record)

    return {
        "passed": all_passed,
        "binary_dir": str(binary_dir),
        "d3d12_validation_enabled": True,
        "fixture_runs": runs,
        "compute_readback": compute_readback,
    }


def run_full_mode(repo_root: Path) -> tuple[int, dict[str, Any]]:
    """Execute Mode=Full verification checks and record reproducibility evidence when built."""
    report: dict[str, Any] = {
        "mode": "Full",
        "host_os": platform.system(),
        "host_arch": platform.machine(),
        "checks": {},
        "missing_prerequisites": [],
        "unmet_evidence": [],
        "full_baseline_passed": False,
    }

    # 1. Schema validation
    schema_res = validate_all_repository_artifacts(repo_root)
    report["checks"]["schemas"] = {"passed": True, "validated_count": schema_res["validated_count"]}

    # 2. Offline shader source & manifest verification (re-evaluated after the Windows build below)
    dxc_bin = find_dxc_executable()
    offline_shader = evaluate_offline_shader(repo_root, dxc_bin)
    shader_ok = offline_shader["passed"]
    report["checks"]["offline_shader"] = offline_shader

    # 3. Dependency closure & size ledger checks
    dep_reports = verify_all_recorded_ledgers(repo_root)
    ledger_comparison = generate_size_comparison_report(repo_root)
    report["checks"]["dependency_closures"] = {"passed": True, "verified_ledgers": len(dep_reports)}
    report["checks"]["size_ledgers"] = {"passed": True, "comparison": ledger_comparison}

    # 4. Build & two-clean-directory reproducibility prerequisites check
    is_windows = platform.system().lower() == "windows"
    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    clang_cl_bin = shutil.which("clang-cl") or shutil.which("clang-cl.exe")
    lld_link_bin = shutil.which("lld-link") or shutil.which("lld-link.exe")

    if not is_windows:
        report["missing_prerequisites"].append("Windows 11 x64 OS host required for native D3D12 build and execution")
    if not cmake_bin:
        report["missing_prerequisites"].append("cmake (pinned 3.30.2) not found in PATH")
    if not ninja_bin:
        report["missing_prerequisites"].append("ninja (pinned 1.12.1) not found in PATH")
    if not clang_cl_bin:
        report["missing_prerequisites"].append("clang-cl (pinned 18.1.8) not found in PATH")
    if not lld_link_bin:
        report["missing_prerequisites"].append("lld-link (pinned 18.1.8) not found in PATH")
    if not dxc_bin:
        report["missing_prerequisites"].append("dxc (pinned 1.8.2407) not found in PATH")

    fixture_validation_passed = False
    if is_windows and cmake_bin and ninja_bin and clang_cl_bin and lld_link_bin and dxc_bin:
        try:
            repro_cmp = build_and_compare_two_clean_directories(repo_root)
            report["checks"]["reproducibility_build"] = {
                "passed": repro_cmp["all_artifacts_match"],
                "all_artifacts_match": repro_cmp["all_artifacts_match"],
                "recorded_to": "benchmarks/reproducibility/release-build-reproducibility.json",
            }
            if repro_cmp["all_artifacts_match"]:
                # The clean build is the authoritative source of offline DXC evidence.
                offline_shader = evaluate_offline_shader(
                    repo_root,
                    dxc_bin,
                    search_dirs=[repo_root / "build" / "repro-clean-a"],
                )
                shader_ok = offline_shader["passed"]
                report["checks"]["offline_shader"] = offline_shader
                fixture_validation = execute_release_fixture_validation(
                    repo_root, repo_root / "build" / "repro-clean-a"
                )
                fixture_validation_passed = fixture_validation["passed"]
                report["checks"]["fixture_validation"] = fixture_validation
                try:
                    ledger_recording = record_release_ledgers(
                        repo_root,
                        repo_root / "build" / "repro-clean-a",
                        deterministic_repro_verified=repro_cmp["all_artifacts_match"],
                    )
                    ledger_comparison = generate_size_comparison_report(repo_root)
                    report["checks"]["size_ledgers"] = {
                        "passed": True,
                        "recorded": ledger_recording,
                        "comparison": ledger_comparison,
                    }
                except Exception as exc:
                    report["checks"]["size_ledgers"] = {
                        "passed": False,
                        "reason": f"Measured size-ledger recording failed: {exc}",
                    }
            else:
                report["checks"]["fixture_validation"] = {
                    "passed": False,
                    "reason": "Two-clean-directory builds did not produce matching artifacts",
                }
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            report["checks"]["reproducibility_build"] = {
                "passed": False,
                "reason": f"Pinned Windows build failed: {exc}",
            }
            report["checks"]["fixture_validation"] = {
                "passed": False,
                "reason": "Skipped because the pinned Windows build failed",
            }
    else:
        report["checks"]["reproducibility_build"] = {
            "passed": False,
            "reason": "Skipped live Windows clang-cl/DXC build due to missing host prerequisites",
        }
        report["checks"]["fixture_validation"] = {
            "passed": False,
            "reason": "Skipped release fixture execution because host prerequisites are missing",
        }

    # 5. Evidence completeness check (RTX 4060, RTX 50-series, clean-system, reproducibility)
    evidence = check_evidence_completeness(repo_root)
    report["unmet_evidence"] = evidence["unmet_requirements"]
    report["checks"]["evidence_completeness"] = {"passed": evidence["complete"]}

    # Full baseline passes only when every prerequisite and measured evidence item is verified.
    if (
        not report["missing_prerequisites"]
        and evidence["complete"]
        and shader_ok
        and fixture_validation_passed
        and report["checks"]["size_ledgers"].get("passed") is True
        and report["checks"]["reproducibility_build"].get("passed") is True
    ):
        report["full_baseline_passed"] = True
        return 0, report

    return 2, report


def run_clean_system_mode(
    repo_root: Path,
    binary_dir: Path | None = None,
    *,
    allow_non_windows_simulation: bool = False,
    env: dict[str, str] | None = None,
    system32_dir: Path | None = None,
    executor: Callable[[Path, str], int | tuple[int, str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute Mode=CleanSystem validation on a target machine without developer SDKs and record the report."""
    clean_report_path = repo_root / "benchmarks" / "clean-system" / "clean-system-validation.json"
    clean_schema_path = repo_root / "schemas" / "clean-system-report.schema.json"
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))

    validate_file_against_schema(clean_report_path, clean_schema_path)
    existing_report = json.loads(clean_report_path.read_text(encoding="utf-8"))

    is_windows = (platform.system().lower() == "windows") or allow_non_windows_simulation
    search_dir = binary_dir or (repo_root / "build" / "clang-cl-release")
    required_exes = [
        ("empty_player", "nw_empty_player.exe"),
        ("clear_present", "nw_clear_present_fixture.exe"),
        ("compute_shader", "nw_compute_shader_fixture.exe"),
    ]

    missing_prereqs: list[str] = []
    if not is_windows:
        missing_prereqs.append("CleanSystem execution requires a physical Windows 11 x64 host")

    for _, exe_name in required_exes:
        if not (search_dir / exe_name).exists():
            missing_prereqs.append(f"Release fixture executable not found at {search_dir / exe_name}")

    if missing_prereqs:
        return 2, {
            "mode": "CleanSystem",
            "clean_system_passed": False,
            "verification_status": existing_report["verification_status"],
            "missing_prerequisites": missing_prereqs,
            "recorded_report_valid": True,
        }

    expected_compute = load_compute_expected_output(repo_root)
    shader_evidence = check_shader_manifest_evidence(repo_root)

    # 1. Actively probe SDK and redistributable absence on the target system
    sdk_checks = probe_sdk_absence(search_dir, env=env, system32_dir=system32_dir)
    all_sdk_absent = all(sdk_checks.values())

    # 2. Verify dependency closure and absence of forbidden sidecar DLLs in deployment folder
    sidecars = [
        p.name
        for p in search_dir.iterdir()
        if p.is_file()
        and p.name not in {e[1] for e in required_exes}
        and p.suffix.lower() in SIDECAR_SUFFIXES
    ]

    fixture_specs: dict[str, dict[str, Any]] = {}
    for fx_path in (repo_root / "fixtures").glob("*/fixture.json"):
        fx_data = json.loads(fx_path.read_text(encoding="utf-8"))
        fixture_specs[fx_data["fixture_id"]] = fx_data

    deployed_files: list[dict[str, Any]] = []
    resolved_inbox_dlls: set[str] = set()
    fixture_runs: list[dict[str, Any]] = []
    all_runs_passed = True
    compute_readback: dict[str, Any] | None = None
    embedded_dxil_verified = False
    closure_failures: list[str] = []

    for fixture_id, exe_name in required_exes:
        exe_path = search_dir / exe_name
        raw_bytes = exe_path.read_bytes()
        pe_info = parse_pe64_sections_and_imports(raw_bytes)
        if fixture_id == "compute_shader":
            embedded = find_embedded_dxil_container(raw_bytes)
            if embedded is not None:
                embedded_sha256 = hashlib.sha256(embedded).hexdigest()
                embedded_dxil_verified = (
                    shader_evidence["offline_dxil_sha256"] == embedded_sha256
                    and shader_evidence["offline_dxil_bytes"] == len(embedded)
                )
        expected_imports = fixture_specs.get(fixture_id, {}).get("expected_release_imports")
        try:
            verify_closure(
                profile="release",
                fixture_id=fixture_id,
                imported_dlls=pe_info["imports"],
                sidecar_files=sidecars,
                inventory=inventory,
                expected_fixture_release_imports=expected_imports,
            )
        except DependencyClosureError as exc:
            closure_failures.append(f"{fixture_id}: {exc}")
        for dll in pe_info["imports"]:
            resolved_inbox_dlls.add(dll.lower())

        deployed_files.append(
            {
                "file_name": exe_name,
                "size_bytes": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "category": "executable",
            }
        )

        run_args = [str(exe_path), "--no-validation"]
        if fixture_id == "compute_shader":
            run_args.append("--emit-readback-values")
        stdout = ""
        if executor is not None:
            result = executor(exe_path, fixture_id)
            if isinstance(result, tuple):
                ret_code, stdout = int(result[0]), str(result[1])
            else:
                ret_code = int(result)
        else:
            proc = subprocess.run(run_args, capture_output=True, text=True)
            ret_code = proc.returncode
            stdout = proc.stdout

        passed = ret_code == 0
        if fixture_id == "compute_shader":
            compute_readback = parse_observed_compute_readback(stdout, expected_compute)
            passed = passed and compute_readback["elements_match"] and compute_readback.get("hash_match") is True
        if not passed:
            all_runs_passed = False

        fixture_runs.append(
            {
                "fixture_id": fixture_id,
                "executable": exe_name,
                "execution_status": "passed" if passed else "failed",
                "exit_code": ret_code,
            }
        )

    total_distributed_bytes = sum(f["size_bytes"] for f in deployed_files)
    compute_output_verified = bool(
        compute_readback
        and compute_readback.get("elements_match") is True
        and compute_readback.get("hash_match") is True
    )
    clean_passed = (
        all_runs_passed
        and all_sdk_absent
        and (len(sidecars) == 0)
        and not closure_failures
        and shader_evidence["verified"]
        and embedded_dxil_verified
        and compute_output_verified
    )
    expected_sha256 = expected_compute["output_sha256"]

    deployed_and_imported = {name.lower() for name in resolved_inbox_dlls}
    deployed_and_imported.update(name.lower() for name in sidecars)
    runtime_dxcompiler_loaded = any("dxcompiler" in name or "dxil.dll" in name for name in deployed_and_imported)
    runtime_d3dcompiler_loaded = any("d3dcompiler" in name for name in deployed_and_imported)
    agility_redist_loaded = any("d3d12core" in name for name in deployed_and_imported)

    updated_report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "report_id": existing_report["report_id"],
        "verification_status": "verified" if clean_passed else "unverified",
        "clean_system_passed": clean_passed,
        "system_environment": existing_report["system_environment"],
        "sdk_absence_checks": sdk_checks if clean_passed else {k: None for k in sdk_checks},
        "deployed_dependency_inventory": {
            "deployed_files": (
                deployed_files
                if clean_passed
                else [
                    {"file_name": f["file_name"], "size_bytes": None, "sha256": None, "category": "executable"}
                    for f in deployed_files
                ]
            ),
            "total_distributed_bytes": total_distributed_bytes if clean_passed else None,
            "inbox_dlls_resolved": sorted(resolved_inbox_dlls),
            "unexpected_sidecars_found": sidecars if not clean_passed else [],
        },
        "offline_shader_execution_evidence": {
            "shader_id": "compute_fixture_cs",
            "shader_model": "cs_6_0",
            "compiled_offline_with_dxc": True if (clean_passed and shader_evidence["verified"]) else None,
            "runtime_dxcompiler_loaded": (
                runtime_dxcompiler_loaded if clean_passed else None
            ),
            "runtime_d3dcompiler_loaded": (
                runtime_d3dcompiler_loaded if clean_passed else None
            ),
            "agility_d3d12core_redist_loaded": (
                agility_redist_loaded if clean_passed else None
            ),
            "expected_output_sha256": expected_sha256,
            "observed_output_sha256": (
                compute_readback["observed_output_sha256"]
                if (compute_output_verified and compute_readback)
                else None
            ),
            "output_verified": True if (clean_passed and compute_output_verified) else None,
        },
        "fixture_runs": (
            fixture_runs
            if clean_passed
            else [
                {
                    "fixture_id": r["fixture_id"],
                    "executable": r["executable"],
                    "execution_status": "unverified",
                    "exit_code": None,
                }
                for r in fixture_runs
            ]
        ),
        "unavailable_measurements": (
            []
            if clean_passed
            else existing_report.get("unavailable_measurements", [])
        ),
    }
    if not clean_passed:
        reasons = []
        if not all_runs_passed:
            reasons.append("one or more fixtures failed")
        if not all_sdk_absent:
            reasons.append("developer SDK/redistributable absence checks failed")
        if sidecars:
            reasons.append("unexpected sidecar files were present")
        if closure_failures:
            reasons.append("deployment dependency closure failed: " + " | ".join(closure_failures))
        if not shader_evidence["verified"]:
            reasons.append("offline DXC shader evidence in shaders/manifest.json is unverified")
        if not embedded_dxil_verified:
            reasons.append(
                "deployed compute executable does not embed the validated, manifest-matched DXIL container"
            )
        if not compute_output_verified:
            reasons.append("observed compute readback did not match the committed expected bytes")
        updated_report["unverified_reason"] = (
            "CleanSystem validation failed: " + "; ".join(reasons) if reasons else
            "CleanSystem validation failed; see command summary."
        )

    clean_report_path.write_text(json.dumps(updated_report, indent=2) + "\n", encoding="utf-8")
    validate_file_against_schema(clean_report_path, clean_schema_path)

    return (0 if clean_passed else 1), {
        "mode": "CleanSystem",
        "clean_system_passed": clean_passed,
        "verification_status": updated_report["verification_status"],
        "sdk_absence_checks": sdk_checks,
        "embedded_dxil_verified": embedded_dxil_verified,
        "dependency_closure_failures": closure_failures,
        "total_distributed_bytes": total_distributed_bytes,
        "fixture_runs": fixture_runs,
        "recorded_to": "benchmarks/clean-system/clean-system-validation.json",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nodewerk NW-01 verification entry point.")
    parser.add_argument(
        "--mode",
        choices=["Full", "CleanSystem", "ContractsOnly"],
        default="Full",
        help="Verification mode: Full, CleanSystem, or ContractsOnly",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--binary-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.mode == "CleanSystem":
            code, summary = run_clean_system_mode(args.repo_root, args.binary_dir)
        elif args.mode == "ContractsOnly":
            schema_res = validate_all_repository_artifacts(args.repo_root)
            dep_res = verify_all_recorded_ledgers(args.repo_root)
            ledger_res = generate_size_comparison_report(args.repo_root)
            code = 0
            summary = {
                "mode": "ContractsOnly",
                "schemas_validated": schema_res["validated_count"],
                "ledgers_verified": len(dep_res),
                "selected_toolchain": ledger_res["selected_toolchain"],
            }
        else:
            code, summary = run_full_mode(args.repo_root)

        print(json.dumps(summary, indent=2))
        return code
    except Exception as exc:
        print(f"[NW-VERIFY-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
