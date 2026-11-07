#include "d3d12_bootstrap.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>

#if defined(_WIN32)
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <psapi.h>
#include <d3d12.h>
#include <d3d12sdklayers.h>
#include <dxgi1_6.h>
#include <tlhelp32.h>
#endif

namespace nw::shaders {
#include "compute_fixture_dxil.h"
} // namespace nw::shaders

// The release profile suppresses the dynamic VCRuntime import library while keeping the
// static libvcruntime + inbox UCRT split. Define the floating-point ABI marker explicitly so
// the linker does not depend on a CRT startup object to provide it.
extern "C" int _fltused = 0x9875;

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
        } else if (std::strcmp(arg, "--emit-benchmark-metrics") == 0) {
            out_config->emit_benchmark_metrics = true;
        } else if (std::strcmp(arg, "--emit-loaded-modules") == 0) {
            out_config->emit_loaded_modules = true;
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

double NwNowMs() noexcept {
#if defined(_WIN32)
    LARGE_INTEGER frequency{};
    LARGE_INTEGER counter{};
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart == 0) {
        return 0.0;
    }
    QueryPerformanceCounter(&counter);
    return (1000.0 * static_cast<double>(counter.QuadPart)) /
           static_cast<double>(frequency.QuadPart);
#else
    return 0.0;
#endif
}

#if defined(_WIN32)
std::uint64_t QueryProcessCommittedBytes() noexcept {
    using K32GetProcessMemoryInfoFn =
        BOOL(WINAPI*)(HANDLE, PPROCESS_MEMORY_COUNTERS, DWORD);
    HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
    if (!kernel32) {
        return 0u;
    }
    auto get_process_memory_info = reinterpret_cast<K32GetProcessMemoryInfoFn>(
        reinterpret_cast<void*>(GetProcAddress(kernel32, "K32GetProcessMemoryInfo")));
    if (!get_process_memory_info) {
        return 0u;
    }
    PROCESS_MEMORY_COUNTERS_EX counters{};
    if (get_process_memory_info(
            GetCurrentProcess(),
            reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&counters),
            static_cast<DWORD>(sizeof(counters)))) {
        return static_cast<std::uint64_t>(counters.PrivateUsage);
    }
    return 0u;
}

struct ValidationSummary {
    bool has_error = false;
    bool has_corruption = false;
    unsigned int message_count = 0;
};

ValidationSummary DrainD3D12ValidationMessages(ID3D12InfoQueue* info_queue) noexcept {
    ValidationSummary summary{};
    if (!info_queue) {
        return summary;
    }
    const UINT64 stored = info_queue->GetNumStoredMessages();
    for (UINT64 index = 0; index < stored; ++index) {
        SIZE_T message_bytes = 0;
        if (FAILED(info_queue->GetMessage(index, nullptr, &message_bytes)) || message_bytes == 0) {
            continue;
        }
        if (message_bytes > 16384u) {
            std::printf(
                "NW_D3D12_VALIDATION_MESSAGE=index=%llu truncated_bytes=%llu\n",
                static_cast<unsigned long long>(index),
                static_cast<unsigned long long>(message_bytes));
            continue;
        }
        alignas(D3D12_MESSAGE) char buffer[16384]{};
        auto* message = reinterpret_cast<D3D12_MESSAGE*>(buffer);
        SIZE_T buffer_bytes = sizeof(buffer);
        if (FAILED(info_queue->GetMessage(index, message, &buffer_bytes))) {
            continue;
        }
        ++summary.message_count;
        const bool is_error =
            message->Severity == D3D12_MESSAGE_SEVERITY_ERROR ||
            message->Severity == D3D12_MESSAGE_SEVERITY_CORRUPTION;
        if (message->Severity == D3D12_MESSAGE_SEVERITY_CORRUPTION) {
            summary.has_corruption = true;
        }
        if (is_error) {
            summary.has_error = true;
            std::printf(
                "NW_D3D12_VALIDATION_ERROR=index=%llu severity=%d id=%d description=%s\n",
                static_cast<unsigned long long>(index),
                static_cast<int>(message->Severity),
                static_cast<int>(message->ID),
                message->pDescription ? message->pDescription : "");
        } else {
            std::printf(
                "NW_D3D12_VALIDATION_MESSAGE=index=%llu severity=%d id=%d description=%s\n",
                static_cast<unsigned long long>(index),
                static_cast<int>(message->Severity),
                static_cast<int>(message->ID),
                message->pDescription ? message->pDescription : "");
        }
    }
    std::fflush(stdout);
    return summary;
}

struct FenceWaitResult {
    ExitCode code = ExitCode::Success;
    HRESULT hresult = S_OK;
    const char* stage = "fence_wait";
};

