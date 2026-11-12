#!/usr/bin/env python3
"""NW-05 explicit single-queue resource scheduling and physical alias proof.

The logical schedule is portable compiler output: it describes instruction
order, logical reads/writes, lifetimes, required transitions, and alias
eligibility.  It deliberately contains **no physical heap offsets**.

Physical alias realisation is performed against an adapter allocation query at
startup.  Queried sizes, alignments, and resource constraints decide whether a
compatible logical alias group may share memory.  The resulting offsets are
adapter-specific evidence and are never serialized as a portable plan.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

import nwp_package as nwp


SINGLE_QUEUE = "direct"
QUEUE_COUNT = 1
COMMON_PROFILE_ID = nwp.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST

# Preflight upper bound for allocations owned directly by PackagePlayer rather
# than by the package alias plan: two swap-chain buffers, one display texture,
# per-frame upload rings, descriptor heaps, and readback/timestamp storage.
PLAYER_OWNED_SWAPCHAIN_BUFFERS = 2
PLAYER_OWNED_DEFAULT_WIDTH = 1280
PLAYER_OWNED_DEFAULT_HEIGHT = 720
PLAYER_OWNED_FRAME_UPLOAD_BYTES = 64 * 1024
PLAYER_OWNED_DESCRIPTOR_HEAP_BYTES = 256 * 1024
PLAYER_OWNED_MISC_BYTES = 64 * 1024
MAX_PLACEMENT_ALIGNMENT = 64 * 1024

# Stable player mapping documented by NW-04.
BARRIER_STATE_NAMES = {
    0: "COMMON",
    1: "VERTEX_AND_CONSTANT_BUFFER",
    2: "INDEX_BUFFER",
    3: "RENDER_TARGET",
    4: "UNORDERED_ACCESS",
    5: "DEPTH_WRITE",
    6: "DEPTH_READ",
    7: "NON_PIXEL_SHADER_RESOURCE",
    8: "PIXEL_SHADER_RESOURCE",
    9: "COPY_DEST",
    10: "COPY_SOURCE",
    11: "PRESENT",
    12: "GENERIC_READ",
}

READ_STATES = frozenset(
    {
        "VERTEX_AND_CONSTANT_BUFFER",
        "INDEX_BUFFER",
        "NON_PIXEL_SHADER_RESOURCE",
        "PIXEL_SHADER_RESOURCE",
        "COPY_SOURCE",
        "GENERIC_READ",
    }
)
WRITE_STATES = frozenset({"RENDER_TARGET", "UNORDERED_ACCESS", "DEPTH_WRITE", "COPY_DEST"})


class ScheduleError(RuntimeError):
    """Raised when a package cannot be represented as a valid schedule."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


@dataclass(frozen=True)
class ResourceAccess:
    phase: str
    index: int
    opcode: str
    access: str  # "read", "write", or "readwrite"
    state: str
    queue: str = SINGLE_QUEUE

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "index": self.index,
            "opcode": self.opcode,
            "access": self.access,
            "state": self.state,
            "queue": self.queue,
        }


@dataclass(frozen=True)
class ResourceLifetime:
    first_instruction: int
    last_instruction: int
    first_frame: int
    last_frame: int
    persistent: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_instruction": self.first_instruction,
            "last_instruction": self.last_instruction,
            "first_frame": self.first_frame,
            "last_frame": self.last_frame,
            "persistent": self.persistent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LogicalResource:
    resource_id: int
    name: str
    kind: str
    format: str
    usage: str
    size_bytes: int
    width: int
    height: int
    depth: int
    access: tuple[ResourceAccess, ...]
    lifetime: ResourceLifetime
    alias_eligible: bool
    alias_group: str
    alias_ineligibility_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "name": self.name,
            "kind": self.kind,
            "format": self.format,
            "usage": self.usage,
            "size_bytes": self.size_bytes,
            "dimensions": [self.width, self.height, self.depth],
            "access": [item.to_dict() for item in self.access],
            "lifetime": self.lifetime.to_dict(),
            "alias_eligible": self.alias_eligible,
            "alias_group": self.alias_group,
            "alias_ineligibility_reason": self.alias_ineligibility_reason,
        }


@dataclass(frozen=True)
class Transition:
    resource_id: int
    before: str
    after: str
    phase: str
    index: int
    reason: str
    uav_barrier: bool = False
    aliasing_barrier: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "before": self.before,
            "after": self.after,
            "phase": self.phase,
            "index": self.index,
            "reason": self.reason,
            "uav_barrier": self.uav_barrier,
            "aliasing_barrier": self.aliasing_barrier,
        }


