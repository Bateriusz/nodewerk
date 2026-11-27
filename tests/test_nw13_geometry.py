from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw13_geometry as geometry  # noqa: E402
import nw03_compiler as nw03  # noqa: E402
from schema_validator import validate_instance  # noqa: E402


FIXTURE = REPO_ROOT / "fixtures" / "nw13" / "geometry_room.json"
SCHEMA = REPO_ROOT / "schemas" / "nw13-geometry-graph.v1.json"


def _node(operator: str, **parameters: object) -> dict:
    return {"id": f"{operator}.test", "operator": operator, "version": "1.0.0", "parameters": parameters}


def _document(root: dict, **overrides: object) -> dict:
    document = {
        "format": "nw13.geometry",
        "schema_version": 1,
        "budget": {
            "max_nodes": 64,
            "max_depth": 8,
            "max_repetition_instances": 256,
            "max_generated_primitives": 256,
            "max_evaluation_steps": 65536,
            "max_memory_bytes": 1048576,
        },
        "root": root,
    }
    document.update(overrides)
    return document


def test_public_schema_and_fixture_validate() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    validate_instance(fixture, schema, schema)
    operators = set(schema["$defs"]["node"]["properties"]["operator"]["enum"])
    assert {
        "nw.geom.sphere",
        "nw.geom.box",
        "nw.geom.cylinder",
        "nw.geom.capsule",
        "nw.geom.torus",
        "nw.geom.translate",
        "nw.geom.rotate",
        "nw.geom.scale",
        "nw.geom.union",
        "nw.geom.intersection",
        "nw.geom.subtract",
        "nw.geom.smooth_union",
        "nw.geom.repeat",
        "nw.geom.twist",
        "nw.geom.bend",
        "nw.geom.displace",
    } == operators
    contracts = json.loads((REPO_ROOT / "references" / "operator_catalog" / "nw13-geometry-contracts.v1.json").read_text(encoding="utf-8"))
    assert contracts == geometry.contract_document()
    assert {entry["operator_id"] for entry in contracts["operators"]} == operators


def test_primitive_reference_points_and_bounds() -> None:
    cases = [
        (_node("nw.geom.sphere", radius=2.0), [(0, 0, 0), (2, 0, 0)]),
        (_node("nw.geom.box", half_extents=[1, 2, 3]), [(0, 0, 0), (1, 0, 0)]),
        (_node("nw.geom.cylinder", radius=1.0, half_height=2.0), [(0, 0, 0), (1, 0, 0)]),
        (_node("nw.geom.capsule", radius=1.0, half_height=1.0), [(0, 0, 0), (0, 2, 0)]),
        (_node("nw.geom.torus", major_radius=2.0, minor_radius=0.5), [(2, 0, 0), (2.5, 0, 0)]),
    ]
    for node, points in cases:
        prepared = geometry.prepare(_document(node))
        assert prepared.evaluate(points[0]) < 0.0
        assert abs(prepared.evaluate(points[1])) <= 1e-6
        bounds = prepared.bounds
        # Every point on the surface must be contained.
        for point in points:
            assert bounds.contains(point, tolerance=1e-9), (node["operator"], point, bounds)


def test_transforms_rotate_scale_and_translate_the_surface() -> None:
    sphere = _node("nw.geom.sphere", radius=1.0)
    translated = {"id": "t", "operator": "nw.geom.translate", "version": "1.0.0", "parameters": {"offset": [3, 0, 0]}, "children": [sphere]}
    prepared = geometry.prepare(_document(translated))
    assert abs(prepared.evaluate((4, 0, 0))) <= 1e-6
    assert prepared.evaluate((0, 0, 0)) > 1.0
    assert prepared.bounds.contains((4, 0, 0))

    box = _node("nw.geom.box", half_extents=[2, 0.5, 0.25])
    rotated = {"id": "r", "operator": "nw.geom.rotate", "version": "1.0.0", "parameters": {"axis": "y", "degrees": 90}, "children": [box]}
    prepared = geometry.prepare(_document(rotated))
    assert abs(prepared.evaluate((0.25, 0, 2))) <= 1e-6
    assert prepared.bounds.contains((0, 0, 2))

    scaled = {"id": "s", "operator": "nw.geom.scale", "version": "1.0.0", "parameters": {"scale": [2, 1, 1]}, "children": [sphere]}
    prepared = geometry.prepare(_document(scaled))
    assert abs(prepared.evaluate((2, 0, 0))) <= 1e-6


