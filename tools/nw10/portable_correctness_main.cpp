// NW-10 portable common-artifact correctness capture runner.
//
// Built on the qualified build host with the NW-07 release profile. It does
// not use NW07_CAPTURE_CONTROLS and therefore does not require the dev/debug
// CRT. Fixed-tick qualification uses PackagePlayer::readbackPresentedOutput(),
// which reads the exact RGBA32F frame.present resource.

#include "player/embedded_package.h"
#include "player/exit_codes.h"
#include "player/package_player.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#if defined(_WIN32)
#include <windows.h>
#endif

namespace {

constexpr std::uint64_t kExpectedCaptureBytes =
    1920ull * 1080ull * 4ull * sizeof(float);

bool EnsureDirectory(const std::string& path) {
#if defined(_WIN32)
    if (path.empty()) return false;
    if (CreateDirectoryA(path.c_str(), nullptr) != FALSE) return true;
    return GetLastError() == ERROR_ALREADY_EXISTS;
#else
    (void)path;
    return false;
#endif
}

bool WriteBytes(const std::string& path,
                const std::vector<std::uint8_t>& bytes) {
    FILE* stream = nullptr;
#if defined(_MSC_VER)
    if (fopen_s(&stream, path.c_str(), "wb") != 0 || stream == nullptr) {
        return false;
    }
#else
    stream = std::fopen(path.c_str(), "wb");
    if (stream == nullptr) return false;
#endif
    const std::size_t written =
        std::fwrite(bytes.data(), 1u, bytes.size(), stream);
    const int close_status = std::fclose(stream);
    return written == bytes.size() && close_status == 0;
}

std::string CapturePath(const std::string& directory, std::uint64_t tick) {
    std::string result = directory;
    if (!result.empty() && result.back() != '\\' && result.back() != '/') {
        result.push_back('\\');
    }
    result += "tick_";
    result += std::to_string(tick);
    result += ".rgba32f";
    return result;
}

}  // namespace

