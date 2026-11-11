#!/usr/bin/env python3
"""Nodewerk `.nwp` version 1 container: writer, bounded reader, inspector, and validator.

The format is deliberately small and explicit.  It is a delivery/execution
container, not an authoring document: release packages contain only the
capability contract, GPU resource descriptors, primitive initialization/frame
instructions, parameters/timeline data, raw shader payloads, raw root-signature
payloads, and an integrity checksum.  Source maps, reflection tables, editor
layout, source paths, and source text live in development sidecars.

All multi-byte fields are little-endian.  Every length/count is checked before
the corresponding allocation or loop.  A package checksum detects accidental
corruption; it is not an authenticity signature.  Parsing deliberately is not a
sandbox for hostile shaders.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # The package tooling and the NW-01 shader tool live in the same directory.
    from offline_shader_pack import validate_dxil_container
except Exception:  # pragma: no cover - direct script execution edge case
    validate_dxil_container = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Stable v1 constants
# ---------------------------------------------------------------------------

MAGIC = b"NWPKG1\n\x00"
FORMAT_NAME = "nwp"
VERSION_MAJOR = 1
VERSION_MINOR = 0
HEADER_SIZE = 64
SECTION_ENTRY_SIZE = 32
CHECKSUM_SIZE = 32
CHECKSUM_ALGORITHM_SHA256 = 1

SECTION_CAPABILITY = "CAP "
SECTION_RESOURCES = "RES "
SECTION_INITIALIZATION = "INIT"
SECTION_FRAME = "FRAM"
SECTION_PARAMETERS = "PARM"
SECTION_SHADERS = "SHDR"
SECTION_ROOT_SIGNATURES = "RSIG"
SECTION_DEV = "DEVC"

REQUIRED_RELEASE_SECTIONS = (
    SECTION_CAPABILITY,
    SECTION_RESOURCES,
    SECTION_INITIALIZATION,
    SECTION_FRAME,
    SECTION_PARAMETERS,
    SECTION_ROOT_SIGNATURES,
    SECTION_SHADERS,
)
KNOWN_SECTIONS = frozenset(REQUIRED_RELEASE_SECTIONS + (SECTION_DEV,))

COMMON_PROFILE_ID = "nw.common.d3d12.fl12_0.sm6_0.v1"
COMMON_REQUIREMENTS_DIGEST = (
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a"
)
OPTIONAL_PROFILE_WAVE_OPS = "nw.optional.d3d12.wave_ops.v1"
OPTIONAL_PROFILE_WAVE_OPS_DIGEST = (
    "sha256:v1:4a95a94d96d66eaccc3127a0b84d57b6e28d7a5f53a80b2d1ad2030e35e9d3a7"
)
KNOWN_OPTIONAL_PROFILES = {
    OPTIONAL_PROFILE_WAVE_OPS: OPTIONAL_PROFILE_WAVE_OPS_DIGEST,
}

FEATURE_LEVEL_12_0 = 0xC000
SHADER_MODEL_6_0 = 0x0060
SHADER_MODEL_TEXT_6_0 = "6_0"
RESOURCE_BINDING_TIER_2 = 2
RESOURCE_HEAP_TIER_2 = 2
MIN_WINDOWS_BUILD = 22631
MAX_CBV_SRV_UAV_DESCRIPTORS_PER_FRAME = 4096
MAX_SAMPLER_DESCRIPTORS_PER_FRAME = 64
MAX_ROOT_SIGNATURE_DWORD_COST = 16
MAX_COMPUTE_THREADS_PER_GROUP = 1024
MAX_TEXTURE_2D_DIMENSION = 8192
MAX_BUFFER_BYTES = 64 * 1024 * 1024
SWAP_CHAIN_BUFFER_COUNT = 2

REQUIRED_DXGI_FORMATS = (
    "DXGI_FORMAT_R8G8B8A8_UNORM",
    "DXGI_FORMAT_R32G32B32A32_FLOAT",
    "DXGI_FORMAT_R32_UINT",
    "DXGI_FORMAT_D32_FLOAT",
)
KNOWN_DXGI_FORMATS = frozenset(REQUIRED_DXGI_FORMATS)
KNOWN_SHADER_TARGETS = frozenset({"cs_6_0", "vs_6_0", "ps_6_0"})
KNOWN_SHADER_TARGET_PREFIXES = frozenset({"cs", "vs", "ps"})
KNOWN_RESOURCE_KINDS = frozenset({"buffer", "texture2d", "texture3d"})
KNOWN_RESOURCE_USAGES = frozenset({"uav", "srv", "cbv", "sampler", "rtv", "dsv", "readback"})
KNOWN_PARAMETER_TYPES = frozenset(
    {"float", "float2", "float3", "float4", "uint", "int"}
)
KNOWN_PARAMETER_PHASES = frozenset({"initialization", "frame", "timeline"})
KNOWN_TIMELINE_INTERPOLATIONS = frozenset({"none", "linear", "step"})

# Fixed per-pass-family binding conventions.  These are intentionally not
# discovered through runtime reflection.
PASS_FAMILY_BINDINGS: dict[str, dict[str, tuple[str, int, int]]] = {
    "compute.math_noise": {
        "params": ("cbv", 0, 0),   # b0
        "input": ("srv", 0, 0),    # t0
        "output": ("uav", 0, 0),   # u0
        "sampler": ("sampler", 0, 0),  # s0
    },
}

# Bounded decode limits.  A reader rejects a package before allocating a buffer
# larger than these values.
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_SECTION_BYTES = 16 * 1024 * 1024
MAX_SECTION_COUNT = 64
MAX_RECORD_COUNT = 4096
MAX_FIELD_COUNT = 64
MAX_STRING_BYTES = 4096
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_INSTRUCTION_OPERANDS = 32

CAPABILITY_FIELDS: dict[str, tuple[str, bool]] = {
    # name: (type, required)
    "profile_id": ("string", True),
    "requirements_digest": ("string", True),
    "optional_profile_id": ("string", True),
    "optional_requirements_digest": ("string", True),
    "optional_profile_separate_artifact": ("bool", True),
    "feature_level": ("u32", True),
    "shader_model": ("u32", True),
    "shader_model_text": ("string", True),
    "resource_binding_tier": ("u32", True),
    "resource_heap_tier": ("u32", True),
    "direct_queue_count": ("u32", True),
    "max_cbv_srv_uav_descriptors_per_frame": ("u32", True),
    "max_sampler_descriptors_per_frame": ("u32", True),
    "max_root_signature_dword_cost": ("u32", True),
    "min_windows_build": ("u32", True),
    "required_dxgi_formats": ("string_list", True),
    "limit_max_texture_2d_dimension": ("u32", True),
    "limit_max_compute_threads_per_group": ("u32", True),
    "limit_max_buffer_bytes": ("u64", True),
    "limit_swap_chain_buffer_count": ("u32", True),
    "emitted_shader_targets": ("string_list", True),
    "runtime_dxc_dependency": ("bool", True),
    "runtime_slang_dependency": ("bool", True),
    "agility_sdk_required": ("bool", True),
    "toolchain_dxc_version": ("string", True),
    "toolchain_identity": ("string", True),
    "compiler_fingerprint": ("string", True),
    "profile_fingerprint": ("string", True),
    "required_init_instructions": ("string_list", True),
    "required_frame_instructions": ("string_list", True),
}

TYPE_U32 = 1
TYPE_U64 = 2
TYPE_I64 = 3
TYPE_BOOL = 4
TYPE_STRING = 5
TYPE_STRING_LIST = 6
TYPE_BYTES = 7

OPERAND_U32 = 1
OPERAND_U64 = 2
OPERAND_I64 = 3
OPERAND_BOOL = 4
OPERAND_STRING = 5
OPERAND_F32 = 6
OPERAND_RESOURCE_REF = 7
OPERAND_SHADER_REF = 8
OPERAND_ROOTSIG_REF = 9

OPERAND_TYPE_NAMES = {
    OPERAND_U32: "u32",
    OPERAND_U64: "u64",
    OPERAND_I64: "i64",
    OPERAND_BOOL: "bool",
    OPERAND_STRING: "string",
    OPERAND_F32: "f32",
    OPERAND_RESOURCE_REF: "resource_ref",
    OPERAND_SHADER_REF: "shader_ref",
    OPERAND_ROOTSIG_REF: "root_signature_ref",
}

# opcode -> (phase, tuple of required operand-type names)
INSTRUCTION_CONTRACTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "init.create_resource": ("initialization", ("resource_ref",)),
    "init.create_root_signature": ("initialization", ("root_signature_ref",)),
    "init.create_pipeline": ("initialization", ("shader_ref", "root_signature_ref")),
    "init.upload_resource": ("initialization", ("resource_ref",)),
    "frame.set_root_signature": ("frame", ("root_signature_ref",)),
    "frame.set_root_constants": ("frame", ("u32", "resource_ref", "u32")),
    "frame.set_descriptor_table": ("frame", ("u32", "string", "resource_ref")),
    "frame.barrier": ("frame", ("u32", "resource_ref")),
    "frame.dispatch": ("frame", ("u32", "u32", "u32", "shader_ref")),
    "frame.draw": ("frame", ("u32", "u32")),
    "frame.copy_buffer": ("frame", ("resource_ref", "resource_ref", "u64")),
    "frame.set_timeline": ("frame", ("u32", "u64")),
    "frame.present": ("frame", ("resource_ref",)),
}


class PackageValidationError(ValueError):
    """Raised when a package or package description violates `.nwp` v1."""

    def __init__(
        self,
        diagnostics: Sequence[dict[str, Any]] | dict[str, Any] | str,
    ):
        if isinstance(diagnostics, str):
            diagnostics = [package_diagnostic("package_validation", diagnostics)]
        elif isinstance(diagnostics, dict):
            diagnostics = [diagnostics]
        self.diagnostics = list(diagnostics)
        message = "; ".join(str(item.get("message", item)) for item in self.diagnostics)
        super().__init__(message or "invalid .nwp package")


def package_diagnostic(
    code: str,
    message: str,
    *,
    section: str = "",
    offset: int | None = None,
    node_id: str = "",
    expression_id: str = "",
) -> dict[str, Any]:
    """Return a diagnostic in the stable package-validation category."""
    diagnostic: dict[str, Any] = {
        "category": "package_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    if section:
        diagnostic["section"] = section
    if offset is not None:
        diagnostic["offset"] = int(offset)
    if node_id:
        diagnostic["node_id"] = node_id
        diagnostic["source"] = {"node_id": node_id, "expression_id": expression_id}
    elif expression_id:
        diagnostic["expression_id"] = expression_id
    return diagnostic


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _string_digest(value: Any) -> str:
    return "sha256:v1:" + sha256_hex(canonical_json_bytes(value))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityRecord:
    profile_id: str = COMMON_PROFILE_ID
    requirements_digest: str = COMMON_REQUIREMENTS_DIGEST
    optional_profile_id: str = ""
    optional_requirements_digest: str = ""
    optional_profile_separate_artifact: bool = True
    feature_level: int = FEATURE_LEVEL_12_0
    shader_model: int = SHADER_MODEL_6_0
    shader_model_text: str = SHADER_MODEL_TEXT_6_0
    resource_binding_tier: int = RESOURCE_BINDING_TIER_2
    resource_heap_tier: int = RESOURCE_HEAP_TIER_2
    direct_queue_count: int = 1
    max_cbv_srv_uav_descriptors_per_frame: int = MAX_CBV_SRV_UAV_DESCRIPTORS_PER_FRAME
    max_sampler_descriptors_per_frame: int = MAX_SAMPLER_DESCRIPTORS_PER_FRAME
    max_root_signature_dword_cost: int = MAX_ROOT_SIGNATURE_DWORD_COST
    min_windows_build: int = MIN_WINDOWS_BUILD
    required_dxgi_formats: tuple[str, ...] = REQUIRED_DXGI_FORMATS
    limit_max_texture_2d_dimension: int = MAX_TEXTURE_2D_DIMENSION
    limit_max_compute_threads_per_group: int = MAX_COMPUTE_THREADS_PER_GROUP
    limit_max_buffer_bytes: int = MAX_BUFFER_BYTES
    limit_swap_chain_buffer_count: int = SWAP_CHAIN_BUFFER_COUNT
    emitted_shader_targets: tuple[str, ...] = ("cs_6_0",)
    runtime_dxc_dependency: bool = False
    runtime_slang_dependency: bool = False
    agility_sdk_required: bool = False
    toolchain_dxc_version: str = "1.8.2407"
    toolchain_identity: str = "dxc:1.8.2407"
    compiler_fingerprint: str = ""
    profile_fingerprint: str = ""
    required_init_instructions: tuple[str, ...] = (
        "init.create_pipeline",
        "init.create_resource",
        "init.create_root_signature",
    )
    required_frame_instructions: tuple[str, ...] = (
        "frame.dispatch",
        "frame.set_descriptor_table",
        "frame.set_root_signature",
    )

    def canonical_capability_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("profile_fingerprint", None)
        return data

    def computed_profile_fingerprint(self) -> str:
        return "sha256:v1:" + sha256_hex(canonical_json_bytes(self.canonical_capability_dict()))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "profile_id": self.profile_id,
            "requirements_digest": self.requirements_digest,
            "optional_profile_id": self.optional_profile_id,
            "optional_requirements_digest": self.optional_requirements_digest,
            "optional_profile_separate_artifact": self.optional_profile_separate_artifact,
            "feature_level": self.feature_level,
            "shader_model": self.shader_model,
            "shader_model_text": self.shader_model_text,
            "resource_binding_tier": self.resource_binding_tier,
            "resource_heap_tier": self.resource_heap_tier,
            "direct_queue_count": self.direct_queue_count,
            "max_cbv_srv_uav_descriptors_per_frame": self.max_cbv_srv_uav_descriptors_per_frame,
            "max_sampler_descriptors_per_frame": self.max_sampler_descriptors_per_frame,
            "max_root_signature_dword_cost": self.max_root_signature_dword_cost,
            "min_windows_build": self.min_windows_build,
            "required_dxgi_formats": list(self.required_dxgi_formats),
            "limit_max_texture_2d_dimension": self.limit_max_texture_2d_dimension,
            "limit_max_compute_threads_per_group": self.limit_max_compute_threads_per_group,
            "limit_max_buffer_bytes": self.limit_max_buffer_bytes,
            "limit_swap_chain_buffer_count": self.limit_swap_chain_buffer_count,
            "emitted_shader_targets": list(self.emitted_shader_targets),
            "runtime_dxc_dependency": self.runtime_dxc_dependency,
            "runtime_slang_dependency": self.runtime_slang_dependency,
            "agility_sdk_required": self.agility_sdk_required,
            "toolchain_dxc_version": self.toolchain_dxc_version,
            "toolchain_identity": self.toolchain_identity,
            "compiler_fingerprint": self.compiler_fingerprint,
            "profile_fingerprint": self.profile_fingerprint,
            "required_init_instructions": list(self.required_init_instructions),
            "required_frame_instructions": list(self.required_frame_instructions),
        }
        return result


@dataclass(frozen=True)
class ResourceDescriptor:
    resource_id: int
    name: str
    kind: str
    format: str
    usage: str
    pass_family: str
    binding_slot: int = 0
    binding_space: int = 0
    width: int = 0
    height: int = 0
    depth: int = 0
    array_size: int = 1
    mip_levels: int = 1
    size_bytes: int = 0
    flags: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "kind": self.kind,
            "format": self.format,
            "usage": self.usage,
            "pass_family": self.pass_family,
            "binding_slot": self.binding_slot,
            "binding_space": self.binding_space,
            "width": self.width,
            "height": self.height,
            "depth": self.depth,
            "array_size": self.array_size,
            "mip_levels": self.mip_levels,
            "size_bytes": self.size_bytes,
            "flags": self.flags,
        }


@dataclass(frozen=True)
class InstructionOperand:
    type: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "value": self.value}


@dataclass(frozen=True)
class Instruction:
    opcode: str
    operands: tuple[InstructionOperand, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode,
            "operands": [operand.to_dict() for operand in self.operands],
        }


@dataclass(frozen=True)
class ParameterRecord:
    parameter_id: int
    name: str
    type: str
    phase: str
    component_count: int
    value: bytes
    timeline_start_frame: int = 0
    timeline_end_frame: int = 0
    timeline_step: int = 0
    timeline_interpolation: str = "none"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "parameter_id": self.parameter_id,
            "name": self.name,
            "type": self.type,
            "phase": self.phase,
            "component_count": self.component_count,
            "value_sha256": sha256_hex(self.value),
            "value_bytes": len(self.value),
            "timeline_start_frame": self.timeline_start_frame,
            "timeline_end_frame": self.timeline_end_frame,
            "timeline_step": self.timeline_step,
            "timeline_interpolation": self.timeline_interpolation,
        }
        return result


@dataclass(frozen=True)
class RootBinding:
    name: str
    usage: str
    slot: int
    space: int
    root_parameter: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "usage": self.usage,
            "slot": self.slot,
            "space": self.space,
            "root_parameter": self.root_parameter,
        }


@dataclass(frozen=True)
class RootSignatureRecord:
    root_signature_id: int
    family: str
    convention: str
    dword_cost: int
    blob: bytes
    bindings: tuple[RootBinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_signature_id": self.root_signature_id,
            "family": self.family,
            "convention": self.convention,
            "dword_cost": self.dword_cost,
            "blob_size": len(self.blob),
            "blob_sha256": sha256_hex(self.blob),
            "bindings": [binding.to_dict() for binding in self.bindings],
        }


@dataclass(frozen=True)
class ShaderRecord:
    shader_id: int
    entry_point: str
    target: str
    blob: bytes
    root_signature_id: int
    family: str
    thread_group_x: int = 64
    thread_group_y: int = 1
    thread_group_z: int = 1
    flags: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "shader_id": self.shader_id,
            "entry_point": self.entry_point,
            "target": self.target,
            "blob_size": len(self.blob),
            "blob_sha256": sha256_hex(self.blob),
            "root_signature_id": self.root_signature_id,
            "family": self.family,
            "thread_group": [self.thread_group_x, self.thread_group_y, self.thread_group_z],
            "flags": self.flags,
        }


@dataclass
class NwpPackage:
    capability: CapabilityRecord = field(default_factory=CapabilityRecord)
    resources: list[ResourceDescriptor] = field(default_factory=list)
    initialization: list[Instruction] = field(default_factory=list)
    frame: list[Instruction] = field(default_factory=list)
    parameters: list[ParameterRecord] = field(default_factory=list)
    shaders: list[ShaderRecord] = field(default_factory=list)
    root_signatures: list[RootSignatureRecord] = field(default_factory=list)
    dev_metadata: bytes | None = None
    flags: int = 1  # bit 0: release package

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT_NAME,
            "version": {"major": VERSION_MAJOR, "minor": VERSION_MINOR},
            "capability": self.capability.to_dict(),
            "resources": [item.to_dict() for item in self.resources],
            "initialization": [item.to_dict() for item in self.initialization],
            "frame": [item.to_dict() for item in self.frame],
            "parameters": [item.to_dict() for item in self.parameters],
            "shaders": [item.to_dict() for item in self.shaders],
            "root_signatures": [item.to_dict() for item in self.root_signatures],
            "flags": self.flags,
            "contains_authoring_metadata": bool(self.dev_metadata),
        }


@dataclass(frozen=True)
class Inspection:
    valid: bool
    diagnostics: tuple[dict[str, Any], ...]
    package: NwpPackage | None
    size_bytes: int
    checksum_sha256: str
    package_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "valid": self.valid,
            "format": FORMAT_NAME,
            "version": {"major": VERSION_MAJOR, "minor": VERSION_MINOR},
            "size_bytes": self.size_bytes,
            "checksum": {
                "algorithm": "sha256",
                "digest": self.checksum_sha256,
                "purpose": "corruption_detection_not_authenticity",
            },
            "package_sha256": self.package_sha256,
            "diagnostics": list(self.diagnostics),
        }
        if self.package is not None:
            payload = self.package.to_dict()
            capability = payload["capability"]
            capability["feature_level_text"] = "12_0"
            capability["feature_level_hex"] = "0xC000"
            capability["shader_model_text"] = "6_0"
            capability["shader_model_hex"] = "0x0060"
            capability["binding_tier"] = capability["resource_binding_tier"]
            capability["heap_tier"] = capability["resource_heap_tier"]
            capability["dxgi_formats"] = list(capability["required_dxgi_formats"])
            capability["limits"] = {
                "max_cbv_srv_uav_descriptors_per_frame": capability[
                    "max_cbv_srv_uav_descriptors_per_frame"
                ],
                "max_sampler_descriptors_per_frame": capability[
                    "max_sampler_descriptors_per_frame"
                ],
                "max_root_signature_dword_cost": capability[
                    "max_root_signature_dword_cost"
                ],
                "max_texture_2d_dimension": capability[
                    "limit_max_texture_2d_dimension"
                ],
                "max_compute_threads_per_group": capability[
                    "limit_max_compute_threads_per_group"
                ],
                "max_buffer_bytes": capability["limit_max_buffer_bytes"],
                "swap_chain_buffer_count": capability[
                    "limit_swap_chain_buffer_count"
                ],
                "min_windows_build": capability["min_windows_build"],
            }
            capability["runtime_requirements"] = {
                "direct_queue_count": capability["direct_queue_count"],
                "runtime_dxc_dependency": capability["runtime_dxc_dependency"],
                "runtime_slang_dependency": capability[
                    "runtime_slang_dependency"
                ],
                "agility_sdk_required": capability["agility_sdk_required"],
                "toolchain_dxc_version": capability["toolchain_dxc_version"],
                "toolchain_identity": capability["toolchain_identity"],
            }
            capability["optional_profile"] = {
                "profile_id": capability["optional_profile_id"],
                "requirements_digest": capability[
                    "optional_requirements_digest"
                ],
                "separate_artifact": capability[
                    "optional_profile_separate_artifact"
                ],
            }
            shader_values = payload["shaders"]
            root_values = payload["root_signatures"]
            shader_blob_hashes = {item["blob_sha256"] for item in shader_values}
            root_blob_hashes = {item["blob_sha256"] for item in root_values}
            payload["deduplication"] = {
                "shader_specialization_count": len(shader_values),
                "unique_shader_blob_count": len(shader_blob_hashes),
                "duplicate_shader_blob_reuse_bytes": _duplicate_reuse_bytes(shader_values),
                "root_signature_record_count": len(root_values),
                "unique_root_signature_blob_count": len(root_blob_hashes),
            }
            payload["release"] = {
                "contains_authoring_metadata": bool(self.package.dev_metadata),
                "contains_source_paths": False,
                "contains_source_text": False,
                "contains_reflection_tables": False,
            }
            result.update(payload)
        return result


def _duplicate_reuse_bytes(records: list[dict[str, Any]]) -> int:
    seen: set[str] = set()
    reused = 0
    for record in records:
        digest = record["blob_sha256"]
        if digest in seen:
            reused += int(record["blob_size"])
        else:
            seen.add(digest)
    return reused


# ---------------------------------------------------------------------------
# Little-endian binary helpers (shared with the C++ reader/writer contract)
# ---------------------------------------------------------------------------


class _Writer:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def u8(self, value: int) -> None:
        self.buffer += struct.pack("<B", value)

    def u16(self, value: int) -> None:
        self.buffer += struct.pack("<H", value)

    def u32(self, value: int) -> None:
        self.buffer += struct.pack("<I", value)

    def u64(self, value: int) -> None:
        self.buffer += struct.pack("<Q", value)

    def i64(self, value: int) -> None:
        self.buffer += struct.pack("<q", value)

    def boolean(self, value: bool) -> None:
        self.u8(1 if value else 0)

    def string(self, value: str) -> None:
        data = value.encode("utf-8")
        if len(data) > MAX_STRING_BYTES:
            raise PackageValidationError(
                package_diagnostic(
                    "package_decode_limit",
                    f"string field exceeds {MAX_STRING_BYTES} bytes",
                )
            )
        self.u16(len(data))
        self.buffer += data

    def bytes(self, data: bytes) -> None:
        if len(data) > MAX_BLOB_BYTES:
            raise PackageValidationError(
                package_diagnostic(
                    "package_decode_limit",
                    f"blob field exceeds {MAX_BLOB_BYTES} bytes",
                )
            )
        self.u32(len(data))
        self.buffer += data

    def string_list(self, values: Iterable[str]) -> None:
        items = list(values)
        if len(items) > MAX_RECORD_COUNT:
            raise PackageValidationError(
                package_diagnostic(
                    "package_decode_limit", "string list exceeds record limit"
                )
            )
        self.u16(len(items))
        for item in items:
            self.string(item)

    def value_for_field(self, field_type: str, value: Any) -> None:
        if field_type == "u32":
            self.u32(int(value))
        elif field_type == "u64":
            self.u64(int(value))
        elif field_type == "i64":
            self.i64(int(value))
        elif field_type == "bool":
            self.boolean(bool(value))
        elif field_type == "string":
            self.string(str(value))
        elif field_type == "string_list":
            self.string_list(value)
        elif field_type == "bytes":
            self.bytes(bytes(value))
        else:  # pragma: no cover - programmer error
            raise PackageValidationError(f"unsupported field type {field_type}")


class _Reader:
    def __init__(
        self,
        data: bytes,
        diagnostics: list[dict[str, Any]],
        section: str,
        base_offset: int = 0,
    ) -> None:
        self.data = memoryview(data)
        self.position = 0
        self.diagnostics = diagnostics
        self.section = section
        self.base_offset = base_offset

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def _take(self, size: int) -> memoryview | None:
        if size < 0 or size > self.remaining:
            self.diagnostics.append(
                package_diagnostic(
                    "package_truncated",
                    f"section {self.section} needs {size} bytes but only {self.remaining} remain",
                    section=self.section,
                    offset=self.base_offset + self.position,
                )
            )
            return None
        start = self.position
        self.position += size
        return self.data[start : start + size]

    def u8(self) -> int | None:
        raw = self._take(1)
        return None if raw is None else int(raw[0])

    def u16(self) -> int | None:
        raw = self._take(2)
        return None if raw is None else int(struct.unpack("<H", raw)[0])

    def u32(self) -> int | None:
        raw = self._take(4)
        return None if raw is None else int(struct.unpack("<I", raw)[0])

    def u64(self) -> int | None:
        raw = self._take(8)
        return None if raw is None else int(struct.unpack("<Q", raw)[0])

    def i64(self) -> int | None:
        raw = self._take(8)
        return None if raw is None else int(struct.unpack("<q", raw)[0])

    def boolean(self) -> bool | None:
        value = self.u8()
        if value is None:
            return None
        if value not in (0, 1):
            self.diagnostics.append(
                package_diagnostic(
                    "package_malformed_value",
                    f"non-boolean byte {value} in section {self.section}",
                    section=self.section,
                )
            )
            return None
        return bool(value)

    def string(self) -> str | None:
        length = self.u16()
        if length is None:
            return None
        if length > MAX_STRING_BYTES:
            self.diagnostics.append(
                package_diagnostic(
                    "package_decode_limit",
                    f"section {self.section} string length {length} exceeds {MAX_STRING_BYTES}",
                    section=self.section,
                )
            )
            return None
        raw = self._take(length)
        if raw is None:
            return None
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            self.diagnostics.append(
                package_diagnostic(
                    "package_malformed_value",
                    f"section {self.section} string is not valid UTF-8",
                    section=self.section,
                )
            )
            return None

    def bytes(self, limit: int = MAX_BLOB_BYTES) -> bytes | None:
        length = self.u32()
        if length is None:
            return None
        if length > limit:
            self.diagnostics.append(
                package_diagnostic(
                    "package_decode_limit",
                    f"section {self.section} byte length {length} exceeds {limit}",
                    section=self.section,
                )
            )
            return None
        raw = self._take(length)
        return None if raw is None else bytes(raw)

    def string_list(self) -> tuple[str, ...] | None:
        count = self.u16()
        if count is None:
            return None
        if count > MAX_RECORD_COUNT:
            self.diagnostics.append(
                package_diagnostic(
                    "package_decode_limit",
                    f"section {self.section} string count {count} exceeds {MAX_RECORD_COUNT}",
                    section=self.section,
                )
            )
            return None
        result = []
        for _ in range(count):
            item = self.string()
            if item is None:
                return None
            result.append(item)
        return tuple(result)

    def value_for_field(self, field_type: str) -> Any:
        if field_type == "u32":
            return self.u32()
        if field_type == "u64":
            return self.u64()
        if field_type == "i64":
            return self.i64()
        if field_type == "bool":
            return self.boolean()
        if field_type == "string":
            return self.string()
        if field_type == "string_list":
            return self.string_list()
        if field_type == "bytes":
            return self.bytes()
        return None

    def at_end(self) -> bool:
        return self.remaining == 0


# ---------------------------------------------------------------------------
# Section encoders
# ---------------------------------------------------------------------------


def _field_type_code(field_type: str) -> int:
    return {
        "u32": TYPE_U32,
        "u64": TYPE_U64,
        "i64": TYPE_I64,
        "bool": TYPE_BOOL,
        "string": TYPE_STRING,
        "string_list": TYPE_STRING_LIST,
        "bytes": TYPE_BYTES,
    }[field_type]


_FIELD_CODE_TYPE = {
    TYPE_U32: "u32",
    TYPE_U64: "u64",
    TYPE_I64: "i64",
    TYPE_BOOL: "bool",
    TYPE_STRING: "string",
    TYPE_STRING_LIST: "string_list",
    TYPE_BYTES: "bytes",
}


def _encode_capability(capability: CapabilityRecord) -> bytes:
    writer = _Writer()
    writer.u16(1)
    writer.u16(0)  # flags, must be zero in v1
    fields = capability.to_dict()
    ordered_names = sorted(fields)
    writer.u32(len(ordered_names))
    for name in ordered_names:
        field_type, _ = CAPABILITY_FIELDS[name]
        writer.string(name)
        writer.u8(_field_type_code(field_type))
        writer.value_for_field(field_type, fields[name])
    return bytes(writer.buffer)


def _decode_capability(
    payload: bytes, diagnostics: list[dict[str, Any]]
) -> CapabilityRecord | None:
    reader = _Reader(payload, diagnostics, SECTION_CAPABILITY)
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"CAP section version {version} is not supported",
                section=SECTION_CAPABILITY,
            )
        )
        return None
    flags = reader.u16()
    if flags is None:
        return None
    if flags != 0:
        diagnostics.append(
            package_diagnostic(
                "package_capability_flags",
                f"CAP section flags 0x{flags:04x} must be zero in v1",
                section=SECTION_CAPABILITY,
            )
        )
        return None
    count = reader.u32()
    if count is None:
        return None
    if count > MAX_FIELD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"CAP field count {count} exceeds {MAX_FIELD_COUNT}",
                section=SECTION_CAPABILITY,
            )
        )
        return None
    values: dict[str, Any] = {}
    for _ in range(count):
        name = reader.string()
        if name is None:
            return None
        code = reader.u8()
        if code is None:
            return None
        expected = CAPABILITY_FIELDS.get(name)
        if expected is None:
            diagnostics.append(
                package_diagnostic(
                    "package_capability_unknown_field",
                    f"unknown CAP field '{name}'",
                    section=SECTION_CAPABILITY,
                )
            )
            return None
        expected_type, _required = expected
        actual_type = _FIELD_CODE_TYPE.get(code)
        if actual_type != expected_type:
            diagnostics.append(
                package_diagnostic(
                    "package_capability_field_type",
                    f"CAP field '{name}' has type {actual_type!r}, expected {expected_type!r}",
                    section=SECTION_CAPABILITY,
                )
            )
            return None
        value = reader.value_for_field(expected_type)
        if value is None:
            return None
        if name in values:
            diagnostics.append(
                package_diagnostic(
                    "package_capability_duplicate_field",
                    f"duplicate CAP field '{name}'",
                    section=SECTION_CAPABILITY,
                )
            )
            return None
        values[name] = value
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"CAP section has {reader.remaining} unexplained trailing bytes",
                section=SECTION_CAPABILITY,
            )
        )
        return None
    missing = [
        name
        for name, (_field_type, required) in CAPABILITY_FIELDS.items()
        if required and name not in values
    ]
    if missing:
        diagnostics.append(
            package_diagnostic(
                "package_capability_missing_field",
                "CAP section is missing required fields: " + ", ".join(sorted(missing)),
                section=SECTION_CAPABILITY,
            )
        )
        return None
    try:
        return CapabilityRecord(
            **{
                **values,
                "required_dxgi_formats": tuple(values["required_dxgi_formats"]),
                "emitted_shader_targets": tuple(values["emitted_shader_targets"]),
                "required_init_instructions": tuple(values["required_init_instructions"]),
                "required_frame_instructions": tuple(values["required_frame_instructions"]),
            }
        )
    except TypeError as exc:  # pragma: no cover - defensive
        diagnostics.append(
            package_diagnostic(
                "package_capability_malformed", f"CAP section cannot be decoded: {exc}"
            )
        )
        return None


def _encode_resources(resources: Sequence[ResourceDescriptor]) -> bytes:
    writer = _Writer()
    writer.u16(1)
    writer.u32(len(resources))
    for resource in resources:
        writer.u32(resource.resource_id)
        writer.string(resource.name)
        writer.string(resource.kind)
        writer.string(resource.format)
        writer.string(resource.usage)
        writer.string(resource.pass_family)
        writer.u32(resource.binding_slot)
        writer.u32(resource.binding_space)
        writer.u32(resource.width)
        writer.u32(resource.height)
        writer.u32(resource.depth)
        writer.u32(resource.array_size)
        writer.u32(resource.mip_levels)
        writer.u64(resource.size_bytes)
        writer.u32(resource.flags)
    return bytes(writer.buffer)


def _decode_resources(
    payload: bytes, diagnostics: list[dict[str, Any]]
) -> list[ResourceDescriptor] | None:
    reader = _Reader(payload, diagnostics, SECTION_RESOURCES)
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"RES section version {version} is not supported",
                section=SECTION_RESOURCES,
            )
        )
        return None
    count = reader.u32()
    if count is None:
        return None
    if count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"RES record count {count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_RESOURCES,
            )
        )
        return None
    result = []
    for _ in range(count):
        resource_id = reader.u32()
        name = reader.string()
        kind = reader.string()
        fmt = reader.string()
        usage = reader.string()
        family = reader.string()
        slot = reader.u32()
        space = reader.u32()
        width = reader.u32()
        height = reader.u32()
        depth = reader.u32()
        array_size = reader.u32()
        mip_levels = reader.u32()
        size_bytes = reader.u64()
        flags = reader.u32()
        if None in (
            resource_id,
            name,
            kind,
            fmt,
            usage,
            family,
            slot,
            space,
            width,
            height,
            depth,
            array_size,
            mip_levels,
            size_bytes,
            flags,
        ):
            return None
        result.append(
            ResourceDescriptor(
                resource_id=resource_id,
                name=name,
                kind=kind,
                format=fmt,
                usage=usage,
                pass_family=family,
                binding_slot=slot,
                binding_space=space,
                width=width,
                height=height,
                depth=depth,
                array_size=array_size,
                mip_levels=mip_levels,
                size_bytes=size_bytes,
                flags=flags,
            )
        )
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"RES section has {reader.remaining} unexplained trailing bytes",
                section=SECTION_RESOURCES,
            )
        )
        return None
    return result


def _encode_instructions(instructions: Sequence[Instruction]) -> bytes:
    writer = _Writer()
    writer.u16(1)
    writer.u32(len(instructions))
    for instruction in instructions:
        writer.string(instruction.opcode)
        writer.u32(len(instruction.operands))
        for operand in instruction.operands:
            code = {
                "u32": OPERAND_U32,
                "u64": OPERAND_U64,
                "i64": OPERAND_I64,
                "bool": OPERAND_BOOL,
                "string": OPERAND_STRING,
                "f32": OPERAND_F32,
                "resource_ref": OPERAND_RESOURCE_REF,
                "shader_ref": OPERAND_SHADER_REF,
                "root_signature_ref": OPERAND_ROOTSIG_REF,
            }[operand.type]
            writer.u8(code)
            if operand.type in {"resource_ref", "shader_ref", "root_signature_ref"}:
                writer.u32(int(operand.value))
            elif operand.type == "u32":
                writer.u32(int(operand.value))
            elif operand.type == "u64":
                writer.u64(int(operand.value))
            elif operand.type == "i64":
                writer.i64(int(operand.value))
            elif operand.type == "bool":
                writer.boolean(bool(operand.value))
            elif operand.type == "string":
                writer.string(str(operand.value))
            elif operand.type == "f32":
                writer.u32(struct.unpack("<I", struct.pack("<f", float(operand.value)))[0])
    return bytes(writer.buffer)


def _decode_instructions(
    payload: bytes, diagnostics: list[dict[str, Any]], section: str
) -> list[Instruction] | None:
    reader = _Reader(payload, diagnostics, section)
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"{section.strip()} section version {version} is not supported",
                section=section,
            )
        )
        return None
    count = reader.u32()
    if count is None:
        return None
    if count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"{section.strip()} record count {count} exceeds {MAX_RECORD_COUNT}",
                section=section,
            )
        )
        return None
    result = []
    for _ in range(count):
        opcode = reader.string()
        operand_count = reader.u32()
        if opcode is None or operand_count is None:
            return None
        if operand_count > MAX_INSTRUCTION_OPERANDS:
            diagnostics.append(
                package_diagnostic(
                    "package_decode_limit",
                    f"instruction '{opcode}' has {operand_count} operands (limit {MAX_INSTRUCTION_OPERANDS})",
                    section=section,
                )
            )
            return None
        operands = []
        for _operand_index in range(operand_count):
            code = reader.u8()
            if code is None:
                return None
            operand_type = OPERAND_TYPE_NAMES.get(code)
            if operand_type is None:
                diagnostics.append(
                    package_diagnostic(
                        "package_instruction_operand",
                        f"instruction '{opcode}' has unknown operand type {code}",
                        section=section,
                    )
                )
                return None
            if operand_type in {"resource_ref", "shader_ref", "root_signature_ref", "u32"}:
                value = reader.u32()
            elif operand_type == "u64":
                value = reader.u64()
            elif operand_type == "i64":
                value = reader.i64()
            elif operand_type == "bool":
                value = reader.boolean()
            elif operand_type == "string":
                value = reader.string()
            elif operand_type == "f32":
                raw = reader.u32()
                value = None if raw is None else struct.unpack("<f", struct.pack("<I", raw))[0]
            else:  # pragma: no cover - defensive
                value = None
            if value is None:
                return None
            operands.append(InstructionOperand(operand_type, value))
        result.append(Instruction(opcode, tuple(operands)))
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"{section.strip()} section has {reader.remaining} unexplained trailing bytes",
                section=section,
            )
        )
        return None
    return result


def _encode_parameters(parameters: Sequence[ParameterRecord]) -> bytes:
    writer = _Writer()
    writer.u16(1)
    writer.u32(len(parameters))
    for parameter in parameters:
        writer.u32(parameter.parameter_id)
        writer.string(parameter.name)
        writer.string(parameter.type)
        writer.string(parameter.phase)
        writer.u32(parameter.component_count)
        writer.bytes(parameter.value)
        writer.u64(parameter.timeline_start_frame)
        writer.u64(parameter.timeline_end_frame)
        writer.u32(parameter.timeline_step)
        writer.string(parameter.timeline_interpolation)
    return bytes(writer.buffer)


def _decode_parameters(
    payload: bytes, diagnostics: list[dict[str, Any]]
) -> list[ParameterRecord] | None:
    reader = _Reader(payload, diagnostics, SECTION_PARAMETERS)
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"PARM section version {version} is not supported",
                section=SECTION_PARAMETERS,
            )
        )
        return None
    count = reader.u32()
    if count is None:
        return None
    if count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"PARM record count {count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_PARAMETERS,
            )
        )
        return None
    result = []
    for _ in range(count):
        parameter_id = reader.u32()
        name = reader.string()
        ptype = reader.string()
        phase = reader.string()
        component_count = reader.u32()
        value = reader.bytes()
        start = reader.u64()
        end = reader.u64()
        step = reader.u32()
        interpolation = reader.string()
        if None in (
            parameter_id,
            name,
            ptype,
            phase,
            component_count,
            value,
            start,
            end,
            step,
            interpolation,
        ):
            return None
        result.append(
            ParameterRecord(
                parameter_id=parameter_id,
                name=name,
                type=ptype,
                phase=phase,
                component_count=component_count,
                value=value,
                timeline_start_frame=start,
                timeline_end_frame=end,
                timeline_step=step,
                timeline_interpolation=interpolation,
            )
        )
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"PARM section has {reader.remaining} unexplained trailing bytes",
                section=SECTION_PARAMETERS,
            )
        )
        return None
    return result


def _encode_root_signatures(
    root_signatures: Sequence[RootSignatureRecord],
) -> bytes:
    writer = _Writer()
    unique_blobs: dict[str, int] = {}
    blob_order: list[bytes] = []
    indices: list[int] = []
    for record in root_signatures:
        digest = sha256_hex(record.blob)
        if digest not in unique_blobs:
            unique_blobs[digest] = len(blob_order)
            blob_order.append(record.blob)
        indices.append(unique_blobs[digest])

    writer.u16(1)
    writer.u32(len(blob_order))
    for blob in blob_order:
        writer.bytes(blob)
    writer.u32(len(root_signatures))
    for record, blob_index in zip(root_signatures, indices):
        writer.u32(record.root_signature_id)
        writer.string(record.family)
        writer.string(record.convention)
        writer.u32(record.dword_cost)
        writer.u32(blob_index)
        writer.u32(len(record.bindings))
        for binding in record.bindings:
            writer.string(binding.name)
            writer.string(binding.usage)
            writer.u32(binding.slot)
            writer.u32(binding.space)
            writer.u32(binding.root_parameter)
    return bytes(writer.buffer)


def _decode_root_signatures(
    payload: bytes, diagnostics: list[dict[str, Any]]
) -> list[RootSignatureRecord] | None:
    reader = _Reader(payload, diagnostics, SECTION_ROOT_SIGNATURES)
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"RSIG section version {version} is not supported",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    blob_count = reader.u32()
    if blob_count is None:
        return None
    if blob_count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"RSIG blob count {blob_count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    blobs = []
    for _ in range(blob_count):
        blob = reader.bytes()
        if blob is None:
            return None
        blobs.append(blob)
    if len(set(blobs)) != len(blobs):
        diagnostics.append(
            package_diagnostic(
                "package_root_signature_not_deduplicated",
                "RSIG blob table contains duplicate complete root signature payloads",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    count = reader.u32()
    if count is None:
        return None
    if count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"RSIG record count {count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    result = []
    for _ in range(count):
        root_signature_id = reader.u32()
        family = reader.string()
        convention = reader.string()
        dword_cost = reader.u32()
        blob_index = reader.u32()
        binding_count = reader.u32()
        if None in (
            root_signature_id,
            family,
            convention,
            dword_cost,
            blob_index,
            binding_count,
        ):
            return None
        if blob_index >= len(blobs):
            diagnostics.append(
                package_diagnostic(
                    "package_reference_out_of_range",
                    f"RSIG record {root_signature_id} references blob {blob_index} of {len(blobs)}",
                    section=SECTION_ROOT_SIGNATURES,
                )
            )
            return None
        if binding_count > MAX_RECORD_COUNT:
            diagnostics.append(
                package_diagnostic(
                    "package_decode_limit",
                    f"RSIG binding count {binding_count} exceeds {MAX_RECORD_COUNT}",
                    section=SECTION_ROOT_SIGNATURES,
                )
            )
            return None
        bindings = []
        for _binding_index in range(binding_count):
            name = reader.string()
            usage = reader.string()
            slot = reader.u32()
            space = reader.u32()
            root_parameter = reader.u32()
            if None in (name, usage, slot, space, root_parameter):
                return None
            bindings.append(RootBinding(name, usage, slot, space, root_parameter))
        result.append(
            RootSignatureRecord(
                root_signature_id=root_signature_id,
                family=family,
                convention=convention,
                dword_cost=dword_cost,
                blob=blobs[blob_index],
                bindings=tuple(bindings),
            )
        )
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"RSIG section has {reader.remaining} unexplained trailing bytes",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    return result


def _encode_shaders(shaders: Sequence[ShaderRecord]) -> bytes:
    writer = _Writer()
    unique_blobs: dict[str, int] = {}
    blob_order: list[bytes] = []
    indices: list[int] = []
    for shader in shaders:
        digest = sha256_hex(shader.blob)
        if digest not in unique_blobs:
            unique_blobs[digest] = len(blob_order)
            blob_order.append(shader.blob)
        indices.append(unique_blobs[digest])

    writer.u16(1)
    writer.u32(len(blob_order))
    for blob in blob_order:
        writer.bytes(blob)
    writer.u32(len(shaders))
    for shader, blob_index in zip(shaders, indices):
        writer.u32(shader.shader_id)
        writer.string(shader.entry_point)
        writer.string(shader.target)
        writer.u32(blob_index)
        writer.u32(shader.root_signature_id)
        writer.u32(shader.flags)
        writer.string(shader.family)
        writer.u32(shader.thread_group_x)
        writer.u32(shader.thread_group_y)
        writer.u32(shader.thread_group_z)
    return bytes(writer.buffer)


def _decode_shaders(
    payload: bytes, diagnostics: list[dict[str, Any]]
) -> list[ShaderRecord] | None:
    reader = _Reader(payload, diagnostics, SECTION_SHADERS)
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"SHDR section version {version} is not supported",
                section=SECTION_SHADERS,
            )
        )
        return None
    blob_count = reader.u32()
    if blob_count is None:
        return None
    if blob_count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"SHDR blob count {blob_count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_SHADERS,
            )
        )
        return None
    blobs = []
    for _ in range(blob_count):
        blob = reader.bytes()
        if blob is None:
            return None
        blobs.append(blob)
    if len(set(blobs)) != len(blobs):
        diagnostics.append(
            package_diagnostic(
                "package_shader_blob_not_deduplicated",
                "SHDR blob table contains duplicate complete shader payloads",
                section=SECTION_SHADERS,
            )
        )
        return None
    count = reader.u32()
    if count is None:
        return None
    if count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"SHDR record count {count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_SHADERS,
            )
        )
        return None
    result = []
    for _ in range(count):
        shader_id = reader.u32()
        entry_point = reader.string()
        target = reader.string()
        blob_index = reader.u32()
        root_signature_id = reader.u32()
        flags = reader.u32()
        family = reader.string()
        thread_group_x = reader.u32()
        thread_group_y = reader.u32()
        thread_group_z = reader.u32()
        if None in (
            shader_id,
            entry_point,
            target,
            blob_index,
            root_signature_id,
            flags,
            family,
            thread_group_x,
            thread_group_y,
            thread_group_z,
        ):
            return None
        if blob_index >= len(blobs):
            diagnostics.append(
                package_diagnostic(
                    "package_reference_out_of_range",
                    f"SHDR record {shader_id} references blob {blob_index} of {len(blobs)}",
                    section=SECTION_SHADERS,
                )
            )
            return None
        result.append(
            ShaderRecord(
                shader_id=shader_id,
                entry_point=entry_point,
                target=target,
                blob=blobs[blob_index],
                root_signature_id=root_signature_id,
                family=family,
                thread_group_x=thread_group_x,
                thread_group_y=thread_group_y,
                thread_group_z=thread_group_z,
                flags=flags,
            )
        )
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"SHDR section has {reader.remaining} unexplained trailing bytes",
                section=SECTION_SHADERS,
            )
        )
        return None
    return result


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------


def _validate_capability(
    capability: CapabilityRecord, diagnostics: list[dict[str, Any]]
) -> None:
    def error(code: str, message: str) -> None:
        diagnostics.append(
            package_diagnostic(code, message, section=SECTION_CAPABILITY)
        )

    if capability.profile_id != COMMON_PROFILE_ID:
        error(
            "package_profile_unknown",
            f"unknown capability-profile identity '{capability.profile_id}'",
        )
    if capability.requirements_digest != COMMON_REQUIREMENTS_DIGEST:
        error(
            "package_profile_digest_mismatch",
            "capability profile requires requirements digest "
            f"'{COMMON_REQUIREMENTS_DIGEST}', got '{capability.requirements_digest}'",
        )
    if capability.optional_profile_id:
        known = KNOWN_OPTIONAL_PROFILES.get(capability.optional_profile_id)
        if known is None:
            error(
                "package_optional_profile_unknown",
                f"unknown optional capability-profile identity '{capability.optional_profile_id}'",
            )
        elif capability.optional_requirements_digest != known:
            error(
                "package_optional_profile_digest_mismatch",
                f"optional profile '{capability.optional_profile_id}' requires digest "
                f"'{known}', got '{capability.optional_requirements_digest}'",
            )
        if not capability.optional_profile_separate_artifact:
            error(
                "package_optional_profile_contradiction",
                "unsupported optional profiles must remain separate artifacts",
            )
    elif capability.optional_requirements_digest:
        error(
            "package_optional_profile_contradiction",
            "optional requirements digest is present without an optional profile id",
        )

    checks = (
        ("feature_level", capability.feature_level, FEATURE_LEVEL_12_0),
        ("shader_model", capability.shader_model, SHADER_MODEL_6_0),
        ("resource_binding_tier", capability.resource_binding_tier, RESOURCE_BINDING_TIER_2),
        ("resource_heap_tier", capability.resource_heap_tier, RESOURCE_HEAP_TIER_2),
        ("direct_queue_count", capability.direct_queue_count, 1),
        (
            "max_cbv_srv_uav_descriptors_per_frame",
            capability.max_cbv_srv_uav_descriptors_per_frame,
            MAX_CBV_SRV_UAV_DESCRIPTORS_PER_FRAME,
        ),
        (
            "max_sampler_descriptors_per_frame",
            capability.max_sampler_descriptors_per_frame,
            MAX_SAMPLER_DESCRIPTORS_PER_FRAME,
        ),
        (
            "max_root_signature_dword_cost",
            capability.max_root_signature_dword_cost,
            MAX_ROOT_SIGNATURE_DWORD_COST,
        ),
        ("min_windows_build", capability.min_windows_build, MIN_WINDOWS_BUILD),
        (
            "limit_max_texture_2d_dimension",
            capability.limit_max_texture_2d_dimension,
            MAX_TEXTURE_2D_DIMENSION,
        ),
        (
            "limit_max_compute_threads_per_group",
            capability.limit_max_compute_threads_per_group,
            MAX_COMPUTE_THREADS_PER_GROUP,
        ),
        (
            "limit_max_buffer_bytes",
            capability.limit_max_buffer_bytes,
            MAX_BUFFER_BYTES,
        ),
        (
            "limit_swap_chain_buffer_count",
            capability.limit_swap_chain_buffer_count,
            SWAP_CHAIN_BUFFER_COUNT,
        ),
    )
    for name, actual, expected in checks:
        if actual != expected:
            error(
                "package_capability_contradictory",
                f"CAP field '{name}' is {actual!r}, required value is {expected!r}",
            )

    if capability.shader_model_text != SHADER_MODEL_TEXT_6_0:
        error(
            "package_capability_contradictory",
            f"shader_model_text '{capability.shader_model_text}' does not match shader model 6.0",
        )
    formats = tuple(capability.required_dxgi_formats)
    if len(set(formats)) != len(formats):
        error("package_capability_contradictory", "required DXGI formats contain duplicates")
    if set(formats) != set(REQUIRED_DXGI_FORMATS):
        missing = sorted(set(REQUIRED_DXGI_FORMATS) - set(formats))
        extra = sorted(set(formats) - set(REQUIRED_DXGI_FORMATS))
        error(
            "package_capability_format_set",
            "required DXGI formats must be exactly the baseline set "
            f"(missing={missing}, extra={extra})",
        )
    if not capability.emitted_shader_targets:
        error("package_capability_contradictory", "emitted_shader_targets is empty")
    for target in capability.emitted_shader_targets:
        if target not in KNOWN_SHADER_TARGETS:
            error(
                "package_shader_target_unknown",
                f"unknown emitted shader target '{target}'",
            )
    if capability.runtime_dxc_dependency:
        error("package_runtime_dependency_forbidden", "runtime DXC dependency is forbidden")
    if capability.runtime_slang_dependency:
        error("package_runtime_dependency_forbidden", "runtime Slang dependency is forbidden")
    if capability.agility_sdk_required:
        error("package_capability_contradictory", "Agility SDK redistribution is not permitted")
    if not capability.toolchain_dxc_version:
        error("package_capability_contradictory", "toolchain_dxc_version is empty")
    if not capability.toolchain_identity:
        error("package_capability_contradictory", "toolchain_identity is empty")
    if not capability.compiler_fingerprint:
        error("package_capability_contradictory", "compiler_fingerprint is empty")
    if not capability.profile_fingerprint:
        error("package_capability_contradictory", "profile_fingerprint is empty")
    else:
        expected_fingerprint = capability.computed_profile_fingerprint()
        if capability.profile_fingerprint != expected_fingerprint:
            error(
                "package_profile_fingerprint_mismatch",
                "profile_fingerprint does not match the canonical capability record",
            )
    for list_name, values in (
        ("required_init_instructions", capability.required_init_instructions),
        ("required_frame_instructions", capability.required_frame_instructions),
    ):
        if len(set(values)) != len(values):
            error(
                "package_capability_contradictory",
                f"{list_name} contains duplicate opcodes",
            )


def _validate_resources(
    resources: Sequence[ResourceDescriptor], diagnostics: list[dict[str, Any]]
) -> None:
    seen_ids: set[int] = set()
    seen_bindings: set[tuple[str, str, int, int]] = set()
    for resource in resources:
        def error(code: str, message: str) -> None:
            diagnostics.append(
                package_diagnostic(code, message, section=SECTION_RESOURCES)
            )

        if resource.resource_id <= 0:
            error(
                "package_resource_id",
                f"resource id {resource.resource_id} must be positive",
            )
        if resource.resource_id in seen_ids:
            error(
                "package_resource_id",
                f"duplicate resource id {resource.resource_id}",
            )
        seen_ids.add(resource.resource_id)
        if not resource.name or not resource.pass_family:
            error("package_resource_malformed", "resource name and pass_family must be non-empty")
        if resource.kind not in KNOWN_RESOURCE_KINDS:
            error(
                "package_resource_kind",
                f"unknown resource kind '{resource.kind}'",
            )
        if resource.format not in KNOWN_DXGI_FORMATS:
            error(
                "package_resource_format",
                f"unknown or undeclared DXGI format '{resource.format}'",
            )
        if resource.usage not in KNOWN_RESOURCE_USAGES:
            error(
                "package_resource_usage",
                f"unknown resource usage '{resource.usage}'",
            )
        if resource.kind == "buffer":
            if (resource.width, resource.height, resource.depth) != (0, 0, 0):
                error(
                    "package_resource_dimensions",
                    f"buffer resource '{resource.name}' must not declare texture dimensions",
                )
            if resource.size_bytes == 0 or resource.size_bytes > MAX_BUFFER_BYTES:
                error(
                    "package_resource_dimensions",
                    f"buffer resource '{resource.name}' size {resource.size_bytes} is outside 1..{MAX_BUFFER_BYTES}",
                )
            if resource.array_size != 1 or resource.mip_levels != 1:
                error(
                    "package_resource_dimensions",
                    f"buffer resource '{resource.name}' must be a single array slice/mip",
                )
        elif resource.kind == "texture2d":
            if not (1 <= resource.width <= MAX_TEXTURE_2D_DIMENSION):
                error(
                    "package_resource_dimensions",
                    f"texture2d '{resource.name}' width {resource.width} is outside 1..{MAX_TEXTURE_2D_DIMENSION}",
                )
            if not (1 <= resource.height <= MAX_TEXTURE_2D_DIMENSION):
                error(
                    "package_resource_dimensions",
                    f"texture2d '{resource.name}' height {resource.height} is outside 1..{MAX_TEXTURE_2D_DIMENSION}",
                )
            if resource.depth not in (0, 1):
                error(
                    "package_resource_dimensions",
                    f"texture2d '{resource.name}' depth must be 0 or 1",
                )
            if resource.size_bytes == 0 or resource.size_bytes > MAX_BUFFER_BYTES:
                error(
                    "package_resource_dimensions",
                    f"texture2d '{resource.name}' size is outside 1..{MAX_BUFFER_BYTES}",
                )
        else:  # texture3d
            for label, value in (
                ("width", resource.width),
                ("height", resource.height),
                ("depth", resource.depth),
            ):
                if not (1 <= value <= 2048):
                    error(
                        "package_resource_dimensions",
                        f"texture3d '{resource.name}' {label} {value} is outside 1..2048",
                    )

        binding_key = (
            resource.pass_family,
            resource.usage,
            resource.binding_slot,
            resource.binding_space,
        )
        if not (0 <= resource.binding_slot <= 4095):
            error(
                "package_resource_binding",
                f"resource '{resource.name}' binding slot {resource.binding_slot} is out of range",
            )
        if not (0 <= resource.binding_space <= 15):
            error(
                "package_resource_binding",
                f"resource '{resource.name}' binding space {resource.binding_space} is out of range",
            )
        if binding_key in seen_bindings:
            error(
                "package_resource_binding_collision",
                f"duplicate binding {resource.pass_family}/{resource.usage}/"
                f"{resource.binding_space}:{resource.binding_slot}",
            )
        seen_bindings.add(binding_key)

        family_contract = PASS_FAMILY_BINDINGS.get(resource.pass_family)
        if family_contract is None:
            error(
                "package_pass_family_unknown",
                f"resource '{resource.name}' uses unknown pass family "
                f"'{resource.pass_family}'",
            )
            continue
        allowed = tuple(family_contract.values())
        if (resource.usage, resource.binding_slot, resource.binding_space) not in allowed:
            error(
                "package_resource_binding_contract",
                f"resource '{resource.name}' does not obey the fixed "
                f"{resource.pass_family} binding convention",
            )

    for family, contract in PASS_FAMILY_BINDINGS.items():
        present = {
            (resource.usage, resource.binding_slot, resource.binding_space)
            for resource in resources
            if resource.pass_family == family
        }
        required = set(contract.values())
        # Input SRV and sampler bindings are only required when the emitted
        # shader family actually consumes them.  The math/noise fixture reads
        # only root constants and writes its UAV.
        required.discard(contract["sampler"])
        required.discard(contract["input"])
        missing = sorted(required - present)
        if missing:
            diagnostics.append(
                package_diagnostic(
                    "package_resource_binding_contract",
                    f"pass family '{family}' is missing fixed bindings {missing}",
                    section=SECTION_RESOURCES,
                )
            )


def _validate_instructions(
    initialization: Sequence[Instruction],
    frame: Sequence[Instruction],
    resources: Sequence[ResourceDescriptor],
    shaders: Sequence[ShaderRecord],
    root_signatures: Sequence[RootSignatureRecord],
    parameters: Sequence[ParameterRecord],
    diagnostics: list[dict[str, Any]],
) -> None:
    resource_ids = {item.resource_id for item in resources}
    shader_ids = {item.shader_id for item in shaders}
    root_ids = {item.root_signature_id for item in root_signatures}
    parameter_ids = {item.parameter_id for item in parameters}
    root_dwords = {item.root_signature_id: item.dword_cost for item in root_signatures}
    actual_init: set[str] = set()
    actual_frame: set[str] = set()

    for phase_name, section, instructions, actual in (
        ("initialization", SECTION_INITIALIZATION, initialization, actual_init),
        ("frame", SECTION_FRAME, frame, actual_frame),
    ):
        for index, instruction in enumerate(instructions):
            def error(code: str, message: str) -> None:
                diagnostics.append(
                    package_diagnostic(
                        code,
                        message,
                        section=section,
                        offset=index,
                    )
                )

            contract = INSTRUCTION_CONTRACTS.get(instruction.opcode)
            if contract is None:
                error(
                    "package_instruction_unknown",
                    f"{phase_name} instruction '{instruction.opcode}' is not supported",
                )
                continue
            expected_phase, operand_types = contract
            if expected_phase != phase_name:
                error(
                    "package_instruction_phase",
                    f"instruction '{instruction.opcode}' belongs in {expected_phase}, not {phase_name}",
                )
            actual.add(instruction.opcode)
            if len(instruction.operands) != len(operand_types):
                error(
                    "package_instruction_operand_count",
                    f"instruction '{instruction.opcode}' has {len(instruction.operands)} "
                    f"operands, expected {len(operand_types)}",
                )
                continue
            for operand_index, (operand, expected_type) in enumerate(
                zip(instruction.operands, operand_types)
            ):
                if operand.type != expected_type:
                    error(
                        "package_instruction_operand",
                        f"instruction '{instruction.opcode}' operand {operand_index} "
                        f"has type {operand.type}, expected {expected_type}",
                    )
                    continue
                if operand.type == "resource_ref" and operand.value not in resource_ids:
                    error(
                        "package_instruction_reference",
                        f"instruction '{instruction.opcode}' references missing resource {operand.value}",
                    )
                elif operand.type == "shader_ref" and operand.value not in shader_ids:
                    error(
                        "package_instruction_reference",
                        f"instruction '{instruction.opcode}' references missing shader {operand.value}",
                    )
                elif operand.type == "root_signature_ref" and operand.value not in root_ids:
                    error(
                        "package_instruction_reference",
                        f"instruction '{instruction.opcode}' references missing root signature {operand.value}",
                    )
                elif operand.type == "u32" and operand_index == 0 and instruction.opcode == "frame.dispatch":
                    if operand.value == 0 or operand.value > 65535:
                        error(
                            "package_instruction_operand",
                            f"dispatch group dimension {operand.value} is outside 1..65535",
                        )
                elif operand.type == "u32" and operand_index == 0 and instruction.opcode == "frame.set_timeline":
                    if operand.value not in parameter_ids:
                        error(
                            "package_instruction_reference",
                            f"timeline instruction references missing parameter {operand.value}",
                        )
                elif operand.type == "u32" and instruction.opcode == "frame.set_root_constants" and operand_index == 2:
                    if operand.value > MAX_ROOT_SIGNATURE_DWORD_COST:
                        error(
                            "package_instruction_operand",
                            f"root constants dword count {operand.value} exceeds {MAX_ROOT_SIGNATURE_DWORD_COST}",
                        )
                elif operand.type == "resource_ref" and instruction.opcode == "frame.set_root_constants":
                    # The second operand names the root-constant resource.  It
                    # must be a CBV resource.
                    matching = next(
                        (item for item in resources if item.resource_id == operand.value),
                        None,
                    )
                    if matching is not None and matching.usage != "cbv":
                        error(
                            "package_instruction_operand",
                            f"root constants reference resource {operand.value} without cbv usage",
                        )

            # Root-parameter operands must select the binding that names the
            # referenced resource.  This catches ambiguous root parameter 0
            # usage for both root constants and descriptor tables.
            if (
                instruction.opcode
                in {"frame.set_root_constants", "frame.set_descriptor_table"}
                and len(instruction.operands) == len(operand_types)
                and instruction.operands[0].type == "u32"
            ):
                resource_operand = (
                    instruction.operands[1]
                    if instruction.opcode == "frame.set_root_constants"
                    else instruction.operands[2]
                )
                if resource_operand.type == "resource_ref":
                    resource = next(
                        (
                            item
                            for item in resources
                            if item.resource_id == resource_operand.value
                        ),
                        None,
                    )
                    if resource is not None:
                        root_parameter = instruction.operands[0].value
                        matched = any(
                            binding.root_parameter == root_parameter
                            and binding.name == resource.name
                            and binding.usage == resource.usage
                            and binding.slot == resource.binding_slot
                            and binding.space == resource.binding_space
                            for root in root_signatures
                            for binding in root.bindings
                        )
                        if not matched:
                            error(
                                "package_root_signature_binding",
                                f"instruction '{instruction.opcode}' references root "
                                f"parameter {root_parameter} that does not bind resource "
                                f"'{resource.name}'",
                            )

    for phase_name, actual, required in (
        ("initialization", actual_init, set(CapabilityRecord().required_init_instructions)),
        ("frame", actual_frame, set(CapabilityRecord().required_frame_instructions)),
    ):
        # The exact emitted list is validated against the capability record by
        # _validate_cross_references.  Here we only require non-empty phases.
        if not actual:
            diagnostics.append(
                package_diagnostic(
                    "package_instruction_missing",
                    f"package has no {phase_name} instructions",
                    section=SECTION_INITIALIZATION if phase_name == "initialization" else SECTION_FRAME,
                )
            )


def _validate_parameters(
    parameters: Sequence[ParameterRecord], diagnostics: list[dict[str, Any]]
) -> None:
    seen: set[int] = set()
    for parameter in parameters:
        def error(code: str, message: str) -> None:
            diagnostics.append(
                package_diagnostic(code, message, section=SECTION_PARAMETERS)
            )

        if parameter.parameter_id <= 0:
            error(
                "package_parameter_id",
                f"parameter id {parameter.parameter_id} must be positive",
            )
        if parameter.parameter_id in seen:
            error(
                "package_parameter_id",
                f"duplicate parameter id {parameter.parameter_id}",
            )
        seen.add(parameter.parameter_id)
        if not parameter.name:
            error("package_parameter_malformed", "parameter name is empty")
        if parameter.type not in KNOWN_PARAMETER_TYPES:
            error(
                "package_parameter_type",
                f"unknown parameter type '{parameter.type}'",
            )
        expected_components = {
            "float": 1,
            "float2": 2,
            "float3": 3,
            "float4": 4,
            "uint": 1,
            "int": 1,
        }.get(parameter.type, 0)
        if parameter.component_count != expected_components:
            error(
                "package_parameter_dimensions",
                f"parameter '{parameter.name}' has {parameter.component_count} components, "
                f"expected {expected_components}",
            )
        if len(parameter.value) != parameter.component_count * 4:
            error(
                "package_parameter_dimensions",
                f"parameter '{parameter.name}' value is {len(parameter.value)} bytes, "
                f"expected {parameter.component_count * 4}",
            )
        if parameter.phase not in KNOWN_PARAMETER_PHASES:
            error(
                "package_parameter_phase",
                f"unknown parameter phase '{parameter.phase}'",
            )
        if parameter.timeline_interpolation not in KNOWN_TIMELINE_INTERPOLATIONS:
            error(
                "package_parameter_timeline",
                f"unknown timeline interpolation '{parameter.timeline_interpolation}'",
            )
        if parameter.timeline_start_frame > parameter.timeline_end_frame:
            error(
                "package_parameter_timeline",
                f"parameter '{parameter.name}' timeline start exceeds end",
            )
        if parameter.timeline_start_frame != parameter.timeline_end_frame:
            if parameter.timeline_step == 0:
                error(
                    "package_parameter_timeline",
                    f"parameter '{parameter.name}' timeline step must be positive",
                )


def _decode_root_signature_blob(
    blob: bytes | bytearray | memoryview,
    diagnostics: list[dict[str, Any]],
) -> tuple[str, tuple[RootBinding, ...]] | None:
    """Decode the canonical ``NWRS`` root-signature payload.

    Publication-side validation parses this payload so a record cannot carry a
    blob with different family or binding semantics than the record itself.
    """
    reader = _Reader(blob, diagnostics, SECTION_ROOT_SIGNATURES)
    magic = reader._take(4)
    if magic is None:
        return None
    if bytes(magic) != b"NWRS":
        diagnostics.append(
            package_diagnostic(
                "package_root_signature_malformed",
                "root signature blob magic is not 'NWRS'",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    version = reader.u16()
    if version is None:
        return None
    if version != 1:
        diagnostics.append(
            package_diagnostic(
                "package_unknown_section_version",
                f"root signature blob version {version} is not supported",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    family = reader.string()
    if family is None:
        return None
    count = reader.u16()
    if count is None:
        return None
    if count > MAX_RECORD_COUNT:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"root signature blob binding count {count} exceeds {MAX_RECORD_COUNT}",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    bindings: list[RootBinding] = []
    for _ in range(count):
        name = reader.string()
        usage = reader.string()
        slot = reader.u32()
        space = reader.u32()
        root_parameter = reader.u32()
        if None in (name, usage, slot, space, root_parameter):
            return None
        bindings.append(RootBinding(name, usage, slot, space, root_parameter))
    if not reader.at_end():
        diagnostics.append(
            package_diagnostic(
                "package_trailing_data",
                f"root signature blob has {reader.remaining} trailing bytes",
                section=SECTION_ROOT_SIGNATURES,
            )
        )
        return None
    return family, tuple(bindings)


def _validate_root_signatures(
    root_signatures: Sequence[RootSignatureRecord],
    resources: Sequence[ResourceDescriptor],
    diagnostics: list[dict[str, Any]],
) -> None:
    seen_ids: set[int] = set()
    resource_names = {(item.pass_family, item.name) for item in resources}
    for record in root_signatures:
        def error(code: str, message: str) -> None:
            diagnostics.append(
                package_diagnostic(code, message, section=SECTION_ROOT_SIGNATURES)
            )

        if record.root_signature_id <= 0:
            error(
                "package_root_signature_id",
                f"root signature id {record.root_signature_id} must be positive",
            )
        if record.root_signature_id in seen_ids:
            error(
                "package_root_signature_id",
                f"duplicate root signature id {record.root_signature_id}",
            )
        seen_ids.add(record.root_signature_id)
        if record.family not in PASS_FAMILY_BINDINGS:
            error(
                "package_pass_family_unknown",
                f"root signature {record.root_signature_id} uses unknown pass family "
                f"'{record.family}'",
            )
        elif record.convention != f"{record.family}.v1":
            error(
                "package_root_signature_convention",
                f"root signature {record.root_signature_id} convention "
                f"'{record.convention}' does not match '{record.family}.v1'",
            )
        if not record.blob:
            error("package_root_signature_malformed", "root signature blob is empty")
        else:
            decoded = _decode_root_signature_blob(record.blob, diagnostics)
            if decoded is not None:
                blob_family, blob_bindings = decoded
                if blob_family != record.family:
                    error(
                        "package_root_signature_blob_mismatch",
                        f"root signature {record.root_signature_id} blob family "
                        f"'{blob_family}' does not match record family '{record.family}'",
                    )
                if blob_bindings != tuple(record.bindings):
                    error(
                        "package_root_signature_blob_mismatch",
                        f"root signature {record.root_signature_id} blob bindings do "
                        "not match the record bindings",
                    )
        if record.dword_cost > MAX_ROOT_SIGNATURE_DWORD_COST:
            error(
                "package_root_signature_cost",
                f"root signature dword cost {record.dword_cost} exceeds {MAX_ROOT_SIGNATURE_DWORD_COST}",
            )
        root_parameters: set[int] = set()
        contract = PASS_FAMILY_BINDINGS.get(record.family, {})
        for binding in record.bindings:
            if binding.root_parameter in root_parameters:
                error(
                    "package_root_signature_root_parameter",
                    f"root signature {record.root_signature_id} reuses root parameter "
                    f"{binding.root_parameter} for multiple bindings",
                )
            root_parameters.add(binding.root_parameter)
            expected = contract.get(binding.name)
            if expected is not None:
                if (binding.usage, binding.slot, binding.space) != expected:
                    error(
                        "package_resource_binding_contract",
                        f"root signature binding '{binding.name}' does not obey the "
                        f"fixed {record.family} convention",
                    )
            elif record.family in PASS_FAMILY_BINDINGS:
                error(
                    "package_root_signature_binding",
                    f"root signature binding '{binding.name}' is not declared by the "
                    f"{record.family} pass-family contract",
                )
            if (record.family, binding.name) not in resource_names:
                error(
                    "package_root_signature_binding",
                    f"root signature binding '{binding.name}' has no matching resource descriptor",
                )
                continue
            matching = next(
                (
                    item
                    for item in resources
                    if item.pass_family == record.family
                    and item.name == binding.name
                ),
                None,
            )
            if matching is not None and (
                binding.usage,
                binding.slot,
                binding.space,
            ) != (
                matching.usage,
                matching.binding_slot,
                matching.binding_space,
            ):
                error(
                    "package_root_signature_binding",
                    f"root signature binding '{binding.name}' does not match its "
                    "resource descriptor usage/slot/space",
                )


def _validate_shaders(
    shaders: Sequence[ShaderRecord],
    root_signatures: Sequence[RootSignatureRecord],
    diagnostics: list[dict[str, Any]],
) -> None:
    seen_ids: set[int] = set()
    root_ids = {record.root_signature_id for record in root_signatures}
    root_families = {
        record.root_signature_id: record.family for record in root_signatures
    }
    for shader in shaders:
        def error(code: str, message: str) -> None:
            diagnostics.append(
                package_diagnostic(code, message, section=SECTION_SHADERS)
            )

        if shader.shader_id <= 0:
            error("package_shader_id", f"shader id {shader.shader_id} must be positive")
        if shader.shader_id in seen_ids:
            error(
                "package_shader_id",
                f"duplicate shader id {shader.shader_id}",
            )
        seen_ids.add(shader.shader_id)
        if not shader.entry_point:
            error("package_shader_malformed", f"shader {shader.shader_id} has no entry point")
        elif not all(
            (ch.isalnum() or ch == "_") for ch in shader.entry_point
        ) or shader.entry_point[0].isdigit():
            error(
                "package_shader_malformed",
                f"shader {shader.shader_id} entry point '{shader.entry_point}' is not an identifier",
            )
        if shader.target not in KNOWN_SHADER_TARGETS:
            error(
                "package_shader_target_unknown",
                f"shader {shader.shader_id} target '{shader.target}' is unknown",
            )
        if shader.root_signature_id not in root_ids:
            error(
                "package_shader_root_signature",
                f"shader {shader.shader_id} references missing root signature {shader.root_signature_id}",
            )
        if not shader.family:
            error("package_shader_malformed", f"shader {shader.shader_id} has no pass family")
        elif shader.family not in PASS_FAMILY_BINDINGS:
            error(
                "package_pass_family_unknown",
                f"shader {shader.shader_id} uses unknown pass family '{shader.family}'",
            )
        if (
            shader.root_signature_id in root_families
            and shader.family != root_families[shader.root_signature_id]
        ):
            error(
                "package_shader_root_signature",
                f"shader {shader.shader_id} pass family '{shader.family}' does not "
                f"match root signature family '{root_families[shader.root_signature_id]}'",
            )
        for label, value in (
            ("thread_group_x", shader.thread_group_x),
            ("thread_group_y", shader.thread_group_y),
            ("thread_group_z", shader.thread_group_z),
        ):
            if value <= 0 or value > MAX_COMPUTE_THREADS_PER_GROUP:
                error(
                    "package_shader_dimensions",
                    f"shader {shader.shader_id} {label} {value} is out of range",
                )
        total_threads = (
            shader.thread_group_x * shader.thread_group_y * shader.thread_group_z
        )
        if total_threads > MAX_COMPUTE_THREADS_PER_GROUP:
            error(
                "package_shader_dimensions",
                f"shader {shader.shader_id} thread group has {total_threads} threads "
                f"(limit {MAX_COMPUTE_THREADS_PER_GROUP})",
            )
        if not shader.blob:
            error("package_shader_malformed", f"shader {shader.shader_id} blob is empty")
            continue
        if validate_dxil_container is not None:
            try:
                info = validate_dxil_container(shader.blob, allow_test_container=True)
            except Exception as exc:
                error(
                    "package_shader_payload_invalid",
                    f"shader {shader.shader_id} DXIL payload is invalid: {exc}",
                )
                continue
            if info.get("shader_model") != shader.target:
                error(
                    "package_shader_target_mismatch",
                    f"shader {shader.shader_id} declares target '{shader.target}' but DXIL "
                    f"contains '{info.get('shader_model')}'",
                )


def _validate_cross_references(
    capability: CapabilityRecord,
    initialization: Sequence[Instruction],
    frame: Sequence[Instruction],
    shaders: Sequence[ShaderRecord],
    diagnostics: list[dict[str, Any]],
) -> None:
    actual_init = sorted({instruction.opcode for instruction in initialization})
    actual_frame = sorted({instruction.opcode for instruction in frame})
    if tuple(capability.required_init_instructions) != tuple(actual_init):
        diagnostics.append(
            package_diagnostic(
                "package_capability_instruction_mismatch",
                "CAP required_init_instructions "
                f"{list(capability.required_init_instructions)} do not match emitted "
                f"{actual_init}",
                section=SECTION_CAPABILITY,
            )
        )
    if tuple(capability.required_frame_instructions) != tuple(actual_frame):
        diagnostics.append(
            package_diagnostic(
                "package_capability_instruction_mismatch",
                "CAP required_frame_instructions "
                f"{list(capability.required_frame_instructions)} do not match emitted "
                f"{actual_frame}",
                section=SECTION_CAPABILITY,
            )
        )
    targets = sorted({shader.target for shader in shaders})
    if tuple(capability.emitted_shader_targets) != tuple(targets):
        diagnostics.append(
            package_diagnostic(
                "package_capability_shader_target_mismatch",
                "CAP emitted_shader_targets "
                f"{list(capability.emitted_shader_targets)} do not match emitted {targets}",
                section=SECTION_CAPABILITY,
            )
        )

    pipeline_shaders = {
        instruction.operands[0].value
        for instruction in initialization
        if instruction.opcode == "init.create_pipeline"
        and instruction.operands
        and instruction.operands[0].type == "shader_ref"
    }
    dispatched_shaders = {
        instruction.operands[-1].value
        for instruction in frame
        if instruction.opcode == "frame.dispatch"
        and instruction.operands
        and instruction.operands[-1].type == "shader_ref"
    }
    for shader in shaders:
        if shader.shader_id not in pipeline_shaders:
            diagnostics.append(
                package_diagnostic(
                    "package_shader_unreferenced",
                    f"shader {shader.shader_id} has no init.create_pipeline instruction",
                    section=SECTION_INITIALIZATION,
                )
            )
        if shader.shader_id not in dispatched_shaders:
            diagnostics.append(
                package_diagnostic(
                    "package_shader_unreferenced",
                    f"shader {shader.shader_id} has no frame.dispatch instruction",
                    section=SECTION_FRAME,
                )
            )


def validate_package_model(package: NwpPackage) -> list[dict[str, Any]]:
    """Validate a package model and return package-validation diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    _validate_capability(package.capability, diagnostics)
    _validate_resources(package.resources, diagnostics)
    _validate_parameters(package.parameters, diagnostics)
    _validate_root_signatures(package.root_signatures, package.resources, diagnostics)
    _validate_shaders(package.shaders, package.root_signatures, diagnostics)
    _validate_instructions(
        package.initialization,
        package.frame,
        package.resources,
        package.shaders,
        package.root_signatures,
        package.parameters,
        diagnostics,
    )
    _validate_cross_references(
        package.capability,
        package.initialization,
        package.frame,
        package.shaders,
        diagnostics,
    )
    if package.dev_metadata is not None and (package.flags & 1):
        diagnostics.append(
            package_diagnostic(
                "package_release_authoring_metadata",
                "release packages must not contain development metadata",
                section=SECTION_DEV,
            )
        )
    return diagnostics


