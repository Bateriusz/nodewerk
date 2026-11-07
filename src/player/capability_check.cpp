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
    if (snapshot.resource_binding_tier < BaselineCapabilityProfile::kRequiredResourceBindingTier ||
        snapshot.resource_heap_tier < BaselineCapabilityProfile::kRequiredResourceHeapTier) {
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
    if (!data || size_bytes < 44u) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }
    // 1. Check "DXBC" magic (0x43425844 little-endian)
    if (data[0] != 'D' || data[1] != 'X' || data[2] != 'B' || data[3] != 'C') {
        return ExitCode::OfflineShaderPayloadInvalid;
    }

    // 2. Verify 16-byte retail container signature digest at bytes 4..19 is non-zero
    //    (unsigned / -Vd containers have all-zero digest and are rejected by retail D3D12)
    bool digest_non_zero = false;
    for (std::size_t i = 4u; i < 20u; ++i) {
        if (data[i] != 0u) {
            digest_non_zero = true;
            break;
        }
    }
    if (!digest_non_zero) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }

    // 3. Check container version (major == 1, minor == 0) and declared total byte size
    std::uint16_t container_major = 0;
    std::uint16_t container_minor = 0;
    std::uint32_t declared_size = 0;
    std::uint32_t chunk_count = 0;
    std::memcpy(&container_major, data + 20, sizeof(std::uint16_t));
    std::memcpy(&container_minor, data + 22, sizeof(std::uint16_t));
    std::memcpy(&declared_size, data + 24, sizeof(std::uint32_t));
    std::memcpy(&chunk_count, data + 28, sizeof(std::uint32_t));

    if (container_major != 1u || container_minor != 0u) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }
    if (declared_size != static_cast<std::uint32_t>(size_bytes)) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }
    if (chunk_count < 2u || 32u + static_cast<std::size_t>(chunk_count) * 4u > size_bytes) {
        return ExitCode::OfflineShaderPayloadInvalid;
    }

    const std::size_t header_end = 32u + static_cast<std::size_t>(chunk_count) * 4u;
    bool has_dxil_chunk = false;
    bool has_psv0_chunk = false;

    for (std::uint32_t i = 0; i < chunk_count; ++i) {
        std::uint32_t offset = 0;
        std::memcpy(&offset, data + 32u + static_cast<std::size_t>(i) * 4u, sizeof(std::uint32_t));
        if (offset < header_end || static_cast<std::size_t>(offset) + 8u > size_bytes) {
            return ExitCode::OfflineShaderPayloadInvalid;
        }
        std::uint32_t chunk_size = 0;
        std::memcpy(&chunk_size, data + offset + 4u, sizeof(std::uint32_t));
        if (static_cast<std::size_t>(offset) + 8u + static_cast<std::size_t>(chunk_size) > size_bytes) {
            return ExitCode::OfflineShaderPayloadInvalid;
        }

        const std::uint8_t* payload = data + offset + 8u;

        // SFI0 chunk: verify no forbidden SM 6.6+ heap indexing or non-baseline feature flags
        if (data[offset] == 'S' && data[offset + 1] == 'F' &&
            data[offset + 2] == 'I' && data[offset + 3] == '0') {
            if (chunk_size != 8u) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            std::uint64_t feature_flags = 0;
            std::memcpy(&feature_flags, payload, sizeof(std::uint64_t));
            // Bits 32 & 33: ResourceDescriptorHeapIndexing / SamplerDescriptorHeapIndexing (SM 6.6)
            static constexpr std::uint64_t kForbiddenSm66HeapIndexingMask = (1ULL << 32) | (1ULL << 33);
            if ((feature_flags & kForbiddenSm66HeapIndexingMask) != 0ULL) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
        }
        // PSV0 chunk: Pipeline State Validation required by D3D12 CreateComputePipelineState
        else if (data[offset] == 'P' && data[offset + 1] == 'S' &&
                 data[offset + 2] == 'V' && data[offset + 3] == '0') {
            if (chunk_size < 28u) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            std::uint32_t psv_info_size = 0;
            std::memcpy(&psv_info_size, payload, sizeof(std::uint32_t));
            if (psv_info_size < 24u || static_cast<std::size_t>(psv_info_size) + 4u > chunk_size) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            has_psv0_chunk = true;
        }
        // DXIL chunk: verify DxilProgramHeader (cs_6_0) and LLVM 3.7 bitcode MODULE_BLOCK
        else if (data[offset] == 'D' && data[offset + 1] == 'X' &&
                 data[offset + 2] == 'I' && data[offset + 3] == 'L') {
            if (chunk_size < 32u) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            std::uint32_t prog_ver = 0;
            std::uint32_t size_in_uint32 = 0;
            std::uint32_t dxil_version = 0;
            std::uint32_t bitcode_offset = 0;
            std::uint32_t bitcode_size = 0;
            std::memcpy(&prog_ver, payload + 0u, sizeof(std::uint32_t));
            std::memcpy(&size_in_uint32, payload + 4u, sizeof(std::uint32_t));
            std::memcpy(&dxil_version, payload + 12u, sizeof(std::uint32_t));
            std::memcpy(&bitcode_offset, payload + 16u, sizeof(std::uint32_t));
            std::memcpy(&bitcode_size, payload + 20u, sizeof(std::uint32_t));

            // ProgramVersion for cs_6_0: program_type=5 (CS), major=6, minor=0 -> 0x00050060
            static constexpr std::uint32_t kCs60ProgramVersion = (5u << 16) | (6u << 4) | 0u;
            if (prog_ver != kCs60ProgramVersion) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            if (static_cast<std::size_t>(size_in_uint32) * 4u != chunk_size) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            if (payload[8] != 'D' || payload[9] != 'X' || payload[10] != 'I' || payload[11] != 'L') {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            if ((dxil_version & 0xFFFFu) != 0x0100u) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            if (bitcode_offset < 16u || bitcode_size < 64u || (bitcode_size % 4u) != 0u) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            if (8u + static_cast<std::size_t>(bitcode_offset) + static_cast<std::size_t>(bitcode_size) > chunk_size) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }

            const std::uint8_t* bitcode = payload + 8u + bitcode_offset;
            if (bitcode[0] != 'B' || bitcode[1] != 'C' || bitcode[2] != 0xC0u || bitcode[3] != 0xDEu) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            // LLVM 3.7 bitcode stream must begin with ENTER_SUBBLOCK (1) for MODULE_BLOCK (id=8):
            // low 10 bits = (8 << 2) | 1 = 0x21
            std::uint32_t first_bitstream_word = 0;
            std::memcpy(&first_bitstream_word, bitcode + 4u, sizeof(std::uint32_t));
            if ((first_bitstream_word & 0x3FFu) != 0x21u) {
                return ExitCode::OfflineShaderPayloadInvalid;
            }
            has_dxil_chunk = true;
        }
    }

    return (has_dxil_chunk && has_psv0_chunk) ? ExitCode::Success : ExitCode::OfflineShaderPayloadInvalid;
}

} // namespace nw::player
