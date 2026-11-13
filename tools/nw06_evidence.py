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
FIXED_TICK_EVIDENCE_ROLE = "fixed_tick_correctness"
FIXED_TICK_COVERAGE_TARGETS = {
    "ada": "desktop_ada_representative",
    "blackwell": "desktop_blackwell_representative",
}

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
    # A caller-supplied claim of "passed" is never evidence.  Fixed-tick
    # captures must name the evaluated ticks, preserve capture artifacts, and
    # carry validated numeric/image comparisons and tolerance checks.
    "fixed_ticks",
    "capture_artifacts",
    "comparison",
)

REQUIRED_TELEMETRY_METADATA = (
    "queried_capabilities",
    "clock_information",
    "power_information",
    "thermal_information",
    "cache_state",
)

_UNAVAILABLE_VALUE_MARKERS = {
    "",
    "unavailable",
    "unknown",
    "missing",
    "not_available",
    "not-applicable",
    "pending",
}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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


def _value_is_unavailable(value: Any) -> bool:
    if isinstance(value, Mapping):
        if not value:
            return True
        for marker_name in ("status", "state", "availability"):
            marker = value.get(marker_name)
            if isinstance(marker, str) and marker.strip().lower() in (
                _UNAVAILABLE_VALUE_MARKERS
            ):
                return True
        if value.get("available") is False:
            return True
        return False
    if isinstance(value, str):
        return value.strip().lower() in _UNAVAILABLE_VALUE_MARKERS
    return value is None


def _valid_fixed_ticks(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    ticks: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return False
        if item < 0 or item > (1 << 64) - 1:
            return False
        ticks.append(item)
    return len(ticks) == len(set(ticks))


def _valid_capture_artifacts(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    for item in value:
        if not isinstance(item, Mapping):
            return False
        kind = item.get("kind")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("bytes")
        if not isinstance(kind, str) or not kind.strip():
            return False
        if not isinstance(path, str) or not path.strip():
            return False
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            return False
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return False
    return True


def _comparison_passed(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("validated") is not True:
        return False
    if str(value.get("status", "")).strip().lower() not in {
        "passed",
        "within_tolerance",
    }:
        return False
    numeric = value.get("numeric")
    image = value.get("image")
    tolerance = value.get("tolerance_check")
    if not all(isinstance(item, Mapping) for item in (numeric, image, tolerance)):
        return False
    for section, count_name in ((numeric, "samples"), (image, "pixels")):
        if str(section.get("status", "")).strip().lower() not in {
            "passed",
            "within_tolerance",
        }:
            return False
        count = section.get(count_name)
        maximum = section.get("maximum_error")
        declared = section.get("tolerance")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return False
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, (int, float))
            or isinstance(declared, bool)
            or not isinstance(declared, (int, float))
            or maximum < 0.0
            or declared < 0.0
            or maximum > declared
        ):
            return False
    if str(tolerance.get("status", "")).strip().lower() not in {
        "passed",
        "within_tolerance",
    }:
        return False
    observed = tolerance.get("observed_maximum_error")
    declared = tolerance.get("declared_tolerance")
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or isinstance(declared, bool)
        or not isinstance(declared, (int, float))
        or observed < 0.0
        or declared < 0.0
        or observed > declared
    ):
        return False
    return True


