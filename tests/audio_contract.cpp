#include "audio/procedural_audio.h"
#include <array>
#include <cstdint>
#include <cstdio>
#include <limits>

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #x); return 1; } } while (false)
int main() {
    using namespace nw::audio;
    std::uint64_t value = 0;
    for (std::uint32_t rate : {44100u, 48000u, 96000u, 192000u}) {
        CHECK(SampleToTick(std::uint64_t(rate) * 86400, rate, value));
        CHECK(value == 48000ull * 86400);
        for (std::uint64_t t = 0; t < 5000; ++t) {
            std::uint64_t sample = 0;
            CHECK(TickToSample(t, rate, sample));
            CHECK(SampleToTick(sample, rate, value) && value >= t);
            if (sample) CHECK(SampleToTick(sample - 1, rate, value) && value < t);
        }
    }
    CHECK(!SampleToTick(1, 0, value));
    CHECK(!SampleToTick((std::numeric_limits<std::uint64_t>::max)(), 1, value));
    Song song{};
    song.duration_ticks = 2000;
    song.count = 1;
    song.events[0] = {100, 300, 440000, 12000};
    std::array<std::int16_t, 1000> complete{}, chunked{};
    CHECK(RenderPcm(song, {}, 0, complete.data(), complete.size()));
    CHECK(RenderPcm(song, {}, 0, chunked.data(), 123));
    CHECK(RenderPcm(song, {}, 123, chunked.data() + 123, chunked.size() - 123));
    CHECK(complete == chunked);
    CHECK(complete[99] == 0 && complete[100] != 0 && complete[400] == 0);
    CHECK(RenderPcm(song, {}, 200, chunked.data(), 50));
    for (std::size_t i = 0; i < 50; ++i) CHECK(chunked[i] == complete[200 + i]);
    song.count = kMaxEvents + 1;
    CHECK(!RenderPcm(song, {}, 0, complete.data(), complete.size()));
    return 0;
}