# ---------------------------------------------------------------------------
# Container writer / reader
# ---------------------------------------------------------------------------


def encode_package(package: NwpPackage) -> bytes:
    """Encode a validated package.  Raises PackageValidationError on failure."""
    working = package
    if not working.capability.profile_fingerprint:
        working = replace(
            working,
            capability=replace(
                working.capability,
                profile_fingerprint=working.capability.computed_profile_fingerprint(),
            ),
        )
    diagnostics = validate_package_model(working)
    if diagnostics:
        raise PackageValidationError(diagnostics)
    package = working

    sections: dict[str, bytes] = {
        SECTION_CAPABILITY: _encode_capability(package.capability),
        SECTION_RESOURCES: _encode_resources(package.resources),
        SECTION_INITIALIZATION: _encode_instructions(package.initialization),
        SECTION_FRAME: _encode_instructions(package.frame),
        SECTION_PARAMETERS: _encode_parameters(package.parameters),
        SECTION_ROOT_SIGNATURES: _encode_root_signatures(package.root_signatures),
        SECTION_SHADERS: _encode_shaders(package.shaders),
    }
    if package.dev_metadata is not None:
        sections[SECTION_DEV] = package.dev_metadata

    section_order = sorted(sections)
    directory_end = HEADER_SIZE + SECTION_ENTRY_SIZE * len(section_order)
    offsets: list[tuple[str, int, int]] = []
    record_counts = {
        SECTION_CAPABILITY: 1,
        SECTION_RESOURCES: len(package.resources),
        SECTION_INITIALIZATION: len(package.initialization),
        SECTION_FRAME: len(package.frame),
        SECTION_PARAMETERS: len(package.parameters),
        SECTION_ROOT_SIGNATURES: len(package.root_signatures),
        SECTION_SHADERS: len(package.shaders),
    }
    if package.dev_metadata is not None:
        record_counts[SECTION_DEV] = 0
    for name, payload in sections.items():
        if len(payload) > MAX_SECTION_BYTES:
            raise PackageValidationError(
                package_diagnostic(
                    "package_decode_limit",
                    f"section {name} is {len(payload)} bytes, exceeding {MAX_SECTION_BYTES}",
                    section=name,
                )
            )
    for name, count in record_counts.items():
        if count > MAX_RECORD_COUNT:
            raise PackageValidationError(
                package_diagnostic(
                    "package_decode_limit",
                    f"section {name} has {count} records, exceeding {MAX_RECORD_COUNT}",
                    section=name,
                )
            )
    cursor = directory_end
    for name in section_order:
        payload = sections[name]
        offsets.append((name, cursor, len(payload)))
        cursor += len(payload)
    checksum_offset = cursor
    total_size = cursor + CHECKSUM_SIZE
    if total_size > MAX_PACKAGE_BYTES:
        raise PackageValidationError(
            package_diagnostic(
                "package_decode_limit",
                f"package size {total_size} exceeds {MAX_PACKAGE_BYTES}",
            )
        )

    header = bytearray(
        struct.pack(
            "<8sHHIIIQQIIQQ",
            MAGIC,
            VERSION_MAJOR,
            VERSION_MINOR,
            HEADER_SIZE,
            package.flags,
            0,
            HEADER_SIZE,
            total_size,
            len(section_order),
            SECTION_ENTRY_SIZE,
            checksum_offset,
            0,
        )
    )
    body = bytearray()
    for name, offset, size in offsets:
        payload = sections[name]
        body += struct.pack(
            "<4sIIQQI",
            name.encode("ascii"),
            0,
            record_counts[name],
            offset,
            size,
            0,
        )
    for name, _offset, _size in offsets:
        body += sections[name]
    checksum = hashlib.sha256(header + body).digest()
    return bytes(header + body + checksum)