@dataclass
class LogicalSchedule:
    queue: str
    queue_count: int
    initialization_order: tuple[str, ...]
    frame_order: tuple[str, ...]
    resources: tuple[LogicalResource, ...]
    transitions: tuple[Transition, ...]
    frames_in_flight: int
    transient_duplication: tuple[tuple[int, int], ...] = ()
    serialized_transients: tuple[int, ...] = ()

    def resource(self, resource_id: int) -> LogicalResource | None:
        return next(
            (item for item in self.resources if item.resource_id == resource_id),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        alias_groups: dict[str, list[int]] = {}
        for resource in self.resources:
            if resource.alias_eligible:
                alias_groups.setdefault(resource.alias_group, []).append(
                    resource.resource_id
                )
        return {
            "format": "nw05-logical-resource-schedule",
            "version": 1,
            "queue": self.queue,
            "queue_count": self.queue_count,
            "single_queue": True,
            "initialization_order": list(self.initialization_order),
            "frame_order": list(self.frame_order),
            "frames_in_flight": self.frames_in_flight,
            "resources": [item.to_dict() for item in self.resources],
            "alias_eligibility": {
                group: sorted(resource_ids)
                for group, resource_ids in sorted(alias_groups.items())
            },
            "transitions": [item.to_dict() for item in self.transitions],
            "transient_duplication": [
                {"resource_id": resource_id, "instances": instances}
                for resource_id, instances in self.transient_duplication
            ],
            "serialized_transients": list(self.serialized_transients),
            "physical_offsets_present": False,
            "portable_physical_assumptions": False,
        }


@dataclass(frozen=True)
class AllocationRequirement:
    valid: bool
    size_bytes: int = 0
    alignment: int = 1
    adapter_identity: str = ""
    constraints: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "size_bytes": self.size_bytes,
            "alignment": self.alignment,
            "adapter_identity": self.adapter_identity,
            "constraints": list(self.constraints),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AliasBarrier:
    resource_id: int
    before_resource_id: int
    group: str
    reason: str
    uav_barrier: bool
    activation_phase: str = "frame"
    activation_instruction: int = 0
    frame_boundary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "before_resource_id": self.before_resource_id,
            "group": self.group,
            "reason": self.reason,
            "uav_barrier": self.uav_barrier,
            "activation_phase": self.activation_phase,
            "activation_instruction": self.activation_instruction,
            "frame_boundary": self.frame_boundary,
        }


@dataclass(frozen=True)
class PhysicalPlacement:
    resource_id: int
    instance: int
    group: str
    byte_offset: int
    size_bytes: int
    alignment: int
    aliased: bool
    allocated_size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "instance": self.instance,
            "group": self.group,
            "byte_offset": self.byte_offset,
            "size_bytes": self.size_bytes,
            "alignment": self.alignment,
            "aliased": self.aliased,
            "allocated_size_bytes": self.allocated_size_bytes,
        }


@dataclass
class PhysicalRealization:
    adapter_identity: str
    placements: tuple[PhysicalPlacement, ...]
    alias_groups: tuple[dict[str, Any], ...]
    barriers: tuple[AliasBarrier, ...]
    total_bytes: int
    peak_bytes: int
    frames_in_flight: int
    allocation_source: str = "adapter_query"
    serialization: tuple[int, ...] = ()
    not_portable: bool = True
    pending_adapter_query: bool = False
    allocation_padding_bytes: int = 0
    transient_duplication_bytes: int = 0
    player_owned_bytes: int = 0
    accounted_peak_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "nw05-adapter-alias-realization",
            "version": 1,
            "adapter_identity": self.adapter_identity,
            "portable": False,
            "physical_offsets_are_adapter_specific": not self.pending_adapter_query,
            "pending_adapter_query": self.pending_adapter_query,
            "status": (
                "pending_adapter_query"
                if self.pending_adapter_query
                else "realized"
            ),
            "frames_in_flight": self.frames_in_flight,
            "allocation_source": self.allocation_source,
            "adapter_queried": self.allocation_source == "adapter_query",
            "placements": [item.to_dict() for item in self.placements],
            "alias_groups": list(self.alias_groups),
            "barriers": [item.to_dict() for item in self.barriers],
            "total_bytes": self.total_bytes,
            "peak_bytes": self.peak_bytes,
            "memory_accounting": {
                "allocation_padding_bytes": self.allocation_padding_bytes,
                "transient_duplication_bytes": self.transient_duplication_bytes,
                "player_owned_bytes": self.player_owned_bytes,
                "accounted_peak_bytes": self.accounted_peak_bytes,
                "includes_adapter_padding": not self.pending_adapter_query,
                "includes_transient_duplication": True,
                "includes_player_owned_allocations": True,
            },
            "serialization": list(self.serialization),
        }


