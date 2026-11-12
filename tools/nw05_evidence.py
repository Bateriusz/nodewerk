#!/usr/bin/env python3
"""NW-05 alias/output evidence recorder.

NW-05 qualification requires the *identical* common artifact to be executed on
representative desktop Ada and Blackwell hardware.  This tool never turns
missing coverage into success: absent executions stay `pending_unverified`, and
an executed failure is recorded as a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_hardware_tier(adapter_description: str) -> str:
    name = adapter_description.lower()
    if any(token in name for token in ("rtx 50", "b100", "b200", "gb200")):
        return "blackwell"
    if any(
        token in name
        for token in ("rtx 40", "l40", "a40", "a6000", "rtx a")
    ):
        return "ada"
    return "unknown"


def probe_hardware_identity() -> dict[str, Any]:
    """Query the executing host's adapter identity instead of trusting argv."""
    adapters: list[dict[str, Any]] = []
    source = "unavailable"
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            completed = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name,driver_version,compute_cap,pci.bus_id,memory.total",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            source = "nvidia-smi"
            for line in completed.stdout.splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 5:
                    continue
                name, driver, compute_cap, pci, memory = fields
                adapters.append(
                    {
                        "device": name,
                        "adapter_description": name,
                        "driver_version": driver,
                        "pci_identifier": pci,
                        "capabilities": {
                            "compute_capability": compute_cap,
                            "memory_total": memory,
                        },
                        "hardware_tier": classify_hardware_tier(name),
                    }
                )
    if not adapters and os.name == "nt":
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | "
                    "Select-Object Name,DriverVersion,PNPDeviceID | "
                    "ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            try:
                payload = json.loads(completed.stdout or "[]")
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, dict):
                payload = [payload]
            source = "win32-video-controller"
            for item in payload if isinstance(payload, list) else []:
                name = str(item.get("Name", ""))
                if not name:
                    continue
                adapters.append(
                    {
                        "device": name,
                        "adapter_description": name,
                        "driver_version": str(item.get("DriverVersion", "")),
                        "pci_identifier": str(item.get("PNPDeviceID", "")),
                        "capabilities": {},
                        "hardware_tier": classify_hardware_tier(name),
                    }
                )
    return {"source": source, "adapters": adapters}


def _select_probed_adapter(
    probe: dict[str, Any], tier: str
) -> dict[str, Any] | None:
    adapters = probe.get("adapters", [])
    for adapter in adapters:
        if adapter.get("hardware_tier") == tier:
            return adapter
    return adapters[0] if adapters else None


def pending_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task": "NW-05",
        "verification_status": "pending_unverified",
        "common_profile": {
            "profile_id": nwp.COMMON_PROFILE_ID,
            "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
        },
        "qualifications": {
            "ada": {
                "hardware_tier": "desktop_ada_representative",
                "status": "pending_unverified",
                "reason": "No representative desktop Ada execution of the identical NW-05 common artifact is recorded.",
            },
            "blackwell": {
                "hardware_tier": "desktop_blackwell_representative",
                "status": "pending_unverified",
                "reason": "No representative desktop Blackwell execution of the identical NW-05 common artifact is recorded.",
            },
        },
        "executions": [],
        "unavailable_telemetry": [
            "executable_sha256",
            "package_sha256",
            "adapter_description",
            "driver_version",
            "windows_build",
        ],
        "toolchain_manifest_sha256": "unavailable",
        "policy": (
            "NW-05 alias and output equality evidence is accepted only when the "
            "identical common artifact is executed on representative desktop Ada "
            "and Blackwell hardware. Missing coverage remains pending/unverified; "
            "executed failures remain failures. GPU generation, model, adapter "
            "name, PCI identifier, and evidence tier never select targets or "
            "alter byte-identical results."
        ),
    }


def load_record(path: Path) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return pending_record()


def _common_artifact(artifact_directory: Path) -> tuple[Path, bytes]:
    stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
    package_path = artifact_directory / f"{stem}.nwp"
    if not package_path.is_file():
        raise SystemExit(f"common package is unavailable: {package_path}")
    return package_path, package_path.read_bytes()


def _run_player(
    executable: Path,
    arguments: list[str],
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(executable), *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": -1,
            "timed_out": True,
            "stdout": (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            ),
            "stderr": (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            ),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "execution timed out",
        }
    except OSError as exc:
        return {
            "exit_code": -1,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc),
        }
    return {
        "exit_code": int(completed.returncode),
        "timed_out": False,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "error": "",
    }


