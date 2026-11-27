// Validation-only experiment. Reuses inbox D3D12 and the pinned offline DXC.
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string_view>
#include <vector>

namespace {
using Microsoft::WRL::ComPtr;
constexpr UINT kCandidates = 1024;
constexpr UINT kTrials = 9;
constexpr UINT64 kBackingLimit = 32ull * 1024 * 1024;
constexpr UINT64 kOutputBytes = kCandidates * sizeof(std::uint32_t);
constexpr UINT64 kReadbackBytes = kOutputBytes + 2 * sizeof(std::uint64_t);
constexpr UINT kTick = 148800;
constexpr wchar_t kGraphName[] = L"ParticleRouting";

std::uint32_t Hash(std::uint32_t x) {
    x = (x ^ (x >> 16)) * 0x7feb352du;
    x = (x ^ (x >> 15)) * 0x846ca68bu;
    return x ^ (x >> 16);
}

std::uint32_t Expected(std::uint32_t index) {
    if ((Hash(index ^ kTick) & 7) >= 3) return 0;
    auto value = Hash(index + kTick);
    for (std::uint32_t i = 0; i < 24; ++i) value = Hash(value ^ i);
    return value;
}

std::vector<char> ReadBlob(const char* path) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    const auto size = stream.tellg();
    if (size <= 0 || size > std::streamoff{2 * 1024 * 1024}) return {};
    std::vector<char> bytes(static_cast<std::size_t>(size));
    stream.seekg(0);
    if (!stream.read(bytes.data(), size)) return {};
    return bytes;
}

HRESULT Buffer(ID3D12Device* device, UINT64 size, D3D12_HEAP_TYPE heap,
               D3D12_RESOURCE_STATES state, D3D12_RESOURCE_FLAGS flags,
               ComPtr<ID3D12Resource>& out) {
    D3D12_HEAP_PROPERTIES props{};
    props.Type = heap;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Flags = flags;
    return device->CreateCommittedResource(&props, D3D12_HEAP_FLAG_NONE, &desc, state,
                                            nullptr, IID_PPV_ARGS(&out));
}

struct Experiment {
    ComPtr<ID3D12Device> device;
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12GraphicsCommandList10> graph_list;
    ComPtr<ID3D12RootSignature> root;
    ComPtr<ID3D12Fence> fence;
    ComPtr<ID3D12Resource> output, readback, backing;
    ComPtr<ID3D12QueryHeap> timestamps;
    ComPtr<ID3D12PipelineState> conventional;
    ComPtr<ID3D12StateObject> work_graph;
    D3D12_DISPATCH_GRAPH_DESC dispatch{};
    D3D12_SET_PROGRAM_DESC program{};
    UINT64 backing_bytes = 0, frequency = 0, fence_value = 0;
    UINT graph_tier = 0, shader_model = 0;

