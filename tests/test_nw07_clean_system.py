from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_clean_system as clean_system  # noqa: E402
import nw07_contract as contract  # noqa: E402


def _report() -> dict:
    return {
        "format": "nw07-clean-system-report",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "deployment": {
            "only_runtime_executable": True,
            "development_dependencies_present": False,
        },
        "scenarios": [
            {
                "scenario": name,
                "executed": True,
                "expected_exit_code": expected,
                "observed_exit_code": expected,
                "status": "passed",
            }
            for name, expected in (
                ("corrupt_payload", 70),
                ("missing_required_feature", 23),
                ("initialization_failure", 42),
                ("resize", 0),
                ("device_removal", 51),
            )
        ],
        "verification_status": "verified",
        "policy": "Executed failures remain failed.",
    }


def test_complete_clean_system_report_is_verified() -> None:
    result = clean_system.validate_report(_report())
    assert result["status"] == "verified"


def test_missing_scenario_stays_pending() -> None:
    report = _report()
    report["scenarios"].pop()
    assert clean_system.validate_report(report)["status"] == "pending_unverified"


def test_executed_scenario_failure_remains_failed() -> None:
    report = _report()
    report["scenarios"][0]["observed_exit_code"] = 0
    report["scenarios"][0]["status"] = "failed"
    assert clean_system.validate_report(report)["status"] == "failed"


def test_deployment_sidecar_or_dev_dependency_fails() -> None:
    report = _report()
    report["deployment"]["only_runtime_executable"] = False
    assert clean_system.validate_report(report)["status"] == "failed"
    report = _report()
    report["deployment"]["development_dependencies_present"] = True
    assert clean_system.validate_report(report)["status"] == "failed"


def test_pending_report_is_not_verified() -> None:
    report = clean_system.pending_report("not on Windows")
    assert clean_system.validate_report(report)["status"] == "pending_unverified"


def test_required_scenarios_and_exit_codes_are_frozen() -> None:
    assert clean_system.REQUIRED_SCENARIOS == (
        "corrupt_payload",
        "missing_required_feature",
        "initialization_failure",
        "resize",
        "device_removal",
    )
    report = _report()
    expected = {
        row["scenario"]: row["expected_exit_code"] for row in report["scenarios"]
    }
    assert expected == {
        "corrupt_payload": 70,
        "missing_required_feature": 23,
        "initialization_failure": 42,
        "resize": 0,
        "device_removal": 51,
    }


def test_clean_system_cli_records_pending_without_prerequisites(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    code = clean_system.main(
        [
            "--fixture",
            str(tmp_path / "fixture.exe"),
            "--package",
            str(tmp_path / "package"),
            "--deployment",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )
    # Missing prerequisites (dummy paths on any host) must remain pending, not
    # crash and not become passing evidence.
    assert code == 2
    assert output.is_file()


def test_clean_system_cli_does_not_crash_on_windows_without_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    output = tmp_path / "report.json"
    code = clean_system.main(
        [
            "--fixture",
            str(tmp_path / "fixture.exe"),
            "--package",
            str(tmp_path / "package"),
            "--deployment",
            str(tmp_path / "deployment"),
            "--output",
            str(output),
        ]
    )
    assert code == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["verification_status"] == "pending_unverified"
    assert "prerequisites" in report["reason"]