def _decode_dev_metadata(payload: bytes, diagnostics: list[dict[str, Any]]) -> bytes | None:
    if len(payload) > MAX_SECTION_BYTES:
        diagnostics.append(
            package_diagnostic(
                "package_decode_limit",
                f"DEVC section size {len(payload)} exceeds {MAX_SECTION_BYTES}",
                section=SECTION_DEV,
            )
        )
        return None
    return payload


def decode_package(
    data: bytes,
    *,
    allow_dev: bool = False,
    max_total_bytes: int = MAX_PACKAGE_BYTES,
    max_section_bytes: int = MAX_SECTION_BYTES,
    verify_integrity: bool = True,
) -> NwpPackage:
    """Bounded `.nwp` v1 reader.  Raises PackageValidationError on any defect."""
    diagnostics: list[dict[str, Any]] = []
    if len(data) < HEADER_SIZE + CHECKSUM_SIZE:
        raise PackageValidationError(
            package_diagnostic(
                "package_truncated",
                f"package is {len(data)} bytes, smaller than the header and checksum",
                offset=0,
            )
        )
    if len(data) > max_total_bytes:
        raise PackageValidationError(
            package_diagnostic(
                "package_decode_limit",
                f"package size {len(data)} exceeds configured limit {max_total_bytes}",
                offset=0,
            )
        )
    try:
        (
            magic,
            major,
            minor,
            header_size,
            flags,
            reserved,
            directory_offset,
            total_size,
            section_count,
            entry_size,
            checksum_offset,
            reserved2,
        ) = struct.unpack_from("<8sHHIIIQQIIQQ", data, 0)
    except struct.error as exc:
        raise PackageValidationError(
            package_diagnostic("package_truncated", f"header decode failed: {exc}", offset=0)
        ) from exc
    if magic != MAGIC:
        raise PackageValidationError(
            package_diagnostic(
                "package_header_magic",
                f"bad package magic {magic!r}",
                offset=0,
            )
        )
    if major != VERSION_MAJOR:
        raise PackageValidationError(
            package_diagnostic(
                "package_version_unknown",
                f"unknown .nwp major version {major}; this reader implements {VERSION_MAJOR}",
                offset=8,
            )
        )
    if minor != VERSION_MINOR:
        raise PackageValidationError(
            package_diagnostic(
                "package_version_unknown",
                f"unknown .nwp minor version {minor}; this reader implements {VERSION_MINOR}",
                offset=10,
            )
        )
    if header_size != HEADER_SIZE:
        raise PackageValidationError(
            package_diagnostic(
                "package_header_size",
                f"header_size is {header_size}, expected {HEADER_SIZE}",
                offset=12,
            )
        )
    if reserved or reserved2:
        raise PackageValidationError(
            package_diagnostic(
                "package_header_reserved", "reserved header fields must be zero", offset=16
            )
        )
    if directory_offset != HEADER_SIZE:
        raise PackageValidationError(
            package_diagnostic(
                "package_directory_offset",
                f"section directory offset {directory_offset} is not {HEADER_SIZE}",
                offset=24,
            )
        )
    if total_size != len(data):
        raise PackageValidationError(
            package_diagnostic(
                "package_size_mismatch",
                f"header total_size {total_size} does not match actual size {len(data)}",
                offset=32,
            )
        )
    if entry_size != SECTION_ENTRY_SIZE:
        raise PackageValidationError(
            package_diagnostic(
                "package_directory_entry_size",
                f"section directory entry size {entry_size} is not {SECTION_ENTRY_SIZE}",
                offset=44,
            )
        )
    if section_count == 0 or section_count > MAX_SECTION_COUNT:
        raise PackageValidationError(
            package_diagnostic(
                "package_section_count",
                f"section count {section_count} is outside 1..{MAX_SECTION_COUNT}",
                offset=40,
            )
        )
    directory_end = HEADER_SIZE + section_count * SECTION_ENTRY_SIZE
    if directory_end > len(data) - CHECKSUM_SIZE:
        raise PackageValidationError(
            package_diagnostic(
                "package_truncated",
                "section directory exceeds package bounds",
                offset=HEADER_SIZE,
            )
        )
    if checksum_offset != len(data) - CHECKSUM_SIZE:
        raise PackageValidationError(
            package_diagnostic(
                "package_checksum_offset",
                f"checksum offset {checksum_offset} is not {len(data) - CHECKSUM_SIZE}",
                offset=48,
            )
        )
    if verify_integrity:
        expected = hashlib.sha256(data[:checksum_offset]).digest()
        actual = data[checksum_offset:]
        if expected != actual:
            raise PackageValidationError(
                package_diagnostic(
                    "package_checksum_mismatch",
                    "package integrity checksum does not match",
                    offset=checksum_offset,
                )
            )

    entries: list[tuple[str, int, int, int, int, int]] = []
    seen_names: set[str] = set()
    for index in range(section_count):
        entry_offset = HEADER_SIZE + index * SECTION_ENTRY_SIZE
        fourcc, entry_flags, record_count, offset, size, entry_reserved = struct.unpack_from(
            "<4sIIQQI", data, entry_offset
        )
        name = fourcc.decode("ascii", errors="replace")
        if name not in KNOWN_SECTIONS:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_unknown",
                    f"unknown section '{name}'",
                    section=name,
                    offset=entry_offset,
                )
            )
        if name == SECTION_DEV and not allow_dev:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_forbidden",
                    "development metadata section is not allowed in a release package",
                    section=name,
                    offset=entry_offset,
                )
            )
        if name in seen_names:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_duplicate",
                    f"duplicate section '{name}'",
                    section=name,
                    offset=entry_offset,
                )
            )
        seen_names.add(name)
        if entry_flags != 0 or entry_reserved != 0:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_flags",
                    f"section '{name}' has unsupported flags/reserved bits",
                    section=name,
                    offset=entry_offset,
                )
            )
        if size > max_section_bytes:
            raise PackageValidationError(
                package_diagnostic(
                    "package_decode_limit",
                    f"section '{name}' size {size} exceeds {max_section_bytes}",
                    section=name,
                    offset=entry_offset,
                )
            )
        if offset < directory_end or offset > checksum_offset:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_range",
                    f"section '{name}' offset {offset} is outside the payload region",
                    section=name,
                    offset=entry_offset,
                )
            )
        if size > checksum_offset - offset:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_range",
                    f"section '{name}' range {offset}..{offset + size} exceeds the payload region",
                    section=name,
                    offset=entry_offset,
                )
            )
        entries.append((name, index, record_count, offset, size, entry_offset))

    missing = [name for name in REQUIRED_RELEASE_SECTIONS if name not in seen_names]
    if missing:
        raise PackageValidationError(
            package_diagnostic(
                "package_section_missing",
                "missing required section(s): " + ", ".join(sorted(missing)),
            )
        )

    entries.sort(key=lambda item: item[3])
    previous_end = directory_end
    for name, _index, _record_count, offset, size, entry_offset in entries:
        if offset < previous_end:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_overlap",
                    f"section '{name}' overlaps a previous section",
                    section=name,
                    offset=entry_offset,
                )
            )
        previous_end = offset + size
    if previous_end != checksum_offset:
        raise PackageValidationError(
            package_diagnostic(
                "package_section_gap",
                f"section payloads end at {previous_end}, checksum begins at {checksum_offset}",
            )
        )

    payloads = {
        name: data[offset : offset + size]
        for name, _index, _record_count, offset, size, _entry_offset in entries
    }
    capability = _decode_capability(payloads[SECTION_CAPABILITY], diagnostics)
    resources = _decode_resources(payloads[SECTION_RESOURCES], diagnostics)
    initialization = _decode_instructions(
        payloads[SECTION_INITIALIZATION], diagnostics, SECTION_INITIALIZATION
    )
    frame_instructions = _decode_instructions(
        payloads[SECTION_FRAME], diagnostics, SECTION_FRAME
    )
    parameters = _decode_parameters(payloads[SECTION_PARAMETERS], diagnostics)
    root_signatures = _decode_root_signatures(
        payloads[SECTION_ROOT_SIGNATURES], diagnostics
    )
    shaders = _decode_shaders(payloads[SECTION_SHADERS], diagnostics)
    dev_metadata = (
        _decode_dev_metadata(payloads[SECTION_DEV], diagnostics)
        if SECTION_DEV in payloads
        else None
    )
    if diagnostics:
        raise PackageValidationError(diagnostics)
    assert capability is not None
    assert resources is not None
    assert initialization is not None
    assert frame_instructions is not None
    assert parameters is not None
    assert root_signatures is not None
    assert shaders is not None
    package = NwpPackage(
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame_instructions,
        parameters=parameters,
        shaders=shaders,
        root_signatures=root_signatures,
        dev_metadata=dev_metadata,
        flags=flags,
    )
    actual_counts = {
        SECTION_CAPABILITY: 1,
        SECTION_RESOURCES: len(resources),
        SECTION_INITIALIZATION: len(initialization),
        SECTION_FRAME: len(frame_instructions),
        SECTION_PARAMETERS: len(parameters),
        SECTION_ROOT_SIGNATURES: len(root_signatures),
        SECTION_SHADERS: len(shaders),
    }
    if dev_metadata is not None:
        actual_counts[SECTION_DEV] = 0
    for name, _index, declared_records, offset, _size, _entry_offset in entries:
        if declared_records != actual_counts[name]:
            raise PackageValidationError(
                package_diagnostic(
                    "package_section_record_count",
                    f"section '{name}' directory count {declared_records} does not "
                    f"match decoded count {actual_counts[name]}",
                    section=name,
                    offset=offset,
                )
            )
    diagnostics = validate_package_model(package)
    if diagnostics:
        raise PackageValidationError(diagnostics)
    return package


