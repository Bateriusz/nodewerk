#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace nw11 {

enum class Variant {
    CommonBaseline,
    OptionalMesh,
    OptionalRayQuery,
};

struct RendererOptions {
    Variant variant = Variant::CommonBaseline;
    std::string benchmark_json;
    std::string capture_dir;
    std::string capture_manifest;
    std::string role;
    std::string artifact_path;
    std::string device_tier;
    std::string device_key;
    std::string environment_key;
    // LUID of the exact probe-selected adapter, formatted "<high>:<low>".
    // When set, the renderer must measure that adapter or fail initialization.
    std::string adapter_luid;
    std::string capability_decision_path;
    std::string reference_kind = "raster_reference";
    std::string fixed_quality = "nw07.quality.v1";
    std::uint32_t warmup_seconds = 5;
    std::uint32_t runs = 3;
    std::uint32_t run_seconds = 60;
    std::uint64_t distribution_bytes = 0;
    std::vector<std::string> shader_ids;
    bool self_check = false;
};

int RunSceneRenderer(const RendererOptions& options);

}  // namespace nw11
