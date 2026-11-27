#include "audio/procedural_audio.h"
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <vector>

namespace {
std::uint64_t Hash(const std::vector<std::int16_t>& pcm) {
    std::uint64_t value = 1469598103934665603ull;
    for (const auto sample : pcm) {
        const auto bytes = static_cast<std::uint16_t>(sample);
        value ^= bytes & 0xffu;
        value *= 1099511628211ull;
        value ^= bytes >> 8;
        value *= 1099511628211ull;
    }
    return value;
}
std::uint64_t Nonzero(const std::vector<std::int16_t>& pcm) {
    std::uint64_t count = 0;
    for (const auto sample : pcm) {
        if (sample != 0) ++count;
    }
    return count;
}
}

int main(int argc, char** argv) {
    if (argc != 2) return 10;
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) return 70;
    input.seekg(0, std::ios::end);
    const std::streamoff length = input.tellg();
    if (length < 0) return 70;
    input.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> bytes(static_cast<std::size_t>(length));
    input.read(reinterpret_cast<char*>(bytes.data()),
               static_cast<std::streamsize>(bytes.size()));
    if (!input) return 70;
    nw::audio::Song song{};
    if (!nw::audio::DecodeSong(bytes.data(), bytes.size(), song)) return 70;
    std::printf("version=%u instruments=%u notes=%u automation=%u\n",
                song.version, song.instrument_count, song.count,
                song.automation_count);
    nw::audio::PcmFormat stereo{};
    stereo.channels = 2;
    constexpr std::size_t frames = 48000;
    std::vector<std::int16_t> whole(frames * 2u);
    if (!nw::audio::RenderPcm(song, stereo, 0, whole.data(), frames)) return 70;
    std::vector<std::int16_t> chunked(frames * 2u);
    std::size_t offset = 0;
    for (const std::size_t size : {std::size_t(1), std::size_t(17),
                                   std::size_t(333), std::size_t(2048),
                                   std::size_t(4096), std::size_t(8192),
                                   frames - 1u - 17u - 333u - 2048u - 4096u - 8192u}) {
        if (!nw::audio::RenderPcm(song, stereo, offset, chunked.data() + offset * 2u, size))
            return 70;
        offset += size;
    }
    std::printf("pcm_fnv64=%016llx\n",
                static_cast<unsigned long long>(Hash(whole)));
    std::printf("pcm_nonzero=%llu\n",
                static_cast<unsigned long long>(Nonzero(whole)));
    std::printf("chunked_equal=%d\n", whole == chunked ? 1 : 0);
    return 0;
}
