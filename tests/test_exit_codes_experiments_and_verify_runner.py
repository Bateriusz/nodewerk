from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from test_dependency_closure_and_accounting import build_synthetic_pe64  # noqa: E402
from verify_nw01 import (  # noqa: E402
    build_and_compare_two_clean_directories,
    check_evidence_completeness,
    compare_two_build_directories,
    probe_sdk_absence,
    run_clean_system_mode,
    run_full_mode,
)


def test_exit_codes_and_d3d12_bootstrap_safety_contracts() -> None:
    exit_codes_h = (REPO_ROOT / "src" / "player" / "exit_codes.h").read_text(encoding="utf-8")
    diagnostics_cpp = (REPO_ROOT / "src" / "player" / "diagnostics.cpp").read_text(encoding="utf-8")
    bootstrap_cpp = (REPO_ROOT / "src" / "player" / "d3d12_bootstrap.cpp").read_text(encoding="utf-8")
    exit_docs = (REPO_ROOT / "docs" / "exit-codes-and-diagnostics.md").read_text(encoding="utf-8")

    matches = re.findall(r"^\s*([A-Za-z0-9_]+)\s*=\s*([0-9]+),", exit_codes_h, flags=re.MULTILINE)
    assert len(matches) >= 20, "Expected at least 20 distinct initialization/execution exit codes"

    seen_names: set[str] = set()
    seen_values: set[int] = set()
    for name, val_str in matches:
        val = int(val_str)
        assert name not in seen_names, f"Duplicate exit code name {name}"
        assert val not in seen_values, f"Duplicate exit code numeric value {val} for {name}"
        seen_names.add(name)
        seen_values.add(val)
        assert f"ExitCode::{name}" in diagnostics_cpp, f"{name} must be registered in TryParseSimulatedFailure"
        assert name in exit_docs, f"{name} must be documented in docs/exit-codes-and-diagnostics.md"

    # Verify swapchain QueryInterface(IDXGISwapChain3) checks HRESULT and null pointer before dereferencing
    assert 'swapchain1->QueryInterface(__uuidof(IDXGISwapChain3)' in bootstrap_cpp
    assert '"IDXGISwapChain1::QueryInterface(IDXGISwapChain3) failed"' in bootstrap_cpp

    # Verify ExecuteEmptyPlayerBootstrap queries real OS build (RtlGetVersion) and adapter on _WIN32
    assert "RtlGetVersion" in bootstrap_cpp
    assert 'LoadLibraryExA("dxgi.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32)' in bootstrap_cpp
    assert 'LoadLibraryExA("d3d12.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32)' in bootstrap_cpp
    assert "PopulateAdapterCapabilitySnapshot(device, win_build" in bootstrap_cpp


def test_preserved_experiments_retain_reproducible_commands_and_honest_unverified_state() -> None:
    exp_dir = REPO_ROOT / "benchmarks" / "experiments"
    expected_experiments = {
        "msvc-ltcg-comparison.json": ("toolchain_comparison", "rejected_preserved"),
        "compact-profile-experiment.json": ("compact_profile", "failed_preserved"),
        "crt-elimination-experiment.json": ("crt_elimination", "failed_preserved"),
    }
    for filename, (expected_cat, expected_status) in expected_experiments.items():
        record = json.loads((exp_dir / filename).read_text(encoding="utf-8"))
        assert record["category"] == expected_cat
        assert record["status"] == expected_status
        assert record["verification_status"] == "unverified"
        assert record["substituted_silently"] is False
        assert "cmake --preset" in record["reproducible_command"]
        assert record["configuration"]["security_mitigations_retained"] is True
        assert len(record["initialization_and_cleanup_obligations"]) >= 1
        assert record["measurements"]["total_distributed_bytes"] is None
        assert record["measurements"]["delta_vs_release_baseline_bytes"] is None
        assert record["diagnostics"]["exit_code"] is None
        assert len(record["unavailable_measurements"]) >= 2

    # Ensure compact-profile-experiment does NOT map 0xC000007B to ExitCode::OsVersionUnsupported (11)
    compact_rec = json.loads((exp_dir / "compact-profile-experiment.json").read_text(encoding="utf-8"))
    assert compact_rec["diagnostics"]["exit_code"] != 11
    assert "NW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT=ON" in compact_rec["reproducible_command"]


