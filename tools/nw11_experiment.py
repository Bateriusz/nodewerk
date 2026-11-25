#!/usr/bin/env python3
"""Frozen NW-11 experiment definitions and matched-run decision math.

An experiment freezes its workload, quality/reference settings, image
criterion, artifact budget, warm-up policy, sample count, comparison statistic,
and success criterion *before* any measurement.  This module validates those
definitions against the NW-07 production contract and evaluates matched
baseline/variant runs independently per device/environment.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from nw11_contract import (  # type: ignore[import-not-found]
    BASE_GRAPH,
    BASE_GRAPH_SHA256,
    COMMON_PROFILE_ID,
    COMPLETE_DISTRIBUTION_CEILING_BYTES,
    CPU_SUBMIT_MS_MAX,
    DEVICE_TIERS,
    EXPERIMENT_MESH_ID,
    EXPERIMENT_PATH_BY_ID,
    EXPERIMENT_RAY_ID,
    FRESH_PROCESS_PER_RUN,
    GPU_TIME_REDUCTION_PERCENT_MIN,
    IMAGE_ABSOLUTE_TOLERANCE,
    IMAGE_RMSE_TOLERANCE,
    IMPLEMENTATION_BASE,
    LOOP_TICKS,
    MESH_PROFILE_ID,
    MIN_RAW_SAMPLES_PER_RUN,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    PEAK_ENGINE_COMMITTED_GPU_BYTES,
    PERCENTILE,
    PERCENTILE_METHOD,
    PERF_CONTRACT_ID,
    PERF_RUN_COUNT,
    PERF_RUN_PROTOCOL,
    PERF_RUN_SECONDS,
    PROCESS_START_TO_FIRST_PRESENT_MS,
    QUALITY_PROFILE_ID,
    RAY_QUERY_PROFILE_ID,
    REFERENCE_FIXED_TICKS,
    REFERENCE_LOOP_BOUNDARY_TICKS,
    SHADER_COUNT_INCREMENT_MAX,
    TASK,
    TICKS_PER_SECOND,
    TORUS_INSTANCE_COUNT,
    WARMUP_SECONDS,
    canonical_text_sha256,
    frozen_artifact_budget,
    frozen_protocol,
    frozen_workload,
    profile_definition,
)

EXPERIMENT_FORMAT = "nw11-experiment"
EXPERIMENT_VERSION = 1

EXPERIMENT_KINDS = {
    EXPERIMENT_MESH_ID: "mesh_shading",
    EXPERIMENT_RAY_ID: "ray_query_reflection",
}

BASELINE_PIPELINES = {
    EXPERIMENT_MESH_ID: "indexed_instanced_raster",
    EXPERIMENT_RAY_ID: "analytic_reflection_fragment",
}

VARIANT_PIPELINES = {
    EXPERIMENT_MESH_ID: "mesh_shader_tier_1",
    EXPERIMENT_RAY_ID: "dxr_inline_ray_query",
}


class ExperimentError(ValueError):
    """Raised when an experiment definition or measurement is invalid."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentError(f"{label} must be a JSON object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{label} must be a non-empty string")
    return value


def _number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ExperimentError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ExperimentError(f"{label} must be finite")
    return result


def load_experiment(path: Path | str) -> dict[str, Any]:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_experiment(record)


def locate_experiment(experiment_id: str, *, repo_root: Path | str | None = None) -> Path:
    try:
        relative = EXPERIMENT_PATH_BY_ID[experiment_id]
    except KeyError as exc:
        raise ExperimentError(f"unknown NW-11 experiment '{experiment_id}'") from exc
    return Path(repo_root or ".") / relative


def _validate_graph_identity(
    document: Mapping[str, Any], *, repo_root: Path | None
) -> None:
    graph = _mapping(document.get("base_graph"), "base_graph")
    path = _string(graph.get("path"), "base_graph.path")
    digest = _string(graph.get("sha256"), "base_graph.sha256")
    if path != BASE_GRAPH:
        raise ExperimentError(
            "experiments must consume the frozen NW-07 production graph path"
        )
    if digest != BASE_GRAPH_SHA256:
        raise ExperimentError("experiment changed the frozen NW-07 graph digest")
    if repo_root is not None:
        actual = repo_root / path
        if not actual.is_file():
            raise ExperimentError(f"frozen base graph is unavailable: {path}")
        observed = canonical_text_sha256(actual)
        if observed != digest:
            raise ExperimentError(
                "frozen base graph bytes do not match the NW-07 graph identity"
            )


