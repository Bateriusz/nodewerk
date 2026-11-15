#!/usr/bin/env python3
"""NW-07 canonical 256 KiB production export and size accounting.

The exporter consumes the frozen graph and pinned build inputs through the
existing NW-03 compiler / NW-05 export / NW-06 procedural-scene path.  It does
not introduce a second graph model, package format, player, or demo runtime.

The NW-07 hard ceiling applies to the complete runnable distribution: the one
self-contained Windows x64 executable, which already embeds the `.nwp` package
and the generated link manifest.  Auxiliary build/evidence JSON files are not
required to execute and are reported separately; they are never counted as
runtime distribution bytes and can never excuse an over-ceiling executable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import dependency_check  # noqa: E402
import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw06_export  # noqa: E402
import nw06_procedural as procedural  # noqa: E402
import nw06_timeline as timeline  # noqa: E402
import nw07_contract as contract  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402

CANONICAL_CONFIG = REPO_ROOT / "benchmarks" / "nw07" / "canonical-production.v1.json"
PRODUCTION_GRAPH = REPO_ROOT / contract.CANONICAL_CONFIG_RELATIVE_PATHS[0]
CANONICAL_CONFIG_SCHEMA = REPO_ROOT / "schemas" / "nw07-production-config.schema.json"
EXPORT_REPORT_SCHEMA = REPO_ROOT / "schemas" / "nw07-export-report.schema.json"

EXPORT_REPORT_FORMAT = "nw07-export-report"
EXPORT_REPORT_VERSION = 1

# The NW-05 exporter applies its own aux-inclusive budget.  NW-07 evaluates the
# runnable distribution independently, so this internal guard is deliberately
# high enough to avoid rejecting a valid executable because of evidence JSON.
INTERNAL_NW05_BYTE_CEILING = 64 * 1024 * 1024

FORBIDDEN_RUNTIME_DEPENDENCIES = frozenset(
    {
        "dxcompiler",
        "dxil",
        "d3dcompiler_47",
        "nodewerk-studio",
        "nodewerkc",
        "slang",
        "vcruntime140",
        "vcruntime140_1",
        "msvcp140",
    }
)


class Nw07ExportError(RuntimeError):
    """Raised when an NW-07 export or budget check fails closed."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_canonical_text_file(path: Path) -> str:
    """Hash a pinned text input independently of checkout line endings.

    The frozen build-input digest must be identical on Windows and POSIX
    checkouts.  Git may materialize text files as CRLF on Windows unless an
    explicit attribute says otherwise; normalizing line endings here keeps the
    digest deterministic without weakening the file-content identity.
    """
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def load_canonical_config(path: Path | None = None) -> dict[str, Any]:
    config_path = Path(path or CANONICAL_CONFIG)
    record = json.loads(config_path.read_text(encoding="utf-8"))
    return validate_canonical_config_record(record)


