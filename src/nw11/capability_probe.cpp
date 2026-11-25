// NW-11 capability probe.
//
// This is an optional-profile, validation-bundle-only executable.  It is not
// linked into the common player, never becomes a common dependency, and is
// only used to record the actual adapter, driver, OS, and exact D3D12 feature
// queries before the launcher decides optional-profile eligibility.
//
// The probe is deliberately read-only: it creates a device only to call
// CheckFeatureSupport and never builds pipelines, allocates scene resources,
// or substitutes an effect.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <d3d12.h>
#include <dxgi1_4.h>
#include <winternl.h>
#include <winver.h>
#include <wrl/client.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "capture_release_frame.h"

using Microsoft::WRL::ComPtr;

namespace {

constexpr int kExitOk = 0;
constexpr int kExitMalformedArgs = 2;
constexpr int kExitInitializationFailed = 4;

struct Options {
    std::string json_path;
    bool self_check = false;
    bool has_adapter_luid = false;
    LUID adapter_luid{};
    bool has_adapter_index = false;
    UINT adapter_index = 0;
};

std::string JsonEscape(const std::string& input) {
    std::string output;
    output.reserve(input.size() + 8);
    for (unsigned char character : input) {
        switch (character) {
            case '\\': output += "\\\\"; break;
            case '"': output += "\\\""; break;
            case '\n': output += "\\n"; break;
            case '\r': output += "\\r"; break;
            case '\t': output += "\\t"; break;
            default:
                if (character < 0x20) {
                    char buffer[8] = {};
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", character);
                    output += buffer;
                } else {
                    output.push_back(static_cast<char>(character));
                }
        }
    }
    return output;
}

std::string Narrow(const wchar_t* text) {
    if (text == nullptr || text[0] == L'\0') {
        return {};
    }
    int bytes = WideCharToMultiByte(CP_UTF8, 0, text, -1, nullptr, 0, nullptr, nullptr);
    if (bytes <= 1) {
        return {};
    }
    // WideCharToMultiByte writes `bytes` characters including the terminating
    // NUL when cchWideChar is -1.  Allocate exactly that many characters and
    // only then discard the NUL; passing a bytes-1 buffer here would write one
    // byte past the string.
    std::string output(static_cast<size_t>(bytes), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text, -1, output.data(), bytes, nullptr, nullptr);
    output.resize(static_cast<size_t>(bytes - 1));
    return output;
}

std::string LuidString(const LUID& luid) {
    char buffer[32] = {};
    std::snprintf(
        buffer,
        sizeof(buffer),
        "%08lX:%08lX",
        static_cast<unsigned long>(luid.HighPart),
        static_cast<unsigned long>(luid.LowPart));
    return buffer;
}

std::string WindowsBuild() {
    using RtlGetVersionFn = LONG(WINAPI*)(PRTL_OSVERSIONINFOW);
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    if (ntdll != nullptr) {
        auto rtl_get_version =
            reinterpret_cast<RtlGetVersionFn>(GetProcAddress(ntdll, "RtlGetVersion"));
        if (rtl_get_version != nullptr) {
            RTL_OSVERSIONINFOW info{};
            info.dwOSVersionInfoSize = sizeof(info);
            if (rtl_get_version(&info) == 0) {
                char buffer[64] = {};
                std::snprintf(
                    buffer,
                    sizeof(buffer),
                    "%lu.%lu.%lu.%lu",
                    static_cast<unsigned long>(info.dwMajorVersion),
                    static_cast<unsigned long>(info.dwMinorVersion),
                    static_cast<unsigned long>(info.dwBuildNumber),
                    static_cast<unsigned long>(info.dwPlatformId));
                std::string value(buffer);
                const auto last_dot = value.rfind('.');
                if (last_dot != std::string::npos) {
                    value.erase(last_dot);
                }
                return value;
            }
        }
    }
    return "unavailable";
}

std::string DriverVersionForAdapter(IDXGIAdapter1* adapter) {
    // WDDM 2.3+ requires D3D9 and D3D12 UMDs in a driver package to
    // share this version, and the query is bound to the selected adapter.
    LARGE_INTEGER version{};
    if (adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &version) != S_OK ||
        version.QuadPart == 0) return "unavailable";
    char text[64]{};
    std::snprintf(text, sizeof(text), "%u.%u.%u.%u",
        static_cast<unsigned>(HIWORD(version.HighPart)),
        static_cast<unsigned>(LOWORD(version.HighPart)),
        static_cast<unsigned>(HIWORD(version.LowPart)),
        static_cast<unsigned>(LOWORD(version.LowPart)));
    return text;
}

