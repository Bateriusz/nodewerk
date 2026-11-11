#!/usr/bin/env python3
"""Portable NW-04 player/package capability checks.

This module mirrors the player's exact requirement evaluation for GPU-less
contract verification.  Windows execution evidence still comes from the native
player; these checks never claim that a GPU qualification happened.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402


@dataclass(frozen=True)
class CapabilityMismatch:
    code: str
    requirement: str
    required: str
    observed: str
    profile_id: str
    requirements_digest: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "category": (
                "device_and_capability"
                if self.code.startswith("package_capability_")
                else "package_validation"
            ),
            "code": self.code,
            "requirement": self.requirement,
            "required": self.required,
            "observed": self.observed,
            "profile_id": self.profile_id,
            "requirements_digest": self.requirements_digest,
            "message": self.message,
        }


def baseline_adapter(**overrides: Any) -> dict[str, Any]:
    adapter: dict[str, Any] = {
        "queried": True,
        "windows_build_number": nwp.MIN_WINDOWS_BUILD,
        "max_feature_level": nwp.FEATURE_LEVEL_12_0,
        "max_shader_model": nwp.SHADER_MODEL_6_0,
        "resource_binding_tier": nwp.RESOURCE_BINDING_TIER_2,
        "resource_heap_tier": nwp.RESOURCE_HEAP_TIER_2,
        "direct_queue_count": 1,
        "max_cbv_srv_uav_descriptors_per_heap": nwp.MAX_CBV_SRV_UAV_DESCRIPTORS_PER_FRAME,
        "max_sampler_descriptors_per_heap": nwp.MAX_SAMPLER_DESCRIPTORS_PER_FRAME,
        "max_root_signature_dword_cost": nwp.MAX_ROOT_SIGNATURE_DWORD_COST,
        "max_texture_2d_dimension": nwp.MAX_TEXTURE_2D_DIMENSION,
        "max_compute_threads_per_group": nwp.MAX_COMPUTE_THREADS_PER_GROUP,
        "max_buffer_bytes": nwp.MAX_BUFFER_BYTES,
        "max_swap_chain_buffer_count": nwp.SWAP_CHAIN_BUFFER_COUNT,
        "supports_cs_6_0": True,
        "supports_vs_6_0": True,
        "supports_ps_6_0": True,
        "wave_ops_supported": True,
        "int64_shader_ops_supported": True,
        "fp16_shader_ops_supported": True,
        "typed_uav_additional_formats_supported": True,
        "runtime_dxc_available": False,
        "runtime_slang_available": False,
        "formats": {
            "DXGI_FORMAT_R8G8B8A8_UNORM": {
                "supported": True,
                "buffer": True,
                "texture2d": True,
                "shader_resource": True,
                "unordered_access": True,
                "render_target": True,
                "depth_stencil": False,
            },
            "DXGI_FORMAT_R32G32B32A32_FLOAT": {
                "supported": True,
                "buffer": True,
                "texture2d": True,
                "shader_resource": True,
                "unordered_access": True,
                "render_target": True,
                "depth_stencil": False,
            },
            "DXGI_FORMAT_R32_UINT": {
                "supported": True,
                "buffer": True,
                "texture2d": True,
                "shader_resource": True,
                "unordered_access": True,
                "render_target": False,
                "depth_stencil": False,
            },
            "DXGI_FORMAT_D32_FLOAT": {
                "supported": True,
                "buffer": False,
                "texture2d": True,
                "shader_resource": True,
                "unordered_access": False,
                "render_target": False,
                "depth_stencil": True,
            },
        },
    }
    adapter.update(overrides)
    return adapter


def _failure(
    package: nwp.NwpPackage,
    code: str,
    requirement: str,
    required: str,
    observed: str,
    message: str,
) -> CapabilityMismatch:
    return CapabilityMismatch(
        code=code,
        requirement=requirement,
        required=required,
        observed=observed,
        profile_id=package.capability.profile_id,
        requirements_digest=package.capability.requirements_digest,
        message=message,
    )


def validate_package_requirements(package: nwp.NwpPackage) -> list[CapabilityMismatch]:
    failures: list[CapabilityMismatch] = []
    capability = package.capability
    if capability.profile_id != nwp.COMMON_PROFILE_ID:
        failures.append(
            _failure(
                package,
                "package_profile_unknown",
                "profile_id",
                nwp.COMMON_PROFILE_ID,
                capability.profile_id or "unavailable",
                "only the common NW-03 profile is executable",
            )
        )
    if capability.requirements_digest != nwp.COMMON_REQUIREMENTS_DIGEST:
        failures.append(
            _failure(
                package,
                "package_profile_digest_mismatch",
                "requirements_digest",
                nwp.COMMON_REQUIREMENTS_DIGEST,
                capability.requirements_digest or "unavailable",
                "requirements digest is not the finalized NW-03 digest",
            )
        )
    for name, expected in (
        ("feature_level", nwp.FEATURE_LEVEL_12_0),
        ("shader_model", nwp.SHADER_MODEL_6_0),
        ("resource_binding_tier", nwp.RESOURCE_BINDING_TIER_2),
        ("resource_heap_tier", nwp.RESOURCE_HEAP_TIER_2),
        ("direct_queue_count", 1),
        ("min_windows_build", nwp.MIN_WINDOWS_BUILD),
    ):
        actual = getattr(capability, name)
        if actual != expected:
            failures.append(
                _failure(
                    package,
                    f"package_capability_{name}",
                    name,
                    str(expected),
                    str(actual),
                    "capability record does not match the common profile",
                )
            )
    emitted_formats = tuple(sorted({resource.format for resource in package.resources}))
    if tuple(capability.required_dxgi_formats) != emitted_formats:
        failures.append(
            _failure(
                package,
                "package_capability_format_set",
                "required_dxgi_formats",
                ",".join(emitted_formats),
                ",".join(capability.required_dxgi_formats) or "unavailable",
                "declared formats must exactly match emitted resources",
            )
        )
    actual_init = tuple(sorted({i.opcode for i in package.initialization}))
    actual_frame = tuple(sorted({i.opcode for i in package.frame}))
    if tuple(capability.required_init_instructions) != actual_init:
        failures.append(
            _failure(
                package,
                "package_capability_instruction_mismatch",
                "required_init_instructions",
                ",".join(actual_init),
                ",".join(capability.required_init_instructions),
                "initialization requirement is not exact",
            )
        )
    if tuple(capability.required_frame_instructions) != actual_frame:
        failures.append(
            _failure(
                package,
                "package_capability_instruction_mismatch",
                "required_frame_instructions",
                ",".join(actual_frame),
                ",".join(capability.required_frame_instructions),
                "frame requirement is not exact",
            )
        )
    actual_targets = tuple(sorted({shader.target for shader in package.shaders}))
    if tuple(capability.emitted_shader_targets) != actual_targets:
        failures.append(
            _failure(
                package,
                "package_capability_shader_target_mismatch",
                "emitted_shader_targets",
                ",".join(actual_targets),
                ",".join(capability.emitted_shader_targets),
                "shader target requirement is not exact",
            )
        )
    derived = nwp.derive_capability_requirements(
        package.resources,
        package.initialization,
        package.frame,
        package.shaders,
        package.root_signatures,
    )
    for name, expected in derived.items():
        actual = getattr(capability, name)
        if actual != expected:
            failures.append(
                _failure(
                    package,
                    f"package_capability_{name}",
                    name,
                    str(expected),
                    str(actual),
                    "capability record does not match the emitted package",
                )
            )
    if capability.runtime_dxc_dependency:
        failures.append(
            _failure(
                package,
                "package_runtime_dependency_forbidden",
                "runtime_dxc_dependency",
                "false",
                "true",
                "runtime shader compilation is forbidden",
            )
        )
    if capability.runtime_slang_dependency:
        failures.append(
            _failure(
                package,
                "package_runtime_dependency_forbidden",
                "runtime_slang_dependency",
                "false",
                "true",
                "runtime Slang compilation is forbidden",
            )
        )
    if capability.agility_sdk_required:
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "agility_sdk_required",
                "false",
                "true",
                "Agility SDK redistribution is not permitted",
            )
        )
    if not capability.profile_fingerprint:
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "profile_fingerprint",
                "non-empty",
                "unavailable",
                "profile fingerprint is missing",
            )
        )
    elif capability.profile_fingerprint != capability.computed_profile_fingerprint():
        failures.append(
            _failure(
                package,
                "package_profile_fingerprint_mismatch",
                "profile_fingerprint",
                "canonical capability fingerprint",
                capability.profile_fingerprint,
                "profile fingerprint does not match the capability record",
            )
        )
    if capability.optional_profile_id:
        if capability.optional_profile_id not in nwp.KNOWN_OPTIONAL_PROFILES:
            failures.append(
                _failure(
                    package,
                    "package_optional_profile_unknown",
                    "optional_profile_id",
                    "known optional profile",
                    capability.optional_profile_id,
                    "unknown optional profile",
                )
            )
        elif (
            capability.optional_requirements_digest
            != nwp.KNOWN_OPTIONAL_PROFILES[capability.optional_profile_id]
        ):
            failures.append(
                _failure(
                    package,
                    "package_optional_profile_digest_mismatch",
                    "optional_requirements_digest",
                    nwp.KNOWN_OPTIONAL_PROFILES[capability.optional_profile_id],
                    capability.optional_requirements_digest or "unavailable",
                    "optional profile digest mismatch",
                )
            )
        if not capability.optional_profile_separate_artifact:
            failures.append(
                _failure(
                    package,
                    "package_optional_profile_contradiction",
                    "optional_profile_separate_artifact",
                    "true",
                    "false",
                    "unsupported optional profiles must remain separate artifacts",
                )
            )
    return failures


def evaluate_package_against_adapter(
    package: nwp.NwpPackage, adapter: dict[str, Any]
) -> list[CapabilityMismatch]:
    failures = validate_package_requirements(package)
    if not failures and not adapter.get("queried", False):
        failures.append(
            _failure(
                package,
                "package_capability_query_unavailable",
                "adapter_capabilities",
                "queried",
                "unavailable",
                "adapter capability query did not complete",
            )
        )
    capability = package.capability
    checks = [
        ("package_capability_feature_level", "feature_level", "max_feature_level", capability.feature_level, "0x{:X}"),
        ("package_capability_shader_model", "shader_model", "max_shader_model", capability.shader_model, "0x{:X}"),
        ("package_capability_binding_tier", "resource_binding_tier", "resource_binding_tier", capability.resource_binding_tier, "{}"),
        ("package_capability_heap_tier", "resource_heap_tier", "resource_heap_tier", capability.resource_heap_tier, "{}"),
        ("package_capability_windows_build", "min_windows_build", "windows_build_number", capability.min_windows_build, "{}"),
        ("package_capability_direct_queue", "direct_queue_count", "direct_queue_count", capability.direct_queue_count, "{}"),
        ("package_capability_descriptor_limit", "max_cbv_srv_uav_descriptors_per_frame", "max_cbv_srv_uav_descriptors_per_heap", capability.max_cbv_srv_uav_descriptors_per_frame, "{}"),
        ("package_capability_descriptor_limit", "max_sampler_descriptors_per_frame", "max_sampler_descriptors_per_heap", capability.max_sampler_descriptors_per_frame, "{}"),
        ("package_capability_root_signature_limit", "max_root_signature_dword_cost", "max_root_signature_dword_cost", capability.max_root_signature_dword_cost, "{}"),
        ("package_capability_texture_limit", "limit_max_texture_2d_dimension", "max_texture_2d_dimension", capability.limit_max_texture_2d_dimension, "{}"),
        ("package_capability_compute_limit", "limit_max_compute_threads_per_group", "max_compute_threads_per_group", capability.limit_max_compute_threads_per_group, "{}"),
        ("package_capability_buffer_limit", "limit_max_buffer_bytes", "max_buffer_bytes", capability.limit_max_buffer_bytes, "{}"),
        ("package_capability_swap_chain_limit", "limit_swap_chain_buffer_count", "max_swap_chain_buffer_count", capability.limit_swap_chain_buffer_count, "{}"),
    ]
    for code, requirement, observed_key, required_value, fmt in checks:
        observed_value = adapter.get(observed_key)
        if observed_value is None:
            failures.append(
                _failure(
                    package,
                    "package_capability_query_unavailable",
                    requirement,
                    fmt.format(required_value),
                    "unavailable",
                    f"adapter query '{observed_key}' is unavailable",
                )
            )
        elif observed_value < required_value:
            failures.append(
                _failure(
                    package,
                    code,
                    requirement,
                    fmt.format(required_value),
                    fmt.format(observed_value),
                    "adapter capability is below the exact package requirement",
                )
            )
    formats = adapter.get("formats", {})
    for format_name in capability.required_dxgi_formats:
        support = formats.get(format_name)
        if support is None:
            failures.append(
                _failure(
                    package,
                    "package_capability_format_unavailable",
                    "required_dxgi_formats",
                    format_name,
                    "unavailable",
                    "format support query did not return this format",
                )
            )
            continue
        for resource in package.resources:
            if resource.format != format_name:
                continue
            supported = (
                support.get("supported", False)
                and (
                    {
                        "cbv": support.get("buffer", False),
                        "srv": support.get("shader_resource", False),
                        "uav": support.get("unordered_access", False),
                        "rtv": support.get("render_target", False),
                        "dsv": support.get("depth_stencil", False),
                    }.get(resource.usage, True)
                )
            )
            if not supported:
                failures.append(
                    _failure(
                        package,
                        "package_capability_format_usage",
                        "required_dxgi_formats",
                        f"{format_name}:{resource.usage}",
                        "unsupported",
                        "adapter format does not support the emitted usage",
                    )
                )
                break
    for target in capability.emitted_shader_targets:
        key = {
            "cs_6_0": "supports_cs_6_0",
            "vs_6_0": "supports_vs_6_0",
            "ps_6_0": "supports_ps_6_0",
        }[target]
        if not adapter.get(key, False):
            failures.append(
                _failure(
                    package,
                    "package_capability_shader_target",
                    "emitted_shader_targets",
                    target,
                    "unsupported",
                    "adapter did not confirm the emitted shader target",
                )
            )
    return failures


def format_failures(failures: list[CapabilityMismatch]) -> str:
    return "\n".join(
        f"{failure.code}: requirement={failure.requirement} "
        f"required={failure.required} observed={failure.observed} "
        f"profile={failure.profile_id} digest={failure.requirements_digest}"
        for failure in failures
    )
