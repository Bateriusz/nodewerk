// NW-08 optional native authoring preview host.
//
// This executable is part of the ``nodewerk-studio`` authoring product only.
// It drives the NW-04 ``PackagePlayer`` runtime core plus the NW-08
// build-tree-only authoring hot-reload overlay at an absolute tick, with
// optional diagnostic readback through the existing
// capture-control seam.  It is never linked into ``nodewerk-player`` and never
// part of the release dependency closure.
//
// Two execution modes are supported:
//
//   * one-shot (default): initialize, render one tick, read back, shut down.
//   * ``--session``: initialize once and keep the same D3D12 device alive while
//     a line protocol on stdin drives additional ticks.  This is the authoring
//     runtime session the Python preview pipeline keeps alive so repeated
//     timeline scrubs reuse the device and its submitted-work retirement
//     fences instead of recreating the whole device for every frame.
//
// The host reports the *actual* selected adapter identity (vendor/device and
// driver) queried from the D3D12 device, measured instrumentation timings when
// the diagnostic profile provides them, and whether shutdown observed the
// complete retirement of all submitted GPU work.  It never reports marketing
// names as identity and never fabricates timing values.

#include "package_player.h"

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <sys/stat.h>
#include <sys/types.h>
#endif

namespace {

struct Options {
    std::string package_path;
    std::uint64_t tick = 0u;
    std::uint32_t width = 1920u;
    std::uint32_t height = 1080u;
    bool headless = true;
    bool session = false;
    bool allow_software_adapter = false;
    std::string capture_dir;
    std::string stats_path;
    std::uint64_t memory_budget_bytes = 0u;
};

void Usage() {
    std::fprintf(
        stderr,
        "usage: nodewerk-studio-preview --package <nwp> [--tick N] "
        "[--width N] [--height N] [--capture-dir DIR] "
        "[--memory-budget N] [--stats-json PATH] [--session] "
        "[--allow-software-adapter] [--visible]\n");
}

bool ParseU64(const char* text, std::uint64_t* value) {
    if (text == nullptr || *text == '\0') {
        return false;
    }
    std::uint64_t result = 0u;
    for (const char* cursor = text; *cursor != '\0'; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            return false;
        }
        result = result * 10u + static_cast<std::uint64_t>(*cursor - '0');
    }
    *value = result;
    return true;
}

bool ParseArgs(int argc, char** argv, Options* options) {
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&](const char** value) -> bool {
            if (i + 1 >= argc) {
                return false;
            }
            *value = argv[++i];
            return true;
        };
        const char* value = nullptr;
        if (arg == "--package" && next(&value)) {
            options->package_path = value;
        } else if (arg == "--capture-dir" && next(&value)) {
            options->capture_dir = value;
        } else if (arg == "--stats-json" && next(&value)) {
            options->stats_path = value;
        } else if (arg == "--memory-budget" && next(&value)) {
            if (!ParseU64(value, &options->memory_budget_bytes)) {
                return false;
            }
        } else if (arg == "--tick" && next(&value)) {
            if (!ParseU64(value, &options->tick)) {
                return false;
            }
        } else if (arg == "--width" && next(&value)) {
            options->width = static_cast<std::uint32_t>(std::strtoul(value, nullptr, 10));
        } else if (arg == "--height" && next(&value)) {
            options->height = static_cast<std::uint32_t>(std::strtoul(value, nullptr, 10));
        } else if (arg == "--visible") {
            options->headless = false;
        } else if (arg == "--session") {
            options->session = true;
        } else if (arg == "--allow-software-adapter") {
            options->allow_software_adapter = true;
        } else {
            return false;
        }
    }
    return !options->package_path.empty();
}

void AppendJsonEscaped(std::string* out, const std::string& value) {
    out->push_back('"');
    for (char character : value) {
        switch (character) {
            case '"': out->append("\\\""); break;
            case '\\': out->append("\\\\"); break;
            case '\n': out->append("\\n"); break;
            case '\r': out->append("\\r"); break;
            case '\t': out->append("\\t"); break;
            default:
                if (static_cast<unsigned char>(character) < 0x20u) {
                    char buffer[8]{};
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x",
                                  static_cast<unsigned>(character));
                    out->append(buffer);
                } else {
                    out->push_back(character);
                }
                break;
        }
    }
    out->push_back('"');
}

bool WriteReadback(const std::string& path, const std::vector<std::uint8_t>& bytes) {
    std::FILE* file = std::fopen(path.c_str(), "wb");
    if (file == nullptr) {
        return false;
    }
    const std::size_t written =
        std::fwrite(bytes.data(), 1u, bytes.size(), file);
    std::fclose(file);
    return written == bytes.size();
}

