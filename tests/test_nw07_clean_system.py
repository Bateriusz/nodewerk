from __future__ import annotations

import copy
import hashlib
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


def test_complete_clean_system_report_is_verified(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    executable = deployment / "nw_common_release.exe"
    executable.write_bytes(b"MZ")
    report = _report()
    report["deployment"] = {
        "executable": executable.name,
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "executable_bytes": executable.stat().st_size,
        "deployment_files": [executable.name],
        "sidecars": [],
        "only_runtime_executable": True,
        "development_dependencies_present": False,
        "pe_imports": [],
        "forbidden_runtime_dependencies": [],
    }
    result = clean_system.validate_report(
        report, deployment_directory=deployment
    )
    assert result["status"] == "verified"


def test_missing_scenario_stays_pending() -> None:
    report = _report()
    report["scenarios"].pop()
    assert clean_system.validate_report(report)["status"] == "pending_unverified"


def test_unexecuted_scenario_cannot_be_reported_as_verified() -> None:
    report = _report()
    report["scenarios"][0]["executed"] = False
    report["scenarios"][0]["observed_exit_code"] = None
    report["scenarios"][0]["status"] = "not_executed"
    assert clean_system.validate_report(report)["status"] == "pending_unverified"


def test_scenario_cannot_redefine_the_frozen_expected_exit_code() -> None:
    report = _report()
    report["scenarios"][0]["expected_exit_code"] = 0
    report["scenarios"][0]["observed_exit_code"] = 0
    report["scenarios"][0]["status"] = "passed"
    assert clean_system.validate_report(report)["status"] == "failed"


def test_executed_scenario_must_record_passed_status() -> None:
    report = _report()
    report["scenarios"][1]["status"] = "not_executed"
    assert clean_system.validate_report(report)["status"] == "failed"


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


def test_report_without_actual_deployment_cannot_verify() -> None:
    assert (
        clean_system.validate_report(_report())["status"]
        == "pending_unverified"
    )


def test_retained_report_must_bind_the_actual_deployment(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    executable = deployment / "nw_common_release.exe"
    executable.write_bytes(b"MZ")
    identity = {
        "executable": executable.name,
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "executable_bytes": executable.stat().st_size,
        "deployment_files": [executable.name],
        "sidecars": [],
        "only_runtime_executable": True,
        "development_dependencies_present": False,
        "pe_imports": [],
        "forbidden_runtime_dependencies": [],
    }
    report = _report()
    report["deployment"] = dict(identity)
    assert (
        clean_system.validate_report(
            report, deployment_directory=deployment
        )["status"]
        == "verified"
    )
    executable.write_bytes(b"MZ-stale")
    assert (
        clean_system.validate_report(
            report, deployment_directory=deployment
        )["status"]
        == "failed"
    )
    executable.write_bytes(b"MZ")
    (deployment / "unexpected.dll").write_bytes(b"sidecar")
    assert (
        clean_system.validate_report(
            report, deployment_directory=deployment
        )["status"]
        == "failed"
    )


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


def test_corrupt_deployment_copy_only_changes_the_embedded_package(
    tmp_path: Path,
) -> None:
    template = b"MZ" + b"\x00" * 64
    package = b"PK\x03\x04" + b"package-payload" * 4
    manifest = b"{}"
    executable = clean_system.export.nw05_export.embed_package(
        template, package, manifest
    )
    source = tmp_path / "nw_common_release.exe"
    destination = tmp_path / "nw_common_release-corrupt.exe"
    source.write_bytes(executable)
    clean_system._write_corrupt_embedded_copy(source, destination)
    corrupted = destination.read_bytes()
    assert corrupted[: len(template)] == template
    assert corrupted != executable
    with pytest.raises(clean_system.export.nw05_export.ExportError):
        clean_system.export.nw05_export.extract_embedded_package(corrupted)
