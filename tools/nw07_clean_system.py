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
import os
import platform
import shutil
import subprocess
import sys
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
    "deployed_startup",
    "missing_required_feature",
    "initialization_failure",
    "resize",
    "device_removal",
)

# The exit-code contract is frozen.  A report cannot redefine a scenario's
# expected result to make an unexecuted or different run appear successful.
EXPECTED_SCENARIO_EXIT_CODES = {
    "corrupt_payload": 70,
    "deployed_startup": 0,
    "missing_required_feature": 23,
    "initialization_failure": 42,
    "resize": 0,
    "device_removal": 51,
}

_DEVELOPMENT_DEPENDENCY_PROBES = (
    "nodewerkc",
    "nodewerk-studio",
    "dxc",
    "dxcompiler.dll",
    "slangc",
    "spirv-cross",
    "cmake",
    "ninja",
)


def _windows_build() -> str:
    version = platform.version()
    parts = [part for part in version.replace("-", ".").split(".") if part.isdigit()]
    if len(parts) >= 3:
        return ".".join(parts[:3])
    return version



def _windows_virtualization_probe() -> dict[str, Any]:
    if platform.system() != "Windows":
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "error": "not_windows",
        }
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$c=Get-CimInstance Win32_ComputerSystem;"
            "[pscustomobject]@{Manufacturer=$c.Manufacturer;Model=$c.Model;"
            "HypervisorPresent=$c.HypervisorPresent}|ConvertTo-Json -Compress"
        ),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "error": str(exc),
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "error": completed.stderr.strip() or completed.stdout.strip(),
        }
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "error": str(exc),
        }
    return {
        "available": True,
        "manufacturer": str(value.get("Manufacturer", "")),
        "model": str(value.get("Model", "")),
        "hypervisor_present": bool(value.get("HypervisorPresent", False)),
        "error": "",
    }


def clean_host_provenance(
    environ: Mapping[str, str] | None = None,
    *,
    execution_kind: str | None = None,
    passthrough_documentation: str = "",
) -> dict[str, Any]:
    """Retain host identity without inventing a physical-host classification."""
    environment = dict(environ or os.environ)
    probes = {
        name: shutil.which(name, path=environment.get("PATH"))
        for name in _DEVELOPMENT_DEPENDENCY_PROBES
    }
    detected = sorted(name for name, path in probes.items() if path)
    cache_variables = sorted(
        key
        for key in environment
        if key.startswith(("NODEWERK", "DXC_", "SLANG_", "VK_ICD_"))
    )
    system = platform.system()
    machine = platform.machine()
    os_build = _windows_build()
    build_number = 0
    if os_build:
        tail = os_build.rsplit(".", 1)[-1]
        build_number = int(tail) if tail.isdigit() else 0
    virtualization = _windows_virtualization_probe()
    identity_text = " ".join(
        (
            str(virtualization.get("manufacturer", "")),
            str(virtualization.get("model", "")),
        )
    ).lower()
    vm_markers = (
        "virtual machine",
        "vmware",
        "virtualbox",
        "kvm",
        "qemu",
        "xen",
        "parallels",
        "bochs",
        "openstack",
    )
    virtualization_indicators = sorted(
        marker for marker in vm_markers if marker in identity_text
    )
    kind_valid = execution_kind in {"physical", "documented_gpu_passthrough"}
    physical_evidence = (
        execution_kind == "physical"
        and virtualization.get("available") is True
        and bool(virtualization.get("manufacturer"))
        and bool(virtualization.get("model"))
        and not virtualization_indicators
    )
    passthrough_evidence = (
        execution_kind == "documented_gpu_passthrough"
        and bool(str(passthrough_documentation or "").strip())
    )
    clean_environment = (
        system == "Windows"
        and machine.lower() in {"amd64", "x86_64"}
        and build_number >= 22000
        and not detected
        and not cache_variables
        and kind_valid
        and (physical_evidence or passthrough_evidence)
    )
    return {
        "execution_kind": execution_kind or "unspecified",
        "passthrough_documentation": str(passthrough_documentation or ""),
        "platform": system,
        "release": platform.release(),
        "os_build": os_build,
        "windows_11_or_newer": build_number >= 22000,
        "architecture": machine,
        "x64": machine.lower() in {"amd64", "x86_64"},
        "development_dependency_checks": probes,
        "development_dependencies_present_on_path": detected,
        "environment_cache_variables": cache_variables,
        "virtualization_probe": virtualization,
        "virtualization_indicators": virtualization_indicators,
        "physical_host_evidence": physical_evidence,
        "clean_environment": clean_environment,
        "application_pipeline_cache_present": bool(cache_variables),
    }

