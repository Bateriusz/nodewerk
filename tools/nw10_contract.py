#!/usr/bin/env python3
"""NW-10 compact-export contract constants and fail-closed predicates.

NW-10 is deliberately an extension of the existing NW-02 typed graph,
NW-03 package, NW-05 export, NW-06 procedural content, and NW-07 evidence
architecture.  It does not introduce a second player, graph model, quality
profile, or performance contract.

The historical NW-07 build-input digest is retained as provenance.  NW-10
adds release-closure files (the compact package decoder) and therefore owns a
separate pinned-input digest instead of silently reinterpreting the NW-07
evidence identity.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent

TASK = "NW-10"
IMPLEMENTATION_BASE = "c7a73ce64a15973fc18b384543894e68ee2464e8"
PLANNING_PROVENANCE = "270d298f1b037548e0ab4808b2c123c12ddcc4e6"

# Existing tiers are preserved exactly.  The 64 KiB value is an experimental
# target, never a reduction of a previously configured budget.
COMPACT_TARGET_BYTES = 65_536
STRETCH_TIER_BYTES = 131_072
PROOF_TIER_BYTES = 262_144
STANDARD_TIER_BYTES = 1_048_576
LARGER_TIERS_SUPPORTED = True

# Preserved NW-07 gates.
STARTUP_CEILING_MS = 2_000.0
PEAK_ENGINE_COMMITTED_GPU_BYTES = 512 * 1024 * 1024
FRAME_TIME_THRESHOLD_MS = 16.67
FRAME_TIME_OPERATOR = "<"
PERF_CONTRACT_ID = "nw07_perf_ref_v1"
PERF_PROTOCOL_ID = "nw07-perf-run.v1"
WARMUP_SECONDS = 5
PERF_RUN_COUNT = 3
PERF_RUN_SECONDS = 60
FRESH_PROCESS_PER_RUN = True
APPLICATION_PIPELINE_CACHE = False

NUMERIC_ABSOLUTE_TOLERANCE = 1e-5
NUMERIC_RELATIVE_TOLERANCE = 1e-4
IMAGE_ABSOLUTE_TOLERANCE = 1e-4
IMAGE_RELATIVE_TOLERANCE = 1e-4
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
TICKS_PER_SECOND = 48_000
LOOP_SECONDS = 20
LOOP_TICKS = LOOP_SECONDS * TICKS_PER_SECOND
FRAME_STEP_NUMERATOR = 800
FRAME_RATE_HZ = TICKS_PER_SECOND // FRAME_STEP_NUMERATOR

# The NW-07 proof remains the canonical comparison artifact.
CANONICAL_PROOF_GRAPH = "fixtures/nw07/production_loop.nwgraph"
CANONICAL_PROOF_CONFIG = "benchmarks/nw07/canonical-production.v1.json"
HISTORICAL_NW07_BUILD_INPUTS_DIGEST = (
    "sha256:v1:d9ad24057ae7c273ad4c757dafc5d3b54047fcb360b8845a6413136b78c232ab"
)

# Restricted intro profile.  This is frozen, deliberately small, and does not
# expand the general NW-07 production or any larger configured budget.
INTRO_PROFILE_ID = "nw10.intro.sdf20.v1"
INTRO_GRAPH = "fixtures/nw10/restricted_intro.nwgraph"
INTRO_SHADER = "shaders/nw10/nw10_sdf_intro_cs.hlsl"
INTRO_OPERATORS = (
    "nw.render.fullscreen",
    "nw.render.present",
    "nw.residency.upload_texture",
    "nw.shader.expr",
    "nw.texture.procedural",
)
INTRO_REQUIRED_FEATURES = (
    "sphere_sdf",
    "torus_sdf",
    "sdf_composition",
    "seeded_procedural_material",
    "camera_curve",
    "aces_tonemapping",
    "fixed_tick_replay",
)
INTRO_EXCLUDED = (
    "audio",
    "mesh_pipeline",
    "hardware_ray_tracing",
    "runtime_dxc",
    "external_assets",
)

# Compact delivery container identities.  Unknown IDs are rejected before any
# payload allocation or GPU work.
CODEC_STORED_ID = "nwc.codec.stored.v1"
CODEC_MSZIP_ID = "nwc.codec.mszip.v1"
PLAYER_SUPPORTED_CODEC_IDS = (CODEC_STORED_ID, CODEC_MSZIP_ID)
CANDIDATE_ONLY_CODECS = (
    "candidate.zlib.raw",
    "candidate.bzip2",
    "candidate.lzma",
)

LAYOUT_CANONICAL_ID = "nwc.layout.canonical.v1"
LAYOUT_AOS_ID = "nwc.layout.aos.v1"
LAYOUT_SOA_ID = "nwc.layout.soa.v1"
KNOWN_LAYOUT_IDS = (LAYOUT_CANONICAL_ID, LAYOUT_AOS_ID, LAYOUT_SOA_ID)

# Explicit parameter-layout and quantization identifiers for measured
# alternatives.  A packed layout is usable only when the declared error stays
# inside the preserved numeric tolerance.
PARAM_LAYOUT_CANONICAL_ID = "nwc.params.canonical.v1"
PARAM_LAYOUT_VARINT_ID = "nwc.params.varint-stringtable.v1"
PARAM_LAYOUT_F16_ID = "nwc.params.float16.v1"
PARAM_LAYOUT_Q16_ID = "nwc.params.q16.v1"
KNOWN_PARAM_LAYOUTS = (
    PARAM_LAYOUT_CANONICAL_ID,
    PARAM_LAYOUT_VARINT_ID,
    PARAM_LAYOUT_F16_ID,
    PARAM_LAYOUT_Q16_ID,
)

# Packaged decoder identity is recorded in byte ledgers.  The version is the
# repository source revision plus the format contract, not an assumed binary
# size.
COMPACT_CONTAINER_ID = "nwc.container.v1"
COMPACT_DECODER_SOURCE = "src/package/nw_compact.cpp"
COMPACT_DECODER_HEADER = "src/package/nw_compact.h"
PACKAGING_TOOL_ID = "tools/nw10_compact.py"

EVIDENCE_STATUSES = (
    "pending_physical",
    "informational_contract_only",
    "verified_physical",
    "failed",
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_text_sha256(path: Path | str) -> str:
    return hashlib.sha256(
        Path(path).read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def nw10_pinned_build_inputs() -> tuple[str, ...]:
    """Build inputs that define an NW-10 release artifact.

    The NW-07 list remains untouched.  This list adds only files reachable from
    the NW-10 compact decoder and restricted-intro export path.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import nw07_contract as nw07

    extra = (
        "src/package/nw_compact.cpp",
        "src/package/nw_compact.h",
        "src/player/embedded_package.cpp",
        "src/player/embedded_package.h",
        "tools/nw10_contract.py",
        "tools/nw10_compact.py",
        "tools/nw10_intro.py",
        "tools/nw10_proof.py",
        "tools/nw10_evidence.py",
        "shaders/nw10/nw10_sdf_intro_cs.hlsl",
        "fixtures/nw10/restricted_intro.nwgraph",
    )
    return tuple(dict.fromkeys(tuple(nw07.PINNED_BUILD_INPUTS) + extra))