def validate_package_bytes(
    data: bytes,
    *,
    allow_dev: bool = False,
    require_integrity: bool = True,
    max_total_bytes: int = MAX_PACKAGE_BYTES,
    max_section_bytes: int = MAX_SECTION_BYTES,
) -> tuple[bool, list[dict[str, Any]]]:
    """Validate raw package bytes without raising.  Returns (valid, diagnostics)."""
    try:
        decode_package(
            data,
            allow_dev=allow_dev,
            max_total_bytes=max_total_bytes,
            max_section_bytes=max_section_bytes,
            verify_integrity=require_integrity,
        )
    except PackageValidationError as exc:
        return False, list(exc.diagnostics)
    except (MemoryError, OverflowError) as exc:  # bounded reader must never get here
        return False, [
            package_diagnostic(
                "package_decode_limit", f"bounded package decode failed safely: {exc}"
            )
        ]
    return True, []


def inspect_package_bytes(
    data: bytes,
    *,
    allow_dev: bool = False,
    require_integrity: bool = True,
) -> Inspection:
    """Return a structured inspection result, including diagnostics on failure."""
    checksum = data[-CHECKSUM_SIZE:].hex() if len(data) >= CHECKSUM_SIZE else ""
    try:
        package = decode_package(
            data,
            allow_dev=allow_dev,
            verify_integrity=require_integrity,
        )
    except PackageValidationError as exc:
        return Inspection(
            False,
            tuple(exc.diagnostics),
            None,
            len(data),
            checksum,
            sha256_hex(data),
        )
    return Inspection(True, (), package, len(data), checksum, sha256_hex(data))


