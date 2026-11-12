#!/usr/bin/env python3
"""NW-05 capability-profile export, size attribution, and player embedding.

The exporter consumes the existing NW-02 graph and NW-03 `.nwp` v1 package
pipeline.  It does not define a second package format, runtime graph parser, or
portable physical-resource layout.

One artifact is produced per requested capability profile.  Every artifact is
keyed by the stable profile ID, the canonical requirements digest, and the exact
emitted requirements.  The common artifact never includes optional profile
code, shaders, dependencies, or budget accounting.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import nw03_compiler as nw03
import nwp_package as nwp
import nw05_optimizer as optimizer
import nw05_schedule as schedule
import dependency_check


EXPORT_FORMAT = "nw05-export-report"
EXPORT_VERSION = 1
COMMON_PROFILE_ID = nwp.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST
EMBED_MAGIC = b"NWEMB1\x00\x00"
EMBED_VERSION = 1
EMBED_FOOTER = struct.Struct("<8sIQQ32s32s")
EMBED_FOOTER_SIZE = EMBED_FOOTER.size

DEFAULT_BYTE_CEILING = 48 * 1024 * 1024
DEFAULT_MEMORY_BUDGET = 256 * 1024 * 1024

KNOWN_PROFILES: dict[str, str] = {
    nwp.COMMON_PROFILE_ID: nwp.COMMON_REQUIREMENTS_DIGEST,
}
KNOWN_PROFILES.update(optimizer.OPTIONAL_PROFILES)


class ExportError(RuntimeError):
    """Raised when an export fails closed."""

    def __init__(
        self,
        message: str,
        diagnostics: Sequence[dict[str, Any]] | None = None,
        *,
        dependency_path: Sequence[str] = (),
    ):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])
        self.dependency_path = list(dependency_path)


@dataclass(frozen=True)
class ExportSettings:
    byte_ceiling: int = DEFAULT_BYTE_CEILING
    memory_budget: int = DEFAULT_MEMORY_BUDGET
    quantization_tolerance: float | None = None
    target_profile: str = nwp.COMMON_PROFILE_ID
    requirements_digest: str = nwp.COMMON_REQUIREMENTS_DIGEST
    optional_profiles: tuple[str, ...] = ()
    generation_overrides: tuple[tuple[str, str], ...] = ()
    bake_overrides: tuple[tuple[str, str], ...] = ()
    frames_in_flight: int = 2
    player_template: str = ""
    allow_optional_profiles: bool = False
    include_optional_artifacts: bool = True
    fusion_enabled: bool = True
    fusion_max_nodes: int = optimizer.DEFAULT_FUSION_MAX_NODES
    fusion_max_expression_bytes: int = optimizer.DEFAULT_FUSION_MAX_EXPRESSION_BYTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_ceiling": self.byte_ceiling,
            "memory_budget": self.memory_budget,
            "quantization_tolerance": self.quantization_tolerance,
            "target_profile": self.target_profile,
            "requirements_digest": self.requirements_digest,
            "optional_profiles": list(self.optional_profiles),
            "generation_overrides": dict(self.generation_overrides),
            "bake_overrides": dict(self.bake_overrides),
            "frames_in_flight": self.frames_in_flight,
            "player_template": self.player_template,
            "allow_optional_profiles": self.allow_optional_profiles,
            "include_optional_artifacts": self.include_optional_artifacts,
            "fusion_enabled": self.fusion_enabled,
            "fusion_max_nodes": self.fusion_max_nodes,
            "fusion_max_expression_bytes": self.fusion_max_expression_bytes,
        }


@dataclass
class ProfileArtifact:
    profile_id: str
    requirements_digest: str
    emitted_requirements: dict[str, Any]
    artifact_key: str
    package_bytes: bytes
    package: nwp.NwpPackage
    manifest: dict[str, Any]
    logical_schedule: schedule.LogicalSchedule
    physical_realization: schedule.PhysicalRealization
    size_report: dict[str, Any]
    executable_bytes: bytes | None = None
    executable_name: str = ""
    generation_decisions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "requirements_digest": self.requirements_digest,
            "emitted_requirements": self.emitted_requirements,
            "artifact_key": self.artifact_key,
            "package_sha256": hashlib.sha256(self.package_bytes).hexdigest(),
            "package_size_bytes": len(self.package_bytes),
            "manifest_sha256": self.manifest["manifest_sha256"],
            "executable_sha256": (
                hashlib.sha256(self.executable_bytes).hexdigest()
                if self.executable_bytes is not None
                else ""
            ),
            "executable_size_bytes": (
                len(self.executable_bytes) if self.executable_bytes is not None else 0
            ),
            "executable_name": self.executable_name,
            "size_report": self.size_report,
            "generation_decisions": list(self.generation_decisions),
            "logical_schedule_sha256": schedule.schedule_digest(self.logical_schedule),
            "physical_realization": self.physical_realization.to_dict(),
        }


@dataclass
class ExportResult:
    output_directory: str
    settings: ExportSettings
    artifacts: dict[str, ProfileArtifact]
    report: dict[str, Any]
    common_artifact_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.report,
            "output_directory": self.output_directory,
            "artifacts": {
                key: artifact.to_dict() for key, artifact in sorted(self.artifacts.items())
            },
        }


# ---------------------------------------------------------------------------
# Identity and budget helpers
# ---------------------------------------------------------------------------


def canonical_profile_identity(
    profile_id: str,
    requirements_digest: str | None = None,
) -> tuple[str, str]:
    known = KNOWN_PROFILES.get(profile_id)
    if known is None:
        raise ExportError(
            f"unknown or unsupported capability profile '{profile_id}'",
            [
                nwp.package_diagnostic(
                    "export_profile_unknown",
                    f"unknown capability profile '{profile_id}'",
                )
            ],
            dependency_path=["capability_profile", profile_id],
        )
    digest = requirements_digest or known
    if digest != known:
        raise ExportError(
            f"capability profile '{profile_id}' requires digest '{known}', got '{digest}'",
            [
                nwp.package_diagnostic(
                    "export_profile_digest_mismatch",
                    f"capability profile '{profile_id}' digest mismatch",
                )
            ],
            dependency_path=["capability_profile", profile_id, "requirements_digest"],
        )
    return profile_id, digest


def emitted_requirements(package: nwp.NwpPackage) -> dict[str, Any]:
    capability = package.capability
    return {
        "profile_id": capability.profile_id,
        "requirements_digest": capability.requirements_digest,
        "optional_profile_id": capability.optional_profile_id,
        "optional_requirements_digest": capability.optional_requirements_digest,
        "profiles": {
            "common": {
                "profile_id": nwp.COMMON_PROFILE_ID,
                "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
            },
            "optional": (
                {
                    "profile_id": capability.optional_profile_id,
                    "requirements_digest": capability.optional_requirements_digest,
                }
                if capability.optional_profile_id
                else None
            ),
        },
        "required_dxgi_formats": sorted(capability.required_dxgi_formats),
        "max_cbv_srv_uav_descriptors_per_frame": capability.max_cbv_srv_uav_descriptors_per_frame,
        "max_sampler_descriptors_per_frame": capability.max_sampler_descriptors_per_frame,
        "max_root_signature_dword_cost": capability.max_root_signature_dword_cost,
        "limit_max_texture_2d_dimension": capability.limit_max_texture_2d_dimension,
        "limit_max_compute_threads_per_group": capability.limit_max_compute_threads_per_group,
        "limit_max_buffer_bytes": capability.limit_max_buffer_bytes,
        "limit_swap_chain_buffer_count": capability.limit_swap_chain_buffer_count,
        "emitted_shader_targets": sorted(capability.emitted_shader_targets),
        "required_init_instructions": sorted(capability.required_init_instructions),
        "required_frame_instructions": sorted(capability.required_frame_instructions),
        "runtime_dxc_dependency": capability.runtime_dxc_dependency,
        "runtime_slang_dependency": capability.runtime_slang_dependency,
        "agility_sdk_required": capability.agility_sdk_required,
        "compiler_fingerprint": capability.compiler_fingerprint,
        "toolchain_identity": capability.toolchain_identity,
        "toolchain_dxc_version": capability.toolchain_dxc_version,
    }


def artifact_key(profile_id: str, requirements_digest: str, requirements: Mapping[str, Any]) -> str:
    payload = {
        "profile_id": profile_id,
        "requirements_digest": requirements_digest,
        "emitted_requirements": dict(requirements),
    }
    return "sha256:v1:" + hashlib.sha256(
        nwp.canonical_json_bytes(payload)
    ).hexdigest()


def section_sizes(data: bytes) -> dict[str, dict[str, int]]:
    if len(data) < nwp.HEADER_SIZE:
        raise ExportError("package is truncated")
    section_count = struct.unpack_from("<I", data, 40)[0]
    result: dict[str, dict[str, int]] = {}
    for index in range(section_count):
        entry = nwp.HEADER_SIZE + index * nwp.SECTION_ENTRY_SIZE
        if entry + nwp.SECTION_ENTRY_SIZE > len(data):
            raise ExportError("package section directory is truncated")
        section_id = data[entry : entry + 4].decode("ascii", errors="replace")
        records, offset, size = struct.unpack_from("<IQQ", data, entry + 8)
        result[section_id] = {
            "records": int(records),
            "offset": int(offset),
            "size_bytes": int(size),
        }
    return result


def analyze_package_size(
    package: nwp.NwpPackage,
    package_bytes: bytes,
    *,
    manifest_bytes: int = 0,
    executable_bytes: int = 0,
    player_template_bytes: int = 0,
    byte_ceiling: int = DEFAULT_BYTE_CEILING,
    runtime_dependencies: Sequence[str] | None = None,
    dependency_source: str = "package-phase-requirements",
    artifact_files: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    sections = section_sizes(package_bytes)
    resource_bytes = sum(resource.size_bytes for resource in package.resources)
    shader_blob_bytes = sum(len(shader.blob) for shader in package.shaders)
    unique_shader_bytes = len({shader.blob for shader in package.shaders})
    root_signature_bytes = sum(len(record.blob) for record in package.root_signatures)
    parameter_bytes = sum(len(parameter.value) for parameter in package.parameters)
    artifact_files = dict(artifact_files or {})
    executable_growth = max(0, executable_bytes - player_template_bytes)
    auxiliary_bytes = sum(artifact_files.values())
    distributed = (
        len(package_bytes)
        + manifest_bytes
        + executable_bytes
        + auxiliary_bytes
    )
    payload_bytes = sum(item["size_bytes"] for item in sections.values())
    directory_bytes = len(sections) * nwp.SECTION_ENTRY_SIZE
    structure_bytes = nwp.HEADER_SIZE + directory_bytes + nwp.CHECKSUM_SIZE
    dependency_names = (
        sorted(set(runtime_dependencies))
        if runtime_dependencies is not None
        else ["kernel32", "d3d12", "dxgi", "user32"]
    )
    dependencies = [
        {
            "name": name,
            "kind": "windows_inbox" if name != "ucrtbase" else "windows_runtime",
            "distributed_bytes": 0,
            "included": True,
            "reason": "reachable player/dependency-closure import",
        }
        for name in dependency_names
    ] + [
        {
            "name": "runtime-dxc",
            "kind": "forbidden_runtime_dependency",
            "distributed_bytes": 0,
            "included": False,
            "reason": "offline compile only; never redistributed",
        },
        {
            "name": "nodewerk-studio-authoring",
            "kind": "excluded_authoring",
            "distributed_bytes": 0,
            "included": False,
            "reason": "authoring infrastructure is not part of the player closure",
        },
    ]
    return {
        "format": "nw05-size-report",
        "version": 1,
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "package_size_bytes": len(package_bytes),
        "sections": {
            key: {
                **value,
                "percentage": (
                    round(100.0 * value["size_bytes"] / max(1, len(package_bytes)), 3)
                ),
            }
            for key, value in sorted(sections.items())
        },
        "section_summary": {
            key.strip(): value["size_bytes"]
            for key, value in sorted(sections.items())
        },
        "structure": {
            "header_bytes": nwp.HEADER_SIZE,
            "section_directory_bytes": directory_bytes,
            "checksum_bytes": nwp.CHECKSUM_SIZE,
            "section_payload_bytes": payload_bytes,
            "attributed_bytes": payload_bytes + structure_bytes,
            "package_bytes": len(package_bytes),
            "all_bytes_attributed": payload_bytes + structure_bytes == len(package_bytes),
        },
        "content": {
            "resource_count": len(package.resources),
            "resource_bytes": resource_bytes,
            "shader_count": len(package.shaders),
            "shader_blob_bytes": shader_blob_bytes,
            "unique_shader_blob_bytes": unique_shader_bytes,
            "root_signature_bytes": root_signature_bytes,
            "parameter_value_bytes": parameter_bytes,
            "initialization_instruction_count": len(package.initialization),
            "frame_instruction_count": len(package.frame),
        },
        "distribution": {
            "package_bytes": len(package_bytes),
            "manifest_bytes": manifest_bytes,
            "executable_bytes": executable_bytes,
            "player_template_bytes": player_template_bytes,
            "artifact_files": artifact_files,
            "artifact_file_bytes": auxiliary_bytes,
            "embedded_package_growth_bytes": executable_growth,
            "total_distributed_bytes": distributed,
            "byte_ceiling": byte_ceiling,
            "within_byte_ceiling": distributed <= byte_ceiling,
        },
        "dependencies": dependencies,
        "dependency_source": dependency_source,
        "dependency_distributed_bytes": sum(
            item["distributed_bytes"] for item in dependencies if item["included"]
        ),
        "dependency_cost_total_bytes": distributed + sum(
            item["distributed_bytes"] for item in dependencies if item["included"]
        ),
        "excluded_optional_implementations": sorted(
            optimizer.OPTIONAL_IMPLEMENTATIONS
        ),
    }


# ---------------------------------------------------------------------------
# Generation versus bake (explicit, never heuristic)
# ---------------------------------------------------------------------------


def _override_map(settings: ExportSettings) -> dict[str, str]:
    result: dict[str, str] = {}
    for node_id, mode in settings.generation_overrides:
        result[node_id] = mode
    for node_id, mode in settings.bake_overrides:
        result[node_id] = mode
    return result


def _generation_mode_map(
    optimized: optimizer.OptimizedGraph, settings: ExportSettings
) -> dict[str, str]:
    overrides = _override_map(settings)
    modes: dict[str, str] = {}
    node_by_id = {node.node_id: node for node in optimized.nodes}
    for node_id, mode in overrides.items():
        node = node_by_id.get(node_id)
        if node is None:
            continue
        if mode not in {"bake", "generate"}:
            raise ExportError(
                f"unsupported generation override '{mode}' for node '{node_id}'",
                dependency_path=["generation_override", node_id],
            )
        if mode == "bake" and node.operator_id in nw03.SHADER_OPERATORS:
            # A shader-generation node cannot be removed without removing the
            # rendered output.  Retain the correct generated path.
            mode = "generate"
        modes[node_id] = mode
    return modes


def _runtime_dependencies(
    package: nwp.NwpPackage, template_bytes: bytes
) -> tuple[list[str], str]:
    """Derive the player dependency closure from the template or package."""
    if template_bytes:
        try:
            imports = dependency_check.parse_pe64_sections_and_imports(
                template_bytes
            ).get("imports", [])
            names = sorted(
                {
                    str(name).lower().removesuffix(".dll")
                    for name in imports
                    if str(name)
                }
            )
            if names:
                return names, "template-pe-imports"
        except (ValueError, KeyError, struct.error):
            pass
    # Portable fallback: the release player closure is determined by package
    # phase requirements.  Window/display dependencies are only present when
    # the package emits frame.present.
    dependencies = {"kernel32", "d3d12", "dxgi", "user32"}
    if any(instruction.opcode == "frame.present" for instruction in package.frame):
        dependencies.add("gdi32")
    return sorted(dependencies), "package-phase-requirements"


def plan_generation_vs_bake(
    optimized: optimizer.OptimizedGraph,
    settings: ExportSettings,
    *,
    shader_blob_bytes: int = 0,
) -> tuple[dict[str, Any], ...]:
    """Record explicit bake/generate decisions without an automatic cost oracle."""
    overrides = _override_map(settings)
    decisions: list[dict[str, Any]] = []
    for node in sorted(optimized.nodes, key=lambda item: item.node_id):
        requested = overrides.get(node.node_id)
        if requested is not None:
            if requested not in {"bake", "generate"}:
                raise ExportError(
                    f"unsupported generation override '{requested}' for node '{node.node_id}'",
                    dependency_path=["generation_override", node.node_id],
                )
            mode = requested
            reason = "explicit export override"
            if mode == "bake" and node.operator_id in nw03.SHADER_OPERATORS:
                mode = "generate"
                reason = (
                    "bake of a shader-generation node is unsupported; retained "
                    "the correct generated path"
                )
        else:
            mode = "generate"
            reason = "default explicit generate; no automatic bake oracle is applied"
        payload = nwp.canonical_json_bytes(node.to_dict())
        compressed = zlib.compress(payload, level=9)
        baked_bytes = (
            ((len(compressed) + 15) // 16) * 16 if mode == "bake" else 0
        )
        decisions.append(
            {
                "node_id": node.node_id,
                "operator_id": node.operator_id,
                "mode": mode,
                "reason": reason,
                "incremental_compressed_content_bytes": (
                    len(compressed) if mode == "bake" else 0
                ),
                "introduced_code_bytes": (
                    0 if mode == "bake" else len(payload)
                ),
                "introduced_shader_bytes": (
                    0
                    if mode == "bake" or node.operator_id not in nw03.SHADER_OPERATORS
                    else shader_blob_bytes
                ),
                "startup_cost_bytes": len(compressed) if mode == "bake" else 0,
                "peak_memory_bytes": len(compressed) if mode == "bake" else len(payload),
                "baked_content_emitted_bytes": baked_bytes,
                "measured_expression_bytes": len(payload),
                "automatic_cost_oracle": False,
            }
        )
    return tuple(decisions)


# ---------------------------------------------------------------------------
# Player linkage and embedding
# ---------------------------------------------------------------------------


def embed_package(template: bytes, package_bytes: bytes, manifest_bytes: bytes) -> bytes:
    footer = EMBED_FOOTER.pack(
        EMBED_MAGIC,
        EMBED_VERSION,
        len(package_bytes),
        len(manifest_bytes),
        hashlib.sha256(package_bytes).digest(),
        hashlib.sha256(manifest_bytes).digest(),
    )
    return template + package_bytes + manifest_bytes + footer


def extract_embedded_package(executable: bytes) -> tuple[bytes, bytes]:
    if len(executable) < EMBED_FOOTER_SIZE:
        raise ExportError("executable has no embedded package footer")
    magic, version, package_size, manifest_size, package_sha, manifest_sha = EMBED_FOOTER.unpack_from(
        executable, len(executable) - EMBED_FOOTER_SIZE
    )
    if magic != EMBED_MAGIC or version != EMBED_VERSION:
        raise ExportError("executable embedded package footer is invalid")
    total = package_size + manifest_size + EMBED_FOOTER_SIZE
    if total > len(executable):
        raise ExportError("executable embedded package lengths are invalid")
    start = len(executable) - total
    package_bytes = executable[start : start + package_size]
    manifest_bytes = executable[
        start + package_size : start + package_size + manifest_size
    ]
    if hashlib.sha256(package_bytes).digest() != package_sha:
        raise ExportError("embedded package checksum mismatch")
    if hashlib.sha256(manifest_bytes).digest() != manifest_sha:
        raise ExportError("embedded link manifest checksum mismatch")
    return package_bytes, manifest_bytes


def generate_player_source(
    *,
    package_sha256: str,
    manifest_sha256: str,
) -> str:
    """Generate a generic specialized-player main with no per-demo content logic."""
    return (
        "// Generated by nodewerkc export NW-05. Do not edit.\n"
        "// The graph is the sole demo source; this main only binds the\n"
        "// embedded .nwp v1 package and drives the existing PackagePlayer.\n"
        "#include \"player/package_player.h\"\n"
        "#include \"player/embedded_package.h\"\n"
        "#include \"player/exit_codes.h\"\n"
        "#include <cstdio>\n"
        "#include <cstdlib>\n"
        "#include <string>\n\n"
        "int main(int argc, char** argv) {\n"
        "    nw::player::EmbeddedPackage embedded;\n"
        "    if (!nw::player::LoadEmbeddedPackage(&embedded)) {\n"
        "        std::fprintf(stderr, \"embedded package invalid\\n\");\n"
        "        return 70;\n"
        "    }\n"
        "    unsigned long long max_frames = 0;\n"
        "    for (int index = 1; index < argc; ++index) {\n"
        "        if (std::string(argv[index]) == \"--frames\" && index + 1 < argc) {\n"
        "            max_frames = std::strtoull(argv[++index], nullptr, 10);\n"
        "        }\n"
        "    }\n"
        "    nw::player::PlayerConfig config;\n"
        "    config.window_title = \"Nodewerk NW-05 Procedural Image\";\n"
        "    nw::player::PackagePlayer player(config);\n"
        "    const auto status = player.initialize(embedded.bytes, embedded.size);\n"
        "    if (status != nw::player::ExitCode::Success) return static_cast<int>(status);\n"
        "    for (std::uint64_t tick = 0; max_frames == 0 || tick < max_frames; ++tick) {\n"
        "        const auto frame = player.renderAtTick(tick);\n"
        "        if (frame != nw::player::ExitCode::Success) return static_cast<int>(frame);\n"
        "    }\n"
        "    const auto shutdown = player.shutdown();\n"
        "    std::printf(\"NW05_FRAMES=%llu\\n\", static_cast<unsigned long long>(player.frames_rendered()));\n"
        "    return shutdown == nw::player::ExitCode::Success ? 0 : static_cast<int>(shutdown);\n"
        "}\n"
        f"// package_sha256={package_sha256}\n"
        f"// link_manifest_sha256={manifest_sha256}\n"
    )


# ---------------------------------------------------------------------------
# Export pipeline
# ---------------------------------------------------------------------------


def _compose_display_stream(package: nwp.NwpPackage) -> nwp.NwpPackage:
    """Append the established NW-04 display/present stream to a package.

    The input package is entirely graph-derived.  This function only appends
    the display-phase instructions required to observe the graph's UAV output;
    it never substitutes fixture resources, shaders, or output content.
    """
    initialization = list(package.initialization)
    uploaded = {
        instruction.operands[0].value
        for instruction in initialization
        if instruction.opcode == "init.upload_resource"
        and instruction.operands
        and instruction.operands[0].type == "resource_ref"
    }
    for resource in package.resources:
        if resource.usage not in {"cbv", "uav"} or resource.kind != "buffer":
            continue
        if resource.resource_id in uploaded:
            continue
        initialization.append(
            nwp.Instruction(
                "init.upload_resource",
                (nwp.InstructionOperand("resource_ref", resource.resource_id),),
            )
        )
    frame = list(package.frame)
    if package.parameters and not any(
        instruction.opcode == "frame.set_timeline" for instruction in frame
    ):
        frame.insert(
            0,
            nwp.Instruction(
                "frame.set_timeline",
                (
                    nwp.InstructionOperand(
                        "u32", package.parameters[0].parameter_id
                    ),
                    nwp.InstructionOperand("u64", 0),
                ),
            ),
        )
    outputs = [
        resource
        for resource in package.resources
        if resource.usage == "uav" and resource.kind == "buffer"
    ]
    if not outputs:
        raise ValueError("package has no UAV buffer output for display")
    if len(outputs) > 1:
        raise ValueError(
            "graph package is ambiguous: multiple UAV buffer outputs could be "
            "displayed, so no output is selected implicitly"
        )
    output = outputs[0]
    if not any(instruction.opcode == "frame.barrier" for instruction in frame):
        barrier_index = next(
            (
                index
                for index, instruction in enumerate(frame)
                if instruction.opcode == "frame.set_descriptor_table"
            ),
            len(frame) - 1,
        )
        frame.insert(
            barrier_index + 1,
            nwp.Instruction(
                "frame.barrier",
                (
                    nwp.InstructionOperand("u32", 4),
                    nwp.InstructionOperand("resource_ref", output.resource_id),
                ),
            ),
        )
    if not any(instruction.opcode == "frame.present" for instruction in frame):
        frame.append(
            nwp.Instruction(
                "frame.present",
                (nwp.InstructionOperand("resource_ref", output.resource_id),),
            )
        )
    derived = nwp.derive_capability_requirements(
        package.resources,
        initialization,
        frame,
        package.shaders,
        package.root_signatures,
    )
    capability = replace(package.capability, **derived)
    capability = replace(
        capability,
        required_init_instructions=tuple(
            sorted({instruction.opcode for instruction in initialization})
        ),
        required_frame_instructions=tuple(
            sorted({instruction.opcode for instruction in frame})
        ),
    )
    capability = replace(
        capability, profile_fingerprint=capability.computed_profile_fingerprint()
    )
    return replace(
        package,
        capability=capability,
        initialization=initialization,
        frame=frame,
        flags=1,
    )


def _compile_profile_package(
    optimized: optimizer.OptimizedGraph,
    destination: Path,
    *,
    profile_id: str,
    requirements_digest: str,
    generation_modes: Mapping[str, str],
    dxc_executable: str | None,
    optimization: str,
    cache_dir: str | None,
    allow_unpinned_dxc: bool,
    allow_test_payloads: bool,
) -> tuple[bytes, nwp.NwpPackage, dict[str, Any]]:
    # Compile the optimised, quantized document rather than re-reading the
    # authoring file.  This is what makes constant propagation, DCE/CSE,
    # accepted fusion, quantization, and generation/bake overrides observable
    # in the emitted shader specializations and therefore in the .nwp bytes.
    document = optimized.materialize_document(generation_modes=generation_modes)
    report = nw03.compile_package(
        optimized.source_name,
        destination,
        document=document,
        semantic_digest_override=optimized.materialized_semantic_digest(
            generation_modes
        ),
        semantic_digest_test_identity=allow_test_payloads,
        cache_dir=cache_dir,
        dxc_executable=dxc_executable,
        optimization=optimization,
        allow_unpinned_dxc=allow_unpinned_dxc,
        allow_test_payloads=allow_test_payloads,
    )
    package_bytes = destination.read_bytes()
    package = nwp.decode_package(
        package_bytes, allow_dev=not bool(package_bytes and package_bytes[16] & 1)
    )
    if not any(shader.family == nw03.PASS_FAMILY for shader in package.shaders):
        raise ExportError(
            "graph lowering produced no executable procedural shader",
            dependency_path=["graph", "shader"],
        )
    # Append the established NW-04 compute/display instruction stream to the
    # *graph-derived* package.  No hand-authored package content contributes
    # the visible image: the display pass presents the UAV output produced by
    # the shader specializations compiled from this graph.
    try:
        original_flags = package.flags
        package = _compose_display_stream(package)
        package = replace(package, flags=original_flags)
    except ValueError as exc:
        raise ExportError(
            f"graph package has no displayable procedural output: {exc}",
            dependency_path=["display", "frame.present"],
        ) from exc
    package_bytes = nwp.encode_package(package)
    is_common = profile_id == nwp.COMMON_PROFILE_ID

    # Explicit bake overrides emit the incremental compressed node content into
    # the existing NW-03 parameter section.  This is real package content, not
    # just report bookkeeping, and works for shader-expression nodes too.
    baked_parameters: list[nwp.ParameterRecord] = []
    next_parameter_id = max(
        (parameter.parameter_id for parameter in package.parameters), default=0
    ) + 1
    node_by_id = optimized.node_map()
    for node_id, mode in sorted(generation_modes.items()):
        if mode != "bake":
            continue
        node = node_by_id.get(node_id)
        if node is None:
            continue
        payload = zlib.compress(
            nwp.canonical_json_bytes(node.to_dict()), level=9
        )
        padded = payload + b"\x00" * ((16 - len(payload) % 16) % 16)
        for chunk_index in range(0, len(padded), 16):
            baked_parameters.append(
                nwp.ParameterRecord(
                    parameter_id=next_parameter_id,
                    name=f"nw05.baked.{node_id}.{chunk_index // 16}",
                    type="float4",
                    phase="initialization",
                    component_count=4,
                    value=padded[chunk_index : chunk_index + 16],
                    timeline_start_frame=0,
                    timeline_end_frame=0,
                    timeline_step=0,
                    timeline_interpolation="none",
                )
            )
            next_parameter_id += 1
    # Field-specific quantization is also emitted as parameter content, so the
    # published artifact carries the quantized values rather than only a report
    # claim.  Full-precision source values remain recoverable from the IR.
    for decision in optimized.quantization:
        if not 1 <= len(decision.quantized) <= 4:
            continue
        parameter_type = {
            1: "float",
            2: "float2",
            3: "float3",
            4: "float4",
        }[len(decision.quantized)]
        baked_parameters.append(
            nwp.ParameterRecord(
                parameter_id=next_parameter_id,
                name=(
                    f"nw05.quantized.{decision.node_id}."
                    f"{decision.parameter}"
                ),
                type=parameter_type,
                phase="initialization",
                component_count=len(decision.quantized),
                value=struct.pack(
                    "<" + "f" * len(decision.quantized),
                    *decision.quantized,
                ),
                timeline_start_frame=0,
                timeline_end_frame=0,
                timeline_step=0,
                timeline_interpolation="none",
            )
        )
        next_parameter_id += 1
    if baked_parameters:
        package = replace(
            package, parameters=list(package.parameters) + baked_parameters
        )
        package_bytes = nwp.encode_package(package)

    capability = replace(
        package.capability,
        profile_id=profile_id,
        requirements_digest=requirements_digest,
        optional_profile_id="" if is_common else profile_id,
        optional_requirements_digest="" if is_common else requirements_digest,
        optional_profile_separate_artifact=True,
    )
    capability = replace(
        capability, profile_fingerprint=capability.computed_profile_fingerprint()
    )
    package = replace(package, capability=capability)
    package_bytes = nwp.encode_package(package)
    valid, diagnostics = nwp.validate_package_bytes(package_bytes)
    if not valid:
        raise ExportError(
            f"artifact for '{profile_id}' is invalid",
            diagnostics,
            dependency_path=["capability_profile", profile_id],
        )
    if requirements_digest != KNOWN_PROFILES[profile_id]:
        raise ExportError(
            f"profile '{profile_id}' digest mismatch",
            dependency_path=["capability_profile", profile_id, "requirements_digest"],
        )
    return package_bytes, package, report


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=".nw05-export-", suffix=".tmp", dir=str(path.parent))
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _failure_report_path(
    report_path: str | os.PathLike[str] | None,
    output_directory: Path,
) -> Path | None:
    """Never overwrite a prior complete artifact's report on failure."""
    if report_path is None:
        return None
    path = Path(report_path)
    try:
        path.resolve().relative_to(output_directory.resolve())
    except ValueError:
        return path
    suffix = path.suffix or ".json"
    return output_directory.parent / (
        output_directory.name + "-failed-report" + suffix
    )


