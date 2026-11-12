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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def record_execution(
    executable: Path,
    artifact_directory: Path,
    *,
    tier: str,
    status: str,
    exit_code: int,
    details: dict[str, Any] | None = None,
    output_path: Path | None = None,
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

    # Execute the supplied specialized player.  Operator-supplied --status is
    # an expectation, never proof: a passed claim is accepted only when the
    # observed process exit code matches it.
    try:
        completed = subprocess.run(
            [str(executable), "--frames", "1"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"executed evidence could not run the player: {exc}") from exc
    observed_exit = int(completed.returncode)
    expected_exit = int(exit_code)
    status_matches = (
        (status == "passed" and observed_exit == 0 and expected_exit == 0)
        or (status == "failed" and observed_exit != 0)
    )
    details_payload = dict(details or {})
    required_telemetry = (
        "device",
        "adapter_description",
        "driver_version",
        "capabilities",
    )
    missing_telemetry = [
        name for name in required_telemetry if not details_payload.get(name)
    ]
    passed = status == "passed" and status_matches and not missing_telemetry
    failure_reason = ""
    if not status_matches:
        failure_reason = (
            f"operator status '{status}' does not match observed exit "
            f"{observed_exit} (declared {expected_exit})"
        )
    elif status == "passed" and missing_telemetry:
        failure_reason = (
            "passed evidence is missing required telemetry: "
            + ", ".join(missing_telemetry)
        )
    record_path = output_path or (REPO_ROOT / "benchmarks" / "nw05-hardware-evidence.json")
    record = load_record(record_path)
    execution = {
        "hardware_tier": tier,
        "execution_status": "passed" if passed else "failed",
        "exit_code": observed_exit,
        "declared_exit_code": expected_exit,
        "stdout_sha256": hashlib.sha256(
            completed.stdout.encode("utf-8", errors="replace")
        ).hexdigest(),
        "stderr_sha256": hashlib.sha256(
            completed.stderr.encode("utf-8", errors="replace")
        ).hexdigest(),
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
        "failure_reason": failure_reason,
    }
    # A later passed run must never erase an executed failure from the other
    # tier, and both verified tiers must refer to the identical artifact.
    other_tier = "blackwell" if tier == "ada" else "ada"
    other = record.setdefault("qualifications", {}).get(other_tier, {})
    if passed and other:
        if other.get("status") == "failed":
            passed = False
            failure_reason = (
                f"other hardware tier '{other_tier}' remains an executed failure"
            )
            execution["execution_status"] = "failed"
            execution["failure_reason"] = failure_reason
        elif other.get("status") == "verified":
            if (
                other.get("artifact_key") != artifact_key
                or other.get("package_sha256") != execution["package"]["sha256"]
            ):
                passed = False
                failure_reason = (
                    "other verified hardware tier used a different artifact"
                )
                execution["execution_status"] = "failed"
                execution["failure_reason"] = failure_reason
    record.setdefault("executions", []).append(execution)
    if details_payload.get("toolchain_manifest_sha256"):
        record["toolchain_manifest_sha256"] = str(
            details_payload["toolchain_manifest_sha256"]
        )
    qualification = record.setdefault("qualifications", {}).setdefault(tier, {})
    qualification["status"] = "verified" if passed else "failed"
    qualification["reason"] = failure_reason
    qualification["package_sha256"] = execution["package"]["sha256"]
    qualification["executable_sha256"] = execution["executable"]["sha256"]
    qualification["artifact_key"] = artifact_key
    qualification["adapter_description"] = str(
        details_payload.get("adapter_description", "")
    )
    qualification["generation_metadata"] = str(
        details_payload.get("generation_metadata", "")
    )
    if any(
        record["qualifications"].get(name, {}).get("status") == "failed"
        for name in ("ada", "blackwell")
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