def compute_build_inputs(
    *, repo_root: Path | None = None, inputs: tuple[str, ...] | None = None
) -> dict[str, str]:
    root = Path(repo_root or REPO_ROOT)
    result: dict[str, str] = {}
    for relative in inputs or nw10_pinned_build_inputs():
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"NW-10 pinned build input is unavailable: {relative}")
        result[relative] = canonical_text_sha256(path)
    return dict(sorted(result.items()))


def build_inputs_digest(inputs: Mapping[str, str]) -> str:
    return "sha256:v1:" + hashlib.sha256(
        canonical_json_bytes(dict(sorted(inputs.items())))
    ).hexdigest()


def image_tolerances() -> dict[str, Any]:
    return {
        "numeric": {
            "absolute": NUMERIC_ABSOLUTE_TOLERANCE,
            "relative": NUMERIC_RELATIVE_TOLERANCE,
            "mode": "absolute_or_relative",
        },
        "image": {
            "absolute": IMAGE_ABSOLUTE_TOLERANCE,
            "relative": IMAGE_RELATIVE_TOLERANCE,
            "mode": "absolute_required",
        },
    }


def budget_tiers() -> dict[str, Any]:
    return {
        "compact_target_bytes": COMPACT_TARGET_BYTES,
        "stretch_bytes": STRETCH_TIER_BYTES,
        "proof_bytes": PROOF_TIER_BYTES,
        "standard_bytes": STANDARD_TIER_BYTES,
        "larger_configured_budgets_supported": LARGER_TIERS_SUPPORTED,
        "compact_target_is_universal_feasibility_claim": False,
    }


