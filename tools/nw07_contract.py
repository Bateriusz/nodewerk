#!/usr/bin/env python3
"""Frozen NW-07 architectural-proof contract constants.

This module owns the authoritative thresholds, identities, and protocol names
for the 256 KiB architectural proof.  Every verifier and evidence module
imports these values rather than redeclaring (and accidentally weakening)
them.  The common capability profile identity and requirements digest are
inherited unchanged from NW-03..NW-06.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402

TASK = "NW-07"

IMPLEMENTATION_BASE = "b7e2d11062d4f7bb123ae97c21839631ffc09b93"
PLANNING_BASE = "270d298f1b037548e0ab4808b2c123c12ddcc4e6"

PROFILE_ID = nwp.COMMON_PROFILE_ID
REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST

# Presentation / production contract.
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
TICKS_PER_SECOND = 48_000
LOOP_SECONDS = 20
LOOP_TICKS = LOOP_SECONDS * TICKS_PER_SECOND  # 960,000
FRAME_STEP_NUMERATOR = 800
FRAME_STEP_DENOMINATOR = 1
FRAME_RATE_HZ = TICKS_PER_SECOND // FRAME_STEP_NUMERATOR  # 60
TORUS_INSTANCE_COUNT = 256

# Hard architectural gates.
COMPLETE_DISTRIBUTION_CEILING_BYTES = 262_144  # 256 KiB
STRETCH_DISTRIBUTION_CEILING_BYTES = 131_072  # 128 KiB, measurement only
PEAK_ENGINE_COMMITTED_GPU_BYTES = 512 * 1024 * 1024  # 512 MiB
PROCESS_START_TO_FIRST_PRESENT_MS = 2_000.0

# Frame-time contract.
FRAME_TIME_THRESHOLD_MS = 16.67
FRAME_TIME_OPERATOR = "<"  # strict: exactly 16.67 ms does not pass
PERCENTILE = 99.0
PERCENTILE_METHOD = "nearest-rank"
WARMUP_SECONDS = 5
PERF_RUN_COUNT = 3
PERF_RUN_SECONDS = 60
MIN_RAW_SAMPLES_PER_RUN = 600

# Existing image/numeric requirements, preserved from NW-06.
NUMERIC_ABSOLUTE_TOLERANCE = 1e-5
NUMERIC_RELATIVE_TOLERANCE = 1e-4
NUMERIC_TOLERANCE_MODE = "absolute_or_relative"
IMAGE_ABSOLUTE_TOLERANCE = 1e-4
IMAGE_RELATIVE_TOLERANCE = 1e-4
IMAGE_TOLERANCE_MODE = "absolute_required"

# Protocol identifiers.  Evidence that names a different protocol cannot
# certify this contract.
PERF_CONTRACT_ID = "nw07_perf_ref_v1"
PRODUCTION_PROTOCOL = "nw07-production.v1"
CORRECTNESS_CAPTURE_PROTOCOL = "nw07-correctness-capture.v1"
STARTUP_PROTOCOL = "nw07-startup.v1"
PERF_RUN_PROTOCOL = "nw07-perf-run.v1"
PERF_COLLECTION_PROTOCOL = "nw07-perf-collect.v1"
DIAGNOSTIC_OVERHEAD_PROTOCOL = "nw07-diagnostic-overhead.v1"

# Reference performance contract: the exact board name is recorded because
# NW-07 intentionally keeps an explicitly justified absolute performance
# reference.  It is never used as a compatibility predicate.
REFERENCE_BOARD_MODEL = "NVIDIA GeForce RTX 4060 Ti"
REFERENCE_BOARD_VENDOR_ID = 0x10DE
REFERENCE_BOARD_DEVICE_ID = 0x2805
REFERENCE_BOARD_VRAM_MIB = 8_192
REFERENCE_ARCHITECTURE = "Ada"

CORRECTNESS_COVERAGE_TARGETS = {
    "ada": "desktop_ada_representative",
    "blackwell": "desktop_blackwell_representative",
}
BLACKWELL_DEVELOPMENT_TARGET = "desktop_blackwell_development_series"
FIXED_TICK_CORRECTNESS_ROLE = "fixed_tick_correctness"
PERFORMANCE_CERTIFICATION_ROLE = "performance_certification"
DEVELOPMENT_SERIES_ROLE = "blackwell_development_series"
DIAGNOSTIC_OVERHEAD_ROLE = "diagnostic_overhead"

REQUIRED_FIXED_TICKS = (0, 240_000, 480_000, 720_000, 959_999)
LOOP_BOUNDARY_TICKS = (0, LOOP_TICKS)

CAPTURE_FORMAT = "rgba32f"
CAPTURE_KIND = "rgba32f_raw"
CAPTURE_CHANNELS = 4
CAPTURE_BYTES_PER_PIXEL = 16

MATCHED_ENVIRONMENT_RELATIVE_ALERT_PERCENT = 10.0

RELEASE_BUILD_PROFILE = "release"
DIAGNOSTIC_BUILD_PROFILE = "dev"

CANONICAL_CONFIG_RELATIVE_PATHS = (
    "fixtures/nw07/production_loop.nwgraph",
    "benchmarks/nw07/canonical-production.v1.json",
    "benchmarks/nw07/perf-ref-v1.json",
    "benchmarks/nw07/metric-baselines.v1.json",
)

# Build inputs whose exact bytes define a release export.  Keeping the list
# explicit (rather than globbing) means an accidental new source file cannot
# change the artifact without also changing the frozen build-input digest.
PINNED_BUILD_INPUTS = (
    "CMakeLists.txt",
    "CMakePresets.json",
    "cmake/BuildProfiles.cmake",
    "cmake/CompileShaders.cmake",
    "cmake/DependencyCheck.cmake",
    "cmake/ToolchainManifest.cmake",
    "cmake/toolchains/clang-cl-x64.cmake",
    "cmake/toolchains/msvc-x64.cmake",
    "benchmarks/toolchain-manifest.v1.json",
    "fixtures/nw07/production_loop.nwgraph",
    "fixtures/nw05_procedural_image/main.cpp",
    "shaders/compute_fixture.hlsl",
    "shaders/display_fixture.hlsl",
    "shaders/nw06/manifest.json",
    "shaders/nw06/nw06_bloom_cs.hlsl",
    "shaders/nw06/nw06_camera_cs.hlsl",
    "shaders/nw06/nw06_generate_cs.hlsl",
    "shaders/nw06/nw06_instances_cs.hlsl",
    "shaders/nw06/nw06_light_cs.hlsl",
    "shaders/nw06/nw06_material_cs.hlsl",
    "shaders/nw06/nw06_scene_data_cs.hlsl",
    "shaders/nw06/nw06_scene_ps.hlsl",
    "shaders/nw06/nw06_scene_vs.hlsl",
    "shaders/nw06/nw06_tonemap_cs.hlsl",
    "src/package/nwp_format.cpp",
    "src/package/nwp_format.h",
    "src/platform/win32_window.cpp",
    "src/platform/win32_window.h",
    "src/player/capability_check.cpp",
    "src/player/capability_check.h",
    "src/player/capability_requirements.cpp",
    "src/player/capability_requirements.h",
    "src/player/clock_provider.h",
    "src/player/crt_experiment_obligations.h",
    "src/player/d3d12_bootstrap.cpp",
    "src/player/d3d12_bootstrap.h",
    "src/player/diagnostics.cpp",
    "src/player/diagnostics.h",
    "src/player/embedded_package.cpp",
    "src/player/embedded_package.h",
    "src/player/exit_codes.h",
    "src/player/nw07_capture_controls.h",
    "src/player/package_loader.cpp",
    "src/player/package_loader.h",
    "src/player/package_player.cpp",
    "src/player/package_player.h",
    "src/player/player.cpp",
    "src/player/player.h",
    "src/player/player_instrumentation.cpp",
    "src/player/player_instrumentation.h",
    "src/player/resource_plan.cpp",
    "src/player/resource_plan.h",
    "tools/dependency_check.py",
    "tools/nw03_compiler.py",
    "tools/nw03_oracle.py",
    "tools/nw04_fixture.py",
    "tools/nw05_export.py",
    "tools/nw05_optimizer.py",
    "tools/nw05_schedule.py",
    "tools/nw06_export.py",
    "tools/nw06_procedural.py",
    "tools/nw06_timeline.py",
    "tools/nwp_package.py",
    "tools/offline_shader_pack.py",
    "tools/nw07_contract.py",
    "tools/nw07_export.py",
)


def required_operations() -> frozenset[str]:
    return frozenset(
        {
            "nw.texture.noise2d",
            "nw.mesh.torus",
            "nw.scene.instances",
            "nw.material.standard",
            "nw.camera.curve",
            "nw.light.directional",
            "nw.animation.twist",
            "nw.render.instanced",
            "nw.post.bloom",
            "nw.post.tonemap",
            "nw.render.present",
            "nw.residency.generate_texture",
            "nw.residency.generate_mesh",
            "nw.residency.generate_instances",
        }
    )


def authoritative_image_tolerances() -> dict[str, object]:
    return {
        "numeric": {
            "absolute": NUMERIC_ABSOLUTE_TOLERANCE,
            "relative": NUMERIC_RELATIVE_TOLERANCE,
            "mode": NUMERIC_TOLERANCE_MODE,
        },
        "image": {
            "absolute": IMAGE_ABSOLUTE_TOLERANCE,
            "relative": IMAGE_RELATIVE_TOLERANCE,
            "mode": IMAGE_TOLERANCE_MODE,
        },
        "capture": {
            "width": OUTPUT_WIDTH,
            "height": OUTPUT_HEIGHT,
            "format": CAPTURE_FORMAT,
            "kind": CAPTURE_KIND,
            "bytes_per_pixel": CAPTURE_BYTES_PER_PIXEL,
        },
    }


def authoritative_hard_gates() -> dict[str, object]:
    return {
        "complete_distribution_bytes": {
            "operator": "<=",
            "threshold": COMPLETE_DISTRIBUTION_CEILING_BYTES,
        },
        "process_start_to_first_present_ms": {
            "operator": "<=",
            "threshold": PROCESS_START_TO_FIRST_PRESENT_MS,
        },
        "peak_engine_committed_gpu_bytes": {
            "operator": "<=",
            "threshold": PEAK_ENGINE_COMMITTED_GPU_BYTES,
        },
        "p99_gpu_frame_time_ms": {
            "operator": FRAME_TIME_OPERATOR,
            "threshold": FRAME_TIME_THRESHOLD_MS,
            "percentile": PERCENTILE,
            "percentile_method": PERCENTILE_METHOD,
            "independent_per_run": True,
        },
        "p99_cpu_submit_ms": {
            "operator": FRAME_TIME_OPERATOR,
            "threshold": FRAME_TIME_THRESHOLD_MS,
            "percentile": PERCENTILE,
            "percentile_method": PERCENTILE_METHOD,
            "independent_per_run": True,
        },
    }


__all__ = [
    "TASK",
    "IMPLEMENTATION_BASE",
    "PLANNING_BASE",
    "PROFILE_ID",
    "REQUIREMENTS_DIGEST",
    "OUTPUT_WIDTH",
    "OUTPUT_HEIGHT",
    "TICKS_PER_SECOND",
    "LOOP_SECONDS",
    "LOOP_TICKS",
    "FRAME_STEP_NUMERATOR",
    "FRAME_STEP_DENOMINATOR",
    "FRAME_RATE_HZ",
    "TORUS_INSTANCE_COUNT",
    "COMPLETE_DISTRIBUTION_CEILING_BYTES",
    "STRETCH_DISTRIBUTION_CEILING_BYTES",
    "PEAK_ENGINE_COMMITTED_GPU_BYTES",
    "PROCESS_START_TO_FIRST_PRESENT_MS",
    "FRAME_TIME_THRESHOLD_MS",
    "FRAME_TIME_OPERATOR",
    "PERCENTILE",
    "PERCENTILE_METHOD",
    "WARMUP_SECONDS",
    "PERF_RUN_COUNT",
    "PERF_RUN_SECONDS",
    "MIN_RAW_SAMPLES_PER_RUN",
    "NUMERIC_ABSOLUTE_TOLERANCE",
    "NUMERIC_RELATIVE_TOLERANCE",
    "NUMERIC_TOLERANCE_MODE",
    "IMAGE_ABSOLUTE_TOLERANCE",
    "IMAGE_RELATIVE_TOLERANCE",
    "IMAGE_TOLERANCE_MODE",
    "PERF_CONTRACT_ID",
    "PRODUCTION_PROTOCOL",
    "CORRECTNESS_CAPTURE_PROTOCOL",
    "STARTUP_PROTOCOL",
    "PERF_RUN_PROTOCOL",
    "DIAGNOSTIC_OVERHEAD_PROTOCOL",
    "REFERENCE_BOARD_MODEL",
    "REFERENCE_BOARD_VENDOR_ID",
    "REFERENCE_BOARD_DEVICE_ID",
    "REFERENCE_BOARD_VRAM_MIB",
    "REFERENCE_ARCHITECTURE",
    "CORRECTNESS_COVERAGE_TARGETS",
    "BLACKWELL_DEVELOPMENT_TARGET",
    "FIXED_TICK_CORRECTNESS_ROLE",
    "PERFORMANCE_CERTIFICATION_ROLE",
    "DEVELOPMENT_SERIES_ROLE",
    "DIAGNOSTIC_OVERHEAD_ROLE",
    "REQUIRED_FIXED_TICKS",
    "LOOP_BOUNDARY_TICKS",
    "CAPTURE_FORMAT",
    "CAPTURE_KIND",
    "CAPTURE_CHANNELS",
    "CAPTURE_BYTES_PER_PIXEL",
    "MATCHED_ENVIRONMENT_RELATIVE_ALERT_PERCENT",
    "RELEASE_BUILD_PROFILE",
    "DIAGNOSTIC_BUILD_PROFILE",
    "CANONICAL_CONFIG_RELATIVE_PATHS",
    "PINNED_BUILD_INPUTS",
    "required_operations",
    "authoritative_image_tolerances",
    "authoritative_hard_gates",
]