def _validate_workload(document: Mapping[str, Any]) -> None:
    workload = _mapping(document.get("workload"), "workload")
    expected = frozen_workload()
    for key in (
        "graph",
        "graph_sha256",
        "width",
        "height",
        "torus_instances",
        "loop_ticks",
        "ticks_per_second",
        "loop_seconds",
        "frame_rate_hz",
        "quality_profile_id",
    ):
        if workload.get(key) != expected[key]:
            raise ExperimentError(
                f"experiment changed the frozen workload field '{key}'"
            )


def _validate_protocol(document: Mapping[str, Any]) -> dict[str, Any]:
    protocol = _mapping(document.get("protocol"), "protocol")
    expected = frozen_protocol()
    for key in (
        "perf_contract",
        "perf_protocol",
        "warmup_seconds",
        "runs",
        "run_seconds",
        "min_raw_samples_per_run",
        "fresh_process_per_run",
        "application_pipeline_cache",
        "percentile",
        "percentile_method",
    ):
        if protocol.get(key) != expected[key]:
            raise ExperimentError(
                f"experiment changed the frozen protocol field '{key}'"
            )
    if protocol.get("matched_environment_required") is not True:
        raise ExperimentError("experiments require matched baseline/variant environments")
    if protocol.get("independent_per_device") is not True:
        raise ExperimentError("experiments must evaluate decisions independently per device")
    return dict(protocol)


