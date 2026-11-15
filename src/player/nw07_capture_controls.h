#pragma once

// NW-07 diagnostic fixed-tick capture controls.
//
// This header compiles to nothing unless the diagnostic sibling build enables
// NW07_CAPTURE_CONTROLS.  The release player never contains tick seeking,
// readback-to-disk, or capture tooling.

#include "player/package_player.h"

#if defined(NW07_CAPTURE_CONTROLS) && NW07_CAPTURE_CONTROLS

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <string>
#include <vector>

namespace nw::player::nw07 {

inline int WriteFixedTickCaptures(
    PackagePlayer* player,
    const std::string& capture_directory,
    const std::vector<std::uint64_t>& ticks) {
    if (player == nullptr || ticks.empty()) {
        return static_cast<int>(ExitCode::InvalidArguments);
    }
    if (capture_directory.empty()) {
        std::fprintf(stderr, "NW07_CAPTURE_DIRECTORY_MISSING\n");
        return static_cast<int>(ExitCode::InvalidArguments);
    }
    for (const std::uint64_t tick : ticks) {
        const ExitCode rendered = player->renderAtTick(tick);
        if (rendered != ExitCode::Success) {
            return player->Report(true);
        }
        std::vector<std::uint8_t> image;
        const ExitCode readback = player->readbackPresentedOutput(&image);
        if (readback != ExitCode::Success) {
            std::fprintf(stderr, "NW07_CAPTURE_READBACK_FAILED=%d\n",
                         static_cast<int>(readback));
            return player->Report(true);
        }
        const std::string path = capture_directory + "/tick_" +
                                 std::to_string(tick) + ".rgba32f";
        std::ofstream output(path, std::ios::binary | std::ios::trunc);
        if (!output) {
            std::fprintf(stderr, "NW07_CAPTURE_OPEN_FAILED=%s\n", path.c_str());
            return static_cast<int>(ExitCode::ResourceAllocationFailed);
        }
        output.write(
            reinterpret_cast<const char*>(image.data()),
            static_cast<std::streamsize>(image.size()));
        output.close();
        if (!output) {
            std::fprintf(stderr, "NW07_CAPTURE_WRITE_FAILED=%s\n", path.c_str());
            return static_cast<int>(ExitCode::ResourceAllocationFailed);
        }
        std::printf("NW07_CAPTURE_TICK=%llu\n",
                    static_cast<unsigned long long>(tick));
        std::printf("NW07_CAPTURE_BYTES=%llu\n",
                    static_cast<unsigned long long>(image.size()));
        std::printf("NW07_CAPTURE_PATH=%s\n", path.c_str());
    }
    std::fflush(stdout);
    return 0;
}

}  // namespace nw::player::nw07

#endif  // NW07_CAPTURE_CONTROLS
