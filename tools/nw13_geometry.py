#!/usr/bin/env python3
"""NW-13 bounded procedural geometry reference and operator contracts.

The module is deliberately backend-neutral.  It is the authoring/compiler-side
reference used by the public ``nw13.geometry`` JSON schema, the geometry
operator registry in ``src/graph/nw_graph.cpp`` and the deterministic tests.

Every operator is a signed-distance function.  Bounds are conservative
axis-aligned boxes: a bound may be larger than the exact surface but it must
never exclude a reachable point.  CSG composition is intentionally separate
from scene composition/mesh aggregation (see ``nw13_scene``).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


FORMAT = "nw13.geometry"
SCHEMA_VERSION = 1
SUPPORTED_VERSIONS = (1,)
OPERATOR_VERSION = "1.0.0"

Vec3 = tuple[float, float, float]


class GeometryError(ValueError):
    """Raised when a geometry document violates a declared bound or type."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "category": "geometry_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


def _vec(value: Any, width: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != width:
        raise GeometryError(
            f"{name} must contain {width} components",
            [_diagnostic("geometry_invalid_parameter", f"{name} must contain {width} components", parameter=name)],
        )
    components = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise GeometryError(
                f"{name} contains a non-finite or non-numeric component",
                [_diagnostic("geometry_invalid_parameter", f"{name} contains a non-finite component", parameter=name)],
            )
        components.append(float(item))
    return tuple(components)


def _number(params: Mapping[str, Any], name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise GeometryError(
            f"{name} must be a finite number",
            [_diagnostic("geometry_invalid_parameter", f"{name} must be a finite number", parameter=name)],
        )
    value = float(value)
    if minimum is not None and value < minimum:
        raise GeometryError(
            f"{name}={value} is below {minimum}",
            [_diagnostic("geometry_invalid_parameter", f"{name} is below its minimum", parameter=name)],
        )
    if maximum is not None and value > maximum:
        raise GeometryError(
            f"{name}={value} is above {maximum}",
            [_diagnostic("geometry_invalid_parameter", f"{name} is above its maximum", parameter=name)],
        )
    return value


def _integer(params: Mapping[str, Any], name: str, *, minimum: int, maximum: int) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeometryError(
            f"{name} must be an integer",
            [_diagnostic("geometry_invalid_parameter", f"{name} must be an integer", parameter=name)],
        )
    if not minimum <= value <= maximum:
        raise GeometryError(
            f"{name}={value} is outside [{minimum}, {maximum}]",
            [_diagnostic("geometry_invalid_parameter", f"{name} is outside its declared range", parameter=name)],
        )
    return value


def _operation(node: Mapping[str, Any]) -> str:
    op = node.get("operator")
    if not isinstance(op, str):
        raise GeometryError(
            "node operator must be a string",
            [_diagnostic("geometry_unknown_operator", "node operator must be a string")],
        )
    if op not in OPERATOR_CONTRACTS:
        raise GeometryError(
            f"unknown geometry operator '{op}'",
            [_diagnostic("geometry_unknown_operator", f"unknown geometry operator '{op}'", operator=op)],
        )
    version = node.get("version", OPERATOR_VERSION)
    if version != OPERATOR_VERSION:
        raise GeometryError(
            f"unsupported {op} version '{version}'",
            [_diagnostic("geometry_operator_version", f"unsupported operator version '{version}'", operator=op)],
        )
    return op


def _children(node: Mapping[str, Any], count_min: int, count_max: int) -> list[Mapping[str, Any]]:
    children = node.get("children", [])
    if not isinstance(children, list) or not count_min <= len(children) <= count_max:
        raise GeometryError(
            f"operator '{node.get('operator')}' requires {count_min}..{count_max} children",
            [_diagnostic("geometry_invalid_child_count", "child count is outside the declared range", operator=str(node.get("operator")))],
        )
    for child in children:
        if not isinstance(child, Mapping):
            raise GeometryError(
                "child nodes must be objects",
                [_diagnostic("geometry_invalid_node", "child nodes must be objects")],
            )
    return list(children)


@dataclass(frozen=True)
class AABB:
    minimum: Vec3
    maximum: Vec3

    def __post_init__(self) -> None:
        for axis in range(3):
            if self.minimum[axis] > self.maximum[axis]:
                raise GeometryError(
                    "bounds minimum exceeds maximum",
                    [_diagnostic("geometry_invalid_bounds", "axis-aligned bound minimum exceeds maximum")],
                )

    @property
    def center(self) -> Vec3:
        return tuple((self.minimum[i] + self.maximum[i]) * 0.5 for i in range(3))  # type: ignore[return-value]

    @property
    def extent(self) -> Vec3:
        return tuple(self.maximum[i] - self.minimum[i] for i in range(3))  # type: ignore[return-value]

    @property
    def radius(self) -> float:
        half = tuple((self.maximum[i] - self.minimum[i]) * 0.5 for i in range(3))
        return math.sqrt(sum(value * value for value in half))

    def expanded(self, amount: float) -> "AABB":
        return AABB(
            tuple(self.minimum[i] - amount for i in range(3)),  # type: ignore[arg-type]
            tuple(self.maximum[i] + amount for i in range(3)),  # type: ignore[arg-type]
        )

    def union(self, other: "AABB") -> "AABB":
        return AABB(
            tuple(min(self.minimum[i], other.minimum[i]) for i in range(3)),  # type: ignore[arg-type]
            tuple(max(self.maximum[i], other.maximum[i]) for i in range(3)),  # type: ignore[arg-type]
        )

    def intersection(self, other: "AABB") -> "AABB":
        return AABB(
            tuple(max(self.minimum[i], other.minimum[i]) for i in range(3)),  # type: ignore[arg-type]
            tuple(min(self.maximum[i], other.maximum[i]) for i in range(3)),  # type: ignore[arg-type]
        )

    def contains(self, point: Sequence[float], tolerance: float = 0.0) -> bool:
        return all(
            self.minimum[i] - tolerance <= point[i] <= self.maximum[i] + tolerance
            for i in range(3)
        )

    def to_dict(self) -> dict[str, list[float]]:
        return {"minimum": list(self.minimum), "maximum": list(self.maximum)}


@dataclass
class Budget:
    max_nodes: int = 512
    max_depth: int = 32
    max_repetition_instances: int = 4096
    max_generated_primitives: int = 131072
    max_evaluation_steps: int = 1_048_576
    max_memory_bytes: int = 64 * 1024 * 1024

    def to_dict(self) -> dict[str, int]:
        return {
            "max_nodes": self.max_nodes,
            "max_depth": self.max_depth,
            "max_repetition_instances": self.max_repetition_instances,
            "max_generated_primitives": self.max_generated_primitives,
            "max_evaluation_steps": self.max_evaluation_steps,
            "max_memory_bytes": self.max_memory_bytes,
        }


@dataclass
class PreparedGeometry:
    document: dict[str, Any]
    root: dict[str, Any]
    budget: Budget
    stats: dict[str, int]
    bounds: AABB
    contract_hashes: dict[str, str] = field(default_factory=dict)

    def evaluate(self, point: Sequence[float]) -> float:
        return evaluate_node(self.root, point)


@dataclass(frozen=True)
class RayHit:
    distance: float
    point: Vec3
    steps: int
    exhausted: bool = False

    @property
    def hit(self) -> bool:
        return math.isfinite(self.distance)


OPERATOR_CONTRACTS: dict[str, dict[str, Any]] = {
    "nw.geom.sphere": {
        "kind": "primitive",
        "parameters": {"radius": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6}},
        "reference_points": [[[0.0, 0.0, 0.0], -1.0], [[1.0, 0.0, 0.0], 0.0]],
    },
    "nw.geom.box": {
        "kind": "primitive",
        "parameters": {
            "half_extents": {"type": "vec3", "minimum": 1.0e-6, "maximum": 1.0e6}
        },
        "reference_points": [[[0.0, 0.0, 0.0], -1.0], [[1.0, 0.0, 0.0], 0.0]],
    },
    "nw.geom.cylinder": {
        "kind": "primitive",
        "parameters": {
            "radius": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
            "half_height": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
        },
        "reference_points": [[[0.0, 0.0, 0.0], -1.0], [[1.0, 0.0, 0.0], 0.0]],
    },
    "nw.geom.capsule": {
        "kind": "primitive",
        "parameters": {
            "radius": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
            "half_height": {"type": "float", "minimum": 0.0, "maximum": 1.0e6},
        },
        "reference_points": [[[0.0, 0.0, 0.0], -1.0], [[1.0, 0.0, 0.0], 0.0]],
    },
    "nw.geom.torus": {
        "kind": "primitive",
        "parameters": {
            "major_radius": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
            "minor_radius": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
        },
        "reference_points": [[[0.0, 0.0, 0.0], -1.0], [[1.0, 0.0, 0.0], 0.0]],
    },
    "nw.geom.translate": {
        "kind": "transform",
        "parameters": {"offset": {"type": "vec3", "minimum": -1.0e6, "maximum": 1.0e6}},
        "children": [1, 1],
    },
    "nw.geom.rotate": {
        "kind": "transform",
        "parameters": {
            "axis": {"type": "enum", "values": ["x", "y", "z"]},
            "degrees": {"type": "float", "minimum": -3600.0, "maximum": 3600.0},
        },
        "children": [1, 1],
    },
    "nw.geom.scale": {
        "kind": "transform",
        "parameters": {"scale": {"type": "vec3", "minimum": 1.0e-6, "maximum": 1.0e6}},
        "children": [1, 1],
    },
    "nw.geom.union": {"kind": "csg", "children": [2, 8]},
    "nw.geom.intersection": {"kind": "csg", "children": [2, 8]},
    "nw.geom.subtract": {"kind": "csg", "children": [2, 2]},
    "nw.geom.smooth_union": {
        "kind": "csg",
        "parameters": {"k": {"type": "float", "minimum": 0.0, "maximum": 1.0e6}},
        "children": [2, 8],
    },
    "nw.geom.repeat": {
        "kind": "finite",
        "parameters": {
            "spacing": {"type": "vec3", "minimum": 1.0e-6, "maximum": 1.0e6},
            "counts": {"type": "ivec3", "minimum": 1, "maximum": 64},
        },
        "children": [1, 1],
    },
    "nw.geom.twist": {
        "kind": "deformation",
        "parameters": {
            "half_height": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
            "max_degrees": {"type": "float", "minimum": -3600.0, "maximum": 3600.0},
        },
        "children": [1, 1],
    },
    "nw.geom.bend": {
        "kind": "deformation",
        "parameters": {
            "half_width": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
            "max_degrees": {"type": "float", "minimum": -180.0, "maximum": 180.0},
        },
        "children": [1, 1],
    },
    "nw.geom.displace": {
        "kind": "deformation",
        "parameters": {
            "seed": {"type": "uint32", "minimum": 0, "maximum": 4294967295},
            "amplitude": {"type": "float", "minimum": 0.0, "maximum": 1.0e6},
            "frequency": {"type": "float", "minimum": 1.0e-6, "maximum": 1.0e6},
        },
        "children": [1, 1],
    },
}


def _require_children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    contract = OPERATOR_CONTRACTS[_operation(node)]
    minimum, maximum = contract.get("children", [0, 0])
    return _children(node, minimum, maximum)


def _params(node: Mapping[str, Any]) -> Mapping[str, Any]:
    params = node.get("parameters", {})
    if not isinstance(params, Mapping):
        raise GeometryError(
            "node parameters must be an object",
            [_diagnostic("geometry_invalid_parameter", "node parameters must be an object")],
        )
    return params


def _hash_u32(value: int) -> int:
    value &= 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value & 0xFFFFFFFF


def _hash_coord(x: int, y: int, z: int, seed: int) -> int:
    value = (x * 0x9E3779B9) & 0xFFFFFFFF
    value ^= (y * 0x85EBCA6B) & 0xFFFFFFFF
    value ^= (z * 0xC2B2AE35) & 0xFFFFFFFF
    value ^= seed & 0xFFFFFFFF
    return _hash_u32(value)


def _fade(value: float) -> float:
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def value_noise3(x: float, y: float, z: float, seed: int) -> float:
    """Seeded value noise in [0, 1], matching the shader-side integer hash."""
    x0, y0, z0 = math.floor(x), math.floor(y), math.floor(z)
    tx, ty, tz = _fade(x - x0), _fade(y - y0), _fade(z - z0)
    values = [
        _hash_coord(x0 + dx, y0 + dy, z0 + dz, seed) / 4294967296.0
        for dz in (0, 1)
        for dy in (0, 1)
        for dx in (0, 1)
    ]
    x00 = _lerp(values[0], values[1], tx)
    x10 = _lerp(values[2], values[3], tx)
    x01 = _lerp(values[4], values[5], tx)
    x11 = _lerp(values[6], values[7], tx)
    return _lerp(_lerp(x00, x10, ty), _lerp(x01, x11, ty), tz)


def _length(values: Iterable[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _rotate_point(point: Vec3, axis: str, degrees: float) -> Vec3:
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    x, y, z = point
    if axis == "x":
        return (x, c * y - s * z, s * y + c * z)
    if axis == "y":
        return (c * x + s * z, y, -s * x + c * z)
    return (c * x - s * y, s * x + c * y, z)


def evaluate_node(node: Mapping[str, Any], point: Sequence[float]) -> float:
    """Evaluate one bounded signed-distance node at a point."""
    if len(point) != 3 or not all(math.isfinite(float(c)) for c in point):
        raise GeometryError(
            "evaluation point must be a finite vec3",
            [_diagnostic("geometry_invalid_point", "evaluation point must be a finite vec3")],
        )
    p: Vec3 = (float(point[0]), float(point[1]), float(point[2]))
    op = _operation(node)
    params = _params(node)
    if op == "nw.geom.sphere":
        radius = _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        return _length(p) - radius
    if op == "nw.geom.box":
        half = _vec(params.get("half_extents"), 3, "half_extents")
        q = tuple(abs(p[i]) - half[i] for i in range(3))
        outside = _length(max(component, 0.0) for component in q)
        inside = min(max(q), 0.0)
        return outside + inside
    if op == "nw.geom.cylinder":
        radius = _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        half_height = _number(params, "half_height", minimum=1.0e-6, maximum=1.0e6)
        d = (_length((p[0], p[2])) - radius, abs(p[1]) - half_height)
        return min(max(d[0], d[1]), 0.0) + _length(max(component, 0.0) for component in d)
    if op == "nw.geom.capsule":
        radius = _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        half_height = _number(params, "half_height", minimum=0.0, maximum=1.0e6)
        q = (p[0], p[1] - max(-half_height, min(half_height, p[1])), p[2])
        return _length(q) - radius
    if op == "nw.geom.torus":
        major = _number(params, "major_radius", minimum=1.0e-6, maximum=1.0e6)
        minor = _number(params, "minor_radius", minimum=1.0e-6, maximum=1.0e6)
        q = (_length((p[0], p[2])) - major, p[1])
        return _length(q) - minor
    if op == "nw.geom.translate":
        offset = _vec(params.get("offset"), 3, "offset")
        return evaluate_node(_require_children(node)[0], tuple(p[i] - offset[i] for i in range(3)))
    if op == "nw.geom.rotate":
        axis = params.get("axis", "y")
        if axis not in {"x", "y", "z"}:
            raise GeometryError(
                "rotation axis must be x, y or z",
                [_diagnostic("geometry_invalid_parameter", "rotation axis must be x, y or z", parameter="axis")],
            )
        degrees = _number(params, "degrees", minimum=-3600.0, maximum=3600.0)
        return evaluate_node(_require_children(node)[0], _rotate_point(p, axis, -degrees))
    if op == "nw.geom.scale":
        scale = _vec(params.get("scale"), 3, "scale")
        if any(value <= 0.0 for value in scale):
            raise GeometryError(
                "scale components must be positive",
                [_diagnostic("geometry_invalid_parameter", "scale components must be positive", parameter="scale")],
            )
        local = tuple(p[i] / scale[i] for i in range(3))
        return evaluate_node(_require_children(node)[0], local) * min(scale)
    if op == "nw.geom.union":
        return min(evaluate_node(child, p) for child in _require_children(node))
    if op == "nw.geom.intersection":
        return max(evaluate_node(child, p) for child in _require_children(node))
    if op == "nw.geom.subtract":
        a, b = _require_children(node)
        return max(evaluate_node(a, p), -evaluate_node(b, p))
    if op == "nw.geom.smooth_union":
        k = _number(params, "k", minimum=0.0, maximum=1.0e6)
        children = _require_children(node)
        if k == 0.0:
            return min(evaluate_node(child, p) for child in children)
        result = evaluate_node(children[0], p)
        for child in children[1:]:
            b = evaluate_node(child, p)
            h = max(0.0, min(1.0, 0.5 + 0.5 * (b - result) / k))
            result = _lerp(b, result, h) - k * h * (1.0 - h)
        return result
    if op == "nw.geom.repeat":
        spacing = _vec(params.get("spacing"), 3, "spacing")
        counts_raw = params.get("counts")
        if not isinstance(counts_raw, (list, tuple)) or len(counts_raw) != 3:
            raise GeometryError(
                "counts must be an integer vec3",
                [_diagnostic("geometry_invalid_parameter", "counts must be an integer vec3", parameter="counts")],
            )
        counts = tuple(_integer({"counts": value}, "counts", minimum=1, maximum=64) for value in counts_raw)
        child = _require_children(node)[0]
        return min(
            evaluate_node(child, tuple(p[i] - spacing[i] * (index[i] - counts[i] // 2)
                                       for i in range(3)))
            for index in itertools.product(*(range(count) for count in counts))
        )
    if op == "nw.geom.twist":
        half_height = _number(params, "half_height", minimum=1.0e-6, maximum=1.0e6)
        max_degrees = _number(params, "max_degrees", minimum=-3600.0, maximum=3600.0)
        clamped_y = max(-half_height, min(half_height, p[1]))
        angle = -max_degrees * clamped_y / half_height
        return evaluate_node(_require_children(node)[0], _rotate_point(p, "y", angle))
    if op == "nw.geom.bend":
        half_width = _number(params, "half_width", minimum=1.0e-6, maximum=1.0e6)
        max_degrees = _number(params, "max_degrees", minimum=-180.0, maximum=180.0)
        clamped_x = max(-half_width, min(half_width, p[0]))
        angle = -max_degrees * clamped_x / half_width
        return evaluate_node(_require_children(node)[0], _rotate_point(p, "z", angle))
    if op == "nw.geom.displace":
        seed = _integer(params, "seed", minimum=0, maximum=4294967295)
        amplitude = _number(params, "amplitude", minimum=0.0, maximum=1.0e6)
        frequency = _number(params, "frequency", minimum=1.0e-6, maximum=1.0e6)
        noise = value_noise3(p[0] * frequency, p[1] * frequency, p[2] * frequency, seed)
        return evaluate_node(_require_children(node)[0], p) + amplitude * (2.0 * noise - 1.0)
    raise GeometryError(
        f"unknown geometry operator '{op}'",
        [_diagnostic("geometry_unknown_operator", f"unknown geometry operator '{op}'", operator=op)],
    )


def bounds_node(node: Mapping[str, Any], level: float = 0.0) -> AABB:
    """Enclose the field's sublevel set, including positive displacement margins."""
    op = _operation(node)
    params = _params(node)
    if op == "nw.geom.sphere":
        radius = _number(params, "radius", minimum=1.0e-6, maximum=1.0e6) + level
        return AABB((-radius, -radius, -radius), (radius, radius, radius))
    if op == "nw.geom.box":
        half = _vec(params.get("half_extents"), 3, "half_extents")
        return AABB(tuple(-value for value in half), tuple(half)).expanded(level)  # type: ignore[arg-type]
    if op == "nw.geom.cylinder":
        radius = _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        half_height = _number(params, "half_height", minimum=1.0e-6, maximum=1.0e6)
        return AABB((-radius, -half_height, -radius), (radius, half_height, radius)).expanded(level)
    if op == "nw.geom.capsule":
        radius = _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        half_height = _number(params, "half_height", minimum=0.0, maximum=1.0e6)
        extent = radius + half_height
        return AABB((-radius, -extent, -radius), (radius, extent, radius)).expanded(level)
    if op == "nw.geom.torus":
        major = _number(params, "major_radius", minimum=1.0e-6, maximum=1.0e6)
        minor = _number(params, "minor_radius", minimum=1.0e-6, maximum=1.0e6)
        extent = major + minor
        return AABB((-extent, -minor, -extent), (extent, minor, extent)).expanded(level)
    if op == "nw.geom.translate":
        offset = _vec(params.get("offset"), 3, "offset")
        child = bounds_node(_require_children(node)[0], level)
        return AABB(
            tuple(child.minimum[i] + offset[i] for i in range(3)),  # type: ignore[arg-type]
            tuple(child.maximum[i] + offset[i] for i in range(3)),  # type: ignore[arg-type]
        )
    if op == "nw.geom.rotate":
        axis = params.get("axis", "y")
        _number(params, "degrees", minimum=-3600.0, maximum=3600.0)
        child = bounds_node(_require_children(node)[0], level)
        radius = child.radius
        center = _rotate_point(child.center, axis, _number(params, "degrees", minimum=-3600.0, maximum=3600.0))
        # Rotate the bound centre and keep the enclosing radius.  Rotating the
        # un-transformed centre would exclude offset geometry.
        return AABB(
            tuple(center[i] - radius for i in range(3)),  # type: ignore[arg-type]
            tuple(center[i] + radius for i in range(3)),  # type: ignore[arg-type]
        )
    if op == "nw.geom.scale":
        scale = _vec(params.get("scale"), 3, "scale")
        child = bounds_node(_require_children(node)[0], level / min(scale))
        return AABB(
            tuple(child.minimum[i] * scale[i] for i in range(3)),  # type: ignore[arg-type]
            tuple(child.maximum[i] * scale[i] for i in range(3)),  # type: ignore[arg-type]
        )
    if op == "nw.geom.union":
        result = bounds_node(_require_children(node)[0], level)
        for child in _require_children(node)[1:]:
            result = result.union(bounds_node(child, level))
        return result
    if op == "nw.geom.intersection":
        result = bounds_node(_require_children(node)[0], level)
        for child in _require_children(node)[1:]:
            other = bounds_node(child, level)
            if any(result.minimum[i] > other.maximum[i] or other.minimum[i] > result.maximum[i] for i in range(3)):
                # Disjoint solid intersection has no surface.  A degenerate
                # bound is a valid conservative representation.
                point = tuple(
                    max(min(result.minimum[i], other.minimum[i]), min(result.maximum[i], other.maximum[i]))
                    for i in range(3)
                )
                return AABB(point, point)  # type: ignore[arg-type]
            result = result.intersection(other)
        return result
    if op == "nw.geom.subtract":
        a, _ = _require_children(node)
        return bounds_node(a, level)
    if op == "nw.geom.smooth_union":
        k = _number(params, "k", minimum=0.0, maximum=1.0e6)
        children = _require_children(node)
        child_level = level + (len(children) - 1) * k * 0.25
        result = bounds_node(children[0], child_level)
        for child in children[1:]:
            result = result.union(bounds_node(child, child_level))
        return result
    if op == "nw.geom.repeat":
        spacing = _vec(params.get("spacing"), 3, "spacing")
        counts_raw = params.get("counts")
        if not isinstance(counts_raw, (list, tuple)) or len(counts_raw) != 3:
            raise GeometryError(
                "counts must be an integer vec3",
                [_diagnostic("geometry_invalid_parameter", "counts must be an integer vec3", parameter="counts")],
            )
        counts = tuple(_integer({"counts": value}, "counts", minimum=1, maximum=64) for value in counts_raw)
        child = bounds_node(_require_children(node)[0], level)
        offsets = [(-(counts[i] // 2) * spacing[i], ((counts[i] - 1) // 2) * spacing[i])
                   for i in range(3)]
        return AABB(
            tuple(child.minimum[i] + min(offsets[i]) for i in range(3)),  # type: ignore[arg-type]
            tuple(child.maximum[i] + max(offsets[i]) for i in range(3)),  # type: ignore[arg-type]
        )
    if op in {"nw.geom.twist", "nw.geom.bend"}:
        _number(
            params,
            "half_height" if op == "nw.geom.twist" else "half_width",
            minimum=1.0e-6,
            maximum=1.0e6,
        )
        _number(
            params,
            "max_degrees",
            minimum=-3600.0 if op == "nw.geom.twist" else -180.0,
            maximum=3600.0 if op == "nw.geom.twist" else 180.0,
        )
        child = bounds_node(_require_children(node)[0], level)
        radius = child.radius
        center = child.center
        # Twist and bend rotate points about an axis through the origin by an
        # amount that depends on position.  An origin-centred enclosing sphere
        # is conservative for the whole transformed shape.
        origin_radius = _length(center) + radius
        return AABB(
            (-origin_radius, -origin_radius, -origin_radius),
            (origin_radius, origin_radius, origin_radius),
        )
    if op == "nw.geom.displace":
        amplitude = _number(params, "amplitude", minimum=0.0, maximum=1.0e6)
        _integer(params, "seed", minimum=0, maximum=4294967295)
        _number(params, "frequency", minimum=1.0e-6, maximum=1.0e6)
        return bounds_node(_require_children(node)[0], level + amplitude)
    raise GeometryError(
        f"unknown geometry operator '{op}'",
        [_diagnostic("geometry_unknown_operator", f"unknown geometry operator '{op}'", operator=op)],
    )



def field_lipschitz(node: Mapping[str, Any], domain: AABB) -> float:
    """Conservative gradient bound throughout the ray's local clipping box."""
    op = _operation(node)
    params = _params(node)
    children = _require_children(node)
    if not children:
        return 1.0
    if op == "nw.geom.scale":
        scale = _vec(params["scale"], 3, "scale")
        child_domain = AABB(tuple(x / k for x, k in zip(domain.minimum, scale)),
                            tuple(x / k for x, k in zip(domain.maximum, scale)))
        # The evaluator multiplies by min(scale) and the inverse Jacobian's
        # norm is 1/min(scale), so anisotropic scaling does not inflate L.
        return field_lipschitz(children[0], child_domain)
    if op == "nw.geom.translate":
        offset = _vec(params["offset"], 3, "offset")
        child_domain = AABB(tuple(x - d for x, d in zip(domain.minimum, offset)),
                            tuple(x - d for x, d in zip(domain.maximum, offset)))
        return field_lipschitz(children[0], child_domain)
    if op == "nw.geom.rotate":
        center = _rotate_point(domain.center, params.get("axis", "y"), -params["degrees"])
        radius = domain.radius
        return field_lipschitz(children[0], AABB(tuple(x - radius for x in center),
                                                  tuple(x + radius for x in center)))
    if op == "nw.geom.repeat":
        spacing = _vec(params["spacing"], 3, "spacing")
        counts = params["counts"]
        return field_lipschitz(children[0], AABB(
            tuple(x - (n - 1 - n // 2) * k for x, n, k in zip(domain.minimum, counts, spacing)),
            tuple(x + (n // 2) * k for x, n, k in zip(domain.maximum, counts, spacing)),
        ))
    if op in {"nw.geom.twist", "nw.geom.bend"}:
        axes = (0, 2) if op == "nw.geom.twist" else (0, 1)
        extents = tuple(max(abs(a), abs(b)) for a, b in zip(domain.minimum, domain.maximum))
        radial_extent = math.hypot(*(extents[i] for i in axes))
        angle_slope = math.radians(abs(params["max_degrees"])) / params[
            "half_height" if op == "nw.geom.twist" else "half_width"]
        low, high = list(domain.minimum), list(domain.maximum)
        for axis in axes:
            low[axis], high[axis] = -radial_extent, radial_extent
        return (1.0 + angle_slope * radial_extent) * field_lipschitz(children[0], AABB(tuple(low), tuple(high)))
    if op == "nw.geom.displace":
        # Each interpolated noise axis changes by at most the maximum
        # quintic fade derivative, 15/8. Value noise is in [0,1].
        noise_gradient = (15.0 / 4.0) * math.sqrt(3.0) * params["amplitude"] * params["frequency"]
        return field_lipschitz(children[0], domain) + noise_gradient
    # Min/max and smooth min are non-expansive in the max norm of their
    # arguments. Smooth min's derivative weights are nonnegative and sum to 1.
    return max(field_lipschitz(child, domain) for child in children)


def ray_interval(domain: AABB, origin: Sequence[float], ray: Sequence[float],
                 start: float, end: float) -> tuple[float, float] | None:
    for i in range(3):
        if ray[i] == 0.0:
            if not domain.minimum[i] <= origin[i] <= domain.maximum[i]:
                return None
            continue
        a, b = ((bound - origin[i]) / ray[i] for bound in (domain.minimum[i], domain.maximum[i]))
        start, end = max(start, min(a, b)), min(end, max(a, b))
        if start > end:
            return None
    return start, end


def bounded_field_step(value: float, lipschitz: float) -> float:
    return 0.95 * abs(value) / lipschitz

def _prepare_node(
    node: Mapping[str, Any],
    budget: Budget,
    depth: int,
    stats: dict[str, int],
    multiplicity: int = 1,
) -> None:
    if depth > budget.max_depth:
        raise GeometryError(
            "geometry recursion bound exceeded",
            [_diagnostic("geometry_recursion_exceeded", "geometry recursion bound exceeded", depth=depth)],
        )
    stats["nodes"] += 1
    if stats["nodes"] > budget.max_nodes:
        raise GeometryError(
            "geometry node bound exceeded",
            [_diagnostic("geometry_node_budget_exceeded", "geometry node bound exceeded", nodes=stats["nodes"])],
        )
    op = _operation(node)
    params = _params(node)
    if op == "nw.geom.sphere":
        _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
    elif op == "nw.geom.box":
        half = _vec(params.get("half_extents"), 3, "half_extents")
        if any(value <= 0.0 for value in half):
            raise GeometryError(
                "box half_extents must be positive",
                [_diagnostic("geometry_invalid_parameter", "box half_extents must be positive", parameter="half_extents")],
            )
    elif op == "nw.geom.cylinder":
        _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        _number(params, "half_height", minimum=1.0e-6, maximum=1.0e6)
    elif op == "nw.geom.capsule":
        _number(params, "radius", minimum=1.0e-6, maximum=1.0e6)
        _number(params, "half_height", minimum=0.0, maximum=1.0e6)
    elif op == "nw.geom.torus":
        _number(params, "major_radius", minimum=1.0e-6, maximum=1.0e6)
        _number(params, "minor_radius", minimum=1.0e-6, maximum=1.0e6)
    elif op == "nw.geom.translate":
        _vec(params.get("offset"), 3, "offset")
    elif op == "nw.geom.rotate":
        if params.get("axis", "y") not in {"x", "y", "z"}:
            raise GeometryError(
                "rotation axis must be x, y or z",
                [_diagnostic("geometry_invalid_parameter", "rotation axis must be x, y or z", parameter="axis")],
            )
        _number(params, "degrees", minimum=-3600.0, maximum=3600.0)
    elif op == "nw.geom.scale":
        scale = _vec(params.get("scale"), 3, "scale")
        if any(value <= 0.0 for value in scale):
            raise GeometryError(
                "scale components must be positive",
                [_diagnostic("geometry_invalid_parameter", "scale components must be positive", parameter="scale")],
            )
    elif op == "nw.geom.smooth_union":
        _number(params, "k", minimum=0.0, maximum=1.0e6)
    elif op == "nw.geom.repeat":
        _vec(params.get("spacing"), 3, "spacing")
        counts_raw = params.get("counts")
        if not isinstance(counts_raw, (list, tuple)) or len(counts_raw) != 3:
            raise GeometryError(
                "counts must be an integer vec3",
                [_diagnostic("geometry_invalid_parameter", "counts must be an integer vec3", parameter="counts")],
            )
        counts = tuple(_integer({"counts": value}, "counts", minimum=1, maximum=64) for value in counts_raw)
        instances = counts[0] * counts[1] * counts[2]
        multiplicity *= instances
        stats["repetition_instances"] = max(stats["repetition_instances"], multiplicity)
        if stats["repetition_instances"] > budget.max_repetition_instances:
            raise GeometryError(
                "finite repetition bound exceeded",
                [_diagnostic("geometry_repetition_budget_exceeded", "finite repetition bound exceeded", instances=stats["repetition_instances"])],
            )
    elif op == "nw.geom.twist":
        _number(params, "half_height", minimum=1.0e-6, maximum=1.0e6)
        _number(params, "max_degrees", minimum=-3600.0, maximum=3600.0)
    elif op == "nw.geom.bend":
        _number(params, "half_width", minimum=1.0e-6, maximum=1.0e6)
        _number(params, "max_degrees", minimum=-180.0, maximum=180.0)
    elif op == "nw.geom.displace":
        _integer(params, "seed", minimum=0, maximum=4294967295)
        _number(params, "amplitude", minimum=0.0, maximum=1.0e6)
        _number(params, "frequency", minimum=1.0e-6, maximum=1.0e6)
    children = _require_children(node)
    if not children:
        stats["generated_primitives"] += multiplicity
        if stats["generated_primitives"] > budget.max_generated_primitives:
            raise GeometryError(
                "generated geometry bound exceeded",
                [_diagnostic("geometry_generated_geometry_budget_exceeded", "generated geometry bound exceeded", primitives=stats["generated_primitives"])],
            )
    for child in children:
        _prepare_node(child, budget, depth + 1, stats, multiplicity)


def prepare(document: Mapping[str, Any], budget: Budget | None = None) -> PreparedGeometry:
    """Validate a document, count its work and return a prepared tree."""
    if not isinstance(document, Mapping):
        raise GeometryError("geometry document must be an object")
    if document.get("format") not in (None, FORMAT):
        raise GeometryError(
            "geometry document format is not nw13.geometry",
            [_diagnostic("geometry_schema_version", "geometry document format is not nw13.geometry")],
        )
    if document.get("schema_version") == 0:
        raise GeometryError(
            "geometry document requires the explicit v0-to-v1 migration",
            [_diagnostic("geometry_migration_required", "geometry document requires the explicit v0-to-v1 migration")],
        )
    if document.get("schema_version") not in SUPPORTED_VERSIONS:
        raise GeometryError(
            "unsupported geometry schema version",
            [_diagnostic("geometry_schema_version", "unsupported geometry schema version")],
        )
    root = document.get("root")
    if not isinstance(root, Mapping):
        raise GeometryError(
            "geometry document root must be an object",
            [_diagnostic("geometry_invalid_node", "geometry document root must be an object")],
        )
    active_budget = budget or Budget()
    if isinstance(document.get("budget"), Mapping):
        values = document["budget"]
        limits = active_budget.to_dict()
        for name, limit in limits.items():
            requested = values.get(name, limit)
            if type(requested) is not int or requested <= 0:
                raise GeometryError(f"geometry budget {name} must be a positive integer")
            limits[name] = min(requested, limit)
        active_budget = Budget(**limits)
    stats = {"nodes": 0, "repetition_instances": 0, "generated_primitives": 0}
    _prepare_node(root, active_budget, 0, stats)
    estimated_steps = stats["nodes"] * 64 + stats["generated_primitives"] * 8
    estimated_memory = stats["nodes"] * 512 + stats["generated_primitives"] * 64
    if estimated_steps > active_budget.max_evaluation_steps:
        raise GeometryError(
            "geometry evaluation-step bound exceeded",
            [_diagnostic("geometry_evaluation_budget_exceeded", "geometry evaluation-step bound exceeded", steps=estimated_steps)],
        )
    if estimated_memory > active_budget.max_memory_bytes:
        raise GeometryError(
            "geometry memory bound exceeded",
            [_diagnostic("geometry_memory_budget_exceeded", "geometry memory bound exceeded", bytes=estimated_memory)],
        )
    stats["evaluation_steps"] = estimated_steps
    stats["memory_bytes"] = estimated_memory
    root_copy = json.loads(json.dumps(root))
    return PreparedGeometry(
        document=json.loads(json.dumps(document)),
        root=root_copy,
        budget=active_budget,
        stats=stats,
        bounds=bounds_node(root_copy),
        contract_hashes={
            op: contract_hash(op) for op in sorted(_collect_operators(root_copy))
        },
    )


def _collect_operators(node: Mapping[str, Any]) -> set[str]:
    result = {str(node.get("operator"))}
    for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
        if isinstance(child, Mapping):
            result.update(_collect_operators(child))
    return result


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return "sha256:v1:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def contract_hash(operator: str) -> str:
    if operator not in OPERATOR_CONTRACTS:
        raise GeometryError(f"unknown geometry operator '{operator}'")
    payload = {
        "format": FORMAT,
        "version": SCHEMA_VERSION,
        "operator": operator,
        "operator_version": OPERATOR_VERSION,
        "contract": OPERATOR_CONTRACTS[operator],
    }
    return semantic_hash(payload)


def contract_document() -> dict[str, Any]:
    """Public versioned operator-contract catalog for the geometry schema."""
    operators = []
    for operator in sorted(OPERATOR_CONTRACTS):
        contract = OPERATOR_CONTRACTS[operator]
        operators.append(
            {
                "operator_id": operator,
                "operator_version": OPERATOR_VERSION,
                "kind": contract["kind"],
                "parameters": contract.get("parameters", {}),
                "children": contract.get("children", [0, 0]),
                "reference_points": contract.get("reference_points", []),
                "contract_digest": contract_hash(operator),
                "semantics": {
                    "sphere": "|p|-r",
                    "box": "length(max(abs(p)-b,0))+min(max(q),0)",
                    "cylinder": "bounded Y-axis cylinder",
                    "capsule": "bounded Y-axis capsule",
                    "torus": "XZ ring with Y tube",
                    "translate": "p-offset",
                    "rotate": "inverse Euler-axis rotation",
                    "scale": "p/scale with Lipschitz correction",
                    "union": "min(a,b)",
                    "intersection": "max(a,b)",
                    "subtract": "max(a,-b)",
                    "smooth_union": "bounded polynomial smin; k=0 equals union",
                    "repeat": "finite nearest-lattice repetition",
                    "twist": "bounded position-dependent Y rotation",
                    "bend": "bounded position-dependent Z rotation",
                    "displace": "sdf + amplitude*(2*value_noise3-1)",
                }.get(operator.rsplit(".", 1)[-1], ""),
            }
        )
    return {
        "format": "nw13-geometry-operator-contracts",
        "version": 1,
        "schema": "schemas/nw13-geometry-graph.v1.json",
        "operators": operators,
    }


def migrate_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Explicitly migrate the only historical geometry document version."""
    if not isinstance(document, Mapping):
        raise GeometryError("geometry document must be an object")
    if document.get("schema_version") != 0:
        raise GeometryError("only nw13.geometry schema version 0 is migratable")
    migrated = json.loads(json.dumps(document))
    migrated["schema_version"] = 1
    migrated["format"] = FORMAT
    migrated["migration"] = {
        "from": "nw13.geometry.v0",
        "to": "nw13.geometry.v1",
        "rule": "add-format-and-operator-version",
    }

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node.setdefault("version", OPERATOR_VERSION)
        for child in node.get("children", []):
            visit(child)

    visit(migrated.get("root"))
    return migrated


def ray_march(
    prepared: PreparedGeometry,
    origin: Sequence[float],
    direction: Sequence[float],
    *,
    max_distance: float = 64.0,
    max_steps: int = 256,
    epsilon: float = 1.0e-4,
) -> RayHit:
    length = _length(direction)
    if length == 0.0:
        raise GeometryError("ray direction must be non-zero")
    ray = tuple(float(component) / length for component in direction)
    initial = tuple(float(value) for value in origin)
    domain = prepared.bounds.expanded(4.0 * epsilon)
    interval = ray_interval(domain, initial, ray, 0.0, max_distance)
    if interval is None:
        return RayHit(math.inf, initial, 0)
    distance, end = interval
    lipschitz = field_lipschitz(prepared.root, domain)
    point = initial
    for step in range(max_steps):
        point = tuple(initial[i] + ray[i] * distance for i in range(3))
        value = prepared.evaluate(point)
        if abs(value) <= epsilon:
            return RayHit(distance, point, step + 1)
        following = distance + bounded_field_step(value, lipschitz)
        if following > end:
            return RayHit(math.inf, point, step + 1)
        if following <= distance:
            return RayHit(math.inf, point, step + 1, exhausted=True)
        distance = following
    return RayHit(math.inf, point, max_steps, exhausted=True)