def _player_owned_bytes(
    frames_in_flight: int,
    width: int = PLAYER_OWNED_DEFAULT_WIDTH,
    height: int = PLAYER_OWNED_DEFAULT_HEIGHT,
    upload_ring_bytes: int = PLAYER_OWNED_FRAME_UPLOAD_BYTES,
) -> int:
    """Conservative preflight bytes allocated by PackagePlayer itself."""
    width = max(1, int(width))
    height = max(1, int(height))
    frames = max(1, int(frames_in_flight))
    swap_chain = PLAYER_OWNED_SWAPCHAIN_BUFFERS * width * height * 4
    display_texture = width * height * 4
    upload_rings = frames * max(1, int(upload_ring_bytes))
    descriptor_heaps = 2 * PLAYER_OWNED_DESCRIPTOR_HEAP_BYTES
    return (
        swap_chain
        + display_texture
        + upload_rings
        + descriptor_heaps
        + PLAYER_OWNED_MISC_BYTES
    )


def pending_physical_realization(
    schedule: LogicalSchedule,
    *,
    adapter_identity: str = "pending-native-adapter",
    player_width: int = PLAYER_OWNED_DEFAULT_WIDTH,
    player_height: int = PLAYER_OWNED_DEFAULT_HEIGHT,
) -> PhysicalRealization:
    """Return a non-fabricated realization waiting for the native adapter query.

    No offsets, aliases, or barriers are invented.  The logical schedule remains
    fully specified; startup performs the adapter query before any physical
    alias reuse.  ``peak_bytes`` is a conservative logical upper bound used only
    for preflight budgets, not an adapter measurement.
    """
    groups: list[dict[str, Any]] = []
    grouped: dict[str, list[int]] = {}
    for resource in schedule.resources:
        if resource.alias_eligible:
            grouped.setdefault(resource.alias_group, []).append(resource.resource_id)
    for group, resource_ids in sorted(grouped.items()):
        groups.append(
            {
                "group": group,
                "resource_ids": sorted(resource_ids),
                "instances": schedule.frames_in_flight,
                "aliased": False,
                "pending": True,
                "frames_in_flight": schedule.frames_in_flight,
                "serialized": False,
            }
        )
    # Without an adapter query every allocation must be assumed to occupy a
    # full placement-aligned range.  Transient resources are duplicated per
    # frame slot, and the player's own swap-chain/upload/descriptor storage is
    # included so a budget check cannot pass with an incomplete logical sum.
    aligned_payload = sum(
        _round_up(max(resource.size_bytes, 1), MAX_PLACEMENT_ALIGNMENT)
        * (
            schedule.frames_in_flight
            if resource.alias_eligible and schedule.frames_in_flight > 1
            else 1
        )
        for resource in schedule.resources
    )
    duplication = sum(
        _round_up(max(resource.size_bytes, 1), MAX_PLACEMENT_ALIGNMENT)
        * (schedule.frames_in_flight - 1)
        for resource in schedule.resources
        if resource.alias_eligible and schedule.frames_in_flight > 1
    )
    padding = aligned_payload - sum(
        max(resource.size_bytes, 1)
        * (
            schedule.frames_in_flight
            if resource.alias_eligible and schedule.frames_in_flight > 1
            else 1
        )
        for resource in schedule.resources
    )
    upload_ring = max(
        [
            _round_up(max(resource.size_bytes, 1), MAX_PLACEMENT_ALIGNMENT)
            for resource in schedule.resources
        ]
        or [PLAYER_OWNED_FRAME_UPLOAD_BYTES]
    )
    player_owned = _player_owned_bytes(
        schedule.frames_in_flight,
        player_width,
        player_height,
        upload_ring_bytes=upload_ring,
    )
    peak = aligned_payload + player_owned
    return PhysicalRealization(
        adapter_identity=adapter_identity,
        placements=(),
        alias_groups=tuple(groups),
        barriers=(),
        total_bytes=0,
        peak_bytes=peak,
        frames_in_flight=schedule.frames_in_flight,
        allocation_source="pending_adapter_query",
        not_portable=True,
        pending_adapter_query=True,
        allocation_padding_bytes=padding,
        transient_duplication_bytes=duplication,
        player_owned_bytes=player_owned,
        accounted_peak_bytes=peak,
    )


# ---------------------------------------------------------------------------
# Logical schedule construction
# ---------------------------------------------------------------------------


def _resource_refs(instruction: nwp.Instruction) -> list[int]:
    return [
        int(operand.value)
        for operand in instruction.operands
        if operand.type == "resource_ref"
    ]


def _barrier_state(instruction: nwp.Instruction) -> str:
    if not instruction.operands:
        return "COMMON"
    try:
        code = int(instruction.operands[0].value)
    except (TypeError, ValueError):
        return "COMMON"
    return BARRIER_STATE_NAMES.get(code, "COMMON")


def _usage_state(usage: str) -> str:
    if usage == "uav":
        return "UNORDERED_ACCESS"
    if usage == "cbv":
        return "VERTEX_AND_CONSTANT_BUFFER"
    if usage == "srv":
        return "NON_PIXEL_SHADER_RESOURCE"
    if usage == "rtv":
        return "RENDER_TARGET"
    if usage == "dsv":
        return "DEPTH_WRITE"
    if usage == "readback":
        return "COPY_DEST"
    if usage == "sampler":
        return "NON_PIXEL_SHADER_RESOURCE"
    return "COMMON"


