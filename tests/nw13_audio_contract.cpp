#include "audio/procedural_audio.h"
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #x); return 1; } } while (false)

namespace {
nw::audio::Song MakeSong(std::uint32_t waveform) {
    nw::audio::Song song{};
    song.version = 2;
    song.duration_ticks = 48000;
    song.count = 1;
    song.instrument_count = 1;
    song.instruments[0] = {waveform, 1234, 1000, 0, 0, 0, 1000, 0, 96000, 0};
    song.events[0] = {0, 4800, 440000, 8000, 0, 0};
    return song;
}

double Rms(const std::vector<std::int16_t>& pcm, std::size_t stride,
           std::size_t offset) {
    double total = 0.0;
    std::size_t count = 0;
    for (std::size_t i = offset; i < pcm.size(); i += stride) {
        const double value = pcm[i];
        total += value * value;
        ++count;
    }
    return count ? total / double(count) : 0.0;
}
}

int main() {
    using namespace nw::audio;
    constexpr std::size_t frames = 4800;
    std::vector<std::int16_t> sine(frames * 2u);
    std::vector<std::int16_t> noise(frames * 2u);
    auto sine_song = MakeSong(0);
    auto noise_song = MakeSong(4);
    PcmFormat stereo{};
    stereo.channels = 2;
    CHECK(RenderPcm(sine_song, stereo, 0, sine.data(), frames));
    CHECK(RenderPcm(noise_song, stereo, 0, noise.data(), frames));
    CHECK(sine != noise);
    CHECK(Rms(sine, 2, 0) > 0.0);

    // Equal-power pan: full left puts no energy in the right channel.
    auto left_song = MakeSong(0);
    left_song.instruments[0].pan_milli = -1000;
    std::vector<std::int16_t> left(frames * 2u);
    CHECK(RenderPcm(left_song, stereo, 0, left.data(), frames));
    CHECK(Rms(left, 2, 0) > 0.0);
    CHECK(Rms(left, 2, 1) == 0.0);

    // One-pole filter and envelope alter the waveform response.
    auto filtered = MakeSong(0);
    filtered.instruments[0].cutoff_hz = 20;
    filtered.instruments[0].attack_ticks = 2400;
    std::vector<std::int16_t> filtered_pcm(frames * 2u);
    CHECK(RenderPcm(filtered, stereo, 0, filtered_pcm.data(), frames));
    CHECK(filtered_pcm != sine);

    // Bus automation is read from the shared song payload.
    auto automated = MakeSong(0);
    automated.automation_count = 1;
    automated.automation[0].bus = 0;
    automated.automation[0].count = 2;
    automated.automation[0].keys[0] = {0, 250};
    automated.automation[0].keys[1] = {48000, 250};
    std::vector<std::int16_t> quiet(frames * 2u);
    CHECK(RenderPcm(automated, stereo, 0, quiet.data(), frames));
    CHECK(Rms(quiet, 2, 0) < Rms(sine, 2, 0));

    // Preserve NWAS v1 decode and mono rendering.
    Song v1{};
    v1.duration_ticks = 2000;
    v1.count = 1;
    v1.events[0] = {100, 300, 440000, 12000, 0, 0};
    std::array<std::int16_t, 1000> mono{};
    CHECK(RenderPcm(v1, {}, 0, mono.data(), mono.size()));
    CHECK(mono[99] == 0 && mono[100] != 0 && mono[400] == 0);
    return 0;
}
