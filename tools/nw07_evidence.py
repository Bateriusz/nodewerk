#!/usr/bin/env python3
"""NW-07 performance-reference and correctness evidence contracts.

The module is deliberately strict:

* a caller-supplied ``status: passed`` is never evidence;
* percentile values are recomputed from retained raw samples using the
  documented nearest-rank method;
* every run of ``nw07_perf_ref_v1`` is evaluated independently with a strict
  ``< 16.67 ms`` operator;
* mismatched boards, hosts, environments, protocols, artifacts, or incomplete
  provenance remain unverified/informational and cannot certify the reference;
* executed threshold violations remain failed; missing telemetry stays pending.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import nw07_contract as contract

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_CORRECTNESS_METADATA = (
    "task",
    "implementation_base",
    "capture_id",
    "coverage_target",
    "evidence_role",
    "artifact_key",
    "package_sha256",
    "manifest_sha256",
    "executable_sha256",
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
    "renderer",
    "execution_environment",
    "fixed_ticks",
    "capture_artifacts",
    "reference_execution",
    "comparison",
    "startup",
    "memory",
    "measurements",
    "unavailable_telemetry",
)

REQUIRED_PERF_METADATA = (
    "task",
    "implementation_base",
    "capture_id",
    "coverage_target",
    "evidence_role",
    "contract_id",
    "reference",
    "execution_environment",
    "artifact_key",
    "package_sha256",
    "manifest_sha256",
    "executable_sha256",
    "profile_id",
    "requirements_digest",
    "protocol",
    "runs",
    "tools",
)

_UNAVAILABLE_MARKERS = {
    "",
    "unavailable",
    "unknown",
    "missing",
    "not_available",
    "not-applicable",
    "pending",
    "unverified",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _unavailable(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _UNAVAILABLE_MARKERS
    if isinstance(value, Mapping):
        if not value:
            return True
        for marker in ("status", "state", "availability"):
            current = value.get(marker)
            if isinstance(current, str) and current.strip().lower() in _UNAVAILABLE_MARKERS:
                return True
        if value.get("available") is False:
            return True
    return False


def _parse_utc_timestamp(value: Any) -> float | None:
    """Return a POSIX timestamp for an explicit timezone-aware ISO-8601 value."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _execution_environment_state(capture: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Evaluate the physical/passthrough execution contract used by both paths."""
    environment = capture.get("execution_environment")
    if not isinstance(environment, Mapping):
        return "pending", ["execution environment is not documented"]
    kind = environment.get("kind")
    if kind == "generic_vm":
        return "failed", ["a generic VM is insufficient for GPU acceptance"]
    if kind not in {"physical", "documented_gpu_passthrough"}:
        return "pending", ["execution environment kind is not acceptable"]
    if kind == "documented_gpu_passthrough" and _unavailable(
        environment.get("documentation")
    ):
        return "pending", ["GPU passthrough documentation is missing"]
    return "passed", []


def classify_hardware_tier(adapter_description: str) -> str:
    """Metadata-only Ada/Blackwell classification; never a target selector."""
    name = str(adapter_description or "").lower()
    if any(token in name for token in ("rtx 50", "blackwell", "b100", "b200", "gb200")):
        return "blackwell"
    if any(token in name for token in ("rtx 40", "l40", "a40", "a6000", "rtx a")) or re.search(
        r"(^|[^a-z])ada([^a-z]|$)", name
    ):
        return "ada"
    return "unknown"


def nearest_rank_percentile(samples: Sequence[float], percentile: float = contract.PERCENTILE) -> float:
    """Nearest-rank percentile over retained raw samples.

    rank = ceil(percentile / 100 * N), one-based.  The sample at that rank is
    returned.  This is the method frozen in ``nw07_perf_ref_v1``.
    """
    if percentile < 0.0 or percentile > 100.0:
        raise ValueError("percentile must be within [0, 100]")
    values: list[float] = []
    for item in samples:
        value = _number(item)
        if value is None:
            raise ValueError("samples must be finite numbers")
        values.append(value)
    if not values:
        raise ValueError("at least one raw sample is required")
    values.sort()
    rank = max(1, math.ceil((percentile / 100.0) * len(values)))
    return values[rank - 1]