def test_reproducibility_comparison_and_pinned_toolchain_build_runner(tmp_path: Path) -> None:
    dir_a = tmp_path / "repro-clean-a"
    dir_b = tmp_path / "repro-clean-b"
    dir_a.mkdir()
    dir_b.mkdir()
    repro_out = tmp_path / "release-build-reproducibility.json"

    artifacts = [
        ("nw_empty_player.exe", build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"])),
        (
            "nw_clear_present_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]),
        ),
        (
            "nw_compute_shader_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]),
        ),
    ]
    for name, payload in artifacts:
        (dir_a / name).write_bytes(payload)
        (dir_b / name).write_bytes(payload)

    match_report = compare_two_build_directories(dir_a, dir_b, output_report_path=repro_out)
    assert match_report["all_artifacts_match"] is True
    assert match_report["verification_status"] == "verified"
    assert repro_out.exists()
    assert json.loads(repro_out.read_text(encoding="utf-8"))["all_artifacts_match"] is True

    # Verify non-zero TimeDateStamp (violating /Brepro) is rejected
    (dir_b / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"], time_date_stamp=12345)
    )
    with pytest.raises(ValueError, match="TimeDateStamp"):
        compare_two_build_directories(dir_a, dir_b)

    # Restore valid PE with different section content in dir_b to verify mismatch detection
    (dir_b / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"], extra_text_bytes=512)
    )
    mismatch_report = compare_two_build_directories(dir_a, dir_b, output_report_path=repro_out)
    assert mismatch_report["all_artifacts_match"] is False
    assert mismatch_report["verification_status"] == "mismatch_detected"
    assert mismatch_report["mismatch_investigation"]["mismatches_found"] == 1

    # Invoke build_and_compare_two_clean_directories: on Linux without clang-cl/dxc, it must record unverified
    live_repro_out = tmp_path / "live-repro.json"
    live_report = build_and_compare_two_clean_directories(
        REPO_ROOT,
        tmp_path / "live-a",
        tmp_path / "live-b",
        output_report_path=live_repro_out,
    )
    assert live_repro_out.exists()
    if "missing_toolchain_tools" in live_report:
        assert live_report["verification_status"] == "unverified"
        assert live_report["all_artifacts_match"] is False
    else:
        assert live_report["verification_status"] == "verified"
        assert live_report["all_artifacts_match"] is True


def test_verify_nw01_records_clean_system_evidence_and_gates_full_mode(tmp_path: Path) -> None:
    ps1_path = REPO_ROOT / "tools" / "verify-nw01.ps1"
    assert ps1_path.exists()
    ps1_text = ps1_path.read_text(encoding="utf-8")
    assert ".SYNOPSIS" in ps1_text
    assert '[ValidateSet("Full", "CleanSystem")]' in ps1_text
    assert "verify_nw01.py" in ps1_text

    # On the unverified repository checkout, all 4 physical/toolchain evidence items are unmet
    evidence = check_evidence_completeness(REPO_ROOT)
    assert evidence["complete"] is False
    assert len(evidence["unmet_requirements"]) == 4

    full_code, full_summary = run_full_mode(REPO_ROOT)
    assert full_code == 2
    assert full_summary["full_baseline_passed"] is False
    assert full_summary["checks"]["schemas"]["passed"] is True
    assert full_summary["checks"]["offline_shader"]["passed"] is True
    assert full_summary["checks"]["dependency_closures"]["passed"] is True
    assert full_summary["checks"]["size_ledgers"]["passed"] is True

    # Test active SDK-absence probing and CleanSystem recording in an isolated repo copy
    sim_repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, sim_repo / subdir)

    bin_dir = sim_repo / "build" / "clang-cl-release"
    bin_dir.mkdir(parents=True)
    (bin_dir / "nw_empty_player.exe").write_bytes(build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"]))
    (bin_dir / "nw_clear_present_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"])
    )
    (bin_dir / "nw_compute_shader_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"])
    )

    clean_sys32 = tmp_path / "System32"
    clean_sys32.mkdir()
    clean_env: dict[str, str] = {}

    sdk_checks = probe_sdk_absence(bin_dir, env=clean_env, system32_dir=clean_sys32)
    assert all(sdk_checks.values())

    # If WindowsSdkDir is set or d3d12sdklayers.dll is present, probe_sdk_absence detects it
    dirty_checks = probe_sdk_absence(
        bin_dir,
        env={"WindowsSdkDir": "C:/Program Files (x86)/Windows Kits/10/"},
        system32_dir=clean_sys32,
    )
    assert dirty_checks["windows_sdk_absent"] is False

    clean_code, clean_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        executor=lambda _exe, _fx: 0,
    )
    assert clean_code == 0
    assert clean_summary["clean_system_passed"] is True
    assert clean_summary["verification_status"] == "verified"

    recorded_clean = json.loads(
        (sim_repo / "benchmarks" / "clean-system" / "clean-system-validation.json").read_text(encoding="utf-8")
    )
    assert recorded_clean["verification_status"] == "verified"
    assert recorded_clean["clean_system_passed"] is True
    assert all(recorded_clean["sdk_absence_checks"].values())
    assert recorded_clean["offline_shader_execution_evidence"]["output_verified"] is True
