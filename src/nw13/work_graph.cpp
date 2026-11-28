// Validation-only experiment. Reuses inbox D3D12 and the pinned offline DXC.
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string_view>
#include <system_error>
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

bool ParseLuid(std::string_view text, LUID& luid) {
    if (text.size() != 17 || text[8] != ':') return false;
    std::uint32_t high = 0, low = 0;
    const auto a = std::from_chars(text.data(), text.data() + 8, high, 16);
    const auto b = std::from_chars(text.data() + 9, text.data() + 17, low, 16);
    if (a.ec != std::errc{} || a.ptr != text.data() + 8 ||
        b.ec != std::errc{} || b.ptr != text.data() + 17) return false;
    luid = {static_cast<DWORD>(low), static_cast<LONG>(high)};
    return true;
}

bool EqualLuid(LUID a, LUID b) {
    return a.HighPart == b.HighPart && a.LowPart == b.LowPart;
}

struct Sample {
    UINT64 begin_ticks = 0, end_ticks = 0;
    std::uint32_t output_hash = 0;
};

void PrintSample(const Sample& sample) {
    std::printf("{\"begin_ticks\":%llu,\"end_ticks\":%llu,\"output_hash\":%u,"
                "\"oracle_slots_checked\":%u}", static_cast<unsigned long long>(sample.begin_ticks),
                static_cast<unsigned long long>(sample.end_ticks), sample.output_hash, kCandidates);
}

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

D3D12_RESOURCE_DESC BufferDescription(UINT64 size, D3D12_RESOURCE_FLAGS flags) {
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Flags = flags;
    return desc;
}

