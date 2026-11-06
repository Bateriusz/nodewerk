#include "../../src/player/d3d12_bootstrap.h"

int main(int argc, char** argv) {
    nw::player::BootstrapConfig config{};
    config.fixture_name = "compute_shader";
    const nw::player::ExitCode parse_status = nw::player::ParseBootstrapArgs(argc, argv, &config);
    if (parse_status != nw::player::ExitCode::Success) {
        nw::player::DiagnosticRecord rec{
            parse_status,
            0x80070057u,
            "cli_parse",
            "Invalid command-line argument passed to compute_shader fixture",
            {}};
        return nw::player::ReportAndConvertExitCode(rec, true);
    }

    const nw::player::DiagnosticRecord result = nw::player::ExecuteComputeShaderFixture(config);
    return nw::player::ReportAndConvertExitCode(result, config.force_diagnostics);
}
