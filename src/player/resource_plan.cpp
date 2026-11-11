#include "resource_plan.h"

#include <algorithm>
#include <limits>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace nw::player {
namespace {

constexpr std::uint64_t kSimulatedPlacementAlignment = 64u * 1024u;

std::uint32_t BytesPerElement(const std::string& format) {
    if (format == "DXGI_FORMAT_R8G8B8A8_UNORM") return 4u;
    if (format == "DXGI_FORMAT_R32G32B32A32_FLOAT") return 16u;
    if (format == "DXGI_FORMAT_R32_UINT") return 4u;
    if (format == "DXGI_FORMAT_D32_FLOAT") return 4u;
    return 0u;
}

std::uint64_t RoundUp(std::uint64_t value, std::uint64_t alignment) {
    if (alignment == 0u) return value;
    const std::uint64_t remainder = value % alignment;
    return remainder == 0u ? value : value + (alignment - remainder);
}

std::uint64_t CheckedAdd(std::uint64_t a, std::uint64_t b, bool* ok) {
    if (b > std::numeric_limits<std::uint64_t>::max() - a) {
        *ok = false;
        return 0u;
    }
    return a + b;
}

std::uint64_t CheckedMul(std::uint64_t a, std::uint64_t b, bool* ok) {
    if (a != 0u && b > std::numeric_limits<std::uint64_t>::max() / a) {
        *ok = false;
        return 0u;
    }
    return a * b;
}

void AddPlanFailure(ResourcePlanResult* result,
                    std::string code,
                    std::string requirement,
                    std::string required_value,
                    std::string observed_value,
                    std::string detail,
                    const package::Package& package) {
    CapabilityRequirementFailure failure{};
    failure.code = std::move(code);
    failure.requirement = std::move(requirement);
    failure.required_value = std::move(required_value);
    failure.observed_value = std::move(observed_value);
    failure.profile_id = package.capability.profile_id;
    failure.requirements_digest = package.capability.requirements_digest;
    failure.detail = std::move(detail);
    result->failures.push_back(std::move(failure));
}

std::string AliasCandidateGroup(const package::ResourceDescriptor& resource) {
    if (resource.kind == "buffer") {
        return "buffer:" + resource.format + ":" + resource.usage;
    }
    return resource.kind + ":" + resource.format + ":" + resource.usage + ":" +
           std::to_string(resource.width) + "x" +
           std::to_string(resource.height) + "x" +
           std::to_string(resource.depth);
}

void DeriveLifetimes(const package::Package& pkg, ResourcePlan* plan) {
    for (auto& placement : plan->placements) {
        placement.lifetime.resource_id = placement.descriptor.resource_id;
        placement.lifetime.first_use_frame =
            std::numeric_limits<std::uint64_t>::max();
        placement.lifetime.last_use_frame = 0u;
        placement.lifetime.permitted_alias_group =
            AliasCandidateGroup(placement.descriptor);
    }
    std::uint64_t frame_index = 0u;
    for (const auto& instruction : pkg.frame) {
        for (const auto& operand : instruction.operands) {
            if (operand.type != package::OperandType::ResourceRef) {
                continue;
            }
            for (auto& placement : plan->placements) {
                if (placement.descriptor.resource_id != operand.u32) {
                    continue;
                }
                placement.lifetime.referenced_by_frame = true;
                placement.lifetime.first_use_frame =
                    std::min(placement.lifetime.first_use_frame, frame_index);
                placement.lifetime.last_use_frame =
                    std::max(placement.lifetime.last_use_frame, frame_index);
            }
        }
        ++frame_index;
    }
    for (auto& placement : plan->placements) {
        if (!placement.lifetime.referenced_by_frame) {
            placement.lifetime.first_use_frame = 0u;
            placement.lifetime.last_use_frame = 0u;
        }
    }
}

} // namespace

const ResourcePlacement* ResourcePlan::Find(
    std::uint32_t resource_id) const noexcept {
    for (const auto& placement : placements) {
        if (placement.descriptor.resource_id == resource_id) {
            return &placement;
        }
    }
    return nullptr;
}

bool ResourcePlan::UsesOnlyNonAliasedPlacements() const noexcept {
    for (const auto& placement : placements) {
        if (placement.aliased ||
            placement.kind != ResourcePlacementKind::NonAliasedCommitted ||
            placement.heap_offset_bytes != 0u) {
            return false;
        }
    }
    return true;
}

