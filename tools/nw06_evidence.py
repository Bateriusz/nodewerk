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
import math
import os
import re
import shutil
import subprocess
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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

# Authoritative evidence contract.  Callers may report stricter results, but
# they may not relax these bounds or change the proof resolution.
AUTHORITATIVE_NUMERIC_ABSOLUTE_TOLERANCE = 1e-5
AUTHORITATIVE_NUMERIC_RELATIVE_TOLERANCE = 1e-4
AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE = 1e-4
AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE = 1e-4
AUTHORITATIVE_CAPTURE_WIDTH = 256
AUTHORITATIVE_CAPTURE_HEIGHT = 256
AUTHORITATIVE_CAPTURE_FORMAT = "rgba32f"
AUTHORITATIVE_CAPTURE_KIND = "rgba32f_raw"
CAPTURE_PROTOCOL = "nw06-fixed-tick-capture.v1"

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


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _ticks_match(value: Any, fixed_ticks: Any) -> bool:
    if not _valid_fixed_ticks(value) or not _valid_fixed_ticks(fixed_ticks):
        return False
    return set(value) == set(fixed_ticks) and len(value) == len(fixed_ticks)


def _read_rgba32f(path: Path, expected_bytes: int) -> array | None:
    data = path.read_bytes()
    if len(data) != expected_bytes or len(data) % 4 != 0:
        return None
    values = array("f")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _compare_float_samples(
    captured: Sequence[float],
    reference: Sequence[float],
    count: int,
    *,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> tuple[str, str, float, float]:
    if count <= 0 or count > len(captured) or count > len(reference):
        return "missing", "comparison sample count is outside the captured data", 0.0, 0.0
    maximum_absolute = 0.0
    maximum_relative = 0.0
    for index in range(count):
        actual = _number(captured[index])
        expected = _number(reference[index])
        if actual is None or expected is None:
            return "failed", "captured/reference data contains a non-finite value", 0.0, 0.0
        difference = abs(actual - expected)
        relative = difference / max(1e-30, abs(expected))
        maximum_absolute = max(maximum_absolute, difference)
        maximum_relative = max(maximum_relative, relative)
        if (
            difference > absolute_tolerance
            and relative > relative_tolerance
        ):
            return (
                "failed",
                "captured data exceeds the absolute OR relative tolerance",
                maximum_absolute,
                maximum_relative,
            )
    return "passed", "", maximum_absolute, maximum_relative


def _recorded_numeric_violation(section: Mapping[str, Any]) -> bool:
    if str(section.get("status", "")).strip().lower() == "failed":
        return True
    absolute = _number(section.get("maximum_error"))
    absolute_tolerance = _number(section.get("tolerance"))
    relative = _number(section.get("maximum_relative_error"))
    relative_tolerance = _number(section.get("relative_tolerance"))
    if absolute is not None and absolute < 0.0:
        return True
    if relative is not None and relative < 0.0:
        return True
    if (
        absolute_tolerance is not None
        and absolute_tolerance > AUTHORITATIVE_NUMERIC_ABSOLUTE_TOLERANCE
    ):
        return True
    if (
        relative_tolerance is not None
        and relative_tolerance > AUTHORITATIVE_NUMERIC_RELATIVE_TOLERANCE
    ):
        return True
    if absolute is not None and absolute_tolerance is not None:
        if relative is not None and relative_tolerance is not None:
            return not (
                absolute <= absolute_tolerance
                or relative <= relative_tolerance
            )
        return absolute > absolute_tolerance
    return False


def _recorded_image_violation(section: Mapping[str, Any]) -> bool:
    if str(section.get("status", "")).strip().lower() == "failed":
        return True
    absolute = _number(section.get("maximum_error"))
    tolerance = _number(section.get("tolerance"))
    relative = _number(section.get("maximum_relative_error"))
    if absolute is not None and absolute < 0.0:
        return True
    if relative is not None and relative < 0.0:
        return True
    if tolerance is not None and tolerance > AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE:
        return True
    if absolute is not None and tolerance is not None:
        if relative is not None:
            return not (
                absolute <= tolerance
                or relative <= AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE
            )
        return absolute > tolerance
    if relative is not None and relative > AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE:
        return True
    return False


def _recorded_failure(value: Any) -> str | None:
    """Return a failure reason without letting incomplete fields hide it."""
    if not isinstance(value, Mapping):
        return None
    if str(value.get("status", "")).strip().lower() == "failed":
        return "comparison is marked failed"
    for name, check in (
        ("numeric", _recorded_numeric_violation),
        ("image", _recorded_image_violation),
    ):
        section = value.get(name)
        if isinstance(section, Mapping) and check(section):
            return f"{name} comparison records a failure or tolerance violation"
    tolerance = value.get("tolerance_check")
    if isinstance(tolerance, Mapping):
        if str(tolerance.get("status", "")).strip().lower() == "failed":
            return "tolerance check is marked failed"
        observed = _number(tolerance.get("observed_maximum_error"))
        declared = _number(tolerance.get("declared_tolerance"))
        if (
            observed is not None
            and declared is not None
            and (observed < 0.0 or observed > declared)
        ):
            return "tolerance check records an executed violation"
        if (
            declared is not None
            and declared > AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE
        ):
            return "tolerance check relaxed the authoritative tolerance"
    per_tick = value.get("per_tick")
    if isinstance(per_tick, (list, tuple)):
        for row in per_tick:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("status", "")).strip().lower() == "failed":
                return "a per-tick comparison is marked failed"
            numeric = row.get("numeric")
            image = row.get("image")
            if isinstance(numeric, Mapping) and _recorded_numeric_violation(numeric):
                return "a per-tick numeric comparison records a violation"
            if isinstance(image, Mapping) and _recorded_image_violation(image):
                return "a per-tick image comparison records a violation"
    return None


