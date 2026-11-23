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
        "host": {
            "execution_kind": "physical",
            "passthrough_documentation": "",
            "virtualization_probe": {
                "available": True,
                "manufacturer": "ASUSTeK COMPUTER INC.",
                "model": "Physical Desktop",
                "hypervisor_present": False,
                "error": "",
            },
            "virtualization_indicators": [],
            "physical_host_evidence": True,
            "platform": "Windows",
            "release": "11",
            "os_build": "10.0.26100.2314",
            "windows_11_or_newer": True,
            "architecture": "AMD64",
            "x64": True,
            "development_dependency_checks": {
                "nodewerkc": None,
                "dxc": None,
                "cmake": None,
            },
            "development_dependencies_present_on_path": [],
            "environment_cache_variables": [],
            "clean_environment": True,
            "application_pipeline_cache_present": False,
        },
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
                ("deployed_startup", 0),
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
        "deployed_startup",
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
        "deployed_startup": 0,
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
            "--execution-kind",
            "physical",
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
            "--execution-kind",
            "physical",
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


def test_timeout_is_an_executed_failure_not_unavailable_evidence() -> None:
    row = clean_system._scenario_row(
        "deployed_startup",
        command=["player.exe", "--frames", "1"],
        expected_exit_code=0,
        completed=clean_system.subprocess.CompletedProcess(
            ["player.exe"], returncode=None, stdout="partial", stderr="timeout"
        ),
        stdout_path=None,
        stderr_path=None,
        timed_out=True,
    )
    assert row["executed"] is True
    assert row["status"] == "failed"
    report = _report()
    report["scenarios"][1].update(
        executed=True,
        observed_exit_code=None,
        status="failed",
        timed_out=True,
    )
    assert clean_system.validate_report(report)["status"] == "failed"


def test_clean_host_provenance_is_required_and_must_be_clean() -> None:
    report = _report()
    report.pop("host")
    assert clean_system.validate_report(report)["status"] == "pending_unverified"

    report = _report()
    report["host"]["development_dependencies_present_on_path"] = ["cmake"]
    report["host"]["clean_environment"] = False
    assert clean_system.validate_report(report)["status"] == "pending_unverified"

    report = _report()
    report["host"]["platform"] = "Linux"
    assert clean_system.validate_report(report)["status"] == "failed"


def test_clean_host_provenance_records_os_arch_and_dependency_absence() -> None:
    host = clean_system.clean_host_provenance({"PATH": ""}, execution_kind="physical")
    assert host["architecture"]
    assert "development_dependency_checks" in host
    assert host["development_dependencies_present_on_path"] == []


def test_retained_failure_is_not_downgraded_by_missing_host_provenance() -> None:
    report = _report()
    report["verification_status"] = "failed"
    report.pop("host")
    report["deployment"] = {}
    result = clean_system.validate_report(report)
    assert result["status"] == "failed"


