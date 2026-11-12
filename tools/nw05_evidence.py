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
    package_path, package_bytes = _common_artifact(artifact_directory)
    inspection = nwp.inspect_package_bytes(
        package_bytes,
        allow_dev=not bool(len(package_bytes) > 16 and package_bytes[16] & 1),
    )
    if not inspection.valid or inspection.package is None:
        raise SystemExit("common package is invalid")
    if inspection.package.capability.profile_id != nwp.COMMON_PROFILE_ID:
        raise SystemExit("common artifact profile identity changed")
    embedded_bytes = None
    if executable.is_file():
        try:
            embedded_bytes, _manifest = nw05_export.extract_embedded_package(
                executable.read_bytes()
            )
        except nw05_export.ExportError:
            embedded_bytes = None
    if status == "passed" and embedded_bytes != package_bytes:
        raise SystemExit(
            "passed evidence requires the identical common package embedded in "
            "the executed specialized player"
        )
    record_path = output_path or (REPO_ROOT / "benchmarks" / "nw05-hardware-evidence.json")
    record = load_record(record_path)
    execution = {
        "hardware_tier": tier,
        "execution_status": status,
        "exit_code": int(exit_code),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
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
        "details": dict(details or {}),
    }
    record.setdefault("executions", []).append(execution)
    qualification = record.setdefault("qualifications", {}).setdefault(tier, {})
    qualification["status"] = "verified" if status == "passed" else "failed"
    qualification["package_sha256"] = execution["package"]["sha256"]
    qualification["executable_sha256"] = execution["executable"]["sha256"]
    qualification["artifact_key"] = ""
    if status == "failed":
        record["verification_status"] = "executed_failure"
    else:
        record["verification_status"] = "execution_recorded"
    if all(
        record["qualifications"].get(name, {}).get("status") == "verified"
        for name in ("ada", "blackwell")
    ):
        record["verification_status"] = "verified"
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

