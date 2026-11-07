#pragma once
#include "exit_codes.h"
#include <cstddef>
#include <cstdint>

namespace nw::player {

// Baseline capability contract pinned for NW-01 through NW-12:
// - Windows 11 x64 (pinned minimum target build 22631; execution evidence pending)
// - D3D12 Feature Level 12_0 (0xc000)
// - Shader Model 6.0 (0x60)
// - Resource Binding Tier 2 & Resource Heap Tier 2 (matching benchmarks/capability-manifest.v1.json)
// - Bounded descriptor tables
// - Ordinary resource barriers
// - Single direct command queue (D3D12_COMMAND_LIST_TYPE_DIRECT)
// - No SM 6.6 direct heap indexing, enhanced barriers, mesh shaders, or raytracing required.
struct BaselineCapabilityProfile {
    static constexpr std::uint32_t kMinWindowsBuildNumber = 22631u;
    static constexpr std::uint32_t kRequiredFeatureLevel = 0xc000u; // D3D_FEATURE_LEVEL_12_0
    static constexpr std::uint32_t kRequiredShaderModel = 0x60u;    // D3D_SHADER_MODEL_6_0
    static constexpr std::uint32_t kRequiredResourceBindingTier = 2u; // D3D12_RESOURCE_BINDING_TIER_2
    static constexpr std::uint32_t kRequiredResourceHeapTier = 2u;    // D3D12_RESOURCE_HEAP_TIER_2
    static constexpr std::uint32_t kDirectQueueCount = 1u;
    static constexpr bool kRequireBoundedDescriptorTables = true;
    static constexpr bool kRequireOrdinaryBarriers = true;

    // Excluded baseline features (queried only when emitted package explicitly sets optional flags)
    static constexpr bool kRequireSm66DirectHeapIndexing = false;
    static constexpr bool kRequireEnhancedBarriers = false;
    static constexpr bool kRequireMeshShaders = false;
    static constexpr bool kRequireRayTracing = false;
    static constexpr bool kAllowRuntimeShaderCompilation = false;
    static constexpr bool kRequireAgilitySdkRedist = false;
};

// Optional feature request flags emitted by nodewerkc player link manifest when content uses them.
struct EmittedContentFeatureRequest {
    bool uses_wave_intrinsics = false;
    bool uses_int64_shader_ops = false;
    bool uses_fp16_shader_ops = false;
    bool uses_typed_uav_load_additional_formats = false;
};

struct AdapterCapabilitySnapshot {
    std::uint32_t windows_build_number = 0;
    std::uint32_t max_feature_level = 0;
    std::uint32_t max_shader_model = 0;
    std::uint32_t resource_binding_tier = 0;
    std::uint32_t resource_heap_tier = 0;
    bool wave_ops_queried = false;
    bool int64_shader_ops_queried = false;
    bool fp16_shader_ops_queried = false;
    bool wave_ops_supported = false;
    bool int64_shader_ops_supported = false;
    bool fp16_shader_ops_supported = false;
    bool typed_uav_additional_formats_supported = false;
};

// Evaluates adapter snapshot against the NW-01 baseline and any optional emitted-content features.
ExitCode EvaluateCapabilityContract(
    const AdapterCapabilitySnapshot& snapshot,
    const EmittedContentFeatureRequest& optional_request) noexcept;

// Validates offline DXBC/DXIL container header, retail signature, PSV0 chunk, SFI0 flags,
// and LLVM 3.7 bitcode stream integrity before CreateComputePipelineState.
// Ensures no runtime DXC compilation is needed or attempted.
ExitCode ValidateOfflineDxilContainer(const std::uint8_t* data, std::size_t size_bytes) noexcept;

} // namespace nw::player