std::string NativeArchitecture() {
    USHORT process_machine = 0;
    USHORT native_machine = 0;
    if (!IsWow64Process2(GetCurrentProcess(), &process_machine, &native_machine))
        return "unavailable";
    return native_machine == IMAGE_FILE_MACHINE_AMD64 ? "x64" : "other";
}

std::string DisplayMode() {
    DEVMODEW mode{};
    mode.dmSize = sizeof(mode);
    if (!EnumDisplaySettingsW(nullptr, ENUM_CURRENT_SETTINGS, &mode)) return "unavailable";
    return std::to_string(mode.dmPelsWidth) + "x" + std::to_string(mode.dmPelsHeight) +
        "@" + std::to_string(mode.dmDisplayFrequency);
}

std::string ModuleVersion(const wchar_t* module_name) {
    wchar_t system_directory[MAX_PATH] = {};
    UINT length = GetSystemDirectoryW(system_directory, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) {
        return "unavailable";
    }
    std::wstring path(system_directory);
    path += L"\\";
    path += module_name;
    DWORD ignored = 0;
    DWORD size = GetFileVersionInfoSizeW(path.c_str(), &ignored);
    if (size == 0) {
        return "unavailable";
    }
    std::vector<BYTE> data(size);
    if (!GetFileVersionInfoW(path.c_str(), 0, size, data.data())) {
        return "unavailable";
    }
    VS_FIXEDFILEINFO* info = nullptr;
    UINT info_size = 0;
    if (!VerQueryValueW(data.data(), L"\\", reinterpret_cast<LPVOID*>(&info), &info_size) ||
        info == nullptr) {
        return "unavailable";
    }
    char buffer[64] = {};
    std::snprintf(
        buffer,
        sizeof(buffer),
        "%u.%u.%u.%u",
        static_cast<unsigned>(HIWORD(info->dwFileVersionMS)),
        static_cast<unsigned>(LOWORD(info->dwFileVersionMS)),
        static_cast<unsigned>(HIWORD(info->dwFileVersionLS)),
        static_cast<unsigned>(LOWORD(info->dwFileVersionLS)));
    return buffer;
}

const char* FeatureLevelName(D3D_FEATURE_LEVEL level) {
    switch (level) {
        case D3D_FEATURE_LEVEL_12_2: return "D3D_FEATURE_LEVEL_12_2";
        case D3D_FEATURE_LEVEL_12_1: return "D3D_FEATURE_LEVEL_12_1";
        case D3D_FEATURE_LEVEL_12_0: return "D3D_FEATURE_LEVEL_12_0";
        case D3D_FEATURE_LEVEL_11_1: return "D3D_FEATURE_LEVEL_11_1";
        case D3D_FEATURE_LEVEL_11_0: return "D3D_FEATURE_LEVEL_11_0";
        default: return "D3D_FEATURE_LEVEL_UNSUPPORTED";
    }
}

const char* ShaderModelName(D3D_SHADER_MODEL model) {
    switch (model) {
        case static_cast<D3D_SHADER_MODEL>(0x68): return "D3D12_SHADER_MODEL_6_8";
        case static_cast<D3D_SHADER_MODEL>(0x67): return "D3D12_SHADER_MODEL_6_7";
        case D3D_SHADER_MODEL_6_6: return "D3D12_SHADER_MODEL_6_6";
        case D3D_SHADER_MODEL_6_5: return "D3D12_SHADER_MODEL_6_5";
        case D3D_SHADER_MODEL_6_4: return "D3D12_SHADER_MODEL_6_4";
        case D3D_SHADER_MODEL_6_3: return "D3D12_SHADER_MODEL_6_3";
        case D3D_SHADER_MODEL_6_2: return "D3D12_SHADER_MODEL_6_2";
        case D3D_SHADER_MODEL_6_1: return "D3D12_SHADER_MODEL_6_1";
        case D3D_SHADER_MODEL_6_0: return "D3D12_SHADER_MODEL_6_0";
        default: return "D3D12_SHADER_MODEL_UNSUPPORTED";
    }
}

