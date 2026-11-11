#include "package_player.h"

#include "diagnostics.h"
#include "package_loader.h"
#include "platform/win32_window.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <limits>
#include <set>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
#include <d3d12sdklayers.h>
#endif
#endif

namespace nw::player {
namespace {

constexpr std::uint32_t kMaxFramesInFlight = 3u;
#if defined(_WIN32)
constexpr DWORD kFenceTimeoutMs = 5000u;
constexpr std::uint64_t kFrameUploadAlignment = 64u * 1024u;
constexpr std::uint32_t kMaxShaderVisibleDescriptorHeapSize = 1000000u;
constexpr std::uint32_t kMaxShaderVisibleSamplerHeapSize = 2048u;
constexpr std::uint32_t kD3d12MaxRootCost = 64u;
constexpr std::uint32_t kD3d12MaxTexture2dDimension = 16384u;
#endif

std::string JsonEscape(const std::string& value) {
    std::string escaped;
    escaped.reserve(value.size() + 8u);
    for (char ch : value) {
        switch (ch) {
            case '"': escaped += "\\\""; break;
            case '\\': escaped += "\\\\"; break;
            case '\n': escaped += "\\n"; break;
            case '\r': escaped += "\\r"; break;
            case '\t': escaped += "\\t"; break;
            default:
                if (static_cast<unsigned char>(ch) < 0x20u) {
                    char buffer[8]{};
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x",
                                  static_cast<unsigned int>(
                                      static_cast<unsigned char>(ch)));
                    escaped += buffer;
                } else {
                    escaped += ch;
                }
                break;
        }
    }
    return escaped;
}

bool ResourceNeedsCbvSrvUavDescriptor(
    const package::Package& pkg,
    const package::ResourceDescriptor& resource) {
    if (resource.usage == "srv" || resource.usage == "uav") {
        return true;
    }
    if (resource.usage != "cbv") {
        return false;
    }
    for (const auto& instruction : pkg.frame) {
        if (instruction.opcode == "frame.set_root_constants" &&
            instruction.operands.size() == 3u &&
            instruction.operands[1].type == package::OperandType::ResourceRef &&
            instruction.operands[1].u32 == resource.resource_id) {
            return false;
        }
    }
    return true;
}

#if defined(_WIN32)
std::uint64_t RoundUp(std::uint64_t value, std::uint64_t alignment) {
    if (alignment == 0u) return value;
    const std::uint64_t remainder = value % alignment;
    return remainder == 0u ? value : value + (alignment - remainder);
}

std::size_t CountCbvSrvUavDescriptors(const package::Package& pkg) {
    std::size_t count = 0u;
    for (const auto& resource : pkg.resources) {
        if (ResourceNeedsCbvSrvUavDescriptor(pkg, resource)) {
            ++count;
        }
    }
    return count;
}

std::size_t CountSamplerDescriptors(const package::Package& pkg) {
    std::size_t count = 0u;
    for (const auto& resource : pkg.resources) {
        if (resource.usage == "sampler") {
            ++count;
        }
    }
    return count;
}

std::uint64_t MaximumInitialUploadBytes(const package::Package& pkg) {
    std::uint64_t maximum = 0u;
    for (const auto& instruction : pkg.initialization) {
        if (instruction.opcode != "init.upload_resource" ||
            instruction.operands.empty() ||
            instruction.operands[0].type !=
                package::OperandType::ResourceRef) {
            continue;
        }
        for (const auto& resource : pkg.resources) {
            if (resource.resource_id == instruction.operands[0].u32) {
                maximum = std::max(maximum, resource.size_bytes);
            }
        }
    }
    return RoundUp(maximum, kFrameUploadAlignment);
}
#endif

const package::ResourceDescriptor* FindPackageResource(
    const package::Package& pkg, std::uint32_t resource_id) noexcept {
    for (const auto& resource : pkg.resources) {
        if (resource.resource_id == resource_id) return &resource;
    }
    return nullptr;
}

// Platform-neutral validation of the stable `frame.barrier` state codes.  The
// numeric convention is shared with the NW-03 instruction contract; unknown
// codes are rejected instead of silently degenerating to COMMON.
bool IsKnownBarrierStateCode(std::uint32_t code) noexcept {
    return code <= 12u;
}

bool BarrierStateCodeCompatibleWithUsage(std::uint32_t code,
                                         const std::string& usage,
                                         std::string* reason) {
    const auto incompatible = [reason](const char* detail) {
        if (reason != nullptr) *reason = detail;
        return false;
    };
    if (usage == "readback") {
        return code == 9u
                   ? true
                   : incompatible(
                         "readback-heap resources may only remain COPY_DEST");
    }
    if (code == 11u) {
        return incompatible(
            "PRESENT is reserved for swap-chain back buffers, not package "
            "resources");
    }
    if (code == 4u && usage != "uav") {
        return incompatible(
            "UNORDERED_ACCESS requires a UAV-capable package resource");
    }
    if (code == 3u && usage != "rtv") {
        return incompatible(
            "RENDER_TARGET requires an RTV-capable package resource");
    }
    if ((code == 5u || code == 6u) && usage != "dsv") {
        return incompatible(
            "DEPTH_WRITE/DEPTH_READ require a DSV-capable package resource");
    }
    return true;
}

bool TryParameterTickLayout(const package::ParameterRecord& parameter,
                            std::uint8_t* tick_kind,
                            std::uint32_t* parameter_bytes) {
    if (tick_kind == nullptr || parameter_bytes == nullptr) return false;
    std::uint32_t components = 0u;
    if (parameter.type == "float") {
        *tick_kind = 0u;
        components = 1u;
    } else if (parameter.type == "float2") {
        *tick_kind = 0u;
        components = 2u;
    } else if (parameter.type == "float3") {
        *tick_kind = 0u;
        components = 3u;
    } else if (parameter.type == "float4") {
        *tick_kind = 0u;
        components = 4u;
    } else if (parameter.type == "uint") {
        *tick_kind = 1u;
        components = 1u;
    } else if (parameter.type == "int") {
        *tick_kind = 2u;
        components = 1u;
    } else {
        return false;
    }
    *parameter_bytes = components * 4u;
    // The canonical parameter value must match the declared type exactly; a
    // truncated or over-wide value would make a tick update silently partial.
    if (parameter.component_count != components ||
        parameter.value.size() != *parameter_bytes) {
        return false;
    }
    return true;
}

void WriteTimelineTick(std::uint32_t* words,
                       std::uint32_t byte_offset,
                       std::uint32_t parameter_bytes,
                       std::uint8_t tick_kind,
                       std::uint64_t tick) {
    auto* destination =
        reinterpret_cast<std::uint8_t*>(words) + byte_offset;
    if (parameter_bytes == 0u || parameter_bytes % 4u != 0u) return;
    const std::uint32_t component_count = parameter_bytes / 4u;
    for (std::uint32_t component = 0u; component < component_count;
         ++component) {
        std::uint8_t* component_destination =
            destination + static_cast<std::size_t>(component) * 4u;
        if (tick_kind == 0u) {
            const float value = static_cast<float>(tick);
            std::memcpy(component_destination, &value, sizeof(value));
        } else if (tick_kind == 1u) {
            const std::uint32_t value = static_cast<std::uint32_t>(tick);
            std::memcpy(component_destination, &value, sizeof(value));
        } else if (tick_kind == 2u) {
            const std::int32_t value = static_cast<std::int32_t>(tick);
            std::memcpy(component_destination, &value, sizeof(value));
        }
    }
}

#if defined(_WIN32)
// Metadata-only classification for evidence.  This value is never consulted
// by capability eligibility or package selection.
std::string ClassifyAdapterGenerationMetadata(
    const std::string& adapter_description) {
    std::string lower = adapter_description;
    std::transform(lower.begin(), lower.end(), lower.begin(),
                   [](unsigned char ch) {
                       return static_cast<char>(std::tolower(ch));
                   });
    if (lower.find("rtx 50") != std::string::npos ||
        lower.find("blackwell") != std::string::npos) {
        return "blackwell";
    }
    if (lower.find("rtx 40") != std::string::npos ||
        lower.find("ada") != std::string::npos) {
        return "ada";
    }
    return "metadata_only";
}
#endif

} // namespace

struct PlatformState {
    nw::platform::Win32Window window;
    bool swapchain_valid = false;

#if defined(_WIN32)
    struct ResourceSlot {
        std::uint32_t resource_id = 0u;
        ID3D12Resource* resource = nullptr;
        ID3D12Resource* upload = nullptr;
        D3D12_RESOURCE_STATES state = D3D12_RESOURCE_STATE_COMMON;
        D3D12_CPU_DESCRIPTOR_HANDLE cpu_handle{};
        D3D12_GPU_DESCRIPTOR_HANDLE gpu_handle{};
        std::uint32_t descriptor_index = 0xFFFFFFFFu;
        bool has_descriptor = false;
    };

    struct RootSlot {
        std::uint32_t root_signature_id = 0u;
        ID3D12RootSignature* root_signature = nullptr;
    };

    struct PipelineSlot {
        std::uint32_t shader_id = 0u;
        ID3D12PipelineState* pipeline_state = nullptr;
    };

    struct FrameSlot {
        ID3D12CommandAllocator* allocator = nullptr;
        ID3D12GraphicsCommandList* command_list = nullptr;
        ID3D12Resource* upload = nullptr;
        std::uint64_t upload_size = 0u;
        std::uint64_t fence_value = 0u;
    };

    IDXGIFactory4* factory = nullptr;
    IDXGIAdapter1* adapter = nullptr;
    ID3D12Device* device = nullptr;
    ID3D12CommandQueue* queue = nullptr;
    IDXGISwapChain3* swapchain = nullptr;

    ID3D12DescriptorHeap* rtv_heap = nullptr;
    std::vector<ID3D12Resource*> backbuffers;
    std::vector<D3D12_RESOURCE_STATES> backbuffer_states;
    std::vector<D3D12_CPU_DESCRIPTOR_HANDLE> rtv_handles;

    ID3D12DescriptorHeap* cbv_srv_uav_heap = nullptr;
    ID3D12DescriptorHeap* sampler_heap = nullptr;
    D3D12_CPU_DESCRIPTOR_HANDLE cbv_srv_uav_cpu_start{};
    D3D12_GPU_DESCRIPTOR_HANDLE cbv_srv_uav_gpu_start{};
    D3D12_CPU_DESCRIPTOR_HANDLE sampler_cpu_start{};
    D3D12_GPU_DESCRIPTOR_HANDLE sampler_gpu_start{};
    std::uint32_t cbv_srv_uav_increment = 0u;
    std::uint32_t sampler_increment = 0u;
    std::uint32_t rtv_increment = 0u;

    std::vector<ResourceSlot> resources;
    std::vector<RootSlot> root_signatures;
    std::vector<PipelineSlot> pipelines;
    std::vector<FrameSlot> frames;

    ID3D12Fence* fence = nullptr;
    HANDLE fence_event = nullptr;

#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    ID3D12InfoQueue* info_queue = nullptr;
#endif
    ID3D12QueryHeap* timestamp_heap = nullptr;
    ID3D12Resource* timestamp_readback = nullptr;
    std::uint64_t timestamp_frequency = 0u;
    std::uint64_t gpu_local_usage_bytes = 0u;
#endif
};

#if defined(_WIN32)
namespace {

ExitCode WaitForFenceValue(ID3D12Fence* fence,
                           std::uint64_t value,
                           HANDLE fence_event,
                           DWORD timeout_ms) noexcept {
    if (fence == nullptr || fence_event == nullptr) {
        return ExitCode::GpuSubmissionFailed;
    }
    const std::uint64_t completed = fence->GetCompletedValue();
    if (completed == UINT64_MAX) {
        return ExitCode::DeviceRemoved;
    }
    if (completed >= value) {
        return ExitCode::Success;
    }
    if (FAILED(fence->SetEventOnCompletion(value, fence_event))) {
        return ExitCode::GpuSubmissionFailed;
    }
    const DWORD wait_result = WaitForSingleObject(fence_event, timeout_ms);
    if (wait_result == WAIT_TIMEOUT) {
        return ExitCode::ExecutionFenceTimeout;
    }
    if (wait_result != WAIT_OBJECT_0) {
        return ExitCode::GpuSubmissionFailed;
    }
    return ExitCode::Success;
}

#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
bool HasValidationError(ID3D12InfoQueue* info_queue) {
    if (info_queue == nullptr) return false;
    bool has_error = false;
    const UINT64 stored = info_queue->GetNumStoredMessages();
    for (UINT64 index = 0u; index < stored; ++index) {
        SIZE_T message_bytes = 0u;
        if (FAILED(info_queue->GetMessage(index, nullptr, &message_bytes)) ||
            message_bytes == 0u) {
            continue;
        }
        if (message_bytes > 16384u) {
            has_error = true;
            continue;
        }
        alignas(D3D12_MESSAGE) char buffer[16384]{};
        auto* message = reinterpret_cast<D3D12_MESSAGE*>(buffer);
        SIZE_T buffer_bytes = sizeof(buffer);
        if (FAILED(info_queue->GetMessage(index, message, &buffer_bytes))) {
            continue;
        }
        const bool error =
            message->Severity == D3D12_MESSAGE_SEVERITY_ERROR ||
            message->Severity == D3D12_MESSAGE_SEVERITY_CORRUPTION;
        if (error) {
            has_error = true;
            std::fprintf(
                stderr,
                "NW_D3D12_VALIDATION_ERROR=severity=%d id=%d description=%s\n",
                static_cast<int>(message->Severity),
                static_cast<int>(message->ID),
                message->pDescription != nullptr ? message->pDescription : "");
        }
    }
    return has_error;
}
#endif

DXGI_FORMAT MapFormat(const std::string& format) {
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

std::uint32_t BytesPerElement(const std::string& format) {
    if (format == "DXGI_FORMAT_R8G8B8A8_UNORM") return 4u;
    if (format == "DXGI_FORMAT_R32G32B32A32_FLOAT") return 16u;
    if (format == "DXGI_FORMAT_R32_UINT") return 4u;
    if (format == "DXGI_FORMAT_D32_FLOAT") return 4u;
    return 1u;
}

D3D12_RESOURCE_DESC MakeResourceDesc(
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
        desc.Format = MapFormat(resource.format);
        desc.SampleDesc.Count = 1u;
        desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    } else {
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D;
        desc.Width = resource.width;
        desc.Height = resource.height;
        desc.DepthOrArraySize = static_cast<UINT16>(resource.depth);
        desc.MipLevels = static_cast<UINT16>(resource.mip_levels);
        desc.Format = MapFormat(resource.format);
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

ResourceAllocationQuery QueryAdapterAllocationInfo(
    const package::ResourceDescriptor& resource, void* user_data) {
    ResourceAllocationQuery result{};
    auto* state = static_cast<PlatformState*>(user_data);
    if (state == nullptr || state->device == nullptr) {
        result.detail = "device unavailable";
        return result;
    }
    const D3D12_RESOURCE_DESC desc = MakeResourceDesc(resource);
    const D3D12_RESOURCE_ALLOCATION_INFO info =
        state->device->GetResourceAllocationInfo(0u, 1u, &desc);
    if (info.SizeInBytes == 0u || info.Alignment == 0u ||
        info.SizeInBytes == UINT64_MAX) {
        result.detail = "GetResourceAllocationInfo returned an invalid size";
        return result;
    }
    result.valid = true;
    result.size_bytes = info.SizeInBytes;
    result.alignment = info.Alignment;
    result.detail = "GetResourceAllocationInfo";
    return result;
}

D3D12_HEAP_TYPE HeapTypeForUsage(const package::ResourceDescriptor& resource) {
    if (resource.usage == "readback") {
        return D3D12_HEAP_TYPE_READBACK;
    }
    if (resource.usage == "sampler") {
        return D3D12_HEAP_TYPE_DEFAULT;
    }
    return D3D12_HEAP_TYPE_DEFAULT;
}

D3D12_RESOURCE_STATES InitialStateForUsage(
    const package::ResourceDescriptor& resource) {
    if (resource.usage == "readback") {
        return D3D12_RESOURCE_STATE_COPY_DEST;
    }
    return D3D12_RESOURCE_STATE_COMMON;
}

D3D12_RESOURCE_STATES WorkingStateForUsage(
    const package::ResourceDescriptor& resource) {
    if (resource.usage == "uav") {
        return D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    }
    if (resource.usage == "cbv") {
        return D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER;
    }
    if (resource.usage == "srv") {
        return D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    }
    if (resource.usage == "rtv") return D3D12_RESOURCE_STATE_RENDER_TARGET;
    if (resource.usage == "dsv") return D3D12_RESOURCE_STATE_DEPTH_WRITE;
    if (resource.usage == "readback") return D3D12_RESOURCE_STATE_COPY_DEST;
    return D3D12_RESOURCE_STATE_COMMON;
}

bool TryBarrierStateFromCode(std::uint32_t code,
                             D3D12_RESOURCE_STATES* state) noexcept {
    // Stable player convention for `frame.barrier` operand 0.  The NW-03
    // instruction contract records a u32; this mapping keeps it platform-neutral.
    if (state == nullptr) return false;
    switch (code) {
        case 0u: *state = D3D12_RESOURCE_STATE_COMMON; return true;
        case 1u:
            *state = D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER;
            return true;
        case 2u: *state = D3D12_RESOURCE_STATE_INDEX_BUFFER; return true;
        case 3u: *state = D3D12_RESOURCE_STATE_RENDER_TARGET; return true;
        case 4u: *state = D3D12_RESOURCE_STATE_UNORDERED_ACCESS; return true;
        case 5u: *state = D3D12_RESOURCE_STATE_DEPTH_WRITE; return true;
        case 6u: *state = D3D12_RESOURCE_STATE_DEPTH_READ; return true;
        case 7u:
            *state = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
            return true;
        case 8u:
            *state = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
            return true;
        case 9u: *state = D3D12_RESOURCE_STATE_COPY_DEST; return true;
        case 10u: *state = D3D12_RESOURCE_STATE_COPY_SOURCE; return true;
        case 11u: *state = D3D12_RESOURCE_STATE_PRESENT; return true;
        case 12u: *state = D3D12_RESOURCE_STATE_GENERIC_READ; return true;
        default: return false;
    }
}

std::uint32_t QueryWindowsBuild() noexcept {
    struct RtlOsVersionInfoW {
        ULONG size;
        ULONG major;
        ULONG minor;
        ULONG build;
        ULONG platform;
        WCHAR csd[128];
    };
    using RtlGetVersionFn = LONG(WINAPI*)(RtlOsVersionInfoW*);
    HMODULE ntdll = GetModuleHandleA("ntdll.dll");
    if (ntdll != nullptr) {
        auto rtl_get_version = reinterpret_cast<RtlGetVersionFn>(
            reinterpret_cast<void*>(GetProcAddress(ntdll, "RtlGetVersion")));
        if (rtl_get_version != nullptr) {
            RtlOsVersionInfoW version{};
            version.size = sizeof(version);
            if (rtl_get_version(&version) == 0 && version.major >= 10u) {
                return static_cast<std::uint32_t>(version.build);
            }
        }
    }
    return 0u;
}

std::string QueryAdapterDriverVersion(IDXGIAdapter1* adapter) {
    if (adapter == nullptr) return {};
    LARGE_INTEGER umd_version{};
    if (FAILED(adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice),
                                               &umd_version))) {
        return {};
    }
    const std::uint64_t value =
        static_cast<std::uint64_t>(umd_version.QuadPart);
    const unsigned int major =
        static_cast<unsigned int>((value >> 48u) & 0xFFFFu);
    const unsigned int minor =
        static_cast<unsigned int>((value >> 32u) & 0xFFFFu);
    const unsigned int build =
        static_cast<unsigned int>((value >> 16u) & 0xFFFFu);
    const unsigned int revision = static_cast<unsigned int>(value & 0xFFFFu);
    char text[64]{};
    std::snprintf(text, sizeof(text), "%u.%u.%u.%u", major, minor, build,
                  revision);
    return text;
}

std::string LowerAscii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char ch) {
                       return static_cast<char>(std::tolower(ch));
                   });
    return value;
}

