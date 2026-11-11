#include "capability_requirements.h"

#include "exit_codes.h"

#include <algorithm>
#include <limits>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace nw::player {
namespace {

using package::CapabilityRecord;
using package::Instruction;
using package::Package;
using package::ResourceDescriptor;
using package::RootSignatureRecord;
using package::ShaderRecord;

constexpr const char* kUnavailable = "unavailable";

std::string FormatU32(std::uint32_t value) { return std::to_string(value); }
std::string FormatU64(std::uint64_t value) { return std::to_string(value); }

void AddFailure(PackageCapabilityEvaluation* result,
                std::string code,
                std::string requirement,
                std::string required_value,
                std::string observed_value,
                std::string detail,
                const CapabilityRecord& capability) {
    CapabilityRequirementFailure failure{};
    failure.code = std::move(code);
    failure.requirement = std::move(requirement);
    failure.required_value = std::move(required_value);
    failure.observed_value = std::move(observed_value);
    failure.profile_id = capability.profile_id;
    failure.requirements_digest = capability.requirements_digest;
    failure.detail = std::move(detail);
    result->failures.push_back(std::move(failure));
}

bool IsKnownFormat(const std::string& format) {
    return format == "DXGI_FORMAT_R8G8B8A8_UNORM" ||
           format == "DXGI_FORMAT_R32G32B32A32_FLOAT" ||
           format == "DXGI_FORMAT_R32_UINT" ||
           format == "DXGI_FORMAT_D32_FLOAT";
}

bool IsKnownShaderTarget(const std::string& target) {
    return target == "cs_6_0" || target == "vs_6_0" || target == "ps_6_0";
}

std::vector<std::string> SortedUnique(std::vector<std::string> values) {
    std::sort(values.begin(), values.end());
    values.erase(std::unique(values.begin(), values.end()), values.end());
    return values;
}

std::vector<std::string> EmittedFormats(const Package& pkg) {
    std::vector<std::string> formats;
    formats.reserve(pkg.resources.size());
    for (const auto& resource : pkg.resources) {
        formats.push_back(resource.format);
    }
    return SortedUnique(std::move(formats));
}

std::vector<std::string> EmittedTargets(const Package& pkg) {
    std::vector<std::string> targets;
    targets.reserve(pkg.shaders.size());
    for (const auto& shader : pkg.shaders) {
        targets.push_back(shader.target);
    }
    return SortedUnique(std::move(targets));
}

std::vector<std::string> EmittedOpcodes(
    const std::vector<Instruction>& instructions) {
    std::vector<std::string> opcodes;
    opcodes.reserve(instructions.size());
    for (const auto& instruction : instructions) {
        opcodes.push_back(instruction.opcode);
    }
    return SortedUnique(std::move(opcodes));
}

struct DerivedRequirements {
    std::uint32_t descriptor_count = 0u;
    std::uint32_t sampler_count = 0u;
    std::uint32_t max_root_dwords = 0u;
    std::uint32_t max_texture_2d_dimension = 0u;
    std::uint32_t max_compute_threads = 0u;
    std::uint64_t max_buffer_bytes = 0u;
    std::uint32_t swap_chain_buffer_count = 0u;
};

DerivedRequirements DeriveRequirements(const Package& pkg) {
    DerivedRequirements derived{};
    std::set<std::uint32_t> root_constant_resources;
    for (const auto& instruction : pkg.frame) {
        if (instruction.opcode == "frame.set_root_constants" &&
            instruction.operands.size() == 3u &&
            instruction.operands[1].type == package::OperandType::ResourceRef) {
            root_constant_resources.insert(instruction.operands[1].u32);
        }
        if (instruction.opcode == "frame.present") {
            derived.swap_chain_buffer_count = package::kSwapChainBufferCount;
        }
    }
    for (const auto& resource : pkg.resources) {
        if (resource.usage == "sampler") {
            ++derived.sampler_count;
        } else if (resource.usage == "srv" || resource.usage == "uav" ||
                   (resource.usage == "cbv" &&
                    root_constant_resources.count(resource.resource_id) == 0u)) {
            ++derived.descriptor_count;
        }
        if (resource.kind == "buffer") {
            derived.max_buffer_bytes =
                std::max(derived.max_buffer_bytes, resource.size_bytes);
        }
        if (resource.kind == "texture2d") {
            derived.max_texture_2d_dimension =
                std::max(derived.max_texture_2d_dimension,
                         std::max(resource.width, resource.height));
        }
    }
    for (const auto& root : pkg.root_signatures) {
        derived.max_root_dwords =
            std::max(derived.max_root_dwords, root.dword_cost);
    }
    for (const auto& shader : pkg.shaders) {
        const std::uint64_t total =
            static_cast<std::uint64_t>(shader.thread_group_x) *
            static_cast<std::uint64_t>(shader.thread_group_y) *
            static_cast<std::uint64_t>(shader.thread_group_z);
        if (total >
            static_cast<std::uint64_t>(
                std::numeric_limits<std::uint32_t>::max())) {
            derived.max_compute_threads =
                std::numeric_limits<std::uint32_t>::max();
        } else {
            derived.max_compute_threads =
                std::max(derived.max_compute_threads,
                         static_cast<std::uint32_t>(total));
        }
    }
    return derived;
}

void CheckExactU32(PackageCapabilityEvaluation* result,
                   const CapabilityRecord& capability,
                   const char* requirement,
                   std::uint32_t actual,
                   std::uint32_t expected) {
    if (actual != expected) {
        AddFailure(result,
                   std::string("package_capability_") + requirement,
                   requirement,
                   FormatU32(expected),
                   FormatU32(actual),
                   "capability record does not match the emitted package",
                   capability);
    }
}

void CheckExactU64(PackageCapabilityEvaluation* result,
                   const CapabilityRecord& capability,
                   const char* requirement,
                   std::uint64_t actual,
                   std::uint64_t expected) {
    if (actual != expected) {
        AddFailure(result,
                   std::string("package_capability_") + requirement,
                   requirement,
                   FormatU64(expected),
                   FormatU64(actual),
                   "capability record does not match the emitted package",
                   capability);
    }
}

const QueriedFormatCapability* FindFormat(
    const QueriedAdapterCapabilities& capabilities,
    const std::string& format) {
    for (const auto& candidate : capabilities.formats) {
        if (candidate.format == format) {
            return &candidate;
        }
    }
    return nullptr;
}

bool FormatSatisfiesUsage(const QueriedFormatCapability& format,
                          const ResourceDescriptor& resource) {
    const std::string& usage = resource.usage;
    if (usage == "sampler") {
        return true;
    }
    if (!format.supported) {
        return false;
    }
    const bool dimension_supported =
        resource.kind == "buffer" ? format.buffer : format.texture2d;
    if (usage == "cbv") {
        return resource.kind == "buffer" && dimension_supported;
    }
    if (usage == "srv") {
        return dimension_supported && format.shader_resource;
    }
    if (usage == "uav") {
        return dimension_supported && format.unordered_access;
    }
    if (usage == "rtv") {
        return dimension_supported && format.render_target;
    }
    if (usage == "dsv") {
        return dimension_supported && format.depth_stencil;
    }
    return dimension_supported;
}

std::string FormatSupportSummary(const QueriedFormatCapability* format) {
    if (format == nullptr) {
        return kUnavailable;
    }
    if (!format->supported) {
        return "unsupported";
    }
    std::string summary = "supported";
    if (format->buffer) summary += ":buffer";
    if (format->texture2d) summary += ":texture2d";
    if (format->unordered_access) summary += ":uav";
    if (format->render_target) summary += ":rtv";
    if (format->depth_stencil) summary += ":dsv";
    return summary;
}

} // namespace