def test_csg_identities_and_smooth_union_zero_equivalence() -> None:
    a = _node("nw.geom.sphere", radius=1.0)
    b = {"id": "b", "operator": "nw.geom.translate", "version": "1.0.0", "parameters": {"offset": [10, 0, 0]}, "children": [_node("nw.geom.sphere", radius=1.0)]}
    union = {"id": "u", "operator": "nw.geom.union", "version": "1.0.0", "children": [a, b]}
    intersect = {"id": "i", "operator": "nw.geom.intersection", "version": "1.0.0", "children": [a, a]}
    subtract = {"id": "d", "operator": "nw.geom.subtract", "version": "1.0.0", "children": [a, b]}
    smooth = {"id": "sm", "operator": "nw.geom.smooth_union", "version": "1.0.0", "parameters": {"k": 0.0}, "children": [a, b]}
    points = [(0.2, 0.1, 0.3), (0.9, 0, 0), (2, 0, 0), (0, 0, 0)]
    union_p = geometry.prepare(_document(union))
    inter_p = geometry.prepare(_document(intersect))
    sub_p = geometry.prepare(_document(subtract))
    smooth_p = geometry.prepare(_document(smooth))
    sphere_p = geometry.prepare(_document(a))
    for point in points:
        assert inter_p.evaluate(point) == pytest.approx(sphere_p.evaluate(point))
        assert sub_p.evaluate(point) == pytest.approx(sphere_p.evaluate(point))
        assert smooth_p.evaluate(point) == pytest.approx(union_p.evaluate(point))
    # Union of one sphere with a far-away disjoint sphere equals the sphere in
    # the region that contains it.
    assert union_p.evaluate((0, 0, 0)) == pytest.approx(sphere_p.evaluate((0, 0, 0)))


def test_finite_repetition_twist_bend_and_seeded_displacement() -> None:
    sphere = _node("nw.geom.sphere", radius=0.5)
    repeat = {"id": "rep", "operator": "nw.geom.repeat", "version": "1.0.0", "parameters": {"spacing": [2, 2, 2], "counts": [3, 3, 3]}, "children": [sphere]}
    prepared = geometry.prepare(_document(repeat))
    assert prepared.evaluate((0, 0, 0)) < 0.0
    for point in [(0.5, 0, 0), (1.5, 0, 0), (0.5, 2, 0), (1.5, 2, 0)]:
        assert abs(prepared.evaluate(point)) <= 1e-6
    assert prepared.stats["repetition_instances"] == 27

    twist = {"id": "tw", "operator": "nw.geom.twist", "version": "1.0.0", "parameters": {"half_height": 1.0, "max_degrees": 45}, "children": [_node("nw.geom.box", half_extents=[0.5, 1.0, 0.5])]}
    assert math.isfinite(geometry.prepare(_document(twist)).evaluate((1, 1, 0)))
    bend = {"id": "be", "operator": "nw.geom.bend", "version": "1.0.0", "parameters": {"half_width": 1.0, "max_degrees": 20}, "children": [_node("nw.geom.box", half_extents=[1.0, 0.1, 0.5])]}
    assert math.isfinite(geometry.prepare(_document(bend)).evaluate((0.8, 0.2, 0)))

    first = {"id": "d1", "operator": "nw.geom.displace", "version": "1.0.0", "parameters": {"seed": 77, "amplitude": 0.1, "frequency": 2.0}, "children": [sphere]}
    second = {"id": "d2", "operator": "nw.geom.displace", "version": "1.0.0", "parameters": {"seed": 78, "amplitude": 0.1, "frequency": 2.0}, "children": [sphere]}
    p1 = geometry.prepare(_document(first))
    p1_again = geometry.prepare(_document(first))
    p2 = geometry.prepare(_document(second))
    samples = [(0.3, 0.2, 0.1), (-0.4, 0.1, 0.2), (0.1, -0.3, 0.2)]
    assert [p1.evaluate(point) for point in samples] == [p1_again.evaluate(point) for point in samples]
    assert [p1.evaluate(point) for point in samples] != [p2.evaluate(point) for point in samples]
    assert all(abs(p1.evaluate(point)) <= 0.5 + 0.1 + 1e-6 for point in samples)


