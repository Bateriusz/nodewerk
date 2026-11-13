#include "package_player.h"

#include "diagnostics.h"
#include "package_loader.h"
#include "platform/win32_window.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <set>
#include <string>
#include <utility>
#include <vector>

#ifndef NW05_COMPILE_WAVE_OPS_QUERY
#define NW05_COMPILE_WAVE_OPS_QUERY 1
#endif
#ifndef NW05_COMPILE_INT64_SHADER_OPS_QUERY
#define NW05_COMPILE_INT64_SHADER_OPS_QUERY 1
#endif
#ifndef NW05_COMPILE_FP16_SHADER_OPS_QUERY
#define NW05_COMPILE_FP16_SHADER_OPS_QUERY 1
#endif

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

namespace nw::shaders {
#include "display_fixture_dxil.h"
} // namespace nw::shaders
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

// Read one serialized D3D12 root parameter out of an RTS0 payload.  The
// package planner needs the *actual* root-constant width of the referenced
// parameter instead of accepting the package-wide 16-DWORD ceiling.
bool ReadRootSignatureParameterType(
    const package::RootSignatureRecord& record,
    std::uint32_t root_parameter,
    std::uint32_t* parameter_type,
    std::uint32_t* constant_dwords) {
    if (parameter_type == nullptr || constant_dwords == nullptr) return false;
    *parameter_type = 0xFFFFFFFFu;
    *constant_dwords = 0u;
    const std::vector<std::uint8_t>& blob = record.blob;
    if (blob.size() < 44u || std::memcmp(blob.data(), "DXBC", 4u) != 0) {
        return false;
    }
    std::uint16_t major = 0u;
    std::uint16_t minor = 0u;
    std::uint32_t total_size = 0u;
    std::uint32_t chunk_count = 0u;
    std::memcpy(&major, blob.data() + 20u, sizeof(major));
    std::memcpy(&minor, blob.data() + 22u, sizeof(minor));
    std::memcpy(&total_size, blob.data() + 24u, sizeof(total_size));
    std::memcpy(&chunk_count, blob.data() + 28u, sizeof(chunk_count));
    if (major != 1u || minor != 0u || total_size != blob.size() ||
        chunk_count == 0u || chunk_count > 64u ||
        32u + chunk_count * 4u > blob.size()) {
        return false;
    }
    for (std::uint32_t index = 0u; index < chunk_count; ++index) {
        std::uint32_t chunk_offset = 0u;
        std::memcpy(&chunk_offset, blob.data() + 32u + index * 4u,
                    sizeof(chunk_offset));
        if (chunk_offset > blob.size() || blob.size() - chunk_offset < 8u) {
            return false;
        }
        std::uint32_t chunk_size = 0u;
        std::memcpy(&chunk_size, blob.data() + chunk_offset + 4u,
                    sizeof(chunk_size));
        if (chunk_size > blob.size() - chunk_offset - 8u) return false;
        if (std::memcmp(blob.data() + chunk_offset, "RTS0", 4u) != 0) {
            continue;
        }
        const std::uint8_t* payload = blob.data() + chunk_offset + 8u;
        const std::size_t payload_size = chunk_size;
        if (payload_size < 24u) return false;
        std::uint32_t version = 0u;
        std::uint32_t parameter_count = 0u;
        std::uint32_t parameter_offset = 0u;
        std::uint32_t static_sampler_count = 0u;
        std::uint32_t flags = 0u;
        std::memcpy(&version, payload, sizeof(version));
        std::memcpy(&parameter_count, payload + 4u, sizeof(parameter_count));
        std::memcpy(&parameter_offset, payload + 8u,
                    sizeof(parameter_offset));
        std::memcpy(&static_sampler_count, payload + 12u,
                    sizeof(static_sampler_count));
        std::memcpy(&flags, payload + 20u, sizeof(flags));
        if ((version != 1u && version != 2u) || flags != 0u ||
            static_sampler_count != 0u ||
            root_parameter >= parameter_count) {
            return false;
        }
        const std::uint64_t row_offset =
            static_cast<std::uint64_t>(parameter_offset) +
            static_cast<std::uint64_t>(root_parameter) * 12u;
        if (row_offset + 12u > payload_size) return false;
        std::uint32_t type = 0u;
        std::uint32_t data_offset = 0u;
        std::memcpy(&type, payload + row_offset, sizeof(type));
        std::memcpy(&data_offset, payload + row_offset + 8u,
                    sizeof(data_offset));
        *parameter_type = type;
        if (type == 1u) {
            if (static_cast<std::uint64_t>(data_offset) + 12u > payload_size) {
                return false;
            }
            std::uint32_t dwords = 0u;
            std::memcpy(&dwords, payload + data_offset + 8u, sizeof(dwords));
            *constant_dwords = dwords;
        }
        return true;
    }
    return false;
}

const package::RootSignatureRecord* FindRootSignatureRecord(
    const package::Package& pkg, std::uint32_t root_signature_id) noexcept {
    for (const auto& record : pkg.root_signatures) {
        if (record.root_signature_id == root_signature_id) return &record;
    }
    return nullptr;
}

