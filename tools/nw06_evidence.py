#!/usr/bin/env python3
"""NW-06 fixed-tick capture evidence and metadata validation.

Hardware coverage is never inferred from a GPU name or generation.  A capture
is accepted only when the identical common artifact is executed and the
required metadata is present.  Missing telemetry remains pending/unverified;
an executed failure remains a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402


TASK = "NW-06"
COMMON_PROFILE_ID = nwp.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST
TICKS_PER_SECOND = 48_000

REQUIRED_CAPTURE_METADATA = (
    "coverage_target",
    "evidence_role",
    "artifact_key",
    "package_sha256",
    "executable_sha256",
    "manifest_sha256",
    "profile_id",
    "requirements_digest",
    "adapter_description",
    "device",
    "driver_version",
    "os_version",
    "toolchain_identity",
    "queried_capabilities",
    "clock_information",
    "power_information",
    "thermal_information",
    "cache_state",
    "capture_protocol",
    "unavailable_telemetry",
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_hardware_tier(adapter_description: str) -> str:
    """Metadata-only classification; never used as an eligibility branch."""
    name = adapter_description.lower()
    if any(token in name for token in ("rtx 50", "blackwell", "b100", "b200", "gb200")):
        return "blackwell"
    if any(
        token in name
        for token in ("rtx 40", "l40", "a40", "a6000", "rtx a")
    ) or re.search(r"(^|[^a-z])ada([^a-z]|$)", name):
        return "ada"
    return "unknown"


def probe_hardware_identity() -> dict[str, Any]:
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
                        "queried_capabilities": {
                            "compute_capability": compute_cap,
                            "memory_total": memory,
                        },
                        "hardware_tier": classify_hardware_tier(name),
                    }
                )
    return {"source": source, "adapters": adapters}


def pending_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task": TASK,
        "verification_status": "pending_unverified",
        "profile_id": COMMON_PROFILE_ID,
        "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
        "ticks_per_second": TICKS_PER_SECOND,
        "qualifications": {
            "ada": {
                "coverage_target": "desktop_ada_representative",
                "evidence_role": "fixed_tick_correctness",
                "status": "pending_unverified",
                "reason": (
                    "No representative desktop Ada fixed-tick capture of the "
                    "identical NW-06 common artifact is recorded."
                ),
            },
            "blackwell": {
                "coverage_target": "desktop_blackwell_representative",
                "evidence_role": "fixed_tick_correctness",
                "status": "pending_unverified",
                "reason": (
                    "No representative desktop Blackwell fixed-tick capture of "
                    "the identical NW-06 common artifact is recorded."
                ),
            },
        },
        "executions": [],
        "captures": [],
        "unavailable_telemetry": list(REQUIRED_CAPTURE_METADATA),
        "toolchain_manifest_sha256": "unavailable",
        "policy": (
            "NW-06 fixed-tick evidence requires the identical common artifact "
            "on representative desktop Ada and Blackwell hardware. Missing "
            "coverage stays pending/unverified; executed failures stay failed. "
            "Adapter/model/generation/tier metadata never selects a target or "
            "changes tolerances, exclusions, or quality/resolution requirements."
        ),
    }


def _artifact_identity(artifact_directory: Path) -> tuple[Path, bytes, str, str]:
    stem = nw05_export._profile_file_stem(COMMON_PROFILE_ID)
    package_path = artifact_directory / f"{stem}.nwp"
    manifest_path = artifact_directory / f"{stem}.manifest.json"
    if not package_path.is_file():
        raise ValueError(f"common package is unavailable: {package_path}")
    if not manifest_path.is_file():
        raise ValueError(f"common manifest is unavailable: {manifest_path}")
    package_bytes = package_path.read_bytes()
    inspection = nwp.inspect_package_bytes(package_bytes, allow_dev=False)
    if not inspection.valid or inspection.package is None:
        raise ValueError("common package is invalid")
    if inspection.package.capability.profile_id != COMMON_PROFILE_ID:
        raise ValueError("common package profile identity changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_key = manifest.get("artifact_identity", "")
    if not artifact_key:
        artifact_key = nw05_export.artifact_key(
            COMMON_PROFILE_ID,
            COMMON_REQUIREMENTS_DIGEST,
            nw05_export.emitted_requirements(inspection.package),
        )
    return package_path, package_bytes, artifact_key, str(
        manifest.get("manifest_sha256", "")
    )


def _missing_metadata(capture: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for name in REQUIRED_CAPTURE_METADATA:
        value = capture.get(name)
        if (
            value is None
            or value == ""
            or value == {}
            or (value == [] and name != "unavailable_telemetry")
        ):
            missing.append(name)
    return missing


def validate_capture(capture: Mapping[str, Any]) -> dict[str, Any]:
    missing = _missing_metadata(capture)
    status = str(capture.get("status", "pending_unverified"))
    if status == "passed" and missing:
        status = "pending_unverified"
    if status not in {"pending_unverified", "passed", "failed"}:
        status = "failed"
    profile_matches = (
        capture.get("profile_id") == COMMON_PROFILE_ID
        and capture.get("requirements_digest") == COMMON_REQUIREMENTS_DIGEST
    )
    if status == "passed" and not profile_matches:
        status = "failed"
    return {
        "status": status,
        "missing_metadata": missing,
        "profile_matches": profile_matches,
        "metadata": dict(capture),
    }


def record_capture(
    *,
    artifact_directory: Path,
    executable: Path,
    tier: str,
    coverage_target: str,
    evidence_role: str,
    status: str,
    metadata: Mapping[str, Any] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    if tier not in {"ada", "blackwell"}:
        raise ValueError(f"unsupported evidence tier: {tier}")
    if status not in {"passed", "failed"}:
        raise ValueError(f"unsupported execution status: {status}")
    _, package_bytes, artifact_key, manifest_sha256 = _artifact_identity(
        artifact_directory
    )
    if not executable.is_file():
        raise ValueError(f"executed evidence requires an existing player: {executable}")
    capture = dict(metadata or {})
    capture.update(
        {
            "capture_id": f"{tier}-{hashlib.sha256(executable.read_bytes()).hexdigest()[:16]}",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "tier_metadata_only": tier,
            "coverage_target": coverage_target,
            "evidence_role": evidence_role,
            "status": status,
            "artifact_key": artifact_key,
            "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
            "executable_sha256": _sha256_file(executable),
            "manifest_sha256": manifest_sha256,
            "profile_id": COMMON_PROFILE_ID,
            "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
            "ticks_per_second": TICKS_PER_SECOND,
            "cache_state": capture.get("cache_state", "unknown"),
            "capture_protocol": capture.get(
                "capture_protocol", "nw06-fixed-tick-capture.v1"
            ),
            "unavailable_telemetry": capture.get("unavailable_telemetry", []),
        }
    )
    validated = validate_capture(capture)
    record = {
        "schema_version": "1.0.0",
        "task": TASK,
        "verification_status": (
            "executed_failure"
            if validated["status"] == "failed"
            else (
                "execution_recorded"
                if validated["status"] == "passed"
                else "pending_unverified"
            )
        ),
        "profile_id": COMMON_PROFILE_ID,
        "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
        "ticks_per_second": TICKS_PER_SECOND,
        "captures": [capture],
        "executions": [capture],
        "validation": validated,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return record


def validate_evidence_record(record: Mapping[str, Any]) -> dict[str, Any]:
    captures = []
    for capture in record.get("captures", record.get("executions", [])):
        captures.append(validate_capture(capture))
    if not captures:
        return {
            "status": "pending_unverified",
            "reason": "no hardware captures recorded",
            "captures": [],
        }
    statuses = {item["status"] for item in captures}
    if "failed" in statuses:
        status = "failed"
    elif "pending_unverified" in statuses or len(captures) < 2:
        status = "pending_unverified"
    else:
        identities = {
            (
                item["metadata"].get("artifact_key"),
                item["metadata"].get("package_sha256"),
                item["metadata"].get("manifest_sha256"),
                item["metadata"].get("executable_sha256"),
            )
            for item in captures
        }
        if len(identities) != 1:
            status = "failed"
            return {
                "status": status,
                "reason": "captures do not reference the identical common artifact",
                "captures": captures,
            }
        coverage = {
            str(item["metadata"].get("coverage_target", ""))
            for item in captures
        }
        has_ada = any("ada" in value for value in coverage)
        has_blackwell = any("blackwell" in value for value in coverage)
        status = "verified" if has_ada and has_blackwell else "pending_unverified"
    return {"status": status, "captures": captures}


record_execution = record_capture
validate_evidence = validate_evidence_record


__all__ = [
    "REQUIRED_CAPTURE_METADATA",
    "classify_hardware_tier",
    "probe_hardware_identity",
    "pending_record",
    "validate_capture",
    "record_capture",
    "validate_evidence_record",
    "record_execution",
    "validate_evidence",
]
