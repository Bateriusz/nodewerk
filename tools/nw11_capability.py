#!/usr/bin/env python3
"""NW-11 capability profiles, requirements digests, and eligibility.

Eligibility is decided exclusively from values returned by documented D3D12
``CheckFeatureSupport`` queries.  GPU marketing names, driver names, and GPU
generation strings are recorded as provenance metadata but are never used to
decide whether an optional profile may run.

The module is stdlib-only so the exact same implementation can be executed by
the clean-machine validation bundle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from nw11_contract import (  # type: ignore[import-not-found]
    AGILITY_SDK_MINIMUM_VERSION,
    AGILITY_SDK_REQUIRED,
    COMMON_PROFILE_ID,
    COMMON_REQUIREMENTS_DIGEST,
    COMPLETE_DISTRIBUTION_CEILING_BYTES,
    DEVICE_TIERS,
    EVIDENCE_STATUSES,
    IMPLEMENTATION_BASE,
    MIN_WINDOWS_BUILD,
    NVIDIA_DRIVER_FLOOR,
    PROFILE_DEFINITIONS,
    PRODUCTION_FORMATS,
    SHADER_MODEL_ORDER,
    STRETCH_DISTRIBUTION_CEILING_BYTES,
    TASK,
    TOOLCHAIN_DXC_MEASURED_VERSION,
    TOOLCHAIN_DXC_VERSION,
    canonical_json_bytes,
    digest_value,
    frozen_runtime_requirements,
    frozen_toolchain_requirements,
    profile_definition,
    sha256_bytes,
)

PROFILE_FORMAT = "nw11-capability-profile"
PROFILE_VERSION = 1


class CapabilityProfileError(ValueError):
    """Raised when a profile, observation, or eligibility decision is invalid."""


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityProfileError(f"{label} must be a JSON object")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityProfileError(f"{label} must be a non-empty string")
    return value.strip()


def _require_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CapabilityProfileError(f"{label} must be an integer")
    return value


def _production_format_value(query_id: str, observation: Mapping[str, Any]) -> str | None:
    # D3D12_FORMAT_SUPPORT1/2 requirements for shared production loads and typed stores.
    buffer, index, texture2d, target, depth = 0x1, 0x4, 0x20, 0x4000, 0x10000
    shader_resource = 0x100  # SHADER_LOAD (sampling/gather alone is insufficient)
    typed_uav, typed_store = 0x2000000, 0x80
    required, srv, uav = {
        "DXGI_FORMAT_R8G8B8A8_UNORM": (texture2d | target, True, True),
        "DXGI_FORMAT_R32G32B32A32_FLOAT": (buffer | texture2d | target, True, True),
        "DXGI_FORMAT_R32_UINT": (buffer | index, True, True),
        "DXGI_FORMAT_D32_FLOAT": (texture2d | depth, False, False),
    }[query_id]
    queries = _require_mapping(observation.get("format_queries", {}), "observation.format_queries")
    if query_id not in queries:
        return None
    result = _require_mapping(queries[query_id], query_id)
    hresult = _require_int(result.get("hresult"), f"{query_id}.hresult")
    if not -(2**31) <= hresult < 2**31:
        raise CapabilityProfileError(f"{query_id} HRESULT is out of range")
    if hresult < 0:
        return None
    support1 = _require_int(result.get("support1"), f"{query_id}.support1")
    support2 = _require_int(result.get("support2"), f"{query_id}.support2")
    if not (0 <= support1 < 2**32 and 0 <= support2 < 2**32):
        raise CapabilityProfileError(f"{query_id} support masks are out of range")
    supported = ((support1 & required) == required
                 and (not srv or support1 & shader_resource)
                 and (not uav or ((support1 & typed_uav) and (support2 & typed_store))))
    return "production_usage_supported" if supported else "unsupported"


def requirements_digest_inputs(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact projection hashed into a requirements digest.

    Device names, evidence status, budgets, and fallback paths are deliberately
    excluded: the digest identifies the API/shader/runtime/toolchain
    requirements, not the currently measured hardware or bytes.
    """
    queried = profile.get("queried_capabilities")
    if not isinstance(queried, list):
        raise CapabilityProfileError("queried_capabilities must be an array")
    normalized_queries = []
    for query in queried:
        entry = _require_mapping(query, "queried capability")
        normalized_queries.append(
            {
                "query_id": _require_str(entry.get("query_id"), "query_id"),
                "d3d12_query": _require_str(entry.get("d3d12_query"), "d3d12_query"),
                "method": _require_str(entry.get("method"), "method"),
                "member": entry.get("member"),
                "accepted_values": list(entry.get("accepted_values") or []),
                "minimum_rank": _require_int(entry.get("minimum_rank", 0), "minimum_rank"),
            }
        )
    targets = profile.get("shader_targets")
    if not isinstance(targets, list) or not targets:
        raise CapabilityProfileError("shader_targets must be a non-empty array")
    return {
        "profile_id": _require_str(profile.get("profile_id"), "profile_id"),
        "profile_kind": _require_str(profile.get("profile_kind"), "profile_kind"),
        "queried_capabilities": normalized_queries,
        "shader_targets": [str(item) for item in targets],
        "runtime": profile.get("runtime"),
        "toolchain": profile.get("toolchain"),
    }


