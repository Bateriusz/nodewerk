#pragma once

// NW-04: exact emitted `.nwp` v1 capability requirements.
//
// The player consumes the NW-03 capability record verbatim.  It never replaces
// the common profile with an adapter-derived profile, and it never branches on
// adapter description, marketing generation, evidence tier, or PCI identity.
// Adapter identity fields exist only so qualification evidence can record them.

#include "package/nwp_format.h"
#include "exit_codes.h"

#include <cstdint>
#include <string>
#include <vector>

namespace nw::player {

struct QueriedFormatCapability {
    std::string format;
    bool supported = false;
    bool buffer = false;
    bool texture2d = false;
    bool shader_resource = false;
    bool unordered_access = false;
    bool render_target = false;
    bool depth_stencil = false;
    std::uint32_t max_texture_2d_dimension = 0u;
};

// Everything below is produced by actual D3D12 / DXGI queries before bulk
// resource allocation.  A zero/empty value means the query was unavailable and
// is reported as such instead of being treated as satisfying the requirement.
struct QueriedAdapterCapabilities {
    bool queried = false;

    // Metadata only.  These fields must never be used in eligibility branches.
    std::string adapter_description;
    std::string adapter_generation;
    std::string driver_version;
    std::string wddm_version_text;
    std::uint32_t vendor_id = 0u;
    std::uint32_t device_id = 0u;
    std::uint64_t dedicated_video_memory_bytes = 0u;
    std::uint64_t wddm_version = 0u;
    std::uint32_t windows_build_number = 0u;

    bool feature_level_queried = false;
    bool shader_model_queried = false;
    bool binding_tier_queried = false;
    bool heap_tier_queried = false;
    bool windows_build_queried = false;
    bool limits_queried = false;
    bool optional_features_queried = false;
    bool wave_ops_queried = false;
    bool int64_shader_ops_queried = false;
    bool fp16_shader_ops_queried = false;

    std::uint32_t max_feature_level = 0u;
    std::uint32_t max_shader_model = 0u;
    std::uint32_t resource_binding_tier = 0u;
    std::uint32_t resource_heap_tier = 0u;
    std::uint32_t direct_queue_count = 0u;

    std::uint32_t max_cbv_srv_uav_descriptors_per_heap = 0u;
    std::uint32_t max_sampler_descriptors_per_heap = 0u;
    std::uint32_t max_root_signature_dword_cost = 0u;
    std::uint32_t max_texture_2d_dimension = 0u;
    std::uint32_t max_compute_threads_per_group = 0u;
    std::uint64_t max_buffer_bytes = 0u;
    std::uint32_t max_swap_chain_buffer_count = 0u;

    bool supports_cs_6_0 = false;
    bool supports_vs_6_0 = false;
    bool supports_ps_6_0 = false;

    bool wave_ops_supported = false;
    bool int64_shader_ops_supported = false;
    bool fp16_shader_ops_supported = false;
    bool typed_uav_additional_formats_supported = false;

    // A clean-system release player must not need a runtime shader compiler.
    bool runtime_dxc_available = false;
    bool runtime_slang_available = false;

    std::vector<QueriedFormatCapability> formats;
};

struct CapabilityRequirementFailure {
    // Stable machine-readable code, e.g. package_capability_feature_level.
    std::string code;
    // Human-readable requirement name, e.g. "feature_level".
    std::string requirement;
    std::string required_value;
    std::string observed_value;
    std::string profile_id;
    std::string requirements_digest;
    std::string detail;
};

struct PackageCapabilityEvaluation {
    ExitCode exit_code = ExitCode::Success;
    std::string package_profile_id;
    std::string package_requirements_digest;
    std::vector<CapabilityRequirementFailure> failures;

    bool ok() const noexcept { return failures.empty(); }
};

// Validates the self-contained capability record against the package payload:
// exact profile identity/digest, exact emitted formats/limits/targets/opcodes,
// optional-profile identity, runtime requirements, and checked baseline maxima.
PackageCapabilityEvaluation ValidateEmittedRequirementsAgainstPackage(
    const package::Package& package);

// Compares the exact emitted requirements against already-queried adapter
// capabilities.  This runs after device creation but before any bulk resource
// allocation.  No adapter metadata influences the result.
PackageCapabilityEvaluation EvaluatePackageCapabilities(
    const package::Package& package,
    const QueriedAdapterCapabilities& capabilities);

// Convenience baseline snapshot used by host simulation and tests.  It still
// goes through the same exact requirement checks as a real adapter query.
QueriedAdapterCapabilities MakeBaselineQueriedCapabilities(
    std::uint32_t windows_build_number);

} // namespace nw::player
