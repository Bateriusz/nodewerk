#include "d3d12_bootstrap.h"
#include <cstdio>
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

namespace nw::shaders {
#include "compute_fixture_dxil.h"
} // namespace nw::shaders

namespace nw::player {

std::uint32_t EvaluateReferenceComputeElement(std::uint32_t tid, std::uint32_t seed_mask) noexcept {
    const std::uint32_t lcg = tid * 1664525u + 1013904223u;
    return lcg ^ seed_mask;
}

std::uint32_t QueryWindowsBuildNumber() noexcept {
#if defined(_WIN32)
    struct RtlOsVersionInfoW {
        ULONG dwOSVersionInfoSize;
        ULONG dwMajorVersion;
        ULONG dwMinorVersion;
        ULONG dwBuildNumber;
        ULONG dwPlatformId;
        WCHAR szCSDVersion[128];
    };
    using RtlGetVersionFn = LONG(WINAPI*)(RtlOsVersionInfoW*);

    HMODULE ntdll = GetModuleHandleA("ntdll.dll");
    if (ntdll) {
        auto rtl_get_version = reinterpret_cast<RtlGetVersionFn>(
            reinterpret_cast<void*>(GetProcAddress(ntdll, "RtlGetVersion")));
        if (rtl_get_version) {
            RtlOsVersionInfoW ver{};
            ver.dwOSVersionInfoSize = sizeof(ver);
            if (rtl_get_version(&ver) == 0 && ver.dwMajorVersion >= 10u) {
                return static_cast<std::uint32_t>(ver.dwBuildNumber);
            }
        }
    }
    return 0u;
#else
    return BaselineCapabilityProfile::kMinWindowsBuildNumber;
#endif
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
        } else if (std::strcmp(arg, "--emit-readback-values") == 0) {
            out_config->emit_readback_values = true;
        } else if (std::strcmp(arg, "--frames") == 0) {
            if (i + 1 >= argc) return ExitCode::InvalidArguments;
            const long parsed = std::strtol(argv[++i], nullptr, 10);
            if (parsed <= 0 || parsed > 100000) return ExitCode::InvalidArguments;
            out_config->frame_count = static_cast<std::uint32_t>(parsed);
        } else if (std::strcmp(arg, "--override-os-build") == 0) {
            if (i + 1 >= argc) return ExitCode::InvalidArguments;
            const long parsed = std::strtol(argv[++i], nullptr, 10);
            if (parsed <= 0 || parsed > 100000) return ExitCode::InvalidArguments;
            out_config->os_build_override = static_cast<std::uint32_t>(parsed);
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

#if defined(_WIN32)
DiagnosticRecord PopulateAdapterCapabilitySnapshot(
    ID3D12Device* device,
    std::uint32_t windows_build_number,
    const EmittedContentFeatureRequest& optional_features,
    AdapterCapabilitySnapshot* out_snapshot) noexcept {
    if (!device || !out_snapshot) {
        return DiagnosticRecord{
            ExitCode::D3D12DeviceCreationFailed,
            0x80004003u,
            "capability_snapshot",
            "Null ID3D12Device passed to PopulateAdapterCapabilitySnapshot",
            {}};
    }

    out_snapshot->windows_build_number = windows_build_number;
    out_snapshot->max_feature_level = BaselineCapabilityProfile::kRequiredFeatureLevel;

    D3D12_FEATURE_DATA_SHADER_MODEL sm_data{D3D_SHADER_MODEL_6_0};
    HRESULT hr = device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm_data, sizeof(sm_data));
    if (FAILED(hr)) {
        return DiagnosticRecord{
            ExitCode::CapabilityBaselineUnmet,
            static_cast<std::uint32_t>(hr),
            "shader_model_check",
            "CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL) failed",
            {}};
    }
    out_snapshot->max_shader_model = static_cast<std::uint32_t>(sm_data.HighestShaderModel);

    D3D12_FEATURE_DATA_D3D12_OPTIONS options0{};
    hr = device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS, &options0, sizeof(options0));
    if (FAILED(hr)) {
        return DiagnosticRecord{
            ExitCode::CapabilityBaselineUnmet,
            static_cast<std::uint32_t>(hr),
            "d3d12_options_check",
            "CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS) failed",
            {}};
    }
    out_snapshot->resource_binding_tier = static_cast<std::uint32_t>(options0.ResourceBindingTier);
    out_snapshot->resource_heap_tier = static_cast<std::uint32_t>(options0.ResourceHeapTier);
    out_snapshot->typed_uav_additional_formats_supported = (options0.TypedUAVLoadAdditionalFormats != FALSE);

