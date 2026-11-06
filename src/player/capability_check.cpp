#include "capability_check.h"
#include <cstring>

namespace nw::player {

ExitCode EvaluateCapabilityContract(
    const AdapterCapabilitySnapshot& snapshot,
    const EmittedContentFeatureRequest& optional_request) noexcept {
    if (snapshot.windows_build_number < BaselineCapabilityProfile::kMinWindowsBuildNumber) {
        return ExitCode::OsVersionUnsupported;
    }
    if (snapshot.max_feature_level < BaselineCapabilityProfile::kRequiredFeatureLevel) {
        return ExitCode::D3D12DeviceCreationFailed;
    }
    if (snapshot.max_shader_model < BaselineCapabilityProfile::kRequiredShaderModel) {
        return ExitCode::CapabilityBaselineUnmet;
    }
    if (snapshot.resource_binding_tier < 1u || snapshot.resource_heap_tier < 1u) {
        return ExitCode::CapabilityBaselineUnmet;
    }

    // Optional features are checked ONLY when emitted content declares usage in its manifest.
    if (optional_request.uses_wave_intrinsics && !snapshot.wave_ops_supported) {
        return ExitCode::OptionalFeatureUnsupported;
    }
    if (optional_request.uses_int64_shader_ops && !snapshot.int64_shader_ops_supported) {
        return ExitCode::OptionalFeatureUnsupported;
    }
    if (optional_request.uses_fp16_shader_ops && !snapshot.fp16_shader_ops_supported) {
        return ExitCode::OptionalFeatureUnsupported;
    }
    if (optional_request.uses_typed_uav_load_additional_formats &&
        !snapshot.typed_uav_additional_formats_supported) {
        return ExitCode::OptionalFeatureUnsupported;
    }

    return ExitCode::Success;
}

ExitCode ValidateOfflineDxilContainer(const std::uint8_t* data, std::size_t size_bytes) noexcept {
    if (!data || size_bytes < 32u) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }
    // Check "DXBC" magic (0x43425844 little-endian)
    if (data[0] != 'D' || data[1] != 'X' || data[2] != 'B' || data[3] != 'C') {
        return ExitCode::OfflineShaderPayloadInvalid;
    }
    std::uint32_t declared_size = 0;
    std::memcpy(&declared_size, data + 24, sizeof(std::uint32_t));
    if (declared_size != static_cast<std::uint32_t>(size_bytes)) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }
    std::uint32_t chunk_count = 0;
    std::memcpy(&chunk_count, data + 28, sizeof(std::uint32_t));
    if (chunk_count == 0u || 32u + chunk_count * 4u > size_bytes) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }

    bool has_dxil_chunk = false;
    for (std::uint32_t i = 0; i < chunk_count; ++i) {
        std::uint32_t offset = 0;
        std::memcpy(&offset, data + 32u + i * 4u, sizeof(std::uint32_t));
        if (offset + 8u > size_bytes) {
            return ExitCode::OfflineShaderPayloadInvalid;
        }
        if (data[offset] == 'D' && data[offset + 1] == 'X' &&
            data[offset + 2] == 'I' && data[offset + 3] == 'L') {
            has_dxil_chunk = true;
        }
    }
    return has_dxil_chunk ? ExitCode::Success : ExitCode::OfflineShaderPayloadInvalid;
}

} // namespace nw::player
