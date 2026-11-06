#include "diagnostics.h"
#include <cstdio>
#include <cstring>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace nw::player {

namespace {

bool StringsEqual(const char* a, const char* b) noexcept {
    if (!a || !b) return false;
    return std::strcmp(a, b) == 0;
}

} // namespace

int ReportAndConvertExitCode(const DiagnosticRecord& record, bool force_diagnostic_output) noexcept {
#if defined(NW_PROFILE_DEV)
    constexpr bool kProfileDev = true;
#else
    constexpr bool kProfileDev = false;
#endif

    if ((kProfileDev || force_diagnostic_output) && record.exit_code != ExitCode::Success) {
        char buffer[1024]{};
        if (record.exit_code == ExitCode::InboxRuntimeBlocker) {
            std::snprintf(
                buffer,
                sizeof(buffer),
                "[NW-DIAG][exit_code=%d][name=%s][category=%s][stage=%s][hr=0x%08X] "
                "INBOX_RUNTIME_BLOCKER win_build=%u d3d12_dll=%s feature=%s summary=\"%s\" "
                "recovery=\"%s\"\n",
                static_cast<int>(record.exit_code),
                ExitCodeName(record.exit_code),
                ExitCodeCategory(record.exit_code),
                record.stage ? record.stage : "unknown",
                record.hresult_code,
                record.blocker.windows_build_number,
                record.blocker.d3d12_dll_version ? record.blocker.d3d12_dll_version : "unknown",
                record.blocker.failed_api_or_feature ? record.blocker.failed_api_or_feature : "unknown",
                record.blocker.blocker_summary ? record.blocker.blocker_summary : "",
                ExitCodeRecoverySemantics(record.exit_code));
        } else {
            std::snprintf(
                buffer,
                sizeof(buffer),
                "[NW-DIAG][exit_code=%d][name=%s][category=%s][stage=%s][hr=0x%08X] "
                "detail=\"%s\" recovery=\"%s\"\n",
                static_cast<int>(record.exit_code),
                ExitCodeName(record.exit_code),
                ExitCodeCategory(record.exit_code),
                record.stage ? record.stage : "unknown",
                record.hresult_code,
                record.detail ? record.detail : "",
                ExitCodeRecoverySemantics(record.exit_code));
        }
        std::fputs(buffer, stderr);
        std::fflush(stderr);
#if defined(_WIN32)
        OutputDebugStringA(buffer);
#endif
    }
    return static_cast<int>(record.exit_code);
}

bool TryParseSimulatedFailure(const char* name, ExitCode* out_code) noexcept {
    if (!name || !out_code) return false;
    static constexpr ExitCode kAllCodes[] = {
        ExitCode::Success,
        ExitCode::InvalidArguments,
        ExitCode::OsVersionUnsupported,
        ExitCode::WindowCreationFailed,
        ExitCode::DxgiFactoryCreationFailed,
        ExitCode::AdapterEnumerationFailed,
        ExitCode::D3D12DeviceCreationFailed,
        ExitCode::CapabilityBaselineUnmet,
        ExitCode::OptionalFeatureUnsupported,
        ExitCode::InboxRuntimeBlocker,
        ExitCode::DebugLayerUnavailable,
        ExitCode::CommandQueueCreationFailed,
        ExitCode::SwapChainCreationFailed,
        ExitCode::DescriptorHeapCreationFailed,
        ExitCode::ResourceAllocationFailed,
        ExitCode::OfflineShaderPayloadInvalid,
        ExitCode::RootSignatureCreationFailed,
        ExitCode::PipelineStateCreationFailed,
        ExitCode::ExecutionFenceTimeout,
        ExitCode::DeviceRemoved,
        ExitCode::ComputeVerificationFailed,
    };
    for (ExitCode candidate : kAllCodes) {
        if (StringsEqual(name, ExitCodeName(candidate))) {
            *out_code = candidate;
            return true;
        }
    }
    return false;
}

} // namespace nw::player