def _missing_metadata(capture: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    unavailable = {
        str(item)
        for item in (capture.get("unavailable_telemetry") or [])
        if isinstance(item, str)
    }
    for name in REQUIRED_CAPTURE_METADATA:
        value = capture.get(name)
        if (
            name in unavailable
            or _value_is_unavailable(value)
            or (value == [] and name != "unavailable_telemetry")
        ):
            missing.append(name)
            continue
        if name in REQUIRED_TELEMETRY_METADATA and name != "cache_state":
            if not isinstance(value, Mapping) or not value:
                missing.append(name)
        elif name == "cache_state":
            if _value_is_unavailable(value):
                missing.append(name)
    if "fixed_ticks" not in missing and not _valid_fixed_ticks(
        capture.get("fixed_ticks")
    ):
        missing.append("fixed_ticks")
    if "capture_artifacts" not in missing and not _valid_capture_artifacts(
        capture.get("capture_artifacts")
    ):
        missing.append("capture_artifacts")
    if "comparison" not in missing and not _comparison_passed(
        capture.get("comparison")
    ):
        missing.append("comparison")
    return missing


def validate_capture(capture: Mapping[str, Any]) -> dict[str, Any]:
    missing = _missing_metadata(capture)
    status = str(capture.get("status", "pending_unverified"))
    if status == "passed" and missing:
        status = "pending_unverified"
    if status not in {"pending_unverified", "passed", "failed"}:
        status = "failed"
    evidence_role = capture.get("evidence_role")
    coverage_target = capture.get("coverage_target")
    role_matches = evidence_role == FIXED_TICK_EVIDENCE_ROLE
    coverage_matches = coverage_target in set(
        FIXED_TICK_COVERAGE_TARGETS.values()
    )
    # A malformed role/target is not merely missing coverage: it cannot be
    # accepted as fixed-tick correctness evidence.
    if (evidence_role is not None and not role_matches) or (
        coverage_target is not None and not coverage_matches
    ):
        status = "failed"
    profile_matches = (
        capture.get("profile_id") == COMMON_PROFILE_ID
        and capture.get("requirements_digest") == COMMON_REQUIREMENTS_DIGEST
    )
    if status == "passed" and not profile_matches:
        status = "failed"
    capture_protocol = capture.get("capture_protocol")
    if status == "passed" and capture_protocol != "nw06-fixed-tick-capture.v1":
        status = "pending_unverified"
    return {
        "status": status,
        "missing_metadata": missing,
        "profile_matches": profile_matches,
        "capture_protocol_matches": (
            capture_protocol == "nw06-fixed-tick-capture.v1"
        ),
        "evidence_role": evidence_role,
        "coverage_target": coverage_target,
        "fixed_tick_role_matches": role_matches,
        "fixed_tick_coverage_matches": coverage_matches,
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
    if coverage_target != FIXED_TICK_COVERAGE_TARGETS[tier]:
        raise ValueError(
            f"tier '{tier}' requires coverage target "
            f"'{FIXED_TICK_COVERAGE_TARGETS[tier]}', got '{coverage_target}'"
        )
    if evidence_role != FIXED_TICK_EVIDENCE_ROLE:
        raise ValueError(
            f"evidence role must be '{FIXED_TICK_EVIDENCE_ROLE}'"
        )
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
    record = pending_record()
    if output_path is not None and output_path.is_file():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            # Preserve recorded evidence while regenerating required metadata.
            record.update(existing)
            record.setdefault("policy", pending_record()["policy"])
    # Merge the complete pending qualification map so a pre-existing file that
    # contains no qualifications (or only one tier) still gains every
    # schema-required tier.
    pending_qualifications = pending_record()["qualifications"]
    existing_qualifications = record.get("qualifications")
    if not isinstance(existing_qualifications, dict):
        existing_qualifications = {}
    merged_qualifications: dict[str, Any] = {}
    for name, default_entry in pending_qualifications.items():
        entry = dict(default_entry)
        prior_entry = existing_qualifications.get(name)
        if isinstance(prior_entry, dict):
            entry.update(prior_entry)
        merged_qualifications[name] = entry
    record["qualifications"] = merged_qualifications
    record["captures"] = list(record.get("captures", [])) + [capture]
    record["executions"] = list(record.get("executions", [])) + [capture]
    record["validation"] = validated
    qualification = record["qualifications"].setdefault(
        tier,
        {
            "coverage_target": FIXED_TICK_COVERAGE_TARGETS[tier],
            "evidence_role": FIXED_TICK_EVIDENCE_ROLE,
            "status": "pending_unverified",
        },
    )
    prior_status = qualification.get("status")
    if validated["status"] == "passed":
        qualification["status"] = (
            "failed" if prior_status == "failed" else "verified"
        )
        qualification["reason"] = (
            "tier retains an earlier executed failure"
            if prior_status == "failed"
            else ""
        )
    elif validated["status"] == "failed":
        qualification["status"] = "failed"
        qualification["reason"] = "executed fixed-tick capture failed"
    else:
        qualification["status"] = "pending_unverified"
        qualification["reason"] = "required capture telemetry is incomplete"
    qualification.update(
        {
            "coverage_target": coverage_target,
            "evidence_role": evidence_role,
            "artifact_key": artifact_key,
            "package_sha256": capture["package_sha256"],
            "executable_sha256": capture["executable_sha256"],
        }
    )
    statuses = {
        name: record["qualifications"].get(name, {}).get("status")
        for name in FIXED_TICK_COVERAGE_TARGETS
    }
    if "failed" in statuses.values() or any(
        item.get("status") == "failed"
        for item in record["captures"]
    ):
        record["verification_status"] = "executed_failure"
    elif all(value == "verified" for value in statuses.values()):
        record["verification_status"] = "verified"
    elif record["captures"]:
        record["verification_status"] = "execution_recorded"
    else:
        record["verification_status"] = "pending_unverified"
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        from schema_validator import validate_file_against_schema

        schema_path = (
            REPO_ROOT / "schemas" / "nw06-hardware-evidence.schema.json"
        )
        try:
            validate_file_against_schema(output_path, schema_path)
        except Exception as exc:
            raise ValueError(
                f"recorded evidence failed schema validation: {exc}"
            ) from exc
    return record


def validate_evidence_record(record: Mapping[str, Any]) -> dict[str, Any]:
    raw_captures = record.get("captures")
    if not raw_captures:
        raw_captures = record.get("executions", [])
    captures = []
    for capture in raw_captures:
        captures.append(validate_capture(capture))
    qualification_statuses: dict[str, str] = {}
    for tier, target in FIXED_TICK_COVERAGE_TARGETS.items():
        qualification_map = record.get("qualifications", {})
        declared_qualification = (
            qualification_map.get(tier, {})
            if isinstance(qualification_map, Mapping)
            else {}
        )
        declared_status = (
            str(declared_qualification.get("status"))
            if isinstance(declared_qualification, Mapping)
            and "status" in declared_qualification
            else None
        )
        matching = [
            item
            for item in captures
            if item["metadata"].get("coverage_target") == target
        ]
        # A qualification record can retain an executed failure, but a bare
        # caller-supplied "verified" label never establishes correctness.  The
        # status is derived from validated captures and comparison evidence.
        if any(item["status"] == "failed" for item in matching):
            qualification_statuses[tier] = "failed"
        elif declared_status == "failed":
            qualification_statuses[tier] = "failed"
        elif declared_status in {"pending_unverified", "execution_recorded"}:
            # A conservative explicit pending status is never upgraded by
            # metadata alone.
            qualification_statuses[tier] = "pending_unverified"
        elif any(item["status"] == "passed" for item in matching):
            qualification_statuses[tier] = "verified"
        else:
            qualification_statuses[tier] = "pending_unverified"
    if not captures:
        return {
            "status": (
                "failed"
                if "failed" in qualification_statuses.values()
                else "pending_unverified"
            ),
            "reason": (
                "a qualification retains an executed failure"
                if "failed" in qualification_statuses.values()
                else "no hardware captures recorded"
            ),
            "captures": [],
            "qualifications": qualification_statuses,
        }
    statuses = {item["status"] for item in captures}
    if "failed" in statuses:
        status = "failed"
    elif "failed" in qualification_statuses.values():
        status = "failed"
    elif "pending_unverified" in statuses or len(captures) < 2:
        status = "pending_unverified"
    elif any(
        value != "verified" for value in qualification_statuses.values()
    ):
        # Complete capture metadata alone cannot override pending
        # qualification records; missing required coverage stays pending.
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
                "qualifications": qualification_statuses,
            }
        coverage = {
            str(item["metadata"].get("coverage_target", ""))
            for item in captures
        }
        has_ada = any("ada" in value for value in coverage)
        has_blackwell = any("blackwell" in value for value in coverage)
        status = "verified" if has_ada and has_blackwell else "pending_unverified"
    return {
        "status": status,
        "captures": captures,
        "qualifications": qualification_statuses,
    }


record_execution = record_capture
validate_evidence = validate_evidence_record


__all__ = [
    "REQUIRED_CAPTURE_METADATA",
    "FIXED_TICK_EVIDENCE_ROLE",
    "FIXED_TICK_COVERAGE_TARGETS",
    "classify_hardware_tier",
    "probe_hardware_identity",
    "pending_record",
    "validate_capture",
    "record_capture",
    "validate_evidence_record",
    "record_execution",
    "validate_evidence",
]