def _looks_transient(resource: nwp.ResourceDescriptor, accesses: Sequence[ResourceAccess]) -> tuple[bool, str]:
    if resource.usage in {"cbv", "readback", "sampler"}:
        return False, f"{resource.usage} resources are retained across frames"
    if resource.kind not in {"buffer", "texture2d", "texture3d"}:
        return False, "unsupported resource kind for alias eligibility"
    if any(access.opcode == "frame.present" for access in accesses):
        return False, "presented output is read by the player each frame"
    if any(access.opcode == "frame.set_root_constants" for access in accesses):
        return False, "root-constant resource is retained across frames"
    if any(
        access.phase == "initialization"
        and access.opcode == "init.upload_resource"
        for access in accesses
    ):
        return False, "initialization-produced contents are retained across frames"
    if not accesses:
        return False, "resource has no frame access"
    return True, ""


def _alias_group(resource: nwp.ResourceDescriptor) -> str:
    if resource.kind == "buffer":
        return f"buffer:{resource.format}:{resource.usage}"
    return (
        f"{resource.kind}:{resource.format}:{resource.usage}:"
        f"{resource.width}x{resource.height}x{resource.depth}"
    )


def build_logical_schedule(
    package: nwp.NwpPackage,
    *,
    frames_in_flight: int = 2,
) -> LogicalSchedule:
    frames_in_flight = max(1, min(3, int(frames_in_flight)))
    resources_by_id = {resource.resource_id: resource for resource in package.resources}
    access_by_id: dict[int, list[ResourceAccess]] = {
        resource_id: [] for resource_id in resources_by_id
    }
    initialization_order: list[str] = []
    frame_order: list[str] = []

    for index, instruction in enumerate(package.initialization):
        initialization_order.append(instruction.opcode)
        for resource_id in _resource_refs(instruction):
            if resource_id not in resources_by_id:
                continue
            access = "write" if instruction.opcode.startswith("init.") else "read"
            access_by_id[resource_id].append(
                ResourceAccess(
                    phase="initialization",
                    index=index,
                    opcode=instruction.opcode,
                    access=access,
                    state=_usage_state(resources_by_id[resource_id].usage),
                )
            )

    # Descriptor tables and root constants bind resources for subsequent
    # dispatches in the same frame stream.  Track the current binding set.
    bound: dict[int, str] = {}
    for index, instruction in enumerate(package.frame):
        frame_order.append(instruction.opcode)
        opcode = instruction.opcode
        if opcode == "frame.set_descriptor_table":
            refs = _resource_refs(instruction)
            for resource_id in refs:
                resource = resources_by_id.get(resource_id)
                if resource is None:
                    continue
                bound[resource_id] = resource.usage
                access_by_id[resource_id].append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="read",
                        state=_usage_state(resource.usage),
                    )
                )
        elif opcode == "frame.set_root_constants":
            refs = _resource_refs(instruction)
            for resource_id in refs:
                resource = resources_by_id.get(resource_id)
                if resource is None:
                    continue
                bound[resource_id] = resource.usage
                access_by_id[resource_id].append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="read",
                        state=_usage_state(resource.usage),
                    )
                )
        elif opcode == "frame.dispatch":
            for resource_id in sorted(bound):
                resource = resources_by_id.get(resource_id)
                if resource is None:
                    continue
                if resource.usage == "uav":
                    access = "write"
                elif resource.usage in {"srv", "cbv"}:
                    access = "read"
                else:
                    continue
                access_by_id[resource_id].append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access=access,
                        state=_usage_state(resource.usage),
                    )
                )
        elif opcode == "frame.copy_buffer":
            refs = _resource_refs(instruction)
            if len(refs) >= 2:
                access_by_id.setdefault(refs[0], []).append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="read",
                        state="COPY_SOURCE",
                    )
                )
                access_by_id.setdefault(refs[1], []).append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="write",
                        state="COPY_DEST",
                    )
                )
        elif opcode == "frame.barrier":
            refs = _resource_refs(instruction)
            for resource_id in refs:
                state = _barrier_state(instruction)
                access_by_id.setdefault(resource_id, []).append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="readwrite"
                        if state in WRITE_STATES
                        else "read",
                        state=state,
                    )
                )
        elif opcode == "frame.present":
            refs = _resource_refs(instruction)
            for resource_id in refs:
                access_by_id.setdefault(resource_id, []).append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="read",
                        state="PRESENT",
                    )
                )
        else:
            for resource_id in _resource_refs(instruction):
                resource = resources_by_id.get(resource_id)
                if resource is None:
                    continue
                access_by_id[resource_id].append(
                    ResourceAccess(
                        phase="frame",
                        index=index,
                        opcode=opcode,
                        access="read",
                        state=_usage_state(resource.usage),
                    )
                )

    logical_resources: list[LogicalResource] = []
    duplication: list[tuple[int, int]] = []
    serialized: list[int] = []
    for resource in package.resources:
        accesses = tuple(access_by_id.get(resource.resource_id, ()))
        frame_accesses = [item for item in accesses if item.phase == "frame"]
        first_frame = min((item.index for item in frame_accesses), default=0)
        last_frame = max((item.index for item in frame_accesses), default=0)
        transient, reason = _looks_transient(resource, accesses)
        persistent = not transient
        lifetime = ResourceLifetime(
            first_instruction=first_frame,
            last_instruction=last_frame,
            first_frame=first_frame,
            last_frame=last_frame,
            persistent=persistent,
            reason=reason if reason else "transient per-frame scratch",
        )
        eligible = transient
        if eligible and frames_in_flight > 1:
            # A transient touched every frame cannot be aliased with another
            # resource while frames overlap unless it is duplicated per frame
            # slot.  Record the duplication explicitly.
            duplication.append((resource.resource_id, frames_in_flight))
        logical_resources.append(
            LogicalResource(
                resource_id=resource.resource_id,
                name=resource.name,
                kind=resource.kind,
                format=resource.format,
                usage=resource.usage,
                size_bytes=resource.size_bytes,
                width=resource.width,
                height=resource.height,
                depth=resource.depth,
                access=accesses,
                lifetime=lifetime,
                alias_eligible=eligible,
                alias_group=_alias_group(resource),
                alias_ineligibility_reason="" if eligible else reason,
            )
        )

    transitions: list[Transition] = []
    for item in logical_resources:
        previous = "COMMON"
        previous_write = False
        for access in item.access:
            if access.opcode == "frame.barrier" or access.state != previous:
                transitions.append(
                    Transition(
                        resource_id=item.resource_id,
                        before=previous,
                        after=access.state,
                        phase=access.phase,
                        index=access.index,
                        reason="explicit transition required by schedule",
                        uav_barrier=previous_write
                        and ("UNORDERED_ACCESS" in {previous, access.state}),
                    )
                )
            if access.access == "write":
                previous_write = True
            elif access.access == "read" and previous_write:
                transitions.append(
                    Transition(
                        resource_id=item.resource_id,
                        before=access.state,
                        after=access.state,
                        phase=access.phase,
                        index=access.index,
                        reason="UAV write -> read hazard",
                        uav_barrier=True,
                    )
                )
                previous_write = False
            previous = access.state

    return LogicalSchedule(
        queue=SINGLE_QUEUE,
        queue_count=QUEUE_COUNT,
        initialization_order=tuple(initialization_order),
        frame_order=tuple(frame_order),
        resources=tuple(logical_resources),
        transitions=tuple(transitions),
        frames_in_flight=frames_in_flight,
        transient_duplication=tuple(duplication),
        serialized_transients=tuple(serialized),
    )