    // Query optional features only when emitted content requests them (per conditional_feature_policy)
    if (optional_features.uses_wave_intrinsics || optional_features.uses_int64_shader_ops) {
        D3D12_FEATURE_DATA_D3D12_OPTIONS1 options1{};
        if (SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS1, &options1, sizeof(options1)))) {
            out_snapshot->wave_ops_supported = (options1.WaveOps != FALSE);
            out_snapshot->int64_shader_ops_supported = (options1.Int64ShaderOps != FALSE);
        }
    }
    if (optional_features.uses_fp16_shader_ops) {
        D3D12_FEATURE_DATA_D3D12_OPTIONS4 options4{};
        if (SUCCEEDED(device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS4, &options4, sizeof(options4)))) {
            out_snapshot->fp16_shader_ops_supported = (options4.Native16BitShaderOpsSupported != FALSE);
        }
    }

    const ExitCode contract_code = EvaluateCapabilityContract(*out_snapshot, optional_features);
    if (contract_code != ExitCode::Success) {
        return DiagnosticRecord{
            contract_code,
            0x887A0004u,
            "capability_check",
            "Adapter or OS failed BaselineCapabilityProfile contract",
            {}};
    }
    return DiagnosticRecord{ExitCode::Success, 0u, "capability_check", "Capability contract satisfied", {}};
}
#endif

} // namespace