def _artifact_state(
    capture: Mapping[str, Any], artifact_root: Path | None
) -> tuple[str, str]:
    """Classify capture artifacts as passed, failed, or missing."""
    value = capture.get("capture_artifacts")
    fixed_ticks = capture.get("fixed_ticks")
    if not isinstance(value, (list, tuple)) or not value:
        return "missing", "capture artifacts are absent"
    ticks: list[int] = []
    for item in value:
        if not isinstance(item, Mapping):
            return "failed", "capture artifact descriptor is malformed"
        kind = item.get("kind")
        format_name = item.get("format")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("bytes")
        tick = item.get("tick")
        width = item.get("width")
        height = item.get("height")
        if not isinstance(kind, str) or not kind.strip():
            return "failed", "capture artifact kind is malformed"
        if not isinstance(format_name, str) or not format_name.strip():
            return "failed", "capture artifact format is malformed"
        if not isinstance(path, str) or not path.strip():
            return "failed", "capture artifact path is malformed"
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            return "failed", "capture artifact digest is invalid"
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return "failed", "capture artifact byte size is invalid"
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            return "failed", "capture artifact tick is invalid"
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            return "failed", "capture artifact resolution is invalid"
        ticks.append(tick)
    if not _valid_fixed_ticks(fixed_ticks):
        return "missing", "fixed tick coverage is absent"
    if set(ticks) != set(fixed_ticks) or len(ticks) != len(fixed_ticks):
        return "failed", "capture artifacts do not cover every fixed tick"
    if len(set(ticks)) != len(ticks):
        return "failed", "capture artifacts repeat a fixed tick"
    for item in value:
        width = int(item["width"])
        height = int(item["height"])
        if (width, height) != (
            AUTHORITATIVE_CAPTURE_WIDTH,
            AUTHORITATIVE_CAPTURE_HEIGHT,
        ):
            return "failed", "capture artifact resolution violates the proof contract"
        kind = str(item.get("kind", "")).lower()
        format_name = str(item.get("format", "")).lower()
        if kind != AUTHORITATIVE_CAPTURE_KIND:
            return "failed", "capture artifact kind violates the proof contract"
        if format_name != AUTHORITATIVE_CAPTURE_FORMAT:
            return "failed", "capture artifact format violates the proof contract"
        expected_bytes = width * height * 16
        if int(item["bytes"]) != expected_bytes:
            return "failed", "capture artifact byte size does not match rgba32f"
    if artifact_root is None:
        return "missing", "capture artifact contents were not supplied for verification"
    root = Path(artifact_root).resolve()
    for item in value:
        artifact_path = (root / str(item["path"])).resolve()
        try:
            artifact_path.relative_to(root)
        except ValueError:
            return "failed", "capture artifact path escapes the evidence root"
        if not artifact_path.is_file():
            return "missing", f"capture artifact is unavailable: {artifact_path}"
        if artifact_path.stat().st_size != int(item["bytes"]):
            return "failed", "capture artifact byte size does not match the file"
        if _sha256_file(artifact_path) != str(item["sha256"]):
            return "failed", "capture artifact digest does not match the file"
    return "passed", ""


