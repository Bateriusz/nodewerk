// NW-08 optional native authoring preview host.
//
// This executable is part of the ``nodewerk-studio`` authoring product only.
// It drives the unchanged NW-04 ``PackagePlayer`` runtime implementation at an
// absolute tick, with optional diagnostic readback through the existing
// capture-control seam.  It is never linked into ``nodewerk-player`` and never
// part of the release dependency closure.

#include "package_player.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <string>
#include <vector>

namespace {

struct Options {
    std::string package_path;
    std::uint64_t tick = 0u;
    std::uint32_t width = 1920u;
    std::uint32_t height = 1080u;
    bool headless = true;
    std::string capture_dir;
    std::string stats_path;
    std::uint64_t memory_budget_bytes = 0u;
};

void Usage() {
    std::fprintf(
        stderr,
        "usage: nodewerk-studio-preview --package <nwp> [--tick N] "
        "[--width N] [--height N] [--capture-dir DIR] "
        "[--memory-budget N] [--stats-json PATH] [--visible]\n");
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
        } else {
            return false;
        }
    }
    return !options->package_path.empty();
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

bool WriteStats(const std::string& path,
                const nw::player::PlayerAllocationStats& stats,
                bool using_aliased_plan, std::uint32_t width,
                std::uint32_t height, std::uint64_t peak_engine_resource_bytes,
                bool fence_retired) {
    if (path.empty()) {
        return true;
    }
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
        "\"fence_retired\":%s}\n",
        static_cast<unsigned long long>(stats.engine_resource_bytes),
        static_cast<unsigned long long>(peak_engine_resource_bytes),
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
        fence_retired ? "true" : "false");
    std::fclose(file);
    return written > 0;
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
    config.memory_budget_bytes = options.memory_budget_bytes;
    nw::player::PackagePlayer player(config);
    const nw::player::ExitCode initialized =
        player.initializeFromFile(options.package_path.c_str());
    if (initialized != nw::player::ExitCode::Success) {
        return static_cast<int>(initialized);
    }
    const nw::player::ExitCode rendered = player.renderAtTick(options.tick);
    if (rendered != nw::player::ExitCode::Success) {
        player.shutdown();
        return static_cast<int>(rendered);
    }
    if (!options.capture_dir.empty()) {
        std::error_code directory_error;
        std::filesystem::create_directories(options.capture_dir, directory_error);
        if (directory_error) {
            player.shutdown();
            return 1;
        }
        std::vector<std::uint8_t> bytes;
        const nw::player::ExitCode readback =
            player.readbackPresentedOutput(&bytes);
        if (readback != nw::player::ExitCode::Success) {
            player.shutdown();
            return static_cast<int>(readback);
        }
        const std::string path = options.capture_dir + "/tick_" +
                                 std::to_string(options.tick) + ".rgba32f";
        if (!WriteReadback(path, bytes)) {
            player.shutdown();
            return 1;
        }
    }
    const nw::player::PlayerAllocationStats peak_stats =
        player.allocation_stats();
    const bool using_aliased_plan = player.using_aliased_plan();
    const std::uint32_t width = player.width();
    const std::uint32_t height = player.height();
    const nw::player::ExitCode shutdown = player.shutdown();
    const nw::player::PlayerAllocationStats stats = player.allocation_stats();
    const bool fence_retired = shutdown == nw::player::ExitCode::Success;
    if (!WriteStats(options.stats_path, stats, using_aliased_plan, width, height,
                    peak_stats.engine_resource_bytes, fence_retired)) {
        return 1;
    }
    return shutdown == nw::player::ExitCode::Success
               ? 0
               : static_cast<int>(shutdown);
}