struct TimingSnapshot {
    bool cpu_available = false;
    bool gpu_available = false;
    double cpu_submit_ms = 0.0;
    double gpu_ms = 0.0;
    std::string instructions_json;
    std::string pass_timings_json;
};

TimingSnapshot CaptureTimings(const nw::player::PackagePlayer& player) {
    TimingSnapshot timing{};
#if NW_PLAYER_DIAGNOSTICS
    const nw::player::PlayerTiming& measured = player.instrumentation().timing();
    timing.cpu_available = measured.cpu_available;
    timing.gpu_available = measured.gpu_available;
    timing.cpu_submit_ms = measured.cpu_submit_ms;
    timing.gpu_ms = measured.gpu_ms;
#else
    (void)player;
#endif
    timing.instructions_json = "[";
    const std::vector<std::string>& order = player.last_frame_instruction_order();
    for (std::size_t index = 0u; index < order.size(); ++index) {
        if (index != 0u) {
            timing.instructions_json.append(",");
        }
        AppendJsonEscaped(&timing.instructions_json, order[index]);
    }
    timing.instructions_json.append("]");
    timing.pass_timings_json = "[";
    const auto& pass_timings = player.authoring_pass_timings();
    for (std::size_t index = 0u; index < pass_timings.size(); ++index) {
        if (index != 0u) {
            timing.pass_timings_json.append(",");
        }
        timing.pass_timings_json.append("{\"name\":");
        AppendJsonEscaped(&timing.pass_timings_json, pass_timings[index].name);
        char milliseconds[64]{};
        std::snprintf(milliseconds, sizeof(milliseconds), "%.6f",
                      pass_timings[index].milliseconds);
        timing.pass_timings_json.append(",\"milliseconds\":");
        timing.pass_timings_json.append(milliseconds);
        timing.pass_timings_json.append("}");
    }
    timing.pass_timings_json.append("]");
    return timing;
}

bool WriteStats(const std::string& path, const nw::player::PackagePlayer& player,
                const nw::player::PlayerAllocationStats& peak_stats,
                bool using_aliased_plan, std::uint32_t width,
                std::uint32_t height, bool fence_retired,
                std::uint64_t session_renders,
                const TimingSnapshot& timing,
                const nw::player::QueriedAdapterCapabilities* adapter_override =
                    nullptr,
                const nw::player::AuthoringAdapterIdentity* identity_override =
                    nullptr,
                const nw::player::AuthoringReloadResult* reload_override =
                    nullptr) {
    if (path.empty()) {
        return true;
    }
    const nw::player::PlayerAllocationStats& stats = player.allocation_stats();
    const nw::player::QueriedAdapterCapabilities adapter =
        adapter_override != nullptr ? *adapter_override
                                    : player.queried_capabilities();
    const nw::player::AuthoringAdapterIdentity identity =
        identity_override != nullptr ? *identity_override
                                     : player.authoringAdapterIdentity();
    const nw::player::AuthoringReloadResult reload =
        reload_override != nullptr ? *reload_override
                                   : player.authoring_reload_stats();
    std::string driver_version;
    AppendJsonEscaped(&driver_version, adapter.driver_version);
    std::FILE* file = std::fopen(path.c_str(), "wb");
    if (file == nullptr) {
        return false;
    }
    const int written = std::fprintf(
        file,
        "{\"engine_resource_bytes\":%llu,\"peak_engine_resource_bytes\":%llu,"
        "\"engine_cumulative_resource_bytes\":%llu,"
        "\"engine_upload_bytes\":%llu,\"engine_descriptor_count\":%llu,"
        "\"engine_heap_count\":%llu,\"package_plan_bytes\":%llu,"
        "\"allocation_calls\":%llu,\"frames_rendered\":%llu,"
        "\"frames_presented\":%llu,\"using_aliased_plan\":%s,"
        "\"width\":%u,\"height\":%u,\"post_initialization_allocation_violation\":%s,"
        "\"fence_retired\":%s,\"session_renders\":%llu,"
        "\"adapter_vendor_id\":%u,\"adapter_device_id\":%u,"
        "\"adapter_subsystem_id\":%u,\"adapter_luid_low\":%u,"
        "\"adapter_luid_high\":%d,\"adapter_is_software\":%s,"
        "\"adapter_identity_queried\":%s,\"driver_version\":%s,"
        "\"windows_build_number\":%u,"
        "\"runtime_cpu_submit_ms\":%.6f,\"runtime_cpu_available\":%s,"
        "\"runtime_gpu_ms\":%.6f,\"runtime_gpu_available\":%s,"
        "\"frame_instructions\":%s,\"runtime_pass_timings\":%s,"
        "\"authoring_reused_resources\":%u,"
        "\"authoring_recreated_resources\":%u,"
        "\"authoring_reused_root_signatures\":%u,"
        "\"authoring_recreated_root_signatures\":%u,"
        "\"authoring_reused_pipelines\":%u,"
        "\"authoring_recreated_pipelines\":%u,"
        "\"authoring_executed_init_dispatches\":%u,"
        "\"authoring_skipped_init_dispatches\":%u,"
        "\"package_sha256\":\"%s\"}\n",
        static_cast<unsigned long long>(stats.engine_resource_bytes),
        static_cast<unsigned long long>(peak_stats.engine_resource_bytes),
        static_cast<unsigned long long>(stats.engine_cumulative_resource_bytes),
        static_cast<unsigned long long>(stats.engine_upload_bytes),
        static_cast<unsigned long long>(stats.engine_descriptor_count),
        static_cast<unsigned long long>(stats.engine_heap_count),
        static_cast<unsigned long long>(stats.package_plan_bytes),
        static_cast<unsigned long long>(stats.allocation_calls),
        static_cast<unsigned long long>(stats.frames_rendered),
        static_cast<unsigned long long>(stats.frames_presented),
        using_aliased_plan ? "true" : "false",
        width,
        height,
        stats.post_initialization_allocation_violation ? "true" : "false",
        fence_retired ? "true" : "false",
        static_cast<unsigned long long>(session_renders),
        identity.vendor_id,
        identity.device_id,
        identity.subsystem_id,
        identity.luid_low,
        identity.luid_high,
        identity.software ? "true" : "false",
        identity.available ? "true" : "false",
        driver_version.c_str(),
        adapter.windows_build_number,
        timing.cpu_submit_ms,
        timing.cpu_available ? "true" : "false",
        timing.gpu_ms,
        timing.gpu_available ? "true" : "false",
        timing.instructions_json.c_str(),
        timing.pass_timings_json.c_str(),
        reload.reused_resources,
        reload.recreated_resources,
        reload.reused_root_signatures,
        reload.recreated_root_signatures,
        reload.reused_pipelines,
        reload.recreated_pipelines,
        reload.executed_init_dispatches,
        reload.skipped_init_dispatches,
        player.package_sha256().c_str());
    std::fclose(file);
    return written > 0;
}

