#!/usr/bin/env python3
"""NW-07 benchmark/capture harness.

Correctness mode records supplied fixed-tick images and provenance. Performance
collect mode actively launches the final player and an external measurement
collector for the frozen one-warm-up/three-run protocol. The harness never
synthesizes reference provenance, never upgrades a missing value to a pass, and
never writes a certifying status that the evidence validator would not derive
from retained raw data. Raw fixed-tick RGBA32F captures and raw per-run sample
files are retained and hashed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as contract  # noqa: E402
import nw07_evidence as evidence  # noqa: E402
import nw07_export as export  # noqa: E402

COLLECT_PROTOCOL = contract.PERF_COLLECTION_PROTOCOL


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _evidence_namespace(capture_id: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in capture_id.strip()
    )
    if not cleaned:
        raise ValueError("capture id must contain at least one safe character")
    suffix = _sha256_bytes(capture_id.strip().encode("utf-8"))[:12]
    return f"{cleaned}-{suffix}"


def _retain_capture(source: Path, destination_dir: Path) -> Path:
    """Copy a raw capture without overwriting different prior evidence."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    data = source.read_bytes()
    if destination.is_file():
        if destination.read_bytes() != data:
            raise ValueError(
                f"retained capture '{destination}' already exists with different bytes; "
                "capture ids must not overwrite prior evidence"
            )
        return destination
    destination.write_bytes(data)
    return destination


def _validation_status(validation: Mapping[str, Any]) -> str:
    status = str(validation.get("status", "pending_unverified"))
    if status == "verified":
        return "passed"
    if status == "failed":
        return "failed"
    if status == "informational":
        return "informational"
    if status == "unverified":
        return "unverified"
    return "pending_unverified"


def _utc_seconds(value: Any) -> float | None:
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


def _load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_value_or_path(value: Any) -> Any:
    if isinstance(value, str):
        return _load_json(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _read_rgba32f(path: Path) -> array:
    data = path.read_bytes()
    expected = (
        contract.OUTPUT_WIDTH
        * contract.OUTPUT_HEIGHT
        * contract.CAPTURE_BYTES_PER_PIXEL
    )
    if len(data) != expected:
        raise ValueError(
            f"fixed-tick capture '{path}' is {len(data)} bytes; expected {expected}"
        )
    values = array("f")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _compare_images(captured: Path, reference: Path) -> dict[str, float]:
    actual = _read_rgba32f(captured)
    expected = _read_rgba32f(reference)
    maximum_absolute = 0.0
    maximum_relative = 0.0
    for actual_value, expected_value in zip(actual, expected):
        if not math.isfinite(actual_value) or not math.isfinite(expected_value):
            raise ValueError("capture contains a non-finite channel value")
        difference = abs(actual_value - expected_value)
        relative = difference / max(1e-30, abs(expected_value))
        maximum_absolute = max(maximum_absolute, difference)
        maximum_relative = max(maximum_relative, relative)
    return {
        "maximum_error": maximum_absolute,
        "maximum_relative_error": maximum_relative,
    }


def _load_samples(path: Path) -> list[float]:
    text = path.read_text(encoding="utf-8")
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
                raise ValueError(
                    f"sample file '{path}' contains a non-numeric value"
                ) from exc
        elif isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"sample file '{path}' contains a non-numeric value")
        else:
            number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"sample file '{path}' contains a non-finite value")
        resolved.append(number)
    if not resolved:
        raise ValueError(f"sample file '{path}' is empty")
    return resolved


def _sample_section(path: Path) -> dict[str, Any]:
    values = _load_samples(path)
    return {
        "samples": values,
        "sample_count": len(values),
        "reported_p99_ms": evidence.nearest_rank_percentile(values),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
        "path": path.name,
    }