def _validate_criteria(document: Mapping[str, Any], kind: str) -> None:
    criteria = _mapping(document.get("criteria"), "criteria")
    primary = _mapping(criteria.get("primary"), "criteria.primary")
    if primary.get("metric") != "p99_gpu_time_ms":
        raise ExperimentError("the primary criterion must be p99 GPU time")
    reduction = _number(
        primary.get("relative_reduction_percent_min"), "primary.relative_reduction_percent_min"
    )
    if reduction < GPU_TIME_REDUCTION_PERCENT_MIN:
        raise ExperimentError(
            "the primary GPU-time reduction criterion may not be weaker than 10 percent"
        )
    if primary.get("evaluation") != "independent_per_device_environment":
        raise ExperimentError(
            "the primary criterion must be evaluated independently per device/environment"
        )
    if primary.get("statistic") != "nearest-rank p99 per 60-second process; require every matched run pair to improve":
        raise ExperimentError("experiments must use the frozen per-process comparison statistic")
    guardrails = _mapping(criteria.get("guardrails"), "criteria.guardrails")
    for key in (
        "end_to_end_frame_time_must_not_regress",
        "microbenchmark_gain_alone_is_sufficient",
        "cpu_submit_ms_max",
        "complete_distribution_bytes_ceiling",
        "incremental_distribution_bytes_ceiling",
        "shader_count_increment_ceiling",
        "cold_initialization_ms_ceiling",
        "peak_engine_committed_gpu_bytes_ceiling",
    ):
        if key not in guardrails:
            raise ExperimentError(f"criteria.guardrails.{key} is required")
    if guardrails.get("end_to_end_frame_time_must_not_regress") is not True:
        raise ExperimentError("microbenchmark-only gains cannot justify adoption")
    if guardrails.get("microbenchmark_gain_alone_is_sufficient") is not False:
        raise ExperimentError("microbenchmark gain alone must never be sufficient")
    if _number(
        guardrails.get("complete_distribution_bytes_ceiling"),
        "guardrails.complete_distribution_bytes_ceiling",
    ) > COMPLETE_DISTRIBUTION_CEILING_BYTES:
        raise ExperimentError("experiment weakened the complete distribution ceiling")
    if _number(guardrails.get("cold_initialization_ms_ceiling"), "cold_initialization_ms_ceiling") > PROCESS_START_TO_FIRST_PRESENT_MS:
        raise ExperimentError("experiment weakened the cold-initialization ceiling")
    if _number(guardrails.get("peak_engine_committed_gpu_bytes_ceiling"), "peak_engine_committed_gpu_bytes_ceiling") > PEAK_ENGINE_COMMITTED_GPU_BYTES:
        raise ExperimentError("experiment weakened the VRAM ceiling")
    if _number(guardrails.get("cpu_submit_ms_max"), "guardrails.cpu_submit_ms_max") > CPU_SUBMIT_MS_MAX:
        raise ExperimentError("experiment weakened the preserved CPU-submit gate")

    budget = _mapping(document.get("artifact_budget"), "artifact_budget")
    if budget.get("distribution_size_policy") != "gated":
        raise ExperimentError("distribution size must be a gated acceptance criterion")
    for key in (
        "complete_distribution_bytes_ceiling",
        "incremental_distribution_bytes_ceiling",
        "shader_count_increment_ceiling",
        "peak_engine_committed_gpu_bytes_ceiling",
        "process_start_to_first_present_ms_ceiling",
    ):
        if key not in budget:
            raise ExperimentError(f"artifact_budget.{key} is required")
    for key, frozen in (
        ("complete_distribution_bytes_ceiling", COMPLETE_DISTRIBUTION_CEILING_BYTES),
        ("peak_engine_committed_gpu_bytes_ceiling", PEAK_ENGINE_COMMITTED_GPU_BYTES),
        ("process_start_to_first_present_ms_ceiling", PROCESS_START_TO_FIRST_PRESENT_MS),
    ):
        if _number(budget.get(key), f"artifact_budget.{key}") > frozen:
            raise ExperimentError(f"experiment weakened artifact budget '{key}'")
    if _number(
        guardrails.get("complete_distribution_bytes_ceiling"),
        "guardrails.complete_distribution_bytes_ceiling",
    ) > _number(
        budget.get("complete_distribution_bytes_ceiling"),
        "artifact_budget.complete_distribution_bytes_ceiling",
    ):
        raise ExperimentError(
            "experiment guardrail complete byte budget is weaker than its artifact budget"
        )
    profile_id = str(document.get("variant_profile_id"))
    if profile_id in (MESH_PROFILE_ID, RAY_QUERY_PROFILE_ID):
        profile_budget = int(profile_definition(profile_id)["incremental_budget_bytes"])
        if _number(
            budget.get("incremental_distribution_bytes_ceiling"),
            "artifact_budget.incremental_distribution_bytes_ceiling",
        ) > profile_budget:
            raise ExperimentError(
                "experiment incremental byte budget exceeds the frozen profile budget"
            )
        if _number(
            guardrails.get("incremental_distribution_bytes_ceiling"),
            "guardrails.incremental_distribution_bytes_ceiling",
        ) > profile_budget:
            raise ExperimentError(
                "experiment guardrail incremental byte budget exceeds the frozen profile budget"
            )
        if _number(
            guardrails.get("incremental_distribution_bytes_ceiling"),
            "guardrails.incremental_distribution_bytes_ceiling",
        ) > _number(
            budget.get("incremental_distribution_bytes_ceiling"),
            "artifact_budget.incremental_distribution_bytes_ceiling",
        ):
            raise ExperimentError(
                "experiment guardrail incremental byte budget is weaker than its artifact budget"
            )
        if int(
            guardrails.get("shader_count_increment_ceiling", -1)
        ) > SHADER_COUNT_INCREMENT_MAX:
            raise ExperimentError(
                "experiment weakened the shader-count increment guardrail"
            )
        if int(guardrails.get("shader_count_increment_ceiling", -1)) > int(
            budget.get("shader_count_increment_ceiling", -1)
        ):
            raise ExperimentError(
                "experiment guardrail shader-count budget is weaker than its artifact budget"
            )

    image = _mapping(criteria.get("image"), "criteria.image")
    if image.get("criterion") not in {"max_abs_channel_error_and_rmse", "perceptual_reference"}:
        raise ExperimentError("experiment must declare a measurable image criterion")
    if image.get("criterion") == "max_abs_channel_error_and_rmse":
        if _number(image.get("max_abs_channel_error"), "image.max_abs_channel_error") > IMAGE_ABSOLUTE_TOLERANCE:
            raise ExperimentError("experiment weakened the fixed-quality image criterion")
        if _number(image.get("rmse"), "image.rmse") > IMAGE_RMSE_TOLERANCE:
            raise ExperimentError("experiment weakened the fixed-quality RMSE criterion")
    if image.get("fixed_quality") is not True:
        raise ExperimentError("optional experiments must compare fixed-quality images")
    image_ticks = image.get("fixed_ticks")
    reference = _mapping(document.get("reference"), "reference")
    reference_ticks = reference.get("fixed_ticks")
    if list(image_ticks or []) != list(REFERENCE_FIXED_TICKS):
        raise ExperimentError(
            "experiment must compare images at the frozen NW-07 fixed ticks"
        )
    if list(reference_ticks or []) != list(REFERENCE_FIXED_TICKS):
        raise ExperimentError("experiment reference changed the frozen NW-07 fixed ticks")
    if list(reference.get("loop_boundary_ticks") or []) != list(
        REFERENCE_LOOP_BOUNDARY_TICKS
    ):
        raise ExperimentError(
            "experiment reference changed the frozen NW-07 loop boundary ticks"
        )
    if kind == "ray_query_reflection" and image.get("reference_kind") not in {
        "analytic_reference",
        "sdf_reference",
    }:
        raise ExperimentError(
            "the reflection experiment requires an analytic or SDF image reference"
        )