def _reference_state(
    reference: Any,
    capture: Mapping[str, Any],
    tick: int,
    artifact_root: Path | None,
) -> tuple[str, str, Path | None]:
    """Verify a per-tick reference descriptor and its execution provenance."""
    if not isinstance(reference, Mapping):
        return "missing", "reference artifact descriptor is absent", None
    if str(reference.get("kind", "")).lower() != AUTHORITATIVE_CAPTURE_KIND:
        return "failed", "reference artifact kind violates the proof contract", None
    if str(reference.get("format", "")).lower() != AUTHORITATIVE_CAPTURE_FORMAT:
        return "failed", "reference artifact format violates the proof contract", None
    if (
        reference.get("width") != AUTHORITATIVE_CAPTURE_WIDTH
        or reference.get("height") != AUTHORITATIVE_CAPTURE_HEIGHT
        or reference.get("tick") != tick
    ):
        return "failed", "reference artifact tick/resolution is wrong", None
    path = reference.get("path")
    digest = reference.get("sha256")
    size = reference.get("bytes")
    if not isinstance(path, str) or not path.strip():
        return "missing", "reference artifact path is absent", None
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        return "failed", "reference artifact digest is invalid", None
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size != AUTHORITATIVE_CAPTURE_WIDTH
        * AUTHORITATIVE_CAPTURE_HEIGHT
        * 16
    ):
        return "failed", "reference artifact byte size is invalid", None
    provenance = reference.get("provenance")
    if not isinstance(provenance, Mapping):
        return "missing", "reference artifact provenance is absent", None
    for key in (
        "artifact_key",
        "package_sha256",
        "executable_sha256",
        "manifest_sha256",
        "profile_id",
        "requirements_digest",
        "toolchain_identity",
    ):
        if str(provenance.get(key, "")) != str(capture.get(key, "")):
            return (
                "failed",
                f"reference artifact provenance does not match capture {key}",
                None,
            )
    if artifact_root is None:
        return "missing", "reference artifact contents were not supplied", None
    root = Path(artifact_root).resolve()
    reference_path = (root / path).resolve()
    try:
        reference_path.relative_to(root)
    except ValueError:
        return "failed", "reference artifact path escapes the evidence root", None
    if not reference_path.is_file():
        return "missing", "reference artifact is unavailable", None
    if reference_path.stat().st_size != size:
        return "failed", "reference artifact byte size does not match the file", None
    if _sha256_file(reference_path) != digest:
        return "failed", "reference artifact digest does not match the file", None
    return "passed", "", reference_path


