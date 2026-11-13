#!/usr/bin/env python3
"""NW-06 package lowering for the procedural noise/torus/instancing proof.

The module extends the NW-05 exporter without introducing another package
format, graph model, runtime interpreter, or player.  It consumes the same
``OptimizedGraph`` and emits the unchanged ``.nwp`` v1 sections.  Fixed shader
families implement the procedural generation, one indexed instanced raster
pass, half-resolution bloom, and tonemapping operators.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw03_compiler as nw03
import nwp_package as nwp
import nw05_optimizer as optimizer

try:  # Script-style imports from tools/.
    import nw06_procedural as procedural
except ImportError:  # pragma: no cover - package-style imports
    from tools import nw06_procedural as procedural  # type: ignore


GENERATE_FAMILY = "compute.nw06.generate"
MATERIAL_FAMILY = "compute.nw06.material"
INSTANCE_FAMILY = "compute.nw06.instances"
CAMERA_FAMILY = "compute.nw06.camera"
LIGHT_FAMILY = "compute.nw06.light"
GRAPHICS_FAMILY = "graphics.nw06.instanced"
POST_FAMILY = "compute.nw06.post"

NW06_OPERATORS = frozenset(
    {
        "nw.texture.noise2d",
        "nw.mesh.torus",
        "nw.material.standard",
        "nw.scene.instances",
        "nw.camera.curve",
        "nw.animation.twist",
        "nw.render.instanced",
        "nw.post.bloom",
        "nw.post.tonemap",
        "nw.residency.generate_texture",
        "nw.residency.generate_mesh",
        "nw.residency.generate_instances",
    }
)

# Operator aliases accepted for authoring convenience.  They lower to exactly
# the same package semantics and do not create a second runtime path.
NW06_OPERATOR_ALIASES = {
    "nw.geometry.torus": "nw.mesh.torus",
    "nw.material.pbr": "nw.material.standard",
    "nw.scene.flat_instances": "nw.scene.instances",
    "nw.camera.path": "nw.camera.curve",
    "nw.post.tone_map": "nw.post.tonemap",
}

TICKS_PER_SECOND = 48_000
SHADER_ROOT = Path(__file__).resolve().parent.parent / "shaders" / "nw06"


@dataclass(frozen=True)
class FixedShader:
    shader_id: int
    name: str
    source: str
    entry_point: str
    target: str
    family: str
    thread_group: tuple[int, int, int] = (64, 1, 1)
    shader_type: int = 5


FIXED_SHADERS: tuple[FixedShader, ...] = (
    FixedShader(1, "nw06_generate_cs", "nw06_generate_cs.hlsl", "GenerateGeometryCS", "cs_6_0", GENERATE_FAMILY, (64, 1, 1), 5),
    FixedShader(2, "nw06_instances_cs", "nw06_instances_cs.hlsl", "GenerateInstancesCS", "cs_6_0", INSTANCE_FAMILY, (64, 1, 1), 5),
    FixedShader(3, "nw06_material_cs", "nw06_material_cs.hlsl", "MaterialCS", "cs_6_0", MATERIAL_FAMILY, (1, 1, 1), 5),
    FixedShader(4, "nw06_camera_cs", "nw06_scene_data_cs.hlsl", "CameraDataCS", "cs_6_0", CAMERA_FAMILY, (1, 1, 1), 5),
    FixedShader(5, "nw06_light_cs", "nw06_scene_data_cs.hlsl", "LightDataCS", "cs_6_0", LIGHT_FAMILY, (1, 1, 1), 5),
    FixedShader(6, "nw06_scene_vs", "nw06_scene_vs.hlsl", "SceneVS", "vs_6_0", GRAPHICS_FAMILY, (1, 1, 1), 1),
    FixedShader(7, "nw06_scene_ps", "nw06_scene_ps.hlsl", "ScenePS", "ps_6_0", GRAPHICS_FAMILY, (1, 1, 1), 0),
    FixedShader(8, "nw06_bloom_cs", "nw06_bloom_cs.hlsl", "BloomCS", "cs_6_0", POST_FAMILY, (8, 8, 1), 5),
    FixedShader(9, "nw06_tonemap_cs", "nw06_tonemap_cs.hlsl", "TonemapCS", "cs_6_0", POST_FAMILY, (8, 8, 1), 5),
)


def uses_nw06(optimized: optimizer.OptimizedGraph) -> bool:
    return any(
        node.operator_id in NW06_OPERATORS
        or node.operator_id in NW06_OPERATOR_ALIASES
        for node in optimized.nodes
    )


def canonical_operator(operator_id: str) -> str:
    return NW06_OPERATOR_ALIASES.get(operator_id, operator_id)


def _node_int(node: nw03.GraphNode, name: str, default: int, minimum: int = 0, maximum: int = 0xFFFFFFFF) -> int:
    tokens = node.params.get(name, [])
    if not tokens:
        return default
    try:
        value = int(float(tokens[0]))
    except ValueError as exc:
        raise nw03.CompilerError(f"node '{node.node_id}' parameter '{name}' is not numeric") from exc
    if not minimum <= value <= maximum:
        raise nw03.CompilerError(
            f"node '{node.node_id}' parameter '{name}' {value} is outside {minimum}..{maximum}"
        )
    return value


def _node_float(node: nw03.GraphNode, name: str, default: float) -> float:
    tokens = node.params.get(name, [])
    if not tokens:
        return default
    try:
        value = float(tokens[0])
    except ValueError as exc:
        raise nw03.CompilerError(f"node '{node.node_id}' parameter '{name}' is not numeric") from exc
    if not math.isfinite(value):
        raise nw03.CompilerError(f"node '{node.node_id}' parameter '{name}' is not finite")
    return value


def _node_vector(node: nw03.GraphNode, name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    tokens = node.params.get(name, [])
    if not tokens:
        return default
    values: list[float] = []
    for token in tokens:
        values.extend(part for part in token.replace(",", " ").split() if part)
    try:
        result = tuple(float(value) for value in values)
    except ValueError as exc:
        raise nw03.CompilerError(f"node '{node.node_id}' parameter '{name}' is not numeric") from exc
    if not result or any(not math.isfinite(value) for value in result):
        raise nw03.CompilerError(f"node '{node.node_id}' parameter '{name}' is not finite")
    return result


def _test_dxil_container(shader_type: int) -> bytes:
    """Structural test-only DXIL container matching the requested target."""
    sfi0 = b"SFI0" + struct.pack("<I", 8) + struct.pack("<Q", 0)
    psv0_info = bytearray(36)
    struct.pack_into("<III", psv0_info, 12, 64, 1, 1)
    psv0_payload = (
        struct.pack("<I", len(psv0_info))
        + bytes(psv0_info)
        + struct.pack("<II", 0, 1)
    )
    psv0 = b"PSV0" + struct.pack("<I", len(psv0_payload)) + psv0_payload
    words = [0x00000021, 0x00000002, 0x00000021, 0x0000000C] + [0] * 16
    bitcode = b"BC\xc0\xde" + struct.pack(f"<{len(words)}I", *words)
    dxil_payload = (
        struct.pack(
            "<II4sIII",
            (shader_type << 16) | (6 << 4),
            (24 + len(bitcode)) // 4,
            b"DXIL",
            0x00010100,
            16,
            len(bitcode),
        )
        + bitcode
    )
    dxil = b"DXIL" + struct.pack("<I", len(dxil_payload)) + dxil_payload
    chunks = [sfi0, psv0, dxil]
    header_size = 32 + len(chunks) * 4
    offsets: list[int] = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    joined = b"".join(chunks)
    digest = hashlib.blake2s(b"dxc-retail-sig:" + joined, digest_size=16).digest()
    return (
        b"DXBC"
        + digest
        + struct.pack("<HHII", 1, 0, cursor, len(chunks))
        + struct.pack(f"<{len(chunks)}I", *offsets)
        + joined
    )


def _compile_fixed_shaders(
    *,
    dxc_executable: str | None,
    optimization: str,
    allow_unpinned_dxc: bool,
    allow_test_payloads: bool,
    working_directory: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_dir = SHADER_ROOT
    for fixed in FIXED_SHADERS:
        source_path = source_dir / fixed.source
        if not source_path.is_file():
            raise nw03.CompilerError(
                f"NW-06 fixed shader source is missing: {source_path}",
                [
                    nwp.package_diagnostic(
                        "missing_implementation",
                        f"fixed shader '{fixed.name}' is unavailable",
                    )
                ],
            )
        hlsl = source_path.read_text(encoding="utf-8")
        hlsl_sha256 = hashlib.sha256(hlsl.encode("utf-8")).hexdigest()
        if allow_test_payloads:
            blob = _test_dxil_container(fixed.shader_type)
        else:
            if not dxc_executable:
                raise nw03.CompilerError(
                    "NW-06 release export requires the pinned offline DXC executable"
                )
            generated = nw03.GeneratedShader(
                specialization_id=fixed.name,
                entry_point=fixed.entry_point,
                target=fixed.target,
                family=fixed.family,
                hlsl=hlsl,
                source_map=(),
                root_expression="",
                root_node_ids=(),
                thread_group=fixed.thread_group,
            )
            output = working_directory / f"{fixed.name}.dxil"
            compiled = nw03.compile_generated_shader(
                generated,
                output,
                dxc_executable=dxc_executable,
                optimization=optimization,
                allow_unpinned_dxc=allow_unpinned_dxc,
                allow_test_payloads=False,
            )
            blob = compiled["blob"]
        rows.append(
            {
                "shader_id": fixed.shader_id,
                "specialization_id": fixed.name,
                "entry_point": fixed.entry_point,
                "target": fixed.target,
                "family": fixed.family,
                "thread_group": list(fixed.thread_group),
                "blob": blob,
                "blob_size": len(blob),
                "blob_sha256": hashlib.sha256(blob).hexdigest(),
                "hlsl_sha256": hlsl_sha256,
                "execution_root": fixed.name,
                "execution_order": fixed.shader_id - 1,
                "output_region": 0,
                "region_count": 1,
                "unfused": True,
            }
        )
    return rows


def _root_signature(
    root_signature_id: int,
    family: str,
    bindings: Sequence[tuple[str, str, int, int, int]],
    *,
    constant_dwords: Mapping[str, int] | None = None,
) -> nwp.RootSignatureRecord:
    constant_dwords = dict(constant_dwords or {})
    records = tuple(
        nwp.RootBinding(name, usage, slot, space, root_parameter)
        for name, usage, slot, space, root_parameter in bindings
    )
    dword_cost = sum(
        int(constant_dwords.get(binding.name, 4))
        if binding.usage == "cbv"
        else 1
        for binding in records
    )
    return nwp.RootSignatureRecord(
        root_signature_id=root_signature_id,
        family=family,
        convention=f"{family}.v1",
        dword_cost=dword_cost,
        blob=nwp.make_root_signature_blob(
            family, records, constant_dwords=constant_dwords
        ),
        bindings=records,
    )


def _params(
    start_id: int,
    resource_id: int,
    values: Sequence[tuple[str, str, tuple[float, ...]]],
) -> tuple[list[nwp.ParameterRecord], int]:
    records: list[nwp.ParameterRecord] = []
    parameter_id = start_id
    for name, type_name, components in values:
        if type_name == "uint":
            payload = struct.pack("<I", int(components[0]) & 0xFFFFFFFF)
        elif type_name == "int":
            payload = struct.pack("<i", int(components[0]))
        else:
            payload = struct.pack("<" + "f" * len(components), *components)
        records.append(
            nwp.ParameterRecord(
                parameter_id=parameter_id,
                name=f"nw06.params.{resource_id}.{name}",
                type=type_name,
                phase=(
                    "initialization"
                    if resource_id in {1, 5, 8, 10, 12}
                    else "frame"
                ),
                component_count=len(components),
                value=payload,
                timeline_start_frame=0,
                timeline_end_frame=0,
                timeline_step=0,
                timeline_interpolation="none",
            )
        )
        parameter_id += 1
    return records, parameter_id


def _parse_camera_keys(node: nw03.GraphNode) -> tuple[tuple[int, float], ...]:
    text = " ".join(node.params.get("keys", ["0:0"]))
    keys: list[tuple[int, float]] = []
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise nw03.CompilerError(f"camera curve key '{entry}' is malformed")
        tick_text, value_text = entry.split(":", 1)
        try:
            tick = int(tick_text.strip())
            value = float(value_text.strip())
        except ValueError as exc:
            raise nw03.CompilerError(f"camera curve key '{entry}' is malformed") from exc
        if tick < 0 or not math.isfinite(value):
            raise nw03.CompilerError(f"camera curve key '{entry}' is invalid")
        keys.append((tick, value))
    if not keys:
        raise nw03.CompilerError("camera curve must contain at least one key")
    if len(keys) > 4:
        raise nw03.CompilerError(
            "the common NW-06 proof profile supports at most four camera keys"
        )
    keys.sort()
    if any(right[0] <= left[0] for left, right in zip(keys, keys[1:])):
        raise nw03.CompilerError("camera curve keys must have strictly increasing ticks")
    while len(keys) < 4:
        keys.append(keys[-1])
    return tuple(keys)


def build_package(
    optimized: optimizer.OptimizedGraph,
    *,
    document: nw03.GraphDocument,
    shader_rows: Sequence[Mapping[str, Any]],
    width: int,
    height: int,
    allow_test_payloads: bool,
) -> nwp.NwpPackage:
    nodes = document.nodes
    declared_tick_rate = document.settings.get("nw06_ticks_per_second")
    if declared_tick_rate is not None:
        try:
            tick_rate = int(float(declared_tick_rate))
        except ValueError as exc:
            raise nw03.CompilerError(
                "nw06_ticks_per_second must be an integer"
            ) from exc
        if tick_rate != 48_000:
            raise nw03.CompilerError(
                "NW-06 fixes the timeline at 48,000 ticks per second"
            )
    def find_node(preferred: str, operators: set[str]):
        node = nodes.get(preferred)
        if node is not None and node.operator_id in operators:
            return node
        return next(
            (
                nodes[node_id]
                for node_id in sorted(nodes)
                if nodes[node_id].operator_id in operators
            ),
            None,
        )

    texture_node = find_node(
        "texture", {"nw.texture.noise2d", "nw.texture.procedural"}
    )
    torus_node = find_node("torus", {"nw.mesh.torus", "nw.mesh.recipe"})
    instances_node = find_node(
        "instances", {"nw.scene.instances", "nw.scene.flat"}
    )
    material_node = find_node(
        "material", {"nw.material.standard", "nw.material.describe"}
    )
    camera_node = find_node(
        "camera", {"nw.camera.curve", "nw.camera.perspective"}
    )
    twist_node = find_node("twist", {"nw.animation.twist"})
    bloom_node = find_node("bloom", {"nw.post.bloom"})
    tonemap_node = find_node("tonemap", {"nw.post.tonemap"})
    for required, value in (
        ("texture", texture_node),
        ("torus", torus_node),
        ("instances", instances_node),
        ("material", material_node),
        ("camera", camera_node),
        ("twist", twist_node),
        ("bloom", bloom_node),
        ("tonemap", tonemap_node),
    ):
        if value is None:
            raise nw03.CompilerError(f"NW-06 proof graph is missing required node '{required}'")
    if not any(
        node.operator_id in {"nw.render.instanced"}
        for node in nodes.values()
    ):
        raise nw03.CompilerError(
            "NW-06 proof graph is missing required node 'nw.render.instanced'"
        )

    noise = procedural.Noise2DParameters(
        width=_node_int(texture_node, "width", 256, 1, 8192),
        height=_node_int(texture_node, "height", 256, 1, 8192),
        seed=_node_int(texture_node, "seed", 0),
        octaves=_node_int(texture_node, "octaves", 4, 1, 8),
        scale=_node_float(texture_node, "scale", 8.0),
        lacunarity=_node_float(texture_node, "lacunarity", 2.0),
        gain=_node_float(texture_node, "gain", 0.5),
        format=texture_node.params.get("format", ["r8g8b8a8_unorm"])[0],
    )
    noise.validate()
    torus = procedural.TorusParameters(
        major_segments=_node_int(torus_node, "major_segments", 64, 3, 4096),
        minor_segments=_node_int(torus_node, "minor_segments", 32, 3, 4096),
        major_radius=_node_float(torus_node, "major_radius", 0.7),
        minor_radius=_node_float(torus_node, "minor_radius", 0.3),
    )
    torus.validate()
    instances = procedural.InstanceParameters(
        count=_node_int(instances_node, "count", 256, 1, 4096),
        seed=_node_int(instances_node, "seed", 0),
        placement_radius=_node_float(instances_node, "placement_radius", 3.0),
        scale_min=_node_float(instances_node, "scale_min", 0.1),
        scale_max=_node_float(instances_node, "scale_max", 0.2),
        twist_radians=_node_float(instances_node, "twist_radians", math.pi),
    )
    instances.validate()
    base_color = _node_vector(material_node, "base_color", (1.0, 1.0, 1.0, 1.0))
    if len(base_color) != 4:
        raise nw03.CompilerError("material base_color must have four components")
    material = procedural.MaterialParameters(
        base_color=base_color,
        roughness=_node_float(material_node, "roughness", 0.5),
        metallic=_node_float(material_node, "metallic", 0.0),
        specular=_node_float(material_node, "specular", 0.5),
        texture_index=_node_int(material_node, "texture_index", 0, 0, 0xFFFFFFFF),
    )
    material.validate()
    light_node = find_node("sun", {"nw.light.directional"})
    if light_node is None:
        raise nw03.CompilerError("NW-06 proof graph is missing required node 'sun'")
    light_direction = _node_vector(light_node, "direction", (0.0, -1.0, 0.0))
    light_color = _node_vector(light_node, "color", (1.0, 1.0, 1.0))
    if len(light_direction) != 3 or len(light_color) != 3:
        raise nw03.CompilerError(
            "directional light direction/color must have three components"
        )
    keys = _parse_camera_keys(camera_node)
    fov = _node_float(camera_node, "fov_degrees", 60.0)
    aspect = _node_float(camera_node, "aspect", 1.0)
    near = _node_float(camera_node, "near", 0.1)
    far = _node_float(camera_node, "far", 100.0)
    radius_min = _node_float(camera_node, "radius_min", 3.2)
    radius_max = _node_float(camera_node, "radius_max", 4.8)
    height_value = _node_float(camera_node, "height", 1.6)
    twist = math.radians(_node_float(twist_node, "max_twist_degrees", 180.0))
    exposure = _node_float(tonemap_node, "exposure", 1.0)
    gamma = _node_float(tonemap_node, "gamma", 2.2)
    operator = tonemap_node.params.get("operator", ["aces"])[0]
    operator_index = {"linear": 0, "reinhard": 1, "aces": 2}.get(operator, 2)
    bloom_threshold = _node_float(bloom_node, "threshold", 0.7)
    bloom_intensity = _node_float(bloom_node, "intensity", 0.35)
    bloom_radius = _node_float(bloom_node, "radius", 1.5)
    resolution_scale = _node_float(bloom_node, "resolution_scale", 0.5)
    if abs(resolution_scale - 0.5) > 1e-6:
        raise nw03.CompilerError("NW-06 bloom must resolve at exactly half resolution")

    texture_bytes = noise.width * noise.height * (
        16 if noise.format == "rgba32f" else 4
    )
    vertex_bytes = torus.vertex_count * procedural.VERTEX_BYTES
    index_bytes = torus.index_count * procedural.INDEX_BYTES
    instance_bytes = instances.count * procedural.INSTANCE_BYTES
    material_bytes = procedural.MATERIAL_BYTES
    bloom_width = width // 2
    bloom_height = height // 2
    scene_color_bytes = width * height * 16
    bloom_bytes = bloom_width * bloom_height * 16
    final_bytes = width * height * 16

    resources = [
        nwp.ResourceDescriptor(1, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", GENERATE_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(2, "texture", "texture2d", "DXGI_FORMAT_R8G8B8A8_UNORM" if noise.format == "r8g8b8a8_unorm" else "DXGI_FORMAT_R32G32B32A32_FLOAT", "uav_srv", GENERATE_FAMILY, 0, 0, width=noise.width, height=noise.height, size_bytes=texture_bytes),
        nwp.ResourceDescriptor(3, "vertices", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "uav", GENERATE_FAMILY, 1, 0, size_bytes=vertex_bytes),
        nwp.ResourceDescriptor(4, "indices", "buffer", "DXGI_FORMAT_R32_UINT", "uav", GENERATE_FAMILY, 2, 0, size_bytes=index_bytes),
        nwp.ResourceDescriptor(5, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", INSTANCE_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(6, "instances", "buffer", "DXGI_FORMAT_R32_UINT", "uav_srv", INSTANCE_FAMILY, 0, 0, size_bytes=instance_bytes),
        nwp.ResourceDescriptor(7, "args", "buffer", "DXGI_FORMAT_R32_UINT", "uav", INSTANCE_FAMILY, 1, 0, size_bytes=procedural.INDIRECT_ARGS_BYTES),
        nwp.ResourceDescriptor(8, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", MATERIAL_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(9, "materials", "buffer", "DXGI_FORMAT_R32_UINT", "uav_srv", MATERIAL_FAMILY, 0, 0, size_bytes=material_bytes),
        nwp.ResourceDescriptor(10, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", CAMERA_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(11, "camera", "buffer", "DXGI_FORMAT_R32_UINT", "uav_srv", CAMERA_FAMILY, 0, 0, size_bytes=48),
        nwp.ResourceDescriptor(12, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", LIGHT_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(13, "light", "buffer", "DXGI_FORMAT_R32_UINT", "uav_srv", LIGHT_FAMILY, 0, 0, size_bytes=32),
        nwp.ResourceDescriptor(14, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", GRAPHICS_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(15, "depth", "texture2d", "DXGI_FORMAT_D32_FLOAT", "dsv", GRAPHICS_FAMILY, 0, 0, width=width, height=height, size_bytes=width * height * 4),
        nwp.ResourceDescriptor(16, "params", "buffer", "DXGI_FORMAT_R32G32B32A32_FLOAT", "cbv", POST_FAMILY, 0, 0, size_bytes=256),
        nwp.ResourceDescriptor(17, "input", "texture2d", "DXGI_FORMAT_R32G32B32A32_FLOAT", "rtv_srv", POST_FAMILY, 0, 0, width=width, height=height, size_bytes=scene_color_bytes),
        nwp.ResourceDescriptor(18, "bloom", "texture2d", "DXGI_FORMAT_R32G32B32A32_FLOAT", "uav_srv", POST_FAMILY, 1, 0, width=bloom_width, height=bloom_height, size_bytes=bloom_bytes),
        nwp.ResourceDescriptor(19, "output", "buffer", "DXGI_FORMAT_R32_UINT", "uav", POST_FAMILY, 0, 0, size_bytes=final_bytes),
    ]

    roots = [
        _root_signature(
            1,
            GENERATE_FAMILY,
            (
                ("params", "cbv", 0, 0, 0),
                ("output", "uav", 0, 0, 1),
                ("vertices", "uav", 1, 0, 2),
                ("indices", "uav", 2, 0, 3),
            ),
            constant_dwords={"params": 11},
        ),
        _root_signature(
            2,
            INSTANCE_FAMILY,
            (
                ("params", "cbv", 0, 0, 0),
                ("output", "uav", 0, 0, 1),
                ("args", "uav", 1, 0, 2),
            ),
            constant_dwords={"params": 6},
        ),
        _root_signature(
            3,
            MATERIAL_FAMILY,
            (("params", "cbv", 0, 0, 0), ("output", "uav", 0, 0, 1)),
            constant_dwords={"params": 8},
        ),
        _root_signature(
            4,
            CAMERA_FAMILY,
            (("params", "cbv", 0, 0, 0), ("output", "uav", 0, 0, 1)),
            constant_dwords={"params": 11},
        ),
        _root_signature(
            5,
            LIGHT_FAMILY,
            (("params", "cbv", 0, 0, 0), ("output", "uav", 0, 0, 1)),
            constant_dwords={"params": 8},
        ),
        _root_signature(
            6,
            GRAPHICS_FAMILY,
            (
                ("params", "cbv", 0, 0, 0),
                ("texture", "srv", 0, 0, 1),
                ("instances", "srv", 1, 0, 2),
                ("materials", "srv", 2, 0, 3),
                ("camera", "srv", 3, 0, 4),
                ("light", "srv", 4, 0, 5),
            ),
            constant_dwords={"params": 8},
        ),
        _root_signature(
            7,
            POST_FAMILY,
            (
                ("params", "cbv", 0, 0, 0),
                ("input", "srv", 0, 0, 1),
                ("bloom", "srv", 1, 0, 2),
                ("output", "uav", 0, 0, 3),
            ),
            constant_dwords={"params": 10},
        ),
    ]

    parameters: list[nwp.ParameterRecord] = []
    next_id = 1
    gen_values = [
        ("width", "uint", (float(noise.width),)),
        ("height", "uint", (float(noise.height),)),
        ("texture_seed", "uint", (float(noise.seed),)),
        ("noise_octaves", "uint", (float(noise.octaves),)),
        ("noise_scale", "float", (noise.scale,)),
        ("noise_lacunarity", "float", (noise.lacunarity,)),
        ("noise_gain", "float", (noise.gain,)),
        ("major_segments", "uint", (float(torus.major_segments),)),
        ("minor_segments", "uint", (float(torus.minor_segments),)),
        ("major_radius", "float", (torus.major_radius,)),
        ("minor_radius", "float", (torus.minor_radius,)),
    ]
    generated, next_id = _params(next_id, 1, gen_values)
    parameters.extend(generated)
    instance_values = [
        ("count", "uint", (float(instances.count),)),
        ("seed", "uint", (float(instances.seed),)),
        ("placement_radius", "float", (instances.placement_radius,)),
        ("scale_min", "float", (instances.scale_min,)),
        ("scale_max", "float", (instances.scale_max,)),
        ("index_count", "uint", (float(torus.index_count),)),
    ]
    generated, next_id = _params(next_id, 5, instance_values)
    parameters.extend(generated)
    material_values = [
        ("base_r", "float", (material.base_color[0],)),
        ("base_g", "float", (material.base_color[1],)),
        ("base_b", "float", (material.base_color[2],)),
        ("base_a", "float", (material.base_color[3],)),
        ("roughness", "float", (material.roughness,)),
        ("metallic", "float", (material.metallic,)),
        ("specular", "float", (material.specular,)),
        ("texture_index", "uint", (float(material.texture_index),)),
    ]
    generated, next_id = _params(next_id, 8, material_values)
    parameters.extend(generated)
    camera_values = [
        ("curve_tick_0", "uint", (float(keys[0][0]),)),
        ("curve_tick_1", "uint", (float(keys[1][0]),)),
        ("curve_tick_2", "uint", (float(keys[2][0]),)),
        ("curve_tick_3", "uint", (float(keys[3][0]),)),
        ("curve_value_0", "float", (keys[0][1],)),
        ("curve_value_1", "float", (keys[1][1],)),
        ("curve_value_2", "float", (keys[2][1],)),
        ("curve_value_3", "float", (keys[3][1],)),
        ("radius_min", "float", (radius_min,)),
        ("radius_max", "float", (radius_max,)),
        ("height", "float", (height_value,)),
    ]
    generated, next_id = _params(next_id, 10, camera_values)
    parameters.extend(generated)
    light_values = [
        ("direction_x", "float", (light_direction[0],)),
        ("direction_y", "float", (light_direction[1],)),
        ("direction_z", "float", (light_direction[2],)),
        ("pad0", "float", (0.0,)),
        ("color_r", "float", (light_color[0],)),
        ("color_g", "float", (light_color[1],)),
        ("color_b", "float", (light_color[2],)),
        ("pad1", "float", (0.0,)),
    ]
    generated, next_id = _params(next_id, 12, light_values)
    parameters.extend(generated)
    graphics_values = [
        ("tick", "float", (0.0,)),
        ("fov_degrees", "float", (fov,)),
        ("aspect", "float", (aspect,)),
        ("near", "float", (near,)),
        ("far", "float", (far,)),
        ("exposure", "float", (exposure,)),
        ("bloom_intensity", "float", (bloom_intensity,)),
        ("twist_radians", "float", (twist,)),
    ]
    generated, next_id = _params(next_id, 14, graphics_values)
    parameters.extend(generated)
    tick_parameter_id = generated[0].parameter_id
    post_values = [
        ("width", "uint", (float(width),)),
        ("height", "uint", (float(height),)),
        ("half_width", "uint", (float(bloom_width),)),
        ("half_height", "uint", (float(bloom_height),)),
        ("threshold", "float", (bloom_threshold,)),
        ("intensity", "float", (bloom_intensity,)),
        ("radius", "float", (bloom_radius,)),
        ("exposure", "float", (exposure,)),
        ("gamma", "float", (gamma,)),
        ("operator", "uint", (float(operator_index),)),
    ]
    generated, next_id = _params(next_id, 16, post_values)
    parameters.extend(generated)
    for name, value in (
        ("nw06.timeline.ticks_per_second", 48_000),
        ("nw06.timeline.duration_ticks", keys[-1][0]),
        ("nw06.timeline.frame_step_numerator", 1),
        ("nw06.timeline.frame_step_denominator", 1),
        ("nw06.timeline.stateless_scene", 1),
    ):
        parameters.append(
            nwp.ParameterRecord(
                parameter_id=next_id,
                name=name,
                type="uint",
                phase="frame",
                component_count=1,
                value=struct.pack("<I", int(value)),
                timeline_start_frame=0,
                timeline_end_frame=0,
                timeline_step=0,
                timeline_interpolation="none",
            )
        )
        next_id += 1

    shader_records: list[nwp.ShaderRecord] = []
    root_by_family = {root.family: root for root in roots}
    for row in shader_rows:
        family = str(row["family"])
        shader_records.append(
            nwp.ShaderRecord(
                shader_id=int(row["shader_id"]),
                entry_point=str(row["entry_point"]),
                target=str(row["target"]),
                blob=bytes(row["blob"]),
                root_signature_id=root_by_family[family].root_signature_id,
                family=family,
                thread_group_x=int(row["thread_group"][0]),
                thread_group_y=int(row["thread_group"][1]),
                thread_group_z=int(row["thread_group"][2]),
                flags=0,
            )
        )

    initialization: list[nwp.Instruction] = []
    for resource in resources:
        initialization.append(
            nwp.Instruction("init.create_resource", (nwp.InstructionOperand("resource_ref", resource.resource_id),))
        )
    for root in roots:
        initialization.append(
            nwp.Instruction("init.create_root_signature", (nwp.InstructionOperand("root_signature_ref", root.root_signature_id),))
        )
    for shader in shader_records:
        if shader.target == "cs_6_0":
            initialization.append(
                nwp.Instruction(
                    "init.create_pipeline",
                    (nwp.InstructionOperand("shader_ref", shader.shader_id), nwp.InstructionOperand("root_signature_ref", shader.root_signature_id)),
                )
            )
    initialization.append(
        nwp.Instruction(
            "init.create_graphics_pipeline",
            (
                nwp.InstructionOperand("shader_ref", 6),
                nwp.InstructionOperand("shader_ref", 7),
                nwp.InstructionOperand("root_signature_ref", 6),
            ),
        )
    )
    # Initialization-only generators.  These dispatches never enter FRAM.
    initialization.extend(
        [
            nwp.Instruction("init.set_root_signature", (nwp.InstructionOperand("root_signature_ref", 1),)),
            nwp.Instruction("init.set_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 1), nwp.InstructionOperand("u32", 11))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 2))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 2), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 3))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 3), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 4))),
            nwp.Instruction(
                "init.dispatch",
                (
                    nwp.InstructionOperand("u32", (max(noise.width * noise.height, torus.index_count, torus.vertex_count) + 63) // 64),
                    nwp.InstructionOperand("u32", 1),
                    nwp.InstructionOperand("u32", 1),
                    nwp.InstructionOperand("shader_ref", 1),
                ),
            ),
            nwp.Instruction("init.set_root_signature", (nwp.InstructionOperand("root_signature_ref", 2),)),
            nwp.Instruction("init.set_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 5), nwp.InstructionOperand("u32", 6))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 6))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 2), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 7))),
            nwp.Instruction("init.dispatch", (nwp.InstructionOperand("u32", (instances.count + 63) // 64), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("shader_ref", 2))),
            nwp.Instruction("init.set_root_signature", (nwp.InstructionOperand("root_signature_ref", 3),)),
            nwp.Instruction("init.set_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 8), nwp.InstructionOperand("u32", 8))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 9))),
            nwp.Instruction("init.dispatch", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("shader_ref", 3))),
            nwp.Instruction("init.set_root_signature", (nwp.InstructionOperand("root_signature_ref", 4),)),
            nwp.Instruction("init.set_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 10), nwp.InstructionOperand("u32", 11))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 11))),
            nwp.Instruction("init.dispatch", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("shader_ref", 4))),
            nwp.Instruction("init.set_root_signature", (nwp.InstructionOperand("root_signature_ref", 5),)),
            nwp.Instruction("init.set_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 12), nwp.InstructionOperand("u32", 8))),
            nwp.Instruction("init.set_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 13))),
            nwp.Instruction("init.dispatch", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("shader_ref", 5))),
        ]
    )

    frame: list[nwp.Instruction] = [
        nwp.Instruction("frame.set_timeline", (nwp.InstructionOperand("u32", tick_parameter_id), nwp.InstructionOperand("u64", 0))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 2))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 6))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 9))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 11))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 13))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("resource_ref", 3))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 2), nwp.InstructionOperand("resource_ref", 4))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 13), nwp.InstructionOperand("resource_ref", 7))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 3), nwp.InstructionOperand("resource_ref", 17))),
        nwp.Instruction("frame.set_graphics_root_signature", (nwp.InstructionOperand("root_signature_ref", 6),)),
        nwp.Instruction("frame.set_graphics_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 14), nwp.InstructionOperand("u32", 8))),
        nwp.Instruction("frame.set_graphics_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 2))),
        nwp.Instruction("frame.set_graphics_descriptor_table", (nwp.InstructionOperand("u32", 2), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 6))),
        nwp.Instruction("frame.set_graphics_descriptor_table", (nwp.InstructionOperand("u32", 3), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 9))),
        nwp.Instruction("frame.set_graphics_descriptor_table", (nwp.InstructionOperand("u32", 4), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 11))),
        nwp.Instruction("frame.set_graphics_descriptor_table", (nwp.InstructionOperand("u32", 5), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 13))),
        nwp.Instruction("frame.set_graphics_pipeline", (nwp.InstructionOperand("shader_ref", 6),)),
        nwp.Instruction("frame.set_render_targets", (nwp.InstructionOperand("resource_ref", 17), nwp.InstructionOperand("resource_ref", 15))),
        nwp.Instruction("frame.execute_indirect", (nwp.InstructionOperand("resource_ref", 7), nwp.InstructionOperand("u32", 1))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 17))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 4), nwp.InstructionOperand("resource_ref", 18))),
        nwp.Instruction("frame.set_root_signature", (nwp.InstructionOperand("root_signature_ref", 7),)),
        nwp.Instruction("frame.set_root_constants", (nwp.InstructionOperand("u32", 0), nwp.InstructionOperand("resource_ref", 16), nwp.InstructionOperand("u32", 10))),
        nwp.Instruction("frame.set_descriptor_table", (nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 17))),
        nwp.Instruction("frame.set_descriptor_table", (nwp.InstructionOperand("u32", 3), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 18))),
        nwp.Instruction("frame.dispatch", (nwp.InstructionOperand("u32", (bloom_width + 7) // 8), nwp.InstructionOperand("u32", (bloom_height + 7) // 8), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("shader_ref", 8))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 14), nwp.InstructionOperand("resource_ref", 18))),
        nwp.Instruction("frame.set_descriptor_table", (nwp.InstructionOperand("u32", 2), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 18))),
        nwp.Instruction("frame.set_descriptor_table", (nwp.InstructionOperand("u32", 3), nwp.InstructionOperand("string", "cbv_srv_uav"), nwp.InstructionOperand("resource_ref", 19))),
        nwp.Instruction("frame.dispatch", (nwp.InstructionOperand("u32", (width + 7) // 8), nwp.InstructionOperand("u32", (height + 7) // 8), nwp.InstructionOperand("u32", 1), nwp.InstructionOperand("shader_ref", 9))),
        nwp.Instruction("frame.barrier", (nwp.InstructionOperand("u32", 4), nwp.InstructionOperand("resource_ref", 19))),
        nwp.Instruction("frame.present", (nwp.InstructionOperand("resource_ref", 19),)),
    ]

    derived = nwp.derive_capability_requirements(
        resources, initialization, frame, shader_records, roots
    )
    capability = nwp.CapabilityRecord(
        **derived,
        emitted_shader_targets=tuple(sorted({shader.target for shader in shader_records})),
        required_init_instructions=tuple(sorted({item.opcode for item in initialization})),
        required_frame_instructions=tuple(sorted({item.opcode for item in frame})),
        toolchain_dxc_version="1.8.2407",
        toolchain_identity="dxc:1.8.2407",
        compiler_fingerprint="sha256:v1:" + hashlib.sha256(
            nwp.canonical_json_bytes(
                {
                    "graph": optimized.ir_sha256(),
                    "shaders": [
                        {
                            "target": row["target"],
                            "family": row["family"],
                            "hlsl_sha256": row["hlsl_sha256"],
                        }
                        for row in shader_rows
                    ],
                }
            )
        ).hexdigest(),
    )
    capability = replace(
        capability,
        profile_fingerprint=capability.computed_profile_fingerprint(),
    )
    return nwp.NwpPackage(
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame,
        parameters=parameters,
        shaders=shader_records,
        root_signatures=roots,
        flags=0 if allow_test_payloads else 1,
    )


def compile_profile_package(
    optimized: optimizer.OptimizedGraph,
    document: nw03.GraphDocument,
    destination: Path,
    *,
    dxc_executable: str | None,
    optimization: str,
    allow_unpinned_dxc: bool,
    allow_test_payloads: bool,
    width: int = 256,
    height: int = 256,
) -> tuple[bytes, nwp.NwpPackage, dict[str, Any]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nw06-shaders-", dir=str(destination.parent)) as temp:
        rows = _compile_fixed_shaders(
            dxc_executable=dxc_executable,
            optimization=optimization,
            allow_unpinned_dxc=allow_unpinned_dxc,
            allow_test_payloads=allow_test_payloads,
            working_directory=Path(temp),
        )
    try:
        package = build_package(
            optimized,
            document=document,
            shader_rows=rows,
            width=width,
            height=height,
            allow_test_payloads=allow_test_payloads,
        )
    except procedural.ProceduralError as exc:
        raise nw03.CompilerError(
            "NW-06 procedural content validation failed", exc.diagnostics
        ) from exc
    next_parameter_id = max(
        (parameter.parameter_id for parameter in package.parameters), default=0
    ) + 1
    quantized_parameters: list[nwp.ParameterRecord] = []
    for decision in optimized.quantization:
        if not 1 <= len(decision.quantized) <= 4:
            continue
        type_name = {
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
                type=type_name,
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
            package,
            parameters=list(package.parameters) + quantized_parameters,
        )
    package_bytes = nwp.encode_package(package)
    valid, diagnostics = nwp.validate_package_bytes(package_bytes)
    if not valid:
        raise nw03.CompilerError(
            "NW-06 package validation failed",
            diagnostics,
        )
    report_rows = [
        {
            key: value
            for key, value in row.items()
            if key != "blob"
        }
        for row in rows
    ]
    report = {
        "format": "nw06-build-report",
        "version": 1,
        "profile": {
            "id": nwp.COMMON_PROFILE_ID,
            "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
        },
        "source_graph": {
            "path": document.source_name,
            "semantic_digest": optimized.ir_sha256(),
        },
        "fixture": {
            "name": "nw06_procedural_scene",
            "specializations": len(rows),
            "shaders": report_rows,
            "unique_shader_blob_count": len({row["blob"] for row in rows}),
            "unique_shader_bytes": sum(len(row["blob"]) for row in rows),
        },
        "package": {
            "size_bytes": len(package_bytes),
            "sha256": hashlib.sha256(package_bytes).hexdigest(),
            "release": not allow_test_payloads,
        },
        "image_oracle_sha256": "",
        "image_oracle_status": "unavailable",
        "timeline": {
            "ticks_per_second": 48_000,
            "duration_ticks": next(
                (
                    struct.unpack("<I", parameter.value)[0]
                    for parameter in package.parameters
                    if parameter.name == "nw06.timeline.duration_ticks"
                    and len(parameter.value) == 4
                ),
                0,
            ),
            "rational_frame_steps": True,
            "absolute_tick_curve_sampling": True,
            "stateless_scene": True,
            "clock_provider_audio_independent": True,
        },
        "instrumentation": {
            "initialization_time": {
                "status": "unavailable",
                "reason": "contracts-only export has no executing GPU",
            },
            "per_pass_time": {
                "status": "unavailable",
                "reason": "captured only by the PackagePlayer diagnostic profile",
                "passes": {
                    "initialization_generation": "unavailable",
                    "indexed_instanced_raster": "unavailable",
                    "half_resolution_bloom": "unavailable",
                    "tonemap": "unavailable",
                },
            },
            "shader_bytes": {
                "status": "measured",
                "total": sum(len(row["blob"]) for row in rows),
                "records": [
                    {
                        "shader_id": row["shader_id"],
                        "target": row["target"],
                        "bytes": len(row["blob"]),
                    }
                    for row in rows
                ],
            },
            "memory_costs": {
                "status": "measured",
                "resident_resource_bytes": sum(
                    resource.size_bytes for resource in package.resources
                ),
                "parameter_bytes": sum(
                    len(parameter.value) for parameter in package.parameters
                ),
            },
        },
    }
    return package_bytes, package, report


__all__ = [
    "NW06_OPERATORS",
    "NW06_OPERATOR_ALIASES",
    "GENERATE_FAMILY",
    "MATERIAL_FAMILY",
    "GRAPHICS_FAMILY",
    "POST_FAMILY",
    "FIXED_SHADERS",
    "uses_nw06",
    "canonical_operator",
    "build_package",
    "compile_profile_package",
]

# Downstream-friendly names used by verification/tooling.
compile_nw06_package = compile_profile_package
build_nw06_package = build_package