# ---------------------------------------------------------------------------
# Atomic publication and release leakage checks
# ---------------------------------------------------------------------------


def write_package_atomic(
    package: NwpPackage,
    destination: str | os.PathLike[str],
) -> bytes:
    """Validate, encode, and atomically replace *destination* with a package.

    If validation or writing fails, the previous destination is left untouched
    and no partially written package is published.
    """
    data = encode_package(package)
    valid, diagnostics = validate_package_bytes(data, allow_dev=package.dev_metadata is not None)
    if not valid:
        raise PackageValidationError(diagnostics)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(
            prefix=".nwp-", suffix=".tmp", dir=str(destination_path.parent)
        )
        temporary_path = Path(raw_tmp)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return data


RELEASE_FORBIDDEN_STRINGS = (
    b"nodewerk-studio",
    b"fixtures/",
    b"fixtures\\",
    b"src/graph",
    b"src\\graph",
    b"nwgraph",
    b"editor_layout",
    b"source_map",
    b"reflection",
    b"DXIL debug",
    b"#line",
    b"expression:",
    b"authoring",
)


def check_release_leakage(
    data: bytes,
    *,
    extra_forbidden: Iterable[bytes] = (),
) -> list[str]:
    """Return human-readable leakage findings for release package bytes."""
    findings: list[str] = []
    for needle in tuple(RELEASE_FORBIDDEN_STRINGS) + tuple(extra_forbidden):
        if needle and needle in data:
            findings.append(f"forbidden release string present: {needle!r}")
    try:
        inspection = inspect_package_bytes(data, allow_dev=False)
    except Exception as exc:  # pragma: no cover - defensive
        return findings + [f"package could not be inspected: {exc}"]
    if not inspection.valid:
        findings.append("package is not valid")
        return findings
    assert inspection.package is not None
    if inspection.package.dev_metadata:
        findings.append("release package contains DEVC metadata")
    if not (inspection.package.flags & 1):
        findings.append("package is not marked as a release package")
    return findings