const package::RootBinding* FindRootBinding(
    const package::RootSignatureRecord& record,
    std::uint32_t root_parameter) noexcept {
    for (const auto& binding : record.bindings) {
        if (binding.root_parameter == root_parameter) return &binding;
    }
    return nullptr;
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
        // UAV ordering is explicit: two dependent dispatches that leave the
        // resource in UNORDERED_ACCESS still need a UAV barrier between them.
        bool uav_accessed_in_frame = false;
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

    // Package-driven fullscreen display.  The display source is the UAV
    // resource referenced by frame.present; the player-owned compute shader
    // blits it into an offscreen texture which is copied to the back buffer.
    ID3D12RootSignature* display_root_signature = nullptr;
    ID3D12PipelineState* display_pipeline = nullptr;
    ID3D12Resource* display_texture = nullptr;
    std::uint64_t display_texture_allocation_bytes = 0u;
    D3D12_RESOURCE_STATES display_texture_state =
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    D3D12_CPU_DESCRIPTOR_HANDLE display_uav_cpu{};
    D3D12_GPU_DESCRIPTOR_HANDLE display_uav_gpu{};
    std::uint32_t display_uav_descriptor_index = 0xFFFFFFFFu;

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
    // Readback destinations whose fence completion could not be proven are
    // released only after shutdown's best-effort GPU drain.
    std::vector<std::pair<ID3D12Resource*, std::uint64_t>> deferred_readbacks;
    // Adapter-specific placed heap proving the alias plan.  Null when the
    // player falls back to committed resources (non-Windows simulation).
    void* package_heap = nullptr;
    std::vector<RootSlot> root_signatures;
    std::vector<PipelineSlot> pipelines;
    std::vector<FrameSlot> frames;

    ID3D12Fence* fence = nullptr;
    HANDLE fence_event = nullptr;

#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    ID3D12InfoQueue* info_queue = nullptr;
    ID3D12InfoQueue1* info_queue1 = nullptr;
    DWORD info_queue_callback_cookie = 0u;
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
void D3D12ImmediateMessageCallback(
    D3D12_MESSAGE_CATEGORY category,
    D3D12_MESSAGE_SEVERITY severity,
    D3D12_MESSAGE_ID id,
    LPCSTR description,
    void*) {
    if (severity != D3D12_MESSAGE_SEVERITY_CORRUPTION &&
        severity != D3D12_MESSAGE_SEVERITY_ERROR &&
        severity != D3D12_MESSAGE_SEVERITY_WARNING) {
        return;
    }
    std::fprintf(
        stderr,
        "NW_D3D12_MESSAGE=category=%d severity=%d id=%d description=%s\n",
        static_cast<int>(category), static_cast<int>(severity),
        static_cast<int>(id), description != nullptr ? description : "");
    std::fflush(stderr);
}

void TraceD3D12ValidationStage(bool enabled, const char* stage) {
    if (!enabled) return;
    std::fprintf(stderr, "NW_D3D12_STAGE=%s\n",
                 stage != nullptr ? stage : "unknown");
    std::fflush(stderr);
}

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

std::uint64_t QueryTexture2DAllocationBytes(
    ID3D12Device* device,
    std::uint32_t width,
    std::uint32_t height,
    D3D12_RESOURCE_FLAGS flags) noexcept {
    if (device == nullptr || width == 0u || height == 0u) return 0u;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = 1u;
    desc.MipLevels = 1u;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1u;
    desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    desc.Flags = flags;
    const D3D12_RESOURCE_ALLOCATION_INFO info =
        device->GetResourceAllocationInfo(0u, 1u, &desc);
    if (info.SizeInBytes == 0u || info.SizeInBytes == UINT64_MAX ||
        info.Alignment == 0u) {
        return UINT64_MAX;
    }
    return info.SizeInBytes;
}

std::uint64_t QuerySwapChainAllocationBytes(
    PlatformState* state,
    std::uint32_t width,
    std::uint32_t height) noexcept {
    if (state == nullptr || state->device == nullptr ||
        state->swapchain == nullptr || width == 0u || height == 0u) {
        return 0u;
    }
    const std::uint64_t one = QueryTexture2DAllocationBytes(
        state->device, width, height, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET);
    if (one == UINT64_MAX ||
        one > std::numeric_limits<std::uint64_t>::max() /
                  package::kSwapChainBufferCount) {
        return UINT64_MAX;
    }
    return one * package::kSwapChainBufferCount;
}

D3D12_HEAP_TYPE HeapTypeForUsage(const package::ResourceDescriptor& resource) {
    if (resource.usage == "readback") {
        return D3D12_HEAP_TYPE_READBACK;
    }
    if (resource.usage == "upload") {
        return D3D12_HEAP_TYPE_UPLOAD;
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
    stats_.engine_cumulative_resource_bytes += bytes;
    stats_.engine_upload_bytes += bytes_upload;
    if (descriptor) ++stats_.engine_descriptor_count;
    if (heap) ++stats_.engine_heap_count;
}

void PackagePlayer::RemoveEngineResourceAllocation(
    std::uint64_t bytes) noexcept {
    stats_.engine_resource_bytes =
        bytes > stats_.engine_resource_bytes
            ? 0u
            : stats_.engine_resource_bytes - bytes;
}

ExitCode PackagePlayer::CheckMemoryBudget(const char* stage) {
    if (config_.memory_budget_bytes == 0u) return ExitCode::Success;
    std::uint64_t package_descriptors = 0u;
    for (const auto& resource : package_.resources) {
        if (resource.usage == "sampler" ||
            ResourceNeedsCbvSrvUavDescriptor(package_, resource)) {
            ++package_descriptors;
        }
    }
    const std::uint64_t descriptor_bytes =
        std::max<std::uint64_t>(stats_.engine_descriptor_count,
                                package_descriptors) *
        64u;
    std::uint64_t backbuffer_bytes = 0u;
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state != nullptr && state->swapchain != nullptr &&
        !config_.headless && width_ > 0u && height_ > 0u) {
        backbuffer_bytes =
            QuerySwapChainAllocationBytes(state, width_, height_);
        if (backbuffer_bytes == UINT64_MAX) {
            return Fail(
                ExitCode::ResourceAllocationFailed, "memory",
                "memory_budget_allocation_query_failed",
                "swapchain_allocation", "queryable", "unavailable", stage,
                "could not query adapter-specific swap-chain allocation size");
        }
    }
#endif
    const std::uint64_t accounted =
        stats_.engine_resource_bytes + stats_.engine_upload_bytes +
        descriptor_bytes + backbuffer_bytes;
    if (accounted <= config_.memory_budget_bytes) return ExitCode::Success;
    return Fail(
        ExitCode::ResourceAllocationFailed, "memory",
        "memory_budget_exceeded", "peak_bytes",
        std::to_string(config_.memory_budget_bytes),
        std::to_string(accounted), stage,
        "accounted peak memory exceeds the generated artifact budget");
}

bool PackagePlayer::BuildFrameInstructionPlan() {
    frame_ops_.clear();
    last_frame_order_.clear();
    frame_ops_.reserve(package_.frame.size());
    last_frame_order_.reserve(package_.frame.size());
    // A timeline selection is only effective if a later root-constant
    // instruction can consume it. Track unresolved selections in frame order.
    std::set<std::uint32_t> pending_timeline_parameters;
    bool has_active_root_signature = false;
    std::uint32_t active_root_signature_id = 0u;
    std::uint32_t active_descriptor_resource = 0u;
    present_resource_id_ = 0u;
    present_source_width_ = 0u;
    present_source_height_ = 0u;
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
            if (instruction.operands[0].type !=
                package::OperandType::RootSignatureRef) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand", "operand_type",
                     "root_signature_ref",
                     std::string(
                         package::ToString(instruction.operands[0].type)),
                     "frame_plan",
                     "frame.set_root_signature operand type is invalid");
                return false;
            }
            if (FindRootSignatureRecord(package_, op.root_parameter) ==
                nullptr) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_reference", "root_signature_id",
                     std::to_string(op.root_parameter), "missing",
                     "frame_plan",
                     "frame.set_root_signature references a missing root "
                     "signature");
                return false;
            }
            has_active_root_signature = true;
            active_root_signature_id = op.root_parameter;
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
            op.constant_count = instruction.operands[2].u32;
            if (instruction.operands[0].type != package::OperandType::U32 ||
                instruction.operands[1].type !=
                    package::OperandType::ResourceRef ||
                instruction.operands[2].type != package::OperandType::U32) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand", "operand_types",
                     "u32,resource_ref,u32", "invalid", "frame_plan",
                     "frame.set_root_constants operand types are invalid");
                return false;
            }
            const package::RootSignatureRecord* root_record =
                has_active_root_signature
                    ? FindRootSignatureRecord(package_,
                                              active_root_signature_id)
                    : nullptr;
            const package::RootBinding* binding =
                root_record == nullptr
                    ? nullptr
                    : FindRootBinding(*root_record, op.root_parameter);
            if (binding == nullptr || binding->usage != "cbv") {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_root_constant_target",
                     "root_parameter", std::to_string(op.root_parameter),
                     binding == nullptr ? "not a root-constant parameter"
                                        : binding->usage,
                     "frame_plan",
                     "frame.set_root_constants must target the active root "
                     "signature's root-constant parameter");
                return false;
            }
            const package::ResourceDescriptor* constants_resource =
                FindPackageResource(package_, op.resource_a);
            if (constants_resource == nullptr ||
                constants_resource->usage != "cbv" ||
                constants_resource->binding_slot != binding->slot ||
                constants_resource->binding_space != binding->space) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_root_constant_resource", "resource_ref",
                     std::to_string(op.resource_a),
                     constants_resource == nullptr
                         ? "missing"
                         : constants_resource->name,
                     "frame_plan",
                     "frame.set_root_constants references a resource that "
                     "does not match the active root signature binding");
                return false;
            }
            std::uint32_t parameter_type = 0xFFFFFFFFu;
            std::uint32_t allowed_dwords = 0u;
            if (!ReadRootSignatureParameterType(*root_record, op.root_parameter,
                                                &parameter_type,
                                                &allowed_dwords) ||
                parameter_type != 1u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_root_constant_signature", "root_parameter",
                     std::to_string(op.root_parameter),
                     "not a 32-bit constants root parameter", "frame_plan",
                     "active root signature does not declare a 32-bit "
                     "constants parameter at this index");
                return false;
            }
            if (op.constant_count > allowed_dwords ||
                op.constant_count > package::kMaxRootSignatureDwordCost) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_root_constant_bound", "constant_count",
                     std::to_string(allowed_dwords),
                     std::to_string(op.constant_count), "frame_plan",
                     "frame.set_root_constants exceeds the active root "
                     "signature's 32-bit constants width");
                return false;
            }
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
            active_descriptor_resource = op.resource_a;
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
            const package::RootSignatureRecord* root_record =
                has_active_root_signature
                    ? FindRootSignatureRecord(package_,
                                              active_root_signature_id)
                    : nullptr;
            const package::RootBinding* binding =
                root_record == nullptr
                    ? nullptr
                    : FindRootBinding(*root_record, op.root_parameter);
            const bool binding_usage_matches =
                binding != nullptr &&
                (sampler_table ? binding->usage == "sampler"
                               : binding->usage == "uav" ||
                                     binding->usage == "srv");
            if (!binding_usage_matches || binding->slot != resource->binding_slot ||
                binding->space != resource->binding_space) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_descriptor_table_binding", "root_parameter",
                     std::to_string(op.root_parameter),
                     binding == nullptr ? "missing root binding"
                                        : binding->usage + ":" +
                                              std::to_string(binding->slot) +
                                              ":" +
                                              std::to_string(binding->space),
                     "frame_plan",
                     "frame.set_descriptor_table does not match the active "
                     "root signature binding");
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
            op.descriptor_resource = active_descriptor_resource;
            if (instruction.operands[0].type != package::OperandType::U32 ||
                instruction.operands[1].type != package::OperandType::U32 ||
                instruction.operands[2].type != package::OperandType::U32 ||
                instruction.operands[3].type !=
                    package::OperandType::ShaderRef) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand", "operand_types",
                     "u32,u32,u32,shader_ref", "invalid", "frame_plan",
                     "frame.dispatch operand types are invalid");
                return false;
            }
            if (op.group_x == 0u || op.group_y == 0u || op.group_z == 0u ||
                op.group_x > 65535u || op.group_y > 65535u ||
                op.group_z > 65535u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_dispatch_bounds", "dispatch_groups",
                     "1..65535",
                     std::to_string(op.group_x) + "x" +
                         std::to_string(op.group_y) + "x" +
                         std::to_string(op.group_z),
                     "frame_plan",
                     "frame.dispatch group dimensions are out of bounds");
                return false;
            }
            const package::ShaderRecord* dispatch_shader = nullptr;
            for (const auto& candidate : package_.shaders) {
                if (candidate.shader_id == op.shader_id) {
                    dispatch_shader = &candidate;
                    break;
                }
            }
            if (dispatch_shader == nullptr) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_reference", "shader_id",
                     std::to_string(op.shader_id), "missing", "frame_plan",
                     "frame.dispatch references a missing shader");
                return false;
            }
            if (!has_active_root_signature ||
                dispatch_shader->root_signature_id !=
                    active_root_signature_id) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_root_signature", "root_signature_id",
                     std::to_string(dispatch_shader->root_signature_id),
                     has_active_root_signature
                         ? std::to_string(active_root_signature_id)
                         : "no active frame.set_root_signature",
                     "frame_plan",
                     "frame.dispatch shader does not match the active root "
                     "signature");
                return false;
            }
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
            if (instruction.operands[0].type !=
                    package::OperandType::ResourceRef ||
                instruction.operands[1].type !=
                    package::OperandType::ResourceRef ||
                instruction.operands[2].type != package::OperandType::U64) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand", "operand_types",
                     "resource_ref,resource_ref,u64", "invalid", "frame_plan",
                     "frame.copy_buffer operand types are invalid");
                return false;
            }
            const package::ResourceDescriptor* copy_source =
                FindPackageResource(package_, op.resource_a);
            const package::ResourceDescriptor* copy_destination =
                FindPackageResource(package_, op.resource_b);
            if (copy_source == nullptr || copy_destination == nullptr) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_reference", "copy_resource",
                     std::to_string(op.resource_a) + "->" +
                         std::to_string(op.resource_b),
                     copy_source == nullptr ? "source missing"
                                            : "destination missing",
                     "frame_plan",
                     "frame.copy_buffer references a missing resource");
                return false;
            }
            if (copy_source == copy_destination) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_copy_self", "copy_resources",
                     "distinct source and destination",
                     std::to_string(op.resource_a), "frame_plan",
                     "frame.copy_buffer must not copy a resource onto itself");
                return false;
            }
            if (copy_source->kind != "buffer" ||
                copy_destination->kind != "buffer") {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_copy_kind", "resource_kind", "buffer",
                     "source=" + copy_source->kind +
                         " destination=" + copy_destination->kind,
                     "frame_plan",
                     "frame.copy_buffer only supports buffer resources");
                return false;
            }
            if (copy_source->usage == "readback") {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_copy_readback_source",
                     "source_usage", "writable or shader-visible resource",
                     "readback", "frame_plan",
                     "readback-heap resources may only be copy destinations");
                return false;
            }
            const std::uint64_t maximum_copy =
                std::min(copy_source->size_bytes,
                         copy_destination->size_bytes);
            if (op.byte_count == 0u || op.byte_count % 4u != 0u ||
                op.byte_count > maximum_copy) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_copy_bounds", "byte_count",
                     "1.." + std::to_string(maximum_copy) + " (multiple of 4)",
                     std::to_string(op.byte_count), "frame_plan",
                     "frame.copy_buffer byte count exceeds the smaller "
                     "resource or is not DWORD aligned");
                return false;
            }
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
            if (instruction.operands[0].type !=
                package::OperandType::ResourceRef) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_instruction_operand", "operand_type",
                     "resource_ref", "invalid", "frame_plan",
                     "frame.present operand type is invalid");
                return false;
            }
            const package::ResourceDescriptor* present_resource =
                FindPackageResource(package_, op.resource_a);
            if (present_resource == nullptr ||
                present_resource->kind != "buffer" ||
                present_resource->usage != "uav") {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_present_target", "present_resource",
                     "UAV buffer package output",
                     present_resource == nullptr
                         ? "missing"
                         : present_resource->kind + ":" +
                               present_resource->usage,
                     "frame_plan",
                     "frame.present must reference a package UAV buffer that "
                     "the fullscreen display pass can read");
                return false;
            }
            if (present_resource->size_bytes < 16u ||
                present_resource->size_bytes % 16u != 0u) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_present_target_size", "size_bytes",
                     "non-zero multiple of 16",
                     std::to_string(present_resource->size_bytes),
                     "frame_plan",
                     "fullscreen display output must contain complete "
                     "RGBA32F pixels");
                return false;
            }
            const std::uint64_t pixel_count =
                present_resource->size_bytes / 16u;
            bool explicit_geometry = false;
            std::uint32_t explicit_width = 0u;
            std::uint32_t explicit_height = 0u;
            for (const auto& parameter : package_.parameters) {
                if (parameter.value.size() < 4u) continue;
                std::uint32_t value = 0u;
                std::memcpy(&value, parameter.value.data(), sizeof(value));
                if (parameter.name == "nw05.present.width") {
                    explicit_width = value;
                } else if (parameter.name == "nw05.present.height") {
                    explicit_height = value;
                }
            }
            if (explicit_width != 0u && explicit_height != 0u &&
                static_cast<std::uint64_t>(explicit_width) * explicit_height <=
                    pixel_count) {
                present_source_width_ = explicit_width;
                present_source_height_ = explicit_height;
                explicit_geometry = true;
            }
            const std::uint32_t side = static_cast<std::uint32_t>(
                std::sqrt(static_cast<double>(pixel_count)));
            if (explicit_geometry) {
                // Explicit package geometry takes precedence over inference.
            } else if (static_cast<std::uint64_t>(side) * side == pixel_count) {
                present_source_width_ = side;
                present_source_height_ = side;
            } else {
                present_source_width_ =
                    static_cast<std::uint32_t>(pixel_count);
                present_source_height_ = 1u;
            }
            if (present_resource_id_ != 0u &&
                present_resource_id_ != op.resource_a) {
                Fail(ExitCode::PackageValidationFailed, "package_validation",
                     "package_present_multiple_targets", "present_resource",
                     std::to_string(present_resource_id_),
                     std::to_string(op.resource_a), "frame_plan",
                     "all frame.present instructions must reference the same "
                     "package display output");
                return false;
            }
            present_resource_id_ = op.resource_a;
        }
        op.instruction_index =
            static_cast<std::uint32_t>(frame_ops_.size());
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
        if ((adapter_desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0u &&
            !config_.allow_software_adapter) {
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
        // Validation is a CI gate, not an interactive debugger.  Do not let
        // the debug layer raise an unhandled 0x87D break exception before the
        // stored message can be converted into D3D12ValidationError (61).
        (void)platform_->info_queue->SetBreakOnSeverity(
            D3D12_MESSAGE_SEVERITY_CORRUPTION, FALSE);
        (void)platform_->info_queue->SetBreakOnSeverity(
            D3D12_MESSAGE_SEVERITY_ERROR, FALSE);

        // Polling InfoQueue after Present is too late for debug-layer failures
        // that terminate the process inside a D3D12 call. Register a callback
        // so warning/error text is flushed to stderr at the point it is
        // produced. The callback never calls back into D3D12.
        if (SUCCEEDED(device->QueryInterface(
                IID_PPV_ARGS(&platform_->info_queue1))) &&
            platform_->info_queue1 != nullptr) {
            const HRESULT callback_hr =
                platform_->info_queue1->RegisterMessageCallback(
                    D3D12ImmediateMessageCallback,
                    D3D12_MESSAGE_CALLBACK_FLAG_NONE, nullptr,
                    &platform_->info_queue_callback_cookie);
            if (FAILED(callback_hr)) {
                platform_->info_queue_callback_cookie = 0u;
                platform_->info_queue1->Release();
                platform_->info_queue1 = nullptr;
            }
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
    // WDDM metadata is evidence-host information, not a runtime capability
    // predicate. The external evidence collector obtains it without expanding
    // the release player's dependency closure.
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

#if NW05_COMPILE_WAVE_OPS_QUERY || NW05_COMPILE_INT64_SHADER_OPS_QUERY
    if (
#if NW05_COMPILE_WAVE_OPS_QUERY
        config_.query_optional_wave_ops ||
#endif
#if NW05_COMPILE_INT64_SHADER_OPS_QUERY
        config_.query_optional_int64_shader_ops ||
#endif
        false) {
        D3D12_FEATURE_DATA_D3D12_OPTIONS1 options1{};
        if (SUCCEEDED(device->CheckFeatureSupport(
                D3D12_FEATURE_D3D12_OPTIONS1, &options1, sizeof(options1)))) {
            queried.optional_features_queried = true;
#if NW05_COMPILE_WAVE_OPS_QUERY
            queried.wave_ops_queried = config_.query_optional_wave_ops;
            queried.wave_ops_supported =
                config_.query_optional_wave_ops && options1.WaveOps != FALSE;
#endif
#if NW05_COMPILE_INT64_SHADER_OPS_QUERY
            queried.int64_shader_ops_queried =
                config_.query_optional_int64_shader_ops;
            queried.int64_shader_ops_supported =
                config_.query_optional_int64_shader_ops &&
                options1.Int64ShaderOps != FALSE;
#endif
        }
    }
#endif
#if NW05_COMPILE_FP16_SHADER_OPS_QUERY
    if (config_.query_optional_fp16_shader_ops) {
        D3D12_FEATURE_DATA_D3D12_OPTIONS4 options4{};
        if (SUCCEEDED(device->CheckFeatureSupport(
                D3D12_FEATURE_D3D12_OPTIONS4, &options4, sizeof(options4)))) {
            queried.optional_features_queried = true;
            queried.fp16_shader_ops_queried = true;
            queried.fp16_shader_ops_supported =
                options4.Native16BitShaderOpsSupported != FALSE;
        }
    }
#endif

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
    if (using_aliased_plan_ && plan_.total_engine_bytes > 0u) {
        std::string detail;
        state->package_heap = CreateAliasedResourceHeap(
            state->device, plan_.total_engine_bytes,
            static_cast<std::uint32_t>(D3D12_HEAP_TYPE_DEFAULT), &detail);
        if (state->package_heap == nullptr) {
            return Fail(ExitCode::ResourceAllocationFailed, "resource",
                        "alias_heap_creation_failed", "alias_heap",
                        "created", detail.empty() ? "failed" : detail,
                        "alias_realization",
                        "adapter-specific alias heap creation failed");
        }
        NoteEngineAllocation(plan_.total_engine_bytes, 0u, false, true, false);
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
    }

    const bool has_present_output = present_resource_id_ != 0u;
    const std::size_t cbv_count =
        CountCbvSrvUavDescriptors(package_) + (has_present_output ? 1u : 0u);
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
        if (has_present_output) {
            state->display_uav_descriptor_index =
                static_cast<std::uint32_t>(
                    CountCbvSrvUavDescriptors(package_));
            state->display_uav_cpu = state->cbv_srv_uav_cpu_start;
            state->display_uav_cpu.ptr +=
                static_cast<SIZE_T>(state->display_uav_descriptor_index) *
                state->cbv_srv_uav_increment;
            state->display_uav_gpu = state->cbv_srv_uav_gpu_start;
            state->display_uav_gpu.ptr +=
                static_cast<UINT64>(state->display_uav_descriptor_index) *
                state->cbv_srv_uav_increment;
        }
    }

    // RecreateRenderTargets() creates the engine-owned fullscreen display
    // texture when frame.present is present, so the reserved display UAV
    // descriptor must exist before this first render-target creation.
    if (RecreateRenderTargets() != ExitCode::Success) {
        return last_exit_;
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

    if (has_present_output) {
        // Fixed, engine-owned fullscreen display root signature.  It consumes
        // the package's raw UAV output and writes the player display texture.
        const std::vector<package::RootBinding> display_bindings = {
            package::RootBinding{"display_params", "cbv", 0u, 0u, 0u},
            package::RootBinding{"display_source", "uav", 0u, 0u, 1u},
            package::RootBinding{"display_target", "uav", 1u, 0u, 2u},
        };
        const std::vector<std::uint8_t> display_root_blob =
            package::MakeRootSignatureBlob("nw04.display.fullscreen",
                                           display_bindings);
        if (display_root_blob.empty() ||
            FAILED(state->device->CreateRootSignature(
                0u, display_root_blob.data(), display_root_blob.size(),
                IID_PPV_ARGS(&state->display_root_signature))) ||
            state->display_root_signature == nullptr) {
            return Fail(ExitCode::RootSignatureCreationFailed, "pipeline",
                        "display_root_signature_failed", "root_signature",
                        "fullscreen display", "creation failed", "swapchain",
                        "could not create the fullscreen display root "
                        "signature");
        }
        D3D12_COMPUTE_PIPELINE_STATE_DESC display_pso{};
        display_pso.pRootSignature = state->display_root_signature;
        display_pso.CS.pShaderBytecode = nw::shaders::kDisplayFixtureDxil;
        display_pso.CS.BytecodeLength =
            sizeof(nw::shaders::kDisplayFixtureDxil);
        if (FAILED(state->device->CreateComputePipelineState(
                &display_pso, IID_PPV_ARGS(&state->display_pipeline))) ||
            state->display_pipeline == nullptr) {
            return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                        "display_pipeline_failed", "pipeline",
                        "fullscreen display", "creation failed", "swapchain",
                        "could not create the fullscreen display pipeline");
        }
    }

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

    if (state->display_texture != nullptr) {
        RemoveEngineResourceAllocation(
            state->display_texture_allocation_bytes);
        state->display_texture_allocation_bytes = 0u;
        state->display_texture->Release();
        state->display_texture = nullptr;
    }
    if (present_resource_id_ != 0u && width_ > 0u && height_ > 0u) {
        if (state->display_uav_cpu.ptr == 0u) {
            return Fail(ExitCode::DescriptorHeapCreationFailed, "descriptor",
                        "display_descriptor_unavailable", "display_uav",
                        "reserved", "unavailable", "swapchain",
                        "fullscreen display descriptor was not reserved");
        }
        D3D12_HEAP_PROPERTIES heap{};
        heap.Type = D3D12_HEAP_TYPE_DEFAULT;
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        desc.Width = width_;
        desc.Height = height_;
        desc.DepthOrArraySize = 1u;
        desc.MipLevels = 1u;
        desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.SampleDesc.Count = 1u;
        desc.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        const D3D12_RESOURCE_ALLOCATION_INFO allocation =
            state->device->GetResourceAllocationInfo(0u, 1u, &desc);
        if (allocation.SizeInBytes == 0u ||
            allocation.SizeInBytes == UINT64_MAX ||
            allocation.Alignment == 0u) {
            return Fail(ExitCode::ResourceAllocationFailed, "resource",
                        "display_texture_allocation_info",
                        "display_texture_allocation_info",
                        "valid size/alignment", "unavailable", "swapchain",
                        "GetResourceAllocationInfo rejected the display "
                        "texture");
        }
        if (FAILED(state->device->CreateCommittedResource(
                &heap, D3D12_HEAP_FLAG_NONE, &desc,
                D3D12_RESOURCE_STATE_COMMON, nullptr,
                IID_PPV_ARGS(&state->display_texture))) ||
            state->display_texture == nullptr) {
            state->display_texture = nullptr;
            return Fail(ExitCode::ResourceAllocationFailed, "resource",
                        "display_texture_failed", "display_texture",
                        "allocated", "allocation failed", "swapchain",
                        "could not allocate the fullscreen display texture");
        }
        state->display_texture_allocation_bytes =
            allocation.SizeInBytes;
        state->display_texture_state = D3D12_RESOURCE_STATE_COMMON;
        NoteEngineAllocation(allocation.SizeInBytes, 0u, false, false,
                             resize_in_progress_);
        D3D12_UNORDERED_ACCESS_VIEW_DESC uav{};
        uav.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        uav.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
        uav.Texture2D.MipSlice = 0u;
        uav.Texture2D.PlaneSlice = 0u;
        state->device->CreateUnorderedAccessView(
            state->display_texture, nullptr, &uav,
            state->display_uav_cpu);
    }
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
                const bool heap_resident =
                    state->package_heap != nullptr &&
                    placement->descriptor.usage != "readback" &&
                    placement->descriptor.usage != "sampler" &&
                    HeapTypeForUsage(placement->descriptor) ==
                        D3D12_HEAP_TYPE_DEFAULT;
                if (heap_resident) {
                    void* placed = nullptr;
                    std::string detail;
                    if (!CreateAliasedPlacedResource(
                            state->package_heap, *placement, &placed,
                            &detail) ||
                        placed == nullptr) {
                        return Fail(
                            ExitCode::ResourceAllocationFailed, "resource",
                            "placed_resource_failed", "resource",
                            placement->descriptor.name,
                            detail.empty() ? "placement failed" : detail,
                            "init.create_resource",
                            "placed-resource creation failed for the proven alias "
                            "offset");
                    }
                    slot->resource = static_cast<ID3D12Resource*>(placed);
                    slot->state = D3D12_RESOURCE_STATE_COMMON;
                } else {
                    if (FAILED(state->device->CreateCommittedResource(
                            &heap, D3D12_HEAP_FLAG_NONE, &desc, initial,
                            nullptr, IID_PPV_ARGS(&slot->resource))) ||
                        slot->resource == nullptr) {
                        return Fail(
                            ExitCode::ResourceAllocationFailed, "resource",
                            "committed_resource_failed", "resource",
                            placement->descriptor.name, "allocation failed",
                            "init.create_resource",
                            "CreateCommittedResource failed");
                    }
                    NoteEngineAllocation(placement->allocation_size_bytes, 0u,
                                         false, false, false);
                    slot->state = initial;
                }
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
                        if (placement->descriptor.kind == "buffer") {
                            // NW-03's compute output is a RWByteAddressBuffer.
                            // D3D12 requires a raw UAV: R32_TYPELESS with the
                            // RAW flag and a zero structure stride.  A typed or
                            // UNKNOWN-format structured view is invalid.
                            view.Format = DXGI_FORMAT_R32_TYPELESS;
                            view.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
                            view.Buffer.FirstElement = 0u;
                            view.Buffer.NumElements = static_cast<UINT>(
                                placement->descriptor.size_bytes /
                                sizeof(std::uint32_t));
                            view.Buffer.StructureByteStride = 0u;
                            view.Buffer.CounterOffsetInBytes = 0u;
                            view.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
                        } else if (placement->descriptor.kind == "texture2d") {
                            view.Format =
                                MapFormat(placement->descriptor.format);
                            if (placement->descriptor.array_size > 1u) {
                                view.ViewDimension =
                                    D3D12_UAV_DIMENSION_TEXTURE2DARRAY;
                                view.Texture2DArray.MipSlice = 0u;
                                view.Texture2DArray.FirstArraySlice = 0u;
                                view.Texture2DArray.ArraySize =
                                    placement->descriptor.array_size;
                                view.Texture2DArray.PlaneSlice = 0u;
                            } else {
                                view.ViewDimension =
                                    D3D12_UAV_DIMENSION_TEXTURE2D;
                                view.Texture2D.MipSlice = 0u;
                                view.Texture2D.PlaneSlice = 0u;
                            }
                        } else {
                            view.Format =
                                MapFormat(placement->descriptor.format);
                            view.ViewDimension =
                                D3D12_UAV_DIMENSION_TEXTURE3D;
                            view.Texture3D.MipSlice = 0u;
                            view.Texture3D.FirstWSlice = 0u;
                            view.Texture3D.WSize =
                                placement->descriptor.depth;
                        }
                        state->device->CreateUnorderedAccessView(
                            slot->resource, nullptr, &view, handle);
                    } else if (placement->descriptor.usage == "srv") {
                        D3D12_SHADER_RESOURCE_VIEW_DESC view{};
                        view.Shader4ComponentMapping =
                            D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
                        if (placement->descriptor.kind == "buffer") {
                            view.Format =
                                MapFormat(placement->descriptor.format);
                            view.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
                            view.Buffer.FirstElement = 0u;
                            view.Buffer.NumElements = static_cast<UINT>(
                                placement->descriptor.size_bytes /
                                std::max<std::uint32_t>(
                                    1u, BytesPerElement(
                                            placement->descriptor.format)));
                        } else if (placement->descriptor.kind == "texture2d") {
                            view.Format =
                                MapFormat(placement->descriptor.format);
                            if (placement->descriptor.array_size > 1u) {
                                view.ViewDimension =
                                    D3D12_SRV_DIMENSION_TEXTURE2DARRAY;
                                view.Texture2DArray.MostDetailedMip = 0u;
                                view.Texture2DArray.MipLevels =
                                    placement->descriptor.mip_levels;
                                view.Texture2DArray.FirstArraySlice = 0u;
                                view.Texture2DArray.ArraySize =
                                    placement->descriptor.array_size;
                                view.Texture2DArray.PlaneSlice = 0u;
                                view.Texture2DArray.ResourceMinLODClamp = 0.0f;
                            } else {
                                view.ViewDimension =
                                    D3D12_SRV_DIMENSION_TEXTURE2D;
                                view.Texture2D.MostDetailedMip = 0u;
                                view.Texture2D.MipLevels =
                                    placement->descriptor.mip_levels;
                                view.Texture2D.PlaneSlice = 0u;
                                view.Texture2D.ResourceMinLODClamp = 0.0f;
                            }
                        } else {
                            view.Format =
                                MapFormat(placement->descriptor.format);
                            view.ViewDimension =
                                D3D12_SRV_DIMENSION_TEXTURE3D;
                            view.Texture3D.MostDetailedMip = 0u;
                            view.Texture3D.MipLevels =
                                placement->descriptor.mip_levels;
                            view.Texture3D.ResourceMinLODClamp = 0.0f;
                        }
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
                                           readback_bytes)};
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
                readback_bytes);
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
    for (auto& slot : state->resources) {
        slot.uav_accessed_in_frame = false;
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
    // Latest package binding state.  frame.present temporarily switches to the
    // player display root signature/pipeline and must restore this state for a
    // following package dispatch.
    ID3D12RootSignature* package_root_signature = nullptr;
    bool package_has_constants = false;
    std::uint32_t package_constant_root = 0u;
    std::uint32_t package_constant_count = 0u;
    std::array<std::uint32_t, package::kMaxRootSignatureDwordCost>
        package_constant_values{};
    std::array<D3D12_GPU_DESCRIPTOR_HANDLE,
               package::kMaxRootSignatureDwordCost>
        package_tables{};
    std::array<bool, package::kMaxRootSignatureDwordCost>
        package_table_valid{};
    std::vector<bool> alias_barrier_emitted(plan_.alias_barriers.size(), false);
    for (const auto& op : frame_ops_) {
        if (state->package_heap != nullptr) {
            for (std::size_t alias_index = 0u;
                 alias_index < plan_.alias_barriers.size(); ++alias_index) {
                if (alias_barrier_emitted[alias_index]) continue;
                const auto& alias = plan_.alias_barriers[alias_index];
                if (alias.activation_instruction != op.instruction_index) {
                    continue;
                }
                if (op.resource_a != alias.resource_id &&
                    op.resource_b != alias.resource_id &&
                    op.descriptor_resource != alias.resource_id) {
                    continue;
                }
                PlatformState::ResourceSlot* before = nullptr;
                PlatformState::ResourceSlot* after = nullptr;
                for (auto& candidate : state->resources) {
                    if (candidate.resource_id == alias.before_resource_id) {
                        before = &candidate;
                    }
                    if (candidate.resource_id == alias.resource_id) {
                        after = &candidate;
                    }
                }
                EmitAliasingBarrier(
                    list, alias, before == nullptr ? nullptr : before->resource,
                    after == nullptr ? nullptr : after->resource);
                ++stats_.alias_barrier_count;
                if (after != nullptr &&
                    after->resource != nullptr) {
                    after->state = D3D12_RESOURCE_STATE_COMMON;
                }
                if (alias.uav_barrier) ++stats_.uav_barrier_count;
                alias_barrier_emitted[alias_index] = true;
            }
        }
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
                // Re-selecting the already active signature preserves root
                // arguments in D3D12.  Only an actual signature change
                // invalidates the cached parameter layout.
                if (package_root_signature != root) {
                    package_has_constants = false;
                    package_constant_root = 0u;
                    package_constant_count = 0u;
                    package_table_valid.fill(false);
                }
                list->SetComputeRootSignature(root);
                package_root_signature = root;
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
                    if (op.root_parameter < package_constant_values.size()) {
                        package_has_constants = true;
                        package_constant_root = op.root_parameter;
                        package_constant_count = op.constant_count;
                        package_constant_values = values;
                    }
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
                if (op.root_parameter < package_tables.size()) {
                    package_tables[op.root_parameter] = handle;
                    package_table_valid[op.root_parameter] = true;
                }
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
                if (after == D3D12_RESOURCE_STATE_UNORDERED_ACCESS) {
                    // A requested UAV state is also an ordering request for
                    // dependent dispatches that remain in UAV state.
                    D3D12_RESOURCE_BARRIER uav_barrier{};
                    uav_barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
                    uav_barrier.UAV.pResource = slot->resource;
                    list->ResourceBarrier(1u, &uav_barrier);
                    ++stats_.uav_barrier_count;
                    slot->uav_accessed_in_frame = true;
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
                        if (working ==
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS &&
                            candidate.state ==
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS &&
                            candidate.uav_accessed_in_frame) {
                            D3D12_RESOURCE_BARRIER uav_barrier{};
                            uav_barrier.Type =
                                D3D12_RESOURCE_BARRIER_TYPE_UAV;
                            uav_barrier.UAV.pResource = candidate.resource;
                            list->ResourceBarrier(1u, &uav_barrier);
                            ++stats_.uav_barrier_count;
                        }
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
                        if (working ==
                            D3D12_RESOURCE_STATE_UNORDERED_ACCESS) {
                            candidate.uav_accessed_in_frame = true;
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
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
                TraceD3D12ValidationStage(config_.enable_validation,
                                          "frame.present.begin");
#endif
                if (state->swapchain == nullptr || !state->swapchain_valid) {
                    break;
                }
                if (state->display_pipeline == nullptr ||
                    state->display_root_signature == nullptr ||
                    state->display_texture == nullptr) {
                    return Fail(ExitCode::PipelineStateCreationFailed,
                                "pipeline", "display_resources_unavailable",
                                "fullscreen display", "initialized",
                                "unavailable", "renderAtTick",
                                "frame.present requires the fullscreen display "
                                "pipeline and target texture");
                }
                if (op.resource_a != present_resource_id_) {
                    return Fail(ExitCode::PackageValidationFailed,
                                "package_validation",
                                "package_present_target_mismatch",
                                "present_resource",
                                std::to_string(present_resource_id_),
                                std::to_string(op.resource_a), "renderAtTick",
                                "frame.present operand does not match the "
                                "validated display output");
                }
                PlatformState::ResourceSlot* source_slot = nullptr;
                for (auto& candidate : state->resources) {
                    if (candidate.resource_id == op.resource_a) {
                        source_slot = &candidate;
                        break;
                    }
                }
                if (source_slot == nullptr ||
                    source_slot->resource == nullptr ||
                    !source_slot->has_descriptor) {
                    return Fail(ExitCode::ResourceAllocationFailed,
                                "resource", "present_source_missing",
                                "present_resource",
                                std::to_string(op.resource_a), "missing",
                                "renderAtTick",
                                "frame.present source has no realized UAV");
                }
                if (source_slot->state !=
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS) {
                    D3D12_RESOURCE_BARRIER to_uav{};
                    to_uav.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                    to_uav.Transition.pResource = source_slot->resource;
                    to_uav.Transition.StateBefore = source_slot->state;
                    to_uav.Transition.StateAfter =
                        D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
                    to_uav.Transition.Subresource =
                        D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                    list->ResourceBarrier(1u, &to_uav);
                    source_slot->state =
                        D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
                }
                if (state->display_texture_state !=
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS) {
                    D3D12_RESOURCE_BARRIER to_uav{};
                    to_uav.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                    to_uav.Transition.pResource = state->display_texture;
                    to_uav.Transition.StateBefore =
                        state->display_texture_state;
                    to_uav.Transition.StateAfter =
                        D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
                    to_uav.Transition.Subresource =
                        D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                    list->ResourceBarrier(1u, &to_uav);
                    state->display_texture_state =
                        D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
                }
                if (source_slot->uav_accessed_in_frame) {
                    // The display pass reads the exact UAV the compute
                    // dispatches wrote; remaining in UAV state still requires
                    // explicit UAV ordering before the dependent read.
                    D3D12_RESOURCE_BARRIER uav_barrier{};
                    uav_barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
                    uav_barrier.UAV.pResource = source_slot->resource;
                    list->ResourceBarrier(1u, &uav_barrier);
                    ++stats_.uav_barrier_count;
                    source_slot->uav_accessed_in_frame = false;
                }
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
                TraceD3D12ValidationStage(config_.enable_validation,
                                          "frame.display.bind.before");
#endif
                list->SetComputeRootSignature(state->display_root_signature);
                const std::array<std::uint32_t, 4> display_constants = {
                    present_source_width_, present_source_height_, width_,
                    height_};
                list->SetComputeRoot32BitConstants(
                    0u, static_cast<UINT>(display_constants.size()),
                    display_constants.data(), 0u);
                list->SetComputeRootDescriptorTable(
                    1u, source_slot->gpu_handle);
                list->SetComputeRootDescriptorTable(
                    2u, state->display_uav_gpu);
                list->SetPipelineState(state->display_pipeline);
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
                TraceD3D12ValidationStage(config_.enable_validation,
                                          "frame.display.dispatch.before");
#endif
                list->Dispatch((width_ + 7u) / 8u, (height_ + 7u) / 8u, 1u);
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
                TraceD3D12ValidationStage(config_.enable_validation,
                                          "frame.display.dispatch.after");
#endif

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
                D3D12_RESOURCE_BARRIER to_copy_source{};
                to_copy_source.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                to_copy_source.Transition.pResource = state->display_texture;
                to_copy_source.Transition.StateBefore =
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
                to_copy_source.Transition.StateAfter =
                    D3D12_RESOURCE_STATE_COPY_SOURCE;
                to_copy_source.Transition.Subresource =
                    D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                list->ResourceBarrier(1u, &to_copy_source);
                state->display_texture_state =
                    D3D12_RESOURCE_STATE_COPY_SOURCE;
                if (state->backbuffer_states[backbuffer_index] !=
                    D3D12_RESOURCE_STATE_COPY_DEST) {
                    D3D12_RESOURCE_BARRIER to_copy_dest{};
                    to_copy_dest.Type =
                        D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                    to_copy_dest.Transition.pResource =
                        state->backbuffers[backbuffer_index];
                    to_copy_dest.Transition.StateBefore =
                        state->backbuffer_states[backbuffer_index];
                    to_copy_dest.Transition.StateAfter =
                        D3D12_RESOURCE_STATE_COPY_DEST;
                    to_copy_dest.Transition.Subresource =
                        D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                    list->ResourceBarrier(1u, &to_copy_dest);
                    state->backbuffer_states[backbuffer_index] =
                        D3D12_RESOURCE_STATE_COPY_DEST;
                }
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
                TraceD3D12ValidationStage(config_.enable_validation,
                                          "frame.display.copy.before");
#endif
                list->CopyResource(state->backbuffers[backbuffer_index],
                                   state->display_texture);
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
                TraceD3D12ValidationStage(config_.enable_validation,
                                          "frame.display.copy.after");
#endif
                if (package_root_signature != nullptr) {
                    list->SetComputeRootSignature(package_root_signature);
                    if (package_has_constants &&
                        package_constant_count > 0u) {
                        list->SetComputeRoot32BitConstants(
                            package_constant_root, package_constant_count,
                            package_constant_values.data(), 0u);
                    }
                    for (std::size_t root_parameter = 0u;
                         root_parameter < package_tables.size();
                         ++root_parameter) {
                        if (package_table_valid[root_parameter]) {
                            list->SetComputeRootDescriptorTable(
                                static_cast<UINT>(root_parameter),
                                package_tables[root_parameter]);
                        }
                    }
                }
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
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    TraceD3D12ValidationStage(config_.enable_validation,
                              "frame.command_list.close.before");
#endif
    if (FAILED(list->Close())) {
        return Fail(ExitCode::CommandListRecordingFailed, "command_list",
                    "command_list_close_failed", "closed", "closed", "failed",
                    "renderAtTick", "ID3D12GraphicsCommandList::Close failed");
    }
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    TraceD3D12ValidationStage(config_.enable_validation,
                              "frame.command_list.close.after");
#endif
    ID3D12CommandList* lists[] = {list};
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    TraceD3D12ValidationStage(config_.enable_validation,
                              "frame.queue.execute.before");
#endif
    state->queue->ExecuteCommandLists(1u, lists);
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
    TraceD3D12ValidationStage(config_.enable_validation,
                              "frame.queue.execute.after");
#endif
#if NW_PLAYER_DIAGNOSTICS
    // CPU submission timing intentionally stops before Present/VSync. The
    // lifetime fence below is separate: it retires the complete frame,
    // including presentation when a swap chain is active.
    instrumentation_.EndFrame();
#endif

    const auto retire_submitted_work = [&]() -> ExitCode {
        const std::uint64_t fence_value = next_fence_value_++;
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
        TraceD3D12ValidationStage(config_.enable_validation,
                                  "frame.queue.retire_signal.before");
#endif
        if (FAILED(state->queue->Signal(state->fence, fence_value))) {
            return Fail(ExitCode::GpuSubmissionFailed, "gpu_execution",
                        "queue_signal_failed", "signal", "succeeded", "failed",
                        "renderAtTick",
                        "ID3D12CommandQueue::Signal failed");
        }
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
        TraceD3D12ValidationStage(config_.enable_validation,
                                  "frame.queue.retire_signal.after");
#endif
        frame.fence_value = fence_value;
        return ExitCode::Success;
    };

    if (CheckDeviceRemoved("renderAtTick")) {
        // The deterministic test seam can report removal while the real device
        // is still operational; retire submitted work in that case so cleanup
        // never releases in-flight resources. A genuinely removed device
        // cannot signal a usable fence and is skipped.
        if (state->device != nullptr &&
            SUCCEEDED(state->device->GetDeviceRemovedReason())) {
            (void)retire_submitted_work();
        }
        return ExitCode::DeviceRemoved;
    }
    if (present_requested && state->swapchain != nullptr) {
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
        TraceD3D12ValidationStage(config_.enable_validation,
                                  "frame.swapchain.present.before");
#endif
        const HRESULT present_hr =
            config_.simulate_presentation_failure
                ? static_cast<HRESULT>(E_FAIL)
                : state->swapchain->Present(1u, 0u);
#if defined(NW_ENABLE_D3D12_VALIDATION) && NW_ENABLE_D3D12_VALIDATION
        TraceD3D12ValidationStage(config_.enable_validation,
                                  "frame.swapchain.present.after");
#endif
        if (present_hr == DXGI_STATUS_OCCLUDED) {
            const ExitCode retirement = retire_submitted_work();
            if (retirement != ExitCode::Success) return retirement;
            ++stats_.present_failure_retirements;
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
            const ExitCode retirement = retire_submitted_work();
            if (retirement != ExitCode::Success) return retirement;
            ++stats_.present_failure_retirements;
            return Fail(ExitCode::PresentationFailed, "presentation",
                        "present_failed", "present", "succeeded", "failed",
                        "renderAtTick", "IDXGISwapChain3::Present failed");
        }
        ++frames_presented_;
        stats_.frames_presented = frames_presented_;
    }

    // Present operations are ordered on the D3D12 queue used to create the
    // swap chain. Signal only after Present so frame retirement also covers
    // presentation's use of the back buffer. A pre-Present signal can complete
    // while DXGI still owns the resource, making resize/shutdown final-release
    // it while presentation is in flight (D3D12 debug-layer message ID 921).
    const ExitCode retirement = retire_submitted_work();
    if (retirement != ExitCode::Success) return retirement;
    const std::uint64_t fence_value = frame.fence_value;

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
    if (CheckD3D12Validation("renderAtTick_present")) {
        return last_exit_;
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

ExitCode PackagePlayer::readbackPresentedOutput(
    std::vector<std::uint8_t>* bytes) {
    if (bytes == nullptr) {
        return Fail(ExitCode::InvalidArguments, "readback",
                    "readback_buffer_missing", "destination", "available",
                    "null", "readback", "readback destination is null");
    }
    bytes->clear();
    if (!initialized_ || present_resource_id_ == 0u) {
        return Fail(ExitCode::InvalidArguments, "readback",
                    "readback_before_initialize", "initialized", "true",
                    "false", "readback",
                    "image readback requires an initialized presented output");
    }
#if defined(_WIN32)
    PlatformState* state = platform_.get();
    if (state == nullptr || state->device == nullptr ||
        state->queue == nullptr || state->frames.empty()) {
        return Fail(ExitCode::ResourceAllocationFailed, "readback",
                    "readback_platform_unavailable", "device/queue",
                    "available", "unavailable", "readback",
                    "image readback requires the native D3D12 device");
    }
    const ExitCode drained = WaitForAllFrames("readback_image");
    if (drained != ExitCode::Success) return drained;
    PlatformState::ResourceSlot* source = nullptr;
    for (auto& candidate : state->resources) {
        if (candidate.resource_id == present_resource_id_) {
            source = &candidate;
            break;
        }
    }
    const ResourcePlacement* placement = plan_.Find(present_resource_id_);
    if (source == nullptr || source->resource == nullptr ||
        placement == nullptr || placement->descriptor.kind != "buffer" ||
        placement->descriptor.size_bytes == 0u) {
        return Fail(ExitCode::ResourceAllocationFailed, "readback",
                    "readback_source_missing", "present_resource",
                    std::to_string(present_resource_id_), "unavailable",
                    "readback",
                    "presented output is not a readable buffer resource");
    }
    std::uint64_t readback_bytes = placement->descriptor.size_bytes;
    if (present_source_width_ != 0u && present_source_height_ != 0u) {
        const std::uint64_t image_bytes =
            static_cast<std::uint64_t>(present_source_width_) *
            present_source_height_ * 16u;
        if (image_bytes != 0u &&
            image_bytes <= placement->descriptor.size_bytes) {
            // Unfused execution may reserve additional UAV regions after the
            // canonical image. Presentation/readback always expose region 0.
            readback_bytes = image_bytes;
        }
    }
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = readback_bytes;
    desc.Height = 1u;
    desc.DepthOrArraySize = 1u;
    desc.MipLevels = 1u;
    desc.Format = DXGI_FORMAT_UNKNOWN;
    desc.SampleDesc.Count = 1u;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    const D3D12_RESOURCE_ALLOCATION_INFO readback_allocation =
        state->device->GetResourceAllocationInfo(0u, 1u, &desc);
    if (readback_allocation.SizeInBytes == 0u ||
        readback_allocation.SizeInBytes == UINT64_MAX ||
        readback_allocation.Alignment == 0u) {
        return Fail(
            ExitCode::ResourceAllocationFailed, "memory",
            "readback_allocation_query_failed", "readback_allocation",
            "valid size/alignment", "unavailable", "readback",
            "GetResourceAllocationInfo rejected the readback buffer");
    }
    if (config_.memory_budget_bytes != 0u) {
        std::uint64_t package_descriptors = 0u;
        for (const auto& resource : package_.resources) {
            if (resource.usage == "sampler" ||
                ResourceNeedsCbvSrvUavDescriptor(package_, resource)) {
                ++package_descriptors;
            }
        }
        const std::uint64_t descriptor_bytes =
            std::max<std::uint64_t>(
                stats_.engine_descriptor_count, package_descriptors) *
            64u;
        const std::uint64_t backbuffer_bytes =
            QuerySwapChainAllocationBytes(state, width_, height_);
        if (backbuffer_bytes == UINT64_MAX) {
            return Fail(
                ExitCode::ResourceAllocationFailed, "memory",
                "memory_budget_allocation_query_failed",
                "swapchain_allocation", "queryable", "unavailable",
                "readback",
                "could not query adapter-specific swap-chain allocation size");
        }
        const std::uint64_t projected =
            stats_.engine_resource_bytes + stats_.engine_upload_bytes +
            descriptor_bytes + backbuffer_bytes +
            readback_allocation.SizeInBytes;
        if (projected > config_.memory_budget_bytes) {
            return Fail(
                ExitCode::ResourceAllocationFailed, "memory",
                "memory_budget_exceeded", "peak_bytes",
                std::to_string(config_.memory_budget_bytes),
                std::to_string(projected), "readback",
                "readback allocation would exceed the generated artifact "
                "budget");
        }
    }
    ID3D12Resource* readback = nullptr;
    if (FAILED(state->device->CreateCommittedResource(
            &heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
            IID_PPV_ARGS(&readback))) ||
        readback == nullptr) {
        return Fail(ExitCode::ResourceAllocationFailed, "readback",
                    "readback_allocation_failed", "readback", "created",
                    "failed", "readback",
                    "readback buffer allocation failed");
    }
    NoteEngineAllocation(readback_allocation.SizeInBytes, 0u, false, false,
                         true);
    auto& frame = state->frames[0];
    const D3D12_RESOURCE_STATES previous_state = source->state;
    auto release_now = [&]() {
        if (readback != nullptr) readback->Release();
        RemoveEngineResourceAllocation(readback_allocation.SizeInBytes);
    };
    auto defer_release = [&]() {
        if (readback != nullptr) {
            state->deferred_readbacks.emplace_back(
                readback, readback_allocation.SizeInBytes);
            readback = nullptr;
        }
        // Keep accounting live until the deferred release actually happens.
    };
    if (frame.allocator == nullptr || frame.command_list == nullptr) {
        release_now();
        return Fail(ExitCode::CommandListRecordingFailed, "readback",
                    "readback_command_list_unavailable", "command_list",
                    "available", "unavailable", "readback",
                    "readback command list is unavailable");
    }
    if (FAILED(frame.allocator->Reset()) ||
        FAILED(frame.command_list->Reset(frame.allocator, nullptr))) {
        release_now();
        return Fail(ExitCode::CommandListRecordingFailed, "readback",
                    "readback_reset_failed", "reset", "succeeded", "failed",
                    "readback", "readback command list reset failed");
    }
    D3D12_RESOURCE_BARRIER to_copy{};
    to_copy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    to_copy.Transition.pResource = source->resource;
    to_copy.Transition.StateBefore = previous_state;
    to_copy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    to_copy.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    frame.command_list->ResourceBarrier(1u, &to_copy);
    frame.command_list->CopyBufferRegion(
        readback, 0u, source->resource, 0u, readback_bytes);
    D3D12_RESOURCE_BARRIER restore = to_copy;
    restore.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
    restore.Transition.StateAfter = previous_state;
    frame.command_list->ResourceBarrier(1u, &restore);
    if (FAILED(frame.command_list->Close())) {
        release_now();
        return Fail(ExitCode::CommandListRecordingFailed, "readback",
                    "readback_close_failed", "closed", "closed", "failed",
                    "readback", "readback command list close failed");
    }
    ID3D12CommandList* lists[] = {frame.command_list};
    state->queue->ExecuteCommandLists(1u, lists);
    const std::uint64_t fence_value = next_fence_value_++;
    if (FAILED(state->queue->Signal(state->fence, fence_value))) {
        defer_release();
        return Fail(ExitCode::GpuSubmissionFailed, "readback",
                    "readback_signal_failed", "signal", "succeeded", "failed",
                    "readback", "readback queue signal failed");
    }
    frame.fence_value = fence_value;
    const ExitCode wait = WaitForFenceValue(
        state->fence, fence_value, state->fence_event, kFenceTimeoutMs);
    if (wait != ExitCode::Success) {
        defer_release();
        return wait;
    }
    void* mapped = nullptr;
    D3D12_RANGE range{0u, static_cast<SIZE_T>(readback_bytes)};
    if (FAILED(readback->Map(0u, &range, &mapped)) || mapped == nullptr) {
        release_now();
        return Fail(ExitCode::ResourceAllocationFailed, "readback",
                    "readback_map_failed", "mapped", "available", "failed",
                    "readback", "readback buffer Map failed");
    }
    bytes->assign(
        static_cast<const std::uint8_t*>(mapped),
        static_cast<const std::uint8_t*>(mapped) +
            readback_bytes);
    D3D12_RANGE empty{0u, 0u};
    readback->Unmap(0u, &empty);
    release_now();
    return ExitCode::Success;
#else
    return Fail(ExitCode::InvalidArguments, "readback",
                "readback_requires_native_device", "device", "D3D12",
                "unavailable", "readback",
                "image readback is a native Windows qualification path");
#endif
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
    present_resource_id_ = 0u;
    present_source_width_ = 0u;
    present_source_height_ = 0u;
    frames_presented_ = 0u;
    using_aliased_plan_ = false;
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
    ResourcePlanResult plan_result =
#if defined(_WIN32)
        // Realize the compiler's logical alias plan against the actual
        // adapter.  Overlapping frames are serialized here (one frame slot) so
        // every transient instance has a fence-safe lifetime without
        // unproven cross-slot reuse.
        BuildAliasedResourcePlan(package_, query, query_user_data, 1u);
#else
        BuildNonAliasedResourcePlan(package_, query, query_user_data);
#endif
#if defined(_WIN32)
    // Preserve the established NW-04 contract for packages with no proven
    // alias group: use ordinary committed resources and the non-aliased plan.
    // The placed heap path is only entered when the adapter-query proof
    // actually found a fence-safe transient reuse.
    if (config_.force_non_aliased_plan) {
        plan_result =
            BuildNonAliasedResourcePlan(package_, query, query_user_data);
        using_aliased_plan_ = false;
    } else if (plan_result.exit_code == ExitCode::Success &&
        (std::none_of(plan_result.plan.placements.begin(),
                      plan_result.plan.placements.end(),
                      [](const ResourcePlacement& placement) {
                          return placement.aliased;
                      }) ||
         std::any_of(plan_result.plan.placements.begin(),
                     plan_result.plan.placements.end(),
                     [](const ResourcePlacement& placement) {
                         return !IsPlaceableAliasResource(
                             placement.descriptor);
                     }))) {
        plan_result =
            BuildNonAliasedResourcePlan(package_, query, query_user_data);
        using_aliased_plan_ = false;
    } else if (plan_result.exit_code == ExitCode::Success) {
        using_aliased_plan_ = true;
        frames_in_flight_ = 1u;
    }
#endif
    if (plan_result.exit_code != ExitCode::Success) {
        code = FailPlan(plan_result);
        ReleasePlatform();
        return code;
    }
    plan_ = plan_result.plan;
    stats_.package_plan_bytes = plan_.total_engine_bytes;

    // Budget is enforced after adapter allocation requirements were queried and
    // before any bulk GPU allocation.  It includes the alias/committed engine
    // range plus PackagePlayer-owned swap-chain, display, upload, descriptor,
    // and readback storage.
    if (config_.memory_budget_bytes != 0u) {
        const std::uint64_t pixels =
            static_cast<std::uint64_t>(width_) * height_;
        std::uint64_t upload_ring = 64u * 1024u;
        for (const auto& instruction : package_.initialization) {
            if (instruction.opcode != "init.upload_resource" ||
                instruction.operands.empty() ||
                instruction.operands[0].type !=
                    package::OperandType::ResourceRef) {
                continue;
            }
            for (const auto& resource : package_.resources) {
                if (resource.resource_id == instruction.operands[0].u32) {
                    upload_ring = std::max(upload_ring, resource.size_bytes);
                }
            }
        }
        const std::uint64_t player_owned =
            3u * pixels * 4u +
            static_cast<std::uint64_t>(frames_in_flight_) * upload_ring +
            512u * 1024u + 64u * 1024u;
        const std::uint64_t accounted =
            plan_.total_engine_bytes + player_owned;
        if (accounted > config_.memory_budget_bytes) {
            return Fail(
                ExitCode::ResourceAllocationFailed, "memory",
                "memory_budget_exceeded", "peak_bytes",
                std::to_string(config_.memory_budget_bytes),
                std::to_string(accounted), "initialize",
                "accounted peak memory exceeds the generated artifact budget");
        }
    }

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
    const ExitCode budget_code = CheckMemoryBudget("initialize_complete");
    if (budget_code != ExitCode::Success) {
        ReleasePlatform();
        return budget_code;
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
        if (platform_->window.IsMinimized()) {
            minimized_ = true;
            suspended_ = true;
            return ExitCode::Success;
        }
        // An explicit zero-sized resize represents a suspended presentation
        // state even though Win32 may clamp the real client rect to its minimum
        // tracking size.  Do not let that clamped rect auto-resume rendering;
        // only a later non-zero resize or a real restore may do so.
        if (width_ == 0u || height_ == 0u) {
            minimized_ = true;
            suspended_ = true;
            return ExitCode::Success;
        }
        std::uint32_t client_width = 0u;
        std::uint32_t client_height = 0u;
        platform_->window.GetClientSize(&client_width, &client_height);
        if ((client_width != width_ || client_height != height_) &&
            client_width > 0u && client_height > 0u) {
            const ExitCode resize_code = resize(client_width, client_height);
            if (resize_code != ExitCode::Success) return resize_code;
        }
        // An ordinary minimize/restore can return to the exact same client
        // size, so resize() is not called.  Clear suspension on the restore
        // transition itself rather than waiting for a ResizeBuffers path.
        if (minimized_ || suspended_) {
            minimized_ = false;
            suspended_ = false;
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
    accounting.engine_cumulative_resource_bytes =
        stats_.engine_cumulative_resource_bytes;
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
        if (config_.memory_budget_bytes != 0u) {
            const std::uint64_t backbuffers =
                QuerySwapChainAllocationBytes(state, width, height);
            const std::uint64_t display =
                present_resource_id_ == 0u
                    ? 0u
                    : QueryTexture2DAllocationBytes(
                          state->device, width, height,
                          D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
            if (backbuffers == UINT64_MAX || display == UINT64_MAX) {
                resize_in_progress_ = false;
                return Fail(
                    ExitCode::ResourceAllocationFailed, "memory",
                    "memory_budget_allocation_query_failed",
                    "resize_allocations", "queryable", "unavailable",
                    "resize",
                    "could not query adapter-specific resize allocation sizes");
            }
            std::uint64_t package_descriptors = 0u;
            for (const auto& resource : package_.resources) {
                if (resource.usage == "sampler" ||
                    ResourceNeedsCbvSrvUavDescriptor(package_, resource)) {
                    ++package_descriptors;
                }
            }
            const std::uint64_t descriptor_bytes =
                std::max<std::uint64_t>(
                    stats_.engine_descriptor_count, package_descriptors) *
                64u;
            const std::uint64_t old_display =
                state->display_texture_allocation_bytes;
            const std::uint64_t base_resources =
                old_display > stats_.engine_resource_bytes
                    ? 0u
                    : stats_.engine_resource_bytes - old_display;
            // During resize the new swap-chain buffers can coexist with the
            // old display texture until render targets are rebuilt. Account
            // for the larger of old/new display allocations at that peak.
            const std::uint64_t projected =
                base_resources + backbuffers +
                std::max(old_display, display) +
                stats_.engine_upload_bytes + descriptor_bytes;
            if (projected > config_.memory_budget_bytes) {
                resize_in_progress_ = false;
                return Fail(
                    ExitCode::ResourceAllocationFailed, "memory",
                    "memory_budget_exceeded", "peak_bytes",
                    std::to_string(config_.memory_budget_bytes),
                    std::to_string(projected), "resize",
                    "resize would exceed the generated artifact budget");
            }
        }
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
        // The display texture is recreated at the new client size, so apply
        // the validated dimensions before rebuilding render targets.
        width_ = width;
        height_ = height;
        const ExitCode recreate = RecreateRenderTargets();
        if (recreate != ExitCode::Success) {
            resize_in_progress_ = false;
            return recreate;
        }
        if (CheckD3D12Validation("resize")) {
            resize_in_progress_ = false;
            return last_exit_;
        }
        const ExitCode budget = CheckMemoryBudget("resize_complete");
        if (budget != ExitCode::Success) {
            resize_in_progress_ = false;
            return budget;
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
    ExitCode result = ExitCode::Success;
    if (initialized_) {
        result = WaitForAllFrames("shutdown");
        // Drain validation messages before the info queue and every other
        // device object are released.  A fence/device/validation failure must
        // remain the observable shutdown result even though cleanup continues.
        if (CheckD3D12Validation("shutdown") &&
            result == ExitCode::Success) {
            result = last_exit_;
        }
    }
    ReleasePlatform();
    initialized_ = false;
    minimized_ = false;
    suspended_ = false;
    ++shutdown_count_;
    shutting_down_ = false;
    if (result != ExitCode::Success) {
        last_exit_ = result;
    }
    return result;
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
    for (auto& deferred : state->deferred_readbacks) {
        if (deferred.first != nullptr) deferred.first->Release();
        RemoveEngineResourceAllocation(deferred.second);
    }
    state->deferred_readbacks.clear();
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
    if (state->package_heap != nullptr) {
        ReleaseAliasedResourceHeap(state->package_heap);
        state->package_heap = nullptr;
    }
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
    if (state->display_texture != nullptr) {
        RemoveEngineResourceAllocation(
            state->display_texture_allocation_bytes);
        state->display_texture_allocation_bytes = 0u;
        state->display_texture->Release();
        state->display_texture = nullptr;
    }
    if (state->display_pipeline != nullptr) {
        state->display_pipeline->Release();
        state->display_pipeline = nullptr;
    }
    if (state->display_root_signature != nullptr) {
        state->display_root_signature->Release();
        state->display_root_signature = nullptr;
    }
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
    if (state->info_queue1 != nullptr) {
        if (state->info_queue_callback_cookie != 0u) {
            (void)state->info_queue1->UnregisterMessageCallback(
                state->info_queue_callback_cookie);
            state->info_queue_callback_cookie = 0u;
        }
        state->info_queue1->Release();
        state->info_queue1 = nullptr;
    }
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

bool PackagePlayer::SetWindowMinimizedForTesting(bool minimized) noexcept {
    if (platform_ == nullptr) {
        return false;
    }
    platform_->window.SetSimulatedMinimized(minimized);
    return true;
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