std::string QueryDisplayRegistryText(const std::string& adapter_description,
                                     const char* value_name) {
    static constexpr const char* kDisplayClass =
        "SYSTEM\\CurrentControlSet\\Control\\Class\\"
        "{4d36e968-e325-11ce-bfc1-08002be10318}";
    HKEY class_key = nullptr;
    if (RegOpenKeyExA(HKEY_LOCAL_MACHINE, kDisplayClass, 0u, KEY_READ,
                      &class_key) != ERROR_SUCCESS) {
        return {};
    }
    const std::string wanted = LowerAscii(adapter_description);
    std::string result;
    for (DWORD index = 0u;; ++index) {
        char sub_name[256]{};
        DWORD sub_name_size = static_cast<DWORD>(sizeof(sub_name));
        const LONG enum_result =
            RegEnumKeyExA(class_key, index, sub_name, &sub_name_size, nullptr,
                          nullptr, nullptr, nullptr);
        if (enum_result == ERROR_NO_MORE_ITEMS) break;
        if (enum_result != ERROR_SUCCESS) continue;
        bool numeric = sub_name[0] != '\0';
        for (const char* ch = sub_name; *ch != '\0'; ++ch) {
            if (std::isdigit(static_cast<unsigned char>(*ch)) == 0) {
                numeric = false;
                break;
            }
        }
        if (!numeric) continue;
        HKEY device_key = nullptr;
        if (RegOpenKeyExA(class_key, sub_name, 0u, KEY_READ, &device_key) !=
            ERROR_SUCCESS) {
            continue;
        }
        char driver_desc[512]{};
        DWORD driver_desc_size = static_cast<DWORD>(sizeof(driver_desc));
        DWORD driver_desc_type = 0u;
        const bool has_desc =
            RegQueryValueExA(device_key, "DriverDesc", nullptr,
                             &driver_desc_type,
                             reinterpret_cast<LPBYTE>(driver_desc),
                             &driver_desc_size) == ERROR_SUCCESS;
        const std::string actual =
            has_desc ? LowerAscii(driver_desc) : std::string();
        const bool matches =
            wanted.empty() ||
            (!actual.empty() &&
             (actual.find(wanted) != std::string::npos ||
              wanted.find(actual) != std::string::npos));
        if (matches) {
            BYTE value[512]{};
            DWORD value_size = static_cast<DWORD>(sizeof(value));
            DWORD value_type = 0u;
            if (RegQueryValueExA(device_key, value_name, nullptr, &value_type,
                                 value, &value_size) == ERROR_SUCCESS) {
                if (value_type == REG_SZ) {
                    value[sizeof(value) - 1u] = '\0';
                    result.assign(reinterpret_cast<const char*>(value));
                } else if (value_type == REG_DWORD &&
                           value_size >= sizeof(DWORD)) {
                    DWORD number = 0u;
                    std::memcpy(&number, value, sizeof(number));
                    char text[32]{};
                    std::snprintf(text, sizeof(text), "0x%08lX", number);
                    result = text;
                }
            }
        }
        RegCloseKey(device_key);
        if (!result.empty()) break;
    }
    RegCloseKey(class_key);
    return result;
}

} // namespace
#endif // _WIN32

PackagePlayer::PackagePlayer() : PackagePlayer(PlayerConfig{}) {}

PackagePlayer::PackagePlayer(const PlayerConfig& config) : config_(config) {
    frames_in_flight_ = std::clamp(config.frames_in_flight, 1u, kMaxFramesInFlight);
    width_ = config.width;
    height_ = config.height;
}

PackagePlayer::~PackagePlayer() { shutdown(); }

ExitCode PackagePlayer::Fail(ExitCode code,
                             const char* category,
                             const char* diagnostic_code,
                             const char* requirement,
                             const std::string& required_value,
                             const std::string& observed_value,
                             const char* stage,
                             const std::string& message) {
    PlayerDiagnostic diagnostic{};
    (void)category;
    diagnostic.category = ExitCodeCategory(code);
    diagnostic.code = diagnostic_code == nullptr ? "player_failure"
                                                 : diagnostic_code;
    diagnostic.requirement = requirement == nullptr ? "" : requirement;
    diagnostic.required_value = required_value;
    diagnostic.observed_value = observed_value;
    diagnostic.profile_id = package_.capability.profile_id;
    diagnostic.requirements_digest = package_.capability.requirements_digest;
    diagnostic.stage = stage == nullptr ? "player" : stage;
    diagnostic.message = message;
    diagnostics_.push_back(std::move(diagnostic));
    last_exit_ = code;
    return code;
}

void PackagePlayer::AddFailureDiagnostics(
    const std::vector<CapabilityRequirementFailure>& failures,
    const char* category,
    const char* stage) {
    for (const auto& failure : failures) {
        PlayerDiagnostic diagnostic{};
        diagnostic.category = category == nullptr ? "package_validation"
                                                  : category;
        diagnostic.code = failure.code;
        diagnostic.requirement = failure.requirement;
        diagnostic.required_value = failure.required_value;
        diagnostic.observed_value = failure.observed_value;
        diagnostic.profile_id = failure.profile_id.empty()
                                    ? package_.capability.profile_id
                                    : failure.profile_id;
        diagnostic.requirements_digest =
            failure.requirements_digest.empty()
                ? package_.capability.requirements_digest
                : failure.requirements_digest;
        diagnostic.stage = stage == nullptr ? "package" : stage;
        diagnostic.message = failure.detail;
        diagnostics_.push_back(std::move(diagnostic));
    }
}

ExitCode PackagePlayer::FailEvaluation(
    const PackageCapabilityEvaluation& evaluation,
    const char* stage) {
    const bool capability_only =
        evaluation.exit_code == ExitCode::CapabilityBaselineUnmet;
    AddFailureDiagnostics(evaluation.failures,
                          capability_only
                              ? ExitCodeCategory(evaluation.exit_code)
                              : "package_validation",
                          stage);
    last_exit_ = evaluation.exit_code;
    return evaluation.exit_code;
}

ExitCode PackagePlayer::FailPlan(const ResourcePlanResult& plan_result) {
    AddFailureDiagnostics(plan_result.failures,
                          ExitCodeCategory(plan_result.exit_code),
                          "resource_realization");
    last_exit_ = plan_result.exit_code;
    return plan_result.exit_code;
}

void PackagePlayer::NoteEngineAllocation(std::uint64_t bytes,
                                         std::uint64_t bytes_upload,
                                         bool descriptor,
                                         bool heap,
                                         bool explicit_path) {
    ++stats_.allocation_calls;
    if (descriptor) {
        ++stats_.descriptor_allocation_calls;
    }
    if (initialized_ && !explicit_path && !resize_in_progress_ &&
        !shutting_down_) {
        stats_.post_initialization_allocation_violation = true;
    }
    stats_.engine_resource_bytes += bytes;
    stats_.engine_upload_bytes += bytes_upload;
    if (descriptor) ++stats_.engine_descriptor_count;
    if (heap) ++stats_.engine_heap_count;
}