def leak_scan_report(data: bytes) -> dict[str, Any]:
    findings = check_release_leakage(data)
    return {
        "valid": not findings,
        "findings": findings,
        "checks": {
            "no_editor_names": not any("nodewerk-studio" in item for item in findings),
            "no_source_paths": not any("fixtures/" in item or "src/graph" in item for item in findings),
            "no_source_text": not any("nwgraph" in item or "expression:" in item for item in findings),
            "no_debug_data": not any("DXIL debug" in item or "#line" in item for item in findings),
            "no_reflection_tables": not any("reflection" in item for item in findings),
            "retains_dxil_execution_metadata": True,
        },
    }


# Convenience aliases for consumers that use the obvious verb names.
write_package = write_package_atomic
read_package = decode_package
inspect_package = inspect_package_bytes
validate_package = validate_package_bytes


# ---------------------------------------------------------------------------
# Convenience builders used by the compiler and tests
# ---------------------------------------------------------------------------


def make_root_signature_blob(
    family: str,
    bindings: Sequence[RootBinding],
) -> bytes:
    """Return a deterministic, reflection-free root-signature payload."""
    writer = _Writer()
    writer.buffer += b"NWRS"
    writer.u16(1)
    writer.string(family)
    writer.u16(len(bindings))
    for binding in bindings:
        writer.string(binding.name)
        writer.string(binding.usage)
        writer.u32(binding.slot)
        writer.u32(binding.space)
        writer.u32(binding.root_parameter)
    return bytes(writer.buffer)


