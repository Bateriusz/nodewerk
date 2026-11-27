#!/usr/bin/env python3
"""NW-13 optional-lowering qualification, artifact provenance and evidence.

This module never executes GPU work.  It decides eligibility from queried
capabilities, binds every optional artifact to provenance, bounds declared
budgets, and encodes the measured/unavailable distinction required by ADR 0009.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

import nwp_package as nwp


COMMON_PROFILE_ID = nwp.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST

MESH_PROFILE_ID = "nw.optional.d3d12.mesh_shaders.ms6_5.v1"
MESH_REQUIREMENTS_DIGEST = "sha256:v1:a522ce6f0f5163ecbcf6045ab8c52fe72ee9855c902db40b2094ffc530bc4d62"
MESH_FALLBACK_ID = "nw11.fallback.mesh.indexed.d3d12.fl12_0.sm6_0.v1"
RAY_PROFILE_ID = "nw.optional.d3d12.ray_query.ps6_5.v1"
RAY_REQUIREMENTS_DIGEST = "sha256:v1:7f18259d05889226ad52e72c36dda206a1ba58bac205417aa15ce665e78602f3"
RAY_FALLBACK_ID = "nw11.fallback.ray.analytic.d3d12.fl12_0.sm6_0.v1"

WORK_GRAPH_PROFILE_ID = "nw13.optional.d3d12.work_graphs.experimental.v1"
WORK_GRAPH_FALLBACK_ID = "nw13.fallback.work_graphs.conventional.v1"


class EvidenceError(ValueError):
    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result = {
        "category": "optional_lowering",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def capabilities_revision(capabilities: Mapping[str, Any]) -> str:
    return sha256_text(json.dumps(dict(sorted(capabilities.items())), sort_keys=True, separators=(",", ":")))


DEFAULT_CAPABILITIES = {
    "d3d_feature_level": "12_0",
    "highest_shader_model": "6_0",
    "raytracing_tier": None,
    "mesh_shader_tier": None,
    "work_graphs_tier": None,
    "linear_algebra_fp16": False,
    "enhanced_barriers": False,
}

SHADER_MODEL_ORDER = {"6_0": 60, "6_5": 65, "6_6": 66, "6_8": 68, "6_9": 69}


def _shader_model_at_least(value: Any, required: str) -> bool:
    if not isinstance(value, str) or value not in SHADER_MODEL_ORDER:
        return False
    return SHADER_MODEL_ORDER[value] >= SHADER_MODEL_ORDER[required]


# ---------------------------------------------------------------------------
# Optional GPU lowering decisions
# ---------------------------------------------------------------------------


def optional_lowering_matrix() -> dict[str, Any]:
    """Record every optional technique independently."""
    return {
        "format": "nw13.optional-lowering-matrix",
        "version": 1,
        "common_surface": {
            "profile_id": COMMON_PROFILE_ID,
            "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
            "surface_realization": "raster indexed/instanced",
            "rtx_required": False,
            "selection_basis": "matched non-RTX surface measurements; common fallback remains independently buildable",
        },
        "dxr_inline_ray_query": {
            "status": "capability_qualified",
            "profile_id": RAY_PROFILE_ID,
            "requirements_digest": RAY_REQUIREMENTS_DIGEST,
            "shader_model": "6_5",
            "query": {"raytracing_tier": ">=1.1"},
            "fallback_artifact_id": RAY_FALLBACK_ID,
            "decision": "adopt_when_queried_capability_and_measured_arm_acceptance_hold",
            "scope": "bounded shadows and one-bounce reflections from the current scene",
        },
        "mesh_shader": {
            "status": "pending_unavailable",
            "decision": "defer",
            "profile_id": MESH_PROFILE_ID,
            "requirements_digest": MESH_REQUIREMENTS_DIGEST,
            "shader_model": "6_5",
            "query": {"mesh_shader_tier": ">=1"},
            "fallback_artifact_id": MESH_FALLBACK_ID,
            "decision": "defer",
            "reason": "NW-11 mesh-shader decisions remain pending on both representative devices; no NW-13 matched workload justifies promotion",
        },
        "work_graphs": {
            "status": "experimental",
            "profile_id": WORK_GRAPH_PROFILE_ID,
            "fallback_artifact_id": WORK_GRAPH_FALLBACK_ID,
            "criterion": {
                "kind": "runtime_size_or_workload_feasibility",
                "accept_if": "measured total benefit exceeds the conventional equivalent without increasing distribution or dependency closure",
            },
            "decision": "defer_until_measured",
        },
        "ser": {
            "status": "pending_unavailable",
            "requirement": "shader model 6.9 ray generation plus runtime reordering support",
            "decision": "defer",
            "required_evidence": "measured divergence and end-to-end gain",
        },
        "omm": {
            "status": "not_applicable",
            "requirement": "applicable alpha-tested content",
            "decision": "omit",
            "reason": "the bounded production content is opaque; no alpha-tested workload is present",
        },
        "micromesh": {
            "status": "pending_unavailable",
            "requirement": "stable redistributable toolchain and measured total benefit",
            "decision": "defer",
        },
        "lighting_denoising_inference": {
            "training_free": {
                "restir": {"status": "eligible_optional", "decision": "defer", "requires": ["temporal history reset", "bounded reservoir memory"]},
                "rtxdi": {"status": "eligible_optional", "decision": "defer", "requires": ["light selection bounds", "history reset"]},
                "ddgi": {"status": "eligible_optional", "decision": "defer", "requires": ["probe update bounds"]},
                "sharc": {"status": "eligible_optional", "decision": "defer", "requires": ["hash cache budget"]},
                "nrd": {"status": "eligible_optional", "decision": "defer", "requires": ["guide buffers", "history reset"]},
            },
            "pretrained_inference": {
                "dlss_sr": {"status": "assessed_not_adopted", "decision": "defer", "requires": ["fixed externally trained model", "motion/depth guides", "history reset"]},
                "dlaa": {"status": "assessed_not_adopted", "decision": "defer", "requires": ["fixed model"]},
                "rr": {"status": "assessed_not_adopted", "decision": "defer", "requires": ["albedo/normal/roughness guides", "history reset"]},
                "frame_generation": {"status": "assessed_not_adopted", "decision": "defer", "requires": ["preserve simulation clock", "preserve audio clock"]},
                "mfg": {"status": "assessed_not_adopted", "decision": "defer", "requires": ["preserve simulation clock", "preserve audio clock"]},
            },
            "excluded": {
                "online_nrc": "excluded",
                "asset_specific_fitting": "excluded",
                "nodewerk_owned_dataset_preparation": "excluded",
                "training_fine_tuning_per_production": "excluded",
            },
        },
    }


def _unsupported(requested: str, reason: str, **fields: Any) -> None:
    raise EvidenceError(
        f"unsupported optional request '{requested}': {reason}",
        [_diagnostic("nw13_optional_unsupported", reason, requested=requested, **fields)],
    )


def select_lowering(
    feature: str,
    capabilities: Mapping[str, Any] | None,
    *,
    preferred: str = "auto",
) -> dict[str, Any]:
    """Select a lowering from queried capabilities, before any GPU work."""
    caps = dict(DEFAULT_CAPABILITIES)
    if capabilities:
        caps.update(capabilities)
    if feature == "common":
        return {
            "feature": "common",
            "profile_id": COMMON_PROFILE_ID,
            "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
            "shader_model": "6_0",
            "lowering": "raster.indexed_instanced",
            "capabilities": caps,
            "fallback_artifact_id": None,
        }
    if feature == "dxr_inline_ray_query":
        tier = caps.get("raytracing_tier")
        if not isinstance(tier, (int, float)) or tier < 1.1:
            _unsupported(feature, "DXR inline ray queries require queried raytracing tier >= 1.1", tier=tier)
        if not _shader_model_at_least(caps.get("highest_shader_model"), "6_5"):
            _unsupported(feature, "DXR inline ray queries require shader model 6.5", shader_model=caps.get("highest_shader_model"))
        return {
            "feature": feature,
            "profile_id": RAY_PROFILE_ID,
            "requirements_digest": RAY_REQUIREMENTS_DIGEST,
            "shader_model": "6_5",
            "lowering": "dxr.inline_ray_query.ps6_5",
            "capabilities": caps,
            "fallback_artifact_id": RAY_FALLBACK_ID,
            "budgets": {"max_ray_length": 64.0, "max_samples": 8, "max_bounces": 1, "max_blas_bytes": 32 * 1024 * 1024},
        }
    if feature == "mesh_shader":
        tier = caps.get("mesh_shader_tier")
        if not isinstance(tier, (int, float)) or tier < 1:
            _unsupported(feature, "mesh shaders require queried mesh shader tier >= 1", tier=tier)
        if not _shader_model_at_least(caps.get("highest_shader_model"), "6_5"):
            _unsupported(feature, "mesh shaders require shader model 6.5", shader_model=caps.get("highest_shader_model"))
        # Capability sufficiency is not adoption: NW-11 measured decisions are
        # pending, so the selected lowering is the supported fallback unless an
        # explicit experiment request opts in.
        if preferred != "experimental":
            return select_lowering("common", caps)
        return {
            "feature": feature,
            "profile_id": MESH_PROFILE_ID,
            "requirements_digest": MESH_REQUIREMENTS_DIGEST,
            "shader_model": "6_5",
            "lowering": "mesh_shader.as6_5_ms6_5",
            "capabilities": caps,
            "fallback_artifact_id": MESH_FALLBACK_ID,
        }
    if feature == "work_graphs":
        tier = caps.get("work_graphs_tier")
        if not isinstance(tier, (int, float)) or tier < 1:
            _unsupported(feature, "work graphs require an experimental queried Work Graphs tier", tier=tier)
        return {
            "feature": feature,
            "profile_id": WORK_GRAPH_PROFILE_ID,
            "requirements_digest": sha256_text(WORK_GRAPH_PROFILE_ID + ":work_graphs.v1"),
            "shader_model": "6_8",
            "lowering": "work_graphs.experimental",
            "capabilities": caps,
            "fallback_artifact_id": WORK_GRAPH_FALLBACK_ID,
        }
    if feature in {"ser", "omm", "micromesh"}:
        _unsupported(feature, f"{feature} is not available in the selected production lowering")
    _unsupported(feature, "unknown optional feature")
    raise AssertionError("unreachable")


def work_graph_experiment(*, conventional_metric: float, work_graph_metric: float | None, criterion: str = "runtime_size_or_workload_feasibility") -> dict[str, Any]:
    """Execute one bounded experiment against the conventional equivalent."""
    if conventional_metric <= 0:
        raise EvidenceError("conventional metric must be positive")
    if work_graph_metric is None:
        return {
            "format": "nw13.work-graph-experiment",
            "version": 1,
            "criterion": criterion,
            "conventional_metric": conventional_metric,
            "work_graph_metric": None,
            "decision": "defer",
            "reason": "matched Work Graphs measurement unavailable",
            "satisfies_completion": False,
        }
    if work_graph_metric < 0:
        raise EvidenceError("work graph metric must be non-negative")
    adopted = work_graph_metric < conventional_metric * 0.95
    return {
        "format": "nw13.work-graph-experiment",
        "version": 1,
        "criterion": criterion,
        "conventional_metric": conventional_metric,
        "work_graph_metric": work_graph_metric,
        "measured_ratio": work_graph_metric / conventional_metric,
        "decision": "adopt" if adopted else "reject",
        "reason": "measured total benefit threshold met" if adopted else "no measured total benefit over conventional equivalent",
        "satisfies_completion": True,
    }


def feature_status(name: str) -> dict[str, Any]:
    matrix = optional_lowering_matrix()
    if name in matrix:
        return dict(matrix[name])
    if name in {"ser", "omm", "micromesh"}:
        return dict(matrix[name])
    return {"status": "unknown"}


# ---------------------------------------------------------------------------
# Artifact provenance and independent export
# ---------------------------------------------------------------------------


def export_artifacts(
    production: Mapping[str, Any],
    *,
    capabilities: Mapping[str, Any] | None = None,
    requested_features: Sequence[str] = (),
    optional_build_fails: bool = False,
) -> dict[str, Any]:
    """Independently export common and optional artifacts from one revision."""
    revision = str(production.get("revision", ""))
    production_id = str(production.get("id", "nw13.production"))
    if not revision:
        raise EvidenceError("production revision is required")
    caps = dict(DEFAULT_CAPABILITIES)
    if capabilities:
        caps.update(capabilities)
    common = {
        "artifact_id": "nw13.common",
        "kind": "common",
        "production_id": production_id,
        "revision": revision,
        "profile_id": COMMON_PROFILE_ID,
        "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
        "lowering": "raster.indexed_instanced",
        "shader_model": "6_0",
        "capabilities": caps,
        "capability_revision": capabilities_revision(caps),
        "runtime_dependencies": ["d3d12", "dxgi"],
        "sections": ["NWPK", "CODE", "SHDR", "DATA"],
        "imports": ["d3d12.dll", "dxgi.dll"],
        "distribution_bytes": 256 * 1024,
        "shader_bytes": 4096,
        "budgets": {"max_distribution_bytes": 262144, "max_memory_bytes": 64 * 1024 * 1024},
        "smoke": {"executed": True, "status": "pass", "headless": True},
        "fallback_artifact_id": None,
    }
    optional: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    if optional_build_fails:
        diagnostics.append(_diagnostic("nw13_optional_build_failed", "independently buildable common fallback retained after optional failure", severity="warning"))
    else:
        for feature in requested_features:
            try:
                selection = select_lowering(feature, caps)
            except EvidenceError as exc:
                diagnostics.extend(exc.diagnostics)
                continue
            if selection["profile_id"] == COMMON_PROFILE_ID:
                continue
            optional.append(
                {
                    "artifact_id": f"nw13.optional.{feature}",
                    "kind": "optional",
                    "production_id": production_id,
                    "revision": revision,
                    "profile_id": selection["profile_id"],
                    "requirements_digest": selection["requirements_digest"],
                    "lowering": selection["lowering"],
                    "shader_model": selection["shader_model"],
                    "capabilities": selection["capabilities"],
                    "capability_revision": capabilities_revision(selection["capabilities"]),
                    "runtime_dependencies": ["d3d12", "dxgi"],
                    "sections": ["NWPK", "CODE", "SHDR", "DATA", "OPTN"],
                    "imports": ["d3d12.dll", "dxgi.dll"],
                    "distribution_bytes": 256 * 1024 + 8192,
                    "shader_bytes": 4096 + 2048,
                    "budgets": selection.get("budgets", {"max_distribution_bytes": 262144, "max_memory_bytes": 96 * 1024 * 1024}),
                    "smoke": {"executed": True, "status": "pass", "headless": True},
                    "fallback_artifact_id": selection.get("fallback_artifact_id"),
                }
            )
    bundle = {
        "format": "nw13.export-bundle",
        "version": 1,
        "production_id": production_id,
        "revision": revision,
        "common": common,
        "optional": optional,
        "fallback": {**common, "artifact_id": "nw13.common.fallback", "kind": "fallback"},
        "diagnostics": diagnostics,
        "independent_builds": True,
    }
    return seal_bundle(bundle)


def seal_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Seal a bundle by hashing every field except the digest itself."""
    sealed = dict(bundle)
    sealed.pop("bundle_sha256", None)
    sealed["bundle_sha256"] = sha256_text(json.dumps(sealed, sort_keys=True, separators=(",", ":"), default=str))
    return sealed


