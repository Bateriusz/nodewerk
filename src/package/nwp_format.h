#pragma once

// NW-03: `.nwp` version 1 compiler-to-player container.
//
// The package is an execution contract, not an authoring document.  It stores
// capability requirements, resource descriptors, primitive initialization/frame
// instructions, parameter/timeline data, raw shader payloads, raw root-signature
// payloads, and an integrity checksum.  Source maps, reflection tables, editor
// layout, source paths, and source text live in development sidecars.
//
// The integrity checksum detects corruption.  It is not an authenticity
// signature, and parsing is not a sandbox for hostile shader payloads.

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace nw::package {

inline constexpr std::array<std::uint8_t, 8> kMagic = {
    'N', 'W', 'P', 'K', 'G', '1', '\n', '\0'};
inline constexpr std::uint16_t kVersionMajor = 1;
inline constexpr std::uint16_t kVersionMinor = 0;
inline constexpr std::uint32_t kHeaderSize = 64;
inline constexpr std::uint32_t kSectionEntrySize = 32;
inline constexpr std::uint32_t kChecksumSize = 32;

inline constexpr std::string_view kSectionCapability = "CAP ";
inline constexpr std::string_view kSectionResources = "RES ";
inline constexpr std::string_view kSectionInitialization = "INIT";
inline constexpr std::string_view kSectionFrame = "FRAM";
inline constexpr std::string_view kSectionParameters = "PARM";
inline constexpr std::string_view kSectionShaders = "SHDR";
inline constexpr std::string_view kSectionRootSignatures = "RSIG";
inline constexpr std::string_view kSectionDev = "DEVC";

inline constexpr std::string_view kCommonProfileId =
    "nw.common.d3d12.fl12_0.sm6_0.v1";
inline constexpr std::string_view kCommonRequirementsDigest =
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a";
inline constexpr std::string_view kPinnedDxcVersion = "1.8.2407";
inline constexpr std::string_view kOptionalWaveOpsProfileId =
    "nw.optional.d3d12.wave_ops.v1";
inline constexpr std::string_view kOptionalWaveOpsRequirementsDigest =
    "sha256:v1:4a95a94d96d66eaccc3127a0b84d57b6e28d7a5f53a80b2d1ad2030e35e9d3a7";

inline constexpr std::uint32_t kFeatureLevel12_0 = 0xC000u;
inline constexpr std::uint32_t kShaderModel6_0 = 0x0060u;
inline constexpr std::string_view kShaderModelText6_0 = "6_0";
inline constexpr std::uint32_t kResourceBindingTier2 = 2u;
inline constexpr std::uint32_t kResourceHeapTier2 = 2u;
inline constexpr std::uint32_t kMinWindowsBuild = 22631u;
inline constexpr std::uint32_t kMaxCbvSrvUavDescriptorsPerFrame = 4096u;
inline constexpr std::uint32_t kMaxSamplerDescriptorsPerFrame = 64u;
inline constexpr std::uint32_t kMaxRootSignatureDwordCost = 16u;
inline constexpr std::uint32_t kMaxComputeThreadsPerGroup = 1024u;
inline constexpr std::uint32_t kMaxTexture2dDimension = 8192u;
inline constexpr std::uint32_t kMaxTextureArraySize = 2048u;
inline constexpr std::uint32_t kMaxMipLevels = 15u;
inline constexpr std::uint64_t kMaxBufferBytes = 64ull * 1024ull * 1024ull;
inline constexpr std::uint32_t kSwapChainBufferCount = 2u;

inline constexpr std::size_t kMaxPackageBytes = 64u * 1024u * 1024u;
inline constexpr std::size_t kMaxSectionBytes = 16u * 1024u * 1024u;
// Aggregate cap for decoded payload copies.  Blob tables are deduplicated, so
// a valid package normally stays far below this; the cap prevents a malicious
// table from multiplying one large blob by thousands of references.
inline constexpr std::size_t kMaxDecodedBytes = 4u * kMaxPackageBytes;
inline constexpr std::uint32_t kMaxSectionCount = 64u;
inline constexpr std::uint32_t kMaxRecordCount = 4096u;
inline constexpr std::uint32_t kMaxFieldCount = 64u;
inline constexpr std::uint16_t kMaxStringBytes = 4096u;
inline constexpr std::uint32_t kMaxBlobBytes = 8u * 1024u * 1024u;
inline constexpr std::uint32_t kMaxInstructionOperands = 32u;

struct Diagnostic {
    std::string code;
    std::string message;
    std::string section;
    std::uint64_t offset = 0;
    bool has_offset = false;
};

std::string DiagnosticToJson(const Diagnostic& diagnostic);

struct CapabilityRecord {
    std::string profile_id =
        std::string(kCommonProfileId.data(), kCommonProfileId.size());
    std::string requirements_digest = std::string(
        kCommonRequirementsDigest.data(), kCommonRequirementsDigest.size());
    std::string optional_profile_id;
    std::string optional_requirements_digest;
    bool optional_profile_separate_artifact = true;
    std::uint32_t feature_level = kFeatureLevel12_0;
    std::uint32_t shader_model = kShaderModel6_0;
    std::string shader_model_text = std::string(kShaderModelText6_0.data(),
                                                kShaderModelText6_0.size());
    std::uint32_t resource_binding_tier = kResourceBindingTier2;
    std::uint32_t resource_heap_tier = kResourceHeapTier2;
    std::uint32_t direct_queue_count = 1u;
    std::uint32_t max_cbv_srv_uav_descriptors_per_frame =
        kMaxCbvSrvUavDescriptorsPerFrame;
    std::uint32_t max_sampler_descriptors_per_frame =
        kMaxSamplerDescriptorsPerFrame;
    std::uint32_t max_root_signature_dword_cost = kMaxRootSignatureDwordCost;
    std::uint32_t min_windows_build = kMinWindowsBuild;
    std::vector<std::string> required_dxgi_formats = {
        "DXGI_FORMAT_R8G8B8A8_UNORM",
        "DXGI_FORMAT_R32G32B32A32_FLOAT",
        "DXGI_FORMAT_R32_UINT",
        "DXGI_FORMAT_D32_FLOAT"};
    std::uint32_t limit_max_texture_2d_dimension = kMaxTexture2dDimension;
    std::uint32_t limit_max_compute_threads_per_group =
        kMaxComputeThreadsPerGroup;
    std::uint64_t limit_max_buffer_bytes = kMaxBufferBytes;
    std::uint32_t limit_swap_chain_buffer_count = kSwapChainBufferCount;
    std::vector<std::string> emitted_shader_targets = {"cs_6_0"};
    bool runtime_dxc_dependency = false;
    bool runtime_slang_dependency = false;
    bool agility_sdk_required = false;
    std::string toolchain_dxc_version = "1.8.2407";
    std::string toolchain_identity = "dxc:1.8.2407";
    std::string compiler_fingerprint;
    std::string profile_fingerprint;
    std::vector<std::string> required_init_instructions = {
        "init.create_pipeline", "init.create_resource",
        "init.create_root_signature"};
    std::vector<std::string> required_frame_instructions = {
        "frame.dispatch", "frame.set_descriptor_table",
        "frame.set_root_signature"};
};

std::string ComputeProfileFingerprint(const CapabilityRecord& capability);

struct ResourceDescriptor {
    std::uint32_t resource_id = 0;
    std::string name;
    std::string kind;
    std::string format;
    std::string usage;
    std::string pass_family;
    std::uint32_t binding_slot = 0;
    std::uint32_t binding_space = 0;
    std::uint32_t width = 0;
    std::uint32_t height = 0;
    std::uint32_t depth = 0;
    std::uint32_t array_size = 1;
    std::uint32_t mip_levels = 1;
    std::uint64_t size_bytes = 0;
    std::uint32_t flags = 0;
};

enum class OperandType : std::uint8_t {
    U32 = 1,
    U64 = 2,
    I64 = 3,
    Bool = 4,
    String = 5,
    F32 = 6,
    ResourceRef = 7,
    ShaderRef = 8,
    RootSignatureRef = 9,
};

std::string_view ToString(OperandType type) noexcept;
std::optional<OperandType> ParseOperandType(std::string_view text) noexcept;

struct InstructionOperand {
    OperandType type = OperandType::U32;
    std::uint32_t u32 = 0;
    std::uint64_t u64 = 0;
    std::int64_t i64 = 0;
    bool boolean = false;
    double f32 = 0.0;
    std::string string_value;
};

struct Instruction {
    std::string opcode;
    std::vector<InstructionOperand> operands;
};

struct ParameterRecord {
    std::uint32_t parameter_id = 0;
    std::string name;
    std::string type;
    std::string phase;
    std::uint32_t component_count = 0;
    std::vector<std::uint8_t> value;
    std::uint64_t timeline_start_frame = 0;
    std::uint64_t timeline_end_frame = 0;
    std::uint32_t timeline_step = 0;
    std::string timeline_interpolation = "none";
};

struct RootBinding {
    std::string name;
    std::string usage;
    std::uint32_t slot = 0;
    std::uint32_t space = 0;
    std::uint32_t root_parameter = 0;
};

struct RootSignatureRecord {
    std::uint32_t root_signature_id = 0;
    std::string family;
    std::string convention;
    std::uint32_t dword_cost = 0;
    std::vector<std::uint8_t> blob;
    std::vector<RootBinding> bindings;
};

struct ShaderRecord {
    std::uint32_t shader_id = 0;
    std::string entry_point;
    std::string target;
    std::vector<std::uint8_t> blob;
    std::uint32_t root_signature_id = 0;
    std::string family;
    std::uint32_t thread_group_x = 64;
    std::uint32_t thread_group_y = 1;
    std::uint32_t thread_group_z = 1;
    std::uint32_t flags = 0;
};

struct Package {
    CapabilityRecord capability;
    std::vector<ResourceDescriptor> resources;
    std::vector<Instruction> initialization;
    std::vector<Instruction> frame;
    std::vector<ParameterRecord> parameters;
    std::vector<ShaderRecord> shaders;
    std::vector<RootSignatureRecord> root_signatures;
    std::optional<std::vector<std::uint8_t>> dev_metadata;
    std::uint32_t flags = 1u;
};

// Encoding validates the model first.  On failure no package bytes are
// returned and diagnostics describe every detected defect.
std::vector<std::uint8_t> EncodePackage(const Package& package,
                                        std::vector<Diagnostic>& diagnostics);

// Bounded decode.  Checks the magic/version/header sizes, section count,
// directory ranges/overlaps/checked arithmetic, every decoded record count and
// length against fixed limits, resource dimensions, instruction operands and
// references, shader/root-signature payload structure, and the integrity
// checksum before accepting the package for GPU submission.
bool DecodePackage(const std::uint8_t* data, std::size_t size_bytes,
                   Package& package, std::vector<Diagnostic>& diagnostics,
                   bool allow_dev = false,
                   std::size_t max_total_bytes = kMaxPackageBytes,
                   std::size_t max_section_bytes = kMaxSectionBytes,
                   bool verify_integrity = true);

bool ValidatePackage(const Package& package,
                     std::vector<Diagnostic>& diagnostics,
                     bool allow_dev = false);

std::string InspectJson(const Package& package, bool valid,
                        const std::vector<Diagnostic>& diagnostics,
                        std::size_t package_size_bytes,
                        std::string_view checksum_hex,
                        const std::vector<std::string>& leakage =
                            std::vector<std::string>());

std::vector<std::string> CheckReleaseLeakage(
    const std::vector<std::uint8_t>& package_bytes);

// Structural DXIL checks used before packaging (same rules as package decode).
bool ValidateDxilPayload(const std::vector<std::uint8_t>& blob,
                         std::string& reason);

std::vector<std::uint8_t> MakeRootSignatureBlob(
    std::string_view family, const std::vector<RootBinding>& bindings,
    std::uint32_t flags = 0u);

std::string Sha256Hex(const std::uint8_t* data, std::size_t size);
std::string Sha256Hex(std::string_view text);
std::string Sha256Hex(const std::vector<std::uint8_t>& bytes);
std::string BytesToHex(const std::uint8_t* data, std::size_t size);

} // namespace nw::package
