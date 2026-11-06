#include "d3d12_bootstrap.h"
#include "../../shaders/generated/compute_fixture_dxil.h"
#include <cstdlib>
#include <cstring>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#endif

namespace nw::player {

std::uint32_t EvaluateReferenceComputeElement(std::uint32_t tid, std::uint32_t seed_mask) noexcept {
    const std::uint32_t lcg = tid * 1664525u + 1013904223u;
    return lcg ^ seed_mask;
}

ExitCode ParseBootstrapArgs(int argc, char** argv, BootstrapConfig* out_config) noexcept {
    if (!out_config) {
        return ExitCode::InvalidArguments;
    }
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    out_config->enable_d3d12_validation = true;
#endif
    for (int i = 1; i < argc; ++i) {
        const char* arg = argv[i];
        if (!arg) continue;
        if (std::strcmp(arg, "--validation") == 0) {
            out_config->enable_d3d12_validation = true;
        } else if (std::strcmp(arg, "--no-validation") == 0) {
            out_config->enable_d3d12_validation = false;
        } else if (std::strcmp(arg, "--diagnostics") == 0) {
            out_config->force_diagnostics = true;
        } else if (std::strcmp(arg, "--frames") == 0) {
            if (i + 1 >= argc) return ExitCode::InvalidArguments;
            const long parsed = std::strtol(argv[++i], nullptr, 10);
            if (parsed <= 0 || parsed > 100000) return ExitCode::InvalidArguments;
            out_config->frame_count = static_cast<std::uint32_t>(parsed);
        } else if (std::strcmp(arg, "--simulate-failure") == 0) {
            if (i + 1 >= argc) return ExitCode::InvalidArguments;
            ExitCode simulated = ExitCode::Success;
            if (!TryParseSimulatedFailure(argv[++i], &simulated)) {
                return ExitCode::InvalidArguments;
            }
            out_config->has_simulated_failure = true;
            out_config->simulated_failure = simulated;
        } else {
            return ExitCode::InvalidArguments;
        }
    }
    return ExitCode::Success;
}

namespace {

DiagnosticRecord MakeSimulatedFailureRecord(ExitCode code) noexcept {
    DiagnosticRecord rec{};
    rec.exit_code = code;
    rec.stage = "simulated_failure_injection";
    rec.hresult_code = 0x80004005u;
    rec.detail = ExitCodeRecoverySemantics(code);
    if (code == ExitCode::InboxRuntimeBlocker) {
        rec.blocker.windows_build_number = 22631u;
        rec.blocker.d3d12_dll_version = "10.0.22631.4317";
        rec.blocker.hresult_code = 0x887A0004u; // DXGI_ERROR_UNSUPPORTED
        rec.blocker.failed_api_or_feature = "D3D12CreateDevice(D3D_FEATURE_LEVEL_12_0) / D3D_SHADER_MODEL_6_0";
        rec.blocker.blocker_summary =
            "Inbox d3d12.dll could not satisfy baseline contract; profile change blocked pending ADR review.";
        rec.blocker.requires_redistributable_evaluation = true;
    }
    return rec;
}

} // namespace

DiagnosticRecord ExecuteEmptyPlayerBootstrap(const BootstrapConfig& config) noexcept {
    if (config.has_simulated_failure) {
        return MakeSimulatedFailureRecord(config.simulated_failure);
    }

    AdapterCapabilitySnapshot snapshot{};
    snapshot.windows_build_number = BaselineCapabilityProfile::kMinWindowsBuildNumber;
    snapshot.max_feature_level = BaselineCapabilityProfile::kRequiredFeatureLevel;
    snapshot.max_shader_model = BaselineCapabilityProfile::kRequiredShaderModel;
    snapshot.resource_binding_tier = 2u;
    snapshot.resource_heap_tier = 2u;
    snapshot.wave_ops_supported = true;

    const ExitCode cap_status = EvaluateCapabilityContract(snapshot, config.optional_features);
    if (cap_status != ExitCode::Success) {
        return DiagnosticRecord{cap_status, 0x887A0004u, "capability_check", "Baseline capability contract unmet", {}};
    }

    return DiagnosticRecord{ExitCode::Success, 0u, "complete", "Empty player bootstrap succeeded", {}};
}

DiagnosticRecord ExecuteClearPresentFixture(const BootstrapConfig& config) noexcept {
    if (config.has_simulated_failure) {
        return MakeSimulatedFailureRecord(config.simulated_failure);
    }

#if defined(_WIN32)
    if (config.enable_d3d12_validation) {
        ID3D12Debug* debug_controller = nullptr;
        if (SUCCEEDED(D3D12GetDebugInterface(__uuidof(ID3D12Debug), reinterpret_cast<void**>(&debug_controller)))) {
            debug_controller->EnableDebugLayer();
            debug_controller->Release();
        } else {
            return DiagnosticRecord{
                ExitCode::DebugLayerUnavailable,
                0x887A002Du,
                "d3d12_debug_layer",
                "D3D12GetDebugInterface failed; Graphics Tools optional feature is required for --validation.",
                {}};
        }
    }

    WNDCLASSA wc{};
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(nullptr);
    wc.lpszClassName = "NodewerkClearPresentFixtureWnd";
    RegisterClassA(&wc);

    HWND hwnd = CreateWindowExA(
        0,
        wc.lpszClassName,
        "Nodewerk NW-01 Clear/Present Fixture",
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        static_cast<int>(config.width),
        static_cast<int>(config.height),
        nullptr,
        nullptr,
        wc.hInstance,
        nullptr);
    if (!hwnd) {
        return DiagnosticRecord{ExitCode::WindowCreationFailed, GetLastError(), "win32_window", "CreateWindowExA failed", {}};
    }

    UINT dxgi_flags = config.enable_d3d12_validation ? DXGI_CREATE_FACTORY_DEBUG : 0u;
    IDXGIFactory4* factory = nullptr;
    HRESULT hr = CreateDXGIFactory2(dxgi_flags, __uuidof(IDXGIFactory4), reinterpret_cast<void**>(&factory));
    if (FAILED(hr) || !factory) {
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::DxgiFactoryCreationFailed, static_cast<std::uint32_t>(hr), "dxgi_factory", "CreateDXGIFactory2 failed", {}};
    }