bool EnsureDirectory(const std::string& path) {
    if (path.empty()) {
        return true;
    }
#if defined(_WIN32)
    std::string current;
    current.reserve(path.size());
    for (std::size_t index = 0u; index < path.size(); ++index) {
        const char character = path[index];
        current.push_back(character);
        const bool separator = character == '/' || character == '\\';
        const bool last = index + 1u == path.size();
        if ((separator || last) && current.size() > 1u) {
            if (current.back() != ':' ) {
                CreateDirectoryA(current.c_str(), nullptr);
            }
        }
    }
    const DWORD attributes = GetFileAttributesA(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES &&
           (attributes & FILE_ATTRIBUTE_DIRECTORY) != 0u;
#else
    return ::mkdir(path.c_str(), 0777) == 0 || errno == EEXIST;
#endif
}

std::string ParentDirectory(const std::string& path) {
    const std::size_t separator = path.find_last_of("/\\");
    if (separator == std::string::npos) {
        return std::string();
    }
    return path.substr(0u, separator);
}

bool CaptureFrame(nw::player::PackagePlayer* player,
                  const std::string& capture_dir, std::uint64_t tick) {
    if (capture_dir.empty()) {
        return true;
    }
    if (!EnsureDirectory(capture_dir)) {
        return false;
    }
    std::vector<std::uint8_t> bytes;
    if (player->readbackPresentedOutput(&bytes) != nw::player::ExitCode::Success) {
        return false;
    }
    const std::string path = capture_dir + "/tick_" +
                             std::to_string(tick) + ".rgba32f";
    return WriteReadback(path, bytes);
}

int DeviceRemovedExitCode() {
    return static_cast<int>(nw::player::ExitCode::DeviceRemoved);
}

bool IsDeviceRemoved(int exit_code) {
    return exit_code == DeviceRemovedExitCode();
}

void EmitReady(const nw::player::PackagePlayer& player) {
    const nw::player::QueriedAdapterCapabilities& adapter =
        player.queried_capabilities();
    const nw::player::AuthoringAdapterIdentity identity =
        player.authoringAdapterIdentity();
    std::string driver_version;
    AppendJsonEscaped(&driver_version, adapter.driver_version);
    std::printf(
        "{\"event\":\"ready\",\"adapter_vendor_id\":%u,"
        "\"adapter_device_id\":%u,\"adapter_subsystem_id\":%u,"
        "\"adapter_luid_low\":%u,\"adapter_luid_high\":%d,"
        "\"adapter_is_software\":%s,\"adapter_identity_queried\":%s,"
        "\"driver_version\":%s,\"windows_build_number\":%u,"
        "\"package_sha256\":\"%s\"}\n",
        identity.vendor_id, identity.device_id, identity.subsystem_id,
        identity.luid_low, identity.luid_high,
        identity.software ? "true" : "false",
        identity.available ? "true" : "false", driver_version.c_str(),
        adapter.windows_build_number, player.package_sha256().c_str());
    std::fflush(stdout);
}

int RunSession(const Options& options, nw::player::PackagePlayer* player) {
    std::uint64_t session_renders = 0u;
    if (!EnsureDirectory(options.capture_dir)) {
        return 1;
    }
    if (!EnsureDirectory(ParentDirectory(options.stats_path))) {
        return 1;
    }
    EmitReady(*player);
    char line[512]{};
    while (std::fgets(line, static_cast<int>(sizeof(line)), stdin) != nullptr) {
        std::string command(line);
        while (!command.empty() &&
               (command.back() == '\n' || command.back() == '\r')) {
            command.pop_back();
        }
        if (command == "quit") {
            const nw::player::ExitCode shutdown = player->shutdown();
            const bool retired = shutdown == nw::player::ExitCode::Success;
            std::printf("{\"event\":\"shutdown\",\"ok\":%s,\"fence_retired\":%s,"
                        "\"session_renders\":%llu}\n",
                        retired ? "true" : "false",
                        retired ? "true" : "false",
                        static_cast<unsigned long long>(session_renders));
            std::fflush(stdout);
            return retired ? 0 : static_cast<int>(shutdown);
        }
        if (command.rfind("load ", 0u) == 0u) {
            const std::string package_path = command.substr(5u);
            if (package_path.empty()) {
                std::printf(
                    "{\"event\":\"reload\",\"ok\":false,"
                    "\"requires_device_recreation\":false,"
                    "\"exit_code\":2,\"message\":\"empty package path\"}\n");
                std::fflush(stdout);
                continue;
            }
            const nw::player::AuthoringReloadResult reload =
                player->reloadForAuthoringFromFile(package_path.c_str());
            const int exit_code = static_cast<int>(reload.exit_code);
            const bool ok = reload.exit_code == nw::player::ExitCode::Success;
            std::printf(
                "{\"event\":\"reload\",\"ok\":%s,"
                "\"requires_device_recreation\":%s,\"exit_code\":%d,"
                "\"reused_resources\":%u,\"recreated_resources\":%u,"
                "\"reused_root_signatures\":%u,"
                "\"recreated_root_signatures\":%u,"
                "\"reused_pipelines\":%u,\"recreated_pipelines\":%u,"
                "\"executed_init_dispatches\":%u,"
                "\"skipped_init_dispatches\":%u,"
                "\"package_sha256\":\"%s\"}\n",
                ok ? "true" : "false",
                reload.requires_device_recreation ? "true" : "false",
                exit_code, reload.reused_resources, reload.recreated_resources,
                reload.reused_root_signatures,
                reload.recreated_root_signatures,
                reload.reused_pipelines, reload.recreated_pipelines,
                reload.executed_init_dispatches, reload.skipped_init_dispatches,
                player->package_sha256().c_str());
            std::fflush(stdout);
            continue;
        }
        if (command == "lose-device") {
            player->SetForceDeviceRemovedForTesting(true);
            std::printf("{\"event\":\"device-lost\",\"ok\":true}\n");
            std::fflush(stdout);
            continue;
        }
        std::uint64_t tick = 0u;
        if (command.rfind("render ", 0u) == 0u) {
            const char* tick_text = command.c_str() + 7;
            if (!ParseU64(tick_text, &tick)) {
                std::printf("{\"event\":\"error\",\"message\":\"bad tick\"}\n");
                std::fflush(stdout);
                continue;
            }
        } else if (!ParseU64(command.c_str(), &tick)) {
            std::printf("{\"event\":\"error\",\"message\":\"unknown command\"}\n");
            std::fflush(stdout);
            continue;
        }
        const nw::player::ExitCode rendered = player->renderAtTick(tick);
        ++session_renders;
        if (rendered != nw::player::ExitCode::Success) {
            const int exit_code = static_cast<int>(rendered);
            const TimingSnapshot timing = CaptureTimings(*player);
            (void)WriteStats(options.stats_path, *player,
                             player->allocation_stats(),
                             player->using_aliased_plan(), player->width(),
                             player->height(), false, session_renders, timing);
            std::printf(
                "{\"event\":\"render-failed\",\"ok\":false,\"tick\":%llu,"
                "\"exit_code\":%d,\"device_removed\":%s}\n",
                static_cast<unsigned long long>(tick), exit_code,
                IsDeviceRemoved(exit_code) ? "true" : "false");
            std::fflush(stdout);
            if (IsDeviceRemoved(exit_code)) {
                return exit_code;
            }
            continue;
        }
        const bool captured = CaptureFrame(player, options.capture_dir, tick);
        const TimingSnapshot timing = CaptureTimings(*player);
        const bool stats_ok =
            WriteStats(options.stats_path, *player, player->allocation_stats(),
                       player->using_aliased_plan(), player->width(),
                       player->height(), true, session_renders, timing);
        std::printf(
            "{\"event\":\"rendered\",\"ok\":%s,\"tick\":%llu,"
            "\"capture\":%s,\"stats\":%s,\"exit_code\":0}\n",
            (captured && stats_ok) ? "true" : "false",
            static_cast<unsigned long long>(tick),
            captured ? "true" : "false", stats_ok ? "true" : "false");
        std::fflush(stdout);
    }
    const nw::player::ExitCode shutdown = player->shutdown();
    return shutdown == nw::player::ExitCode::Success
               ? 0
               : static_cast<int>(shutdown);
}

}  // namespace