def _per_tick_comparison_state(
    value: Any,
    capture: Mapping[str, Any],
    artifact_root: Path | None,
) -> tuple[str, str, dict[str, float]]:
    """Compute per-tick comparisons from captured and reference bytes."""
    if not isinstance(value, (list, tuple)) or not value:
        return "missing", "per-tick comparison evidence is absent", {}
    failure = _recorded_failure({"per_tick": value})
    if failure is not None:
        return "failed", failure, {}
    fixed_ticks = capture.get("fixed_ticks")
    if not _valid_fixed_ticks(fixed_ticks):
        return "missing", "fixed tick coverage is absent", {}
    capture_artifacts = capture.get("capture_artifacts")
    if not isinstance(capture_artifacts, (list, tuple)) or not capture_artifacts:
        return (
            "missing",
            "capture artifacts are absent, so per-tick comparisons cannot be "
            "bound to captured bytes",
            {},
        )
    artifact_by_tick: dict[int, Mapping[str, Any]] = {}
    for artifact in capture_artifacts:
        if not isinstance(artifact, Mapping):
            continue
        tick = artifact.get("tick")
        if isinstance(tick, int) and not isinstance(tick, bool):
            artifact_by_tick[tick] = artifact

    expected_ticks = set(fixed_ticks)
    seen_ticks: set[int] = set()
    max_numeric_absolute = 0.0
    max_numeric_relative = 0.0
    max_image_absolute = 0.0
    max_numeric_absolute_reported = 0.0
    max_numeric_relative_reported = 0.0
    max_image_absolute_reported = 0.0

    for row in value:
        if not isinstance(row, Mapping):
            return "missing", "a per-tick comparison row is malformed", {}
        tick = row.get("tick")
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            return "missing", "a per-tick comparison tick is invalid", {}
        if tick not in expected_ticks or tick in seen_ticks:
            return "failed", "per-tick comparison coverage is duplicated or unexpected", {}
        seen_ticks.add(tick)
        artifact = artifact_by_tick.get(tick)
        if artifact is None:
            return "missing", "per-tick captured artifact is absent", {}
        artifact_sha = row.get("artifact_sha256")
        if artifact.get("sha256") != artifact_sha:
            return "failed", "per-tick comparison is not tied to the captured artifact digest", {}

        numeric = row.get("numeric")
        image = row.get("image")
        if not isinstance(numeric, Mapping) or not isinstance(image, Mapping):
            return "missing", "per-tick numeric/image comparison is incomplete", {}
        for section in (numeric, image):
            section_status = str(section.get("status", "")).strip().lower()
            if section_status == "failed":
                return "failed", "a per-tick comparison is marked failed", {}
            if section_status not in {"passed", "within_tolerance"}:
                return "missing", "a per-tick comparison has no pass/fail result", {}

        reference_state, reference_detail, reference_path = _reference_state(
            row.get("reference"), capture, tick, artifact_root
        )
        if reference_state != "passed" or reference_path is None:
            return reference_state, reference_detail, {}
        if artifact_root is None:
            return "missing", "captured artifact contents were not supplied", {}
        root = Path(artifact_root).resolve()
        captured_path = (root / str(artifact.get("path", ""))).resolve()
        try:
            captured_path.relative_to(root)
        except ValueError:
            return "failed", "captured artifact path escapes the evidence root", {}
        if not captured_path.is_file():
            return "missing", "captured artifact is unavailable", {}
        expected_bytes = (
            AUTHORITATIVE_CAPTURE_WIDTH * AUTHORITATIVE_CAPTURE_HEIGHT * 16
        )
        captured_values = _read_rgba32f(captured_path, expected_bytes)
        reference_values = _read_rgba32f(reference_path, expected_bytes)
        if captured_values is None or reference_values is None:
            return "failed", "captured/reference data is not complete rgba32f", {}

        numeric_tolerance = _number(numeric.get("tolerance"))
        numeric_relative_tolerance = _number(
            numeric.get("relative_tolerance")
        )
        if (
            numeric_tolerance is None
            or numeric_relative_tolerance is None
        ):
            return "missing", "per-tick numeric comparison is incomplete", {}
        if numeric_tolerance < 0.0 or numeric_relative_tolerance < 0.0:
            return "failed", "per-tick numeric comparison reports a negative value", {}
        if numeric_tolerance > AUTHORITATIVE_NUMERIC_ABSOLUTE_TOLERANCE:
            return "failed", "per-tick numeric comparison relaxed the authoritative tolerance", {}
        if numeric_relative_tolerance > AUTHORITATIVE_NUMERIC_RELATIVE_TOLERANCE:
            return "failed", "per-tick numeric comparison relaxed the authoritative relative tolerance", {}

        samples = numeric.get("samples")
        if isinstance(samples, bool) or not isinstance(samples, int) or samples <= 0:
            return "missing", "per-tick numeric sample count is invalid", {}
        numeric_state, numeric_detail, numeric_absolute, numeric_relative = (
            _compare_float_samples(
                captured_values,
                reference_values,
                samples,
                absolute_tolerance=numeric_tolerance,
                relative_tolerance=numeric_relative_tolerance,
            )
        )
        if numeric_state != "passed":
            return numeric_state, numeric_detail, {}
        reported_numeric_absolute = _number(numeric.get("maximum_error"))
        reported_numeric_relative = _number(
            numeric.get("maximum_relative_error")
        )
        if (
            reported_numeric_absolute is None
            or reported_numeric_relative is None
        ):
            return "missing", "per-tick numeric comparison result is incomplete", {}
        if (
            reported_numeric_absolute < numeric_absolute
            or reported_numeric_relative < numeric_relative
        ):
            return "failed", "per-tick numeric summary understates measured error", {}
        max_numeric_absolute_reported = max(
            max_numeric_absolute_reported, reported_numeric_absolute
        )
        max_numeric_relative_reported = max(
            max_numeric_relative_reported, reported_numeric_relative
        )

        image_tolerance = _number(image.get("tolerance"))
        width = image.get("width")
        height = image.get("height")
        if image_tolerance is None:
            return "missing", "per-tick image comparison is incomplete", {}
        if image_tolerance < 0.0:
            return "failed", "per-tick image comparison reports a negative value", {}
        if (width, height) != (
            AUTHORITATIVE_CAPTURE_WIDTH,
            AUTHORITATIVE_CAPTURE_HEIGHT,
        ):
            return "failed", "per-tick image comparison resolution violates the proof contract", {}
        if image_tolerance > AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE:
            return "failed", "per-tick image comparison relaxed the authoritative tolerance", {}
        pixels = image.get("pixels")
        if isinstance(pixels, bool) or not isinstance(pixels, int) or pixels <= 0:
            return "missing", "per-tick image pixel count is invalid", {}
        if pixels != AUTHORITATIVE_CAPTURE_WIDTH * AUTHORITATIVE_CAPTURE_HEIGHT:
            return "failed", "per-tick image comparison does not cover every pixel", {}
        image_state, image_detail, image_absolute, image_relative = (
            _compare_float_samples(
                captured_values,
                reference_values,
                pixels * 4,
                absolute_tolerance=image_tolerance,
                relative_tolerance=AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE,
            )
        )
        if image_state != "passed":
            return image_state, image_detail, {}
        reported_image_absolute = _number(image.get("maximum_error"))
        if reported_image_absolute is None:
            return "missing", "per-tick image comparison result is incomplete", {}
        if reported_image_absolute < image_absolute:
            return "failed", "per-tick image summary understates measured error", {}
        max_image_absolute_reported = max(
            max_image_absolute_reported, reported_image_absolute
        )

        max_numeric_absolute = max(max_numeric_absolute, numeric_absolute)
        max_numeric_relative = max(max_numeric_relative, numeric_relative)
        max_image_absolute = max(max_image_absolute, image_absolute)

    if seen_ticks != expected_ticks or len(value) != len(fixed_ticks):
        return "failed", "per-tick comparison does not cover every fixed tick exactly once", {}
    return (
        "passed",
        "",
        {
            "numeric_absolute": max_numeric_absolute,
            "numeric_relative": max_numeric_relative,
            "image_absolute": max_image_absolute,
            "numeric_absolute_reported": max_numeric_absolute_reported,
            "numeric_relative_reported": max_numeric_relative_reported,
            "image_absolute_reported": max_image_absolute_reported,
        },
    )


