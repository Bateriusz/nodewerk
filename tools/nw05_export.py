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
import math
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import nw03_compiler as nw03
import nw03_oracle as oracle
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
REPO_ROOT = Path(__file__).resolve().parent.parent


def _graph_image_oracle_sha256(document: nw03.GraphDocument) -> str:
    """CPU oracle digest of the graph-derived procedural image expression."""
    try:
        generated, diagnostics = nw03.lower_to_fused_hlsl(document)
        if diagnostics or not generated:
            return ""
        digest = hashlib.sha256()
        for shader in sorted(generated, key=lambda item: item.specialization_id):
            expression = oracle.parse_expression(shader.root_expression)
            digest.update(shader.specialization_id.encode("utf-8"))
            for y in range(16):
                for x in range(16):
                    value = oracle.evaluate(
                        expression,
                        variables={
                            "uv": (x / 16.0, y / 16.0),
                            "time": 0.0,
                            "seed": 0.0,
                        },
                        float32=True,
                    )
                    components = (
                        value if isinstance(value, tuple) else (value,)
                    )
                    digest.update(
                        struct.pack("<" + "f" * len(components), *components)
                    )
        return "sha256:v1:" + digest.hexdigest()
    except (oracle.ExpressionError, nw03.CompilerError, TypeError, ValueError):
        # The GPU compiler may support operations outside the CPU oracle's
        # vocabulary.  That is unavailable evidence, not an export failure.
        return ""

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


def _node_bakeable(
    optimized: optimizer.OptimizedGraph, node: optimizer.IRNode
) -> bool:
    """Whether the existing v1 package can carry this node as baked content.

    Only fully constant scalar/vector fields have a defined package
    representation today: an initialization parameter record followed by
    literal constant consumers.  Shader roots and dynamic outputs keep the
    generated path; arbitrary payloads are rejected rather than silently
    ignored.
    """
    # The existing v1 player has no runtime consumer that can read a baked node
    # record independently of the generated shader closure.  Bake requests are
    # therefore rejected explicitly instead of being silently rewritten to
    # generation or emitting unconsumed parameter bytes.
    _ = optimized
    _ = node
    return False