const char* MeshTierName(D3D12_MESH_SHADER_TIER tier) {
    switch (tier) {
        case D3D12_MESH_SHADER_TIER_1: return "D3D12_MESH_SHADER_TIER_1";
        default: return "D3D12_MESH_SHADER_TIER_NOT_SUPPORTED";
    }
}

const char* BindingTierName(D3D12_RESOURCE_BINDING_TIER tier) {
    switch (tier) {
        case D3D12_RESOURCE_BINDING_TIER_3: return "D3D12_RESOURCE_BINDING_TIER_3";
        case D3D12_RESOURCE_BINDING_TIER_2: return "D3D12_RESOURCE_BINDING_TIER_2";
        case D3D12_RESOURCE_BINDING_TIER_1: return "D3D12_RESOURCE_BINDING_TIER_1";
        default: return "D3D12_RESOURCE_BINDING_TIER_UNSUPPORTED";
    }
}

const char* HeapTierName(D3D12_RESOURCE_HEAP_TIER tier) {
    switch (tier) {
        case D3D12_RESOURCE_HEAP_TIER_2: return "D3D12_RESOURCE_HEAP_TIER_2";
        default: return "D3D12_RESOURCE_HEAP_TIER_UNSUPPORTED";
    }
}

bool ParseArguments(int argc, char** argv, Options& options) {
    for (int index = 1; index < argc; ++index) {
        if (std::strcmp(argv[index], "--json") == 0) {
            if (index + 1 >= argc) {
                return false;
            }
            options.json_path = argv[++index];
        } else if (std::strcmp(argv[index], "--self-check") == 0) {
            options.self_check = true;
        } else if (std::strcmp(argv[index], "--adapter-index") == 0) {
            if (index + 1 >= argc) {
                return false;
            }
            options.adapter_index =
                static_cast<UINT>(std::strtoul(argv[++index], nullptr, 10));
            options.has_adapter_index = true;
        } else if (std::strcmp(argv[index], "--adapter-luid") == 0) {
            if (index + 1 >= argc) {
                return false;
            }
            const char* text = argv[++index];
            unsigned long long high = 0;
            unsigned long long low = 0;
            if (std::sscanf(text, "%llx:%llx", &high, &low) != 2 &&
                std::sscanf(text, "%llu:%llu", &high, &low) != 2) {
                return false;
            }
            options.adapter_luid.HighPart = static_cast<LONG>(high);
            options.adapter_luid.LowPart = static_cast<DWORD>(low);
            options.has_adapter_luid = true;
        } else {
            return false;
        }
    }
    return true;
}

void WriteOutput(const std::string& path, const std::string& payload) {
    if (path.empty()) {
        std::fputs(payload.c_str(), stdout);
        std::fputc('\n', stdout);
        return;
    }
    FILE* file = nullptr;
    if (fopen_s(&file, path.c_str(), "wb") != 0 || file == nullptr) {
        std::fputs(payload.c_str(), stdout);
        std::fputc('\n', stdout);
        return;
    }
    std::fwrite(payload.data(), 1, payload.size(), file);
    std::fputc('\n', file);
    std::fclose(file);
}

