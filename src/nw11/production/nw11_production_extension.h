#pragma once

// Optional implementations of the existing PackagePlayer graphics instruction.
// Included exclusively by the NW-11 specialization. All resource creation,
// graph initialization, frame scheduling, post-processing, swap chain and
// diagnostic capture outside these two bounded operations remain PackagePlayer.
#include <wrl/client.h>
#include <array>
#include <bit>
#include <cstdint>
#include <cstring>
#include <limits>
#include "nw11_scene_generated.h"

#if NW11_VARIANT_MESH
#include "nw11_torus_amplification_as_dxil.h"
#include "nw11_torus_meshlet_ms_dxil.h"
#endif
#if NW11_VARIANT_RAY
#include "nw11_scene_rayquery_ps_dxil.h"
#include "nw11_deform_cs_dxil.h"
#endif
#if NW11_BASELINE_ANALYTIC
#include "nw11_analytic_reflection_ps_dxil.h"
#endif

namespace nw11::production {
using Microsoft::WRL::ComPtr;
constexpr unsigned kVertices = 3, kIndices = 4, kInstances = 6;
constexpr unsigned kMeshlets = (scene::kMajorSegments / 8u) * (scene::kMinorSegments / 8u);
static_assert(scene::kMajorSegments % 8u == 0 && scene::kMinorSegments % 8u == 0);
static_assert(scene::kInstanceCount * kMeshlets <= 65535u);

template <std::size_t N>
D3D12_SHADER_BYTECODE Code(const unsigned char (&bytes)[N]) noexcept {
    return {bytes, N};
}

// 0 = supported, 3 = queried absence, 4 = query failed/unknown.
inline int Capability(ID3D12Device* device) noexcept {
#if NW11_VARIANT_MESH || NW11_VARIANT_RAY
    D3D12_FEATURE_DATA_SHADER_MODEL sm{D3D_SHADER_MODEL_6_5};
    if (FAILED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm, sizeof(sm)))) return 4;
    if (sm.HighestShaderModel < D3D_SHADER_MODEL_6_5) return 3;
#endif
#if NW11_VARIANT_MESH
    D3D12_FEATURE_DATA_D3D12_OPTIONS7 options{};
    if (FAILED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS7, &options, sizeof(options)))) return 4;
    if (options.MeshShaderTier < D3D12_MESH_SHADER_TIER_1) return 3;
#elif NW11_VARIANT_RAY
    D3D12_FEATURE_DATA_D3D12_OPTIONS5 options{};
    if (FAILED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &options, sizeof(options)))) return 4;
    if (options.RaytracingTier < D3D12_RAYTRACING_TIER_1_1) return 3;
#else
    (void)device;
#endif
    return 0;
}