PackageCapabilityEvaluation ValidateEmittedRequirementsAgainstPackage(
    const Package& pkg) {
    PackageCapabilityEvaluation result{};
    const CapabilityRecord& capability = pkg.capability;
    result.package_profile_id = capability.profile_id;
    result.package_requirements_digest = capability.requirements_digest;

    // Profile identity and digest are immutable NW-03 contract values.
    if (capability.profile_id != package::kCommonProfileId) {
        AddFailure(&result, "package_profile_unknown", "profile_id",
                   std::string(package::kCommonProfileId),
                   capability.profile_id.empty() ? kUnavailable
                                                 : capability.profile_id,
                   "only the common NW-03 profile is executable", capability);
    }
    if (capability.requirements_digest != package::kCommonRequirementsDigest) {
        AddFailure(&result, "package_profile_digest_mismatch",
                   "requirements_digest",
                   std::string(package::kCommonRequirementsDigest),
                   capability.requirements_digest.empty()
                       ? kUnavailable
                       : capability.requirements_digest,
                   "capability profile digest is not the finalized NW-03 digest",
                   capability);
    }

    // Fixed common-profile fields.
    CheckExactU32(&result, capability, "feature_level",
                  capability.feature_level, package::kFeatureLevel12_0);
    CheckExactU32(&result, capability, "shader_model",
                  capability.shader_model, package::kShaderModel6_0);
    CheckExactU32(&result, capability, "resource_binding_tier",
                  capability.resource_binding_tier,
                  package::kResourceBindingTier2);
    CheckExactU32(&result, capability, "resource_heap_tier",
                  capability.resource_heap_tier,
                  package::kResourceHeapTier2);
    CheckExactU32(&result, capability, "direct_queue_count",
                  capability.direct_queue_count, 1u);
    CheckExactU32(&result, capability, "min_windows_build",
                  capability.min_windows_build, package::kMinWindowsBuild);
    if (capability.shader_model_text != package::kShaderModelText6_0) {
        AddFailure(&result, "package_capability_contradictory",
                   "shader_model_text", "6_0",
                   capability.shader_model_text.empty()
                       ? kUnavailable
                       : capability.shader_model_text,
                   "shader_model_text does not match shader model 6.0",
                   capability);
    }

    // Exact emitted formats, opcodes, targets, and limits.
    const auto emitted_formats = EmittedFormats(pkg);
    const auto declared_formats = SortedUnique(capability.required_dxgi_formats);
    if (declared_formats.size() != capability.required_dxgi_formats.size()) {
        AddFailure(&result, "package_capability_contradictory",
                   "required_dxgi_formats", "unique format set",
                   "duplicate format entries",
                   "required format list contains duplicates", capability);
    }
    if (declared_formats != emitted_formats) {
        std::string declared;
        for (const auto& format : declared_formats) {
            if (!declared.empty()) declared += ",";
            declared += format;
        }
        std::string emitted;
        for (const auto& format : emitted_formats) {
            if (!emitted.empty()) emitted += ",";
            emitted += format;
        }
        AddFailure(&result, "package_capability_format_set",
                   "required_dxgi_formats", emitted,
                   declared.empty() ? kUnavailable : declared,
                   "declared formats must exactly match emitted resources",
                   capability);
    }
    for (const auto& format : capability.required_dxgi_formats) {
        if (!IsKnownFormat(format)) {
            AddFailure(&result, "package_capability_format_set",
                       "required_dxgi_formats", "baseline DXGI formats", format,
                       "unknown DXGI format requirement", capability);
        }
    }

    const auto required_init = SortedUnique(capability.required_init_instructions);
    if (required_init.size() != capability.required_init_instructions.size()) {
        AddFailure(&result, "package_capability_contradictory",
                   "required_init_instructions", "unique opcode set",
                   "duplicate opcode entries",
                   "initialization requirement contains duplicates", capability);
    }
    const auto emitted_init = EmittedOpcodes(pkg.initialization);
    if (required_init != emitted_init) {
        AddFailure(&result, "package_capability_instruction_mismatch",
                   "required_init_instructions", "emitted opcodes",
                   "mismatched opcode set", "initialization requirement is not exact",
                   capability);
    }
    const auto required_frame = SortedUnique(capability.required_frame_instructions);
    if (required_frame.size() != capability.required_frame_instructions.size()) {
        AddFailure(&result, "package_capability_contradictory",
                   "required_frame_instructions", "unique opcode set",
                   "duplicate opcode entries",
                   "frame requirement contains duplicates", capability);
    }
    const auto emitted_frame = EmittedOpcodes(pkg.frame);
    if (required_frame != emitted_frame) {
        AddFailure(&result, "package_capability_instruction_mismatch",
                   "required_frame_instructions", "emitted opcodes",
                   "mismatched opcode set", "frame requirement is not exact",
                   capability);
    }

    const auto emitted_targets = EmittedTargets(pkg);
    const auto declared_targets = SortedUnique(capability.emitted_shader_targets);
    if (declared_targets.size() != capability.emitted_shader_targets.size()) {
        AddFailure(&result, "package_capability_contradictory",
                   "emitted_shader_targets", "unique target set",
                   "duplicate target entries",
                   "shader target requirement contains duplicates", capability);
    }
    if (declared_targets != emitted_targets) {
        AddFailure(&result, "package_capability_shader_target_mismatch",
                   "emitted_shader_targets", "emitted shader targets",
                   "mismatched target set", "shader target requirement is not exact",
                   capability);
    }
    for (const auto& target : capability.emitted_shader_targets) {
        if (!IsKnownShaderTarget(target)) {
            AddFailure(&result, "package_shader_target_unknown",
                       "emitted_shader_targets", "cs_6_0/vs_6_0/ps_6_0", target,
                       "unknown shader target requirement", capability);
        }
    }

    const DerivedRequirements derived = DeriveRequirements(pkg);
    CheckExactU32(&result, capability,
                  "max_cbv_srv_uav_descriptors_per_frame",
                  capability.max_cbv_srv_uav_descriptors_per_frame,
                  derived.descriptor_count);
    CheckExactU32(&result, capability, "max_sampler_descriptors_per_frame",
                  capability.max_sampler_descriptors_per_frame,
                  derived.sampler_count);
    CheckExactU32(&result, capability, "max_root_signature_dword_cost",
                  capability.max_root_signature_dword_cost,
                  derived.max_root_dwords);
    CheckExactU32(&result, capability, "limit_max_texture_2d_dimension",
                  capability.limit_max_texture_2d_dimension,
                  derived.max_texture_2d_dimension);
    CheckExactU32(&result, capability, "limit_max_compute_threads_per_group",
                  capability.limit_max_compute_threads_per_group,
                  derived.max_compute_threads);
    CheckExactU64(&result, capability, "limit_max_buffer_bytes",
                  capability.limit_max_buffer_bytes,
                  derived.max_buffer_bytes);
    CheckExactU32(&result, capability, "limit_swap_chain_buffer_count",
                  capability.limit_swap_chain_buffer_count,
                  derived.swap_chain_buffer_count);

    if (capability.max_cbv_srv_uav_descriptors_per_frame >
            package::kMaxCbvSrvUavDescriptorsPerFrame ||
        capability.max_sampler_descriptors_per_frame >
            package::kMaxSamplerDescriptorsPerFrame ||
        capability.max_root_signature_dword_cost >
            package::kMaxRootSignatureDwordCost ||
        capability.limit_max_texture_2d_dimension >
            package::kMaxTexture2dDimension ||
        capability.limit_max_compute_threads_per_group >
            package::kMaxComputeThreadsPerGroup ||
        capability.limit_max_buffer_bytes > package::kMaxBufferBytes ||
        capability.limit_swap_chain_buffer_count >
            package::kSwapChainBufferCount) {
        AddFailure(&result, "package_capability_contradictory",
                   "emitted_limits", "baseline maxima",
                   "requirement exceeds the common profile maximum",
                   "emitted requirement exceeds the immutable baseline",
                   capability);
    }

    // Runtime requirements must remain entirely offline.
    if (capability.runtime_dxc_dependency) {
        AddFailure(&result, "package_runtime_dependency_forbidden",
                   "runtime_dxc_dependency", "false", "true",
                   "runtime shader compilation is forbidden", capability);
    }
    if (capability.runtime_slang_dependency) {
        AddFailure(&result, "package_runtime_dependency_forbidden",
                   "runtime_slang_dependency", "false", "true",
                   "runtime Slang compilation is forbidden", capability);
    }
    if (capability.agility_sdk_required) {
        AddFailure(&result, "package_capability_contradictory",
                   "agility_sdk_required", "false", "true",
                   "Agility SDK redistribution is not part of the common profile",
                   capability);
    }
    if (capability.toolchain_dxc_version.find(
            std::string(package::kPinnedDxcVersion)) ==
        std::string::npos) {
        AddFailure(&result, "package_toolchain_identity",
                   "toolchain_dxc_version",
                   std::string(package::kPinnedDxcVersion),
                   capability.toolchain_dxc_version.empty()
                       ? kUnavailable
                       : capability.toolchain_dxc_version,
                   "release packages require the pinned offline DXC identity",
                   capability);
    }
    const std::string expected_identity =
        "dxc:" + capability.toolchain_dxc_version;
    if (capability.toolchain_identity != expected_identity) {
        AddFailure(&result, "package_toolchain_identity", "toolchain_identity",
                   expected_identity,
                   capability.toolchain_identity.empty()
                       ? kUnavailable
                       : capability.toolchain_identity,
                   "toolchain identity must match the declared DXC version",
                   capability);
    }
    if (capability.compiler_fingerprint.empty()) {
        AddFailure(&result, "package_capability_contradictory",
                   "compiler_fingerprint", "non-empty", kUnavailable,
                   "compiler fingerprint is missing", capability);
    }
    if (capability.profile_fingerprint.empty()) {
        AddFailure(&result, "package_capability_contradictory",
                   "profile_fingerprint", "non-empty", kUnavailable,
                   "profile fingerprint is missing", capability);
    } else if (capability.profile_fingerprint !=
               package::ComputeProfileFingerprint(capability)) {
        AddFailure(&result, "package_profile_fingerprint_mismatch",
                   "profile_fingerprint", "canonical capability fingerprint",
                   capability.profile_fingerprint,
                   "profile fingerprint does not match the capability record",
                   capability);
    }

    // Optional identities are validated but remain separate artifacts.  The
    // common package never silently folds an optional profile in.
    if (!capability.optional_profile_id.empty()) {
        if (capability.optional_profile_id !=
            package::kOptionalWaveOpsProfileId) {
            AddFailure(&result, "package_optional_profile_unknown",
                       "optional_profile_id",
                       std::string(package::kOptionalWaveOpsProfileId),
                       capability.optional_profile_id,
                       "unknown optional capability profile", capability);
        } else if (capability.optional_requirements_digest !=
                   package::kOptionalWaveOpsRequirementsDigest) {
            AddFailure(&result, "package_optional_profile_digest_mismatch",
                       "optional_requirements_digest",
                       std::string(package::kOptionalWaveOpsRequirementsDigest),
                       capability.optional_requirements_digest.empty()
                           ? kUnavailable
                           : capability.optional_requirements_digest,
                       "optional profile digest mismatch", capability);
        }
        if (!capability.optional_profile_separate_artifact) {
            AddFailure(&result, "package_optional_profile_contradiction",
                       "optional_profile_separate_artifact", "true", "false",
                       "unsupported optional profiles must remain separate artifacts",
                       capability);
        }
    } else if (!capability.optional_requirements_digest.empty()) {
        AddFailure(&result, "package_optional_profile_contradiction",
                   "optional_requirements_digest", "empty",
                   capability.optional_requirements_digest,
                   "optional digest without optional profile identity", capability);
    }

    result.exit_code =
        result.failures.empty() ? ExitCode::Success
                                : ExitCode::PackageValidationFailed;
    return result;
}