# ---------------------------------------------------------------------------
# Alias compatibility and physical realisation
# ---------------------------------------------------------------------------


def lifetimes_overlap(
    left: LogicalResource | ResourceLifetime,
    right: LogicalResource | ResourceLifetime,
) -> bool:
    left_lifetime = left.lifetime if isinstance(left, LogicalResource) else left
    right_lifetime = right.lifetime if isinstance(right, LogicalResource) else right
    return not (
        left_lifetime.last_frame < right_lifetime.first_frame
        or right_lifetime.last_frame < left_lifetime.first_frame
    )


def _activation_frame(resource: LogicalResource, phase: str = "frame") -> int:
    """First instruction that actually executes against the resource.

    Descriptor-table bindings precede the dispatch that consumes the binding;
    barriers must be placed at the consuming dispatch, not at binding time.
    """
    if phase == "initialization":
        for access in resource.access:
            if access.phase == "initialization":
                return access.index
        return 0
    for access in resource.access:
        if access.phase != "frame":
            continue
        if access.opcode in {
            "frame.dispatch",
            "frame.copy_buffer",
            "frame.present",
        }:
            return access.index
    return max(0, resource.lifetime.first_frame)


def compatible_alias_group(
    left: LogicalResource,
    right: LogicalResource,
    *,
    frames_in_flight: int = 1,
) -> tuple[bool, str]:
    if left.resource_id == right.resource_id:
        return False, "same resource"
    if not left.alias_eligible or not right.alias_eligible:
        return False, "resource is not transient/alias-eligible"
    if left.alias_group != right.alias_group:
        return False, "incompatible resource kind/format/usage/dimensions"
    if left.format != right.format or left.usage != right.usage:
        return False, "incompatible format or usage"
    if lifetimes_overlap(left, right):
        return False, "lifetimes overlap"
    if frames_in_flight > 1:
        # Without duplication or serialization, overlapping frames reserve
        # distinct transient storage.  The caller duplicates first; this remains
        # a conservative guard for callers that pass raw logical resources.
        overlap = [
            left_access
            for left_access in left.access
            for right_access in right.access
            if left_access.phase == right_access.phase
            and left_access.index == right_access.index
        ]
        if overlap:
            return False, "frames in flight reuse is not fence-safe"
    return True, "compatible transient alias candidates"