bool PackagePlayer::BuildFrameInstructionPlan() {
    frame_ops_.clear();
    last_frame_order_.clear();
    frame_ops_.reserve(package_.frame.size());
    last_frame_order_.reserve(package_.frame.size());
    // A timeline selection is only effective if a later root-constant
    // instruction can consume it. Track unresolved selections in frame order.
    std::set<std::uint32_t> pending_timeline_parameters;
    for (const auto& instruction : package_.frame) {
        bool known = true;
        FrameOp::Type type = FrameOp::Type::Present;
        if (instruction.opcode == "frame.set_root_signature") {
            type = FrameOp::Type::SetRootSignature;
        } else if (instruction.opcode == "frame.set_root_constants") {
            type = FrameOp::Type::SetRootConstants;
        } else if (instruction.opcode == "frame.set_descriptor_table") {
            type = FrameOp::Type::SetDescriptorTable;
        } else if (instruction.opcode == "frame.barrier") {
            type = FrameOp::Type::Barrier;
        } else if (instruction.opcode == "frame.dispatch") {
            type = FrameOp::Type::Dispatch;
        } else if (instruction.opcode == "frame.draw") {
            type = FrameOp::Type::Draw;
        } else if (instruction.opcode == "frame.copy_buffer") {
            type = FrameOp::Type::CopyBuffer;
        } else if (instruction.opcode == "frame.set_timeline") {
            type = FrameOp::Type::SetTimeline;
        } else if (instruction.opcode == "frame.present") {
            type = FrameOp::Type::Present;
        } else {
            known = false;
        }
        if (!known) {
            Fail(ExitCode::PackageValidationFailed, "package_validation",
                 "package_instruction_unsupported_by_player", "opcode",
                 "documented frame opcode", instruction.opcode, "frame_plan",
                 "frame instruction is not supported by the minimal player");
            return false;
        }
        FrameOp op{};
        op.type = type;
        op.opcode = instruction.opcode;
        if (type == FrameOp::Type::SetRootSignature) {
            if (instruction.operands.size() != 1u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "1",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.set_root_signature operand count is invalid");
                return false;
            }
            op.root_parameter = instruction.operands[0].u32;
        } else if (type == FrameOp::Type::SetRootConstants) {
            if (instruction.operands.size() != 3u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "3",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.set_root_constants operand count is invalid");
                return false;
            }
            op.root_parameter = instruction.operands[0].u32;
            op.resource_a = instruction.operands[1].u32;
            op.constant_count =
                std::min(instruction.operands[2].u32,
                         package::kMaxRootSignatureDwordCost);
            // Pack immutable parameter values into the bounded root-constant
            // block.  This is done once during initialization, never per frame.
            std::size_t byte_offset = 0u;
            for (const auto& parameter : package_.parameters) {
                std::uint8_t tick_kind = 0u;
                std::uint32_t declared_bytes = 0u;
                if (!TryParameterTickLayout(parameter, &tick_kind,
                                            &declared_bytes)) {
                    Fail(ExitCode::PackageValidationFailed,
                         "package_validation",
                         "package_parameter_type_unsupported", "parameter_type",
                         "float|float2|float3|float4|uint|int",
                         parameter.type.empty() ? "unavailable"
                                                : parameter.type,
                         "frame_plan",
                         "parameter type/width is not representable by the "
                         "bounded root-constant timeline update");
                    return false;
                }
                const std::size_t parameter_bytes =
                    std::min<std::size_t>(
                        parameter.value.size(),
                        static_cast<std::size_t>(op.constant_count) * 4u -
                            byte_offset);
                if (parameter_bytes == 0u) break;
                if (parameter.name == "time" && parameter_bytes >= 4u &&
                    op.time_byte_offset < 0) {
                    op.time_byte_offset =
                        static_cast<std::int32_t>(byte_offset);
                }
                const bool full_parameter_fits =
                    parameter_bytes == declared_bytes;
                if (full_parameter_fits &&
                    op.parameter_count <
                        package::kMaxRootSignatureDwordCost) {
                    const std::uint32_t entry = op.parameter_count++;
                    op.parameter_ids[entry] = parameter.parameter_id;
                    op.parameter_offsets[entry] =
                        static_cast<std::int32_t>(byte_offset);
                    op.parameter_tick_kinds[entry] = tick_kind;
                    op.parameter_sizes[entry] = declared_bytes;
                    pending_timeline_parameters.erase(
                        parameter.parameter_id);
                }
                std::memcpy(
                    reinterpret_cast<std::uint8_t*>(op.constants.data()) +
                        byte_offset,
                    parameter.value.data(), parameter_bytes);
                byte_offset += parameter_bytes;
                if (byte_offset >=
                    static_cast<std::size_t>(op.constant_count) * 4u) {
                    break;
                }
            }
        } else if (type == FrameOp::Type::SetDescriptorTable) {
            if (instruction.operands.size() != 3u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "3",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.set_descriptor_table operand count is invalid");
                return false;
            }
            op.root_parameter = instruction.operands[0].u32;
            op.heap_name = instruction.operands[1].string_value;
            op.resource_a = instruction.operands[2].u32;
            const package::ResourceDescriptor* resource =
                FindPackageResource(package_, op.resource_a);
            if (resource == nullptr) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_reference",
                     "descriptor_table_resource",
                     std::to_string(op.resource_a), "missing", "frame_plan",
                     "frame.set_descriptor_table references a missing resource");
                return false;
            }
            const bool sampler_table = op.heap_name == "sampler";
            if (!sampler_table && op.heap_name != "cbv_srv_uav") {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_descriptor_heap", "heap_name",
                     "cbv_srv_uav|sampler",
                     op.heap_name.empty() ? "unavailable" : op.heap_name,
                     "frame_plan",
                     "frame.set_descriptor_table names an unsupported heap");
                return false;
            }
            const bool descriptor_available =
                sampler_table
                    ? resource->usage == "sampler"
                    : ResourceNeedsCbvSrvUavDescriptor(package_, *resource);
            if (!descriptor_available) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_descriptor_target",
                     "descriptor_table_resource", resource->name,
                     "no descriptor in " + op.heap_name, "frame_plan",
                     "frame.set_descriptor_table targets a resource without a "
                     "compatible descriptor");
                return false;
            }
        } else if (type == FrameOp::Type::Barrier) {
            if (instruction.operands.size() != 2u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "2",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.barrier operand count is invalid");
                return false;
            }
            op.barrier_state = instruction.operands[0].u32;
            op.resource_a = instruction.operands[1].u32;
            if (!IsKnownBarrierStateCode(op.barrier_state)) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_barrier_state", "barrier_state",
                     "0..12", std::to_string(op.barrier_state), "frame_plan",
                     "frame.barrier uses an unknown resource-state code");
                return false;
            }
            const package::ResourceDescriptor* resource =
                FindPackageResource(package_, op.resource_a);
            if (resource == nullptr) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_reference", "barrier_resource",
                     std::to_string(op.resource_a), "missing", "frame_plan",
                     "frame.barrier references a missing resource");
                return false;
            }
            std::string compatibility_detail;
            if (!BarrierStateCodeCompatibleWithUsage(
                    op.barrier_state, resource->usage,
                    &compatibility_detail)) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_barrier_usage", "resource_usage",
                     resource->usage, compatibility_detail, "frame_plan",
                     "frame.barrier state is incompatible with the target "
                     "resource heap/usage");
                return false;
            }
        } else if (type == FrameOp::Type::Dispatch) {
            if (instruction.operands.size() != 4u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "4",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.dispatch operand count is invalid");
                return false;
            }
            op.group_x = instruction.operands[0].u32;
            op.group_y = instruction.operands[1].u32;
            op.group_z = instruction.operands[2].u32;
            op.shader_id = instruction.operands[3].u32;
        } else if (type == FrameOp::Type::Draw) {
            Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                 "frame_draw_requires_raster_pipeline", "pipeline",
                 "no package raster pipeline", "frame.draw", "frame_plan",
                 "the minimal NW-04 player only realizes package compute "
                 "pipelines; frame.draw requires a later raster task");
            return false;
        } else if (type == FrameOp::Type::CopyBuffer) {
            if (instruction.operands.size() != 3u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "3",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.copy_buffer operand count is invalid");
                return false;
            }
            op.resource_a = instruction.operands[0].u32;
            op.resource_b = instruction.operands[1].u32;
            op.byte_count = instruction.operands[2].u64;
        } else if (type == FrameOp::Type::SetTimeline) {
            if (instruction.operands.size() != 2u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "2",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.set_timeline operand count is invalid");
                return false;
            }
            op.parameter_id = instruction.operands[0].u32;
            op.tick_value = instruction.operands[1].u64;
            bool parameter_known = false;
            for (const auto& parameter : package_.parameters) {
                if (parameter.parameter_id == op.parameter_id) {
                    parameter_known = true;
                    break;
                }
            }
            if (!parameter_known) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_reference", "parameter_id",
                     std::to_string(op.parameter_id), "missing", "frame_plan",
                     "frame.set_timeline references a missing parameter");
                return false;
            }
            pending_timeline_parameters.insert(op.parameter_id);
        } else if (type == FrameOp::Type::Present) {
            if (instruction.operands.size() != 1u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand_count", "operand_count", "1",
                     std::to_string(instruction.operands.size()), "frame_plan",
                     "frame.present operand count is invalid");
                return false;
            }
            op.resource_a = instruction.operands[0].u32;
        }
        frame_ops_.push_back(std::move(op));
        last_frame_order_.push_back(instruction.opcode);
    }
    if (!pending_timeline_parameters.empty()) {
        const std::uint32_t parameter_id =
            *pending_timeline_parameters.begin();
        Fail(ExitCode::PackageValidationFailed, "package_validation",
             "frame_timeline_parameter_not_bound", "parameter_id",
             std::to_string(parameter_id),
             "not present in a subsequent full root-constant block",
             "frame_plan",
             "frame.set_timeline selects a parameter that no later bounded "
             "root-constant upload can update");
        return false;
    }
    return true;
}

