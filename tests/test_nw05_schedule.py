from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw05_schedule as schedule  # noqa: E402


def _transient_pair() -> nwp.NwpPackage:
    resource = nwp.ResourceDescriptor
    instruction = nwp.Instruction
    operand = nwp.InstructionOperand
    return nwp.NwpPackage(
        resources=[
            resource(
                1,
                "scratch_a",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=1024,
            ),
            resource(
                2,
                "scratch_b",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=4096,
            ),
        ],
        initialization=[
            instruction("init.create_resource", (operand("resource_ref", 1),)),
            instruction("init.create_resource", (operand("resource_ref", 2),)),
        ],
        frame=[
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 1),
                    operand("resource_ref", 1),
                    operand("u64", 64),
                ),
            ),
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 2),
                    operand("resource_ref", 2),
                    operand("u64", 64),
                ),
            ),
        ],
    )


def test_logical_schedule_is_single_queue_and_has_no_physical_offsets() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=2)
    payload = logical.to_dict()
    assert payload["queue"] == "direct"
    assert payload["queue_count"] == 1
    assert payload["single_queue"] is True
    assert payload["physical_offsets_present"] is False
    assert payload["portable_physical_assumptions"] is False
    assert all(item["access"] for item in payload["resources"])
    assert logical.transitions


def test_logical_schedule_preserves_initialization_and_frame_order() -> None:
    package = nw04_fixture.build_compute_display_package()
    logical = schedule.build_logical_schedule(package)
    assert logical.initialization_order == tuple(
        instruction.opcode for instruction in package.initialization
    )
    assert logical.frame_order == tuple(
        instruction.opcode for instruction in package.frame
    )


def test_compatible_alias_group_realises_same_offset_and_barrier() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    realization = schedule.realize_physical_aliases(
        logical,
        package,
        frames_in_flight=1,
        allocation_query=schedule.simulate_allocation,
    )
    placements = {item.resource_id: item for item in realization.placements}
    assert placements[1].aliased or placements[2].aliased
    assert placements[1].byte_offset == placements[2].byte_offset
    assert any(
        barrier.before_resource_id in {1, 2} and barrier.uav_barrier
        for barrier in realization.barriers
    )
    transitions = {
        (barrier.before_resource_id, barrier.resource_id)
        for barrier in realization.barriers
    }
    # A reused region needs activation in both execution directions so the
    # next frame can safely reactivate the earlier user.
    assert (1, 2) in transitions
    assert (2, 1) in transitions
    assert any(
        barrier.frame_boundary for barrier in realization.barriers
    )
    payload = realization.to_dict()
    assert payload["portable"] is False
    assert payload["physical_offsets_are_adapter_specific"] is True


def test_overlapping_lifetimes_are_never_aliased() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    first, second = logical.resources
    overlapping = type(first)(
        **{
            **first.__dict__,
            "resource_id": 99,
            "lifetime": type(first.lifetime)(0, 1, 0, 1, False, "overlap"),
        }
    )
    compatible, reason = schedule.compatible_alias_group(
        first, overlapping, frames_in_flight=1
    )
    assert not compatible
    assert "overlap" in reason


def test_incompatible_alias_groups_are_rejected() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    first = logical.resources[0]
    incompatible = type(first)(
        **{
            **first.__dict__,
            "resource_id": 100,
            "format": "DXGI_FORMAT_R32G32B32A32_FLOAT",
            "alias_group": "buffer:DXGI_FORMAT_R32G32B32A32_FLOAT:uav",
        }
    )
    compatible, reason = schedule.compatible_alias_group(
        first, incompatible, frames_in_flight=1
    )
    assert not compatible
    assert "incompatible" in reason


def test_frames_in_flight_duplicate_transients_without_cross_slot_reuse() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=2)
    realization = schedule.realize_physical_aliases(
        logical,
        package,
        frames_in_flight=2,
        allocation_query=schedule.simulate_allocation,
    )
    instances = {item.resource_id: set() for item in realization.placements}
    for item in realization.placements:
        instances[item.resource_id].add(item.instance)
    assert instances == {1: {0, 1}, 2: {0, 1}}
    aliased = [item for item in realization.placements if item.aliased]
    assert aliased
    for left in aliased:
        for right in aliased:
            if left.resource_id != right.resource_id:
                assert left.instance == right.instance


def test_adapter_query_is_required_for_realization() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    with pytest.raises(schedule.ScheduleError, match="explicit adapter allocation"):
        schedule.realize_physical_aliases(
            logical, package, frames_in_flight=1
        )
    realization = schedule.realize_physical_aliases(
        logical,
        package,
        frames_in_flight=1,
        allocation_query={
            1: {"size_bytes": 65536, "alignment": 65536},
            2: {"size_bytes": 131072, "alignment": 65536},
        },
        adapter_identity="query:test",
    )
    assert realization.adapter_identity == "query:test"
    assert realization.total_bytes > 0
    accounting = realization.to_dict()["memory_accounting"]
    assert accounting["includes_adapter_padding"] is True
    assert accounting["includes_player_owned_allocations"] is True
    assert realization.peak_bytes >= realization.total_bytes