def _round_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    remainder = value % alignment
    return value if remainder == 0 else value + alignment - remainder


def simulate_allocation(
    resource: nwp.ResourceDescriptor,
    *,
    adapter_identity: str = "host-simulation",
) -> AllocationRequirement:
    """Deterministic non-adapter fallback used by GPU-less verification only."""
    bytes_per_element = {
        "DXGI_FORMAT_R8G8B8A8_UNORM": 4,
        "DXGI_FORMAT_R32G32B32A32_FLOAT": 16,
        "DXGI_FORMAT_R32_UINT": 4,
        "DXGI_FORMAT_D32_FLOAT": 4,
    }.get(resource.format, 0)
    if resource.kind == "buffer":
        size = resource.size_bytes
    else:
        if bytes_per_element == 0:
            return AllocationRequirement(False, detail="unknown format")
        width = max(1, resource.width)
        height = max(1, resource.height)
        depth = max(1, resource.depth) if resource.kind == "texture3d" else 1
        array_size = resource.array_size if resource.kind == "texture2d" else 1
        size = width * height * depth * array_size * max(1, resource.mip_levels) * bytes_per_element
    if size <= 0:
        return AllocationRequirement(False, detail="zero-sized resource")
    alignment = 64 * 1024
    return AllocationRequirement(
        True,
        size_bytes=_round_up(size, alignment),
        alignment=alignment,
        adapter_identity=adapter_identity,
        constraints=("non-adapter simulation; not hardware evidence",),
        detail="simulated",
    )


def normalize_allocation_query(
    query: Mapping[int, Any] | Callable[[nwp.ResourceDescriptor], Any] | None,
    *,
    allow_simulation: bool = False,
) -> Callable[[nwp.ResourceDescriptor], AllocationRequirement]:
    if query is None:
        if not allow_simulation:
            raise ScheduleError(
                "physical alias realization requires an explicit adapter "
                "allocation query; simulated 64 KiB placement is never a "
                "silent fallback"
            )
        return simulate_allocation
    if callable(query):
        def call(resource: nwp.ResourceDescriptor) -> AllocationRequirement:
            value = query(resource)
            return _coerce_allocation(value, resource)
        return call

    def lookup(resource: nwp.ResourceDescriptor) -> AllocationRequirement:
        value = query.get(resource.resource_id)
        if value is None:
            value = query.get(resource.name)
        if value is None:
            return AllocationRequirement(
                False,
                detail=f"no adapter allocation info for resource {resource.resource_id}",
            )
        return _coerce_allocation(value, resource)

    return lookup


def _coerce_allocation(value: Any, resource: nwp.ResourceDescriptor) -> AllocationRequirement:
    if isinstance(value, AllocationRequirement):
        return value
    if isinstance(value, Mapping):
        return AllocationRequirement(
            bool(value.get("valid", True)),
            int(value.get("size_bytes", resource.size_bytes)),
            int(value.get("alignment", 1)),
            str(value.get("adapter_identity", "")),
            tuple(value.get("constraints", ())),
            str(value.get("detail", "adapter query")),
        )
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return AllocationRequirement(
            True,
            int(value[0]),
            int(value[1]),
            "",
            tuple(value[2]) if len(value) > 2 and not isinstance(value[2], str) else (),
            str(value[2]) if len(value) > 2 and isinstance(value[2], str) else "adapter query",
        )
    raise ScheduleError(f"unsupported allocation query result for resource {resource.resource_id}")


def _instance_lifetime(
    resource: LogicalResource,
    instance: int,
    frames_in_flight: int,
) -> tuple[bool, str]:
    if not resource.alias_eligible:
        return False, resource.alias_ineligibility_reason
    if frames_in_flight <= 1:
        return True, ""
    if instance < 0 or instance >= frames_in_flight:
        return False, "invalid frame-slot instance"
    return True, ""


