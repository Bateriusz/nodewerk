#pragma once

// NW-04: bounded resource realization plan.
//
// Offline logical lifetimes and permitted alias groups describe *when* a
// resource is live, not a portable physical memory offset.  NW-04 therefore
// realizes every resource as an independent non-aliased allocation and queries
// the current adapter with GetResourceAllocationInfo for its real size and
// alignment.  Physical alias reuse is deliberately deferred to NW-05.

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
    ResourceLogicalLifetime lifetime{};
};

struct ResourcePlan {
    std::vector<ResourcePlacement> placements;
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

// Deterministic fallback for GPU-less host verification.  It applies D3D12's
// documented 64 KiB placement alignment and checked byte arithmetic; it never
// claims to be an adapter query.
ResourceAllocationQuery SimulateResourceAllocation(
    const package::ResourceDescriptor& resource, void* user_data);

} // namespace nw::player