    bool Create(bool warp) {
        ComPtr<IDXGIFactory6> factory;
        ComPtr<IDXGIAdapter1> adapter;
        if (FAILED(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)))) return false;
        if (warp) {
            if (FAILED(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)))) return false;
        } else {
            for (UINT i = 0; ; ++i) {
                ComPtr<IDXGIAdapter1> candidate;
                if (FAILED(factory->EnumAdapterByGpuPreference(i, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE,
                    IID_PPV_ARGS(&candidate)))) return false;
                DXGI_ADAPTER_DESC1 desc{};
                if (SUCCEEDED(candidate->GetDesc1(&desc)) && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) &&
                    SUCCEEDED(D3D12CreateDevice(candidate.Get(), D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
                    adapter = candidate;
                    break;
                }
            }
        }
        return SUCCEEDED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)));
    }

    bool QueryWorkGraph() {
        D3D12_FEATURE_DATA_D3D12_OPTIONS21 options{};
        D3D12_FEATURE_DATA_SHADER_MODEL sm{D3D_SHADER_MODEL_6_8};
        if (FAILED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS21, &options, sizeof(options))) ||
            FAILED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm, sizeof(sm)))) return false;
        graph_tier = static_cast<UINT>(options.WorkGraphsTier);
        shader_model = static_cast<UINT>(sm.HighestShaderModel);
        return options.WorkGraphsTier >= D3D12_WORK_GRAPHS_TIER_1_0 && sm.HighestShaderModel >= D3D_SHADER_MODEL_6_8;
    }

    bool Initialize(const std::vector<char>& compute) {
        D3D12_COMMAND_QUEUE_DESC queue_desc{};
        queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        D3D12_QUERY_HEAP_DESC query_desc{};
        query_desc.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
        query_desc.Count = 2;
        if (FAILED(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue))) ||
            FAILED(queue->GetTimestampFrequency(&frequency)) || frequency == 0 ||
            FAILED(device->CreateCommandAllocator(queue_desc.Type, IID_PPV_ARGS(&allocator))) ||
            FAILED(device->CreateCommandList(0, queue_desc.Type, allocator.Get(), nullptr, IID_PPV_ARGS(&list))) ||
            FAILED(list->Close()) || FAILED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence))) ||
            FAILED(device->CreateQueryHeap(&query_desc, IID_PPV_ARGS(&timestamps))) ||
            FAILED(Buffer(device.Get(), kOutputBytes, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                          D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, output)) ||
            FAILED(Buffer(device.Get(), kReadbackBytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST,
                          D3D12_RESOURCE_FLAG_NONE, readback))) return false;
        D3D12_ROOT_PARAMETER parameters[2]{};
        parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
        parameters[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        parameters[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
        parameters[1].Constants.Num32BitValues = 1;
        D3D12_ROOT_SIGNATURE_DESC signature{};
        signature.NumParameters = 2;
        signature.pParameters = parameters;
        ComPtr<ID3DBlob> serialized;
        if (FAILED(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr)) ||
            FAILED(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(), IID_PPV_ARGS(&root)))) return false;
        D3D12_COMPUTE_PIPELINE_STATE_DESC pipeline{};
        pipeline.pRootSignature = root.Get();
        pipeline.CS = {compute.data(), compute.size()};
        return SUCCEEDED(device->CreateComputePipelineState(&pipeline, IID_PPV_ARGS(&conventional)));
    }

    bool MakeGraph(const std::vector<char>& shader) {
        ComPtr<ID3D12Device5> device5;
        if (FAILED(device.As(&device5)) || FAILED(list.As(&graph_list))) return false;
        D3D12_DXIL_LIBRARY_DESC library{};
        library.DXILLibrary = {shader.data(), shader.size()};
        D3D12_GLOBAL_ROOT_SIGNATURE global{root.Get()};
        D3D12_WORK_GRAPH_DESC graph{};
        graph.ProgramName = kGraphName;
        graph.Flags = D3D12_WORK_GRAPH_FLAG_INCLUDE_ALL_AVAILABLE_NODES;
        const D3D12_STATE_SUBOBJECT subobjects[] = {
            {D3D12_STATE_SUBOBJECT_TYPE_DXIL_LIBRARY, &library},
            {D3D12_STATE_SUBOBJECT_TYPE_GLOBAL_ROOT_SIGNATURE, &global},
            {D3D12_STATE_SUBOBJECT_TYPE_WORK_GRAPH, &graph},
        };
        D3D12_STATE_OBJECT_DESC state{};
        state.Type = D3D12_STATE_OBJECT_TYPE_EXECUTABLE;
        state.NumSubobjects = static_cast<UINT>(std::size(subobjects));
        state.pSubobjects = subobjects;
        ComPtr<ID3D12WorkGraphProperties> props;
        ComPtr<ID3D12StateObjectProperties1> state_props;
        if (FAILED(device5->CreateStateObject(&state, IID_PPV_ARGS(&work_graph))) ||
            FAILED(work_graph.As(&props)) || FAILED(work_graph.As(&state_props))) return false;
        const UINT graph_index = props->GetWorkGraphIndex(kGraphName);
        if (graph_index == UINT_MAX) return false;
        const auto entry = props->GetEntrypointIndex(graph_index, D3D12_NODE_ID{L"Route", 0});
        if (entry == UINT_MAX || props->GetEntrypointRecordSizeInBytes(graph_index, entry) != 0) return false;
        D3D12_WORK_GRAPH_MEMORY_REQUIREMENTS memory{};
        props->GetWorkGraphMemoryRequirements(graph_index, &memory);
        backing_bytes = memory.MaxSizeInBytes;
        if (backing_bytes > kBackingLimit ||
            (backing_bytes && FAILED(Buffer(device.Get(), backing_bytes, D3D12_HEAP_TYPE_DEFAULT,
                 D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, backing)))) return false;
        program.Type = D3D12_PROGRAM_TYPE_WORK_GRAPH;
        program.WorkGraph.ProgramIdentifier = state_props->GetProgramIdentifier(kGraphName);
        program.WorkGraph.Flags = D3D12_SET_WORK_GRAPH_FLAG_INITIALIZE;
        if (backing) program.WorkGraph.BackingMemory = {backing->GetGPUVirtualAddress(), backing_bytes};
        dispatch.Mode = D3D12_DISPATCH_GRAPH_MODE_NODE_CPU_INPUT;
        dispatch.NodeCPUInput = {entry, 1, nullptr, 0};
        return true;
    }

    bool Dispatch(bool optional, double& duration, std::uint32_t& output_hash) {
        if (FAILED(allocator->Reset()) ||
            FAILED(list->Reset(allocator.Get(), optional ? nullptr : conventional.Get()))) return false;
        list->SetComputeRootSignature(root.Get());
        list->SetComputeRootUnorderedAccessView(0, output->GetGPUVirtualAddress());
        list->SetComputeRoot32BitConstant(1, kTick, 0);
        list->EndQuery(timestamps.Get(), D3D12_QUERY_TYPE_TIMESTAMP, 0);
        if (optional) {
            graph_list->SetProgram(&program);
            graph_list->DispatchGraph(&dispatch);
            program.WorkGraph.Flags = D3D12_SET_WORK_GRAPH_FLAG_NONE;
        } else {
            list->Dispatch(kCandidates / 64, 1, 1);
        }
        list->EndQuery(timestamps.Get(), D3D12_QUERY_TYPE_TIMESTAMP, 1);
        D3D12_RESOURCE_BARRIER barrier{};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition = {output.Get(), D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES,
                              D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE};
        list->ResourceBarrier(1, &barrier);
        list->CopyBufferRegion(readback.Get(), 0, output.Get(), 0, kOutputBytes);
        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        list->ResourceBarrier(1, &barrier);
        list->ResolveQueryData(timestamps.Get(), D3D12_QUERY_TYPE_TIMESTAMP, 0, 2, readback.Get(), kOutputBytes);
        if (FAILED(list->Close())) return false;
        ID3D12CommandList* command = list.Get();
        queue->ExecuteCommandLists(1, &command);
        ++fence_value;
        if (FAILED(queue->Signal(fence.Get(), fence_value))) return false;
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event) return false;
        const bool completed = SUCCEEDED(fence->SetEventOnCompletion(fence_value, event)) &&
            WaitForSingleObject(event, 30000) == WAIT_OBJECT_0;
        CloseHandle(event);
        if (!completed || FAILED(device->GetDeviceRemovedReason())) return false;
        D3D12_RANGE range{0, kReadbackBytes};
        void* mapped = nullptr;
        if (FAILED(readback->Map(0, &range, &mapped))) return false;
        const auto* values = static_cast<const std::uint32_t*>(mapped);
        bool matches = true;
        output_hash = 2166136261u;
        for (UINT i = 0; i < kCandidates; ++i) {
            matches &= values[i] == Expected(i);
            output_hash = (output_hash ^ values[i]) * 16777619u;
        }
        const auto* time = reinterpret_cast<const std::uint64_t*>(values + kCandidates);
        duration = time[1] > time[0] ? 1e6 * double(time[1] - time[0]) / double(frequency) : 0;
        const D3D12_RANGE written{0, 0};
        readback->Unmap(0, &written);
        return matches && duration > 0;
    }
};
}