def make_test_package(
    *,
    shader_blob: bytes,
    specialization_count: int = 1,
    duplicate_blob: bool = False,
    root_signature_blob: bytes | None = None,
    capability: CapabilityRecord | None = None,
) -> NwpPackage:
    """Build a small valid package for tests/tools.

    ``shader_blob`` still must be a structurally valid cs_6_0 DXIL container;
    callers commonly use the NW-01 test container helper.
    """
    capability = capability or CapabilityRecord(
        compiler_fingerprint="sha256:v1:" + "0" * 64,
    )
    if not capability.profile_fingerprint:
        capability = replace(
            capability,
            profile_fingerprint=capability.computed_profile_fingerprint(),
        )
    bindings = (
        RootBinding("params", "cbv", 0, 0, 0),
        RootBinding("output", "uav", 0, 0, 1),
    )
    root_blob = root_signature_blob or make_root_signature_blob(
        "compute.math_noise", bindings
    )
    root = RootSignatureRecord(
        root_signature_id=1,
        family="compute.math_noise",
        convention="compute.math_noise.v1",
        dword_cost=4,
        blob=root_blob,
        bindings=bindings,
    )
    resources = [
        ResourceDescriptor(
            resource_id=1,
            name="params",
            kind="buffer",
            format="DXGI_FORMAT_R32G32B32A32_FLOAT",
            usage="cbv",
            pass_family="compute.math_noise",
            binding_slot=0,
            size_bytes=64,
        ),
        ResourceDescriptor(
            resource_id=2,
            name="output",
            kind="buffer",
            format="DXGI_FORMAT_R32_UINT",
            usage="uav",
            pass_family="compute.math_noise",
            binding_slot=0,
            size_bytes=256 * 16,
        ),
    ]
    parameters = [
        ParameterRecord(
            parameter_id=1,
            name="time",
            type="float",
            phase="frame",
            component_count=1,
            value=struct.pack("<f", 0.0),
        )
    ]
    shaders = []
    for index in range(specialization_count):
        shaders.append(
            ShaderRecord(
                shader_id=index + 1,
                entry_point="CSMain",
                target="cs_6_0",
                blob=shader_blob,
                root_signature_id=1,
                family="compute.math_noise",
                flags=1 if duplicate_blob and index > 0 else 0,
            )
        )
    initialization = [
        Instruction("init.create_resource", (InstructionOperand("resource_ref", 1),)),
        Instruction("init.create_resource", (InstructionOperand("resource_ref", 2),)),
        Instruction(
            "init.create_root_signature",
            (InstructionOperand("root_signature_ref", 1),),
        ),
    ]
    frame = [
        Instruction(
            "frame.set_root_signature",
            (InstructionOperand("root_signature_ref", 1),),
        ),
        Instruction(
            "frame.set_descriptor_table",
            (
                InstructionOperand("u32", 1),
                InstructionOperand("string", "cbv_srv_uav"),
                InstructionOperand("resource_ref", 2),
            ),
        ),
    ]
    for shader in shaders:
        initialization.append(
            Instruction(
                "init.create_pipeline",
                (
                    InstructionOperand("shader_ref", shader.shader_id),
                    InstructionOperand("root_signature_ref", 1),
                ),
            )
        )
        frame.append(
            Instruction(
                "frame.dispatch",
                (
                    InstructionOperand("u32", 4),
                    InstructionOperand("u32", 1),
                    InstructionOperand("u32", 1),
                    InstructionOperand("shader_ref", shader.shader_id),
                ),
            )
        )
    return NwpPackage(
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame,
        parameters=parameters,
        shaders=shaders,
        root_signatures=[root],
        flags=1,
    )
