#!/usr/bin/env python3
"""Frozen NW-11 advanced-GPU experiment contract.

NW-11 evaluates optional GPU features only through bounded, costed
experiments layered on the existing NW-07 graph-derived production/export
path.  This module owns the identities, thresholds, protocol names, and
budget values that every NW-11 tool imports.  Values are never redeclared in
individual verifiers so an experiment cannot silently weaken a gate.

The module is deliberately stdlib-only: the same bytes are copied into a
validation bundle and must run on a clean machine without a repository
checkout or any Python package installation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import hardware_evidence_devices as hardware_devices

TASK = "NW-11"

# Immutable implementation base for this task.  The older planning base is
# retained as provenance only and is never used to reconstruct the checkout.
IMPLEMENTATION_BASE = "dc8edbafc3f5928861f14b0fbb932f7d42e81c8b"
PLANNING_BASE_PROVENANCE_ONLY = "270d298f1b037548e0ab4808b2c123c12ddcc4e6"

# Common identity inherited unchanged from NW-01/NW-03..NW-07.
COMMON_PROFILE_ID = "nw.common.d3d12.fl12_0.sm6_0.v1"
COMMON_REQUIREMENTS_DIGEST = (
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a"
)

# Optional profile identities.  Each optional profile is separately exported
# and separately tested; neither may become a common-profile requirement.
MESH_PROFILE_ID = "nw.optional.d3d12.mesh_shaders.ms6_5.v1"
RAY_QUERY_PROFILE_ID = "nw.optional.d3d12.ray_query.ps6_5.v1"

# Separately exported, separately tested common-profile fallback artifacts.
# The mesh experiment falls back to the indexed raster artifact; the ray
# experiment falls back to the analytic-reflection common artifact.  They are
# distinct executables and therefore carry distinct artifact identities.
MESH_FALLBACK_ARTIFACT_ID = "nw11.fallback.mesh.indexed.d3d12.fl12_0.sm6_0.v1"
RAY_FALLBACK_ARTIFACT_ID = "nw11.fallback.ray.analytic.d3d12.fl12_0.sm6_0.v1"

EXPERIMENT_MESH_ID = "nw11.mesh-shader-vs-indexed.v1"
EXPERIMENT_RAY_ID = "nw11.ray-query-vs-analytic-reflection.v1"

# The mesh experiment extends the frozen NW-07 workload.  The ray-query
# experiment reuses the exact same graph and geometry path, replacing only
# the fragment shading specialization.
BASE_GRAPH = "fixtures/nw07/production_loop.nwgraph"
BASE_GRAPH_SHA256 = (
    "093b8429d4e592e643dc2eb307ea751a0c78d6544961beb580ea56a9361b7091"
)
CANONICAL_PRODUCTION_CONFIG = "benchmarks/nw07/canonical-production.v1.json"

OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
TORUS_INSTANCE_COUNT = 256
LOOP_TICKS = 960_000
TICKS_PER_SECOND = 48_000
LOOP_SECONDS = 20
FRAME_RATE_HZ = 60

# Frozen fixed-tick coverage for the NW-07 correctness captures.  Both
# experiments must compare images at exactly these ticks against the matching
# NW-07 reference, so a runtime-loaded experiment cannot choose easier ticks.
REFERENCE_FIXED_TICKS = (0, 240_000, 480_000, 720_000, 959_999)
REFERENCE_LOOP_BOUNDARY_TICKS = (0, 960_000)

# Frozen NW-07 evidence contracts.  NW-11 reuses these instead of defining a
# parallel qualification system.
PERF_CONTRACT_ID = "nw07_perf_ref_v1"
PERF_RUN_PROTOCOL = "nw07-perf-run.v1"
STARTUP_PROTOCOL = "nw07-startup.v1"
CORRECTNESS_CAPTURE_PROTOCOL = "nw07-correctness-capture.v1"
QUALITY_PROFILE_ID = "nw07.quality.v1"

WARMUP_SECONDS = 5
PERF_RUN_COUNT = 3
PERF_RUN_SECONDS = 60
MIN_RAW_SAMPLES_PER_RUN = 600
FRESH_PROCESS_PER_RUN = True
APPLICATION_PIPELINE_CACHE = False
PERCENTILE = 99.0
PERCENTILE_METHOD = "nearest-rank"

# Default performance criterion: at least 10 percent lower GPU time at
# matched quality, evaluated independently per qualified device/environment.
GPU_TIME_REDUCTION_PERCENT_MIN = 10.0
CPU_SUBMIT_MS_MAX = 16.67

IMAGE_ABSOLUTE_TOLERANCE = 1e-4
IMAGE_RELATIVE_TOLERANCE = 1e-4
IMAGE_RMSE_TOLERANCE = 1e-4
NUMERIC_ABSOLUTE_TOLERANCE = 1e-5
NUMERIC_RELATIVE_TOLERANCE = 1e-4

# Preserved NW-07 absolute gates.
COMPLETE_DISTRIBUTION_CEILING_BYTES = 262_144
STRETCH_DISTRIBUTION_CEILING_BYTES = 131_072
PEAK_ENGINE_COMMITTED_GPU_BYTES = 512 * 1024 * 1024
PROCESS_START_TO_FIRST_PRESENT_MS = 2_000.0

# Optional-profile incremental budgets.  These count every optional byte that
# is added to the common baseline distribution.  Distribution growth is a
# predeclared acceptance criterion, not an informational report.
DEFAULT_INCREMENTAL_DISTRIBUTION_CEILING_BYTES = 32_768
RAY_QUERY_INCREMENTAL_DISTRIBUTION_CEILING_BYTES = 65_536
SHADER_COUNT_INCREMENT_MAX = 2

TOOLCHAIN_DXC_VERSION = "1.8.2407"
TOOLCHAIN_DXC_MEASURED_VERSION = "1.8.2407.7"
TOOLCHAIN_MSVC_VERSION_PIN = "19.4x"
TOOLCHAIN_WINDOWS_SDK_MIN = "10.0.22621.0"
AGILITY_SDK_REQUIRED = False
AGILITY_SDK_MINIMUM_VERSION = None
AGILITY_SDK_NOTE = (
    "Mesh shaders and inline ray queries are queried through the inbox D3D12 "
    "feature set on Windows 11 23H2+.  No Agility SDK redistribution is "
    "required for these two experiments; any future Agility dependency would "
    "add separately counted bytes and require a new profile version."
)
MIN_WINDOWS_BUILD = 22_631
NVIDIA_DRIVER_FLOOR = "610.88"
PRESENTMON_SHA256 = "364e5d98d4d134bd54dd25c22ed2ca2f4883f8bc3ed6502bee0c151e3436d30c"

COMMON_SHADER_TARGETS = ("vs_6_0", "ps_6_0", "cs_6_0")
MESH_SHADER_TARGETS = ("as_6_5", "ms_6_5", "ps_6_0", "cs_6_0")
RAY_QUERY_SHADER_TARGETS = ("ps_6_5", "cs_6_0", "vs_6_0")

PRODUCTION_FORMATS = {
    "DXGI_FORMAT_R8G8B8A8_UNORM": "TEXTURE2D | RENDER_TARGET; SRV; UAV",
    "DXGI_FORMAT_R32G32B32A32_FLOAT": "BUFFER | TEXTURE2D | RENDER_TARGET; SRV; UAV",
    "DXGI_FORMAT_R32_UINT": "BUFFER | IA_INDEX_BUFFER; SRV; UAV",
    "DXGI_FORMAT_D32_FLOAT": "TEXTURE2D | DEPTH_STENCIL",
}

# Exact D3D12 capabilities queried before eligibility is decided.  Names are
# recorded verbatim so evidence can be checked against the DX12 headers.
COMMON_QUERIES = (
    {
        "query_id": "d3d_feature_level",
        "d3d12_query": "D3D12_FEATURE_FEATURE_LEVELS",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "pFeatureLevels",
        "accepted_values": ["D3D_FEATURE_LEVEL_12_0", "D3D_FEATURE_LEVEL_12_1", "D3D_FEATURE_LEVEL_12_2"],
        "minimum_rank": 0,
    },
    {
        "query_id": "resource_binding_tier",
        "d3d12_query": "D3D12_FEATURE_D3D12_OPTIONS",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "ResourceBindingTier",
        "accepted_values": ["D3D12_RESOURCE_BINDING_TIER_2", "D3D12_RESOURCE_BINDING_TIER_3"],
        "minimum_rank": 0,
    },
    {
        "query_id": "resource_heap_tier",
        "d3d12_query": "D3D12_FEATURE_D3D12_OPTIONS",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "ResourceHeapTier",
        "accepted_values": ["D3D12_RESOURCE_HEAP_TIER_2"],
        "minimum_rank": 0,
    },
    {
        "query_id": "highest_shader_model",
        "d3d12_query": "D3D12_FEATURE_SHADER_MODEL",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "HighestShaderModel",
        "accepted_values": [
            "D3D12_SHADER_MODEL_6_0",
            "D3D12_SHADER_MODEL_6_1",
            "D3D12_SHADER_MODEL_6_2",
            "D3D12_SHADER_MODEL_6_3",
            "D3D12_SHADER_MODEL_6_4",
            "D3D12_SHADER_MODEL_6_5",
            "D3D12_SHADER_MODEL_6_6",
            "D3D12_SHADER_MODEL_6_7",
            "D3D12_SHADER_MODEL_6_8",
        ],
        "minimum_rank": 0,
    },
) + tuple({
    "query_id": format_name,
    "d3d12_query": "D3D12_FEATURE_FORMAT_SUPPORT",
    "method": "ID3D12Device::CheckFeatureSupport",
    "member": f"Format={format_name}; Support1/Support2: {usage}",
    "accepted_values": ["production_usage_supported"],
    "minimum_rank": 0,
} for format_name, usage in PRODUCTION_FORMATS.items())

MESH_QUERIES = COMMON_QUERIES + (
    {
        "query_id": "mesh_shader_tier",
        "d3d12_query": "D3D12_FEATURE_D3D12_OPTIONS7",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "MeshShaderTier",
        "accepted_values": ["D3D12_MESH_SHADER_TIER_1"],
        "minimum_rank": 0,
    },
    {
        "query_id": "highest_shader_model_mesh",
        "d3d12_query": "D3D12_FEATURE_SHADER_MODEL",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "HighestShaderModel",
        "accepted_values": [
            "D3D12_SHADER_MODEL_6_5",
            "D3D12_SHADER_MODEL_6_6",
            "D3D12_SHADER_MODEL_6_7",
            "D3D12_SHADER_MODEL_6_8",
        ],
        "minimum_rank": 0,
    },
)

RAY_QUERIES = COMMON_QUERIES + (
    {
        "query_id": "raytracing_tier",
        "d3d12_query": "D3D12_FEATURE_D3D12_OPTIONS5",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "RaytracingTier",
        "accepted_values": ["D3D12_RAYTRACING_TIER_1_1"],
        "minimum_rank": 11,
    },
    {
        "query_id": "highest_shader_model_ray",
        "d3d12_query": "D3D12_FEATURE_SHADER_MODEL",
        "method": "ID3D12Device::CheckFeatureSupport",
        "member": "HighestShaderModel",
        "accepted_values": [
            "D3D12_SHADER_MODEL_6_5",
            "D3D12_SHADER_MODEL_6_6",
            "D3D12_SHADER_MODEL_6_7",
            "D3D12_SHADER_MODEL_6_8",
        ],
        "minimum_rank": 0,
    },
)

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    COMMON_PROFILE_ID: {
        "profile_kind": "common",
        "queries": COMMON_QUERIES,
        "shader_targets": COMMON_SHADER_TARGETS,
        "fallback_artifact_id": None,
        "incremental_budget_bytes": 0,
    },
    MESH_PROFILE_ID: {
        "profile_kind": "optional",
        "queries": MESH_QUERIES,
        "shader_targets": MESH_SHADER_TARGETS,
        "fallback_artifact_id": MESH_FALLBACK_ARTIFACT_ID,
        "incremental_budget_bytes": DEFAULT_INCREMENTAL_DISTRIBUTION_CEILING_BYTES,
    },
    RAY_QUERY_PROFILE_ID: {
        "profile_kind": "optional",
        "queries": RAY_QUERIES,
        "shader_targets": RAY_QUERY_SHADER_TARGETS,
        "fallback_artifact_id": RAY_FALLBACK_ARTIFACT_ID,
        "incremental_budget_bytes": RAY_QUERY_INCREMENTAL_DISTRIBUTION_CEILING_BYTES,
    },
}

# Ordered from lowest to highest so observations can be compared by rank.
SHADER_MODEL_ORDER = (
    "D3D12_SHADER_MODEL_6_0",
    "D3D12_SHADER_MODEL_6_1",
    "D3D12_SHADER_MODEL_6_2",
    "D3D12_SHADER_MODEL_6_3",
    "D3D12_SHADER_MODEL_6_4",
    "D3D12_SHADER_MODEL_6_5",
    "D3D12_SHADER_MODEL_6_6",
    "D3D12_SHADER_MODEL_6_7",
    "D3D12_SHADER_MODEL_6_8",
)

EVIDENCE_STATUSES = (
    "accepted",
    "rejected",
    "failed",
    "pending",
    "unsupported",
)

DEVICE_TIERS = hardware_devices.nw11_device_tiers()

REQUIRED_FOLLOWUP_FEATURES = (
    "vulkan_1_4",
    "asynchronous_compute",
    "work_graphs",
    "shader_execution_reordering",
    "opacity_micromaps",
    "micromesh_clustered_geometry",
    "tensor_neural_shading",
)

VENDOR_OPTIONAL_COST_ITEMS = (
    "dlss",
    "rtx_kit",
    "model_weights",
    "vendor_sdk_components",
)

PROFILE_PATH_BY_ID = {
    COMMON_PROFILE_ID: "benchmarks/nw11/profiles/common.v1.json",
    MESH_PROFILE_ID: "benchmarks/nw11/profiles/optional-mesh-shaders.v1.json",
    RAY_QUERY_PROFILE_ID: "benchmarks/nw11/profiles/optional-ray-query.v1.json",
}

EXPERIMENT_PATH_BY_ID = {
    EXPERIMENT_MESH_ID: "benchmarks/nw11/experiments/mesh-shader-vs-indexed.v2.json",
    EXPERIMENT_RAY_ID: "benchmarks/nw11/experiments/ray-query-vs-analytic.v2.json",
}

FOLLOWUP_MATRIX_PATH = "benchmarks/nw11/follow-up-matrix.v1.json"
EVIDENCE_STATUS_PATH = "benchmarks/nw11/evidence-status.v1.json"
DECISIONS_PATH = "benchmarks/nw11/decisions"
DECISION_PATH_BY_ID = {
    EXPERIMENT_MESH_ID: "benchmarks/nw11/decisions/mesh-shader.v1.json",
    EXPERIMENT_RAY_ID: "benchmarks/nw11/decisions/ray-query.v1.json",
}
SHADER_MANIFEST_PATH = "shaders/nw11/manifest.json"


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic UTF-8 JSON bytes used for every NW-11 digest."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_text_sha256(path: Path | str) -> str:
    """Line-ending stable hash for text inputs (CRLF checkouts)."""
    return sha256_bytes(Path(path).read_bytes().replace(b"\r\n", b"\n"))


def digest_value(value: Any) -> str:
    return "sha256:v1:" + sha256_bytes(canonical_json_bytes(value))


def profile_definition(profile_id: str) -> dict[str, Any]:
    try:
        return PROFILE_DEFINITIONS[profile_id]
    except KeyError as exc:
        raise KeyError(f"unknown NW-11 profile '{profile_id}'") from exc


def experiment_path(experiment_id: str) -> Path:
    try:
        return Path(EXPERIMENT_PATH_BY_ID[experiment_id])
    except KeyError as exc:
        raise KeyError(f"unknown NW-11 experiment '{experiment_id}'") from exc


def frozen_protocol() -> dict[str, Any]:
    return {
        "perf_contract": PERF_CONTRACT_ID,
        "perf_protocol": PERF_RUN_PROTOCOL,
        "startup_protocol": STARTUP_PROTOCOL,
        "capture_protocol": CORRECTNESS_CAPTURE_PROTOCOL,
        "warmup_seconds": WARMUP_SECONDS,
        "runs": PERF_RUN_COUNT,
        "run_seconds": PERF_RUN_SECONDS,
        "min_raw_samples_per_run": MIN_RAW_SAMPLES_PER_RUN,
        "fresh_process_per_run": FRESH_PROCESS_PER_RUN,
        "application_pipeline_cache": APPLICATION_PIPELINE_CACHE,
        "percentile": PERCENTILE,
        "percentile_method": PERCENTILE_METHOD,
    }


def frozen_workload() -> dict[str, Any]:
    return {
        "graph": BASE_GRAPH,
        "graph_sha256": BASE_GRAPH_SHA256,
        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "torus_instances": TORUS_INSTANCE_COUNT,
        "loop_ticks": LOOP_TICKS,
        "ticks_per_second": TICKS_PER_SECOND,
        "loop_seconds": LOOP_SECONDS,
        "frame_rate_hz": FRAME_RATE_HZ,
        "quality_profile_id": QUALITY_PROFILE_ID,
        "reference_fixed_ticks": list(REFERENCE_FIXED_TICKS),
        "reference_loop_boundary_ticks": list(REFERENCE_LOOP_BOUNDARY_TICKS),
    }


def frozen_artifact_budget() -> dict[str, Any]:
    return {
        "distribution_size_policy": "gated",
        "complete_distribution_bytes_ceiling": COMPLETE_DISTRIBUTION_CEILING_BYTES,
        "stretch_distribution_bytes_ceiling": STRETCH_DISTRIBUTION_CEILING_BYTES,
        "stretch_is_mandatory_gate": False,
        "incremental_distribution_bytes_ceiling": DEFAULT_INCREMENTAL_DISTRIBUTION_CEILING_BYTES,
        "peak_engine_committed_gpu_bytes_ceiling": PEAK_ENGINE_COMMITTED_GPU_BYTES,
        "process_start_to_first_present_ms_ceiling": PROCESS_START_TO_FIRST_PRESENT_MS,
        "shader_count_increment_ceiling": SHADER_COUNT_INCREMENT_MAX,
        "optional_dependency_bytes_policy": (
            "Every optional SDK, SDK component, model weight, or vendor runtime "
            "byte is counted in the incremental distribution budget and is "
            "absent from common/fallback artifacts."
        ),
    }


def frozen_runtime_requirements() -> dict[str, Any]:
    return {
        "os": "windows_11_x64",
        "minimum_windows_build": MIN_WINDOWS_BUILD,
        "minimum_nvidia_driver": NVIDIA_DRIVER_FLOOR,
        "d3d12_runtime": "windows_inbox",
        "agility_sdk": {
            "required": AGILITY_SDK_REQUIRED,
            "minimum_version": AGILITY_SDK_MINIMUM_VERSION,
            "note": AGILITY_SDK_NOTE,
        },
        "runtime_shader_compilation": False,
        "dxc_runtime_redistribution": False,
    }


def frozen_toolchain_requirements() -> dict[str, Any]:
    return {
        "dxc": {
            "pinned_version": TOOLCHAIN_DXC_VERSION,
            "measured_version": TOOLCHAIN_DXC_MEASURED_VERSION,
            "compilation": "offline",
            "runtime_dependency": False,
        },
        "msvc": {"version_family": TOOLCHAIN_MSVC_VERSION_PIN},
        "windows_sdk_minimum": TOOLCHAIN_WINDOWS_SDK_MIN,
        "cmake_minimum": "3.24",
        "ninja_required": True,
    }


def evidence_semantics() -> dict[str, Any]:
    return {
        "statuses": list(EVIDENCE_STATUSES),
        "pending_is_not_pass": True,
        "unsupported_is_not_failure_or_pass": True,
        "failed_is_not_substituted": True,
        "per_device_decision_isolation": True,
        "matched_baseline_variant_environment_required": True,
        "stale_evidence_rejected": True,
        "microbenchmark_only_gain_cannot_justify_adoption": True,
        "common_fallback_bytes_unchanged": True,
    }


__all__ = [
    "TASK",
    "IMPLEMENTATION_BASE",
    "PLANNING_BASE_PROVENANCE_ONLY",
    "COMMON_PROFILE_ID",
    "COMMON_REQUIREMENTS_DIGEST",
    "MESH_PROFILE_ID",
    "RAY_QUERY_PROFILE_ID",
    "MESH_FALLBACK_ARTIFACT_ID",
    "RAY_FALLBACK_ARTIFACT_ID",
    "EXPERIMENT_MESH_ID",
    "EXPERIMENT_RAY_ID",
    "BASE_GRAPH",
    "BASE_GRAPH_SHA256",
    "CANONICAL_PRODUCTION_CONFIG",
    "OUTPUT_WIDTH",
    "OUTPUT_HEIGHT",
    "TORUS_INSTANCE_COUNT",
    "LOOP_TICKS",
    "TICKS_PER_SECOND",
    "LOOP_SECONDS",
    "FRAME_RATE_HZ",
    "REFERENCE_FIXED_TICKS",
    "REFERENCE_LOOP_BOUNDARY_TICKS",
    "PERF_CONTRACT_ID",
    "PERF_RUN_PROTOCOL",
    "STARTUP_PROTOCOL",
    "CORRECTNESS_CAPTURE_PROTOCOL",
    "QUALITY_PROFILE_ID",
    "WARMUP_SECONDS",
    "PERF_RUN_COUNT",
    "PERF_RUN_SECONDS",
    "MIN_RAW_SAMPLES_PER_RUN",
    "FRESH_PROCESS_PER_RUN",
    "APPLICATION_PIPELINE_CACHE",
    "PERCENTILE",
    "PERCENTILE_METHOD",
    "GPU_TIME_REDUCTION_PERCENT_MIN",
    "CPU_SUBMIT_MS_MAX",
    "IMAGE_ABSOLUTE_TOLERANCE",
    "IMAGE_RELATIVE_TOLERANCE",
    "IMAGE_RMSE_TOLERANCE",
    "NUMERIC_ABSOLUTE_TOLERANCE",
    "NUMERIC_RELATIVE_TOLERANCE",
    "COMPLETE_DISTRIBUTION_CEILING_BYTES",
    "STRETCH_DISTRIBUTION_CEILING_BYTES",
    "PEAK_ENGINE_COMMITTED_GPU_BYTES",
    "PROCESS_START_TO_FIRST_PRESENT_MS",
    "DEFAULT_INCREMENTAL_DISTRIBUTION_CEILING_BYTES",
    "RAY_QUERY_INCREMENTAL_DISTRIBUTION_CEILING_BYTES",
    "SHADER_COUNT_INCREMENT_MAX",
    "TOOLCHAIN_DXC_VERSION",
    "TOOLCHAIN_DXC_MEASURED_VERSION",
    "TOOLCHAIN_MSVC_VERSION_PIN",
    "TOOLCHAIN_WINDOWS_SDK_MIN",
    "AGILITY_SDK_REQUIRED",
    "AGILITY_SDK_MINIMUM_VERSION",
    "AGILITY_SDK_NOTE",
    "MIN_WINDOWS_BUILD",
    "NVIDIA_DRIVER_FLOOR",
    "COMMON_SHADER_TARGETS",
    "MESH_SHADER_TARGETS",
    "RAY_QUERY_SHADER_TARGETS",
    "COMMON_QUERIES",
    "MESH_QUERIES",
    "RAY_QUERIES",
    "PROFILE_DEFINITIONS",
    "SHADER_MODEL_ORDER",
    "EVIDENCE_STATUSES",
    "DEVICE_TIERS",
    "REQUIRED_FOLLOWUP_FEATURES",
    "VENDOR_OPTIONAL_COST_ITEMS",
    "PROFILE_PATH_BY_ID",
    "EXPERIMENT_PATH_BY_ID",
    "FOLLOWUP_MATRIX_PATH",
    "EVIDENCE_STATUS_PATH",
    "DECISIONS_PATH",
    "DECISION_PATH_BY_ID",
    "SHADER_MANIFEST_PATH",
    "canonical_json_bytes",
    "sha256_bytes",
    "sha256_file",
    "canonical_text_sha256",
    "digest_value",
    "profile_definition",
    "experiment_path",
    "frozen_protocol",
    "frozen_workload",
    "frozen_artifact_budget",
    "frozen_runtime_requirements",
    "frozen_toolchain_requirements",
    "evidence_semantics",
]