HRESULT Buffer(ID3D12Device* device, UINT64 size, D3D12_HEAP_TYPE heap,
               D3D12_RESOURCE_STATES state, D3D12_RESOURCE_FLAGS flags,
               ComPtr<ID3D12Resource>& out) {
    D3D12_HEAP_PROPERTIES props{};
    props.Type = heap;
    const auto desc = BufferDescription(size, flags);
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
    ComPtr<ID3D12Resource> output, poison, readback, backing;
    ComPtr<ID3D12QueryHeap> timestamps;
    ComPtr<ID3D12PipelineState> conventional;
    ComPtr<ID3D12StateObject> work_graph;
    D3D12_DISPATCH_GRAPH_DESC dispatch{};
    D3D12_SET_PROGRAM_DESC program{};
    DXGI_ADAPTER_DESC1 identity{};
    UINT64 backing_required_bytes = 0, backing_bytes = 0, frequency = 0, fence_value = 0;
    UINT graph_tier = 0, shader_model = 0;

    bool Create(bool warp, LUID requested) {
        ComPtr<IDXGIFactory6> factory;
        ComPtr<IDXGIAdapter1> adapter;
        if (FAILED(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)))) return false;
        if (warp) {
            if (FAILED(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)))) return false;
        } else if (FAILED(factory->EnumAdapterByLuid(requested, IID_PPV_ARGS(&adapter)))) {
            return false;
        }
        if (FAILED(adapter->GetDesc1(&identity)) ||
            (!warp && ((identity.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ||
                       !EqualLuid(identity.AdapterLuid, requested))) ||
            FAILED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)))) return false;
        return EqualLuid(identity.AdapterLuid, device->GetAdapterLuid());
    }

    void PrintIdentity() const {
        const auto luid = device->GetAdapterLuid();
        std::printf("\"adapter\":{\"adapter_luid\":\"%08lx:%08lx\",\"vendor_id\":%u,"
                    "\"device_id\":%u,\"subsystem_id\":%u,\"revision\":%u,\"software\":%s}",
                    static_cast<unsigned long>(luid.HighPart), static_cast<unsigned long>(luid.LowPart),
                    identity.VendorId, identity.DeviceId, identity.SubSysId, identity.Revision,
                    (identity.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ? "true" : "false");
    }

    bool QueryWorkGraph() {
        D3D12_FEATURE_DATA_D3D12_OPTIONS21 options{};
        D3D12_FEATURE_DATA_SHADER_MODEL sm{D3D_SHADER_MODEL_6_8};
        if (FAILED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS21, &options, sizeof(options))) ||
            FAILED(device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm, sizeof(sm)))) return false;
        graph_tier = static_cast<UINT>(options.WorkGraphsTier);
        shader_model = static_cast<UINT>(sm.HighestShaderModel);
        return true;
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
            FAILED(Buffer(device.Get(), kOutputBytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ,
                          D3D12_RESOURCE_FLAG_NONE, poison)) ||
            FAILED(Buffer(device.Get(), kReadbackBytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST,
                          D3D12_RESOURCE_FLAG_NONE, readback))) return false;
        const D3D12_RANGE no_read{0, 0};
        void* mapped = nullptr;
        if (FAILED(poison->Map(0, &no_read, &mapped))) return false;
        auto* values = static_cast<std::uint32_t*>(mapped);
        for (UINT i = 0; i < kCandidates; ++i) values[i] = ~Expected(i);
        const D3D12_RANGE written{0, kOutputBytes};
        poison->Unmap(0, &written);
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
        backing_required_bytes = memory.MaxSizeInBytes;
        if (backing_required_bytes) {
            const auto desc = BufferDescription(backing_required_bytes, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
            backing_bytes = device->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
            if (!backing_bytes || backing_bytes > kBackingLimit || backing_required_bytes > backing_bytes ||
                FAILED(Buffer(device.Get(), backing_required_bytes, D3D12_HEAP_TYPE_DEFAULT,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, backing))) return false;
        }
        program.Type = D3D12_PROGRAM_TYPE_WORK_GRAPH;
        program.WorkGraph.ProgramIdentifier = state_props->GetProgramIdentifier(kGraphName);
        program.WorkGraph.Flags = D3D12_SET_WORK_GRAPH_FLAG_INITIALIZE;
        if (backing) program.WorkGraph.BackingMemory = {backing->GetGPUVirtualAddress(), backing_required_bytes};
        dispatch.Mode = D3D12_DISPATCH_MODE_NODE_CPU_INPUT;
        dispatch.NodeCPUInput = {entry, 1, nullptr, 0};
        return true;
    }

    bool Dispatch(bool optional, Sample& sample, bool verify_reset = false) {
        if (FAILED(allocator->Reset()) ||
            FAILED(list->Reset(allocator.Get(), optional ? nullptr : conventional.Get()))) return false;
        // Prove that every measured arm writes every slot, independent of
        // the preceding arm. Copy the inverted reference outside the timer.
        D3D12_RESOURCE_BARRIER reset{};
        reset.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        reset.Transition = {output.Get(), D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES,
                            D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST};
        list->ResourceBarrier(1, &reset);
        list->CopyBufferRegion(output.Get(), 0, poison.Get(), 0, kOutputBytes);
        std::swap(reset.Transition.StateBefore, reset.Transition.StateAfter);
        list->ResourceBarrier(1, &reset);
        list->SetComputeRootSignature(root.Get());
        list->SetComputeRootUnorderedAccessView(0, output->GetGPUVirtualAddress());
        list->SetComputeRoot32BitConstant(1, kTick, 0);
        if (!verify_reset && optional) {
            graph_list->SetProgram(&program);
            program.WorkGraph.Flags = D3D12_SET_WORK_GRAPH_FLAG_NONE;
        }
        list->EndQuery(timestamps.Get(), D3D12_QUERY_TYPE_TIMESTAMP, 0);
        if (!verify_reset && optional) {
            graph_list->DispatchGraph(&dispatch);
        } else if (!verify_reset) {
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
        sample.output_hash = 2166136261u;
        for (UINT i = 0; i < kCandidates; ++i) {
            const auto expected = Expected(i);
            matches &= values[i] == (verify_reset ? ~expected : expected);
            sample.output_hash = (sample.output_hash ^ values[i]) * 16777619u;
        }
        const auto* time = reinterpret_cast<const std::uint64_t*>(values + kCandidates);
        sample.begin_ticks = time[0];
        sample.end_ticks = time[1];
        const D3D12_RANGE written{0, 0};
        readback->Unmap(0, &written);
        return matches && (verify_reset || sample.end_ticks > sample.begin_ticks);
    }
};
}

