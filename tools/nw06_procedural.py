#!/usr/bin/env python3
"""NW-06 procedural scene reference data and encodings.

This module is compiler/authoring-side code.  It defines the compact
parameters, deterministic CPU/high-precision references, exact buffer layouts,
and resource-estimate helpers for the proof scene:

* a seeded 256x256 two-dimensional value-noise material texture;
* a parametric torus with 64 major and 32 minor segments;
* 256 procedurally placed instances with one dense material record.

Nothing in this module is linked into the player.  The player receives only the
operators, explicit seeds, buffer sizes, and parameters emitted into the
unchanged ``.nwp`` v1 package.  GPU initialization produces the actual texture,
mesh, instance, material, and indirect-argument bytes.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


SCENE_WIDTH = 256
SCENE_HEIGHT = 256
NOISE_WIDTH = 256
NOISE_HEIGHT = 256
TORUS_MAJOR_SEGMENTS = 64
TORUS_MINOR_SEGMENTS = 32
INSTANCE_COUNT = 256
TICKS_PER_SECOND = 48_000

UINT32_MAX = 0xFFFFFFFF
_MASK32 = UINT32_MAX

# Exact byte encodings, asserted by tests and consumed by the fixed HLSL
# implementation.  All offsets are relative to the start of one record.
VERTEX_STRUCT = struct.Struct("<3f3f2f")  # position, normal, uv
VERTEX_BYTES = VERTEX_STRUCT.size  # 32
INDEX_STRUCT = struct.Struct("<I")
INDEX_BYTES = INDEX_STRUCT.size  # 4
INSTANCE_STRUCT = struct.Struct("<16fIIII")  # row-major float4x4, material, seed, flags, pad
INSTANCE_BYTES = INSTANCE_STRUCT.size  # 80
MATERIAL_STRUCT = struct.Struct("<4f3fI")  # base color, roughness/metallic/specular, uint texture index
MATERIAL_BYTES = MATERIAL_STRUCT.size  # 32
LIGHT_STRUCT = struct.Struct("<3f1f3f1f")  # direction.xyz, pad, color.rgb, pad
LIGHT_BYTES = LIGHT_STRUCT.size  # 32
INDIRECT_ARGS_STRUCT = struct.Struct("<5I")
INDIRECT_ARGS_BYTES = INDIRECT_ARGS_STRUCT.size  # 20

VERTEX_LAYOUT = (
    {"name": "position", "format": "R32G32B32_FLOAT", "offset": 0, "bytes": 12},
    {"name": "normal", "format": "R32G32B32_FLOAT", "offset": 12, "bytes": 12},
    {"name": "uv", "format": "R32G32_FLOAT", "offset": 24, "bytes": 8},
)
INSTANCE_LAYOUT = (
    {"name": "transform", "format": "R32G32B32A32_FLOAT[4]", "offset": 0, "bytes": 64},
    {"name": "material_index", "format": "R32_UINT", "offset": 64, "bytes": 4},
    {"name": "seed", "format": "R32_UINT", "offset": 68, "bytes": 4},
    {"name": "flags", "format": "R32_UINT", "offset": 72, "bytes": 4},
    {"name": "reserved", "format": "R32_UINT", "offset": 76, "bytes": 4},
)
MATERIAL_LAYOUT = (
    {"name": "base_color", "format": "R32G32B32A32_FLOAT", "offset": 0, "bytes": 16},
    {"name": "roughness", "format": "R32_FLOAT", "offset": 16, "bytes": 4},
    {"name": "metallic", "format": "R32_FLOAT", "offset": 20, "bytes": 4},
    {"name": "specular", "format": "R32_FLOAT", "offset": 24, "bytes": 4},
    {"name": "texture_index", "format": "R32_UINT", "offset": 28, "bytes": 4},
)
LIGHT_LAYOUT = (
    {"name": "direction", "format": "R32G32B32_FLOAT", "offset": 0, "bytes": 12},
    {"name": "direction_pad", "format": "R32_FLOAT", "offset": 12, "bytes": 4},
    {"name": "color", "format": "R32G32B32_FLOAT", "offset": 16, "bytes": 12},
    {"name": "color_pad", "format": "R32_FLOAT", "offset": 28, "bytes": 4},
)
INDIRECT_ARGS_LAYOUT = (
    {"name": "index_count_per_instance", "offset": 0},
    {"name": "instance_count", "offset": 4},
    {"name": "first_index", "offset": 8},
    {"name": "base_vertex", "offset": 12},
    {"name": "first_instance", "offset": 16},
)


class ProceduralError(ValueError):
    """Raised when procedural parameters or generated data are invalid."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "category": "procedural_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


def _u32(value: int) -> int:
    return int(value) & _MASK32


def hash_u32(value: int) -> int:
    """Exact 32-bit avalanche hash used by both the CPU and GPU references."""
    value = _u32(value)
    value ^= value >> 16
    value = _u32(value * 0x7FEB352D)
    value ^= value >> 15
    value = _u32(value * 0x846CA68B)
    value ^= value >> 16
    return _u32(value)