ExitCode PackagePlayer::OpenDeviceAndWindow() {
    platform_ = std::unique_ptr<PlatformState>(new PlatformState());
    if (platform_ == nullptr) {
        return Fail(ExitCode::WindowCreationFailed, "platform",
                    "platform_state_allocation_failed", "state", "allocated",
                    "unavailable", "platform_init",
                    "could not allocate platform state");
    }
    width_ = config_.width;
    height_ = config_.height;
    if (config_.create_window && !config_.headless) {
        nw::platform::WindowConfig window_config{};
        window_config.title = config_.window_title;
        window_config.width = config_.width;
        window_config.height = config_.height;
        window_config.visible = config_.visible_window;
        if (!platform_->window.Create(window_config)) {
            return Fail(ExitCode::WindowCreationFailed, "platform",
                        "window_creation_failed", "window", "created",
                        "CreateWindowEx failed", "window",
                        "Win32 window creation failed");
        }
        platform_->window.GetClientSize(&width_, &height_);
    }

#if defined(_WIN32)
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    if (config_.enable_validation) {
        ID3D12Debug* debug = nullptr;
        if (FAILED(D3D12GetDebugInterface(IID_PPV_ARGS(&debug))) ||
            debug == nullptr) {
            return Fail(ExitCode::DebugLayerUnavailable, "device",
                        "debug_layer_unavailable", "d3d12sdklayers",
                        "available", "unavailable", "validation",
                        "D3D12 debug layer was requested but is unavailable");
        }
        debug->EnableDebugLayer();
        debug->Release();
    }
#else
    if (config_.enable_validation) {
        return Fail(ExitCode::DebugLayerUnavailable, "device",
                    "diagnostic_tooling_excluded_from_release",
                    "validation build", "diagnostic profile", "release profile",
                    "validation",
                    "release linkage excludes D3D12 diagnostic tooling");
    }
#endif

    if (FAILED(CreateDXGIFactory2(0u, IID_PPV_ARGS(&platform_->factory))) ||
        platform_->factory == nullptr) {
        return Fail(ExitCode::DxgiFactoryCreationFailed, "device",
                    "dxgi_factory_failed", "factory", "created", "failed",
                    "dxgi", "CreateDXGIFactory2 failed");
    }

    ID3D12Device* device = nullptr;
    for (UINT index = 0u;; ++index) {
        IDXGIAdapter1* candidate = nullptr;
        const HRESULT enum_hr =
            platform_->factory->EnumAdapters1(index, &candidate);
        if (enum_hr == DXGI_ERROR_NOT_FOUND) {
            break;
        }
        if (FAILED(enum_hr) || candidate == nullptr) {
            if (candidate != nullptr) candidate->Release();
            return Fail(ExitCode::AdapterEnumerationFailed, "device",
                        "adapter_enumeration_failed", "adapter", "enumerated",
                        "failed", "adapter",
                        "DXGI adapter enumeration failed");
        }
        DXGI_ADAPTER_DESC1 adapter_desc{};
        if (FAILED(candidate->GetDesc1(&adapter_desc))) {
            candidate->Release();
            continue;
        }
        if ((adapter_desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0u) {
            candidate->Release();
            continue;
        }
        ID3D12Device* created = nullptr;
        const HRESULT create_hr = D3D12CreateDevice(
            candidate, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&created));
        if (FAILED(create_hr) || created == nullptr) {
            if (created != nullptr) created->Release();
            candidate->Release();
            continue;
        }
        device = created;
        platform_->adapter = candidate;
        capabilities_.adapter_description.clear();
        if (adapter_desc.Description[0] != L'\0') {
            char narrow[256]{};
            const int converted = WideCharToMultiByte(
                CP_UTF8, 0u, adapter_desc.Description, -1, narrow,
                static_cast<int>(sizeof(narrow)), nullptr, nullptr);
            if (converted > 0) {
                capabilities_.adapter_description.assign(
                    narrow, static_cast<std::size_t>(converted - 1));
            }
        }
        capabilities_.vendor_id = adapter_desc.VendorId;
        capabilities_.device_id = adapter_desc.DeviceId;
        capabilities_.dedicated_video_memory_bytes =
            adapter_desc.DedicatedVideoMemory;
        break;
    }
    if (device == nullptr) {
        return Fail(ExitCode::AdapterEnumerationFailed, "device",
                    "no_d3d12_adapter", "adapter", "D3D_FEATURE_LEVEL_12_0",
                    "none", "adapter",
                    "no hardware adapter supports D3D_FEATURE_LEVEL_12_0");
    }
    platform_->device = device;
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    if (config_.enable_validation) {
        if (FAILED(device->QueryInterface(
                __uuidof(ID3D12InfoQueue),
                reinterpret_cast<void**>(&platform_->info_queue))) ||
            platform_->info_queue == nullptr) {
            return Fail(ExitCode::DebugLayerUnavailable, "device",
                        "info_queue_unavailable", "ID3D12InfoQueue",
                        "available", "unavailable", "validation",
                        "D3D12 debug layer enabled without an info queue");
        }
    }
#endif

    QueriedAdapterCapabilities queried{};
    queried.queried = true;
    queried.windows_build_number = QueryWindowsBuild();
    queried.windows_build_queried = queried.windows_build_number != 0u;
    queried.adapter_description = capabilities_.adapter_description;
    queried.adapter_generation =
        ClassifyAdapterGenerationMetadata(queried.adapter_description);
    queried.driver_version =
        QueryAdapterDriverVersion(platform_->adapter);
    if (queried.driver_version.empty()) {
        queried.driver_version = QueryDisplayRegistryText(
            queried.adapter_description, "DriverVersion");
    }
    queried.wddm_version_text = QueryDisplayRegistryText(
        queried.adapter_description, "WddmVersion");
    queried.vendor_id = capabilities_.vendor_id;
    queried.device_id = capabilities_.device_id;
    queried.dedicated_video_memory_bytes =
        capabilities_.dedicated_video_memory_bytes;

    static constexpr D3D_FEATURE_LEVEL kRequestedLevels[] = {
        D3D_FEATURE_LEVEL_12_2, D3D_FEATURE_LEVEL_12_1,
        D3D_FEATURE_LEVEL_12_0, D3D_FEATURE_LEVEL_11_1,
        D3D_FEATURE_LEVEL_11_0,
    };
    D3D12_FEATURE_DATA_FEATURE_LEVELS feature_levels{};
    feature_levels.NumFeatureLevels =
        static_cast<UINT>(sizeof(kRequestedLevels) / sizeof(kRequestedLevels[0]));
    feature_levels.pFeatureLevelsRequested = kRequestedLevels;
    if (SUCCEEDED(device->CheckFeatureSupport(
            D3D12_FEATURE_FEATURE_LEVELS, &feature_levels,
            sizeof(feature_levels)))) {
        queried.feature_level_queried = true;
        queried.max_feature_level =
            static_cast<std::uint32_t>(feature_levels.MaxSupportedFeatureLevel);
    }

    for (unsigned int shader_model = 0x68u; shader_model >= 0x60u;
         --shader_model) {
        D3D12_FEATURE_DATA_SHADER_MODEL candidate{
            static_cast<D3D_SHADER_MODEL>(shader_model)};
        if (SUCCEEDED(device->CheckFeatureSupport(
                D3D12_FEATURE_SHADER_MODEL, &candidate,
                sizeof(candidate))) &&
            static_cast<unsigned int>(candidate.HighestShaderModel) >=
                shader_model) {
            queried.shader_model_queried = true;
            queried.max_shader_model = shader_model;
            break;
        }
        if (shader_model == 0x60u) break;
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS options{};
    std::uint32_t max_gpu_va_bits = 0u;
    if (SUCCEEDED(device->CheckFeatureSupport(
            D3D12_FEATURE_D3D12_OPTIONS, &options, sizeof(options)))) {
        queried.binding_tier_queried = true;
        queried.heap_tier_queried = true;
        queried.resource_binding_tier =
            static_cast<std::uint32_t>(options.ResourceBindingTier);
        queried.resource_heap_tier =
            static_cast<std::uint32_t>(options.ResourceHeapTier);
        queried.typed_uav_additional_formats_supported =
            options.TypedUAVLoadAdditionalFormats != FALSE;
        max_gpu_va_bits =
            static_cast<std::uint32_t>(options.MaxGPUVirtualAddressBitsPerResource);
    }

    D3D12_FEATURE_DATA_D3D12_OPTIONS1 options1{};
    if (SUCCEEDED(device->CheckFeatureSupport(
            D3D12_FEATURE_D3D12_OPTIONS1, &options1, sizeof(options1)))) {
        queried.optional_features_queried = true;
        queried.wave_ops_queried = true;
        queried.int64_shader_ops_queried = true;
        queried.wave_ops_supported = options1.WaveOps != FALSE;
        queried.int64_shader_ops_supported = options1.Int64ShaderOps != FALSE;
    }
    D3D12_FEATURE_DATA_D3D12_OPTIONS4 options4{};
    if (SUCCEEDED(device->CheckFeatureSupport(
            D3D12_FEATURE_D3D12_OPTIONS4, &options4, sizeof(options4)))) {
        queried.optional_features_queried = true;
        queried.fp16_shader_ops_queried = true;
        queried.fp16_shader_ops_supported =
            options4.Native16BitShaderOpsSupported != FALSE;
    }

    if (max_gpu_va_bits != 0u) {
        const std::uint32_t bits = max_gpu_va_bits;
        if (bits >= 63u) {
            queried.max_buffer_bytes =
                std::numeric_limits<std::uint64_t>::max();
        } else if (bits != 0u) {
            queried.max_buffer_bytes = (1ull << bits) - 1ull;
        }
    }
    queried.limits_queried =
        queried.max_buffer_bytes != 0u && queried.feature_level_queried;
    queried.direct_queue_count = 1u;
    queried.max_cbv_srv_uav_descriptors_per_heap =
        queried.resource_binding_tier >= package::kResourceBindingTier2
            ? kMaxShaderVisibleDescriptorHeapSize
            : kMaxShaderVisibleDescriptorHeapSize / 2u;
    queried.max_sampler_descriptors_per_heap =
        kMaxShaderVisibleSamplerHeapSize;
    queried.max_root_signature_dword_cost = kD3d12MaxRootCost;
    queried.max_texture_2d_dimension =
        queried.max_feature_level >= package::kFeatureLevel12_0
            ? kD3d12MaxTexture2dDimension
            : package::kMaxTexture2dDimension;
    queried.max_compute_threads_per_group =
        queried.max_feature_level >= package::kFeatureLevel12_0
            ? package::kMaxComputeThreadsPerGroup
            : 0u;
    queried.max_swap_chain_buffer_count = 3u;
    queried.supports_cs_6_0 =
        queried.shader_model_queried &&
        queried.max_shader_model >= package::kShaderModel6_0;
    queried.supports_vs_6_0 = queried.supports_cs_6_0;
    queried.supports_ps_6_0 = queried.supports_cs_6_0;

    static constexpr const char* kRequiredFormats[] = {
        "DXGI_FORMAT_R8G8B8A8_UNORM",
        "DXGI_FORMAT_R32G32B32A32_FLOAT",
        "DXGI_FORMAT_R32_UINT",
        "DXGI_FORMAT_D32_FLOAT",
    };
    for (const char* format_name : kRequiredFormats) {
        QueriedFormatCapability format{};
        format.format = format_name;
        D3D12_FEATURE_DATA_FORMAT_SUPPORT support{};
        support.Format = MapFormat(format_name);
        if (SUCCEEDED(device->CheckFeatureSupport(
                D3D12_FEATURE_FORMAT_SUPPORT, &support,
                sizeof(support)))) {
            // D3D12_FEATURE_DATA_FORMAT_SUPPORT is { DXGI_FORMAT, UINT, UINT }.
            // Read the two support masks through their ABI-stable offsets so
            // this remains compatible with SDK headers that name the members
            // Support1/Support2 or OutFormatSupport1/OutFormatSupport2.
            static_assert(sizeof(support) == 3u * sizeof(UINT),
                          "unexpected D3D12 format-support layout");
            UINT support_masks[2]{};
            std::memcpy(support_masks,
                        reinterpret_cast<const std::uint8_t*>(&support) +
                            sizeof(DXGI_FORMAT),
                        sizeof(support_masks));
            const UINT flags1 = support_masks[0];
            const UINT flags2 = support_masks[1];
            format.supported = flags1 != 0u;
            format.buffer =
                (flags1 & D3D12_FORMAT_SUPPORT1_BUFFER) != 0u;
            format.texture2d =
                (flags1 & D3D12_FORMAT_SUPPORT1_TEXTURE2D) != 0u;
            format.shader_resource =
                (flags1 & (D3D12_FORMAT_SUPPORT1_SHADER_SAMPLE |
                           D3D12_FORMAT_SUPPORT1_SHADER_LOAD |
                           D3D12_FORMAT_SUPPORT1_SHADER_GATHER)) != 0u;
            format.unordered_access =
                (flags1 & D3D12_FORMAT_SUPPORT1_TYPED_UNORDERED_ACCESS_VIEW) !=
                    0u ||
                (flags2 & D3D12_FORMAT_SUPPORT2_UAV_TYPED_STORE) != 0u;
            format.render_target =
                (flags1 & D3D12_FORMAT_SUPPORT1_RENDER_TARGET) != 0u;
            format.depth_stencil =
                (flags1 & D3D12_FORMAT_SUPPORT1_DEPTH_STENCIL) != 0u;
            format.max_texture_2d_dimension = queried.max_texture_2d_dimension;
        }
        queried.formats.push_back(std::move(format));
    }
    if (!config_.enable_validation) {
        queried.runtime_dxc_available = false;
        queried.runtime_slang_available = false;
    }

    if (platform_->adapter != nullptr) {
        IDXGIAdapter3* adapter3 = nullptr;
        if (SUCCEEDED(platform_->adapter->QueryInterface(
                IID_PPV_ARGS(&adapter3))) &&
            adapter3 != nullptr) {
            DXGI_QUERY_VIDEO_MEMORY_INFO memory_info{};
            if (SUCCEEDED(adapter3->QueryVideoMemoryInfo(
                    0u, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &memory_info))) {
                platform_->gpu_local_usage_bytes = memory_info.CurrentUsage;
            }
            adapter3->Release();
        }
    }

    if (!capabilities_overridden_) {
        capabilities_ = std::move(queried);
    }
#else
    if (!capabilities_overridden_) {
        capabilities_ = MakeBaselineQueriedCapabilities(
            package::kMinWindowsBuild);
        capabilities_.adapter_description = "host_simulation";
        capabilities_.adapter_generation = "metadata_only";
    }
#endif
    return ExitCode::Success;
}

ExitCode PackagePlayer::CreateSwapChainAndFrames() {
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state == nullptr || state->device == nullptr) {
        return Fail(ExitCode::D3D12DeviceCreationFailed, "device",
                    "device_unavailable", "device", "created", "unavailable",
                    "frame_ring",
                    "swap-chain creation requires an initialized D3D12 device");
    }
    D3D12_COMMAND_QUEUE_DESC queue_desc{};
    queue_desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    queue_desc.Priority = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    queue_desc.Flags = D3D12_COMMAND_QUEUE_FLAG_NONE;
    if (FAILED(state->device->CreateCommandQueue(
            &queue_desc, IID_PPV_ARGS(&state->queue))) ||
        state->queue == nullptr) {
        return Fail(ExitCode::CommandQueueCreationFailed, "queue",
                    "command_queue_failed", "direct_queue", "1", "0", "queue",
                    "ID3D12CommandQueue creation failed");
    }

    if (!config_.headless && config_.create_window &&
        state->window.NativeHandle() != nullptr) {
        DXGI_SWAP_CHAIN_DESC1 swap_desc{};
        swap_desc.Width = width_;
        swap_desc.Height = height_;
        swap_desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        swap_desc.Stereo = FALSE;
        swap_desc.SampleDesc.Count = 1u;
        swap_desc.SampleDesc.Quality = 0u;
        swap_desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        swap_desc.BufferCount = package::kSwapChainBufferCount;
        swap_desc.Scaling = DXGI_SCALING_STRETCH;
        swap_desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        swap_desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
        swap_desc.Flags = 0u;
        IDXGISwapChain1* swapchain1 = nullptr;
        const HRESULT swap_hr = state->factory->CreateSwapChainForHwnd(
            state->queue, static_cast<HWND>(state->window.NativeHandle()),
            &swap_desc, nullptr, nullptr, &swapchain1);
        if (FAILED(swap_hr) || swapchain1 == nullptr) {
            return Fail(ExitCode::SwapChainCreationFailed, "swapchain",
                        "swapchain_creation_failed", "buffers", "2", "0",
                        "swapchain",
                        "CreateSwapChainForHwnd(FLIP_DISCARD) failed");
        }
        if (FAILED(swapchain1->QueryInterface(
                IID_PPV_ARGS(&state->swapchain))) ||
            state->swapchain == nullptr) {
            swapchain1->Release();
            return Fail(ExitCode::SwapChainCreationFailed, "swapchain",
                        "swapchain3_unavailable", "IDXGISwapChain3",
                        "available", "unavailable", "swapchain",
                        "IDXGISwapChain1::QueryInterface(IDXGISwapChain3) failed");
        }
        swapchain1->Release();
        state->swapchain_valid = true;
        state->factory->MakeWindowAssociation(
            static_cast<HWND>(state->window.NativeHandle()),
            DXGI_MWA_NO_ALT_ENTER);

        D3D12_DESCRIPTOR_HEAP_DESC rtv_heap_desc{};
        rtv_heap_desc.NumDescriptors = package::kSwapChainBufferCount;
        rtv_heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
        rtv_heap_desc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_NONE;
        if (FAILED(state->device->CreateDescriptorHeap(
                &rtv_heap_desc, IID_PPV_ARGS(&state->rtv_heap))) ||
            state->rtv_heap == nullptr) {
            return Fail(ExitCode::DescriptorHeapCreationFailed, "descriptor",
                        "rtv_heap_failed", "rtv_descriptors", "2", "0",
                        "swapchain", "RTV descriptor heap creation failed");
        }
        state->rtv_increment = state->device->GetDescriptorHandleIncrementSize(
            D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
        stats_.engine_descriptor_count += package::kSwapChainBufferCount;
        ++stats_.descriptor_allocation_calls;
        ++stats_.engine_heap_count;
        if (RecreateRenderTargets() != ExitCode::Success) {
            return last_exit_;
        }
    }

    const std::size_t cbv_count = CountCbvSrvUavDescriptors(package_);
    const std::size_t sampler_count = CountSamplerDescriptors(package_);
    if (cbv_count > 0u) {
        D3D12_DESCRIPTOR_HEAP_DESC heap_desc{};
        heap_desc.NumDescriptors = static_cast<UINT>(cbv_count);
        heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
        heap_desc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        if (FAILED(state->device->CreateDescriptorHeap(
                &heap_desc, IID_PPV_ARGS(&state->cbv_srv_uav_heap))) ||
            state->cbv_srv_uav_heap == nullptr) {
            return Fail(ExitCode::DescriptorHeapCreationFailed, "descriptor",
                        "cbv_srv_uav_heap_failed", "cbv_srv_uav_descriptors",
                        std::to_string(cbv_count), "0", "descriptor",
                        "CBV/SRV/UAV descriptor heap creation failed");
        }
        state->cbv_srv_uav_cpu_start =
            state->cbv_srv_uav_heap->GetCPUDescriptorHandleForHeapStart();
        state->cbv_srv_uav_gpu_start =
            state->cbv_srv_uav_heap->GetGPUDescriptorHandleForHeapStart();
        state->cbv_srv_uav_increment =
            state->device->GetDescriptorHandleIncrementSize(
                D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
        stats_.engine_descriptor_count += cbv_count;
        stats_.descriptor_allocation_calls += cbv_count;
        ++stats_.engine_heap_count;
    }
    if (sampler_count > 0u) {
        D3D12_DESCRIPTOR_HEAP_DESC heap_desc{};
        heap_desc.NumDescriptors = static_cast<UINT>(sampler_count);
        heap_desc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER;
        heap_desc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        if (FAILED(state->device->CreateDescriptorHeap(
                &heap_desc, IID_PPV_ARGS(&state->sampler_heap))) ||
            state->sampler_heap == nullptr) {
            return Fail(ExitCode::DescriptorHeapCreationFailed, "descriptor",
                        "sampler_heap_failed", "sampler_descriptors",
                        std::to_string(sampler_count), "0", "descriptor",
                        "SAMPLER descriptor heap creation failed");
        }
        state->sampler_cpu_start =
            state->sampler_heap->GetCPUDescriptorHandleForHeapStart();
        state->sampler_gpu_start =
            state->sampler_heap->GetGPUDescriptorHandleForHeapStart();
        state->sampler_increment =
            state->device->GetDescriptorHandleIncrementSize(
                D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER);
        stats_.engine_descriptor_count += sampler_count;
        stats_.descriptor_allocation_calls += sampler_count;
        ++stats_.engine_heap_count;
    }

    state->resources.resize(package_.resources.size());
    std::uint32_t cbv_index = 0u;
    std::uint32_t sampler_index = 0u;
    for (std::size_t index = 0u; index < package_.resources.size(); ++index) {
        const auto& descriptor = package_.resources[index];
        auto& slot = state->resources[index];
        slot.resource_id = descriptor.resource_id;
        slot.state = InitialStateForUsage(descriptor);
        if (descriptor.usage == "sampler") {
            slot.descriptor_index = sampler_index++;
            slot.has_descriptor = true;
        } else if (ResourceNeedsCbvSrvUavDescriptor(package_, descriptor)) {
            slot.descriptor_index = cbv_index++;
            slot.has_descriptor = true;
        }
    }
    state->root_signatures.reserve(package_.root_signatures.size());
    state->pipelines.reserve(package_.shaders.size());

    if (FAILED(state->device->CreateFence(
            0u, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&state->fence))) ||
        state->fence == nullptr) {
        return Fail(ExitCode::GpuSubmissionFailed, "synchronization",
                    "fence_creation_failed", "fence", "created", "failed",
                    "frame_ring", "ID3D12Fence creation failed");
    }
    state->fence_event = CreateEventA(nullptr, FALSE, FALSE, nullptr);
    if (state->fence_event == nullptr) {
        return Fail(ExitCode::GpuSubmissionFailed, "synchronization",
                    "fence_event_failed", "event", "created", "failed",
                    "frame_ring", "CreateEventA failed");
    }

    state->frames.resize(frames_in_flight_);
    const std::uint64_t upload_size = MaximumInitialUploadBytes(package_);
    for (std::size_t index = 0u; index < state->frames.size(); ++index) {
        auto& frame = state->frames[index];
        if (FAILED(state->device->CreateCommandAllocator(
                D3D12_COMMAND_LIST_TYPE_DIRECT,
                IID_PPV_ARGS(&frame.allocator))) ||
            frame.allocator == nullptr) {
            return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                        "command_allocator_failed", "allocator", "created",
                        "failed", "frame_ring",
                        "CreateCommandAllocator failed");
        }
        if (FAILED(state->device->CreateCommandList(
                0u, D3D12_COMMAND_LIST_TYPE_DIRECT, frame.allocator, nullptr,
                IID_PPV_ARGS(&frame.command_list))) ||
            frame.command_list == nullptr) {
            return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                        "command_list_failed", "command_list", "created",
                        "failed", "frame_ring",
                        "CreateCommandList failed");
        }
        if (FAILED(frame.command_list->Close())) {
            return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                        "command_list_close_failed", "closed", "closed",
                        "failed", "frame_ring",
                        "initial ID3D12GraphicsCommandList::Close failed");
        }
        if (upload_size > 0u) {
            D3D12_HEAP_PROPERTIES upload_heap{};
            upload_heap.Type = D3D12_HEAP_TYPE_UPLOAD;
            D3D12_RESOURCE_DESC upload_desc{};
            upload_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
            upload_desc.Width = upload_size;
            upload_desc.Height = 1u;
            upload_desc.DepthOrArraySize = 1u;
            upload_desc.MipLevels = 1u;
            upload_desc.Format = DXGI_FORMAT_UNKNOWN;
            upload_desc.SampleDesc.Count = 1u;
            upload_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
            if (FAILED(state->device->CreateCommittedResource(
                    &upload_heap, D3D12_HEAP_FLAG_NONE, &upload_desc,
                    D3D12_RESOURCE_STATE_GENERIC_READ, nullptr,
                    IID_PPV_ARGS(&frame.upload))) ||
                frame.upload == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed,
                            "resource", "frame_upload_failed", "upload_bytes",
                            std::to_string(upload_size), "0", "frame_ring",
                            "per-frame upload ring allocation failed");
            }
            frame.upload_size = upload_size;
            NoteEngineAllocation(0u, upload_size, false, false, false);
        }
    }

#if NW_PLAYER_DIAGNOSTICS
    D3D12_QUERY_HEAP_DESC query_heap_desc{};
    query_heap_desc.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    query_heap_desc.Count = 2u;
    if (SUCCEEDED(state->device->CreateQueryHeap(
            &query_heap_desc, IID_PPV_ARGS(&state->timestamp_heap))) &&
        state->timestamp_heap != nullptr) {
        D3D12_HEAP_PROPERTIES readback_heap{};
        readback_heap.Type = D3D12_HEAP_TYPE_READBACK;
        D3D12_RESOURCE_DESC readback_desc{};
        readback_desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        readback_desc.Width = 2u * sizeof(UINT64);
        readback_desc.Height = 1u;
        readback_desc.DepthOrArraySize = 1u;
        readback_desc.MipLevels = 1u;
        readback_desc.Format = DXGI_FORMAT_UNKNOWN;
        readback_desc.SampleDesc.Count = 1u;
        readback_desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        if (FAILED(state->device->CreateCommittedResource(
                &readback_heap, D3D12_HEAP_FLAG_NONE, &readback_desc,
                D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                IID_PPV_ARGS(&state->timestamp_readback)))) {
            state->timestamp_readback = nullptr;
        }
        if (FAILED(state->queue->GetTimestampFrequency(
                &state->timestamp_frequency))) {
            state->timestamp_frequency = 0u;
        }
    }
#endif
#endif
    return ExitCode::Success;
}