def _telemetry(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("NW05_"):
            values[key] = value.strip()
    return values


def _int_telemetry(values: dict[str, str], name: str) -> int | None:
    try:
        return int(values.get(name, ""))
    except (TypeError, ValueError):
        return None


def record_execution(
    executable: Path,
    artifact_directory: Path,
    *,
    tier: str,
    status: str,
    exit_code: int,
    details: dict[str, Any] | None = None,
    output_path: Path | None = None,
    probe: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if tier not in {"ada", "blackwell"}:
        raise SystemExit(f"unsupported evidence tier: {tier}")
    if status not in {"passed", "failed"}:
        raise SystemExit(f"unsupported execution status: {status}")
    if not executable.is_file():
        raise SystemExit(f"executed evidence requires an existing player: {executable}")
    package_path, package_bytes = _common_artifact(artifact_directory)
    inspection = nwp.inspect_package_bytes(
        package_bytes,
        allow_dev=not bool(len(package_bytes) > 16 and package_bytes[16] & 1),
    )
    if not inspection.valid or inspection.package is None:
        raise SystemExit("common package is invalid")
    if inspection.package.capability.profile_id != nwp.COMMON_PROFILE_ID:
        raise SystemExit("common artifact profile identity changed")
    try:
        embedded_bytes, embedded_manifest = nw05_export.extract_embedded_package(
            executable.read_bytes()
        )
    except nw05_export.ExportError as exc:
        raise SystemExit(
            f"executed evidence requires an embedded common artifact: {exc}"
        ) from exc
    if embedded_bytes != package_bytes:
        raise SystemExit(
            "evidence requires the identical common package embedded in "
            "the executed specialized player"
        )
    try:
        manifest_payload = json.loads(embedded_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"embedded link manifest is invalid: {exc}") from exc
    artifact_key = str(manifest_payload.get("artifact_identity", ""))
    if not artifact_key:
        raise SystemExit("embedded link manifest has no artifact identity")

    # Execute the supplied specialized player in both realizations.  A passed
    # claim requires observed output/alias telemetry from the executing
    # adapter, a matching non-aliased output image, and the actual alias plan
    # to have been realized.  Operator-supplied --status is only an expectation.
    aliased_run = _run_player(
        executable, ["--frames", "2", "--emit-image-hash"]
    )
    non_aliased_run = _run_player(
        executable,
        ["--frames", "2", "--emit-image-hash", "--force-non-aliased-plan"],
    )
    observed_exit = int(aliased_run["exit_code"])
    expected_exit = int(exit_code)
    aliased_telemetry = _telemetry(str(aliased_run["stdout"]))
    non_aliased_telemetry = _telemetry(str(non_aliased_run["stdout"]))
    status_matches = (
        (status == "passed" and observed_exit == 0 and expected_exit == 0)
        or (status == "failed" and observed_exit != 0)
    )
    details_payload = dict(details or {})
    executed_adapter = aliased_telemetry.get("NW05_ADAPTER", "")
    executed_driver = aliased_telemetry.get("NW05_DRIVER", "")
    if executed_adapter:
        details_payload["device"] = executed_adapter
        details_payload["adapter_description"] = executed_adapter
    if executed_driver:
        details_payload["driver_version"] = executed_driver
    required_telemetry = (
        "device",
        "adapter_description",
        "driver_version",
        "capabilities",
    )
    probe_fn = probe or probe_hardware_identity
    try:
        probed = probe_fn()
    except Exception:
        probed = {"source": "unavailable", "adapters": []}
    probed_adapter = _select_probed_adapter(probed, tier)
    probed_capabilities = (
        probed_adapter.get("capabilities", {}) if probed_adapter else {}
    )
    hardware_identity_verified = bool(
        probed_adapter
        and probed_adapter.get("adapter_description")
        and probed_adapter.get("driver_version")
        and probed_capabilities
    )
    if probed_adapter:
        details_payload.setdefault(
            "device", probed_adapter.get("adapter_description", "")
        )
        details_payload.setdefault(
            "adapter_description",
            probed_adapter.get("adapter_description", ""),
        )
        details_payload.setdefault(
            "driver_version", probed_adapter.get("driver_version", "")
        )
        if not details_payload.get("capabilities"):
            details_payload["capabilities"] = probed_capabilities
        details_payload["verified_capabilities"] = probed_capabilities
    missing_telemetry = [
        name for name in required_telemetry if not details_payload.get(name)
    ]
    alias_barrier_count = _int_telemetry(aliased_telemetry, "NW05_ALIAS_BARRIERS")
    non_aliased_barrier_count = _int_telemetry(
        non_aliased_telemetry, "NW05_ALIAS_BARRIERS"
    )
    alias_telemetry_ok = bool(
        aliased_telemetry.get("NW05_LINK_MANIFEST_VERIFIED") == "1"
        and aliased_telemetry.get("NW05_ALIASED_PLAN") == "1"
        and alias_barrier_count is not None
        and alias_barrier_count > 0
        and (_int_telemetry(aliased_telemetry, "NW05_IMAGE_NONZERO_BYTES") or 0)
        > 0
        and executed_adapter
        and executed_driver
    )
    non_aliased_telemetry_ok = bool(
        non_aliased_telemetry.get("NW05_LINK_MANIFEST_VERIFIED") == "1"
        and non_aliased_telemetry.get("NW05_ALIASED_PLAN") == "0"
        and (_int_telemetry(non_aliased_telemetry, "NW05_IMAGE_NONZERO_BYTES") or 0)
        > 0
    )
    output_match = bool(
        aliased_telemetry.get("NW05_IMAGE_FNV64")
        and aliased_telemetry.get("NW05_IMAGE_FNV64")
        == non_aliased_telemetry.get("NW05_IMAGE_FNV64")
        and aliased_telemetry.get("NW05_IMAGE_BYTES")
        == non_aliased_telemetry.get("NW05_IMAGE_BYTES")
    )
    if (
        non_aliased_barrier_count is not None
        and non_aliased_barrier_count != 0
    ):
        non_aliased_telemetry_ok = False
    passed = (
        status == "passed"
        and status_matches
        and not missing_telemetry
        and hardware_identity_verified
        and not aliased_run["timed_out"]
        and not non_aliased_run["timed_out"]
        and non_aliased_run["exit_code"] == 0
        and alias_telemetry_ok
        and non_aliased_telemetry_ok
        and output_match
    )
    failure_reason = ""
    if not status_matches:
        failure_reason = (
            f"operator status '{status}' does not match observed exit "
            f"{observed_exit} (declared {expected_exit})"
        )
    elif status == "failed":
        failure_reason = "declared failed execution"
    elif status == "passed" and missing_telemetry:
        failure_reason = (
            "passed evidence is missing required telemetry: "
            + ", ".join(missing_telemetry)
        )
    elif status == "passed" and not hardware_identity_verified:
        failure_reason = (
            "passed evidence could not independently verify adapter identity, "
            "driver, capabilities, and requested hardware tier"
        )
    elif status == "passed" and aliased_run["timed_out"]:
        failure_reason = "aliased execution timed out"
    elif status == "passed" and non_aliased_run["timed_out"]:
        failure_reason = "non-aliased comparison execution timed out"
    elif status == "passed" and non_aliased_run["exit_code"] != 0:
        failure_reason = (
            "non-aliased comparison execution exited "
            f"{non_aliased_run['exit_code']}"
        )
    elif status == "passed" and not alias_telemetry_ok:
        failure_reason = (
            "passed evidence lacks executed aliased-plan/image telemetry tied "
            "to the running adapter"
        )
    elif status == "passed" and not non_aliased_telemetry_ok:
        failure_reason = (
            "non-aliased comparison did not run the ordinary committed plan "
            "and produce image telemetry"
        )
    elif status == "passed" and not output_match:
        failure_reason = (
            "aliased and non-aliased fixture outputs do not match"
        )
    if status == "passed" and hardware_identity_verified and probed_adapter:
        supplied_adapter = str(details_payload.get("adapter_description", ""))
        supplied_driver = str(details_payload.get("driver_version", ""))
        if (
            supplied_adapter
            and supplied_adapter.lower()
            not in str(probed_adapter.get("adapter_description", "")).lower()
            and str(probed_adapter.get("adapter_description", "")).lower()
            not in supplied_adapter.lower()
        ):
            passed = False
            failure_reason = (
                "caller-supplied adapter does not match the probed adapter"
            )
        elif supplied_driver and supplied_driver != str(
            probed_adapter.get("driver_version", "")
        ):
            passed = False
            failure_reason = (
                "caller-supplied driver does not match the probed driver"
            )
        elif probed_adapter.get("hardware_tier") != tier:
            passed = False
            failure_reason = (
                f"probed adapter tier '{probed_adapter.get('hardware_tier')}' "
                f"does not match requested tier '{tier}'"
            )
    record_path = output_path or (REPO_ROOT / "benchmarks" / "nw05-hardware-evidence.json")
    record = load_record(record_path)
    execution = {
        "hardware_tier": tier,
        "execution_status": "passed" if passed else "failed",
        "exit_code": observed_exit,
        "declared_exit_code": expected_exit,
        "timed_out": bool(aliased_run["timed_out"]),
        "non_aliased_timed_out": bool(non_aliased_run["timed_out"]),
        "non_aliased_exit_code": int(non_aliased_run["exit_code"]),
        "elapsed_ms": int(aliased_run["elapsed_ms"]),
        "stdout_sha256": hashlib.sha256(
            str(aliased_run["stdout"]).encode("utf-8", errors="replace")
        ).hexdigest(),
        "stderr_sha256": hashlib.sha256(
            str(aliased_run["stderr"]).encode("utf-8", errors="replace")
        ).hexdigest(),
        "telemetry": aliased_telemetry,
        "non_aliased_telemetry": non_aliased_telemetry,
        "aliased_plan_realized": aliased_telemetry.get("NW05_ALIASED_PLAN") == "1",
        "alias_barrier_count": alias_barrier_count or 0,
        "output_matches_non_aliased": output_match,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_key": artifact_key,
        "package": {
            "path": str(package_path),
            "sha256": hashlib.sha256(package_bytes).hexdigest(),
            "profile_id": inspection.package.capability.profile_id,
            "requirements_digest": inspection.package.capability.requirements_digest,
        },
        "executable": {
            "path": str(executable),
            "sha256": _sha256(executable) if executable.is_file() else "unavailable",
            "embedded_package_matches": embedded_bytes == package_bytes,
        },
        "details": details_payload,
        "probed_hardware": probed_adapter or {},
        "hardware_identity_verified": hardware_identity_verified,
        "failure_reason": failure_reason,
    }
    # A later passed run must never erase an executed failure, and both
    # verified tiers must refer to the identical package and executable.
    other_tier = "blackwell" if tier == "ada" else "ada"
    other = record.setdefault("qualifications", {}).get(other_tier, {})
    if passed and other and other.get("status") == "verified":
        if (
            other.get("artifact_key") != artifact_key
            or other.get("package_sha256") != execution["package"]["sha256"]
            or (
                other.get("executable_sha256")
                and other.get("executable_sha256")
                != execution["executable"]["sha256"]
            )
        ):
            passed = False
            failure_reason = (
                "other verified hardware tier used a different artifact or "
                "executable"
            )
            execution["execution_status"] = "failed"
            execution["failure_reason"] = failure_reason
    record.setdefault("executions", []).append(execution)
    if details_payload.get("toolchain_manifest_sha256"):
        record["toolchain_manifest_sha256"] = str(
            details_payload["toolchain_manifest_sha256"]
        )
    qualification = record.setdefault("qualifications", {}).setdefault(tier, {})
    tier_executions = [
        item
        for item in record["executions"]
        if item.get("hardware_tier") == tier
    ]
    failed_executions = [
        item
        for item in tier_executions
        if item.get("execution_status") == "passed"
        and item.get("output_matches_non_aliased") is False
    ] + [
        item
        for item in tier_executions
        if item.get("execution_status") != "passed"
    ]
    prior_failed = qualification.get("status") == "failed"
    verified_execution = next(
        (
            item
            for item in reversed(tier_executions)
            if item.get("execution_status") == "passed"
        ),
        None,
    )
    qualification["status"] = (
        "failed"
        if prior_failed or failed_executions
        else "verified"
        if verified_execution is not None
        else "pending_unverified"
    )
    qualification["reason"] = (
        failure_reason
        if failure_reason
        else "tier retains an earlier executed failure"
        if prior_failed or failed_executions
        else ""
    )
    qualification["failed_executions"] = len(failed_executions)
    qualification["execution_count"] = len(tier_executions)
    qualification["package_sha256"] = execution["package"]["sha256"]
    qualification["executable_sha256"] = execution["executable"]["sha256"]
    qualification["artifact_key"] = artifact_key
    qualification["hardware_identity_verified"] = hardware_identity_verified
    qualification["hardware_identity_source"] = str(probed.get("source", "unavailable"))
    qualification["probed_hardware"] = probed_adapter or {}
    qualification["adapter_description"] = str(
        details_payload.get("adapter_description", "")
    )
    qualification["generation_metadata"] = str(
        details_payload.get("generation_metadata", "")
    )
    if any(
        record["qualifications"].get(name, {}).get("status") == "failed"
        for name in ("ada", "blackwell")
    ) or any(
        item.get("execution_status") != "passed"
        for item in record.get("executions", [])
    ):
        record["verification_status"] = "executed_failure"
    elif all(
        record["qualifications"].get(name, {}).get("status") == "verified"
        for name in ("ada", "blackwell")
    ):
        record["verification_status"] = "verified"
    else:
        record["verification_status"] = "execution_recorded"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record NW-05 Ada/Blackwell evidence")
    parser.add_argument("--executable")
    parser.add_argument("--artifact-directory")
    parser.add_argument("--tier", choices=["ada", "blackwell"], required=True)
    parser.add_argument("--status", choices=["passed", "failed"])
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--record")
    parser.add_argument("--print-pending", action="store_true")
    args = parser.parse_args(argv)
    if args.print_pending or not args.executable:
        print(json.dumps(pending_record(), indent=2, sort_keys=True))
        return 0
    if not args.artifact_directory or args.status is None:
        parser.error("--artifact-directory and --status are required with --executable")
    record = record_execution(
        Path(args.executable),
        Path(args.artifact_directory),
        tier=args.tier,
        status=args.status,
        exit_code=args.exit_code,
        output_path=Path(args.record) if args.record else None,
    )
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

