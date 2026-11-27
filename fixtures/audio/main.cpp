#include "audio/procedural_audio.h"
#include "audio/wasapi_output.h"
#include "player/package_player.h"
#include <array>
#include <cstdio>
#include <fstream>
#include <windows.h>

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "Usage: nw_audio_player scene.nwp\n");
        return 10;
    }
    nw::player::PlayerConfig config;
    config.allow_audio_package = true;
    config.window_title = "Nodewerk audio playback";
    nw::player::PackagePlayer player(config);
    auto status = player.initializeFromFile(argv[1]);
    if (status != nw::player::ExitCode::Success) return static_cast<int>(status);
    const auto& payload = player.package().audio_song;
    nw::audio::Song song{};
    if (!payload || !nw::audio::DecodeSong(payload->data(), payload->size(), song)) return 70;
    nw::audio::WasapiOutput output;
    if (!output.start(song)) {
        std::fprintf(stderr, "NW_AUDIO_INITIALIZATION_FAILED\n");
        return 80; // explicit fail policy for this opt-in diagnostic target
    }
    while (!player.window_closed() && !output.failed()) {
        const auto tick = output.now_ticks();
        if (tick >= song.duration_ticks) break;
        status = player.renderAtTick(tick);
        if (status != nw::player::ExitCode::Success) break;
        Sleep(1);
    }
    output.stop();
    std::printf("AUDIO_STARVATION_OBSERVATIONS=%llu\nAUDIO_CLOCK_DISCONTINUITIES=%llu\nAUDIO_SYNTHESIS_CPU_100NS=%llu\n",
        static_cast<unsigned long long>(output.starvation_observations()),
        static_cast<unsigned long long>(output.clock_discontinuities()),
        static_cast<unsigned long long>(output.synthesis_cpu_100ns()));
    std::printf("AUDIO_OBSERVED_UNDERRUNS=%llu\nAUDIO_MISSING_SAMPLES_LOWER_BOUND=%llu\nAUDIO_PHYSICAL_UNDERRUNS=unavailable\nAUDIO_SYNTHESIS_CPU_AVAILABLE=%u\n",
        static_cast<unsigned long long>(output.observed_underruns()),
        static_cast<unsigned long long>(output.missing_samples_lower_bound()),
        output.synthesis_cpu_available() ? 1u : 0u);
    std::printf("AUDIO_WORKER_CPU_100NS=%llu\nAUDIO_WORKER_CPU_AVAILABLE=%u\n",
        static_cast<unsigned long long>(output.worker_cpu_100ns()),
        output.worker_cpu_available() ? 1u : 0u);
    if (output.failed()) return 81;
    if (status != nw::player::ExitCode::Success) return static_cast<int>(status);
    return static_cast<int>(player.shutdown());
}
