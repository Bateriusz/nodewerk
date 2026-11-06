#pragma once
#include "capability_check.h"
#include "diagnostics.h"
#include "exit_codes.h"
#include <cstdint>

namespace nw::player {

struct BootstrapConfig {
    const char* fixture_name = "nw_player";
    std::uint32_t width = 1280u;
    std::uint32_t height = 720u;
    std::uint32_t frame_count = 4u;
    bool enable_d3d12_validation = false;
    bool force_diagnostics = false;
    bool has_simulated_failure = false;
    ExitCode simulated_failure = ExitCode::Success;
    EmittedContentFeatureRequest optional_features{};
};

// Parses common CLI flags (--validation, --diagnostics, --frames N, --simulate-failure <CodeName>).
ExitCode ParseBootstrapArgs(int argc, char** argv, BootstrapConfig* out_config) noexcept;

// Runs the empty-player platform/bootstrap verification (OS baseline, capability contract, exit code).
DiagnosticRecord ExecuteEmptyPlayerBootstrap(const BootstrapConfig& config) noexcept;

// Runs the minimal D3D12 clear/present measurement fixture (Feature Level 12_0, 1 direct queue,
// ordinary resource barriers, bounded descriptor tables).
DiagnosticRecord ExecuteClearPresentFixture(const BootstrapConfig& config) noexcept;

// Runs the minimal D3D12 offline compute-shader measurement fixture (Shader Model 6.0 DXIL,
// bounded descriptor table u0, ordinary UAV/copy barriers, readback verification of 256 uint32s).
DiagnosticRecord ExecuteComputeShaderFixture(const BootstrapConfig& config) noexcept;

// Deterministic reference evaluation of the compute shader kernel for CPU-side readback verification.
std::uint32_t EvaluateReferenceComputeElement(std::uint32_t tid, std::uint32_t seed_mask = 0xA5A55A5Au) noexcept;

} // namespace nw::player