ExitCode PackagePlayer::RecreateRenderTargets() {
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state == nullptr || state->swapchain == nullptr ||
        state->rtv_heap == nullptr) {
        return ExitCode::Success;
    }
    for (ID3D12Resource* backbuffer : state->backbuffers) {
        if (backbuffer != nullptr) backbuffer->Release();
    }
    state->backbuffers.assign(package::kSwapChainBufferCount, nullptr);
    state->backbuffer_states.assign(package::kSwapChainBufferCount,
                                    D3D12_RESOURCE_STATE_PRESENT);
    state->rtv_handles.assign(package::kSwapChainBufferCount,
                              D3D12_CPU_DESCRIPTOR_HANDLE{});
    const D3D12_CPU_DESCRIPTOR_HANDLE rtv_start =
        state->rtv_heap->GetCPUDescriptorHandleForHeapStart();
    for (UINT index = 0u; index < package::kSwapChainBufferCount; ++index) {
        ID3D12Resource* buffer = nullptr;
        if (FAILED(state->swapchain->GetBuffer(index, IID_PPV_ARGS(&buffer))) ||
            buffer == nullptr) {
            return Fail(ExitCode::SwapChainCreationFailed, "swapchain",
                        "swapchain_buffer_unavailable", "buffer",
                        std::to_string(index), "unavailable", "swapchain",
                        "IDXGISwapChain3::GetBuffer failed");
        }
        D3D12_CPU_DESCRIPTOR_HANDLE handle = rtv_start;
        handle.ptr += static_cast<SIZE_T>(index) * state->rtv_increment;
        state->device->CreateRenderTargetView(buffer, nullptr, handle);
        state->backbuffers[index] = buffer;
        state->rtv_handles[index] = handle;
    }
    state->swapchain_valid = true;
#endif
    return ExitCode::Success;
}

ExitCode PackagePlayer::WaitForAllFrames(const char* stage) {
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state == nullptr || state->fence == nullptr ||
        state->fence_event == nullptr) {
        return ExitCode::Success;
    }
    for (auto& frame : state->frames) {
        if (frame.fence_value == 0u) continue;
        const ExitCode wait = WaitForFenceValue(
            state->fence, frame.fence_value, state->fence_event,
            kFenceTimeoutMs);
        if (wait != ExitCode::Success) {
            return Fail(wait, "gpu_execution", "fence_wait_failed", "fence",
                        "completed", "timeout_or_failure",
                        stage == nullptr ? "fence_wait" : stage,
                        "waiting for affected frame work failed");
        }
        frame.fence_value = 0u;
    }
#else
    (void)stage;
#endif
    return ExitCode::Success;
}

bool PackagePlayer::CheckDeviceRemoved(const char* stage) {
    if (force_device_removed_) {
        Fail(ExitCode::DeviceRemoved, "device", "device_removed", "device",
             "available", "removed", stage,
             "device removal was reported; automatic recovery/replay is "
             "forbidden");
        return true;
    }
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state != nullptr && state->device != nullptr) {
        const HRESULT reason = state->device->GetDeviceRemovedReason();
        if (FAILED(reason)) {
            Fail(ExitCode::DeviceRemoved, "device", "device_removed", "device",
                 "available", "removed", stage,
                 "ID3D12Device::GetDeviceRemovedReason reported removal");
            return true;
        }
    }
#endif
    return false;
}

bool PackagePlayer::CheckD3D12Validation(const char* stage) {
#if defined(_WIN32) && defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    PlatformState* state = platform_.get();
    if (state != nullptr && state->info_queue != nullptr &&
        HasValidationError(state->info_queue)) {
        Fail(ExitCode::D3D12ValidationError, "validation",
             "d3d12_validation_error", "validation_clean", "clean", "error",
             stage == nullptr ? "validation" : stage,
             "D3D12 debug layer reported an error or corruption message");
        return true;
    }
#else
    (void)stage;
#endif
    return false;
}

ExitCode PackagePlayer::RunInitializationInstructions() {
    // A bounded frame upload buffer is only needed for init.upload_resource.
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state == nullptr) {
        return Fail(ExitCode::PackageValidationFailed, "package_validation",
                    "platform_state_unavailable", "platform", "initialized",
                    "unavailable", "init.execute",
                    "initialization requires an initialized platform/device");
    }
    const auto require_operands =
        [this](const package::Instruction& instruction,
               std::size_t expected) -> bool {
        if (instruction.operands.size() == expected) return true;
        Fail(ExitCode::PackageValidationFailed, "package_validation",
             "package_instruction_operand_count", "operand_count",
             std::to_string(expected),
             std::to_string(instruction.operands.size()), "init.execute",
             "initialization instruction operand count is invalid");
        return false;
    };
    for (const auto& instruction : package_.initialization) {
        if (instruction.opcode == "init.create_resource") {
            if (!require_operands(instruction, 1u)) {
                return last_exit_;
            }
            if (instruction.operands[0].type !=
                package::OperandType::ResourceRef) {
                return Fail(ExitCode::PackageValidationFailed,
                            "package_validation",
                            "package_instruction_operand", "operand_type",
                            "resource_ref", "invalid", "init.execute",
                            "init.create_resource operand type is invalid");
            }
            const std::uint32_t resource_id = instruction.operands[0].u32;
            const ResourcePlacement* placement = plan_.Find(resource_id);
            if (placement == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "resource_placement_missing", "resource_id",
                            std::to_string(resource_id), "missing",
                            "init.create_resource",
                            "resource has no validated non-aliased placement");
            }
            PlatformState::ResourceSlot* slot = nullptr;
            for (auto& candidate : state->resources) {
                if (candidate.resource_id == resource_id) {
                    slot = &candidate;
                    break;
                }
            }
            if (slot == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "resource_slot_missing", "resource_id",
                            std::to_string(resource_id), "missing",
                            "init.create_resource",
                            "resource slot was not registered during swap-chain "
                            "and frame creation");
            }
            if (slot->resource != nullptr) {
                continue;
            }
            if (placement->descriptor.usage != "sampler") {
                const D3D12_RESOURCE_DESC desc =
                    MakeResourceDesc(placement->descriptor);
                D3D12_HEAP_PROPERTIES heap{};
                heap.Type = HeapTypeForUsage(placement->descriptor);
                const D3D12_RESOURCE_STATES initial =
                    InitialStateForUsage(placement->descriptor);
                if (FAILED(state->device->CreateCommittedResource(
                        &heap, D3D12_HEAP_FLAG_NONE, &desc, initial, nullptr,
                        IID_PPV_ARGS(&slot->resource))) ||
                    slot->resource == nullptr) {
                    return Fail(ExitCode::ResourceAllocationFailed, "resource",
                                "committed_resource_failed", "resource",
                                placement->descriptor.name, "allocation failed",
                                "init.create_resource",
                                "CreateCommittedResource failed");
                }
                slot->state = initial;
                NoteEngineAllocation(placement->allocation_size_bytes, 0u,
                                     false, false, false);
            }
            if (slot->has_descriptor) {
                if (placement->descriptor.usage == "sampler") {
                    D3D12_SAMPLER_DESC sampler{};
                    sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
                    sampler.AddressU = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
                    sampler.AddressV = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
                    sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
                    sampler.ComparisonFunc = D3D12_COMPARISON_FUNC_NEVER;
                    sampler.MaxLOD = D3D12_FLOAT32_MAX;
                    D3D12_CPU_DESCRIPTOR_HANDLE handle =
                        state->sampler_cpu_start;
                    handle.ptr += static_cast<SIZE_T>(slot->descriptor_index) *
                                  state->sampler_increment;
                    state->device->CreateSampler(&sampler, handle);
                    slot->cpu_handle = handle;
                    slot->gpu_handle = state->sampler_gpu_start;
                    slot->gpu_handle.ptr +=
                        static_cast<UINT64>(slot->descriptor_index) *
                        state->sampler_increment;
                } else if (slot->resource != nullptr) {
                    D3D12_CPU_DESCRIPTOR_HANDLE handle =
                        state->cbv_srv_uav_cpu_start;
                    handle.ptr += static_cast<SIZE_T>(slot->descriptor_index) *
                                  state->cbv_srv_uav_increment;
                    if (placement->descriptor.usage == "cbv") {
                        D3D12_CONSTANT_BUFFER_VIEW_DESC view{};
                        view.BufferLocation =
                            slot->resource->GetGPUVirtualAddress();
                        view.SizeInBytes = static_cast<UINT>(
                            std::max<std::uint64_t>(
                                256u,
                                (placement->descriptor.size_bytes + 255u) &
                                    ~255ull));
                        state->device->CreateConstantBufferView(&view, handle);
                    } else if (placement->descriptor.usage == "uav") {
                        D3D12_UNORDERED_ACCESS_VIEW_DESC view{};
                        view.Format = DXGI_FORMAT_UNKNOWN;
                        view.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
                        view.Buffer.FirstElement = 0u;
                        view.Buffer.NumElements = static_cast<UINT>(
                            placement->descriptor.size_bytes /
                            sizeof(std::uint32_t));
                        view.Buffer.StructureByteStride = 0u;
                        state->device->CreateUnorderedAccessView(
                            slot->resource, nullptr, &view, handle);
                    } else if (placement->descriptor.usage == "srv") {
                        D3D12_SHADER_RESOURCE_VIEW_DESC view{};
                        view.Format = MapFormat(placement->descriptor.format);
                        view.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
                        view.Shader4ComponentMapping =
                            D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
                        view.Buffer.FirstElement = 0u;
                        view.Buffer.NumElements = static_cast<UINT>(
                            placement->descriptor.size_bytes /
                            std::max<std::uint32_t>(
                                1u, BytesPerElement(
                                        placement->descriptor.format)));
                        state->device->CreateShaderResourceView(
                            slot->resource, &view, handle);
                    } else if (placement->descriptor.usage == "rtv") {
                        state->device->CreateRenderTargetView(
                            slot->resource, nullptr, handle);
                    } else if (placement->descriptor.usage == "dsv") {
                        state->device->CreateDepthStencilView(
                            slot->resource, nullptr, handle);
                    }
                    slot->cpu_handle = handle;
                    slot->gpu_handle = state->cbv_srv_uav_gpu_start;
                    slot->gpu_handle.ptr +=
                        static_cast<UINT64>(slot->descriptor_index) *
                        state->cbv_srv_uav_increment;
                }
            }
        } else if (instruction.opcode == "init.create_root_signature") {
            if (!require_operands(instruction, 1u)) {
                return last_exit_;
            }
            if (instruction.operands[0].type !=
                package::OperandType::RootSignatureRef) {
                return Fail(ExitCode::PackageValidationFailed,
                            "package_validation",
                            "package_instruction_operand", "operand_type",
                            "root_signature_ref", "invalid", "init.execute",
                            "init.create_root_signature operand type is invalid");
            }
            const std::uint32_t root_id = instruction.operands[0].u32;
            const package::RootSignatureRecord* record = nullptr;
            for (const auto& candidate : package_.root_signatures) {
                if (candidate.root_signature_id == root_id) {
                    record = &candidate;
                    break;
                }
            }
            if (record == nullptr) {
                return Fail(ExitCode::RootSignatureCreationFailed, "pipeline",
                            "root_signature_record_missing", "root_signature",
                            std::to_string(root_id), "missing",
                            "init.create_root_signature",
                            "package carries no root-signature record");
            }
            if (force_root_signature_creation_failure_) {
                return Fail(ExitCode::RootSignatureCreationFailed, "pipeline",
                            "simulated_root_signature_failure",
                            "root_signature", std::to_string(root_id),
                            "simulated failure",
                            "init.create_root_signature",
                            "deterministic root-signature failure seam");
            }
            PlatformState::RootSlot root_slot{};
            root_slot.root_signature_id = root_id;
            if (record->blob.empty() ||
                FAILED(state->device->CreateRootSignature(
                    0u, record->blob.data(), record->blob.size(),
                    IID_PPV_ARGS(&root_slot.root_signature))) ||
                root_slot.root_signature == nullptr) {
                return Fail(ExitCode::RootSignatureCreationFailed,
                            "pipeline", "root_signature_failed",
                            "root_signature", std::to_string(root_id),
                            "creation failed", "init.create_root_signature",
                            "ID3D12Device::CreateRootSignature failed");
            }
            state->root_signatures.push_back(root_slot);
        } else if (instruction.opcode == "init.create_pipeline") {
            if (!require_operands(instruction, 2u)) {
                return last_exit_;
            }
            if (instruction.operands[0].type !=
                    package::OperandType::ShaderRef ||
                instruction.operands[1].type !=
                    package::OperandType::RootSignatureRef) {
                return Fail(ExitCode::PackageValidationFailed,
                            "package_validation",
                            "package_instruction_operand", "operand_type",
                            "shader_ref,root_signature_ref", "invalid",
                            "init.execute",
                            "init.create_pipeline operand types are invalid");
            }
            const std::uint32_t shader_id = instruction.operands[0].u32;
            const std::uint32_t root_id = instruction.operands[1].u32;
            const package::ShaderRecord* shader = nullptr;
            for (const auto& candidate : package_.shaders) {
                if (candidate.shader_id == shader_id) {
                    shader = &candidate;
                    break;
                }
            }
            ID3D12RootSignature* root = nullptr;
            for (const auto& candidate : state->root_signatures) {
                if (candidate.root_signature_id == root_id) {
                    root = candidate.root_signature;
                    break;
                }
            }
            if (shader == nullptr || root == nullptr) {
                return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                            "pipeline_dependency_missing", "pipeline",
                            std::to_string(shader_id),
                            shader == nullptr ? "shader missing"
                                              : "root signature missing",
                            "init.create_pipeline",
                            "compute pipeline dependencies are unavailable");
            }
            if (force_pipeline_creation_failure_) {
                return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                            "simulated_pipeline_failure", "pipeline",
                            std::to_string(shader_id), "simulated failure",
                            "init.create_pipeline",
                            "deterministic pipeline creation failure seam");
            }
            if (shader->target != "cs_6_0") {
                return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                            "unsupported_shader_target", "shader_target",
                            "cs_6_0", shader->target, "init.create_pipeline",
                            "minimal NW-04 player realizes compute pipelines");
            }
            D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
            pso_desc.pRootSignature = root;
            pso_desc.CS.pShaderBytecode = shader->blob.data();
            pso_desc.CS.BytecodeLength = shader->blob.size();
            PlatformState::PipelineSlot pipeline{};
            pipeline.shader_id = shader_id;
            if (FAILED(state->device->CreateComputePipelineState(
                    &pso_desc, IID_PPV_ARGS(&pipeline.pipeline_state))) ||
                pipeline.pipeline_state == nullptr) {
                return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                            "compute_pso_failed", "pipeline",
                            std::to_string(shader_id), "creation failed",
                            "init.create_pipeline",
                            "CreateComputePipelineState failed for offline DXIL");
            }
            state->pipelines.push_back(pipeline);
        } else if (instruction.opcode == "init.upload_resource") {
            if (!require_operands(instruction, 1u)) {
                return last_exit_;
            }
            if (instruction.operands[0].type !=
                package::OperandType::ResourceRef) {
                return Fail(ExitCode::PackageValidationFailed,
                            "package_validation",
                            "package_instruction_operand", "operand_type",
                            "resource_ref", "invalid", "init.execute",
                            "init.upload_resource operand type is invalid");
            }
            const std::uint32_t resource_id = instruction.operands[0].u32;
            PlatformState::ResourceSlot* slot = nullptr;
            for (auto& candidate : state->resources) {
                if (candidate.resource_id == resource_id) {
                    slot = &candidate;
                    break;
                }
            }
            if (slot == nullptr || slot->resource == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "upload_resource_missing", "resource_id",
                            std::to_string(resource_id), "missing",
                            "init.upload_resource",
                            "upload target resource was not created");
            }
            const ResourcePlacement* placement = plan_.Find(resource_id);
            if (placement == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "upload_placement_missing", "resource_id",
                            std::to_string(resource_id), "missing",
                            "init.upload_resource",
                            "upload target has no validated placement");
            }
            if (placement->descriptor.kind != "buffer") {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "upload_kind_unsupported", "resource_kind",
                            "buffer", placement->descriptor.kind,
                            "init.upload_resource",
                            "the minimal player uploads buffer resources only");
            }
            if (state->frames.empty() || state->frames[0].upload == nullptr ||
                state->frames[0].upload_size <
                    placement->descriptor.size_bytes) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "frame_upload_ring_unavailable", "upload_bytes",
                            std::to_string(placement->descriptor.size_bytes),
                            state->frames.empty() || state->frames[0].upload ==
                                    nullptr
                                ? "unavailable"
                                : std::to_string(
                                      state->frames[0].upload_size),
                            "init.upload_resource",
                            "per-frame upload ring is missing or too small");
            }
            // The ring is owned by frame slot 0 during initialization.  If a
            // previous initialization upload is still in flight, wait on that
            // slot's fence before rewriting the mapped region.
            auto& frame = state->frames[0];
            if (frame.fence_value != 0u) {
                const ExitCode wait = WaitForFenceValue(
                    state->fence, frame.fence_value, state->fence_event,
                    kFenceTimeoutMs);
                if (wait != ExitCode::Success) {
                    return Fail(wait, "gpu_execution", "fence_wait_failed",
                                "fence", "completed", "timeout_or_failure",
                                "init.upload_resource",
                                "waiting for upload-ring ownership failed");
                }
                frame.fence_value = 0u;
                ++stats_.upload_reuse_waits;
            }
            void* mapped = nullptr;
            D3D12_RANGE read_range{0u, 0u};
            if (FAILED(frame.upload->Map(0u, &read_range, &mapped)) ||
                mapped == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "upload_map_failed", "mapped", "available",
                            "unavailable", "init.upload_resource",
                            "per-frame upload ring Map failed");
            }
            std::memset(mapped, 0, static_cast<std::size_t>(
                                      placement->descriptor.size_bytes));
            std::size_t offset = 0u;
            for (const auto& parameter : package_.parameters) {
                if (offset + parameter.value.size() >
                    placement->descriptor.size_bytes) {
                    break;
                }
                std::memcpy(static_cast<std::uint8_t*>(mapped) + offset,
                            parameter.value.data(), parameter.value.size());
                offset += parameter.value.size();
            }
            D3D12_RANGE write_range{0u, static_cast<SIZE_T>(
                                           placement->descriptor.size_bytes)};
            frame.upload->Unmap(0u, &write_range);

            if (frame.command_list == nullptr || frame.allocator == nullptr) {
                return Fail(ExitCode::CommandListRecordingFailed,
                            "command_list", "upload_command_list_unavailable",
                            "command_list", "created", "unavailable",
                            "init.upload_resource",
                            "frame command list/allocator is unavailable");
            }
            if (FAILED(frame.allocator->Reset()) ||
                FAILED(frame.command_list->Reset(frame.allocator, nullptr))) {
                return Fail(ExitCode::CommandListRecordingFailed,
                            "command_list", "command_list_reset_failed",
                            "reset", "succeeded", "failed",
                            "init.upload_resource",
                            "command list reset failed for initialization upload");
            }
            D3D12_RESOURCE_BARRIER to_copy{};
            to_copy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            to_copy.Transition.pResource = slot->resource;
            to_copy.Transition.StateBefore = slot->state;
            to_copy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
            to_copy.Transition.Subresource =
                D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            frame.command_list->ResourceBarrier(1u, &to_copy);
            frame.command_list->CopyBufferRegion(
                slot->resource, 0u, frame.upload, 0u,
                placement->descriptor.size_bytes);
            const D3D12_RESOURCE_STATES final_state =
                WorkingStateForUsage(placement->descriptor);
            D3D12_RESOURCE_BARRIER to_work = to_copy;
            to_work.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
            to_work.Transition.StateAfter = final_state;
            frame.command_list->ResourceBarrier(1u, &to_work);
            slot->state = final_state;
            if (FAILED(frame.command_list->Close())) {
                return Fail(ExitCode::CommandListRecordingFailed,
                            "command_list", "command_list_close_failed",
                            "closed", "closed", "failed",
                            "init.upload_resource",
                            "command list close failed");
            }
            ID3D12CommandList* lists[] = {frame.command_list};
            state->queue->ExecuteCommandLists(1u, lists);
            const std::uint64_t fence_value = next_fence_value_++;
            if (FAILED(state->queue->Signal(state->fence, fence_value))) {
                return Fail(ExitCode::GpuSubmissionFailed,
                            "gpu_execution", "queue_signal_failed", "signal",
                            "succeeded", "failed", "init.upload_resource",
                            "queue Signal failed");
            }
            frame.fence_value = fence_value;
        }
    }
    // Initialization is complete only when the ordered upload/copy submissions
    // have retired.  This also releases upload-ring ownership to the frame
    // ring before the first render tick.
    {
        const ExitCode wait = WaitForAllFrames("initialization_complete");
        if (wait != ExitCode::Success) return wait;
    }