def compute_requirements_digest(profile: Mapping[str, Any]) -> str:
    """Return the requirements digest a profile must publish.

    The common profile identity is inherited unchanged from the completed
    NW-01/NW-03..NW-07 contract, so its digest is the frozen
    ``COMMON_REQUIREMENTS_DIGEST`` value rather than a re-hash of the
    NW-11 presentation of those same requirements.  Optional profiles are
    content-addressed: their digest is a canonical hash over the exact
    queried capabilities, shader targets, runtime, and toolchain fields.
    """
    if profile.get("profile_kind") == "common":
        return COMMON_REQUIREMENTS_DIGEST
    return digest_value(requirements_digest_inputs(profile))


def _expected_queries(profile_id: str) -> list[dict[str, Any]]:
    return [dict(item) for item in profile_definition(profile_id)["queries"]]


def load_profile(path: Path | str) -> dict[str, Any]:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_profile(record)


def validate_profile(record: Mapping[str, Any]) -> dict[str, Any]:
    profile = _require_mapping(record, "profile")
    if profile.get("format") != PROFILE_FORMAT:
        raise CapabilityProfileError("profile format is not nw11-capability-profile")
    if profile.get("version") != PROFILE_VERSION:
        raise CapabilityProfileError("profile version is not 1")
    if profile.get("task") != TASK:
        raise CapabilityProfileError("profile task is not NW-11")
    if profile.get("implementation_base") != IMPLEMENTATION_BASE:
        raise CapabilityProfileError("profile is not bound to the NW-11 implementation base")

    profile_id = _require_str(profile.get("profile_id"), "profile_id")
    definition = profile_definition(profile_id)
    if _require_str(profile.get("profile_kind"), "profile_kind") != definition["profile_kind"]:
        raise CapabilityProfileError(
            f"profile '{profile_id}' declares the wrong profile_kind"
        )

    expected_queries = _expected_queries(profile_id)
    observed_queries = profile.get("queried_capabilities")
    if observed_queries != expected_queries:
        raise CapabilityProfileError(
            f"profile '{profile_id}' must declare the exact queried D3D12 capabilities"
        )
    if list(profile.get("shader_targets") or []) != list(definition["shader_targets"]):
        raise CapabilityProfileError(
            f"profile '{profile_id}' must declare the exact shader targets"
        )

    eligibility = _require_mapping(profile.get("eligibility"), "eligibility")
    if eligibility.get("source") != "queried_capabilities":
        raise CapabilityProfileError("eligibility must be determined from queried capabilities")
    if eligibility.get("gpu_marketing_name_used") is not False:
        raise CapabilityProfileError("GPU marketing names must never decide eligibility")

    runtime = _require_mapping(profile.get("runtime"), "runtime")
    if runtime != frozen_runtime_requirements():
        raise CapabilityProfileError("profile runtime requirements differ from the frozen contract")
    toolchain = _require_mapping(profile.get("toolchain"), "toolchain")
    if toolchain != frozen_toolchain_requirements():
        raise CapabilityProfileError("profile toolchain requirements differ from the frozen contract")

    fallback = profile.get("fallback_artifact_id")
    if definition["profile_kind"] == "common":
        if fallback is not None:
            raise CapabilityProfileError("the common profile must not declare a fallback")
        if profile.get("requirements_digest") != COMMON_REQUIREMENTS_DIGEST:
            raise CapabilityProfileError("the common profile changed the shared requirements digest")
    else:
        if fallback != definition["fallback_artifact_id"]:
            raise CapabilityProfileError(
                f"profile '{profile_id}' must declare the frozen common fallback identity"
            )

    digest = _require_str(profile.get("requirements_digest"), "requirements_digest")
    # Recompute for both kinds so the published digest is always verifiable.
    # For the common profile this intentionally returns the inherited NW-07
    # identity; for optional profiles it hashes the actual profile content.
    computed = compute_requirements_digest(profile)
    if digest != computed:
        raise CapabilityProfileError(
            f"profile '{profile_id}' requirements digest does not match its content",
        )

    fallback_artifact = profile.get("fallback_artifact")
    if definition["profile_kind"] == "optional":
        fallback_record = _require_mapping(
            fallback_artifact, "fallback_artifact"
        )
        if fallback_record.get("artifact_id") != definition["fallback_artifact_id"]:
            raise CapabilityProfileError(
                f"profile '{profile_id}' declares the wrong fallback artifact identity"
            )
        if fallback_record.get("profile_id") != COMMON_PROFILE_ID:
            raise CapabilityProfileError(
                f"profile '{profile_id}' fallback must use the common profile"
            )
        if fallback_record.get("requirements_digest") != COMMON_REQUIREMENTS_DIGEST:
            raise CapabilityProfileError(
                f"profile '{profile_id}' fallback requirements digest is stale"
            )
        if fallback_record.get("separately_exported") is not True or fallback_record.get(
            "separately_tested"
        ) is not True:
            raise CapabilityProfileError(
                f"profile '{profile_id}' fallback must be separately exported and tested"
            )
        if fallback_record.get("optional_payloads") != []:
            raise CapabilityProfileError(
                f"profile '{profile_id}' fallback must not carry optional payloads"
            )
    elif fallback_artifact is not None:
        raise CapabilityProfileError("the common profile must not declare a fallback")

    status = profile.get("evidence_status")
    allowed_statuses = set(EVIDENCE_STATUSES)
    if definition["profile_kind"] == "common":
        # "verified" is retained for compatibility with historical profile
        # documents, but the published common profile must not use it until
        # physical common-artifact correctness coverage exists on both targets.
        allowed_statuses.add("verified")
    if status not in allowed_statuses:
        raise CapabilityProfileError("profile evidence_status is not a known NW-11 status")

    budgets = _require_mapping(profile.get("artifact_budget"), "artifact_budget")
    if budgets.get("distribution_size_policy") != "gated":
        raise CapabilityProfileError(
            "distribution size must be a gated predeclared budget"
        )
    for key in ("complete_distribution_bytes_ceiling", "incremental_distribution_bytes_ceiling"):
        if key not in budgets:
            raise CapabilityProfileError(f"profile artifact_budget.{key} is required")
    if int(budgets["complete_distribution_bytes_ceiling"]) > COMPLETE_DISTRIBUTION_CEILING_BYTES:
        raise CapabilityProfileError(
            "profile weakened the complete distribution ceiling"
        )
    expected_incremental = int(definition["incremental_budget_bytes"])
    if int(budgets["incremental_distribution_bytes_ceiling"]) != expected_incremental:
        raise CapabilityProfileError(
            f"profile '{profile_id}' changed its frozen incremental byte budget"
        )

    device_coverage = profile.get("device_coverage")
    if not isinstance(device_coverage, list) or not device_coverage:
        raise CapabilityProfileError("device_coverage must be a non-empty array")
    for device in device_coverage:
        entry = _require_mapping(device, "device coverage")
        tier = entry.get("device_tier")
        if tier not in DEVICE_TIERS:
            raise CapabilityProfileError(f"unknown device tier '{tier}'")
        if entry.get("status") not in EVIDENCE_STATUSES:
            raise CapabilityProfileError("device coverage status is not a known NW-11 status")

    return dict(profile)


