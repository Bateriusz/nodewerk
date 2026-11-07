#pragma once
#include <cstdint>

namespace nw::player {

// Distinct exit codes for initialization, capability, and execution failures.
// Player builds compile without C++ exceptions (/EHs-c-) and without RTTI (/GR-);
// all initialization and runtime errors propagate deterministically via ExitCode.
enum class ExitCode : int {
    Success = 0,

    // CLI / Environment / OS validation (10-19)
    InvalidArguments = 10,
    OsVersionUnsupported = 11,
    WindowCreationFailed = 12,

    // DXGI / Adapter / Device / Baseline Capability checks (20-29)
    DxgiFactoryCreationFailed = 20,
    AdapterEnumerationFailed = 21,
    D3D12DeviceCreationFailed = 22,
    CapabilityBaselineUnmet = 23,
    OptionalFeatureUnsupported = 24,
    InboxRuntimeBlocker = 25,
    DebugLayerUnavailable = 26,

    // Queue / SwapChain / Descriptor Heap / Memory allocation (30-39)
    CommandQueueCreationFailed = 30,
    SwapChainCreationFailed = 31,
    DescriptorHeapCreationFailed = 32,
    ResourceAllocationFailed = 33,
    CommandListRecordingFailed = 34,
    PresentationFailed = 35,
    PresentationOccluded = 36,

    // Offline Shader / RootSignature / Pipeline State (40-49)
    OfflineShaderPayloadInvalid = 40,
    RootSignatureCreationFailed = 41,
    PipelineStateCreationFailed = 42,

    // Command Execution / Synchronization / Verification (50-69)
    ExecutionFenceTimeout = 50,
    DeviceRemoved = 51,
    GpuSubmissionFailed = 52,
    ComputeVerificationFailed = 60,
    D3D12ValidationError = 61,
};

constexpr const char* ExitCodeName(ExitCode code) noexcept {
    switch (code) {
        case ExitCode::Success: return "Success";
        case ExitCode::InvalidArguments: return "InvalidArguments";
        case ExitCode::OsVersionUnsupported: return "OsVersionUnsupported";
        case ExitCode::WindowCreationFailed: return "WindowCreationFailed";
        case ExitCode::DxgiFactoryCreationFailed: return "DxgiFactoryCreationFailed";
        case ExitCode::AdapterEnumerationFailed: return "AdapterEnumerationFailed";
        case ExitCode::D3D12DeviceCreationFailed: return "D3D12DeviceCreationFailed";
        case ExitCode::CapabilityBaselineUnmet: return "CapabilityBaselineUnmet";
        case ExitCode::OptionalFeatureUnsupported: return "OptionalFeatureUnsupported";
        case ExitCode::InboxRuntimeBlocker: return "InboxRuntimeBlocker";
        case ExitCode::DebugLayerUnavailable: return "DebugLayerUnavailable";
        case ExitCode::CommandQueueCreationFailed: return "CommandQueueCreationFailed";
        case ExitCode::SwapChainCreationFailed: return "SwapChainCreationFailed";
        case ExitCode::DescriptorHeapCreationFailed: return "DescriptorHeapCreationFailed";
        case ExitCode::ResourceAllocationFailed: return "ResourceAllocationFailed";
        case ExitCode::CommandListRecordingFailed: return "CommandListRecordingFailed";
        case ExitCode::PresentationFailed: return "PresentationFailed";
        case ExitCode::PresentationOccluded: return "PresentationOccluded";
        case ExitCode::OfflineShaderPayloadInvalid: return "OfflineShaderPayloadInvalid";
        case ExitCode::RootSignatureCreationFailed: return "RootSignatureCreationFailed";
        case ExitCode::PipelineStateCreationFailed: return "PipelineStateCreationFailed";
        case ExitCode::ExecutionFenceTimeout: return "ExecutionFenceTimeout";
        case ExitCode::DeviceRemoved: return "DeviceRemoved";
        case ExitCode::GpuSubmissionFailed: return "GpuSubmissionFailed";
        case ExitCode::ComputeVerificationFailed: return "ComputeVerificationFailed";
        case ExitCode::D3D12ValidationError: return "D3D12ValidationError";
    }
    return "UnknownExitCode";
}

constexpr const char* ExitCodeCategory(ExitCode code) noexcept {
    const int value = static_cast<int>(code);
    if (value == 0) return "ok";
    if (value >= 10 && value <= 19) return "platform_bootstrap";
    if (value >= 20 && value <= 29) return "device_and_capability";
    if (value >= 30 && value <= 39) return "resource_and_queue_init";
    if (value >= 40 && value <= 49) return "offline_shader_and_pipeline";
    if (value >= 50 && value <= 69) return "gpu_execution_and_validation";
    return "unclassified";
}

constexpr const char* ExitCodeRecoverySemantics(ExitCode code) noexcept {
    switch (code) {
        case ExitCode::Success:
            return "Normal completion; release GPU/Win32 resources deterministically.";
        case ExitCode::InboxRuntimeBlocker:
            return "STOP: Record exact Windows build, inbox d3d12.dll version, HRESULT, and capability blocker before proposing any profile or redistributable change. Any added redistributable counts toward total_distributed_bytes.";
        case ExitCode::CapabilityBaselineUnmet:
            return "Abort initialization; adapter does not satisfy D3D_FEATURE_LEVEL_12_0 + Shader Model 6.0 baseline.";
        case ExitCode::OptionalFeatureUnsupported:
            return "Emitted package requested an optional queried feature unavailable on this adapter; abort or select fallback permutation if declared in manifest.";
        case ExitCode::DebugLayerUnavailable:
            return "Development validation requested (--validation), but Graphics Tools (d3d12sdklayers.dll) is not installed.";
        case ExitCode::D3D12ValidationError:
            return "STOP: the D3D12 debug layer emitted error/corruption messages; preserve the info-queue diagnostics and fix the offending API usage before treating the run as passing.";
        case ExitCode::PresentationOccluded:
            return "Treat the run as unmeasured: ensure the fixture window is visible and unoccluded, then rerun. DXGI_STATUS_OCCLUDED must never be accepted as a successful visible-present measurement.";
        case ExitCode::OfflineShaderPayloadInvalid:
            return "Abort pipeline creation; do NOT fall back to runtime shader compilation (dxcompiler.dll / D3DCompile).";
        default:
            return "Abort execution cleanly, release allocated COM interfaces, emit dev diagnostic, and return distinct exit code.";
    }
}

} // namespace nw::player