#else
    // Host simulation still executes the same ordered initialization contract.
    std::array<bool, kMaxFramesInFlight> upload_slots_used{};
    for (const auto& instruction : package_.initialization) {
        if (instruction.opcode == "init.create_resource") {
            const ResourcePlacement* placement =
                plan_.Find(instruction.operands[0].u32);
            if (placement == nullptr) {
                return Fail(ExitCode::ResourceAllocationFailed, "resource",
                            "resource_placement_missing", "resource_id",
                            std::to_string(instruction.operands[0].u32),
                            "missing", "init.create_resource",
                            "resource has no validated non-aliased placement");
            }
            NoteEngineAllocation(placement->allocation_size_bytes, 0u, false,
                                 false, false);
            if (placement->descriptor.usage == "sampler") {
                NoteEngineAllocation(0u, 0u, true, false, false);
            } else if (ResourceNeedsCbvSrvUavDescriptor(
                           package_, placement->descriptor)) {
                NoteEngineAllocation(0u, 0u, true, false, false);
            }
        } else if (instruction.opcode == "init.create_root_signature" &&
                   force_root_signature_creation_failure_) {
            return Fail(ExitCode::RootSignatureCreationFailed, "pipeline",
                        "simulated_root_signature_failure", "root_signature",
                        std::to_string(instruction.operands[0].u32),
                        "simulated failure", "init.create_root_signature",
                        "deterministic root-signature failure seam");
        } else if (instruction.opcode == "init.create_pipeline" &&
                   force_pipeline_creation_failure_) {
            return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                        "simulated_pipeline_failure", "pipeline",
                        std::to_string(instruction.operands[0].u32),
                        "simulated failure", "init.create_pipeline",
                        "deterministic pipeline creation failure seam");
        } else if (instruction.opcode == "init.upload_resource") {
            // Initialization owns frame slot 0's upload ring, matching the
            // native path.
            const std::size_t slot = 0u;
            if (upload_slots_used[slot]) {
                ++stats_.upload_reuse_waits;
            }
            upload_slots_used[slot] = true;
        }
    }
    NoteEngineAllocation(0u, 0u, false, true, false);
#endif
    return ExitCode::Success;
}

ExitCode PackagePlayer::ExecuteFrameInstructions(std::uint64_t tick) {
    (void)tick;
    // Frame planning was completed during initialization.  This loop performs
    // no graph traversal, reflection, readback, or engine-owned heap allocation.
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state == nullptr || state->queue == nullptr) {
        return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                    "command_queue_unavailable", "command_queue", "created",
                    "unavailable", "renderAtTick",
                    "frame execution requires an initialized direct queue");
    }
    const std::size_t slot_index =
        static_cast<std::size_t>(frames_rendered_ % frames_in_flight_);
    auto& frame = state->frames[slot_index];
    if (frame.fence_value != 0u) {
        const ExitCode wait = WaitForFenceValue(
            state->fence, frame.fence_value, state->fence_event,
            kFenceTimeoutMs);
        if (wait != ExitCode::Success) {
            return Fail(wait, "gpu_execution", "frame_fence_wait_failed",
                        "fence", "completed", "timeout_or_failure",
                        "renderAtTick",
                        "waiting for frame-slot ownership failed");
        }
        frame.fence_value = 0u;
    }
#if NW_PLAYER_DIAGNOSTICS
    instrumentation_.BeginFrame(tick);
#endif
    if (FAILED(frame.allocator->Reset()) ||
        FAILED(frame.command_list->Reset(frame.allocator, nullptr))) {
        return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                    "command_list_reset_failed", "reset", "succeeded",
                    "failed", "renderAtTick",
                    "ID3D12CommandAllocator/List reset failed");
    }
    ID3D12GraphicsCommandList* list = frame.command_list;
    ID3D12DescriptorHeap* heaps[2]{};
    UINT heap_count = 0u;
    if (state->cbv_srv_uav_heap != nullptr) {
        heaps[heap_count++] = state->cbv_srv_uav_heap;
    }
    if (state->sampler_heap != nullptr) {
        heaps[heap_count++] = state->sampler_heap;
    }
    if (heap_count > 0u) {
        list->SetDescriptorHeaps(heap_count, heaps);
    }

#if NW_PLAYER_DIAGNOSTICS
    const bool timestamping = state->timestamp_heap != nullptr &&
                              state->timestamp_readback != nullptr &&
                              state->timestamp_frequency != 0u;
    if (timestamping) {
        list->EndQuery(state->timestamp_heap, D3D12_QUERY_TYPE_TIMESTAMP, 0u);
    }
#endif
    bool present_requested = false;
    // frame.set_timeline updates this bounded state in emitted instruction
    // order.  The supplied render tick advances the selected parameter without
    // allocating, reflecting, or re-planning anything.
    bool timeline_override = timeline_override_active_;
    std::uint32_t timeline_parameter = timeline_parameter_id_;
    std::uint64_t timeline_value = timeline_tick_;
    for (const auto& op : frame_ops_) {
        switch (op.type) {
            case FrameOp::Type::SetRootSignature: {
                ID3D12RootSignature* root = nullptr;
                for (const auto& candidate : state->root_signatures) {
                    if (candidate.root_signature_id == op.root_parameter) {
                        root = candidate.root_signature;
                        break;
                    }
                }
                if (root == nullptr) {
                    return Fail(ExitCode::RootSignatureCreationFailed,
                                "pipeline", "root_signature_missing",
                                "root_signature", std::to_string(op.root_parameter),
                                "missing", "renderAtTick",
                                "frame root signature was not created");
                }
                list->SetComputeRootSignature(root);
                break;
            }
            case FrameOp::Type::SetRootConstants:
                if (op.constant_count > 0u) {
                    // A `time` parameter is advanced from the supplied tick
                    // without allocating or rebuilding the frame plan.
                    std::array<std::uint32_t,
                               package::kMaxRootSignatureDwordCost>
                        values = op.constants;
                    bool timeline_applied = false;
                    if (timeline_override) {
                        for (std::uint32_t entry = 0u;
                             entry < op.parameter_count; ++entry) {
                            if (op.parameter_ids[entry] !=
                                timeline_parameter) {
                                continue;
                            }
                            WriteTimelineTick(
                                values.data(),
                                static_cast<std::uint32_t>(
                                    op.parameter_offsets[entry]),
                                op.parameter_sizes[entry],
                                op.parameter_tick_kinds[entry],
                                timeline_value);
                            timeline_applied = true;
                            break;
                        }
                    }
                    if (!timeline_applied && op.time_byte_offset >= 0 &&
                        static_cast<std::uint32_t>(op.time_byte_offset) + 4u <=
                            op.constant_count * 4u) {
                        const float time_value = static_cast<float>(tick);
                        std::memcpy(
                            reinterpret_cast<std::uint8_t*>(values.data()) +
                                op.time_byte_offset,
                            &time_value, sizeof(time_value));
                    }
                    list->SetComputeRoot32BitConstants(
                        op.root_parameter, op.constant_count,
                        values.data(), 0u);
                    last_root_constants_ = values;
                    last_root_constant_count_ = op.constant_count;
                }
                break;
            case FrameOp::Type::SetDescriptorTable: {
                D3D12_GPU_DESCRIPTOR_HANDLE handle{};
                for (const auto& candidate : state->resources) {
                    if (candidate.resource_id != op.resource_a ||
                        !candidate.has_descriptor) {
                        continue;
                    }
                    handle = candidate.gpu_handle;
                    break;
                }
                if (handle.ptr == 0u) {
                    return Fail(ExitCode::CommandListRecordingFailed,
                                "command_list",
                                "descriptor_table_target_missing",
                                "descriptor_table_resource",
                                std::to_string(op.resource_a),
                                "no GPU descriptor", "renderAtTick",
                                "frame.set_descriptor_table has no descriptor "
                                "for the referenced resource");
                }
                list->SetComputeRootDescriptorTable(op.root_parameter, handle);
                break;
            }
            case FrameOp::Type::Barrier: {
                PlatformState::ResourceSlot* slot = nullptr;
                for (auto& candidate : state->resources) {
                    if (candidate.resource_id == op.resource_a) {
                        slot = &candidate;
                        break;
                    }
                }
                if (slot == nullptr || slot->resource == nullptr) {
                    return Fail(ExitCode::ResourceAllocationFailed,
                                "resource", "barrier_resource_missing",
                                "resource_id", std::to_string(op.resource_a),
                                "missing", "renderAtTick",
                                "frame.barrier references an uncreated resource");
                }
                D3D12_RESOURCE_STATES after =
                    D3D12_RESOURCE_STATE_COMMON;
                if (!TryBarrierStateFromCode(op.barrier_state, &after)) {
                    return Fail(ExitCode::PackageValidationFailed,
                                "package_validation",
                                "package_instruction_barrier_state",
                                "barrier_state", "0..12",
                                std::to_string(op.barrier_state),
                                "renderAtTick",
                                "frame.barrier uses an unknown resource-state "
                                "code");
                }
                if (after != slot->state) {
                    D3D12_RESOURCE_BARRIER barrier{};
                    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                    barrier.Transition.pResource = slot->resource;
                    barrier.Transition.StateBefore = slot->state;
                    barrier.Transition.StateAfter = after;
                    barrier.Transition.Subresource =
                        D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                    list->ResourceBarrier(1u, &barrier);
                    slot->state = after;
                }
                break;
            }
            case FrameOp::Type::Dispatch: {
                ID3D12PipelineState* pipeline = nullptr;
                for (const auto& candidate : state->pipelines) {
                    if (candidate.shader_id == op.shader_id) {
                        pipeline = candidate.pipeline_state;
                        break;
                    }
                }
                if (pipeline == nullptr) {
                    return Fail(ExitCode::PipelineStateCreationFailed,
                                "pipeline", "pipeline_missing", "shader_id",
                                std::to_string(op.shader_id), "missing",
                                "renderAtTick",
                                "frame.dispatch pipeline was not created");
                }
                for (auto& candidate : state->resources) {
                    if (!candidate.has_descriptor ||
                        candidate.resource == nullptr) {
                        continue;
                    }
                    for (const auto& descriptor : package_.resources) {
                        if (descriptor.resource_id != candidate.resource_id) {
                            continue;
                        }
                        const D3D12_RESOURCE_STATES working =
                            WorkingStateForUsage(descriptor);
                        if (candidate.state != working) {
                            D3D12_RESOURCE_BARRIER barrier{};
                            barrier.Type =
                                D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                            barrier.Transition.pResource =
                                candidate.resource;
                            barrier.Transition.StateBefore = candidate.state;
                            barrier.Transition.StateAfter = working;
                            barrier.Transition.Subresource =
                                D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                            list->ResourceBarrier(1u, &barrier);
                            candidate.state = working;
                        }
                    }
                }
                list->SetPipelineState(pipeline);
                list->Dispatch(op.group_x, op.group_y, op.group_z);
                break;
            }
            case FrameOp::Type::CopyBuffer: {
                PlatformState::ResourceSlot* source = nullptr;
                PlatformState::ResourceSlot* destination = nullptr;
                for (auto& candidate : state->resources) {
                    if (candidate.resource_id == op.resource_a) source = &candidate;
                    if (candidate.resource_id == op.resource_b)
                        destination = &candidate;
                }
                if (source == nullptr || destination == nullptr ||
                    source->resource == nullptr ||
                    destination->resource == nullptr) {
                    return Fail(ExitCode::ResourceAllocationFailed,
                                "resource", "copy_resource_missing",
                                "resource_ref", "created", "missing",
                                "renderAtTick",
                                "frame.copy_buffer references a missing resource");
                }
                D3D12_RESOURCE_BARRIER barriers[2]{};
                UINT barrier_count = 0u;
                if (source->state != D3D12_RESOURCE_STATE_COPY_SOURCE) {
                    barriers[barrier_count].Type =
                        D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                    barriers[barrier_count].Transition.pResource =
                        source->resource;
                    barriers[barrier_count].Transition.StateBefore =
                        source->state;
                    barriers[barrier_count].Transition.StateAfter =
                        D3D12_RESOURCE_STATE_COPY_SOURCE;
                    barriers[barrier_count].Transition.Subresource =
                        D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                    source->state = D3D12_RESOURCE_STATE_COPY_SOURCE;
                    ++barrier_count;
                }
                if (destination->state != D3D12_RESOURCE_STATE_COPY_DEST) {
                    barriers[barrier_count].Type =
                        D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                    barriers[barrier_count].Transition.pResource =
                        destination->resource;
                    barriers[barrier_count].Transition.StateBefore =
                        destination->state;
                    barriers[barrier_count].Transition.StateAfter =
                        D3D12_RESOURCE_STATE_COPY_DEST;
                    barriers[barrier_count].Transition.Subresource =
                        D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                    destination->state = D3D12_RESOURCE_STATE_COPY_DEST;
                    ++barrier_count;
                }
                if (barrier_count > 0u) {
                    list->ResourceBarrier(barrier_count, barriers);
                }
                list->CopyBufferRegion(destination->resource, 0u,
                                       source->resource, 0u, op.byte_count);
                break;
            }
            case FrameOp::Type::SetTimeline:
                // Package data selects the parameter; the externally supplied
                // render tick advances it from the emitted tick base.  No
                // runtime graph, reflection, or allocation is involved.
                timeline_override = true;
                timeline_parameter = op.parameter_id;
                timeline_value =
                    op.tick_value >
                            std::numeric_limits<std::uint64_t>::max() - tick
                        ? std::numeric_limits<std::uint64_t>::max()
                        : op.tick_value + tick;
                timeline_parameter_id_ = timeline_parameter;
                timeline_tick_ = timeline_value;
                timeline_override_active_ = true;
                break;
            case FrameOp::Type::Draw:
                return Fail(ExitCode::PipelineStateCreationFailed,
                            "pipeline", "frame_draw_requires_raster_pipeline",
                            "pipeline", "package raster pipeline",
                            "unsupported", "renderAtTick",
                            "frame.draw is outside the minimal NW-04 player");
            case FrameOp::Type::Present: {
                present_requested = true;
                if (state->swapchain == nullptr || !state->swapchain_valid) {
                    break;
                }
                const UINT backbuffer_index =
                    state->swapchain->GetCurrentBackBufferIndex();
                if (backbuffer_index >= state->backbuffers.size()) {
                    return Fail(ExitCode::PresentationFailed, "presentation",
                                "backbuffer_index_invalid",
                                "backbuffer_index", "< buffer_count",
                                std::to_string(backbuffer_index),
                                "renderAtTick",
                                "swap-chain reported an out-of-range back-buffer "
                                "index");
                }
                D3D12_RESOURCE_BARRIER to_render{};
                to_render.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                to_render.Transition.pResource =
                    state->backbuffers[backbuffer_index];
                to_render.Transition.StateBefore =
                    state->backbuffer_states[backbuffer_index];
                to_render.Transition.StateAfter =
                    D3D12_RESOURCE_STATE_RENDER_TARGET;
                to_render.Transition.Subresource =
                    D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                if (to_render.Transition.StateBefore !=
                    D3D12_RESOURCE_STATE_RENDER_TARGET) {
                    list->ResourceBarrier(1u, &to_render);
                    state->backbuffer_states[backbuffer_index] =
                        D3D12_RESOURCE_STATE_RENDER_TARGET;
                }
                const D3D12_CPU_DESCRIPTOR_HANDLE rtv =
                    state->rtv_handles[backbuffer_index];
                list->OMSetRenderTargets(1u, &rtv, FALSE, nullptr);
                const float clear_color[4] = {0.02f, 0.03f, 0.05f, 1.0f};
                list->ClearRenderTargetView(rtv, clear_color, 0u, nullptr);
                break;
            }
        }
    }
    if (present_requested && state->swapchain != nullptr) {
        const UINT index = state->swapchain->GetCurrentBackBufferIndex();
        if (index < state->backbuffer_states.size() &&
            state->backbuffer_states[index] !=
                D3D12_RESOURCE_STATE_PRESENT) {
            D3D12_RESOURCE_BARRIER to_present{};
            to_present.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            to_present.Transition.pResource = state->backbuffers[index];
            to_present.Transition.StateBefore =
                state->backbuffer_states[index];
            to_present.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
            to_present.Transition.Subresource =
                D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            list->ResourceBarrier(1u, &to_present);
            state->backbuffer_states[index] = D3D12_RESOURCE_STATE_PRESENT;
        }
    }