def _validate_derivation(document: Mapping[str, Any], kind: str) -> None:
    derivation = _mapping(document.get("graph_derivation"), "graph_derivation")
    if derivation.get("base_graph_unchanged") is not True:
        raise ExperimentError("experiments must extend, not replace, the base graph")
    if derivation.get("hand_authored_renderer") is not False:
        raise ExperimentError("experiments may not add a hand-authored renderer")
    if derivation.get("second_production_backend") is not False:
        raise ExperimentError("experiments may not add a second production backend")
    added = derivation.get("new_operators") or []
    if not isinstance(added, list):
        raise ExperimentError("graph_derivation.new_operators must be an array")
    if added:
        raise ExperimentError(
            "the initial NW-11 experiments must not add graph operators"
        )
    expected_mode = (
        "pipeline_lowering_overlay"
        if kind == "mesh_shading"
        else "fragment_shader_specialization_overlay"
    )
    if derivation.get("mode") != expected_mode:
        raise ExperimentError(f"experiment derivation mode must be '{expected_mode}'")


def _validate_pipeline(
    document: Mapping[str, Any], kind: str, profile_id: str
) -> None:
    pipeline = _mapping(document.get("pipeline"), "pipeline")
    if pipeline.get("baseline") != BASELINE_PIPELINES[document["experiment_id"]]:
        raise ExperimentError("experiment changed the frozen baseline pipeline")
    if pipeline.get("variant") != VARIANT_PIPELINES[document["experiment_id"]]:
        raise ExperimentError("experiment changed the frozen variant pipeline")
    if pipeline.get("fallback_profile_id") != COMMON_PROFILE_ID:
        raise ExperimentError("the fallback must be the common profile")
    if pipeline.get("fallback_artifact_id") != document.get("fallback_artifact_id"):
        raise ExperimentError("pipeline fallback identity disagrees with the experiment")
    variant_shaders = pipeline.get("variant_shaders")
    if not isinstance(variant_shaders, list) or not variant_shaders:
        raise ExperimentError("the variant must declare its shaders")
    allowed_targets = set(profile_definition(profile_id)["shader_targets"])
    for shader in variant_shaders:
        entry = _mapping(shader, "variant shader")
        path = _string(entry.get("path"), "variant shader path")
        target = _string(entry.get("target"), "variant shader target")
        if not path.startswith("shaders/nw11/"):
            raise ExperimentError(
                "optional shader implementations must live under shaders/nw11/"
            )
        if target not in allowed_targets:
            raise ExperimentError(
                f"variant shader target '{target}' is not declared by the profile"
            )
    common_targets = set(profile_definition(COMMON_PROFILE_ID)["shader_targets"])
    baseline_shaders = pipeline.get("baseline_shaders")
    if not isinstance(baseline_shaders, list) or not baseline_shaders:
        raise ExperimentError("the baseline must declare its shaders")
    for shader in baseline_shaders:
        entry = _mapping(shader, "baseline shader")
        path = _string(entry.get("path"), "baseline shader path")
        target = _string(entry.get("target"), "baseline shader target")
        if target not in common_targets:
            raise ExperimentError(
                f"baseline shader target '{target}' is not valid for the common profile"
            )
        if not path.startswith(("shaders/nw06/", "shaders/nw11/")):
            raise ExperimentError(
                "baseline shaders must reuse the frozen NW-06 production shaders "
                "or a common-profile shader under shaders/nw11/"
            )
    for key in ("shared_fragment_shader", "shared_vertex_shader"):
        shared = pipeline.get(key)
        if shared is None:
            continue
        shared_path = _string(shared, key)
        if not shared_path.startswith(("shaders/nw06/", "shaders/nw11/")):
            raise ExperimentError(f"{key} must reference a frozen NW-06/common shader")