def _read_numeric_samples(value: Any, artifact_root: Path | None) -> tuple[list[float], str]:
    """Read inline or file-backed samples and return ``(samples, source)``."""
    if isinstance(value, (list, tuple)):
        resolved = []
        for item in value:
            number = _number(item)
            if number is None:
                raise ValueError("raw sample contains a non-finite value")
            resolved.append(number)
        return resolved, "inline"
    if not isinstance(value, Mapping):
        raise ValueError("raw sample section is missing")
    samples_value = value.get("samples")
    if isinstance(samples_value, (list, tuple)):
        resolved = []
        for item in samples_value:
            number = _number(item)
            if number is None:
                raise ValueError("raw sample contains a non-finite value")
            resolved.append(number)
        return resolved, "inline"
    path_value = value.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("raw sample section has neither samples nor a path")
    if artifact_root is None:
        raise ValueError("raw sample file cannot be read without an evidence root")
    root = Path(artifact_root).resolve()
    path = (root / path_value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("raw sample path escapes the evidence root") from exc
    if not path.is_file():
        raise ValueError(f"raw sample file is unavailable: {path}")
    raw = path.read_bytes()
    declared_sha = value.get("sha256")
    if isinstance(declared_sha, str) and _SHA256_PATTERN.fullmatch(declared_sha):
        if sha256_bytes(raw) != declared_sha:
            raise ValueError("raw sample file digest does not match the record")
    elif "sha256" in value:
        raise ValueError("raw sample file digest is malformed")
    declared_bytes = value.get("bytes")
    if isinstance(declared_bytes, int) and not isinstance(declared_bytes, bool):
        if declared_bytes != len(raw):
            raise ValueError("raw sample file byte count does not match the record")
    text = raw.decode("utf-8")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        values = decoded
    elif isinstance(decoded, Mapping) and isinstance(decoded.get("samples"), list):
        values = decoded["samples"]
    else:
        values = [line for line in text.splitlines() if line.strip()]
    resolved = []
    for item in values:
        if isinstance(item, str):
            try:
                number = float(item.strip())
            except ValueError as exc:
                raise ValueError("raw sample file contains a non-numeric value") from exc
            if not math.isfinite(number):
                raise ValueError("raw sample file contains a non-finite value")
        else:
            number = _number(item)
        if number is None:
            raise ValueError("raw sample file contains a non-finite value")
        resolved.append(number)
    return resolved, str(path)


def _validate_sample_section(
    section: Any,
    *,
    artifact_root: Path | None,
    minimum_count: int,
    reported_percentile_key: str = "reported_p99_ms",
) -> dict[str, Any]:
    if not isinstance(section, Mapping):
        raise ValueError("sample section is missing")
    samples, source = _read_numeric_samples(section, artifact_root)
    if len(samples) < minimum_count:
        raise ValueError(
            f"raw sample count {len(samples)} is below the required {minimum_count}"
        )
    declared_count = section.get("sample_count")
    if declared_count is not None and declared_count != len(samples):
        raise ValueError("declared sample count does not match retained raw samples")
    computed = nearest_rank_percentile(samples, contract.PERCENTILE)
    reported = _number(section.get(reported_percentile_key))
    if reported is None:
        raise ValueError("reported p99 value is missing")
    if abs(reported - computed) > 1e-6:
        raise ValueError(
            f"reported p99 {reported} does not match the nearest-rank calculation {computed}"
        )
    return {
        "sample_count": len(samples),
        "source": source,
        "p99_ms": computed,
        "reported_p99_ms": reported,
        "percentile_method": contract.PERCENTILE_METHOD,
        "minimum_sample_count": minimum_count,
    }


def _strict_below(value: float, threshold: float) -> bool:
    return value < threshold


def _mismatch_paths(observed: Any, expected: Any, prefix: str = "") -> list[str]:
    """Return exact-value mismatches for non-null expected binding fields."""
    mismatches: list[str] = []
    if isinstance(expected, Mapping):
        if not isinstance(observed, Mapping):
            return [prefix or "<root>"]
        for key, expected_value in expected.items():
            if key == "binding_status":
                continue
            if expected_value is None:
                continue
            path = f"{prefix}.{key}" if prefix else key
            if key not in observed:
                mismatches.append(path)
            else:
                mismatches.extend(_mismatch_paths(observed[key], expected_value, path))
        return mismatches
    if isinstance(expected, list):
        if not isinstance(observed, list) or observed != expected:
            return [prefix or "<root>"]
        return []
    if isinstance(expected, float) or isinstance(observed, float):
        observed_number = _number(observed)
        expected_number = _number(expected)
        if observed_number is None or expected_number is None:
            return [prefix or "<root>"]
        if abs(observed_number - expected_number) > 1e-9:
            return [prefix or "<root>"]
        return []
    if observed != expected:
        return [prefix or "<root>"]
    return []


def validate_perf_contract(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that the frozen reference contract was not weakened."""
    reasons: list[str] = []
    if record.get("format") != "nw07-performance-reference":
        reasons.append("contract format changed")
    if record.get("contract_id") != contract.PERF_CONTRACT_ID:
        reasons.append("contract id changed")
    if record.get("contract_status") != "frozen":
        reasons.append("contract is not frozen")
    if record.get("task") != contract.TASK:
        reasons.append("contract task is not NW-07")
    if record.get("profile_id") != contract.PROFILE_ID:
        reasons.append("contract changed the common profile")
    if record.get("requirements_digest") != contract.REQUIREMENTS_DIGEST:
        reasons.append("contract changed the requirements digest")
    reference = record.get("reference") or {}
    board = reference.get("board") or {}
    if board.get("model") != contract.REFERENCE_BOARD_MODEL:
        reasons.append("reference board model is not the exact RTX 4060 Ti")
    if board.get("vendor_id") != contract.REFERENCE_BOARD_VENDOR_ID:
        reasons.append("reference vendor id changed")
    if board.get("device_id") != contract.REFERENCE_BOARD_DEVICE_ID:
        reasons.append("reference device id changed")
    if board.get("vram_mib") != contract.REFERENCE_BOARD_VRAM_MIB:
        reasons.append("reference VRAM changed")
    quality = reference.get("quality") or {}
    resolution = quality.get("resolution") or {}
    if (
        resolution.get("width") != contract.OUTPUT_WIDTH
        or resolution.get("height") != contract.OUTPUT_HEIGHT
    ):
        reasons.append("contract resolution is not 1920x1080")
    if quality.get("vsync") is not False:
        reasons.append("contract does not disable vsync")
    if quality.get("msaa_samples") != 1:
        reasons.append("contract changed MSAA")
    if quality.get("bloom_resolution_scale") != 0.5:
        reasons.append("contract changed bloom resolution scale")
    if quality.get("tonemap_operator") != "aces":
        reasons.append("contract changed tonemap operator")
    if float(quality.get("tone_exposure", 0.0)) != 1.1:
        reasons.append("contract changed exposure")
    if float(quality.get("tone_gamma", 0.0)) != 2.2:
        reasons.append("contract changed gamma")
    if quality.get("feature_level") != "12_0":
        reasons.append("contract changed feature level")
    if quality.get("shader_model") != "6_0":
        reasons.append("contract changed shader model")
    environment = reference.get("environment") or {}
    if environment.get("execution_kind") not in {
        "physical",
        "documented_gpu_passthrough",
    }:
        reasons.append("contract execution kind is not physical or documented passthrough")
    if environment.get("execution_kind") == "documented_gpu_passthrough" and not str(
        environment.get("virtualization_documentation", "")
    ).strip():
        reasons.append("passthrough reference lacks documentation")
    metrics = record.get("metric_definitions") or {}
    for name, expected in (
        ("gpu_frame_time_ms", "gpu"),
        ("cpu_submit_ms", "cpu"),
    ):
        metric = metrics.get(name) or {}
        if float(metric.get("percentile", -1.0)) != contract.PERCENTILE:
            reasons.append(f"{expected} percentile changed")
        if metric.get("percentile_method") != contract.PERCENTILE_METHOD:
            reasons.append(f"{expected} percentile method changed")
        if metric.get("operator") != contract.FRAME_TIME_OPERATOR:
            reasons.append(f"{expected} operator is not strict '<'")
        if float(metric.get("threshold", 0.0)) != contract.FRAME_TIME_THRESHOLD_MS:
            reasons.append(f"{expected} threshold changed")
        if metric.get("per_run_independent") is not True:
            reasons.append(f"{expected} is not independent per run")
    protocol = record.get("protocol") or {}
    if protocol.get("protocol_id") != contract.PERF_RUN_PROTOCOL:
        reasons.append("performance protocol id changed")
    if protocol.get("warmup_seconds") != contract.WARMUP_SECONDS:
        reasons.append("warm-up duration changed")
    if protocol.get("runs") != contract.PERF_RUN_COUNT:
        reasons.append("run count changed")
    if protocol.get("run_seconds") != contract.PERF_RUN_SECONDS:
        reasons.append("run duration changed")
    if protocol.get("uncapped") is not True:
        reasons.append("runs are not uncapped")
    if protocol.get("fresh_process_per_run") is not True:
        reasons.append("runs do not use a fresh process")
    if protocol.get("application_pipeline_cache") is not False:
        reasons.append("application pipeline cache is enabled")
    if protocol.get("aggregation_cannot_conceal_failed_run") is not True:
        reasons.append("aggregation is allowed to conceal a failed run")
    rules = record.get("matching_rules") or {}
    for key in (
        "exact_board_model",
        "exact_vendor_device_id",
        "exact_host_identity",
        "exact_os_build",
        "exact_driver_version",
        "exact_toolchain_identity",
        "exact_artifact_hashes",
        "exact_resolution_and_quality",
        "exact_cache_conditions",
        "exact_protocol",
        "reject_other_ada_devices",
        "reject_blackwell_devices",
        "reject_faster_or_higher_tier_skus",
        "reject_legacy_tier_labels",
        "reject_stale_artifacts",
        "reject_incomplete_provenance",
        "reject_unverified_captures",
    ):
        if rules.get(key) is not True:
            reasons.append(f"matching rule '{key}' is not enforced")
    change_control = record.get("change_control") or {}
    if change_control.get("performance_reference_change_requires_new_contract_version") is not True:
        reasons.append("reference change control is missing")
    if change_control.get("budget_or_threshold_change_requires_architecture_revision") is not True:
        reasons.append("budget/threshold change control is missing")
    if change_control.get("retroactive_pass_forbidden") is not True:
        reasons.append("retroactive pass is not forbidden")
    if change_control.get("previous_results_and_unresolved_statuses_are_preserved") is not True:
        reasons.append("previous results are not preserved across contract changes")
    if change_control.get("unvalidated_normalization_forbidden") is not True:
        reasons.append("unvalidated normalization is not forbidden")
    replacement = change_control.get("replacement_reference_requires") or {}
    for key in (
        "new_contract_version",
        "independently_justified_thresholds",
        "documented_validation_uncertainty",
        "bridge_measurements_where_feasible",
        "cannot_retroactively_pass_v1",
    ):
        if replacement.get(key) is not True:
            reasons.append(f"replacement-reference requirement '{key}' is missing")

    missing_bindings: list[str] = []
    for section_name, required in (
        ("board", ("subsystem_vendor_id", "subsystem_device_id")),
        (
            "host",
            (
                "cpu_model",
                "physical_cores",
                "logical_processors",
                "motherboard",
                "memory",
            ),
        ),
        (
            "environment",
            (
                "os_build",
                "driver_version",
                "wddm_driver_version",
                "windows_power_plan",
                "nvidia_power_management_mode",
                "pcie_power_management",
                "gpu_clocks",
                "temperature_c",
            ),
        ),
        (
            "toolchain",
            ("toolchain_identity", "toolchain_manifest_sha256", "msvc_version", "windows_sdk"),
        ),
        (
            "artifact",
            (
                "artifact_key",
                "package_sha256",
                "manifest_sha256",
                "executable_sha256",
                "complete_distribution_bytes",
            ),
        ),
    ):
        section = reference.get(section_name) or {}
        for field in required:
            if section.get(field) in (None, ""):
                missing_bindings.append(f"{section_name}.{field}")
    if reasons:
        return {
            "status": "invalid",
            "reasons": reasons,
            "missing_bindings": missing_bindings,
        }
    if missing_bindings:
        return {
            "status": "pending_binding",
            "reasons": [],
            "missing_bindings": missing_bindings,
        }
    return {"status": "frozen", "reasons": [], "missing_bindings": []}


def bind_reference(
    record: Mapping[str, Any],
    *,
    board: Mapping[str, Any] | None = None,
    host: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    toolchain: Mapping[str, Any] | None = None,
    artifact: Mapping[str, Any] | None = None,
    cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bound copy of the frozen contract.

    Binding never changes the frozen methodology or thresholds.  The helper is
    used by the reference-capture workflow and tests; callers cannot use it to
    relax a gate.
    """
    bound = copy.deepcopy(dict(record))
    reference = bound.setdefault("reference", {})
    for name, update in (
        ("board", board),
        ("host", host),
        ("environment", environment),
        ("toolchain", toolchain),
        ("artifact", artifact),
        ("cache", cache),
    ):
        if update:
            section = reference.setdefault(name, {})
            section.update(dict(update))
            section["binding_status"] = "bound"
    bound["binding_status"] = "bound"
    return bound


def fully_bound_reference(
    record: Mapping[str, Any],
    *,
    artifact_identity: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convenience binding used by tests and the reference-capture workflow.

    The concrete host values are the NW-07 reference-host designation.  They
    are evidence metadata, not compatibility predicates.
    """
    overrides = dict(overrides or {})
    base = {
        "board": {
            "subsystem_vendor_id": 0x1462,
            "subsystem_device_id": 0xCA00,
        },
        "host": {
            "cpu_model": "AMD Ryzen 7 7800X3D 8-Core Processor",
            "physical_cores": 8,
            "logical_processors": 16,
            "motherboard": "MSI MAG B650 TOMAHAWK WIFI",
            "memory": "32 GiB DDR5-6000 CL30",
        },
        "environment": {
            "execution_kind": "physical",
            "virtualization_documentation": "",
            "os_build": "10.0.26100.2314",
            "driver_version": "566.36",
            "wddm_driver_version": "31.0.15.6636",
            "windows_power_plan": "High performance",
            "nvidia_power_management_mode": "Prefer maximum performance",
            "pcie_power_management": "off",
            "gpu_clocks": "2310 MHz core / 9001 MHz memory",
            "temperature_c": 62.0,
        },
        "toolchain": {
            "toolchain_identity": "msvc-ltcg",
            "toolchain_manifest_sha256": "f" * 64,
            "msvc_version": "19.40.33821",
            "windows_sdk": "10.0.26100.0",
        },
        "artifact": {
            "artifact_key": artifact_identity.get("artifact_key"),
            "package_sha256": artifact_identity.get("package_sha256"),
            "manifest_sha256": artifact_identity.get("manifest_sha256"),
            "executable_sha256": artifact_identity.get("executable_sha256"),
            "complete_distribution_bytes": artifact_identity.get("executable_bytes"),
        },
        "cache": {
            "clean_start_application_pipeline_cache": False,
            "cold_preparation": "Clear the driver shader cache or reboot the host.",
            "warm_preparation": "Pre-run the identical artifact once to completion.",
            "accepted_states": ["cold_driver_cache", "warm"],
        },
    }
    for name, values in overrides.items():
        base.setdefault(name, {}).update(values)
    return bind_reference(record, **base)


def _artifact_mismatches(
    capture: Mapping[str, Any], artifact_identity: Mapping[str, Any]
) -> dict[str, Any]:
    expected = {
        "artifact_key": artifact_identity.get("artifact_key"),
        "package_sha256": artifact_identity.get("package_sha256"),
        "manifest_sha256": artifact_identity.get("manifest_sha256"),
        "executable_sha256": artifact_identity.get("executable_sha256"),
    }
    if artifact_identity.get("executable_bytes") is not None:
        expected["executable_bytes"] = artifact_identity.get("executable_bytes")
    differences: dict[str, Any] = {}
    for field, value in expected.items():
        if value and capture.get(field) != value:
            differences[field] = {"expected": value, "observed": capture.get(field)}
    return differences


def _protocol_mismatches(capture: Mapping[str, Any]) -> list[str]:
    protocol = capture.get("protocol") or {}
    reasons: list[str] = []
    if protocol.get("protocol_id") != contract.PERF_RUN_PROTOCOL:
        reasons.append("protocol id is not nw07-perf-run.v1")
    if protocol.get("warmup_seconds") != contract.WARMUP_SECONDS:
        reasons.append("warm-up is not five seconds")
    if protocol.get("runs") != contract.PERF_RUN_COUNT:
        reasons.append("protocol does not declare three runs")
    if protocol.get("run_seconds") != contract.PERF_RUN_SECONDS:
        reasons.append("run duration is not sixty seconds")
    if protocol.get("uncapped") is not True:
        reasons.append("runs are not uncapped")
    if protocol.get("fresh_process_per_run") is not True:
        reasons.append("runs do not use a fresh process")
    if protocol.get("application_pipeline_cache") is not False:
        reasons.append("application pipeline cache is enabled")
    return reasons


def validate_perf_capture(
    capture: Mapping[str, Any],
    perf_contract: Mapping[str, Any],
    *,
    artifact_identity: Mapping[str, Any],
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate one ``nw07_perf_ref_v1`` performance certification record."""
    result: dict[str, Any] = {
        "status": "pending_unverified",
        "reasons": [],
        "runs": [],
        "contract_validation": validate_perf_contract(perf_contract),
    }
    reasons: list[str] = result["reasons"]
    contract_validation = result["contract_validation"]
    if contract_validation["status"] == "invalid":
        result["status"] = "failed"
        reasons.extend(f"contract: {reason}" for reason in contract_validation["reasons"])
        return result
    missing = [
        name
        for name in REQUIRED_PERF_METADATA
        if name not in capture or _unavailable(capture.get(name))
    ]
    if missing:
        result["missing_metadata"] = missing
        return result
    if capture.get("evidence_role") != contract.PERFORMANCE_CERTIFICATION_ROLE:
        result["status"] = "unverified"
        reasons.append("capture is not performance-certification evidence")
        return result
    if capture.get("task") != contract.TASK:
        result["status"] = "unverified"
        reasons.append("capture task is not NW-07")
    if capture.get("implementation_base") != contract.IMPLEMENTATION_BASE:
        result["status"] = "unverified"
        reasons.append("capture is bound to a different implementation base")
    if (
        capture.get("coverage_target") != contract.PERF_CONTRACT_ID
        or capture.get("contract_id") != contract.PERF_CONTRACT_ID
    ):
        result["status"] = "unverified"
        reasons.append("capture does not target nw07_perf_ref_v1")
        return result
    if capture.get("profile_id") != contract.PROFILE_ID:
        result["status"] = "unverified"
        reasons.append("capture changed the common capability profile")
    if capture.get("requirements_digest") != contract.REQUIREMENTS_DIGEST:
        result["status"] = "unverified"
        reasons.append("capture changed the requirements digest")
    if contract_validation["status"] == "pending_binding":
        result["status"] = "pending_unverified"
        result["missing_contract_bindings"] = contract_validation["missing_bindings"]
        return result
    environment_state, environment_reasons = _execution_environment_state(capture)
    if environment_reasons:
        result["status"] = (
            "failed" if environment_state == "failed" else "unverified"
        )
        result["execution_environment"] = dict(
            capture.get("execution_environment") or {}
        )
        reasons.extend(environment_reasons)
    differences = _artifact_mismatches(capture, artifact_identity)
    contract_artifact = dict(
        ((perf_contract.get("reference") or {}).get("artifact") or {})
    )
    for field, value in contract_artifact.items():
        if field == "binding_status" or value in (None, ""):
            continue
        if field == "complete_distribution_bytes":
            if capture.get("executable_bytes") != value:
                differences.setdefault(
                    field, {"expected": value, "observed": capture.get("executable_bytes")}
                )
        elif capture.get(field) != value:
            differences.setdefault(field, {"expected": value, "observed": capture.get(field)})
    if differences:
        result["status"] = "unverified"
        result["artifact_mismatches"] = differences
        reasons.append("capture does not reference the exact final artifact")
    reference = capture.get("reference") or {}
    contract_reference = perf_contract.get("reference") or {}
    for marker in ("tier_label", "legacy_tier_label", "evidence_tier"):
        if capture.get(marker) not in (None, "") or (
            isinstance(reference.get("board"), Mapping)
            and reference["board"].get(marker) not in (None, "")
        ):
            result["status"] = "unverified"
            reasons.append(f"legacy/informal tier label '{marker}' cannot certify the reference")
    reference_mismatches: dict[str, list[str]] = {}
    for section in (
        "board",
        "host",
        "environment",
        "toolchain",
        "artifact",
        "quality",
        "cache",
    ):
        mismatches = _mismatch_paths(
            reference.get(section), contract_reference.get(section), section
        )
        if mismatches:
            reference_mismatches[section] = mismatches
    if reference_mismatches:
        result["status"] = "unverified"
        result["reference_mismatches"] = reference_mismatches
        reasons.append(
            "observed board/host/environment/toolchain/quality/cache does not "
            "match the frozen reference"
        )
    protocol_reasons = _protocol_mismatches(capture)
    if protocol_reasons:
        result["status"] = "unverified"
        result["protocol_mismatches"] = protocol_reasons
        reasons.extend(protocol_reasons)
    tools = capture.get("tools") or {}
    contract_tools = perf_contract.get("tools") or {}
    for tool_field in (
        "percentile_tool",
        "performance_measurement_tools",
        "sample_source",
        "displayed_frame_interval_source",
    ):
        if tools.get(tool_field) != contract_tools.get(tool_field):
            result["status"] = "unverified"
            reasons.append(f"measurement tool '{tool_field}' does not match the frozen contract")
    runs = capture.get("runs")
    if not isinstance(runs, list) or len(runs) != contract.PERF_RUN_COUNT:
        result["status"] = "unverified"
        reasons.append("exactly three independent runs are required")
        return result
    indices = sorted(
        int(run.get("run_index", 0))
        for run in runs
        if isinstance(run, Mapping)
    )
    if indices != [1, 2, 3]:
        result["status"] = "unverified"
        reasons.append("run indices must be exactly 1, 2, 3")
    executed_failure = False
    for run in runs:
        if not isinstance(run, Mapping):
            executed_failure = True
            result["runs"].append({"status": "failed", "reason": "run is malformed"})
            continue
        run_result: dict[str, Any] = {
            "run_index": run.get("run_index"),
            "status": "passed",
            "reasons": [],
        }
        if run.get("status") == "failed":
            run_result["status"] = "failed"
            run_result["reasons"].append("run records an executed failure")
        if run.get("uncapped") is not True:
            run_result["status"] = "failed"
            run_result["reasons"].append("run is not uncapped")
        if run.get("fresh_process") is not True:
            run_result["status"] = "failed"
            run_result["reasons"].append("run did not use a fresh process")
        timing = run.get("timing")
        if not isinstance(timing, Mapping):
            timing = {}
        run_started = _parse_utc_timestamp(
            run.get("run_started_utc")
            or timing.get("run_started_utc")
            or timing.get("warmup_ended_utc")
        )
        run_ended = _parse_utc_timestamp(
            run.get("run_ended_utc") or timing.get("run_ended_utc")
        )
        warmup_started = _parse_utc_timestamp(
            run.get("warmup_started_utc")
            or timing.get("warmup_started_utc")
        )
        if run_started is None or run_ended is None or warmup_started is None:
            run_result["status"] = "failed"
            run_result["reasons"].append(
                "run start/end/warm-up timing evidence is missing or malformed"
            )
            derived_duration = None
            derived_warmup = None
        else:
            derived_duration = run_ended - run_started
            derived_warmup = run_started - warmup_started
            if derived_duration < contract.PERF_RUN_SECONDS or derived_duration > (
                contract.PERF_RUN_SECONDS + 1.0
            ):
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    f"retained run timing covers {derived_duration:.3f} s, not a "
                    f"{contract.PERF_RUN_SECONDS}-second run"
                )
            if derived_warmup < contract.WARMUP_SECONDS or derived_warmup > (
                contract.WARMUP_SECONDS + 1.0
            ):
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    f"retained warm-up timing covers {derived_warmup:.3f} s, not "
                    f"{contract.WARMUP_SECONDS} seconds"
                )
            declared_duration = _number(run.get("duration_seconds"))
            if (
                declared_duration is None
                or abs(declared_duration - derived_duration) > 0.05
            ):
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    "declared run duration does not match retained timing evidence"
                )
            declared_warmup = _number(run.get("warmup_seconds"))
            if (
                declared_warmup is None
                or abs(declared_warmup - derived_warmup) > 0.05
            ):
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    "declared warm-up does not match retained timing evidence"
                )
        for field, expected in (
            ("uncapped", True),
            ("fresh_process", True),
            ("application_pipeline_cache", False),
        ):
            if run.get(field) is not timing.get(field):
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    f"retained run metadata disagrees on '{field}'"
                )
            if timing.get(field) is not expected:
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    f"retained run metadata '{field}' is not {expected}"
                )
        run_result["timing"] = {
            "warmup_seconds": derived_warmup,
            "duration_seconds": derived_duration,
            "uncapped": timing.get("uncapped"),
            "fresh_process": timing.get("fresh_process"),
            "application_pipeline_cache": timing.get("application_pipeline_cache"),
        }
        for metric_name in ("gpu_frame_time_ms", "cpu_submit_ms"):
            try:
                validated = _validate_sample_section(
                    run.get(metric_name),
                    artifact_root=artifact_root,
                    minimum_count=(
                        max(
                            contract.MIN_RAW_SAMPLES_PER_RUN,
                            math.ceil(derived_duration * 10.0),
                        )
                        if derived_duration and derived_duration > 0
                        else contract.MIN_RAW_SAMPLES_PER_RUN
                    ),
                )
            except ValueError as exc:
                run_result["status"] = "failed"
                run_result["reasons"].append(f"{metric_name}: {exc}")
                continue
            run_result[metric_name] = validated
            if not _strict_below(validated["p99_ms"], contract.FRAME_TIME_THRESHOLD_MS):
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    f"{metric_name} p99 {validated['p99_ms']} is not strictly below "
                    f"{contract.FRAME_TIME_THRESHOLD_MS}"
                )
        displayed = run.get("displayed_frame_intervals_ms")
        if not isinstance(displayed, Mapping) or not isinstance(
            displayed.get("samples"), (list, tuple)
        ):
            run_result["status"] = "failed"
            run_result["reasons"].append(
                "displayed frame intervals were not reported separately"
            )
        else:
            if displayed.get("gate") is not False:
                run_result["status"] = "failed"
                run_result["reasons"].append(
                    "displayed frame intervals must remain a separate non-gating report"
                )
            try:
                displayed_values = [
                    float(_number(item))
                    for item in displayed["samples"]
                    if _number(item) is not None
                ]
                if not displayed_values:
                    raise ValueError("displayed interval samples are empty")
                run_result["displayed_frame_interval_ms"] = {
                    "sample_count": len(displayed_values),
                    "p99_ms": nearest_rank_percentile(displayed_values),
                    "gate": False,
                }
            except ValueError as exc:
                run_result["status"] = "failed"
                run_result["reasons"].append(f"displayed frame intervals: {exc}")
        sample_counts = [
            run_result[name]["sample_count"]
            for name in ("gpu_frame_time_ms", "cpu_submit_ms")
            if isinstance(run_result.get(name), Mapping)
            and isinstance(run_result[name].get("sample_count"), int)
        ]
        if sample_counts and derived_duration and derived_duration > 0:
            run_result["sample_density_hz"] = min(sample_counts) / derived_duration
        if run_result["status"] == "failed":
            executed_failure = True
        result["runs"].append(run_result)
    if executed_failure:
        result["status"] = "failed"
    elif result["status"] == "pending_unverified" and not reasons:
        result["status"] = "verified"
    result["aggregate_percentiles_are_informational"] = True
    return result


