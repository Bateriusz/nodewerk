#!/usr/bin/env python3
"""Portable NW-04 player/package capability checks.

This module mirrors the player's exact requirement evaluation for GPU-less
contract verification.  Windows execution evidence still comes from the native
player; these checks never claim that a GPU qualification happened.
"""

from __future__ import annotations

import struct
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
    category: str = "package_validation"

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
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
        "feature_level_queried": True,
        "shader_model_queried": True,
        "binding_tier_queried": True,
        "heap_tier_queried": True,
        "windows_build_queried": True,
        "limits_queried": True,
        "optional_features_queried": True,
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
    category: str = "package_validation",
) -> CapabilityMismatch:
    return CapabilityMismatch(
        code=code,
        requirement=requirement,
        required=required,
        observed=observed,
        profile_id=package.capability.profile_id,
        requirements_digest=package.capability.requirements_digest,
        message=message,
        category=category,
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
    if capability.shader_model_text != nwp.SHADER_MODEL_TEXT_6_0:
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "shader_model_text",
                nwp.SHADER_MODEL_TEXT_6_0,
                capability.shader_model_text or "unavailable",
                "shader_model_text does not match shader model 6.0",
            )
        )
    emitted_formats = tuple(sorted({resource.format for resource in package.resources}))
    declared_formats = tuple(capability.required_dxgi_formats)
    if len(set(declared_formats)) != len(declared_formats):
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "required_dxgi_formats",
                "unique format set",
                "duplicate format entries",
                "required format list contains duplicates",
            )
        )
    if tuple(sorted(set(declared_formats))) != emitted_formats:
        failures.append(
            _failure(
                package,
                "package_capability_format_set",
                "required_dxgi_formats",
                ",".join(emitted_formats),
                ",".join(declared_formats) or "unavailable",
                "declared formats must exactly match emitted resources",
            )
        )
    known_formats = {
        "DXGI_FORMAT_R8G8B8A8_UNORM",
        "DXGI_FORMAT_R32G32B32A32_FLOAT",
        "DXGI_FORMAT_R32_UINT",
        "DXGI_FORMAT_D32_FLOAT",
    }
    for format_name in declared_formats:
        if format_name not in known_formats:
            failures.append(
                _failure(
                    package,
                    "package_capability_format_set",
                    "required_dxgi_formats",
                    "baseline DXGI formats",
                    format_name,
                    "unknown DXGI format requirement",
                )
            )
    actual_init = tuple(sorted({i.opcode for i in package.initialization}))
    actual_frame = tuple(sorted({i.opcode for i in package.frame}))
    declared_init = tuple(capability.required_init_instructions)
    declared_frame = tuple(capability.required_frame_instructions)
    if len(set(declared_init)) != len(declared_init):
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "required_init_instructions",
                "unique opcode set",
                "duplicate opcode entries",
                "initialization requirement contains duplicates",
            )
        )
    if tuple(sorted(set(declared_init))) != actual_init:
        failures.append(
            _failure(
                package,
                "package_capability_instruction_mismatch",
                "required_init_instructions",
                ",".join(actual_init),
                ",".join(declared_init),
                "initialization requirement is not exact",
            )
        )
    if len(set(declared_frame)) != len(declared_frame):
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "required_frame_instructions",
                "unique opcode set",
                "duplicate opcode entries",
                "frame requirement contains duplicates",
            )
        )
    if tuple(sorted(set(declared_frame))) != actual_frame:
        failures.append(
            _failure(
                package,
                "package_capability_instruction_mismatch",
                "required_frame_instructions",
                ",".join(actual_frame),
                ",".join(declared_frame),
                "frame requirement is not exact",
            )
        )
    actual_targets = tuple(sorted({shader.target for shader in package.shaders}))
    declared_targets = tuple(capability.emitted_shader_targets)
    if len(set(declared_targets)) != len(declared_targets):
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "emitted_shader_targets",
                "unique target set",
                "duplicate target entries",
                "shader target requirement contains duplicates",
            )
        )
    if tuple(sorted(set(declared_targets))) != actual_targets:
        failures.append(
            _failure(
                package,
                "package_capability_shader_target_mismatch",
                "emitted_shader_targets",
                ",".join(actual_targets),
                ",".join(declared_targets),
                "shader target requirement is not exact",
            )
        )
    for target in declared_targets:
        if target not in {"cs_6_0", "vs_6_0", "ps_6_0"}:
            failures.append(
                _failure(
                    package,
                    "package_shader_target_unknown",
                    "emitted_shader_targets",
                    "cs_6_0/vs_6_0/ps_6_0",
                    target,
                    "unknown shader target requirement",
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
    if (
        capability.max_cbv_srv_uav_descriptors_per_frame
        > nwp.MAX_CBV_SRV_UAV_DESCRIPTORS_PER_FRAME
        or capability.max_sampler_descriptors_per_frame
        > nwp.MAX_SAMPLER_DESCRIPTORS_PER_FRAME
        or capability.max_root_signature_dword_cost
        > nwp.MAX_ROOT_SIGNATURE_DWORD_COST
        or capability.limit_max_texture_2d_dimension
        > nwp.MAX_TEXTURE_2D_DIMENSION
        or capability.limit_max_compute_threads_per_group
        > nwp.MAX_COMPUTE_THREADS_PER_GROUP
        or capability.limit_max_buffer_bytes > nwp.MAX_BUFFER_BYTES
        or capability.limit_swap_chain_buffer_count > nwp.SWAP_CHAIN_BUFFER_COUNT
    ):
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "emitted_limits",
                "baseline maxima",
                "requirement exceeds the common profile maximum",
                "emitted requirement exceeds the immutable baseline",
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
    if nwp.PINNED_DXC_VERSION not in capability.toolchain_dxc_version:
        failures.append(
            _failure(
                package,
                "package_toolchain_identity",
                "toolchain_dxc_version",
                nwp.PINNED_DXC_VERSION,
                capability.toolchain_dxc_version or "unavailable",
                "release packages require the pinned offline DXC identity",
            )
        )
    expected_toolchain_identity = f"dxc:{capability.toolchain_dxc_version}"
    if capability.toolchain_identity != expected_toolchain_identity:
        failures.append(
            _failure(
                package,
                "package_toolchain_identity",
                "toolchain_identity",
                expected_toolchain_identity,
                capability.toolchain_identity or "unavailable",
                "toolchain identity must match the declared DXC version",
            )
        )
    if not capability.compiler_fingerprint:
        failures.append(
            _failure(
                package,
                "package_capability_contradictory",
                "compiler_fingerprint",
                "non-empty",
                "unavailable",
                "compiler fingerprint is missing",
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
    elif capability.optional_requirements_digest:
        failures.append(
            _failure(
                package,
                "package_optional_profile_contradiction",
                "optional_requirements_digest",
                "empty",
                capability.optional_requirements_digest,
                "optional digest without optional profile identity",
            )
        )
    return failures


def evaluate_package_against_adapter(
    package: nwp.NwpPackage, adapter: dict[str, Any]
) -> list[CapabilityMismatch]:
    failures = validate_package_requirements(package)
    adapter_category = (
        "package_validation" if failures else "device_and_capability"
    )
    if not adapter.get("queried", False):
        failures.append(
            _failure(
                package,
                "package_capability_query_unavailable",
                "adapter_capabilities",
                "queried",
                "unavailable",
                "adapter capability query did not complete",
                category=adapter_category,
            )
        )
    capability = package.capability
    queried_fields = (
        ("feature_level_queried", "feature_level", "12_0"),
        ("shader_model_queried", "shader_model", "6_0"),
        ("binding_tier_queried", "resource_binding_tier", "2"),
        ("heap_tier_queried", "resource_heap_tier", "2"),
        (
            "windows_build_queried",
            "min_windows_build",
            str(capability.min_windows_build),
        ),
        ("limits_queried", "limits", "queried"),
    )
    for queried_key, requirement, required in queried_fields:
        if not adapter.get(queried_key, False):
            failures.append(
                _failure(
                    package,
                    "package_capability_query_unavailable",
                    requirement,
                    required,
                    "unavailable",
                    f"adapter query '{queried_key}' is unavailable",
                    category=adapter_category,
                )
            )
    checks = [
        ("package_capability_feature_level", "feature_level", "max_feature_level", capability.feature_level, "0x{:X}", "feature_level_queried"),
        ("package_capability_shader_model", "shader_model", "max_shader_model", capability.shader_model, "0x{:X}", "shader_model_queried"),
        ("package_capability_binding_tier", "resource_binding_tier", "resource_binding_tier", capability.resource_binding_tier, "{}", "binding_tier_queried"),
        ("package_capability_heap_tier", "resource_heap_tier", "resource_heap_tier", capability.resource_heap_tier, "{}", "heap_tier_queried"),
        ("package_capability_windows_build", "min_windows_build", "windows_build_number", capability.min_windows_build, "{}", "windows_build_queried"),
        ("package_capability_direct_queue", "direct_queue_count", "direct_queue_count", capability.direct_queue_count, "{}", None),
        ("package_capability_descriptor_limit", "max_cbv_srv_uav_descriptors_per_frame", "max_cbv_srv_uav_descriptors_per_heap", capability.max_cbv_srv_uav_descriptors_per_frame, "{}", "limits_queried"),
        ("package_capability_descriptor_limit", "max_sampler_descriptors_per_frame", "max_sampler_descriptors_per_heap", capability.max_sampler_descriptors_per_frame, "{}", "limits_queried"),
        ("package_capability_root_signature_limit", "max_root_signature_dword_cost", "max_root_signature_dword_cost", capability.max_root_signature_dword_cost, "{}", "limits_queried"),
        ("package_capability_texture_limit", "limit_max_texture_2d_dimension", "max_texture_2d_dimension", capability.limit_max_texture_2d_dimension, "{}", "limits_queried"),
        ("package_capability_compute_limit", "limit_max_compute_threads_per_group", "max_compute_threads_per_group", capability.limit_max_compute_threads_per_group, "{}", "limits_queried"),
        ("package_capability_buffer_limit", "limit_max_buffer_bytes", "max_buffer_bytes", capability.limit_max_buffer_bytes, "{}", "limits_queried"),
        ("package_capability_swap_chain_limit", "limit_swap_chain_buffer_count", "max_swap_chain_buffer_count", capability.limit_swap_chain_buffer_count, "{}", "limits_queried"),
    ]
    for code, requirement, observed_key, required_value, fmt, queried_key in checks:
        if queried_key is not None and not adapter.get(queried_key, False):
            continue
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
                    category=adapter_category,
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
                    category=adapter_category,
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
                    category=adapter_category,
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
                        category=adapter_category,
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
                    category=adapter_category,
                )
            )
    if capability.optional_profile_id and not adapter.get(
        "optional_features_queried", False
    ):
        failures.append(
            _failure(
                package,
                "package_capability_query_unavailable",
                "optional_features",
                "queried",
                "unavailable",
                "optional feature query unavailable for declared optional profile",
                category=adapter_category,
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


_PARAMETER_LAYOUTS = {
    "float": (0, 1),
    "float2": (0, 2),
    "float3": (0, 3),
    "float4": (0, 4),
    "uint": (1, 1),
    "int": (2, 1),
}


def _parameter_layout(parameter: nwp.ParameterRecord) -> tuple[int, int] | None:
    layout = _PARAMETER_LAYOUTS.get(parameter.type)
    if layout is None:
        return None
    tick_kind, components = layout
    size_bytes = components * 4
    if (
        parameter.component_count != components
        or len(parameter.value) != size_bytes
    ):
        return None
    return tick_kind, size_bytes


def _resource_needs_cbv_srv_uav_descriptor(
    package: nwp.NwpPackage, resource: nwp.ResourceDescriptor
) -> bool:
    if resource.usage in {"srv", "uav"}:
        return True
    if resource.usage != "cbv":
        return False
    for instruction in package.frame:
        if (
            instruction.opcode == "frame.set_root_constants"
            and len(instruction.operands) == 3
            and instruction.operands[1].type == "resource_ref"
            and instruction.operands[1].value == resource.resource_id
        ):
            return False
    return True


def _barrier_usage_compatible(code: int, usage: str) -> bool:
    if usage == "readback":
        return code == 9
    if code == 11:
        return False
    if code == 4 and usage != "uav":
        return False
    if code == 3 and usage != "rtv":
        return False
    if code in (5, 6) and usage != "dsv":
        return False
    return True


def _root_constant_entries(
    package: nwp.NwpPackage, constant_count: int
) -> tuple[list[tuple[int, int, int, int]], list[CapabilityMismatch]]:
    failures: list[CapabilityMismatch] = []
    entries: list[tuple[int, int, int, int]] = []
    byte_offset = 0
    total_bytes = constant_count * 4
    for parameter in package.parameters:
        layout = _parameter_layout(parameter)
        if layout is None:
            failures.append(
                _failure(
                    package,
                    "package_parameter_type_unsupported",
                    "parameter_type",
                    "float|float2|float3|float4|uint|int",
                    parameter.type or "unavailable",
                    "parameter type/width is not representable by the bounded "
                    "root-constant timeline update",
                )
            )
            break
        tick_kind, size_bytes = layout
        remaining = total_bytes - byte_offset
        if remaining <= 0 or size_bytes > remaining:
            break
        entries.append((parameter.parameter_id, byte_offset, tick_kind, size_bytes))
        byte_offset += size_bytes
        if byte_offset >= total_bytes:
            break
    return entries, failures


def validate_frame_plan(package: nwp.NwpPackage) -> list[CapabilityMismatch]:
    """Mirror PackagePlayer::BuildFrameInstructionPlan.

    This is the GPU-less/host-simulation planner check used by the repository
    verification suite.  It covers the bounded descriptor-table, barrier, and
    timeline rules that are enforced before any GPU submission.
    """
    failures: list[CapabilityMismatch] = []
    resources = {resource.resource_id: resource for resource in package.resources}
    timeline_parameters: set[int] = set()
    recorded_parameters: set[int] = set()
    parameter_ids = {parameter.parameter_id for parameter in package.parameters}
    for instruction in package.frame:
        if instruction.opcode == "frame.draw":
            failures.append(
                _failure(
                    package,
                    "frame_draw_requires_raster_pipeline",
                    "pipeline",
                    "package raster pipeline",
                    "unsupported",
                    "the minimal NW-04 player only realizes compute pipelines",
                )
            )
        elif instruction.opcode == "frame.set_descriptor_table":
            heap_name = instruction.operands[1].value
            resource = resources.get(instruction.operands[2].value)
            if resource is None:
                failures.append(
                    _failure(
                        package,
                        "package_instruction_reference",
                        "descriptor_table_resource",
                        str(instruction.operands[2].value),
                        "missing",
                        "frame.set_descriptor_table references a missing resource",
                    )
                )
            elif heap_name not in {"cbv_srv_uav", "sampler"}:
                failures.append(
                    _failure(
                        package,
                        "package_instruction_descriptor_heap",
                        "heap_name",
                        "cbv_srv_uav|sampler",
                        heap_name or "unavailable",
                        "frame.set_descriptor_table names an unsupported heap",
                    )
                )
            else:
                available = (
                    resource.usage == "sampler"
                    if heap_name == "sampler"
                    else _resource_needs_cbv_srv_uav_descriptor(
                        package, resource
                    )
                )
                if not available:
                    failures.append(
                        _failure(
                            package,
                            "package_instruction_descriptor_target",
                            "descriptor_table_resource",
                            resource.name,
                            f"no descriptor in {heap_name}",
                            "frame.set_descriptor_table targets a resource "
                            "without a compatible descriptor",
                        )
                    )
        elif instruction.opcode == "frame.barrier":
            code = instruction.operands[0].value
            resource = resources.get(instruction.operands[1].value)
            if code > 12:
                failures.append(
                    _failure(
                        package,
                        "package_instruction_barrier_state",
                        "barrier_state",
                        "0..12",
                        str(code),
                        "frame.barrier uses an unknown resource-state code",
                    )
                )
            elif resource is None:
                failures.append(
                    _failure(
                        package,
                        "package_instruction_reference",
                        "barrier_resource",
                        str(instruction.operands[1].value),
                        "missing",
                        "frame.barrier references a missing resource",
                    )
                )
            elif not _barrier_usage_compatible(code, resource.usage):
                failures.append(
                    _failure(
                        package,
                        "package_instruction_barrier_usage",
                        "resource_usage",
                        resource.usage,
                        f"state code {code} is incompatible with {resource.usage}",
                        "frame.barrier state is incompatible with the target "
                        "resource heap/usage",
                    )
                )
        elif instruction.opcode == "frame.set_timeline":
            parameter_id = instruction.operands[0].value
            if parameter_id not in parameter_ids:
                failures.append(
                    _failure(
                        package,
                        "package_instruction_reference",
                        "parameter_id",
                        str(parameter_id),
                        "missing",
                        "frame.set_timeline references a missing parameter",
                    )
                )
            timeline_parameters.add(parameter_id)
        elif instruction.opcode == "frame.set_root_constants":
            constant_count = min(instruction.operands[2].value, 16)
            entries, entry_failures = _root_constant_entries(
                package, constant_count
            )
            failures.extend(entry_failures)
            recorded_parameters.update(entry[0] for entry in entries)
    for parameter_id in sorted(timeline_parameters):
        if parameter_id not in recorded_parameters:
            failures.append(
                _failure(
                    package,
                    "frame_timeline_parameter_not_bound",
                    "parameter_id",
                    str(parameter_id),
                    "not present in a full root-constant block",
                    "frame.set_timeline selects a parameter that no bounded "
                    "root-constant upload can update",
                )
            )
    return failures


def _write_timeline_tick(
    values: bytearray, byte_offset: int, tick_kind: int, size_bytes: int, tick: int
) -> None:
    for component in range(size_bytes // 4):
        offset = byte_offset + component * 4
        if tick_kind == 0:
            values[offset : offset + 4] = struct.pack("<f", float(tick))
        elif tick_kind == 1:
            values[offset : offset + 4] = struct.pack("<I", tick & 0xFFFFFFFF)
        else:
            signed = tick & 0xFFFFFFFF
            if signed >= 0x80000000:
                signed -= 0x100000000
            values[offset : offset + 4] = struct.pack("<i", signed)


def apply_frame_constants(
    package: nwp.NwpPackage, tick: int
) -> tuple[bytes | None, int | None]:
    """Mirror the bounded set_timeline -> set_root_constants update.

    Returns the last emitted root-constant bytes and the applied timeline value,
    or ``None`` for each when the package does not use the respective feature.
    """
    timeline_active = False
    timeline_parameter = 0
    timeline_value = 0
    last_constants: bytes | None = None
    last_timeline: int | None = None
    for instruction in package.frame:
        if instruction.opcode == "frame.set_timeline":
            timeline_active = True
            timeline_parameter = instruction.operands[0].value
            timeline_value = instruction.operands[1].value + tick
            last_timeline = timeline_value
        elif instruction.opcode == "frame.set_root_constants":
            constant_count = min(instruction.operands[2].value, 16)
            values = bytearray(constant_count * 4)
            entries, _ = _root_constant_entries(package, constant_count)
            offset = 0
            for parameter in package.parameters:
                remaining = len(values) - offset
                if remaining <= 0:
                    break
                copy_bytes = min(len(parameter.value), remaining)
                values[offset : offset + copy_bytes] = parameter.value[:copy_bytes]
                offset += copy_bytes
                if offset >= len(values):
                    break
            applied = False
            if timeline_active:
                for parameter_id, parameter_offset, kind, size_bytes in entries:
                    if parameter_id == timeline_parameter:
                        _write_timeline_tick(
                            values,
                            parameter_offset,
                            kind,
                            size_bytes,
                            timeline_value,
                        )
                        applied = True
                        break
            if not applied:
                for parameter in package.parameters:
                    if parameter.name == "time" and len(parameter.value) >= 4:
                        offset = 0
                        for preceding in package.parameters:
                            if preceding is parameter:
                                break
                            offset += len(preceding.value)
                        if offset + 4 <= len(values):
                            values[offset : offset + 4] = struct.pack(
                                "<f", float(tick)
                            )
                        break
            last_constants = bytes(values)
    return last_constants, last_timeline