def test_bounds_contain_sampled_surface() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prepared = geometry.prepare(document)
    bounds = prepared.bounds
    for x in range(-12, 13, 3):
        for y in range(-6, 7, 3):
            for z in range(-14, 15, 3):
                point = (x * 0.5, y * 0.5, z * 0.5)
                if abs(prepared.evaluate(point)) > 0.08:
                    continue
                assert bounds.contains(point, tolerance=0.08), point


def test_budget_rejects_excessive_repetition_nodes_recursion_and_memory() -> None:
    excessive = {
        "id": "rep",
        "operator": "nw.geom.repeat",
        "version": "1.0.0",
        "parameters": {"spacing": [1, 1, 1], "counts": [64, 64, 64]},
        "children": [_node("nw.geom.sphere", radius=1.0)],
    }
    with pytest.raises(geometry.GeometryError) as exc:
        geometry.prepare(_document(excessive))
    assert any(item["code"] == "geometry_repetition_budget_exceeded" for item in exc.value.diagnostics)

    node = _node("nw.geom.sphere", radius=1.0)
    for _ in range(40):
        node = {"id": "translate", "operator": "nw.geom.translate", "version": "1.0.0", "parameters": {"offset": [0, 0, 0]}, "children": [node]}
    with pytest.raises(geometry.GeometryError) as exc:
        geometry.prepare(_document(node))
    assert any(item["code"] == "geometry_recursion_exceeded" for item in exc.value.diagnostics)

    with pytest.raises(geometry.GeometryError) as exc:
        geometry.prepare(_document(_node("nw.geom.sphere", radius=1.0), budget={"max_nodes": 1, "max_depth": 1, "max_repetition_instances": 1, "max_generated_primitives": 1, "max_evaluation_steps": 1, "max_memory_bytes": 1}))
    assert any(item["code"] in {"geometry_evaluation_budget_exceeded", "geometry_memory_budget_exceeded"} for item in exc.value.diagnostics)


def test_explicit_migration_preserves_parameters() -> None:
    document = {
        "format": "nw13.geometry",
        "schema_version": 0,
        "root": {"id": "s", "operator": "nw.geom.sphere", "parameters": {"radius": 2.0}},
    }
    with pytest.raises(geometry.GeometryError):
        geometry.prepare(document)
    migrated = geometry.migrate_document(document)
    assert migrated["schema_version"] == 1
    assert migrated["root"]["version"] == "1.0.0"
    prepared = geometry.prepare(migrated)
    assert abs(prepared.evaluate((2, 0, 0))) <= 1e-6


def test_shared_nwgraph_reader_accepts_all_public_geometry_operators() -> None:
    document = nw03.load_graph(REPO_ROOT / "fixtures" / "nw13" / "geometry_operators.nwgraph")
    assert len(document.nodes) == 16
    assert {node.operator_id for node in document.nodes.values()} == set(geometry.OPERATOR_CONTRACTS)



def test_nested_repetition_admission_and_analytic_march() -> None:
    sphere = _node("nw.geom.sphere", radius=1)
    prepared = geometry.prepare(_document(sphere))
    hit = geometry.ray_march(prepared, (0, 0, 5), (0, 0, -1))
    assert hit.distance == pytest.approx(4.0, abs=1e-4)
    nested = sphere
    for i in range(3):
        nested = {"id": f"repeat{i}", "operator": "nw.geom.repeat", "version": "1.0.0",
                  "parameters": {"spacing": [2, 1, 1], "counts": [32, 1, 1]}, "children": [nested]}
    doc = _document(nested)
    doc.pop("budget")
    with pytest.raises(geometry.GeometryError, match="repetition"):
        geometry.prepare(doc)


def test_displacement_bounds_after_nonuniform_scaling() -> None:
    sphere = _node("nw.geom.sphere", radius=1)
    scaled = {**_node("nw.geom.scale", scale=[100, 1, 1]), "children": [sphere]}
    displaced = {**_node("nw.geom.displace", amplitude=0.1, frequency=1, seed=5), "children": [scaled]}
    prepared = geometry.prepare(_document(displaced))
    assert prepared.evaluate((105, 0, 0)) < 0
    assert prepared.bounds.contains((105, 0, 0))
    assert prepared.bounds.maximum[0] == pytest.approx(110)
