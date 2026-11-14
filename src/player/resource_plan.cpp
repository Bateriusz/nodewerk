#include "resource_plan.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#ifndef NW06_COMPILE_GPU_INITIALIZATION
#define NW06_COMPILE_GPU_INITIALIZATION 1
#endif
#ifndef NW06_COMPILE_INDEXED_INSTANCED_INDIRECT
#define NW06_COMPILE_INDEXED_INSTANCED_INDIRECT 1
#endif
#ifndef NW06_COMPILE_ANALYTIC_LIGHTING_TWIST
#define NW06_COMPILE_ANALYTIC_LIGHTING_TWIST 1
#endif
#ifndef NW06_COMPILE_ABSOLUTE_TICK_CAMERA
#define NW06_COMPILE_ABSOLUTE_TICK_CAMERA 1
#endif
#ifndef NW06_COMPILE_HALF_RESOLUTION_BLOOM
#define NW06_COMPILE_HALF_RESOLUTION_BLOOM 1
#endif
#ifndef NW06_COMPILE_TONEMAP
#define NW06_COMPILE_TONEMAP 1
#endif
#if NW06_COMPILE_GPU_INITIALIZATION || \
    NW06_COMPILE_INDEXED_INSTANCED_INDIRECT || \
    NW06_COMPILE_ANALYTIC_LIGHTING_TWIST || \
    NW06_COMPILE_ABSOLUTE_TICK_CAMERA || \
    NW06_COMPILE_HALF_RESOLUTION_BLOOM || NW06_COMPILE_TONEMAP
