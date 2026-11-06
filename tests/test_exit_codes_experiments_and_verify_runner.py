from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from verify_nw01 import (  # noqa: E402
    check_evidence_completeness,
    compare_two_build_directories,
    run_clean_system_mode,
    run_full_mode,
)


def test_exit_codes_are_distinct_and_documented() -> None:
    exit_codes_h = (REPO_ROOT / "src" / "player" / "exit_codes.h").read_text(encoding="utf-8")
    diagnostics_cpp = (REPO_ROOT / "src" / "player" / "diagnostics.cpp").read_text(encoding="utf-8")
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

    # Verify InboxRuntimeBlocker recovery semantics
    assert "InboxRuntimeBlocker" in seen_names
    assert "INBOX_RUNTIME_BLOCKER" in diagnostics_cpp
    assert "total_distributed_bytes" in exit_codes_h


def test_preserved_experiments_retain_measurements_and_diagnostics() -> None:
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
        assert record["substituted_silently"] is False
        assert record["configuration"]["security_mitigations_retained"] is True
        assert len(record["initialization_and_cleanup_obligations"]) >= 1
        assert record["measurements"]["total_distributed_bytes"] > 0
        assert len(record["diagnostics"]["summary"]) > 10
        assert len(record["diagnostics"]["preservation_statement"]) > 10


def test_two_clean_directory_reproducibility_comparison(tmp_path: Path) -> None:
    dir_a = tmp_path / "repro-clean-a"
    dir_b = tmp_path / "repro-clean-b"
    dir_a.mkdir()
    dir_b.mkdir()

    artifacts = [
        ("nw_empty_player.exe", b"MZ-deterministic-empty-player"),
        ("nw_clear_present_fixture.exe", b"MZ-deterministic-clear-present"),
        ("nw_compute_shader_fixture.exe", b"MZ-deterministic-compute-shader"),
    ]
    for name, payload in artifacts:
        (dir_a / name).write_bytes(payload)
        (dir_b / name).write_bytes(payload)

    match_report = compare_two_build_directories(dir_a, dir_b)
    assert match_report["all_artifacts_match"] is True
    assert match_report["verification_status"] == "verified"
    assert match_report["mismatch_investigation"]["mismatches_found"] == 0

    # Introduce a 1-byte timestamp/path drift in dir_b and verify mismatch detection
    (dir_b / "nw_clear_present_fixture.exe").write_bytes(b"MZ-deterministic-clear-presenX")
    mismatch_report = compare_two_build_directories(dir_a, dir_b)
    assert mismatch_report["all_artifacts_match"] is False
    assert mismatch_report["verification_status"] == "mismatch_detected"
    assert mismatch_report["mismatch_investigation"]["mismatches_found"] == 1


def test_verify_nw01_modes_and_unverified_hardware_gate() -> None:
    ps1_path = REPO_ROOT / "tools" / "verify-nw01.ps1"
    assert ps1_path.exists()
    ps1_text = ps1_path.read_text(encoding="utf-8")
    assert ".SYNOPSIS" in ps1_text
    assert '[ValidateSet("Full", "CleanSystem")]' in ps1_text
    assert "verify_nw01.py" in ps1_text

    # Evidence completeness must report unmet physical RTX 4060, RTX 50-series, and clean-system runs
    evidence = check_evidence_completeness(REPO_ROOT)
    assert evidence["complete"] is False
    assert len(evidence["unmet_requirements"]) == 4

    # Full mode must validate schemas/ledgers/shaders (passed=True) but return non-zero exit code (2)
    # because physical Windows 11 hardware evidence is unverified.
    full_code, full_summary = run_full_mode(REPO_ROOT)
    assert full_code != 0
    assert full_summary["full_baseline_passed"] is False
    assert full_summary["checks"]["schemas"]["passed"] is True
    assert full_summary["checks"]["offline_shader"]["passed"] is True
    assert full_summary["checks"]["dependency_closures"]["passed"] is True
    assert full_summary["checks"]["size_ledgers"]["passed"] is True

    # CleanSystem mode must also refuse to report a false-positive pass when prerequisites/binaries are absent
    clean_code, clean_summary = run_clean_system_mode(REPO_ROOT)
    assert clean_code != 0
    assert clean_summary["clean_system_passed"] is False
    assert clean_summary["recorded_report_valid"] is True