ResourcePlanResult BuildNonAliasedResourcePlan(
    const package::Package& package,
    ResourceAllocationQueryFn query,
    void* user_data) {
    ResourcePlanResult result{};
    if (query == nullptr) {
        query = SimulateResourceAllocation;
    }
    std::set<std::uint32_t> seen_ids;
    bool arithmetic_ok = true;
    for (const auto& resource : package.resources) {
        if (!seen_ids.insert(resource.resource_id).second) {
            AddPlanFailure(&result, "package_resource_id",
                           "resource_id", "unique", "duplicate", 
                           "duplicate resource id in realization plan", package);
            continue;
        }
        const ResourceAllocationQuery allocation = query(resource, user_data);
        if (!allocation.valid || allocation.size_bytes == 0u ||
            allocation.alignment == 0u) {
            AddPlanFailure(
                &result, "resource_allocation_info_unavailable",
                "GetResourceAllocationInfo", "valid size/alignment",
                allocation.detail.empty() ? "unavailable" : allocation.detail,
                "adapter-specific allocation query failed", package);
            continue;
        }

        ResourcePlacement placement{};
        placement.descriptor = resource;
        placement.kind = ResourcePlacementKind::NonAliasedCommitted;
        placement.allocation_size_bytes = allocation.size_bytes;
        placement.allocation_alignment = allocation.alignment;
        // Non-aliased committed realizations do not share a heap offset.
        placement.heap_offset_bytes = 0u;
        placement.aliased = false;
        result.plan.placements.push_back(std::move(placement));
        result.plan.total_engine_bytes = CheckedAdd(
            result.plan.total_engine_bytes, allocation.size_bytes,
            &arithmetic_ok);
        result.plan.max_alignment =
            std::max(result.plan.max_alignment, allocation.alignment);
    }
    if (!arithmetic_ok) {
        AddPlanFailure(&result, "resource_allocation_size_overflow",
                       "total_engine_bytes", "no overflow", "overflow",
                       "checked arithmetic rejected aggregate allocation size",
                       package);
    }
    DeriveLifetimes(package, &result.plan);
    if (result.plan.placements.size() != package.resources.size()) {
        AddPlanFailure(&result, "resource_allocation_incomplete",
                       "placements", std::to_string(package.resources.size()),
                       std::to_string(result.plan.placements.size()),
                       "not every resource received a non-aliased placement",
                       package);
    }
    result.exit_code = result.failures.empty()
                           ? ExitCode::Success
                           : ExitCode::ResourceAllocationFailed;
    return result;
}

ResourceAllocationQuery SimulateResourceAllocation(
    const package::ResourceDescriptor& resource, void* user_data) {
    (void)user_data;
    ResourceAllocationQuery result{};
    std::uint64_t bytes = 0u;
    if (resource.kind == "buffer") {
        bytes = resource.size_bytes;
    } else {
        const std::uint32_t bpe = BytesPerElement(resource.format);
        if (bpe == 0u) {
            result.detail = "unknown format";
            return result;
        }
        bool arithmetic_ok = true;
        for (std::uint32_t mip = 0u; mip < resource.mip_levels; ++mip) {
            const std::uint64_t width =
                std::max<std::uint64_t>(1u, resource.width >> mip);
            const std::uint64_t height =
                std::max<std::uint64_t>(1u, resource.height >> mip);
            const std::uint64_t depth =
                resource.kind == "texture3d"
                    ? std::max<std::uint64_t>(1u, resource.depth >> mip)
                    : 1u;
            const std::uint64_t arrays =
                resource.kind == "texture2d" ? resource.array_size : 1u;
            std::uint64_t level = width;
            level = CheckedMul(level, height, &arithmetic_ok);
            level = CheckedMul(level, depth, &arithmetic_ok);
            level = CheckedMul(level, arrays, &arithmetic_ok);
            level = CheckedMul(level, bpe, &arithmetic_ok);
            bytes = CheckedAdd(bytes, level, &arithmetic_ok);
            if (!arithmetic_ok) {
                result.detail = "size overflow";
                return result;
            }
        }
    }
    if (bytes == 0u) {
        result.detail = "zero-sized resource";
        return result;
    }
    result.valid = true;
    result.alignment = kSimulatedPlacementAlignment;
    result.size_bytes = RoundUp(bytes, result.alignment);
    result.detail = "simulated";
    return result;
}

} // namespace nw::player