def _comparison_state(
    value: Any,
    capture: Mapping[str, Any],
    artifact_root: Path | None,
) -> tuple[str, str]:
    """Classify numeric/image comparison evidence as passed, failed, or missing."""
    if not isinstance(value, Mapping):
        return "missing", "comparison evidence is absent"
    failure = _recorded_failure(value)
    if failure is not None:
        return "failed", failure
    if value.get("validated") is not True:
        return "missing", "comparison was not validated"
    status = str(value.get("status", "")).strip().lower()
    if status not in {"passed", "within_tolerance"}:
        return "missing", "comparison status is absent or unknown"
    numeric = value.get("numeric")
    image = value.get("image")
    tolerance = value.get("tolerance_check")
    if not all(isinstance(item, Mapping) for item in (numeric, image, tolerance)):
        return "missing", "numeric/image/tolerance comparison sections are incomplete"
    for section in (numeric, image, tolerance):
        section_status = str(section.get("status", "")).strip().lower()
        if section_status not in {"passed", "within_tolerance"}:
            return "missing", "a comparison section has no pass/fail result"
    fixed_ticks = capture.get("fixed_ticks")
    if not _valid_fixed_ticks(fixed_ticks):
        return "missing", "fixed tick coverage is absent"
    comparison_ticks = value.get("fixed_ticks")
    if comparison_ticks is None:
        return "missing", "comparison fixed-tick coverage is absent"
    if not _ticks_match(comparison_ticks, fixed_ticks):
        return "failed", "comparison fixed ticks do not match the capture"
    per_tick_state, per_tick_detail, per_tick_maxima = _per_tick_comparison_state(
        value.get("per_tick"), capture, artifact_root
    )
    if per_tick_state != "passed":
        return per_tick_state, per_tick_detail
    for section, name in (
        (numeric, "numeric"),
        (image, "image"),
        (tolerance, "tolerance"),
    ):
        if not _ticks_match(section.get("ticks"), fixed_ticks):
            return "failed", f"{name} comparison does not cover every fixed tick"
    samples = numeric.get("samples")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples <= 0:
        return "missing", "numeric comparison sample count is invalid"
    if samples < len(fixed_ticks):
        return "failed", "numeric comparison does not sample every fixed tick"
    numeric_observed = _number(numeric.get("maximum_error"))
    numeric_declared = _number(numeric.get("tolerance"))
    numeric_relative = _number(numeric.get("maximum_relative_error"))
    relative_declared = _number(numeric.get("relative_tolerance"))
    if None in (
        numeric_observed,
        numeric_declared,
        numeric_relative,
        relative_declared,
    ):
        return "missing", "numeric comparison result is incomplete"
    assert numeric_observed is not None
    assert numeric_declared is not None
    assert numeric_relative is not None
    assert relative_declared is not None
    if (
        numeric_observed < 0.0
        or numeric_declared < 0.0
        or numeric_relative < 0.0
        or relative_declared < 0.0
    ):
        return "failed", "numeric comparison reported a negative error/tolerance"
    if numeric_declared > AUTHORITATIVE_NUMERIC_ABSOLUTE_TOLERANCE:
        return "failed", "numeric comparison relaxed the authoritative tolerance"
    if relative_declared > AUTHORITATIVE_NUMERIC_RELATIVE_TOLERANCE:
        return "failed", "numeric comparison relaxed the authoritative relative tolerance"
    if not (
        numeric_observed <= numeric_declared
        or numeric_relative <= relative_declared
    ):
        return "failed", "numeric comparison exceeded the absolute OR relative tolerance"
    if numeric_observed < max(
        per_tick_maxima["numeric_absolute"],
        per_tick_maxima["numeric_absolute_reported"],
    ):
        return "failed", "numeric summary understates a per-tick absolute error"
    if numeric_relative < max(
        per_tick_maxima["numeric_relative"],
        per_tick_maxima["numeric_relative_reported"],
    ):
        return "failed", "numeric summary understates a per-tick relative error"

    pixels = image.get("pixels")
    width = image.get("width")
    height = image.get("height")
    if (
        isinstance(pixels, bool)
        or not isinstance(pixels, int)
        or pixels <= 0
        or isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
    ):
        return "missing", "image comparison dimensions are incomplete"
    if (width, height) != (
        AUTHORITATIVE_CAPTURE_WIDTH,
        AUTHORITATIVE_CAPTURE_HEIGHT,
    ):
        return "failed", "image comparison resolution violates the proof contract"
    if pixels != width * height:
        return "failed", "image comparison does not cover every pixel"
    image_observed = _number(image.get("maximum_error"))
    image_declared = _number(image.get("tolerance"))
    if image_observed is None or image_declared is None:
        return "missing", "image comparison result is incomplete"
    if image_observed < 0.0 or image_declared < 0.0:
        return "failed", "image comparison reported a negative error/tolerance"
    if image_declared > AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE:
        return "failed", "image comparison relaxed the authoritative tolerance"
    image_relative = _number(image.get("maximum_relative_error"))
    if not (
        image_observed <= image_declared
        or (
            image_relative is not None
            and image_relative <= AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE
        )
    ):
        return "failed", "image comparison exceeded the absolute OR relative tolerance"
    if image_observed < max(
        per_tick_maxima["image_absolute"],
        per_tick_maxima["image_absolute_reported"],
    ):
        return "failed", "image summary understates a per-tick error"
    if "maximum_relative_error" in image or "relative_tolerance" in image:
        image_relative = _number(image.get("maximum_relative_error"))
        relative_declared = _number(image.get("relative_tolerance"))
        if image_relative is None or relative_declared is None:
            return "missing", "relative image comparison result is incomplete"
        if image_relative < 0.0 or relative_declared < 0.0:
            return "failed", "relative image comparison reported a negative error/tolerance"
        if relative_declared > AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE:
            return "failed", "image comparison relaxed the authoritative relative tolerance"
        if image_relative > relative_declared or image_relative > AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE:
            return "failed", "image comparison exceeded the declared/authoritative relative tolerance"

    observed = _number(tolerance.get("observed_maximum_error"))
    declared = _number(tolerance.get("declared_tolerance"))
    if observed is None or declared is None:
        return "missing", "tolerance check result is incomplete"
    if observed < 0.0 or declared < 0.0:
        return "failed", "tolerance check reported a negative error/tolerance"
    if declared > AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE:
        return "failed", "tolerance check relaxed the authoritative tolerance"
    if observed > declared or observed > AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE:
        return "failed", "tolerance check violated the authoritative tolerance"
    if observed < image_observed:
        return (
            "failed",
            "tolerance check understates the aggregate image maximum error",
        )
    if observed < max(
        per_tick_maxima["image_absolute"],
        per_tick_maxima["image_absolute_reported"],
    ):
        return "failed", "tolerance summary understates a per-tick image error"
    return "passed", ""