def _validate_tolerance_section(
    section: Any,
    *,
    kind: str,
    absolute_tolerance: float,
    relative_tolerance: float,
    relative_can_satisfy: bool,
) -> tuple[str, str]:
    if not isinstance(section, Mapping):
        return "missing", f"{kind} comparison is absent"
    if str(section.get("status", "")).strip().lower() == "failed":
        return "failed", f"{kind} comparison is marked failed"
    maximum = _number(section.get("maximum_error"))
    absolute = _number(section.get("tolerance"))
    relative = _number(section.get("maximum_relative_error"))
    relative_declared = _number(section.get("relative_tolerance"))
    if maximum is None or absolute is None:
        return "missing", f"{kind} comparison result is incomplete"
    if maximum < 0.0 or absolute < 0.0:
        return "failed", f"{kind} comparison reported a negative error/tolerance"
    if absolute > absolute_tolerance:
        return "failed", f"{kind} comparison relaxed the authoritative absolute tolerance"
    if relative_declared is not None and relative_declared > relative_tolerance:
        return "failed", f"{kind} comparison relaxed the authoritative relative tolerance"
    if kind == "image":
        if maximum > absolute or maximum > absolute_tolerance:
            return "failed", "image comparison exceeded the absolute channel tolerance"
    else:
        absolute_ok = maximum <= absolute
        relative_ok = (
            relative_can_satisfy
            and relative is not None
            and relative_declared is not None
            and relative <= relative_declared
        )
        if not absolute_ok and not relative_ok:
            return "failed", "numeric comparison exceeded absolute and relative tolerances"
    return "passed", ""