def realize_physical_aliases(
    schedule: LogicalSchedule,
    package: nwp.NwpPackage,
    *,
    allocation_query: Mapping[int, Any] | Callable[[nwp.ResourceDescriptor], Any] | None = None,
    adapter_identity: str = "host-simulation",
    frames_in_flight: int | None = None,
    allow_simulated_allocation: bool = False,
    player_width: int = PLAYER_OWNED_DEFAULT_WIDTH,
    player_height: int = PLAYER_OWNED_DEFAULT_HEIGHT,
) -> PhysicalRealization:
    """Prove alias compatibility against queried sizes/alignments and emit barriers.

    Offsets returned here are adapter-specific runtime evidence.  They never
    appear in the logical schedule or in the `.nwp` container.
    """
    frames = schedule.frames_in_flight if frames_in_flight is None else max(1, int(frames_in_flight))
    query = normalize_allocation_query(
        allocation_query, allow_simulation=allow_simulated_allocation
    )
    resource_by_id = {resource.resource_id: resource for resource in schedule.resources}
    package_by_id = {resource.resource_id: resource for resource in package.resources}
    requirements: dict[int, AllocationRequirement] = {}
    for resource in schedule.resources:
        requirement = query(package_by_id[resource.resource_id])
        if not requirement.valid or requirement.size_bytes <= 0 or requirement.alignment <= 0:
            raise ScheduleError(
                f"adapter allocation query failed for resource {resource.resource_id}: "
                f"{requirement.detail or 'unavailable'}",
                [
                    nwp.package_diagnostic(
                        "resource_allocation_info_unavailable",
                        requirement.detail or "unavailable",
                    )
                ],
            )
        requirements[resource.resource_id] = requirement

    # Group resources by the compatible logical group.  Within a group, order
    # by first use so non-overlapping lifetimes can share the same offset.
    groups: dict[str, list[LogicalResource]] = {}
    for resource in schedule.resources:
        if resource.alias_eligible:
            groups.setdefault(resource.alias_group, []).append(resource)
    group_payload: list[dict[str, Any]] = []
    placements: list[PhysicalPlacement] = []
    barriers: list[AliasBarrier] = []
    seen_ids: set[tuple[int, int]] = set()
    total_bytes = 0
    aliased_group_count = 0

    def place_instance(resource: LogicalResource, instance: int, group: str) -> None:
        nonlocal total_bytes
        key = (resource.resource_id, instance)
        if key in seen_ids:
            return
        seen_ids.add(key)
        requirement = requirements[resource.resource_id]
        candidates = [
            item
            for item in placements
            if item.group == group and item.instance == instance
        ]
        offset = 0
        reused = False
        for candidate in candidates:
            other = resource_by_id[candidate.resource_id]
            compatible, _reason = compatible_alias_group(
                other, resource, frames_in_flight=1
            )
            if not compatible or lifetimes_overlap(other, resource):
                continue
            # The candidate's queried allocation must be large enough for the
            # incoming resource; otherwise the alias would spill into the next
            # committed range and under-size the aggregate heap.
            if candidate.size_bytes < requirement.size_bytes:
                continue
            if (
                requirement.alignment > 1
                and candidate.byte_offset % requirement.alignment != 0
            ):
                continue
            # Other transients may already alias this exact range.  Every
            # occupant must be dead for the incoming lifetime, not just the
            # first candidate visited.
            if any(
                item.group == group
                and item.instance == instance
                and item.byte_offset == candidate.byte_offset
                and lifetimes_overlap(resource_by_id[item.resource_id], resource)
                for item in placements
            ):
                continue
            offset = candidate.byte_offset
            reused = True
            barriers.append(
                AliasBarrier(
                    resource_id=resource.resource_id,
                    before_resource_id=other.resource_id,
                    group=group,
                    reason="fence-safe alias reuse after prior lifetime",
                    uav_barrier=other.usage == "uav" or resource.usage == "uav",
                    activation_phase="frame",
                    activation_instruction=_activation_frame(resource),
                )
            )
            break
        if not reused:
            offset = _round_up(
                max((item.byte_offset + item.size_bytes for item in placements), default=0),
                requirement.alignment,
            )
        placements.append(
            PhysicalPlacement(
                resource_id=resource.resource_id,
                instance=instance,
                group=group,
                byte_offset=offset,
                size_bytes=requirement.size_bytes,
                alignment=requirement.alignment,
                aliased=reused,
                allocated_size_bytes=requirement.size_bytes,
            )
        )
        total_bytes = max(total_bytes, offset + requirement.size_bytes)

    aliased_resources: set[int] = set()
    for group, members in sorted(groups.items()):
        ordered = sorted(
            members,
            key=lambda item: (item.lifetime.first_frame, item.lifetime.last_frame, item.resource_id),
        )
        group_aliased = False
        for index, resource in enumerate(ordered):
            for previous in ordered[:index]:
                compatible, _reason = compatible_alias_group(
                    previous, resource, frames_in_flight=1
                )
                if compatible:
                    group_aliased = True
                    aliased_resources.add(resource.resource_id)
                    aliased_resources.add(previous.resource_id)
        if group_aliased:
            aliased_group_count += 1
        instances = frames if any(
            resource_id == resource.resource_id
            for resource_id, _count in schedule.transient_duplication
        ) else 1
        for resource in ordered:
            for instance in range(instances):
                place_instance(resource, instance, group)
        group_payload.append(
            {
                "group": group,
                "resource_ids": [item.resource_id for item in ordered],
                "instances": instances,
                "aliased": group_aliased,
                "frames_in_flight": frames,
                "serialized": group in schedule.serialized_transients,
            }
        )

    # Persistent/non-eligible resources receive independent committed slots.
    for resource in schedule.resources:
        if resource.alias_eligible:
            continue
        requirement = requirements[resource.resource_id]
        offset = _round_up(
            max((item.byte_offset + item.size_bytes for item in placements), default=0),
            requirement.alignment,
        )
        placements.append(
            PhysicalPlacement(
                resource_id=resource.resource_id,
                instance=0,
                group=f"committed:{resource.resource_id}",
                byte_offset=offset,
                size_bytes=requirement.size_bytes,
                alignment=requirement.alignment,
                aliased=False,
                allocated_size_bytes=requirement.size_bytes,
            )
        )
        total_bytes = max(total_bytes, offset + requirement.size_bytes)
        group_payload.append(
            {
                "group": f"committed:{resource.resource_id}",
                "resource_ids": [resource.resource_id],
                "instances": 1,
                "aliased": False,
                "frames_in_flight": frames,
                "serialized": False,
            }
        )

    # Alias activation barriers must be emitted in execution order.  A region
    # reused by A then B needs A->B before B and B->A at the next frame boundary
    # before A is used again.  Waiting on a fence does not replace either.
    regions: dict[tuple[int, int], set[int]] = {}
    for placement in placements:
        if placement.group.startswith("committed:"):
            continue
        regions.setdefault(
            (placement.instance, placement.byte_offset), set()
        ).add(placement.resource_id)
    existing_barriers = {
        (barrier.before_resource_id, barrier.resource_id)
        for barrier in barriers
    }
    for (instance, _offset), resource_ids in sorted(regions.items()):
        if len(resource_ids) < 2:
            continue
        ordered = sorted(
            resource_ids,
            key=lambda resource_id: (
                resource_by_id[resource_id].lifetime.first_frame,
                resource_by_id[resource_id].lifetime.last_frame,
                resource_id,
            ),
        )
        group = resource_by_id[ordered[0]].alias_group
        for index, resource_id in enumerate(ordered):
            previous = ordered[index - 1]
            target = resource_by_id[resource_id]
            source = resource_by_id[previous]
            key = (previous, resource_id)
            if key in existing_barriers:
                continue
            barriers.append(
                AliasBarrier(
                    resource_id=resource_id,
                    before_resource_id=previous,
                    group=group,
                    reason=(
                        "frame-boundary alias activation before reuse"
                        if index == 0
                        else "execution-order alias activation"
                    ),
                    uav_barrier=source.usage == "uav" or target.usage == "uav",
                    activation_phase="frame",
                    activation_instruction=_activation_frame(target),
                    frame_boundary=index == 0,
                )
            )
            existing_barriers.add(key)
        # Keep the single-queue execution deterministic even when multiple
        # frame-slot instances exist.
        _ = instance

    occupied_ranges: dict[tuple[int, int], int] = {}
    for placement in placements:
        key = (placement.instance, placement.byte_offset)
        occupied_ranges[key] = max(
            occupied_ranges.get(key, 0), placement.size_bytes
        )
    occupied_bytes = sum(occupied_ranges.values())
    allocation_padding = max(0, total_bytes - occupied_bytes)
    duplicated = sum(
        placement.size_bytes
        for placement in placements
        if placement.instance > 0
    )
    upload_ring = max(
        [
            _round_up(max(resource.size_bytes, 1), MAX_PLACEMENT_ALIGNMENT)
            for resource in schedule.resources
        ]
        or [PLAYER_OWNED_FRAME_UPLOAD_BYTES]
    )
    player_owned = _player_owned_bytes(
        frames, player_width, player_height, upload_ring_bytes=upload_ring
    )
    accounted_peak = total_bytes + player_owned

    return PhysicalRealization(
        adapter_identity=adapter_identity,
        placements=tuple(placements),
        alias_groups=tuple(group_payload),
        barriers=tuple(barriers),
        total_bytes=total_bytes,
        peak_bytes=accounted_peak,
        frames_in_flight=frames,
        allocation_source=(
            "simulated_fallback" if allocation_query is None else "adapter_query"
        ),
        serialization=schedule.serialized_transients,
        allocation_padding_bytes=allocation_padding,
        transient_duplication_bytes=duplicated,
        player_owned_bytes=player_owned,
        accounted_peak_bytes=accounted_peak,
    )


def schedule_digest(schedule: LogicalSchedule) -> str:
    return "sha256:v1:" + hashlib.sha256(
        nwp.canonical_json_bytes(schedule.to_dict())
    ).hexdigest()


# Backwards-friendly aliases.
logical_schedule = build_logical_schedule
physical_alias_realization = realize_physical_aliases
alias_compatible = compatible_alias_group
build_resource_schedule = build_logical_schedule