def _rank(value: str, accepted_values: list[str], minimum_rank: int) -> int | None:
    full_order = list(SHADER_MODEL_ORDER)
    for candidate in accepted_values:
        if candidate not in full_order:
            full_order.append(candidate)
    if value not in full_order:
        return None
    rank = full_order.index(value)
    minimum = max(minimum_rank, full_order.index(accepted_values[0]))
    return rank if rank >= minimum else None


def physical_device_tier(key: str) -> str | None:
    """Classify physical coverage independently of feature eligibility."""
    return next((tier for tier, board in DEVICE_TIERS.items()
                 if board["device_key"] == key), None)


def _runtime_diagnostics(observation: Mapping[str, Any], vendor_id: int) -> list[dict[str, Any]]:
    diagnostics = []
    os_version = re.fullmatch(r"10\.0\.(\d+)(?:\.\d+)?", str(observation.get("os_build")))
    if os_version is None:
        diagnostics.append({"code": "NW11-RUNTIME-PENDING", "field": "os_build"})
    elif int(os_version[1]) < MIN_WINDOWS_BUILD:
        diagnostics.append({"code": "NW11-RUNTIME-UNSUPPORTED", "field": "os_build",
                            "minimum": MIN_WINDOWS_BUILD})
    runtime = _require_mapping(observation.get("runtime", {}), "observation.runtime")
    architecture = runtime.get("native_architecture")
    if architecture in (None, "unavailable"):
        diagnostics.append({"code": "NW11-RUNTIME-PENDING", "field": "native_architecture"})
    elif architecture != "x64":
        diagnostics.append({"code": "NW11-RUNTIME-UNSUPPORTED", "field": "native_architecture"})
    for field in ("d3d12_dll_version", "dxgi_dll_version"):
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(runtime.get(field))):
            diagnostics.append({"code": "NW11-RUNTIME-PENDING", "field": field})
    if vendor_id == 0x10DE:
        driver = str(observation.get("driver_version"))
        # The Windows class registry reports e.g. 32.0.16.1088 for 610.88.
        windows_driver = re.fullmatch(r"\d+\.\d+\.(\d+)\.(\d{4})", driver)
        if windows_driver:
            digits = (windows_driver[1] + windows_driver[2])[-5:]
            driver = f"{digits[:3]}.{digits[3:]}"
        if not re.fullmatch(r"\d+\.\d{2}", driver):
            diagnostics.append({"code": "NW11-RUNTIME-PENDING", "field": "driver_version"})
        elif tuple(map(int, driver.split("."))) < tuple(map(int, NVIDIA_DRIVER_FLOOR.split("."))):
            diagnostics.append({"code": "NW11-RUNTIME-UNSUPPORTED", "field": "driver_version",
                                "minimum": NVIDIA_DRIVER_FLOOR})
    return diagnostics


