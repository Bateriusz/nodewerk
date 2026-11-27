#!/usr/bin/env python3
"""NW-13 seeded spatial particles, history reconstruction and image composition.

The fixed simulation cadence is inherited from NW-09: 120 Hz, exactly 400
timeline ticks per step.  The module is backend-neutral and provides a compact
CPU reference for authoring, seek/rewind reconstruction, depth occlusion,
feedback, bloom and tonemapped composition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


FORMAT = "nw13.particles"
SCHEMA_VERSION = 1
TICK_RATE_HZ = 48_000
SIMULATION_HZ = 120
TICKS_PER_STEP = TICK_RATE_HZ // SIMULATION_HZ


class ParticleError(ValueError):
    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result = {
        "category": "particle_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


def _number(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ParticleError(f"{name} must be finite")
    value = float(value)
    if minimum is not None and value < minimum:
        raise ParticleError(f"{name} is below its minimum")
    if maximum is not None and value > maximum:
        raise ParticleError(f"{name} is above its maximum")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ParticleError(f"{name} is outside [{minimum}, {maximum}]")
    return value


def _vec3(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ParticleError(f"{name} must be a vec3")
    return tuple(_number(item, f"{name}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _length(value: Sequence[float]) -> float:
    return math.sqrt(sum(component * component for component in value))


def _normalize(value: Sequence[float]) -> tuple[float, float, float]:
    length = _length(value)
    if length <= 1.0e-12:
        return (0.0, 1.0, 0.0)
    return tuple(component / length for component in value)  # type: ignore[return-value]


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(a[i] * b[i] for i in range(3))


def splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return (value ^ (value >> 31)) & 0xFFFFFFFFFFFFFFFF


def unit(seed: int, index: int, stream: int) -> float:
    return splitmix64((seed & 0xFFFFFFFF) ^ ((index + 1) * 0xD6E8FEB86659FD93) ^ (stream * 0xA24BAED4963EE407)) / 18446744073709551616.0


@dataclass(frozen=True)
class Emitter:
    id: str
    seed: int
    count: int
    origin: tuple[float, float, float]
    direction: tuple[float, float, float]
    speed_min: float
    speed_max: float
    spread_degrees: float
    gravity: tuple[float, float, float]
    drag: float
    lifetime_ticks: int
    size_start: float
    size_end: float
    material: str
    depth_bias: float = 0.0

    @property
    def lifetime_steps(self) -> int:
        return max(1, math.ceil(self.lifetime_ticks / TICKS_PER_STEP))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed": self.seed,
            "count": self.count,
            "origin": list(self.origin),
            "direction": list(self.direction),
            "speed_min": self.speed_min,
            "speed_max": self.speed_max,
            "spread_degrees": self.spread_degrees,
            "gravity": list(self.gravity),
            "drag": self.drag,
            "lifetime_ticks": self.lifetime_ticks,
            "size_start": self.size_start,
            "size_end": self.size_end,
            "material": self.material,
            "depth_bias": self.depth_bias,
        }


def compile_emitter(document: Mapping[str, Any]) -> Emitter:
    if not isinstance(document, Mapping):
        raise ParticleError("emitter must be an object")
    identifier = document.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ParticleError("emitter id must be a non-empty string")
    speed_min = _number(document.get("speed_min", 1.0), "speed_min", minimum=0.0, maximum=1.0e6)
    speed_max = _number(document.get("speed_max", speed_min), "speed_max", minimum=0.0, maximum=1.0e6)
    if speed_max < speed_min:
        raise ParticleError(
            "emitter speed_max is below speed_min",
            [_diagnostic("particles_invalid_emitter", "speed_max is below speed_min", emitter_id=identifier)],
        )
    return Emitter(
        id=identifier,
        seed=_integer(document.get("seed", 0), "seed", minimum=0, maximum=4294967295),
        count=_integer(document.get("count", 64), "count", minimum=1, maximum=65536),
        origin=_vec3(document.get("origin", [0, 0, 0]), "origin"),
        direction=_normalize(_vec3(document.get("direction", [0, 1, 0]), "direction")),
        speed_min=speed_min,
        speed_max=speed_max,
        spread_degrees=_number(document.get("spread_degrees", 15.0), "spread_degrees", minimum=0.0, maximum=180.0),
        gravity=_vec3(document.get("gravity", [0, -9.81, 0]), "gravity"),
        drag=_number(document.get("drag", 0.0), "drag", minimum=0.0, maximum=1.0),
        lifetime_ticks=_integer(document.get("lifetime_ticks", 4800), "lifetime_ticks", minimum=1, maximum=34560000),
        size_start=_number(document.get("size_start", 0.05), "size_start", minimum=0.0, maximum=1.0e6),
        size_end=_number(document.get("size_end", 0.01), "size_end", minimum=0.0, maximum=1.0e6),
        material=str(document.get("material", "default")),
        depth_bias=_number(document.get("depth_bias", 0.0), "depth_bias", minimum=-1.0e6, maximum=1.0e6),
    )


@dataclass(frozen=True)
class Particle:
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]
    age_steps: int
    lifetime_steps: int
    size: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": list(self.position),
            "velocity": list(self.velocity),
            "age_steps": self.age_steps,
            "lifetime_steps": self.lifetime_steps,
            "size": self.size,
        }


def _spawn(emitter: Emitter, index: int, step: int) -> Particle:
    direction = emitter.direction
    spread = math.radians(emitter.spread_degrees)
    if spread > 0.0:
        azimuth = unit(emitter.seed, index, 1) * math.tau
        polar = math.acos(1.0 - unit(emitter.seed, index, 2) * (1.0 - math.cos(spread)))
        # Build an orthonormal frame around the emitter direction.
        helper = (0.0, 1.0, 0.0) if abs(direction[1]) < 0.99 else (1.0, 0.0, 0.0)
        tangent = _normalize(_cross(helper, direction))
        bitangent = _cross(direction, tangent)
        sin_polar, cos_polar = math.sin(polar), math.cos(polar)
        direction = _normalize(
            tuple(
                direction[i] * cos_polar
                + tangent[i] * sin_polar * math.cos(azimuth)
                + bitangent[i] * sin_polar * math.sin(azimuth)
                for i in range(3)
            )
        )
    speed = emitter.speed_min + (emitter.speed_max - emitter.speed_min) * unit(emitter.seed, index, 3)
    velocity = tuple(direction[i] * speed for i in range(3))
    # Deterministic sub-frame phase offset keeps emitters spatially distributed.
    phase = (index + step) % emitter.count
    return Particle(
        position=emitter.origin,
        velocity=velocity,
        age_steps=phase % emitter.lifetime_steps,
        lifetime_steps=emitter.lifetime_steps,
        size=emitter.size_start,
    )


@dataclass
class ParticleHistory:
    """Bounded reconstruction history for reset, seek and rewind."""

    emitter: Emitter
    max_steps: int = 120 * 60
    max_memory_bytes: int = 32 * 1024 * 1024
    step: int = 0
    particles: tuple[Particle, ...] = ()
    checkpoints: dict[int, tuple[Particle, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_steps <= 0:
            raise ParticleError("history max_steps must be positive")
        self.reset()

    @property
    def memory_bytes(self) -> int:
        return len(self.checkpoints) * self.emitter.count * 80 + len(self.particles) * 80

    def reset(self) -> None:
        self.step = 0
        self.particles = tuple(_spawn(self.emitter, index, 0) for index in range(self.emitter.count))
        self.checkpoints = {0: self.particles}
        if self.memory_bytes > self.max_memory_bytes:
            raise ParticleError(
                "particle history memory bound exceeded",
                [_diagnostic("particles_memory_budget_exceeded", "particle history memory bound exceeded", bytes=self.memory_bytes)],
            )

    def _advance_one(self) -> None:
        dt = 1.0 / SIMULATION_HZ
        next_particles = []
        for index, particle in enumerate(self.particles):
            age = particle.age_steps + 1
            if age >= particle.lifetime_steps:
                next_particles.append(_spawn(self.emitter, index, self.step + 1))
                continue
            vx = particle.velocity[0] * (1.0 - self.emitter.drag * dt) + self.emitter.gravity[0] * dt
            vy = particle.velocity[1] * (1.0 - self.emitter.drag * dt) + self.emitter.gravity[1] * dt
            vz = particle.velocity[2] * (1.0 - self.emitter.drag * dt) + self.emitter.gravity[2] * dt
            position = (
                particle.position[0] + vx * dt,
                particle.position[1] + vy * dt,
                particle.position[2] + vz * dt,
            )
            ratio = age / particle.lifetime_steps
            size = self.emitter.size_start + (self.emitter.size_end - self.emitter.size_start) * ratio
            next_particles.append(Particle(position, (vx, vy, vz), age, particle.lifetime_steps, size))
        self.particles = tuple(next_particles)
        self.step += 1
        self.checkpoints[self.step] = self.particles
        # Keep only a bounded window of checkpoints.
        if len(self.checkpoints) > self.max_steps:
            for old in sorted(self.checkpoints)[: len(self.checkpoints) - self.max_steps]:
                del self.checkpoints[old]
        if self.memory_bytes > self.max_memory_bytes:
            raise ParticleError(
                "particle history memory bound exceeded",
                [_diagnostic("particles_memory_budget_exceeded", "particle history memory bound exceeded", bytes=self.memory_bytes)],
            )

    def advance_to(self, step: int) -> tuple[Particle, ...]:
        if step < 0:
            raise ParticleError("particle step must be non-negative")
        if step < self.step:
            return self.seek(step)
        while self.step < step:
            self._advance_one()
        return self.particles

    def seek(self, step: int) -> tuple[Particle, ...]:
        if step < 0:
            raise ParticleError("particle step must be non-negative")
        if step in self.checkpoints:
            self.step = step
            self.particles = self.checkpoints[step]
            return self.particles
        retained = sorted(self.checkpoints)
        candidate = next((value for value in reversed(retained) if value < step), None)
        if candidate is None:
            # The requested step precedes the retained checkpoint window.  The
            # history is bounded, so the origin itself may have been evicted;
            # reconstruct deterministically from step 0 instead of indexing a
            # missing checkpoint.  This keeps reset/seek/rewind bounded for long
            # timelines.
            self.reset()
            self.advance_to(step)
            return self.particles
        self.step = candidate
        self.particles = self.checkpoints[candidate]
        self.advance_to(step)
        return self.particles

    def rewind(self, steps: int) -> tuple[Particle, ...]:
        return self.seek(max(0, self.step - steps))


@dataclass(frozen=True)
class CameraDepth:
    position: tuple[float, float, float]
    forward: tuple[float, float, float]
    up: tuple[float, float, float]
    fov_degrees: float
    near: float
    far: float
    aspect: float
    width: int
    height: int

    def project(self, point: Sequence[float]) -> tuple[float, float, float] | None:
        forward = _normalize(self.forward)
        helper = self.up if abs(_dot(forward, _normalize(self.up))) < 0.999 else (1.0, 0.0, 0.0)
        right = _normalize(_cross(forward, helper))
        up = _cross(right, forward)
        relative = tuple(point[i] - self.position[i] for i in range(3))
        z = _dot(relative, forward)
        if z <= self.near or z >= self.far:
            return None
        x = _dot(relative, right) / z
        y = _dot(relative, up) / z
        tangent = math.tan(math.radians(self.fov_degrees) * 0.5)
        nx = x / (tangent * self.aspect)
        ny = y / tangent
        if not -1.0 <= nx <= 1.0 or not -1.0 <= ny <= 1.0:
            return None
        return ((nx + 1.0) * 0.5 * self.width, (1.0 - ny) * 0.5 * self.height, z)

    def linear_depth(self, z: float) -> float:
        return (z - self.near) / (self.far - self.near)


def occlusion_mask(
    particles: Sequence[Particle],
    camera: CameraDepth,
    scene_depth: Sequence[float] | Callable[[int, int], float],
) -> list[bool]:
    """Return True when a particle is occluded by the current scene depth."""
    mask = []
    for particle in particles:
        projected = camera.project(particle.position)
        if projected is None:
            mask.append(False)
            continue
        x = max(0, min(camera.width - 1, int(projected[0])))
        y = max(0, min(camera.height - 1, int(projected[1])))
        depth = scene_depth(x, y) if callable(scene_depth) else scene_depth[y * camera.width + x]
        mask.append(projected[2] > depth + 1.0e-6)
    return mask


def depth_metadata(camera: CameraDepth) -> dict[str, Any]:
    return {
        "type": "nw13.camera-depth.v1",
        "position": list(camera.position),
        "forward": list(camera.forward),
        "up": list(camera.up),
        "fov_degrees": camera.fov_degrees,
        "near": camera.near,
        "far": camera.far,
        "aspect": camera.aspect,
        "width": camera.width,
        "height": camera.height,
        "depth_convention": "positive_view_space_linear",
        "alpha_convention": "straight",
    }


def feedback(
    current: Sequence[float],
    previous: Sequence[float] | None,
    *,
    decay: float,
    gain: float,
    occluded: Sequence[bool] | None = None,
    discontinuity: bool = False,
) -> list[float]:
    """Composite feedback from the current image and prior scheduled state."""
    if len(current) == 0:
        return []
    if previous is None or discontinuity or len(previous) != len(current):
        previous = [0.0] * len(current)
    result = []
    for index, value in enumerate(current):
        reject = bool(occluded[index]) if occluded is not None and index < len(occluded) else False
        history = 0.0 if reject else previous[index] * decay
        result.append(value * gain + history)
    return result


def is_history_discontinuity(events: Sequence[Mapping[str, Any]]) -> bool:
    """Cuts, loops and seeks clear unrelated feedback history."""
    return any(event.get("kind") in {"cut", "loop", "seek"} for event in events)


def bloom(
    image: Sequence[float],
    width: int,
    height: int,
    *,
    threshold: float,
    intensity: float,
    radius: int,
) -> list[float]:
    if len(image) != width * height * 4:
        raise ParticleError("bloom image size does not match width*height*4")
    if radius < 0 or radius > 64:
        raise ParticleError("bloom radius is outside the bounded range")
    bright = [0.0] * len(image)
    for pixel in range(width * height):
        base = pixel * 4
        luminance = 0.2126 * image[base] + 0.7152 * image[base + 1] + 0.0722 * image[base + 2]
        if luminance > threshold:
            scale = (luminance - threshold) * intensity
            for channel in range(3):
                bright[base + channel] = image[base + channel] * scale
        bright[base + 3] = 1.0
    blurred = [0.0] * len(image)
    for y in range(height):
        for x in range(width):
            total = [0.0, 0.0, 0.0]
            count = 0
            for dy in range(-radius, radius + 1):
                py = y + dy
                if not 0 <= py < height:
                    continue
                for dx in range(-radius, radius + 1):
                    px = x + dx
                    if not 0 <= px < width:
                        continue
                    base = (py * width + px) * 4
                    for channel in range(3):
                        total[channel] += bright[base + channel]
                    count += 1
            base = (y * width + x) * 4
            for channel in range(3):
                blurred[base + channel] = total[channel] / max(1, count)
            blurred[base + 3] = 1.0
    return [max(0.0, image[i] + blurred[i]) for i in range(len(image))]


def tonemap(image: Sequence[float], exposure: float = 1.0, gamma: float = 2.2, operator: str = "reinhard") -> list[float]:
    if gamma <= 0.0:
        raise ParticleError("tonemap gamma must be positive")
    result = []
    for index in range(0, len(image), 4):
        for channel in range(3):
            value = max(0.0, image[index + channel] * exposure)
            if operator == "reinhard":
                value = value / (1.0 + value)
            elif operator == "aces":
                value = (value * (2.51 * value + 0.03)) / (value * (2.43 * value + 0.59) + 0.14)
                value = max(0.0, min(1.0, value))
            elif operator != "linear":
                raise ParticleError(f"unsupported tonemap operator '{operator}'")
            result.append(value ** (1.0 / gamma))
        result.append(1.0)
    return result


def composite(
    scene_image: Sequence[float],
    effect_image: Sequence[float],
    *,
    width: int,
    height: int,
    alpha_convention: str = "straight",
) -> list[float]:
    """Composite the actual scene and effect RGBA32F images."""
    expected = width * height * 4
    if len(scene_image) != expected or len(effect_image) != expected:
        raise ParticleError("composite images must both be width*height*4")
    if alpha_convention not in {"straight", "premultiplied"}:
        raise ParticleError("unsupported alpha convention")
    result = []
    for index in range(0, expected, 4):
        alpha = max(0.0, min(1.0, effect_image[index + 3]))
        if alpha_convention == "premultiplied":
            for channel in range(3):
                result.append(effect_image[index + channel] + scene_image[index + channel] * (1.0 - alpha))
        else:
            for channel in range(3):
                result.append(effect_image[index + channel] * alpha + scene_image[index + channel] * (1.0 - alpha))
        result.append(1.0)
    return result


def validate_document(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not isinstance(document, Mapping) or document.get("format") != FORMAT or document.get("version") != SCHEMA_VERSION:
        diagnostics.append(_diagnostic("particles_schema_version", "particle document must use nw13.particles version 1"))
        return diagnostics
    try:
        compile_emitter(document.get("emitter", {}))
    except ParticleError as exc:
        diagnostics.extend(exc.diagnostics or [_diagnostic("particles_invalid_emitter", str(exc))])
    budget = document.get("budget", {})
    if isinstance(budget, Mapping):
        memory = budget.get("max_memory_bytes", 32 * 1024 * 1024)
        if not isinstance(memory, int) or memory <= 0:
            diagnostics.append(_diagnostic("particles_invalid_budget", "max_memory_bytes must be positive"))
    return diagnostics

