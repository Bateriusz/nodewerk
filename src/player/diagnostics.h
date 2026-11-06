#pragma once
#include "exit_codes.h"
#include <cstdint>

namespace nw::player {

struct InboxRuntimeBlockerReport {
    std::uint32_t windows_build_number = 0;
    const char* d3d12_dll_version = "unknown";
    std::uint32_t hresult_code = 0;
    const char* failed_api_or_feature = "none";
    const char* blocker_summary = "none";
    bool requires_redistributable_evaluation = false;
};

struct DiagnosticRecord {
    ExitCode exit_code = ExitCode::Success;
    std::uint32_t hresult_code = 0;
    const char* stage = "init";
    const char* detail = "";
    InboxRuntimeBlockerReport blocker{};
};

// Emits structured development diagnostics in dev profile (or when --diagnostics is passed).
// Never throws exceptions or uses RTTI. Returns static_cast<int>(record.exit_code).
int ReportAndConvertExitCode(const DiagnosticRecord& record, bool force_diagnostic_output = false) noexcept;

// Helper to parse failure-simulation flag for automated recovery/exit-code tests.
bool TryParseSimulatedFailure(const char* name, ExitCode* out_code) noexcept;

} // namespace nw::player
