from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw06_procedural as procedural  # noqa: E402


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def test_noise2d_is_seeded_bounded_and_deterministic() -> None:
    first = procedural.generate_noise2d(16, 12, seed=1234)
    second = procedural.generate_noise2d(16, 12, seed=1234)
    other = procedural.generate_noise2d(16, 12, seed=1235)
    assert first == second
    assert first != other
    assert len(first) == 16 * 12
    assert all(0.0 <= value <= 1.0 for value in first)
    assert procedural.hash_coord_u32(3, 7, 99) == procedural.hash_coord_u32(
        3, 7, 99
    )
    assert procedural.hash_coord_u32(3, 7, 99) != procedural.hash_coord_u32(
        3, 7, 100
    )


def test_noise2d_high_precision_reference_matches_float32_tolerance() -> None:
    for y in range(8):
        for x in range(8):
            u = (x + 0.5) / 8.0
            v = (y + 0.5) / 8.0
            reference = procedural.sample_noise2d(u, v, seed=77)
            float32 = struct.unpack("<f", struct.pack("<f", reference))[0]
            assert abs(float32 - reference) <= 1e-5 + 1e-4 * abs(reference)
    comparison = procedural.validate_noise2d_reference_tolerance(
        [((x + 0.5) / 32.0, (y + 0.5) / 32.0) for y in range(32) for x in range(32)]
    )
    assert comparison["within_tolerance"]
    assert comparison["maximum_absolute_error"] <= 1e-5


def test_torus_has_closed_topology_bounds_and_outward_normals() -> None:
    mesh = procedural.generate_torus_mesh()
    mesh.validate()
    assert mesh.vertex_count == 64 * 32
    assert mesh.index_count == 64 * 32 * 6
    assert min(mesh.indices) == 0
    assert max(mesh.indices) < mesh.vertex_count
    a, b, c = (mesh.vertices[index] for index in mesh.indices[:3])
    normal = _cross(_sub(b.position, a.position), _sub(c.position, a.position))
    assert sum(x * y for x, y in zip(normal, a.normal)) > 0.0
    assert all(
        abs(math.sqrt(sum(component * component for component in vertex.normal)) - 1.0)
        <= 1e-9
        for vertex in mesh.vertices
    )
    comparison = procedural.validate_torus_reference_tolerance(mesh)
    assert comparison["within_tolerance"]
    assert comparison["maximum_position_error"] <= 1e-5
    assert comparison["maximum_normal_error"] <= 1e-5


def test_explicit_vertex_instance_material_and_indirect_encodings() -> None:
    assert procedural.VERTEX_BYTES == 32
    assert procedural.INSTANCE_BYTES == 80
    assert procedural.MATERIAL_BYTES == 32
    assert procedural.INDIRECT_ARGS_BYTES == 20
    assert [entry["offset"] for entry in procedural.VERTEX_LAYOUT] == [0, 12, 24]
    assert [entry["offset"] for entry in procedural.INSTANCE_LAYOUT] == [
        0,
        64,
        68,
        72,
        76,
    ]
    assert [entry["offset"] for entry in procedural.MATERIAL_LAYOUT] == [
        0,
        16,
        20,
        24,
        28,
    ]
    instances = procedural.generate_instances()
    material = procedural.generate_materials()[0]
    encoded = instances[0].encode()
    assert len(encoded) == 80
    assert struct.unpack_from("<f", encoded, 48)[0] == pytest.approx(
        instances[0].transform[12]
    )
    assert struct.unpack_from("<I", encoded, 64)[0] == 0
    assert len(material.encode()) == 32
    args = procedural.encode_indirect_args(12_288, 256)
    assert struct.unpack("<5I", args) == (12_288, 256, 0, 0, 0)


