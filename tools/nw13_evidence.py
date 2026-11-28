#!/usr/bin/env python3
"""NW-13 optional-lowering qualification, artifact provenance and evidence.

This module never executes GPU work.  It decides eligibility from queried
capabilities, binds every optional artifact to provenance, bounds declared
budgets, and encodes the measured/unavailable distinction required by ADR 0009.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Mapping, Sequence

import nwp_package as nwp
import nw13_work_graph_protocol as work_graph


COMMON_PROFILE_ID = nwp.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST

MESH_PROFILE_ID = "nw.optional.d3d12.mesh_shaders.ms6_5.v1"
MESH_REQUIREMENTS_DIGEST = "sha256:v1:a522ce6f0f5163ecbcf6045ab8c52fe72ee9855c902db40b2094ffc530bc4d62"
MESH_FALLBACK_ID = "nw11.fallback.mesh.indexed.d3d12.fl12_0.sm6_0.v1"
RAY_PROFILE_ID = "nw.optional.d3d12.nw13.inline_ray.cs6_5.v1"
RAY_REQUIREMENTS_DIGEST = "sha256:v1:" + hashlib.sha256(b"NW-13 inline ray v1: FL12.0, SM6.5, DXR1.1, swept procedural AABB").hexdigest()
RAY_FALLBACK_ID = "nw13.common.fallback"

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

SHADER_MODEL_ORDER = {f"6_{minor}": 60 + minor for minor in range(10)}


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
            "artifact_id": "nw13.common",
            "surface_realization": "compute.bounded_solid_trace",
            "rtx_required": False,
            "selection_basis": "implemented bounded compute tracing without RTX; physical performance comparison remains pending",
        },
        "dxr_inline_ray_query": {
            "status": "capability_gated_optional",
            "lowering": "dxr.inline_ray_query.cs6_5",
            "matched_physical_acceptance": None,
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
            "decision_record": "benchmarks/nw11/decisions/mesh-shader.v1.json",
            "reason": "NW-11 mesh-shader decisions remain pending on both representative devices; no NW-13 matched workload justifies promotion",
        },
        "work_graphs": {
            "status": "experimental",
            "profile_id": WORK_GRAPH_PROFILE_ID,
            "fallback_artifact_id": WORK_GRAPH_FALLBACK_ID,
            "criterion": {
                "kind": "matched_gpu_dispatch_median",
                "accept_if": work_graph.CRITERION,
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
            "lowering": "compute.bounded_solid_trace",
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
            "lowering": "dxr.inline_ray_query.cs6_5",
            "capabilities": caps,
            "fallback_artifact_id": RAY_FALLBACK_ID,
            "budgets": {"max_ray_length": 64.0, "max_samples": 1, "max_bounces": 1, "max_as_bytes": 16 * 1024 * 1024, "max_as_boxes": 32},
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
        if not _shader_model_at_least(caps.get("highest_shader_model"), "6_8"):
            _unsupported(feature, "work graphs require queried shader model 6.8")
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


def work_graph_experiment(*, conventional_metric: float, work_graph_metric: float | None, criterion: str = work_graph.CRITERION) -> dict[str, Any]:
    """Project supplied medians; the physical runner separately verifies raw paired samples."""
    if criterion != work_graph.CRITERION:
        raise EvidenceError("Work Graphs criterion cannot be changed for the declared experiment")
    try:
        work_graph.median_decision(conventional_metric, conventional_metric)
    except work_graph.ObservationError as exc:
        raise EvidenceError(str(exc)) from exc
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
    try:
        decision = work_graph.median_decision(conventional_metric, work_graph_metric)
    except work_graph.ObservationError as exc:
        raise EvidenceError(str(exc)) from exc
    return {
        "format": "nw13.work-graph-experiment",
        "version": 1,
        "criterion": criterion,
        "conventional_metric": conventional_metric,
        "work_graph_metric": work_graph_metric,
        "measured_ratio": work_graph_metric / conventional_metric,
        "decision": decision,
        "reason": "measured dispatch threshold met" if decision == "adopt" else "strict dispatch improvement threshold not met",
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
    common_observation: Mapping[str, Any] | None = None,
    fallback_observation: Mapping[str, Any] | None = None,
    optional_observations: Mapping[str, Mapping[str, Any]] | None = None,
    independent_builds: bool = False,
) -> dict[str, Any]:
    """Bind exported artifact provenance to measured observations.

    Build sizes, package sections, imports and smoke results are never invented.
    Callers that ran independent exports supply ``common_observation`` and
    ``optional_observations``; otherwise those fields stay ``None`` and smoke is
    recorded as ``unavailable`` so committed evidence cannot claim an
    unexecuted pass.
    """
    revision = production.get("revision")
    production_id = str(production.get("id", "nw13.production"))
    if not revision and (common_observation or optional_observations or fallback_observation):
        raise EvidenceError("measured observations require a source revision")
    caps = dict(DEFAULT_CAPABILITIES)
    if capabilities:
        caps.update(capabilities)
    common_budgets = {"max_memory_bytes": 64 * 1024 * 1024}
    common_obs = _artifact_observation(common_observation, common_budgets)
    common = {
        "artifact_id": "nw13.common",
        "kind": "common",
        "production_id": production_id,
        "revision": revision,
        "profile_id": COMMON_PROFILE_ID,
        "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
        "lowering": "compute.bounded_solid_trace",
        "shader_model": "6_0",
        "capabilities": caps,
        "capability_revision": capabilities_revision(caps),
        "runtime_dependencies": ["d3d12", "dxgi"],
        "budgets": common_budgets,
        "fallback_artifact_id": None,
        **common_obs,
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
            optional_budgets = {
                "max_memory_bytes": 96 * 1024 * 1024,
                **selection.get("budgets", {}),
            }
            observation = _artifact_observation(
                (optional_observations or {}).get(feature),
                optional_budgets,
            )
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
                    "budgets": optional_budgets,
                    "fallback_artifact_id": selection.get("fallback_artifact_id"),
                    **observation,
                }
            )
    bundle = {
        "format": "nw13.export-bundle",
        "version": 1,
        "production_id": production_id,
        "revision": revision,
        "source_state": "clean_revision" if revision else "uncommitted",
        "implementation_base": production.get("implementation_base"),
        "common": common,
        "optional": optional,
        "fallback": {
            **common,
            **_artifact_observation(fallback_observation, common_budgets),
            "artifact_id": "nw13.common.fallback",
            "kind": "fallback",
        },
        "diagnostics": diagnostics,
        "independent_builds": bool(independent_builds),
    }
    return seal_bundle(bundle)


def seal_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Seal a bundle by hashing every field except the digest itself."""
    sealed = dict(bundle)
    sealed.pop("bundle_sha256", None)
    sealed["bundle_sha256"] = sha256_text(json.dumps(sealed, sort_keys=True, separators=(",", ":"), default=str))
    return sealed


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceError(f"measurement '{name}' must be a non-negative integer or null")
    return value