#if NW_PLAYER_DIAGNOSTICS
    if (timestamping) {
        list->EndQuery(state->timestamp_heap, D3D12_QUERY_TYPE_TIMESTAMP, 1u);
        list->ResolveQueryData(state->timestamp_heap,
                               D3D12_QUERY_TYPE_TIMESTAMP, 0u, 2u,
                               state->timestamp_readback, 0u);
    }
#endif

    if (simulate_post_initialization_allocation_) {
        // Diagnostic seam: this follows exactly the same engine allocation
        // tracking path as a real post-init heap/descriptor allocation, so the
        // violation counter is exercised rather than assumed.
        NoteEngineAllocation(64u, 0u, true, true, false);
    }
    if (FAILED(list->Close())) {
        return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                    "command_list_close_failed", "closed", "closed", "failed",
                    "renderAtTick", "ID3D12GraphicsCommandList::Close failed");
    }
    ID3D12CommandList* lists[] = {list};
    state->queue->ExecuteCommandLists(1u, lists);
    const std::uint64_t fence_value = next_fence_value_++;
    if (FAILED(state->queue->Signal(state->fence, fence_value))) {
        return Fail(ExitCode::GpuSubmissionFailed, "gpu_execution",
                    "queue_signal_failed", "signal", "succeeded", "failed",
                    "renderAtTick", "ID3D12CommandQueue::Signal failed");
    }
    frame.fence_value = fence_value;

#if NW_PLAYER_DIAGNOSTICS
    // CPU submission ends at queue Signal.  Record it before fence waits and
    // Present so the metric does not include GPU execution or VSync.
    instrumentation_.EndFrame();
#endif

    if (config_.wait_for_gpu_each_frame) {
        const ExitCode wait = WaitForFenceValue(
            state->fence, fence_value, state->fence_event, kFenceTimeoutMs);
        if (wait != ExitCode::Success) {
            return Fail(wait, "gpu_execution", "fence_wait_failed", "fence",
                        "completed", "timeout_or_failure", "renderAtTick",
                        "frame fence wait failed");
        }
        frame.fence_value = 0u;
#if NW_PLAYER_DIAGNOSTICS
        if (timestamping) {
            void* timestamps = nullptr;
            D3D12_RANGE range{0u, 2u * sizeof(UINT64)};
            if (SUCCEEDED(state->timestamp_readback->Map(0u, &range,
                                                         &timestamps)) &&
                timestamps != nullptr) {
                const auto* values =
                    static_cast<const UINT64*>(timestamps);
                if (values[1] > values[0]) {
                    instrumentation_.SetGpuTiming(
                        (1000.0 * static_cast<double>(values[1] - values[0])) /
                        static_cast<double>(state->timestamp_frequency));
                }
                D3D12_RANGE empty{0u, 0u};
                state->timestamp_readback->Unmap(0u, &empty);
            }
        }
#endif
    }
    if (CheckDeviceRemoved("renderAtTick")) {
        return ExitCode::DeviceRemoved;
    }
    if (present_requested && state->swapchain != nullptr) {
        const HRESULT present_hr = state->swapchain->Present(1u, 0u);
        if (present_hr == DXGI_STATUS_OCCLUDED) {
            return Fail(ExitCode::PresentationOccluded, "presentation",
                        "present_occluded", "visible_present", "available",
                        "occluded", "renderAtTick",
                        "DXGI_STATUS_OCCLUDED cannot certify a visible frame");
        }
        if (FAILED(present_hr)) {
            if (FAILED(state->device->GetDeviceRemovedReason())) {
                return Fail(ExitCode::DeviceRemoved, "device",
                            "device_removed", "device", "available", "removed",
                            "renderAtTick",
                            "Present failed after device removal");
            }
            return Fail(ExitCode::PresentationFailed, "presentation",
                        "present_failed", "present", "succeeded", "failed",
                        "renderAtTick", "IDXGISwapChain3::Present failed");
        }
        if (CheckD3D12Validation("renderAtTick_present")) {
            return last_exit_;
        }
    }
#else
    // Host simulation executes the same ordered frame contract without a GPU.
#if NW_PLAYER_DIAGNOSTICS
    instrumentation_.BeginFrame(tick);
#endif
    if (!ApplyFramePlanForTesting(tick)) {
        return last_exit_;
    }
#if NW_PLAYER_DIAGNOSTICS
    instrumentation_.EndFrame();
#endif
    if (simulate_post_initialization_allocation_) {
        NoteEngineAllocation(64u, 0u, true, true, false);
    }
    if (force_device_removed_) {
        return Fail(ExitCode::DeviceRemoved, "device", "device_removed",
                    "device", "available", "removed", "renderAtTick",
                    "device removal was reported; automatic recovery/replay is "
                    "forbidden");
    }
#endif
    return ExitCode::Success;
}

ExitCode PackagePlayer::initialize(const package::Package& pkg) {
    if (initialized_ || initialization_count_ != 0u) {
        return Fail(ExitCode::InvalidArguments, "lifecycle",
                    "initialization_already_performed", "initialization",
                    "exactly once", "already performed", "initialize",
                    "initialize(package) may run only once per player instance");
    }
    diagnostics_.clear();
    last_exit_ = ExitCode::Success;
    package_ = pkg;
    package_sha256_.clear();
    stats_ = PlayerAllocationStats{};
    plan_ = ResourcePlan{};
    if (!capabilities_overridden_) {
        capabilities_ = QueriedAdapterCapabilities{};
    }
    frame_ops_.clear();
    last_frame_order_.clear();
    initialization_count_ = 0u;
    frames_rendered_ = 0u;
    last_tick_ = 0u;
    next_fence_value_ = 1u;
    timeline_parameter_id_ = 0u;
    timeline_tick_ = 0u;
    timeline_override_active_ = false;
    width_ = config_.width;
    height_ = config_.height;
    minimized_ = false;
    suspended_ = false;
    window_closed_ = false;
    resize_in_progress_ = false;
    shutting_down_ = false;
    frames_in_flight_ =
        std::clamp(config_.frames_in_flight, 1u, kMaxFramesInFlight);

    if ((package_.flags & 1u) == 0u) {
        return Fail(ExitCode::PackageValidationFailed, "package_validation",
                    "package_release_flag_required", "release_flag", "1",
                    std::to_string(package_.flags), "package_contract",
                    "the player executes release .nwp v1 packages only");
    }
    std::vector<package::Diagnostic> package_diagnostics;
    if (!package::ValidatePackage(package_, package_diagnostics,
                                  /*allow_dev=*/false)) {
        for (const auto& package_diagnostic : package_diagnostics) {
            PlayerDiagnostic diagnostic{};
            diagnostic.category = "package_validation";
            diagnostic.code = package_diagnostic.code.empty()
                                  ? "package_validation"
                                  : package_diagnostic.code;
            diagnostic.requirement = package_diagnostic.section.empty()
                                         ? "package"
                                         : package_diagnostic.section;
            diagnostic.required_value = "valid release .nwp v1 package";
            diagnostic.observed_value =
                package_diagnostic.has_offset
                    ? "offset=" + std::to_string(package_diagnostic.offset)
                    : "invalid";
            diagnostic.profile_id = package_.capability.profile_id;
            diagnostic.requirements_digest =
                package_.capability.requirements_digest;
            diagnostic.stage = "package_contract";
            diagnostic.message = package_diagnostic.message;
            diagnostics_.push_back(std::move(diagnostic));
        }
        if (package_diagnostics.empty()) {
            PlayerDiagnostic diagnostic{};
            diagnostic.category = "package_validation";
            diagnostic.code = "package_validation";
            diagnostic.requirement = "package";
            diagnostic.required_value = "valid release .nwp v1 package";
            diagnostic.observed_value = "invalid";
            diagnostic.profile_id = package_.capability.profile_id;
            diagnostic.requirements_digest =
                package_.capability.requirements_digest;
            diagnostic.stage = "package_contract";
            diagnostic.message =
                "package model validation failed without diagnostics";
            diagnostics_.push_back(std::move(diagnostic));
        }
        last_exit_ = ExitCode::PackageValidationFailed;
        return ExitCode::PackageValidationFailed;
    }

    const PackageCapabilityEvaluation exact =
        ValidateEmittedRequirementsAgainstPackage(package_);
    if (!exact.ok()) {
        return FailEvaluation(exact, "package_contract");
    }

    ExitCode code = OpenDeviceAndWindow();
    if (code != ExitCode::Success) {
        ReleasePlatform();
        return code;
    }
    const PackageCapabilityEvaluation capability_result =
        EvaluatePackageCapabilities(package_, capabilities_);
    if (!capability_result.ok()) {
        code = FailEvaluation(capability_result, "capability_validation");
        ReleasePlatform();
        return code;
    }

    const ResourceAllocationQueryFn default_query =
#if defined(_WIN32)
        QueryAdapterAllocationInfo;
    void* const default_user_data = platform_.get();
#else
        SimulateResourceAllocation;
    void* const default_user_data = nullptr;
#endif
    const ResourceAllocationQueryFn query =
        allocation_query_ != nullptr ? allocation_query_ : default_query;
    void* const query_user_data = allocation_query_ != nullptr
                                      ? allocation_user_data_
                                      : default_user_data;
    const ResourcePlanResult plan_result =
        BuildNonAliasedResourcePlan(package_, query, query_user_data);
    if (plan_result.exit_code != ExitCode::Success) {
        code = FailPlan(plan_result);
        ReleasePlatform();
        return code;
    }
    plan_ = plan_result.plan;
    stats_.package_plan_bytes = plan_.total_engine_bytes;

    if (!BuildFrameInstructionPlan()) {
        code = last_exit_;
        ReleasePlatform();
        return code;
    }

    code = CreateSwapChainAndFrames();
    if (code != ExitCode::Success) {
        ReleasePlatform();
        return code;
    }
    code = RunInitializationInstructions();
    if (code != ExitCode::Success) {
        ReleasePlatform();
        return code;
    }
    if (CheckD3D12Validation("initialize")) {
        const ExitCode validation_code = last_exit_;
        ReleasePlatform();
        return validation_code;
    }
    initialized_ = true;
    initialization_count_ = 1u;
    last_exit_ = ExitCode::Success;
    return ExitCode::Success;
}

ExitCode PackagePlayer::initialize(const std::uint8_t* package_bytes,
                                   std::size_t size_bytes) {
    if (initialized_ || initialization_count_ != 0u) {
        return Fail(ExitCode::InvalidArguments, "lifecycle",
                    "initialization_already_performed", "initialization",
                    "exactly once", "already performed", "initialize",
                    "initialize(package) may run only once per player instance");
    }
    const PackageLoadResult loaded =
        LoadPackageFromBytes(package_bytes, size_bytes);
    if (loaded.exit_code != ExitCode::Success) {
        diagnostics_.clear();
        package_ = loaded.package;
        AddFailureDiagnostics(loaded.failures, "package_validation",
                              "package_load");
        last_exit_ = loaded.exit_code;
        return loaded.exit_code;
    }
    const ExitCode code = initialize(loaded.package);
    if (code == ExitCode::Success) {
        package_sha256_ = loaded.package_sha256;
    }
    return code;
}

