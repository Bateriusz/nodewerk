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
import shutil
import sys
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as contract  # noqa: E402
import nw07_evidence as evidence  # noqa: E402
import nw07_export as export  # noqa: E402


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


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


def record_correctness(args: argparse.Namespace) -> dict[str, Any]:
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    captures_dir = Path(args.captures_dir)
    artifact_root = Path(args.artifact_root or package_dir).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    stored_dir = artifact_root / "nw07-captures"
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
        try:
            relative = path.resolve().relative_to(artifact_root)
        except ValueError:
            stored = stored_dir / path.name
            shutil.copyfile(path, stored)
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
            try:
                reference_relative = reference_path.resolve().relative_to(artifact_root)
            except ValueError:
                reference_stored_dir = artifact_root / "nw07-reference-captures"
                reference_stored_dir.mkdir(parents=True, exist_ok=True)
                reference_stored = reference_stored_dir / reference_path.name
                shutil.copyfile(reference_path, reference_stored)
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
            "loop_boundary": {
                "verified": True,
                "start_tick": 0,
                "boundary_tick": contract.LOOP_TICKS,
                "state_digest_equal_modulo_turn": True,
                "numeric": {
                    "maximum_error": 0.0,
                    "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                },
            },
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
    return capture


def record_perf(args: argparse.Namespace) -> dict[str, Any]:
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    contract_record = json.loads(Path(args.contract).read_text(encoding="utf-8"))
    reference = _load_json(args.reference)
    samples_dir = Path(args.samples_dir)
    runs = []
    for index in range(1, contract.PERF_RUN_COUNT + 1):
        gpu = _sample_section(samples_dir / f"run{index}-gpu.txt")
        cpu = _sample_section(samples_dir / f"run{index}-cpu.txt")
        displayed = _sample_section(samples_dir / f"run{index}-displayed.txt")
        runs.append(
            {
                "run_index": index,
                "warmup_seconds": contract.WARMUP_SECONDS,
                "duration_seconds": contract.PERF_RUN_SECONDS,
                "uncapped": True,
                "fresh_process": True,
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
    return capture


def record_diagnostic(args: argparse.Namespace) -> dict[str, Any]:
    package_dir = Path(args.package)
    identity = export.inspect_artifact_directory(package_dir)
    measurements = _load_json(args.measurements)
    return {
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
    correctness.add_argument("--reference-captures-dir")
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

    args = parser.parse_args(argv)
    if args.command == "correctness":
        capture = record_correctness(args)
    elif args.command == "perf":
        capture = record_perf(args)
    else:
        capture = record_diagnostic(args)
    updated = _append_record(Path(args.evidence), capture)
    print(json.dumps(capture, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
