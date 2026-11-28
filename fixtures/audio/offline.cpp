#include "audio/procedural_audio.h"
#include <array>
#include <cstdio>
#include <fstream>
#include <cstdint>
#include <cstring>

namespace {
void Put32(std::ostream& stream, std::uint32_t n) {
    const char bytes[4] = {static_cast<char>(n), static_cast<char>(n >> 8),
        static_cast<char>(n >> 16), static_cast<char>(n >> 24)};
    stream.write(bytes, 4);
}
void Put16(std::ostream& stream, std::uint16_t n) {
    const char bytes[2] = {static_cast<char>(n), static_cast<char>(n >> 8)};
    stream.write(bytes, 2);
}
}
int main(int argc, char** argv) {
    using namespace nw::audio;
    if (argc != 4) {
        std::fprintf(stderr, "Usage: nw_audio_offline song.nwas output.wav frame-plan.csv\n");
        return 10;
    }
    constexpr auto max_payload = 28 + 40 * kMaxInstruments + 24 * kMaxEvents +
        kMaxAutomationBuses * (8 + 16 * kMaxAutomationKeys);
    std::array<std::uint8_t, max_payload + 1> bytes{};
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) return 70;
    input.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    Song song{};
    if (input.bad() || !DecodeSong(bytes.data(), static_cast<std::size_t>(input.gcount()), song))
        return 70;
    const PcmFormat format = SongPcmFormat(song);
    const auto rate = format.sample_rate;
    const auto frame_bytes = format.channels * 2u;
    PcmStream stream;
    if (!stream.Start(song, format, 0)) return 70;
    constexpr std::uint32_t frame_ticks = 800; // exact 60 fps
    std::uint64_t samples = 0;
    if (!TickToSample(song.duration_ticks, rate, samples) || samples > (UINT32_MAX - 36u) / frame_bytes)
        return 70;
    const auto pcm_bytes = static_cast<std::uint32_t>(samples * frame_bytes);
    std::ofstream wav(argv[2], std::ios::binary | std::ios::trunc);
    std::ofstream plan(argv[3], std::ios::binary | std::ios::trunc);
    if (!wav || !plan) return 10;
    wav.write("RIFF", 4); Put32(wav, 36u + pcm_bytes); wav.write("WAVEfmt ", 8);
    Put32(wav, 16); Put16(wav, 1); Put16(wav, static_cast<std::uint16_t>(format.channels)); Put32(wav, rate);
    Put32(wav, rate * frame_bytes); Put16(wav, static_cast<std::uint16_t>(frame_bytes)); Put16(wav, 16);
    wav.write("data", 4); Put32(wav, pcm_bytes);
    std::array<std::int16_t, 4096> pcm{};
    for (std::uint64_t cursor = 0; cursor < samples;) {
        const auto capacity = pcm.size() / format.channels;
        const auto count = static_cast<std::size_t>(samples - cursor < capacity ?
            samples - cursor : capacity);
        if (!stream.Render(pcm.data(), count)) return 70;
        for (std::size_t i = 0; i < count * format.channels; ++i)
            Put16(wav, static_cast<std::uint16_t>(pcm[i]));
        if (!wav) return 10;
        cursor += count;
    }
    plan << "frame,tick,sample\n";
    for (std::uint64_t tick = 0, frame = 0; tick < song.duration_ticks;
         tick += frame_ticks, ++frame) {
        std::uint64_t sample = 0;
        if (!TickToSample(tick, rate, sample)) return 70;
        plan << frame << ',' << tick << ',' << sample << '\n';
    }
    wav.close();
    plan.close();
    return wav && plan ? 0 : 10;
}
