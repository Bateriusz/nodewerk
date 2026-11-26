#pragma once
#include "player/clock_provider.h"
#include <array>
#include <cstddef>
#include <cstdint>

namespace nw::audio {
inline constexpr std::uint32_t kMaxEvents = 64;
inline constexpr std::uint64_t kMaxSongTicks = 48'000ull * 3600;
inline constexpr std::uint32_t kMaxSampleRate = 384'000;
struct PcmFormat { std::uint32_t sample_rate = 48'000; }; // signed PCM16, mono
struct Event {
    std::uint64_t tick = 0;
    std::uint32_t duration = 0;
    std::uint32_t frequency_millihz = 0;
    std::uint16_t gain = 0;
};
struct Song {
    std::uint64_t duration_ticks = 0;
    std::uint32_t count = 0;
    std::array<Event, kMaxEvents> events{};
};
// NWAS v1 is little-endian and independent of the GPU package ABI.
bool DecodeSong(const std::uint8_t* bytes, std::size_t size, Song& song) noexcept;
bool ValidateSong(const Song& song) noexcept;
// Floor: playback sample -> absolute tick. Ceil: first sample at or after event.
bool SampleToTick(std::uint64_t sample, std::uint32_t rate,
                  std::uint64_t& tick) noexcept;
bool TickToSample(std::uint64_t tick, std::uint32_t rate,
                  std::uint64_t& sample) noexcept;
// Pure, absolute-sample synthesis; seeking needs no replay or allocation.
bool RenderPcm(const Song& song, PcmFormat format, std::uint64_t first_sample,
               std::int16_t* output, std::size_t count) noexcept;
}
