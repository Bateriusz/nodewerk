#!/usr/bin/env python3
"""NW-03 compiler-side lowering, DXC compilation, shader cache, and packaging.

This module consumes NW-02 graph semantics and ShaderExpr values, lowers the
reachable ShaderExpr DAG to fused HLSL, compiles offline with pinned DXC,
validates the resulting DXIL containers, stores compiler/profile fingerprints in
development sidecars/build reports, and publishes the `.nwp` package atomically.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import nw03_oracle
import nwp_package
from nwp_package import (
    CapabilityRecord,
    Instruction,
    InstructionOperand,
    NwpPackage,
    PackageValidationError,
    ParameterRecord,
    ResourceDescriptor,
    RootBinding,
    RootSignatureRecord,
    ShaderRecord,
    package_diagnostic,
    sha256_hex,
)

try:
    from offline_shader_pack import (
        OfflineShaderCompilerError,
        _dxc_command_prefix,
        find_dxc_executable,
        find_windows_kits_dxc_executable,
        load_pinned_dxc_version,
        query_dxc_version,
        validate_dxil_container,
    )
except Exception:  # pragma: no cover - direct script import edge case
    OfflineShaderCompilerError = RuntimeError  # type: ignore[assignment,misc]
    _dxc_command_prefix = lambda value: [str(value)]  # type: ignore[assignment]
    find_dxc_executable = lambda explicit_path=None: None  # type: ignore[assignment]
    find_windows_kits_dxc_executable = lambda: None  # type: ignore[assignment]
    load_pinned_dxc_version = lambda repo_root: "1.8.2407"  # type: ignore[assignment]
    query_dxc_version = lambda dxc_bin: "unknown"  # type: ignore[assignment]
    validate_dxil_container = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parent.parent
PINNED_DXC_VERSION = "1.8.2407"
SHADER_TARGET = "cs_6_0"
ENTRY_POINT = "CSMain"
PASS_FAMILY = "compute.math_noise"
ROOT_SIGNATURE_CONVENTION = "compute.math_noise.v1"
THREAD_GROUP = (64, 1, 1)
DISPATCH_GROUPS = (4, 1, 1)
OUTPUT_ELEMENT_COUNT = 256
PARAMETER_BUFFER_BYTES = 64
OUTPUT_BUFFER_BYTES = OUTPUT_ELEMENT_COUNT * 16

_NW02_DIGEST_BINARY: Path | None | bool = None
_NW02_DIGEST_CACHE: dict[str, str] = {}


@dataclass(frozen=True)
class SemanticIdentity:
    digest: str
    provenance: str  # "nw02" or "test"

    @property
    def is_test(self) -> bool:
        return self.provenance == "test"


class CompilerError(RuntimeError):
    """Raised when lowering/compilation/package validation fails."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


# ---------------------------------------------------------------------------
# Small .nwgraph v1 reader / semantic validator (compiler-side only)
# ---------------------------------------------------------------------------


SHADER_OPERATIONS = {
    "nw.shader.expr": {
        "vec2",
        "vec3",
        "vec4",
        "sin",
        "cos",
        "mix",
        "texture2d",
        "uv",
        "time",
        "const",
    },
    "nw.shader.vec4_const": {"vec4", "const"},
    "nw.shader.add": {"add"},
    "nw.shader.mul": {"mul"},
    "nw.shader.mix": {"mix"},
}

KNOWN_OPERATORS = {
    "nw.math.scalar_const",
    "nw.math.vec2_const",
    "nw.math.vec3_const",
    "nw.math.vec4_const",
    "nw.math.mat4_const",
    "nw.math.add",
    "nw.math.mul",
    "nw.math.sin",
    "nw.math.clamp01",
    "nw.math.vec4_scale",
    "nw.math.vec4_split",
    "nw.math.dot",
    "nw.control.smoothstep",
    "nw.control.seed",
    "nw.control.image_cpu_passthrough",
    "nw.proc.hash_noise",
    "nw.proc.value_noise",
    "nw.proc.checker",
    "nw.proc.palette",
    "nw.curve.keyframes",
    "nw.curve.sample",
    "nw.scene.flat",
    "nw.camera.perspective",
    "nw.light.directional",
    "nw.material.describe",
    "nw.shader.vec4_const",
    "nw.shader.expr",
    "nw.shader.add",
    "nw.shader.mul",
    "nw.shader.mix",
    "nw.texture.procedural",
    "nw.buffer.recipe",
    "nw.mesh.recipe",
    "nw.residency.upload_texture",
    "nw.residency.upload_buffer",
    "nw.residency.upload_mesh",
    "nw.residency.cpu_to_gpu",
    "nw.residency.gpu_to_cpu",
    "nw.readback.scalar",
    "nw.render.fullscreen",
    "nw.render.gbuffer",
    "nw.render.composite",
    "nw.render.present",
}

OPERATOR_INPUTS = {
    "nw.math.add": {"a", "b"},
    "nw.math.mul": {"a", "b"},
    "nw.math.sin": {"x"},
    "nw.math.clamp01": {"x"},
    "nw.math.vec4_scale": {"v", "s"},
    "nw.math.vec4_split": {"v"},
    "nw.math.dot": {"a", "b"},
    "nw.control.smoothstep": {"edge0", "edge1", "x"},
    "nw.control.image_cpu_passthrough": {"image"},
    "nw.proc.hash_noise": {"seed"},
    "nw.proc.value_noise": {"position"},
    "nw.proc.checker": {"uv", "scale"},
    "nw.proc.palette": {"t"},
    "nw.curve.sample": {"curve", "t"},
    "nw.material.describe": {"base_color", "roughness"},
    "nw.shader.add": {"a", "b"},
    "nw.shader.mul": {"a", "b"},
    "nw.shader.mix": {"a", "b"},
    "nw.residency.upload_texture": {"texture"},
    "nw.residency.upload_buffer": {"buffer"},
    "nw.residency.upload_mesh": {"mesh"},
    "nw.residency.cpu_to_gpu": {"image"},
    "nw.residency.gpu_to_cpu": {"image"},
    "nw.readback.scalar": {"image"},
    "nw.render.fullscreen": {"shader", "texture"},
    "nw.render.gbuffer": {"mesh", "material", "camera", "light", "shader"},
    "nw.render.composite": {"color", "normal"},
    "nw.render.present": {"image"},
}

OUTPUT_PORTS = {
    "nw.math.add": {"out"},
    "nw.math.mul": {"out"},
    "nw.math.sin": {"out"},
    "nw.math.clamp01": {"out"},
    "nw.math.vec4_scale": {"out"},
    "nw.math.vec4_split": {"x", "y", "z", "w"},
    "nw.math.dot": {"out"},
    "nw.control.smoothstep": {"out"},
    "nw.control.seed": {"out"},
    "nw.control.image_cpu_passthrough": {"out"},
    "nw.proc.hash_noise": {"out"},
    "nw.proc.value_noise": {"out"},
    "nw.proc.checker": {"out"},
    "nw.proc.palette": {"out"},
    "nw.curve.keyframes": {"out"},
    "nw.curve.sample": {"out"},
    "nw.scene.flat": {"out"},
    "nw.camera.perspective": {"out"},
    "nw.light.directional": {"out"},
    "nw.material.describe": {"out"},
    "nw.shader.vec4_const": {"out"},
    "nw.shader.expr": {"out"},
    "nw.shader.add": {"out"},
    "nw.shader.mul": {"out"},
    "nw.shader.mix": {"out"},
    "nw.texture.procedural": {"out"},
    "nw.buffer.recipe": {"out"},
    "nw.mesh.recipe": {"out"},
    "nw.residency.upload_texture": {"out"},
    "nw.residency.upload_buffer": {"out"},
    "nw.residency.upload_mesh": {"out"},
    "nw.residency.cpu_to_gpu": {"out"},
    "nw.residency.gpu_to_cpu": {"out"},
    "nw.readback.scalar": {"value"},
    "nw.render.fullscreen": {"image"},
    "nw.render.gbuffer": {"color", "normals"},
    "nw.render.composite": {"out"},
    "nw.render.present": {"out"},
}

SHADER_OPERATORS = {
    "nw.shader.vec4_const",
    "nw.shader.expr",
    "nw.shader.add",
    "nw.shader.mul",
    "nw.shader.mix",
}

RESIDENCY_GPU = {"gpu", "readback"}