int main(int argc, char** argv) {
    std::string capture_directory;
    std::vector<std::uint64_t> capture_ticks;
    std::uint32_t required_mask = 0u;
    bool probe_only = false;

    for (int index = 1; index < argc; ++index) {
        const std::string arg = argv[index];
        if (arg == "--probe") {
            probe_only = true;
        } else if (arg == "--capture-dir" && index + 1 < argc) {
            capture_directory = argv[++index];
        } else if (arg == "--capture-tick" && index + 1 < argc) {
            capture_ticks.push_back(std::strtoull(argv[++index], nullptr, 10));
        } else if (arg == "--required-mask" && index + 1 < argc) {
            required_mask = static_cast<std::uint32_t>(
                std::strtoul(argv[++index], nullptr, 0));
        } else {
            std::fprintf(stderr, "NW10_PORTABLE_INVALID_ARGUMENT=%s\n",
                         arg.c_str());
            return 64;
        }
    }

    nw::player::EmbeddedPackage embedded;
    if (!nw::player::LoadEmbeddedPackage(&embedded)) {
        std::fprintf(stderr, "NW10_PORTABLE_EMBEDDED_PACKAGE_INVALID\n");
        return static_cast<int>(
            nw::player::ExitCode::PackageValidationFailed);
    }
    nw::player::EmbeddedLinkMetadata link_metadata;
    if (!nw::player::ParseEmbeddedLinkMetadata(embedded, &link_metadata)) {
        std::fprintf(stderr, "NW10_PORTABLE_LINK_METADATA_INVALID\n");
        return static_cast<int>(
            nw::player::ExitCode::PackageValidationFailed);
    }

    const std::uint32_t implementation_mask =
        nw::player::PackagePlayer::specialized_implementation_mask();
    if ((implementation_mask & required_mask) != required_mask) {
        std::fprintf(
            stderr,
            "NW10_PORTABLE_IMPLEMENTATION_MASK_MISMATCH=%u/%u\n",
            implementation_mask, required_mask);
        return 70;
    }

    nw::player::PlayerConfig config;
    config.window_title = "Nodewerk NW-10 Portable Correctness";
    config.width = 1920u;
    config.height = 1080u;
    config.present_sync_interval = 0u;
    config.frames_in_flight = 2u;
    config.query_optional_wave_ops = link_metadata.wave_ops;
    config.query_optional_fp16_shader_ops = link_metadata.fp16_shader_ops;
    config.query_optional_int64_shader_ops = link_metadata.int64_shader_ops;
    config.memory_budget_bytes = link_metadata.memory_budget_bytes;

    nw::player::PackagePlayer player(config);
    const nw::player::ExitCode initialized =
        player.initialize(embedded.bytes, embedded.size);
    if (initialized != nw::player::ExitCode::Success) {
        std::fprintf(stderr, "NW10_PORTABLE_INIT_FAILED=%d\n",
                     static_cast<int>(initialized));
        return static_cast<int>(initialized);
    }

    std::printf("NW10_PACKAGE_SHA=%s\n", player.package_sha256().c_str());
    std::printf("NW10_RUNTIME_IMPLEMENTATION_MASK=%u\n",
                implementation_mask);
    std::printf("NW10_PRESENT_SYNC_INTERVAL=%u\n",
                config.present_sync_interval);
    std::printf("NW10_ADAPTER=%s\n",
                player.queried_capabilities().adapter_description.c_str());
    std::printf("NW10_DRIVER=%s\n",
                player.queried_capabilities().driver_version.c_str());
    std::printf("NW10_VENDOR_ID=%u\n",
                player.queried_capabilities().vendor_id);
    std::printf("NW10_DEVICE_ID=%u\n",
                player.queried_capabilities().device_id);

    if (probe_only) {
        const auto shutdown = player.shutdown();
        return shutdown == nw::player::ExitCode::Success
                   ? 0
                   : static_cast<int>(shutdown);
    }

    if (capture_directory.empty() || capture_ticks.empty() ||
        !EnsureDirectory(capture_directory)) {
        std::fprintf(stderr, "NW10_PORTABLE_CAPTURE_ARGUMENTS_INVALID\n");
        (void)player.shutdown();
        return 64;
    }

    for (const std::uint64_t tick : capture_ticks) {
        const nw::player::ExitCode rendered = player.renderAtTick(tick);
        if (rendered != nw::player::ExitCode::Success) {
            std::fprintf(stderr, "NW10_PORTABLE_RENDER_FAILED=%llu/%d\n",
                         static_cast<unsigned long long>(tick),
                         static_cast<int>(rendered));
            (void)player.shutdown();
            return static_cast<int>(rendered);
        }
        std::vector<std::uint8_t> image;
        const nw::player::ExitCode readback =
            player.readbackPresentedOutput(&image);
        if (readback != nw::player::ExitCode::Success) {
            std::fprintf(stderr, "NW10_PORTABLE_READBACK_FAILED=%llu/%d\n",
                         static_cast<unsigned long long>(tick),
                         static_cast<int>(readback));
            (void)player.shutdown();
            return static_cast<int>(readback);
        }
        if (image.size() != kExpectedCaptureBytes) {
            std::fprintf(stderr,
                         "NW10_PORTABLE_CAPTURE_SIZE_INVALID=%llu/%llu\n",
                         static_cast<unsigned long long>(image.size()),
                         static_cast<unsigned long long>(
                             kExpectedCaptureBytes));
            (void)player.shutdown();
            return 70;
        }
        const std::string path = CapturePath(capture_directory, tick);
        if (!WriteBytes(path, image)) {
            std::fprintf(stderr, "NW10_PORTABLE_CAPTURE_WRITE_FAILED=%llu\n",
                         static_cast<unsigned long long>(tick));
            (void)player.shutdown();
            return 74;
        }
        std::printf("NW10_CAPTURED_TICK=%llu\n",
                    static_cast<unsigned long long>(tick));
    }

    const auto shutdown = player.shutdown();
    if (shutdown != nw::player::ExitCode::Success) {
        return static_cast<int>(shutdown);
    }
    std::printf("NW10_CAPTURE_COMPLETE=1\n");
    return 0;
}
