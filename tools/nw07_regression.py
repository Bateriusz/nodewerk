#!/usr/bin/env python3
"""NW-07 continuous size/visual/startup/memory/frame-time regression policy.

Absolute gate violations always fail.  Relative performance or memory
regressions above ten percent are flagged only when baseline and current
measurement identify the same matched environment.  Size changes of any
magnitude are tracked, but there is no percentage-only size failure rule.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
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


def evidence_timestamp(value: Any) -> float | None:
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


def _perf_p99_from_evidence(
    evidence_record: Mapping[str, Any],
    *,
    artifact_sha256: str | None = None,
    environment: Mapping[str, Any] | None = None,
    performance_capture_id: str | None = None,
    max_evidence_age_seconds: float | None = None,
) -> dict[str, float]:
    captures = evidence_record.get("captures")
    if not isinstance(captures, list):
        return {}
    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for capture in captures:
        if not isinstance(capture, Mapping):
            continue
        if capture.get("evidence_role") != contract.PERFORMANCE_CERTIFICATION_ROLE:
            continue
        if (
            performance_capture_id is not None
            and capture.get("capture_id") != performance_capture_id
        ):
            continue
        if artifact_sha256 is not None and capture.get(
            "executable_sha256"
        ) != artifact_sha256:
            continue
        if isinstance(environment, Mapping):
            reference = capture.get("reference")
            reference = reference if isinstance(reference, Mapping) else {}
            board = reference.get("board")
            board = board if isinstance(board, Mapping) else {}
            capture_environment = reference.get("environment")
            capture_environment = (
                capture_environment
                if isinstance(capture_environment, Mapping)
                else {}
            )
            toolchain = reference.get("toolchain")
            toolchain = toolchain if isinstance(toolchain, Mapping) else {}
            host = reference.get("host")
            host = host if isinstance(host, Mapping) else {}
            comparisons = (
                ("adapter_description", board.get("model")),
                ("driver_version", capture_environment.get("driver_version")),
                ("os_version", capture_environment.get("os_build")),
                (
                    "toolchain_identity",
                    toolchain.get("toolchain_identity"),
                ),
                ("host", host.get("cpu_model")),
                ("cpu_model", host.get("cpu_model")),
            )
            mismatched = False
            for key, observed in comparisons:
                expected = environment.get(key)
                if expected not in (None, "") and observed != expected:
                    mismatched = True
                    break
            if mismatched:
                continue
        captured_at = evidence_timestamp(capture.get("captured_at_utc"))
        if captured_at is None:
            continue
        if max_evidence_age_seconds is not None:
            age_seconds = datetime.now(timezone.utc).timestamp() - captured_at
            if (
                age_seconds < 0
                or age_seconds > float(max_evidence_age_seconds)
            ):
                continue
        validation = capture.get("validation")
        if not isinstance(validation, Mapping) or validation.get("status") != "verified":
            continue
        runs = validation.get("runs") if isinstance(validation, Mapping) else None
        if not isinstance(runs, list) or not runs:
            continue
        candidates.append((captured_at, capture))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, capture in candidates:
        validation = capture.get("validation")
        runs = validation.get("runs") if isinstance(validation, Mapping) else None
        gpu_values: list[float] = []
        cpu_values: list[float] = []
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            gpu = run.get("gpu_frame_time_ms")
            cpu = run.get("cpu_submit_ms")
            gpu_p99 = _number(gpu.get("p99_ms")) if isinstance(gpu, Mapping) else None
            cpu_p99 = _number(cpu.get("p99_ms")) if isinstance(cpu, Mapping) else None
            if gpu_p99 is not None:
                gpu_values.append(gpu_p99)
            if cpu_p99 is not None:
                cpu_values.append(cpu_p99)
        if gpu_values and cpu_values:
            # The strict gate is per-run; the regression series uses the worst
            # observed p99 so one failing run cannot be hidden by averaging.
            return {
                "p99_gpu_frame_time_ms": max(gpu_values),
                "p99_cpu_submit_ms": max(cpu_values),
            }
    return {}


def build_current_metrics(
    metrics: Mapping[str, Any],
    *,
    tier: str,
    artifact_bytes: int,
    evidence_record: Mapping[str, Any] | None = None,
    artifact_sha256: str | None = None,
    performance_capture_id: str | None = None,
    max_evidence_age_seconds: float | None = None,
) -> dict[str, Any]:
    metadata = metrics.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    measurements = metrics.get("measurements")
    measurements = measurements if isinstance(measurements, Mapping) else {}
    cache_state = metadata.get("cache_state")
    cache_kind = (
        cache_state.get("kind")
        if isinstance(cache_state, Mapping)
        else cache_state
    )
    current_metrics: dict[str, Any] = {
        "complete_distribution_bytes": artifact_bytes,
    }
    for name in (
        "process_start_to_first_present_ms",
        "peak_engine_committed_gpu_bytes",
        "process_ram_bytes",
        "driver_memory_bytes",
        "startup_pipeline_ms",
        "generation_ms",
        "image_quality_metrics",
        "scaling_1440p",
    ):
        if name in measurements:
            current_metrics[name] = measurements[name]
    if tier == "ada" and isinstance(evidence_record, Mapping):
        current_metrics.update(
            _perf_p99_from_evidence(
                evidence_record,
                artifact_sha256=artifact_sha256,
                environment=metadata,
                performance_capture_id=performance_capture_id,
                max_evidence_age_seconds=max_evidence_age_seconds,
            )
        )
    return {
        "format": "nw07-current-metrics",
        "version": 1,
        "task": contract.TASK,
        "tier": tier,
        "environment": {
            "board": metadata.get("adapter_description", ""),
            "host": metadata.get("host", metadata.get("cpu_model", "")),
            "driver_version": metadata.get("driver_version", ""),
            "os_build": metadata.get("os_version", ""),
            "toolchain_identity": metadata.get("toolchain_identity", ""),
            "resolution": "1920x1080",
            "quality_profile": "nw07.quality.v1",
            "cache_state": cache_kind or "",
        },
        "metrics": current_metrics,
    }


def merge_nightly_metrics(
    metrics_paths: Sequence[Path],
    *,
    evidence_path: Path,
    artifact_path: Path,
    output_dir: Path,
    performance_capture_id: str,
    max_evidence_age_seconds: float,
) -> list[Path]:
    evidence_record = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence_record, Mapping):
        raise ValueError("merged evidence is not a JSON object")
    if not str(performance_capture_id).strip():
        raise ValueError("current nightly performance capture id is required")
    if max_evidence_age_seconds <= 0:
        raise ValueError("nightly evidence age window must be positive")
    artifact_bytes = artifact_path.stat().st_size
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for path in metrics_paths:
        metrics = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(metrics, Mapping):
            raise ValueError(f"metrics file is not an object: {path}")
        stem = path.stem
        tier = stem.removeprefix("metrics-")
        if tier not in {"ada", "blackwell"}:
            raise ValueError(f"metrics file name does not identify a tier: {path}")
        current = build_current_metrics(
            metrics,
            tier=tier,
            artifact_bytes=artifact_bytes,
            evidence_record=evidence_record,
            artifact_sha256=artifact_sha256,
            performance_capture_id=performance_capture_id,
            max_evidence_age_seconds=max_evidence_age_seconds,
        )
        destination = output_dir / f"metrics-current-{tier}.json"
        destination.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(destination)
    return written


def requires_nightly_gpu_check(config: Mapping[str, Any]) -> bool:
    regression = config.get("regression") or {}
    return "gpu_frame_time" in (regression.get("nightly_checks") or [])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    parser.add_argument("--current")
    parser.add_argument("--metrics", action="append", default=[])
    parser.add_argument("--evidence")
    parser.add_argument("--artifact")
    parser.add_argument("--output-dir")
    parser.add_argument("--performance-capture-id")
    parser.add_argument("--max-evidence-age-seconds", type=float)
    parser.add_argument(
        "--matched-environment",
        choices=("auto", "true", "false"),
        default="auto",
    )
    parser.add_argument("--summary-json")
    args = parser.parse_args(argv)
    if args.metrics:
        if (
            not args.evidence
            or not args.artifact
            or not args.output_dir
            or not args.performance_capture_id
            or args.max_evidence_age_seconds is None
        ):
            parser.error(
                "--metrics requires --evidence, --artifact, --output-dir, "
                "--performance-capture-id, and --max-evidence-age-seconds"
            )
        if args.max_evidence_age_seconds <= 0:
            parser.error("--max-evidence-age-seconds must be positive")
        written = merge_nightly_metrics(
            [Path(value) for value in args.metrics],
            evidence_path=Path(args.evidence),
            artifact_path=Path(args.artifact),
            output_dir=Path(args.output_dir),
            performance_capture_id=args.performance_capture_id,
            max_evidence_age_seconds=args.max_evidence_age_seconds,
        )
        print(
            json.dumps(
                {
                    "status": "merged",
                    "current_metrics": [str(path) for path in written],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.baseline or not args.current:
        parser.error("--baseline and --current are required unless --metrics is used")
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
