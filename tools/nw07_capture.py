#!/usr/bin/env python3
"""NW-07 benchmark/capture harness.

The harness only records measurements it is given.  It never synthesizes
reference provenance, never upgrades a missing value to a pass, and never
writes a certifying status that the evidence validator would not derive from
the raw data.  Raw fixed-tick RGBA32F captures and raw per-run sample files are
retained and hashed.
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

COLLECT_PROTOCOL = "nw07-perf-collect.v1"


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
    numeric = _load_json(args.numeric_comparisons)
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
    metadata = _load_json(args.metadata)
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
            "image_quality_metrics": _load_json(args.image_quality),
            "scaling_1440p": _load_json(args.scaling_1440p),
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
        warmup_started = _utc_seconds(timing.get("warmup_started_utc"))
        duration_seconds = (
            run_ended - run_started
            if run_started is not None and run_ended is not None
            else None
        )
        warmup_seconds = (
            run_started - warmup_started
            if run_started is not None and warmup_started is not None
            else None
        )
        runs.append(
            {
                "run_index": index,
                "warmup_seconds": warmup_seconds,
                "duration_seconds": duration_seconds,
                "uncapped": timing.get("uncapped"),
                "fresh_process": timing.get("fresh_process"),
                "application_pipeline_cache": timing.get(
                    "application_pipeline_cache"
                ),
                "warmup_started_utc": timing.get("warmup_started_utc"),
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
        "execution_environment": {"kind": args.execution_kind},
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
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    return stdout or "", stderr or ""


def collect_perf(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the frozen warm-up/three-run protocol and retain raw samples.

    The release player is launched as a fresh process for every run.  An
    external measurement collector (PresentMon/GPUView or equivalent) is
    required to write the raw GPU, CPU-submission, and displayed-interval
    samples; this harness never fabricates samples when the collector produces
    nothing.
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
        raise ValueError(
            "release executable does not match the published final artifact"
        )
    collector = shlex.split(args.collector_command, posix=os.name != "nt")
    if not collector:
        raise ValueError("a measurement collector command is required")
    player_args = shlex.split(args.player_args, posix=os.name != "nt")
    samples_dir = Path(args.samples_dir)
    samples_dir.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(
        [str(executable), "--frames", "1"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=dict(os.environ),
    )
    if (
        probe.returncode != 0
        or "NW05_PRESENT_SYNC_INTERVAL=0" not in probe.stdout
    ):
        raise ValueError(
            "release executable did not execute with uncapped presentation; "
            "performance evidence cannot be accepted"
        )
    record = {
        "protocol": COLLECT_PROTOCOL,
        "artifact_key": identity["artifact_key"],
        "executable_sha256": identity["executable_sha256"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "present_sync_interval": 0,
        "uncapped": True,
        "fresh_process_per_run": True,
        "application_pipeline_cache": False,
        "presentation_probe": {
            "exit_code": probe.returncode,
            "present_sync_interval_line": "NW05_PRESENT_SYNC_INTERVAL=0",
            "stdout": probe.stdout[-4000:],
            "stderr": probe.stderr[-4000:],
        },
        "warmup_seconds": contract.WARMUP_SECONDS,
        "runs": [],
        "collected_at_utc": _utc_now(),
    }
    for index in range(1, contract.PERF_RUN_COUNT + 1):
        warmup_started = _utc_now()
        warmup = _start_player(executable, player_args)
        time.sleep(contract.WARMUP_SECONDS)
        warmup_stdout, warmup_stderr = _stop_player(warmup)
        run_started = _utc_now()
        player = _start_player(executable, player_args)
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
        try:
            collector_completed = subprocess.run(
                collector,
                capture_output=True,
                text=True,
                timeout=contract.PERF_RUN_SECONDS + 30,
                check=False,
                env=env,
            )
        finally:
            player_stdout, player_stderr = _stop_player(player)
        run_ended = _utc_now()
        timing = {
            "warmup_started_utc": warmup_started,
            "run_started_utc": run_started,
            "run_ended_utc": run_ended,
            "duration_seconds": contract.PERF_RUN_SECONDS,
            "warmup_seconds": contract.WARMUP_SECONDS,
            "uncapped": True,
            "fresh_process": True,
            "application_pipeline_cache": False,
            "collector_protocol": COLLECT_PROTOCOL,
            "collector_exit_code": collector_completed.returncode,
            "collector_stdout": collector_completed.stdout[-4000:],
            "collector_stderr": collector_completed.stderr[-4000:],
            "player_stdout": player_stdout[-4000:],
            "player_stderr": player_stderr[-4000:],
            "warmup_stdout": warmup_stdout[-2000:],
            "warmup_stderr": warmup_stderr[-2000:],
        }
        (samples_dir / f"run{index}-timing.json").write_text(
            json.dumps(timing, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        record["runs"].append(
            {
                "run_index": index,
                "timing": timing,
                "collector_exit_code": collector_completed.returncode,
                "samples_present": {
                    name: (samples_dir / f"run{index}-{name}.txt").is_file()
                    for name in ("gpu", "cpu", "displayed")
                },
            }
        )
        if collector_completed.returncode != 0:
            raise ValueError(
                f"measurement collector failed for run {index}: "
                f"{collector_completed.stderr.strip() or collector_completed.stdout.strip()}"
            )
        for name in ("gpu", "cpu", "displayed"):
            sample_path = samples_dir / f"run{index}-{name}.txt"
            if not sample_path.is_file() or sample_path.stat().st_size == 0:
                raise ValueError(
                    f"measurement collector produced no {name} samples for run {index}"
                )
    manifest_path = samples_dir / "collector-record.json"
    manifest_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    namespace = argparse.Namespace(
        package=args.package,
        contract=args.contract,
        samples_dir=str(samples_dir),
        reference=args.reference,
        capture_id=args.capture_id,
        execution_kind=args.execution_kind,
        evidence=args.evidence,
        presentation_probe=record["presentation_probe"],
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
    collect.add_argument("--evidence", required=True)

    args = parser.parse_args(argv)
    if args.command == "correctness":
        capture = record_correctness(args)
    elif args.command == "perf":
        capture = record_perf(args)
    elif args.command == "diagnostic-overhead":
        capture = record_diagnostic(args)
    else:
        capture = collect_perf(args)
    updated = _append_record(Path(args.evidence), capture)
    print(json.dumps(capture, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