def test_physical_clean_host_rejects_virtual_machine_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clean_system.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(clean_system.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(
        clean_system,
        "_windows_virtualization_probe",
        lambda: {
            "available": True,
            "manufacturer": "VMware, Inc.",
            "model": "VMware Virtual Platform",
            "hypervisor_present": True,
            "error": "",
        },
    )
    host = clean_system.clean_host_provenance({"PATH": ""}, execution_kind="physical")
    assert host["clean_environment"] is False
    assert host["virtualization_indicators"]
    report = _report()
    report["host"] = host
    assert clean_system.validate_report(report)["status"] == "failed"


def test_passthrough_clean_host_requires_documentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clean_system.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(clean_system.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(
        clean_system,
        "_windows_virtualization_probe",
        lambda: {
            "available": True,
            "manufacturer": "QEMU",
            "model": "Standard PC",
            "hypervisor_present": True,
            "error": "",
        },
    )
    host = clean_system.clean_host_provenance(
        {"PATH": ""},
        execution_kind="documented_gpu_passthrough",
        passthrough_documentation="",
    )
    assert host["clean_environment"] is False


def test_physical_clean_host_rejects_vbs_hypervisor_without_independent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clean_system.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(clean_system.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(
        clean_system,
        "_windows_virtualization_probe",
        lambda: {
            "available": True,
            "manufacturer": "Contoso",
            "model": "Workstation 9000",
            "hypervisor_present": True,
            "virtualization_based_security_status": 2,
            "error": "",
        },
    )
    host = clean_system.clean_host_provenance({"PATH": ""}, execution_kind="physical")
    assert host["physical_host_evidence"] is False
    assert host["clean_environment"] is False
    report = _report()
    report["host"] = host
    result = clean_system.validate_report(report)
    assert result["status"] == "failed"
    assert any("hypervisor" in reason for reason in result["reasons"])


def test_physical_clean_host_accepts_vbs_only_with_complete_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clean_system.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(clean_system.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(
        clean_system,
        "_windows_virtualization_probe",
        lambda: {
            "available": True,
            "manufacturer": "ASUSTeK COMPUTER INC.",
            "model": "NUC13ANH-B",
            "product_vendor": "ASUSTeK COMPUTER INC.",
            "product_name": "NUC13ANH-B",
            "bios_manufacturer": "American Megatrends Inc.",
            "bios_version": "NUC13ANBi.0058.2023.0613.1143",
            "baseboard_manufacturer": "ASUSTeK COMPUTER INC.",
            "baseboard_product": "NUC13ANH-B",
            "hypervisor_present": True,
            "virtualization_based_security_status": 2,
            "error": "",
        },
    )
    host = clean_system.clean_host_provenance({"PATH": ""}, execution_kind="physical")
    assert host["physical_host_evidence"] is True
    assert host["hypervisor_explained_by_vbs"] is True
    assert host["clean_environment"] is True
    report = _report()
    report["host"] = host
    result = clean_system.validate_report(report)
    # _report intentionally has no bound deployment directory, so overall
    # verification remains pending. The VBS host itself must not be rejected.
    assert result["status"] == "pending_unverified"
    assert not any("hypervisor" in reason for reason in result["reasons"])


def test_physical_clean_host_rejects_vm_product_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clean_system.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(clean_system.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(
        clean_system,
        "_windows_virtualization_probe",
        lambda: {
            "available": True,
            "manufacturer": "Microsoft Corporation",
            "model": "Virtual Machine",
            "product_vendor": "Microsoft Corporation",
            "product_name": "Virtual Machine",
            "bios_manufacturer": "Microsoft Corporation",
            "bios_version": "Hyper-V UEFI Release v4.1",
            "baseboard_manufacturer": "Microsoft Corporation",
            "baseboard_product": "Virtual Machine",
            "hypervisor_present": True,
            "virtualization_based_security_status": 2,
            "error": "",
        },
    )
    host = clean_system.clean_host_provenance({"PATH": ""}, execution_kind="physical")
    assert host["physical_host_evidence"] is False
    assert "virtual machine" in host["virtualization_indicators"]
    report = _report()
    report["host"] = host
    assert clean_system.validate_report(report)["status"] == "failed"


def test_physical_clean_host_rejects_unexplained_hypervisor_without_vm_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clean_system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(clean_system.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(clean_system.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(
        clean_system,
        "_windows_virtualization_probe",
        lambda: {
            "available": True,
            "manufacturer": "Contoso",
            "model": "Workstation 9000",
            "hypervisor_present": True,
            "virtualization_based_security_status": 0,
            "error": "",
        },
    )
    host = clean_system.clean_host_provenance({"PATH": ""}, execution_kind="physical")
    assert host["physical_host_evidence"] is False
    assert host["clean_environment"] is False
    report = _report()
    report["host"] = host
    assert clean_system.validate_report(report)["status"] == "failed"