def evaluate_observation(
    profile: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one capability observation against a validated profile.

    A missing queried value is ``pending`` (coverage is not established), a
    present-but-insufficient value is ``unsupported``, and an explicit
    initialization failure is ``failed``.  Only the all-queries-satisfied case
    is ``eligible``.
    """
    record = validate_profile(profile)
    observation = _require_mapping(observation, "observation")
    initialization = _require_mapping(observation.get("initialization", {}), "observation.initialization")
    if initialization.get("status") == "failed" and "adapter" not in observation:
        return {"status": "failed", "profile_id": record["profile_id"],
                "requirements_digest": record["requirements_digest"],
                "diagnostics": [dict(initialization)]}
    adapter = _require_mapping(observation.get("adapter"), "observation.adapter")
    vendor_id = _require_int(adapter.get("vendor_id"), "adapter.vendor_id")
    device_id = _require_int(adapter.get("device_id"), "adapter.device_id")
    driver = _require_str(observation.get("driver_version"), "driver_version")
    os_build = _require_str(observation.get("os_build"), "os_build")
    features = _require_mapping(observation.get("features"), "observation.features")

    if initialization.get("status") == "failed":
        return {
            "profile_id": record["profile_id"],
            "requirements_digest": record["requirements_digest"],
            "status": "failed",
            "diagnostics": [
                {
                    "code": "NW11-INITIALIZATION-FAILED",
                    "stage": str(initialization.get("stage", "unknown")),
                    "hresult": initialization.get("hresult"),
                    "message": str(
                        initialization.get(
                            "message", "optional feature initialization failed"
                        )
                    ),
                }
            ],
            "device_key": device_key(vendor_id, device_id),
            "environment_key": environment_key(
                vendor_id,
                device_id,
                driver,
                os_build,
                observation.get("toolchain_identity"),
            ),
            "adapter": {
                "vendor_id": vendor_id,
                "device_id": device_id,
                "adapter_luid": adapter.get("adapter_luid"),
                "adapter_name_metadata_only": adapter.get("adapter_name"),
            },
            "queried": {},
            "marketing_name_used_for_eligibility": False,
            "satisfies_optional_profile": False,
        }

    from nw11_host import qualify
    provenance = qualify(observation.get("host_observation") or {}, adapter)
    diagnostics = _runtime_diagnostics(observation, vendor_id)
    queried: dict[str, Any] = {}
    pending = any(item["code"] == "NW11-RUNTIME-PENDING" for item in diagnostics)
    for requirement in record["queried_capabilities"]:
        query_id = requirement["query_id"]
        value = (_production_format_value(query_id, observation)
                 if query_id in PRODUCTION_FORMATS else features.get(query_id))
        if value is None:
            pending = True
            diagnostics.append(
                {
                    "code": "NW11-CAPABILITY-PENDING",
                    "query_id": query_id,
                    "d3d12_query": requirement["d3d12_query"],
                    "message": f"capability '{query_id}' was not queried by the probe",
                }
            )
            continue
        queried[query_id] = value
        accepted_values = list(requirement.get("accepted_values") or [])
        minimum_rank = requirement["minimum_rank"]
        if query_id == "raytracing_tier":
            tier = _require_int(value, "raytracing_tier")
            rank = tier if tier >= minimum_rank else None
        else:
            rank = _rank(str(value), accepted_values, minimum_rank)
        if rank is None:
            diagnostics.append(
                {
                    "code": "NW11-CAPABILITY-UNSUPPORTED",
                    "query_id": query_id,
                    "observed": value,
                    "accepted_values": accepted_values,
                    "d3d12_query": requirement["d3d12_query"],
                    "message": (
                        f"observed {value} does not satisfy {query_id}; "
                        "no effect substitution is permitted"
                    ),
                }
            )

    if any(d["code"].endswith("-UNSUPPORTED") for d in diagnostics):
        status = "unsupported"
    elif pending:
        status = "pending"
    else:
        status = "eligible"

    return {
        "profile_id": record["profile_id"],
        "requirements_digest": record["requirements_digest"],
        "status": status,
        "diagnostics": diagnostics,
        "device_key": device_key(vendor_id, device_id),
        "environment_key": environment_key(
            vendor_id,
            device_id,
            driver,
            os_build,
            {"probe": observation.get("toolchain_identity"),
             "runtime": observation.get("runtime"),
             "board": {key: adapter.get(key) for key in ("subsystem_id", "revision", "dedicated_video_memory_bytes")},
             "adapter_luid": adapter.get("adapter_luid"), "physical_provenance": provenance},
        ),
        "adapter": {
            "vendor_id": vendor_id,
            "device_id": device_id,
            "adapter_luid": adapter.get("adapter_luid"),
            "adapter_name_metadata_only": adapter.get("adapter_name"),
        },
        "physical_device_tier": physical_device_tier(device_key(vendor_id, device_id)) if provenance else None,
        "physical_provenance": provenance,
        "driver_version": driver,
        "os_build": os_build,
        "queried": queried,
        "marketing_name_used_for_eligibility": False,
        "satisfies_optional_profile": status == "eligible",
        "fallback_artifact_id": record.get("fallback_artifact_id"),
    }


def device_key(vendor_id: int, device_id: int) -> str:
    """Stable per-device identity; one GPU can never inherit another's result."""
    from hardware_evidence_devices import device_key as shared_device_key
    return shared_device_key(vendor_id, device_id)


def environment_key(
    vendor_id: int,
    device_id: int,
    driver_version: str,
    os_build: str,
    toolchain_identity: Any = None,
) -> str:
    payload = {
        "device": device_key(vendor_id, device_id),
        "driver_version": str(driver_version),
        "os_build": str(os_build),
        "toolchain_identity": toolchain_identity,
    }
    return digest_value(payload)


def profile_manifest_template(profile_id: str) -> dict[str, Any]:
    """Return a complete profile document skeleton for the frozen definition."""
    definition = profile_definition(profile_id)
    document: dict[str, Any] = {
        "format": PROFILE_FORMAT,
        "version": PROFILE_VERSION,
        "task": TASK,
        "implementation_base": IMPLEMENTATION_BASE,
        "profile_id": profile_id,
        "profile_kind": definition["profile_kind"],
        "queried_capabilities": [dict(item) for item in definition["queries"]],
        "shader_targets": list(definition["shader_targets"]),
        "runtime": frozen_runtime_requirements(),
        "toolchain": frozen_toolchain_requirements(),
        "eligibility": {
            "source": "queried_capabilities",
            "gpu_marketing_name_used": False,
            "fallback_on_unsupported": "separate_common_artifact_only",
            "silent_effect_substitution": False,
        },
        "fallback_artifact_id": definition["fallback_artifact_id"],
        "artifact_budget": {
            "distribution_size_policy": "gated",
            "complete_distribution_bytes_ceiling": COMPLETE_DISTRIBUTION_CEILING_BYTES,
            "incremental_distribution_bytes_ceiling": int(
                definition["incremental_budget_bytes"]
            ),
            "stretch_distribution_bytes_ceiling": STRETCH_DISTRIBUTION_CEILING_BYTES,
            "stretch_is_mandatory_gate": False,
            "peak_engine_committed_gpu_bytes_ceiling": 536870912,
            "process_start_to_first_present_ms_ceiling": 2000.0,
        },
        "device_coverage": [
            {
                "device_tier": tier,
                "architecture": DEVICE_TIERS[tier]["architecture"],
                "representative_model_metadata_only": DEVICE_TIERS[tier][
                    "representative_model"
                ],
                "status": "pending",
            }
            for tier in DEVICE_TIERS
        ],
        "dependencies": [
            {"name": name, "kind": "windows_inbox", "counted": True,
             "distributed_bytes": 0, "optional": False}
            for name in ("d3d12", "dxgi", "user32", "kernel32", "gdi32", "psapi", "advapi32", "version")
        ],
        "qualification": {
            "canonical_production_contract": "benchmarks/nw07/canonical-production.v1.json",
            "nw01_policy_preserved": True,
            "nw01b_evidence_semantics_reused": True,
            "nw07_renderer_and_export_reused": True,
            "perf_contract": "nw07_perf_ref_v1",
            "perf_protocol": "nw07-perf-run.v1",
            "quality_profile_id": "nw07.quality.v1",
        },
        "evidence_status": "pending",
    }
    if definition["profile_kind"] == "optional":
        document["fallback_artifact"] = {
            "artifact_id": definition["fallback_artifact_id"],
            "profile_id": COMMON_PROFILE_ID,
            "requirements_digest": COMMON_REQUIREMENTS_DIGEST,
            "separately_exported": True,
            "separately_tested": True,
            "optional_payloads": [],
        }
    document["requirements_digest"] = compute_requirements_digest(document)
    return document


def write_profile(path: Path | str, profile_id: str) -> dict[str, Any]:
    document = profile_manifest_template(profile_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return document


__all__ = [
    "PROFILE_FORMAT",
    "PROFILE_VERSION",
    "CapabilityProfileError",
    "requirements_digest_inputs",
    "compute_requirements_digest",
    "load_profile",
    "validate_profile",
    "evaluate_observation",
    "device_key",
    "environment_key",
    "profile_manifest_template",
    "write_profile",
]
