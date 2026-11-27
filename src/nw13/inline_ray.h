#pragma once

// Only linked into the selected film specialization. The swept boxes are an
// acceleration structure for candidate selection. Intersections and normals
// still come from the current frame's solid field and object transforms.
#include "nw13_inline_ray_payload.h"
#include <d3d12.h>
#include <wrl/client.h>
#include <cstring>
#include <iterator>

namespace nw::film {
using Microsoft::WRL::ComPtr;
inline constexpr D3D12_SHADER_BYTECODE kShader{kFilmRayDxil, sizeof(kFilmRayDxil)};
inline constexpr UINT kBoxCount = static_cast<UINT>(std::size(kFilmRayBounds));
static_assert(kBoxCount && kBoxCount <= 32);
inline constexpr UINT64 kAccelerationBudget = 16ull * 1024 * 1024;

inline bool Supported(ID3D12Device* device) {
    D3D12_FEATURE_DATA_SHADER_MODEL shader{D3D_SHADER_MODEL_6_5};
    D3D12_FEATURE_DATA_D3D12_OPTIONS5 options{};
    return SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &shader, sizeof(shader))) &&
           shader.HighestShaderModel >= D3D_SHADER_MODEL_6_5 &&
           SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &options, sizeof(options))) &&
           options.RaytracingTier >= D3D12_RAYTRACING_TIER_1_1;
}

inline HRESULT Root(ID3D12Device* device, ID3D12RootSignature** root) {
    D3D12_DESCRIPTOR_RANGE range{};
    range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    range.NumDescriptors = 1;
    D3D12_ROOT_PARAMETER parameters[3]{};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    parameters[0].Constants.Num32BitValues = 4;
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    parameters[1].DescriptorTable = {1, &range};
    parameters[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
    D3D12_ROOT_SIGNATURE_DESC desc{};
    desc.NumParameters = static_cast<UINT>(std::size(parameters));
    desc.pParameters = parameters;
    ComPtr<ID3DBlob> blob;
    const HRESULT hr = D3D12SerializeRootSignature(&desc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr);
    if (FAILED(hr)) return hr;
    return device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(root));
}

struct Acceleration {
    ComPtr<ID3D12Resource> boxes, instance, bottom, top, scratch;
    UINT64 bytes = 0, upload = 0;

    bool Buffer(ID3D12Device* device, UINT64 size, D3D12_HEAP_TYPE heap,
                D3D12_RESOURCE_STATES state, ComPtr<ID3D12Resource>& resource) {
        if (!size || size > kAccelerationBudget) return false;
        D3D12_HEAP_PROPERTIES properties{};
        properties.Type = heap;
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = size;
        desc.Height = 1;
        desc.DepthOrArraySize = 1;
        desc.MipLevels = 1;
        desc.SampleDesc.Count = 1;
        desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (heap == D3D12_HEAP_TYPE_DEFAULT) desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        const auto size_on_device = device->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
        if (!size_on_device || size_on_device > kAccelerationBudget - bytes - upload) return false;
        if (FAILED(device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc,
                                                   state, nullptr, IID_PPV_ARGS(&resource)))) return false;
        if (heap == D3D12_HEAP_TYPE_UPLOAD) upload += size_on_device;
        else bytes += size_on_device;
        return true;
    }

    bool Upload(ID3D12Device* device, const void* data, SIZE_T size, ComPtr<ID3D12Resource>& resource) {
        if (!Buffer(device, size, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ,
                    resource)) return false;
        void* target = nullptr;
        const D3D12_RANGE none{0, 0};
        if (FAILED(resource->Map(0, &none, &target))) return false;
        std::memcpy(target, data, size);
        const D3D12_RANGE written{0, size};
        resource->Unmap(0, &written);
        return true;
    }

    bool Record(ID3D12Device* device, ID3D12GraphicsCommandList* list) {
        ComPtr<ID3D12Device5> device5;
        ComPtr<ID3D12GraphicsCommandList4> command;
        if (FAILED(device->QueryInterface(IID_PPV_ARGS(&device5))) ||
            FAILED(list->QueryInterface(IID_PPV_ARGS(&command)))) return false;
        if (!Upload(device, kFilmRayBounds, sizeof(kFilmRayBounds), boxes)) return false;
        D3D12_RAYTRACING_GEOMETRY_DESC geometry{};
        geometry.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_PROCEDURAL_PRIMITIVE_AABBS;
        geometry.AABBs.AABBCount = kBoxCount;
        geometry.AABBs.AABBs = {boxes->GetGPUVirtualAddress(), sizeof(D3D12_RAYTRACING_AABB)};
        D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC build_bottom{};
        build_bottom.Inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
        build_bottom.Inputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
        build_bottom.Inputs.NumDescs = 1;
        build_bottom.Inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
        build_bottom.Inputs.pGeometryDescs = &geometry;

        D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC build_top{};
        build_top.Inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL;
        build_top.Inputs.Flags = build_bottom.Inputs.Flags;
        build_top.Inputs.NumDescs = 1;
        build_top.Inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;

        D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO bottom_sizes{}, top_sizes{};
        device5->GetRaytracingAccelerationStructurePrebuildInfo(&build_bottom.Inputs, &bottom_sizes);
        device5->GetRaytracingAccelerationStructurePrebuildInfo(&build_top.Inputs, &top_sizes);
        constexpr auto local = D3D12_HEAP_TYPE_DEFAULT;
        constexpr auto acceleration = D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE;
        if (!Buffer(device, bottom_sizes.ResultDataMaxSizeInBytes, local, acceleration, bottom) ||
            !Buffer(device, top_sizes.ResultDataMaxSizeInBytes, local, acceleration, top)) return false;
        const UINT64 scratch_size = (bottom_sizes.ScratchDataSizeInBytes > top_sizes.ScratchDataSizeInBytes)
            ? bottom_sizes.ScratchDataSizeInBytes : top_sizes.ScratchDataSizeInBytes;
        if (!Buffer(device, scratch_size, local, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, scratch)) return false;

        D3D12_RAYTRACING_INSTANCE_DESC instance_desc{};
        instance_desc.Transform[0][0] = instance_desc.Transform[1][1] = instance_desc.Transform[2][2] = 1;
        instance_desc.InstanceMask = 255;
        instance_desc.AccelerationStructure = bottom->GetGPUVirtualAddress();
        if (!Upload(device, &instance_desc, sizeof(instance_desc), instance)) return false;
        build_bottom.ScratchAccelerationStructureData = scratch->GetGPUVirtualAddress();
        build_bottom.DestAccelerationStructureData = bottom->GetGPUVirtualAddress();
        build_top.ScratchAccelerationStructureData = scratch->GetGPUVirtualAddress();
        build_top.DestAccelerationStructureData = top->GetGPUVirtualAddress();
        build_top.Inputs.InstanceDescs = instance->GetGPUVirtualAddress();

        D3D12_RESOURCE_BARRIER barrier{};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
        command->BuildRaytracingAccelerationStructure(&build_bottom, 0, nullptr);
        command->ResourceBarrier(1, &barrier);
        command->BuildRaytracingAccelerationStructure(&build_top, 0, nullptr);
        command->ResourceBarrier(1, &barrier);
        return true;
    }
};
}