#define NW06_COMPILE_ANY_RUNTIME 1
#else
#define NW06_COMPILE_ANY_RUNTIME 0
#endif

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
    if (format == "DXGI_FORMAT_B8G8R8A8_UNORM") {
        return DXGI_FORMAT_B8G8R8A8_UNORM;
    }
    if (format == "DXGI_FORMAT_R8_UNORM") return DXGI_FORMAT_R8_UNORM;
    if (format == "DXGI_FORMAT_R16_FLOAT") return DXGI_FORMAT_R16_FLOAT;
    if (format == "DXGI_FORMAT_R16G16_FLOAT") {
        return DXGI_FORMAT_R16G16_FLOAT;
    }
    if (format == "DXGI_FORMAT_R16G16B16A16_FLOAT") {
        return DXGI_FORMAT_R16G16B16A16_FLOAT;
    }
    if (format == "DXGI_FORMAT_R32_FLOAT") return DXGI_FORMAT_R32_FLOAT;
    if (format == "DXGI_FORMAT_R32G32_FLOAT") {
        return DXGI_FORMAT_R32G32_FLOAT;
    }
    if (format == "DXGI_FORMAT_R32G32B32_FLOAT") {
        return DXGI_FORMAT_R32G32B32_FLOAT;
    }
    if (format == "DXGI_FORMAT_R32G32B32A32_FLOAT") {
        return DXGI_FORMAT_R32G32B32A32_FLOAT;
    }
    if (format == "DXGI_FORMAT_R32_UINT") return DXGI_FORMAT_R32_UINT;
    if (format == "DXGI_FORMAT_R10G10B10A2_UNORM") {
        return DXGI_FORMAT_R10G10B10A2_UNORM;
    }
    if (format == "DXGI_FORMAT_R11G11B10_FLOAT") {
        return DXGI_FORMAT_R11G11B10_FLOAT;
    }
    if (format == "DXGI_FORMAT_D32_FLOAT") return DXGI_FORMAT_D32_FLOAT;
    if (format == "DXGI_FORMAT_D24_UNORM_S8_UINT") {
        return DXGI_FORMAT_D24_UNORM_S8_UINT;
    }
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
    if (resource.usage == "uav"
#if NW06_COMPILE_ANY_RUNTIME
        || resource.usage == "uav_srv"
#endif
    ) {
        desc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(
            desc.Flags | D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    }
    if (resource.usage == "rtv"
#if NW06_COMPILE_ANY_RUNTIME
        || resource.usage == "rtv_srv"
#endif
    ) {
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

struct DerivedResourceUse {
    std::uint64_t first_frame = std::numeric_limits<std::uint64_t>::max();
    std::uint64_t last_frame = 0u;
    std::uint64_t activation_frame = std::numeric_limits<std::uint64_t>::max();
    bool frame_access = false;
    bool read = false;
    bool write = false;
    bool initialized = false;
    bool persistent = false;
    std::string alias_group;
};

bool BarrierStateWrites(std::uint32_t state) {
    switch (state) {
        case 3u:  // RENDER_TARGET
        case 4u:  // UNORDERED_ACCESS
        case 5u:  // DEPTH_WRITE
        case 9u:  // COPY_DEST
            return true;
        default:
            return false;
    }
}

#if NW06_COMPILE_ANY_RUNTIME
std::map<std::uint32_t, DerivedResourceUse> DeriveResourceUses(
    const package::Package& pkg) {
    std::map<std::uint32_t, DerivedResourceUse> uses;
    std::map<std::uint32_t, const package::ResourceDescriptor*> by_id;
    for (const auto& resource : pkg.resources) {
        DerivedResourceUse use{};
        use.alias_group = AliasCandidateGroup(resource);
        uses[resource.resource_id] = use;
        by_id[resource.resource_id] = &resource;
    }

    std::map<std::uint32_t, const package::RootSignatureRecord*> roots_by_id;
    for (const auto& root : pkg.root_signatures) {
        roots_by_id[root.root_signature_id] = &root;
    }
    const auto binding_usage_for = [&roots_by_id](std::uint32_t root_id,
                                                  std::uint32_t root_parameter)
        -> std::string {
        const auto root_it = roots_by_id.find(root_id);
        if (root_it == roots_by_id.end() || root_it->second == nullptr) {
            return {};
        }
        for (const auto& binding : root_it->second->bindings) {
            if (binding.root_parameter == root_parameter) {
                return binding.usage;
            }
        }
        return {};
    };
    const auto effective_usage =
        [&by_id](std::uint32_t resource_id,
                 const std::string& binding) -> std::string {
        if (!binding.empty()) return binding;
        const auto it = by_id.find(resource_id);
        return it == by_id.end() ? std::string{} : it->second->usage;
    };

    // Initialization-produced contents are lifetime roots: resources uploaded
    // or written by GPU initialization may not be aliased over by frame
    // scratch that is still expected to preserve those contents.
    std::map<std::uint32_t, std::map<std::uint32_t, std::string>>
        init_bound_by_root;
    std::uint32_t init_root_signature_id = 0u;
    for (const auto& instruction : pkg.initialization) {
        std::vector<std::uint32_t> refs;
        for (const auto& operand : instruction.operands) {
            if (operand.type == package::OperandType::ResourceRef) {
                refs.push_back(operand.u32);
            }
        }
        if (instruction.opcode == "init.set_root_signature") {
            const std::uint32_t root_id =
                instruction.operands.empty() ? 0u
                                             : instruction.operands[0].u32;
            if (root_id != init_root_signature_id) {
                init_root_signature_id = root_id;
                init_bound_by_root.clear();
            }
        } else if (instruction.opcode == "init.set_descriptor_table" ||
                   instruction.opcode == "init.set_root_constants") {
            const std::uint32_t root_parameter =
                instruction.operands.empty() ? 0u
                                             : instruction.operands[0].u32;
            const std::string binding =
                instruction.opcode == "init.set_root_constants"
                    ? std::string("cbv")
                    : binding_usage_for(init_root_signature_id,
                                        root_parameter);
            auto& root_bindings = init_bound_by_root[root_parameter];
            root_bindings.clear();
            for (const std::uint32_t resource_id : refs) {
                root_bindings[resource_id] =
                    effective_usage(resource_id, binding);
            }
        } else if (instruction.opcode == "init.dispatch") {
            for (const auto& root_binding : init_bound_by_root) {
                for (const auto& resource_binding : root_binding.second) {
                    const auto it = uses.find(resource_binding.first);
                    if (it == uses.end()) continue;
                    if (resource_binding.second == "uav" ||
                        resource_binding.second == "uav_srv") {
                        it->second.initialized = true;
                        it->second.write = true;
                    } else if (resource_binding.second == "srv" ||
                               resource_binding.second == "cbv") {
                        it->second.read = true;
                    }
                }
            }
        } else if (instruction.opcode == "init.upload_resource") {
            for (const std::uint32_t resource_id : refs) {
                auto it = uses.find(resource_id);
                if (it == uses.end()) continue;
                it->second.initialized = true;
                it->second.write = true;
            }
        }
    }

    // Descriptor bindings remain live for every subsequent frame.dispatch even
    // though those dispatches carry no ResourceRef operand.  Track the active
    // binding set, and derive read/write access from the active root binding's
    // usage rather than a combined uav_srv resource usage.
    std::map<std::uint32_t, std::map<std::uint32_t, std::string>>
        bound_by_root;
    std::uint32_t active_root_signature_id = 0u;
    std::uint64_t frame_index = 0u;
    auto mark = [&](std::uint32_t resource_id, bool read, bool write) {
        auto it = uses.find(resource_id);
        if (it == uses.end()) return;
        it->second.read = it->second.read || read;
        it->second.write = it->second.write || write;
        it->second.frame_access = true;
        it->second.first_frame =
            std::min(it->second.first_frame, frame_index);
        it->second.activation_frame =
            std::min(it->second.activation_frame, frame_index);
        it->second.last_frame =
            std::max(it->second.last_frame, frame_index);
    };
    for (const auto& instruction : pkg.frame) {
        const auto refs = [&instruction]() {
            std::vector<std::uint32_t> values;
            for (const auto& operand : instruction.operands) {
                if (operand.type == package::OperandType::ResourceRef) {
                    values.push_back(operand.u32);
                }
            }
            return values;
        };
        if (instruction.opcode == "frame.set_root_signature" ||
            instruction.opcode == "frame.set_graphics_root_signature") {
            const std::uint32_t root_id =
                instruction.operands.empty() ? 0u
                                             : instruction.operands[0].u32;
            if (root_id != active_root_signature_id) {
                active_root_signature_id = root_id;
                bound_by_root.clear();
            }
        } else if (instruction.opcode == "frame.set_descriptor_table" ||
            instruction.opcode == "frame.set_root_constants" ||
            instruction.opcode == "frame.set_graphics_descriptor_table" ||
            instruction.opcode == "frame.set_graphics_root_constants") {
            const std::uint32_t root_parameter =
                instruction.operands.empty()
                    ? 0u
                    : instruction.operands[0].u32;
            const bool root_constants =
                instruction.opcode == "frame.set_root_constants" ||
                instruction.opcode == "frame.set_graphics_root_constants";
            const std::string binding =
                root_constants
                    ? std::string("cbv")
                    : binding_usage_for(active_root_signature_id,
                                        root_parameter);
            auto& root_bindings = bound_by_root[root_parameter];
            root_bindings.clear();
            for (const std::uint32_t resource_id : refs()) {
                root_bindings[resource_id] =
                    effective_usage(resource_id, binding);
                mark(resource_id, true, false);
            }
        } else if (instruction.opcode == "frame.dispatch") {
            for (const auto& binding : bound_by_root) {
                for (const auto& resource_binding : binding.second) {
                    if (resource_binding.second == "uav" ||
                        resource_binding.second == "uav_srv") {
                        mark(resource_binding.first, true, true);
                    } else if (resource_binding.second == "srv" ||
                               resource_binding.second == "cbv") {
                        mark(resource_binding.first, true, false);
                    }
                }
            }
        } else if (instruction.opcode == "frame.set_render_targets") {
            std::vector<std::uint32_t> ids = refs();
            if (!ids.empty()) mark(ids[0], false, true);
            if (ids.size() > 1u) mark(ids[1], false, true);
        } else if (instruction.opcode == "frame.execute_indirect") {
            for (const auto resource_id : refs()) {
                mark(resource_id, true, false);
            }
            for (const auto& binding : bound_by_root) {
                for (const auto& resource_binding : binding.second) {
                    mark(resource_binding.first, true, false);
                }
            }
            for (const auto& resource : pkg.resources) {
                if (resource.name == "vertices" ||
                    resource.name == "indices") {
                    mark(resource.resource_id, true, false);
                }
            }
        } else if (instruction.opcode == "frame.copy_buffer") {
            std::vector<std::uint32_t> ids = refs();
            if (!ids.empty()) mark(ids[0], true, false);
            if (ids.size() > 1u) mark(ids[1], false, true);
        } else if (instruction.opcode == "frame.barrier") {
            if (!instruction.operands.empty()) {
                const bool writes =
                    BarrierStateWrites(instruction.operands[0].u32);
                for (const auto resource_id : refs()) {
                    mark(resource_id, true, writes);
                }
            }
        } else if (instruction.opcode == "frame.present") {
            for (const auto resource_id : refs()) {
                mark(resource_id, true, false);
            }
        } else {
            for (const auto resource_id : refs()) {
                mark(resource_id, true, false);
            }
        }
        ++frame_index;
    }

    for (auto& entry : uses) {
        DerivedResourceUse& use = entry.second;
        const auto resource_it = by_id.find(entry.first);
        if (resource_it == by_id.end()) continue;
        const std::string& usage = resource_it->second->usage;
        // Constant/uniform inputs, samples, readback targets, and any resource
        // whose contents originate in initialization persist across frames.
        use.persistent = usage == "cbv" || usage == "sampler" ||
                         usage == "readback" || use.initialized ||
                         (use.read && !use.write);
        if (!use.frame_access) {
            use.first_frame = 0u;
            use.last_frame = 0u;
        }
    }
    return uses;
}

#else
std::map<std::uint32_t, DerivedResourceUse> DeriveResourceUses(
    const package::Package& pkg) {
    std::map<std::uint32_t, DerivedResourceUse> uses;
    std::map<std::uint32_t, const package::ResourceDescriptor*> by_id;
    for (const auto& resource : pkg.resources) {
        DerivedResourceUse use{};
        use.alias_group = AliasCandidateGroup(resource);
        uses[resource.resource_id] = use;
        by_id[resource.resource_id] = &resource;
    }

    // Initialization-produced contents are lifetime roots: a resource uploaded
    // during initialization may not be aliased over by frame scratch that is
    // still expected to preserve those contents.
    for (const auto& instruction : pkg.initialization) {
        for (const auto& operand : instruction.operands) {
            if (operand.type != package::OperandType::ResourceRef) continue;
            auto it = uses.find(operand.u32);
            if (it == uses.end()) continue;
            if (instruction.opcode == "init.upload_resource") {
                it->second.initialized = true;
                it->second.write = true;
            }
        }
    }

    // Descriptor bindings remain live for every subsequent frame.dispatch even
    // though those dispatches carry no ResourceRef operand.  Track the active
    // binding set, and derive read/write access from each resource's usage.
    std::map<std::uint32_t, std::set<std::uint32_t>> bound_by_root;
    std::uint32_t active_root_signature_id = 0u;
    std::uint64_t frame_index = 0u;
    auto mark = [&](std::uint32_t resource_id, bool read, bool write) {
        auto it = uses.find(resource_id);
        if (it == uses.end()) return;
        it->second.read = it->second.read || read;
        it->second.write = it->second.write || write;
        it->second.frame_access = true;
        it->second.first_frame =
            std::min(it->second.first_frame, frame_index);
        it->second.activation_frame =
            std::min(it->second.activation_frame, frame_index);
        it->second.last_frame =
            std::max(it->second.last_frame, frame_index);
    };
    for (const auto& instruction : pkg.frame) {
        const auto refs = [&instruction]() {
            std::vector<std::uint32_t> values;
            for (const auto& operand : instruction.operands) {
                if (operand.type == package::OperandType::ResourceRef) {
                    values.push_back(operand.u32);
                }
            }
            return values;
        };
        if (instruction.opcode == "frame.set_root_signature" ||
            instruction.opcode == "frame.set_graphics_root_signature") {
            const std::uint32_t root_id =
                instruction.operands.empty() ? 0u
                                             : instruction.operands[0].u32;
            if (root_id != active_root_signature_id) {
                active_root_signature_id = root_id;
                bound_by_root.clear();
            }
        } else if (instruction.opcode == "frame.set_descriptor_table" ||
            instruction.opcode == "frame.set_root_constants") {
            const std::uint32_t root_parameter =
                instruction.operands.empty()
                    ? 0u
                    : instruction.operands[0].u32;
            bound_by_root[root_parameter] = std::set<std::uint32_t>{};
            for (const auto resource_id : refs()) {
                bound_by_root[root_parameter].insert(resource_id);
                if (instruction.opcode == "frame.set_root_constants") {
                    mark(resource_id, true, false);
                }
            }
        } else if (instruction.opcode == "frame.dispatch") {
            for (const auto& binding : bound_by_root) {
                for (const auto resource_id : binding.second) {
                    const auto resource_it = by_id.find(resource_id);
                    if (resource_it == by_id.end()) continue;
                    const std::string& usage = resource_it->second->usage;
                    if (usage == "uav") {
                        mark(resource_id, true, true);
                    } else if (usage == "srv" || usage == "cbv") {
                        mark(resource_id, true, false);
                    }
                }
            }
        } else if (instruction.opcode == "frame.copy_buffer") {
            std::vector<std::uint32_t> ids = refs();
            if (!ids.empty()) mark(ids[0], true, false);
            if (ids.size() > 1u) mark(ids[1], false, true);
        } else if (instruction.opcode == "frame.barrier") {
            if (!instruction.operands.empty()) {
                const bool writes =
                    BarrierStateWrites(instruction.operands[0].u32);
                for (const auto resource_id : refs()) {
                    mark(resource_id, true, writes);
                }
            }
        } else if (instruction.opcode == "frame.present") {
            for (const auto resource_id : refs()) {
                mark(resource_id, true, false);
            }
        } else {
            for (const auto resource_id : refs()) {
                mark(resource_id, true, false);
            }
        }
        ++frame_index;
    }

    for (auto& entry : uses) {
        DerivedResourceUse& use = entry.second;
        const auto resource_it = by_id.find(entry.first);
        if (resource_it == by_id.end()) continue;
        const std::string& usage = resource_it->second->usage;
        // Constant/uniform inputs, samples, readback targets, and any resource
        // whose contents originate in initialization persist across frames.
        use.persistent = usage == "cbv" || usage == "sampler" ||
                         usage == "readback" || use.initialized ||
                         (use.read && !use.write);
        if (!use.frame_access) {
            use.first_frame = 0u;
            use.last_frame = 0u;
        }
    }
    return uses;
}

#endif

void DeriveLifetimes(const package::Package& pkg, ResourcePlan* plan) {
    const auto uses = DeriveResourceUses(pkg);
    for (auto& placement : plan->placements) {
        const auto it = uses.find(placement.descriptor.resource_id);
        placement.lifetime.resource_id = placement.descriptor.resource_id;
        placement.lifetime.permitted_alias_group =
            AliasCandidateGroup(placement.descriptor);
        if (it == uses.end()) {
            placement.lifetime.first_use_frame =
                std::numeric_limits<std::uint64_t>::max();
            placement.lifetime.last_use_frame = 0u;
            continue;
        }
        placement.lifetime.referenced_by_frame = it->second.frame_access;
        placement.lifetime.first_use_frame = it->second.first_frame;
        placement.lifetime.last_use_frame = it->second.last_frame;
    }
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
    const auto derived_uses = DeriveResourceUses(pkg);

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
        const auto use_it = derived_uses.find(resource.resource_id);
        persistent[resource.resource_id] =
            use_it != derived_uses.end() && use_it->second.persistent;
        if (!persistent[resource.resource_id]) {
            groups[AliasCandidateGroup(resource)].push_back(resource.resource_id);
        }
    }

    // Derive complete binding-aware lifetimes.  A descriptor-table binding
    // makes its resource live for every following dispatch until rebinding,
    // even though frame.dispatch itself contains no ResourceRef operand.
    for (const auto& resource : pkg.resources) {
        ResourceLogicalLifetime lifetime{};
        lifetime.resource_id = resource.resource_id;
        lifetime.permitted_alias_group = AliasCandidateGroup(resource);
        const auto use_it = derived_uses.find(resource.resource_id);
        if (use_it != derived_uses.end()) {
            lifetime.referenced_by_frame = use_it->second.frame_access;
            lifetime.first_use_frame = use_it->second.first_frame;
            lifetime.last_use_frame = use_it->second.last_frame;
        } else {
            lifetime.first_use_frame =
                std::numeric_limits<std::uint64_t>::max();
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
                if (is_persistent) continue;
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
                const auto use_it =
                    derived_uses.find(resource.resource_id);
                barrier.activation_instruction =
                    static_cast<std::uint32_t>(
                        std::min<std::uint64_t>(
                            use_it == derived_uses.end()
                                ? placement.lifetime.first_use_frame
                                : use_it->second.activation_frame,
                            std::numeric_limits<std::uint32_t>::max()));
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

    // Complete alias activation order.  A region reused by A then B needs
    // A->B before B and B->A at the next frame boundary before A.  The
    // activation index is the first frame instruction that uses the incoming
    // resource, so descriptor/execution order (not package resource order)
    // drives emission.
    std::map<std::pair<std::uint32_t, std::uint64_t>, std::set<std::uint32_t>>
        regions;
    for (const auto& placement : result.plan.placements) {
        regions[{placement.frame_slot_instance,
                 placement.heap_offset_bytes}]
            .insert(placement.descriptor.resource_id);
    }
    std::set<std::pair<std::uint32_t, std::uint32_t>> barrier_keys;
    for (const auto& barrier : result.plan.alias_barriers) {
        barrier_keys.insert(
            {barrier.before_resource_id, barrier.resource_id});
    }
    for (const auto& region : regions) {
        if (region.second.size() < 2u) continue;
        std::vector<std::uint32_t> ordered(region.second.begin(),
                                           region.second.end());
        std::sort(
            ordered.begin(), ordered.end(),
            [&derived_uses](std::uint32_t left, std::uint32_t right) {
                const auto left_it = derived_uses.find(left);
                const auto right_it = derived_uses.find(right);
                const std::uint64_t left_frame =
                    left_it == derived_uses.end()
                        ? 0u
                        : left_it->second.activation_frame;
                const std::uint64_t right_frame =
                    right_it == derived_uses.end()
                        ? 0u
                        : right_it->second.activation_frame;
                return left_frame == right_frame
                           ? left < right
                           : left_frame < right_frame;
            });
        for (std::size_t index = 0u; index < ordered.size(); ++index) {
            const std::uint32_t resource_id = ordered[index];
            const std::uint32_t previous =
                ordered[(index + ordered.size() - 1u) % ordered.size()];
            if (!barrier_keys
                     .insert({previous, resource_id})
                     .second) {
                continue;
            }
            ResourceAliasBarrier activation{};
            activation.resource_id = resource_id;
            activation.before_resource_id = previous;
            activation.group = lifetimes[resource_id].permitted_alias_group;
            activation.uav_barrier = true;
            activation.reason =
                index == 0u
                    ? "frame-boundary alias activation before reuse"
                    : "execution-order alias activation";
            activation.activation_instruction =
                static_cast<std::uint32_t>(
                    std::min<std::uint64_t>(
                        derived_uses.count(resource_id) != 0u
                            ? derived_uses.at(resource_id).activation_frame
                            : lifetimes[resource_id].first_use_frame,
                        std::numeric_limits<std::uint32_t>::max()));
            activation.frame_boundary = index == 0u;
            result.plan.alias_barriers.push_back(std::move(activation));
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
                                std::uint32_t heap_type,
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
    desc.Properties.Type = static_cast<D3D12_HEAP_TYPE>(heap_type);
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
    (void)heap_type;
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

bool IsPlaceableAliasResource(
    const package::ResourceDescriptor& resource) {
#if defined(_WIN32)
    if (resource.usage == "sampler" || resource.usage == "readback") {
        return true;
    }
    if (resource.usage == "upload") return false;
    if (resource.kind == "buffer") return true;
    if (resource.kind != "texture2d" && resource.kind != "texture3d") {
        return false;
    }
    return PlacedFormat(resource.format) != DXGI_FORMAT_UNKNOWN;
#else
    (void)resource;
    return true;
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
