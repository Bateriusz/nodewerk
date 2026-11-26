#include "audio/procedural_audio.h"
#include "audio/wasapi_output.h"
#include "player/package_player.h"
#include <array>
#include <cstdio>
#include <fstream>
#include <windows.h>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::fprintf(stderr, "Usage: nw_audio_player scene.nwp song.nwas\n");
        return 10;
    }
    std::array<std::uint8_t, 20 + 20 * nw::audio::kMaxEvents + 1> bytes{};
    std::ifstream input(argv[2], std::ios::binary);
    if (!input) return 70;
    input.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    nw::audio::Song song{};
    if (input.bad() || !nw::audio::DecodeSong(bytes.data(),
                static_cast<std::size_t>(input.gcount()), song)) return 70;
    nw::player::PlayerConfig config;
    config.window_title = "Nodewerk audio playback";
    nw::player::PackagePlayer player(config);
    auto status = player.initializeFromFile(argv[1]);
    if (status != nw::player::ExitCode::Success) return static_cast<int>(status);
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
    if (output.failed()) return 81;
    if (status != nw::player::ExitCode::Success) return static_cast<int>(status);
    return static_cast<int>(player.shutdown());
}
