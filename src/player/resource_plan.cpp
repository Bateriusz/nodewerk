#include "resource_plan.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#include <d3d12.h>
#include <dxgi.h>
#endif

namespace nw::player {
namespace {

constexpr std::uint64_t kSimulatedPlacementAlignment = 64u * 1024u;

#if defined(_WIN32)
DXGI_FORMAT PlacedFormat(const std::string& format) {
    if (format == "DXGI_FORMAT_R8G8B8A8_UNORM") {
        return DXGI_FORMAT_R8G8B8A8_UNORM;
    }
    if (format == "DXGI_FORMAT_R32G32B32A32_FLOAT") {
        return DXGI_FORMAT_R32G32B32A32_FLOAT;
    }
    if (format == "DXGI_FORMAT_R32_UINT") return DXGI_FORMAT_R32_UINT;
    if (format == "DXGI_FORMAT_D32_FLOAT") return DXGI_FORMAT_D32_FLOAT;
    return DXGI_FORMAT_UNKNOWN;
}

D3D12_RESOURCE_DESC PlacedResourceDesc(
    const package::ResourceDescriptor& resource) {
    D3D12_RESOURCE_DESC desc{};
    if (resource.kind == "buffer") {
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = resource.size_bytes;
        desc.Height = 1u;
        desc.DepthOrArraySize = 1u;
        desc.MipLevels = 1u;
        desc.Format = DXGI_FORMAT_UNKNOWN;
        desc.SampleDesc.Count = 1u;
        desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    } else if (resource.kind == "texture2d") {
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        desc.Width = resource.width;
        desc.Height = resource.height;
        desc.DepthOrArraySize = static_cast<UINT16>(resource.array_size);
        desc.MipLevels = static_cast<UINT16>(resource.mip_levels);
        desc.Format = PlacedFormat(resource.format);
        desc.SampleDesc.Count = 1u;
        desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    } else {
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D;
        desc.Width = resource.width;
        desc.Height = resource.height;
        desc.DepthOrArraySize = static_cast<UINT16>(resource.depth);
        desc.MipLevels = static_cast<UINT16>(resource.mip_levels);
        desc.Format = PlacedFormat(resource.format);
        desc.SampleDesc.Count = 1u;
        desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    }
    if (resource.usage == "uav") {
        desc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(
            desc.Flags | D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    }
    if (resource.usage == "rtv") {
        desc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(
            desc.Flags | D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET);
    }
    if (resource.usage == "dsv") {
        desc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(
            desc.Flags | D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL);
    }
    return desc;
}
#endif

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
                    const package::Package& pkg) {
    CapabilityRequirementFailure failure{};
    failure.code = std::move(code);
    failure.requirement = std::move(requirement);
    failure.required_value = std::move(required_value);
    failure.observed_value = std::move(observed_value);
    failure.profile_id = pkg.capability.profile_id;
    failure.requirements_digest = pkg.capability.requirements_digest;
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

bool IsPersistentResource(const package::Package& pkg,
                          const package::ResourceDescriptor& resource) {
    if (resource.usage == "cbv" || resource.usage == "sampler" ||
        resource.usage == "readback") {
        return true;
    }
    for (const auto& instruction : pkg.frame) {
        if (instruction.opcode == "frame.present" ||
            instruction.opcode == "frame.set_root_constants") {
            for (const auto& operand : instruction.operands) {
                if (operand.type == package::OperandType::ResourceRef &&
                    operand.u32 == resource.resource_id) {
                    return true;
                }
            }
        }
    }
    return false;
}

bool LifetimesOverlap(const ResourceLogicalLifetime& left,
                      const ResourceLogicalLifetime& right) {
    return !(left.last_use_frame < right.first_use_frame ||
             right.last_use_frame < left.first_use_frame);
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

ResourcePlanResult BuildAliasedResourcePlan(
    const package::Package& pkg,
    ResourceAllocationQueryFn query,
    void* user_data,
    std::uint32_t frames_in_flight) {
    ResourcePlanResult result{};
    if (query == nullptr) {
        AddPlanFailure(
            &result, "resource_allocation_info_unavailable",
            "GetResourceAllocationInfo", "adapter allocation query",
            "unavailable",
            "physical alias realization requires a real adapter allocation "
            "query; simulated placement is never a silent fallback", pkg);
        result.exit_code = ExitCode::ResourceAllocationFailed;
        return result;
    }
    frames_in_flight = std::max<std::uint32_t>(1u, std::min<std::uint32_t>(3u, frames_in_flight));
    std::set<std::uint32_t> seen_ids;
    bool arithmetic_ok = true;
    std::map<std::uint32_t, ResourceAllocationQuery> allocations;
    std::map<std::uint32_t, bool> persistent;
    std::map<std::uint32_t, ResourceLogicalLifetime> lifetimes;
    std::map<std::string, std::vector<std::uint32_t>> groups;

    for (const auto& resource : pkg.resources) {
        if (!seen_ids.insert(resource.resource_id).second) {
            AddPlanFailure(&result, "package_resource_id", "resource_id",
                           "unique", "duplicate",
                           "duplicate resource id in aliased realization plan",
                           pkg);
            continue;
        }
        const ResourceAllocationQuery allocation = query(resource, user_data);
        if (!allocation.valid || allocation.size_bytes == 0u ||
            allocation.alignment == 0u) {
            AddPlanFailure(
                &result, "resource_allocation_info_unavailable",
                "GetResourceAllocationInfo", "valid size/alignment",
                allocation.detail.empty() ? "unavailable" : allocation.detail,
                "adapter-specific allocation query failed", pkg);
            continue;
        }
        allocations[resource.resource_id] = allocation;
        persistent[resource.resource_id] = IsPersistentResource(pkg, resource);
        if (!persistent[resource.resource_id]) {
            groups[AliasCandidateGroup(resource)].push_back(resource.resource_id);
        }
    }

    // Derive a conservative per-resource lifetime from the ordered frame
    // instruction stream.  Resources absent from the frame stream are
    // persistent for aliasing purposes.
    for (const auto& resource : pkg.resources) {
        ResourceLogicalLifetime lifetime{};
        lifetime.resource_id = resource.resource_id;
        lifetime.first_use_frame = std::numeric_limits<std::uint64_t>::max();
        lifetime.last_use_frame = 0u;
        lifetime.permitted_alias_group = AliasCandidateGroup(resource);
        std::uint64_t frame_index = 0u;
        for (const auto& instruction : pkg.frame) {
            for (const auto& operand : instruction.operands) {
                if (operand.type == package::OperandType::ResourceRef &&
                    operand.u32 == resource.resource_id) {
                    lifetime.referenced_by_frame = true;
                    lifetime.first_use_frame =
                        std::min(lifetime.first_use_frame, frame_index);
                    lifetime.last_use_frame =
                        std::max(lifetime.last_use_frame, frame_index);
                }
            }
            ++frame_index;
        }
        if (!lifetime.referenced_by_frame) {
            lifetime.first_use_frame = 0u;
            lifetime.last_use_frame = 0u;
        }
        lifetimes[resource.resource_id] = lifetime;
    }

    std::uint64_t cursor = 0u;
    for (const auto& resource : pkg.resources) {
        const auto allocation_it = allocations.find(resource.resource_id);
        if (allocation_it == allocations.end()) continue;
        const bool is_persistent = persistent[resource.resource_id];
        const std::uint32_t instances = is_persistent ? 1u : frames_in_flight;
        std::uint32_t aliased_count = 0u;
        for (std::uint32_t instance = 0u; instance < instances; ++instance) {
            cursor = RoundUp(cursor, allocation_it->second.alignment);
            ResourcePlacement placement{};
            placement.descriptor = resource;
            placement.kind = ResourcePlacementKind::NonAliasedCommitted;
            placement.allocation_size_bytes = allocation_it->second.size_bytes;
            placement.allocation_alignment = allocation_it->second.alignment;
            placement.heap_offset_bytes = cursor;
            placement.aliased = false;
            placement.frame_slot_instance = instance;
            placement.lifetime = lifetimes[resource.resource_id];

            // Reuse an earlier placement in the same compatible group when the
            // logical lifetimes do not intersect.  Resources that are persistent
            // or that overlap in time always receive independent storage.
            for (const auto& candidate : result.plan.placements) {
                if (candidate.frame_slot_instance != instance) continue;
                if (persistent[candidate.descriptor.resource_id]) continue;
                if (candidate.lifetime.permitted_alias_group !=
                    placement.lifetime.permitted_alias_group) {
                    continue;
                }
                if (LifetimesOverlap(candidate.lifetime, placement.lifetime)) {
                    continue;
                }
                if (candidate.descriptor.format != resource.format ||
                    candidate.descriptor.usage != resource.usage ||
                    candidate.descriptor.kind != resource.kind) {
                    continue;
                }
                // A candidate's storage must be large enough for the incoming
                // allocation; otherwise aliasing would spill beyond the
                // proven region and under-size the aggregate heap.
                if (candidate.allocation_size_bytes <
                    allocation_it->second.size_bytes) {
                    continue;
                }
                if (candidate.heap_offset_bytes %
                        allocation_it->second.alignment != 0u) {
                    continue;
                }
                if (candidate.heap_offset_bytes > cursor ||
                    candidate.allocation_size_bytes >
                        cursor - candidate.heap_offset_bytes) {
                    continue;
                }
                // Every placement already sharing this region must also be
                // dead for the incoming lifetime.  Checking only the first
                // candidate would miss another transient aliased into the same
                // bytes.
                bool region_busy = false;
                for (const auto& occupant : result.plan.placements) {
                    if (occupant.frame_slot_instance != instance) continue;
                    if (occupant.heap_offset_bytes !=
                        candidate.heap_offset_bytes) {
                        continue;
                    }
                    if (LifetimesOverlap(occupant.lifetime,
                                         placement.lifetime)) {
                        region_busy = true;
                        break;
                    }
                }
                if (region_busy) {
                    continue;
                }
                placement.heap_offset_bytes = candidate.heap_offset_bytes;
                placement.aliased = true;
                ++aliased_count;
                ResourceAliasBarrier barrier{};
                barrier.resource_id = resource.resource_id;
                barrier.before_resource_id = candidate.descriptor.resource_id;
                barrier.group = placement.lifetime.permitted_alias_group;
                barrier.uav_barrier = resource.usage == "uav" ||
                                      candidate.descriptor.usage == "uav";
                barrier.reason =
                    "fence-safe transient alias reuse after prior lifetime";
                result.plan.alias_barriers.push_back(std::move(barrier));
                break;
            }
            if (!placement.aliased) {
                cursor = CheckedAdd(cursor, allocation_it->second.size_bytes,
                                    &arithmetic_ok);
            } else {
                const std::uint64_t alias_end =
                    CheckedAdd(placement.heap_offset_bytes,
                               allocation_it->second.size_bytes,
                               &arithmetic_ok);
                cursor = std::max(cursor, alias_end);
            }
            if (placement.aliased) {
                placement.kind = ResourcePlacementKind::AliasedTransient;
            }
            result.plan.max_alignment =
                std::max(result.plan.max_alignment, allocation_it->second.alignment);
            result.plan.placements.push_back(std::move(placement));
        }
        const std::string group_name = is_persistent
            ? "committed:" + std::to_string(resource.resource_id)
            : AliasCandidateGroup(resource);
        auto existing = std::find_if(
            result.plan.alias_groups.begin(), result.plan.alias_groups.end(),
            [&group_name](const ResourceAliasGroup& group) {
                return group.group == group_name;
            });
        if (existing == result.plan.alias_groups.end()) {
            ResourceAliasGroup group{};
            group.group = group_name;
            group.resource_ids.push_back(resource.resource_id);
            group.frame_slot_instances = instances;
            group.aliased = aliased_count > 0u;
            result.plan.alias_groups.push_back(std::move(group));
        } else {
            existing->resource_ids.push_back(resource.resource_id);
            existing->aliased = existing->aliased || aliased_count > 0u;
        }
    }

    if (!arithmetic_ok) {
        AddPlanFailure(&result, "resource_allocation_size_overflow",
                       "total_engine_bytes", "no overflow", "overflow",
                       "checked arithmetic rejected aggregate allocation size",
                       pkg);
    }
    result.plan.total_engine_bytes = cursor;
    if (result.plan.placements.size() !=
        static_cast<std::size_t>(pkg.resources.size()) * frames_in_flight) {
        // Persistent resources intentionally have one instance, so the exact
        // count depends on the package; only report missing placements.
        std::set<std::uint32_t> placed;
        for (const auto& placement : result.plan.placements) {
            placed.insert(placement.descriptor.resource_id);
        }
        if (placed.size() != pkg.resources.size()) {
            AddPlanFailure(&result, "resource_allocation_incomplete",
                           "placements", std::to_string(pkg.resources.size()),
                           std::to_string(placed.size()),
                           "not every resource received an aliased/committed placement",
                           pkg);
        }
    }
    result.exit_code = result.failures.empty()
                           ? ExitCode::Success
                           : ExitCode::ResourceAllocationFailed;
    return result;
}

ResourcePlanResult BuildNonAliasedResourcePlan(
    const package::Package& pkg,
    ResourceAllocationQueryFn query,
    void* user_data) {
    ResourcePlanResult result{};
    if (query == nullptr) {
        query = SimulateResourceAllocation;
    }
    std::set<std::uint32_t> seen_ids;
    bool arithmetic_ok = true;
    for (const auto& resource : pkg.resources) {
        if (!seen_ids.insert(resource.resource_id).second) {
            AddPlanFailure(&result, "package_resource_id",
                           "resource_id", "unique", "duplicate", 
                           "duplicate resource id in realization plan", pkg);
            continue;
        }
        const ResourceAllocationQuery allocation = query(resource, user_data);
        if (!allocation.valid || allocation.size_bytes == 0u ||
            allocation.alignment == 0u) {
            AddPlanFailure(
                &result, "resource_allocation_info_unavailable",
                "GetResourceAllocationInfo", "valid size/alignment",
                allocation.detail.empty() ? "unavailable" : allocation.detail,
                "adapter-specific allocation query failed", pkg);
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
                       pkg);
    }
    DeriveLifetimes(pkg, &result.plan);
    if (result.plan.placements.size() != pkg.resources.size()) {
        AddPlanFailure(&result, "resource_allocation_incomplete",
                       "placements", std::to_string(pkg.resources.size()),
                       std::to_string(result.plan.placements.size()),
                       "not every resource received a non-aliased placement",
                       pkg);
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

void* CreateAliasedResourceHeap(void* device,
                                std::uint64_t heap_size_bytes,
                                std::string* detail) {
#if defined(_WIN32)
    if (device == nullptr || heap_size_bytes == 0u) {
        if (detail != nullptr) *detail = "device or heap size unavailable";
        return nullptr;
    }
    auto* d3d_device = static_cast<ID3D12Device*>(device);
    D3D12_HEAP_DESC desc{};
    desc.SizeInBytes = RoundUp(
        heap_size_bytes, D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT);
    desc.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    desc.Properties.CreationNodeMask = 1u;
    desc.Properties.VisibleNodeMask = 1u;
    desc.Alignment = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    desc.Flags = D3D12_HEAP_FLAG_NONE;
    ID3D12Heap* heap = nullptr;
    const HRESULT hr = d3d_device->CreateHeap(&desc, IID_PPV_ARGS(&heap));
    if (FAILED(hr) || heap == nullptr) {
        if (detail != nullptr) {
            *detail = "ID3D12Device::CreateHeap failed for the alias plan";
        }
        return nullptr;
    }
    if (detail != nullptr) {
        *detail = "ID3D12Device::CreateHeap";
    }
    return heap;
#else
    (void)device;
    (void)heap_size_bytes;
    if (detail != nullptr) *detail = "D3D12 unavailable on this host";
    return nullptr;
#endif
}

bool CreateAliasedPlacedResource(void* heap,
                                 const ResourcePlacement& placement,
                                 void** resource,
                                 std::string* detail) {
    if (resource != nullptr) *resource = nullptr;
#if defined(_WIN32)
    if (heap == nullptr || resource == nullptr ||
        placement.descriptor.usage == "sampler" ||
        placement.descriptor.usage == "readback") {
        if (detail != nullptr) *detail = "placement is not heap-resident";
        return false;
    }
    const D3D12_RESOURCE_DESC desc =
        PlacedResourceDesc(placement.descriptor);
    const D3D12_RESOURCE_STATES initial =
        placement.descriptor.usage == "readback"
            ? D3D12_RESOURCE_STATE_COPY_DEST
            : D3D12_RESOURCE_STATE_COMMON;
    ID3D12Device* device = nullptr;
    if (FAILED(static_cast<ID3D12Heap*>(heap)->GetDevice(
            IID_PPV_ARGS(&device))) ||
        device == nullptr) {
        if (detail != nullptr) *detail = "alias heap device query failed";
        return false;
    }
    ID3D12Resource* created = nullptr;
    const HRESULT create_hr = device->CreatePlacedResource(
        static_cast<ID3D12Heap*>(heap), placement.heap_offset_bytes, &desc,
        initial, nullptr, IID_PPV_ARGS(&created));
    device->Release();
    if (FAILED(create_hr) || created == nullptr) {
        if (detail != nullptr) {
            *detail = "CreatePlacedResource failed at the proven alias offset";
        }
        return false;
    }
    *resource = created;
    if (detail != nullptr) *detail = "CreatePlacedResource";
    return true;
#else
    (void)heap;
    (void)placement;
    if (detail != nullptr) *detail = "D3D12 unavailable on this host";
    return false;
#endif
}

void ReleaseAliasedResourceHeap(void* heap) {
#if defined(_WIN32)
    if (heap != nullptr) {
        static_cast<ID3D12Heap*>(heap)->Release();
    }
#else
    (void)heap;
#endif
}

void EmitAliasingBarrier(void* command_list,
                         const ResourceAliasBarrier& barrier,
                         void* before_resource,
                         void* after_resource) {
#if defined(_WIN32)
    if (command_list == nullptr) return;
    if (before_resource == nullptr && after_resource == nullptr) return;
    auto* list = static_cast<ID3D12GraphicsCommandList*>(command_list);
    D3D12_RESOURCE_BARRIER alias{};
    alias.Type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
    alias.Aliasing.pResourceBefore =
        static_cast<ID3D12Resource*>(before_resource);
    alias.Aliasing.pResourceAfter =
        static_cast<ID3D12Resource*>(after_resource);
    list->ResourceBarrier(1u, &alias);
    if (barrier.uav_barrier && after_resource != nullptr) {
        D3D12_RESOURCE_BARRIER uav{};
        uav.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        uav.UAV.pResource = static_cast<ID3D12Resource*>(after_resource);
        list->ResourceBarrier(1u, &uav);
    }
#else
    (void)command_list;
    (void)barrier;
    (void)before_resource;
    (void)after_resource;
#endif
}

} // namespace nw::player