int main(int argc, char** argv) {
    if (argc != 4 && argc != 6) return 2;
    const std::string_view mode = argv[1];
    if (mode != "--warp" && mode != "--matched" && mode != "--baseline") return 2;
    const bool warp = mode == "--warp";
    const bool matched = mode == "--matched";
    LUID requested{};
    if (warp ? argc != 4 : (argc != 6 || std::string_view(argv[4]) != "--adapter-luid" ||
                            !ParseLuid(argv[5], requested))) return 2;
    Experiment experiment;
    if (!experiment.Create(warp, requested)) return 4;
    if (matched && !experiment.QueryWorkGraph()) {
        std::printf("{\"status\":\"inconclusive\",\"reason\":\"capability_query_failed\",");
        experiment.PrintIdentity();
        std::puts("}");
        return 4;
    }
    if (matched && (experiment.graph_tier < D3D12_WORK_GRAPHS_TIER_1_0 ||
                    experiment.shader_model < D3D_SHADER_MODEL_6_8)) {
        std::printf("{\"status\":\"unsupported\",\"work_graph_tier\":%u,\"shader_model\":%u,",
                    experiment.graph_tier, experiment.shader_model);
        experiment.PrintIdentity();
        std::puts("}");
        return 3;
    }
    const auto baseline = ReadBlob(argv[2]);
    const auto graph = ReadBlob(argv[3]);
    if (baseline.empty() || graph.empty() || !experiment.Initialize(baseline)) return 4;
    if (matched && !experiment.MakeGraph(graph)) {
        std::printf("{\"status\":\"inconclusive\",\"reason\":\"work_graph_setup_failed\","
                    "\"backing_required_bytes\":%llu,\"backing_bytes\":%llu,",
                    static_cast<unsigned long long>(experiment.backing_required_bytes),
                    static_cast<unsigned long long>(experiment.backing_bytes));
        experiment.PrintIdentity();
        std::puts("}");
        return 4;
    }
    Sample common_warmup, graph_warmup;
    if (!experiment.Dispatch(false, common_warmup) ||
        (matched && !experiment.Dispatch(true, graph_warmup))) return 5;
    if (!matched) {
        // Exercise the exact per-arm reset after a launch with known output.
        // Do not substitute this check for a conventional or optional run.
        Sample reset;
        if (!experiment.Dispatch(false, reset, true)) return 5;
        std::printf("{\"status\":\"%s\",\"output_hash\":%u,\"oracle_slots_checked\":%u,",
                    warp ? "warp_baseline_executed" : "baseline_executed", common_warmup.output_hash, kCandidates);
        experiment.PrintIdentity();
        std::puts("}");
        return 0;
    }
    std::array<Sample, kTrials> common{}, optional{};
    for (UINT i = 0; i < kTrials; ++i) {
        const bool ok = (i % 2 == 0)
            ? (experiment.Dispatch(false, common[i]) && experiment.Dispatch(true, optional[i]))
            : (experiment.Dispatch(true, optional[i]) && experiment.Dispatch(false, common[i]));
        if (!ok || common[i].output_hash != optional[i].output_hash) return 5;
    }
    std::printf("{\"status\":\"measured\",\"candidates\":%u,\"samples_per_arm\":%u,"
                "\"tick\":%u,\"timestamp_frequency_hz\":%llu,\"work_graph_tier\":%u,\"shader_model\":%u,"
                "\"backing_required_bytes\":%llu,\"backing_bytes\":%llu,"
                "\"conventional_shader_bytes\":%zu,\"work_graph_shader_bytes\":%zu,",
                kCandidates, kTrials, kTick, static_cast<unsigned long long>(experiment.frequency),
                experiment.graph_tier, experiment.shader_model,
                static_cast<unsigned long long>(experiment.backing_required_bytes),
                static_cast<unsigned long long>(experiment.backing_bytes), baseline.size(), graph.size());
    experiment.PrintIdentity();
    std::printf(",\"warmup\":{\"conventional\":");
    PrintSample(common_warmup);
    std::printf(",\"work_graph\":");
    PrintSample(graph_warmup);
    std::printf("},\"pairs\":[");
    for (UINT i = 0; i < kTrials; ++i) {
        std::printf("%s{\"pair\":%u,\"first\":\"%s\",\"conventional\":", i ? "," : "", i,
                    i % 2 == 0 ? "conventional" : "work_graph");
        PrintSample(common[i]);
        std::printf(",\"work_graph\":");
        PrintSample(optional[i]);
        std::printf("}");
    }
    std::puts("]}");
    return 0;
}