def validate_experiment(
    record: Mapping[str, Any], *, repo_root: Path | str | None = None
) -> dict[str, Any]:
    document = _mapping(record, "experiment")
    if document.get("format") != EXPERIMENT_FORMAT:
        raise ExperimentError("experiment format is not nw11-experiment")
    if document.get("version") != EXPERIMENT_VERSION:
        raise ExperimentError("experiment version is not 1")
    if document.get("task") != TASK:
        raise ExperimentError("experiment task is not NW-11")
    if document.get("implementation_base") != IMPLEMENTATION_BASE:
        raise ExperimentError("experiment is not bound to the NW-11 implementation base")
    experiment_id = _string(document.get("experiment_id"), "experiment_id")
    if experiment_id not in EXPERIMENT_KINDS:
        raise ExperimentError(f"unknown experiment id '{experiment_id}'")
    if document.get("kind") != EXPERIMENT_KINDS[experiment_id]:
        raise ExperimentError("experiment kind does not match its identifier")
    if document.get("status") != "frozen":
        raise ExperimentError(
            "experiments must freeze their definition before measurement"
        )
    if document.get("baseline_profile_id") != COMMON_PROFILE_ID:
        raise ExperimentError("the baseline must use the common profile")
    expected_variant = (
        MESH_PROFILE_ID if experiment_id == EXPERIMENT_MESH_ID else RAY_QUERY_PROFILE_ID
    )
    if document.get("variant_profile_id") != expected_variant:
        raise ExperimentError("experiment changed its optional profile identity")
    if not isinstance(document.get("fallback_artifact_id"), str) or not document.get(
        "fallback_artifact_id"
    ):
        raise ExperimentError("experiment must declare a fallback artifact identity")

    root = Path(repo_root) if repo_root is not None else None
    _validate_graph_identity(document, repo_root=root)
    _validate_workload(document)
    protocol = _validate_protocol(document)
    _validate_criteria(document, document["kind"])
    _validate_derivation(document, document["kind"])
    _validate_pipeline(document, document["kind"], expected_variant)

    budget = _mapping(document.get("artifact_budget"), "artifact_budget")
    frozen_budget = frozen_artifact_budget()
    for key in (
        "shader_count_increment_ceiling",
        "peak_engine_committed_gpu_bytes_ceiling",
        "process_start_to_first_present_ms_ceiling",
    ):
        if key not in budget:
            raise ExperimentError(f"artifact_budget.{key} is required")
    for key in (
        "peak_engine_committed_gpu_bytes_ceiling",
        "process_start_to_first_present_ms_ceiling",
    ):
        if _number(budget.get(key), f"artifact_budget.{key}") > frozen_budget[key]:
            raise ExperimentError(f"experiment weakened artifact budget '{key}'")
    if int(budget.get("shader_count_increment_ceiling", -1)) > SHADER_COUNT_INCREMENT_MAX:
        raise ExperimentError("experiment weakened the shader-count increment budget")

    if document.get("evidence_status") not in {
        "accepted",
        "rejected",
        "failed",
        "pending",
        "unsupported",
    }:
        raise ExperimentError("experiment evidence status is not a known NW-11 status")

    if root is not None:
        for shader in document["pipeline"]["variant_shaders"]:
            target = root / shader["path"]
            if not target.is_file():
                raise ExperimentError(f"variant shader is unavailable: {shader['path']}")
        for shader in document["pipeline"]["baseline_shaders"]:
            target = root / shader["path"]
            if not target.is_file():
                raise ExperimentError(f"baseline shader is unavailable: {shader['path']}")
        for key in ("shared_fragment_shader", "shared_vertex_shader"):
            shared = document["pipeline"].get(key)
            if shared and not (root / shared).is_file():
                raise ExperimentError(f"shared shader is unavailable: {shared}")

    # The protocol block is normalized into the returned record so downstream
    # evidence evaluation cannot silently use a different sample policy.
    result = dict(document)
    result["protocol"] = protocol
    return result