def _missing_metadata(
    capture: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
) -> list[str]:
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
    artifact_state, _ = _artifact_state(capture, artifact_root)
    if "capture_artifacts" not in missing and artifact_state == "missing":
        missing.append("capture_artifacts")
    comparison_state, _ = _comparison_state(
        capture.get("comparison"), capture, artifact_root
    )
    if "comparison" not in missing and comparison_state == "missing":
        missing.append("comparison")
    return missing


def validate_capture(
    capture: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    missing = _missing_metadata(capture, artifact_root=artifact_root)
    artifact_state, artifact_detail = _artifact_state(capture, artifact_root)
    comparison_state, comparison_detail = _comparison_state(
        capture.get("comparison"), capture, artifact_root
    )
    status = str(capture.get("status", "pending_unverified"))
    if artifact_state == "failed" or comparison_state == "failed":
        status = "failed"
    elif status == "passed" and missing:
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
    if status == "passed" and capture_protocol != CAPTURE_PROTOCOL:
        status = "pending_unverified"
    return {
        "status": status,
        "missing_metadata": missing,
        "artifact_state": artifact_state,
        "artifact_detail": artifact_detail,
        "comparison_state": comparison_state,
        "comparison_detail": comparison_detail,
        "profile_matches": profile_matches,
        "capture_protocol_matches": (
            capture_protocol == CAPTURE_PROTOCOL
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
                "capture_protocol", CAPTURE_PROTOCOL
            ),
            "unavailable_telemetry": capture.get("unavailable_telemetry", []),
        }
    )
    comparison = capture.get("comparison")
    if isinstance(comparison, Mapping):
        for row in comparison.get("per_tick", ()):
            if not isinstance(row, dict):
                continue
            reference = row.get("reference")
            if isinstance(reference, dict):
                reference["provenance"] = {
                    key: capture[key]
                    for key in (
                        "artifact_key",
                        "package_sha256",
                        "executable_sha256",
                        "manifest_sha256",
                        "profile_id",
                        "requirements_digest",
                        "toolchain_identity",
                    )
                }
    # Capture artifact paths are relative to the explicit artifact directory,
    # not to wherever the evidence JSON happens to be written.
    artifact_root = artifact_directory
    validated = validate_capture(capture, artifact_root=artifact_root)
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


