from __future__ import annotations

import sys
from pathlib import Path

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
        logical, package, frames_in_flight=1
    )
    placements = {item.resource_id: item for item in realization.placements}
    assert placements[1].aliased or placements[2].aliased
    assert placements[1].byte_offset == placements[2].byte_offset
    assert any(
        barrier.before_resource_id in {1, 2} and barrier.uav_barrier
        for barrier in realization.barriers
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
        logical, package, frames_in_flight=2
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