def _artifact_file_state(
    capture: Mapping[str, Any],
    artifact_root: Path | None,
    *,
    expected_ticks: Iterable[int],
) -> tuple[str, str]:
    artifacts = capture.get("capture_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return "missing", "capture artifacts are absent"
    ticks: list[int] = []
    for item in artifacts:
        if not isinstance(item, Mapping):
            return "failed", "capture artifact descriptor is malformed"
        if item.get("kind") != contract.CAPTURE_KIND:
            return "failed", "capture artifact kind violates the proof contract"
        if item.get("format") != contract.CAPTURE_FORMAT:
            return "failed", "capture artifact format violates the proof contract"
        if item.get("width") != contract.OUTPUT_WIDTH or item.get("height") != contract.OUTPUT_HEIGHT:
            return "failed", "capture artifact resolution is not 1920x1080"
        expected_bytes = (
            contract.OUTPUT_WIDTH
            * contract.OUTPUT_HEIGHT
            * contract.CAPTURE_BYTES_PER_PIXEL
        )
        if item.get("bytes") != expected_bytes:
            return "failed", "capture artifact byte size does not match RGBA32F 1920x1080"
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            return "failed", "capture artifact digest is malformed"
        tick = item.get("tick")
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            return "failed", "capture artifact tick is invalid"
        ticks.append(tick)
    required = set(int(tick) for tick in expected_ticks)
    if not required <= set(ticks) or len(ticks) != len(set(ticks)):
        return "failed", "capture artifacts do not cover every fixed tick exactly once"
    if artifact_root is None:
        return "missing", "capture artifact bytes were not supplied for verification"
    root = Path(artifact_root).resolve()
    for item in artifacts:
        path = (root / str(item.get("path", ""))).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return "failed", "capture artifact path escapes the evidence root"
        if not path.is_file():
            return "missing", f"capture artifact is unavailable: {path}"
        if path.stat().st_size != item.get("bytes"):
            return "failed", "capture artifact byte size does not match the file"
        if sha256_file(path) != item.get("sha256"):
            return "failed", "capture artifact digest does not match the file"
    return "passed", ""


def _validate_measurements(capture: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    measurements = capture.get("measurements")
    if not isinstance(measurements, Mapping):
        return "missing", {}
    required = (
        "process_ram_bytes",
        "driver_memory_bytes",
        "startup_pipeline_ms",
        "generation_ms",
        "image_quality_metrics",
        "scaling_1440p",
    )
    missing = [name for name in required if name not in measurements]
    if missing:
        return "missing", {"missing": missing}
    numeric_fields = (
        "process_ram_bytes",
        "driver_memory_bytes",
        "startup_pipeline_ms",
        "generation_ms",
    )
    unavailable = [
        name
        for name in numeric_fields
        if _unavailable(measurements.get(name))
    ]
    invalid = [
        name
        for name in numeric_fields
        if name not in unavailable and (_number(measurements.get(name)) is None or _number(measurements.get(name)) <= 0.0)
    ]
    if invalid:
        return "failed", {
            "invalid": invalid,
            "recorded": {name: measurements.get(name) for name in required},
        }
    return ("pending" if unavailable else "passed"), {
        "unavailable": unavailable,
        "recorded": {name: measurements.get(name) for name in required},
    }


def validate_startup_memory(capture: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "pending_unverified", "reasons": []}
    startup = capture.get("startup")
    memory = capture.get("memory")
    if not isinstance(startup, Mapping) or not isinstance(memory, Mapping):
        result["reasons"].append("startup or memory measurement is absent")
        return result
    if startup.get("protocol") != contract.STARTUP_PROTOCOL:
        result["status"] = "failed"
        result["reasons"].append("startup protocol does not match")
        return result
    elapsed = _number(startup.get("process_start_to_first_present_ms"))
    if elapsed is None:
        result["reasons"].append("process-start-to-first-present is not measured")
    elif elapsed > contract.PROCESS_START_TO_FIRST_PRESENT_MS:
        result["status"] = "failed"
        result["reasons"].append(
            f"process-start-to-first-present {elapsed} ms exceeds 2000 ms"
        )
    else:
        result["process_start_to_first_present_ms"] = elapsed
    peak = _number(memory.get("peak_engine_committed_gpu_bytes"))
    if peak is None:
        result["reasons"].append("peak engine-committed GPU memory is not measured")
    elif peak > contract.PEAK_ENGINE_COMMITTED_GPU_BYTES:
        result["status"] = "failed"
        result["reasons"].append(
            f"peak engine-committed GPU bytes {peak} exceed 512 MiB"
        )
    else:
        result["peak_engine_committed_gpu_bytes"] = peak
    cache = startup.get("cache_state")
    if not isinstance(cache, Mapping):
        result["reasons"].append("startup cache state is missing")
    else:
        kind = cache.get("kind")
        if kind not in {"cold_driver_cache", "warm"}:
            result["status"] = "failed"
            result["reasons"].append("cache state must be cold_driver_cache or warm")
        if cache.get("application_pipeline_cache_used") is not False:
            result["status"] = "failed"
            result["reasons"].append("clean start used an application pipeline cache")
        result["cache_state"] = dict(cache)
    for field in ("cold_driver_cache_preparation", "warm_preparation"):
        if not isinstance(startup.get(field), str) or not startup.get(field, "").strip():
            result["reasons"].append(f"{field} is not documented")
    if result["status"] != "failed" and not result["reasons"]:
        result["status"] = "passed"
    return result


def validate_correctness_capture(
    capture: Mapping[str, Any],
    *,
    coverage_target: str,
    artifact_identity: Mapping[str, Any],
    artifact_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = dict(config or {})
    tolerances = record.get("image_tolerances", {})
    numeric_absolute = float(
        tolerances.get("numeric", {}).get("absolute", contract.NUMERIC_ABSOLUTE_TOLERANCE)
    )
    numeric_relative = float(
        tolerances.get("numeric", {}).get("relative", contract.NUMERIC_RELATIVE_TOLERANCE)
    )
    image_absolute = float(
        tolerances.get("image", {}).get("absolute", contract.IMAGE_ABSOLUTE_TOLERANCE)
    )
    image_relative = float(
        tolerances.get("image", {}).get("relative", contract.IMAGE_RELATIVE_TOLERANCE)
    )
    result: dict[str, Any] = {
        "status": "pending_unverified",
        "reasons": [],
        "coverage_target": coverage_target,
    }
    if numeric_absolute > contract.NUMERIC_ABSOLUTE_TOLERANCE:
        result["status"] = "failed"
        result["reasons"].append("numeric absolute tolerance was weakened")
    if numeric_relative > contract.NUMERIC_RELATIVE_TOLERANCE:
        result["status"] = "failed"
        result["reasons"].append("numeric relative tolerance was weakened")
    if image_absolute > contract.IMAGE_ABSOLUTE_TOLERANCE:
        result["status"] = "failed"
        result["reasons"].append("image absolute tolerance was weakened")
    if image_relative > contract.IMAGE_RELATIVE_TOLERANCE:
        result["status"] = "failed"
        result["reasons"].append("image relative tolerance was weakened")
    missing = [
        name
        for name in REQUIRED_CORRECTNESS_METADATA
        if name not in capture or _unavailable(capture.get(name))
    ]
    if missing:
        result["missing_metadata"] = missing
        return result
    if capture.get("evidence_role") != contract.FIXED_TICK_CORRECTNESS_ROLE:
        result["status"] = "failed"
        result["reasons"].append("evidence role is not fixed_tick_correctness")
        return result
    if capture.get("coverage_target") != coverage_target:
        result["status"] = "failed"
        result["reasons"].append("coverage target does not match the qualification")
        return result
    if capture.get("capture_protocol") != contract.CORRECTNESS_CAPTURE_PROTOCOL:
        result["status"] = "failed"
        result["reasons"].append("correctness capture protocol does not match")
    if capture.get("task") != contract.TASK:
        result["status"] = "failed"
        result["reasons"].append("capture task is not NW-07")
    if capture.get("implementation_base") != contract.IMPLEMENTATION_BASE:
        result["status"] = "failed"
        result["reasons"].append("capture is bound to a different implementation base")
    if capture.get("profile_id") != contract.PROFILE_ID:
        result["status"] = "failed"
        result["reasons"].append("capture changed the common profile")
    if capture.get("requirements_digest") != contract.REQUIREMENTS_DIGEST:
        result["status"] = "failed"
        result["reasons"].append("capture changed the requirements digest")
    environment_state, environment_reasons = _execution_environment_state(capture)
    if environment_state == "failed":
        result["status"] = "failed"
        result["reasons"].extend(environment_reasons)
    elif environment_state == "pending":
        if result["status"] != "failed":
            result["status"] = "pending_unverified"
        result["reasons"].extend(environment_reasons)
    tier = classify_hardware_tier(str(capture.get("adapter_description", "")))
    expected_tier = "ada" if "ada" in coverage_target else "blackwell" if "blackwell" in coverage_target else "unknown"
    if tier != expected_tier:
        if result["status"] != "failed":
            result["status"] = "unverified"
        result["reasons"].append(
            f"observed hardware tier '{tier}' does not match coverage target '{coverage_target}'"
        )
    differences = _artifact_mismatches(capture, artifact_identity)
    if differences:
        if result["status"] != "failed":
            result["status"] = "unverified"
        result["artifact_mismatches"] = differences
        result["reasons"].append("capture does not reference the exact final artifact")
    fixed_ticks = capture.get("fixed_ticks")
    required_ticks = set(contract.REQUIRED_FIXED_TICKS)
    if not isinstance(fixed_ticks, list) or set(fixed_ticks) != required_ticks:
        result["status"] = "failed"
        result["reasons"].append("fixed ticks do not match the frozen coverage set")
    artifact_state, artifact_detail = _artifact_file_state(
        capture, artifact_root, expected_ticks=required_ticks
    )
    result["artifact_state"] = artifact_state
    result["artifact_detail"] = artifact_detail
    if artifact_state == "failed":
        result["status"] = "failed"
        result["reasons"].append(artifact_detail)
    elif artifact_state == "missing":
        if result["status"] != "failed":
            result["status"] = "pending_unverified"
        result["reasons"].append(artifact_detail)
    reference = capture.get("reference_execution")
    if not isinstance(reference, Mapping):
        result["status"] = "failed"
        result["reasons"].append("reference execution provenance is absent")
    else:
        if reference.get("execution_id") == capture.get("capture_id"):
            result["status"] = "failed"
            result["reasons"].append("reference execution identifies the capture under test")
        if reference.get("renderer") == capture.get("renderer"):
            result["status"] = "failed"
            result["reasons"].append(
                "comparison does not cross diagnostic-preview and exported-player rendering"
            )
        for field in ("execution_id", "package_sha256", "executable_sha256", "manifest_sha256", "capture_protocol"):
            if _unavailable(reference.get(field)):
                if result["status"] != "failed":
                    result["status"] = "pending_unverified"
                result["reasons"].append(f"reference execution {field} is incomplete")
    comparison = capture.get("comparison")
    if not isinstance(comparison, Mapping):
        result["status"] = "failed"
        result["reasons"].append("comparison evidence is absent")
    else:
        if str(comparison.get("status", "")).strip().lower() == "failed":
            result["status"] = "failed"
            result["reasons"].append("comparison records an executed failure")
        summary = comparison.get("summary")
        if isinstance(summary, Mapping):
            if str(summary.get("status", "")).strip().lower() == "failed":
                result["status"] = "failed"
                result["reasons"].append("comparison summary records an executed failure")
            numeric_state, numeric_detail = _validate_tolerance_section(
                summary.get("numeric"),
                kind="numeric",
                absolute_tolerance=numeric_absolute,
                relative_tolerance=numeric_relative,
                relative_can_satisfy=True,
            )
            image_state, image_detail = _validate_tolerance_section(
                summary.get("image"),
                kind="image",
                absolute_tolerance=image_absolute,
                relative_tolerance=image_relative,
                relative_can_satisfy=False,
            )
            if "failed" in (numeric_state, image_state):
                result["status"] = "failed"
                result["reasons"].append(
                    "comparison summary: " + (numeric_detail or image_detail)
                )
            elif "missing" in (numeric_state, image_state) and result["status"] != "failed":
                result["status"] = "pending_unverified"
                result["reasons"].append("comparison summary is incomplete")
        per_tick = comparison.get("per_tick")
        if not isinstance(per_tick, list) or not per_tick:
            result["status"] = "failed"
            result["reasons"].append("per-tick comparisons are absent")
        else:
            rows_by_tick: dict[int, Mapping[str, Any]] = {}
            for row in per_tick:
                if not isinstance(row, Mapping):
                    result["status"] = "failed"
                    result["reasons"].append("per-tick comparison row is malformed")
                    continue
                tick = row.get("tick")
                if not isinstance(tick, int) or isinstance(tick, bool):
                    result["status"] = "failed"
                    result["reasons"].append("per-tick comparison tick is invalid")
                    continue
                if tick in rows_by_tick:
                    result["status"] = "failed"
                    result["reasons"].append("per-tick comparison repeats a tick")
                rows_by_tick[tick] = row
                if str(row.get("status", "")).strip().lower() == "failed":
                    result["status"] = "failed"
                    result["reasons"].append(
                        f"tick {tick}: comparison row records an executed failure"
                    )
                numeric_state, numeric_detail = _validate_tolerance_section(
                    row.get("numeric"),
                    kind="numeric",
                    absolute_tolerance=numeric_absolute,
                    relative_tolerance=numeric_relative,
                    relative_can_satisfy=True,
                )
                image_state, image_detail = _validate_tolerance_section(
                    row.get("image"),
                    kind="image",
                    absolute_tolerance=image_absolute,
                    relative_tolerance=image_relative,
                    relative_can_satisfy=False,
                )
                if "failed" in (numeric_state, image_state):
                    result["status"] = "failed"
                    result["reasons"].append(
                        f"tick {tick}: {numeric_detail or image_detail}"
                    )
                elif "missing" in (numeric_state, image_state) and result["status"] != "failed":
                    result["status"] = "pending_unverified"
                    result["reasons"].append(
                        f"tick {tick}: comparison evidence is incomplete"
                    )
                row_artifact = row.get("artifact_sha256")
                expected_artifact = next(
                    (
                        item.get("sha256")
                        for item in capture.get("capture_artifacts", [])
                        if item.get("tick") == tick
                    ),
                    None,
                )
                if expected_artifact is not None and row_artifact != expected_artifact:
                    result["status"] = "failed"
                    result["reasons"].append(
                        f"tick {tick}: comparison does not name the captured artifact"
                    )
                if _unavailable(row.get("reference_artifact_sha256")):
                    if result["status"] != "failed":
                        result["status"] = "pending_unverified"
                    result["reasons"].append(
                        f"tick {tick}: reference artifact is not identified"
                    )
                reference_relative = row.get("reference_artifact_path")
                if isinstance(reference_relative, str) and reference_relative.strip():
                    if artifact_root is None:
                        if result["status"] != "failed":
                            result["status"] = "pending_unverified"
                        result["reasons"].append(
                            f"tick {tick}: reference artifact bytes were not supplied"
                        )
                    else:
                        root = Path(artifact_root).resolve()
                        reference_path = (root / reference_relative).resolve()
                        try:
                            reference_path.relative_to(root)
                        except ValueError:
                            result["status"] = "failed"
                            result["reasons"].append(
                                f"tick {tick}: reference artifact path escapes the evidence root"
                            )
                        else:
                            if not reference_path.is_file():
                                if result["status"] != "failed":
                                    result["status"] = "pending_unverified"
                                result["reasons"].append(
                                    f"tick {tick}: reference artifact is unavailable"
                                )
                            elif (
                                sha256_file(reference_path)
                                != row.get("reference_artifact_sha256")
                            ):
                                result["status"] = "failed"
                                result["reasons"].append(
                                    f"tick {tick}: reference artifact digest mismatch"
                                )
                row_renderer = row.get("renderer")
                row_reference_renderer = row.get("reference_renderer")
                if row_renderer is None or row_reference_renderer is None:
                    if result["status"] != "failed":
                        result["status"] = "pending_unverified"
                    result["reasons"].append(
                        f"tick {tick}: comparison does not identify both renderers"
                    )
                elif row_renderer == row_reference_renderer:
                    result["status"] = "failed"
                    result["reasons"].append(
                        f"tick {tick}: comparison does not cross renderer modes"
                    )
                elif row_renderer != capture.get("renderer"):
                    result["status"] = "failed"
                    result["reasons"].append(
                        f"tick {tick}: comparison renderer does not match the capture"
                    )
            if set(rows_by_tick) != required_ticks:
                result["status"] = "failed"
                result["reasons"].append("per-tick comparisons do not cover the fixed ticks")
        loop_boundary = comparison.get("loop_boundary")
        if not isinstance(loop_boundary, Mapping):
            result["status"] = "failed"
            result["reasons"].append("loop-boundary comparison is absent")
        elif loop_boundary.get("verified") is not True:
            if (
                loop_boundary.get("source") == "graph-derived-provenance"
                and result["status"] != "failed"
            ):
                result["status"] = "pending_unverified"
                result["reasons"].append(
                    "loop-boundary comparison is not measured; only canonical "
                    "graph provenance is available"
                )
            else:
                result["status"] = "failed"
                result["reasons"].append("loop-boundary comparison is not verified")
        else:
            if loop_boundary.get("source") != "captured_boundary_comparison":
                result["status"] = "failed"
                result["reasons"].append(
                    "loop-boundary evidence is not an executed tick-0/tick-960000 comparison"
                )
            if loop_boundary.get("start_tick") != contract.LOOP_BOUNDARY_TICKS[0] or loop_boundary.get(
                "boundary_tick"
            ) != contract.LOOP_BOUNDARY_TICKS[1]:
                result["status"] = "failed"
                result["reasons"].append("loop-boundary ticks are not 0 and 960,000")
            numeric_state, numeric_detail = _validate_tolerance_section(
                loop_boundary.get("numeric"),
                kind="numeric",
                absolute_tolerance=numeric_absolute,
                relative_tolerance=numeric_relative,
                relative_can_satisfy=True,
            )
            if numeric_state == "failed":
                result["status"] = "failed"
                result["reasons"].append(f"loop boundary: {numeric_detail}")
            elif numeric_state == "missing" and result["status"] != "failed":
                result["status"] = "pending_unverified"
                result["reasons"].append("loop-boundary comparison is incomplete")
            if loop_boundary.get("state_digest_equal_modulo_turn") is not True:
                result["status"] = "failed"
                result["reasons"].append(
                    "loop-boundary deterministic state is not equal modulo one turn"
                )
            graph_loop = loop_boundary.get("graph_loop_boundary")
            if not isinstance(graph_loop, Mapping) or graph_loop.get(
                "seamless_modulo_turn"
            ) is not True:
                result["status"] = "failed"
                result["reasons"].append(
                    "loop-boundary deterministic-state claim lacks canonical graph provenance"
                )
            boundary_sha = loop_boundary.get("boundary_artifact_sha256")
            retained = {
                item.get("sha256")
                for item in capture.get("capture_artifacts", [])
                if isinstance(item, Mapping)
                and item.get("tick") == contract.LOOP_BOUNDARY_TICKS[1]
            }
            if not isinstance(boundary_sha, str) or boundary_sha not in retained:
                result["status"] = "failed"
                result["reasons"].append(
                    "loop-boundary comparison does not retain the tick-960,000 capture"
                )
    startup_memory = validate_startup_memory(capture)
    result["startup_memory"] = startup_memory
    if startup_memory["status"] == "failed":
        result["status"] = "failed"
        result["reasons"].extend(startup_memory["reasons"])
    elif startup_memory["status"] != "passed" and result["status"] != "failed":
        result["status"] = "pending_unverified"
        result["reasons"].extend(startup_memory["reasons"])
    measurement_state, measurement_detail = _validate_measurements(capture)
    result["measurements"] = measurement_detail
    if measurement_state == "missing":
        if result["status"] != "failed":
            result["status"] = "pending_unverified"
        result["reasons"].append("required informational measurements are incomplete")
    elif measurement_state == "pending" and result["status"] != "failed":
        result["status"] = "pending_unverified"
        result["reasons"].append("some informational measurements are unavailable")
    elif measurement_state == "failed":
        result["status"] = "failed"
        result["reasons"].append("informational measurements are malformed")
    if result["status"] == "pending_unverified" and not result["reasons"]:
        result["status"] = "verified"
    return result


def validate_diagnostic_overhead(
    record: Mapping[str, Any],
    *,
    artifact_identity: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "pending_unverified", "reasons": []}
    if record.get("evidence_role") != contract.DIAGNOSTIC_OVERHEAD_ROLE:
        result["status"] = "failed"
        result["reasons"].append("evidence role is not diagnostic_overhead")
        return result
    if record.get("protocol") != contract.DIAGNOSTIC_OVERHEAD_PROTOCOL:
        result["status"] = "failed"
        result["reasons"].append("diagnostic overhead protocol does not match")
    release = record.get("release") or {}
    diagnostic = record.get("diagnostic") or {}
    expected_executable = artifact_identity.get("executable_sha256")
    binding_matched = (
        isinstance(expected_executable, str)
        and bool(expected_executable)
        and record.get("executable_sha256") == expected_executable
        and release.get("executable_sha256") == expected_executable
    )
    if record.get("executable_sha256") != expected_executable:
        result["status"] = "unverified"
        result["reasons"].append("diagnostic record does not bind the final release artifact")
    if release.get("executable_sha256") != expected_executable:
        result["status"] = "unverified"
        result["reasons"].append("release artifact identity does not match the final artifact")
    if release.get("contains_capture_controls") is not False:
        result["status"] = "failed"
        result["reasons"].append("release build contains capture controls")
    if diagnostic.get("build_profile") != contract.DIAGNOSTIC_BUILD_PROFILE:
        result["status"] = "failed"
        result["reasons"].append("diagnostic build is not the dev sibling profile")
    if diagnostic.get("executable_sha256") in (None, artifact_identity.get("executable_sha256")):
        result["status"] = "failed"
        result["reasons"].append("diagnostic build is not a separate artifact")
    measurements = record.get("measurements")
    if not isinstance(measurements, Mapping):
        if result["status"] != "failed":
            result["status"] = "pending_unverified"
        result["reasons"].append("diagnostic overhead measurements are absent")
        return result
    required = (
        "release_size_bytes",
        "diagnostic_size_bytes",
        "size_overhead_bytes",
        "release_p99_gpu_frame_time_ms",
        "diagnostic_p99_gpu_frame_time_ms",
        "gpu_frame_time_overhead_ms",
        "release_p99_cpu_submit_ms",
        "diagnostic_p99_cpu_submit_ms",
        "cpu_submit_overhead_ms",
    )
    missing = [name for name in required if _number(measurements.get(name)) is None]
    if missing:
        if result["status"] != "failed" and binding_matched:
            result["status"] = "pending_unverified"
        result["reasons"].append("diagnostic overhead measurements are incomplete")
    if result["status"] != "failed" and binding_matched and not missing:
        result["status"] = "verified"
        result["overhead"] = {name: measurements[name] for name in required}
    elif result["status"] != "failed" and not binding_matched:
        result["status"] = "unverified"
    return result


def validate_development_series_capture(
    capture: Mapping[str, Any],
    *,
    artifact_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Record a matched-environment Blackwell development-series measurement.

    The series deliberately has no absolute gate.  Its measurements remain
    informational and can never certify Ada correctness or ``nw07_perf_ref_v1``.
    """
    result: dict[str, Any] = {
        "status": "unverified",
        "absolute_gate": False,
        "reasons": [],
    }
    if capture.get("evidence_role") != contract.DEVELOPMENT_SERIES_ROLE:
        result["status"] = "failed"
        result["reasons"].append("evidence role is not the Blackwell development series")
        return result
    if capture.get("coverage_target") != contract.BLACKWELL_DEVELOPMENT_TARGET:
        result["status"] = "failed"
        result["reasons"].append("coverage target is not the Blackwell development series")
        return result
    if capture.get("task") != contract.TASK:
        result["status"] = "failed"
        result["reasons"].append("capture task is not NW-07")
    if capture.get("implementation_base") != contract.IMPLEMENTATION_BASE:
        result["status"] = "unverified"
        result["reasons"].append("capture is bound to a different implementation base")
    differences = _artifact_mismatches(capture, artifact_identity)
    if differences:
        result["status"] = "unverified"
        result["artifact_mismatches"] = differences
        result["reasons"].append("capture does not reference the exact final artifact")
    if classify_hardware_tier(str(capture.get("adapter_description", ""))) != "blackwell":
        result["status"] = "unverified"
        result["reasons"].append("development series is not on Blackwell hardware")
    if _unavailable(capture.get("matched_environment_id")):
        result["status"] = "unverified"
        result["reasons"].append("matched environment identity is missing")
    metrics = capture.get("metrics") or capture.get("measurements")
    if not isinstance(metrics, Mapping) or not metrics:
        result["status"] = "unverified"
        result["reasons"].append("development-series measurements are missing")
    if result["status"] != "failed" and not result["reasons"]:
        result["status"] = "informational"
        result["metrics"] = dict(metrics)
    return result


def _qualification(
    status: str,
    *,
    coverage_target: str,
    evidence_role: str,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "coverage_target": coverage_target,
        "evidence_role": evidence_role,
        "status": status,
        "reason": reason,
    }


def pending_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "verification_status": "pending_unverified",
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "perf_contract_id": contract.PERF_CONTRACT_ID,
        "production_config": "benchmarks/nw07/canonical-production.v1.json",
        "perf_contract": "benchmarks/nw07/perf-ref-v1.json",
        "metric_baselines": "benchmarks/nw07/metric-baselines.v1.json",
        "expected_artifact_identity": "pending_release_export",
        "qualifications": {
            "ada_correctness": _qualification(
                "pending_unverified",
                coverage_target=contract.CORRECTNESS_COVERAGE_TARGETS["ada"],
                evidence_role=contract.FIXED_TICK_CORRECTNESS_ROLE,
                reason="No representative desktop Ada fixed-tick capture of the identical NW-07 artifact is recorded.",
            ),
            "blackwell_correctness": _qualification(
                "pending_unverified",
                coverage_target=contract.CORRECTNESS_COVERAGE_TARGETS["blackwell"],
                evidence_role=contract.FIXED_TICK_CORRECTNESS_ROLE,
                reason="No representative desktop Blackwell fixed-tick capture of the identical NW-07 artifact is recorded.",
            ),
            "startup_memory": _qualification(
                "pending_unverified",
                coverage_target="final_nw07_artifact",
                evidence_role="startup_memory",
                reason="Startup and engine-committed memory have not been measured on qualifying hardware.",
            ),
            "perf_ref_certification": _qualification(
                "pending_unverified",
                coverage_target=contract.PERF_CONTRACT_ID,
                evidence_role=contract.PERFORMANCE_CERTIFICATION_ROLE,
                reason="No nw07_perf_ref_v1 runs on the matching reference host are recorded.",
            ),
            "diagnostic_overhead": _qualification(
                "pending_unverified",
                coverage_target="diagnostic_sibling_build",
                evidence_role=contract.DIAGNOSTIC_OVERHEAD_ROLE,
                reason="Diagnostic sibling-build overhead has not been measured.",
            ),
            "blackwell_development_series": _qualification(
                "informational",
                coverage_target=contract.BLACKWELL_DEVELOPMENT_TARGET,
                evidence_role=contract.DEVELOPMENT_SERIES_ROLE,
                reason="Matched-environment Blackwell development series; no absolute gate.",
            ),
        },
        "captures": [],
        "executions": [],
        "unavailable_telemetry": list(REQUIRED_CORRECTNESS_METADATA),
        "policy": (
            "NW-07 evidence binds only to the final 1920x1080 common artifact produced "
            "from the frozen graph and pinned build inputs. Missing Ada, Blackwell, "
            "startup/memory, reference-performance, or diagnostic-overhead evidence "
            "remains pending; executed failures remain failed. Observed GPU names are "
            "metadata only except for the explicitly justified nw07_perf_ref_v1 "
            "reference board."
        ),
    }


def validate_evidence_record(
    record: Mapping[str, Any],
    *,
    artifact_identity: Mapping[str, Any],
    artifact_root: Path | None = None,
    perf_contract: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    captures = record.get("captures") or record.get("executions") or []
    if not isinstance(captures, list):
        captures = []
    validated: list[dict[str, Any]] = []
    for capture in captures:
        if not isinstance(capture, Mapping):
            validated.append({"status": "failed", "reasons": ["capture is malformed"]})
            continue
        declared_status = capture.get("status")
        role = capture.get("evidence_role")
        if declared_status == "failed":
            validated.append(
                {
                    "status": "failed",
                    "reasons": ["capture records an executed failure"],
                    "capture_id": capture.get("capture_id"),
                    "evidence_role": role,
                }
            )
            continue
        if role == contract.FIXED_TICK_CORRECTNESS_ROLE:
            target = capture.get("coverage_target")
            if target not in contract.CORRECTNESS_COVERAGE_TARGETS.values():
                validated.append(
                    {
                        "status": "unverified",
                        "reasons": ["correctness capture has an unsupported coverage target"],
                        "capture_id": capture.get("capture_id"),
                    }
                )
                continue
            validated_capture = validate_correctness_capture(
                capture,
                coverage_target=str(target),
                artifact_identity=artifact_identity,
                artifact_root=artifact_root,
                config=config,
            )
        elif role == contract.PERFORMANCE_CERTIFICATION_ROLE:
            validated_capture = validate_perf_capture(
                capture,
                perf_contract or {},
                artifact_identity=artifact_identity,
                artifact_root=artifact_root,
            )
        elif role == contract.DIAGNOSTIC_OVERHEAD_ROLE:
            validated_capture = validate_diagnostic_overhead(
                capture, artifact_identity=artifact_identity
            )
        elif role == contract.DEVELOPMENT_SERIES_ROLE:
            validated_capture = validate_development_series_capture(
                capture, artifact_identity=artifact_identity
            )
            validated_capture["evidence_role"] = contract.DEVELOPMENT_SERIES_ROLE
        else:
            validated_capture = {
                "status": "unverified",
                "reasons": [f"capture role '{role}' is not certifying evidence"],
            }
        if (
            declared_status == "pending_unverified"
            and validated_capture.get("status") == "verified"
        ):
            validated_capture["status"] = "pending_unverified"
            validated_capture.setdefault("reasons", []).append(
                "capture explicitly retains a pending/unverified status"
            )
        validated_capture["capture_id"] = capture.get("capture_id")
        validated.append(validated_capture)

    # Map validated captures back to their raw records by capture id so a
    # qualification cannot be established by a bare label.
    raw_by_id = {
        capture.get("capture_id"): capture
        for capture in captures
        if isinstance(capture, Mapping)
    }
    validated_by_id = {
        item.get("capture_id"): item
        for item in validated
        if item.get("capture_id") is not None
    }

    def best_status(predicate) -> str:
        matching = [item for item in validated if predicate(item)]
        if any(item["status"] == "failed" for item in matching):
            return "failed"
        if any(item["status"] == "verified" for item in matching):
            return "verified"
        if any(item["status"] in {"pending_unverified", "unverified"} for item in matching):
            return "pending_unverified"
        return "pending_unverified"

    def capture_matches(item: Mapping[str, Any], target: str, role: str) -> bool:
        raw = raw_by_id.get(item.get("capture_id"))
        return bool(
            raw
            and raw.get("coverage_target") == target
            and raw.get("evidence_role") == role
        )

    qualifications: dict[str, dict[str, Any]] = {}
    for tier, target in contract.CORRECTNESS_COVERAGE_TARGETS.items():
        status = best_status(
            lambda item, target=target: capture_matches(
                item, target, contract.FIXED_TICK_CORRECTNESS_ROLE
            )
        )
        qualifications[f"{tier}_correctness"] = _qualification(
            status,
            coverage_target=target,
            evidence_role=contract.FIXED_TICK_CORRECTNESS_ROLE,
        )
    startup_statuses = [
        item.get("startup_memory", {}).get("status", "pending_unverified")
        for item in validated
        if item.get("capture_id") is not None
        and raw_by_id.get(item.get("capture_id"), {}).get("evidence_role")
        == contract.FIXED_TICK_CORRECTNESS_ROLE
    ]
    if any(status == "failed" for status in startup_statuses):
        startup_status = "failed"
    elif startup_statuses and all(status == "passed" for status in startup_statuses):
        startup_status = "verified"
    else:
        startup_status = "pending_unverified"
    qualifications["startup_memory"] = _qualification(
        startup_status,
        coverage_target="final_nw07_artifact",
        evidence_role="startup_memory",
    )
    qualifications["perf_ref_certification"] = _qualification(
        best_status(
            lambda item: capture_matches(
                item, contract.PERF_CONTRACT_ID, contract.PERFORMANCE_CERTIFICATION_ROLE
            )
        ),
        coverage_target=contract.PERF_CONTRACT_ID,
        evidence_role=contract.PERFORMANCE_CERTIFICATION_ROLE,
    )
    qualifications["diagnostic_overhead"] = _qualification(
        best_status(
            lambda item: capture_matches(
                item,
                "diagnostic_sibling_build",
                contract.DIAGNOSTIC_OVERHEAD_ROLE,
            )
        ),
        coverage_target="diagnostic_sibling_build",
        evidence_role=contract.DIAGNOSTIC_OVERHEAD_ROLE,
    )
    # Development-series evidence is recorded but never gates the milestone.
    dev_series = [
        raw
        for raw in captures
        if isinstance(raw, Mapping)
        and raw.get("evidence_role") == contract.DEVELOPMENT_SERIES_ROLE
    ]
    qualifications["blackwell_development_series"] = _qualification(
        (
            "failed"
            if any(
                item.get("evidence_role") == contract.DEVELOPMENT_SERIES_ROLE
                and item.get("status") == "failed"
                for item in validated
            )
            else "informational"
            if any(
                item.get("evidence_role") == contract.DEVELOPMENT_SERIES_ROLE
                and item.get("status") == "informational"
                for item in validated
            )
            else "pending_unverified"
        ),
        coverage_target=contract.BLACKWELL_DEVELOPMENT_TARGET,
        evidence_role=contract.DEVELOPMENT_SERIES_ROLE,
        reason="No absolute gate; matched-environment development series only.",
    )

    required_names = (
        "ada_correctness",
        "blackwell_correctness",
        "startup_memory",
        "perf_ref_certification",
        "diagnostic_overhead",
    )
    statuses = {name: qualifications[name]["status"] for name in required_names}
    required_capture_failures = [
        item
        for item in validated
        if item["status"] == "failed"
        and item.get("evidence_role") != contract.DEVELOPMENT_SERIES_ROLE
    ]
    development_series_failures = [
        item
        for item in validated
        if item["status"] == "failed"
        and item.get("evidence_role") == contract.DEVELOPMENT_SERIES_ROLE
    ]
    if required_capture_failures or "failed" in statuses.values():
        overall = "failed"
    elif all(status == "verified" for status in statuses.values()):
        overall = "verified"
    else:
        overall = "pending_unverified"

    # Every certifying capture must bind the identical final artifact and
    # implement the NW-07 base.
    identity_sets: set[tuple[Any, Any, Any, Any]] = set()
    base_mismatch = False
    for raw in captures:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("evidence_role") not in {
            contract.FIXED_TICK_CORRECTNESS_ROLE,
            contract.PERFORMANCE_CERTIFICATION_ROLE,
            contract.DIAGNOSTIC_OVERHEAD_ROLE,
        }:
            continue
        if raw.get("implementation_base") != contract.IMPLEMENTATION_BASE:
            base_mismatch = True
        identity_sets.add(
            (
                raw.get("artifact_key"),
                raw.get("package_sha256"),
                raw.get("manifest_sha256"),
                raw.get("executable_sha256"),
            )
        )
    if base_mismatch:
        overall = "failed"
    if len(identity_sets) > 1:
        overall = "failed"
        identity_reason = "captures do not reference the identical final NW-07 artifact"
    else:
        identity_reason = ""
    if record.get("task") != contract.TASK:
        overall = "failed"
        identity_reason = "record task is not NW-07"
    return {
        "status": overall,
        "milestone_complete": overall == "verified",
        "qualifications": qualifications,
        "required_qualifications": list(required_names),
        "captures": validated,
        "identity_reason": identity_reason,
        "blackwell_development_series": {
            "status": "informational",
            "absolute_gate": False,
            "records": len(dev_series),
            "failures": len(development_series_failures),
        },
    }


def validate_evidence_file(
    path: Path,
    *,
    artifact_identity: Mapping[str, Any],
    artifact_root: Path | None = None,
    perf_contract_path: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    perf_contract = (
        json.loads(Path(perf_contract_path).read_text(encoding="utf-8"))
        if perf_contract_path is not None
        else None
    )
    return validate_evidence_record(
        record,
        artifact_identity=artifact_identity,
        artifact_root=artifact_root,
        perf_contract=perf_contract,
        config=config,
    )


def record_execution(
    record: Mapping[str, Any],
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    """Append an execution record without synthesizing reference provenance."""
    updated = copy.deepcopy(dict(record))
    updated.setdefault("captures", [])
    updated.setdefault("executions", [])
    entry = copy.deepcopy(dict(capture))
    updated["captures"].append(entry)
    updated["executions"].append(entry)
    return updated


__all__ = [
    "REQUIRED_CORRECTNESS_METADATA",
    "REQUIRED_PERF_METADATA",
    "sha256_bytes",
    "sha256_file",
    "classify_hardware_tier",
    "nearest_rank_percentile",
    "validate_perf_contract",
    "bind_reference",
    "fully_bound_reference",
    "validate_perf_capture",
    "validate_startup_memory",
    "validate_correctness_capture",
    "validate_diagnostic_overhead",
    "validate_development_series_capture",
    "pending_record",
    "validate_evidence_record",
    "validate_evidence_file",
    "record_execution",
]