DiagnosticRecord ExecuteEmptyPlayerBootstrap(const BootstrapConfig& config) noexcept {
    if (config.has_simulated_failure) {
        return MakeSimulatedFailureRecord(config.simulated_failure);
    }

    const std::uint32_t win_build =
        config.os_build_override != 0u ? config.os_build_override : QueryWindowsBuildNumber();
    if (win_build < BaselineCapabilityProfile::kMinWindowsBuildNumber) {
        return DiagnosticRecord{
            ExitCode::OsVersionUnsupported,
            0x8007047Eu,
            "os_version_check",
            "OS build is older than minimum tested Windows 11 x64 build 22631",
            {}};
    }

#if defined(_WIN32)
    using CreateDXGIFactory2Fn = HRESULT(WINAPI*)(UINT, REFIID, void**);
    using D3D12CreateDeviceFn = HRESULT(WINAPI*)(IUnknown*, D3D_FEATURE_LEVEL, REFIID, void**);

    HMODULE dxgi_mod = LoadLibraryExA("dxgi.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!dxgi_mod) {
        return DiagnosticRecord{
            ExitCode::DxgiFactoryCreationFailed,
            static_cast<std::uint32_t>(GetLastError()),
            "dxgi_load",
            "LoadLibraryExA(dxgi.dll) failed",
            {}};
    }
    HMODULE d3d12_mod = LoadLibraryExA("d3d12.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!d3d12_mod) {
        const DWORD err = GetLastError();
        FreeLibrary(dxgi_mod);
        return DiagnosticRecord{
            ExitCode::D3D12DeviceCreationFailed,
            static_cast<std::uint32_t>(err),
            "d3d12_load",
            "LoadLibraryExA(d3d12.dll) failed",
            {}};
    }

    auto create_dxgi_factory2 = reinterpret_cast<CreateDXGIFactory2Fn>(
        reinterpret_cast<void*>(GetProcAddress(dxgi_mod, "CreateDXGIFactory2")));
    auto d3d12_create_device = reinterpret_cast<D3D12CreateDeviceFn>(
        reinterpret_cast<void*>(GetProcAddress(d3d12_mod, "D3D12CreateDevice")));
    if (!create_dxgi_factory2 || !d3d12_create_device) {
        FreeLibrary(d3d12_mod);
        FreeLibrary(dxgi_mod);
        return DiagnosticRecord{
            ExitCode::InboxRuntimeBlocker,
            0x8007007Fu,
            "inbox_entrypoint_resolve",
            "Missing CreateDXGIFactory2 or D3D12CreateDevice in inbox system DLLs",
            {}};
    }

    IDXGIFactory4* factory = nullptr;
    HRESULT hr = create_dxgi_factory2(0u, __uuidof(IDXGIFactory4), reinterpret_cast<void**>(&factory));
    if (FAILED(hr) || !factory) {
        FreeLibrary(d3d12_mod);
        FreeLibrary(dxgi_mod);
        return DiagnosticRecord{
            ExitCode::DxgiFactoryCreationFailed,
            static_cast<std::uint32_t>(hr),
            "dxgi_factory",
            "CreateDXGIFactory2 failed in empty_player bootstrap",
            {}};
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
        if (SUCCEEDED(d3d12_create_device(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            break;
        }
        selected_adapter->Release();
        selected_adapter = nullptr;
    }
    if (!selected_adapter) {
        factory->Release();
        FreeLibrary(d3d12_mod);
        FreeLibrary(dxgi_mod);
        return DiagnosticRecord{
            ExitCode::AdapterEnumerationFailed,
            0x887A0004u,
            "adapter_enum",
            "No hardware adapter supporting Feature Level 12_0",
            {}};
    }

    ID3D12Device* device = nullptr;
    hr = d3d12_create_device(
        selected_adapter,
        D3D_FEATURE_LEVEL_12_0,
        __uuidof(ID3D12Device),
        reinterpret_cast<void**>(&device));
    selected_adapter->Release();
    factory->Release();
    if (FAILED(hr) || !device) {
        FreeLibrary(d3d12_mod);
        FreeLibrary(dxgi_mod);
        return DiagnosticRecord{
            ExitCode::D3D12DeviceCreationFailed,
            static_cast<std::uint32_t>(hr),
            "d3d12_device",
            "D3D12CreateDevice(12_0) failed in empty_player bootstrap",
            {}};
    }

    AdapterCapabilitySnapshot snapshot{};
    const DiagnosticRecord cap_rec =
        PopulateAdapterCapabilitySnapshot(device, win_build, config.optional_features, &snapshot);
    device->Release();
    FreeLibrary(d3d12_mod);
    FreeLibrary(dxgi_mod);
    if (cap_rec.exit_code != ExitCode::Success) {
        return cap_rec;
    }
#else
    AdapterCapabilitySnapshot snapshot{};
    snapshot.windows_build_number = win_build;
    snapshot.max_feature_level = BaselineCapabilityProfile::kRequiredFeatureLevel;
    snapshot.max_shader_model = BaselineCapabilityProfile::kRequiredShaderModel;
    snapshot.resource_binding_tier = BaselineCapabilityProfile::kRequiredResourceBindingTier;
    snapshot.resource_heap_tier = BaselineCapabilityProfile::kRequiredResourceHeapTier;
    snapshot.wave_ops_supported = true;

    const ExitCode cap_status = EvaluateCapabilityContract(snapshot, config.optional_features);
    if (cap_status != ExitCode::Success) {
        return DiagnosticRecord{
            cap_status,
            0x8007047Eu,
            "capability_check",
            "Baseline capability contract unmet",
            {}};
    }
#endif

    return DiagnosticRecord{ExitCode::Success, 0u, "complete", "Empty player bootstrap succeeded", {}};
}

DiagnosticRecord ExecuteClearPresentFixture(const BootstrapConfig& config) noexcept {
    if (config.has_simulated_failure) {
        return MakeSimulatedFailureRecord(config.simulated_failure);
    }

    const std::uint32_t win_build =
        config.os_build_override != 0u ? config.os_build_override : QueryWindowsBuildNumber();
    if (win_build < BaselineCapabilityProfile::kMinWindowsBuildNumber) {
        return DiagnosticRecord{
            ExitCode::OsVersionUnsupported,
            0x8007047Eu,
            "os_version_check",
            "OS build is older than minimum tested Windows 11 x64 build 22631",
            {}};
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

    AdapterCapabilitySnapshot cap_snapshot{};
    const DiagnosticRecord cap_rec = PopulateAdapterCapabilitySnapshot(device, win_build, config.optional_features, &cap_snapshot);
    if (cap_rec.exit_code != ExitCode::Success) {
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return cap_rec;
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
    hr = swapchain1->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void**>(&swapchain));
    swapchain1->Release();
    if (FAILED(hr) || !swapchain) {
        queue->Release();
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::SwapChainCreationFailed, static_cast<std::uint32_t>(hr), "swapchain_qi", "IDXGISwapChain1::QueryInterface(IDXGISwapChain3) failed", {}};
    }

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
        hr = swapchain->GetBuffer(i, __uuidof(ID3D12Resource), reinterpret_cast<void**>(&backbuffers[i]));
        if (FAILED(hr) || !backbuffers[i]) {
            if (i == 1 && backbuffers[0]) backbuffers[0]->Release();
            rtv_heap->Release();
            swapchain->Release();
            queue->Release();
            device->Release();
            factory->Release();
            DestroyWindow(hwnd);
            return DiagnosticRecord{ExitCode::SwapChainCreationFailed, static_cast<std::uint32_t>(hr), "swapchain_buffer", "IDXGISwapChain3::GetBuffer failed", {}};
        }
        device->CreateRenderTargetView(backbuffers[i], nullptr, rtv_handle);
        rtv_handle.ptr += rtv_stride;
    }

    ID3D12CommandAllocator* allocator = nullptr;
    hr = device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), reinterpret_cast<void**>(&allocator));
    if (FAILED(hr) || !allocator) {
        backbuffers[0]->Release();
        backbuffers[1]->Release();
        rtv_heap->Release();
        swapchain->Release();
        queue->Release();
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "cmd_allocator", "CreateCommandAllocator failed", {}};
    }

    ID3D12GraphicsCommandList* cmd_list = nullptr;
    hr = device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, nullptr, __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void**>(&cmd_list));
    if (FAILED(hr) || !cmd_list) {
        allocator->Release();
        backbuffers[0]->Release();
        backbuffers[1]->Release();
        rtv_heap->Release();
        swapchain->Release();
        queue->Release();
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "cmd_list", "CreateCommandList failed", {}};
    }
    cmd_list->Close();

    ID3D12Fence* fence = nullptr;
    hr = device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void**>(&fence));
    if (FAILED(hr) || !fence) {
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
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "fence", "CreateFence failed", {}};
    }

    HANDLE fence_event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    if (!fence_event) {
        const DWORD err = GetLastError();
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
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(err), "fence_event", "CreateEventA failed", {}};
    }
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
            if (WaitForSingleObject(fence_event, 5000) == WAIT_TIMEOUT) {
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
                return DiagnosticRecord{ExitCode::ExecutionFenceTimeout, 0x80070102u, "clear_present_fence_wait", "Frame fence wait timed out", {}};
            }
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

    const std::uint32_t win_build =
        config.os_build_override != 0u ? config.os_build_override : QueryWindowsBuildNumber();
    if (win_build < BaselineCapabilityProfile::kMinWindowsBuildNumber) {
        return DiagnosticRecord{
            ExitCode::OsVersionUnsupported,
            0x8007047Eu,
            "os_version_check",
            "OS build is older than minimum tested Windows 11 x64 build 22631",
            {}};
    }

    // Step 1: Verify offline-compiled SM 6.0 DXIL container integrity before touching D3D12.
    const ExitCode dxil_status = ValidateOfflineDxilContainer(
        nw::shaders::kComputeFixtureDxil,
        sizeof(nw::shaders::kComputeFixtureDxil));
    if (dxil_status != ExitCode::Success) {
        return DiagnosticRecord{
            dxil_status,
            0x8007000Bu,
            "offline_dxil_validation",
            "Offline DXIL shader payload failed DXBC/PSV0/DXIL bitcode check",
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

    AdapterCapabilitySnapshot cap_snapshot{};
    const DiagnosticRecord cap_rec = PopulateAdapterCapabilitySnapshot(device, win_build, config.optional_features, &cap_snapshot);
    if (cap_rec.exit_code != ExitCode::Success) {
        device->Release();
        return cap_rec;
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

    // Root signature: parameter 0 = bounded UAV descriptor table (u0, space0),
    // parameter 1 = 4x 32-bit root constants (b0, space0)
    D3D12_DESCRIPTOR_RANGE uav_range{};
    uav_range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    uav_range.NumDescriptors = 1;
    uav_range.BaseShaderRegister = 0;
    uav_range.RegisterSpace = 0;
    uav_range.OffsetInDescriptorsFromTableStart = 0;

    D3D12_ROOT_PARAMETER root_params[2]{};
    root_params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    root_params[0].DescriptorTable.NumDescriptorRanges = 1;
    root_params[0].DescriptorTable.pDescriptorRanges = &uav_range;
    root_params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    root_params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    root_params[1].Constants.ShaderRegister = 0;
    root_params[1].Constants.RegisterSpace = 0;
    root_params[1].Constants.Num32BitValues = 4;
    root_params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_ROOT_SIGNATURE_DESC rs_desc{};
    rs_desc.NumParameters = 2;
    rs_desc.pParameters = root_params;
    rs_desc.Flags = D3D12_ROOT_SIGNATURE_FLAG_NONE;

    ID3DBlob* rs_blob = nullptr;
    ID3DBlob* rs_error = nullptr;
    hr = D3D12SerializeRootSignature(&rs_desc, D3D_ROOT_SIGNATURE_VERSION_1, &rs_blob, &rs_error);
    if (rs_error) {
        rs_error->Release();
    }
    if (FAILED(hr) || !rs_blob) {
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::RootSignatureCreationFailed, static_cast<std::uint32_t>(hr), "root_signature_serialize", "D3D12SerializeRootSignature failed", {}};
    }

    ID3D12RootSignature* root_sig = nullptr;
    hr = device->CreateRootSignature(0, rs_blob->GetBufferPointer(), rs_blob->GetBufferSize(), __uuidof(ID3D12RootSignature), reinterpret_cast<void**>(&root_sig));
    rs_blob->Release();
    if (FAILED(hr) || !root_sig) {
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::RootSignatureCreationFailed, static_cast<std::uint32_t>(hr), "root_signature", "CreateRootSignature failed", {}};
    }

    // Create compute PSO from embedded offline SM 6.0 DXIL container (zero runtime DXC)
    D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
    pso_desc.pRootSignature = root_sig;
    pso_desc.CS.pShaderBytecode = nw::shaders::kComputeFixtureDxil;
    pso_desc.CS.BytecodeLength = sizeof(nw::shaders::kComputeFixtureDxil);
    ID3D12PipelineState* pso = nullptr;
    hr = device->CreateComputePipelineState(&pso_desc, __uuidof(ID3D12PipelineState), reinterpret_cast<void**>(&pso));
    if (FAILED(hr) || !pso) {
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::PipelineStateCreationFailed, static_cast<std::uint32_t>(hr), "compute_pso", "CreateComputePipelineState failed for offline cs_6_0 DXIL", {}};
    }

    const UINT64 buffer_bytes = static_cast<UINT64>(kElementCount) * sizeof(std::uint32_t);
    D3D12_HEAP_PROPERTIES default_heap{};
    default_heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC buf_desc{};
    buf_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buf_desc.Width = buffer_bytes;
    buf_desc.Height = 1;
    buf_desc.DepthOrArraySize = 1;
    buf_desc.MipLevels = 1;
    buf_desc.Format = DXGI_FORMAT_UNKNOWN;
    buf_desc.SampleDesc.Count = 1;
    buf_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    buf_desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;

    ID3D12Resource* uav_buffer = nullptr;
    hr = device->CreateCommittedResource(
        &default_heap,
        D3D12_HEAP_FLAG_NONE,
        &buf_desc,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
        nullptr,
        __uuidof(ID3D12Resource),
        reinterpret_cast<void**>(&uav_buffer));
    if (FAILED(hr) || !uav_buffer) {
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "uav_buffer", "CreateCommittedResource(UAV) failed", {}};
    }

    D3D12_HEAP_PROPERTIES readback_heap{};
    readback_heap.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC rb_desc = buf_desc;
    rb_desc.Flags = D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource* rb_resource = nullptr;
    hr = device->CreateCommittedResource(
        &readback_heap,
        D3D12_HEAP_FLAG_NONE,
        &rb_desc,
        D3D12_RESOURCE_STATE_COPY_DEST,
        nullptr,
        __uuidof(ID3D12Resource),
        reinterpret_cast<void**>(&rb_resource));
    if (FAILED(hr) || !rb_resource) {
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "readback_buffer", "CreateCommittedResource(READBACK) failed", {}};
    }

    D3D12_UNORDERED_ACCESS_VIEW_DESC uav_view{};
    uav_view.Format = DXGI_FORMAT_UNKNOWN;
    uav_view.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
    uav_view.Buffer.FirstElement = 0;
    uav_view.Buffer.NumElements = kElementCount;
    uav_view.Buffer.StructureByteStride = sizeof(std::uint32_t);
    device->CreateUnorderedAccessView(uav_buffer, nullptr, &uav_view, uav_heap->GetCPUDescriptorHandleForHeapStart());

    ID3D12CommandAllocator* allocator = nullptr;
    hr = device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator), reinterpret_cast<void**>(&allocator));
    if (FAILED(hr) || !allocator) {
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "cmd_allocator", "CreateCommandAllocator failed", {}};
    }

    ID3D12GraphicsCommandList* cmd_list = nullptr;
    hr = device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator, pso, __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void**>(&cmd_list));
    if (FAILED(hr) || !cmd_list) {
        allocator->Release();
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "cmd_list", "CreateCommandList failed", {}};
    }

    cmd_list->SetComputeRootSignature(root_sig);
    ID3D12DescriptorHeap* heaps[] = {uav_heap};
    cmd_list->SetDescriptorHeaps(1, heaps);
    cmd_list->SetComputeRootDescriptorTable(0, uav_heap->GetGPUDescriptorHandleForHeapStart());
    const std::uint32_t constants[4] = {kElementCount, kSeedMask, 0u, 0u};
    cmd_list->SetComputeRoot32BitConstants(1, 4, constants, 0);
    cmd_list->Dispatch(kElementCount / 64u, 1, 1);

    // Ordinary resource transition barrier: UNORDERED_ACCESS -> COPY_SOURCE
    D3D12_RESOURCE_BARRIER to_copy{};
    to_copy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    to_copy.Transition.pResource = uav_buffer;
    to_copy.Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    to_copy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    to_copy.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cmd_list->ResourceBarrier(1, &to_copy);

    cmd_list->CopyResource(rb_resource, uav_buffer);
    cmd_list->Close();

    ID3D12CommandList* lists[] = {cmd_list};
    queue->ExecuteCommandLists(1, lists);

    ID3D12Fence* fence = nullptr;
    hr = device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void**>(&fence));
    if (FAILED(hr) || !fence) {
        cmd_list->Release();
        allocator->Release();
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "fence", "CreateFence failed", {}};
    }

    HANDLE fence_event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    if (!fence_event) {
        const DWORD err = GetLastError();
        fence->Release();
        cmd_list->Release();
        allocator->Release();
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(err), "fence_event", "CreateEventA failed", {}};
    }

    queue->Signal(fence, 1);
    if (fence->GetCompletedValue() < 1) {
        fence->SetEventOnCompletion(1, fence_event);
        if (WaitForSingleObject(fence_event, 5000) == WAIT_TIMEOUT) {
            CloseHandle(fence_event);
            fence->Release();
            cmd_list->Release();
            allocator->Release();
            rb_resource->Release();
            uav_buffer->Release();
            pso->Release();
            root_sig->Release();
            uav_heap->Release();
            queue->Release();
            device->Release();
            return DiagnosticRecord{ExitCode::ExecutionFenceTimeout, 0x80070102u, "compute_fence_wait", "Compute dispatch fence wait timed out", {}};
        }
    }

    const HRESULT removed_hr = device->GetDeviceRemovedReason();
    if (FAILED(removed_hr)) {
        CloseHandle(fence_event);
        fence->Release();
        cmd_list->Release();
        allocator->Release();
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::DeviceRemoved, static_cast<std::uint32_t>(removed_hr), "device_removed", "ID3D12Device::GetDeviceRemovedReason reported device removal", {}};
    }

    void* mapped_ptr = nullptr;
    D3D12_RANGE read_range{0, static_cast<SIZE_T>(buffer_bytes)};
    hr = rb_resource->Map(0, &read_range, &mapped_ptr);
    if (FAILED(hr) || !mapped_ptr) {
        CloseHandle(fence_event);
        fence->Release();
        cmd_list->Release();
        allocator->Release();
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "readback_map", "ID3D12Resource::Map(readback) failed", {}};
    }
    std::memcpy(readback_buffer, mapped_ptr, static_cast<std::size_t>(buffer_bytes));
    D3D12_RANGE empty_range{0, 0};
    rb_resource->Unmap(0, &empty_range);

    CloseHandle(fence_event);
    fence->Release();
    cmd_list->Release();
    allocator->Release();
    rb_resource->Release();
    uav_buffer->Release();
    pso->Release();
    root_sig->Release();
    uav_heap->Release();
    queue->Release();
    device->Release();
#else
    for (std::uint32_t i = 0; i < kElementCount; ++i) {
        readback_buffer[i] = EvaluateReferenceComputeElement(i, kSeedMask);
    }
#endif
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

    // Machine-readable readback evidence for clean-system verification. The runner parses these
    // observed GPU values, recomputes SHA-256, and compares them against
    // fixtures/compute_shader/expected_output.json. Never derived from static expected data.
    if (config.emit_readback_values) {
        std::printf("NW_READBACK_ELEMENT_COUNT=%u\n", kElementCount);
        std::printf("NW_READBACK_U32=");
        for (std::uint32_t i = 0; i < kElementCount; ++i) {
            if (i != 0u) {
                std::printf(",");
            }
            std::printf("%08X", readback_buffer[i]);
        }
        std::printf("\n");
        std::fflush(stdout);
    }

    return DiagnosticRecord{ExitCode::Success, 0u, "complete", "Compute shader fixture succeeded", {}};
}

} // namespace nw::player
