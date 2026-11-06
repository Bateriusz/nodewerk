#!/usr/bin/env python3
"""Nodewerk NW-01 Verification Runner backing tools/verify-nw01.ps1.

Supports two documented modes:
- Full:
    Performs build, schema, dependency, reproducibility, fixture-validation,
    and evidence-completeness checks. Missing prerequisites or unverified
    physical hardware/clean-system evidence NEVER produce a passing Full result.
- CleanSystem:
    Executes and records release-fixture validation on a target clean physical
    Windows 11 x64 machine without requiring developer SDKs, DXC, or Agility SDK.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from dependency_check import parse_pe64_sections_and_imports, verify_all_recorded_ledgers, verify_closure
from offline_shader_pack import check_hlsl_source_constraints, validate_dxil_container
from schema_validator import validate_all_repository_artifacts, validate_file_against_schema
from size_ledger import generate_size_comparison_report


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_two_build_directories(
    dir_a: Path,
    dir_b: Path,
    artifact_names: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare release artifacts across two independent clean build directories."""
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
            continue
        bytes_a = file_a.read_bytes()
        bytes_b = file_b.read_bytes()
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
    verification_status = "verified" if all_match else ("unverified" if missing_files else "mismatch_detected")

    return {
        "schema_version": "1.0.0",
        "report_id": "nw01-clang-cl-release-reproducibility-v1",
        "toolchain_manifest_ref": "benchmarks/toolchain-manifest.v1.json",
        "build_profile": "release",
        "directory_a": str(dir_a),
        "directory_b": str(dir_b),
        "verification_status": verification_status,
        "all_artifacts_match": all_match,
        "artifacts": artifacts_report,
        "missing_files": missing_files,
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
                else "Investigate section-by-section PE diffs before claiming reproducibility."
            ),
        },
    }


def check_evidence_completeness(repo_root: Path) -> dict[str, Any]:
    """Check whether all physical hardware, reproducibility, and clean-system evidence is verified."""
    unmet_requirements: list[str] = []

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


def run_full_mode(repo_root: Path) -> tuple[int, dict[str, Any]]:
    """Execute Mode=Full verification checks."""
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

    # 2. Offline shader verification
    hlsl_text = (repo_root / "shaders" / "compute_fixture.hlsl").read_text(encoding="utf-8")
    hlsl_violations = check_hlsl_source_constraints(hlsl_text)
    if hlsl_violations:
        raise RuntimeError(f"HLSL source constraint violations: {hlsl_violations}")
    dxil_blob = (repo_root / "shaders" / "compute_fixture.dxil").read_bytes()
    dxil_info = validate_dxil_container(dxil_blob)
    report["checks"]["offline_shader"] = {"passed": True, "dxil": dxil_info}

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

    if not is_windows:
        report["missing_prerequisites"].append("Windows 11 x64 OS host required for native D3D12 build and execution")
    if not cmake_bin:
        report["missing_prerequisites"].append("cmake (>= 3.28) not found in PATH")
    if not ninja_bin:
        report["missing_prerequisites"].append("ninja not found in PATH")
    if not clang_cl_bin:
        report["missing_prerequisites"].append("clang-cl (18.1.8) not found in PATH")

    if is_windows and cmake_bin and ninja_bin and clang_cl_bin:
        dir_a = repo_root / "build" / "repro-clean-a"
        dir_b = repo_root / "build" / "repro-clean-b"
        for d in (dir_a, dir_b):
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
                ],
                check=True,
            )
            subprocess.run([cmake_bin, "--build", str(d)], check=True)

        repro_cmp = compare_two_build_directories(dir_a, dir_b)
        report["checks"]["reproducibility_build"] = {
            "passed": repro_cmp["all_artifacts_match"],
            "all_artifacts_match": repro_cmp["all_artifacts_match"],
        }
    else:
        report["checks"]["reproducibility_build"] = {
            "passed": False,
            "reason": "Skipped live Windows clang-cl build due to missing host prerequisites",
        }

    # 5. Evidence completeness check (RTX 4060, RTX 50-series, clean-system, reproducibility)
    evidence = check_evidence_completeness(repo_root)
    report["unmet_evidence"] = evidence["unmet_requirements"]
    report["checks"]["evidence_completeness"] = {"passed": evidence["complete"]}

    # Full baseline passes ONLY if there are zero missing prerequisites AND all hardware/clean-system evidence is verified
    if not report["missing_prerequisites"] and evidence["complete"]:
        report["full_baseline_passed"] = True
        return 0, report

    return 2, report


def run_clean_system_mode(repo_root: Path, binary_dir: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Execute Mode=CleanSystem validation on a target machine without developer SDKs."""
    clean_report_path = repo_root / "benchmarks" / "clean-system" / "clean-system-validation.json"
    clean_schema_path = repo_root / "schemas" / "clean-system-report.schema.json"
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))

    validate_file_against_schema(clean_report_path, clean_schema_path)
    existing_report = json.loads(clean_report_path.read_text(encoding="utf-8"))

    is_windows = platform.system().lower() == "windows"
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

    # Verify dependency closure and absence of forbidden sidecar DLLs in deployment folder
    sidecars = [
        p.name
        for p in search_dir.iterdir()
        if p.is_file()
        and p.name not in {e[1] for e in required_exes}
        and p.suffix.lower() in (".dll", ".pdb", ".dxil", ".cso")
    ]

    fixture_runs = []
    all_passed = True
    for fixture_id, exe_name in required_exes:
        exe_path = search_dir / exe_name
        pe_info = parse_pe64_sections_and_imports(exe_path.read_bytes())
        verify_closure(
            profile="release",
            fixture_id=fixture_id,
            imported_dlls=pe_info["imports"],
            sidecar_files=sidecars,
            inventory=inventory,
        )
        proc = subprocess.run([str(exe_path), "--no-validation"], capture_output=True, text=True)
        passed = proc.returncode == 0
        if not passed:
            all_passed = False
        fixture_runs.append(
            {
                "fixture_id": fixture_id,
                "executable": exe_name,
                "execution_status": "passed" if passed else "failed",
                "exit_code": proc.returncode,
            }
        )

    return (0 if all_passed else 1), {
        "mode": "CleanSystem",
        "clean_system_passed": all_passed,
        "verification_status": "verified" if all_passed else "unverified",
        "fixture_runs": fixture_runs,
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