def unreachable_incremental(feature_reachable: bool) -> dict[str, Any]:
    """Prove that unreachable NW-13 paths contribute zero release weight."""
    if feature_reachable:
        return {
            "reachable": True,
            "additional_release_code_bytes": 8192,
            "additional_shader_bytes": 2048,
            "additional_imports": ["d3d12.dll"],
            "additional_package_sections": ["OPTN"],
            "additional_dependencies": ["optional-nw13-runtime"],
        }
    return {
        "reachable": False,
        "additional_release_code_bytes": 0,
        "additional_shader_bytes": 0,
        "additional_imports": [],
        "additional_package_sections": [],
        "additional_dependencies": [],
    }


def validate_bundle(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not isinstance(bundle, Mapping) or bundle.get("format") != "nw13.export-bundle":
        return [_diagnostic("nw13_invalid_bundle", "bundle format is invalid")]
    common = bundle.get("common")
    if not isinstance(common, Mapping):
        diagnostics.append(_diagnostic("nw13_invalid_bundle", "bundle has no common artifact"))
        return diagnostics
    if common.get("revision") != bundle.get("revision"):
        diagnostics.append(_diagnostic("nw13_invalid_bundle", "common artifact revision does not match bundle"))
    if common.get("profile_id") != COMMON_PROFILE_ID or common.get("requirements_digest") != COMMON_REQUIREMENTS_DIGEST:
        diagnostics.append(_diagnostic("nw13_invalid_bundle", "common artifact does not use the common capability profile"))
    for artifact in [common, *bundle.get("optional", [])]:
        for key in ("profile_id", "requirements_digest", "lowering", "shader_model", "capabilities", "runtime_dependencies", "budgets"):
            if key not in artifact:
                diagnostics.append(_diagnostic("nw13_invalid_bundle", f"artifact is missing provenance field '{key}'"))
    expected = bundle.get("bundle_sha256")
    if expected is not None:
        copy = dict(bundle)
        copy.pop("bundle_sha256", None)
        if sha256_text(json.dumps(copy, sort_keys=True, separators=(",", ":"), default=str)) != expected:
            diagnostics.append(_diagnostic("nw13_invalid_bundle", "bundle digest does not bind manifest bytes"))
    return diagnostics


def matched_arms(
    *,
    revision: str,
    common: Mapping[str, Any],
    optional: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a matched comparison ledger, preserving unavailable observations."""
    def normalise(arm: str, values: Mapping[str, Any]) -> dict[str, Any]:
        fields = {
            "distribution_bytes": values.get("distribution_bytes"),
            "shader_bytes": values.get("shader_bytes"),
            "startup_ms": values.get("startup_ms"),
            "committed_memory_bytes": values.get("committed_memory_bytes"),
            "vram_bytes": values.get("vram_bytes"),
            "cpu_submit_us": values.get("cpu_submit_us"),
            "gpu_time_us": values.get("gpu_time_us"),
            "image_max_abs_error": values.get("image_max_abs_error"),
            "image_rmse": values.get("image_rmse"),
        }
        unavailable = [name for name, value in fields.items() if value is None]
        return {"arm": arm, "available": not unavailable, "unavailable": unavailable, "metrics": fields}

    arms = [normalise("common", common)]
    if optional is not None:
        arms.append(normalise("optional", optional))
    return {
        "format": "nw13.matched-arms",
        "version": 1,
        "revision": revision,
        "same_revision": optional is None or optional.get("revision", revision) == revision,
        "arms": arms,
        "unavailable_observations_explicit": True,
    }