def nearest_rank_percentile(values: Sequence[float], percentile: float = PERCENTILE) -> float:
    if not values:
        raise ExperimentError("cannot compute a percentile over no samples")
    if not (0.0 < percentile <= 100.0):
        raise ExperimentError("percentile must be in (0, 100]")
    ordered = sorted(float(value) for value in values)
    rank = int(math.ceil((percentile / 100.0) * len(ordered))) - 1
    rank = max(0, min(rank, len(ordered) - 1))
    return ordered[rank]


def summarize_samples(values: Iterable[float], percentile: float = PERCENTILE) -> dict[str, Any]:
    samples = [float(value) for value in values]
    if not samples:
        raise ExperimentError("cannot summarize an empty sample set")
    if any(not math.isfinite(sample) or sample < 0.0 for sample in samples):
        raise ExperimentError("samples must be finite and nonnegative")
    return {
        "count": len(samples),
        "min": min(samples),
        "max": max(samples),
        "mean": sum(samples) / len(samples),
        "median": nearest_rank_percentile(samples, 50.0),
        "p99": nearest_rank_percentile(samples, percentile),
        "percentile": percentile,
        "percentile_method": PERCENTILE_METHOD,
    }


def _require_metric(mapping: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        raise ExperimentError(f"{label}.{key} is missing")
    return mapping[key]


def _summarize_run(run: Mapping[str, Any], label: str, protocol: Mapping[str, Any]) -> dict[str, Any]:
    gpu = _require_metric(run, "gpu_time_samples", label)
    cpu = _require_metric(run, "cpu_submit_samples", label)
    frame = _require_metric(run, "frame_time_samples", label)
    for name, samples in (("gpu_time_samples", gpu), ("cpu_submit_samples", cpu), ("frame_time_samples", frame)):
        if not isinstance(samples, list) or len(samples) < int(protocol["min_raw_samples_per_run"]):
            raise ExperimentError(
                f"{label}.{name} must preserve at least "
                f"{protocol['min_raw_samples_per_run']} raw samples per run"
            )
    return {
        "gpu_time": summarize_samples(gpu),
        "cpu_submit": summarize_samples(cpu),
        "frame_time": summarize_samples(frame),
        "raw_gpu_time_samples": [float(value) for value in gpu],
        "raw_cpu_submit_samples": [float(value) for value in cpu],
        "raw_frame_time_samples": [float(value) for value in frame],
        "startup_ms": _number(
            _require_metric(run, "process_start_to_first_present_ms", label),
            f"{label}.process_start_to_first_present_ms",
        ),
        "displayed_presents": int(_require_metric(run, "displayed_presents", label)),
        "dropped_presents": int(_require_metric(run, "dropped_presents", label)),
        "distribution_bytes": int(
            _require_metric(run, "distribution_bytes", label)
        ),
        "shader_count": int(_require_metric(run, "shader_count", label)),
        "peak_engine_committed_gpu_bytes": int(
            _require_metric(run, "peak_engine_committed_gpu_bytes", label)
        ),
    }


def summarize_process_runs(arm: Mapping[str, Any], label: str, protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    processes = arm.get("process_runs")
    if not isinstance(processes, list) or len(processes) != protocol["runs"]:
        raise ExperimentError(f"{label} requires {protocol['runs']} independent process runs")
    return [_summarize_run(_mapping(run, label), f"{label} run {index}", protocol)
            for index, run in enumerate(processes, 1)]


def reference_gate_failures(device_key: str, runs: list[dict[str, Any]]) -> list[str]:
    # NW-07 specifies an absolute Ada reference gate and a separate Blackwell
    # development series. Qualification of the exact NW-07 host is independent.
    if device_key != DEVICE_TIERS["desktop_ada_representative"]["device_key"]:
        return []
    return [f"run {index} p99 {label} must be strictly below {CPU_SUBMIT_MS_MAX} ms"
            for index, run in enumerate(runs, 1)
            for metric, label in (("gpu_time", "GPU frame time"), ("cpu_submit", "CPU submission"))
            if run[metric]["p99"] >= CPU_SUBMIT_MS_MAX]


def evaluate_experiment(
    experiment: Mapping[str, Any],
    measurement: Mapping[str, Any],
    *,
    device_key: str,
    environment_key: str,
) -> dict[str, Any]:
    """Evaluate one matched baseline/variant measurement on one device.

    The requested device/environment identity must match the measurement.
    Results from one GPU can never be attributed to another.
    """
    document = validate_experiment(experiment)
    if not device_key or not environment_key:
        raise ExperimentError("device_key and environment_key are required")
    observed_device = _string(measurement.get("device_key"), "measurement.device_key")
    observed_environment = _string(
        measurement.get("environment_key"), "measurement.environment_key"
    )
    if observed_device != device_key:
        raise ExperimentError(
            "measurement device does not match the requested decision device"
        )
    if observed_environment != environment_key:
        raise ExperimentError(
            "measurement environment does not match the requested decision environment"
        )

    protocol = document["protocol"]
    baseline = _mapping(measurement.get("baseline"), "measurement.baseline")
    variant = _mapping(measurement.get("variant"), "measurement.variant")
    baseline_runs = summarize_process_runs(baseline, "baseline", protocol)
    variant_runs = summarize_process_runs(variant, "variant", protocol)
    reductions = [
        (left["gpu_time"]["p99"] - right["gpu_time"]["p99"]) / left["gpu_time"]["p99"] * 100
        if left["gpu_time"]["p99"] > 0 else 0.0
        for left, right in zip(baseline_runs, variant_runs)
    ]
    reduction = min(reductions)
    worst_pair = reductions.index(reduction)
    baseline_p99 = baseline_runs[worst_pair]["gpu_time"]["p99"]
    variant_p99 = variant_runs[worst_pair]["gpu_time"]["p99"]
    criteria = document["criteria"]
    primary = criteria["primary"]
    guardrails = criteria["guardrails"]
    image = criteria["image"]

    reasons: list[dict[str, Any]] = []

    def reject(code: str, message: str) -> None:
        reasons.append({"code": code, "message": message})

    for label, arm_runs in (("baseline", baseline_runs), ("variant", variant_runs)):
        for failure in reference_gate_failures(device_key, arm_runs):
            reject("NW11-NW07-REFERENCE-GATE", f"{label} {failure}")
    for index, (left, right, improvement) in enumerate(zip(baseline_runs, variant_runs, reductions), 1):
        if improvement < float(primary["relative_reduction_percent_min"]):
            reject("NW11-PRIMARY-NOT-MET",
                   f"run {index} p99 GPU time reduction {improvement:.3f}% is below "
                   f"{primary['relative_reduction_percent_min']:.3f}%")
        if right["frame_time"]["p99"] > left["frame_time"]["p99"]:
            reject("NW11-FRAME-TIME-REGRESSION", f"run {index} displayed-frame p99 regressed")
        if right["displayed_presents"] < left["displayed_presents"]:
            reject("NW11-DISPLAYED-FRAME-COUNT", f"run {index} delivered fewer frames to the display")
        if right["cpu_submit"]["p99"] > float(guardrails["cpu_submit_ms_max"]):
            reject("NW11-CPU-SUBMIT-GATE", f"run {index} p99 CPU submission exceeded the experiment ceiling")
        if right["cpu_submit"]["p99"] > left["cpu_submit"]["p99"]:
            reject("NW11-CPU-REGRESSION", f"run {index} p99 CPU submission regressed")
    if max(baseline["distribution_bytes"], variant["distribution_bytes"]) > int(
        document["artifact_budget"]["complete_distribution_bytes_ceiling"]
    ):
        reject(
            "NW11-DISTRIBUTION-CEILING",
            "baseline or variant complete distribution exceeds the frozen ceiling",
        )
    incremental_bytes = variant["distribution_bytes"] - baseline["distribution_bytes"]
    if incremental_bytes > int(
        document["artifact_budget"]["incremental_distribution_bytes_ceiling"]
    ):
        reject(
            "NW11-INCREMENTAL-BUDGET",
            "variant incremental distribution bytes exceed the frozen experiment budget",
        )
    shader_increment = variant["shader_count"] - baseline["shader_count"]
    if shader_increment > int(document["artifact_budget"]["shader_count_increment_ceiling"]):
        reject(
            "NW11-SHADER-COUNT-BUDGET",
            "variant shader-count increment exceeds the frozen experiment budget",
        )
    missing_cold = False
    for label, arm, arm_runs in (("baseline", baseline, baseline_runs), ("variant", variant, variant_runs)):
        cold = arm.get("cold_initialization_ms")
        if cold is None:
            missing_cold = True
        else:
            if _number(cold, f"{label}.cold_initialization_ms") > float(guardrails["cold_initialization_ms_ceiling"]):
                reject("NW11-COLD-INITIALIZATION", f"{label} cold initialization exceeds the ceiling")
        if any(run["startup_ms"] > float(guardrails["cold_initialization_ms_ceiling"]) for run in arm_runs):
            reject("NW11-WARM-INITIALIZATION", f"{label} warm initialization exceeds the ceiling")
        if arm["peak_engine_committed_gpu_bytes"] > int(guardrails["peak_engine_committed_gpu_bytes_ceiling"]):
            reject("NW11-VRAM-CEILING", f"{label} peak engine-committed GPU bytes exceed the ceiling")
    if variant["image"]["max_abs_channel_error"] > float(image["max_abs_channel_error"]):
        reject(
            "NW11-IMAGE-MAX-ERROR",
            "variant fixed-quality maximum absolute channel error exceeds the criterion",
        )
    if variant["image"]["rmse"] > float(image["rmse"]):
        reject("NW11-IMAGE-RMSE", "variant fixed-quality RMSE exceeds the criterion")
    expected_ticks = list(image["fixed_ticks"])
    if list(variant["image"]["fixed_ticks"]) != expected_ticks:
        reject(
            "NW11-IMAGE-TICKS",
            "variant image results do not record the frozen fixed-tick coverage",
        )
    if list(baseline["image"]["fixed_ticks"]) != expected_ticks:
        reject(
            "NW11-IMAGE-TICKS",
            "baseline image results do not record the frozen fixed-tick coverage",
        )
    if variant["image"]["reference_kind"] != image["reference_kind"]:
        reject(
            "NW11-IMAGE-REFERENCE",
            "variant image result does not use the frozen experiment reference",
        )

    if missing_cold:
        reject("NW11-COLD-PENDING", "driver-cache preparation did not establish cold startup coverage")
    status = "pending" if missing_cold else ("rejected" if reasons else "accepted")
    return {
        "experiment_id": document["experiment_id"],
        "variant_profile_id": document["variant_profile_id"],
        "baseline_profile_id": document["baseline_profile_id"],
        "fallback_artifact_id": document["fallback_artifact_id"],
        "device_key": device_key,
        "environment_key": environment_key,
        "status": status,
        "satisfies_completion": status in {"accepted", "rejected"},
        "reasons": reasons,
        "primary": {
            "metric": "p99_gpu_time_ms",
            "baseline": baseline_p99,
            "variant": variant_p99,
            "relative_reduction_percent": reduction,
            "relative_reduction_percent_per_run": reductions,
            "required_reduction_percent": float(
                primary["relative_reduction_percent_min"]
            ),
            "evaluation": primary["evaluation"],
        },
        "aggregate": {
            "baseline": {**baseline, "process_runs": baseline_runs},
            "variant": {**variant, "process_runs": variant_runs},
            "incremental_distribution_bytes": incremental_bytes,
            "incremental_shader_count": shader_increment,
            "baseline_distribution_bytes": baseline["distribution_bytes"],
            "variant_distribution_bytes": variant["distribution_bytes"],
            "complete_distribution_bytes_ceiling": int(
                document["artifact_budget"]["complete_distribution_bytes_ceiling"]
            ),
            "incremental_distribution_bytes_ceiling": int(
                document["artifact_budget"]["incremental_distribution_bytes_ceiling"]
            ),
        },
        "policy": {
            "microbenchmark_gain_alone_is_sufficient": False,
            "pending_is_not_pass": True,
            "unsupported_is_not_pass": True,
            "per_device_decision_isolation": True,
        },
    }


__all__ = [
    "EXPERIMENT_FORMAT",
    "EXPERIMENT_VERSION",
    "EXPERIMENT_KINDS",
    "ExperimentError",
    "load_experiment",
    "locate_experiment",
    "validate_experiment",
    "nearest_rank_percentile",
    "summarize_samples",
    "evaluate_experiment",
]