def _generation_mode_map(
    optimized: optimizer.OptimizedGraph, settings: ExportSettings
) -> dict[str, str]:
    overrides = _override_map(settings)
    modes: dict[str, str] = {}
    node_by_id = {node.node_id: node for node in optimized.nodes}

    for node_id, mode in overrides.items():
        node = node_by_id.get(node_id)
        if node is None:
            raise ExportError(
                f"generation override references node '{node_id}' that is not "
                "present in the reachable optimised graph",
                dependency_path=["generation_override", node_id],
            )
        if mode not in {"bake", "generate"}:
            raise ExportError(
                f"unsupported generation override '{mode}' for node '{node_id}'",
                dependency_path=["generation_override", node_id],
            )
        if mode == "bake" and not _node_bakeable(optimized, node):
            raise ExportError(
                f"bake override for node '{node_id}' is unsupported: no "
                "runtime consumer exists in the .nwp v1 player",
                dependency_path=["generation_override", node_id, "bake"],
            )
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
        requested_mode = requested or "generate"
        if requested is not None:
            if requested not in {"bake", "generate"}:
                raise ExportError(
                    f"unsupported generation override '{requested}' for node '{node.node_id}'",
                    dependency_path=["generation_override", node.node_id],
                )
            if requested == "bake":
                raise ExportError(
                    f"bake override for node '{node.node_id}' is unsupported: "
                    "the .nwp v1 player has no runtime consumer for baked "
                    "node records",
                    dependency_path=[
                        "generation_override",
                        node.node_id,
                        "bake",
                    ],
                )
            mode = "generate"
            reason = "explicit generate override"
        else:
            mode = "generate"
            reason = "default explicit generate; no automatic bake oracle is applied"
        payload = nwp.canonical_json_bytes(node.to_dict())
        bake_requested = requested_mode == "bake"
        decisions.append(
            {
                "node_id": node.node_id,
                "operator_id": node.operator_id,
                "mode": mode,
                "requested_mode": requested_mode,
                "replaces_generation": mode == "bake",
                "reason": reason,
                "incremental_compressed_content_bytes": 0,
                "introduced_code_bytes": 0,
                "introduced_shader_bytes": 0,
                "startup_cost_bytes": 0,
                "peak_memory_bytes": 0,
                "baked_content_emitted_bytes": 0,
                "requested_bake_compressed_bytes": 0,
                "requested_bake_startup_bytes": 0,
                "requested_bake_peak_memory_bytes": 0,
                "measured_expression_bytes": len(payload),
                "measurement_status": "unavailable",
                "measurement_detail": (
                    "shader/resource closure costs are attributed by the "
                    "package and link-manifest reports, not per node; bake "
                    "content has no runtime consumer"
                ),
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
    manifest: Mapping[str, Any] | None = None,
    memory_budget_bytes: int = 0,
) -> str:
    """Generate a specialized-player main with no per-demo content logic.

    The generated compile-time flags come from the linked manifest: unused
    optional feature queries are not enabled in the common artifact, and the
    runtime checks the embedded manifest hash before initializing the player.
    """
    manifest = manifest or {}
    feature_requirements = manifest.get("feature_requirements", {})
    link_wave_ops = bool(feature_requirements.get("wave_ops", False))
    link_fp16 = bool(feature_requirements.get("fp16_shader_ops", False))
    link_int64 = bool(feature_requirements.get("int64_shader_ops", False))
    manifest_payload = optimizer.manifest_bytes(manifest) if manifest else b""
    manifest_fnv = 1469598103934665603
    for byte in manifest_payload:
        manifest_fnv ^= byte
        manifest_fnv = (manifest_fnv * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return (
        "// Generated by nodewerkc export NW-05. Do not edit.\n"
        "// The graph is the sole demo source; this main only binds the\n"
        "// embedded .nwp v1 package and drives the existing PackagePlayer.\n"
        "#include \"player/package_player.h\"\n"
        "#include \"player/embedded_package.h\"\n"
        "#include \"player/exit_codes.h\"\n"
        "#include <cstdio>\n"
        "#include <cstdlib>\n"
        "#include <string>\n"
        "#include <vector>\n\n"
        "int main(int argc, char** argv) {\n"
        "    nw::player::EmbeddedPackage embedded;\n"
        "    if (!nw::player::LoadEmbeddedPackage(&embedded)) {\n"
        "        std::fprintf(stderr, \"embedded package invalid\\n\");\n"
        "        return 70;\n"
        "    }\n"
        "    std::uint64_t manifest_hash = 1469598103934665603ULL;\n"
        "    for (std::size_t i = 0; i < embedded.manifest_size; ++i) {\n"
        "        manifest_hash ^= embedded.manifest[i];\n"
        "        manifest_hash *= 1099511628211ULL;\n"
        "    }\n"
        f"    if (manifest_hash != {manifest_fnv}ULL) {{\n"
        "        std::fprintf(stderr, \"NW05_LINK_MANIFEST_MISMATCH\\n\");\n"
        "        return 70;\n"
        "    }\n"
        "    nw::player::EmbeddedLinkMetadata link_metadata;\n"
        "    if (!nw::player::ParseEmbeddedLinkMetadata(embedded, &link_metadata)) {\n"
        "        std::fprintf(stderr, \"NW05_LINK_METADATA_INVALID\\n\");\n"
        "        return 70;\n"
        "    }\n"
        f"    if (link_metadata.wave_ops != {'true' if link_wave_ops else 'false'} ||\n"
        f"        link_metadata.fp16_shader_ops != {'true' if link_fp16 else 'false'} ||\n"
        f"        link_metadata.int64_shader_ops != {'true' if link_int64 else 'false'} ||\n"
        f"        link_metadata.memory_budget_bytes != {int(memory_budget_bytes)}ULL) {{\n"
        "        std::fprintf(stderr, \"NW05_LINK_METADATA_MISMATCH\\n\");\n"
        "        return 70;\n"
        "    }\n"
        "    std::printf(\"NW05_LINK_MANIFEST_VERIFIED=1\\n\");\n"
        "    nw::player::PlayerConfig config;\n"
        "    config.window_title = \"Nodewerk NW-05 Procedural Image\";\n"
        f"    config.query_optional_wave_ops = {'true' if link_wave_ops else 'false'};\n"
        f"    config.query_optional_fp16_shader_ops = {'true' if link_fp16 else 'false'};\n"
        f"    config.query_optional_int64_shader_ops = {'true' if link_int64 else 'false'};\n"
        f"    config.memory_budget_bytes = {int(memory_budget_bytes)}ULL;\n"
        "    unsigned long long max_frames = 0;\n"
        "    bool emit_image_hash = false;\n"
        "    for (int index = 1; index < argc; ++index) {\n"
        "        if (std::string(argv[index]) == \"--frames\" && index + 1 < argc) {\n"
        "            max_frames = std::strtoull(argv[++index], nullptr, 10);\n"
        "        } else if (std::string(argv[index]) == \"--allow-software-adapter\") {\n"
        "            config.allow_software_adapter = true;\n"
        "        } else if (std::string(argv[index]) == \"--force-non-aliased-plan\") {\n"
        "            config.force_non_aliased_plan = true;\n"
        "        } else if (std::string(argv[index]) == \"--validation\") {\n"
        "            config.enable_validation = true;\n"
        "        } else if (std::string(argv[index]) == \"--emit-image-hash\") {\n"
        "            emit_image_hash = true;\n"
        "        }\n"
        "    }\n"
        "    nw::player::PackagePlayer player(config);\n"
        "    const auto status = player.initialize(embedded.bytes, embedded.size);\n"
        "    if (status != nw::player::ExitCode::Success) return static_cast<int>(status);\n"
        "    for (std::uint64_t tick = 0; max_frames == 0 || tick < max_frames; ++tick) {\n"
        "        const auto frame = player.renderAtTick(tick);\n"
        "        if (frame != nw::player::ExitCode::Success) return static_cast<int>(frame);\n"
        "        if (player.window_closed()) break;\n"
        "    }\n"
        "    if (emit_image_hash) {\n"
        "        std::vector<std::uint8_t> image;\n"
        "        const auto readback = player.readbackPresentedOutput(&image);\n"
        "        if (readback != nw::player::ExitCode::Success) {\n"
        "            std::fprintf(stderr, \"NW05_IMAGE_READBACK_FAILED=%d\\n\", static_cast<int>(readback));\n"
        "            (void)player.shutdown();\n"
        "            return static_cast<int>(readback);\n"
        "        }\n"
        "        std::uint64_t hash = 1469598103934665603ULL;\n"
        "        std::uint64_t nonzero_bytes = 0u;\n"
        "        for (const std::uint8_t byte : image) {\n"
        "            if (byte != 0u) ++nonzero_bytes;\n"
        "            hash ^= static_cast<std::uint64_t>(byte);\n"
        "            hash *= 1099511628211ULL;\n"
        "        }\n"
        "        std::printf(\"NW05_IMAGE_BYTES=%llu\\n\", static_cast<unsigned long long>(image.size()));\n"
        "        std::printf(\"NW05_IMAGE_NONZERO_BYTES=%llu\\n\", static_cast<unsigned long long>(nonzero_bytes));\n"
        "        std::printf(\"NW05_IMAGE_FNV64=%016llx\\n\", static_cast<unsigned long long>(hash));\n"
        "    }\n"
        "    std::printf(\"NW05_ADAPTER=%s\\n\", player.queried_capabilities().adapter_description.c_str());\n"
        "    std::printf(\"NW05_DRIVER=%s\\n\", player.queried_capabilities().driver_version.c_str());\n"
        "    std::printf(\"NW05_VENDOR_ID=%u\\n\", player.queried_capabilities().vendor_id);\n"
        "    std::printf(\"NW05_DEVICE_ID=%u\\n\", player.queried_capabilities().device_id);\n"
        "    std::printf(\"NW05_ALIASED_PLAN=%d\\n\", player.using_aliased_plan() ? 1 : 0);\n"
        "    std::printf(\"NW05_ALIAS_BARRIERS=%llu\\n\", static_cast<unsigned long long>(player.resource_plan().alias_barriers.size()));\n"
        "    std::printf(\"NW05_ALIAS_ACTIVATIONS=%llu\\n\", static_cast<unsigned long long>(player.allocation_stats().alias_barrier_count));\n"
        "    std::printf(\"NW05_UAV_BARRIERS=%llu\\n\", static_cast<unsigned long long>(player.allocation_stats().uav_barrier_count));\n"
        "    std::printf(\"NW05_VALIDATION_ENABLED=%d\\n\", player.validation_enabled() ? 1 : 0);\n"
        "    const auto shutdown = player.shutdown();\n"
        "    std::printf(\"NW05_FRAMES=%llu\\n\", static_cast<unsigned long long>(player.frames_rendered()));\n"
        "    std::printf(\"NW05_PRESENTS=%llu\\n\", static_cast<unsigned long long>(player.frames_presented()));\n"
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
    present_ids = [
        instruction.operands[0].value
        for instruction in frame
        if instruction.opcode == "frame.present" and instruction.operands
    ]
    present_target = next(
        (
            resource
            for resource in outputs
            if present_ids and resource.resource_id == present_ids[0]
        ),
        None,
    )
    preferred = [resource for resource in outputs if resource.name == "output"]
    if present_target is not None:
        output = present_target
    elif len(preferred) == 1:
        output = preferred[0]
    elif len(outputs) == 1:
        output = outputs[0]
    else:
        raise ValueError(
            "graph package is ambiguous: multiple UAV buffer outputs exist and "
            "none is the compiler's explicit 'output' display target"
        )
    # If an authoring/compute package already carried a present instruction for
    # a non-UAV (or otherwise unusable) target, rewrite it to the package's
    # verified display output instead of leaving the old target in place.
    normalized_frame: list[nwp.Instruction] = []
    for instruction in frame:
        if instruction.opcode == "frame.present":
            normalized_frame.append(
                nwp.Instruction(
                    "frame.present",
                    (
                        nwp.InstructionOperand(
                            "resource_ref", output.resource_id
                        ),
                    ),
                )
            )
        else:
            normalized_frame.append(instruction)
    frame = normalized_frame
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


def _refresh_manifest_sha256(manifest: dict[str, Any]) -> None:
    manifest["manifest_sha256"] = "sha256:v1:" + hashlib.sha256(
        nwp.canonical_json_bytes(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
    ).hexdigest()


def _cmake_build_root_from_template(template_path: Path) -> Path:
    for candidate in (template_path.parent, *template_path.parents):
        if (candidate / "CMakeCache.txt").is_file():
            return candidate
    raise ExportError(
        "player template is not inside a configured CMake build tree",
        dependency_path=["specialized_player", "cmake_build_tree"],
    )


def _pe_runtime_dependencies(executable: bytes) -> list[str]:
    try:
        imports = dependency_check.parse_pe64_sections_and_imports(executable).get(
            "imports", []
        )
    except (ValueError, KeyError, struct.error) as exc:
        raise ExportError(
            f"specialized player PE dependency inspection failed: {exc}",
            dependency_path=["specialized_player", "imports"],
        ) from exc
    dependencies = sorted(
        {
            str(name).lower().removesuffix(".dll")
            for name in imports
            if str(name)
        }
    )
    if not dependencies:
        raise ExportError(
            "specialized player exposes no inspectable PE import closure",
            dependency_path=["specialized_player", "imports"],
        )
    return dependencies


def _build_specialized_player(
    template_path: Path,
    source_text: str,
    manifest: Mapping[str, Any],
    profile_id: str,
) -> bytes:
    if os.name != "nt":
        raise ExportError(
            "production specialized-player linking requires the pinned Windows build",
            dependency_path=["specialized_player", "platform"],
        )
    build_root = _cmake_build_root_from_template(template_path)
    generated_dir = build_root / "nw05-generated-src"
    generated_dir.mkdir(parents=True, exist_ok=True)
    source_path = generated_dir / f"{_profile_file_stem(profile_id)}.cpp"
    source_path.write_text(source_text, encoding="utf-8", newline="\n")

    requirements = manifest.get("feature_requirements", {})
    flags = {
        "NW05_COMPILE_WAVE_OPS_QUERY": bool(requirements.get("wave_ops", False)),
        "NW05_COMPILE_FP16_SHADER_OPS_QUERY": bool(
            requirements.get("fp16_shader_ops", False)
        ),
        "NW05_COMPILE_INT64_SHADER_OPS_QUERY": bool(
            requirements.get("int64_shader_ops", False)
        ),
    }
    configure = [
        "cmake",
        "-S",
        str(REPO_ROOT),
        "-B",
        str(build_root),
        f"-DNW05_GENERATED_PLAYER_SOURCE:FILEPATH={source_path}",
    ]
    configure.extend(
        f"-D{name}:BOOL={'ON' if enabled else 'OFF'}"
        for name, enabled in sorted(flags.items())
    )
    for command in (
        configure,
        ["cmake", "--build", str(build_root), "--target", "nw05_generated_player"],
    ):
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ExportError(
                "specialized player configure/build failed: "
                + (completed.stderr.strip() or completed.stdout.strip()),
                dependency_path=["specialized_player", "build"],
            )

    executable = build_root / "nw05-generated" / "nw05_generated_player.exe"
    if not executable.is_file():
        raise ExportError(
            f"specialized player build did not produce '{executable}'",
            dependency_path=["specialized_player", "output"],
        )
    return executable.read_bytes()


_PACKAGE_RESOURCE_OPERATORS = {
    "nw.texture.procedural",
    "nw.buffer.recipe",
    "nw.mesh.recipe",
}

_PACKAGE_UPLOAD_OPERATORS = {
    "nw.residency.upload_texture",
    "nw.residency.upload_buffer",
    "nw.residency.upload_mesh",
    "nw.residency.cpu_to_gpu",
    "nw.residency.gpu_to_cpu",
}

_PACKAGE_RENDER_OPERATORS = {
    "nw.render.fullscreen",
    "nw.render.gbuffer",
    "nw.render.composite",
    "nw.render.present",
}


def _node_int(
    node: nw03.GraphNode,
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 8192,
) -> int:
    tokens = node.params.get(name, [])
    if not tokens:
        return default
    text = tokens[0].strip()
    try:
        value = int(text, 0)
    except ValueError:
        try:
            numeric = float(text)
        except ValueError as exc:
            raise ExportError(
                f"node '{node.node_id}' parameter '{name}' is not numeric",
                dependency_path=["graph", node.node_id, name],
            ) from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ExportError(
                f"node '{node.node_id}' parameter '{name}' is not an integer",
                dependency_path=["graph", node.node_id, name],
            )
        value = int(numeric)
    if not minimum <= value <= maximum:
        raise ExportError(
            f"node '{node.node_id}' parameter '{name}' {value} is outside "
            f"{minimum}..{maximum}",
            dependency_path=["graph", node.node_id, name],
        )
    return value


def _node_float(node: nw03.GraphNode, name: str, default: float) -> float:
    tokens = node.params.get(name, [])
    if not tokens:
        return default
    try:
        return float(tokens[0])
    except ValueError:
        return default


def _graph_render_dimensions(
    document: nw03.GraphDocument,
    optimized: optimizer.OptimizedGraph,
) -> tuple[int, int]:
    reachable = set(optimized.reachable_node_ids) | set(
        optimized.constants_folded
    )
    textures = [
        document.nodes[node_id]
        for node_id in sorted(document.nodes)
        if node_id in reachable
        and document.nodes[node_id].operator_id == "nw.texture.procedural"
    ]
    if textures:
        return (
            _node_int(textures[0], "width", 256),
            _node_int(textures[0], "height", 256),
        )

    def setting_dimension(name: str, default: int) -> int:
        raw = str(document.settings.get(name, default)).strip()
        try:
            value = float(raw)
        except ValueError:
            return default
        if not math.isfinite(value) or not value.is_integer():
            return default
        return max(1, min(8192, int(value)))

    return (
        setting_dimension("nw05_output_width", 256),
        setting_dimension("nw05_output_height", 256),
    )


def _resource_format(format_name: str) -> str:
    return {
        "r8g8b8a8_unorm": "DXGI_FORMAT_R8G8B8A8_UNORM",
        "rgba32f": "DXGI_FORMAT_R32G32B32A32_FLOAT",
        "r32_uint": "DXGI_FORMAT_R32_UINT",
    }.get(format_name, "DXGI_FORMAT_R32_UINT")


def _parameter_type(values: Sequence[float]) -> str:
    return {1: "float", 2: "float2", 3: "float3", 4: "float4"}[len(values)]


def _parameter_value_bytes(values: Sequence[float]) -> bytes:
    return struct.pack("<" + "f" * len(values), *values)


def _numeric_parameter_values(node: nw03.GraphNode) -> dict[str, tuple[float, ...]]:
    result: dict[str, tuple[float, ...]] = {}
    skip = {"expression", "keys", "interpolation", "format", "entries"}
    for name in sorted(node.params):
        if name.startswith("__") or name in skip:
            continue
        tokens: list[str] = []
        for token in node.params[name]:
            tokens.extend(part for part in token.replace(",", " ").split() if part)
        try:
            values = tuple(float(token) for token in tokens)
        except ValueError:
            continue
        if 1 <= len(values) <= 4:
            result[name] = values
    return result


_PACKAGE_SUPPORTED_UNSUPPORTED = {
    "nw.readback.scalar",
    "nw.render.composite",
}

_FORMAT_BYTES_PER_ELEMENT = {
    "DXGI_FORMAT_R8G8B8A8_UNORM": 4,
    "DXGI_FORMAT_R32G32B32A32_FLOAT": 16,
    "DXGI_FORMAT_R32_UINT": 4,
    "DXGI_FORMAT_D32_FLOAT": 4,
}


def _texture_storage_bytes(
    width: int,
    height: int,
    *,
    depth: int = 1,
    array_size: int = 1,
    mip_levels: int = 1,
    bytes_per_element: int = 4,
) -> int:
    total = 0
    for mip in range(max(1, mip_levels)):
        level_width = max(1, width >> mip)
        level_height = max(1, height >> mip)
        level_depth = max(1, depth >> mip) if depth > 1 else 1
        total += (
            level_width
            * level_height
            * level_depth
            * max(1, array_size)
            * max(1, bytes_per_element)
        )
    return max(1, total)


def _shader_execution_plan(
    shader_rows: Sequence[Mapping[str, Any]],
    shader_records: Sequence[nwp.ShaderRecord],
) -> tuple[list[str], list[dict[str, Any]]]:
    rows = sorted(shader_rows, key=lambda item: str(item.get("specialization_id", "")))
    ordered_records = sorted(shader_records, key=lambda item: item.shader_id)
    if len(rows) != len(ordered_records):
        raise ExportError(
            "compile report and package disagree about compiled shader count",
            dependency_path=["graph", "shader", "compile_report"],
        )
    records_by_blob: dict[str, list[nwp.ShaderRecord]] = {}
    records_by_id = {record.shader_id: record for record in ordered_records}
    for record in ordered_records:
        records_by_blob.setdefault(
            hashlib.sha256(record.blob).hexdigest(), []
        ).append(record)
    roots: list[str] = []
    execution: list[dict[str, Any]] = []
    for row in rows:
        specialization_id = str(row["specialization_id"])
        root = str(row.get("execution_root") or specialization_id)
        blob_sha256 = str(row.get("blob_sha256", ""))
        report_shader_id = row.get("shader_id")
        record = None
        if report_shader_id is not None:
            record = records_by_id.get(int(report_shader_id))
            if record is not None and blob_sha256 and (
                hashlib.sha256(record.blob).hexdigest() != blob_sha256
            ):
                record = None
        if record is None:
            candidates = records_by_blob.get(blob_sha256)
            if not candidates:
                raise ExportError(
                    "compile report shader blob does not exist in the package",
                    dependency_path=[
                        "graph",
                        "shader",
                        specialization_id,
                        "blob_sha256",
                    ],
                )
            record = candidates.pop(0)
        if root not in roots:
            roots.append(root)
        execution.append(
            {
                "root": root,
                "specialization_id": specialization_id,
                "shader_id": record.shader_id,
                "execution_order": int(row.get("execution_order", 0)),
                "output_region": int(row.get("output_region", 0)),
                "region_count": max(1, int(row.get("region_count", 1))),
                "unfused": bool(row.get("unfused", False)),
            }
        )
    return sorted(roots), execution

def _build_graph_derived_package(
    document: nw03.GraphDocument,
    optimized: optimizer.OptimizedGraph,
    compiled_package: nwp.NwpPackage,
    compile_report: Mapping[str, Any],
    *,
    width: int,
    height: int,
    dispatch_groups: tuple[int, int, int],
) -> nwp.NwpPackage:
    """Lower reachable graph semantics into the existing .nwp v1 payload.

    Shader roots get independent UAV destinations.  Resource recipes become
    actual SRV allocations, upload nodes bind to their producer resource, and
    render nodes resolve their declared input dependencies.  Operations with no
    implemented package lowering fail closed rather than silently becoming
    metadata parameters.
    """
    used_operators = {node.operator_id for node in optimized.nodes}
    # Export only semantics the current package/player actually executes.
    # Rich resource/material/mesh/camera/render operators are valid NW-02
    # authoring vocabulary, but until their consumers exist NW-05 must reject
    # them instead of publishing metadata-only semantics.
    supported = (
        set(nw03.SHADER_OPERATORS)
        | set(_PACKAGE_UPLOAD_OPERATORS)
        | {"nw.render.fullscreen", "nw.render.present"}
        | {
            "nw.texture.procedural",
            "nw.buffer.recipe",
            "nw.mesh.recipe",
        }
    )
    unsupported = sorted(used_operators - supported)
    if unsupported:
        raise ExportError(
            "reachable graph uses an operator with no package lowering",
            [
                nwp.package_diagnostic(
                    "missing_implementation",
                    f"no executable .nwp v1 lowering for {operator_id}",
                )
                for operator_id in unsupported
            ],
            dependency_path=["graph", unsupported[0]],
        )

    shader_rows = sorted(
        compile_report.get("fixture", {}).get("shaders", []),
        key=lambda item: item.get("specialization_id", ""),
    )
    shader_records = sorted(compiled_package.shaders, key=lambda item: item.shader_id)
    if not shader_rows or not shader_records:
        raise ExportError(
            "graph lowering produced no compiled shader rows",
            dependency_path=["graph", "shader"],
        )

    params = nwp.ResourceDescriptor(
        resource_id=1,
        name="params",
        kind="buffer",
        format="DXGI_FORMAT_R32G32B32A32_FLOAT",
        usage="cbv",
        pass_family=nw03.PASS_FAMILY,
        binding_slot=0,
        binding_space=0,
        size_bytes=256,
    )
    resources: list[nwp.ResourceDescriptor] = [params]
    resource_by_node: dict[str, int] = {}
    upload_resource_by_texture: dict[str, int] = {}
    resource_by_output: dict[tuple[str, str], int] = {}
    resource_by_shader: dict[str, int] = {}
    next_resource_id = 2
    specs, shader_execution = _shader_execution_plan(shader_rows, shader_records)
    for spec in specs:
        root = document.nodes.get(spec)
        if root is not None and root.frequency == "initialization":
            raise ExportError(
                f"initialization-frequency shader root '{spec}' has no "
                "initialization-phase dispatch instruction in the .nwp v1 "
                "player",
                dependency_path=["graph", spec, "frequency"],
            )

    # One UAV allocation per terminal shader root. Rejected/unmeasured fusion
    # uses multiple deterministic image-sized regions inside that allocation:
    # dependencies write non-zero regions, while the final root always writes
    # region zero so presentation/readback sees the canonical output.
    for spec in specs:
        region_count = max(
            (
                int(item["region_count"])
                for item in shader_execution
                if item["root"] == spec
            ),
            default=1,
        )
        resource_by_shader[spec] = next_resource_id
        resources.append(
            nwp.ResourceDescriptor(
                resource_id=next_resource_id,
                name="output",
                kind="buffer",
                format="DXGI_FORMAT_R32_UINT",
                usage="uav",
                pass_family=nw03.PASS_FAMILY,
                binding_slot=0,
                binding_space=0,
                size_bytes=width * height * 16 * region_count,
            )
        )
        next_resource_id += 1

    reachable = set(optimized.reachable_node_ids) | set(optimized.constants_folded)
    for node_id in sorted(document.nodes):
        if node_id not in reachable:
            continue
        node = document.nodes[node_id]
        if node.operator_id == "nw.texture.procedural":
            format_name = node.params.get("format", ["r8g8b8a8_unorm"])[0]
            resource_format = _resource_format(format_name)
            texture_width = _node_int(node, "width", 256)
            texture_height = _node_int(node, "height", 256)
            resource_by_node[node_id] = next_resource_id
            resources.append(
                nwp.ResourceDescriptor(
                    resource_id=next_resource_id,
                    name=f"texture_{node_id}",
                    kind="texture2d",
                    format=resource_format,
                    usage="srv",
                    pass_family=nw03.PASS_FAMILY,
                    binding_slot=0,
                    binding_space=0,
                    size_bytes=_texture_storage_bytes(
                        texture_width,
                        texture_height,
                        bytes_per_element=_FORMAT_BYTES_PER_ELEMENT[resource_format],
                    ),
                    width=texture_width,
                    height=texture_height,
                )
            )
            next_resource_id += 1
            upload_resource_by_texture[node_id] = next_resource_id
            resources.append(
                nwp.ResourceDescriptor(
                    resource_id=next_resource_id,
                    name=f"texture_upload_{node_id}",
                    kind="buffer",
                    format="DXGI_FORMAT_R32_UINT",
                    usage="srv",
                    pass_family=nw03.PASS_FAMILY,
                    binding_slot=0,
                    binding_space=0,
                    size_bytes=max(1, texture_width * texture_height * 4),
                )
            )
            next_resource_id += 1
        elif node.operator_id == "nw.buffer.recipe":
            resource_format = _resource_format(
                node.params.get("format", ["r32_uint"])[0]
            )
            resource_by_node[node_id] = next_resource_id
            resources.append(
                nwp.ResourceDescriptor(
                    resource_id=next_resource_id,
                    name=f"buffer_{node_id}",
                    kind="buffer",
                    format=resource_format,
                    usage="srv",
                    pass_family=nw03.PASS_FAMILY,
                    binding_slot=0,
                    binding_space=0,
                    size_bytes=_node_int(
                        node, "bytes", 256, minimum=1, maximum=nwp.MAX_BUFFER_BYTES
                    ),
                )
            )
            next_resource_id += 1
        elif node.operator_id == "nw.mesh.recipe":
            vertices = _node_int(node, "vertices", 3, maximum=1048576)
            indices = _node_int(node, "indices", 3, maximum=3145728)
            resource_by_node[node_id] = next_resource_id
            resources.append(
                nwp.ResourceDescriptor(
                    resource_id=next_resource_id,
                    name=f"mesh_{node_id}",
                    kind="buffer",
                    format="DXGI_FORMAT_R32_UINT",
                    usage="srv",
                    pass_family=nw03.PASS_FAMILY,
                    binding_slot=0,
                    binding_space=0,
                    size_bytes=vertices * 32 + indices * 4,
                )
            )
            next_resource_id += 1

    uploaded_resources: list[int] = []
    for node_id in sorted(document.nodes):
        if node_id not in reachable:
            continue
        node = document.nodes[node_id]
        if node.operator_id not in _PACKAGE_UPLOAD_OPERATORS:
            continue
        if node.frequency != "initialization":
            raise ExportError(
                f"upload node '{node_id}' is frequency '{node.frequency}'; "
                "the .nwp v1 player supports initialization uploads only",
                dependency_path=["graph", node_id, "frequency"],
            )
        connection = next(iter(sorted(node.inputs.values())), None)
        source_id = resource_by_node.get(connection[0]) if connection else None
        if source_id is None:
            raise ExportError(
                f"upload node '{node_id}' has no lowered producer resource",
                dependency_path=["graph", node_id, "input"],
            )
        resource_by_node[node_id] = source_id
        if node.operator_id == "nw.residency.upload_texture":
            upload_resource = upload_resource_by_texture.get(connection[0])
            if upload_resource is not None:
                resource_by_node[node_id] = upload_resource
                uploaded_resources.append(upload_resource)
                continue
        source_resource = next(
            resource for resource in resources if resource.resource_id == source_id
        )
        if (
            node.operator_id
            in {"nw.residency.upload_buffer", "nw.residency.upload_mesh"}
            and source_resource.kind == "buffer"
        ):
            uploaded_resources.append(source_id)

    # Render dependencies resolve to the resource produced by their declared
    # input nodes.  Present follows its image input rather than "last dispatch".
    node_lookup = optimized.node_map()
    texture_input_resource_id: int | None = None
    for node in sorted(document.nodes.values(), key=lambda item: item.node_id):
        if node.operator_id not in _PACKAGE_RENDER_OPERATORS:
            continue
        if node.frequency == "initialization":
            raise ExportError(
                f"render node '{node.node_id}' is initialization-frequency; "
                "the .nwp v1 player has no initialization-phase dispatch",
                dependency_path=["graph", node.node_id, "frequency"],
            )
        if node.operator_id in {"nw.render.composite", "nw.render.gbuffer"}:
            raise ExportError(
                f"render node '{node.node_id}' has no complete package output "
                "lowering",
                dependency_path=["graph", node.node_id],
            )
        shader_connection = node.inputs.get("shader")
        if shader_connection is not None:
            source = node_lookup.get(shader_connection[0])
            spec = source.node_id if source is not None else shader_connection[0]
            output_id = resource_by_shader.get(spec)
            if output_id is None:
                output_id = resource_by_output.get((shader_connection[0], "image"))
            if output_id is None:
                raise ExportError(
                    f"render node '{node.node_id}' has no lowered shader output",
                    dependency_path=["graph", node.node_id, "shader"],
                )
            resource_by_output[(node.node_id, "image")] = output_id
        if node.operator_id == "nw.render.fullscreen":
            texture_connection = node.inputs.get("texture")
            if texture_connection is None:
                raise ExportError(
                    f"fullscreen render node '{node.node_id}' has no texture input",
                    dependency_path=["graph", node.node_id, "texture"],
                )
            if texture_connection[0] not in resource_by_node:
                raise ExportError(
                    f"fullscreen render node '{node.node_id}' references an "
                    "unlowered texture",
                    dependency_path=["graph", node.node_id, "texture"],
                )
            if texture_input_resource_id is None:
                texture_input_resource_id = resource_by_node[
                    texture_connection[0]
                ]

    def shader_for_render_node(node_id: str) -> str | None:
        node = document.nodes.get(node_id)
        if node is None:
            return None
        connection = node.inputs.get("shader")
        if connection is not None:
            source = node_lookup.get(connection[0])
            return source.node_id if source is not None else connection[0]
        image = node.inputs.get("image")
        if image is not None:
            source = node_lookup.get(image[0])
            if source is not None and source.node_id in resource_by_shader:
                return source.node_id
            for (producer, _port), _resource_id in resource_by_output.items():
                if producer == image[0]:
                    producer_node = document.nodes.get(producer)
                    if producer_node is not None:
                        shader = producer_node.inputs.get("shader")
                        if shader is not None:
                            source = node_lookup.get(shader[0])
                            return (
                                source.node_id if source is not None else shader[0]
                            )
        return None

    parameters: list[nwp.ParameterRecord] = [
        nwp.ParameterRecord(
            parameter_id=1,
            name="time",
            type="float",
            phase="frame",
            component_count=1,
            value=struct.pack("<f", 0.0),
            timeline_start_frame=0,
            timeline_end_frame=0,
            timeline_step=0,
            timeline_interpolation="none",
        )
    ]
    next_parameter_id = 2
    for name, value in (
        ("nw05.present.width", width),
        ("nw05.present.height", height),
    ):
        parameters.append(
            nwp.ParameterRecord(
                parameter_id=next_parameter_id,
                name=name,
                type="uint",
                phase="initialization",
                component_count=1,
                value=struct.pack("<I", int(value)),
                timeline_start_frame=0,
                timeline_end_frame=0,
                timeline_step=0,
                timeline_interpolation="none",
            )
        )
        next_parameter_id += 1
    node_by_id = {node.node_id: node for node in optimized.nodes}
    for node_id in sorted(document.nodes):
        if node_id not in reachable:
            continue
        node = document.nodes[node_id]
        if node.operator_id in (set(nw03.SHADER_OPERATORS) | _PACKAGE_RENDER_OPERATORS):
            continue
        values_by_name: dict[str, tuple[float, ...]] = {}
        optimized_node = node_by_id.get(node_id)
        if optimized_node is not None and optimized_node.constant:
            for port, values in sorted(optimized_node.constant.items()):
                if 1 <= len(values) <= 4:
                    values_by_name[f"const_{port}"] = tuple(values)
        values_by_name.update(_numeric_parameter_values(node))
        for name, values in sorted(values_by_name.items()):
            parameters.append(
                nwp.ParameterRecord(
                    parameter_id=next_parameter_id,
                    name=f"nw05.{node_id}.{name}",
                    type=_parameter_type(values),
                    phase=(
                        "initialization"
                        if node.frequency == "initialization"
                        else "frame"
                    ),
                    component_count=len(values),
                    value=_parameter_value_bytes(values),
                    timeline_start_frame=0,
                    timeline_end_frame=0,
                    timeline_step=0,
                    timeline_interpolation="none",
                )
            )
            next_parameter_id += 1

    root_signatures = list(compiled_package.root_signatures)
    if not root_signatures:
        raise ExportError(
            "compiled package has no root signature",
            dependency_path=["graph", "root_signature"],
        )
    root_signature = root_signatures[0]
    if texture_input_resource_id is not None:
        resources = [
            nwp.replace(resource, name="input")
            if resource.resource_id == texture_input_resource_id
            else resource
            for resource in resources
        ]
        bindings = list(root_signature.bindings) + [
            nwp.RootBinding("input", "srv", 0, 0, 2)
        ]
        root_signature = nwp.replace(
            root_signature,
            bindings=tuple(bindings),
            blob=nwp.make_root_signature_blob(
                root_signature.family, bindings
            ),
            dword_cost=root_signature.dword_cost + 1,
        )
        root_signatures[0] = root_signature

    initialization: list[nwp.Instruction] = [
        nwp.Instruction(
            "init.create_resource",
            (nwp.InstructionOperand("resource_ref", resource.resource_id),),
        )
        for resource in resources
    ]
    initialization.append(
        nwp.Instruction(
            "init.create_root_signature",
            (
                nwp.InstructionOperand(
                    "root_signature_ref", root_signature.root_signature_id
                ),
            ),
        )
    )
    initialization.append(
        nwp.Instruction(
            "init.upload_resource",
            (nwp.InstructionOperand("resource_ref", 1),),
        )
    )
    for resource_id in sorted(set(uploaded_resources)):
        initialization.append(
            nwp.Instruction(
                "init.upload_resource",
                (nwp.InstructionOperand("resource_ref", resource_id),),
            )
        )
    for shader in shader_records:
        initialization.append(
            nwp.Instruction(
                "init.create_pipeline",
                (
                    nwp.InstructionOperand("shader_ref", shader.shader_id),
                    nwp.InstructionOperand(
                        "root_signature_ref", shader.root_signature_id
                    ),
                ),
            )
        )

    frame: list[nwp.Instruction] = [
        nwp.Instruction(
            "frame.set_timeline",
            (
                nwp.InstructionOperand("u32", parameters[0].parameter_id),
                nwp.InstructionOperand("u64", 0),
            ),
        ),
        nwp.Instruction(
            "frame.set_root_signature",
            (
                nwp.InstructionOperand(
                    "root_signature_ref", root_signature.root_signature_id
                ),
            ),
        ),
        nwp.Instruction(
            "frame.set_root_constants",
            (
                nwp.InstructionOperand("u32", 0),
                nwp.InstructionOperand("resource_ref", 1),
                nwp.InstructionOperand("u32", 4),
            ),
        ),
    ]
    if texture_input_resource_id is not None:
        frame.append(
            nwp.Instruction(
                "frame.set_descriptor_table",
                (
                    nwp.InstructionOperand("u32", 2),
                    nwp.InstructionOperand("string", "cbv_srv_uav"),
                    nwp.InstructionOperand(
                        "resource_ref", texture_input_resource_id
                    ),
                ),
            )
        )

    dispatch_order: list[str] = []
    ordered_partitions = (
        list(optimized.partitions.get("initialization", ()))
        + list(optimized.partitions.get("frame", ()))
    )
    for node_id in ordered_partitions:
        node = document.nodes.get(node_id)
        if node is None or node.operator_id not in _PACKAGE_RENDER_OPERATORS:
            continue
        if node.operator_id == "nw.render.present":
            continue
        spec = shader_for_render_node(node_id)
        if spec in resource_by_shader and spec not in dispatch_order:
            dispatch_order.append(spec)
    # Shader-only graphs (no render node) still execute every compiled root.
    for spec in specs:
        if spec not in dispatch_order:
            dispatch_order.append(spec)
    if not dispatch_order:
        raise ExportError(
            "reachable graph has no executable shader root",
            dependency_path=["graph", "shader"],
        )

    for spec in dispatch_order:
        output_id = resource_by_shader[spec]
        executions = sorted(
            (item for item in shader_execution if item["root"] == spec),
            key=lambda item: (
                int(item["execution_order"]),
                str(item["specialization_id"]),
            ),
        )
        if not executions:
            raise ExportError(
                f"shader root '{spec}' has no compiled execution plan",
                dependency_path=["graph", spec, "shader"],
            )
        frame.append(
            nwp.Instruction(
                "frame.set_descriptor_table",
                (
                    nwp.InstructionOperand("u32", 1),
                    nwp.InstructionOperand("string", "cbv_srv_uav"),
                    nwp.InstructionOperand("resource_ref", output_id),
                ),
            )
        )
        for execution in executions:
            frame.extend(
                [
                    nwp.Instruction(
                        "frame.barrier",
                        (
                            nwp.InstructionOperand("u32", 4),
                            nwp.InstructionOperand("resource_ref", output_id),
                        ),
                    ),
                    nwp.Instruction(
                        "frame.dispatch",
                        (
                            nwp.InstructionOperand("u32", dispatch_groups[0]),
                            nwp.InstructionOperand("u32", dispatch_groups[1]),
                            nwp.InstructionOperand("u32", dispatch_groups[2]),
                            nwp.InstructionOperand(
                                "shader_ref", int(execution["shader_id"])
                            ),
                        ),
                    ),
                ]
            )

    present_target: int | None = None
    for node in document.nodes.values():
        if node.operator_id != "nw.render.present":
            continue
        connection = node.inputs.get("image")
        if connection is None:
            continue
        source = node_lookup.get(connection[0])
        spec = source.node_id if source is not None else connection[0]
        present_target = resource_by_shader.get(spec)
        if present_target is None:
            present_target = resource_by_output.get((connection[0], "image"))
            if present_target is None:
                for (producer, _port), resource_id in resource_by_output.items():
                    if producer == connection[0]:
                        present_target = resource_id
                        break
    if present_target is None:
        present_target = resource_by_shader.get(dispatch_order[-1])
    if present_target is None:
        raise ExportError(
            "graph package has no displayable procedural output",
            dependency_path=["display", "frame.present"],
        )
    frame.extend(
        [
            nwp.Instruction(
                "frame.barrier",
                (
                    nwp.InstructionOperand("u32", 4),
                    nwp.InstructionOperand("resource_ref", present_target),
                ),
            ),
            nwp.Instruction(
                "frame.present",
                (nwp.InstructionOperand("resource_ref", present_target),),
            ),
        ]
    )

    shaders = [
        nwp.replace(
            shader,
            thread_group_x=max(1, min(1024, width)),
            thread_group_y=1,
            thread_group_z=1,
        )
        for shader in shader_records
    ]
    derived = nwp.derive_capability_requirements(
        resources,
        initialization,
        frame,
        shaders,
        root_signatures,
    )
    capability = replace(
        compiled_package.capability,
        **derived,
        emitted_shader_targets=tuple(sorted({shader.target for shader in shaders})),
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
        compiled_package,
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame,
        parameters=parameters,
        shaders=shaders,
        root_signatures=root_signatures,
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
    # authoring file. Accepted measured fusion roots use one shader; rejected,
    # unmeasured, disabled, or bounded-out fusion roots retain their graph
    # dependencies and lower to a real multi-dispatch UAV execution plan.
    has_composed_shader = any(
        node.operator_id in {"nw.shader.add", "nw.shader.mul", "nw.shader.mix"}
        for node in optimized.nodes
    )
    document = optimized.materialize_document(
        generation_modes=generation_modes,
        apply_accepted_fusions=not has_composed_shader,
    )
    width, height = _graph_render_dimensions(document, optimized)
    thread_group = (max(1, min(1024, width)), 1, 1)
    dispatch_groups = (
        (width + thread_group[0] - 1) // thread_group[0],
        max(1, height),
        1,
    )
    generated_override = None
    if has_composed_shader:
        generated_override, diagnostics = nw03.lower_to_selective_hlsl(
            document,
            fused_roots={
                record.root_node for record in optimized.fusions if record.accepted
            },
            thread_group=thread_group,
            dispatch_groups=dispatch_groups,
            image_width=width,
            image_height=height,
        )
        if diagnostics:
            raise nw03.CompilerError("failed to lower selective shader graph", diagnostics)
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
        shader_thread_group=thread_group,
        dispatch_groups=dispatch_groups,
        image_width=width,
        image_height=height,
        generated_shaders_override=generated_override,
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
    package = _build_graph_derived_package(
        document,
        optimized,
        package,
        report,
        width=width,
        height=height,
        dispatch_groups=dispatch_groups,
    )
    package_bytes = nwp.encode_package(package)
    is_common = profile_id == nwp.COMMON_PROFILE_ID

    next_parameter_id = max(
        (parameter.parameter_id for parameter in package.parameters), default=0
    ) + 1
    quantized_parameters: list[nwp.ParameterRecord] = []
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
        quantized_parameters.append(
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
    if quantized_parameters:
        package = replace(
            package, parameters=list(package.parameters) + quantized_parameters
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
    report["image_oracle_sha256"] = _graph_image_oracle_sha256(document)
    report["image_oracle_status"] = (
        "available" if report["image_oracle_sha256"] else "unavailable"
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


def _publish_directory(
    staging: Path,
    destination: Path,
    after_publish: Callable[[], None] | None = None,
) -> None:
    """Publish a complete directory with rollback of any previous artifact."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    published = False
    try:
        if destination.exists():
            backup = destination.parent / (
                destination.name + ".nw05-backup-" + next(tempfile._get_candidate_names())
            )
            os.replace(destination, backup)
        os.replace(staging, destination)
        published = True
        if after_publish is not None:
            after_publish()
    except Exception:
        if published and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
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
    *,
    common_extra_files: Sequence[str] = (),
) -> None:
    """Attribute every emitted artifact file, including reports themselves.

    Size-report bytes are self-referential.  A bounded fixed-point iteration
    rewrites the deterministic JSON until the recorded file sizes and totals
    match the actual bytes on disk.
    """
    converged = False
    for _ in range(64):
        stable = True
        for profile_id, artifact in artifacts.items():
            stem = _profile_file_stem(profile_id)
            common = profile_id == nwp.COMMON_PROFILE_ID
            files: dict[str, int] = {}
            for name in _artifact_auxiliary_names(stem, common=common):
                path = staging / name
                if path.is_file():
                    files[name] = path.stat().st_size
            if common:
                for name in common_extra_files:
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
            converged = True
            break
    if not converged:
        raise ExportError(
            "size accounting did not converge",
            [
                nwp.package_diagnostic(
                    "export_size_accounting_nonconvergent",
                    "self-referential size report totals did not stabilize",
                )
            ],
            dependency_path=["size_report", "convergence"],
        )
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
        render_width, render_height = _graph_render_dimensions(document, optimized)
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
                    player_width=render_width,
                    player_height=render_height,
                )
            else:
                physical = schedule.realize_physical_aliases(
                    logical_schedule,
                    package,
                    allocation_query=allocation_query,
                    adapter_identity=adapter_identity,
                    frames_in_flight=settings.frames_in_flight,
                    allow_simulated_allocation=allow_test_payloads,
                    player_width=render_width,
                    player_height=render_height,
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
            if not allow_test_payloads and not template_bytes:
                raise ExportError(
                    "production NW-05 export requires a configured Windows player template "
                    "so the generated specialized main can be compiled and linked",
                    dependency_path=["specialized_player", "template"],
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
                memory_budget_bytes=settings.memory_budget,
            )
            if compile_report.get("fixture", {}).get("shaders"):
                manifest["shaders"] = [
                    {
                        "specialization_id": row["specialization_id"],
                        "target": row["target"],
                        "blob_bytes": row["blob_size"],
                        "blob_sha256": row["blob_sha256"],
                        "hlsl_sha256": row["hlsl_sha256"],
                        "root_node_ids": row.get("root_node_ids", []),
                        "execution_root": row.get(
                            "execution_root", row["specialization_id"]
                        ),
                        "execution_order": int(row.get("execution_order", 0)),
                        "output_region": int(row.get("output_region", 0)),
                        "region_count": int(row.get("region_count", 1)),
                        "unfused": bool(row.get("unfused", False)),
                    }
                    for row in compile_report["fixture"]["shaders"]
                ]
            manifest["dependencies_source"] = dependency_source
            manifest["artifact_profile_id"] = profile_id
            manifest["artifact_requirements_digest"] = requirements_digest
            manifest["materialized_semantic_digest"] = (
                optimized.materialized_semantic_digest(generation_modes)
            )
            manifest["image_oracle_sha256"] = compile_report.get(
                "image_oracle_sha256", ""
            )
            manifest["image_oracle_status"] = compile_report.get(
                "image_oracle_status", "unavailable"
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
            specialized_player_bytes: bytes | None = None
            if template_bytes and not allow_test_payloads:
                preliminary_source = generate_player_source(
                    package_sha256=hashlib.sha256(package_bytes).hexdigest(),
                    manifest_sha256=manifest["manifest_sha256"],
                    manifest=manifest,
                    memory_budget_bytes=settings.memory_budget,
                )
                preliminary_player = _build_specialized_player(
                    template_path, preliminary_source, manifest, profile_id
                )
                actual_dependencies = _pe_runtime_dependencies(preliminary_player)
                manifest["retained"]["dependencies"] = actual_dependencies
                manifest["dependencies_source"] = "specialized-pe-imports"
                _refresh_manifest_sha256(manifest)
                manifest_data = optimizer.manifest_bytes(manifest)

                final_source = generate_player_source(
                    package_sha256=hashlib.sha256(package_bytes).hexdigest(),
                    manifest_sha256=manifest["manifest_sha256"],
                    manifest=manifest,
                    memory_budget_bytes=settings.memory_budget,
                )
                specialized_player_bytes = _build_specialized_player(
                    template_path, final_source, manifest, profile_id
                )
                final_dependencies = _pe_runtime_dependencies(specialized_player_bytes)
                if final_dependencies != actual_dependencies:
                    raise ExportError(
                        "specialized executable dependency closure changed after "
                        "final manifest linkage",
                        dependency_path=["specialized_player", "imports"],
                    )
                runtime_dependencies = final_dependencies
                dependency_source = "specialized-pe-imports"
            decisions = plan_generation_vs_bake(
                optimized,
                settings,
                shader_blob_bytes=sum(len(shader.blob) for shader in package.shaders),
            )

            executable_bytes: bytes | None = None
            executable_name = ""
            if template_bytes:
                player_image = (
                    template_bytes
                    if allow_test_payloads
                    else specialized_player_bytes
                )
                if player_image is None:
                    raise ExportError(
                        "specialized player image is unavailable",
                        dependency_path=["specialized_player", "output"],
                    )
                executable_bytes = embed_package(
                    player_image, package_bytes, manifest_data
                )
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
                manifest=manifest,
                memory_budget_bytes=settings.memory_budget,
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
                player_template_bytes=(
                    len(specialized_player_bytes)
                    if specialized_player_bytes is not None
                    else len(template_bytes)
                ),
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
                    manifest=artifact.manifest,
                    memory_budget_bytes=settings.memory_budget,
                )
                _write_bytes(staging / f"{stem}.player.cpp", source.encode("utf-8"))
            else:
                source = generate_player_source(
                    package_sha256=hashlib.sha256(artifact.package_bytes).hexdigest(),
                    manifest_sha256=artifact.manifest["manifest_sha256"],
                    manifest=artifact.manifest,
                    memory_budget_bytes=settings.memory_budget,
                )
                _write_bytes(staging / f"{stem}.player.cpp", source.encode("utf-8"))

        def _report_payload() -> dict[str, Any]:
            common_artifact = artifacts[nwp.COMMON_PROFILE_ID]
            common_bytes = common_artifact.size_report["distribution"][
                "total_distributed_bytes"
            ]
            optional_bytes = sum(
                artifact.size_report["distribution"]["total_distributed_bytes"]
                for profile_id, artifact in artifacts.items()
                if profile_id != nwp.COMMON_PROFILE_ID
            )
            return {
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
                "package_sha256": hashlib.sha256(
                    common_artifact.package_bytes
                ).hexdigest(),
                "manifest_sha256": common_artifact.manifest["manifest_sha256"],
                "executable_sha256": (
                    hashlib.sha256(common_artifact.executable_bytes).hexdigest()
                    if common_artifact.executable_bytes is not None
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
                    "artifact_key": common_artifact.artifact_key,
                    "distributed_bytes": common_bytes,
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
                "common_distributed_bytes": common_bytes,
                "optional_distributed_bytes": optional_bytes,
                "common_within_byte_ceiling": common_bytes <= settings.byte_ceiling,
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
                    for shader in common_artifact.package.shaders
                ],
                "retained_dependencies": common_artifact.manifest["retained"][
                    "dependencies"
                ],
                "excluded_optional_implementations": common_artifact.manifest[
                    "excluded"
                ]["optional_implementations"],
                "excluded_authoring_infrastructure": common_artifact.manifest[
                    "excluded"
                ][
                    "authoring_infrastructure"
                ],
            },
            "generation_vs_bake": {
                "automatic_oracle": False,
                "default": "generate",
                "decisions": list(common_artifact.generation_decisions),
                "documentation": (
                    "Bake is never selected automatically. Explicit bake overrides "
                    "record the measured package-parameter content that is actually "
                    "emitted, introduced code/shaders, startup cost, and peak memory. "
                    "Unsupported requests fail closed; unavailable measurements are "
                    "labelled rather than estimated."
                ),
            },
            "evidence": {
                "windows_native_executable": (
                    "generated"
                    if common_artifact.executable_bytes is not None
                    else "pending_unavailable"
                ),
                "gpu_alias_validation": "pending_unverified",
                "note": (
                    "Missing Windows/Ada/Blackwell coverage remains pending; "
                    "executed failures remain failures."
                ),
            },
            "success": True,
            }

        def _report_with_artifacts(payload: dict[str, Any]) -> dict[str, Any]:
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
            return {**payload, "artifacts": artifact_payloads}

        # An in-directory report is part of the distributed artifact, so stage
        # and account for it before publication.  The report embeds the size
        # totals that include itself, so iterate to a deterministic fixed point.
        in_directory_report: Path | None = None
        if report_path is not None:
            try:
                candidate = Path(report_path).resolve().relative_to(
                    output_directory.resolve()
                )
            except ValueError:
                candidate = None
            if candidate is not None and candidate.parts:
                in_directory_report = candidate
        if in_directory_report is not None:
            previous: bytes | None = None
            for _iteration in range(64):
                report = _report_payload()
                payload = json.dumps(
                    _report_with_artifacts(report), indent=2, sort_keys=True
                ).encode("utf-8")
                _write_bytes(staging / in_directory_report, payload)
                _finalize_size_accounting(
                    staging,
                    artifacts,
                    settings,
                    common_extra_files=(str(in_directory_report),),
                )
                if previous == payload:
                    break
                previous = payload
            else:
                raise ExportError(
                    "in-directory report size accounting did not converge",
                    dependency_path=["size_report", "report"],
                )
            report = _report_payload()
        else:
            _finalize_size_accounting(staging, artifacts, settings)
            report = _report_payload()

        # Final publication is all-or-nothing for the directory.
        external_report_temp: Path | None = None
        if report_path is not None and in_directory_report is None:
            destination_report = Path(report_path)
            destination_report.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_tmp = tempfile.mkstemp(
                prefix=".nw05-report-", suffix=".tmp",
                dir=str(destination_report.parent),
            )
            external_report_temp = Path(raw_tmp)
            with os.fdopen(fd, "wb") as stream:
                stream.write(
                    json.dumps(
                        _report_with_artifacts(report),
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                )
                stream.flush()
                os.fsync(stream.fileno())

        def _publish_external_report() -> None:
            if external_report_temp is not None:
                os.replace(external_report_temp, Path(report_path))

        try:
            _publish_directory(
                staging, output_directory, after_publish=_publish_external_report
            )
        finally:
            if external_report_temp is not None:
                try:
                    external_report_temp.unlink()
                except FileNotFoundError:
                    pass
        staging = output_directory
        return ExportResult(
            output_directory=str(output_directory),
            settings=settings,
            artifacts=artifacts,
            report=report,
            common_artifact_key=artifacts[nwp.COMMON_PROFILE_ID].artifact_key,
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
        sibling_package = target.parent / f"{stem.name}.nwp"
        sibling_manifest = target.parent / f"{stem.name}.manifest.json"
        if sibling_package.is_file():
            artifact_files[sibling_package.name] = sibling_package.stat().st_size
        if sibling_manifest.is_file():
            artifact_files[sibling_manifest.name] = (
                sibling_manifest.stat().st_size
            )
        distribution["embedded_package_bytes"] = len(data)
        distribution["embedded_manifest_bytes"] = manifest_bytes
        distribution["embedded_content_counted_in_executable"] = True
        distribution["artifact_files"] = dict(sorted(artifact_files.items()))
        distribution["artifact_file_bytes"] = sum(artifact_files.values())
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