def validate_canonical_config_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the canonical NW-07 contract to a file-backed or caller-supplied config."""
    if not isinstance(record, Mapping):
        raise Nw07ExportError("canonical production config is not an object")

    def section(name: str) -> Mapping[str, Any]:
        value = record.get(name)
        if not isinstance(value, Mapping):
            raise Nw07ExportError(
                f"canonical production section '{name}' is not an object"
            )
        return value

    def number(container: Mapping[str, Any], key: str, default: float = 0.0) -> float:
        try:
            value = float(container.get(key, default))
        except (TypeError, ValueError) as exc:
            raise Nw07ExportError(
                f"canonical production value '{key}' is not numeric"
            ) from exc
        if not math.isfinite(value) or value < 0.0:
            raise Nw07ExportError(
                f"canonical production value '{key}' must be finite and nonnegative"
            )
        return value

    if record.get("format") != "nw07-canonical-production":
        raise Nw07ExportError("canonical production config has the wrong format")
    if record.get("version") != 1:
        raise Nw07ExportError("canonical production config version is not 1")
    if record.get("implementation_base") != contract.IMPLEMENTATION_BASE:
        raise Nw07ExportError("canonical production config is not bound to the NW-07 base")
    if record.get("profile_id") != contract.PROFILE_ID:
        raise Nw07ExportError("canonical production config changed the common profile")
    if record.get("requirements_digest") != contract.REQUIREMENTS_DIGEST:
        raise Nw07ExportError("canonical production config changed the requirements digest")
    graph = record.get("graph")
    if not isinstance(graph, str) or not graph.strip():
        raise Nw07ExportError("canonical production graph path is missing")
    loop = section("loop")
    if loop.get("ticks") != contract.LOOP_TICKS:
        raise Nw07ExportError("canonical production loop is not 960,000 ticks")
    if loop.get("duration_seconds") != contract.LOOP_SECONDS:
        raise Nw07ExportError("canonical production loop is not 20 seconds")
    if loop.get("looping") is not True:
        raise Nw07ExportError("canonical production loop is not looping")
    resolution = section("resolution")
    if (
        resolution.get("width") != contract.OUTPUT_WIDTH
        or resolution.get("height") != contract.OUTPUT_HEIGHT
    ):
        raise Nw07ExportError("canonical production resolution is not 1920x1080")
    content = section("content")
    if content.get("torus_instances") != contract.TORUS_INSTANCE_COUNT:
        raise Nw07ExportError("canonical production does not contain 256 torus instances")
    for key in (
        "seeded",
        "noise_material",
        "camera",
        "twist",
        "lighting",
        "bloom",
        "tonemapping",
    ):
        if not content.get(key):
            raise Nw07ExportError(f"canonical production content is missing '{key}'")
    for key in (
        "noise_material",
        "torus",
        "camera",
        "twist",
        "lighting",
        "bloom",
        "tonemapping",
    ):
        if not isinstance(content.get(key), Mapping):
            raise Nw07ExportError(
                f"canonical production content.{key} is not an object"
            )
    budget = section("artifact_budget")
    if budget.get("complete_distribution_bytes_ceiling") != contract.COMPLETE_DISTRIBUTION_CEILING_BYTES:
        raise Nw07ExportError("canonical production weakened the 256 KiB ceiling")
    if budget.get("stretch_distribution_bytes_ceiling") != contract.STRETCH_DISTRIBUTION_CEILING_BYTES:
        raise Nw07ExportError("canonical production changed the 128 KiB stretch measurement")
    if budget.get("stretch_is_mandatory_gate") is not False:
        raise Nw07ExportError("the 128 KiB stretch measurement must not be a gate")
    if budget.get("peak_engine_committed_gpu_bytes_ceiling") != contract.PEAK_ENGINE_COMMITTED_GPU_BYTES:
        raise Nw07ExportError("canonical production weakened the 512 MiB GPU budget")
    if number(budget, "process_start_to_first_present_ms_ceiling") != contract.PROCESS_START_TO_FIRST_PRESENT_MS:
        raise Nw07ExportError("canonical production weakened the 2 s startup gate")
    tolerances = section("image_tolerances")
    numeric_tolerances = tolerances.get("numeric")
    image_tolerances = tolerances.get("image")
    if not isinstance(numeric_tolerances, Mapping) or not isinstance(
        image_tolerances, Mapping
    ):
        raise Nw07ExportError(
            "canonical production numeric/image tolerance sections are not objects"
        )
    if number(numeric_tolerances, "absolute", 1.0) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
        raise Nw07ExportError("canonical production weakened the numeric absolute tolerance")
    if number(numeric_tolerances, "relative", 1.0) > contract.NUMERIC_RELATIVE_TOLERANCE:
        raise Nw07ExportError("canonical production weakened the numeric relative tolerance")
    if number(image_tolerances, "absolute", 1.0) > contract.IMAGE_ABSOLUTE_TOLERANCE:
        raise Nw07ExportError("canonical production weakened the image absolute tolerance")
    if number(image_tolerances, "relative", 1.0) > contract.IMAGE_RELATIVE_TOLERANCE:
        raise Nw07ExportError("canonical production weakened the image relative tolerance")
    quality = section("quality")
    settings = quality.get("settings")
    if not isinstance(settings, Mapping):
        raise Nw07ExportError("canonical production quality settings are not an object")
    if quality.get("profile_id") != "nw07.quality.v1":
        raise Nw07ExportError("canonical production changed the quality profile")
    if settings.get("resolution") != {"width": contract.OUTPUT_WIDTH, "height": contract.OUTPUT_HEIGHT}:
        raise Nw07ExportError("canonical production changed quality resolution")
    for name, expected in (
        ("vsync", False),
        ("msaa_samples", 1),
        ("bloom_resolution_scale", 0.5),
        ("tonemap_operator", "aces"),
        ("tone_exposure", 1.1),
        ("tone_gamma", 2.2),
        ("feature_level", "12_0"),
        ("shader_model", "6_0"),
    ):
        if settings.get(name) != expected:
            raise Nw07ExportError(f"canonical production changed quality setting '{name}'")
    capture = section("capture")
    if capture.get("protocol") != contract.CORRECTNESS_CAPTURE_PROTOCOL:
        raise Nw07ExportError("canonical production changed the correctness capture protocol")
    if capture.get("startup_protocol") != contract.STARTUP_PROTOCOL:
        raise Nw07ExportError("canonical production changed the startup protocol")
    if capture.get("perf_run_protocol") != contract.PERF_RUN_PROTOCOL:
        raise Nw07ExportError("canonical production changed the performance protocol")
    if capture.get("diagnostic_overhead_protocol") != contract.DIAGNOSTIC_OVERHEAD_PROTOCOL:
        raise Nw07ExportError("canonical production changed the diagnostic-overhead protocol")
    if capture.get("application_pipeline_cache_used_on_clean_start") is not False:
        raise Nw07ExportError("canonical production allows an application pipeline cache")
    diagnostics = section("diagnostics")
    if diagnostics.get("release_build_profile") != contract.RELEASE_BUILD_PROFILE:
        raise Nw07ExportError("canonical production changed the release build profile")
    if diagnostics.get("diagnostic_sibling_build_profile") != contract.DIAGNOSTIC_BUILD_PROFILE:
        raise Nw07ExportError("canonical production changed the diagnostic build profile")
    if diagnostics.get("release_contains_capture_controls") is not False:
        raise Nw07ExportError("release build may not contain capture controls")
    if diagnostics.get("diagnostic_build_is_separate_artifact") is not True:
        raise Nw07ExportError("diagnostic build must be a separate artifact")
    if diagnostics.get("diagnostic_overhead_measured_separately") is not True:
        raise Nw07ExportError("diagnostic overhead must be measured separately")
    build_configuration = section("build_configuration")
    if build_configuration.get("cmake_generator") != "Ninja":
        raise Nw07ExportError("canonical production changed the CMake generator")
    if build_configuration.get("build_type") != "MinSizeRel":
        raise Nw07ExportError("canonical production changed the release build type")
    if build_configuration.get("build_profile") != contract.RELEASE_BUILD_PROFILE:
        raise Nw07ExportError("canonical production changed the release build profile")
    if build_configuration.get("pinned_toolchain_enforcement") != "ON":
        raise Nw07ExportError(
            "canonical production does not enforce the pinned toolchain"
        )
    if build_configuration.get("present_sync_interval") != 0:
        raise Nw07ExportError(
            "canonical production does not freeze uncapped presentation"
        )
    if (
        build_configuration.get("specialized_build_requires_empty_directory")
        is not True
    ):
        raise Nw07ExportError(
            "canonical production allows a reused specialized-player build tree"
        )
    fixed = section("fixed_tick_evaluation")
    if list(fixed.get("ticks") or []) != list(contract.REQUIRED_FIXED_TICKS):
        raise Nw07ExportError("canonical production changed the fixed-tick coverage")
    if list(fixed.get("loop_boundary_ticks") or []) != list(contract.LOOP_BOUNDARY_TICKS):
        raise Nw07ExportError("canonical production changed the loop-boundary coverage")
    if fixed.get("deterministic_replay") is not True or fixed.get("stateless_scene") is not True:
        raise Nw07ExportError("canonical production weakened deterministic replay")
    regression = section("regression")
    if number(regression, "matched_environment_relative_alert_percent", -1.0) != contract.MATCHED_ENVIRONMENT_RELATIVE_ALERT_PERCENT:
        raise Nw07ExportError("canonical production changed the regression alert threshold")
    if regression.get("size_percentage_only_failure") is not False:
        raise Nw07ExportError("canonical production enabled a size-only percentage failure")
    if regression.get("relative_comparison_requires_matched_environment") is not True:
        raise Nw07ExportError("canonical production allows unmatched relative regression flags")
    configured_inputs = record.get("pinned_inputs")
    if (
        not isinstance(configured_inputs, list)
        or not all(isinstance(item, str) for item in configured_inputs)
        or len(configured_inputs) != len(contract.PINNED_BUILD_INPUTS)
        or set(configured_inputs) != set(contract.PINNED_BUILD_INPUTS)
    ):
        raise Nw07ExportError(
            "canonical production pinned_inputs do not match the authoritative "
            "pinned build-input contract"
        )
    pinned_digest = record.get("build_inputs_digest")
    if (
        not isinstance(pinned_digest, str)
        or not pinned_digest.strip()
        or pinned_digest.strip() in {"pending", "sha256:v1:pending-final-freeze"}
    ):
        raise Nw07ExportError(
            "canonical production has no frozen build-input digest"
        )
    return record


def compute_pinned_inputs(
    config: Mapping[str, Any] | None = None,
    *,
    repo_root: Path | None = None,
) -> dict[str, str]:
    root = Path(repo_root or REPO_ROOT)
    configured = (config or {}).get("pinned_inputs")
    if configured is not None and (
        not isinstance(configured, list)
        or not all(isinstance(item, str) for item in configured)
        or len(configured) != len(contract.PINNED_BUILD_INPUTS)
        or set(configured) != set(contract.PINNED_BUILD_INPUTS)
    ):
        raise Nw07ExportError(
            "pinned_inputs do not match the authoritative pinned build-input contract"
        )
    paths = list(contract.PINNED_BUILD_INPUTS)
    result: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise Nw07ExportError(f"pinned build input is unavailable: {relative}")
        result[relative] = sha256_canonical_text_file(path)
    return dict(sorted(result.items()))


def build_inputs_digest(inputs: Mapping[str, str]) -> str:
    return "sha256:v1:" + sha256_bytes(canonical_json_bytes(dict(sorted(inputs.items()))))


def verify_pinned_inputs(
    config: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    inputs = compute_pinned_inputs(config, repo_root=repo_root)
    digest = build_inputs_digest(inputs)
    pinned = str(config.get("build_inputs_digest", ""))
    frozen = pinned not in {
        "",
        "pending",
        "sha256:v1:pending-final-freeze",
    }
    return {
        "inputs": inputs,
        "digest": digest,
        "frozen_digest": pinned,
        "frozen": frozen,
        "matches_frozen_digest": frozen and digest == pinned,
    }


def _parse_curve_keys(node: nw03.GraphNode) -> tuple[tuple[int, float], ...]:
    text = " ".join(node.params.get("keys", ["0:0"]))
    keys: list[tuple[int, float]] = []
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        tick_text, value_text = entry.split(":", 1)
        keys.append((int(tick_text), float(value_text)))
    keys.sort()
    return tuple(keys)


def evaluate_camera_at_tick(
    document: nw03.GraphDocument, tick: int
) -> dict[str, Any]:
    camera_nodes = [
        node
        for node in document.nodes.values()
        if node.operator_id in {"nw.camera.curve", "nw.camera.perspective"}
    ]
    twist_nodes = [
        node for node in document.nodes.values() if node.operator_id == "nw.animation.twist"
    ]
    if len(camera_nodes) != 1 or len(twist_nodes) != 1:
        raise Nw07ExportError("production graph must have one camera and one twist operator")
    keys = _parse_curve_keys(camera_nodes[0])
    curve = timeline.Curve(
        tuple(timeline.CurveKey(key_tick, value) for key_tick, value in keys),
        value_min=min(value for _, value in keys),
        value_max=max(value for _, value in keys),
    )
    wrapped_tick = tick % contract.LOOP_TICKS
    angle = curve.sample(wrapped_tick)
    max_twist = float(twist_nodes[0].params.get("max_twist_degrees", ["180"])[0])
    phase = wrapped_tick / contract.LOOP_TICKS
    twist_degrees = max_twist * math.sin(2.0 * math.pi * phase)
    return {
        "tick": int(tick),
        "wrapped_tick": int(wrapped_tick),
        "camera_angle_radians": angle,
        "camera_angle_modulo_turn": angle % (2.0 * math.pi),
        "twist_degrees": twist_degrees,
    }


def verify_canonical_graph(
    config: Mapping[str, Any] | None = None,
    *,
    graph_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT)
    record = (
        validate_canonical_config_record(dict(config))
        if config is not None
        else load_canonical_config()
    )
    path = Path(graph_path or (root / str(record["graph"])))
    if not path.is_file():
        raise Nw07ExportError(f"canonical production graph is unavailable: {path}")
    graph_digest = sha256_file(path)
    if graph_digest != record.get("graph_sha256"):
        raise Nw07ExportError(
            "canonical production graph digest does not match the frozen config",
            details={
                "expected": record.get("graph_sha256"),
                "observed": graph_digest,
            },
        )
    document = nw03.load_graph(path)
    if document.profile_id != contract.PROFILE_ID:
        raise Nw07ExportError("production graph changed the common profile")
    if document.requirements_digest != contract.REQUIREMENTS_DIGEST:
        raise Nw07ExportError("production graph changed the requirements digest")
    expected_settings = {
        "nw06_ticks_per_second": str(contract.TICKS_PER_SECOND),
        "nw06_output_width": str(contract.OUTPUT_WIDTH),
        "nw06_output_height": str(contract.OUTPUT_HEIGHT),
        "nw06_frame_step_numerator": str(contract.FRAME_STEP_NUMERATOR),
        "nw06_frame_step_denominator": str(contract.FRAME_STEP_DENOMINATOR),
        "nw07_output_width": str(contract.OUTPUT_WIDTH),
        "nw07_output_height": str(contract.OUTPUT_HEIGHT),
        "nw07_loop_ticks": str(contract.LOOP_TICKS),
        "nw07_loop_seconds": str(contract.LOOP_SECONDS),
    }
    for name, expected in expected_settings.items():
        if str(document.settings.get(name, "")).strip() != expected:
            raise Nw07ExportError(
                f"production graph setting '{name}' is not '{expected}'"
            )
    optimized = optimizer.optimize_graph(document)
    used = {node.operator_id for node in optimized.nodes}
    missing = sorted(contract.required_operations() - used)
    if missing:
        raise Nw07ExportError(f"production graph is missing operators {missing}")
    for node_id in ("texture", "texture_gpu", "torus", "mesh_gpu", "instances", "scene_gpu"):
        if node_id not in optimized.partitions.get("initialization", ()):
            raise Nw07ExportError(f"'{node_id}' is not initialization-frequency")
    if "render" not in optimized.partitions.get("frame", ()):
        raise Nw07ExportError("render pass is not frame-frequency")
    node_map = optimized.node_map()
    instances = node_map["instances"]
    if int(float(instances.params.get("count", ["0"])[0])) != contract.TORUS_INSTANCE_COUNT:
        raise Nw07ExportError("production graph does not use 256 torus instances")
    content = record.get("content", {})
    if int(float(instances.params.get("seed", ["0"])[0])) != int(
        content.get("instance_seed", -1)
    ):
        raise Nw07ExportError("production instance seed does not match the frozen config")
    if float(instances.params.get("twist_radians", ["0"])[0]) <= 0.0:
        raise Nw07ExportError("production instance placement twist is not enabled")
    noise = node_map["texture"]
    if noise.params.get("format", [""])[0] != "r8g8b8a8_unorm":
        raise Nw07ExportError("production material texture format changed")
    noise_config = content.get("noise_material", {})
    for name in ("width", "height", "seed", "octaves"):
        if int(float(noise.params.get(name, ["-1"])[0])) != int(noise_config.get(name, -2)):
            raise Nw07ExportError(f"production noise parameter '{name}' changed")
    torus_config = content.get("torus", {})
    torus = node_map["torus"]
    for name in ("major_segments", "minor_segments"):
        if int(float(torus.params.get(name, ["-1"])[0])) != int(torus_config.get(name, -2)):
            raise Nw07ExportError(f"production torus parameter '{name}' changed")
    for name in ("major_radius", "minor_radius"):
        if abs(float(torus.params.get(name, ["-1"])[0]) - float(torus_config.get(name, -2))) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
            raise Nw07ExportError(f"production torus parameter '{name}' changed")
    twist = node_map["twist"]
    if abs(
        float(twist.params.get("max_twist_degrees", ["0"])[0])
        - float(content.get("twist", {}).get("max_twist_degrees", -1))
    ) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
        raise Nw07ExportError("production twist animation changed")
    sun = node_map["sun"]
    if [float(value) for value in sun.params.get("direction", [])] != [
        float(value) for value in content.get("lighting", {}).get("direction", [])
    ]:
        raise Nw07ExportError("production analytic light direction changed")
    bloom = node_map["bloom"]
    bloom_config = content.get("bloom", {})
    for name in ("threshold", "intensity", "radius", "resolution_scale"):
        if abs(
            float(bloom.params.get(name, ["-1"])[0])
            - float(bloom_config.get(name, -2))
        ) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
            raise Nw07ExportError(f"production bloom parameter '{name}' changed")
    tonemap = node_map["tonemap"]
    tonemap_config = content.get("tonemapping", {})
    if tonemap.params.get("operator", [""])[0] != tonemap_config.get("operator", ""):
        raise Nw07ExportError("production tonemap operator changed")
    for name in ("exposure", "gamma"):
        if abs(
            float(tonemap.params.get(name, ["-1"])[0])
            - float(tonemap_config.get(name, -2))
        ) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
            raise Nw07ExportError(f"production tonemap parameter '{name}' changed")
    camera = node_map["camera"]
    keys = _parse_curve_keys(camera)
    if len(keys) != 4:
        raise Nw07ExportError("production camera curve must have four keys")
    if keys[0][0] != 0 or keys[-1][0] != contract.LOOP_TICKS:
        raise Nw07ExportError("production camera curve does not span the loop")
    if abs(keys[-1][1] - keys[0][1] - 2.0 * math.pi) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
        raise Nw07ExportError("production camera loop does not complete one revolution")
    for required in ("nw.render.present", "frame.present"):
        if required == "nw.render.present" and required not in used:
            raise Nw07ExportError("production graph has no terminal present operator")
    # Deterministic absolute-tick evaluation: replaying the same tick and
    # wrapping the loop must not depend on accumulated state.
    samples = []
    for tick in contract.REQUIRED_FIXED_TICKS:
        first = evaluate_camera_at_tick(document, tick)
        second = evaluate_camera_at_tick(document, tick)
        if first != second:
            raise Nw07ExportError("stateless absolute-tick evaluation is not deterministic")
        samples.append(first)
    start = evaluate_camera_at_tick(document, 0)
    boundary = evaluate_camera_at_tick(document, contract.LOOP_TICKS)
    turn = 2.0 * math.pi
    boundary_delta = abs(
        ((boundary["camera_angle_radians"] - start["camera_angle_radians"]) % turn)
    )
    if boundary_delta > contract.NUMERIC_ABSOLUTE_TOLERANCE and abs(
        boundary_delta - turn
    ) > contract.NUMERIC_ABSOLUTE_TOLERANCE:
        raise Nw07ExportError("camera loop boundary is not seamless modulo one turn")
    if contract.LOOP_TICKS % contract.FRAME_STEP_NUMERATOR != 0:
        raise Nw07ExportError("loop length is not an exact integer frame count")
    return {
        "graph_path": str(path),
        "graph_sha256": graph_digest,
        "ir_sha256": optimized.ir_sha256(),
        "reachable_node_ids": list(optimized.reachable_node_ids),
        "partitions": {name: list(values) for name, values in optimized.partitions.items()},
        "used_operators": sorted(used),
        "fixed_tick_samples": samples,
        "loop_boundary": {
            "start": start,
            "boundary": boundary,
            "seamless_modulo_turn": True,
        },
        "frame_count_per_loop": contract.LOOP_TICKS // contract.FRAME_STEP_NUMERATOR,
    }


def _artifact_paths(
    directory: Path,
) -> tuple[Path, Path, Path]:
    stem = nw05_export._profile_file_stem(contract.PROFILE_ID)
    package = directory / f"{stem}.nwp"
    manifest = directory / f"{stem}.manifest.json"
    executable = directory / f"{stem}.exe"
    for label, path in (
        ("package", package),
        ("manifest", manifest),
        ("executable", executable),
    ):
        if not path.is_file():
            raise Nw07ExportError(f"NW-07 {label} is unavailable: {path}")
    return package, manifest, executable


def _runtime_dependency_check(manifest: Mapping[str, Any]) -> dict[str, Any]:
    runtime = manifest.get("runtime_requirements", {})
    dependencies = [
        str(name).lower()
        for name in manifest.get("retained", {}).get("dependencies", [])
    ]
    forbidden = sorted(
        name
        for name in dependencies
        if any(token in name for token in FORBIDDEN_RUNTIME_DEPENDENCIES)
    )
    if runtime.get("runtime_dxc_dependency") is not False:
        raise Nw07ExportError("link manifest allows a runtime DXC dependency")
    if runtime.get("runtime_slang_dependency") is not False:
        raise Nw07ExportError("link manifest allows a runtime Slang dependency")
    if forbidden:
        raise Nw07ExportError(
            "distribution includes a forbidden runtime dependency",
            details={"forbidden": forbidden, "dependencies": dependencies},
        )
    excluded = manifest.get("excluded", {})
    authoring = list(excluded.get("authoring_infrastructure", []))
    if not authoring:
        raise Nw07ExportError("link manifest does not exclude authoring infrastructure")
    if excluded.get("gpu_name_predicates"):
        raise Nw07ExportError("link manifest contains GPU-name compatibility predicates")
    return {
        "dependencies": dependencies,
        "forbidden": forbidden,
        "runtime_dxc_dependency": False,
        "runtime_slang_dependency": False,
        "authoring_infrastructure_excluded": authoring,
        "gpu_name_predicates": [],
    }


def inspect_artifact_directory(
    directory: Path | str,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify an already-published artifact and return its complete identity."""
    artifact_directory = Path(directory)
    package_path, manifest_path, executable_path = _artifact_paths(artifact_directory)
    package_bytes = package_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    executable_bytes = executable_path.read_bytes()
    inspection = nwp.inspect_package_bytes(package_bytes, allow_dev=False)
    if not inspection.valid or inspection.package is None:
        raise Nw07ExportError(
            "published package is not a valid release package",
            details={"diagnostics": inspection.diagnostics},
        )
    package = inspection.package
    if package.capability.profile_id != contract.PROFILE_ID:
        raise Nw07ExportError("published artifact changed the common profile")
    if package.capability.requirements_digest != contract.REQUIREMENTS_DIGEST:
        raise Nw07ExportError("published artifact changed the requirements digest")
    embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
        executable_bytes
    )
    if embedded_package != package_bytes:
        raise Nw07ExportError("executable does not embed the published package")
    if embedded_manifest != manifest_bytes:
        raise Nw07ExportError("executable does not embed the published manifest")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    dependency = _runtime_dependency_check(manifest)
    embedded_bytes = (
        len(package_bytes) + len(manifest_bytes) + nw05_export.EMBED_FOOTER_SIZE
    )
    player_image_bytes = len(executable_bytes) - embedded_bytes
    if player_image_bytes < 0:
        raise Nw07ExportError("executable is smaller than its embedded payload")
    complete = len(executable_bytes)
    within_ceiling = complete <= contract.COMPLETE_DISTRIBUTION_CEILING_BYTES
    within_stretch = complete <= contract.STRETCH_DISTRIBUTION_CEILING_BYTES
    auxiliary_bytes = sum(
        path.stat().st_size
        for path in artifact_directory.iterdir()
        if path.is_file()
        and path not in {package_path, manifest_path, executable_path}
    )
    return {
        "format": "nw07-artifact-identity",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "artifact_key": str(manifest.get("artifact_identity", "")),
        "package_sha256": sha256_bytes(package_bytes),
        "package_bytes": len(package_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "manifest_bytes": len(manifest_bytes),
        "manifest_declared_sha256": str(manifest.get("manifest_sha256", "")),
        "executable_sha256": sha256_bytes(executable_bytes),
        "executable_bytes": complete,
        "link_manifest_sha256": sha256_bytes(manifest_bytes),
        "embedded_package_bytes": len(embedded_package),
        "embedded_manifest_bytes": len(embedded_manifest),
        "embedded_footer_bytes": nw05_export.EMBED_FOOTER_SIZE,
        "player_image_bytes": player_image_bytes,
        "auxiliary_evidence_bytes": auxiliary_bytes,
        "size_accounting": {
            "complete_distribution_bytes": complete,
            "required_non_system_bytes": complete,
            "player_image_bytes": player_image_bytes,
            "embedded_package_bytes": len(embedded_package),
            "embedded_manifest_bytes": len(embedded_manifest),
            "embedded_footer_bytes": nw05_export.EMBED_FOOTER_SIZE,
            "system_dependency_bytes": 0,
            "auxiliary_evidence_bytes": auxiliary_bytes,
            "all_required_bytes_accounted": (
                player_image_bytes
                + len(embedded_package)
                + len(embedded_manifest)
                + nw05_export.EMBED_FOOTER_SIZE
                == complete
            ),
            "ceiling": contract.COMPLETE_DISTRIBUTION_CEILING_BYTES,
            "within_ceiling": within_ceiling,
            "stretch_ceiling": contract.STRETCH_DISTRIBUTION_CEILING_BYTES,
            "within_stretch": within_stretch,
            "stretch_mandatory_gate": False,
        },
        "dependency_closure": dependency,
        "capability": {
            "profile_id": package.capability.profile_id,
            "requirements_digest": package.capability.requirements_digest,
            "emitted_shader_targets": list(package.capability.emitted_shader_targets),
            "required_init_instructions": list(package.capability.required_init_instructions),
            "required_frame_instructions": list(package.capability.required_frame_instructions),
        },
        "init_opcodes": [item.opcode for item in package.initialization],
        "frame_opcodes": [item.opcode for item in package.frame],
        "resource_bytes": sum(item.size_bytes for item in package.resources),
        "has_readback_resource": any(item.usage == "readback" for item in package.resources),
    }


def _toolchain_provenance(
    config: Mapping[str, Any], repo_root: Path | None = None
) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT)
    manifest_path = root / "benchmarks" / "toolchain-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pinned_dxc = str(
        manifest.get("pinned_dxc_version")
        or manifest.get("dxc_version")
        or nwp.PINNED_DXC_VERSION
    )
    return {
        "toolchain_manifest": str(manifest_path.relative_to(root)),
        "toolchain_manifest_sha256": sha256_file(manifest_path),
        "pinned_dxc_version": pinned_dxc,
        "required_dxc_version": nwp.PINNED_DXC_VERSION,
        "windows_min_build": nwp.MIN_WINDOWS_BUILD,
        "toolchain_manifest_identity": str(
            manifest.get("toolchain_id")
            or manifest.get("manifest_id")
            or manifest.get("toolchain_identity")
            or ""
        ),
    }