FenceWaitResult WaitForFenceValue(
    ID3D12Fence* fence,
    UINT64 value,
    HANDLE fence_event,
    DWORD timeout_ms) noexcept {
    if (!fence || !fence_event) {
        return FenceWaitResult{ExitCode::GpuSubmissionFailed, E_INVALIDARG, "fence_wait_invalid"};
    }
    const UINT64 completed = fence->GetCompletedValue();
    if (completed == UINT64_MAX) {
        // A removed device reports UINT64_MAX. Never treat this as "already complete".
        return FenceWaitResult{
            ExitCode::DeviceRemoved,
            static_cast<HRESULT>(0x887A0005u), // DXGI_ERROR_DEVICE_REMOVED
            "fence_get_completed_value"};
    }
    if (completed >= value) {
        return FenceWaitResult{ExitCode::Success, S_OK, "fence_wait"};
    }
    const HRESULT set_hr = fence->SetEventOnCompletion(value, fence_event);
    if (FAILED(set_hr)) {
        return FenceWaitResult{ExitCode::GpuSubmissionFailed, set_hr, "fence_set_event"};
    }
    const DWORD wait_result = WaitForSingleObject(fence_event, timeout_ms);
    if (wait_result == WAIT_TIMEOUT) {
        return FenceWaitResult{
            ExitCode::ExecutionFenceTimeout,
            static_cast<HRESULT>(0x80070102u),
            "fence_wait_timeout"};
    }
    if (wait_result != WAIT_OBJECT_0) {
        return FenceWaitResult{
            ExitCode::GpuSubmissionFailed,
            HRESULT_FROM_WIN32(GetLastError()),
            "fence_wait_failed"};
    }
    return FenceWaitResult{ExitCode::Success, S_OK, "fence_wait"};
}