def hash_coord_u32(x: int, y: int, seed: int) -> int:
    """Exact integer hash for the seed/control acceptance path."""
    value = _u32(_u32(x) * 0x9E3779B9)
    value ^= _u32(_u32(y) * 0x85EBCA6B)
    value ^= _u32(seed)
    return hash_u32(value)


def hash_unit(x: int, y: int, seed: int) -> float:
    return hash_coord_u32(x, y, seed) / 4294967296.0


def _fade(value: float) -> float:
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def value_noise_2d(x: float, y: float, seed: int) -> float:
    """Single-octave value noise in [0, 1] with a deterministic integer hash."""
    x0 = math.floor(x)
    y0 = math.floor(y)
    tx = _fade(x - x0)
    ty = _fade(y - y0)
    v00 = hash_unit(x0, y0, seed)
    v10 = hash_unit(x0 + 1, y0, seed)
    v01 = hash_unit(x0, y0 + 1, seed)
    v11 = hash_unit(x0 + 1, y0 + 1, seed)
    return _lerp(_lerp(v00, v10, tx), _lerp(v01, v11, tx), ty)


@dataclass(frozen=True)
class Noise2DParameters:
    width: int = NOISE_WIDTH
    height: int = NOISE_HEIGHT
    seed: int = 0x4E573036
    octaves: int = 4
    scale: float = 8.0
    lacunarity: float = 2.0
    gain: float = 0.5
    format: str = "r8g8b8a8_unorm"
    normalize: bool = True

    def validate(self) -> None:
        errors: list[dict[str, Any]] = []
        if not 1 <= self.width <= 8192:
            errors.append(_diagnostic("invalid_dimension", f"texture width {self.width} is outside 1..8192"))
        if not 1 <= self.height <= 8192:
            errors.append(_diagnostic("invalid_dimension", f"texture height {self.height} is outside 1..8192"))
        if not 0 <= self.seed <= UINT32_MAX:
            errors.append(_diagnostic("invalid_seed", f"texture seed {self.seed} is outside uint32"))
        if not 1 <= self.octaves <= 8:
            errors.append(_diagnostic("invalid_octave_count", f"octaves {self.octaves} is outside 1..8"))
        for name, value, minimum in (
            ("scale", self.scale, 0.000001),
            ("lacunarity", self.lacunarity, 1.0),
            ("gain", self.gain, 0.0),
        ):
            if not math.isfinite(value) or value < minimum:
                errors.append(_diagnostic("invalid_noise_parameter", f"{name}={value!r} is invalid"))
        if self.gain > 1.0:
            errors.append(_diagnostic("invalid_noise_parameter", "gain must be in [0, 1]"))
        if self.format not in {"r8g8b8a8_unorm", "rgba32f"}:
            errors.append(_diagnostic("invalid_texture_format", f"unsupported texture format '{self.format}'"))
        if errors:
            raise ProceduralError("invalid Noise2D parameters", errors)


def sample_noise2d(
    u: float,
    v: float,
    *,
    seed: int = 0x4E573036,
    octaves: int = 4,
    scale: float = 8.0,
    lacunarity: float = 2.0,
    gain: float = 0.5,
) -> float:
    """High-precision CPU reference for the GPU value-noise implementation."""
    amplitude = 1.0
    frequency = scale
    total = 0.0
    normalization = 0.0
    for octave in range(max(1, octaves)):
        octave_seed = _u32(seed + _u32(0x9E3779B9 * octave))
        total += amplitude * value_noise_2d(u * frequency, v * frequency, octave_seed)
        normalization += amplitude
        amplitude *= gain
        frequency *= lacunarity
    return total / normalization if normalization else 0.0