def validate_evidence_record(
    record: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    raw_captures = record.get("captures")
    if not raw_captures:
        raw_captures = record.get("executions", [])
    captures = []
    for capture in raw_captures:
        captures.append(
            validate_capture(capture, artifact_root=artifact_root)
        )
    qualification_statuses: dict[str, str] = {}
    qualifications_complete = True
    qualification_map = record.get("qualifications")
    if not isinstance(qualification_map, Mapping):
        qualification_map = {}
    for tier, target in FIXED_TICK_COVERAGE_TARGETS.items():
        declared_qualification = (
            qualification_map.get(tier, {})
            if isinstance(qualification_map, Mapping)
            else {}
        )
        if not isinstance(declared_qualification, Mapping):
            qualifications_complete = False
            qualification_statuses[tier] = "pending_unverified"
            continue
        declared_status = (
            str(declared_qualification.get("status"))
            if isinstance(declared_qualification, Mapping)
            and "status" in declared_qualification
            else None
        )
        declared_coverage = declared_qualification.get("coverage_target")
        declared_role = declared_qualification.get("evidence_role")
        if (
            declared_coverage is None
            or declared_role is None
            or declared_status is None
        ):
            qualifications_complete = False
            qualification_statuses[tier] = "pending_unverified"
            continue
        if (
            declared_coverage != target
            or declared_role != FIXED_TICK_EVIDENCE_ROLE
            or declared_status
            not in {
                "pending_unverified",
                "execution_recorded",
                "failed",
                "verified",
            }
        ):
            qualifications_complete = False
            qualification_statuses[tier] = "failed"
            continue
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
    elif not qualifications_complete:
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
    "AUTHORITATIVE_NUMERIC_ABSOLUTE_TOLERANCE",
    "AUTHORITATIVE_NUMERIC_RELATIVE_TOLERANCE",
    "AUTHORITATIVE_IMAGE_ABSOLUTE_TOLERANCE",
    "AUTHORITATIVE_IMAGE_RELATIVE_TOLERANCE",
    "AUTHORITATIVE_CAPTURE_WIDTH",
    "AUTHORITATIVE_CAPTURE_HEIGHT",
    "AUTHORITATIVE_CAPTURE_FORMAT",
    "AUTHORITATIVE_CAPTURE_KIND",
    "CAPTURE_PROTOCOL",
    "classify_hardware_tier",
    "probe_hardware_identity",
    "pending_record",
    "validate_capture",
    "record_capture",
    "validate_evidence_record",
    "record_execution",
    "validate_evidence",
]
