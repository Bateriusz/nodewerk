#!/usr/bin/env python3
"""NW-09 explicit temporal state: fixed-step scheduling, history and checkpoints.

This module is the single temporal-state contract shared by the authoring
editor, the asynchronous preview runtime, the compiler/export path, and the
package extension.  It deliberately does not introduce a second graph model:
it consumes the authoritative ``nw03_compiler.GraphDocument`` and the NW-08
generation/device-epoch machinery.

Semantics
---------
* A History node runs at a fixed 120 Hz simulation cadence.  The authoritative
  timeline is 48,000 integer ticks/second, so every simulation tick is exactly
  400 timeline ticks.
* The previous-tick state is an **external input** to the current-tick DAG.  It
  is injected by :class:`TemporalRuntime`; it is never authored as an ordinary
  edge and does not create a graph cycle.
* All history nodes read the previous state snapshot for a tick, then commit
  their new state together when the tick completes.  A consumer can therefore
  never observe a half-written temporal tick.
* History values are never pure cache outputs.  They are not memoized as
  frame-independent constants, and checkpoint replay is explicit.

The runtime performs only deterministic scalar arithmetic with a declared
order.  It never uses floating-point atomic reductions and makes no claim that
GPU state is portable between devices or device lifetimes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

import nw03_compiler as nw03
import nwp_package as nwp


FIXED_STEP_HZ = nw03.NW09_FIXED_STEP_HZ
TIMELINE_TICKS_PER_SECOND = nw03.NW09_TIMELINE_TICKS_PER_SECOND
TIMELINE_TICKS_PER_SIMULATION_TICK = nw03.NW09_TIMELINE_TICKS_PER_SIMULATION_TICK
CHECKPOINT_VERSION = nw03.NW09_CHECKPOINT_VERSION
RESET_POLICIES = nw03.NW09_RESET_POLICIES
STATE_SEMANTICS = nw03.NW09_STATE_SEMANTICS


class TemporalError(RuntimeError):
    """Base error for temporal-state validation, scheduling, and replay."""

    def __init__(
        self,
        message: str,
        diagnostics: Sequence[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


class CheckpointError(TemporalError):
    """A checkpoint is malformed or incompatible with the current runtime."""


class ReplayError(TemporalError):
    """Replay failed; identifies the operator and exact simulation tick."""

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        operator_id: str,
        simulation_tick: int,
        diagnostics: Sequence[dict[str, Any]] | None = None,
    ):
        super().__init__(message, diagnostics)
        self.node_id = node_id
        self.operator_id = operator_id
        self.simulation_tick = int(simulation_tick)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": "temporal_replay",
            "severity": "error",
            "code": "temporal_replay_failed",
            "message": str(self),
            "node_id": self.node_id,
            "operator_id": self.operator_id,
            "simulation_tick": self.simulation_tick,
            "diagnostics": list(self.diagnostics),
        }


def sha256_value(value: Any) -> str:
    return (
        "sha256:v1:"
        + hashlib.sha256(nwp.canonical_json_bytes(value)).hexdigest()
    )


def simulation_tick_for_timeline_tick(timeline_tick: int) -> int:
    if isinstance(timeline_tick, bool) or not isinstance(timeline_tick, int):
        raise TemporalError("timeline tick must be an integer")
    if timeline_tick < 0:
        raise TemporalError("timeline tick must be non-negative")
    return timeline_tick // TIMELINE_TICKS_PER_SIMULATION_TICK


def timeline_tick_for_simulation_tick(simulation_tick: int) -> int:
    if isinstance(simulation_tick, bool) or not isinstance(simulation_tick, int):
        raise TemporalError("simulation tick must be an integer")
    if simulation_tick < 0:
        raise TemporalError("simulation tick must be non-negative")
    return simulation_tick * TIMELINE_TICKS_PER_SIMULATION_TICK


def _parse_canonical_initial(value: str) -> Any:
    text = value.strip()
    if not text:
        raise TemporalError("History initial value must not be empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fields: dict[str, str] = {}
    if ";" in text:
        for entry in text.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                raise TemporalError(
                    f"History initial value entry '{entry}' is not key=value"
                )
            key, raw = entry.split("=", 1)
            fields[key.strip()] = raw.strip()
        return fields
    raise TemporalError(
        "History initial value must be JSON or ';'-separated key=value fields"
    )


def _uint_hash(*values: int) -> int:
    """Deterministic 32-bit hash shared with the C++ oracle and History shader.

    The previous SHA-256-derived value could not be reproduced by the packaged
    GPU shader without implementing SHA-256 in HLSL.  This documented mix uses
    only uint32 wrap-around operations, so CPU, C++ and GPU evaluate the same
    per-element injection in declared order.
    """

    mask = 0xFFFFFFFF
    h = 0x9E3779B9
    for value in values:
        h ^= (
            (int(value) & mask)
            + 0x9E3779B9
            + ((h << 6) & mask)
            + (h >> 2)
        ) & mask
        h &= mask
    h ^= h >> 16
    h = (h * 0x7FEB352D) & mask
    h ^= h >> 15
    h = (h * 0x846CA68B) & mask
    h ^= h >> 16
    return h


def _unit_hash(*values: int) -> float:
    return (_uint_hash(*values) >> 8) / float(1 << 24)


@dataclass(frozen=True)
class HistorySpec:
    node_id: str
    operator_id: str
    state_type: str
    initial_value: str
    reset_policy: str
    step_rate_hz: int
    state_layout_hash: str
    values_per_element: int
    element_count: int
    params: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def state_bytes(self) -> int:
        # History state is a packed float32 raw buffer.  The resource
        # descriptor advertises DXGI_FORMAT_R32_UINT and exactly this many
        # bytes, so the declared layout and the executable allocation agree.
        return self.values_per_element * self.element_count * 4

    @property
    def external_previous_port(self) -> str:
        """Name of the scheduler-injected previous-tick input."""

        return "previous"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "operator_id": self.operator_id,
            "state_type": self.state_type,
            "initial_value": self.initial_value,
            "reset_policy": self.reset_policy,
            "step_rate_hz": self.step_rate_hz,
            "timeline_ticks_per_step": TIMELINE_TICKS_PER_SIMULATION_TICK,
            "state_layout_hash": self.state_layout_hash,
            "state_semantics": STATE_SEMANTICS,
            "state_component_type": "float32",
            "state_component_bytes": 4,
            "values_per_element": self.values_per_element,
            "element_count": self.element_count,
            "state_bytes": self.state_bytes,
        }


def _param_float(
    node: nw03.GraphNode, name: str, default: float
) -> float:
    tokens = node.params.get(name)
    if not tokens:
        return default
    try:
        return float(tokens[0])
    except (TypeError, ValueError):
        return default


def _param_int(node: nw03.GraphNode, name: str, default: int) -> int:
    tokens = node.params.get(name)
    if not tokens:
        return default
    try:
        return int(float(tokens[0]))
    except (TypeError, ValueError):
        return default


def _history_element_layout(
    node: nw03.GraphNode, declaration: nw03.HistoryDeclaration
) -> tuple[int, int]:
    parsed = _parse_canonical_initial(declaration.initial_value)
    fields = parsed if isinstance(parsed, Mapping) else {}
    if declaration.state_type == "nw.particles.v1":
        count = max(1, _declared_int(fields, "count", _param_int(node, "count", 8)))
        return 5, count
    if declaration.state_type == "nw.feedback.image.v1":
        width = max(
            1, _declared_int(fields, "width", _param_int(node, "width", 16))
        )
        height = max(
            1, _declared_int(fields, "height", _param_int(node, "height", 8))
        )
        return 4, width * height
    raise TemporalError(f"unknown History state type '{declaration.state_type}'")


def _declared_int(fields: Mapping[str, Any], name: str, default: int) -> int:
    raw = fields.get(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _declared_float(fields: Mapping[str, Any], name: str, default: float) -> float:
    raw = fields.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def history_specs(document: nw03.GraphDocument) -> tuple[HistorySpec, ...]:
    """Return declarations in deterministic node-id order."""

    specs: list[HistorySpec] = []
    errors = nw03.validate_graph_document(document)
    if any(item.get("severity") == "error" for item in errors):
        raise TemporalError(
            "graph is not valid for temporal scheduling", errors
        )
    for node_id in sorted(document.nodes):
        node = document.nodes[node_id]
        if node.frequency != "history":
            continue
        declaration = node.history
        if declaration is None:
            raise TemporalError(
                f"history node '{node_id}' has no History declaration"
            )
        values_per_element, element_count = _history_element_layout(
            node, declaration
        )
        specs.append(
            HistorySpec(
                node_id=node_id,
                operator_id=node.operator_id,
                state_type=declaration.state_type,
                initial_value=declaration.initial_value,
                reset_policy=declaration.reset_policy,
                step_rate_hz=declaration.step_rate_hz,
                state_layout_hash=declaration.state_layout_hash(),
                values_per_element=values_per_element,
                element_count=element_count,
                params={
                    name: tuple(values) for name, values in node.params.items()
                },
            )
        )
    return tuple(specs)


def document_has_history(document: nw03.GraphDocument) -> bool:
    return nw03.document_has_history(document)


def history_external_inputs(
    document: nw03.GraphDocument,
) -> dict[str, tuple[str, str, bool]]:
    """Expose previous-tick state as explicit external DAG inputs.

    Returns ``node_id -> (port, state_type, external)``.  These inputs never
    appear as ordinary authored edges and therefore never create a cycle.
    """

    return {
        spec.node_id: (spec.external_previous_port, spec.state_type, True)
        for spec in history_specs(document)
    }


@dataclass(frozen=True)
class TemporalState:
    node_id: str
    operator_id: str
    state_type: str
    simulation_tick: int
    values: tuple[float, ...]
    state_layout_hash: str
    loop_generation: int = 0

    def canonical_values(self) -> list[float]:
        return [float(value) for value in self.values]

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "operator_id": self.operator_id,
            "state_type": self.state_type,
            "simulation_tick": self.simulation_tick,
            "state_layout_hash": self.state_layout_hash,
            "loop_generation": self.loop_generation,
            "value_count": len(self.values),
            "values": self.canonical_values(),
        }

    def digest(self) -> str:
        return sha256_value(self.to_dict())


def _initial_values(
    spec: HistorySpec, node: nw03.GraphNode
) -> tuple[float, ...]:
    parsed = _parse_canonical_initial(spec.initial_value)
    if spec.state_type == "nw.particles.v1":
        if isinstance(parsed, list):
            values = tuple(float(value) for value in parsed)
            if len(values) != spec.element_count * 5:
                raise TemporalError(
                    "particle initial state length does not match count*5"
                )
            return values
        fields = parsed if isinstance(parsed, Mapping) else {}
        mode = str(fields.get("mode", "seeded")).lower()
        count = _declared_int(fields, "count", spec.element_count)
        if count != spec.element_count:
            raise TemporalError(
                "particle initial count does not match the declared state layout"
            )
        if mode == "zero" or parsed in (0, 0.0):
            return tuple(0.0 for _ in range(spec.element_count * 5))
        seed = _declared_int(fields, "seed", _param_int(node, "seed", 0))
        initial_velocity = _declared_float(
            fields, "initial_velocity", _param_float(node, "initial_velocity", 1.0)
        )
        values: list[float] = []
        for index in range(count):
            x = _unit_hash(seed, index, 1)
            y = _unit_hash(seed, index, 2)
            speed = initial_velocity * (0.5 + 0.5 * _unit_hash(seed, index, 4))
            # Use independent hash-derived direction components rather than
            # libm sin/cos.  This keeps every deterministic operation to IEEE
            # basic arithmetic so the fixed-tick state digest is identical on
            # Windows and Linux hosts.
            direction_x = 2.0 * _unit_hash(seed, index, 3) - 1.0
            direction_y = 2.0 * _unit_hash(seed, index, 5) - 1.0
            values.extend(
                [
                    x,
                    y,
                    speed * direction_x,
                    speed * direction_y,
                    0.0,
                ]
            )
        return tuple(values)
    if spec.state_type == "nw.feedback.image.v1":
        if isinstance(parsed, list):
            values = tuple(float(value) for value in parsed)
            if len(values) != spec.element_count * 4:
                raise TemporalError(
                    "feedback initial image length does not match width*height*4"
                )
            return values
        fields = parsed if isinstance(parsed, Mapping) else {}
        mode = str(fields.get("mode", "seeded")).lower()
        if mode == "zero" or parsed in (0, 0.0, "zero"):
            return tuple(0.0 for _ in range(spec.element_count * 4))
        width = _declared_int(fields, "width", _param_int(node, "width", 16))
        height = _declared_int(fields, "height", _param_int(node, "height", 8))
        if width * height != spec.element_count:
            raise TemporalError(
                "feedback initial dimensions do not match the declared state layout"
            )
        seed = _declared_int(fields, "seed", _param_int(node, "seed", 0))
        values = []
        for index in range(spec.element_count * 4):
            values.append(_unit_hash(seed, index, 7) * 0.01)
        return tuple(values)
    raise TemporalError(f"unknown History state type '{spec.state_type}'")


def _step_particles(
    spec: HistorySpec,
    node: nw03.GraphNode,
    previous: tuple[float, ...],
) -> tuple[float, ...]:
    count = spec.element_count
    parsed = _parse_canonical_initial(spec.initial_value)
    fields = parsed if isinstance(parsed, Mapping) else {}
    gravity = _declared_float(fields, "gravity", _param_float(node, "gravity", -0.5))
    width = max(1, _declared_int(fields, "width", _param_int(node, "width", 16)))
    height = max(1, _declared_int(fields, "height", _param_int(node, "height", 8)))
    dt = 1.0 / FIXED_STEP_HZ
    values = list(previous)
    for index in range(count):
        offset = index * 5
        x, y, vx, vy, age = values[offset : offset + 5]
        vy += gravity * dt
        x += vx * dt
        y += vy * dt
        age += 1.0
        if x < 0.0 or x >= width:
            vx = -vx
            x = min(max(x, 0.0), width - 1.0)
        if y < 0.0 or y >= height:
            vy = -vy
            y = min(max(y, 0.0), height - 1.0)
        values[offset : offset + 5] = [x, y, vx, vy, age]
    return tuple(values)


def _step_feedback(
    spec: HistorySpec,
    node: nw03.GraphNode,
    previous: tuple[float, ...],
    simulation_tick: int,
) -> tuple[float, ...]:
    parsed = _parse_canonical_initial(spec.initial_value)
    fields = parsed if isinstance(parsed, Mapping) else {}
    decay = _declared_float(fields, "decay", _param_float(node, "decay", 0.95))
    gain = _declared_float(fields, "gain", _param_float(node, "gain", 0.5))
    seed = _declared_int(fields, "seed", _param_int(node, "seed", 0))
    width = max(1, _declared_int(fields, "width", _param_int(node, "width", 16)))
    height = max(1, _declared_int(fields, "height", _param_int(node, "height", 8)))
    values = list(previous)
    # Deterministic per-element scalar update.  No reduction order is left to
    # the implementation: each output element has exactly one writer.
    for y in range(height):
        for x in range(width):
            base = (y * width + x) * 4
            injection = _unit_hash(seed, simulation_tick, x, y, 11)
            values[base] = decay * values[base] + gain * injection
            values[base + 1] = decay * values[base + 1] + gain * (1.0 - injection)
            values[base + 2] = decay * values[base + 2] + gain * (
                0.5 + 0.5 * injection
            )
            values[base + 3] = 1.0
    return tuple(values)


def step_history_state(
    spec: HistorySpec,
    node: nw03.GraphNode,
    previous: TemporalState,
    simulation_tick: int,
) -> TemporalState:
    if spec.state_type == "nw.particles.v1":
        values = _step_particles(spec, node, previous.values)
    elif spec.state_type == "nw.feedback.image.v1":
        values = _step_feedback(spec, node, previous.values, simulation_tick)
    else:
        raise TemporalError(f"unknown History state type '{spec.state_type}'")
    return TemporalState(
        node_id=spec.node_id,
        operator_id=spec.operator_id,
        state_type=spec.state_type,
        simulation_tick=simulation_tick,
        values=values,
        state_layout_hash=spec.state_layout_hash,
        loop_generation=previous.loop_generation,
    )


def initial_state_values(
    document: nw03.GraphDocument, spec: HistorySpec
) -> tuple[float, ...]:
    """Return the declared initial values as executable float32 state."""

    node = document.nodes[spec.node_id]
    values = _initial_values(spec, node)
    if len(values) != spec.element_count * spec.values_per_element:
        raise TemporalError(
            f"declared initial state for '{spec.node_id}' does not match its "
            "element layout"
        )
    return values


@dataclass(frozen=True)
class EventCursor:
    loop_generation: int = 0
    tick: int = -1
    sequence: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "loop_generation": self.loop_generation,
            "tick": self.tick,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class TemporalEvent:
    event_id: str
    tick: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tick": self.tick,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class DeviceLifetime:
    """Identity of one actual D3D12 device lifetime/recreation epoch."""

    lifetime_id: str
    epoch: int

    def __post_init__(self) -> None:
        if not self.lifetime_id:
            raise TemporalError("device lifetime id must not be empty")
        if self.epoch < 0:
            raise TemporalError("device epoch must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"lifetime_id": self.lifetime_id, "epoch": self.epoch}


@dataclass(frozen=True)
class Checkpoint:
    version: int
    tick: int
    simulation_tick: int
    semantic_digest: str
    state_layout_hash: str
    capability_profile_id: str
    capability_requirements_digest: str
    known_state_layout_hashes: tuple[str, ...]
    states: tuple[Mapping[str, Any], ...]
    event_cursor: Mapping[str, int]
    loop_generation: int
    device_lifetime: DeviceLifetime | None = None
    residency: str = "host"

    def byte_cost(self) -> int:
        return len(
            nwp.canonical_json_bytes(
                {
                    key: value
                    for key, value in self.to_dict().items()
                    if key != "byte_cost"
                }
            )
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "format": "nw09-checkpoint",
            "version": self.version,
            "tick": self.tick,
            "simulation_tick": self.simulation_tick,
            "semantic_digest": self.semantic_digest,
            "state_layout_hash": self.state_layout_hash,
            "capability_profile_id": self.capability_profile_id,
            "capability_requirements_digest": self.capability_requirements_digest,
            "known_state_layout_hashes": list(self.known_state_layout_hashes),
            "states": [dict(state) for state in self.states],
            "event_cursor": dict(self.event_cursor),
            "loop_generation": self.loop_generation,
            "device_lifetime": (
                self.device_lifetime.to_dict() if self.device_lifetime else None
            ),
            "residency": self.residency,
            "portable_gpu_state_claim": False,
        }
        payload["byte_cost"] = len(nwp.canonical_json_bytes(payload))
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Checkpoint":
        if payload.get("format") != "nw09-checkpoint":
            raise CheckpointError("not an NW-09 checkpoint")
        device = payload.get("device_lifetime")
        return cls(
            version=int(payload.get("version", 0)),
            tick=int(payload.get("tick", -1)),
            simulation_tick=int(payload.get("simulation_tick", -1)),
            semantic_digest=str(payload.get("semantic_digest", "")),
            state_layout_hash=str(payload.get("state_layout_hash", "")),
            capability_profile_id=str(payload.get("capability_profile_id", "")),
            capability_requirements_digest=str(
                payload.get("capability_requirements_digest", "")
            ),
            known_state_layout_hashes=tuple(
                str(item) for item in payload.get("known_state_layout_hashes", ())
            ),
            states=tuple(dict(item) for item in payload.get("states", ())),
            event_cursor=dict(payload.get("event_cursor", {})),
            loop_generation=int(payload.get("loop_generation", 0)),
            device_lifetime=(
                DeviceLifetime(str(device.get("lifetime_id", "")), int(device.get("epoch", 0)))
                if isinstance(device, Mapping)
                else None
            ),
            residency=str(payload.get("residency", "host")),
        )


@dataclass(frozen=True)
class CheckpointCompatibility:
    compatible: bool
    reason: str
    fields: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "reason": self.reason,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True)
class AdvanceResult:
    requested_timeline_tick: int
    processed_simulation_ticks: int
    pending_simulation_ticks: int
    presentation_timeline_tick: int
    lag_timeline_ticks: int
    events: tuple[TemporalEvent, ...]
    loop_generation: int

    @property
    def presentation_lagging(self) -> bool:
        return self.pending_simulation_ticks > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_timeline_tick": self.requested_timeline_tick,
            "processed_simulation_ticks": self.processed_simulation_ticks,
            "pending_simulation_ticks": self.pending_simulation_ticks,
            "presentation_timeline_tick": self.presentation_timeline_tick,
            "lag_timeline_ticks": self.lag_timeline_ticks,
            "presentation_lagging": self.presentation_lagging,
            "events": [event.to_dict() for event in self.events],
            "loop_generation": self.loop_generation,
        }


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    target_tick: int
    presentation_tick: int
    from_checkpoint: bool
    fallback_to_initial_state: bool
    events: tuple[TemporalEvent, ...]
    error: ReplayError | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "target_tick": self.target_tick,
            "presentation_tick": self.presentation_tick,
            "from_checkpoint": self.from_checkpoint,
            "fallback_to_initial_state": self.fallback_to_initial_state,
            "events": [event.to_dict() for event in self.events],
            "error": self.error.to_dict() if self.error else None,
        }


class TemporalRuntime:
    """Deterministic fixed-step runtime over history declarations."""

    def __init__(
        self,
        document: nw03.GraphDocument,
        *,
        semantic_digest: str | None = None,
        capability_profile_id: str | None = None,
        capability_requirements_digest: str | None = None,
        events: Sequence[TemporalEvent] = (),
        device_lifetime: DeviceLifetime | None = None,
    ):
        self.document = copy.deepcopy(document)
        self.specs = history_specs(self.document)
        if not self.specs:
            raise TemporalError(
                "temporal runtime requires at least one History declaration"
            )
        self._spec_by_id = {spec.node_id: spec for spec in self.specs}
        try:
            self.semantic_digest = semantic_digest or self.document.semantic_digest()
        except nw03.CompilerError:
            # Authoring-only hosts may have no native NW-02 helper.  This
            # identity is explicit and stable; production export still refuses
            # to substitute it for package identity.
            self.semantic_digest = sha256_value(
                {
                    "domain": "nw09.temporal.semantic.v1",
                    "source": self.document.source_text,
                }
            )
        self.capability_profile_id = (
            capability_profile_id or self.document.profile_id
        )
        self.capability_requirements_digest = (
            capability_requirements_digest
            or self.document.requirements_digest
        )
        self.events = tuple(sorted(events, key=lambda item: (item.tick, item.event_id)))
        self.device_lifetime = device_lifetime
        self._recovery_boundary_passed = device_lifetime is None
        self._states: dict[str, TemporalState] = {}
        self._timeline_tick = 0
        self._simulation_tick = 0
        self._loop_generation = 0
        self._event_cursor = EventCursor()
        self._emitted_event_keys: set[tuple[int, int, str]] = set()
        self._pending_events: list[TemporalEvent] = []
        self._reset_states()

    # -- configuration -----------------------------------------------------
    def _loop_ticks(self) -> int | None:
        for key in ("nw09_loop_ticks", "nw07_loop_ticks", "nw06_loop_ticks"):
            raw = self.document.settings.get(key)
            if raw is None:
                continue
            try:
                value = int(float(raw))
            except ValueError:
                continue
            if value > 0:
                return value
        return None

    @property
    def loop_ticks(self) -> int | None:
        return self._loop_ticks()

    @property
    def simulation_tick(self) -> int:
        return self._simulation_tick

    @property
    def timeline_tick(self) -> int:
        return self._timeline_tick

    @property
    def presentation_timeline_tick(self) -> int:
        return timeline_tick_for_simulation_tick(self._simulation_tick)

    @property
    def loop_generation(self) -> int:
        return self._loop_generation

    @property
    def event_cursor(self) -> EventCursor:
        return self._event_cursor

    def state(self, node_id: str) -> TemporalState:
        try:
            return self._states[node_id]
        except KeyError as exc:
            raise TemporalError(f"unknown History node '{node_id}'") from exc

    def external_inputs(self) -> dict[str, tuple[str, str, bool]]:
        return {
            spec.node_id: (spec.external_previous_port, spec.state_type, True)
            for spec in self.specs
        }

    def previous_state(self, node_id: str) -> TemporalState:
        """Return the committed previous-tick state used by the next step."""

        return self.state(node_id)

    def state_digest(self) -> str:
        return sha256_value(
            {
                "domain": "nw09.temporal.state.v1",
                "simulation_tick": self._simulation_tick,
                "loop_generation": self._loop_generation,
                "states": [
                    self._states[spec.node_id].to_dict() for spec in self.specs
                ],
            }
        )

    def presentation_values(self, node_id: str) -> tuple[float, ...]:
        return self.state(node_id).values

    # -- lifecycle ---------------------------------------------------------
    def _initial_state(
        self, spec: HistorySpec, simulation_tick: int = 0
    ) -> TemporalState:
        node = self.document.nodes[spec.node_id]
        return TemporalState(
            node_id=spec.node_id,
            operator_id=spec.operator_id,
            state_type=spec.state_type,
            simulation_tick=int(simulation_tick),
            values=_initial_values(spec, node),
            state_layout_hash=spec.state_layout_hash,
            loop_generation=self._loop_generation,
        )

    def _reset_states(
        self,
        node_ids: Iterable[str] | None = None,
        *,
        simulation_tick: int = 0,
    ) -> None:
        selected = (
            {spec.node_id for spec in self.specs}
            if node_ids is None
            else set(node_ids)
        )
        if not self._states:
            self._states = {}
        for spec in self.specs:
            if spec.node_id in selected:
                self._states[spec.node_id] = self._initial_state(
                    spec, simulation_tick
                )

    def reset(self) -> None:
        """Reset to declared initial state at tick zero."""

        self._timeline_tick = 0
        self._simulation_tick = 0
        self._loop_generation = 0
        self._event_cursor = EventCursor()
        self._emitted_event_keys.clear()
        self._pending_events.clear()
        self._reset_states()

    def initialize(self) -> None:
        self.reset()

    def reinitialize_device(self, lifetime: DeviceLifetime) -> None:
        """Advance the actual device lifetime and invalidate device state.

        Host checkpoints remain usable.  Device-resident checkpoints from the
        previous lifetime/epoch are rejected.  Recovery must pass through the
        explicit preview reinitialization boundary before replay may publish.
        """

        if (
            self.device_lifetime is not None
            and lifetime.lifetime_id == self.device_lifetime.lifetime_id
            and lifetime.epoch < self.device_lifetime.epoch
        ):
            raise TemporalError("device epoch must not move backwards")
        self.device_lifetime = lifetime
        self._recovery_boundary_passed = False

    def confirm_preview_reinitialization(self) -> None:
        """Cross the explicit NW-08 preview reinitialization boundary."""

        if self.device_lifetime is None:
            raise TemporalError("no device lifetime is bound")
        self._recovery_boundary_passed = True

    def _require_recovery_boundary(self, *, from_device_state: bool) -> None:
        if from_device_state and not self._recovery_boundary_passed:
            raise CheckpointError(
                "device-resident replay requires the explicit NW-08 preview "
                "reinitialization boundary"
            )

    # -- advancement -------------------------------------------------------
    def _advance_one_tick(self) -> None:
        next_tick = self._simulation_tick + 1
        loop_ticks = self._loop_ticks()
        loop_sim_ticks = (
            loop_ticks // TIMELINE_TICKS_PER_SIMULATION_TICK
            if loop_ticks is not None
            and loop_ticks % TIMELINE_TICKS_PER_SIMULATION_TICK == 0
            else 0
        )
        loop_reset_nodes = {
            spec.node_id
            for spec in self.specs
            if spec.reset_policy == "on-loop"
        }
        loop_boundary = (
            loop_reset_nodes
            and loop_sim_ticks > 0
            and next_tick % loop_sim_ticks == 0
        )
        if loop_boundary:
            # The authoritative loop boundary is a reset tick.  The runtime
            # commits the declared initial state for on-loop nodes and still
            # advances every other node through this tick.
            self._events_through(loop_ticks or 0)
            self._loop_generation += 1
            self._event_cursor = EventCursor(
                loop_generation=self._loop_generation, tick=-1, sequence=0
            )
        previous = dict(self._states)
        updated: dict[str, TemporalState] = {}
        for spec in self.specs:
            if loop_boundary and spec.node_id in loop_reset_nodes:
                updated[spec.node_id] = self._initial_state(spec, next_tick)
                continue
            prior = previous[spec.node_id]
            if (
                spec.reset_policy in {"never", "manual"}
                and prior.simulation_tick >= next_tick
            ):
                # A manual/never state recorded ahead of the replayed global
                # tick remains frozen until replay catches up to it.  It is
                # never stepped as though its old value belonged to tick zero.
                updated[spec.node_id] = prior
                continue
            node = self.document.nodes[spec.node_id]
            try:
                updated[spec.node_id] = step_history_state(
                    spec, node, prior, next_tick
                )
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise ReplayError(
                    f"history operator '{spec.operator_id}' failed at "
                    f"simulation tick {next_tick}",
                    node_id=spec.node_id,
                    operator_id=spec.operator_id,
                    simulation_tick=next_tick,
                    diagnostics=[{"code": "evaluation_failure", "message": str(exc)}],
                ) from exc
        # Commit only after every history node for this tick succeeded.  A
        # failed replay therefore leaves the previous valid state untouched.
        self._states = updated
        self._simulation_tick = next_tick

    def _events_through(self, target_tick: int) -> tuple[TemporalEvent, ...]:
        emitted: list[TemporalEvent] = []
        start = self._event_cursor.tick
        sequence = self._event_cursor.sequence
        for event in self.events:
            if event.tick <= start or event.tick > target_tick:
                continue
            key = (self._loop_generation, event.tick, event.event_id)
            if key in self._emitted_event_keys:
                continue
            self._emitted_event_keys.add(key)
            emitted.append(event)
            self._pending_events.append(event)
            sequence += 1
        self._event_cursor = EventCursor(
            loop_generation=self._loop_generation,
            tick=max(start, target_tick),
            sequence=sequence,
        )
        return tuple(emitted)

    def _local_timeline_tick(self) -> int:
        loop_ticks = self._loop_ticks()
        if loop_ticks is not None and loop_ticks > 0:
            return (self._simulation_tick * TIMELINE_TICKS_PER_SIMULATION_TICK) % loop_ticks
        return self._simulation_tick * TIMELINE_TICKS_PER_SIMULATION_TICK

    def advance_to(
        self,
        timeline_tick: int,
        *,
        work_cap: int | None = None,
    ) -> AdvanceResult:
        """Process every required simulation tick in order.

        ``work_cap`` may limit how many simulation ticks are processed by this
        call.  Remaining required ticks stay scheduled; they are never dropped.
        """

        if isinstance(work_cap, bool):
            raise TemporalError("work cap must be an integer")
        if work_cap is not None and work_cap < 0:
            raise TemporalError("work cap must be non-negative")
        if timeline_tick < 0:
            raise TemporalError("timeline tick must be non-negative")
        target_sim = simulation_tick_for_timeline_tick(timeline_tick)
        processed = 0
        while self._simulation_tick < target_sim:
            if work_cap is not None and processed >= work_cap:
                break
            self._advance_one_tick()
            processed += 1
        self._timeline_tick = max(
            self._timeline_tick, self._simulation_tick * TIMELINE_TICKS_PER_SIMULATION_TICK
        )
        pending = max(0, target_sim - self._simulation_tick)
        self._events_through(self._local_timeline_tick())
        emitted = tuple(self._pending_events)
        self._pending_events.clear()
        return AdvanceResult(
            requested_timeline_tick=timeline_tick,
            processed_simulation_ticks=processed,
            pending_simulation_ticks=pending,
            presentation_timeline_tick=self.presentation_timeline_tick,
            lag_timeline_ticks=max(
                0, timeline_tick - self.presentation_timeline_tick
            ),
            events=emitted,
            loop_generation=self._loop_generation,
        )

    def advance_frames(
        self,
        frame_ticks: Sequence[int],
        *,
        work_cap: int | None = None,
    ) -> tuple[AdvanceResult, ...]:
        """Advance over irregular presentation frames without dropping ticks."""

        return tuple(
            self.advance_to(tick, work_cap=work_cap) for tick in frame_ticks
        )

    # -- checkpoints -------------------------------------------------------
    def checkpoint(
        self,
        *,
        residency: str = "host",
    ) -> Checkpoint:
        if residency not in {"host", "device"}:
            raise CheckpointError("checkpoint residency must be host or device")
        device = self.device_lifetime if residency == "device" else None
        if residency == "device" and device is None:
            raise CheckpointError("device checkpoint requires a device lifetime")
        states = tuple(
            self._states[spec.node_id].to_dict() for spec in self.specs
        )
        return Checkpoint(
            version=CHECKPOINT_VERSION,
            tick=self.presentation_timeline_tick,
            simulation_tick=self._simulation_tick,
            semantic_digest=self.semantic_digest,
            state_layout_hash=sha256_value(
                {
                    "domain": "nw09.state-layout-set.v1",
                    "layouts": sorted(
                        spec.state_layout_hash for spec in self.specs
                    ),
                }
            ),
            capability_profile_id=self.capability_profile_id,
            capability_requirements_digest=self.capability_requirements_digest,
            known_state_layout_hashes=tuple(
                sorted(spec.state_layout_hash for spec in self.specs)
            ),
            states=states,
            event_cursor=self._event_cursor.to_dict(),
            loop_generation=self._loop_generation,
            device_lifetime=device,
            residency=residency,
        )

    def checkpoint_compatibility(
        self,
        checkpoint: Checkpoint,
        *,
        target_tick: int | None = None,
    ) -> CheckpointCompatibility:
        fields: dict[str, Any] = {}
        reasons: list[str] = []

        def check(name: str, actual: Any, expected: Any) -> None:
            fields[name] = {"actual": actual, "expected": expected}
            if actual != expected:
                reasons.append(name)

        check("version", checkpoint.version, CHECKPOINT_VERSION)
        fields["tick"] = {
            "actual": checkpoint.tick,
            "target": target_tick,
        }
        if (
            checkpoint.tick < 0
            or checkpoint.tick % TIMELINE_TICKS_PER_SIMULATION_TICK != 0
            or checkpoint.simulation_tick
            != simulation_tick_for_timeline_tick(checkpoint.tick)
        ):
            reasons.append("tick")
        if target_tick is None:
            # Standalone compatibility is an exact checkpoint-identity check:
            # the checkpoint tick must describe the runtime's committed tick.
            if checkpoint.tick != self.presentation_timeline_tick:
                reasons.append("tick")
        elif checkpoint.tick > target_tick:
            reasons.append("tick_ahead_of_target")
        check("semantic_digest", checkpoint.semantic_digest, self.semantic_digest)
        expected_layouts = tuple(sorted(spec.state_layout_hash for spec in self.specs))
        check(
            "state_layout_hash",
            checkpoint.state_layout_hash,
            sha256_value(
                {
                    "domain": "nw09.state-layout-set.v1",
                    "layouts": list(expected_layouts),
                }
            ),
        )
        check(
            "capability_profile_id",
            checkpoint.capability_profile_id,
            self.capability_profile_id,
        )
        check(
            "capability_requirements_digest",
            checkpoint.capability_requirements_digest,
            self.capability_requirements_digest,
        )
        check(
            "known_state_layout_hashes",
            checkpoint.known_state_layout_hashes,
            expected_layouts,
        )
        if checkpoint.residency == "device":
            check(
                "device_lifetime",
                checkpoint.device_lifetime.to_dict()
                if checkpoint.device_lifetime
                else None,
                self.device_lifetime.to_dict() if self.device_lifetime else None,
            )
        return CheckpointCompatibility(
            compatible=not reasons,
            reason=(
                "compatible"
                if not reasons
                else "incompatible checkpoint fields: " + ", ".join(reasons)
            ),
            fields=fields,
        )

    def restore_checkpoint(
        self, checkpoint: Checkpoint, *, allow_device_replay: bool = False
    ) -> CheckpointCompatibility:
        compatibility = self.checkpoint_compatibility(
            checkpoint, target_tick=checkpoint.tick
        )
        if not compatibility.compatible:
            raise CheckpointError(compatibility.reason, [compatibility.to_dict()])
        if checkpoint.residency == "device" and not allow_device_replay:
            raise CheckpointError(
                "device-resident checkpoint restoration must be explicit"
            )
        self._require_recovery_boundary(
            from_device_state=checkpoint.residency == "device"
        )
        restored: dict[str, TemporalState] = {}
        states_by_node = {
            str(item.get("node_id")): item for item in checkpoint.states
        }
        unknown = set(states_by_node) - {spec.node_id for spec in self.specs}
        if unknown:
            raise CheckpointError(
                "checkpoint contains unknown History node(s): "
                + ", ".join(sorted(unknown))
            )
        for spec in self.specs:
            raw = states_by_node.get(spec.node_id)
            if raw is None:
                raise CheckpointError(
                    f"checkpoint is missing History node '{spec.node_id}'"
                )
            if raw.get("operator_id") != spec.operator_id:
                raise CheckpointError(
                    f"checkpoint state for '{spec.node_id}' belongs to operator "
                    f"'{raw.get('operator_id')}', expected '{spec.operator_id}'"
                )
            if raw.get("state_type") != spec.state_type:
                raise CheckpointError(
                    f"checkpoint state for '{spec.node_id}' has state type "
                    f"'{raw.get('state_type')}', expected '{spec.state_type}'"
                )
            if raw.get("state_layout_hash") not in (None, spec.state_layout_hash):
                raise CheckpointError(
                    f"checkpoint state for '{spec.node_id}' has an incompatible "
                    "state layout hash"
                )
            values = tuple(float(value) for value in raw.get("values", ()))
            if len(values) != spec.element_count * spec.values_per_element:
                raise CheckpointError(
                    f"checkpoint state for '{spec.node_id}' has the wrong "
                    "layout length"
                )
            stored_tick = int(
                raw.get("simulation_tick", checkpoint.simulation_tick)
            )
            tick_valid = stored_tick == checkpoint.simulation_tick
            if spec.reset_policy in {"never", "manual"}:
                # Manual/never state may intentionally be ahead of (or behind)
                # the global replay tick; it stays frozen until replay catches
                # up.  The snapshot itself is still a coherent checkpoint.
                tick_valid = stored_tick >= 0
            if not tick_valid:
                raise CheckpointError(
                    f"checkpoint state for '{spec.node_id}' is from simulation "
                    f"tick {stored_tick}, checkpoint tick is "
                    f"{checkpoint.simulation_tick}"
                )
            restored[spec.node_id] = TemporalState(
                node_id=spec.node_id,
                operator_id=spec.operator_id,
                state_type=spec.state_type,
                simulation_tick=stored_tick,
                values=values,
                state_layout_hash=spec.state_layout_hash,
                loop_generation=int(
                    raw.get("loop_generation", checkpoint.loop_generation)
                ),
            )
        self._states = restored
        self._simulation_tick = checkpoint.simulation_tick
        self._timeline_tick = checkpoint.tick
        self._loop_generation = checkpoint.loop_generation
        cursor = checkpoint.event_cursor
        self._event_cursor = EventCursor(
            loop_generation=int(cursor.get("loop_generation", self._loop_generation)),
            tick=int(cursor.get("tick", checkpoint.tick)),
            sequence=int(cursor.get("sequence", 0)),
        )
        return compatibility

    # -- seek / replay -----------------------------------------------------
    def seek(
        self,
        timeline_tick: int,
        *,
        checkpoint: Checkpoint | None = None,
        work_cap: int | None = None,
    ) -> ReplayResult:
        """Seek to an absolute timeline tick.

        A compatible checkpoint at or before the requested tick is restored.
        Otherwise the runtime resets to declared initial state and replays every
        required simulation tick from zero.  Backward seeks reset the event
        cursor but never duplicate an already observed event in the same loop
        generation.
        """

        if timeline_tick < 0:
            raise TemporalError("timeline tick must be non-negative")
        target_sim = simulation_tick_for_timeline_tick(timeline_tick)
        used_checkpoint = False
        fallback = False
        checkpoint_error: CheckpointError | None = None

        if checkpoint is not None:
            compatibility = self.checkpoint_compatibility(
                checkpoint, target_tick=timeline_tick
            )
            if (
                compatibility.compatible
                and checkpoint.simulation_tick <= target_sim
            ):
                try:
                    self.restore_checkpoint(
                        checkpoint,
                        allow_device_replay=checkpoint.residency == "device",
                    )
                    used_checkpoint = True
                except CheckpointError as exc:
                    checkpoint_error = exc
            else:
                checkpoint_error = CheckpointError(
                    compatibility.reason
                    if not compatibility.compatible
                    else "checkpoint is ahead of the requested tick"
                )

        backward = target_sim < self._simulation_tick
        if not used_checkpoint and (backward or checkpoint_error is not None):
            # Per-node reset policy: seeking restarts states that participate
            # in replay (on-loop/on-seek), while never/manual states keep their
            # committed value.  A full explicit reset still resets every node.
            seek_reset_nodes = {
                spec.node_id
                for spec in self.specs
                if spec.reset_policy not in {"never", "manual"}
            }
            self._reset_states(seek_reset_nodes)
            self._simulation_tick = 0
            self._timeline_tick = 0
            self._event_cursor = EventCursor(
                loop_generation=self._loop_generation, tick=-1, sequence=0
            )
            fallback = True

        advance = self.advance_to(timeline_tick, work_cap=work_cap)
        return ReplayResult(
            ok=not advance.presentation_lagging,
            target_tick=timeline_tick,
            presentation_tick=advance.presentation_timeline_tick,
            from_checkpoint=used_checkpoint,
            fallback_to_initial_state=fallback and not used_checkpoint,
            events=advance.events,
        )

    def replay(
        self,
        target_tick: int,
        *,
        checkpoint: Checkpoint | None = None,
        work_cap: int | None = None,
    ) -> ReplayResult:
        try:
            return self.seek(target_tick, checkpoint=checkpoint, work_cap=work_cap)
        except ReplayError as exc:
            return ReplayResult(
                ok=False,
                target_tick=target_tick,
                presentation_tick=self.presentation_timeline_tick,
                from_checkpoint=checkpoint is not None,
                fallback_to_initial_state=False,
                events=(),
                error=exc,
            )


class TemporalPreviewGuard:
    """Protect the last valid authoring preview from stale failed replay work.

    The guard is intentionally independent of a concrete preview backend.  It
    accepts a generation token and a publish callback and enforces the existing
    NW-08 rule: a failure or an obsolete generation can never replace the last
    valid preview.
    """

    def __init__(self) -> None:
        self._last_valid: Any = None
        self._last_error: ReplayError | None = None
        self._generation = 0
        self._device_epoch = 0

    @property
    def last_valid(self) -> Any:
        return self._last_valid

    @property
    def last_error(self) -> ReplayError | None:
        return self._last_error

    def note_generation(self, generation: int) -> None:
        if generation < 0:
            raise TemporalError("generation must be non-negative")
        self._generation = int(generation)

    def note_device_epoch(self, epoch: int) -> None:
        if epoch < 0:
            raise TemporalError("device epoch must be non-negative")
        self._device_epoch = int(epoch)

    def attempt(
        self,
        *,
        generation: int,
        device_epoch: int,
        replay: Callable[[], ReplayResult],
        value: Any,
    ) -> bool:
        """Publish ``value`` only when replay succeeds on current work."""

        if generation != self._generation or device_epoch != self._device_epoch:
            return False
        try:
            result = replay()
        except Exception as exc:
            self._last_error = (
                exc
                if isinstance(exc, ReplayError)
                else ReplayError(
                    f"temporal replay failed: {exc}",
                    node_id="",
                    operator_id="",
                    simulation_tick=0,
                )
            )
            return False
        if not result.ok:
            self._last_error = result.error
            return False
        self._last_valid = value
        self._last_error = None
        return True


class TemporalReferencePreviewBackend:
    """Deterministic reference preview for NW-09 contract tests.

    This backend reuses the NW-08 ``PreviewFrame`` and asynchronous pipeline
    contracts.  It is explicitly not hardware evidence and never claims
    portable GPU-state replay.
    """

    name = "nw09-temporal-reference"
    hardware_evidence = False
    persistent_realizations = True
    reserves_gpu_memory = False

    def __init__(self, *, device_lifetime: DeviceLifetime | None = None):
        import threading

        self.device_lifetime = device_lifetime
        self._runtime: TemporalRuntime | None = None
        self._runtime_identity = ""
        self._lock = threading.RLock()

    def _ensure_runtime(self, document: nw03.GraphDocument) -> TemporalRuntime:
        identity = sha256_value(
            {
                "domain": "nw09.preview-document.v1",
                "source": document.source_text,
                "profile": document.profile_id,
                "requirements": document.requirements_digest,
            }
        )
        if self._runtime is None or self._runtime_identity != identity:
            self._runtime = TemporalRuntime(
                document,
                semantic_digest=identity,
                device_lifetime=self.device_lifetime,
            )
            self._runtime_identity = identity
        return self._runtime

    def render(
        self,
        document: nw03.GraphDocument,
        tick: int,
        *,
        width: int,
        height: int,
    ):
        import nw08_preview
        from dataclasses import replace as dataclass_replace

        with self._lock:
            runtime = self._ensure_runtime(document)
            result = runtime.seek(tick)
            if not result.ok:
                error = result.error or ReplayError(
                    "temporal replay did not complete",
                    node_id=runtime.specs[0].node_id,
                    operator_id=runtime.specs[0].operator_id,
                    simulation_tick=runtime.simulation_tick,
                )
                raise nw08_preview.PreviewError(
                    str(error), [error.to_dict()]
                )
            state_digest = runtime.state_digest()
            content_digest = sha256_value(
                {
                    "domain": "nw09.preview-frame.v1",
                    "state_digest": state_digest,
                    "tick": tick,
                    "width": width,
                    "height": height,
                }
            )
            pixel = bytes.fromhex(content_digest.rsplit(":", 1)[-1][:32] * 2)
            image_data = pixel * max(1, min(16, width * height // 16))
            timings = nw08_preview.PassTimings(
                (
                    nw08_preview.PassTiming(
                        "temporal_fixed_step", 0.0, measured=False
                    ),
                )
            )
            return nw08_preview.PreviewFrame(
                tick=tick,
                width=width,
                height=height,
                image_sha256=content_digest,
                semantic_digest=runtime.semantic_digest,
                content_digest=state_digest,
                resident_bytes=sum(
                    spec.state_bytes * 2 for spec in runtime.specs
                ),
                pass_timings=timings,
                backend=self.name,
                hardware_evidence=False,
                execution_kind="contract_reference",
                image_data=image_data,
                device_epoch=(
                    runtime.device_lifetime.epoch
                    if runtime.device_lifetime
                    else None
                ),
            )

    def reinitialize_device(self, lifetime: DeviceLifetime) -> int:
        with self._lock:
            self.device_lifetime = lifetime
            if self._runtime is not None:
                self._runtime.reinitialize_device(lifetime)
                self._runtime.confirm_preview_reinitialization()
            return lifetime.epoch


class TemporalPreviewSession:
    """Bind a :class:`TemporalRuntime` to an NW-08 preview pipeline.

    Failed replay never publishes and never replaces the pipeline's last valid
    authoring preview.  Device recreation passes through the explicit NW-08
    preview reinitialization boundary before replay can publish.
    """

    def __init__(
        self,
        session,
        pipeline=None,
        *,
        device_lifetime: DeviceLifetime | None = None,
        events: Sequence[TemporalEvent] = (),
    ):
        self.session = session
        self.runtime = TemporalRuntime(
            session.document,
            semantic_digest=getattr(session, "semantic_digest", None),
            device_lifetime=device_lifetime,
            events=events,
        )
        self.pipeline = pipeline
        self.guard = TemporalPreviewGuard()

    @property
    def last_valid_preview(self):
        return self.pipeline.last_valid_preview if self.pipeline else self.guard.last_valid

    @property
    def last_error(self) -> ReplayError | None:
        return self.guard.last_error

    def _sync_guard(self) -> None:
        if self.pipeline is not None:
            self.guard.note_generation(int(getattr(self.pipeline, "snapshot_generation", 0)))
            self.guard.note_device_epoch(int(getattr(self.pipeline.device, "epoch", 0)))

    def seek(
        self,
        tick: int,
        *,
        checkpoint: Checkpoint | None = None,
        publish: bool = True,
    ) -> ReplayResult:
        self._sync_guard()
        result = self.runtime.seek(tick, checkpoint=checkpoint)
        if not result.ok:
            self.guard._last_error = result.error
            return result
        if publish and self.pipeline is not None:
            try:
                self.pipeline.preview_now(tick=result.presentation_tick)
                self.guard._last_valid = self.pipeline.last_valid_preview
            except Exception as exc:
                error = ReplayError(
                    f"preview publication failed at tick {tick}: {exc}",
                    node_id=self.runtime.specs[0].node_id,
                    operator_id=self.runtime.specs[0].operator_id,
                    simulation_tick=self.runtime.simulation_tick,
                )
                self.guard._last_error = error
                # A failed publish must never replace the last valid preview.
                return ReplayResult(
                    ok=False,
                    target_tick=tick,
                    presentation_tick=self.runtime.presentation_timeline_tick,
                    from_checkpoint=checkpoint is not None,
                    fallback_to_initial_state=result.fallback_to_initial_state,
                    events=result.events,
                    error=error,
                )
        else:
            self.guard._last_valid = self.runtime.state_digest()
        self.guard._last_error = None
        return result

    def replay(
        self,
        tick: int,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> ReplayResult:
        result = self.runtime.replay(tick, checkpoint=checkpoint)
        if self.pipeline is not None and result.ok:
            try:
                self.pipeline.preview_now(tick=result.presentation_tick)
            except Exception:
                pass
        return result

    def reinitialize_device(self, lifetime: DeviceLifetime) -> int:
        if self.pipeline is not None:
            recreate = getattr(self.pipeline, "reinitialize_device", None)
            if callable(recreate):
                generation = recreate()
                epoch = int(
                    generation.epoch
                    if hasattr(generation, "epoch")
                    else generation
                )
                if epoch != lifetime.epoch:
                    raise TemporalError(
                        "NW-08 preview device epoch did not match the requested "
                        "reinitialization epoch"
                    )
            self.guard.note_device_epoch(int(getattr(self.pipeline.device, "epoch", 0)))
        self.runtime.reinitialize_device(lifetime)
        self.runtime.confirm_preview_reinitialization()
        self.guard.note_generation(
            int(getattr(self.pipeline, "snapshot_generation", 0))
        )
        return lifetime.epoch

