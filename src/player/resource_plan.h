#pragma once

// NW-04/NW-05: bounded resource realization plan.
//
// Offline logical lifetimes and permitted alias groups describe *when* a
// resource is live, not a portable physical memory offset.  NW-04 therefore
// realizes every resource as an independent non-aliased allocation and queries
// the current adapter with GetResourceAllocationInfo for its real size and
// alignment.  NW-05 adds an explicit aliased realization that proves
// compatibility against those queried sizes/alignments, duplicates transient
// resources across overlapping frames, and records the required aliasing/UAV
// barriers.  Physical offsets remain adapter-specific evidence and are never a
// portable plan.

#include "capability_requirements.h"
#include "exit_codes.h"
#include "package/nwp_format.h"

#include <cstdint>
#include <string>
#include <vector>

namespace nw::player {

struct ResourceAllocationQuery {
    bool valid = false;
    std::uint64_t size_bytes = 0u;
    std::uint64_t alignment = 0u;
    std::string detail;
};

// Platform hook implemented with ID3D12Device::GetResourceAllocationInfo on
// Windows.  A deterministic host-simulation hook is used by GPU-less tests.
using ResourceAllocationQueryFn = ResourceAllocationQuery (*)(
    const package::ResourceDescriptor& resource, void* user_data);

enum class ResourcePlacementKind : std::uint8_t {
    NonAliasedCommitted = 0,
    AliasedTransient = 1,
};

struct ResourceLogicalLifetime {
    std::uint32_t resource_id = 0u;
    std::uint64_t first_use_frame = 0u;
    std::uint64_t last_use_frame = 0u;
    bool referenced_by_frame = false;
    // Compiler-derived alias candidate identity.  It is evidence only in NW-04;
    // no physical offset is inferred from it.
    std::string permitted_alias_group;
};

struct ResourcePlacement {
    package::ResourceDescriptor descriptor{};
    ResourcePlacementKind kind = ResourcePlacementKind::NonAliasedCommitted;
    std::uint64_t allocation_size_bytes = 0u;
    std::uint64_t allocation_alignment = 0u;
    std::uint64_t heap_offset_bytes = 0u;
    bool aliased = false;
    std::uint32_t frame_slot_instance = 0u;
    ResourceLogicalLifetime lifetime{};
};

struct ResourceAliasBarrier {
    std::uint32_t resource_id = 0u;
    std::uint32_t before_resource_id = 0u;
    std::string group;
    bool uav_barrier = false;
    std::string reason;
};

struct ResourceAliasGroup {
    std::string group;
    std::vector<std::uint32_t> resource_ids;
    std::uint32_t frame_slot_instances = 1u;
    bool aliased = false;
};

struct ResourcePlan {
    std::vector<ResourcePlacement> placements;
    std::vector<ResourceAliasGroup> alias_groups;
    std::vector<ResourceAliasBarrier> alias_barriers;
    std::uint64_t total_engine_bytes = 0u;
    std::uint64_t max_alignment = 0u;

    const ResourcePlacement* Find(std::uint32_t resource_id) const noexcept;
    bool UsesOnlyNonAliasedPlacements() const noexcept;
};

struct ResourcePlanResult {
    ExitCode exit_code = ExitCode::Success;
    ResourcePlan plan{};
    std::vector<CapabilityRequirementFailure> failures;
};

// Builds a conservative, non-aliased plan after capability validation and
// before any bulk GPU allocation.  Every placement queries the current adapter.
ResourcePlanResult BuildNonAliasedResourcePlan(
    const package::Package& package,
    ResourceAllocationQueryFn query,
    void* user_data);

// Proves compatible alias groups against queried adapter allocation sizes and
// alignments.  Transient resources are duplicated once per overlapping frame
// slot; resources whose lifetimes intersect are never aliased.  The resulting
// offsets are adapter-specific evidence, not a portable package layout.
ResourcePlanResult BuildAliasedResourcePlan(
    const package::Package& package,
    ResourceAllocationQueryFn query,
    void* user_data,
    std::uint32_t frames_in_flight = 2u);

// Deterministic fallback for GPU-less host verification.  It applies D3D12's
// documented 64 KiB placement alignment and checked byte arithmetic; it never
// claims to be an adapter query.
ResourceAllocationQuery SimulateResourceAllocation(
    const package::ResourceDescriptor& resource, void* user_data);

} // namespace nw::player
