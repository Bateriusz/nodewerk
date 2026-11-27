#!/usr/bin/env python3
"""NW-13 bounded CPU reference renderer and image evidence helpers.

The renderer evaluates the same backend-neutral geometry/scene documents used
by the common and optional paths.  It is intentionally small: shadows are
bounded ray queries, reflections are at most one bounce, and particles use the
actual scene depth buffer.  Tests compare declared regions and tolerances rather
than treating this as a production GPU renderer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import nw13_geometry as geometry
import nw13_particles as particles
import nw13_scene as scene_module


class RenderError(ValueError):
    pass


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(v: Sequence[float]) -> float:
    return math.sqrt(_dot(v, v))


def _normalize(v: Sequence[float]) -> tuple[float, float, float]:
    length = _length(v)
    if length <= 1.0e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def invert_matrix(m: scene_module.Mat4) -> scene_module.Mat4:
    aug = [list(row) + [1.0 if row_index == column else 0.0 for column in range(4)] for row_index, row in enumerate(m)]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda row: abs(aug[row][column]))
        if abs(aug[pivot][column]) <= 1.0e-12:
            raise RenderError("world transform is singular")
        aug[column], aug[pivot] = aug[pivot], aug[column]
        divisor = aug[column][column]
        aug[column] = [value / divisor for value in aug[column]]
        for row in range(4):
            if row == column:
                continue
            factor = aug[row][column]
            if factor:
                aug[row] = [aug[row][index] - factor * aug[column][index] for index in range(8)]
    return tuple(tuple(row[4:]) for row in aug)  # type: ignore[return-value]


def transform_direction(m: scene_module.Mat4, direction: Sequence[float]) -> tuple[float, float, float]:
    return (
        m[0][0] * direction[0] + m[0][1] * direction[1] + m[0][2] * direction[2],
        m[1][0] * direction[0] + m[1][1] * direction[1] + m[1][2] * direction[2],
        m[2][0] * direction[0] + m[2][1] * direction[1] + m[2][2] * direction[2],
    )


@dataclass(frozen=True)
class Hit:
    distance: float
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    object_id: str
    material: str
    local_point: tuple[float, float, float]


@dataclass
class RenderObject:
    object_id: str
    node: Mapping[str, Any]
    world: scene_module.Mat4
    inverse: scene_module.Mat4
    center: tuple[float, float, float]
    radius: float
    material: str
    bounds: geometry.AABB


def _walk_nodes(node: Mapping[str, Any], index: dict[str, Mapping[str, Any]]) -> None:
    identifier = node.get("id")
    if isinstance(identifier, str):
        index[identifier] = node
    for child in node.get("children", []):
        if isinstance(child, Mapping):
            _walk_nodes(child, index)


class Renderer:
    def __init__(
        self,
        scene: scene_module.Scene,
        geometry_document: Mapping[str, Any],
        *,
        width: int = 48,
        height: int = 27,
        max_steps: int = 96,
        epsilon: float = 2.0e-3,
        max_distance: float = 48.0,
        samples: int = 1,
        shadows: bool = True,
        reflection_bounces: int = 1,
        miss_radiance: Sequence[float] = (0.035, 0.045, 0.065),
        backend: str = "common",
    ) -> None:
        if not 1 <= width <= 2048 or not 1 <= height <= 2048:
            raise RenderError("render dimensions are outside the bounded range")
        if not 1 <= max_steps <= 512:
            raise RenderError("ray march steps are outside the bounded range")
        if not 1 <= samples <= 8:
            raise RenderError("ray samples must be in 1..8")
        if not 0 <= reflection_bounces <= 1:
            raise RenderError("reflection bounces must be 0 or 1")
        if backend != "common":
            raise RenderError("optional film rendering requires the native GPU artifact")
        self.scene = scene
        self.geometry_index: dict[str, Mapping[str, Any]] = {}
        geometry.prepare(geometry_document)
        _walk_nodes(geometry_document["root"], self.geometry_index)
        self.geometry_bounds = {
            identifier: geometry.bounds_node(node)
            for identifier, node in self.geometry_index.items()
        }
        scene_module._check_scene_bounds(scene, {
            identifier: (bounds.minimum, bounds.maximum)
            for identifier, bounds in self.geometry_bounds.items()
        })
        self.width = width
        self.height = height
        self.max_steps = max_steps
        self.epsilon = epsilon
        self.max_distance = max_distance
        self.samples = samples
        self.shadows = shadows
        self.reflection_bounces = reflection_bounces
        self.backend = backend
        self.miss_radiance = tuple(float(value) for value in miss_radiance)
        self.objects: list[RenderObject] = []
        self.action_tick = 0
        self.objects_by_tick: dict[int, list[RenderObject]] = {}
        self.light_gain: Mapping[str, float] = {}

    def _objects_at(self, tick: int) -> list[RenderObject]:
        if tick in self.objects_by_tick:
            return self.objects_by_tick[tick]
        objects: list[RenderObject] = []
        for obj in self.scene.objects.values():
            node = self.geometry_index.get(obj.geometry)
            if node is None:
                raise RenderError(f"scene object '{obj.id}' references unknown geometry '{obj.geometry}'")
            world = scene_module.world_matrix(self.scene, obj.id, tick)
            inverse = invert_matrix(world)
            local_bounds = self.geometry_bounds[obj.geometry]
            local_center = local_bounds.center
            center = scene_module.transform_point(world, local_center)
            # Use the world-space distance from the transformed bound corners.
            radius = 0.0
            for x in (local_bounds.minimum[0], local_bounds.maximum[0]):
                for y in (local_bounds.minimum[1], local_bounds.maximum[1]):
                    for z in (local_bounds.minimum[2], local_bounds.maximum[2]):
                        radius = max(radius, _length(_sub(scene_module.transform_point(world, (x, y, z)), center)))
            objects.append(RenderObject(obj.id, node, world, inverse, center, radius, obj.material, local_bounds))
        self.objects_by_tick = {tick: objects}
        return objects

    def _local_ray(self, obj: RenderObject, origin: Sequence[float], direction: Sequence[float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        local_origin = scene_module.transform_point(obj.inverse, (origin[0], origin[1], origin[2]))
        local_direction = transform_direction(obj.inverse, direction)
        length = _length(local_direction)
        if length <= 1.0e-12:
            return local_origin, (0.0, 0.0, 0.0)
        return local_origin, (local_direction[0] / length, local_direction[1] / length, local_direction[2] / length)

    @staticmethod
    def _ray_sphere(origin: Sequence[float], direction: Sequence[float], center: Sequence[float], radius: float) -> tuple[float, float] | None:
        oc = _sub(origin, center)
        b = _dot(oc, direction)
        c = _dot(oc, oc) - radius * radius
        discriminant = b * b - c
        if discriminant < 0.0:
            return None
        root = math.sqrt(discriminant)
        return (-b - root, -b + root)

    def _march_node(self, node: Mapping[str, Any], origin: Sequence[float], direction: Sequence[float], max_distance: float) -> tuple[float, tuple[float, float, float]] | None:
        distance = 0.0
        point = (origin[0], origin[1], origin[2])
        for _ in range(self.max_steps):
            value = geometry.evaluate_node(node, point)
            if abs(value) <= self.epsilon:
                return distance, point
            step = max(abs(value) * 0.8, self.epsilon)
            distance += step
            if distance > max_distance:
                return None
            point = (
                point[0] + direction[0] * step,
                point[1] + direction[1] * step,
                point[2] + direction[2] * step,
            )
        return None

    def trace(self, origin: Sequence[float], direction: Sequence[float], *, ignore: str | None = None, max_distance: float | None = None) -> Hit | None:
        ray = _normalize(direction)
        if ray == (0.0, 0.0, 0.0):
            return None
        limit = self.max_distance if max_distance is None else min(self.max_distance, max_distance)
        best: Hit | None = None
        for obj in self._objects_at(self.action_tick):
            if ignore is not None and obj.object_id == ignore:
                continue
            sphere = self._ray_sphere(origin, ray, obj.center, obj.radius + self.epsilon)
            if sphere is None or sphere[1] <= 0.0:
                continue
            local_origin, local_direction = self._local_ray(obj, origin, ray)
            if local_direction == (0.0, 0.0, 0.0):
                continue
            # Normalizing the transformed ray makes its parameter local distance.
            raw_local = transform_direction(obj.inverse, ray)
            scale = _length(raw_local)
            local_limit = min(limit * scale, sphere[1] * scale + 1.0)
            march = self._march_node(obj.node, local_origin, local_direction, local_limit)
            if march is None:
                continue
            local_distance, local_point = march
            world_distance = local_distance / scale
            if world_distance >= limit or (best is not None and world_distance >= best.distance):
                continue
            normal = self._local_normal(obj.node, local_point)
            inverse_transpose = tuple(tuple(row) for row in zip(*obj.inverse[:3]))
            world_normal = _normalize(transform_direction(inverse_transpose, normal))  # type: ignore[arg-type]
            position = (
                origin[0] + ray[0] * world_distance,
                origin[1] + ray[1] * world_distance,
                origin[2] + ray[2] * world_distance,
            )
            best = Hit(world_distance, position, world_normal, obj.object_id, obj.material, local_point)
        return best

    def _local_normal(self, node: Mapping[str, Any], point: Sequence[float]) -> tuple[float, float, float]:
        delta = max(self.epsilon * 2.0, 1.0e-3)
        dx = geometry.evaluate_node(node, (point[0] + delta, point[1], point[2])) - geometry.evaluate_node(node, (point[0] - delta, point[1], point[2]))
        dy = geometry.evaluate_node(node, (point[0], point[1] + delta, point[2])) - geometry.evaluate_node(node, (point[0], point[1] - delta, point[2]))
        dz = geometry.evaluate_node(node, (point[0], point[1], point[2] + delta)) - geometry.evaluate_node(node, (point[0], point[1], point[2] - delta))
        return _normalize((dx, dy, dz))

    def _miss(self, direction: Sequence[float]) -> tuple[float, float, float]:
        # A stable directional sky component makes reflection misses measurable.
        up = max(0.0, direction[1])
        return (
            self.miss_radiance[0] + 0.10 * up,
            self.miss_radiance[1] + 0.14 * up,
            self.miss_radiance[2] + 0.22 * up,
        )

    def _shadowed(self, point: Sequence[float], light_position: Sequence[float], ignore: str) -> bool:
        if not self.shadows:
            return False
        to_light = _sub(light_position, point)
        distance = _length(to_light)
        if distance <= self.epsilon:
            return False
        direction = _normalize(to_light)
        origin = (
            point[0] + direction[0] * self.epsilon * 4.0,
            point[1] + direction[1] * self.epsilon * 4.0,
            point[2] + direction[2] * self.epsilon * 4.0,
        )
        hit = self.trace(origin, direction, ignore=ignore, max_distance=distance - self.epsilon * 2.0)
        return hit is not None

    def shade(self, hit: Hit, view_direction: Sequence[float], bounce: int = 0) -> tuple[float, float, float]:
        material = self.scene.materials[hit.material]
        normal = hit.normal
        color = [0.025 + material.base_color[0] * 0.04, 0.025 + material.base_color[1] * 0.04, 0.025 + material.base_color[2] * 0.04]
        for light in self.scene.lights.values():
            intensity = light.intensity * self.light_gain.get(light.id, 1.0)
            if intensity == 0.0:
                continue
            to_light = _sub(light.position, hit.position)
            distance = _length(to_light)
            if distance >= light.range:
                continue
            direction = _normalize(to_light)
            ndotl = max(0.0, _dot(normal, direction))
            if ndotl <= 0.0:
                continue
            falloff = (1.0 - distance / light.range) ** 2
            attenuation = falloff * intensity / (1.0 + distance * distance)
            shadow = 0.0 if self._shadowed(hit.position, light.position, hit.object_id) else 1.0
            for channel in range(3):
                color[channel] += material.base_color[channel] * light.color[channel] * ndotl * attenuation * shadow
            half_vector = _normalize((direction[0] - view_direction[0], direction[1] - view_direction[1], direction[2] - view_direction[2]))
            specular = max(0.0, _dot(normal, half_vector)) ** max(2.0, 64.0 * (1.0 - material.roughness))
            specular_strength = material.specular * (0.25 + material.metallic)
            for channel in range(3):
                color[channel] += specular * specular_strength * light.color[channel] * attenuation * shadow
        for channel in range(3):
            color[channel] += material.emission[channel]
        if bounce < self.reflection_bounces and material.metallic > 0.2:
            reflected = _normalize((
                view_direction[0] - 2.0 * _dot(view_direction, normal) * normal[0],
                view_direction[1] - 2.0 * _dot(view_direction, normal) * normal[1],
                view_direction[2] - 2.0 * _dot(view_direction, normal) * normal[2],
            ))
            origin = (
                hit.position[0] + reflected[0] * self.epsilon * 6.0,
                hit.position[1] + reflected[1] * self.epsilon * 6.0,
                hit.position[2] + reflected[2] * self.epsilon * 6.0,
            )
            reflected_hit = self.trace(origin, reflected, ignore=hit.object_id)
            if reflected_hit is not None:
                reflected_color = self.shade(reflected_hit, reflected, bounce + 1)
            else:
                reflected_color = self._miss(reflected)
            weight = material.metallic * (1.0 - material.roughness * 0.5)
            for channel in range(3):
                color[channel] = color[channel] * (1.0 - weight) + reflected_color[channel] * weight
        return (max(0.0, color[0]), max(0.0, color[1]), max(0.0, color[2]))

    def render(
        self,
        tick: int,
        *,
        action_tick: int | None = None,
        camera_tick: float | None = None,
        light_gain: Mapping[str, float] | None = None,
    ) -> tuple[list[float], list[float]]:
        self.action_tick = tick if action_tick is None else action_tick
        self.light_gain = light_gain or {}
        camera = scene_module.camera_state(self.scene, tick if camera_tick is None else camera_tick, action_tick=self.action_tick)
        forward = _normalize(camera.forward)
        right = _normalize(_cross(forward, camera.up))
        up = _normalize(_cross(right, forward))
        tangent = math.tan(math.radians(camera.fov_degrees) * 0.5)
        width, height = self.width, self.height
        image = [0.0] * (width * height * 4)
        depth = [math.inf] * (width * height)
        for y in range(height):
            ny = (1.0 - 2.0 * (y + 0.5) / height) * tangent
            for x in range(width):
                nx = (2.0 * (x + 0.5) / width - 1.0) * tangent * camera.aspect
                direction = _normalize((
                    forward[0] + right[0] * nx + up[0] * ny,
                    forward[1] + right[1] * nx + up[1] * ny,
                    forward[2] + right[2] * nx + up[2] * ny,
                ))
                view_projection = _dot(direction, forward)
                near_distance = camera.near / view_projection
                far_distance = min(camera.far / view_projection, self.max_distance)
                start = tuple(camera.position[i] + direction[i] * near_distance for i in range(3))
                hit = self.trace(start, direction, max_distance=far_distance - near_distance)
                pixel = (y * width + x) * 4
                if hit is None:
                    color = self._miss(direction)
                else:
                    color = self.shade(hit, direction)
                    depth[y * width + x] = (hit.distance + near_distance) * view_projection
                image[pixel] = color[0]
                image[pixel + 1] = color[1]
                image[pixel + 2] = color[2]
                image[pixel + 3] = 1.0
        return image, depth

    def depth_camera(self, tick: int, *, action_tick: int | None = None) -> particles.CameraDepth:
        camera = scene_module.camera_state(self.scene, tick, action_tick=action_tick)
        return particles.CameraDepth(
            position=camera.position,
            forward=camera.forward,
            up=camera.up,
            fov_degrees=camera.fov_degrees,
            near=camera.near,
            far=camera.far,
            aspect=camera.aspect,
            width=self.width,
            height=self.height,
        )

    def render_particles(
        self,
        image: Sequence[float],
        depth: Sequence[float],
        emitter_document: Mapping[str, Any],
        tick: int,
        *,
        action_tick: int | None = None,
        film_cycle: bool = False,
    ) -> list[float]:
        emitter = particles.compile_emitter(emitter_document)
        step = max(0, (tick if action_tick is None else action_tick) // particles.TICKS_PER_STEP)
        state = (particles.film_particles(emitter, step) if film_cycle else
                 particles.ParticleHistory(emitter, max_steps=240).advance_to(step))
        camera = self.depth_camera(tick, action_tick=action_tick)
        mask = [False] * len(state) if film_cycle else particles.occlusion_mask(state, camera, list(depth))
        tint = self.scene.materials[emitter.material].base_color if film_cycle else (0.65, 0.55, 0.45)
        rgba = [0.0] * len(image)
        for particle, occluded in zip(state, mask):
            if occluded:
                continue
            projected = camera.project(particle.position)
            if projected is None:
                continue
            px, py, z = projected
            radius = max(0.6, particle.size * self.width / max(0.1, z))
            for y in range(max(0, int(py - radius)), min(self.height, int(py + radius) + 1)):
                for x in range(max(0, int(px - radius)), min(self.width, int(px + radius) + 1)):
                    if z > depth[y * self.width + x] + emitter.depth_bias:
                        continue
                    dx = (x + 0.5 - px) / radius
                    dy = (y + 0.5 - py) / radius
                    strength = max(0.0, 1.0 - dx * dx - dy * dy)
                    if strength <= 0.0:
                        continue
                    index = (y * self.width + x) * 4
                    for channel in range(3):
                        rgba[index + channel] = max(rgba[index + channel], tint[channel] * (1.0 if film_cycle else strength))
                    rgba[index + 3] = max(rgba[index + 3], min(1.0, strength * 0.8))
        return particles.composite(image, rgba, width=self.width, height=self.height)


def render_backend(
    scene: scene_module.Scene,
    geometry_document: Mapping[str, Any],
    backend: str,
    **settings: Any,
) -> Renderer:
    """Construct the common or capability-qualified DXR reference path."""
    return Renderer(scene, geometry_document, backend=backend, **settings)


def to_ppm(image: Sequence[float], width: int, height: int, path: str | Path, *, exposure: float = 1.0) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bytearray(f"P6\n{width} {height}\n255\n".encode("ascii"))
    for pixel in range(width * height):
        base = pixel * 4
        for channel in range(3):
            value = max(0.0, image[base + channel] * exposure)
            value = value / (1.0 + value)
            value = value ** (1.0 / 2.2)
            payload.append(max(0, min(255, int(round(value * 255.0)))))
    path.write_bytes(bytes(payload))


def contact_sheet(frames: Sequence[Sequence[float]], width: int, height: int, columns: int, path: str | Path, *, exposure: float = 1.0) -> None:
    if not frames:
        raise RenderError("contact sheet requires at least one frame")
    columns = max(1, columns)
    rows = (len(frames) + columns - 1) // columns
    sheet_width = width * columns
    sheet_height = height * rows
    sheet = [0.0] * (sheet_width * sheet_height * 4)
    for index, frame in enumerate(frames):
        ox = (index % columns) * width
        oy = (index // columns) * height
        for y in range(height):
            for x in range(width):
                source = (y * width + x) * 4
                target = ((oy + y) * sheet_width + ox + x) * 4
                for channel in range(4):
                    sheet[target + channel] = frame[source + channel]
    to_ppm(sheet, sheet_width, sheet_height, path, exposure=exposure)


def luma(image: Sequence[float], width: int, height: int, x0: int, y0: int, x1: int, y1: int) -> float:
    total = 0.0
    count = 0
    for y in range(max(0, y0), min(height, y1)):
        for x in range(max(0, x0), min(width, x1)):
            base = (y * width + x) * 4
            total += 0.2126 * image[base] + 0.7152 * image[base + 1] + 0.0722 * image[base + 2]
            count += 1
    return total / max(1, count)


def image_difference(a: Sequence[float], b: Sequence[float]) -> dict[str, float]:
    if len(a) != len(b):
        raise RenderError("images have different sizes")
    maximum = 0.0
    total = 0.0
    for index in range(0, len(a), 4):
        for channel in range(3):
            delta = abs(a[index + channel] - b[index + channel])
            maximum = max(maximum, delta)
            total += delta * delta
    count = max(1, (len(a) // 4) * 3)
    return {"max_abs_channel_error": maximum, "rmse": math.sqrt(total / count)}

