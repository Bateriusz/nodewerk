#pragma once
#include "player/clock_provider.h"
#include <array>
#include <cstddef>
#include <cstdint>

namespace nw::audio {
inline constexpr std::uint32_t kMaxEvents = 64;
inline constexpr std::uint32_t kMaxInstruments = 64;
inline constexpr std::uint32_t kMaxAutomationBuses = 8;
inline constexpr std::uint32_t kMaxAutomationKeys = 64;
inline constexpr std::uint64_t kMaxSongTicks = 48'000ull * 3600;
inline constexpr std::uint32_t kMaxSampleRate = 384'000;
struct PcmFormat {
    std::uint32_t sample_rate = 48'000;
    std::uint32_t channels = 1; // signed PCM16, interleaved when 2
};
struct Instrument {
    std::uint32_t waveform = 0; // 0 sine, 1 square, 2 saw, 3 triangle, 4 noise
    std::uint32_t seed = 0;
    std::uint32_t gain_milli = 0;
    std::int32_t pan_milli = 0;
    std::uint32_t attack_ticks = 0;
    std::uint32_t decay_ticks = 0;
    std::uint32_t sustain_milli = 0;
    std::uint32_t release_ticks = 0;
    std::uint32_t cutoff_hz = 0;
    std::uint32_t filter_type = 0; // 0 one-pole low-pass
};
struct Event {
    std::uint64_t tick = 0;
    std::uint32_t duration = 0;
    std::uint32_t frequency_millihz = 0;
    std::uint16_t gain = 0;
    std::uint16_t instrument = 0;
    std::uint8_t bus = 0;
};
struct AutomationKey {
    std::uint64_t tick = 0;
    std::uint32_t value_milli = 0;
};
struct Automation {
    std::uint8_t bus = 0;
    std::uint32_t count = 0;
    std::array<AutomationKey, kMaxAutomationKeys> keys{};
};
struct Song {
    std::uint32_t version = 1;
    std::uint64_t duration_ticks = 0;
    std::uint32_t count = 0;
    std::array<Event, kMaxEvents> events{};
    std::uint32_t instrument_count = 0;
    std::array<Instrument, kMaxInstruments> instruments{};
    std::uint32_t automation_count = 0;
    std::array<Automation, kMaxAutomationBuses> automation{};
};
// NWAS v1 (read-only) and v2 are little-endian and independent of the GPU
// package ABI.  v1 remains decodable; v2 carries the instrument, pan, filter,
// bus and automation fields used by the shared procedural-audio renderer.
bool DecodeSong(const std::uint8_t* bytes, std::size_t size, Song& song) noexcept;
bool ValidateSong(const Song& song) noexcept;
// Floor: playback sample -> absolute tick. Ceil: first sample at or after event.
bool SampleToTick(std::uint64_t sample, std::uint32_t rate,
                  std::uint64_t& tick) noexcept;
bool TickToSample(std::uint64_t tick, std::uint32_t rate,
                  std::uint64_t& sample) noexcept;
// Pure, absolute-sample synthesis; seeking needs no replay or allocation.
// `count` is a frame count; stereo output writes two PCM16 values per frame.
bool RenderPcm(const Song& song, PcmFormat format, std::uint64_t first_sample,
               std::int16_t* output, std::size_t count) noexcept;
}