PackageCapabilityEvaluation EvaluatePackageCapabilities(
    const Package& pkg,
    const QueriedAdapterCapabilities& capabilities) {
    PackageCapabilityEvaluation result = ValidateEmittedRequirementsAgainstPackage(pkg);
    const bool static_failed = !result.failures.empty();
    const CapabilityRecord& requirement = pkg.capability;
    result.package_profile_id = requirement.profile_id;
    result.package_requirements_digest = requirement.requirements_digest;

    if (!capabilities.queried) {
        AddFailure(&result, "package_capability_query_unavailable",
                   "adapter_capabilities", "queried", kUnavailable,
                   "adapter capability query did not complete", requirement);
    }

    if (!capabilities.feature_level_queried) {
        AddFailure(&result, "package_capability_query_unavailable",
                   "feature_level", "12_0", kUnavailable,
                   "D3D_FEATURE_LEVELS query unavailable", requirement);
    } else if (capabilities.max_feature_level < requirement.feature_level) {
        AddFailure(&result, "package_capability_feature_level", "feature_level",
                   "0x" + FormatU32(requirement.feature_level),
                   capabilities.max_feature_level == 0u
                       ? kUnavailable
                       : "0x" + FormatU32(capabilities.max_feature_level),
                   "adapter feature level is below the exact package requirement",
                   requirement);
    }

    if (!capabilities.shader_model_queried) {
        AddFailure(&result, "package_capability_query_unavailable",
                   "shader_model", "6_0", kUnavailable,
                   "D3D12_FEATURE_SHADER_MODEL query unavailable", requirement);
    } else if (capabilities.max_shader_model < requirement.shader_model) {
        AddFailure(&result, "package_capability_shader_model", "shader_model",
                   "6_0", "0x" + FormatU32(capabilities.max_shader_model),
                   "adapter shader model is below the exact package requirement",
                   requirement);
    }

    if (!capabilities.binding_tier_queried) {
        AddFailure(&result, "package_capability_query_unavailable",
                   "resource_binding_tier", "2", kUnavailable,
                   "resource binding tier query unavailable", requirement);
    } else if (capabilities.resource_binding_tier <
               requirement.resource_binding_tier) {
        AddFailure(&result, "package_capability_binding_tier",
                   "resource_binding_tier",
                   FormatU32(requirement.resource_binding_tier),
                   FormatU32(capabilities.resource_binding_tier),
                   "adapter resource binding tier is below the package requirement",
                   requirement);
    }
    if (!capabilities.heap_tier_queried) {
        AddFailure(&result, "package_capability_query_unavailable",
                   "resource_heap_tier", "2", kUnavailable,
                   "resource heap tier query unavailable", requirement);
    } else if (capabilities.resource_heap_tier < requirement.resource_heap_tier) {
        AddFailure(&result, "package_capability_heap_tier", "resource_heap_tier",
                   FormatU32(requirement.resource_heap_tier),
                   FormatU32(capabilities.resource_heap_tier),
                   "adapter resource heap tier is below the package requirement",
                   requirement);
    }
    if (!capabilities.windows_build_queried) {
        AddFailure(&result, "package_capability_query_unavailable",
                   "min_windows_build",
                   FormatU32(requirement.min_windows_build), kUnavailable,
                   "Windows build query unavailable", requirement);
    } else if (capabilities.windows_build_number < requirement.min_windows_build) {
        AddFailure(&result, "package_capability_windows_build",
                   "min_windows_build",
                   FormatU32(requirement.min_windows_build),
                   FormatU32(capabilities.windows_build_number),
                   "Windows build is below the package runtime floor", requirement);
    }
    if (capabilities.direct_queue_count < requirement.direct_queue_count) {
        AddFailure(&result, "package_capability_direct_queue", "direct_queue_count",
                   FormatU32(requirement.direct_queue_count),
                   FormatU32(capabilities.direct_queue_count),
                   "adapter does not expose the required direct queue", requirement);
    }

    if (!capabilities.limits_queried) {
        AddFailure(&result, "package_capability_query_unavailable", "limits",
                   "queried", kUnavailable,
                   "adapter limit query unavailable", requirement);
    } else {
        if (capabilities.max_cbv_srv_uav_descriptors_per_heap <
            requirement.max_cbv_srv_uav_descriptors_per_frame) {
            AddFailure(&result, "package_capability_descriptor_limit",
                       "max_cbv_srv_uav_descriptors_per_frame",
                       FormatU32(requirement.max_cbv_srv_uav_descriptors_per_frame),
                       FormatU32(capabilities.max_cbv_srv_uav_descriptors_per_heap),
                       "descriptor heap capacity is below the package requirement",
                       requirement);
        }
        if (capabilities.max_sampler_descriptors_per_heap <
            requirement.max_sampler_descriptors_per_frame) {
            AddFailure(&result, "package_capability_descriptor_limit",
                       "max_sampler_descriptors_per_frame",
                       FormatU32(requirement.max_sampler_descriptors_per_frame),
                       FormatU32(capabilities.max_sampler_descriptors_per_heap),
                       "sampler heap capacity is below the package requirement",
                       requirement);
        }
        if (capabilities.max_root_signature_dword_cost <
            requirement.max_root_signature_dword_cost) {
            AddFailure(&result, "package_capability_root_signature_limit",
                       "max_root_signature_dword_cost",
                       FormatU32(requirement.max_root_signature_dword_cost),
                       FormatU32(capabilities.max_root_signature_dword_cost),
                       "root-signature dword capacity is below the package requirement",
                       requirement);
        }
        if (capabilities.max_texture_2d_dimension <
            requirement.limit_max_texture_2d_dimension) {
            AddFailure(&result, "package_capability_texture_limit",
                       "limit_max_texture_2d_dimension",
                       FormatU32(requirement.limit_max_texture_2d_dimension),
                       capabilities.max_texture_2d_dimension == 0u
                           ? kUnavailable
                           : FormatU32(capabilities.max_texture_2d_dimension),
                       "2D texture dimension capacity is below the package requirement",
                       requirement);
        }
        if (capabilities.max_compute_threads_per_group <
            requirement.limit_max_compute_threads_per_group) {
            AddFailure(&result, "package_capability_compute_limit",
                       "limit_max_compute_threads_per_group",
                       FormatU32(requirement.limit_max_compute_threads_per_group),
                       capabilities.max_compute_threads_per_group == 0u
                           ? kUnavailable
                           : FormatU32(capabilities.max_compute_threads_per_group),
                       "compute thread-group capacity is below the package requirement",
                       requirement);
        }
        if (capabilities.max_buffer_bytes < requirement.limit_max_buffer_bytes) {
            AddFailure(&result, "package_capability_buffer_limit",
                       "limit_max_buffer_bytes",
                       FormatU64(requirement.limit_max_buffer_bytes),
                       capabilities.max_buffer_bytes == 0u
                           ? kUnavailable
                           : FormatU64(capabilities.max_buffer_bytes),
                       "buffer capacity is below the package requirement",
                       requirement);
        }
        if (capabilities.max_swap_chain_buffer_count <
            requirement.limit_swap_chain_buffer_count) {
            AddFailure(&result, "package_capability_swap_chain_limit",
                       "limit_swap_chain_buffer_count",
                       FormatU32(requirement.limit_swap_chain_buffer_count),
                       capabilities.max_swap_chain_buffer_count == 0u
                           ? kUnavailable
                           : FormatU32(capabilities.max_swap_chain_buffer_count),
                       "swap-chain buffer capacity is below the package requirement",
                       requirement);
        }
    }

    // Required DXGI formats are checked per emitted resource usage.  A missing
    // or unavailable format record is never treated as supported.
    for (const auto& format_name : requirement.required_dxgi_formats) {
        const QueriedFormatCapability* format =
            FindFormat(capabilities, format_name);
        if (format == nullptr) {
            AddFailure(&result, "package_capability_format_unavailable",
                       "required_dxgi_formats", format_name, kUnavailable,
                       "format support query did not return this format",
                       requirement);
            continue;
        }
        for (const auto& resource : pkg.resources) {
            if (resource.format != format_name) {
                continue;
            }
            if (!FormatSatisfiesUsage(*format, resource)) {
                AddFailure(&result, "package_capability_format_usage",
                           "required_dxgi_formats",
                           format_name + ":" + resource.usage,
                           FormatSupportSummary(format),
                           "adapter format does not support the emitted usage",
                           requirement);
                break;
            }
        }
    }

    // Shader targets are queried through D3D12_FEATURE_SHADER_MODEL.  Supporting
    // a newer model does not imply the exact stage target was queried.
    for (const auto& target : requirement.emitted_shader_targets) {
        bool supported = false;
        if (target == "cs_6_0") supported = capabilities.supports_cs_6_0;
        if (target == "vs_6_0") supported = capabilities.supports_vs_6_0;
        if (target == "ps_6_0") supported = capabilities.supports_ps_6_0;
        if (!supported) {
            AddFailure(&result, "package_capability_shader_target",
                       "emitted_shader_targets", target,
                       capabilities.shader_model_queried ? "unsupported"
                                                         : kUnavailable,
                       "adapter did not confirm the emitted shader target",
                       requirement);
        }
    }

    // Runtime requirements remain offline-only.  Availability of a runtime
    // compiler never makes a package requiring one acceptable.
    if (requirement.runtime_dxc_dependency) {
        AddFailure(&result, "package_runtime_dependency_forbidden",
                   "runtime_dxc_dependency", "false",
                   capabilities.runtime_dxc_available ? "available"
                                                      : kUnavailable,
                   "runtime DXC is forbidden even when installed", requirement);
    }
    if (requirement.runtime_slang_dependency) {
        AddFailure(&result, "package_runtime_dependency_forbidden",
                   "runtime_slang_dependency", "false",
                   capabilities.runtime_slang_available ? "available"
                                                        : kUnavailable,
                   "runtime Slang is forbidden even when installed", requirement);
    }

    // Optional profiles are separate artifacts.  Unsupported optional
    // requirements are rejected instead of being silently folded in.
    if (!requirement.optional_profile_id.empty()) {
        if (!requirement.optional_profile_separate_artifact) {
            AddFailure(&result, "package_optional_profile_unsupported",
                       "optional_profile_id", "separate artifact",
                       requirement.optional_profile_id,
                       "optional profile is not a separate artifact", requirement);
        } else if (requirement.optional_profile_id ==
                   package::kOptionalWaveOpsProfileId) {
            if (!capabilities.optional_features_queried ||
                !capabilities.wave_ops_queried) {
                AddFailure(&result, "package_capability_query_unavailable",
                           "optional_features", "queried", kUnavailable,
                           "optional feature query unavailable for declared optional profile",
                           requirement);
            } else if (!capabilities.wave_ops_supported) {
                AddFailure(&result, "package_optional_feature_unsupported",
                           "wave_ops", "true", "false",
                           "declared optional profile requires wave operations "
                           "the adapter did not report",
                           requirement);
            }
        } else {
            AddFailure(&result, "package_optional_profile_unknown",
                       "optional_profile_id", "known optional profile",
                       requirement.optional_profile_id,
                       "unknown optional profile", requirement);
        }
    }

    result.exit_code = result.failures.empty()
                           ? ExitCode::Success
                           : (static_failed
                                  ? ExitCode::PackageValidationFailed
                                  : ExitCode::CapabilityBaselineUnmet);
    return result;
}