def _scenario_row(
    name: str,
    *,
    command: list[str],
    expected_exit_code: int,
    completed: subprocess.CompletedProcess[str] | None,
    stdout_path: Path | None,
    stderr_path: Path | None,
    timed_out: bool = False,
    execution_error: str = "",
) -> dict[str, Any]:
    observed = completed.returncode if completed is not None else None
    passed = observed == expected_exit_code and not timed_out
    return {
        "scenario": name,
        "executed": completed is not None,
        "command": command,
        "expected_exit_code": expected_exit_code,
        "observed_exit_code": observed,
        "status": "passed" if passed else ("not_executed" if completed is None else "failed"),
        "timed_out": timed_out,
        "execution_error": execution_error,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stderr_path": str(stderr_path) if stderr_path else None,
        "stdout": (completed.stdout[-4000:] if completed is not None else ""),
        "stderr": (completed.stderr[-4000:] if completed is not None else ""),
    }


def deployment_report(
    executable: Path,
    deployment_directory: Path,
    *,
    exclude_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    excluded = {path.resolve() for path in exclude_paths}
    files = sorted(
        path
        for path in deployment_directory.rglob("*")
        if path.is_file() and path.resolve() not in excluded
    )
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


def deployment_executable(directory: Path) -> Path | None:
    return next(
        (
            path
            for path in sorted(directory.glob("*.exe"))
            if "nw_common_" in path.name
        ),
        None,
    )


def _write_corrupt_embedded_copy(executable: Path, destination: Path) -> None:
    """Copy the deployed executable and corrupt only its embedded package."""
    data = bytearray(executable.read_bytes())
    footer = export.nw05_export.EMBED_FOOTER
    if len(data) < footer.size:
        raise ValueError("deployed executable has no embedded package footer")
    (
        magic,
        version,
        package_size,
        manifest_size,
        _package_sha,
        _manifest_sha,
    ) = footer.unpack_from(data, len(data) - footer.size)
    if (
        magic != export.nw05_export.EMBED_MAGIC
        or version != export.nw05_export.EMBED_VERSION
        or package_size == 0
    ):
        raise ValueError("deployed executable embedded footer is invalid")
    total = package_size + manifest_size + footer.size
    if total > len(data):
        raise ValueError("deployed executable embedded package is invalid")
    start = len(data) - total
    data[start + package_size // 2] ^= 0xFF
    destination.write_bytes(bytes(data))


def run_clean_system(
    *,
    fixture: Path,
    package_directory: Path,
    deployment_directory: Path,
    output: Path,
    execution_kind: str | None = None,
    passthrough_documentation: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    executable = deployment_executable(deployment_directory)
    if executable is None:
        raise ValueError("deployment directory has no NW-07 executable")
    identity = export.inspect_artifact_directory(package_directory)
    host = clean_host_provenance(
        execution_kind=execution_kind,
        passthrough_documentation=passthrough_documentation,
    )
    embedded_package, _ = export.nw05_export.extract_embedded_package(
        executable.read_bytes()
    )
    inspection = nwp.inspect_package_bytes(embedded_package, allow_dev=False)
    if not inspection.valid:
        raise ValueError("embedded package is invalid")
    deployment = deployment_report(
        executable, deployment_directory, exclude_paths=(output,)
    )
    rows: list[dict[str, Any]] = []
    evidence_dir = output.parent / f"{output.stem}-artifacts"
    try:
        evidence_dir.resolve().relative_to(deployment_directory.resolve())
    except ValueError:
        pass
    else:
        evidence_dir = deployment_directory.parent / f"{output.stem}-artifacts"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    package_path = evidence_dir / "production.nwp"
    package_path.write_bytes(embedded_package)
    corrupt_executable = evidence_dir / f"{executable.stem}-corrupt{executable.suffix}"
    _write_corrupt_embedded_copy(executable, corrupt_executable)

    def execute(
        name: str,
        executable_path: Path,
        args: list[str],
        expected: int,
    ) -> dict[str, Any]:
        stdout_path = evidence_dir / f"{name}.stdout.txt"
        stderr_path = evidence_dir / f"{name}.stderr.txt"
        command = [str(executable_path), *args]
        timed_out = False
        execution_error = ""
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            execution_error = "execution timed out; the player started but did not exit"
            stdout_value = exc.stdout or ""
            stderr_value = exc.stderr or ""
            if isinstance(stdout_value, bytes):
                stdout_value = stdout_value.decode("utf-8", errors="replace")
            if isinstance(stderr_value, bytes):
                stderr_value = stderr_value.decode("utf-8", errors="replace")
            completed = subprocess.CompletedProcess(
                command,
                returncode=None,
                stdout=stdout_value,
                stderr=(stderr_value + "\n" + str(exc))[-4000:],
            )
            stdout_path.write_text(stdout_value, encoding="utf-8")
            stderr_path.write_text(
                (stderr_value + "\n" + str(exc)), encoding="utf-8"
            )
        except OSError as exc:
            completed = None
            execution_error = str(exc)
            stdout_path.write_text("", encoding="utf-8")
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
            timed_out=timed_out,
            execution_error=execution_error,
        )
        row["captured_stdout"] = str(stdout_path)
        row["captured_stderr"] = str(stderr_path)
        if executable_path == executable:
            row["executable_role"] = "deployed_release"
        elif executable_path == corrupt_executable:
            row["executable_role"] = "deployed_release_corrupted_copy"
        else:
            row["executable_role"] = "fixture"
        return row

    rows.append(
        execute(
            "corrupt_payload",
            corrupt_executable,
            [],
            EXPECTED_SCENARIO_EXIT_CODES["corrupt_payload"],
        )
    )
    rows.append(
        execute(
            "deployed_startup",
            executable,
            ["--frames", "1"],
            0,
        )
    )
    rows.append(
        execute(
            "missing_required_feature",
            fixture,
            [
                "--package",
                str(package_path),
                "--headless",
                "--frames",
                "0",
                "--simulate-capability-failure",
            ],
            EXPECTED_SCENARIO_EXIT_CODES["missing_required_feature"],
        )
    )
    rows.append(
        execute(
            "initialization_failure",
            fixture,
            [
                "--package",
                str(package_path),
                "--headless",
                "--frames",
                "0",
                "--simulate-pipeline-failure",
            ],
            EXPECTED_SCENARIO_EXIT_CODES["initialization_failure"],
        )
    )
    rows.append(
        execute(
            "resize",
            fixture,
            [
                "--package",
                str(package_path),
                "--headless",
                "--frames",
                "5",
                "--resize-sequence",
            ],
            EXPECTED_SCENARIO_EXIT_CODES["resize"],
        )
    )
    rows.append(
        execute(
            "device_removal",
            fixture,
            [
                "--package",
                str(package_path),
                "--headless",
                "--frames",
                "2",
                "--simulate-device-removal",
            ],
            EXPECTED_SCENARIO_EXIT_CODES["device_removal"],
        )
    )
    scenario_failed = any(row["status"] == "failed" for row in rows)
    scenario_missing = any(row["status"] == "not_executed" for row in rows)
    host_failed = (
        host.get("platform") != "Windows"
        or host.get("x64") is not True
        or host.get("windows_11_or_newer") is not True
    )
    report = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "captured_at_utc": now,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "artifact": identity,
        "host": host,
        "deployment": deployment,
        "scenarios": rows,
        "verification_status": (
            "failed"
            if scenario_failed
            or host_failed
            or not deployment["only_runtime_executable"]
            else "pending_unverified"
            if scenario_missing or host.get("clean_environment") is not True
            else "verified"
        ),
        "policy": (
            "An executed scenario failure remains failed. Missing execution stays "
            "pending. Deployment must contain only the self-contained release "
            "executable on a verified clean Windows 11 x64 runtime host."
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
        "host": {},
        "deployment": {},
        "scenarios": [],
        "verification_status": "pending_unverified",
        "reason": reason,
        "policy": (
            "Clean-system execution requires a physical Windows 11 x64 host and the "
            "final NW-07 artifact. Missing execution never becomes passing evidence."
        ),
    }


def validate_report(
    report: Mapping[str, Any],
    *,
    deployment_directory: Path | None = None,
    exclude_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    reasons: list[str] = []
    if report.get("format") != REPORT_FORMAT or report.get("version") != REPORT_VERSION:
        reasons.append("report format/version is not the NW-07 clean-system contract")
    if report.get("task") != contract.TASK:
        reasons.append("report task is not NW-07")
    host_value = report.get("host")
    host = host_value if isinstance(host_value, Mapping) else None
    host_failed = False
    host_pending = False
    if host is None or not host:
        host_pending = True
        reasons.append("clean-system host provenance is missing")
    else:
        execution_kind = host.get("execution_kind")
        if execution_kind not in {"physical", "documented_gpu_passthrough"}:
            host_pending = True
            reasons.append("clean-system execution kind is not attested")
        if execution_kind == "physical":
            probe = host.get("virtualization_probe")
            indicators = host.get("virtualization_indicators")
            if not isinstance(probe, Mapping) or probe.get("available") is not True:
                host_pending = True
                reasons.append("physical clean-system host identity was not probed")
            elif not probe.get("manufacturer") or not probe.get("model"):
                host_pending = True
                reasons.append("physical clean-system host manufacturer/model is missing")
            if not isinstance(indicators, list):
                host_pending = True
                reasons.append("physical clean-system virtualization indicators are missing")
            elif indicators:
                host_failed = True
                reasons.append(
                    "physical clean-system claim conflicts with virtualization indicators: "
                    + ", ".join(str(item) for item in indicators)
                )
            if host.get("physical_host_evidence") is not True:
                host_pending = True
                reasons.append("physical clean-system host evidence is incomplete")
        elif execution_kind == "documented_gpu_passthrough":
            if not str(host.get("passthrough_documentation") or "").strip():
                host_pending = True
                reasons.append("GPU-passthrough clean-system documentation is missing")
        if host.get("platform") != "Windows":
            host_failed = True
            reasons.append("clean-system execution host is not Windows")
        if host.get("x64") is not True:
            host_failed = True
            reasons.append("clean-system execution host is not x64")
        if host.get("windows_11_or_newer") is not True:
            host_failed = True
            reasons.append("clean-system execution host is not Windows 11 or newer")
        if host.get("clean_environment") is not True:
            host_pending = True
            reasons.append(
                "clean-system host retains development dependencies or cache state"
            )
        present = host.get("development_dependencies_present_on_path")
        if not isinstance(present, list) or present:
            host_pending = True
            reasons.append(
                "clean-system host did not prove development dependencies are absent"
            )
        checks = host.get("development_dependency_checks")
        if not isinstance(checks, Mapping) or not checks:
            host_pending = True
            reasons.append("clean-system dependency probes were not retained")
        else:
            detected_from_probes = sorted(
                name for name, path in checks.items() if path
            )
            if detected_from_probes != sorted(present or []):
                host_pending = True
                reasons.append(
                    "clean-system dependency probes disagree with the retained result"
                )
        if host.get("application_pipeline_cache_present") is True:
            host_pending = True
            reasons.append(
                "clean-system host exposes an application pipeline cache"
            )
        cache_variables = host.get("environment_cache_variables")
        if not isinstance(cache_variables, list) or cache_variables:
            host_pending = True
            reasons.append(
                "clean-system host retains development cache environment variables"
            )
    deployment_value = report.get("deployment")
    deployment = (
        deployment_value if isinstance(deployment_value, Mapping) else None
    )
    if deployment is None:
        if deployment_value not in (None, {}):
            return {
                "status": "failed",
                "reasons": reasons + ["clean-system deployment record is malformed"],
                "scenarios": report.get("scenarios") or [],
            }
        return {
            "status": "pending_unverified",
            "reasons": reasons + ["clean-system deployment was not inspected"],
            "scenarios": report.get("scenarios") or [],
        }
    if not deployment:
        return {
            "status": "pending_unverified",
            "reasons": reasons + ["clean-system deployment was not inspected"],
            "scenarios": report.get("scenarios") or [],
        }
    deployment_failed = False
    if deployment_directory is not None:
        actual_executable = deployment_executable(Path(deployment_directory))
        if actual_executable is None:
            deployment_failed = True
            reasons.append("deployment directory has no NW-07 executable")
        else:
            actual = deployment_report(
                actual_executable,
                Path(deployment_directory),
                exclude_paths=exclude_paths,
            )
            for field in (
                "executable",
                "executable_sha256",
                "executable_bytes",
                "deployment_files",
                "sidecars",
                "only_runtime_executable",
                "development_dependencies_present",
                "pe_imports",
                "forbidden_runtime_dependencies",
            ):
                if deployment.get(field) != actual.get(field):
                    deployment_failed = True
                    reasons.append(
                        f"clean-system deployment field '{field}' does not match "
                        "the actual deployment"
                    )
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
    failed: list[str] = []
    not_executed: list[str] = []
    for name in REQUIRED_SCENARIOS:
        row = by_name.get(name)
        if row is None:
            continue
        expected = EXPECTED_SCENARIO_EXIT_CODES[name]
        if row.get("expected_exit_code") != expected:
            failed.append(name)
            reasons.append(
                f"{name} declares expected exit code "
                f"{row.get('expected_exit_code')} instead of the frozen {expected}"
            )
        if row.get("status") == "failed":
            failed.append(name)
            continue
        if row.get("executed") is not True:
            not_executed.append(name)
            continue
        if row.get("observed_exit_code") != expected:
            failed.append(name)
            reasons.append(
                f"{name} observed exit code {row.get('observed_exit_code')} "
                f"instead of {expected}"
            )
        elif row.get("status") != "passed":
            failed.append(name)
            reasons.append(f"{name} did not record a passed execution")
    if deployment_failed or failed or host_failed:
        status = "failed"
        if failed:
            reasons.append("executed clean-system scenarios failed: " + ", ".join(failed))
    elif missing:
        status = "pending_unverified"
        reasons.append("clean-system scenarios are missing: " + ", ".join(missing))
    elif not_executed:
        status = "pending_unverified"
        reasons.append(
            "clean-system scenarios were not executed: " + ", ".join(not_executed)
        )
    elif host_pending:
        status = "pending_unverified"
        reasons.append("clean-system host provenance is incomplete or not clean")
    elif report.get("verification_status") == "failed":
        status = "failed"
        reasons.append("clean-system report records an executed failure")
    elif report.get("verification_status") != "verified":
        status = "pending_unverified"
        reasons.append("clean-system report is not verified")
    else:
        if deployment_directory is None:
            status = "pending_unverified"
            reasons.append(
                "actual deployment was not supplied for executable and sidecar binding"
            )
        else:
            status = "verified"
    return {"status": status, "reasons": reasons, "scenarios": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--execution-kind",
        choices=("physical", "documented_gpu_passthrough"),
        required=True,
    )
    parser.add_argument("--passthrough-documentation", default="")
    args = parser.parse_args(argv)
    if platform.system().lower() != "windows":
        report = pending_report("clean-system execution requires a physical Windows 11 x64 host")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 2
    try:
        report = run_clean_system(
            fixture=Path(args.fixture),
            package_directory=Path(args.package),
            deployment_directory=Path(args.deployment),
            output=Path(args.output),
            execution_kind=args.execution_kind,
            passthrough_documentation=args.passthrough_documentation,
        )
    except (OSError, ValueError, export.Nw07ExportError) as exc:
        report = pending_report(
            f"clean-system prerequisites are unavailable: {exc}"
        )
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 2
    return 0 if report["verification_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