std::string JsonNumber(unsigned int value) {
    char buffer[32] = {};
    std::snprintf(buffer, sizeof(buffer), "%u", value);
    return buffer;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 4 && std::strcmp(argv[1], "--capture-release-pid") == 0) {
        const auto pid = static_cast<DWORD>(std::strtoul(argv[2], nullptr, 10));
        return nw11::CaptureReleaseFrame(pid, argv[3]) ? kExitOk : kExitInitializationFailed;
    }
    Options options;
    if (!ParseArguments(argc, argv, options)) {
        std::fputs(
            "[NW11-PROBE][exit_code=2] usage: nw11_capability_probe.exe "
            "[--json <path>] [--adapter-index <n>] [--adapter-luid <high>:<low>] "
            "[--self-check]\n",
            stderr);
        return kExitMalformedArgs;
    }
    if (options.self_check) {
        WriteOutput(
            options.json_path,
            "{\"format\":\"nw11-capability-snapshot\",\"version\":1,\"status\":\"self_check_ok\"}");
        return kExitOk;
    }

    const auto fail = [&options](const char* stage, HRESULT hr) {
        char payload[512]{};
        std::snprintf(payload, sizeof(payload),
            "{\"format\":\"nw11-capability-snapshot\",\"version\":1,"
            "\"status\":\"failed\",\"initialization\":{\"status\":\"failed\","
            "\"stage\":\"%s\",\"hresult\":\"0x%08lX\"}}",
            stage, static_cast<unsigned long>(hr));
        WriteOutput(options.json_path, payload);
        std::fprintf(stderr, "[NW11-PROBE][exit_code=4] %s failed (0x%08lX)\n",
            stage, static_cast<unsigned long>(hr));
        return kExitInitializationFailed;
    };

    ComPtr<IDXGIFactory1> factory;
    HRESULT hr = CreateDXGIFactory1(IID_PPV_ARGS(&factory));
    if (FAILED(hr)) {
        return fail("CreateDXGIFactory1", hr);
    }

    ComPtr<IDXGIAdapter1> adapter;
    std::vector<std::string> adapter_records;
    DXGI_ADAPTER_DESC1 best_desc{};
    std::string release_default_luid;
    bool have_best = false;
    for (UINT index = 0;; ++index) {
        ComPtr<IDXGIAdapter1> candidate;
        hr = factory->EnumAdapters1(index, &candidate);
        if (hr == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        if (FAILED(hr) || candidate == nullptr) {
            return fail("EnumAdapters1", FAILED(hr) ? hr : E_POINTER);
        }
        DXGI_ADAPTER_DESC1 candidate_desc{};
        hr = candidate->GetDesc1(&candidate_desc);
        if (FAILED(hr)) {
            return fail("GetDesc1", hr);
        }
        const bool software = (candidate_desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
        // The byte-frozen NW-07 release selects the first hardware adapter
        // that can create its FL12.0 device. Prove its use before capture.
        if (!software && release_default_luid.empty() &&
            SUCCEEDED(D3D12CreateDevice(candidate.Get(), D3D_FEATURE_LEVEL_12_0,
                                       __uuidof(ID3D12Device), nullptr))) {
            release_default_luid = LuidString(candidate_desc.AdapterLuid);
        }
        char record[1024] = {};
        std::snprintf(
            record,
            sizeof(record),
            "{\"index\": %u, \"adapter_name\": \"%s\", "
            "\"vendor_id\": %u, \"device_id\": %u, "
            "\"dedicated_video_memory_bytes\": %llu, \"software\": %s}",
            index,
            JsonEscape(Narrow(candidate_desc.Description)).c_str(),
            candidate_desc.VendorId,
            candidate_desc.DeviceId,
            static_cast<unsigned long long>(candidate_desc.DedicatedVideoMemory),
            software ? "true" : "false");
        adapter_records.emplace_back(record);

        bool selected = false;
        if (options.has_adapter_luid) {
            selected = candidate_desc.AdapterLuid.HighPart ==
                           options.adapter_luid.HighPart &&
                       candidate_desc.AdapterLuid.LowPart ==
                           options.adapter_luid.LowPart;
        } else if (options.has_adapter_index) {
            selected = index == options.adapter_index;
        } else {
            // Default selection avoids an integrated GPU on multi-GPU
            // validation hosts: choose the hardware adapter with the largest
            // dedicated video memory.  Marketing names never decide this.
            selected =
                !software &&
                (!have_best ||
                 candidate_desc.DedicatedVideoMemory >
                     best_desc.DedicatedVideoMemory);
        }
        if (selected && !software) {
            adapter = candidate;
            best_desc = candidate_desc;
            have_best = true;
        }
    }
    if (!have_best || adapter == nullptr) {
        return fail("select hardware adapter", DXGI_ERROR_NOT_FOUND);
    }
    DXGI_ADAPTER_DESC1 description = best_desc;

    ComPtr<ID3D12Device> device;
    hr = D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device));
    if (FAILED(hr) || device == nullptr) {
        return fail("D3D12CreateDevice(D3D_FEATURE_LEVEL_12_0)", FAILED(hr) ? hr : E_POINTER);
    }

    D3D12_FEATURE_DATA_FEATURE_LEVELS levels{};
    const D3D_FEATURE_LEVEL requested_levels[] = {
        D3D_FEATURE_LEVEL_12_2,
        D3D_FEATURE_LEVEL_12_1,
        D3D_FEATURE_LEVEL_12_0,
    };
    levels.NumFeatureLevels = static_cast<UINT>(sizeof(requested_levels) / sizeof(requested_levels[0]));
    levels.pFeatureLevelsRequested = requested_levels;
    D3D_FEATURE_LEVEL feature_level = D3D_FEATURE_LEVEL_11_0;
    const bool has_feature_level = SUCCEEDED(device->CheckFeatureSupport(
            D3D12_FEATURE_FEATURE_LEVELS, &levels, sizeof(levels)));
    if (has_feature_level) {
        feature_level = levels.MaxSupportedFeatureLevel;
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS feature_options{};
    const bool has_options = SUCCEEDED(
        device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS, &feature_options, sizeof(feature_options)));

    D3D12_FEATURE_DATA_SHADER_MODEL shader_model{};
    bool has_shader_model = false;
    // Older inbox runtimes reject unknown requested enumerators with E_INVALIDARG.
    // Descend through known shader-model values; never report an unsupported
    // feature merely because a newer enum was not recognized.
    for (unsigned requested = 0x68; requested >= 0x60; --requested) {
        shader_model.HighestShaderModel = static_cast<D3D_SHADER_MODEL>(requested);
        const HRESULT query = device->CheckFeatureSupport(
            D3D12_FEATURE_SHADER_MODEL, &shader_model, sizeof(shader_model));
        if (SUCCEEDED(query)) {
            has_shader_model = true;
            break;
        }
        if (query != E_INVALIDARG) break;
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS5 options5{};
    const bool has_options5 = SUCCEEDED(
        device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &options5, sizeof(options5)));

    D3D12_FEATURE_DATA_D3D12_OPTIONS7 options7{};
    const bool has_options7 = SUCCEEDED(
        device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS7, &options7, sizeof(options7)));

    const LUID luid = description.AdapterLuid;
    const std::string adapter_name = Narrow(description.Description);
    const std::string driver = DriverVersionForAdapter(adapter.Get());
    const std::string os_build = WindowsBuild();
    const std::string shader_model_name =
        has_shader_model ? ShaderModelName(shader_model.HighestShaderModel)
                         : "D3D12_SHADER_MODEL_UNSUPPORTED";

    std::string payload;
    payload.reserve(2048);
    payload += "{\n";
    payload += "  \"format\": \"nw11-capability-snapshot\",\n";
    payload += "  \"version\": 1,\n";
    payload += "  \"status\": \"ok\",\n";
    payload += "  \"adapters\": [";
    for (size_t index = 0; index < adapter_records.size(); ++index) {
        if (index != 0) {
            payload += ", ";
        }
        payload += adapter_records[index];
    }
    payload += "],\n";
    payload += "  \"adapter\": {\n";
    payload += "    \"vendor_id\": " + JsonNumber(description.VendorId) + ",\n";
    payload += "    \"device_id\": " + JsonNumber(description.DeviceId) + ",\n";
    payload += "    \"subsystem_id\": " + JsonNumber(description.SubSysId) + ",\n";
    payload += "    \"revision\": " + JsonNumber(description.Revision) + ",\n";
    payload += "    \"adapter_name\": \"" + JsonEscape(adapter_name) + "\",\n";
    payload += "    \"adapter_luid\": \"" + LuidString(description.AdapterLuid) + "\",\n";
    payload += "    \"dedicated_video_memory_bytes\": " + std::to_string(description.DedicatedVideoMemory) + "\n";
    payload += "  },\n";
    payload += "  \"release_default_adapter_luid\": \"" + release_default_luid + "\",\n";
    payload += "  \"driver_version\": \"" + JsonEscape(driver) + "\",\n";
    payload += "  \"os_build\": \"" + JsonEscape(os_build) + "\",\n";
    payload += "  \"toolchain_identity\": \"nw11-capability-probe/1.0.0/d3d12\",\n";
    payload += "  \"runtime\": {\n";
    payload += "    \"native_architecture\": \"" + NativeArchitecture() + "\",\n";
    payload += "    \"display_mode\": \"" + DisplayMode() + "\",\n";
    payload += "    \"d3d12_dll_version\": \"" + JsonEscape(ModuleVersion(L"d3d12.dll")) + "\",\n";
    payload += "    \"dxgi_dll_version\": \"" + JsonEscape(ModuleVersion(L"dxgi.dll")) + "\",\n";
    payload += "    \"agility_sdk_d3d12core_present\": ";
    payload += (GetModuleHandleW(L"d3d12core.dll") != nullptr) ? "true" : "false";
    payload += "\n";
    payload += "  },\n";
    payload += "  \"features\": {\n";
    const auto queried = [](bool success, const char* value) -> std::string {
        return success ? "\"" + std::string(value) + "\"" : "null";
    };
    payload += "    \"d3d_feature_level\": " + queried(has_feature_level, FeatureLevelName(feature_level)) + ",\n";
    payload += "    \"resource_binding_tier\": " + queried(has_options, BindingTierName(feature_options.ResourceBindingTier)) + ",\n";
    payload += "    \"resource_heap_tier\": " + queried(has_options, HeapTierName(feature_options.ResourceHeapTier)) + ",\n";
    payload += "    \"highest_shader_model\": " + queried(has_shader_model, shader_model_name.c_str()) + ",\n";
    payload += "    \"highest_shader_model_mesh\": " + queried(has_shader_model, shader_model_name.c_str()) + ",\n";
    payload += "    \"highest_shader_model_ray\": " + queried(has_shader_model, shader_model_name.c_str()) + ",\n";
    payload += "    \"mesh_shader_tier\": " + queried(has_options7, MeshTierName(options7.MeshShaderTier)) + ",\n";
    payload += "    \"raytracing_tier\": " + (has_options5 ? JsonNumber(static_cast<unsigned>(options5.RaytracingTier)) : "null") + "\n";
    payload += "  },\n";
    struct FormatRequirement {
        const char* name;
        DXGI_FORMAT format;
    };
    const FormatRequirement formats[] = {
        {"DXGI_FORMAT_R8G8B8A8_UNORM", DXGI_FORMAT_R8G8B8A8_UNORM},
        {"DXGI_FORMAT_R32G32B32A32_FLOAT", DXGI_FORMAT_R32G32B32A32_FLOAT},
        {"DXGI_FORMAT_R32_UINT", DXGI_FORMAT_R32_UINT},
        {"DXGI_FORMAT_D32_FLOAT", DXGI_FORMAT_D32_FLOAT},
    };
    std::string format_queries = "{";
    for (const auto& requirement : formats) {
        D3D12_FEATURE_DATA_FORMAT_SUPPORT support{};
        support.Format = requirement.format;
        const HRESULT result = device->CheckFeatureSupport(D3D12_FEATURE_FORMAT_SUPPORT, &support, sizeof(support));
        UINT flags[2]{};
        static_assert(sizeof(support) == 3 * sizeof(UINT));
        std::memcpy(flags, reinterpret_cast<const unsigned char*>(&support) + sizeof(DXGI_FORMAT), sizeof(flags));
        if (format_queries.size() > 1) format_queries += ", ";
        format_queries += "\"" + std::string(requirement.name) + "\": {\"hresult\": " +
            std::to_string(result) + ", \"support1\": " + JsonNumber(flags[0]) +
            ", \"support2\": " + JsonNumber(flags[1]) + "}";
    }
    payload += "  \"format_queries\": " + format_queries + "},\n";
    payload += "  \"initialization\": {\"status\": \"ok\", \"stage\": \"device_and_capability_query\"}\n";
    payload += "}\n";
    WriteOutput(options.json_path, payload);
    return kExitOk;
}
