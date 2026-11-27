#!/usr/bin/env python3
"""NW-13 bounded scene composition, animation, cameras and fixed-tick transport.

This module is deliberately separate from ``nw13_geometry``:

* geometry documents describe solid signed-distance recipes and CSG;
* mesh aggregation is the existing mesh-recipe/instance path;
* this module binds shared geometry to stable object identities, materials,
  local lights, parent-child transforms, typed tracks, camera shots and cuts.

All transforms are rigid TRS compositions.  Parenting is a scene hierarchy, not
a CSG operation, and every hierarchy is cycle-checked before evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


FORMAT = "nw13.scene"
SCHEMA_VERSION = 1
TICK_RATE_HZ = 48_000
SUPPORTED_INTERPOLATIONS = {"linear", "step", "smoothstep"}
SUPPORTED_TRACK_CHANNELS = {
    "transform.position": "vec3",
    "transform.rotation": "vec3",
    "transform.scale": "vec3",
}


class SceneError(ValueError):
    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "category": "scene_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


Vec3 = tuple[float, float, float]
Mat4 = tuple[tuple[float, float, float, float], ...]


def _vec3(value: Any, name: str) -> Vec3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise SceneError(f"{name} must be a vec3")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise SceneError(f"{name} must contain finite numbers")
        result.append(float(item))
    return tuple(result)  # type: ignore[return-value]


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SceneError(f"{name} must be a finite number")
    return float(value)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _length(values: Iterable[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _normalize(value: Vec3) -> Vec3:
    length = _length(value)
    if length <= 1.0e-12:
        raise SceneError("cannot normalize a zero vector")
    return tuple(component / length for component in value)  # type: ignore[return-value]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def euler_degrees_to_matrix(rotation: Vec3) -> Mat4:
    rx, ry, rz = (math.radians(value) for value in rotation)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    # Row-major Rz * Ry * Rx.
    return (
        (cy * cz, cz * sx * sy - cx * sz, cx * cz * sy + sx * sz, 0.0),
        (cy * sz, cx * cz + sx * sy * sz, -cz * sx + cx * sy * sz, 0.0),
        (-sy, cy * sx, cx * cy, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def transform_matrix(position: Vec3, rotation: Vec3, scale: Vec3) -> Mat4:
    r = euler_degrees_to_matrix(rotation)
    return tuple(
        (
            r[row][0] * scale[0],
            r[row][1] * scale[1],
            r[row][2] * scale[2],
            position[row] if row < 3 else 1.0,
        )
        for row in range(4)
    )  # type: ignore[return-value]


def multiply_matrix(a: Mat4, b: Mat4) -> Mat4:
    return tuple(
        tuple(sum(a[row][k] * b[k][column] for k in range(4)) for column in range(4))
        for row in range(4)
    )  # type: ignore[return-value]


def transform_point(matrix: Mat4, point: Vec3) -> Vec3:
    x = matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2] + matrix[0][3]
    y = matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2] + matrix[1][3]
    z = matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2] + matrix[2][3]
    return (x, y, z)


@dataclass(frozen=True)
class TrackKey:
    tick: int
    value: Vec3
    interpolation: str = "linear"


@dataclass
class TypedTrack:
    channel: str
    value_type: str
    keys: tuple[TrackKey, ...]

    def value_at(self, tick: float) -> Vec3:
        if not self.keys:
            raise SceneError(f"track '{self.channel}' has no keys")
        if tick <= self.keys[0].tick:
            return self.keys[0].value
        if tick >= self.keys[-1].tick:
            return self.keys[-1].value
        for index in range(1, len(self.keys)):
            previous = self.keys[index - 1]
            current = self.keys[index]
            if tick <= current.tick:
                if current.interpolation == "step" or current.tick == previous.tick:
                    return previous.value
                alpha = (tick - previous.tick) / (current.tick - previous.tick)
                if current.interpolation == "smoothstep":
                    alpha = _smoothstep(alpha)
                return tuple(_lerp(previous.value[i], current.value[i], alpha) for i in range(3))  # type: ignore[return-value]
        return self.keys[-1].value


@dataclass
class SceneObject:
    id: str
    geometry: str
    material: str
    parent: str | None
    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)
    tracks: dict[str, TypedTrack] = field(default_factory=dict)

    def local_components(self, tick: float) -> tuple[Vec3, Vec3, Vec3]:
        position = self.tracks["transform.position"].value_at(tick) if "transform.position" in self.tracks else self.position
        rotation = self.tracks["transform.rotation"].value_at(tick) if "transform.rotation" in self.tracks else self.rotation
        scale = self.tracks["transform.scale"].value_at(tick) if "transform.scale" in self.tracks else self.scale
        return position, rotation, scale

    def local_matrix(self, tick: float) -> Mat4:
        return transform_matrix(*self.local_components(tick))


@dataclass(frozen=True)
class Material:
    id: str
    base_color: tuple[float, float, float, float]
    roughness: float
    metallic: float
    specular: float
    emission: tuple[float, float, float]


@dataclass(frozen=True)
class Light:
    id: str
    type: str
    position: Vec3
    color: Vec3
    intensity: float
    range: float


@dataclass(frozen=True)
class CameraShot:
    id: str
    start_tick: int
    end_tick: int
    position: Vec3
    target: Vec3
    up: Vec3
    fov_degrees: float
    near: float
    far: float
    aspect: float
    position_track: TypedTrack | None = None
    target_track: TypedTrack | None = None
    transition_ticks: int = 0
    transition: str = "cut"


@dataclass(frozen=True)
class CameraState:
    position: Vec3
    target: Vec3
    up: Vec3
    forward: Vec3
    fov_degrees: float
    near: float
    far: float
    aspect: float
    shot_id: str
    transition_weight: float
    cut: bool


@dataclass
class Scene:
    duration_ticks: int
    objects: dict[str, SceneObject]
    materials: dict[str, Material]
    lights: dict[str, Light]
    shots: tuple[CameraShot, ...]
    cuts: tuple[int, ...]
    cues: tuple[dict[str, Any], ...]
    bounds_min: Vec3
    bounds_max: Vec3
    source: dict[str, Any]

    def contains_point(self, point: Vec3) -> bool:
        return all(self.bounds_min[i] <= point[i] <= self.bounds_max[i] for i in range(3))


def _parse_track(channel: str, doc: Mapping[str, Any], duration: int) -> TypedTrack:
    if channel not in SUPPORTED_TRACK_CHANNELS:
        raise SceneError(
            f"unsupported track channel '{channel}'",
            [_diagnostic("scene_unknown_track_channel", f"unsupported track channel '{channel}'", channel=channel)],
        )
    keys_doc = doc.get("keys")
    if not isinstance(keys_doc, list) or not 2 <= len(keys_doc) <= 4096:
        raise SceneError(
            f"track '{channel}' needs 2..4096 keys",
            [_diagnostic("scene_invalid_track", f"track '{channel}' needs 2..4096 keys", channel=channel)],
        )
    keys: list[TrackKey] = []
    previous_tick = -1
    for index, key in enumerate(keys_doc):
        if not isinstance(key, Mapping):
            raise SceneError(f"track '{channel}' key {index} must be an object")
        tick = key.get("tick")
        if isinstance(tick, bool) or not isinstance(tick, int) or not 0 <= tick <= duration:
            raise SceneError(
                f"track '{channel}' key {index} tick is outside the timeline",
                [_diagnostic("scene_invalid_track", "track key tick is outside the timeline", channel=channel, key=index)],
            )
        if tick < previous_tick:
            raise SceneError(
                f"track '{channel}' keys must be non-decreasing",
                [_diagnostic("scene_invalid_track", "track keys must be non-decreasing", channel=channel, key=index)],
            )
        previous_tick = tick
        interpolation = key.get("interpolation", "linear")
        if interpolation not in SUPPORTED_INTERPOLATIONS:
            raise SceneError(
                f"track '{channel}' interpolation '{interpolation}' is unsupported",
                [_diagnostic("scene_invalid_track", "unsupported interpolation", channel=channel, key=index)],
            )
        keys.append(TrackKey(tick, _vec3(key.get("value"), f"{channel}[{index}].value"), interpolation))
    return TypedTrack(channel, SUPPORTED_TRACK_CHANNELS[channel], tuple(keys))


def compile_scene(document: Mapping[str, Any]) -> Scene:
    if not isinstance(document, Mapping):
        raise SceneError("scene document must be an object")
    if document.get("format") != FORMAT or document.get("version") != SCHEMA_VERSION:
        raise SceneError(
            "scene must use nw13.scene version 1",
            [_diagnostic("scene_schema_version", "scene must use nw13.scene version 1")],
        )
    duration = document.get("duration_ticks")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= TICK_RATE_HZ * 3600:
        raise SceneError(
            "scene duration_ticks is outside the bounded timeline",
            [_diagnostic("scene_invalid_duration", "scene duration_ticks is outside the bounded timeline")],
        )
    tick_rate = document.get("tick_rate_hz", TICK_RATE_HZ)
    if tick_rate != TICK_RATE_HZ:
        raise SceneError(
            "the authoritative scene tick rate is 48000 Hz",
            [_diagnostic("scene_invalid_tick_rate", "the authoritative scene tick rate is 48000 Hz")],
        )

    materials_doc = document.get("materials", [])
    if not isinstance(materials_doc, list) or not materials_doc:
        raise SceneError("scene requires at least one material")
    materials: dict[str, Material] = {}
    for entry in materials_doc:
        if not isinstance(entry, Mapping):
            raise SceneError("material entries must be objects")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in materials:
            raise SceneError("material ids must be unique non-empty strings")
        base_doc = entry.get("base_color", [1, 1, 1, 1])
        if not isinstance(base_doc, (list, tuple)) or len(base_doc) not in {3, 4}:
            raise SceneError(f"material {identifier}.base_color must have 3 or 4 components")
        base = _vec3(list(base_doc[:3]), f"material {identifier}.base_color")
        alpha = _finite(base_doc[3] if len(base_doc) == 4 else 1.0, "alpha")
        if not 0.0 <= alpha <= 1.0:
            raise SceneError(f"material {identifier} alpha must be in [0, 1]")
        materials[identifier] = Material(
            id=identifier,
            base_color=(base[0], base[1], base[2], alpha),
            roughness=_finite(entry.get("roughness", 0.5), f"material {identifier}.roughness"),
            metallic=_finite(entry.get("metallic", 0.0), f"material {identifier}.metallic"),
            specular=_finite(entry.get("specular", 0.5), f"material {identifier}.specular"),
            emission=_vec3(entry.get("emission", [0, 0, 0]), f"material {identifier}.emission"),
        )
        if not 0.0 <= materials[identifier].roughness <= 1.0 or not 0.0 <= materials[identifier].metallic <= 1.0 or not 0.0 <= materials[identifier].specular <= 1.0:
            raise SceneError(f"material {identifier} reflectance values must be in [0, 1]")

    lights_doc = document.get("lights", [])
    lights: dict[str, Light] = {}
    for entry in lights_doc:
        if not isinstance(entry, Mapping):
            raise SceneError("light entries must be objects")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in lights:
            raise SceneError("light ids must be unique non-empty strings")
        light_range = _finite(entry.get("range", 10.0), f"light {identifier}.range")
        intensity = _finite(entry.get("intensity", 1.0), f"light {identifier}.intensity")
        if light_range <= 0.0 or intensity < 0.0:
            raise SceneError(f"light {identifier} has an invalid falloff range or intensity")
        lights[identifier] = Light(
            id=identifier,
            type=str(entry.get("type", "point")),
            position=_vec3(entry.get("position", [0, 0, 0]), f"light {identifier}.position"),
            color=_vec3(entry.get("color", [1, 1, 1]), f"light {identifier}.color"),
            intensity=intensity,
            range=light_range,
        )

    objects_doc = document.get("objects", [])
    if not isinstance(objects_doc, list) or not objects_doc:
        raise SceneError("scene requires at least one object")
    objects: dict[str, SceneObject] = {}
    for entry in objects_doc:
        if not isinstance(entry, Mapping):
            raise SceneError("object entries must be objects")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in objects:
            raise SceneError(
                "object ids must be unique non-empty strings",
                [_diagnostic("scene_duplicate_object", "object ids must be unique", object_id=identifier)],
            )
        transform = entry.get("transform", {})
        if not isinstance(transform, Mapping):
            raise SceneError(f"object {identifier}.transform must be an object")
        position = _vec3(transform.get("position", [0, 0, 0]), f"object {identifier}.position")
        rotation = _vec3(transform.get("rotation", [0, 0, 0]), f"object {identifier}.rotation")
        scale = _vec3(transform.get("scale", [1, 1, 1]), f"object {identifier}.scale")
        if any(component == 0.0 for component in scale):
            raise SceneError(
                f"object {identifier} has a degenerate scale",
                [_diagnostic("scene_invalid_transform", "object scale must be non-zero", object_id=identifier)],
            )
        material = entry.get("material")
        if not isinstance(material, str) or material not in materials:
            raise SceneError(
                f"object {identifier} references unknown material '{material}'",
                [_diagnostic("scene_unknown_material", "object references an unknown material", object_id=identifier)],
            )
        geometry = entry.get("geometry")
        if not isinstance(geometry, str) or not geometry:
            raise SceneError(f"object {identifier} requires a geometry reference")
        tracks_doc = entry.get("tracks", [])
        tracks: dict[str, TypedTrack] = {}
        if not isinstance(tracks_doc, list):
            raise SceneError(f"object {identifier}.tracks must be an array")
        for track_doc in tracks_doc:
            if not isinstance(track_doc, Mapping):
                raise SceneError("track entries must be objects")
            channel = track_doc.get("channel")
            if not isinstance(channel, str):
                raise SceneError("track channel must be a string")
            if channel in tracks:
                raise SceneError(f"object {identifier} declares duplicate track '{channel}'")
            tracks[channel] = _parse_track(channel, track_doc, duration)
        parent = entry.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise SceneError(
                f"object {identifier} parent must be a string or null",
                [_diagnostic("scene_unknown_parent", "object parent must be a string or null", object_id=identifier)],
            )
        objects[identifier] = SceneObject(
            id=identifier,
            geometry=geometry,
            material=str(material),
            parent=parent,
            position=position,
            rotation=rotation,
            scale=scale,
            tracks=tracks,
        )
    for obj in objects.values():
        if obj.parent is not None and obj.parent not in objects:
            raise SceneError(
                f"object {obj.id} references unknown parent '{obj.parent}'",
                [_diagnostic("scene_unknown_parent", "object references an unknown parent", object_id=obj.id)],
            )
    _check_hierarchy_cycles(objects)

    shots: list[CameraShot] = []
    shot_ids: set[str] = set()
    cameras_doc = document.get("cameras", [])
    if not isinstance(cameras_doc, list):
        raise SceneError("cameras must be an array")
    for camera in cameras_doc:
        if not isinstance(camera, Mapping):
            raise SceneError("camera entries must be objects")
        for shot_doc in camera.get("shots", []):
            if not isinstance(shot_doc, Mapping):
                raise SceneError("camera shots must be objects")
            shot_id = shot_doc.get("id")
            if not isinstance(shot_id, str) or not shot_id or shot_id in shot_ids:
                raise SceneError("camera shot ids must be non-empty strings")
            shot_ids.add(shot_id)
            start = shot_doc.get("start_tick", 0)
            end = shot_doc.get("end_tick", duration)
            if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
                raise SceneError("camera shot ticks must be integers")
            if not 0 <= start < end <= duration:
                raise SceneError(
                    f"camera shot {shot_id} interval is invalid",
                    [_diagnostic("scene_invalid_shot", "camera shot interval is invalid", shot_id=shot_id)],
                )
            projection = shot_doc.get("projection", {})
            if not isinstance(projection, Mapping):
                raise SceneError(f"camera shot {shot_id} projection must be an object")
            fov = _finite(projection.get("fov_degrees", 60.0), f"{shot_id}.fov")
            near = _finite(projection.get("near", 0.05), f"{shot_id}.near")
            far = _finite(projection.get("far", 100.0), f"{shot_id}.far")
            aspect = _finite(projection.get("aspect", 16.0 / 9.0), f"{shot_id}.aspect")
            if not 0.0 < fov < 180.0 or near <= 0.0 or far <= near or aspect <= 0.0:
                raise SceneError(
                    f"camera shot {shot_id} has invalid projection/clipping planes",
                    [_diagnostic("scene_invalid_camera", "invalid camera projection or clipping planes", shot_id=shot_id)],
                )
            transition = str(shot_doc.get("transition", "cut"))
            transition_ticks = shot_doc.get("transition_ticks", 0)
            if transition not in {"cut", "linear", "smoothstep"}:
                raise SceneError(f"camera shot {shot_id} has an unsupported transition")
            if isinstance(transition_ticks, bool) or not isinstance(transition_ticks, int) or transition_ticks < 0:
                raise SceneError(f"camera shot {shot_id} has a negative transition")
            if transition_ticks > end - start:
                raise SceneError(f"camera shot {shot_id} transition exceeds its interval")
            position_track = None
            target_track = None
            if "position_track" in shot_doc:
                position_track = _parse_track("transform.position", shot_doc["position_track"], duration)
            if "target_track" in shot_doc:
                target_track = _parse_track("transform.position", shot_doc["target_track"], duration)
            shots.append(
                CameraShot(
                    id=shot_id,
                    start_tick=start,
                    end_tick=end,
                    position=_vec3(shot_doc.get("position", [0, 0, 5]), f"{shot_id}.position"),
                    target=_vec3(shot_doc.get("target", [0, 0, 0]), f"{shot_id}.target"),
                    up=_vec3(shot_doc.get("up", [0, 1, 0]), f"{shot_id}.up"),
                    fov_degrees=fov,
                    near=near,
                    far=far,
                    aspect=aspect,
                    position_track=position_track,
                    target_track=target_track,
                    transition_ticks=transition_ticks,
                    transition=transition,
                )
            )
    if not shots:
        raise SceneError("scene requires at least one camera shot")
    shots.sort(key=lambda shot: (shot.start_tick, shot.id))

    cuts_doc = document.get("cuts", [])
    if not isinstance(cuts_doc, list) or any(
        isinstance(tick, bool) or not isinstance(tick, int) or not 0 <= tick <= duration
        for tick in cuts_doc
    ):
        raise SceneError("cuts must be an array of in-range integer ticks")
    cuts = tuple(sorted({int(tick) for tick in cuts_doc if isinstance(tick, int) and not isinstance(tick, bool)}))
    cues_doc = document.get("cues", [])
    if not isinstance(cues_doc, list):
        raise SceneError("cues must be an array")
    cues: list[dict[str, Any]] = []
    for cue in cues_doc:
        if not isinstance(cue, Mapping):
            raise SceneError("cue entries must be objects")
        cue_id = cue.get("id")
        tick = cue.get("tick")
        policy = cue.get("policy", "one-shot")
        if not isinstance(cue_id, str) or not cue_id or isinstance(tick, bool) or not isinstance(tick, int) or not 0 <= tick <= duration:
            raise SceneError("cue id/tick is invalid")
        if policy not in {"one-shot", "repeating"}:
            raise SceneError(
                f"cue {cue_id} has an unsupported policy '{policy}'",
                [_diagnostic("scene_invalid_cue", "unsupported cue policy", cue_id=cue_id)],
            )
        cues.append({"id": cue_id, "tick": tick, "policy": policy, "kind": str(cue.get("kind", "event"))})
    cues.sort(key=lambda cue: (cue["tick"], cue["id"]))

    bounds = document.get("bounds", {})
    if not isinstance(bounds, Mapping):
        raise SceneError("scene bounds must be an object")
    bounds_min = _vec3(bounds.get("minimum", [-100, -100, -100]), "scene.bounds.minimum")
    bounds_max = _vec3(bounds.get("maximum", [100, 100, 100]), "scene.bounds.maximum")
    if any(bounds_min[i] > bounds_max[i] for i in range(3)):
        raise SceneError("scene bounds minimum exceeds maximum")
    scene = Scene(
        duration_ticks=duration,
        objects=objects,
        materials=materials,
        lights=lights,
        shots=tuple(shots),
        cuts=cuts,
        cues=tuple(cues),
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        source=dict(document),
    )
    _check_scene_bounds(scene)
    return scene


def _check_hierarchy_cycles(objects: Mapping[str, SceneObject]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str, trail: list[str]) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            raise SceneError(
                "scene hierarchy contains a cycle: " + " -> ".join(trail + [identifier]),
                [_diagnostic("scene_hierarchy_cycle", "scene hierarchy contains a cycle", object_id=identifier, trail=trail + [identifier])],
            )
        visiting.add(identifier)
        parent = objects[identifier].parent
        if parent is not None:
            visit(parent, trail + [identifier])
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in objects:
        visit(identifier, [])


def _check_scene_bounds(scene: Scene) -> None:
    for obj in scene.objects.values():
        for track in obj.tracks.values():
            for key in track.keys:
                if track.channel == "transform.position":
                    if not scene.contains_point(key.value):
                        raise SceneError(
                            f"object {obj.id} track '{track.channel}' exceeds scene bounds",
                            [_diagnostic("scene_bounds_exceeded", "animated position exceeds scene bounds", object_id=obj.id, channel=track.channel)],
                        )
                elif track.channel == "transform.rotation":
                    if any(abs(component) > 3600.0 for component in key.value):
                        raise SceneError(
                            f"object {obj.id} track '{track.channel}' exceeds rotation bounds",
                            [_diagnostic("scene_bounds_exceeded", "animated rotation exceeds bounds", object_id=obj.id, channel=track.channel)],
                        )
                elif track.channel == "transform.scale":
                    if any(component <= 0.0 or component > 1000.0 for component in key.value):
                        raise SceneError(
                            f"object {obj.id} track '{track.channel}' exceeds scale bounds",
                            [_diagnostic("scene_bounds_exceeded", "animated scale exceeds bounds", object_id=obj.id, channel=track.channel)],
                        )
        if not scene.contains_point(obj.position):
            raise SceneError(
                f"object {obj.id} position exceeds scene bounds",
                [_diagnostic("scene_bounds_exceeded", "object position exceeds scene bounds", object_id=obj.id)],
            )
    for light in scene.lights.values():
        if not scene.contains_point(light.position):
            raise SceneError(
                f"light {light.id} position exceeds scene bounds",
                [_diagnostic("scene_bounds_exceeded", "light position exceeds scene bounds", light_id=light.id)],
            )
    for shot in scene.shots:
        if not scene.contains_point(shot.position) or not scene.contains_point(shot.target):
            raise SceneError(
                f"camera shot {shot.id} exceeds scene bounds",
                [_diagnostic("scene_bounds_exceeded", "camera pose exceeds scene bounds", shot_id=shot.id)],
            )
        for track in (shot.position_track, shot.target_track):
            if track is None:
                continue
            if any(not scene.contains_point(key.value) for key in track.keys):
                raise SceneError(
                    f"camera shot {shot.id} contains an out-of-bounds track",
                    [_diagnostic("scene_bounds_exceeded", "camera track exceeds scene bounds", shot_id=shot.id)],
                )


def world_matrix(scene: Scene, object_id: str, tick: float) -> Mat4:
    chain: list[SceneObject] = []
    current: str | None = object_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise SceneError(
                "scene hierarchy contains a cycle",
                [_diagnostic("scene_hierarchy_cycle", "scene hierarchy contains a cycle", object_id=current)],
            )
        seen.add(current)
        obj = scene.objects.get(current)
        if obj is None:
            raise SceneError(f"unknown object '{current}'")
        chain.append(obj)
        current = obj.parent
    matrix: Mat4 = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    for obj in reversed(chain):
        matrix = multiply_matrix(matrix, obj.local_matrix(tick))
    return matrix


def object_world_position(scene: Scene, object_id: str, tick: float) -> Vec3:
    return transform_point(world_matrix(scene, object_id, tick), (0.0, 0.0, 0.0))


def _shot_pose(shot: CameraShot, tick: float) -> tuple[Vec3, Vec3, Vec3]:
    position = shot.position_track.value_at(tick) if shot.position_track else shot.position
    target = shot.target_track.value_at(tick) if shot.target_track else shot.target
    up = shot.up
    return position, target, up


def _pose_state(shot: CameraShot, tick: float, transition_weight: float, cut: bool) -> CameraState:
    position, target, up = _shot_pose(shot, tick)
    forward = _sub(target, position)
    if _length(forward) <= 1.0e-9:
        raise SceneError(
            f"camera shot {shot.id} has a degenerate forward vector",
            [_diagnostic("scene_degenerate_camera", "camera position and target coincide", shot_id=shot.id)],
        )
    forward = _normalize(forward)
    if _length(_cross(forward, up)) <= 1.0e-9:
        raise SceneError(
            f"camera shot {shot.id} has a degenerate up vector",
            [_diagnostic("scene_degenerate_camera", "camera up is parallel to forward", shot_id=shot.id)],
        )
    return CameraState(
        position=position,
        target=target,
        up=up,
        forward=forward,
        fov_degrees=shot.fov_degrees,
        near=shot.near,
        far=shot.far,
        aspect=shot.aspect,
        shot_id=shot.id,
        transition_weight=transition_weight,
        cut=cut,
    )


def camera_state(scene: Scene, tick: float, *, action_tick: float | None = None) -> CameraState:
    """Resolve a camera pose at a presentation tick.

    ``action_tick`` is deliberately independent: a camera may move around
    frozen action, and the returned camera state never rewrites animation time.
    """
    if tick < 0:
        tick = 0.0
    active = None
    for shot in scene.shots:
        if shot.start_tick <= tick < shot.end_tick:
            active = shot
            break
    if active is None:
        active = scene.shots[-1]
    cut = active.start_tick in scene.cuts and tick < active.start_tick + 1
    current = _pose_state(active, tick, 0.0, cut)
    # Transition into this shot is evaluated at the shot start.
    if active.transition != "cut" and active.transition_ticks > 0:
        previous = None
        for shot in scene.shots:
            if shot.end_tick == active.start_tick:
                previous = shot
        if previous is not None and active.start_tick <= tick < active.start_tick + active.transition_ticks:
            alpha = (tick - active.start_tick) / active.transition_ticks
            if active.transition == "smoothstep":
                alpha = _smoothstep(alpha)
            before = _pose_state(previous, tick, 1.0 - alpha, False)
            blended_position = tuple(_lerp(before.position[i], current.position[i], alpha) for i in range(3))
            blended_target = tuple(_lerp(before.target[i], current.target[i], alpha) for i in range(3))
            forward = _normalize(_sub(blended_target, blended_position))  # type: ignore[arg-type]
            return CameraState(
                position=blended_position,  # type: ignore[arg-type]
                target=blended_target,  # type: ignore[arg-type]
                up=current.up,
                forward=forward,
                fov_degrees=current.fov_degrees,
                near=current.near,
                far=current.far,
                aspect=current.aspect,
                shot_id=current.shot_id,
                transition_weight=alpha,
                cut=False,
            )
    if action_tick is not None:
        # Validate that the requested action tick is finite without coupling it
        # to camera motion.
        _finite(action_tick, "action_tick")
    return current


@dataclass
class Transport:
    scene: Scene
    tick: int = 0
    direction: int = 1
    frozen: bool = False
    loop_start: int = 0
    loop_end: int | None = None
    generation: int = 0
    emitted: set[tuple[int, str]] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.loop_end is None:
            self.loop_end = self.scene.duration_ticks
        if not 0 <= self.loop_start < int(self.loop_end) <= self.scene.duration_ticks:
            raise SceneError("transport loop interval is invalid")

    def freeze(self) -> None:
        self.frozen = True
        self.events.append({"kind": "freeze", "tick": self.tick})

    def resume(self) -> None:
        self.frozen = False
        self.events.append({"kind": "resume", "tick": self.tick})

    def reverse(self) -> None:
        self.direction = -self.direction
        self.frozen = False
        self.events.append({"kind": "reverse", "tick": self.tick, "direction": self.direction})

    def seek(self, tick: int) -> None:
        if isinstance(tick, bool) or not isinstance(tick, int) or not self.loop_start <= tick <= int(self.loop_end or 0):
            raise SceneError("seek target is outside the transport interval")
        self.tick = tick
        self.events.append({"kind": "seek", "tick": tick})

    def _cues_at(self, tick: int) -> list[dict[str, Any]]:
        result = []
        for cue in self.scene.cues:
            if cue["tick"] != tick:
                continue
            key = (self.generation, cue["id"])
            if cue["policy"] == "one-shot" and key in self.emitted:
                continue
            if cue["policy"] == "one-shot":
                self.emitted.add(key)
            result.append({"kind": "cue", "cue_id": cue["id"], "tick": tick, "policy": cue["policy"], "generation": self.generation})
        return result

    def step(self, ticks: int = 1) -> list[dict[str, Any]]:
        if ticks <= 0:
            raise SceneError("transport step must be positive")
        if self.frozen:
            self.events.append({"kind": "frozen", "tick": self.tick})
            return list(self.events[-1:])
        produced: list[dict[str, Any]] = []
        for _ in range(ticks):
            target = self.tick + self.direction
            if self.direction > 0 and target > int(self.loop_end or 0):
                self.generation += 1
                self.emitted.clear()
                target = self.loop_start
                produced.append({"kind": "loop", "tick": target, "generation": self.generation})
            elif self.direction < 0 and target < self.loop_start:
                self.generation += 1
                self.emitted.clear()
                target = int(self.loop_end or 0)
                produced.append({"kind": "loop", "tick": target, "generation": self.generation})
            for cut in self.scene.cuts:
                if min(self.tick, target) < cut <= max(self.tick, target):
                    produced.append({"kind": "cut", "cut_tick": cut, "tick": target})
            self.tick = target
            produced.extend(self._cues_at(self.tick))
        self.events.extend(produced)
        return produced


def action_state(scene: Scene, tick: float) -> dict[str, Any]:
    """Evaluate scene action independently from camera time."""
    return {
        "tick": tick,
        "objects": {
            identifier: {
                "matrix": world_matrix(scene, identifier, tick),
                "position": object_world_position(scene, identifier, tick),
                "material": scene.objects[identifier].material,
            }
            for identifier in scene.objects
        },
    }


def validate_scene_document(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        compile_scene(document)
    except SceneError as exc:
        return list(exc.diagnostics)
    return []