def classify_measurement(record: Mapping[str, Any]) -> str:
    """Classify evidence without promoting contract-only numbers to physical proof.

    A physical capture is verified only when it names the exact artifact,
    matching environment, and frozen protocol.  Everything else is retained as
    informational or pending rather than relabelled.
    """
    status = str(record.get("status", "")).strip()
    if status == "failed":
        return "failed"
    if status == "verified_physical":
        if (
            record.get("execution_kind") in {"physical", "documented_gpu_passthrough"}
            and record.get("artifact_sha256")
            and record.get("environment_match") is True
            and record.get("protocol") == PERF_PROTOCOL_ID
        ):
            return "verified_physical"
        return "pending_physical"
    if record.get("test_payload") is True or status == "contract_only":
        return "informational_contract_only"
    return "pending_physical"


def validate_codec_identifiers(
    codec_id: str, layout_id: str, *, parameter_layout_id: str | None = None
) -> None:
    if codec_id not in PLAYER_SUPPORTED_CODEC_IDS:
        raise ValueError(f"unsupported compact codec identifier '{codec_id}'")
    if layout_id not in KNOWN_LAYOUT_IDS:
        raise ValueError(f"unsupported compact layout identifier '{layout_id}'")
    if parameter_layout_id is not None and parameter_layout_id not in KNOWN_PARAM_LAYOUTS:
        raise ValueError(
            f"unsupported parameter layout identifier '{parameter_layout_id}'"
        )


def validate_intro_profile(record: Mapping[str, Any]) -> None:
    if record.get("profile_id") != INTRO_PROFILE_ID:
        raise ValueError("restricted intro profile id changed")
    if record.get("loop_ticks") != LOOP_TICKS:
        raise ValueError("restricted intro is not a 20-second 960,000 tick loop")
    if record.get("loop_seconds") != LOOP_SECONDS:
        raise ValueError("restricted intro duration is not 20 seconds")
    if (record.get("width"), record.get("height")) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        raise ValueError("restricted intro is not 1920x1080")
    if record.get("audio") is not False:
        raise ValueError("restricted intro must exclude audio")
    if record.get("mesh_pipeline") is not False:
        raise ValueError("restricted intro must exclude the mesh pipeline")
    if record.get("hardware_ray_tracing_required") is not False:
        raise ValueError("restricted intro must not require hardware ray tracing")
    if record.get("runtime_dxc") is not False:
        raise ValueError("restricted intro must compile shaders offline")
    if tuple(record.get("operators") or ()) != INTRO_OPERATORS:
        raise ValueError("restricted intro operator set changed")
    for feature in INTRO_REQUIRED_FEATURES:
        if feature not in (record.get("features") or ()):  # type: ignore[operator]
            raise ValueError(f"restricted intro is missing feature '{feature}'")


def authoritative_limits() -> dict[str, Any]:
    return {
        "complete_distribution_bytes_ceiling": PROOF_TIER_BYTES,
        "stretch_distribution_bytes_ceiling": STRETCH_TIER_BYTES,
        "compact_target_bytes": COMPACT_TARGET_BYTES,
        "process_start_to_first_present_ms_ceiling": STARTUP_CEILING_MS,
        "peak_engine_committed_gpu_bytes_ceiling": PEAK_ENGINE_COMMITTED_GPU_BYTES,
        "p99_gpu_frame_time_ms": {
            "operator": FRAME_TIME_OPERATOR,
            "threshold": FRAME_TIME_THRESHOLD_MS,
            "per_run_independent": True,
            "runs": PERF_RUN_COUNT,
            "warmup_seconds": WARMUP_SECONDS,
            "run_seconds": PERF_RUN_SECONDS,
        },
        "p99_cpu_submit_ms": {
            "operator": FRAME_TIME_OPERATOR,
            "threshold": FRAME_TIME_THRESHOLD_MS,
            "per_run_independent": True,
            "runs": PERF_RUN_COUNT,
            "warmup_seconds": WARMUP_SECONDS,
            "run_seconds": PERF_RUN_SECONDS,
        },
    }