QueriedAdapterCapabilities MakeBaselineQueriedCapabilities(
    std::uint32_t windows_build_number) {
    QueriedAdapterCapabilities capabilities{};
    capabilities.queried = true;
    capabilities.feature_level_queried = true;
    capabilities.shader_model_queried = true;
    capabilities.binding_tier_queried = true;
    capabilities.heap_tier_queried = true;
    capabilities.windows_build_queried = true;
    capabilities.limits_queried = true;
    capabilities.optional_features_queried = true;
    capabilities.wave_ops_queried = true;
    capabilities.int64_shader_ops_queried = true;
    capabilities.fp16_shader_ops_queried = true;
    capabilities.windows_build_number = windows_build_number;
    capabilities.max_feature_level = package::kFeatureLevel12_0;
    capabilities.max_shader_model = package::kShaderModel6_0;
    capabilities.resource_binding_tier = package::kResourceBindingTier2;
    capabilities.resource_heap_tier = package::kResourceHeapTier2;
    capabilities.direct_queue_count = 1u;
    capabilities.max_cbv_srv_uav_descriptors_per_heap =
        package::kMaxCbvSrvUavDescriptorsPerFrame;
    capabilities.max_sampler_descriptors_per_heap =
        package::kMaxSamplerDescriptorsPerFrame;
    capabilities.max_root_signature_dword_cost =
        package::kMaxRootSignatureDwordCost;
    capabilities.max_texture_2d_dimension = package::kMaxTexture2dDimension;
    capabilities.max_compute_threads_per_group =
        package::kMaxComputeThreadsPerGroup;
    capabilities.max_buffer_bytes = package::kMaxBufferBytes;
    capabilities.max_swap_chain_buffer_count = package::kSwapChainBufferCount;
    capabilities.supports_cs_6_0 = true;
    capabilities.supports_vs_6_0 = true;
    capabilities.supports_ps_6_0 = true;
    capabilities.wave_ops_supported = true;
    capabilities.int64_shader_ops_supported = true;
    capabilities.fp16_shader_ops_supported = true;
    capabilities.typed_uav_additional_formats_supported = true;
    capabilities.formats = {
        QueriedFormatCapability{"DXGI_FORMAT_R8G8B8A8_UNORM", true, true, true,
                                true, true, true, false, 8192u},
        QueriedFormatCapability{"DXGI_FORMAT_R32G32B32A32_FLOAT", true, true,
                                true, true, true, true, false, 8192u},
        QueriedFormatCapability{"DXGI_FORMAT_R32_UINT", true, true, true, true,
                                true, false, false, 8192u},
        QueriedFormatCapability{"DXGI_FORMAT_D32_FLOAT", true, false, true,
                                true, false, false, true, 8192u},
    };
    return capabilities;
}

} // namespace nw::player