int main(int argc, char** argv) {
    Options options;
    if (!ParseArgs(argc, argv, &options)) {
        Usage();
        return 2;
    }
    nw::player::PlayerConfig config;
    config.width = options.width;
    config.height = options.height;
    config.headless = options.headless;
    config.visible_window = !options.headless;
    config.create_window = !options.headless;
    config.enable_validation = false;
    config.allow_software_adapter = options.allow_software_adapter;
    config.memory_budget_bytes = options.memory_budget_bytes;
    config.force_non_aliased_plan = true;
    // The authoring host waits for submitted GPU work before it reports a
    // result.  This is the fence boundary the Python session uses to retire
    // realizations and to guarantee that readback never races presentation.
    config.wait_for_gpu_each_frame = true;
    nw::player::PackagePlayer player(config);
    const nw::player::ExitCode initialized =
        player.initializeFromFile(options.package_path.c_str());
    if (initialized != nw::player::ExitCode::Success) {
        if (options.session) {
            std::printf("{\"event\":\"init-failed\",\"exit_code\":%d}\n",
                        static_cast<int>(initialized));
            std::fflush(stdout);
        }
        return static_cast<int>(initialized);
    }
    if (options.session) {
        return RunSession(options, &player);
    }
    const nw::player::ExitCode rendered = player.renderAtTick(options.tick);
    if (rendered != nw::player::ExitCode::Success) {
        const TimingSnapshot timing = CaptureTimings(player);
        (void)WriteStats(options.stats_path, player, player.allocation_stats(),
                         player.using_aliased_plan(), player.width(),
                         player.height(), false, 1u, timing);
        player.shutdown();
        return static_cast<int>(rendered);
    }
    if (!CaptureFrame(&player, options.capture_dir, options.tick)) {
        player.shutdown();
        return 1;
    }
    const nw::player::PlayerAllocationStats peak_stats =
        player.allocation_stats();
    const bool using_aliased_plan = player.using_aliased_plan();
    const std::uint32_t width = player.width();
    const std::uint32_t height = player.height();
    const TimingSnapshot timing = CaptureTimings(player);
    const nw::player::QueriedAdapterCapabilities adapter =
        player.queried_capabilities();
    const nw::player::AuthoringAdapterIdentity identity =
        player.authoringAdapterIdentity();
    const nw::player::AuthoringReloadResult reload =
        player.authoring_reload_stats();
    const nw::player::ExitCode shutdown = player.shutdown();
    const bool fence_retired = shutdown == nw::player::ExitCode::Success;
    if (!WriteStats(options.stats_path, player, peak_stats, using_aliased_plan,
                    width, height, fence_retired, 1u, timing, &adapter,
                    &identity, &reload)) {
        return 1;
    }
    return fence_retired ? 0 : static_cast<int>(shutdown);
}