def test_instances_are_dense_deterministic_and_material_indices_are_valid() -> None:
    instances = procedural.generate_instances()
    again = procedural.generate_instances()
    assert instances == again
    assert len(instances) == 256
    materials = procedural.generate_materials()
    assert all(
        record.material_index < len(materials) for record in instances
    )
    assert all(record.transform[15] == 1.0 for record in instances)
    assert instances[0].seed != instances[1].seed


def test_resource_estimate_is_parameter_only_and_budgeted() -> None:
    estimate = procedural.estimate_resources()
    assert estimate["texture"]["bytes"] == 256 * 256 * 4
    assert estimate["vertices"]["bytes"] == 2048 * 32
    assert estimate["indices"]["bytes"] == 12_288 * 4
    assert estimate["instances"]["bytes"] == 256 * 80
    assert estimate["materials"]["bytes"] == 32
    assert estimate["bloom"]["width"] == 128
    assert estimate["bloom"]["height"] == 128
    assert estimate["baked_asset_bytes"] == 0
    procedural.validate_resource_budget(
        estimate, byte_budget=16 * 1024 * 1024, memory_budget=16 * 1024 * 1024
    )
    with pytest.raises(procedural.ProceduralError):
        procedural.validate_resource_budget(
            estimate, byte_budget=1024, memory_budget=1024
        )


def test_invalid_dimensions_topology_ranges_and_indices_fail() -> None:
    with pytest.raises(procedural.ProceduralError):
        procedural.Noise2DParameters(width=0).validate()
    with pytest.raises(procedural.ProceduralError):
        procedural.TorusParameters(minor_radius=1.0).validate()
    with pytest.raises(procedural.ProceduralError):
        procedural.InstanceParameters(scale_min=2.0, scale_max=1.0).validate()
    mesh = procedural.MeshData(
        vertices=procedural.torus_vertices(4, 4)[:3],
        indices=(0, 1, 99),
    )
    with pytest.raises(procedural.ProceduralError):
        procedural.validate_mesh(mesh)


def test_torus_reference_is_independent_and_detects_broken_generators() -> None:
    mesh = procedural.generate_torus_mesh()
    comparison = procedural.validate_torus_reference_tolerance(mesh)
    assert comparison["within_tolerance"]
    assert comparison["maximum_implicit_surface_residual"] <= 1e-5

    # Reflect Z without reflecting the reference normals.  A reference built by
    # calling the same generator would still report a zero error here.
    corrupted = procedural.MeshData(
        vertices=tuple(
            procedural.Vertex(
                (vertex.position[0], vertex.position[1], -vertex.position[2]),
                vertex.normal,
                vertex.uv,
            )
            for vertex in mesh.vertices
        ),
        indices=mesh.indices,
    )
    corrupted_comparison = procedural.validate_torus_reference_tolerance(
        corrupted
    )
    assert not corrupted_comparison["within_tolerance"]
    assert corrupted_comparison["maximum_position_error"] > 1e-3


def test_nonzero_material_texture_index_and_light_record_layout() -> None:
    material = procedural.generate_materials(
        procedural.MaterialParameters(texture_index=17)
    )[0]
    encoded = material.encode()
    assert len(encoded) == 32
    assert struct.unpack_from("<I", encoded, 28)[0] == 17
    assert procedural.MATERIAL_LAYOUT[-1]["name"] == "texture_index"
    assert procedural.MATERIAL_LAYOUT[-1]["offset"] == 28

    direction = (-0.4, -1.0, -0.3)
    color = (1.0, 0.75, 0.5)
    light = procedural.encode_light_record(direction, color)
    assert len(light) == procedural.LIGHT_BYTES == 32
    assert struct.unpack_from("<3f", light, 0) == pytest.approx(direction)
    assert struct.unpack_from("<3f", light, 16) == pytest.approx(color)
    assert struct.unpack_from("<f", light, 12)[0] == 0.0
    assert struct.unpack_from("<f", light, 28)[0] == 0.0
    assert [entry["offset"] for entry in procedural.LIGHT_LAYOUT] == [
        0,
        12,
        16,
        28,
    ]
