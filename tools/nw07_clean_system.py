#!/usr/bin/env python3
"""NW-07 clean-system deployment and failure-mode harness.

On a physical Windows 11 x64 host this extracts the package embedded in the
final NW-07 executable and drives the established PackagePlayer fixture through
the required failure and recovery scenarios.  On other hosts it records an
honest pending report and never claims clean-system verification.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import dependency_check  # noqa: E402
import nw07_contract as contract  # noqa: E402
import nw07_export as export  # noqa: E402
import nwp_package as nwp  # noqa: E402

REPORT_FORMAT = "nw07-clean-system-report"
REPORT_VERSION = 1

REQUIRED_SCENARIOS = (
    "corrupt_payload",
    "missing_required_feature",
    "initialization_failure",
    "resize",
    "device_removal",
)


def _scenario_row(
    name: str,
    *,
    command: list[str],
    expected_exit_code: int,
    completed: subprocess.CompletedProcess[str] | None,
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> dict[str, Any]:
    observed = completed.returncode if completed is not None else None
    passed = observed == expected_exit_code
    return {
        "scenario": name,
        "executed": completed is not None,
        "command": command,
        "expected_exit_code": expected_exit_code,
        "observed_exit_code": observed,
        "status": "passed" if passed else ("not_executed" if completed is None else "failed"),
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stderr_path": str(stderr_path) if stderr_path else None,
        "stdout": (completed.stdout[-4000:] if completed is not None else ""),
        "stderr": (completed.stderr[-4000:] if completed is not None else ""),
    }


def deployment_report(executable: Path, deployment_directory: Path) -> dict[str, Any]:
    files = [
        path
        for path in deployment_directory.rglob("*")
        if path.is_file()
    ]
    sidecars = [
        str(path.relative_to(deployment_directory))
        for path in files
        if path != executable
    ]
    imports: list[str] = []
    try:
        imports = sorted(
            {
                str(name).lower().removesuffix(".dll")
                for name in dependency_check.parse_pe64_sections_and_imports(
                    executable.read_bytes()
                ).get("imports", [])
                if name
            }
        )
    except Exception:
        imports = []
    forbidden = [
        name
        for name in imports
        if any(token in name for token in export.FORBIDDEN_RUNTIME_DEPENDENCIES)
    ]
    return {
        "executable": executable.name,
        "executable_sha256": export.sha256_file(executable),
        "executable_bytes": executable.stat().st_size,
        "deployment_files": [str(path.relative_to(deployment_directory)) for path in files],
        "sidecars": sidecars,
        "only_runtime_executable": not sidecars,
        "pe_imports": imports,
        "forbidden_runtime_dependencies": forbidden,
        "development_dependencies_present": bool(forbidden),
    }


def run_clean_system(
    *,
    fixture: Path,
    package_directory: Path,
    deployment_directory: Path,
    output: Path,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    executable = next(
        (
            path
            for path in sorted(deployment_directory.glob("*.exe"))
            if "nw_common_" in path.name
        ),
        None,
    )
    if executable is None:
        raise ValueError("deployment directory has no NW-07 executable")
    identity = export.inspect_artifact_directory(package_directory)
    embedded_package, _ = export.nw05_export.extract_embedded_package(
        executable.read_bytes()
    )
    inspection = nwp.inspect_package_bytes(embedded_package, allow_dev=False)
    if not inspection.valid:
        raise ValueError("embedded package is invalid")
    deployment = deployment_report(executable, deployment_directory)
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="nw07-clean-") as temporary:
        temp = Path(temporary)
        package_path = temp / "production.nwp"
        package_path.write_bytes(embedded_package)
        corrupt_path = temp / "corrupt.nwp"
        corrupt_path.write_bytes(embedded_package[:128])

        def execute(name: str, args: list[str], expected: int) -> dict[str, Any]:
            stdout_path = temp / f"{name}.stdout.txt"
            stderr_path = temp / f"{name}.stderr.txt"
            command = [str(fixture), *args]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                completed = None
                stderr_path.write_text(str(exc), encoding="utf-8")
            if completed is not None:
                stdout_path.write_text(completed.stdout, encoding="utf-8")
                stderr_path.write_text(completed.stderr, encoding="utf-8")
            row = _scenario_row(
                name,
                command=command,
                expected_exit_code=expected,
                completed=completed,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            row["captured_stdout"] = str(stdout_path)
            row["captured_stderr"] = str(stderr_path)
            return row

        rows.append(
            execute(
                "corrupt_payload",
                ["--package", str(corrupt_path), "--headless", "--frames", "0"],
                70,
            )
        )
        rows.append(
            execute(
                "missing_required_feature",
                [
                    "--package",
                    str(package_path),
                    "--headless",
                    "--frames",
                    "0",
                    "--simulate-capability-failure",
                ],
                23,
            )
        )
        rows.append(
            execute(
                "initialization_failure",
                [
                    "--package",
                    str(package_path),
                    "--headless",
                    "--frames",
                    "0",
                    "--simulate-pipeline-failure",
                ],
                42,
            )
        )
        rows.append(
            execute(
                "resize",
                [
                    "--package",
                    str(package_path),
                    "--headless",
                    "--frames",
                    "5",
                    "--resize-sequence",
                ],
                0,
            )
        )
        rows.append(
            execute(
                "device_removal",
                [
                    "--package",
                    str(package_path),
                    "--headless",
                    "--frames",
                    "2",
                    "--simulate-device-removal",
                ],
                51,
            )
        )
    scenario_failed = any(row["status"] == "failed" for row in rows)
    scenario_missing = any(row["status"] == "not_executed" for row in rows)
    report = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "captured_at_utc": now,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "artifact": identity,
        "deployment": deployment,
        "scenarios": rows,
        "verification_status": (
            "failed"
            if scenario_failed or not deployment["only_runtime_executable"]
            else "pending_unverified"
            if scenario_missing
            else "verified"
        ),
        "policy": (
            "An executed scenario failure remains failed. Missing execution stays "
            "pending. Deployment must contain only the self-contained release executable."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def pending_report(reason: str) -> dict[str, Any]:
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "deployment": {},
        "scenarios": [],
        "verification_status": "pending_unverified",
        "reason": reason,
        "policy": (
            "Clean-system execution requires a physical Windows 11 x64 host and the "
            "final NW-07 artifact. Missing execution never becomes passing evidence."
        ),
    }


def validate_report(report: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if report.get("format") != REPORT_FORMAT or report.get("version") != REPORT_VERSION:
        reasons.append("report format/version is not the NW-07 clean-system contract")
    if report.get("task") != contract.TASK:
        reasons.append("report task is not NW-07")
    deployment = report.get("deployment") or {}
    if not deployment:
        return {
            "status": "pending_unverified",
            "reasons": reasons + ["clean-system deployment was not inspected"],
            "scenarios": report.get("scenarios") or [],
        }
    deployment_failed = False
    if deployment.get("only_runtime_executable") is not True:
        reasons.append("deployment contains sidecars or development dependencies")
        deployment_failed = True
    if deployment.get("development_dependencies_present") is not False:
        reasons.append("deployment records development dependencies")
        deployment_failed = True
    rows = report.get("scenarios")
    if not isinstance(rows, list):
        return {"status": "failed", "reasons": reasons + ["scenario results are absent"]}
    by_name = {
        row.get("scenario"): row
        for row in rows
        if isinstance(row, Mapping) and row.get("scenario")
    }
    missing = [name for name in REQUIRED_SCENARIOS if name not in by_name]
    failed = [
        name
        for name, row in by_name.items()
        if row.get("status") == "failed"
        or (
            row.get("executed")
            and row.get("observed_exit_code") != row.get("expected_exit_code")
        )
    ]
    if deployment_failed or failed:
        status = "failed"
        if failed:
            reasons.append("executed clean-system scenarios failed: " + ", ".join(failed))
    elif missing:
        status = "pending_unverified"
        reasons.append("clean-system scenarios are missing: " + ", ".join(missing))
    elif report.get("verification_status") != "verified":
        status = "pending_unverified"
        reasons.append("clean-system report is not verified")
    else:
        status = "verified"
    return {"status": status, "reasons": reasons, "scenarios": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if platform.system().lower() != "windows":
        report = pending_report("clean-system execution requires a physical Windows 11 x64 host")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 2
    report = run_clean_system(
        fixture=Path(args.fixture),
        package_directory=Path(args.package),
        deployment_directory=Path(args.deployment),
        output=Path(args.output),
    )
    return 0 if report["verification_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