#if NW11_VARIANT_MESH || NW11_VARIANT_RAY
inline HRESULT GraphicsRoot(ID3D12Device* device, ID3D12RootSignature** out) noexcept {
    // Preserve NW-06's original root parameter indices (b0 and t0..t4).
    // Append only the raw geometry bindings and variant-specific parameters.
    D3D12_ROOT_PARAMETER params[9]{};
    D3D12_DESCRIPTOR_RANGE ranges[5]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants = {0, 0, 8};
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    for (unsigned i = 0; i < 5; ++i) {
        ranges[i] = {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, i, 0, 0};
        params[1+i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[1+i].DescriptorTable = {1, &ranges[i]};
        params[1+i].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    }
    for (unsigned i = 6; i <= 7; ++i) {
        params[i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
        params[i].Descriptor = {i, 0};
        params[i].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    }
#if NW11_VARIANT_MESH
    params[8].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[8].Constants = {1, 0, 8};
    constexpr auto flags = D3D12_ROOT_SIGNATURE_FLAG_NONE;
#else
    params[8].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
    params[8].Descriptor = {5, 0};
    constexpr auto flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;
#endif
    params[8].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC desc{9, params, 0, nullptr, flags};
    ComPtr<ID3DBlob> blob;
    HRESULT hr = D3D12SerializeRootSignature(&desc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr);
    if (FAILED(hr)) return hr;
    return device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(out));
}
#endif

#if NW11_VARIANT_MESH
template <D3D12_PIPELINE_STATE_SUBOBJECT_TYPE Type, typename T>
struct alignas(void*) Subobject {
    D3D12_PIPELINE_STATE_SUBOBJECT_TYPE type = Type;
    T value{};
};
inline HRESULT MeshPipeline(ID3D12Device* device,
    const D3D12_GRAPHICS_PIPELINE_STATE_DESC& desc,
    ID3D12PipelineState** out) noexcept {
    struct Stream {
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_ROOT_SIGNATURE, ID3D12RootSignature*> root;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_AS, D3D12_SHADER_BYTECODE> as;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_MS, D3D12_SHADER_BYTECODE> ms;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PS, D3D12_SHADER_BYTECODE> ps;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_BLEND, D3D12_BLEND_DESC> blend;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_SAMPLE_MASK, UINT> mask;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_RASTERIZER, D3D12_RASTERIZER_DESC> raster;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL, D3D12_DEPTH_STENCIL_DESC> depth;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_PRIMITIVE_TOPOLOGY, D3D12_PRIMITIVE_TOPOLOGY_TYPE> topology;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_RENDER_TARGET_FORMATS, D3D12_RT_FORMAT_ARRAY> rtv;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_DEPTH_STENCIL_FORMAT, DXGI_FORMAT> dsv;
        Subobject<D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_SAMPLE_DESC, DXGI_SAMPLE_DESC> samples;
    } stream{};
    stream.root.value = desc.pRootSignature;
    stream.as.value = Code(kNw11TorusAsDxil);
    stream.ms.value = Code(kNw11TorusMsDxil);
    stream.ps.value = desc.PS;
    stream.blend.value = desc.BlendState;
    stream.mask.value = desc.SampleMask;
    stream.raster.value = desc.RasterizerState;
    stream.depth.value = desc.DepthStencilState;
    stream.topology.value = desc.PrimitiveTopologyType;
    stream.rtv.value.NumRenderTargets = desc.NumRenderTargets;
    stream.rtv.value.RTFormats[0] = desc.RTVFormats[0];
    stream.dsv.value = desc.DSVFormat;
    stream.samples.value = desc.SampleDesc;
    ComPtr<ID3D12Device2> device2;
    HRESULT hr = device->QueryInterface(IID_PPV_ARGS(&device2));
    if (FAILED(hr)) return hr;
    D3D12_PIPELINE_STATE_STREAM_DESC stream_desc{sizeof(stream), &stream};
    return device2->CreatePipelineState(&stream_desc, IID_PPV_ARGS(out));
}
#endif

template<class State>
typename State::ResourceSlot* Slot(State& state, unsigned id) noexcept {
    for (auto& slot : state.resources)
        if (slot.resource_id == id && slot.resource != nullptr) return &slot;
    return nullptr;
}

inline void Transition(ID3D12GraphicsCommandList* list, ID3D12Resource* resource,
    D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) noexcept {
    if (before == after) return;
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition = {resource, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, before, after};
    list->ResourceBarrier(1, &barrier);
}
template<class SlotType>
void TransitionSlot(ID3D12GraphicsCommandList* list, SlotType& slot,
    D3D12_RESOURCE_STATES after) noexcept {
    Transition(list, slot.resource, slot.state, after);
    slot.state = after;
}
inline void UavBarrier(ID3D12GraphicsCommandList* list, ID3D12Resource* resource) noexcept {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
    barrier.UAV.pResource = resource;
    list->ResourceBarrier(1, &barrier);
}

#if NW11_VARIANT_MESH
template<class State>
HRESULT DispatchMesh(State& state, ID3D12GraphicsCommandList* list) noexcept {
    auto* vertices = Slot(state, kVertices);
    auto* indices = Slot(state, kIndices);
    if (!vertices || !indices) return E_INVALIDARG;
    ComPtr<ID3D12GraphicsCommandList6> mesh;
    HRESULT hr = list->QueryInterface(IID_PPV_ARGS(&mesh));
    if (FAILED(hr)) return hr;
    TransitionSlot(list, *vertices, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    TransitionSlot(list, *indices, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    list->SetGraphicsRootShaderResourceView(6, vertices->resource->GetGPUVirtualAddress());
    list->SetGraphicsRootShaderResourceView(7, indices->resource->GetGPUVirtualAddress());
    const std::uint32_t constants[8] = {
        scene::kMajorSegments, scene::kMinorSegments,
        std::bit_cast<std::uint32_t>(scene::kMajorRadius),
        std::bit_cast<std::uint32_t>(scene::kMinorRadius),
        scene::kVertexCount, scene::kIndexCount, scene::kInstanceCount, kMeshlets};
    list->SetGraphicsRoot32BitConstants(8, 8, constants, 0);
    mesh->DispatchMesh(scene::kInstanceCount * kMeshlets, 1, 1);
    return S_OK;
}
#endif

#if NW11_VARIANT_RAY
struct RayState {
    ComPtr<ID3D12Device5> device;
    ComPtr<ID3D12Resource> positions, blas, tlas, blas_scratch, tlas_scratch, instances, readback;
    ComPtr<ID3D12RootSignature> deform_root;
    ComPtr<ID3D12PipelineState> deform_pipeline;
    D3D12_RAYTRACING_GEOMETRY_DESC geometry{};
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS blas_inputs{}, tlas_inputs{};
    D3D12_RESOURCE_STATES position_state = D3D12_RESOURCE_STATE_COMMON;
};

template<class Note>
HRESULT Allocate(ID3D12Device* device, std::uint64_t size, D3D12_HEAP_TYPE type,
    D3D12_RESOURCE_FLAGS flags, D3D12_RESOURCE_STATES initial,
    ComPtr<ID3D12Resource>& resource, Note note) noexcept {
    if (!size) return E_INVALIDARG;
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = type;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Flags = flags;
    const auto info = device->GetResourceAllocationInfo(0, 1, &desc);
    if (info.SizeInBytes == UINT64_MAX || info.SizeInBytes == 0) return E_INVALIDARG;
    HRESULT hr = device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, initial, nullptr, IID_PPV_ARGS(&resource));
    if (SUCCEEDED(hr)) note(info.SizeInBytes, type == D3D12_HEAP_TYPE_UPLOAD);
    return hr;
}

template<class State, class Note>
HRESULT PrepareRay(State& state, Note note) noexcept {
    auto& ray = state.nw11_ray;
    auto* indices = Slot(state, kIndices);
    auto* vertices = Slot(state, kVertices);
    auto* instances = Slot(state, kInstances);
    if (!indices || !vertices || !instances) return E_INVALIDARG;
    HRESULT hr = state.device->QueryInterface(IID_PPV_ARGS(&ray.device));
    if (FAILED(hr)) return hr;
    constexpr auto uav = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    constexpr auto common = D3D12_RESOURCE_STATE_COMMON;
    constexpr auto as = D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE;
    constexpr auto scratch = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    auto buffer = [&](std::uint64_t size, D3D12_RESOURCE_STATES initial, ComPtr<ID3D12Resource>& resource) {
        return Allocate(state.device, size, D3D12_HEAP_TYPE_DEFAULT, uav, initial, resource, note);
    };
    hr = buffer(std::uint64_t(scene::kVertexCount) * 12u, common, ray.positions);
    if (FAILED(hr)) return hr;
    hr = Allocate(state.device, std::uint64_t(scene::kInstanceCount) * 80u,
        D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_FLAG_NONE,
        D3D12_RESOURCE_STATE_COPY_DEST, ray.readback, note);
    if (FAILED(hr)) return hr;
    hr = Allocate(state.device, std::uint64_t(scene::kInstanceCount) * sizeof(D3D12_RAYTRACING_INSTANCE_DESC),
        D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE,
        D3D12_RESOURCE_STATE_GENERIC_READ, ray.instances, note);
    if (FAILED(hr)) return hr;
    ray.geometry.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    ray.geometry.Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE;
    auto& tri = ray.geometry.Triangles;
    tri.IndexFormat = DXGI_FORMAT_R32_UINT;
    tri.IndexCount = scene::kIndexCount;
    tri.IndexBuffer = indices->resource->GetGPUVirtualAddress();
    tri.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    tri.VertexCount = scene::kVertexCount;
    tri.VertexBuffer = {ray.positions->GetGPUVirtualAddress(), 12};
    ray.blas_inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    ray.blas_inputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
    ray.blas_inputs.NumDescs = 1;
    ray.blas_inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    ray.blas_inputs.pGeometryDescs = &ray.geometry;
    ray.tlas_inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL;
    ray.tlas_inputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
    ray.tlas_inputs.NumDescs = scene::kInstanceCount;
    ray.tlas_inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    ray.tlas_inputs.InstanceDescs = ray.instances->GetGPUVirtualAddress();
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO bottom{}, top{};
    ray.device->GetRaytracingAccelerationStructurePrebuildInfo(&ray.blas_inputs, &bottom);
    ray.device->GetRaytracingAccelerationStructurePrebuildInfo(&ray.tlas_inputs, &top);
    if (FAILED(hr = buffer(bottom.ResultDataMaxSizeInBytes, as, ray.blas))) return hr;
    if (FAILED(hr = buffer(bottom.ScratchDataSizeInBytes, scratch, ray.blas_scratch))) return hr;
    if (FAILED(hr = buffer(top.ResultDataMaxSizeInBytes, as, ray.tlas))) return hr;
    if (FAILED(hr = buffer(top.ScratchDataSizeInBytes, scratch, ray.tlas_scratch))) return hr;

    D3D12_ROOT_PARAMETER params[3]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants = {0, 0, 4};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
    params[1].Descriptor = {0, 0};
    params[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    params[2].Descriptor = {0, 0};
    D3D12_ROOT_SIGNATURE_DESC root{3, params, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> blob;
    hr = D3D12SerializeRootSignature(&root, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr);
    if (FAILED(hr)) return hr;
    hr = state.device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(&ray.deform_root));
    if (FAILED(hr)) return hr;
    D3D12_COMPUTE_PIPELINE_STATE_DESC pso{};
    pso.pRootSignature = ray.deform_root.Get();
    pso.CS = Code(kNw11DeformDxil);
    return state.device->CreateComputePipelineState(&pso, IID_PPV_ARGS(&ray.deform_pipeline));
}

template<class State>
HRESULT CopyInstances(State& state, ID3D12GraphicsCommandList* list) noexcept {
    auto* slot = Slot(state, kInstances);
    if (!slot) return E_INVALIDARG;
    const auto original = slot->state;
    TransitionSlot(list, *slot, D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->CopyBufferRegion(state.nw11_ray.readback.Get(), 0, slot->resource, 0,
        std::uint64_t(scene::kInstanceCount) * 80u);
    TransitionSlot(list, *slot, original);
    return S_OK;
}
inline HRESULT PopulateInstances(RayState& ray) noexcept {
    void* source = nullptr;
    D3D12_RANGE read_range{0, std::size_t(scene::kInstanceCount) * 80u};
    HRESULT hr = ray.readback->Map(0, &read_range, &source);
    if (FAILED(hr)) return hr;
    void* target = nullptr;
    D3D12_RANGE empty{0, 0};
    hr = ray.instances->Map(0, &empty, &target);
    if (FAILED(hr)) { ray.readback->Unmap(0, &empty); return hr; }
    const auto* floats = static_cast<const float*>(source);
    auto* output = static_cast<D3D12_RAYTRACING_INSTANCE_DESC*>(target);
    for (unsigned i = 0; i < scene::kInstanceCount; ++i) {
        auto& desc = output[i];
        std::memset(&desc, 0, sizeof(desc));
        const float* matrix = floats + std::size_t(i) * 20;
        for (unsigned row = 0; row < 3; ++row)
            for (unsigned col = 0; col < 4; ++col)
                desc.Transform[row][col] = matrix[col * 4 + row];
        desc.InstanceID = i;
        desc.InstanceMask = 0xff;
        desc.Flags = D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_OPAQUE;
        desc.AccelerationStructure = ray.blas->GetGPUVirtualAddress();
    }
    D3D12_RANGE written{0, std::size_t(scene::kInstanceCount) * sizeof(*output)};
    ray.instances->Unmap(0, &written);
    ray.readback->Unmap(0, &empty);
    return S_OK;
}

template<class State>
HRESULT BuildRayFrame(State& state, ID3D12GraphicsCommandList* list,
    std::uint32_t tick, ID3D12PipelineState* graphics) noexcept {
    auto& ray = state.nw11_ray;
    auto* vertices = Slot(state, kVertices);
    auto* indices = Slot(state, kIndices);
    if (!vertices || !indices || !graphics) return E_INVALIDARG;
    ComPtr<ID3D12GraphicsCommandList4> rt;
    HRESULT hr = list->QueryInterface(IID_PPV_ARGS(&rt));
    if (FAILED(hr)) return hr;
    // All deformation, BLAS/TLAS construction and traversal are recorded in
    // the same ordered production command list, so timing includes their cost.
    TransitionSlot(list, *vertices, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER |
        D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    TransitionSlot(list, *indices, D3D12_RESOURCE_STATE_INDEX_BUFFER |
        D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE | D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
    Transition(list, ray.positions.Get(), ray.position_state, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    list->SetComputeRootSignature(ray.deform_root.Get());
    const std::uint32_t constants[4] = {tick, scene::kVertexCount,
        std::bit_cast<std::uint32_t>(scene::kTwistRadians), scene::kLoopTicks};
    list->SetComputeRoot32BitConstants(0, 4, constants, 0);
    list->SetComputeRootShaderResourceView(1, vertices->resource->GetGPUVirtualAddress());
    list->SetComputeRootUnorderedAccessView(2, ray.positions->GetGPUVirtualAddress());
    list->SetPipelineState(ray.deform_pipeline.Get());
    list->Dispatch((scene::kVertexCount + 63u) / 64u, 1, 1);
    UavBarrier(list, ray.positions.Get());
    ray.position_state = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE | D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    Transition(list, ray.positions.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, ray.position_state);
    // Explicit barriers also order reuse of the scratch and AS allocations
    // between consecutive frames on this single direct queue.
    UavBarrier(list, ray.blas_scratch.Get());
    UavBarrier(list, ray.blas.Get());
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC bottom{};
    bottom.Inputs = ray.blas_inputs;
    bottom.DestAccelerationStructureData = ray.blas->GetGPUVirtualAddress();
    bottom.ScratchAccelerationStructureData = ray.blas_scratch->GetGPUVirtualAddress();
    rt->BuildRaytracingAccelerationStructure(&bottom, 0, nullptr);
    UavBarrier(list, ray.blas.Get());
    UavBarrier(list, ray.tlas_scratch.Get());
    UavBarrier(list, ray.tlas.Get());
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC top{};
    top.Inputs = ray.tlas_inputs;
    top.DestAccelerationStructureData = ray.tlas->GetGPUVirtualAddress();
    top.ScratchAccelerationStructureData = ray.tlas_scratch->GetGPUVirtualAddress();
    rt->BuildRaytracingAccelerationStructure(&top, 0, nullptr);
    UavBarrier(list, ray.tlas.Get());
    list->SetPipelineState(graphics);
    list->SetGraphicsRootShaderResourceView(6, ray.positions->GetGPUVirtualAddress());
    list->SetGraphicsRootShaderResourceView(7, indices->resource->GetGPUVirtualAddress());
    list->SetGraphicsRootShaderResourceView(8, ray.tlas->GetGPUVirtualAddress());
    return S_OK;
}
#endif
}  // namespace nw11::production