int main(int argc, char** argv) {
    if (argc != 4) return 2;
    const std::string_view mode = argv[1];
    if (mode != "--warp" && mode != "--matched" && mode != "--baseline") return 2;
    const bool warp = mode == "--warp";
    const bool matched = mode == "--matched";
    Experiment experiment;
    if (!experiment.Create(warp)) return 4;
    const bool supported = experiment.QueryWorkGraph();
    if (matched && !supported) {
        std::printf("{\"status\":\"unavailable\",\"work_graph_tier\":%u,\"shader_model\":%u}\n",
                    experiment.graph_tier, experiment.shader_model);
        return 3;
    }
    const auto baseline = ReadBlob(argv[2]);
    const auto graph = ReadBlob(argv[3]);
    if (baseline.empty() || graph.empty() || !experiment.Initialize(baseline) ||
        (matched && !experiment.MakeGraph(graph))) return 4;
    std::uint32_t common_hash = 0, graph_hash = 0;
    double ignored = 0;
    if (!experiment.Dispatch(false, ignored, common_hash) ||
        (matched && !experiment.Dispatch(true, ignored, graph_hash))) return 5;
    if (!matched) {
        std::printf("{\"status\":\"%s\",\"output_hash\":%u}\n",
                    warp ? "warp_baseline_executed" : "baseline_executed", common_hash);
        return 0;
    }
    std::array<double, kTrials> common{}, optional{};
    for (UINT i = 0; i < kTrials; ++i) {
        if (!experiment.Dispatch(false, common[i], common_hash) ||
            !experiment.Dispatch(true, optional[i], graph_hash) || common_hash != graph_hash) return 5;
    }
    std::sort(common.begin(), common.end());
    std::sort(optional.begin(), optional.end());
    std::printf("{\"status\":\"measured\",\"candidates\":%u,\"samples_per_arm\":%u,"
                "\"work_graph_tier\":%u,\"shader_model\":%u,\"backing_bytes\":%llu,"
                "\"output_hash\":%u,\"conventional_shader_bytes\":%zu,\"work_graph_shader_bytes\":%zu,"
                "\"conventional_gpu_median_us\":%.6f,\"work_graph_gpu_median_us\":%.6f}\n",
                kCandidates, kTrials, experiment.graph_tier, experiment.shader_model,
                static_cast<unsigned long long>(experiment.backing_bytes), common_hash,
                baseline.size(), graph.size(), common[kTrials / 2], optional[kTrials / 2]);
    return 0;
}