void EmitLoadedModuleInventory() noexcept {
    const DWORD process_id = GetCurrentProcessId();
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, process_id);
    if (snapshot == INVALID_HANDLE_VALUE) {
        return;
    }
    MODULEENTRY32W entry{};
    entry.dwSize = static_cast<DWORD>(sizeof(entry));
    unsigned int module_count = 0;
    if (Module32FirstW(snapshot, &entry)) {
        do {
            std::printf("NW_LOADED_MODULE=%ls\n", entry.szModule);
            ++module_count;
            entry.dwSize = static_cast<DWORD>(sizeof(entry));
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    std::printf("NW_MODULE_COUNT=%u\n", module_count);
    std::fflush(stdout);
}

struct AdapterHardwareInfo {
    bool valid = false;
    unsigned int vendor_id = 0;
    unsigned int device_id = 0;
    std::uint64_t vram_mb = 0;
    std::uint64_t gpu_committed_memory_bytes = 0;
};

AdapterHardwareInfo QueryAdapterHardwareInfo(IDXGIAdapter1* adapter) noexcept {
    AdapterHardwareInfo info{};
    if (!adapter) {
        return info;
    }
    DXGI_ADAPTER_DESC1 desc{};
    if (FAILED(adapter->GetDesc1(&desc))) {
        return info;
    }
    info.valid = true;
    info.vendor_id = desc.VendorId;
    info.device_id = desc.DeviceId;
    info.vram_mb = desc.DedicatedVideoMemory / (1024ull * 1024ull);
    IDXGIAdapter3* adapter3 = nullptr;
    if (SUCCEEDED(adapter->QueryInterface(
            __uuidof(IDXGIAdapter3), reinterpret_cast<void**>(&adapter3))) && adapter3) {
        DXGI_QUERY_VIDEO_MEMORY_INFO memory_info{};
        if (SUCCEEDED(adapter3->QueryVideoMemoryInfo(
                0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &memory_info))) {
            info.gpu_committed_memory_bytes = memory_info.CurrentUsage;
        }
        adapter3->Release();
    }
    return info;
}

void EmitBenchmarkMetrics(
    const AdapterCapabilitySnapshot& snapshot,
    const AdapterHardwareInfo& hardware,
    const char* adapter_name,
    double startup_ms,
    double cpu_ms,
    double gpu_ms) noexcept {
    std::printf("NW_STARTUP_MS=%.3f\n", startup_ms > 0.0 ? startup_ms : 0.001);
    std::printf("NW_CPU_TIME_MS=%.3f\n", cpu_ms > 0.0 ? cpu_ms : 0.001);
    if (gpu_ms > 0.0) {
        std::printf("NW_GPU_TIME_MS=%.3f\n", gpu_ms);
    }
    const std::uint64_t committed_bytes = QueryProcessCommittedBytes();
    if (committed_bytes > 0u) {
        std::printf("NW_COMMITTED_MEMORY_BYTES=%llu\n",
            static_cast<unsigned long long>(committed_bytes));
    }

    if (hardware.valid) {
        std::printf("NW_VENDOR_ID=%u\n", hardware.vendor_id);
        std::printf("NW_DEVICE_ID=%u\n", hardware.device_id);
        std::printf("NW_VRAM_MB=%llu\n",
            static_cast<unsigned long long>(hardware.vram_mb));
        if (hardware.gpu_committed_memory_bytes > 0) {
            std::printf("NW_GPU_COMMITTED_MEMORY_BYTES=%llu\n",
                static_cast<unsigned long long>(hardware.gpu_committed_memory_bytes));
        }
    }
    if (adapter_name && adapter_name[0] != '\0') {
        std::printf("NW_ADAPTER_DESCRIPTION=%s\n", adapter_name);
    }
    const char* feature_level_name = "unknown";
    if (snapshot.max_feature_level >= static_cast<std::uint32_t>(D3D_FEATURE_LEVEL_12_1)) {
        feature_level_name = "12_1";
    } else if (snapshot.max_feature_level >= static_cast<std::uint32_t>(D3D_FEATURE_LEVEL_12_0)) {
        feature_level_name = "12_0";
    } else if (snapshot.max_feature_level >= static_cast<std::uint32_t>(D3D_FEATURE_LEVEL_11_1)) {
        feature_level_name = "11_1";
    } else if (snapshot.max_feature_level >= static_cast<std::uint32_t>(D3D_FEATURE_LEVEL_11_0)) {
        feature_level_name = "11_0";
    }
    std::printf("NW_FEATURE_LEVEL=%s\n", feature_level_name);
    char shader_model_name[16] = "unknown";
    if (snapshot.max_shader_model >= BaselineCapabilityProfile::kRequiredShaderModel) {
        const unsigned int major = (snapshot.max_shader_model >> 4u) & 0xFu;
        const unsigned int minor = snapshot.max_shader_model & 0xFu;
        std::snprintf(shader_model_name, sizeof(shader_model_name), "%u_%u", major, minor);
    }
    std::printf("NW_MAX_SHADER_MODEL=%s\n", shader_model_name);
    std::printf("NW_BASELINE_FEATURE_LEVEL_12_0_SUPPORTED=%d\n",
        snapshot.max_feature_level >= BaselineCapabilityProfile::kRequiredFeatureLevel ? 1 : 0);
    std::printf("NW_BASELINE_SHADER_MODEL_6_0_SUPPORTED=%d\n",
        snapshot.max_shader_model >= BaselineCapabilityProfile::kRequiredShaderModel ? 1 : 0);
    std::printf("NW_RESOURCE_BINDING_TIER=%u\n", snapshot.resource_binding_tier);
    std::printf("NW_RESOURCE_HEAP_TIER=%u\n", snapshot.resource_heap_tier);
    std::printf("NW_WAVE_OPS_SUPPORTED=%d\n", snapshot.wave_ops_supported ? 1 : 0);
    std::fflush(stdout);
}
#endif

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

    static constexpr D3D_FEATURE_LEVEL kRequestedFeatureLevels[] = {
        D3D_FEATURE_LEVEL_12_1,
        D3D_FEATURE_LEVEL_12_0,
        D3D_FEATURE_LEVEL_11_1,
        D3D_FEATURE_LEVEL_11_0,
    };
    D3D12_FEATURE_DATA_FEATURE_LEVELS feature_levels{};
    feature_levels.NumFeatureLevels =
        static_cast<UINT>(sizeof(kRequestedFeatureLevels) / sizeof(kRequestedFeatureLevels[0]));
    feature_levels.pFeatureLevelsRequested = kRequestedFeatureLevels;
    HRESULT hr = device->CheckFeatureSupport(
        D3D12_FEATURE_FEATURE_LEVELS, &feature_levels, sizeof(feature_levels));
    if (FAILED(hr)) {
        return DiagnosticRecord{
            ExitCode::CapabilityBaselineUnmet,
            static_cast<std::uint32_t>(hr),
            "feature_level_check",
            "CheckFeatureSupport(D3D12_FEATURE_FEATURE_LEVELS) failed",
            {}};
    }
    out_snapshot->max_feature_level = static_cast<std::uint32_t>(feature_levels.MaxSupportedFeatureLevel);

    D3D12_FEATURE_DATA_SHADER_MODEL sm_data{D3D_SHADER_MODEL_6_0};
    hr = device->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm_data, sizeof(sm_data));
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
    for (UINT idx = 0;; ++idx) {
        IDXGIAdapter1* candidate = nullptr;
        const HRESULT enum_hr = factory->EnumAdapters1(idx, &candidate);
        if (enum_hr == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        if (FAILED(enum_hr) || !candidate) {
            if (candidate) {
                candidate->Release();
            }
            factory->Release();
            FreeLibrary(d3d12_mod);
            FreeLibrary(dxgi_mod);
            return DiagnosticRecord{
                ExitCode::AdapterEnumerationFailed,
                static_cast<std::uint32_t>(enum_hr),
                "adapter_enum",
                "IDXGIFactory1::EnumAdapters1 failed in empty_player bootstrap",
                {}};
        }
        DXGI_ADAPTER_DESC1 desc{};
        if (FAILED(candidate->GetDesc1(&desc))) {
            candidate->Release();
            continue;
        }
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0) {
            candidate->Release();
            continue;
        }
        if (SUCCEEDED(d3d12_create_device(candidate, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            selected_adapter = candidate;
            break;
        }
        candidate->Release();
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

    const double start_ms = NwNowMs();
    char adapter_name[128]{};

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
    for (UINT idx = 0;; ++idx) {
        IDXGIAdapter1* candidate = nullptr;
        const HRESULT enum_hr = factory->EnumAdapters1(idx, &candidate);
        if (enum_hr == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        if (FAILED(enum_hr) || !candidate) {
            if (candidate) {
                candidate->Release();
            }
            factory->Release();
            DestroyWindow(hwnd);
            return DiagnosticRecord{
                ExitCode::AdapterEnumerationFailed,
                static_cast<std::uint32_t>(enum_hr),
                "adapter_enum",
                "IDXGIFactory1::EnumAdapters1 failed in clear_present fixture",
                {}};
        }
        DXGI_ADAPTER_DESC1 desc{};
        if (FAILED(candidate->GetDesc1(&desc))) {
            candidate->Release();
            continue;
        }
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0) {
            candidate->Release();
            continue;
        }
        if (SUCCEEDED(D3D12CreateDevice(candidate, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            selected_adapter = candidate;
            break;
        }
        candidate->Release();
    }
    if (!selected_adapter) {
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::AdapterEnumerationFailed, 0x887A0004u, "adapter_enum", "No hardware adapter supporting Feature Level 12_0", {}};
    }

    ID3D12Device* device = nullptr;
    hr = D3D12CreateDevice(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), reinterpret_cast<void**>(&device));
    if (FAILED(hr) || !device) {
        selected_adapter->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return DiagnosticRecord{ExitCode::D3D12DeviceCreationFailed, static_cast<std::uint32_t>(hr), "d3d12_device", "D3D12CreateDevice(12_0) failed", {}};
    }

    DXGI_ADAPTER_DESC1 selected_desc{};
    const AdapterHardwareInfo hardware_info = QueryAdapterHardwareInfo(selected_adapter);
    if (SUCCEEDED(selected_adapter->GetDesc1(&selected_desc))) {
        for (std::size_t c = 0; c < 127u && selected_desc.Description[c] != L'\0'; ++c) {
            const wchar_t wc = selected_desc.Description[c];
            adapter_name[c] = (wc >= 0x20 && wc <= 0x7E) ? static_cast<char>(wc) : '?';
        }
    }
    selected_adapter->Release();

    AdapterCapabilitySnapshot cap_snapshot{};
    const DiagnosticRecord cap_rec = PopulateAdapterCapabilitySnapshot(device, win_build, config.optional_features, &cap_snapshot);
    if (cap_rec.exit_code != ExitCode::Success) {
        device->Release();
        factory->Release();
        DestroyWindow(hwnd);
        return cap_rec;
    }

    ID3D12InfoQueue* info_queue = nullptr;
    if (config.enable_d3d12_validation) {
        hr = device->QueryInterface(__uuidof(ID3D12InfoQueue), reinterpret_cast<void**>(&info_queue));
        if (FAILED(hr) || !info_queue) {
            device->Release();
            factory->Release();
            DestroyWindow(hwnd);
            return DiagnosticRecord{
                ExitCode::D3D12ValidationError,
                static_cast<std::uint32_t>(hr),
                "d3d12_info_queue",
                "D3D12 debug layer enabled but ID3D12InfoQueue is unavailable; cannot verify validation cleanliness.",
                {}};
        }
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
    const double setup_ms = NwNowMs() - start_ms;
    UINT64 fence_value = 1;

    // Optional GPU timestamp queries. If the adapter/queue does not support them the
    // measurement is reported as unavailable rather than zero.
    ID3D12QueryHeap* timestamp_heap = nullptr;
    ID3D12Resource* timestamp_readback = nullptr;
    UINT64 timestamp_frequency = 0;
    const UINT timestamp_count = config.frame_count * 2u;
    D3D12_QUERY_HEAP_DESC query_heap_desc{};
    query_heap_desc.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    query_heap_desc.Count = timestamp_count;
    if (SUCCEEDED(device->CreateQueryHeap(
            &query_heap_desc,
            __uuidof(ID3D12QueryHeap),
            reinterpret_cast<void**>(&timestamp_heap)))) {
        D3D12_HEAP_PROPERTIES timestamp_heap_props{};
        timestamp_heap_props.Type = D3D12_HEAP_TYPE_READBACK;
        D3D12_RESOURCE_DESC timestamp_desc{};
        timestamp_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        timestamp_desc.Width = static_cast<UINT64>(timestamp_count) * sizeof(UINT64);
        timestamp_desc.Height = 1;
        timestamp_desc.DepthOrArraySize = 1;
        timestamp_desc.MipLevels = 1;
        timestamp_desc.Format = DXGI_FORMAT_UNKNOWN;
        timestamp_desc.SampleDesc.Count = 1;
        timestamp_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(device->CreateCommittedResource(
                &timestamp_heap_props,
                D3D12_HEAP_FLAG_NONE,
                &timestamp_desc,
                D3D12_RESOURCE_STATE_COPY_DEST,
                nullptr,
                __uuidof(ID3D12Resource),
                reinterpret_cast<void**>(&timestamp_readback)))) {
            timestamp_heap->Release();
            timestamp_heap = nullptr;
        }
        if (FAILED(queue->GetTimestampFrequency(&timestamp_frequency)) ||
            timestamp_frequency == 0) {
            timestamp_frequency = 0;
        }
    }

    auto cleanup = [&]() noexcept {
        if (timestamp_readback) timestamp_readback->Release();
        if (timestamp_heap) timestamp_heap->Release();
        if (info_queue) info_queue->Release();
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
    };

    static constexpr float kClearColor[4] = {0.05f, 0.10f, 0.20f, 1.0f};

    for (std::uint32_t frame = 0; frame < config.frame_count; ++frame) {
        const UINT backbuffer_idx = swapchain->GetCurrentBackBufferIndex();
        HRESULT loop_hr = allocator->Reset();
        if (FAILED(loop_hr)) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::CommandListRecordingFailed,
                static_cast<std::uint32_t>(loop_hr),
                "command_allocator_reset",
                "ID3D12CommandAllocator::Reset failed",
                {}};
        }
        loop_hr = cmd_list->Reset(allocator, nullptr);
        if (FAILED(loop_hr)) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::CommandListRecordingFailed,
                static_cast<std::uint32_t>(loop_hr),
                "command_list_reset",
                "ID3D12GraphicsCommandList::Reset failed",
                {}};
        }
        if (timestamp_heap && timestamp_readback) {
            cmd_list->EndQuery(
                timestamp_heap, D3D12_QUERY_TYPE_TIMESTAMP, frame * 2u);
        }

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
        if (timestamp_heap && timestamp_readback) {
            cmd_list->EndQuery(
                timestamp_heap, D3D12_QUERY_TYPE_TIMESTAMP, frame * 2u + 1u);
            cmd_list->ResolveQueryData(
                timestamp_heap,
                D3D12_QUERY_TYPE_TIMESTAMP,
                frame * 2u,
                2u,
                timestamp_readback,
                static_cast<UINT64>(frame) * 2u * sizeof(UINT64));
        }

        // Ordinary resource transition barrier: RENDER_TARGET -> PRESENT
        D3D12_RESOURCE_BARRIER to_present = to_rt;
        to_present.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        to_present.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        cmd_list->ResourceBarrier(1, &to_present);

        loop_hr = cmd_list->Close();
        if (FAILED(loop_hr)) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::CommandListRecordingFailed,
                static_cast<std::uint32_t>(loop_hr),
                "command_list_close",
                "ID3D12GraphicsCommandList::Close failed",
                {}};
        }
        ID3D12CommandList* lists[] = {cmd_list};
        queue->ExecuteCommandLists(1, lists);
        const HRESULT present_hr = swapchain->Present(0, 0);
        if (FAILED(present_hr)) {
            const HRESULT removed = device->GetDeviceRemovedReason();
            const HRESULT failure = FAILED(removed) ? removed : present_hr;
            cleanup();
            return DiagnosticRecord{
                FAILED(removed) ? ExitCode::DeviceRemoved : ExitCode::PresentationFailed,
                static_cast<std::uint32_t>(failure),
                FAILED(removed) ? "device_removed" : "swapchain_present",
                "IDXGISwapChain3::Present failed",
                {}};
        }

        const UINT64 signal_val = fence_value++;
        const HRESULT signal_hr = queue->Signal(fence, signal_val);
        if (FAILED(signal_hr)) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::GpuSubmissionFailed,
                static_cast<std::uint32_t>(signal_hr),
                "queue_signal",
                "ID3D12CommandQueue::Signal failed",
                {}};
        }
        const FenceWaitResult wait_result =
            WaitForFenceValue(fence, signal_val, fence_event, 5000);
        if (wait_result.code != ExitCode::Success) {
            cleanup();
            return DiagnosticRecord{
                wait_result.code,
                static_cast<std::uint32_t>(wait_result.hresult),
                wait_result.stage,
                wait_result.code == ExitCode::DeviceRemoved
                    ? "Device removal reported while waiting for clear/present frame completion"
                    : "Clear/present frame fence wait failed",
                {}};
        }
        const HRESULT removed_hr = device->GetDeviceRemovedReason();
        if (FAILED(removed_hr)) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::DeviceRemoved,
                static_cast<std::uint32_t>(removed_hr),
                "device_removed",
                "ID3D12Device::GetDeviceRemovedReason reported device removal after Present",
                {}};
        }
    }

    double gpu_ms = 0.0;
    if (timestamp_heap && timestamp_readback && timestamp_frequency != 0) {
        void* mapped = nullptr;
        D3D12_RANGE read_range{0, static_cast<SIZE_T>(timestamp_count) * sizeof(UINT64)};
        if (SUCCEEDED(timestamp_readback->Map(0, &read_range, &mapped)) && mapped) {
            const auto* timestamps = static_cast<const UINT64*>(mapped);
            UINT64 elapsed_ticks = 0;
            for (UINT frame = 0; frame < config.frame_count; ++frame) {
                const UINT64 begin = timestamps[frame * 2u];
                const UINT64 end = timestamps[frame * 2u + 1u];
                if (end >= begin) {
                    elapsed_ticks += end - begin;
                }
            }
            if (elapsed_ticks > 0) {
                gpu_ms = (1000.0 * static_cast<double>(elapsed_ticks)) /
                         static_cast<double>(timestamp_frequency);
            }
            D3D12_RANGE empty_range{0, 0};
            timestamp_readback->Unmap(0, &empty_range);
        }
    }

    if (config.enable_d3d12_validation && info_queue) {
        const ValidationSummary validation = DrainD3D12ValidationMessages(info_queue);
        if (validation.has_error || validation.has_corruption) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::D3D12ValidationError,
                0x887A0001u,
                "d3d12_validation_info_queue",
                "D3D12 debug layer emitted error/corruption messages; see NW_D3D12_VALIDATION_ERROR lines.",
                {}};
        }
    }

    if (config.emit_benchmark_metrics) {
        EmitBenchmarkMetrics(
            cap_snapshot, hardware_info, adapter_name, setup_ms, NwNowMs() - start_ms, gpu_ms);
    }
    if (config.emit_loaded_modules) {
        EmitLoadedModuleInventory();
    }
    cleanup();