ExitCode PackagePlayer::initializeFromFile(const char* package_path) {
    if (initialized_ || initialization_count_ != 0u) {
        return Fail(ExitCode::InvalidArguments, "lifecycle",
                    "initialization_already_performed", "initialization",
                    "exactly once", "already performed", "initialize",
                    "initialize(package) may run only once per player instance");
    }
    const PackageLoadResult loaded = LoadPackageFromFile(package_path);
    if (loaded.exit_code != ExitCode::Success) {
        diagnostics_.clear();
        package_ = loaded.package;
        AddFailureDiagnostics(loaded.failures, "package_validation",
                              "package_load");
        last_exit_ = loaded.exit_code;
        return loaded.exit_code;
    }
    const ExitCode code = initialize(loaded.package);
    if (code == ExitCode::Success) {
        package_sha256_ = loaded.package_sha256;
    }
    return code;
}

ExitCode PackagePlayer::renderAtTick(std::uint64_t tick) {
    if (!initialized_) {
        return Fail(ExitCode::InvalidArguments, "lifecycle",
                    "render_before_initialize", "initialized", "true", "false",
                    "renderAtTick",
                    "renderAtTick requires initialize(package) first");
    }
    if (window_closed_) {
        return ExitCode::Success;
    }
    if (config_.create_window && !config_.headless) {
        if (!platform_->window.PumpMessages()) {
            window_closed_ = true;
            return ExitCode::Success;
        }
        std::uint32_t client_width = 0u;
        std::uint32_t client_height = 0u;
        platform_->window.GetClientSize(&client_width, &client_height);
        if (!platform_->window.IsMinimized() &&
            (client_width != width_ || client_height != height_) &&
            client_width > 0u && client_height > 0u) {
            const ExitCode resize_code = resize(client_width, client_height);
            if (resize_code != ExitCode::Success) return resize_code;
        }
        if (platform_->window.IsMinimized()) {
            minimized_ = true;
            suspended_ = true;
            return ExitCode::Success;
        }
    }
    if (minimized_ || suspended_ || width_ == 0u || height_ == 0u) {
        suspended_ = true;
        return ExitCode::Success;
    }
    if (CheckDeviceRemoved("renderAtTick")) {
        return ExitCode::DeviceRemoved;
    }
    const std::uint64_t allocation_calls_before_frame = stats_.allocation_calls;
    const ExitCode code = ExecuteFrameInstructions(tick);
    if (stats_.allocation_calls != allocation_calls_before_frame) {
        // Any engine allocation tracked while rendering is a violation; the
        // render path must not grow heap or descriptor state.
        stats_.post_initialization_allocation_violation = true;
    }
    if (code != ExitCode::Success) {
        if (last_exit_ != code) {
            (void)Fail(code, ExitCodeCategory(code),
                       "frame_execution_failure_without_diagnostic", "stage",
                       "success", ExitCodeName(code), "renderAtTick",
                       "frame execution failed without a structured diagnostic");
        }
        last_exit_ = code;
        return code;
    }
    if (CheckD3D12Validation("renderAtTick")) {
        return last_exit_;
    }
#if NW_PLAYER_DIAGNOSTICS
    EngineMemoryAccounting accounting{};
    accounting.engine_resource_bytes = stats_.engine_resource_bytes;
    accounting.engine_upload_bytes = stats_.engine_upload_bytes;
    accounting.engine_descriptor_count = stats_.engine_descriptor_count;
    accounting.engine_heap_count = stats_.engine_heap_count;
    instrumentation_.SetEngineAccounting(accounting);
    std::uint64_t gpu_local_usage = 0u;
#if defined(_WIN32)
    if (platform_ != nullptr) {
        gpu_local_usage = platform_->gpu_local_usage_bytes;
    }
#endif
    instrumentation_.SetProcessTotals(QueryProcessPrivateBytes(),
                                      gpu_local_usage);
    instrumentation_.Capture(tick);
#endif
    last_tick_ = tick;
    ++frames_rendered_;
    stats_.frames_rendered = frames_rendered_;
    last_exit_ = ExitCode::Success;
    return ExitCode::Success;
}

ExitCode PackagePlayer::resize(std::uint32_t width, std::uint32_t height) {
    if (!initialized_) {
        return Fail(ExitCode::InvalidArguments, "lifecycle",
                    "resize_before_initialize", "initialized", "true", "false",
                    "resize", "resize requires initialize(package) first");
    }
    if (width == 0u || height == 0u) {
        width_ = 0u;
        height_ = 0u;
        minimized_ = true;
        suspended_ = true;
        if (platform_ != nullptr) {
            platform_->window.RequestClientSize(0u, 0u);
        }
        return ExitCode::Success;
    }
    resize_in_progress_ = true;
    const ExitCode wait = WaitForAllFrames("resize_wait");
    if (wait != ExitCode::Success) {
        resize_in_progress_ = false;
        return wait;
    }
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state != nullptr && state->swapchain != nullptr && !config_.headless) {
        for (ID3D12Resource* backbuffer : state->backbuffers) {
            if (backbuffer != nullptr) backbuffer->Release();
        }
        state->backbuffers.clear();
        const HRESULT resize_hr = state->swapchain->ResizeBuffers(
            package::kSwapChainBufferCount, width, height,
            DXGI_FORMAT_R8G8B8A8_UNORM, 0u);
        if (FAILED(resize_hr)) {
            resize_in_progress_ = false;
            if (FAILED(state->device->GetDeviceRemovedReason())) {
                return Fail(ExitCode::DeviceRemoved, "device",
                            "device_removed_during_resize", "device",
                            "available", "removed", "resize",
                            "ResizeBuffers failed after device removal");
            }
            return Fail(ExitCode::SwapChainCreationFailed, "swapchain",
                        "resize_buffers_failed", "swapchain", "resized",
                        "failed", "resize", "IDXGISwapChain3::ResizeBuffers failed");
        }
        const ExitCode recreate = RecreateRenderTargets();
        if (recreate != ExitCode::Success) {
            resize_in_progress_ = false;
            return recreate;
        }
        if (CheckD3D12Validation("resize")) {
            resize_in_progress_ = false;
            return last_exit_;
        }
    }
#endif
    width_ = width;
    height_ = height;
    minimized_ = false;
    suspended_ = false;
    if (platform_ != nullptr) {
        platform_->window.RequestClientSize(width, height);
    }
    ++stats_.resize_count;
    NoteEngineAllocation(0u, 0u, false, false, true);
    resize_in_progress_ = false;
    last_exit_ = ExitCode::Success;
    return ExitCode::Success;
}

ExitCode PackagePlayer::shutdown() {
    if (shutting_down_) {
        return ExitCode::Success;
    }
    shutting_down_ = true;
    if (initialized_) {
        (void)WaitForAllFrames("shutdown");
    }
    ReleasePlatform();
    initialized_ = false;
    minimized_ = false;
    suspended_ = false;
    ++shutdown_count_;
    shutting_down_ = false;
    return ExitCode::Success;
}

void PackagePlayer::ReleasePlatform() noexcept {
    if (platform_ == nullptr) {
        return;
    }
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state->fence != nullptr && state->fence_event != nullptr) {
        for (auto& frame : state->frames) {
            if (frame.fence_value != 0u) {
                (void)WaitForFenceValue(state->fence, frame.fence_value,
                                        state->fence_event, kFenceTimeoutMs);
                frame.fence_value = 0u;
            }
        }
    }
    for (auto& frame : state->frames) {
        if (frame.upload != nullptr) frame.upload->Release();
        if (frame.command_list != nullptr) frame.command_list->Release();
        if (frame.allocator != nullptr) frame.allocator->Release();
    }
    state->frames.clear();
    for (auto& slot : state->resources) {
        if (slot.upload != nullptr) slot.upload->Release();
        if (slot.resource != nullptr) slot.resource->Release();
    }
    state->resources.clear();
    for (auto& pipeline : state->pipelines) {
        if (pipeline.pipeline_state != nullptr) {
            pipeline.pipeline_state->Release();
        }
    }
    state->pipelines.clear();
    for (auto& root : state->root_signatures) {
        if (root.root_signature != nullptr) root.root_signature->Release();
    }
    state->root_signatures.clear();
    for (ID3D12Resource* backbuffer : state->backbuffers) {
        if (backbuffer != nullptr) backbuffer->Release();
    }
    state->backbuffers.clear();
    if (state->timestamp_readback != nullptr) {
        state->timestamp_readback->Release();
    }
    if (state->timestamp_heap != nullptr) state->timestamp_heap->Release();
    if (state->rtv_heap != nullptr) state->rtv_heap->Release();
    if (state->cbv_srv_uav_heap != nullptr) {
        state->cbv_srv_uav_heap->Release();
    }
    if (state->sampler_heap != nullptr) state->sampler_heap->Release();
    if (state->swapchain != nullptr) state->swapchain->Release();
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    if (state->info_queue != nullptr) state->info_queue->Release();
#endif
    if (state->fence_event != nullptr) CloseHandle(state->fence_event);
    if (state->fence != nullptr) state->fence->Release();
    if (state->queue != nullptr) state->queue->Release();
    if (state->device != nullptr) state->device->Release();
    if (state->adapter != nullptr) state->adapter->Release();
    if (state->factory != nullptr) state->factory->Release();
#endif
    platform_->window.Destroy();
    platform_.reset();
}

std::string PackagePlayer::diagnostics_json() const {
    std::string json = "[";
    for (std::size_t index = 0u; index < diagnostics_.size(); ++index) {
        const PlayerDiagnostic& diagnostic = diagnostics_[index];
        if (index != 0u) json += ",";
        json += "{\"category\":\"" + JsonEscape(diagnostic.category) + "\"";
        json += ",\"code\":\"" + JsonEscape(diagnostic.code) + "\"";
        json += ",\"requirement\":\"" + JsonEscape(diagnostic.requirement) +
                "\"";
        json += ",\"required_value\":\"" +
                JsonEscape(diagnostic.required_value) + "\"";
        json += ",\"observed_value\":\"" +
                JsonEscape(diagnostic.observed_value) + "\"";
        json += ",\"profile_id\":\"" + JsonEscape(diagnostic.profile_id) +
                "\"";
        json += ",\"requirements_digest\":\"" +
                JsonEscape(diagnostic.requirements_digest) + "\"";
        json += ",\"stage\":\"" + JsonEscape(diagnostic.stage) + "\"";
        json += ",\"message\":\"" + JsonEscape(diagnostic.message) + "\"}";
    }
    json += "]";
    return json;
}

int PackagePlayer::Report(bool force_diagnostic_output) const noexcept {
#if defined(NW_PROFILE_DEV) && NW_PROFILE_DEV
    constexpr bool kProfileDev = true;
#else
    constexpr bool kProfileDev = false;
#endif
    const int exit_code = static_cast<int>(last_exit_);
    if ((kProfileDev || force_diagnostic_output) &&
        last_exit_ != ExitCode::Success) {
        std::fprintf(stderr, "NW_PLAYER_DIAGNOSTICS=%s\n",
                     diagnostics_json().c_str());
        for (const auto& diagnostic : diagnostics_) {
            std::fprintf(
                stderr,
                "[NW-DIAG][category=%s][code=%s][stage=%s][profile_id=%s]"
                "[requirements_digest=%s][requirement=%s][required=%s]"
                "[observed=%s] detail=\"%s\" recovery=\"%s\"\n",
                diagnostic.category.c_str(), diagnostic.code.c_str(),
                diagnostic.stage.c_str(), diagnostic.profile_id.c_str(),
                diagnostic.requirements_digest.c_str(),
                diagnostic.requirement.c_str(),
                diagnostic.required_value.c_str(),
                diagnostic.observed_value.c_str(),
                diagnostic.message.c_str(),
                ExitCodeRecoverySemantics(last_exit_));
        }
        std::fflush(stderr);
        if (!diagnostics_.empty()) {
            DiagnosticRecord record{};
            record.exit_code = last_exit_;
            record.stage = diagnostics_.front().stage.c_str();
            record.detail = diagnostics_.front().message.c_str();
            (void)ReportAndConvertExitCode(record,
                                           force_diagnostic_output);
        }
    }
    return exit_code;
}

void PackagePlayer::SetQueriedCapabilitiesForTesting(
    const QueriedAdapterCapabilities& capabilities) {
    capabilities_ = capabilities;
    capabilities_overridden_ = true;
}

void PackagePlayer::SetResourceAllocationQueryForTesting(
    ResourceAllocationQueryFn query, void* user_data) {
    allocation_query_ = query;
    allocation_user_data_ = user_data;
}

void PackagePlayer::SetForceDeviceRemovedForTesting(bool removed) noexcept {
    force_device_removed_ = removed;
}

void PackagePlayer::SetForceRootSignatureCreationFailureForTesting(
    bool fail) noexcept {
    force_root_signature_creation_failure_ = fail;
}

void PackagePlayer::SetForcePipelineCreationFailureForTesting(
    bool fail) noexcept {
    force_pipeline_creation_failure_ = fail;
}

void PackagePlayer::SetSimulatePostInitializationAllocationForTesting(
    bool allocate) noexcept {
    simulate_post_initialization_allocation_ = allocate;
}

bool PackagePlayer::BuildFramePlanForTesting(const package::Package& pkg) {
    diagnostics_.clear();
    last_exit_ = ExitCode::Success;
    package_ = pkg;
    last_root_constants_.fill(0u);
    last_root_constant_count_ = 0u;
    timeline_parameter_id_ = 0u;
    timeline_tick_ = 0u;
    timeline_override_active_ = false;
    return BuildFrameInstructionPlan();
}

bool PackagePlayer::ApplyFramePlanForTesting(std::uint64_t tick) {
    bool timeline_override = timeline_override_active_;
    std::uint32_t timeline_parameter = timeline_parameter_id_;
    std::uint64_t timeline_value = timeline_tick_;
    last_root_constants_.fill(0u);
    last_root_constant_count_ = 0u;
    for (const auto& op : frame_ops_) {
        if (op.type == FrameOp::Type::SetTimeline) {
            timeline_override = true;
            timeline_parameter = op.parameter_id;
            timeline_value =
                op.tick_value >
                        std::numeric_limits<std::uint64_t>::max() - tick
                    ? std::numeric_limits<std::uint64_t>::max()
                    : op.tick_value + tick;
            timeline_parameter_id_ = timeline_parameter;
            timeline_tick_ = timeline_value;
            timeline_override_active_ = true;
        } else if (op.type == FrameOp::Type::SetRootConstants &&
                   op.constant_count > 0u) {
            std::array<std::uint32_t,
                       package::kMaxRootSignatureDwordCost>
                values = op.constants;
            bool timeline_applied = false;
            if (timeline_override) {
                for (std::uint32_t entry = 0u; entry < op.parameter_count;
                     ++entry) {
                    if (op.parameter_ids[entry] != timeline_parameter) {
                        continue;
                    }
                    WriteTimelineTick(
                        values.data(),
                        static_cast<std::uint32_t>(
                            op.parameter_offsets[entry]),
                        op.parameter_sizes[entry],
                        op.parameter_tick_kinds[entry], timeline_value);
                    timeline_applied = true;
                    break;
                }
            }
            if (!timeline_applied && op.time_byte_offset >= 0 &&
                static_cast<std::uint32_t>(op.time_byte_offset) + 4u <=
                    op.constant_count * 4u) {
                const float time_value = static_cast<float>(tick);
                std::memcpy(
                    reinterpret_cast<std::uint8_t*>(values.data()) +
                        op.time_byte_offset,
                    &time_value, sizeof(time_value));
            }
            last_root_constants_ = values;
            last_root_constant_count_ = op.constant_count;
        }
    }
    return true;
}

#if NW_PLAYER_DIAGNOSTICS
void PackagePlayer::SetCaptureHook(CaptureHook hook,
                                   void* user_data) noexcept {
    instrumentation_.SetCaptureHook(hook, user_data);
}
#endif

} // namespace nw::player