def test_pending_realization_never_invents_physical_offsets() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=2)
    pending = schedule.pending_physical_realization(logical)
    payload = pending.to_dict()
    assert payload["pending_adapter_query"] is True
    assert payload["status"] == "pending_adapter_query"
    assert payload["physical_offsets_are_adapter_specific"] is False
    assert payload["placements"] == []
    assert payload["barriers"] == []
    assert payload["total_bytes"] == 0
    logical_payload = sum(item.size_bytes for item in package.resources)
    assert pending.peak_bytes > logical_payload
    accounting = payload["memory_accounting"]
    assert accounting["includes_transient_duplication"] is True
    assert accounting["includes_player_owned_allocations"] is True
    assert pending.player_owned_bytes > 0
    assert pending.transient_duplication_bytes > 0
    assert pending.accounted_peak_bytes == pending.peak_bytes


def test_read_only_srv_and_present_outputs_are_not_alias_transient() -> None:
    operand = nwp.InstructionOperand
    instruction = nwp.Instruction
    resource = nwp.ResourceDescriptor
    package = nwp.NwpPackage(
        resources=[
            resource(
                1,
                "source",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "srv",
                "compute.math_noise",
                size_bytes=1024,
            ),
            resource(
                2,
                "dest",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=1024,
            ),
        ],
        initialization=[],
        frame=[
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 1),
                    operand("resource_ref", 2),
                    operand("u64", 64),
                ),
            )
        ],
    )
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    by_id = {item.resource_id: item for item in logical.resources}
    assert by_id[1].alias_eligible is False
    assert "read-only" in by_id[1].alias_ineligibility_reason
    assert by_id[2].alias_eligible is True


def test_alias_reuse_requires_candidate_allocation_capacity() -> None:
    package = _transient_pair()
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    realization = schedule.realize_physical_aliases(
        logical,
        package,
        frames_in_flight=1,
        allocation_query={
            1: {"size_bytes": 65536, "alignment": 65536},
            2: {"size_bytes": 131072, "alignment": 65536},
        },
        adapter_identity="query:test",
    )
    placements = {item.resource_id: item for item in realization.placements}
    assert not placements[1].aliased
    assert not placements[2].aliased
    assert (
        placements[1].byte_offset + placements[1].size_bytes
        <= placements[2].byte_offset
    )
    assert realization.total_bytes >= (
        placements[2].byte_offset + placements[2].size_bytes
    )


def test_alias_reuse_checks_every_occupant_of_a_range() -> None:
    resource = nwp.ResourceDescriptor
    instruction = nwp.Instruction
    operand = nwp.InstructionOperand
    package = nwp.NwpPackage(
        resources=[
            resource(
                resource_id,
                f"scratch_{resource_id}",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=4096,
            )
            for resource_id in (1, 2, 3)
        ],
        initialization=[
            instruction("init.create_resource", (operand("resource_ref", resource_id),))
            for resource_id in (1, 2, 3)
        ],
        frame=[
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 1),
                    operand("resource_ref", 1),
                    operand("u64", 64),
                ),
            ),
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 3),
                    operand("resource_ref", 3),
                    operand("u64", 64),
                ),
            ),
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 3),
                    operand("resource_ref", 2),
                    operand("u64", 64),
                ),
            ),
        ],
    )
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    realization = schedule.realize_physical_aliases(
        logical,
        package,
        frames_in_flight=1,
        allocation_query=schedule.simulate_allocation,
    )
    placements = {item.resource_id: item for item in realization.placements}
    # Resource 3 aliases resource 1, so resource 2 (live with resource 3) must
    # not reuse the same range merely because resource 1 is already dead.
    assert placements[3].byte_offset == placements[1].byte_offset
    assert placements[2].byte_offset != placements[1].byte_offset
    for left in realization.placements:
        for right in realization.placements:
            if left.resource_id >= right.resource_id:
                continue
            left_end = left.byte_offset + left.size_bytes
            right_end = right.byte_offset + right.size_bytes
            if left_end <= right.byte_offset or right_end <= left.byte_offset:
                continue
            left_logical = logical.resource(left.resource_id)
            right_logical = logical.resource(right.resource_id)
            assert not schedule.lifetimes_overlap(left_logical, right_logical)


def test_initialization_upload_is_never_aliased_by_later_transient() -> None:
    resource = nwp.ResourceDescriptor
    instruction = nwp.Instruction
    operand = nwp.InstructionOperand
    package = nwp.NwpPackage(
        resources=[
            resource(
                1,
                "initialized",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=4096,
            ),
            resource(
                2,
                "hidden",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=4096,
            ),
        ],
        initialization=[
            instruction("init.create_resource", (operand("resource_ref", 1),)),
            instruction("init.upload_resource", (operand("resource_ref", 1),)),
            instruction("init.create_resource", (operand("resource_ref", 2),)),
        ],
        frame=[
            instruction("frame.present", (operand("resource_ref", 1),)),
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 2),
                    operand("resource_ref", 2),
                    operand("u64", 64),
                ),
            ),
        ],
    )
    logical = schedule.build_logical_schedule(package, frames_in_flight=1)
    initialized = logical.resource(1)
    assert initialized is not None and not initialized.alias_eligible
    realization = schedule.realize_physical_aliases(
        logical,
        package,
        frames_in_flight=1,
        allocation_query=schedule.simulate_allocation,
    )
    placements = {item.resource_id: item for item in realization.placements}
    assert placements[1].byte_offset != placements[2].byte_offset