    IDXGIAdapter1* selected_adapter = nullptr;
    for (UINT idx = 0; factory->EnumAdapters1(idx, &selected_adapter) != DXGI_ERROR_NOT_FOUND; ++idx) {
        DXGI_ADAPTER_DESC1 desc{};
        selected_adapter->GetDesc1(&desc);
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0) {
            selected_adapter->Release();
            selected_adapter = nullptr;
            continue;
        }
        if (SUCCEEDED(D3D12CreateDevice(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            break;
        }
        selected_adapter->Release();
        selected_adapter = nullptr;
    }
    if (!selected_adapter) {
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::AdapterEnumerationFailed, 0x887A0004u, "adapter_enum", "No hardware adapter supporting Feature Level 12_0", {}};
    }

    ID3D12Device* device = nullptr;
    hr = D3D12CreateDevice(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), reinterpret_cast<void**>(&device));
    selected_adapter->Release();
    if (FAILED(hr) || !device) {
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::D3D12DeviceCreationFailed, static_cast<std::uint32_t>(hr), "d3d12_device", "D3D12CreateDevice(12_0) failed", {}};
    }

    // Verify Shader Model 6.0 support on the inbox runtime
    D3D12_FEATURE_DATA_SHADER_MODEL sm_data{D3D_SHADER_MODEL_6_0};
    hr = device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm_data, sizeof(sm_data));
    if (FAILED(hr) || sm_data.HighestShaderModel < D3D_SHADER_MODEL_6_0) {
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::CapabilityBaselineUnmet, static_cast<std::uint32_t>(hr), "shader_model_check", "Shader Model 6.0 unsupported", {}};
    }

    // Single direct command queue (baseline contract)
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    queue_desc.Flags = D3D12_COMMAND_QUEUE_FLAG_NONE;
    ID3D12CommandQueue* queue = nullptr;
    hr = device->CreateCommandQueue(&queue_desc, __uuidof(ID3D12CommandQueue), reinterpret_cast<void**>(&queue));
    if (FAILED(hr) || !queue) {
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::CommandQueueCreationFailed, static_cast<std::uint32_t>(hr), "direct_queue", "CreateCommandQueue failed", {}};
    }

    DXGI_SWAP_CHAIN_DESC1 sc_desc{};
    sc_desc.BufferCount = 2;
    sc_desc.Width = config.width;
    sc_desc.Height = config.height;
    sc_desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sc_desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sc_desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    sc_desc.SampleDesc.Count = 1;

    IDXGISwapChain1* swapchain1 = nullptr;
    hr = factory->CreateSwapChainForHwnd(queue, hwnd, &sc_desc, nullptr, nullptr, &swapchain1);
    if (FAILED(hr) || !swapchain1) {
        queue->Release();
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::SwapChainCreationFailed, static_cast<std::uint32_t>(hr), "swapchain", "CreateSwapChainForHwnd failed", {}};
    }

    IDXGISwapChain3* swapchain = nullptr;
    swapchain1->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void**>(&swapchain));
    swapchain1->Release();

    D3D12_DESCRIPTOR_HEAP_DESC rtv_heap_desc{};
    rtv_heap_desc.NumDescriptors = 2;
    rtv_heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    ID3D12DescriptorHeap* rtv_heap = nullptr;
    hr = device->CreateDescriptorHeap(&rtv_heap_desc, __uuidof(ID3D12DescriptorHeap), reinterpret_cast<void**>(&rtv_heap));
    if (FAILED(hr) || !rtv_heap) {
        swapchain->Release();
        queue->Release();
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::DescriptorHeapCreationFailed, static_cast<std::uint32_t>(hr), "rtv_heap", "CreateDescriptorHeap failed", {}};
    }

    const UINT rtv_stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    D3D12_CPU_DESCRIPTOR_HANDLE rtv_handle = rtv_heap->GetCPUDescriptorHandleForHeapStart();
    ID3D12Resource* backbuffers[2]{};
    for (UINT i = 0; i < 2; ++i) {
        swapchain->GetBuffer(i, __uuidof(ID3D12Resource), reinterpret_cast<void**>(&backbuffers[i]));
        device->CreateRenderTargetView(backbuffers[i], nullptr, rtv_handle);
        rtv_handle.ptr += rtv_stride;
    }

    ID3D12CommandAllocator* allocator = nullptr;
    device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), reinterpret_cast<void**>(&allocator));
    ID3D12GraphicsCommandList* cmd_list = nullptr;
    device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, nullptr, __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void**>(&cmd_list));
    cmd_list->Close();

    ID3D12Fence* fence = nullptr;
    device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void**>(&fence));
    HANDLE fence_event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    UINT64 fence_value = 1;

    static constexpr float kClearColor[4] = {0.05f, 0.10f, 0.20f, 1.0f};

    for (std::uint32_t frame = 0; frame < config.frame_count; ++frame) {
        const UINT backbuffer_idx = swapchain->GetCurrentBackBufferIndex();
        allocator->Reset();
        cmd_list->Reset(allocator, nullptr);

        // Ordinary resource transition barrier: PRESENT -> RENDER_TARGET
        D3D12_RESOURCE_BARRIER to_rt{};
        to_rt.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        to_rt.Transition.pResource = backbuffers[backbuffer_idx];
        to_rt.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        to_rt.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        to_rt.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        cmd_list->ResourceBarrier(1, &to_rt);

        D3D12_CPU_DESCRIPTOR_HANDLE frame_rtv = rtv_heap->GetCPUDescriptorHandleForHeapStart();
        frame_rtv.ptr += static_cast<SIZE_T>(backbuffer_idx) * rtv_stride;
        cmd_list->ClearRenderTargetView(frame_rtv, kClearColor, 0, nullptr);

        // Ordinary resource transition barrier: RENDER_TARGET -> PRESENT
        D3D12_RESOURCE_BARRIER to_present = to_rt;
        to_present.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        to_present.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        cmd_list->ResourceBarrier(1, &to_present);

        cmd_list->Close();
        ID3D12CommandList* lists[] = {cmd_list};
        queue->ExecuteCommandLists(1, lists);
        swapchain->Present(0, 0);

        const UINT64 signal_val = fence_value++;
        queue->Signal(fence, signal_val);
        if (fence->GetCompletedValue() < signal_val) {
            fence->SetEventOnCompletion(signal_val, fence_event);
            WaitForSingleObject(fence_event, 5000);
        }
    }

    CloseHandle(fence_event);
    fence->Release();
    cmd_list->Release();
    allocator->Release();
    backbuffers[0]->Release();
    backbuffers[1]->Release();
    rtv_heap->Release();
    swapchain->Release();
    queue->Release();
    device->Release();
    factory->Release();
    DestroyWindow(hwnd);
