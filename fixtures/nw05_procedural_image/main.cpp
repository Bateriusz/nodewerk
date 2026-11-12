// NW-05 generated-player fixture.
//
// This translation unit is content-free: the native image produced by
// `nodewerkc export` carries the `.nwp` v1 package and generated link manifest
// in its deterministic embedded footer.  The graph is the sole source of demo
// content; this main only drives the existing PackagePlayer.

#include "player/embedded_package.h"
#include "player/exit_codes.h"
#include "player/package_player.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    nw::player::EmbeddedPackage embedded;
    if (!nw::player::LoadEmbeddedPackage(&embedded)) {
        std::fprintf(stderr, "NW05_EMBEDDED_PACKAGE_INVALID\n");
        return static_cast<int>(nw::player::ExitCode::PackageValidationFailed);
    }

    nw::player::PlayerConfig config;
    config.window_title = "Nodewerk NW-05 Procedural Image";
    std::uint64_t max_frames = 0;
    bool emit_image_hash = false;
    for (int index = 1; index < argc; ++index) {
        if (std::string(argv[index]) == "--frames" && index + 1 < argc) {
            max_frames = std::strtoull(argv[++index], nullptr, 10);
        } else if (std::string(argv[index]) == "--allow-software-adapter") {
            config.allow_software_adapter = true;
        } else if (std::string(argv[index]) == "--force-non-aliased-plan") {
            config.force_non_aliased_plan = true;
        } else if (std::string(argv[index]) == "--emit-image-hash") {
            emit_image_hash = true;
        }
    }
    nw::player::PackagePlayer player(config);
    nw::player::ExitCode status =
        player.initialize(embedded.bytes, embedded.size);
    if (status != nw::player::ExitCode::Success) {
        std::fprintf(stderr, "NW05_INIT_FAILED=%d\n", static_cast<int>(status));
        return static_cast<int>(status);
    }

    for (std::uint64_t tick = 0; max_frames == 0 || tick < max_frames; ++tick) {
        status = player.renderAtTick(tick);
        if (status != nw::player::ExitCode::Success) {
            std::fprintf(stderr, "NW05_FRAME_FAILED=%d\n", static_cast<int>(status));
            return static_cast<int>(status);
        }
    }
    if (emit_image_hash) {
        std::vector<std::uint8_t> image;
        const nw::player::ExitCode readback =
            player.readbackPresentedOutput(&image);
        if (readback != nw::player::ExitCode::Success) {
            std::fprintf(stderr, "NW05_IMAGE_READBACK_FAILED=%d\n",
                         static_cast<int>(readback));
            return static_cast<int>(readback);
        }
        std::uint64_t hash = 1469598103934665603ULL;
        for (const std::uint8_t byte : image) {
            hash ^= static_cast<std::uint64_t>(byte);
            hash *= 1099511628211ULL;
        }
        std::printf("NW05_IMAGE_BYTES=%llu\n",
                    static_cast<unsigned long long>(image.size()));
        std::printf("NW05_IMAGE_FNV64=%016llx\n",
                    static_cast<unsigned long long>(hash));
    }
    status = player.shutdown();
    std::printf(
        "NW05_FRAMES=%llu\n",
        static_cast<unsigned long long>(player.frames_rendered()));
    std::printf(
        "NW05_PRESENTS=%llu\n",
        static_cast<unsigned long long>(player.frames_presented()));
    return status == nw::player::ExitCode::Success ? 0 : static_cast<int>(status);
}