def float32(value: float) -> float:
    """Round a scalar to IEEE-754 binary32, matching the GPU arithmetic model."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def sample_noise2d_float32(
    u: float,
    v: float,
    *,
    seed: int = 0x4E573036,
    octaves: int = 4,
    scale: float = 8.0,
    lacunarity: float = 2.0,
    gain: float = 0.5,
) -> float:
    """Float32 arithmetic reference used to exercise the CPU/GPU tolerance."""
    amplitude = float32(1.0)
    frequency = float32(scale)
    total = float32(0.0)
    normalization = float32(0.0)
    fu = float32(u)
    fv = float32(v)
    for octave in range(max(1, octaves)):
        octave_seed = _u32(seed + _u32(0x9E3779B9 * octave))
        x = float32(fu * frequency)
        y = float32(fv * frequency)
        x0 = math.floor(x)
        y0 = math.floor(y)
        tx = float32(x - x0)
        ty = float32(y - y0)
        tx = float32(tx * tx * tx * (tx * (tx * 6.0 - 15.0) + 10.0))
        ty = float32(ty * ty * ty * (ty * (ty * 6.0 - 15.0) + 10.0))
        v00 = float32(hash_unit(x0, y0, octave_seed))
        v10 = float32(hash_unit(x0 + 1, y0, octave_seed))
        v01 = float32(hash_unit(x0, y0 + 1, octave_seed))
        v11 = float32(hash_unit(x0 + 1, y0 + 1, octave_seed))
        lower = float32(v00 + float32((v10 - v00) * tx))
        upper = float32(v01 + float32((v11 - v01) * tx))
        value = float32(lower + float32((upper - lower) * ty))
        total = float32(total + float32(amplitude * value))
        normalization = float32(normalization + amplitude)
        amplitude = float32(amplitude * gain)
        frequency = float32(frequency * lacunarity)
    return float32(total / normalization) if normalization else 0.0


def validate_noise2d_reference_tolerance(
    samples: Sequence[tuple[float, float]],
    *,
    seed: int = 0x4E573036,
    absolute_tolerance: float = 1e-5,
    relative_tolerance: float = 1e-4,
) -> dict[str, float | int | bool]:
    """Compare the float32/GPU arithmetic model with the double reference."""
    maximum_absolute = 0.0
    maximum_relative = 0.0
    for u, v in samples:
        reference = sample_noise2d(u, v, seed=seed)
        generated = sample_noise2d_float32(u, v, seed=seed)
        absolute = abs(generated - reference)
        relative = absolute / max(1e-30, abs(reference))
        maximum_absolute = max(maximum_absolute, absolute)
        maximum_relative = max(maximum_relative, relative)
    within = maximum_absolute <= absolute_tolerance or (
        maximum_relative <= relative_tolerance
    )
    return {
        "within_tolerance": within,
        "maximum_absolute_error": maximum_absolute,
        "maximum_relative_error": maximum_relative,
        "absolute_tolerance": absolute_tolerance,
        "relative_tolerance": relative_tolerance,
        "sample_count": len(samples),
    }


def generate_noise2d(
    width: int = NOISE_WIDTH,
    height: int = NOISE_HEIGHT,
    *,
    seed: int = 0x4E573036,
    octaves: int = 4,
    scale: float = 8.0,
    lacunarity: float = 2.0,
    gain: float = 0.5,
) -> tuple[float, ...]:
    """Return row-major grayscale noise samples at pixel centres."""
    params = Noise2DParameters(
        width=width,
        height=height,
        seed=seed,
        octaves=octaves,
        scale=scale,
        lacunarity=lacunarity,
        gain=gain,
    )
    params.validate()
    return tuple(
        sample_noise2d(
            (x + 0.5) / width,
            (y + 0.5) / height,
            seed=seed,
            octaves=octaves,
            scale=scale,
            lacunarity=lacunarity,
            gain=gain,
        )
        for y in range(height)
        for x in range(width)
    )


def noise2d_rgba32f(
    params: Noise2DParameters | None = None,
) -> tuple[tuple[float, float, float, float], ...]:
    params = params or Noise2DParameters()
    params.validate()
    grayscale = generate_noise2d(
        params.width,
        params.height,
        seed=params.seed,
        octaves=params.octaves,
        scale=params.scale,
        lacunarity=params.lacunarity,
        gain=params.gain,
    )
    return tuple((value, value, value, 1.0) for value in grayscale)


def noise2d_rgba8_bytes(params: Noise2DParameters | None = None) -> bytes:
    params = params or Noise2DParameters()
    pixels = noise2d_rgba32f(params)
    output = bytearray(len(pixels) * 4)
    for index, (r, g, b, a) in enumerate(pixels):
        offset = index * 4
        output[offset + 0] = max(0, min(255, int(round(r * 255.0))))
        output[offset + 1] = max(0, min(255, int(round(g * 255.0))))
        output[offset + 2] = max(0, min(255, int(round(b * 255.0))))
        output[offset + 3] = max(0, min(255, int(round(a * 255.0))))
    return bytes(output)


def noise2d_rgba32f_bytes(params: Noise2DParameters | None = None) -> bytes:
    params = params or Noise2DParameters()
    pixels = noise2d_rgba32f(params)
    return struct.pack("<" + "f" * (len(pixels) * 4), *(v for pixel in pixels for v in pixel))


def noise2d_digest(params: Noise2DParameters | None = None) -> str:
    params = params or Noise2DParameters()
    payload = {
        "width": params.width,
        "height": params.height,
        "seed": params.seed,
        "octaves": params.octaves,
        "scale": params.scale,
        "lacunarity": params.lacunarity,
        "gain": params.gain,
        "format": params.format,
    }
    return "sha256:v1:" + hashlib.sha256(
        b"nw06-noise2d-v1\0"
        + repr(tuple(sorted(payload.items()))).encode("ascii")
    ).hexdigest()


@dataclass(frozen=True)
class Vertex:
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    uv: tuple[float, float]

    def encode(self) -> bytes:
        return VERTEX_STRUCT.pack(
            *self.position, *self.normal, *self.uv
        )


@dataclass(frozen=True)
class TorusParameters:
    major_segments: int = TORUS_MAJOR_SEGMENTS
    minor_segments: int = TORUS_MINOR_SEGMENTS
    major_radius: float = 0.7
    minor_radius: float = 0.3

    @property
    def vertex_count(self) -> int:
        return self.major_segments * self.minor_segments

    @property
    def index_count(self) -> int:
        return self.major_segments * self.minor_segments * 6

    def validate(self) -> None:
        errors: list[dict[str, Any]] = []
        if not 3 <= self.major_segments <= 4096:
            errors.append(_diagnostic("invalid_torus_topology", "major_segments must be in 3..4096"))
        if not 3 <= self.minor_segments <= 4096:
            errors.append(_diagnostic("invalid_torus_topology", "minor_segments must be in 3..4096"))
        if not math.isfinite(self.major_radius) or self.major_radius <= 0.0:
            errors.append(_diagnostic("invalid_torus_radius", "major_radius must be finite and positive"))
        if not math.isfinite(self.minor_radius) or self.minor_radius <= 0.0:
            errors.append(_diagnostic("invalid_torus_radius", "minor_radius must be finite and positive"))
        if self.minor_radius >= self.major_radius:
            errors.append(_diagnostic("invalid_torus_radius", "minor_radius must be smaller than major_radius"))
        if errors:
            raise ProceduralError("invalid torus parameters", errors)


def torus_vertices(
    major_segments: int = TORUS_MAJOR_SEGMENTS,
    minor_segments: int = TORUS_MINOR_SEGMENTS,
    *,
    major_radius: float = 0.7,
    minor_radius: float = 0.3,
) -> tuple[Vertex, ...]:
    params = TorusParameters(
        max(3, int(major_segments)),
        max(3, int(minor_segments)),
        float(major_radius),
        float(minor_radius),
    )
    params.validate()
    vertices: list[Vertex] = []
    two_pi = 2.0 * math.pi
    for major in range(params.major_segments):
        u = (major / params.major_segments) * two_pi
        cos_u = math.cos(u)
        sin_u = math.sin(u)
        for minor in range(params.minor_segments):
            v = (minor / params.minor_segments) * two_pi
            cos_v = math.cos(v)
            sin_v = math.sin(v)
            radial = params.major_radius + params.minor_radius * cos_v
            position = (radial * cos_u, params.minor_radius * sin_v, radial * sin_u)
            normal = (cos_v * cos_u, sin_v, cos_v * sin_u)
            vertices.append(
                Vertex(
                    position,
                    normal,
                    (major / params.major_segments, minor / params.minor_segments),
                )
            )
    return tuple(vertices)


def torus_indices(
    major_segments: int = TORUS_MAJOR_SEGMENTS,
    minor_segments: int = TORUS_MINOR_SEGMENTS,
) -> tuple[int, ...]:
    params = TorusParameters(
        max(3, int(major_segments)), max(3, int(minor_segments))
    )
    params.validate()
    indices: list[int] = []
    for major in range(params.major_segments):
        next_major = (major + 1) % params.major_segments
        for minor in range(params.minor_segments):
            next_minor = (minor + 1) % params.minor_segments
            a = major * params.minor_segments + minor
            b = next_major * params.minor_segments + minor
            c = major * params.minor_segments + next_minor
            d = next_major * params.minor_segments + next_minor
            indices.extend((a, c, b, b, c, d))
    return tuple(indices)


def generate_torus_mesh(
    major_segments: int = TORUS_MAJOR_SEGMENTS,
    minor_segments: int = TORUS_MINOR_SEGMENTS,
    *,
    major_radius: float = 0.7,
    minor_radius: float = 0.3,
) -> "MeshData":
    vertices = torus_vertices(
        major_segments,
        minor_segments,
        major_radius=major_radius,
        minor_radius=minor_radius,
    )
    indices = torus_indices(major_segments, minor_segments)
    return MeshData(vertices=vertices, indices=indices)


# Convenient aliases used by downstream tooling and tests.
create_torus = generate_torus_mesh
torus_mesh = generate_torus_mesh
mesh_reference = generate_torus_mesh
noise2d_reference = generate_noise2d
generate_noise2d_samples = generate_noise2d


@dataclass(frozen=True)
class MeshData:
    vertices: tuple[Vertex, ...]
    indices: tuple[int, ...]

    @property
    def vertex_count(self) -> int:
        return len(self.vertices)

    @property
    def index_count(self) -> int:
        return len(self.indices)

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3

    def encode_vertices(self) -> bytes:
        return b"".join(vertex.encode() for vertex in self.vertices)

    def encode_indices(self) -> bytes:
        return b"".join(INDEX_STRUCT.pack(index) for index in self.indices)

    def validate(self) -> None:
        validate_mesh(self)


def validate_mesh(mesh: MeshData) -> None:
    errors: list[dict[str, Any]] = []
    if mesh.vertex_count < 3:
        errors.append(_diagnostic("invalid_mesh", "mesh needs at least three vertices"))
    if mesh.index_count == 0 or mesh.index_count % 3:
        errors.append(_diagnostic("invalid_mesh_topology", "index count must be a non-zero multiple of three"))
    for index in mesh.indices:
        if not 0 <= index < mesh.vertex_count:
            errors.append(_diagnostic("index_out_of_bounds", f"index {index} is outside vertex count {mesh.vertex_count}"))
            break
    # Closed torus topology: every directed edge is followed by a reverse edge
    # in another triangle, and Euler's characteristic is zero (genus one).
    directed: set[tuple[int, int]] = set()
    undirected: dict[tuple[int, int], int] = {}
    for offset in range(0, mesh.index_count, 3):
        triangle = mesh.indices[offset : offset + 3]
        for edge_index in range(3):
            a = triangle[edge_index]
            b = triangle[(edge_index + 1) % 3]
            if (a, b) in directed:
                errors.append(_diagnostic("duplicate_directed_edge", f"directed edge ({a}, {b}) appears twice"))
            directed.add((a, b))
            key = (a, b) if a < b else (b, a)
            undirected[key] = undirected.get(key, 0) + 1
    for (a, b), count in undirected.items():
        if count != 2:
            errors.append(_diagnostic("non_manifold_edge", f"edge ({a}, {b}) has {count} incident triangles"))
            break
    euler = mesh.vertex_count - len(undirected) + mesh.triangle_count
    if euler != 0:
        errors.append(_diagnostic("invalid_mesh_topology", f"Euler characteristic is {euler}, expected 0"))
    if errors:
        raise ProceduralError("invalid procedural mesh", errors)


def validate_torus_reference_tolerance(
    mesh: MeshData | None = None,
    *,
    major_segments: int = TORUS_MAJOR_SEGMENTS,
    minor_segments: int = TORUS_MINOR_SEGMENTS,
    major_radius: float = 0.7,
    minor_radius: float = 0.3,
    absolute_tolerance: float = 1e-5,
    relative_tolerance: float = 1e-4,
) -> dict[str, Any]:
    """Compare encoded torus data against an independent high-precision model.

    The reference is evaluated independently with complex exponentials and the
    implicit torus equation.  Comparing against the same ``generate_torus_mesh``
    implementation would make a broken generator appear perfect, so the actual
    encoded vertex bytes are decoded and checked here.
    """
    mesh = mesh or generate_torus_mesh(
        major_segments,
        minor_segments,
        major_radius=major_radius,
        minor_radius=minor_radius,
    )
    params = TorusParameters(
        max(3, int(major_segments)),
        max(3, int(minor_segments)),
        float(major_radius),
        float(minor_radius),
    )
    params.validate()
    if (
        mesh.vertex_count != params.vertex_count
        or mesh.index_count != params.index_count
    ):
        return {
            "within_tolerance": False,
            "reason": "vertex/index count differs from the reference",
        }
    # Independent reference: a circle of radius r swept around the Y axis at
    # radius R, expressed with complex exponentials rather than the generator's
    # cos/sin sequence.
    import cmath

    two_pi = 2.0 * math.pi
    reference: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for major in range(params.major_segments):
        ring = cmath.exp(1j * (two_pi * major / params.major_segments))
        for minor in range(params.minor_segments):
            tube = cmath.exp(1j * (two_pi * minor / params.minor_segments))
            radial = params.major_radius + params.minor_radius * tube.real
            reference.append(
                (
                    (radial * ring.real, params.minor_radius * tube.imag,
                     radial * ring.imag),
                    (tube.real * ring.real, tube.imag,
                     tube.real * ring.imag),
                )
            )
    encoded = mesh.encode_vertices()
    maximum_position = 0.0
    maximum_normal = 0.0
    maximum_surface_residual = 0.0
    for index, (expected_position, expected_normal) in enumerate(reference):
        fields = VERTEX_STRUCT.unpack_from(encoded, index * VERTEX_BYTES)
        actual_position = fields[0:3]
        actual_normal = fields[3:6]
        for actual_value, expected_value in zip(
            actual_position, expected_position
        ):
            maximum_position = max(
                maximum_position,
                abs(float32(actual_value) - expected_value),
            )
        for actual_value, expected_value in zip(
            actual_normal, expected_normal
        ):
            maximum_normal = max(
                maximum_normal,
                abs(float32(actual_value) - expected_value),
            )
        x, y, z = actual_position
        radial = math.hypot(x, z)
        residual = abs(
            (radial - params.major_radius) ** 2
            + y * y
            - params.minor_radius ** 2
        )
        maximum_surface_residual = max(maximum_surface_residual, residual)
    within = maximum_position <= absolute_tolerance and (
        maximum_normal <= absolute_tolerance
        or maximum_normal <= relative_tolerance * max(1.0, maximum_normal)
    ) and (
        maximum_surface_residual <= absolute_tolerance
        or maximum_surface_residual
        <= relative_tolerance * max(1.0, params.minor_radius ** 2)
    )
    return {
        "within_tolerance": within,
        "maximum_position_error": maximum_position,
        "maximum_normal_error": maximum_normal,
        "maximum_implicit_surface_residual": maximum_surface_residual,
        "absolute_tolerance": absolute_tolerance,
        "relative_tolerance": relative_tolerance,
        "vertex_count": mesh.vertex_count,
    }


@dataclass(frozen=True)
class InstanceParameters:
    count: int = INSTANCE_COUNT
    seed: int = 0x1A2B3C4D
    placement_radius: float = 3.0
    scale_min: float = 0.10
    scale_max: float = 0.20
    twist_radians: float = math.pi
    material_index: int = 0

    def validate(self) -> None:
        errors: list[dict[str, Any]] = []
        if not 1 <= self.count <= 4096:
            errors.append(_diagnostic("invalid_instance_count", "instance count must be in 1..4096"))
        if not 0 <= self.seed <= UINT32_MAX:
            errors.append(_diagnostic("invalid_seed", "instance seed must be uint32"))
        if not math.isfinite(self.placement_radius) or self.placement_radius <= 0.0:
            errors.append(_diagnostic("invalid_instance_placement", "placement radius must be positive"))
        if not (0.0 <= self.scale_min <= self.scale_max):
            errors.append(_diagnostic("invalid_instance_scale", "scale range must satisfy 0 <= min <= max"))
        if not math.isfinite(self.twist_radians):
            errors.append(_diagnostic("invalid_twist", "twist must be finite"))
        if errors:
            raise ProceduralError("invalid instance parameters", errors)


@dataclass(frozen=True)
class InstanceRecord:
    transform: tuple[float, ...]
    material_index: int = 0
    seed: int = 0
    flags: int = 0

    def encode(self) -> bytes:
        if len(self.transform) != 16:
            raise ProceduralError("instance transform must contain 16 floats")
        return INSTANCE_STRUCT.pack(
            *self.transform,
            _u32(self.material_index),
            _u32(self.seed),
            _u32(self.flags),
            0,
        )


def _rotation_y(angle: float) -> tuple[float, float]:
    return math.cos(angle), math.sin(angle)


def generate_instances(
    params: InstanceParameters | None = None,
) -> tuple[InstanceRecord, ...]:
    params = params or InstanceParameters()
    params.validate()
    golden_angle = 2.399963229728653
    records: list[InstanceRecord] = []
    for index in range(params.count):
        jitter = hash_unit(index, 0x51ED, params.seed)
        angle = index * golden_angle + jitter * 0.17
        radius = params.placement_radius * math.sqrt((index + 0.5) / params.count)
        scale_t = hash_unit(index, 0xA11C, params.seed)
        scale = params.scale_min + (params.scale_max - params.scale_min) * scale_t
        height = (hash_unit(index, 0xBEEF, params.seed) - 0.5) * 0.7
        cos_a, sin_a = _rotation_y(angle)
        tx = radius * cos_a
        ty = height
        tz = radius * sin_a
        # Row-major 4x4: Y rotation and uniform scale, translation in row 3.
        transform = (
            scale * cos_a, 0.0, -scale * sin_a, 0.0,
            0.0, scale, 0.0, 0.0,
            scale * sin_a, 0.0, scale * cos_a, 0.0,
            tx, ty, tz, 1.0,
        )
        records.append(
            InstanceRecord(
                transform=transform,
                material_index=params.material_index,
                seed=_u32(params.seed + index),
            )
        )
    return tuple(records)


# Common name used by the graph CPU reference.
place_instances = generate_instances


@dataclass(frozen=True)
class MaterialParameters:
    base_color: tuple[float, float, float, float] = (0.72, 0.78, 0.86, 1.0)
    roughness: float = 0.42
    metallic: float = 0.0
    specular: float = 0.5
    texture_index: int = 0

    def validate(self) -> None:
        errors: list[dict[str, Any]] = []
        if len(self.base_color) != 4:
            errors.append(_diagnostic("invalid_material", "base_color must have four components"))
        for name, value in (
            ("roughness", self.roughness),
            ("metallic", self.metallic),
            ("specular", self.specular),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                errors.append(_diagnostic("invalid_material", f"{name} must be finite and in [0, 1]"))
        if any(not math.isfinite(value) for value in self.base_color):
            errors.append(_diagnostic("invalid_material", "base_color components must be finite"))
        if not 0 <= self.texture_index <= UINT32_MAX:
            errors.append(_diagnostic("invalid_material", "texture_index must be uint32"))
        if errors:
            raise ProceduralError("invalid material parameters", errors)


@dataclass(frozen=True)
class MaterialRecord:
    base_color: tuple[float, float, float, float]
    roughness: float
    metallic: float
    specular: float
    texture_index: int = 0

    def encode(self) -> bytes:
        return MATERIAL_STRUCT.pack(
            *self.base_color,
            self.roughness,
            self.metallic,
            self.specular,
            _u32(self.texture_index),
        )


def generate_materials(
    params: MaterialParameters | None = None,
) -> tuple[MaterialRecord, ...]:
    params = params or MaterialParameters()
    params.validate()
    return (
        MaterialRecord(
            base_color=tuple(float(value) for value in params.base_color),
            roughness=float(params.roughness),
            metallic=float(params.metallic),
            specular=float(params.specular),
            texture_index=int(params.texture_index),
        ),
    )


def encode_light_record(
    direction: Sequence[float],
    color: Sequence[float],
) -> bytes:
    """Exact 32-byte directional-light record used by the GPU initializer."""
    if len(direction) != 3 or len(color) != 3:
        raise ProceduralError(
            "light direction and color must each have three components"
        )
    values = tuple(float(value) for value in (*direction, *color))
    if any(not math.isfinite(value) for value in values):
        raise ProceduralError("light direction/color must be finite")
    return LIGHT_STRUCT.pack(
        values[0], values[1], values[2], 0.0,
        values[3], values[4], values[5], 0.0,
    )


def encode_instances(records: Iterable[InstanceRecord]) -> bytes:
    return b"".join(record.encode() for record in records)


def encode_materials(records: Iterable[MaterialRecord]) -> bytes:
    return b"".join(record.encode() for record in records)


def indirect_args(
    index_count: int,
    instance_count: int,
    *,
    first_index: int = 0,
    base_vertex: int = 0,
    first_instance: int = 0,
) -> tuple[int, int, int, int, int]:
    for name, value in (
        ("index_count", index_count),
        ("instance_count", instance_count),
        ("first_index", first_index),
        ("base_vertex", base_vertex),
        ("first_instance", first_instance),
    ):
        if not 0 <= value <= UINT32_MAX:
            raise ProceduralError(f"indirect argument '{name}' is outside uint32")
    return (index_count, instance_count, first_index, base_vertex, first_instance)


def encode_indirect_args(
    index_count: int,
    instance_count: int,
    *,
    first_index: int = 0,
    base_vertex: int = 0,
    first_instance: int = 0,
) -> bytes:
    return INDIRECT_ARGS_STRUCT.pack(
        *indirect_args(
            index_count,
            instance_count,
            first_index=first_index,
            base_vertex=base_vertex,
            first_instance=first_instance,
        )
    )


def estimate_resources(
    *,
    noise: Noise2DParameters | None = None,
    torus: TorusParameters | None = None,
    instances: InstanceParameters | None = None,
    materials: MaterialParameters | None = None,
    width: int = SCENE_WIDTH,
    height: int = SCENE_HEIGHT,
) -> dict[str, Any]:
    noise = noise or Noise2DParameters()
    torus = torus or TorusParameters()
    instances = instances or InstanceParameters()
    materials = materials or MaterialParameters()
    noise.validate()
    torus.validate()
    instances.validate()
    materials.validate()
    texture_bytes = noise.width * noise.height * (
        16 if noise.format == "rgba32f" else 4
    )
    vertex_bytes = torus.vertex_count * VERTEX_BYTES
    index_bytes = torus.index_count * INDEX_BYTES
    instance_bytes = instances.count * INSTANCE_BYTES
    material_records = generate_materials(materials)
    material_bytes = len(material_records) * MATERIAL_BYTES
    bloom_width = max(1, width // 2)
    bloom_height = max(1, height // 2)
    scene_color_bytes = width * height * 16
    bloom_bytes = bloom_width * bloom_height * 16
    final_bytes = width * height * 16
    camera_curve_bytes = 48
    light_bytes = LIGHT_BYTES
    depth_bytes = width * height * 4
    parameter_buffer_bytes = 7 * 256
    total = (
        texture_bytes
        + vertex_bytes
        + index_bytes
        + instance_bytes
        + material_bytes
        + INDIRECT_ARGS_BYTES
        + scene_color_bytes
        + bloom_bytes
        + final_bytes
        + camera_curve_bytes
        + light_bytes
        + depth_bytes
        + parameter_buffer_bytes
    )
    return {
        "format": "nw06-resource-estimate",
        "version": 1,
        "texture": {
            "width": noise.width,
            "height": noise.height,
            "format": noise.format,
            "bytes": texture_bytes,
        },
        "vertices": {
            "count": torus.vertex_count,
            "stride_bytes": VERTEX_BYTES,
            "bytes": vertex_bytes,
            "layout": list(VERTEX_LAYOUT),
        },
        "indices": {
            "count": torus.index_count,
            "stride_bytes": INDEX_BYTES,
            "bytes": index_bytes,
        },
        "instances": {
            "count": instances.count,
            "stride_bytes": INSTANCE_BYTES,
            "bytes": instance_bytes,
            "layout": list(INSTANCE_LAYOUT),
        },
        "materials": {
            "count": len(material_records),
            "stride_bytes": MATERIAL_BYTES,
            "bytes": material_bytes,
            "layout": list(MATERIAL_LAYOUT),
        },
        "indirect_args_bytes": INDIRECT_ARGS_BYTES,
        "scene_color_bytes": scene_color_bytes,
        "bloom": {
            "width": bloom_width,
            "height": bloom_height,
            "resolution_scale": 0.5,
            "bytes": bloom_bytes,
        },
        "final_color_bytes": final_bytes,
        "camera_curve_bytes": camera_curve_bytes,
        "light_bytes": light_bytes,
        "depth_bytes": depth_bytes,
        "parameter_buffer_bytes": parameter_buffer_bytes,
        "resident_payload_bytes": total,
        "baked_asset_bytes": 0,
        "procedural_parameters_only": True,
    }


def validate_resource_budget(
    resources: dict[str, Any],
    *,
    byte_budget: int,
    memory_budget: int,
    package_bytes: int | None = None,
) -> None:
    errors: list[dict[str, Any]] = []
    if byte_budget <= 0:
        errors.append(_diagnostic("invalid_budget", "byte budget must be positive"))
    if memory_budget <= 0:
        errors.append(_diagnostic("invalid_budget", "memory budget must be positive"))
    payload = int(resources.get("resident_payload_bytes", 0))
    if package_bytes is not None and package_bytes > byte_budget:
        errors.append(
            _diagnostic(
                "byte_budget_exceeded",
                f"package bytes {package_bytes} exceed {byte_budget}",
            )
        )
    if payload > memory_budget:
        errors.append(
            _diagnostic(
                "memory_budget_exceeded",
                f"estimated resident payload {payload} exceeds {memory_budget}",
            )
        )
    if resources.get("baked_asset_bytes", 0) != 0:
        errors.append(_diagnostic("baked_asset_present", "baseline package must not bake procedural assets"))
    if errors:
        raise ProceduralError("procedural resource budget failure", errors)


def procedural_content_digest(
    *,
    noise: Noise2DParameters | None = None,
    torus: TorusParameters | None = None,
    instances: InstanceParameters | None = None,
    materials: MaterialParameters | None = None,
) -> str:
    """Compact, deterministic digest of procedural parameters (not baked data)."""
    noise = noise or Noise2DParameters()
    torus = torus or TorusParameters()
    instances = instances or InstanceParameters()
    materials = materials or MaterialParameters()
    payload = (
        "nw06-content-v1",
        noise.width,
        noise.height,
        noise.seed,
        noise.octaves,
        float(noise.scale),
        float(noise.lacunarity),
        float(noise.gain),
        noise.format,
        torus.major_segments,
        torus.minor_segments,
        float(torus.major_radius),
        float(torus.minor_radius),
        instances.count,
        instances.seed,
        float(instances.placement_radius),
        float(instances.scale_min),
        float(instances.scale_max),
        float(instances.twist_radians),
        instances.material_index,
        tuple(float(value) for value in materials.base_color),
        float(materials.roughness),
        float(materials.metallic),
        float(materials.specular),
        materials.texture_index,
    )
    return "sha256:v1:" + hashlib.sha256(repr(payload).encode("ascii")).hexdigest()


# Public shorthand aliases used by authoring and verification tooling.
Noise2D = Noise2DParameters
Torus = TorusParameters
InstanceTable = InstanceParameters
Material = MaterialParameters


__all__ = [
    "SCENE_WIDTH",
    "SCENE_HEIGHT",
    "NOISE_WIDTH",
    "NOISE_HEIGHT",
    "TORUS_MAJOR_SEGMENTS",
    "TORUS_MINOR_SEGMENTS",
    "INSTANCE_COUNT",
    "TICKS_PER_SECOND",
    "VERTEX_STRUCT",
    "VERTEX_BYTES",
    "INDEX_STRUCT",
    "INDEX_BYTES",
    "INSTANCE_STRUCT",
    "INSTANCE_BYTES",
    "MATERIAL_STRUCT",
    "MATERIAL_BYTES",
    "LIGHT_STRUCT",
    "LIGHT_BYTES",
    "INDIRECT_ARGS_STRUCT",
    "INDIRECT_ARGS_BYTES",
    "VERTEX_LAYOUT",
    "INSTANCE_LAYOUT",
    "MATERIAL_LAYOUT",
    "LIGHT_LAYOUT",
    "INDIRECT_ARGS_LAYOUT",
    "ProceduralError",
    "Noise2DParameters",
    "Noise2D",
    "TorusParameters",
    "Torus",
    "InstanceParameters",
    "InstanceTable",
    "InstanceRecord",
    "MaterialParameters",
    "Material",
    "MaterialRecord",
    "Vertex",
    "MeshData",
    "hash_u32",
    "hash_coord_u32",
    "hash_unit",
    "value_noise_2d",
    "sample_noise2d",
    "sample_noise2d_float32",
    "float32",
    "validate_noise2d_reference_tolerance",
    "generate_noise2d",
    "noise2d_rgba32f",
    "noise2d_rgba8_bytes",
    "noise2d_rgba32f_bytes",
    "noise2d_digest",
    "torus_vertices",
    "torus_indices",
    "generate_torus_mesh",
    "create_torus",
    "torus_mesh",
    "mesh_reference",
    "noise2d_reference",
    "generate_noise2d_samples",
    "validate_mesh",
    "validate_torus_reference_tolerance",
    "generate_instances",
    "place_instances",
    "generate_materials",
    "encode_light_record",
    "encode_instances",
    "encode_materials",
    "indirect_args",
    "encode_indirect_args",
    "estimate_resources",
    "validate_resource_budget",
    "procedural_content_digest",
]