def _loop_boundary_evidence(
    captures_dir: Path,
    graph_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure tick 0 against tick 960,000 instead of asserting the boundary.

    When either boundary capture is unavailable, the record keeps the canonical
    graph's loop-boundary provenance but explicitly reports ``verified: false``.
    """
    start_tick, boundary_tick = contract.LOOP_BOUNDARY_TICKS
    graph_loop = graph_evidence.get("loop_boundary")
    graph_loop = dict(graph_loop) if isinstance(graph_loop, Mapping) else {}
    result: dict[str, Any] = {
        "verified": False,
        "source": "graph-derived-provenance",
        "start_tick": start_tick,
        "boundary_tick": boundary_tick,
        "state_digest_equal_modulo_turn": (
            graph_loop.get("seamless_modulo_turn") is True
        ),
        "state_digest_source": "verify_canonical_graph",
        "graph_loop_boundary": graph_loop,
        "numeric": {
            "maximum_error": None,
            "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
            "maximum_relative_error": None,
            "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
        },
    }
    start_path = captures_dir / f"tick_{start_tick}.rgba32f"
    boundary_path = captures_dir / f"tick_{boundary_tick}.rgba32f"
    if not start_path.is_file() or not boundary_path.is_file():
        result["unavailable_reason"] = (
            "tick 0 and tick 960,000 captures were not both supplied"
        )
        return result
    image = _compare_images(boundary_path, start_path)
    result.update(
        verified=True,
        source="captured_boundary_comparison",
        boundary_artifact_sha256=_sha256_file(boundary_path),
        start_artifact_sha256=_sha256_file(start_path),
    )
    result["numeric"].update(
        maximum_error=image["maximum_error"],
        maximum_relative_error=image["maximum_relative_error"],
    )
    return result


def record_correctness(args: argparse.Namespace) -> dict[str, Any]:
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    captures_dir = Path(args.captures_dir)
    artifact_root = Path(args.artifact_root or package_dir).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    namespace = _evidence_namespace(args.capture_id)
    stored_dir = artifact_root / "nw07-captures" / namespace
    stored_dir.mkdir(parents=True, exist_ok=True)
    reference_dir = Path(args.reference_captures_dir) if args.reference_captures_dir else None
    capture_artifacts = []
    per_tick = []
    numeric = _json_value_or_path(args.numeric_comparisons) or {}
    config = export.load_canonical_config()
    for tick in contract.REQUIRED_FIXED_TICKS:
        path = captures_dir / f"tick_{tick}.rgba32f"
        if not path.is_file():
            raise ValueError(f"fixed-tick capture is unavailable: {path}")
        stored = _retain_capture(path, stored_dir)
        relative = stored.relative_to(artifact_root)
        digest = _sha256_file(artifact_root / relative)
        capture_artifacts.append(
            {
                "kind": contract.CAPTURE_KIND,
                "format": contract.CAPTURE_FORMAT,
                "path": str(relative).replace("\\", "/"),
                "sha256": digest,
                "bytes": path.stat().st_size,
                "tick": tick,
                "width": contract.OUTPUT_WIDTH,
                "height": contract.OUTPUT_HEIGHT,
            }
        )
        if reference_dir is not None:
            reference_path = reference_dir / f"tick_{tick}.rgba32f"
            if not reference_path.is_file():
                raise ValueError(f"reference capture is unavailable: {reference_path}")
            reference_stored_dir = (
                artifact_root / "nw07-reference-captures" / namespace
            )
            reference_stored = _retain_capture(reference_path, reference_stored_dir)
            reference_relative = reference_stored.relative_to(artifact_root)
            image = _compare_images(path, reference_path)
            reference_digest = _sha256_file(reference_path)
            reference_relative_text = str(reference_relative).replace("\\", "/")
        else:
            image = {"maximum_error": None, "maximum_relative_error": None}
            reference_digest = None
            reference_relative_text = None
        numeric_row = numeric.get(str(tick), numeric.get(tick, {}))
        per_tick.append(
            {
                "tick": tick,
                "artifact_sha256": digest,
                "reference_artifact_sha256": reference_digest,
                "reference_artifact_path": reference_relative_text,
                "renderer": args.renderer,
                "reference_renderer": args.reference_renderer,
                "numeric": {
                    "maximum_error": numeric_row.get("maximum_error"),
                    "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                    "maximum_relative_error": numeric_row.get(
                        "maximum_relative_error"
                    ),
                    "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
                },
                "image": {
                    "maximum_error": image["maximum_error"],
                    "tolerance": contract.IMAGE_ABSOLUTE_TOLERANCE,
                    "maximum_relative_error": image["maximum_relative_error"],
                    "relative_tolerance": contract.IMAGE_RELATIVE_TOLERANCE,
                },
            }
        )
    graph_evidence = export.verify_canonical_graph(config)
    loop_boundary = _loop_boundary_evidence(captures_dir, graph_evidence)
    boundary_tick = contract.LOOP_BOUNDARY_TICKS[1]
    boundary_path = captures_dir / f"tick_{boundary_tick}.rgba32f"
    if boundary_path.is_file():
        boundary_stored = _retain_capture(boundary_path, stored_dir)
        boundary_relative = boundary_stored.relative_to(artifact_root)
        capture_artifacts.append(
            {
                "kind": contract.CAPTURE_KIND,
                "format": contract.CAPTURE_FORMAT,
                "path": str(boundary_relative).replace("\\", "/"),
                "sha256": _sha256_file(artifact_root / boundary_relative),
                "bytes": boundary_path.stat().st_size,
                "tick": boundary_tick,
                "width": contract.OUTPUT_WIDTH,
                "height": contract.OUTPUT_HEIGHT,
            }
        )
    metadata = _json_value_or_path(args.metadata) or {}
    capture = {
        "capture_id": args.capture_id,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.CORRECTNESS_COVERAGE_TARGETS[args.tier],
        "evidence_role": contract.FIXED_TICK_CORRECTNESS_ROLE,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "adapter_description": metadata.get("adapter_description", args.adapter),
        "device_form_factor": metadata.get("device_form_factor", getattr(args, "device_form_factor", "")),
        "device": metadata.get("device", args.adapter),
        "driver_version": metadata.get("driver_version", args.driver_version),
        "os_version": metadata.get("os_version", args.os_version),
        "toolchain_identity": metadata.get("toolchain_identity", args.toolchain_identity),
        "queried_capabilities": metadata.get("queried_capabilities", {}),
        "clock_information": metadata.get("clock_information", {}),
        "power_information": metadata.get("power_information", {}),
        "thermal_information": metadata.get("thermal_information", {}),
        "cache_state": metadata.get("cache_state", {}),
        "capture_protocol": contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "renderer": args.renderer,
        "execution_environment": {
            "kind": args.execution_kind,
            "documentation": args.passthrough_documentation,
        },
        "fixed_ticks": list(contract.REQUIRED_FIXED_TICKS),
        "capture_artifacts": capture_artifacts,
        "reference_execution": _load_json(args.reference_execution),
        "comparison": {
            "per_tick": per_tick,
            "loop_boundary": loop_boundary,
        },
        "startup": {
            "protocol": contract.STARTUP_PROTOCOL,
            "process_start_to_first_present_ms": args.startup_ms,
            "cache_state": {
                "kind": args.cache_kind,
                "application_pipeline_cache_used": False,
            },
            "cold_driver_cache_preparation": args.cold_preparation,
            "warm_preparation": args.warm_preparation,
        },
        "memory": {
            "peak_engine_committed_gpu_bytes": args.peak_gpu_bytes,
            "measurement_source": args.memory_source,
        },
        "measurements": {
            "process_ram_bytes": args.process_ram_bytes,
            "driver_memory_bytes": args.driver_memory_bytes,
            "startup_pipeline_ms": args.startup_pipeline_ms,
            "generation_ms": args.generation_ms,
            "image_quality_metrics": _json_value_or_path(args.image_quality),
            "scaling_1440p": _json_value_or_path(args.scaling_1440p),
        },
        "unavailable_telemetry": [],
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "loop_boundary_evidence": graph_evidence["loop_boundary"],
    }
    validation = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=identity,
        artifact_root=artifact_root,
        config=config,
    )
    capture["validation"] = validation
    capture["status"] = _validation_status(validation)
    return capture


def _expected_capture_bytes() -> int:
    return (
        contract.OUTPUT_WIDTH
        * contract.OUTPUT_HEIGHT
        * contract.CAPTURE_BYTES_PER_PIXEL
    )


def _run_external_capture(
    command: list[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(environment),
    )



def _normalize_fresh_correctness_metrics(
    measured: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    tier: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate fresh nightly metadata/measurements without static fallbacks."""
    metadata_value = measured.get("metadata")
    measurements_value = measured.get("measurements")
    numeric_value = measured.get("numeric_comparisons")
    if not isinstance(metadata_value, Mapping) or not metadata_value:
        raise ValueError("fresh metrics output has no metadata object")
    if not isinstance(measurements_value, Mapping) or not measurements_value:
        raise ValueError("fresh metrics output has no measurements object")
    if not isinstance(numeric_value, Mapping) or not numeric_value:
        raise ValueError("fresh metrics output has no numeric_comparisons object")
    metadata = dict(metadata_value)
    measurements = dict(measurements_value)
    required_metadata = (
        "adapter_description",
        "device_form_factor",
        "driver_version",
        "os_version",
        "toolchain_identity",
        "queried_capabilities",
        "clock_information",
        "power_information",
        "thermal_information",
        "cache_state",
    )
    missing_metadata = [
        name for name in required_metadata
        if metadata.get(name) in (None, "", {})
    ]
    if missing_metadata:
        raise ValueError(
            "fresh metrics metadata is incomplete: " + ", ".join(missing_metadata)
        )
    if metadata.get("device_form_factor") != "desktop":
        raise ValueError("fresh correctness metrics are not from a desktop device")
    observed_tier = evidence.classify_hardware_tier(
        str(metadata.get("adapter_description", "")),
        device_form_factor=str(metadata.get("device_form_factor", "")),
    )
    if observed_tier != tier:
        raise ValueError(
            f"fresh metrics report {observed_tier!r} hardware, expected {tier!r}"
        )
    required_measurements = (
        "process_start_to_first_present_ms",
        "peak_engine_committed_gpu_bytes",
        "cache_kind",
        "cold_driver_cache_preparation",
        "warm_preparation",
        "memory_source",
        "process_ram_bytes",
        "driver_memory_bytes",
        "startup_pipeline_ms",
        "generation_ms",
        "image_quality_metrics",
        "scaling_1440p",
    )
    missing_measurements = [
        name for name in required_measurements
        if measurements.get(name) in (None, "", {})
    ]
    if missing_measurements:
        raise ValueError(
            "fresh metrics measurements are incomplete: "
            + ", ".join(missing_measurements)
        )
    environment = {
        "adapter_description": metadata["adapter_description"],
        "driver_version": metadata["driver_version"],
        "os_version": metadata["os_version"],
        "toolchain_identity": metadata["toolchain_identity"],
    }
    for name in ("image_quality_metrics", "scaling_1440p"):
        value = measurements.get(name)
        if not isinstance(value, Mapping) or not value:
            raise ValueError(f"fresh {name} measurement is not an object")
        bound = dict(value)
        bound["artifact_sha256"] = identity["executable_sha256"]
        bound["environment"] = dict(environment)
        measurements[name] = bound
    expected_ticks = {str(int(tick)) for tick in contract.REQUIRED_FIXED_TICKS}
    numeric_ticks = {str(key) for key in numeric_value.keys()}
    if not expected_ticks.issubset(numeric_ticks):
        missing = sorted(expected_ticks - numeric_ticks)
        raise ValueError(
            "fresh numeric comparisons do not cover required ticks: "
            + ", ".join(missing)
        )
    return metadata, measurements, dict(numeric_value)


def _failed_release_correctness_capture(
    args: argparse.Namespace,
    identity: Mapping[str, Any],
    *,
    failure: str,
    executions: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "capture_id": args.capture_id,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.CORRECTNESS_COVERAGE_TARGETS[args.tier],
        "evidence_role": contract.FIXED_TICK_CORRECTNESS_ROLE,
        "status": "failed",
        "artifact_key": identity.get("artifact_key"),
        "package_sha256": identity.get("package_sha256"),
        "manifest_sha256": identity.get("manifest_sha256"),
        "executable_sha256": identity.get("executable_sha256"),
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "release_capture_protocol": contract.RELEASE_CAPTURE_PROTOCOL,
        "execution_records": [dict(row) for row in executions],
        "failure": failure,
        "validation": {"status": "failed", "reasons": [failure]},
        "captured_at_utc": _utc_now(),
    }


def capture_release_correctness(args: argparse.Namespace) -> dict[str, Any]:
    """Capture every frozen tick from the identical final release executable."""
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    release = Path(args.executable)
    diagnostic = Path(args.diagnostic_executable)
    if not release.is_file():
        raise ValueError(f"release executable is unavailable: {release}")
    if _sha256_file(release) != identity["executable_sha256"]:
        raise ValueError("release executable does not match the published final artifact")
    if not diagnostic.is_file():
        raise ValueError(f"diagnostic sibling executable is unavailable: {diagnostic}")
    diagnostic_sha = _sha256_file(diagnostic)
    if diagnostic_sha == identity["executable_sha256"]:
        raise ValueError("diagnostic reference must be a separate sibling executable")

    capture_command = shlex.split(args.capture_command, posix=os.name != "nt")
    metrics_command = shlex.split(args.metrics_command, posix=os.name != "nt")
    if not capture_command:
        raise ValueError("an external release capture command is required")
    if not metrics_command:
        raise ValueError("a fresh metrics command is required")
    metrics_output = Path(args.metrics_output)
    metrics_output.parent.mkdir(parents=True, exist_ok=True)
    metrics_environment = dict(os.environ)
    metrics_environment.update(
        {
            "NW07_PACKAGE": str(package_dir),
            "NW07_EXECUTABLE": str(release),
            "NW07_ARTIFACT_SHA256": identity["executable_sha256"],
            "NW07_METRICS_OUTPUT": str(metrics_output),
            "NW07_TIER": str(args.tier),
            "NW07_CAPTURE_ID": str(args.capture_id),
        }
    )
    metrics_completed = _run_external_capture(
        metrics_command, environment=metrics_environment, timeout=300.0
    )
    if metrics_completed.returncode != 0 or not metrics_output.is_file():
        raise ValueError(
            "fresh startup/memory/quality measurement command failed: "
            + (metrics_completed.stderr.strip() or metrics_completed.stdout.strip())
        )
    measured = json.loads(metrics_output.read_text(encoding="utf-8"))
    if not isinstance(measured, Mapping):
        raise ValueError("fresh metrics output is not a JSON object")
    metadata, measurements, numeric_comparisons = _normalize_fresh_correctness_metrics(
        measured, identity=identity, tier=args.tier
    )

    captures_dir = Path(args.captures_dir)
    reference_dir = Path(args.reference_captures_dir)
    captures_dir.mkdir(parents=True, exist_ok=True)
    reference_dir.mkdir(parents=True, exist_ok=True)
    ticks = list(contract.REQUIRED_FIXED_TICKS) + [contract.LOOP_BOUNDARY_TICKS[1]]
    executions: list[dict[str, Any]] = []

    for tick in ticks:
        output = captures_dir / f"tick_{tick}.rgba32f"
        started = _utc_now()
        player: subprocess.Popen[str] | None = None
        capture_completed: subprocess.CompletedProcess[str] | None = None
        stdout = ""
        stderr = ""
        try:
            player = _start_player(release, ["--tick", str(tick), "--frames", "1"])
            time.sleep(args.capture_delay_ms / 1000.0)
            env = dict(os.environ)
            env.update(
                {
                    "NW07_TICK": str(tick),
                    "NW07_OUTPUT": str(output),
                    "NW07_PLAYER_PID": str(player.pid),
                    "NW07_EXECUTABLE": str(release),
                    "NW07_ARTIFACT_SHA256": identity["executable_sha256"],
                }
            )
            capture_completed = _run_external_capture(
                capture_command,
                environment=env,
                timeout=max(30.0, contract.RELEASE_CAPTURE_HOLD_MS / 1000.0 + 20.0),
            )
            try:
                stdout, stderr = player.communicate(
                    timeout=max(30.0, contract.RELEASE_CAPTURE_HOLD_MS / 1000.0 + 20.0)
                )
            except subprocess.TimeoutExpired:
                stdout, stderr = _stop_player(player)
            if not output.is_file() or output.stat().st_size != _expected_capture_bytes():
                raise ValueError(
                    f"external capture did not produce a complete RGBA32F image for tick {tick}"
                )
            if player.returncode != 0:
                raise ValueError(
                    f"release executable failed at fixed tick {tick}: exit {player.returncode}"
                )
            if capture_completed.returncode != 0:
                raise ValueError(
                    f"external capture process failed at tick {tick}: "
                    f"{capture_completed.stderr.strip() or capture_completed.stdout.strip()}"
                )
        except Exception as exc:
            if player is not None and player.poll() is None:
                more_stdout, more_stderr = _stop_player(player)
                stdout += more_stdout
                stderr += more_stderr
            executions.append(
                {
                    "renderer": "exported_player",
                    "tick": tick,
                    "executable_sha256": identity["executable_sha256"],
                    "player_pid": player.pid if player is not None else None,
                    "started_utc": started,
                    "ended_utc": _utc_now(),
                    "exit_code": player.returncode if player is not None else None,
                    "capture_exit_code": (
                        capture_completed.returncode
                        if capture_completed is not None
                        else None
                    ),
                    "player_stdout": stdout[-2000:],
                    "player_stderr": stderr[-2000:],
                    "status": "failed",
                    "failure": str(exc),
                }
            )
            return _failed_release_correctness_capture(
                args, identity, failure=str(exc), executions=executions
            )

        executions.append(
            {
                "renderer": "exported_player",
                "tick": tick,
                "executable_sha256": identity["executable_sha256"],
                "player_pid": player.pid,
                "started_utc": started,
                "ended_utc": _utc_now(),
                "exit_code": player.returncode,
                "capture_command": capture_command,
                "capture_exit_code": capture_completed.returncode,
                "capture_stdout": capture_completed.stdout[-2000:],
                "capture_stderr": capture_completed.stderr[-2000:],
                "player_stdout": (stdout or "")[-2000:],
                "player_stderr": (stderr or "")[-2000:],
                "artifact_sha256": _sha256_file(output),
                "bytes": output.stat().st_size,
                "status": "passed",
            }
        )

        diagnostic_completed = subprocess.run(
            [
                str(diagnostic),
                "--capture-dir",
                str(reference_dir),
                "--capture-tick",
                str(tick),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=dict(os.environ),
        )
        reference_output = reference_dir / f"tick_{tick}.rgba32f"
        if (
            diagnostic_completed.returncode != 0
            or not reference_output.is_file()
            or reference_output.stat().st_size != _expected_capture_bytes()
        ):
            failure = f"diagnostic reference capture failed at fixed tick {tick}"
            executions.append(
                {
                    "renderer": "diagnostic_preview",
                    "tick": tick,
                    "executable_sha256": diagnostic_sha,
                    "exit_code": diagnostic_completed.returncode,
                    "stdout": diagnostic_completed.stdout[-2000:],
                    "stderr": diagnostic_completed.stderr[-2000:],
                    "status": "failed",
                    "failure": failure,
                }
            )
            return _failed_release_correctness_capture(
                args, identity, failure=failure, executions=executions
            )
        executions.append(
            {
                "renderer": "diagnostic_preview",
                "tick": tick,
                "executable_sha256": diagnostic_sha,
                "exit_code": diagnostic_completed.returncode,
                "artifact_sha256": _sha256_file(reference_output),
                "bytes": reference_output.stat().st_size,
                "status": "passed",
            }
        )

    reference_execution = {
        "execution_id": f"diagnostic-preview-{args.capture_id}",
        "renderer": "diagnostic_preview",
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": diagnostic_sha,
        "capture_protocol": contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "fixed_tick_executions": [
            item for item in executions if item["renderer"] == "diagnostic_preview"
        ],
    }
    reference_path = Path(args.reference_execution_output)
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_text(
        json.dumps(reference_execution, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    names = {
        "package": args.package,
        "captures_dir": str(captures_dir),
        "artifact_root": args.artifact_root,
        "reference_captures_dir": str(reference_dir),
        "capture_id": args.capture_id,
        "tier": args.tier,
        "renderer": "exported_player",
        "reference_renderer": "diagnostic_preview",
        "reference_execution": str(reference_path),
        "execution_kind": args.execution_kind,
        "passthrough_documentation": args.passthrough_documentation,
        "metadata": metadata,
        "numeric_comparisons": numeric_comparisons,
        "adapter": metadata["adapter_description"],
        "device_form_factor": metadata["device_form_factor"],
        "driver_version": metadata["driver_version"],
        "os_version": metadata["os_version"],
        "toolchain_identity": metadata["toolchain_identity"],
        "startup_ms": measurements["process_start_to_first_present_ms"],
        "peak_gpu_bytes": measurements["peak_engine_committed_gpu_bytes"],
        "cache_kind": measurements["cache_kind"],
        "cold_preparation": measurements["cold_driver_cache_preparation"],
        "warm_preparation": measurements["warm_preparation"],
        "memory_source": measurements["memory_source"],
        "process_ram_bytes": measurements["process_ram_bytes"],
        "driver_memory_bytes": measurements["driver_memory_bytes"],
        "startup_pipeline_ms": measurements["startup_pipeline_ms"],
        "generation_ms": measurements["generation_ms"],
        "image_quality": measurements["image_quality_metrics"],
        "scaling_1440p": measurements["scaling_1440p"],
    }
    capture = record_correctness(argparse.Namespace(**names))
    capture["execution_records"] = executions
    capture["release_capture_protocol"] = contract.RELEASE_CAPTURE_PROTOCOL
    validation = evidence.validate_correctness_capture(
        capture,
        coverage_target=capture["coverage_target"],
        artifact_identity=identity,
        artifact_root=Path(args.artifact_root or package_dir),
        config=export.load_canonical_config(),
    )
    capture["validation"] = validation
    capture["status"] = _validation_status(validation)
    return capture

def record_perf(args: argparse.Namespace) -> dict[str, Any]:
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    production = export.load_canonical_config()
    if production.get("quality", {}).get("settings", {}).get("vsync") is not False:
        raise ValueError("frozen production configuration does not disable vsync")
    contract_record = json.loads(Path(args.contract).read_text(encoding="utf-8"))
    reference = _load_json(args.reference)
    samples_dir = Path(args.samples_dir)
    runs = []
    for index in range(1, contract.PERF_RUN_COUNT + 1):
        gpu = _sample_section(samples_dir / f"run{index}-gpu.txt")
        cpu = _sample_section(samples_dir / f"run{index}-cpu.txt")
        displayed = _sample_section(samples_dir / f"run{index}-displayed.txt")
        timing_path = samples_dir / f"run{index}-timing.json"
        timing = _load_json(str(timing_path)) if timing_path.is_file() else {}
        if not isinstance(timing, Mapping):
            timing = {}
        run_started = _utc_seconds(
            timing.get("run_started_utc") or timing.get("warmup_ended_utc")
        )
        run_ended = _utc_seconds(timing.get("run_ended_utc"))
        duration_seconds = (
            run_ended - run_started
            if run_started is not None and run_ended is not None
            else None
        )
        runs.append(
            {
                "run_index": index,
                "duration_seconds": duration_seconds,
                "player_pid": timing.get("player_pid"),
                "collector_protocol": timing.get("collector_protocol"),
                "artifact_sha256": timing.get("artifact_sha256"),
                "collector_exit_code": timing.get("collector_exit_code"),
                "uncapped": timing.get("uncapped"),
                "fresh_process": timing.get("fresh_process"),
                "application_pipeline_cache": timing.get(
                    "application_pipeline_cache"
                ),
                "player_exited_early": timing.get("player_exited_early"),
                "run_started_utc": timing.get("run_started_utc"),
                "run_ended_utc": timing.get("run_ended_utc"),
                "timing": timing,
                "sample_density_hz": (
                    min(gpu["sample_count"], cpu["sample_count"]) / duration_seconds
                    if duration_seconds
                    else None
                ),
                "gpu_frame_time_ms": gpu,
                "cpu_submit_ms": cpu,
                "displayed_frame_intervals_ms": {
                    "samples": displayed["samples"],
                    "gate": False,
                },
            }
        )
    capture = {
        "capture_id": args.capture_id,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.PERF_CONTRACT_ID,
        "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
        "contract_id": contract.PERF_CONTRACT_ID,
        "execution_environment": {
            "kind": args.execution_kind,
            "documentation": args.passthrough_documentation,
        },
        "reference": reference,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "tools": dict(contract_record.get("tools", {})),
        "protocol": {
            "protocol_id": contract.PERF_RUN_PROTOCOL,
            "warmup_seconds": contract.WARMUP_SECONDS,
            "runs": contract.PERF_RUN_COUNT,
            "run_seconds": contract.PERF_RUN_SECONDS,
            "uncapped": True,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
        },
        "present_sync_interval": 0,
        "presentation_mode": "flip_discard",
        "uncapped": True,
        "presentation_probe": getattr(args, "presentation_probe", None),
        "reference_binding_digest": contract_record.get("binding_digest"),
        "reference_binding_id": contract_record.get("binding_id"),
        "warmup": getattr(args, "warmup", None),
        "runs": runs,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    validation = evidence.validate_perf_capture(
        capture,
        contract_record,
        artifact_identity=identity,
        artifact_root=None,
    )
    capture["validation"] = validation
    capture["status"] = _validation_status(validation)
    return capture


def record_diagnostic(args: argparse.Namespace) -> dict[str, Any]:
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    measurements = _load_json(args.measurements)
    capture = {
        "capture_id": args.capture_id,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": "diagnostic_sibling_build",
        "evidence_role": contract.DIAGNOSTIC_OVERHEAD_ROLE,
        "protocol": contract.DIAGNOSTIC_OVERHEAD_PROTOCOL,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "release": {
            "executable_sha256": identity["executable_sha256"],
            "contains_capture_controls": False,
        },
        "diagnostic": {
            "build_profile": contract.DIAGNOSTIC_BUILD_PROFILE,
            "executable_sha256": args.diagnostic_sha256,
        },
        "measurements": measurements,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    validation = evidence.validate_diagnostic_overhead(
        capture, artifact_identity=identity
    )
    capture["validation"] = validation
    capture["status"] = _validation_status(validation)
    return capture


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_player(executable: Path, player_args: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(executable), *player_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def _stop_player(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    return stdout or "", stderr or ""


def _timeout_text(exc: subprocess.TimeoutExpired) -> str:
    parts: list[str] = []
    for value in (exc.stdout, exc.stderr):
        if isinstance(value, bytes):
            parts.append(value.decode("utf-8", errors="replace"))
        elif value:
            parts.append(str(value))
    parts.append(str(exc))
    return "\\n".join(parts)[-4000:]


def _failed_collection_capture(
    args: argparse.Namespace,
    identity: Mapping[str, Any],
    *,
    failure: str,
    runs: list[Any] | None = None,
    collection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "capture_id": args.capture_id,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.PERF_CONTRACT_ID,
        "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
        "contract_id": contract.PERF_CONTRACT_ID,
        "status": "failed",
        "artifact_key": identity.get("artifact_key"),
        "package_sha256": identity.get("package_sha256"),
        "manifest_sha256": identity.get("manifest_sha256"),
        "executable_sha256": identity.get("executable_sha256"),
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "failure": failure,
        "runs": list(runs or []),
        "collection": dict(collection or {}),
        "validation": {"status": "failed", "reasons": [failure]},
        "captured_at_utc": _utc_now(),
    }


def collect_perf(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the frozen warm-up/three-run protocol and retain raw samples.

    The release player is launched as a fresh process for every run.  An
    external measurement collector (PresentMon/GPUView or equivalent) is
    required to write the raw GPU, CPU-submission, and displayed-interval
    samples; this harness never fabricates samples when the collector produces
    nothing.  Executed failures are persisted with all available logs and
    samples instead of escaping before evidence is written.
    """
    config = export.load_canonical_config()
    if (config.get("quality", {}).get("settings", {}).get("vsync")) is not False:
        raise ValueError("frozen production configuration does not disable vsync")
    identity = export.inspect_artifact_directory(Path(args.package))
    executable = Path(args.executable)
    if not executable.is_file():
        raise ValueError(f"release executable is unavailable: {executable}")
    actual_sha = _sha256_file(executable)
    if actual_sha != identity["executable_sha256"]:
        return _failed_collection_capture(
            args,
            identity,
            failure="release executable does not match the published final artifact",
        )
    collector = shlex.split(args.collector_command, posix=os.name != "nt")
    if not collector:
        return _failed_collection_capture(
            args, identity, failure="a measurement collector command is required"
        )
    player_args = shlex.split(args.player_args, posix=os.name != "nt")
    samples_dir = Path(args.samples_dir)
    samples_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "protocol": COLLECT_PROTOCOL,
        "artifact_key": identity["artifact_key"],
        "executable_sha256": identity["executable_sha256"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "present_sync_interval": 0,
        "uncapped": True,
        "fresh_process_per_run": True,
        "application_pipeline_cache": False,
        "warmup_seconds": contract.WARMUP_SECONDS,
        "runs": [],
        "collected_at_utc": _utc_now(),
    }

    try:
        probe = subprocess.run(
            [str(executable), "--frames", "1"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=dict(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record["failure"] = f"uncapped-presentation probe failed: {exc}"
        (samples_dir / "collector-record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return _failed_collection_capture(
            args, identity, failure=record["failure"], collection=record
        )
    record["presentation_probe"] = {
        "exit_code": probe.returncode,
        "present_sync_interval_line": "NW05_PRESENT_SYNC_INTERVAL=0",
        "stdout": probe.stdout[-4000:],
        "stderr": probe.stderr[-4000:],
    }
    if (
        probe.returncode != 0
        or "NW05_PRESENT_SYNC_INTERVAL=0" not in probe.stdout
    ):
        record["failure"] = (
            "release executable did not execute with uncapped presentation; "
            "performance evidence cannot be accepted"
        )
        (samples_dir / "collector-record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return _failed_collection_capture(
            args, identity, failure=record["failure"], collection=record
        )

    # One frozen five-second warm-up, ended before teardown so process
    # termination never extends the measured window.
    warmup_started = _utc_now()
    try:
        warmup_process = _start_player(executable, player_args)
    except OSError as exc:
        record["failure"] = f"release executable could not be launched: {exc}"
        (samples_dir / "collector-record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return _failed_collection_capture(
            args, identity, failure=record["failure"], collection=record
        )
    warmup_pid = warmup_process.pid
    time.sleep(contract.WARMUP_SECONDS)
    warmup_exited_early = warmup_process.poll() is not None
    warmup_ended = _utc_now()
    warmup_stdout, warmup_stderr = _stop_player(warmup_process)
    warmup_duration = (
        (_utc_seconds(warmup_ended) or 0.0) - (_utc_seconds(warmup_started) or 0.0)
    )
    record["warmup"] = {
        "warmup_started_utc": warmup_started,
        "warmup_ended_utc": warmup_ended,
        "duration_seconds": warmup_duration,
        "player_pid": warmup_pid,
        "artifact_sha256": identity["executable_sha256"],
        "completed": not warmup_exited_early,
        "stdout": warmup_stdout[-2000:],
        "stderr": warmup_stderr[-2000:],
    }
    if warmup_exited_early:
        record["failure"] = (
            "release executable exited before the five-second warm-up completed"
        )
        (samples_dir / "collector-record.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return _failed_collection_capture(
            args, identity, failure=record["failure"], collection=record
        )

    failure: str | None = None
    for index in range(1, contract.PERF_RUN_COUNT + 1):
        try:
            player = _start_player(executable, player_args)
        except OSError as exc:
            failure = f"release executable could not be launched for run {index}: {exc}"
            record["failure"] = failure
            record["runs"].append(
                {
                    "run_index": index,
                    "status": "failed",
                    "failure": failure,
                    "player_pid": None,
                }
            )
            break
        run_started = _utc_now()
        run_dir = samples_dir / f"run{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "NW07_COLLECT_PROTOCOL": COLLECT_PROTOCOL,
                "NW07_ARTIFACT_SHA256": identity["executable_sha256"],
                "NW07_RUN_INDEX": str(index),
                "NW07_PLAYER_PID": str(player.pid),
                "NW07_RUN_SECONDS": str(contract.PERF_RUN_SECONDS),
                "NW07_RUN_DIR": str(run_dir),
                "NW07_GPU_SAMPLES": str(samples_dir / f"run{index}-gpu.txt"),
                "NW07_CPU_SAMPLES": str(samples_dir / f"run{index}-cpu.txt"),
                "NW07_DISPLAYED_SAMPLES": str(
                    samples_dir / f"run{index}-displayed.txt"
                ),
            }
        )
        collector_completed: subprocess.CompletedProcess[str] | None = None
        collector_error = ""
        try:
            collector_completed = subprocess.run(
                collector,
                capture_output=True,
                text=True,
                timeout=contract.PERF_RUN_SECONDS + 30,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            collector_error = _timeout_text(exc)
        except OSError as exc:
            collector_error = str(exc)
        player_exited_early = player.poll() is not None
        # The measurement window ends before teardown.  Terminating the
        # indefinitely looping player must not inflate the retained duration.
        run_ended = _utc_now()
        player_stdout, player_stderr = _stop_player(player)
        duration_seconds = (
            (_utc_seconds(run_ended) or 0.0) - (_utc_seconds(run_started) or 0.0)
        )
        collector_stdout = (
            collector_completed.stdout[-4000:] if collector_completed else ""
        )
        collector_stderr = (
            collector_completed.stderr[-4000:] if collector_completed else ""
        )
        collector_exit = (
            collector_completed.returncode if collector_completed is not None else None
        )
        timing = {
            "warmup_started_utc": warmup_started,
            "warmup_ended_utc": warmup_ended,
            "run_started_utc": run_started,
            "run_ended_utc": run_ended,
            "duration_seconds": duration_seconds,
            "uncapped": True,
            "fresh_process": True,
            "application_pipeline_cache": False,
            "player_pid": player.pid,
            "artifact_sha256": identity["executable_sha256"],
            "collector_protocol": COLLECT_PROTOCOL,
            "collector_exit_code": collector_exit,
            "player_exited_early": player_exited_early,
            "collector_error": collector_error,
            "collector_stdout": collector_stdout,
            "collector_stderr": collector_stderr,
            "player_stdout": player_stdout[-4000:],
            "player_stderr": player_stderr[-4000:],
        }
        (samples_dir / f"run{index}-timing.json").write_text(
            json.dumps(timing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        sample_presence = {
            name: (
                (samples_dir / f"run{index}-{name}.txt").is_file()
                and (samples_dir / f"run{index}-{name}.txt").stat().st_size > 0
            )
            for name in ("gpu", "cpu", "displayed")
        }
        record["runs"].append(
            {
                "run_index": index,
                "status": (
                    "failed"
                    if collector_error
                    or collector_exit not in (None, 0)
                    or player_exited_early
                    or not all(sample_presence.values())
                    else "passed"
                ),
                "timing": timing,
                "player_pid": player.pid,
                "collector_exit_code": collector_exit,
                "collector_error": collector_error,
                "player_exited_early": player_exited_early,
                "samples_present": sample_presence,
            }
        )
        if collector_error:
            failure = f"measurement collector failed for run {index}: {collector_error}"
        elif collector_exit not in (None, 0):
            failure = (
                f"measurement collector failed for run {index}: "
                f"{collector_stderr.strip() or collector_stdout.strip()}"
            )
        elif player_exited_early:
            failure = f"release executable exited before measured run {index} completed"
        else:
            for name in ("gpu", "cpu", "displayed"):
                if not sample_presence[name]:
                    failure = (
                        f"measurement collector produced no {name} samples for run {index}"
                    )
                    break
        if failure:
            record["failure"] = failure
            break

    (samples_dir / "collector-record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if failure:
        return _failed_collection_capture(
            args, identity, failure=failure, runs=record["runs"], collection=record
        )

    namespace = argparse.Namespace(
        package=args.package,
        contract=args.contract,
        samples_dir=str(samples_dir),
        reference=args.reference,
        capture_id=args.capture_id,
        execution_kind=args.execution_kind,
        passthrough_documentation=args.passthrough_documentation,
        evidence=args.evidence,
        presentation_probe=record["presentation_probe"],
        warmup=record["warmup"],
    )
    capture = record_perf(namespace)
    capture["collection"] = record
    return capture

def _append_record(path: Path, capture: Mapping[str, Any]) -> dict[str, Any]:
    record = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else evidence.pending_record()
    )
    updated = evidence.record_execution(record, capture)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return updated



def merge_evidence_files(
    base_path: Path,
    input_paths: list[Path],
    output_path: Path,
) -> dict[str, Any]:
    """Merge independently collected nightly fragments without status laundering."""
    if not base_path.is_file():
        raise ValueError(f"base evidence is unavailable: {base_path}")
    merged_value = json.loads(base_path.read_text(encoding="utf-8"))
    if not isinstance(merged_value, Mapping):
        raise ValueError("base evidence is not a JSON object")
    merged = dict(merged_value)
    merged.setdefault("captures", [])
    existing = {
        str(item.get("capture_id")): item
        for item in merged["captures"]
        if isinstance(item, Mapping) and item.get("capture_id")
    }
    for input_path in input_paths:
        fragment = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(fragment, Mapping):
            raise ValueError(f"evidence fragment is not a JSON object: {input_path}")
        for item in fragment.get("captures", []):
            if not isinstance(item, Mapping) or not item.get("capture_id"):
                raise ValueError(f"evidence fragment has an invalid capture: {input_path}")
            capture_id = str(item["capture_id"])
            prior = existing.get(capture_id)
            if prior is not None:
                if prior != item:
                    raise ValueError(
                        f"capture id {capture_id!r} has conflicting evidence fragments"
                    )
                continue
            copied = dict(item)
            merged["captures"].append(copied)
            existing[capture_id] = copied
    merged["executions"] = list(merged["captures"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return merged

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    correctness = subparsers.add_parser("correctness")
    correctness.add_argument("--package", required=True)
    correctness.add_argument("--captures-dir", required=True)
    correctness.add_argument("--artifact-root")
    correctness.add_argument("--reference-captures-dir", required=True)
    correctness.add_argument("--capture-id", required=True)
    correctness.add_argument("--tier", choices=("ada", "blackwell"), required=True)
    correctness.add_argument(
        "--renderer", choices=("exported_player", "diagnostic_preview"), required=True
    )
    correctness.add_argument(
        "--reference-renderer",
        choices=("exported_player", "diagnostic_preview"),
        required=True,
    )
    correctness.add_argument("--reference-execution", required=True)
    correctness.add_argument(
        "--execution-kind",
        choices=("physical", "documented_gpu_passthrough"),
        default="physical",
    )
    correctness.add_argument("--passthrough-documentation", default="")
    correctness.add_argument("--metadata")
    correctness.add_argument("--numeric-comparisons")
    correctness.add_argument("--adapter", default="")
    correctness.add_argument(
        "--device-form-factor",
        choices=("desktop", "laptop", "mobile", "workstation", "data_center", "server", "other"),
        default="",
    )
    correctness.add_argument("--driver-version", default="")
    correctness.add_argument("--os-version", default="")
    correctness.add_argument("--toolchain-identity", default="")
    correctness.add_argument("--startup-ms", type=float, required=True)
    correctness.add_argument("--peak-gpu-bytes", type=int, required=True)
    correctness.add_argument("--cache-kind", choices=("cold_driver_cache", "warm"), required=True)
    correctness.add_argument("--cold-preparation", required=True)
    correctness.add_argument("--warm-preparation", required=True)
    correctness.add_argument("--memory-source", required=True)
    correctness.add_argument("--process-ram-bytes", type=int)
    correctness.add_argument("--driver-memory-bytes", type=int)
    correctness.add_argument("--startup-pipeline-ms", type=float)
    correctness.add_argument("--generation-ms", type=float)
    correctness.add_argument("--image-quality")
    correctness.add_argument("--scaling-1440p")
    correctness.add_argument("--evidence", required=True)

    release_correctness = subparsers.add_parser("release-correctness")
    release_correctness.add_argument("--package", required=True)
    release_correctness.add_argument("--executable", required=True)
    release_correctness.add_argument("--diagnostic-executable", required=True)
    release_correctness.add_argument("--capture-command", required=True)
    release_correctness.add_argument("--captures-dir", required=True)
    release_correctness.add_argument("--artifact-root")
    release_correctness.add_argument("--reference-captures-dir", required=True)
    release_correctness.add_argument("--reference-execution-output", required=True)
    release_correctness.add_argument("--capture-id", required=True)
    release_correctness.add_argument("--tier", choices=("ada", "blackwell"), required=True)
    release_correctness.add_argument(
        "--execution-kind",
        choices=("physical", "documented_gpu_passthrough"),
        default="physical",
    )
    release_correctness.add_argument("--passthrough-documentation", default="")
    release_correctness.add_argument("--metadata")
    release_correctness.add_argument("--numeric-comparisons")
    release_correctness.add_argument("--adapter", default="")
    release_correctness.add_argument(
        "--device-form-factor",
        choices=("desktop", "laptop", "mobile", "workstation", "data_center", "server", "other"),
        default="",
    )
    release_correctness.add_argument("--driver-version", default="")
    release_correctness.add_argument("--os-version", default="")
    release_correctness.add_argument("--toolchain-identity", default="")
    release_correctness.add_argument("--startup-ms", type=float)
    release_correctness.add_argument("--peak-gpu-bytes", type=int)
    release_correctness.add_argument("--cache-kind", choices=("cold_driver_cache", "warm"))
    release_correctness.add_argument("--cold-preparation")
    release_correctness.add_argument("--warm-preparation")
    release_correctness.add_argument("--memory-source")
    release_correctness.add_argument("--process-ram-bytes", type=int)
    release_correctness.add_argument("--driver-memory-bytes", type=int)
    release_correctness.add_argument("--startup-pipeline-ms", type=float)
    release_correctness.add_argument("--generation-ms", type=float)
    release_correctness.add_argument("--image-quality")
    release_correctness.add_argument("--scaling-1440p")
    release_correctness.add_argument("--capture-delay-ms", type=int, default=750)
    release_correctness.add_argument("--metrics-command", required=True)
    release_correctness.add_argument("--metrics-output", required=True)
    release_correctness.add_argument("--evidence", required=True)

    perf = subparsers.add_parser("perf")
    perf.add_argument("--package", required=True)
    perf.add_argument("--contract", default=str(REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"))
    perf.add_argument("--samples-dir", required=True)
    perf.add_argument("--reference", required=True)
    perf.add_argument("--capture-id", required=True)
    perf.add_argument(
        "--execution-kind",
        choices=("physical", "documented_gpu_passthrough"),
        default="physical",
    )
    perf.add_argument("--passthrough-documentation", default="")
    perf.add_argument("--evidence", required=True)

    diagnostic = subparsers.add_parser("diagnostic-overhead")
    diagnostic.add_argument("--package", required=True)
    diagnostic.add_argument("--measurements", required=True)
    diagnostic.add_argument("--diagnostic-sha256", required=True)
    diagnostic.add_argument("--capture-id", default="diagnostic-overhead")
    diagnostic.add_argument("--evidence", required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--package", required=True)
    collect.add_argument("--executable", required=True)
    collect.add_argument("--contract", default=str(REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"))
    collect.add_argument("--samples-dir", required=True)
    collect.add_argument("--reference", required=True)
    collect.add_argument("--collector-command", required=True)
    collect.add_argument("--player-args", default="--frames 0")
    collect.add_argument("--capture-id", required=True)
    collect.add_argument(
        "--execution-kind",
        choices=("physical", "documented_gpu_passthrough"),
        default="physical",
    )
    collect.add_argument("--passthrough-documentation", default="")
    collect.add_argument("--evidence", required=True)

    merge = subparsers.add_parser("merge-evidence")
    merge.add_argument("--base", required=True)
    merge.add_argument("--input", action="append", required=True)
    merge.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    if args.command == "merge-evidence":
        try:
            merged = merge_evidence_files(
                Path(args.base),
                [Path(value) for value in args.input],
                Path(args.output),
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "format": "nw07-evidence-merge-failure",
                        "status": "failed",
                        "error": str(exc),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        print(
            json.dumps(
                {"status": "merged", "captures": len(merged.get("captures", []))},
                sort_keys=True,
            )
        )
        return 0
    try:
        if args.command == "correctness":
            capture = record_correctness(args)
        elif args.command == "release-correctness":
            capture = capture_release_correctness(args)
        elif args.command == "perf":
            capture = record_perf(args)
        elif args.command == "diagnostic-overhead":
            capture = record_diagnostic(args)
        else:
            capture = collect_perf(args)
    except Exception as exc:
        role = {
            "correctness": contract.FIXED_TICK_CORRECTNESS_ROLE,
            "release-correctness": contract.FIXED_TICK_CORRECTNESS_ROLE,
            "perf": contract.PERFORMANCE_CERTIFICATION_ROLE,
            "collect": contract.PERFORMANCE_CERTIFICATION_ROLE,
            "diagnostic-overhead": contract.DIAGNOSTIC_OVERHEAD_ROLE,
        }.get(args.command, "unverified_capture")
        failure_record = {
            "capture_id": getattr(args, "capture_id", f"{args.command}-failure"),
            "task": contract.TASK,
            "implementation_base": contract.IMPLEMENTATION_BASE,
            "profile_id": contract.PROFILE_ID,
            "requirements_digest": contract.REQUIREMENTS_DIGEST,
            "evidence_role": role,
            "status": "failed",
            "failure": str(exc),
            "captured_at_utc": _utc_now(),
        }
        try:
            _append_record(Path(args.evidence), failure_record)
        except Exception:
            pass
        print(
            json.dumps(
                {
                    "format": "nw07-capture-failure",
                    "command": args.command,
                    "capture_id": getattr(args, "capture_id", ""),
                    "status": "failed",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        _append_record(Path(args.evidence), capture)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "format": "nw07-evidence-persist-failure",
                    "status": "failed",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(capture, indent=2, sort_keys=True))
    validation_status = str(
        (capture.get("validation") or {}).get("status", "")
    )
    if capture.get("status") == "failed" or validation_status == "failed":
        return 1
    if validation_status in {"pending_unverified", "unverified"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