def unavailable_smoke() -> dict[str, Any]:
    """Explicitly represent a smoke run that was not executed."""
    return {"executed": False, "status": "unavailable", "headless": None}


def smoke_observation(executable: str, arguments: Sequence[str] = ("--frames", "1"), *, timeout: float = 180.0, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run a real artifact smoke and record the observation, including failure."""
    try:
        completed = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "executed": True,
            "status": "error",
            "headless": "--headless" in arguments,
            "error": str(exc),
        }
    observation = {
        "executed": True,
        "status": "pass" if completed.returncode == 0 else "fail",
        "headless": "--headless" in arguments,
        "returncode": completed.returncode,
        "stdout_sha256": sha256_text(completed.stdout),
        "stderr_sha256": sha256_text(completed.stderr),
    }

    for line in completed.stdout.splitlines():
        if line.startswith("NW13_STARTUP_US="):
            micros = line.removeprefix("NW13_STARTUP_US=")
            if micros.isdecimal():
                observation["startup_ms"] = int(micros) / 1000
        elif line.startswith("NW13_MANAGED_COMMITTED_BYTES="):
            value = line.removeprefix("NW13_MANAGED_COMMITTED_BYTES=")
            if value.isdecimal():
                observation["committed_memory_bytes"] = int(value)
        elif line.startswith("NW13_AS_COMMITTED_BYTES="):
            value = line.removeprefix("NW13_AS_COMMITTED_BYTES=")
            if value.isdecimal():
                observation["acceleration_structure_bytes"] = int(value)
        elif line.startswith("NW07_IMAGE_FNV64=") or line.startswith("NW05_IMAGE_FNV64="):
            observation["image_hash"] = line.split("=", 1)[1]
    return observation


def _normalise_smoke(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value.get("executed"):
        return unavailable_smoke()
    status = value.get("status")
    if status not in {"pass", "fail", "error"}:
        raise EvidenceError("executed smoke observation must record pass, fail or error")
    result = {"executed": True, "status": status, "headless": value.get("headless")}
    for key in ("returncode", "stdout_sha256", "stderr_sha256", "error", "startup_ms", "image_hash", "acceleration_structure_bytes", "capture", "capture_sha256", "committed_memory_bytes"):
        if key in value:
            result[key] = value[key]
    return result


def _budget_checks(
    observation: Mapping[str, Any], budgets: Mapping[str, Any]
) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    def check(name: str, observed: Any, limit: Any) -> None:
        if limit is None:
            checks[name] = {"limit": None, "observed": observed, "status": "unavailable"}
        elif observed is None:
            checks[name] = {"limit": limit, "observed": None, "status": "unavailable"}
        else:
            checks[name] = {
                "limit": limit,
                "observed": observed,
                "status": "passed" if observed <= limit else "exceeded",
            }

    check("committed_memory_bytes", observation.get("committed_memory_bytes"), budgets.get("max_memory_bytes"))
    if "max_as_bytes" in budgets:
        check("acceleration_structure_bytes", observation["smoke"].get("acceleration_structure_bytes"), budgets["max_as_bytes"])
    return checks


def _artifact_observation(
    observation: Mapping[str, Any] | None,
    budgets: Mapping[str, Any],
) -> dict[str, Any]:
    observation = dict(observation or {})
    result: dict[str, Any] = {
        "distribution_bytes": _optional_nonnegative_int(observation.get("distribution_bytes"), "distribution_bytes"),
        "shader_bytes": _optional_nonnegative_int(observation.get("shader_bytes"), "shader_bytes"),
        "sections": list(observation["sections"]) if observation.get("sections") is not None else None,
        "imports": list(observation["imports"]) if observation.get("imports") is not None else None,
        "smoke": _normalise_smoke(observation.get("smoke")),
    }
    for key in (
        "size_accounting",
        "startup_ms",
        "committed_memory_bytes",
        "vram_bytes",
        "cpu_submit_us",
        "gpu_time_us",
        "image_max_abs_error",
        "image_rmse",
        "image_within_tolerance",
        "film_lowering",
        "executable_sha256",
        "package_sha256",
        "audio_song_sha256",
        "manifest_sha256",
        "dependency_identities",
    ):
        result[key] = observation.get(key)
    measured = result["distribution_bytes"] is not None and result["shader_bytes"] is not None
    executed = bool(result["smoke"]["executed"])
    passed = executed and result["smoke"]["status"] == "pass" and result["smoke"].get("returncode") == 0
    result["measurement_status"] = (
        "failed" if executed and not passed else
        "measured" if measured and passed else
        "partial" if measured or executed else "unavailable"
    )
    result["budget_checks"] = _budget_checks(result, budgets)
    observed_checks = [check for check in result["budget_checks"].values() if check["status"] != "unavailable"]
    if not observed_checks:
        result["within_budgets"] = None
    else:
        result["within_budgets"] = all(check["status"] == "passed" for check in observed_checks)
    return result


def unreachable_incremental(
    feature_reachable: bool,
    *,
    observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare unrelated same-revision exports, or record unavailability.

    Zero additional release weight is only asserted when a matched pair of
    unrelated exports was actually inspected.  Without that observation every
    byte/section/import/dependency count is ``None`` and the measurement status
    is explicitly ``unavailable``.
    """
    if observations is None:
        return {
            "reachable": feature_reachable,
            "measurement_status": "unavailable",
            "additional_release_code_bytes": None,
            "additional_shader_bytes": None,
            "additional_imports": None,
            "additional_package_sections": None,
            "additional_dependencies": None,
        }
    return {
        "reachable": feature_reachable,
        "measurement_status": "measured",
        "additional_release_code_bytes": _optional_nonnegative_int(
            observations.get("additional_release_code_bytes"), "additional_release_code_bytes"
        ),
        "additional_shader_bytes": _optional_nonnegative_int(
            observations.get("additional_shader_bytes"), "additional_shader_bytes"
        ),
        "additional_imports": list(observations.get("additional_imports", [])),
        "additional_package_sections": list(observations.get("additional_package_sections", [])),
        "additional_dependencies": list(observations.get("additional_dependencies", [])),
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
    fallback = bundle.get("fallback")
    if not isinstance(fallback, Mapping):
        diagnostics.append(_diagnostic("nw13_invalid_bundle", "bundle has no common fallback record"))
        return diagnostics
    for artifact in [common, fallback, *bundle.get("optional", [])]:
        for key in ("profile_id", "requirements_digest", "lowering", "shader_model", "capabilities", "runtime_dependencies", "budgets"):
            if key not in artifact:
                diagnostics.append(_diagnostic("nw13_invalid_bundle", f"artifact is missing provenance field '{key}'"))
        smoke = artifact.get("smoke")
        if not isinstance(smoke, Mapping):
            diagnostics.append(_diagnostic("nw13_invalid_bundle", "artifact is missing a smoke observation"))
            continue
        if artifact.get("revision") != bundle.get("revision"):
            diagnostics.append(_diagnostic("nw13_invalid_bundle", "artifact revision does not match bundle"))
        if smoke.get("executed") and (smoke.get("status") != "pass" or smoke.get("returncode") != 0):
            diagnostics.append(_diagnostic(
                "nw13_smoke_failed", "executed artifact smoke did not succeed",
                artifact_id=artifact.get("artifact_id"), returncode=smoke.get("returncode"),
            ))
        status = artifact.get("measurement_status")
        if status == "measured":
            if artifact.get("distribution_bytes") is None or artifact.get("shader_bytes") is None:
                diagnostics.append(_diagnostic("nw13_invalid_bundle", "measured artifact lacks byte observations"))
            if not smoke.get("executed"):
                diagnostics.append(_diagnostic("nw13_invalid_bundle", "measured artifact lacks an executed smoke"))
        elif not smoke.get("executed") and smoke.get("status") != "unavailable":
            diagnostics.append(_diagnostic("nw13_invalid_bundle", "unexecuted smoke must be marked unavailable"))
        if artifact.get("image_within_tolerance") is False:
            diagnostics.append(_diagnostic("nw13_image_mismatch", "matched artifact exceeds its image tolerance"))
        for name, check in (artifact.get("budget_checks") or {}).items():
            # Historic reports may still contain the retired byte checks.
            # Integrity, real smoke and runtime budgets remain mandatory.
            if name not in {"distribution_bytes", "shader_bytes"} and check.get("status") == "exceeded":
                diagnostics.append(
                    _diagnostic(
                        "nw13_budget_exceeded",
                        f"measured {name} exceeds its declared budget",
                        artifact_id=artifact.get("artifact_id"),
                        observed=check.get("observed"),
                        limit=check.get("limit"),
                    )
                )
    if bundle.get("independent_builds") and common.get("measurement_status") != "measured":
        diagnostics.append(
            _diagnostic(
                "nw13_invalid_bundle",
                "independent_builds cannot be claimed without measured common bytes and an executed smoke",
            )
        )
    if bundle.get("independent_observations") is not None:
        pair = bundle["independent_observations"]
        first, second = pair.get("a", {}), pair.get("b", {})
        identity = ("executable_sha256", "package_sha256", "manifest_file_sha256", "dependency_identities")
        if not bundle.get("independent_builds") or any(
            key not in first or key not in second or first[key] != second[key] for key in identity
        ):
            diagnostics.append(_diagnostic("nw13_rebuild_identity_mismatch", "independent artifact identities differ"))
        if fallback.get("measurement_status") != "measured":
            diagnostics.append(_diagnostic("nw13_fallback_unverified", "native evidence requires an independent fallback smoke"))
    expected = bundle.get("bundle_sha256")
    if not isinstance(expected, str):
        diagnostics.append(_diagnostic("nw13_invalid_bundle", "bundle digest is required"))
    else:
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