def export_production(
    output_directory: Path | str,
    *,
    player_template: Path | str,
    dxc_executable: str | None,
    report_path: Path | str | None = None,
    config: Mapping[str, Any] | None = None,
    graph_path: Path | str | None = None,
    repo_root: Path | None = None,
    allow_test_payloads: bool = False,
    optimization: str = "O2",
    allocation_query: Mapping[int, Any] | None = None,
    adapter_identity: str = "unavailable-native-adapter",
    specialized_build_root: Path | str | None = None,
    validate_schema: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT)
    record = (
        validate_canonical_config_record(dict(config))
        if config is not None
        else load_canonical_config()
    )
    graph_evidence = verify_canonical_graph(
        record, graph_path=Path(graph_path) if graph_path else None, repo_root=root
    )
    inputs = verify_pinned_inputs(record, repo_root=root)
    if not inputs["matches_frozen_digest"]:
        raise Nw07ExportError(
            "pinned build inputs do not match the frozen build-input digest",
            details={
                "frozen": inputs["frozen_digest"],
                "observed": inputs["digest"],
            },
        )
    template = Path(player_template)
    if not template.is_file():
        raise Nw07ExportError(f"player template is unavailable: {template}")
    template_bytes = template.read_bytes()
    output = Path(output_directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".nw07-export-", dir=str(output.parent))
    )
    export_target = staging_root / "artifact"
    settings = nw05_export.ExportSettings(
        byte_ceiling=INTERNAL_NW05_BYTE_CEILING,
        memory_budget=contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
        target_profile=contract.PROFILE_ID,
        requirements_digest=contract.REQUIREMENTS_DIGEST,
        frames_in_flight=2,
        player_template=str(template),
        specialized_build_root=str(
            Path(specialized_build_root)
            if specialized_build_root
            else staging_root / "specialized-native-build"
        ),
        present_sync_interval=0,
        include_optional_artifacts=False,
        allow_optional_profiles=False,
    )
    try:
        result = nw05_export.export_graph(
            Path(graph_path) if graph_path else (root / str(record["graph"])),
            export_target,
            settings=settings,
            report_path=None,
            dxc_executable=dxc_executable,
            optimization=optimization,
            allow_test_payloads=allow_test_payloads,
            allocation_query=allocation_query,
            adapter_identity=adapter_identity,
        )
    except Exception as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        failure = {
            "format": EXPORT_REPORT_FORMAT,
            "version": EXPORT_REPORT_VERSION,
            "task": contract.TASK,
            "implementation_base": contract.IMPLEMENTATION_BASE,
            "success": False,
            "release_grade": not allow_test_payloads,
            "test_payload": bool(allow_test_payloads),
            "error": str(exc),
            "build_inputs": {
                "digest": inputs["digest"],
                "frozen_digest": inputs["frozen_digest"],
                "files": inputs["inputs"],
            },
            "graph_evidence": graph_evidence,
        }
        if report_path is not None:
            path = Path(report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(failure, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        raise Nw07ExportError(f"NW-07 production export failed: {exc}", details=failure) from exc

    identity = inspect_artifact_directory(export_target, config=record)
    # A real (non-test) executable must expose an inspectable PE import closure
    # that agrees with the generated manifest.
    pe_closure: dict[str, Any] | None = None
    if not allow_test_payloads:
        try:
            imports = dependency_check.parse_pe64_sections_and_imports(
                (export_target / f"{nw05_export._profile_file_stem(contract.PROFILE_ID)}.exe").read_bytes()
            ).get("imports", [])
        except Exception as exc:
            raise Nw07ExportError(
                f"specialized executable PE inspection failed: {exc}"
            ) from exc
        names = sorted({str(name).lower().removesuffix(".dll") for name in imports if name})
        if not names:
            raise Nw07ExportError("specialized executable exposes no import closure")
        actual = set(names)
        declared = set(identity["dependency_closure"]["dependencies"])
        if actual != declared:
            raise Nw07ExportError(
                "specialized executable imports do not match the generated manifest",
                details={"actual": names, "declared": sorted(declared)},
            )
        forbidden = sorted(
            name
            for name in names
            if any(token in name for token in FORBIDDEN_RUNTIME_DEPENDENCIES)
        )
        if forbidden:
            raise Nw07ExportError(
                "specialized executable imports a forbidden runtime dependency",
                details={"forbidden": forbidden},
            )
        pe_closure = {"imports": names, "matches_manifest": True}

    size = identity["size_accounting"]
    if not size["within_ceiling"]:
        raise Nw07ExportError(
            "complete distribution exceeds the 262,144-byte ceiling",
            details={"complete_distribution_bytes": size["complete_distribution_bytes"]},
        )
    if identity["has_readback_resource"]:
        raise Nw07ExportError("production package contains a CPU-observable readback resource")
    package = nwp.decode_package(
        (export_target / f"{nw05_export._profile_file_stem(contract.PROFILE_ID)}.nwp").read_bytes(),
        allow_dev=False,
    )
    expected_ops = {
        "init.dispatch": 5,
        "frame.execute_indirect": 1,
        "frame.dispatch": 2,
    }
    for opcode, expected in expected_ops.items():
        observed = (
            identity["init_opcodes"].count(opcode)
            if opcode.startswith("init.")
            else identity["frame_opcodes"].count(opcode)
        )
        if observed != expected:
            raise Nw07ExportError(
                f"production stream expected {expected} '{opcode}' instructions, observed {observed}"
            )
    if identity["frame_opcodes"][-1:] != ["frame.present"]:
        raise Nw07ExportError("production frame stream does not terminate in frame.present")
    if any(code == "init.upload_resource" for code in identity["init_opcodes"]):
        raise Nw07ExportError("production content is uploaded from the CPU instead of generated")

    # Publish only after every hard ceiling and closure check has passed.  A
    # failure above leaves the previous complete artifact directory untouched.
    nw05_export._publish_directory(export_target, output)
    shutil.rmtree(staging_root, ignore_errors=True)

    report: dict[str, Any] = {
        "format": EXPORT_REPORT_FORMAT,
        "version": EXPORT_REPORT_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "success": True,
        "release_grade": not allow_test_payloads,
        "test_payload": bool(allow_test_payloads),
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "graph": graph_evidence,
        "build_inputs": {
            "files": inputs["inputs"],
            "digest": inputs["digest"],
            "frozen_digest": inputs["frozen_digest"],
            "matches_frozen_digest": inputs["matches_frozen_digest"],
            "optimization": optimization,
            "player_template_sha256": sha256_bytes(template_bytes),
            "player_template_bytes": len(template_bytes),
            "build_configuration": {
                "frozen": {
                    key: value
                    for key, value in dict(
                        record.get("build_configuration") or {}
                    ).items()
                },
                "present_sync_interval": settings.present_sync_interval,
                "presentation_uncapped": settings.present_sync_interval == 0,
                "specialized_build_root": settings.specialized_build_root,
                "clean_build_directory": bool(settings.specialized_build_root),
                "pinned_toolchain_enforced": True,
            },
            "toolchain": _toolchain_provenance(record, root),
        },
        "artifact": identity,
        "distribution": {
            "single_executable": True,
            "runtime_files_required": 1,
            "complete_distribution_bytes": size["complete_distribution_bytes"],
            "ceiling": contract.COMPLETE_DISTRIBUTION_CEILING_BYTES,
            "within_ceiling": True,
            "stretch_ceiling": contract.STRETCH_DISTRIBUTION_CEILING_BYTES,
            "within_stretch": size["within_stretch"],
            "stretch_mandatory_gate": False,
            "stretch_status": (
                "measured_within_stretch"
                if size["within_stretch"]
                else "measured_above_stretch"
            ),
            "auxiliary_evidence_bytes_excluded": size["auxiliary_evidence_bytes"],
            "embedded_package": True,
            "embedded_precompiled_shaders": True,
            "runtime_dxc_required": False,
            "editor_required": False,
            "developer_runtime_required": False,
            "system_dependency_bytes": 0,
            "all_required_non_system_bytes_accounted": size["all_required_bytes_accounted"],
        },
        "startup": {
            "protocol": contract.STARTUP_PROTOCOL,
            "process_start_to_first_present_ms_ceiling": contract.PROCESS_START_TO_FIRST_PRESENT_MS,
            "status": "pending_measurement",
        },
        "memory": {
            "peak_engine_committed_gpu_bytes_ceiling": contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
            "logical_resident_payload_bytes": identity["resource_bytes"],
            "logical_peak_physical_bytes": result.artifacts[
                nwp.COMMON_PROFILE_ID
            ].physical_realization.peak_bytes,
            "within_export_memory_budget": (
                result.artifacts[nwp.COMMON_PROFILE_ID].physical_realization.peak_bytes
                <= contract.PEAK_ENGINE_COMMITTED_GPU_BYTES
            ),
            "status": "pending_measurement",
        },
        "diagnostic_sibling_build": {
            "release_build_profile": contract.RELEASE_BUILD_PROFILE,
            "diagnostic_build_profile": contract.DIAGNOSTIC_BUILD_PROFILE,
            "separate_artifact_required": True,
            "release_contains_capture_controls": False,
            "overhead_evidence_required": True,
        },
        "pe_dependency_closure": pe_closure,
        "policy": (
            "One self-contained Windows x64 executable produced exclusively from the "
            "frozen graph and pinned build inputs through the existing compiler, "
            "package, export, and specialized player path.  The 128 KiB stretch "
            "value is reported but never gates success."
        ),
    }
    if report_path is not None:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if validate_schema:
            validate_file_against_schema(path, EXPORT_REPORT_SCHEMA)
    return report


def export_reproducibility(
    work_root: Path | str,
    *,
    player_template: Path | str,
    dxc_executable: str | None,
    allow_test_payloads: bool = False,
    config: Mapping[str, Any] | None = None,
    graph_path: Path | str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Export twice from clean directories and compare every identity byte."""
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    first_dir = root / "clean-a"
    second_dir = root / "clean-b"
    first = export_production(
        first_dir,
        player_template=player_template,
        dxc_executable=dxc_executable,
        report_path=root / "clean-a-report.json",
        config=config,
        graph_path=graph_path,
        repo_root=repo_root,
        allow_test_payloads=allow_test_payloads,
        specialized_build_root=root / "clean-a-build",
    )
    second = export_production(
        second_dir,
        player_template=player_template,
        dxc_executable=dxc_executable,
        report_path=root / "clean-b-report.json",
        config=config,
        graph_path=graph_path,
        repo_root=repo_root,
        allow_test_payloads=allow_test_payloads,
        specialized_build_root=root / "clean-b-build",
    )
    fields = (
        "artifact_key",
        "package_sha256",
        "manifest_sha256",
        "executable_sha256",
        "link_manifest_sha256",
    )
    differences = {
        field: {
            "first": first["artifact"].get(field),
            "second": second["artifact"].get(field),
        }
        for field in fields
        if first["artifact"].get(field) != second["artifact"].get(field)
    }
    if differences:
        raise Nw07ExportError(
            "repeated exports from clean build directories are not reproducible",
            details={"differences": differences},
        )
    fields_map = {field: first["artifact"].get(field) for field in fields}
    reproducibility_payload = {
        "format": "nw07-reproducibility-report",
        "version": 1,
        "matching": True,
        "fields": fields_map,
        "build_inputs_digest": first["build_inputs"]["digest"],
    }
    for report_name in ("clean-a-report.json", "clean-b-report.json"):
        report_path = root / report_name
        try:
            report_record = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(report_record, Mapping):
            report_record["reproducibility"] = reproducibility_payload
            report_path.write_text(
                json.dumps(report_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    return {
        "format": "nw07-reproducibility-report",
        "version": 1,
        "success": True,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "clean_build_directories": [str(first_dir), str(second_dir)],
        "specialized_build_directories": [
            str(root / "clean-a-build"),
            str(root / "clean-b-build"),
        ],
        "matching": True,
        "fields": fields_map,
        "build_inputs_digest": first["build_inputs"]["digest"],
        "artifact": first["artifact"],
        "distribution": first["distribution"],
        "build_inputs": first["build_inputs"],
        "reproducibility": reproducibility_payload,
        "release_grade": not allow_test_payloads,
        "test_payload": bool(allow_test_payloads),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--player-template", required=True)
    parser.add_argument("--dxc", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--graph", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--allow-test-payloads", action="store_true")
    parser.add_argument("--reproducibility-root", default=None)
    args = parser.parse_args(argv)
    config = load_canonical_config(Path(args.config) if args.config else None)
    if args.reproducibility_root:
        report = export_reproducibility(
            args.reproducibility_root,
            player_template=args.player_template,
            dxc_executable=args.dxc,
            allow_test_payloads=args.allow_test_payloads,
            config=config,
            graph_path=args.graph,
        )
        if args.report:
            path = Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    else:
        report = export_production(
            args.output,
            player_template=args.player_template,
            dxc_executable=args.dxc,
            report_path=args.report,
            config=config,
            graph_path=args.graph,
            allow_test_payloads=args.allow_test_payloads,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