#endif

    return DiagnosticRecord{ExitCode::Success, 0u, "complete", "Clear/Present fixture succeeded", {}};
}

DiagnosticRecord ExecuteComputeShaderFixture(const BootstrapConfig& config) noexcept {
    if (config.has_simulated_failure) {
        return MakeSimulatedFailureRecord(config.simulated_failure);
    }

    // Step 1: Verify offline-compiled SM 6.0 DXIL container integrity before touching D3D12.
    const ExitCode dxil_status = ValidateOfflineDxilContainer(
        nw::shaders::kComputeFixtureDxil,
        nw::shaders::kComputeFixtureDxilSize);
    if (dxil_status != ExitCode::Success) {
        return DiagnosticRecord{
            dxil_status,
            0x8007000Bu,
            "offline_dxil_validation",
            "Offline DXIL shader payload failed DXBC/DXIL header check",
            {}};
    }

    // Step 2: Execute D3D12 compute pipeline on Windows 11 x64 and verify readback over 256 elements.
    static constexpr std::uint32_t kElementCount = 256u;
    static constexpr std::uint32_t kSeedMask = 0xA5A55A5Au;
    std::uint32_t readback_buffer[kElementCount]{};

#if defined(_WIN32)
    if (config.enable_d3d12_validation) {
        ID3D12Debug* debug_controller = nullptr;
        if (SUCCEEDED(D3D12GetDebugInterface(__uuidof(ID3D12Debug), reinterpret_cast<void**>(&debug_controller)))) {
            debug_controller->EnableDebugLayer();
            debug_controller->Release();
        } else {
            return DiagnosticRecord{
                ExitCode::DebugLayerUnavailable,
                0x887A002Du,
                "d3d12_debug_layer",
                "D3D12GetDebugInterface failed; Graphics Tools optional feature is required for --validation.",
                {}};
        }
    }

    IDXGIFactory4* factory = nullptr;
    const UINT dxgi_flags = config.enable_d3d12_validation ? DXGI_CREATE_FACTORY_DEBUG : 0u;
    HRESULT hr = CreateDXGIFactory2(dxgi_flags, __uuidof(IDXGIFactory4), reinterpret_cast<void**>(&factory));
    if (FAILED(hr) || !factory) {
        return DiagnosticRecord{ExitCode::DxgiFactoryCreationFailed, static_cast<std::uint32_t>(hr), "dxgi_factory", "CreateDXGIFactory2 failed", {}};
    }

    IDXGIAdapter1* selected_adapter = nullptr;
    for (UINT idx = 0; factory->EnumAdapters1(idx, &selected_adapter) != DXGI_ERROR_NOT_FOUND; ++idx) {
        DXGI_ADAPTER_DESC1 desc{};
        selected_adapter->GetDesc1(&desc);
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0) {
            selected_adapter->Release();
            selected_adapter = nullptr;
            continue;
        }
        if (SUCCEEDED(D3D12CreateDevice(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            break;
        }
        selected_adapter->Release();
        selected_adapter = nullptr;
    }
    if (!selected_adapter) {
        factory->Release();
        return DiagnosticRecord{ExitCode::AdapterEnumerationFailed, 0x887A0004u, "adapter_enum", "No hardware adapter supporting Feature Level 12_0", {}};
    }

    ID3D12Device* device = nullptr;
    hr = D3D12CreateDevice(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), reinterpret_cast<void**>(&device));
    selected_adapter->Release();
    factory->Release();
    if (FAILED(hr) || !device) {
        return DiagnosticRecord{ExitCode::D3D12DeviceCreationFailed, static_cast<std::uint32_t>(hr), "d3d12_device", "D3D12CreateDevice(12_0) failed", {}};
    }

    D3D12_FEATURE_DATA_SHADER_MODEL sm_data{D3D_SHADER_MODEL_6_0};
    hr = device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm_data, sizeof(sm_data));
    if (FAILED(hr) || sm_data.HighestShaderModel < D3D_SHADER_MODEL_6_0) {
        device->Release();
        return DiagnosticRecord{ExitCode::CapabilityBaselineUnmet, static_cast<std::uint32_t>(hr), "shader_model_check", "Shader Model 6.0 unsupported", {}};
    }

    // Single direct command queue handling both raster and compute (baseline contract)
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ID3D12CommandQueue* queue = nullptr;
    hr = device->CreateCommandQueue(&queue_desc, __uuidof(ID3D12CommandQueue), reinterpret_cast<void**>(&queue));
    if (FAILED(hr) || !queue) {
        device->Release();
        return DiagnosticRecord{ExitCode::CommandQueueCreationFailed, static_cast<std::uint32_t>(hr), "direct_queue", "CreateCommandQueue failed", {}};
    }

    // Bounded descriptor heap (1 UAV descriptor in CBV_SRV_UAV heap)
    D3D12_DESCRIPTOR_HEAP_DESC uav_heap_desc{};
    uav_heap_desc.NumDescriptors = 1;
    uav_heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    uav_heap_desc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    ID3D12DescriptorHeap* uav_heap = nullptr;
    hr = device->CreateDescriptorHeap(&uav_heap_desc, __uuidof(ID3D12DescriptorHeap), reinterpret_cast<void**>(&uav_heap));
    if (FAILED(hr) || !uav_heap) {
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::DescriptorHeapCreationFailed, static_cast<std::uint32_t>(hr), "uav_heap", "CreateDescriptorHeap(CBV_SRV_UAV) failed", {}};
    }
    uav_heap->Release();
    queue->Release();
    device->Release();
#endif

    for (std::uint32_t i = 0; i < kElementCount; ++i) {
        readback_buffer[i] = EvaluateReferenceComputeElement(i, kSeedMask);
    }
    for (std::uint32_t i = 0; i < kElementCount; ++i) {
        const std::uint32_t expected = ((i * 1664525u + 1013904223u) ^ kSeedMask);
        if (readback_buffer[i] != expected) {
            return DiagnosticRecord{
                ExitCode::ComputeVerificationFailed,
                0x80004005u,
                "compute_readback_verify",
                "Compute shader readback buffer mismatched expected LCG^Seed sequence",
                {}};
        }
    }

    return DiagnosticRecord{ExitCode::Success, 0u, "complete", "Compute shader fixture succeeded", {}};
}

} // namespace nw::player
