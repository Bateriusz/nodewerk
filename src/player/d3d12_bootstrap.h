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
    std::uint32_t os_build_override = 0u;
    bool enable_d3d12_validation = false;
    bool force_diagnostics = false;
    bool emit_readback_values = false;
    bool emit_benchmark_metrics = false;
    bool emit_loaded_modules = false;
    bool has_simulated_failure = false;
    ExitCode simulated_failure = ExitCode::Success;
    EmittedContentFeatureRequest optional_features{};
};

// Parses common CLI flags (--validation, --no-validation, --diagnostics, --frames N,
// --override-os-build N, --emit-readback-values, --emit-benchmark-metrics,
// --emit-loaded-modules, --simulate-failure <CodeName>).
ExitCode ParseBootstrapArgs(int argc, char** argv, BootstrapConfig* out_config) noexcept;

// Queries the active Windows build number via RtlGetVersion (ntdll.dll) on Windows.
std::uint32_t QueryWindowsBuildNumber() noexcept;

// Runs the empty-player platform/bootstrap verification (real OS build check, adapter capability
// contract evaluation, and deterministic exit code).
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