# Minimal compiler-side port contracts used for source-linked validation before
# lowering.  The authoritative NW-02 registry remains src/graph/; these entries
# let the standalone Python tool report the same high-level invalid-graph
# categories without linking a graph parser into the player.
PORT_SPECS: dict[str, dict[str, dict[str, tuple[str, int, str]]]] = {
    "nw.math.scalar_const": {"outputs": {"out": ("scalar", 1, "cpu")}},
    "nw.math.vec2_const": {"outputs": {"out": ("vec2", 2, "cpu")}},
    "nw.math.vec3_const": {"outputs": {"out": ("vec3", 3, "cpu")}},
    "nw.math.vec4_const": {"outputs": {"out": ("vec4", 4, "cpu")}},
    "nw.math.mat4_const": {"outputs": {"out": ("mat4", 16, "cpu")}},
    "nw.math.add": {
        "inputs": {"a": ("scalar", 1, "cpu"), "b": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.math.mul": {
        "inputs": {"a": ("scalar", 1, "cpu"), "b": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.math.sin": {
        "inputs": {"x": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.math.clamp01": {
        "inputs": {"x": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.math.vec4_scale": {
        "inputs": {"v": ("vec4", 4, "cpu"), "s": ("scalar", 1, "cpu")},
        "outputs": {"out": ("vec4", 4, "cpu")},
    },
    "nw.math.vec4_split": {
        "inputs": {"v": ("vec4", 4, "cpu")},
        "outputs": {
            "x": ("scalar", 1, "cpu"),
            "y": ("scalar", 1, "cpu"),
            "z": ("scalar", 1, "cpu"),
            "w": ("scalar", 1, "cpu"),
        },
    },
    "nw.math.dot": {
        "inputs": {"a": ("vec3", 3, "cpu"), "b": ("vec3", 3, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.control.smoothstep": {
        "inputs": {
            "edge0": ("scalar", 1, "cpu"),
            "edge1": ("scalar", 1, "cpu"),
            "x": ("scalar", 1, "cpu"),
        },
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.control.seed": {"outputs": {"out": ("scalar", 1, "cpu")}},
    "nw.control.image_cpu_passthrough": {
        "inputs": {"image": ("image", 1, "cpu")},
        "outputs": {"out": ("image", 1, "cpu")},
    },
    "nw.proc.hash_noise": {
        "inputs": {"seed": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.proc.value_noise": {
        "inputs": {"position": ("vec3", 3, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.proc.checker": {
        "inputs": {"uv": ("vec2", 2, "cpu"), "scale": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.proc.palette": {
        "inputs": {"t": ("scalar", 1, "cpu")},
        "outputs": {"out": ("vec3", 3, "cpu")},
    },
    "nw.curve.keyframes": {"outputs": {"out": ("curve", 1, "cpu")}},
    "nw.curve.sample": {
        "inputs": {"curve": ("curve", 1, "cpu"), "t": ("scalar", 1, "cpu")},
        "outputs": {"out": ("scalar", 1, "cpu")},
    },
    "nw.scene.flat": {"outputs": {"out": ("scene", 1, "cpu")}},
    "nw.camera.perspective": {"outputs": {"out": ("camera", 1, "cpu")}},
    "nw.light.directional": {"outputs": {"out": ("light", 1, "cpu")}},
    "nw.material.describe": {
        "inputs": {
            "base_color": ("vec4", 4, "cpu"),
            "roughness": ("scalar", 1, "cpu"),
        },
        "outputs": {"out": ("material", 1, "cpu")},
    },
    "nw.shader.vec4_const": {"outputs": {"out": ("shader_expr", 1, "logical")}},
    "nw.shader.expr": {"outputs": {"out": ("shader_expr", 1, "logical")}},
    "nw.shader.add": {
        "inputs": {
            "a": ("shader_expr", 1, "logical"),
            "b": ("shader_expr", 1, "logical"),
        },
        "outputs": {"out": ("shader_expr", 1, "logical")},
    },
    "nw.shader.mul": {
        "inputs": {
            "a": ("shader_expr", 1, "logical"),
            "b": ("shader_expr", 1, "logical"),
        },
        "outputs": {"out": ("shader_expr", 1, "logical")},
    },
    "nw.shader.mix": {
        "inputs": {
            "a": ("shader_expr", 1, "logical"),
            "b": ("shader_expr", 1, "logical"),
        },
        "outputs": {"out": ("shader_expr", 1, "logical")},
    },
    "nw.texture.procedural": {"outputs": {"out": ("texture", 1, "logical")}},
    "nw.buffer.recipe": {"outputs": {"out": ("buffer", 1, "logical")}},
    "nw.mesh.recipe": {"outputs": {"out": ("mesh", 1, "logical")}},
    "nw.residency.upload_texture": {
        "inputs": {"texture": ("texture", 1, "logical")},
        "outputs": {"out": ("texture", 1, "gpu")},
    },
    "nw.residency.upload_buffer": {
        "inputs": {"buffer": ("buffer", 1, "logical")},
        "outputs": {"out": ("buffer", 1, "gpu")},
    },
    "nw.residency.upload_mesh": {
        "inputs": {"mesh": ("mesh", 1, "logical")},
        "outputs": {"out": ("mesh", 1, "gpu")},
    },
    "nw.residency.cpu_to_gpu": {
        "inputs": {"image": ("image", 1, "cpu")},
        "outputs": {"out": ("image", 1, "gpu")},
    },
    "nw.residency.gpu_to_cpu": {
        "inputs": {"image": ("image", 1, "gpu")},
        "outputs": {"out": ("image", 1, "cpu")},
    },
    "nw.readback.scalar": {
        "inputs": {"image": ("image", 1, "gpu")},
        "outputs": {"value": ("scalar", 1, "cpu")},
    },
    "nw.render.fullscreen": {
        "inputs": {
            "shader": ("shader_expr", 1, "logical"),
            "texture": ("texture", 1, "gpu"),
        },
        "outputs": {"image": ("image", 1, "gpu")},
    },
    "nw.render.gbuffer": {
        "inputs": {
            "mesh": ("mesh", 1, "gpu"),
            "material": ("material", 1, "cpu"),
            "camera": ("camera", 1, "cpu"),
            "light": ("light", 1, "cpu"),
            "shader": ("shader_expr", 1, "logical"),
        },
        "outputs": {
            "color": ("image", 1, "gpu"),
            "normals": ("image", 1, "gpu"),
        },
    },
    "nw.render.composite": {
        "inputs": {
            "color": ("image", 1, "gpu"),
            "normal": ("image", 1, "gpu"),
        },
        "outputs": {"out": ("image", 1, "gpu")},
    },
    "nw.render.present": {
        "inputs": {"image": ("image", 1, "gpu")},
        "outputs": {"out": ("image", 1, "gpu")},
    },
}

PARAM_RULES: dict[str, dict[str, dict[str, Any]]] = {
    "nw.shader.mix": {"factor": {"kind": "float", "min": 0.0, "max": 1.0, "clamp01": True}},
    "nw.material.describe": {
        "metallic": {"kind": "float", "min": 0.0, "max": 1.0, "clamp01": True},
        "specular": {"kind": "float", "min": 0.0, "max": 1.0, "clamp01": True},
    },
    "nw.math.scalar_const": {"value": {"kind": "float"}},
    "nw.math.vec2_const": {"value": {"kind": "vec2"}},
    "nw.math.vec3_const": {"value": {"kind": "vec3"}},
    "nw.math.vec4_const": {"value": {"kind": "vec4"}},
    "nw.math.mat4_const": {"value": {"kind": "mat4", "allow_identity": True}},
    "nw.texture.procedural": {
        "width": {"kind": "int", "min": 1, "max": 8192},
        "height": {"kind": "int", "min": 1, "max": 8192},
        "format": {"kind": "enum", "values": {"r8g8b8a8_unorm", "rgba32f"}},
        "seed": {"kind": "int", "min": 0, "max": 4294967295},
    },
    "nw.buffer.recipe": {
        "bytes": {"kind": "int", "min": 1, "max": 67108864},
        "format": {"kind": "enum", "values": {"r32_uint", "rgba32f"}},
    },
    "nw.mesh.recipe": {
        "vertices": {"kind": "int", "min": 1, "max": 1048576},
        "indices": {"kind": "int", "min": 1, "max": 3145728},
    },
    "nw.camera.perspective": {
        "fov_degrees": {"kind": "float", "min": 1.0, "max": 179.0},
        "aspect": {"kind": "float", "min": 0.01},
        "near": {"kind": "float", "min": 0.0001},
        "far": {"kind": "float", "min": 0.0002},
    },
}

SHADER_SEED_BEHAVIORS = {
    "nw.control.seed": {"explicit", "derived-from-graph", "node-id", "derived"},
    "nw.proc.hash_noise": {"explicit", "derived", "derived-from-upstream"},
}


@dataclass
class GraphNode:
    node_id: str
    operator_id: str
    version: str
    frequency: str
    residency: str
    params: dict[str, list[str]] = field(default_factory=dict)
    inputs: dict[str, tuple[str, str]] = field(default_factory=dict)
    # NW-05 dead-output elimination: retained output ports for this node.
    # Empty means the operator's full declared output set.
    selected_outputs: tuple[str, ...] = ()

    @property
    def operator_key(self) -> str:
        return f"{self.operator_id}@{self.version}"


@dataclass
class GraphDocument:
    schema_version: int
    profile_id: str
    requirements_digest: str
    settings: dict[str, str]
    nodes: dict[str, GraphNode]
    source_name: str
    source_text: str
    layout: dict[str, tuple[float, float]] = field(default_factory=dict)

    def ordered_nodes(self) -> list[GraphNode]:
        return [self.nodes[node_id] for node_id in self.nodes]

    def semantic_identity(self) -> SemanticIdentity:
        native = _native_nw02_digest(self)
        if native is not None:
            return SemanticIdentity(native, "nw02")
        if os.environ.get("NODEWERK_NW03_TEST_DIGEST") == "1":
            # Explicit test-only identity used by packaging/cache tests on hosts
            # without a C++ compiler. Production never takes this path.
            return SemanticIdentity(
                "sha256:v1:"
                + sha256_hex(
                    b"nw03-test-semantic-digest:"
                    + self.source_text.encode("utf-8")
                ),
                "test",
            )
        raise CompilerError(
            "NW-02 canonical semantic digest helper is unavailable; refusing "
            "to substitute an incompatible package identity"
        )

    def semantic_digest(self) -> str:
        return self.semantic_identity().digest


def _diagnostic(
    code: str,
    message: str,
    *,
    node_id: str = "",
    operator_id: str = "",
    port_id: str = "",
    line: int = 0,
    column: int = 0,
    category: str = "graph_validation",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "category": category,
        "severity": "error",
        "code": code,
        "message": message,
    }
    source = {
        "node_id": node_id,
        "operator_id": operator_id,
        "port_id": port_id,
        "line": line,
        "column": column,
    }
    if any(source.values()):
        result["source"] = source
    return result


def _tokenize_line(line: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in line:
        if quoted:
            if escaped:
                current.append(
                    {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(
                        character, character
                    )
                )
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
                tokens.append("".join(current))
                current = []
            else:
                current.append(character)
            continue
        if character == "#":
            break
        if character == '"':
            if current:
                tokens.append("".join(current))
                current = []
            quoted = True
            continue
        if character.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(character)
    if current:
        tokens.append("".join(current))
    return tokens


def parse_graph_text(
    text: str,
    *,
    source_name: str = "<memory>",
) -> tuple[GraphDocument | None, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    lines = text.splitlines()
    schema_version = 1
    seen_header = False
    profile_id = ""
    requirements_digest = ""
    settings: dict[str, str] = {}
    nodes: dict[str, GraphNode] = {}
    layout: dict[str, tuple[float, float]] = {}
    current: GraphNode | None = None
    for line_number, raw_line in enumerate(lines, start=1):
        tokens = _tokenize_line(raw_line)
        if not tokens:
            continue
        keyword = tokens[0]
        if keyword == "nwgraph":
            if len(tokens) != 2:
                diagnostics.append(
                    _diagnostic(
                        "parse_error",
                        "nwgraph header requires exactly one schema version",
                        line=line_number,
                    )
                )
                continue
            seen_header = True
            try:
                schema_version = int(tokens[1], 10)
            except ValueError:
                diagnostics.append(
                    _diagnostic(
                        "parse_error",
                        f"invalid schema version '{tokens[1]}'",
                        line=line_number,
                    )
                )
            continue
        if keyword == "profile":
            if len(tokens) != 3:
                diagnostics.append(
                    _diagnostic(
                        "malformed_capability_profile",
                        "profile requires an id and a requirements digest",
                        line=line_number,
                    )
                )
                continue
            profile_id = tokens[1]
            requirements_digest = tokens[2]
            current = None
            continue
        if keyword == "setting":
            if len(tokens) < 3:
                diagnostics.append(
                    _diagnostic(
                        "parse_error", "setting requires a key and value", line=line_number
                    )
                )
                continue
            settings[tokens[1]] = " ".join(tokens[2:])
            current = None
            continue
        if keyword == "node":
            if len(tokens) < 3 or "@" not in tokens[2]:
                diagnostics.append(
                    _diagnostic(
                        "parse_error",
                        "node requires an id and operator@version",
                        line=line_number,
                    )
                )
                current = None
                continue
            node_id = tokens[1]
            operator_id, version = tokens[2].split("@", 1)
            frequency = "frame"
            residency = "cpu"
            for attribute in tokens[3:]:
                if attribute.startswith("frequency="):
                    frequency = attribute.split("=", 1)[1]
                elif attribute.startswith("residency="):
                    residency = attribute.split("=", 1)[1]
            if node_id in nodes:
                diagnostics.append(
                    _diagnostic(
                        "duplicate_node_id",
                        f"duplicate node id '{node_id}'",
                        node_id=node_id,
                        line=line_number,
                    )
                )
            node = GraphNode(
                node_id=node_id,
                operator_id=operator_id,
                version=version,
                frequency=frequency,
                residency=residency,
            )
            nodes[node_id] = node
            current = node
            continue
        if keyword == "layout":
            current = None
            if len(tokens) >= 4:
                try:
                    layout[tokens[1]] = (float(tokens[2]), float(tokens[3]))
                except ValueError:
                    diagnostics.append(
                        _diagnostic(
                            "parse_error",
                            "layout coordinates must be numeric",
                            line=line_number,
                        )
                    )
            continue
        if current is None:
            diagnostics.append(
                _diagnostic(
                    "parse_error",
                    f"unexpected directive '{keyword}'",
                    line=line_number,
                )
            )
            continue
        if keyword == "param":
            if len(tokens) < 2:
                diagnostics.append(
                    _diagnostic(
                        "parse_error",
                        "param requires a name",
                        node_id=current.node_id,
                        line=line_number,
                    )
                )
                continue
            current.params[tokens[1]] = tokens[2:]
        elif keyword == "input":
            if len(tokens) != 3 or "." not in tokens[2]:
                diagnostics.append(
                    _diagnostic(
                        "parse_error",
                        "input requires <port> <node>.<port>",
                        node_id=current.node_id,
                        line=line_number,
                    )
                )
                continue
            source_node, source_port = tokens[2].rsplit(".", 1)
            current.inputs[tokens[1]] = (source_node, source_port)
        elif keyword == "seed":
            current.params.setdefault("__seed__", tokens[1:])
        else:
            diagnostics.append(
                _diagnostic(
                    "parse_error",
                    f"unexpected node directive '{keyword}'",
                    node_id=current.node_id,
                    line=line_number,
                )
            )

    if not seen_header:
        diagnostics.append(
            _diagnostic("parse_error", "missing nwgraph header", line=1)
        )
    if not profile_id or not requirements_digest:
        diagnostics.append(
            _diagnostic(
                "malformed_capability_profile",
                "graph is missing a complete profile identity",
            )
        )
    if any(item["severity"] == "error" for item in diagnostics):
        return None, diagnostics
    _apply_clamp01_normalisation(nodes)
    document = GraphDocument(
        schema_version=schema_version,
        profile_id=profile_id,
        requirements_digest=requirements_digest,
        settings=_normalise_settings(settings),
        nodes=nodes,
        source_name=source_name,
        source_text=text,
        layout=layout,
    )
    diagnostics.extend(validate_graph_document(document))
    return document, diagnostics


def _normalise_settings(settings: dict[str, str]) -> dict[str, str]:
    result = {
        "optimization": "O2",
        "deterministic": "true",
        "api": "d3d12",
        "feature_level": "12_0",
        "shader_model": "6_0",
        "frequency_policy": "strict",
        "shader_blob_policy": "stable",
        "quantization_policy": "none",
        "quantization_bits": "0",
        "quantization_step": "0",
    }
    result.update(settings)
    return result


def _is_wellformed_profile_id(value: str) -> bool:
    if not value.startswith("nw.") or len(value) < 6:
        return False
    version_position = value.rfind(".v")
    if version_position < 0 or version_position + 2 >= len(value):
        return False
    if not value[version_position + 2 :].isdigit():
        return False
    return value.count(".") >= 4


def _is_wellformed_digest(value: str) -> bool:
    prefix = "sha256:v1:"
    return value.startswith(prefix) and len(value) == len(prefix) + 64 and all(
        character in "0123456789abcdef" for character in value[len(prefix):]
    )


def _param_tokens(node: GraphNode, name: str) -> list[str]:
    result: list[str] = []
    for token in node.params.get(name, []):
        result.extend(part for part in re.split(r"[,\s]+", token) if part)
    return result


def _parse_numeric_token(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value


def _normalise_zero(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _shader_number_text(value: float) -> str:
    """Match NW-02 ``ShaderNumberText`` for normalized parameter values."""
    value = _normalise_zero(value)
    text = format(value, ".17g")
    if "." not in text and "e" not in text and "E" not in text:
        text += ".0"
    return text


def _apply_clamp01_normalisation(nodes: dict[str, GraphNode]) -> None:
    """Apply NW-02's clamp01 parameter normalization before evaluation."""
    for node in nodes.values():
        rules = PARAM_RULES.get(node.operator_id, {})
        for name, rule in rules.items():
            if not rule.get("clamp01"):
                continue
            tokens = _param_tokens(node, name)
            if not tokens:
                continue
            normalized: list[str] = []
            valid = True
            for token in tokens:
                value = _parse_numeric_token(token)
                if value is None or not math.isfinite(value):
                    valid = False
                    break
                normalized.append(
                    _shader_number_text(
                        _normalise_zero(min(max(value, 0.0), 1.0))
                    )
                )
            if valid:
                node.params[name] = [" ".join(normalized)]


def _ensure_nw02_digest_binary() -> Path | None:
    """Build the authoritative NW-02 digest CLI once when a compiler exists."""
    global _NW02_DIGEST_BINARY
    if _NW02_DIGEST_BINARY is not None:
        return _NW02_DIGEST_BINARY or None  # type: ignore[return-value]
    compiler = None
    for name in ("c++", "g++", "clang++", "clang-cl", "cl"):
        compiler = shutil.which(name)
        if compiler:
            break
    if compiler is None:
        _NW02_DIGEST_BINARY = False
        return None
    source_dir = REPO_ROOT / "src" / "graph"
    sources = [source_dir / "nw_graph.cpp", source_dir / "nw_graph_cli.cpp"]
    if not all(path.is_file() for path in sources):
        _NW02_DIGEST_BINARY = False
        return None
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.read_bytes())
    output_dir = Path(tempfile.gettempdir()) / (
        "nw03-nw02-digest-" + digest.hexdigest()[:16]
    )
    executable = output_dir / (
        "nw_graph_cli.exe" if os.name == "nt" else "nw_graph_cli"
    )
    if not executable.is_file():
        output_dir.mkdir(parents=True, exist_ok=True)
        compiler_name = Path(compiler).name.lower()
        if compiler_name in {"cl", "cl.exe", "clang-cl", "clang-cl.exe"}:
            command = [
                compiler,
                "/nologo",
                "/std:c++20",
                "/EHsc",
                "/O2",
                "/I",
                str(source_dir),
                *[str(source) for source in sources],
                f"/Fe:{executable}",
            ]
        else:
            command = [
                compiler,
                "-std=c++20",
                "-O2",
                "-I",
                str(source_dir),
                *[str(source) for source in sources],
                "-o",
                str(executable),
            ]
        process = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=120
        )
        if process.returncode != 0 or not executable.is_file():
            _NW02_DIGEST_BINARY = False
            return None
    _NW02_DIGEST_BINARY = executable
    return executable


def _native_nw02_digest(document: GraphDocument) -> str | None:
    key = hashlib.sha256(document.source_text.encode("utf-8")).hexdigest()
    cached = _NW02_DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    executable = _ensure_nw02_digest_binary()
    if executable is None:
        return None
    temporary = None
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix="nw02-digest-", suffix=".nwgraph"
        )
        temporary = Path(raw_path)
        with os.fdopen(fd, "wb") as stream:
            stream.write(document.source_text.encode("utf-8"))
        process = subprocess.run(
            [str(executable), "digest", str(temporary)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if process.returncode != 0:
            return None
        for line in reversed(process.stdout.splitlines()):
            value = line.strip()
            if value.startswith("sha256:v1:") and len(value) == 74:
                _NW02_DIGEST_CACHE[key] = value
                return value
        return None
    except Exception:
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _validate_node_parameters(node: GraphNode) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    rules = PARAM_RULES.get(node.operator_id, {})
    for name, rule in rules.items():
        tokens = _param_tokens(node, name)
        if not tokens:
            diagnostics.append(
                _diagnostic(
                    "missing_parameter",
                    f"node '{node.node_id}' is missing parameter '{name}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    port_id=name,
                )
            )
            continue
        kind = rule["kind"]
        if rule.get("allow_identity") and tokens and tokens[0] == "identity":
            continue
        if kind == "enum":
            if tokens[0] not in rule["values"]:
                diagnostics.append(
                    _diagnostic(
                        "invalid_parameter_value",
                        f"parameter '{name}' value '{tokens[0]}' is not supported",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=name,
                    )
                )
            continue
        numeric_values: list[float] = []
        for token in tokens:
            value = _parse_numeric_token(token)
            if value is None:
                diagnostics.append(
                    _diagnostic(
                        "invalid_parameter_value",
                        f"parameter '{name}' has non-numeric value '{token}'",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=name,
                    )
                )
                break
            if not math.isfinite(value):
                diagnostics.append(
                    _diagnostic(
                        "non_finite_parameter",
                        f"parameter '{name}' contains a non-finite value",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=name,
                    )
                )
                break
            numeric_values.append(value)
        expected_width = {"float": 1, "int": 1, "vec2": 2, "vec3": 3, "vec4": 4, "mat4": 16}.get(kind)
        if expected_width is not None and len(numeric_values) != expected_width:
            diagnostics.append(
                _diagnostic(
                    "invalid_parameter_value",
                    f"parameter '{name}' has {len(numeric_values)} components, "
                    f"expected {expected_width}",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    port_id=name,
                )
            )
        if kind == "int":
            for value in numeric_values:
                if not float(value).is_integer():
                    diagnostics.append(
                        _diagnostic(
                            "invalid_parameter_value",
                            f"parameter '{name}' must be an integer",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id=name,
                        )
                    )
                    break
        for value in numeric_values:
            if rule.get("clamp01"):
                continue
            if "min" in rule and value < rule["min"]:
                diagnostics.append(
                    _diagnostic(
                        "parameter_out_of_range",
                        f"parameter '{name}' value {value} is below {rule['min']}",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=name,
                    )
                )
                break
            if "max" in rule and value > rule["max"]:
                diagnostics.append(
                    _diagnostic(
                        "parameter_out_of_range",
                        f"parameter '{name}' value {value} is above {rule['max']}",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=name,
                    )
                )
                break

    if node.operator_id == "nw.texture.procedural":
        width_tokens = _param_tokens(node, "width")
        height_tokens = _param_tokens(node, "height")
        format_tokens = _param_tokens(node, "format")
        if width_tokens and height_tokens:
            width = _parse_numeric_token(width_tokens[0])
            height = _parse_numeric_token(height_tokens[0])
            format_name = format_tokens[0] if format_tokens else "r8g8b8a8_unorm"
            bytes_per_element = {"r8g8b8a8_unorm": 4, "rgba32f": 16}.get(format_name)
            if width is not None and height is not None and bytes_per_element is not None:
                if width > 8192 or height > 8192:
                    diagnostics.append(
                        _diagnostic(
                            "bounded_resource_exceeded",
                            f"texture dimensions {width}x{height} exceed 8192",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                        )
                    )
                elif width * height * bytes_per_element > 8192 * 8192 * 4:
                    diagnostics.append(
                        _diagnostic(
                            "bounded_resource_exceeded",
                            "texture byte budget exceeds the bounded 256 MiB descriptor",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                        )
                    )

    if node.operator_id == "nw.curve.keyframes":
        keys = " ".join(node.params.get("keys", []))
        valid = bool(keys.strip())
        if valid:
            for entry in keys.split(";"):
                if ":" not in entry:
                    valid = False
                    break
                time_text, value_text = entry.split(":", 1)
                time_value = _parse_numeric_token(time_text.strip())
                sample_value = _parse_numeric_token(value_text.strip())
                if (
                    time_value is None
                    or sample_value is None
                    or not math.isfinite(time_value)
                    or not math.isfinite(sample_value)
                ):
                    valid = False
                    break
        if not valid:
            diagnostics.append(
                _diagnostic(
                    "invalid_parameter_value",
                    "curve keys must be finite '<time>:<value>' entries",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    port_id="keys",
                )
            )

    seed_tokens = node.params.get("__seed__", [])
    if seed_tokens:
        behavior = seed_tokens[0]
        allowed = SHADER_SEED_BEHAVIORS.get(node.operator_id)
        if behavior not in {"none", "explicit", "node-id", "derived", "derived-from-graph", "derived-from-upstream"}:
            diagnostics.append(
                _diagnostic(
                    "invalid_seed_source",
                    f"unknown seed behavior '{behavior}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        elif allowed is not None and behavior not in allowed:
            diagnostics.append(
                _diagnostic(
                    "unsupported_seed_behavior",
                    f"seed behavior '{behavior}' is not supported by {node.operator_id}",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        if behavior == "explicit" and len(seed_tokens) < 2:
            diagnostics.append(
                _diagnostic(
                    "missing_explicit_seed",
                    "explicit seed behavior requires a uint64 value",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        if behavior == "node-id" and len(seed_tokens) < 2:
            diagnostics.append(
                _diagnostic(
                    "invalid_seed_source",
                    "node-id seed behavior requires a node id",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
    return diagnostics


def validate_graph_document(document: GraphDocument) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if document.schema_version != 1:
        diagnostics.append(
            _diagnostic(
                "unknown_schema_version",
                f"unknown graph schema version {document.schema_version}",
            )
        )
        return diagnostics
    if not _is_wellformed_profile_id(document.profile_id):
        diagnostics.append(
            _diagnostic(
                "malformed_capability_profile",
                f"malformed capability-profile identity '{document.profile_id}'",
            )
        )
    elif document.profile_id != nwp_package.COMMON_PROFILE_ID:
        diagnostics.append(
            _diagnostic(
                "unknown_capability_profile",
                f"unknown capability-profile identity '{document.profile_id}'",
            )
        )
    if not _is_wellformed_digest(document.requirements_digest):
        diagnostics.append(
            _diagnostic(
                "malformed_capability_profile",
                "malformed capability-profile requirements digest",
            )
        )
    elif document.requirements_digest != nwp_package.COMMON_REQUIREMENTS_DIGEST:
        diagnostics.append(
            _diagnostic(
                "capability_profile_digest_mismatch",
                "capability-profile requirements digest does not match the common profile",
            )
        )
    if document.settings.get("shader_model") != "6_0":
        diagnostics.append(
            _diagnostic(
                "invalid_compiler_setting",
                f"unsupported shader_model '{document.settings.get('shader_model')}'",
            )
        )
    if document.settings.get("feature_level") != "12_0":
        diagnostics.append(
            _diagnostic(
                "invalid_compiler_setting",
                f"unsupported feature_level '{document.settings.get('feature_level')}'",
            )
        )
    if document.settings.get("shader_blob_policy") != "stable":
        diagnostics.append(
            _diagnostic(
                "invalid_compiler_setting",
                "shader_blob_policy must be 'stable'",
            )
        )

    frequency_rank = {"compile-time": 0, "initialization": 1, "frame": 2}
    residency_rank = {"logical": 0, "cpu": 1, "gpu": 2, "readback": 3}
    for node in document.ordered_nodes():
        if node.operator_id not in KNOWN_OPERATORS:
            diagnostics.append(
                _diagnostic(
                    "unknown_operator",
                    f"unknown operator '{node.operator_id}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
            continue
        if node.version != "1.0.0":
            diagnostics.append(
                _diagnostic(
                    "unknown_operator_version",
                    f"unknown operator version '{node.operator_key}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        if node.frequency == "history":
            diagnostics.append(
                _diagnostic(
                    "history_not_supported",
                    "history frequency is reserved for NW-09",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        elif node.frequency not in frequency_rank:
            diagnostics.append(
                _diagnostic(
                    "unsupported_frequency",
                    f"unsupported frequency '{node.frequency}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        if node.residency not in residency_rank:
            diagnostics.append(
                _diagnostic(
                    "invalid_residency",
                    f"invalid residency '{node.residency}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                )
            )
        if node.operator_id in SHADER_OPERATORS:
            if node.operator_id == "nw.shader.expr":
                expression_values = node.params.get("expression", [])
                expression = " ".join(expression_values)
                try:
                    parsed = nw03_oracle.parse_expression(expression)
                    _validate_declared_shader_operations(parsed, node.operator_id, node.node_id)
                except nw03_oracle.ExpressionError as exc:
                    diagnostics.append(
                        _diagnostic(
                            "invalid_expression",
                            str(exc),
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id="expression",
                        )
                    )
        diagnostics.extend(_validate_node_parameters(node))
        input_specs = PORT_SPECS.get(node.operator_id, {}).get("inputs", {})
        for missing in sorted(set(input_specs) - set(node.inputs)):
            diagnostics.append(
                _diagnostic(
                    "missing_input_port",
                    f"node '{node.node_id}' is missing input port '{missing}'",
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    port_id=missing,
                )
            )
        for port, (source_node, source_port) in node.inputs.items():
            if input_specs and port not in input_specs:
                diagnostics.append(
                    _diagnostic(
                        "unknown_input_port",
                        f"node '{node.node_id}' has no input port '{port}'",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=port,
                    )
                )
            if source_node not in document.nodes:
                diagnostics.append(
                    _diagnostic(
                        "unknown_input_node",
                        f"input '{port}' references unknown node '{source_node}'",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=port,
                    )
                )
                continue
            source = document.nodes[source_node]
            allowed_ports = set(
                PORT_SPECS.get(source.operator_id, {}).get("outputs", {})
            )
            if not allowed_ports:
                allowed_ports = OUTPUT_PORTS.get(source.operator_id)
            if allowed_ports is not None and source_port not in allowed_ports:
                diagnostics.append(
                    _diagnostic(
                        "unknown_output_port",
                        f"input '{port}' references unknown output port "
                        f"'{source_node}.{source_port}'",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=port,
                    )
                )
            if (
                source.frequency in frequency_rank
                and node.frequency in frequency_rank
                and frequency_rank[source.frequency] > frequency_rank[node.frequency]
            ):
                diagnostics.append(
                    _diagnostic(
                        "frequency_transition",
                        f"node '{node.node_id}' runs earlier than upstream '{source_node}'",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=port,
                    )
                )
            output_spec = (
                PORT_SPECS.get(source.operator_id, {})
                .get("outputs", {})
                .get(source_port)
            )
            input_spec = input_specs.get(port)
            if output_spec and input_spec:
                source_type, source_dimension, source_residency = output_spec
                expected_type, expected_dimension, expected_residency = input_spec
                both_vectors = source_type.startswith("vec") and expected_type.startswith(
                    "vec"
                )
                if both_vectors and source_dimension != expected_dimension:
                    diagnostics.append(
                        _diagnostic(
                            "port_dimension_mismatch",
                            f"input '{port}' expects dimension {expected_dimension}, "
                            f"got {source_dimension}",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id=port,
                        )
                    )
                elif source_type != expected_type:
                    diagnostics.append(
                        _diagnostic(
                            "port_type_mismatch",
                            f"input '{port}' expects {expected_type}, got {source_type} "
                            f"from '{source_node}.{source_port}'",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id=port,
                        )
                    )
                elif source_dimension != expected_dimension:
                    diagnostics.append(
                        _diagnostic(
                            "port_dimension_mismatch",
                            f"input '{port}' expects dimension {expected_dimension}, "
                            f"got {source_dimension}",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id=port,
                        )
                    )
                if source_residency == "gpu" and expected_residency == "cpu":
                    diagnostics.append(
                        _diagnostic(
                            "implicit_readback",
                            f"input '{port}' consumes GPU residency without an explicit "
                            "readback/passthrough node",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id=port,
                        )
                    )
                elif source_residency == "logical" and expected_residency == "gpu":
                    diagnostics.append(
                        _diagnostic(
                            "implicit_upload",
                            f"input '{port}' consumes a logical recipe without an explicit upload",
                            node_id=node.node_id,
                            operator_id=node.operator_id,
                            port_id=port,
                        )
                    )

    # Ordinary cycle detection and topological order.
    visited: dict[str, int] = {}

    def visit(node_id: str, path: list[str]) -> None:
        state = visited.get(node_id, 0)
        if state == 2:
            return
        if state == 1:
            cycle = " -> ".join(path + [node_id])
            diagnostics.append(
                _diagnostic(
                    "ordinary_cycle",
                    f"ordinary graph cycle detected: {cycle}",
                    node_id=node_id,
                )
            )
            return
        visited[node_id] = 1
        node = document.nodes.get(node_id)
        if node is not None:
            for source_node, _source_port in node.inputs.values():
                if source_node in document.nodes:
                    visit(source_node, path + [node_id])
        visited[node_id] = 2

    for node_id in document.nodes:
        visit(node_id, [])
    return diagnostics


def _validate_declared_shader_operations(
    expression: nw03_oracle.Expression,
    operator_id: str,
    node_id: str,
) -> None:
    allowed = SHADER_OPERATIONS.get(operator_id, set())
    for node in nw03_oracle.expression_nodes(expression):
        if isinstance(node, nw03_oracle.Call) and node.name not in allowed:
            raise nw03_oracle.ExpressionError(
                f"shader expression uses operation '{node.name}' not declared by "
                f"{operator_id}"
            )
        if isinstance(node, nw03_oracle.Variable) and node.name not in allowed:
            raise nw03_oracle.ExpressionError(
                f"shader expression uses identifier '{node.name}' not declared by "
                f"{operator_id}"
            )


def load_graph(path: str | os.PathLike[str]) -> GraphDocument:
    path = Path(path)
    try:
        source_bytes = path.read_bytes()
        text = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CompilerError(f"cannot read graph '{path}': {exc}") from exc
    document, diagnostics = parse_graph_text(text, source_name=str(path))
    if document is None:
        raise CompilerError(
            f"graph '{path}' is invalid",
            [item for item in diagnostics if item.get("severity") == "error"],
        )
    errors = [item for item in diagnostics if item.get("severity") == "error"]
    if errors:
        raise CompilerError(f"graph '{path}' is invalid", errors)
    return document


# ---------------------------------------------------------------------------
# ShaderExpr lowering and fused HLSL emission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedShader:
    specialization_id: str
    entry_point: str
    target: str
    family: str
    hlsl: str
    source_map: tuple[dict[str, Any], ...]
    root_expression: str
    root_node_ids: tuple[str, ...]
    thread_group: tuple[int, int, int] = THREAD_GROUP

    @property
    def hlsl_sha256(self) -> str:
        return sha256_hex(self.hlsl.encode("utf-8"))

    @property
    def specialization_key(self) -> str:
        return sha256_hex(
            nwp_package.canonical_json_bytes(
                {
                    "entry": self.entry_point,
                    "target": self.target,
                    "family": self.family,
                    "hlsl_sha256": self.hlsl_sha256,
                    "thread_group": list(self.thread_group),
                }
            )
        )


def _shader_expression_text(document: GraphDocument, node: GraphNode) -> str:
    if node.operator_id in {"nw.shader.expr"}:
        return " ".join(node.params.get("expression", []))
    if node.operator_id == "nw.shader.vec4_const":
        values: list[str] = []
        for token in node.params.get("value", []):
            values.extend(part for part in re.split(r"[,\s]+", token) if part)
        values = values[:4]
        while len(values) < 4:
            values.append("0.0")
        return "vec4(" + ", ".join(values) + ")"
    if node.operator_id in {"nw.shader.add", "nw.shader.mul", "nw.shader.mix"}:
        required = ("a", "b")
        parts: list[str] = []
        for port in required:
            connection = node.inputs.get(port)
            if connection is None:
                raise CompilerError(
                    f"shader node '{node.node_id}' is missing input '{port}'"
                )
            source_node, _source_port = connection
            source = document.nodes.get(source_node)
            if source is None:
                raise CompilerError(
                    f"shader node '{node.node_id}' references unknown node '{source_node}'"
                )
            if source.selected_outputs and _source_port not in source.selected_outputs:
                raise CompilerError(
                    f"shader node '{node.node_id}' reads dead output port "
                    f"'{source_node}.{_source_port}'"
                )
            parts.append(_shader_expression_text(document, source))
        if node.operator_id == "nw.shader.add":
            return f"({parts[0]} + {parts[1]})"
        if node.operator_id == "nw.shader.mul":
            return f"({parts[0]} * {parts[1]})"
        factor = node.params.get("factor", ["0.5"])
        return f"mix({parts[0]}, {parts[1]}, {' '.join(factor)})"
    raise CompilerError(
        f"node '{node.node_id}' ({node.operator_id}) is not a ShaderExpr producer"
    )


def _reachable_shader_roots(document: GraphDocument) -> list[GraphNode]:
    # Entry points are shader inputs of reachable GPU passes.  A ShaderExpr
    # that is not consumed by any GPU pass is not emitted when the graph has an
    # explicit render path; standalone authoring-expression documents (no GPU
    # pass) use their terminal ShaderExpr nodes as entry points instead.
    gpu_roots: list[GraphNode] = []
    for node in sorted(document.nodes.values(), key=lambda item: item.node_id):
        if not node.operator_id.startswith("nw.render."):
            continue
        connection = node.inputs.get("shader")
        if connection is None:
            continue
        source_node, _source_port = connection
        source = document.nodes.get(source_node)
        if (
            source is not None
            and source.node_id not in {item.node_id for item in gpu_roots}
            and (
                not source.selected_outputs
                or _source_port in source.selected_outputs
            )
        ):
            gpu_roots.append(source)
    if gpu_roots:
        return [node for node in gpu_roots if node.operator_id in SHADER_OPERATORS]

    consumers: dict[str, list[str]] = {node_id: [] for node_id in document.nodes}
    for node in document.nodes.values():
        for source_node, _source_port in node.inputs.values():
            if source_node in consumers:
                consumers[source_node].append(node.node_id)

    roots: list[GraphNode] = []
    seen: set[str] = set()
    for node in sorted(document.nodes.values(), key=lambda item: item.node_id):
        if node.operator_id not in SHADER_OPERATORS:
            continue
        # A shader expression is a root when consumed by a GPU pass or when no
        # downstream shader composition consumes it (standalone authoring
        # expression).  Nodes only used by another shader node remain part of
        # that fused expression.
        is_consumed_by_shader = any(
            document.nodes[consumer].operator_id in SHADER_OPERATORS
            for consumer in consumers.get(node.node_id, [])
        )
        if is_consumed_by_shader:
            continue
        if node.selected_outputs and "out" not in node.selected_outputs:
            continue
        if node.node_id not in seen:
            roots.append(node)
            seen.add(node.node_id)
    return roots


def _shader_dependencies(document: GraphDocument, root: GraphNode) -> set[str]:
    reachable: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in reachable:
            return
        reachable.add(node_id)
        node = document.nodes.get(node_id)
        if node is None:
            return
        for source_node, _source_port in node.inputs.values():
            visit(source_node)

    visit(root.node_id)
    return reachable


def lower_to_fused_hlsl(
    document: GraphDocument,
    *,
    target: str = SHADER_TARGET,
    entry_point: str = ENTRY_POINT,
    thread_group: tuple[int, int, int] = THREAD_GROUP,
    dispatch_groups: tuple[int, int, int] = DISPATCH_GROUPS,
) -> tuple[list[GeneratedShader], list[dict[str, Any]]]:
    """Lower all reachable shader roots and return fused generated shaders."""
    diagnostics: list[dict[str, Any]] = []
    roots = _reachable_shader_roots(document)
    if not roots:
        diagnostics.append(
            _diagnostic(
                "missing_implementation",
                "graph has no reachable ShaderExpr producer",
            )
        )
        return [], diagnostics

    generated: list[GeneratedShader] = []
    seen_keys: set[str] = set()
    for root in roots:
        try:
            expression_text = _shader_expression_text(document, root)
            parsed = nw03_oracle.parse_expression(expression_text)
            hlsl_expression = nw03_oracle.lower_to_hlsl(parsed)
        except (CompilerError, nw03_oracle.ExpressionError) as exc:
            diagnostics.append(
                _diagnostic(
                    "invalid_expression",
                    str(exc),
                    node_id=root.node_id,
                    operator_id=root.operator_id,
                    port_id="expression",
                )
            )
            continue

        dependency_ids = sorted(_shader_dependencies(document, root))
        hlsl, source_map = _emit_compute_hlsl(
            hlsl_expression,
            dependency_ids,
            thread_group=thread_group,
            dispatch_groups=dispatch_groups,
            entry_point=entry_point,
        )
        candidate = GeneratedShader(
            specialization_id=root.node_id,
            entry_point=entry_point,
            target=target,
            family=PASS_FAMILY,
            hlsl=hlsl,
            source_map=tuple(source_map),
            root_expression=expression_text,
            root_node_ids=tuple(dependency_ids),
            thread_group=thread_group,
        )
        if candidate.specialization_key in seen_keys:
            continue
        seen_keys.add(candidate.specialization_key)
        generated.append(candidate)
    return generated, diagnostics


def _emit_compute_hlsl(
    hlsl_expression: str,
    node_ids: Sequence[str],
    *,
    thread_group: tuple[int, int, int],
    dispatch_groups: tuple[int, int, int],
    entry_point: str,
) -> tuple[str, list[dict[str, Any]]]:
    group_x, group_y, group_z = thread_group
    dispatch_x, dispatch_y, dispatch_z = dispatch_groups
    width = group_x * dispatch_x
    height = group_y * dispatch_y
    lines = [
        "// Generated by nodewerkc NW-03; fused ShaderExpr compute entry point.",
        "// No source paths, editor layout, reflection, or debug metadata are emitted.",
        "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
        "RWByteAddressBuffer nw_output : register(u0);",
        "",
        f"[numthreads({group_x}, {group_y}, {group_z})]",
        f"void {entry_point}(uint3 nw_dispatch_id : SV_DispatchThreadID) {{",
        f"    float2 uv = (float2(nw_dispatch_id.xy) + 0.5) / float2({float(width)}, {float(height)});",
        "    float g_time = g_params[0].x;",
        f"    float4 nw_value = {hlsl_expression};",
        "    uint nw_index = nw_dispatch_id.x + nw_dispatch_id.y * "
        f"{width}u + nw_dispatch_id.z * {width * height}u;",
        "    nw_output.Store4(nw_index * 16u, asuint(nw_value));",
        "}",
        "",
    ]
    source_map = []
    for node_id in node_ids:
        source_map.append(
            {
                "generated_line": 10,
                "generated_column": 24,
                "node_id": node_id,
                "expression_id": "nw_value",
            }
        )
    return "\n".join(lines), source_map


def generated_shader_to_dict(shader: GeneratedShader) -> dict[str, Any]:
    return {
        "specialization_id": shader.specialization_id,
        "specialization_key": shader.specialization_key,
        "entry_point": shader.entry_point,
        "target": shader.target,
        "family": shader.family,
        "hlsl_sha256": shader.hlsl_sha256,
        "hlsl": shader.hlsl,
        "source_map": list(shader.source_map),
        "root_expression": shader.root_expression,
        "root_node_ids": list(shader.root_node_ids),
        "thread_group": list(shader.thread_group),
    }


# ---------------------------------------------------------------------------
# Offline DXC compilation and shader cache
# ---------------------------------------------------------------------------


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stores: int = 0
    invalidations: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "invalidations": self.invalidations,
        }


class ShaderCache:
    """Content-addressed cache for complete DXIL blobs and their metadata."""

    def __init__(self, directory: str | os.PathLike[str] | None):
        self.directory = Path(directory) if directory is not None else None
        self.stats = CacheStats()
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(
        *,
        shader: GeneratedShader,
        semantic_digest: str,
        profile_id: str,
        requirements_digest: str,
        compiler_settings: dict[str, str],
        dxc_identity: str,
        dxc_version: str,
        optimization: str,
        bindings_identity: str,
    ) -> str:
        return sha256_hex(
            nwp_package.canonical_json_bytes(
                {
                    "semantic_digest": semantic_digest,
                    "profile_id": profile_id,
                    "requirements_digest": requirements_digest,
                    "compiler_settings": compiler_settings,
                    "dxc_identity": dxc_identity,
                    "dxc_version": dxc_version,
                    "optimization": optimization,
                    "bindings_identity": bindings_identity,
                    "entry_point": shader.entry_point,
                    "target": shader.target,
                    "family": shader.family,
                    "thread_group": list(shader.thread_group),
                    "hlsl_sha256": shader.hlsl_sha256,
                }
            )
        )

    def lookup(self, key: str, expected_meta: dict[str, Any]) -> bytes | None:
        if self.directory is None:
            self.stats.misses += 1
            return None
        blob_path = self.directory / f"{key}.dxil"
        meta_path = self.directory / f"{key}.json"
        if not blob_path.is_file() or not meta_path.is_file():
            self.stats.misses += 1
            return None
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.stats.invalidations += 1
            self.stats.misses += 1
            return None
        if any(metadata.get(name) != value for name, value in expected_meta.items()):
            self.stats.invalidations += 1
            self.stats.misses += 1
            return None
        blob = blob_path.read_bytes()
        if sha256_hex(blob) != metadata.get("blob_sha256"):
            self.stats.invalidations += 1
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return blob

    def store(self, key: str, blob: bytes, metadata: dict[str, Any]) -> None:
        if self.directory is None:
            return
        metadata = dict(metadata)
        metadata["blob_sha256"] = sha256_hex(blob)
        blob_path = self.directory / f"{key}.dxil"
        meta_path = self.directory / f"{key}.json"
        _atomic_write(blob_path, blob)
        _atomic_write(meta_path, json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
        self.stats.stores += 1

    def invalidate(self, key: str) -> None:
        if self.directory is None:
            return
        for suffix in (".dxil", ".json"):
            try:
                (self.directory / f"{key}{suffix}").unlink()
                self.stats.invalidations += 1
            except FileNotFoundError:
                pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=".nw03-", suffix=".tmp", dir=str(path.parent))
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


def resolve_dxc(explicit: str | None = None) -> str | None:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        return None
    return find_dxc_executable() or find_windows_kits_dxc_executable()


def _run_dxc(
    dxc_bin: str,
    hlsl_path: Path,
    output_path: Path,
    *,
    target: str,
    entry_point: str,
    optimization: str,
    working_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    # DXC can retain the source filename in the DXIL module even after
    # -Qstrip_debug.  Compile with a stable relative source/output name from a
    # private working directory so two clean export directories do not produce
    # path-dependent shader blobs.
    source_argument = (
        hlsl_path.name if working_directory is not None else str(hlsl_path)
    )
    output_argument = (
        output_path.name if working_directory is not None else str(output_path)
    )
    command = [
        *_dxc_command_prefix(dxc_bin),
        "-T",
        target,
        "-E",
        entry_point,
        "-HV",
        "2021",
        f"-{optimization}",
        "-Qstrip_reflect",
        "-Qstrip_debug",
        "-Fo",
        output_argument,
        source_argument,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(working_directory) if working_directory is not None else None,
    )


def compile_generated_shader(
    shader: GeneratedShader,
    output_path: Path,
    *,
    dxc_executable: str,
    optimization: str = "O2",
    pinned_dxc_version: str = PINNED_DXC_VERSION,
    allow_unpinned_dxc: bool = False,
    allow_test_payloads: bool = False,
) -> dict[str, Any]:
    measured_version = query_dxc_version(dxc_executable)
    if not allow_test_payloads and str(dxc_executable).lower().endswith(".py"):
        raise CompilerError(
            "test/mock DXC payloads are not accepted for release compilation",
            [
                package_diagnostic(
                    "toolchain_test_mock_forbidden",
                    "release verification requires a real pinned DXC executable",
                )
            ],
        )
    if not allow_unpinned_dxc and pinned_dxc_version not in measured_version:
        raise CompilerError(
            f"DXC version '{measured_version}' does not match pinned '{pinned_dxc_version}'"
        )
    hlsl_path = output_path.with_suffix(".hlsl")
    hlsl_path.parent.mkdir(parents=True, exist_ok=True)
    hlsl_path.write_text(shader.hlsl, encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _run_dxc(
        dxc_executable,
        hlsl_path,
        output_path,
        target=shader.target,
        entry_point=shader.entry_point,
        optimization=optimization,
        working_directory=hlsl_path.parent,
    )
    source = shader.specialization_id or (
        shader.source_map[0]["node_id"] if shader.source_map else ""
    )
    if proc.returncode != 0 or not output_path.is_file():
        raise CompilerError(
            f"DXC failed for specialization '{shader.specialization_id}' "
            f"(exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}",
            [
                package_diagnostic(
                    "shader_compilation_failed",
                    proc.stderr.strip() or proc.stdout.strip() or "DXC returned nonzero",
                    node_id=source,
                    expression_id=shader.specialization_id,
                )
            ],
        )
    blob = output_path.read_bytes()
    if validate_dxil_container is None:
        raise CompilerError("offline DXIL structural validator is unavailable")
    try:
        validation = validate_dxil_container(
            blob,
            allow_test_container=allow_test_payloads,
            reject_debug_reflection=not allow_test_payloads,
        )
    except Exception as exc:
        raise CompilerError(
            f"compiled shader '{shader.specialization_id}' failed DXIL validation: {exc}",
            [
                package_diagnostic(
                    "shader_payload_invalid",
                    str(exc),
                    node_id=source,
                    expression_id=shader.specialization_id,
                )
            ],
        ) from exc
    if validation.get("is_test_container") and not allow_test_payloads:
        raise CompilerError(
            f"compiled shader '{shader.specialization_id}' is a synthetic test payload",
            [
                package_diagnostic(
                    "shader_payload_synthetic_forbidden",
                    "release packages must contain real DXC-compiled DXIL",
                    node_id=source,
                    expression_id=shader.specialization_id,
                )
            ],
        )
    if validation.get("shader_model") != shader.target:
        raise CompilerError(
            f"compiled shader target '{validation.get('shader_model')}' does not match "
            f"'{shader.target}'"
        )
    return {
        "blob": blob,
        "validation": validation,
        "measured_dxc_version": measured_version,
        "dxc_executable": dxc_executable,
        "hlsl_sha256": shader.hlsl_sha256,
        "is_test_container": bool(validation.get("is_test_container")),
        "dxc_flags": [
            "-T",
            shader.target,
            "-E",
            shader.entry_point,
            "-HV",
            "2021",
            f"-{optimization}",
            "-Qstrip_reflect",
            "-Qstrip_debug",
        ],
    }


# ---------------------------------------------------------------------------
# Package assembly / build report
# ---------------------------------------------------------------------------


def _compiler_fingerprint(
    document: GraphDocument,
    generated: Sequence[GeneratedShader],
    *,
    semantic_digest: str,
    dxc_identity: str,
    optimization: str,
) -> str:
    return "sha256:v1:" + sha256_hex(
        nwp_package.canonical_json_bytes(
            {
                "graph_semantic_digest": semantic_digest,
                "compiler_settings": document.settings,
                "selected_outputs": {
                    node.node_id: list(node.selected_outputs)
                    for node in sorted(
                        document.nodes.values(), key=lambda item: item.node_id
                    )
                    if node.selected_outputs
                },
                "generated": [shader.specialization_key for shader in generated],
                "dxc_identity": dxc_identity,
                "optimization": optimization,
                "bindings": nwp_package.PASS_FAMILY_BINDINGS,
            }
        )
    )


def _build_package(
    document: GraphDocument,
    generated: Sequence[GeneratedShader],
    compiled: dict[str, dict[str, Any]],
    *,
    semantic_digest: str,
    dxc_identity: str,
    dxc_version: str,
    optimization: str,
    release: bool = True,
) -> NwpPackage:
    bindings = (
        RootBinding("params", "cbv", 0, 0, 0),
        RootBinding("output", "uav", 0, 0, 1),
    )
    root_blob = nwp_package.make_root_signature_blob(PASS_FAMILY, bindings)
    root_signature = RootSignatureRecord(
        root_signature_id=1,
        family=PASS_FAMILY,
        convention=ROOT_SIGNATURE_CONVENTION,
        dword_cost=5,
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
            pass_family=PASS_FAMILY,
            binding_slot=0,
            binding_space=0,
            size_bytes=PARAMETER_BUFFER_BYTES,
        ),
        ResourceDescriptor(
            resource_id=2,
            name="output",
            kind="buffer",
            format="DXGI_FORMAT_R32_UINT",
            usage="uav",
            pass_family=PASS_FAMILY,
            binding_slot=0,
            binding_space=0,
            size_bytes=OUTPUT_BUFFER_BYTES,
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
            timeline_start_frame=0,
            timeline_end_frame=0,
            timeline_step=0,
            timeline_interpolation="none",
        )
    ]
    shaders: list[ShaderRecord] = []
    for index, shader in enumerate(sorted(generated, key=lambda item: item.specialization_id), start=1):
        shader_compiled = compiled[shader.specialization_key]
        shaders.append(
            ShaderRecord(
                shader_id=index,
                entry_point=shader.entry_point,
                target=shader.target,
                blob=shader_compiled["blob"],
                root_signature_id=1,
                family=shader.family,
                thread_group_x=shader.thread_group[0],
                thread_group_y=shader.thread_group[1],
                thread_group_z=shader.thread_group[2],
                flags=0,
            )
        )

    initialization = [
        Instruction("init.create_resource", (InstructionOperand("resource_ref", 1),)),
        Instruction("init.create_resource", (InstructionOperand("resource_ref", 2),)),
        Instruction(
            "init.create_root_signature",
            (InstructionOperand("root_signature_ref", 1),),
        ),
        Instruction(
            "init.upload_resource",
            (InstructionOperand("resource_ref", 1),),
        ),
    ]
    frame = [
        Instruction(
            "frame.set_root_signature",
            (InstructionOperand("root_signature_ref", 1),),
        ),
        Instruction(
            "frame.set_root_constants",
            (
                InstructionOperand("u32", 0),
                InstructionOperand("resource_ref", 1),
                InstructionOperand("u32", 4),
            ),
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
                    InstructionOperand("u32", DISPATCH_GROUPS[0]),
                    InstructionOperand("u32", DISPATCH_GROUPS[1]),
                    InstructionOperand("u32", DISPATCH_GROUPS[2]),
                    InstructionOperand("shader_ref", shader.shader_id),
                ),
            )
        )
    derived = nwp_package.derive_capability_requirements(
        resources, initialization, frame, shaders, [root_signature]
    )
    capability = CapabilityRecord(
        **derived,
        emitted_shader_targets=tuple(sorted({shader.target for shader in generated})),
        required_init_instructions=tuple(sorted({item.opcode for item in initialization})),
        required_frame_instructions=tuple(sorted({item.opcode for item in frame})),
        toolchain_dxc_version=dxc_version,
        toolchain_identity=dxc_identity,
        compiler_fingerprint=_compiler_fingerprint(
            document,
            generated,
            semantic_digest=semantic_digest,
            dxc_identity=dxc_identity,
            optimization=optimization,
        ),
    )
    capability = replace(
        capability,
        profile_fingerprint=capability.computed_profile_fingerprint(),
    )
    return NwpPackage(
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame,
        parameters=parameters,
        shaders=shaders,
        root_signatures=[root_signature],
        flags=1 if release else 0,
    )


def _build_sidecar(
    document: GraphDocument,
    generated: Sequence[GeneratedShader],
    compiled: dict[str, dict[str, Any]],
    *,
    semantic_digest: str,
    semantic_digest_test_identity: bool,
    dxc_identity: str,
    dxc_version: str,
    optimization: str,
) -> bytes:
    payload = {
        "format": "nw03-development-sidecar",
        "version": 1,
        "profile": {
            "id": document.profile_id,
            "requirements_digest": document.requirements_digest,
        },
        "compiler_fingerprint": _compiler_fingerprint(
            document,
            generated,
            semantic_digest=semantic_digest,
            dxc_identity=dxc_identity,
            optimization=optimization,
        ),
        "toolchain": {
            "dxc_identity": dxc_identity,
            "dxc_version": dxc_version,
            "optimization": optimization,
            "semantic_digest_test_identity": semantic_digest_test_identity,
        },
        "source_graph": {
            "name": document.source_name,
            "sha256": sha256_hex(document.source_text.encode("utf-8")),
            "text": document.source_text,
        },
        "source_maps": [
            {
                "specialization_id": shader.specialization_id,
                "specialization_key": shader.specialization_key,
                "hlsl_sha256": shader.hlsl_sha256,
                "hlsl": shader.hlsl,
                "source_map": list(shader.source_map),
            }
            for shader in sorted(generated, key=lambda item: item.specialization_id)
        ],
        "reflection": {
            "note": "Development-only explicit binding reflection; never linked into the player.",
            "pass_family": PASS_FAMILY,
            "bindings": {
                "params": {"visibility": "compute", "type": "cbv", "slot": 0, "space": 0},
                "output": {"visibility": "compute", "type": "uav", "slot": 0, "space": 0},
            },
            "specializations": [
                {
                    "specialization_id": shader.specialization_id,
                    "target": shader.target,
                    "entry_point": shader.entry_point,
                    "blob_sha256": sha256_hex(compiled[shader.specialization_key]["blob"]),
                    "blob_size": len(compiled[shader.specialization_key]["blob"]),
                }
                for shader in sorted(generated, key=lambda item: item.specialization_id)
            ],
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def compile_package(
    graph_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    document: GraphDocument | None = None,
    semantic_digest_override: str | None = None,
    semantic_digest_test_identity: bool | None = None,
    report_path: str | os.PathLike[str] | None = None,
    dev_sidecar_path: str | os.PathLike[str] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    dxc_executable: str | None = None,
    optimization: str = "O2",
    allow_unpinned_dxc: bool = False,
    allow_test_payloads: bool = False,
    shader_thread_group: tuple[int, int, int] | None = None,
    dispatch_groups: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    """Compile a graph to a validated `.nwp` package and publish atomically."""
    output_path = Path(output_path)
    if document is None:
        document = load_graph(graph_path)
    generated, diagnostics = lower_to_fused_hlsl(
        document,
        thread_group=shader_thread_group or THREAD_GROUP,
        dispatch_groups=dispatch_groups or DISPATCH_GROUPS,
    )
    if diagnostics:
        raise CompilerError("failed to lower graph", diagnostics)
    if not generated:
        raise CompilerError("graph produced no shader specializations")
    if semantic_digest_override is not None:
        semantic_digest = semantic_digest_override
        test_semantic_digest = (
            bool(semantic_digest_test_identity)
            if semantic_digest_test_identity is not None
            else False
        )
    else:
        semantic_identity = document.semantic_identity()
        semantic_digest = semantic_identity.digest
        test_semantic_digest = semantic_identity.is_test

    dxc_bin = resolve_dxc(dxc_executable)
    if not dxc_bin:
        raise CompilerError(
            "offline DXC compiler (dxc / dxc.exe) was not found; "
            "runtime shader compilation is forbidden",
            [
                package_diagnostic(
                    "toolchain_unavailable",
                    "offline DXC compiler is unavailable",
                )
            ],
        )
    dxc_version = query_dxc_version(dxc_bin)
    pinned_version = PINNED_DXC_VERSION
    try:
        pinned_version = load_pinned_dxc_version(REPO_ROOT)
    except Exception:
        pass
    if not allow_unpinned_dxc and pinned_version not in dxc_version:
        raise CompilerError(
            f"DXC version '{dxc_version}' does not match pinned '{pinned_version}'",
            [
                package_diagnostic(
                    "toolchain_version_mismatch",
                    f"DXC version '{dxc_version}' does not match pinned '{pinned_version}'",
                )
            ],
        )
    release = (pinned_version in dxc_version) and not test_semantic_digest
    dxc_identity = f"dxc:{dxc_version}"
    cache = ShaderCache(cache_dir)
    bindings_identity = sha256_hex(
        nwp_package.canonical_json_bytes(
            {
                "pass_family_bindings": nwp_package.PASS_FAMILY_BINDINGS,
                "convention": ROOT_SIGNATURE_CONVENTION,
            }
        )
    )
    compiled: dict[str, dict[str, Any]] = {}
    # Publish targets may live in a directory that does not exist yet.  Create
    # it up front so the staging directory never falls back to the (possibly
    # read-only) process working directory.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".nw03-compile-", dir=str(output_path.parent))
    )
    try:
        for index, shader in enumerate(sorted(generated, key=lambda item: item.specialization_id)):
            cache_key = ShaderCache.make_key(
                shader=shader,
                semantic_digest=semantic_digest,
                profile_id=document.profile_id,
                requirements_digest=document.requirements_digest,
                compiler_settings=document.settings,
                dxc_identity=dxc_identity,
                dxc_version=dxc_version,
                optimization=optimization,
                bindings_identity=bindings_identity,
            )
            meta = {
                "key": cache_key,
                "semantic_digest": semantic_digest,
                "profile_id": document.profile_id,
                "requirements_digest": document.requirements_digest,
                "compiler_settings": document.settings,
                "dxc_identity": dxc_identity,
                "dxc_version": dxc_version,
                "optimization": optimization,
                "bindings_identity": bindings_identity,
                "hlsl_sha256": shader.hlsl_sha256,
                "entry_point": shader.entry_point,
                "target": shader.target,
                "family": shader.family,
                "thread_group": list(shader.thread_group),
            }
            cached = cache.lookup(cache_key, meta)
            if cached is not None:
                try:
                    cached_validation = (
                        validate_dxil_container(
                            cached,
                            allow_test_container=allow_test_payloads,
                            reject_debug_reflection=not allow_test_payloads,
                        )
                        if validate_dxil_container is not None
                        else {}
                    )
                except Exception:
                    cache.invalidate(cache_key)
                    cached = None
                    cached_validation = {}
                if (
                    cached_validation.get("is_test_container")
                    and not allow_test_payloads
                ):
                    cache.invalidate(cache_key)
                    cached = None
            if cached is not None:
                compiled[shader.specialization_key] = {
                    "blob": cached,
                    "validation": cached_validation,
                    "measured_dxc_version": dxc_version,
                    "dxc_executable": dxc_bin,
                    "hlsl_sha256": shader.hlsl_sha256,
                    "is_test_container": bool(
                        cached_validation.get("is_test_container")
                    ),
                    "cache_hit": True,
                }
                continue
            result = compile_generated_shader(
                shader,
                staging_root / f"shader_{index}{'.dxil'}",
                dxc_executable=dxc_bin,
                optimization=optimization,
                pinned_dxc_version=pinned_version,
                allow_unpinned_dxc=allow_unpinned_dxc,
                allow_test_payloads=allow_test_payloads,
            )
            result["cache_hit"] = False
            cache.store(cache_key, result["blob"], meta)
            compiled[shader.specialization_key] = result

        package = _build_package(
            document,
            generated,
            compiled,
            semantic_digest=semantic_digest,
            dxc_identity=dxc_identity,
            dxc_version=dxc_version,
            optimization=optimization,
            release=release,
        )
        package_bytes = nwp_package.encode_package(package)
        valid, package_diagnostics = nwp_package.validate_package_bytes(package_bytes)
        if not valid:
            raise PackageValidationError(package_diagnostics)
        if release:
            leakage = nwp_package.check_release_leakage(package_bytes)
            if leakage:
                raise CompilerError(
                    "release package leakage check failed: " + "; ".join(leakage)
                )
        dev_sidecar = _build_sidecar(
            document,
            generated,
            compiled,
            semantic_digest=semantic_digest,
            semantic_digest_test_identity=test_semantic_digest,
            dxc_identity=dxc_identity,
            dxc_version=dxc_version,
            optimization=optimization,
        )
        report = _build_report(
            document,
            generated,
            compiled,
            package_bytes,
            cache.stats,
            semantic_digest=semantic_digest,
            semantic_digest_test_identity=test_semantic_digest,
            dxc_identity=dxc_identity,
            dxc_version=dxc_version,
            optimization=optimization,
            release=release,
            source_path=str(graph_path),
        )
        # Publish only after every validation succeeded.
        nwp_package.write_package_atomic(package, output_path)
        if dev_sidecar_path is not None:
            _atomic_write(Path(dev_sidecar_path), dev_sidecar)
        if report_path is not None:
            _atomic_write(
                Path(report_path),
                json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
            )
        return report
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _build_report(
    document: GraphDocument,
    generated: Sequence[GeneratedShader],
    compiled: dict[str, dict[str, Any]],
    package_bytes: bytes,
    cache_stats: CacheStats,
    *,
    semantic_digest: str,
    semantic_digest_test_identity: bool,
    dxc_identity: str,
    dxc_version: str,
    optimization: str,
    release: bool,
    source_path: str = "",
) -> dict[str, Any]:
    shader_rows = []
    for shader in sorted(generated, key=lambda item: item.specialization_id):
        result = compiled[shader.specialization_key]
        shader_rows.append(
            {
                "specialization_id": shader.specialization_id,
                "specialization_key": shader.specialization_key,
                "entry_point": shader.entry_point,
                "target": shader.target,
                "family": shader.family,
                "hlsl_sha256": shader.hlsl_sha256,
                "blob_sha256": sha256_hex(result["blob"]),
                "blob_size": len(result["blob"]),
                "cache_hit": bool(result.get("cache_hit")),
                "root_node_ids": list(shader.root_node_ids),
            }
        )
    unique_blobs = {row["blob_sha256"]: row["blob_size"] for row in shader_rows}
    roots = _reachable_shader_roots(document)
    emitted_roots = {shader.specialization_id for shader in generated}
    reachable_nodes: set[str] = set()
    for root in roots:
        reachable_nodes.update(_shader_dependencies(document, root))
    return {
        "format": "nw03-build-report",
        "version": 1,
        "source_graph": {
            "path": source_path,
            "sha256": sha256_hex(document.source_text.encode("utf-8")),
            "semantic_digest": semantic_digest,
        },
        "profile": {
            "id": document.profile_id,
            "requirements_digest": document.requirements_digest,
            "profile_fingerprint": _build_package(
                document,
                generated,
                compiled,
                semantic_digest=semantic_digest,
                dxc_identity=dxc_identity,
                dxc_version=dxc_version,
                optimization=optimization,
                release=release,
            ).capability.profile_fingerprint,
        },
        "compiler": {
            "fingerprint": _compiler_fingerprint(
                document,
                generated,
                semantic_digest=semantic_digest,
                dxc_identity=dxc_identity,
                optimization=optimization,
            ),
            "settings": document.settings,
        },
        "toolchain": {
            "identity": dxc_identity,
            "dxc_version": dxc_version,
            "optimization": optimization,
            "pinned": release,
            "semantic_digest_test_identity": semantic_digest_test_identity,
            "test_payload": any(
                bool(item.get("is_test_container"))
                for item in compiled.values()
            ),
            "runtime_dxc_dependency": False,
            "runtime_slang_dependency": False,
        },
        "fixture": {
            "name": "math_noise",
            "specializations": len(shader_rows),
            "shader_specialization_count": len(shader_rows),
            "reachable_root_count": len(roots),
            "deduplicated_root_count": len(
                [root for root in roots if root.node_id not in emitted_roots]
            ),
            "deduplicated_roots": sorted(
                root.node_id for root in roots if root.node_id not in emitted_roots
            ),
            "unreachable_shader_nodes": sorted(
                node.node_id
                for node in document.nodes.values()
                if node.operator_id in SHADER_OPERATORS
                and node.node_id not in reachable_nodes
            ),
            "unique_shader_blob_count": len(unique_blobs),
            "specialization_bytes": sum(row["blob_size"] for row in shader_rows),
            "unique_shader_bytes": sum(unique_blobs.values()),
            "shaders": shader_rows,
        },
        "shader_cache": cache_stats.to_dict(),
        "package": {
            "size_bytes": len(package_bytes),
            "sha256": sha256_hex(package_bytes),
            "release": release,
            "contains_authoring_metadata": False,
        },
        "release": {
            "authoring_metadata_in_package": False,
            "source_maps_in_package": False,
            "reflection_tables_in_package": False,
            "debug_data_in_package": False,
        },
    }


def build_report_from_graph(
    graph_path: str | os.PathLike[str],
    *,
    optimization: str = "O2",
) -> dict[str, Any]:
    """Non-compiling lowering report used by contract/CI checks."""
    document = load_graph(graph_path)
    generated, diagnostics = lower_to_fused_hlsl(document)
    if diagnostics:
        raise CompilerError("failed to lower graph", diagnostics)
    roots = _reachable_shader_roots(document)
    reachable_nodes: set[str] = set()
    for root in roots:
        reachable_nodes.update(_shader_dependencies(document, root))
    emitted_roots = {shader.specialization_id for shader in generated}
    semantic_identity = document.semantic_identity()
    return {
        "format": "nw03-lowering-report",
        "version": 1,
        "source_graph": {
            "path": str(graph_path),
            "sha256": sha256_hex(document.source_text.encode("utf-8")),
            "semantic_digest": semantic_identity.digest,
        },
        "profile": {
            "id": document.profile_id,
            "requirements_digest": document.requirements_digest,
        },
        "specialization_count": len(generated),
        "reachable_root_count": len(roots),
        "deduplicated_root_count": len(
            [root for root in roots if root.node_id not in emitted_roots]
        ),
        "specializations": [generated_shader_to_dict(item) for item in generated],
        "reachable_nodes": sorted(reachable_nodes),
        "deduplicated_roots": sorted(
            root.node_id for root in roots if root.node_id not in emitted_roots
        ),
        "non_reachable_shader_nodes": sorted(
            node.node_id
            for node in document.nodes.values()
            if node.operator_id in SHADER_OPERATORS
            and node.node_id not in reachable_nodes
        ),
        "optimization": optimization,
    }


def default_fixture_shader() -> GeneratedShader:
    """Return the documented math/noise shader without requiring a graph file."""
    document = GraphDocument(
        schema_version=1,
        profile_id=nwp_package.COMMON_PROFILE_ID,
        requirements_digest=nwp_package.COMMON_REQUIREMENTS_DIGEST,
        settings=_normalise_settings({}),
        nodes={},
        source_name="<generated>",
        source_text="",
    )
    hlsl, source_map = _emit_compute_hlsl(
        nw03_oracle.lower_expression_text_to_hlsl(nw03_oracle.MATH_NOISE_EXPRESSION),
        ("nw03.math_noise",),
        thread_group=THREAD_GROUP,
        dispatch_groups=DISPATCH_GROUPS,
        entry_point=ENTRY_POINT,
    )
    return GeneratedShader(
        specialization_id="nw03.math_noise",
        entry_point=ENTRY_POINT,
        target=SHADER_TARGET,
        family=PASS_FAMILY,
        hlsl=hlsl,
        source_map=tuple(source_map),
        root_expression=nw03_oracle.MATH_NOISE_EXPRESSION,
        root_node_ids=("nw03.math_noise",),
    )