def _publish_directory(staging: Path, destination: Path) -> None:
    """Publish a complete directory with rollback of any previous artifact."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    try:
        if destination.exists():
            backup = destination.parent / (
                destination.name + ".nw05-backup-" + next(tempfile._get_candidate_names())
            )
            os.replace(destination, backup)
        os.replace(staging, destination)
    except Exception:
        if backup is not None and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def _artifact_auxiliary_names(stem: str, *, common: bool) -> list[str]:
    names = [
        f"{stem}.schedule.json",
        f"{stem}.aliases.json",
        f"{stem}.player.cpp",
        f"{stem}.size.json",
    ]
    if common:
        names.extend(["link-manifest.json", "size-report.json"])
    return names


def _finalize_size_accounting(
    staging: Path,
    artifacts: Mapping[str, "ProfileArtifact"],
    settings: ExportSettings,
) -> None:
    """Attribute every emitted artifact file, including reports themselves.

    Size-report bytes are self-referential.  A bounded fixed-point iteration
    rewrites the deterministic JSON until the recorded file sizes and totals
    match the actual bytes on disk.
    """
    for _ in range(8):
        stable = True
        for profile_id, artifact in artifacts.items():
            stem = _profile_file_stem(profile_id)
            common = profile_id == nwp.COMMON_PROFILE_ID
            files: dict[str, int] = {}
            for name in _artifact_auxiliary_names(stem, common=common):
                path = staging / name
                if path.is_file():
                    files[name] = path.stat().st_size
            manifest_path = staging / f"{stem}.manifest.json"
            executable_path = (
                staging / artifact.executable_name
                if artifact.executable_name
                else None
            )
            manifest_bytes = (
                manifest_path.stat().st_size if manifest_path.is_file() else 0
            )
            executable_bytes = (
                executable_path.stat().st_size
                if executable_path is not None and executable_path.is_file()
                else 0
            )
            distribution = artifact.size_report["distribution"]
            previous_total = distribution["total_distributed_bytes"]
            distribution["manifest_bytes"] = manifest_bytes
            distribution["executable_bytes"] = executable_bytes
            distribution["artifact_files"] = dict(sorted(files.items()))
            distribution["artifact_file_bytes"] = sum(files.values())
            total = (
                len(artifact.package_bytes)
                + manifest_bytes
                + executable_bytes
                + sum(files.values())
            )
            distribution["total_distributed_bytes"] = total
            distribution["byte_ceiling"] = settings.byte_ceiling
            distribution["within_byte_ceiling"] = total <= settings.byte_ceiling
            payload = json.dumps(
                artifact.size_report, indent=2, sort_keys=True
            ).encode("utf-8")
            _write_bytes(staging / f"{stem}.size.json", payload)
            if common:
                _write_bytes(staging / "size-report.json", payload)
            if previous_total != total:
                stable = False
        if stable:
            break
    for profile_id, artifact in artifacts.items():
        distribution = artifact.size_report["distribution"]
        total = distribution["total_distributed_bytes"]
        if total > settings.byte_ceiling:
            raise ExportError(
                f"artifact '{profile_id}' exceeds the byte ceiling "
                f"({total} > {settings.byte_ceiling})",
                [
                    nwp.package_diagnostic(
                        "export_byte_ceiling_exceeded",
                        "distributed bytes exceed the configured ceiling",
                    )
                ],
                dependency_path=["budget", profile_id, "byte_ceiling"],
            )
        # Memory budget remains based on the queried physical realization.
        if artifact.physical_realization.peak_bytes > settings.memory_budget:
            raise ExportError(
                f"artifact '{profile_id}' exceeds the memory budget "
                f"({artifact.physical_realization.peak_bytes} > "
                f"{settings.memory_budget})",
                [
                    nwp.package_diagnostic(
                        "export_memory_budget_exceeded",
                        "peak physical realization bytes exceed the budget",
                    )
                ],
                dependency_path=["budget", profile_id, "memory_budget"],
            )


def export_graph(
    graph_path: str | os.PathLike[str],
    output_directory: str | os.PathLike[str],
    *,
    settings: ExportSettings | None = None,
    report_path: str | os.PathLike[str] | None = None,
    dxc_executable: str | None = None,
    optimization: str = "O2",
    cache_dir: str | os.PathLike[str] | None = None,
    allow_unpinned_dxc: bool = False,
    allow_test_payloads: bool = False,
    allocation_query: Mapping[int, Any]
    | Callable[[nwp.ResourceDescriptor], Any]
    | None = None,
    adapter_identity: str = "unavailable-native-adapter",
    prepare_staging: Callable[[Path], None] | None = None,
) -> ExportResult:
    """Export one artifact per capability profile with transactional publication."""
    settings = settings or ExportSettings()
    graph_path = Path(graph_path)
    output_directory = Path(output_directory)
    document = nw03.load_graph(graph_path)
    digest_override = settings.requirements_digest or None
    if (
        settings.target_profile != nwp.COMMON_PROFILE_ID
        and digest_override == nwp.COMMON_REQUIREMENTS_DIGEST
    ):
        # A default common digest is treated as "resolve from the selected
        # profile"; an explicitly wrong digest for the common target still
        # fails in canonical_profile_identity.
        digest_override = None
    target_profile, target_digest = canonical_profile_identity(
        settings.target_profile, digest_override
    )
    if digest_override is None:
        settings = replace(settings, requirements_digest=target_digest)
    if document.profile_id != nwp.COMMON_PROFILE_ID:
        raise ExportError(
            "the common graph profile must remain the common capability profile",
            dependency_path=["graph_profile", document.profile_id],
        )
    if document.requirements_digest != nwp.COMMON_REQUIREMENTS_DIGEST:
        raise ExportError(
            "graph requirements digest does not match the common profile",
            dependency_path=["graph_profile", "requirements_digest"],
        )
    optional_profiles: list[str] = []
    if settings.target_profile != nwp.COMMON_PROFILE_ID:
        canonical_profile_identity(settings.target_profile)
        optional_profiles.append(settings.target_profile)
    for profile_id in settings.optional_profiles:
        canonical_profile_identity(profile_id)
        if profile_id == nwp.COMMON_PROFILE_ID:
            continue
        optional_profiles.append(profile_id)
    if settings.allow_optional_profiles and not optional_profiles:
        optional_profiles = [
            profile_id
            for profile_id in KNOWN_PROFILES
            if profile_id != nwp.COMMON_PROFILE_ID
        ]

    try:
        optimized = optimizer.optimize_graph(
            document,
            quantization_tolerance=settings.quantization_tolerance,
            fusion_enabled=settings.fusion_enabled,
            fusion_max_nodes=settings.fusion_max_nodes,
            fusion_max_expression_bytes=settings.fusion_max_expression_bytes,
        )
    except optimizer.OptimizationError as exc:
        failure = ExportError("graph optimisation failed", exc.diagnostics)
        diagnostic_path = _failure_report_path(report_path, output_directory)
        if diagnostic_path is not None:
            _write_bytes(
                diagnostic_path,
                json.dumps(
                    {
                        "format": EXPORT_FORMAT,
                        "version": EXPORT_VERSION,
                        "success": False,
                        "error": str(failure),
                        "diagnostics": failure.diagnostics,
                        "dependency_path": failure.dependency_path,
                        "settings": settings.to_dict(),
                        "budgets": {
                            "byte_ceiling": settings.byte_ceiling,
                            "memory_budget": settings.memory_budget,
                        },
                        "preserved_previous_artifact": True,
                        "stage": "optimization",
                    },
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8"),
            )
        raise failure from exc

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".nw05-export-stage-", dir=str(output_directory.parent))
    )
    artifacts: dict[str, ProfileArtifact] = {}
    compile_reports: dict[str, dict[str, Any]] = {}
    try:
        profile_order = [
            nwp.COMMON_PROFILE_ID,
            *sorted(
                set(optional_profiles)
                - {nwp.COMMON_PROFILE_ID}
            ),
        ]
        generation_modes = _generation_mode_map(optimized, settings)
        for profile_id in profile_order:
            is_common = profile_id == nwp.COMMON_PROFILE_ID
            requirements_digest = KNOWN_PROFILES[profile_id]
            package_path = staging / f"{_profile_file_stem(profile_id)}.nwp"
            try:
                package_bytes, package, compile_report = _compile_profile_package(
                    optimized,
                    package_path,
                    profile_id=profile_id,
                    requirements_digest=requirements_digest,
                    generation_modes=generation_modes,
                    dxc_executable=dxc_executable,
                    optimization=optimization,
                    cache_dir=str(cache_dir) if cache_dir is not None else None,
                    allow_unpinned_dxc=allow_unpinned_dxc,
                    allow_test_payloads=allow_test_payloads,
                )
            except nw03.CompilerError:
                raise
            compile_reports[profile_id] = compile_report
            logical_schedule = schedule.build_logical_schedule(
                package, frames_in_flight=settings.frames_in_flight
            )
            if allocation_query is None and not allow_test_payloads:
                physical = schedule.pending_physical_realization(
                    logical_schedule,
                    adapter_identity="pending-native-adapter",
                )
            else:
                physical = schedule.realize_physical_aliases(
                    logical_schedule,
                    package,
                    allocation_query=allocation_query,
                    adapter_identity=adapter_identity,
                    frames_in_flight=settings.frames_in_flight,
                    allow_simulated_allocation=allow_test_payloads,
                )
            shader_rows = [
                (
                    f"{shader.entry_point}:{shader.shader_id}",
                    shader.target,
                    len(shader.blob),
                )
                for shader in package.shaders
            ]
            excluded_optional = sorted(
                other_profile
                for other_profile in KNOWN_PROFILES
                if other_profile != nwp.COMMON_PROFILE_ID
                and other_profile != profile_id
            )
            template_path = Path(settings.player_template) if settings.player_template else None
            template_bytes = (
                template_path.read_bytes()
                if template_path is not None and template_path.is_file()
                else b""
            )
            runtime_dependencies, dependency_source = _runtime_dependencies(
                package, template_bytes
            )
            requires_aliasing = any(
                bool(group.get("aliased"))
                or (
                    bool(group.get("pending"))
                    and len(group.get("resource_ids", ())) > 1
                )
                for group in physical.alias_groups
            )
            manifest = optimizer.build_link_manifest(
                optimized,
                package=package,
                shader_blobs=shader_rows,
                excluded_optional_profiles=excluded_optional,
                profile_id=profile_id,
                requirements_digest=requirements_digest,
                optional_profile_id="" if is_common else profile_id,
                requires_aliasing=requires_aliasing,
                runtime_dependencies=runtime_dependencies,
            )
            manifest["dependencies_source"] = dependency_source
            manifest["artifact_profile_id"] = profile_id
            manifest["artifact_requirements_digest"] = requirements_digest
            manifest["materialized_semantic_digest"] = (
                optimized.materialized_semantic_digest(generation_modes)
            )
            manifest["artifact_identity"] = artifact_key(
                profile_id, requirements_digest, emitted_requirements(package)
            )
            manifest["optional_artifact"] = not is_common
            manifest["manifest_sha256"] = "sha256:v1:" + hashlib.sha256(
                nwp.canonical_json_bytes(
                    {
                        key: value
                        for key, value in manifest.items()
                        if key != "manifest_sha256"
                    }
                )
            ).hexdigest()
            manifest_data = optimizer.manifest_bytes(manifest)
            decisions = plan_generation_vs_bake(
                optimized,
                settings,
                shader_blob_bytes=sum(len(shader.blob) for shader in package.shaders),
            )

            executable_bytes: bytes | None = None
            executable_name = ""
            if template_bytes:
                executable_bytes = embed_package(template_bytes, package_bytes, manifest_data)
                executable_name = f"{_profile_file_stem(profile_id)}.exe"

            schedule_data = json.dumps(
                logical_schedule.to_dict(), indent=2, sort_keys=True
            ).encode("utf-8")
            aliases_data = json.dumps(
                physical.to_dict(), indent=2, sort_keys=True
            ).encode("utf-8")
            player_source = generate_player_source(
                package_sha256=hashlib.sha256(package_bytes).hexdigest(),
                manifest_sha256=manifest["manifest_sha256"],
            ).encode("utf-8")
            aux_files = {
                f"{_profile_file_stem(profile_id)}.schedule.json": len(schedule_data),
                f"{_profile_file_stem(profile_id)}.aliases.json": len(aliases_data),
                f"{_profile_file_stem(profile_id)}.player.cpp": len(player_source),
            }
            if is_common:
                # Conventional duplicate names are separate distributed files.
                aux_files["link-manifest.json"] = len(manifest_data)

            size_report = analyze_package_size(
                package,
                package_bytes,
                manifest_bytes=len(manifest_data),
                executable_bytes=len(executable_bytes) if executable_bytes else 0,
                player_template_bytes=len(template_bytes),
                byte_ceiling=settings.byte_ceiling,
                runtime_dependencies=runtime_dependencies,
                dependency_source=dependency_source,
                artifact_files=aux_files,
            )
            size_report["content"]["reachable_node_ids"] = list(
                optimized.reachable_node_ids
            )
            size_report["content"]["shader_specializations"] = [
                row[0] for row in shader_rows
            ]
            size_report["content"]["excluded_optional_implementations"] = manifest[
                "excluded"
            ]["optional_implementations"]
            size_report["artifact_identity"] = manifest["artifact_identity"]

            # Budget validation runs again after auxiliary/size-report bytes
            # are finalized below; this early check catches obvious failures.
            if not size_report["distribution"]["within_byte_ceiling"]:
                raise ExportError(
                    f"artifact '{profile_id}' exceeds the byte ceiling "
                    f"({size_report['distribution']['total_distributed_bytes']} > "
                    f"{settings.byte_ceiling})",
                    [
                        nwp.package_diagnostic(
                            "export_byte_ceiling_exceeded",
                            "distributed bytes exceed the configured ceiling",
                        )
                    ],
                    dependency_path=["budget", profile_id, "byte_ceiling"],
                )
            if physical.peak_bytes > settings.memory_budget:
                raise ExportError(
                    f"artifact '{profile_id}' exceeds the memory budget "
                    f"({physical.peak_bytes} > {settings.memory_budget})",
                    [
                        nwp.package_diagnostic(
                            "export_memory_budget_exceeded",
                            "peak physical realization bytes exceed the budget",
                        )
                    ],
                    dependency_path=["budget", profile_id, "memory_budget"],
                )

            artifacts[profile_id] = ProfileArtifact(
                profile_id=profile_id,
                requirements_digest=requirements_digest,
                emitted_requirements=emitted_requirements(package),
                artifact_key=manifest["artifact_identity"],
                package_bytes=package_bytes,
                package=package,
                manifest=manifest,
                logical_schedule=logical_schedule,
                physical_realization=physical,
                size_report=size_report,
                executable_bytes=executable_bytes,
                executable_name=executable_name,
                generation_decisions=decisions,
            )

        if prepare_staging is not None:
            prepare_staging(staging)
        for profile_id, artifact in artifacts.items():
            stem = _profile_file_stem(profile_id)
            _write_bytes(staging / f"{stem}.nwp", artifact.package_bytes)
            _write_bytes(
                staging / f"{stem}.manifest.json",
                optimizer.manifest_bytes(artifact.manifest),
            )
            _write_bytes(
                staging / f"{stem}.size.json",
                json.dumps(artifact.size_report, indent=2, sort_keys=True).encode("utf-8"),
            )
            _write_bytes(
                staging / f"{stem}.schedule.json",
                json.dumps(
                    artifact.logical_schedule.to_dict(), indent=2, sort_keys=True
                ).encode("utf-8"),
            )
            _write_bytes(
                staging / f"{stem}.aliases.json",
                json.dumps(
                    artifact.physical_realization.to_dict(), indent=2, sort_keys=True
                ).encode("utf-8"),
            )
            if profile_id == nwp.COMMON_PROFILE_ID:
                # Conventional, unambiguous names for downstream evidence tools.
                _write_bytes(
                    staging / "link-manifest.json",
                    optimizer.manifest_bytes(artifact.manifest),
                )
                _write_bytes(
                    staging / "size-report.json",
                    json.dumps(
                        artifact.size_report, indent=2, sort_keys=True
                    ).encode("utf-8"),
                )
            if artifact.executable_bytes is not None:
                _write_bytes(
                    staging / artifact.executable_name,
                    artifact.executable_bytes,
                )
                source = generate_player_source(
                    package_sha256=hashlib.sha256(artifact.package_bytes).hexdigest(),
                    manifest_sha256=artifact.manifest["manifest_sha256"],
                )
                _write_bytes(staging / f"{stem}.player.cpp", source.encode("utf-8"))
            else:
                source = generate_player_source(
                    package_sha256=hashlib.sha256(artifact.package_bytes).hexdigest(),
                    manifest_sha256=artifact.manifest["manifest_sha256"],
                )
                _write_bytes(staging / f"{stem}.player.cpp", source.encode("utf-8"))

        _finalize_size_accounting(staging, artifacts, settings)
        common = artifacts[nwp.COMMON_PROFILE_ID]
        common_total = common.size_report["distribution"]["total_distributed_bytes"]
        optional_total = sum(
            artifact.size_report["distribution"]["total_distributed_bytes"]
            for profile_id, artifact in artifacts.items()
            if profile_id != nwp.COMMON_PROFILE_ID
        )
        report = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "settings": settings.to_dict(),
            "source_graph": {
                "path": str(graph_path),
                "sha256": optimized.source_sha256,
            },
            "provenance": {
                "toolchain": compile_reports.get(nwp.COMMON_PROFILE_ID, {}).get(
                    "toolchain", {}
                ),
                "compiler": compile_reports.get(nwp.COMMON_PROFILE_ID, {}).get(
                    "compiler", {}
                ),
                "package_sha256": hashlib.sha256(common.package_bytes).hexdigest(),
                "manifest_sha256": common.manifest["manifest_sha256"],
                "executable_sha256": (
                    hashlib.sha256(common.executable_bytes).hexdigest()
                    if common.executable_bytes is not None
                    else "unavailable"
                ),
                "player_template_sha256": (
                    hashlib.sha256(
                        Path(settings.player_template).read_bytes()
                    ).hexdigest()
                    if settings.player_template
                    and Path(settings.player_template).is_file()
                    else "unavailable"
                ),
            },
            "optimized_ir": optimized.to_dict(),
            "optimized_ir_text_sha256": hashlib.sha256(
                optimized.to_text().encode("utf-8")
            ).hexdigest(),
            "profiles": {
                "common": {
                    "profile_id": nwp.COMMON_PROFILE_ID,
                    "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
                    "artifact_key": common.artifact_key,
                    "distributed_bytes": common_total,
                },
                "optional": [
                    {
                        "profile_id": profile_id,
                        "requirements_digest": artifact.requirements_digest,
                        "artifact_key": artifact.artifact_key,
                        "distributed_bytes": artifact.size_report["distribution"][
                            "total_distributed_bytes"
                        ],
                        "independent_budget": True,
                    }
                    for profile_id, artifact in sorted(artifacts.items())
                    if profile_id != nwp.COMMON_PROFILE_ID
                ],
            },
            "budgets": {
                "byte_ceiling": settings.byte_ceiling,
                "memory_budget": settings.memory_budget,
                "common_distributed_bytes": common_total,
                "optional_distributed_bytes": optional_total,
                "common_within_byte_ceiling": common_total <= settings.byte_ceiling,
                "optional_independent": True,
                "common_baseline_raised_by_optional": False,
            },
            "content_accounting": {
                "reachable_node_ids": list(optimized.reachable_node_ids),
                "dead_node_ids": list(optimized.dead_nodes),
                "dead_outputs": [
                    f"{node}.{port}" for node, port in optimized.dead_outputs
                ],
                "retained_shaders": [
                    {
                        "shader_id": shader.shader_id,
                        "entry_point": shader.entry_point,
                        "target": shader.target,
                        "bytes": len(shader.blob),
                    }
                    for shader in package.shaders
                ],
                "retained_dependencies": common.manifest["retained"]["dependencies"],
                "excluded_optional_implementations": common.manifest["excluded"][
                    "optional_implementations"
                ],
                "excluded_authoring_infrastructure": common.manifest["excluded"][
                    "authoring_infrastructure"
                ],
            },
            "generation_vs_bake": {
                "automatic_oracle": False,
                "default": "generate",
                "decisions": list(common.generation_decisions),
                "documentation": (
                    "Bake is never selected automatically. Explicit bake overrides "
                    "record incremental compressed content, introduced code/shaders, "
                    "startup cost, and peak memory for human comparison."
                ),
            },
            "evidence": {
                "windows_native_executable": (
                    "generated" if common.executable_bytes is not None else "pending_unavailable"
                ),
                "gpu_alias_validation": "pending_unverified",
                "note": (
                    "Missing Windows/Ada/Blackwell coverage remains pending; "
                    "executed failures remain failures."
                ),
            },
            "success": True,
        }

        # Final publication is all-or-nothing for the directory.
        _publish_directory(staging, output_directory)
        staging = output_directory

        if report_path is not None:
            artifact_payloads = {}
            for key, artifact in sorted(artifacts.items()):
                stem = _profile_file_stem(key)
                artifact_payload = artifact.to_dict()
                artifact_payload["files"] = {
                    "package": f"{stem}.nwp",
                    "link_manifest": f"{stem}.manifest.json",
                    "size_report": f"{stem}.size.json",
                    "logical_schedule": f"{stem}.schedule.json",
                    "alias_realization": f"{stem}.aliases.json",
                    "player_source": f"{stem}.player.cpp",
                    "executable": artifact.executable_name,
                }
                artifact_payloads[key] = artifact_payload
            _write_bytes(
                Path(report_path),
                json.dumps(
                    {**report, "artifacts": artifact_payloads},
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8"),
            )
        return ExportResult(
            output_directory=str(output_directory),
            settings=settings,
            artifacts=artifacts,
            report=report,
            common_artifact_key=common.artifact_key,
        )
    except Exception as exc:
        if staging.exists() and staging != output_directory:
            shutil.rmtree(staging, ignore_errors=True)
        if report_path is not None:
            diagnostic_report = {
                "format": EXPORT_FORMAT,
                "version": EXPORT_VERSION,
                "success": False,
                "error": str(exc),
                "diagnostics": getattr(exc, "diagnostics", []),
                "dependency_path": getattr(exc, "dependency_path", []),
                "settings": settings.to_dict(),
                "budgets": {
                    "byte_ceiling": settings.byte_ceiling,
                    "memory_budget": settings.memory_budget,
                },
                "preserved_previous_artifact": True,
            }
            diagnostic_path = _failure_report_path(report_path, output_directory)
            assert diagnostic_path is not None
            _write_bytes(
                diagnostic_path,
                json.dumps(diagnostic_report, indent=2, sort_keys=True).encode("utf-8"),
            )
        raise


def _profile_file_stem(profile_id: str) -> str:
    return profile_id.replace(".", "_")


# ---------------------------------------------------------------------------
# inspect-size
# ---------------------------------------------------------------------------


def inspect_size(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Attribute package and export directory bytes without GPU submission."""
    target = Path(path)
    if target.is_dir():
        artifacts: list[dict[str, Any]] = []
        total = 0
        total_distributed = 0
        for package_path in sorted(target.glob("*.nwp")):
            data = package_path.read_bytes()
            inspection = nwp.inspect_package_bytes(data)
            if not inspection.valid or inspection.package is None:
                raise ExportError(
                    f"invalid package '{package_path}'",
                    list(inspection.diagnostics),
                )
            stem = package_path.with_suffix("")
            manifest_path = target / f"{stem.name}.manifest.json"
            executable_path = target / f"{stem.name}.exe"
            manifest_bytes = (
                len(manifest_path.read_bytes()) if manifest_path.is_file() else 0
            )
            executable_bytes = (
                len(executable_path.read_bytes()) if executable_path.is_file() else 0
            )
            manifest: dict[str, Any] = {}
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            common = (
                inspection.package.capability.profile_id
                == nwp.COMMON_PROFILE_ID
            )
            artifact_files: dict[str, int] = {}
            for name in _artifact_auxiliary_names(stem.name, common=common):
                aux_path = target / name
                if aux_path.is_file():
                    artifact_files[name] = aux_path.stat().st_size
            size = analyze_package_size(
                inspection.package,
                data,
                manifest_bytes=manifest_bytes,
                executable_bytes=executable_bytes,
                runtime_dependencies=manifest.get("retained", {}).get(
                    "dependencies"
                ),
                dependency_source=manifest.get(
                    "dependencies_source", "manifest-unavailable"
                ),
                artifact_files=artifact_files,
            )
            size["distribution_discovered"] = {
                "manifest_path": str(manifest_path) if manifest_path.is_file() else "",
                "executable_path": str(executable_path) if executable_path.is_file() else "",
                "manifest_bytes": manifest_bytes,
                "executable_bytes": executable_bytes,
            }
            if manifest:
                size["manifest_sha256"] = manifest.get("manifest_sha256", "")
                size["artifact_identity"] = manifest.get("artifact_identity", "")
            size["path"] = str(package_path)
            artifacts.append(size)
            total += len(data)
            total_distributed += size["distribution"]["total_distributed_bytes"]
        if not artifacts:
            raise ExportError(f"no .nwp artifacts in '{target}'")
        total_distributed = sum(
            path.stat().st_size
            for path in sorted(target.iterdir())
            if path.is_file()
        )
        return {
            "format": "nw05-inspect-size",
            "version": 1,
            "directory": str(target),
            "artifact_count": len(artifacts),
            "total_package_bytes": total,
            "total_distributed_bytes": total_distributed,
            "artifacts": artifacts,
        }
    raw = target.read_bytes()
    executable_bytes = 0
    embedded_manifest_bytes = 0
    if target.suffix.lower() == ".exe":
        try:
            data, embedded_manifest = extract_embedded_package(raw)
            executable_bytes = len(raw)
            embedded_manifest_bytes = len(embedded_manifest)
        except ExportError as exc:
            raise ExportError(
                f"executable '{target}' has no valid embedded package: {exc}"
            ) from exc
    else:
        data = raw
    inspection = nwp.inspect_package_bytes(
        data, allow_dev=not bool(len(data) > 16 and data[16] & 1)
    )
    if not inspection.valid or inspection.package is None:
        raise ExportError(f"invalid package '{target}'", list(inspection.diagnostics))
    stem = target.with_suffix("")
    manifest_path = target.parent / f"{stem.name}.manifest.json"
    executable_path = target.parent / f"{stem.name}.exe"
    manifest_bytes = (
        len(manifest_path.read_bytes()) if manifest_path.is_file() else embedded_manifest_bytes
    )
    if target.suffix.lower() != ".exe":
        executable_bytes = (
            len(executable_path.read_bytes()) if executable_path.is_file() else 0
        )
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    common = inspection.package.capability.profile_id == nwp.COMMON_PROFILE_ID
    artifact_files: dict[str, int] = {}
    for name in _artifact_auxiliary_names(stem.name, common=common):
        aux_path = target.parent / name
        if aux_path.is_file():
            artifact_files[name] = aux_path.stat().st_size
    result = analyze_package_size(
        inspection.package,
        data,
        manifest_bytes=manifest_bytes,
        executable_bytes=executable_bytes,
        runtime_dependencies=manifest.get("retained", {}).get("dependencies"),
        dependency_source=manifest.get("dependencies_source", "manifest-unavailable"),
        artifact_files=artifact_files,
    )
    if target.suffix.lower() == ".exe":
        distribution = result["distribution"]
        distribution["embedded_package_bytes"] = len(data)
        distribution["embedded_manifest_bytes"] = manifest_bytes
        distribution["embedded_content_counted_in_executable"] = True
        distribution["total_distributed_bytes"] = (
            executable_bytes + distribution["artifact_file_bytes"]
        )
        distribution["within_byte_ceiling"] = (
            distribution["total_distributed_bytes"]
            <= distribution["byte_ceiling"]
        )
    result["distribution_discovered"] = {
        "manifest_path": str(manifest_path) if manifest_path.is_file() else "",
        "executable_path": str(executable_path) if executable_path.is_file() else "",
        "manifest_bytes": manifest_bytes,
        "executable_bytes": executable_bytes,
    }
    result["path"] = str(target)
    result["capability"] = {
        "profile_id": inspection.package.capability.profile_id,
        "requirements_digest": inspection.package.capability.requirements_digest,
        "optional_profile_id": inspection.package.capability.optional_profile_id,
    }
    return result


# Backwards-friendly aliases.
export = export_graph
inspect_size_report = inspect_size
build_export = export_graph