#endif

    return DiagnosticRecord{ExitCode::Success, 0u, "complete", "Clear/Present fixture succeeded", {}};
}

DiagnosticRecord ExecuteComputeShaderFixture(const BootstrapConfig& config) noexcept {
    if (config.has_simulated_failure) {
        return MakeSimulatedFailureRecord(config.simulated_failure);
    }

    const double start_ms = NwNowMs();

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
    char adapter_name[128]{};

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
    for (UINT idx = 0;; ++idx) {
        IDXGIAdapter1* candidate = nullptr;
        const HRESULT enum_hr = factory->EnumAdapters1(idx, &candidate);
        if (enum_hr == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        if (FAILED(enum_hr) || !candidate) {
            if (candidate) {
                candidate->Release();
            }
            factory->Release();
            return DiagnosticRecord{
                ExitCode::AdapterEnumerationFailed,
                static_cast<std::uint32_t>(enum_hr),
                "adapter_enum",
                "IDXGIFactory1::EnumAdapters1 failed in compute_shader fixture",
                {}};
        }
        DXGI_ADAPTER_DESC1 desc{};
        if (FAILED(candidate->GetDesc1(&desc))) {
            candidate->Release();
            continue;
        }
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0) {
            candidate->Release();
            continue;
        }
        if (SUCCEEDED(D3D12CreateDevice(candidate, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr))) {
            for (std::size_t c = 0; c < 127u && desc.Description[c] != L'\0'; ++c) {
                const wchar_t wc = desc.Description[c];
                adapter_name[c] = (wc >= 0x20 && wc <= 0x7E) ? static_cast<char>(wc) : '?';
            }
            selected_adapter = candidate;
            break;
        }
        candidate->Release();
    }
    if (!selected_adapter) {
        factory->Release();
        return DiagnosticRecord{ExitCode::AdapterEnumerationFailed, 0x887A0004u, "adapter_enum", "No hardware adapter supporting Feature Level 12_0", {}};
    }

    ID3D12Device* device = nullptr;
    hr = D3D12CreateDevice(selected_adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), reinterpret_cast<void**>(&device));
    if (FAILED(hr) || !device) {
        selected_adapter->Release();
        factory->Release();
        return DiagnosticRecord{ExitCode::D3D12DeviceCreationFailed, static_cast<std::uint32_t>(hr), "d3d12_device", "D3D12CreateDevice(12_0) failed", {}};
    }
    const AdapterHardwareInfo hardware_info = QueryAdapterHardwareInfo(selected_adapter);
    selected_adapter->Release();
    factory->Release();

    AdapterCapabilitySnapshot cap_snapshot{};
    const DiagnosticRecord cap_rec = PopulateAdapterCapabilitySnapshot(device, win_build, config.optional_features, &cap_snapshot);
    if (cap_rec.exit_code != ExitCode::Success) {
        device->Release();
        return cap_rec;
    }

    ID3D12InfoQueue* info_queue = nullptr;
    if (config.enable_d3d12_validation) {
        hr = device->QueryInterface(__uuidof(ID3D12InfoQueue), reinterpret_cast<void**>(&info_queue));
        if (FAILED(hr) || !info_queue) {
            device->Release();
            return DiagnosticRecord{
                ExitCode::D3D12ValidationError,
                static_cast<std::uint32_t>(hr),
                "d3d12_info_queue",
                "D3D12 debug layer enabled but ID3D12InfoQueue is unavailable; cannot verify validation cleanliness.",
                {}};
        }
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

    ID3D12QueryHeap* timestamp_heap = nullptr;
    ID3D12Resource* timestamp_readback = nullptr;
    UINT64 timestamp_frequency = 0;
    D3D12_QUERY_HEAP_DESC timestamp_heap_desc{};
    timestamp_heap_desc.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    timestamp_heap_desc.Count = 2;
    if (SUCCEEDED(device->CreateQueryHeap(
            &timestamp_heap_desc,
            __uuidof(ID3D12QueryHeap),
            reinterpret_cast<void**>(&timestamp_heap)))) {
        D3D12_HEAP_PROPERTIES timestamp_props{};
        timestamp_props.Type = D3D12_HEAP_TYPE_READBACK;
        D3D12_RESOURCE_DESC timestamp_desc{};
        timestamp_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        timestamp_desc.Width = 2u * sizeof(UINT64);
        timestamp_desc.Height = 1;
        timestamp_desc.DepthOrArraySize = 1;
        timestamp_desc.MipLevels = 1;
        timestamp_desc.Format = DXGI_FORMAT_UNKNOWN;
        timestamp_desc.SampleDesc.Count = 1;
        timestamp_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(device->CreateCommittedResource(
                &timestamp_props,
                D3D12_HEAP_FLAG_NONE,
                &timestamp_desc,
                D3D12_RESOURCE_STATE_COPY_DEST,
                nullptr,
                __uuidof(ID3D12Resource),
                reinterpret_cast<void**>(&timestamp_readback)))) {
            timestamp_heap->Release();
            timestamp_heap = nullptr;
        }
        if (FAILED(queue->GetTimestampFrequency(&timestamp_frequency)) ||
            timestamp_frequency == 0) {
            timestamp_frequency = 0;
        }
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
    if (timestamp_heap && timestamp_readback) {
        cmd_list->EndQuery(timestamp_heap, D3D12_QUERY_TYPE_TIMESTAMP, 0);
    }
    cmd_list->Dispatch(kElementCount / 64u, 1, 1);
    if (timestamp_heap && timestamp_readback) {
        cmd_list->EndQuery(timestamp_heap, D3D12_QUERY_TYPE_TIMESTAMP, 1);
        cmd_list->ResolveQueryData(
            timestamp_heap,
            D3D12_QUERY_TYPE_TIMESTAMP,
            0,
            2,
            timestamp_readback,
            0);
    }

    // Ordinary resource transition barrier: UNORDERED_ACCESS -> COPY_SOURCE
    D3D12_RESOURCE_BARRIER to_copy{};
    to_copy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    to_copy.Transition.pResource = uav_buffer;
    to_copy.Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    to_copy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    to_copy.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cmd_list->ResourceBarrier(1, &to_copy);

    cmd_list->CopyResource(rb_resource, uav_buffer);
    const HRESULT close_hr = cmd_list->Close();
    if (FAILED(close_hr)) {
        if (timestamp_readback) timestamp_readback->Release();
        if (timestamp_heap) timestamp_heap->Release();
        if (info_queue) info_queue->Release();
        cmd_list->Release();
        allocator->Release();
        rb_resource->Release();
        uav_buffer->Release();
        pso->Release();
        root_sig->Release();
        uav_heap->Release();
        queue->Release();
        device->Release();
        return DiagnosticRecord{
            ExitCode::CommandListRecordingFailed,
            static_cast<std::uint32_t>(close_hr),
            "command_list_close",
            "ID3D12GraphicsCommandList::Close failed",
            {}};
    }

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

    const double setup_ms = NwNowMs() - start_ms;
    auto cleanup = [&]() noexcept {
        if (timestamp_readback) timestamp_readback->Release();
        if (timestamp_heap) timestamp_heap->Release();
        if (info_queue) info_queue->Release();
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
    };

    const HRESULT signal_hr = queue->Signal(fence, 1);
    if (FAILED(signal_hr)) {
        cleanup();
        return DiagnosticRecord{
            ExitCode::GpuSubmissionFailed,
            static_cast<std::uint32_t>(signal_hr),
            "queue_signal",
            "ID3D12CommandQueue::Signal failed",
            {}};
    }
    const FenceWaitResult wait_result = WaitForFenceValue(fence, 1, fence_event, 5000);
    if (wait_result.code != ExitCode::Success) {
        cleanup();
        return DiagnosticRecord{
            wait_result.code,
            static_cast<std::uint32_t>(wait_result.hresult),
            wait_result.stage,
            wait_result.code == ExitCode::DeviceRemoved
                ? "Device removal reported while waiting for compute completion"
                : "Compute dispatch fence wait failed",
            {}};
    }

    const HRESULT removed_hr = device->GetDeviceRemovedReason();
    if (FAILED(removed_hr)) {
        cleanup();
        return DiagnosticRecord{ExitCode::DeviceRemoved, static_cast<std::uint32_t>(removed_hr), "device_removed", "ID3D12Device::GetDeviceRemovedReason reported device removal", {}};
    }

    void* mapped_ptr = nullptr;
    D3D12_RANGE read_range{0, static_cast<SIZE_T>(buffer_bytes)};
    hr = rb_resource->Map(0, &read_range, &mapped_ptr);
    if (FAILED(hr) || !mapped_ptr) {
        cleanup();
        return DiagnosticRecord{ExitCode::ResourceAllocationFailed, static_cast<std::uint32_t>(hr), "readback_map", "ID3D12Resource::Map(readback) failed", {}};
    }
    std::memcpy(readback_buffer, mapped_ptr, static_cast<std::size_t>(buffer_bytes));
    D3D12_RANGE empty_range{0, 0};
    rb_resource->Unmap(0, &empty_range);

    double gpu_ms = 0.0;
    if (timestamp_heap && timestamp_readback && timestamp_frequency != 0) {
        void* timestamp_mapped = nullptr;
        D3D12_RANGE timestamp_range{0, 2u * sizeof(UINT64)};
        if (SUCCEEDED(timestamp_readback->Map(0, &timestamp_range, &timestamp_mapped)) &&
            timestamp_mapped) {
            const auto* timestamps = static_cast<const UINT64*>(timestamp_mapped);
            if (timestamps[1] >= timestamps[0] && timestamps[1] > timestamps[0]) {
                gpu_ms = (1000.0 * static_cast<double>(timestamps[1] - timestamps[0])) /
                         static_cast<double>(timestamp_frequency);
            }
            D3D12_RANGE timestamp_empty{0, 0};
            timestamp_readback->Unmap(0, &timestamp_empty);
        }
    }

    if (config.enable_d3d12_validation && info_queue) {
        const ValidationSummary validation = DrainD3D12ValidationMessages(info_queue);
        if (validation.has_error || validation.has_corruption) {
            cleanup();
            return DiagnosticRecord{
                ExitCode::D3D12ValidationError,
                0x887A0001u,
                "d3d12_validation_info_queue",
                "D3D12 debug layer emitted error/corruption messages; see NW_D3D12_VALIDATION_ERROR lines.",
                {}};
        }
    }

    if (config.emit_benchmark_metrics) {
        EmitBenchmarkMetrics(
            cap_snapshot, hardware_info, adapter_name, setup_ms, NwNowMs() - start_ms, gpu_ms);
    }
    if (config.emit_loaded_modules) {
        EmitLoadedModuleInventory();
    }
    cleanup();
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
        std::printf("NW_WINDOWS_BUILD=%u\n", win_build);
        if (adapter_name[0] != '\0') {
            std::printf("NW_ADAPTER_DESCRIPTION=%s\n", adapter_name);
        }
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
