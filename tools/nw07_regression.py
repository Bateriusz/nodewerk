#!/usr/bin/env python3
"""NW-07 continuous size/visual/startup/memory/frame-time regression policy.

Absolute gate violations always fail.  Relative performance or memory
regressions above ten percent are flagged only when baseline and current
measurement identify the same matched environment.  Size changes of any
magnitude are tracked, but there is no percentage-only size failure rule.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw07_contract as contract

ABSOLUTE_GATE_METRICS = (
    "complete_distribution_bytes",
    "process_start_to_first_present_ms",
    "peak_engine_committed_gpu_bytes",
    "p99_gpu_frame_time_ms",
    "p99_cpu_submit_ms",
)

RELATIVE_ALERT_METRICS = (
    "process_start_to_first_present_ms",
    "peak_engine_committed_gpu_bytes",
    "p99_gpu_frame_time_ms",
    "p99_cpu_submit_ms",
)

MATCHED_ENVIRONMENT_FIELDS = (
    "board",
    "host",
    "driver_version",
    "os_build",
    "toolchain_identity",
    "resolution",
    "quality_profile",
    "cache_state",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _metric(record: Mapping[str, Any], name: str) -> float | None:
    for section_name in ("metrics", "measurements", "baseline", "current"):
        section = record.get(section_name)
        if isinstance(section, Mapping) and name in section:
            value = section[name]
            if isinstance(value, Mapping):
                for key in ("value", "current", "measured", "baseline"):
                    if key in value:
                        number = _number(value[key])
                        if number is not None:
                            return number
                return None
            number = _number(value)
            if number is not None:
                return number
    return None


def environments_match(
    baseline: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
        return False
    for field in MATCHED_ENVIRONMENT_FIELDS:
        if baseline.get(field) in (None, "") or current.get(field) in (None, ""):
            return False
        if baseline.get(field) != current.get(field):
            return False
    return True


def _authoritative_gate(name: str) -> tuple[str, float] | None:
    gates = contract.authoritative_hard_gates()
    entry = gates.get(name)
    if not isinstance(entry, Mapping):
        return None
    return str(entry.get("operator", ">=")), float(entry.get("threshold", 0.0))


def _violates(value: float, operator: str, threshold: float) -> bool:
    if operator == "<":
        return not value < threshold
    if operator == "<=":
        return not value <= threshold
    if operator == ">":
        return not value > threshold
    if operator == ">=":
        return not value >= threshold
    raise ValueError(f"unsupported comparison operator '{operator}'")


def evaluate_regression(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    matched_environment: bool | None = None,
    relative_alert_percent: float = contract.MATCHED_ENVIRONMENT_RELATIVE_ALERT_PERCENT,
) -> dict[str, Any]:
    if relative_alert_percent < 0.0:
        raise ValueError("relative alert percentage must be non-negative")
    baseline_environment = baseline.get("environment")
    current_environment = current.get("environment")
    if matched_environment is None:
        matched_environment = environments_match(baseline_environment, current_environment)
    absolute_failures: list[dict[str, Any]] = []
    pending: list[str] = []
    tracked: dict[str, Any] = {}
    for name in ABSOLUTE_GATE_METRICS:
        value = _metric(current, name)
        gate = _authoritative_gate(name)
        if gate is None:
            continue
        operator, threshold = gate
        if value is None:
            pending.append(name)
            continue
        tracked[name] = {"value": value, "operator": operator, "threshold": threshold}
        if _violates(value, operator, threshold):
            absolute_failures.append(
                {
                    "metric": name,
                    "value": value,
                    "operator": operator,
                    "threshold": threshold,
                }
            )
    size_current = _metric(current, "complete_distribution_bytes")
    size_baseline = _metric(baseline, "complete_distribution_bytes")
    size_record: dict[str, Any] = {
        "current": size_current,
        "baseline": size_baseline,
        "delta_bytes": (
            int(size_current - size_baseline)
            if size_current is not None and size_baseline is not None
            else None
        ),
        "delta_percent": (
            round(100.0 * (size_current - size_baseline) / size_baseline, 6)
            if size_current is not None and size_baseline not in (None, 0)
            else None
        ),
        "percentage_only_failure": False,
        "stretch_ceiling": contract.STRETCH_DISTRIBUTION_CEILING_BYTES,
        "within_stretch": (
            size_current is not None
            and size_current <= contract.STRETCH_DISTRIBUTION_CEILING_BYTES
        ),
        "tracked": True,
    }
    relative_alerts: list[dict[str, Any]] = []
    informational: list[dict[str, Any]] = []
    if matched_environment:
        for name in RELATIVE_ALERT_METRICS:
            current_value = _metric(current, name)
            baseline_value = _metric(baseline, name)
            if current_value is None or baseline_value in (None, 0):
                continue
            change = 100.0 * (current_value - baseline_value) / baseline_value
            entry = {
                "metric": name,
                "baseline": baseline_value,
                "current": current_value,
                "change_percent": round(change, 6),
                "threshold_percent": relative_alert_percent,
                "matched_environment": True,
            }
            if change > relative_alert_percent:
                entry["status"] = "flagged"
                relative_alerts.append(entry)
            else:
                entry["status"] = "within_threshold"
                informational.append(entry)
    else:
        for name in RELATIVE_ALERT_METRICS:
            current_value = _metric(current, name)
            baseline_value = _metric(baseline, name)
            if current_value is None or baseline_value in (None, 0):
                continue
            informational.append(
                {
                    "metric": name,
                    "baseline": baseline_value,
                    "current": current_value,
                    "change_percent": round(
                        100.0 * (current_value - baseline_value) / baseline_value, 6
                    ),
                    "status": "informational_unmatched_environment",
                    "matched_environment": False,
                }
            )
    if absolute_failures:
        status = "failed"
    elif pending:
        status = "pending_unverified"
    elif relative_alerts:
        status = "flagged"
    else:
        status = "passed"
    return {
        "status": status,
        "absolute_failures": absolute_failures,
        "pending_metrics": pending,
        "relative_alerts": relative_alerts,
        "informational": informational,
        "size": size_record,
        "tracked": tracked,
        "matched_environment": matched_environment,
        "matched_environment_fields": list(MATCHED_ENVIRONMENT_FIELDS),
        "policy": {
            "absolute_gate_violation": "fail",
            "matched_environment_relative_alert_percent": relative_alert_percent,
            "size_percentage_only_failure": False,
        },
    }


def evaluate_series(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    result = evaluate_regression(baseline, current)
    series = {
        "absolute": result["status"] == "failed",
        "relative_flags": len(result["relative_alerts"]),
        "pending": len(result["pending_metrics"]),
    }
    return {**result, "series_summary": series}


def requires_nightly_gpu_check(config: Mapping[str, Any]) -> bool:
    regression = config.get("regression") or {}
    return "gpu_frame_time" in (regression.get("nightly_checks") or [])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument(
        "--matched-environment",
        choices=("auto", "true", "false"),
        default="auto",
    )
    parser.add_argument("--summary-json")
    args = parser.parse_args(argv)
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    matched = (
        None
        if args.matched_environment == "auto"
        else args.matched_environment == "true"
    )
    result = evaluate_regression(baseline, current, matched_environment=matched)
    payload = {
        "format": "nw07-regression-result",
        "version": 1,
        "task": contract.TASK,
        "result": result,
    }
    if args.summary_json:
        path = Path(args.summary_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    if result["status"] == "failed":
        return 1
    if result["status"] == "pending_unverified":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ABSOLUTE_GATE_METRICS",
    "RELATIVE_ALERT_METRICS",
    "MATCHED_ENVIRONMENT_FIELDS",
    "environments_match",
    "evaluate_regression",
    "evaluate_series",
    "requires_nightly_gpu_check",
    "main",
]
