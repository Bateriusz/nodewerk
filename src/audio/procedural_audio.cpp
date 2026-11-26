#include "audio/procedural_audio.h"
#include <limits>

namespace nw::audio {
namespace {
bool Scale(std::uint64_t n, std::uint32_t denominator, std::uint32_t numerator,
           bool ceil, std::uint64_t& result) noexcept {
    if (!denominator || !numerator) return false;
    constexpr auto max = (std::numeric_limits<std::uint64_t>::max)();
    const auto q = n / denominator;
    const auto r = n % denominator;
    if (q > max / numerator) return false;
    const auto whole = q * numerator;
    const auto product = r * numerator;
    const auto fraction = product / denominator +
        static_cast<std::uint64_t>(ceil && product % denominator != 0);
    if (whole > max - fraction) return false;
    result = whole + fraction;
    return true;
}
std::uint32_t Read32(const std::uint8_t* p) noexcept {
    return std::uint32_t(p[0]) | (std::uint32_t(p[1]) << 8) |
        (std::uint32_t(p[2]) << 16) | (std::uint32_t(p[3]) << 24);
}
std::uint64_t Read64(const std::uint8_t* p) noexcept {
    return Read32(p) | (std::uint64_t(Read32(p + 4)) << 32);
}
}
bool SampleToTick(std::uint64_t sample, std::uint32_t rate,
                  std::uint64_t& tick) noexcept {
    return Scale(sample, rate, player::kTicksPerSecond, false, tick);
}
bool TickToSample(std::uint64_t tick, std::uint32_t rate,
                  std::uint64_t& sample) noexcept {
    return Scale(tick, player::kTicksPerSecond, rate, true, sample);
}
bool ValidateSong(const Song& song) noexcept {
    if (!song.duration_ticks || song.duration_ticks > kMaxSongTicks ||
        !song.count || song.count > kMaxEvents) return false;
    std::uint64_t previous = 0;
    for (std::uint32_t i = 0; i < song.count; ++i) {
        const auto& e = song.events[i];
        if (e.tick < previous || e.tick >= song.duration_ticks ||
            !e.duration || e.duration > song.duration_ticks - e.tick ||
            !e.frequency_millihz || e.frequency_millihz > 20'000'000 ||
            !e.gain || e.gain > 32767) return false;
        previous = e.tick;
    }
    return true;
}
bool DecodeSong(const std::uint8_t* bytes, std::size_t size, Song& song) noexcept {
    if (!bytes || size < 20 || Read32(bytes) != 0x5341574eu ||
        Read32(bytes + 4) != 1) return false;
    Song candidate{};
    candidate.duration_ticks = Read64(bytes + 8);
    candidate.count = Read32(bytes + 16);
    if (candidate.count > kMaxEvents || size != 20u + 20u * candidate.count)
        return false;
    for (std::uint32_t i = 0; i < candidate.count; ++i) {
        const auto* p = bytes + 20u + 20u * i;
        const auto gain = Read32(p + 16);
        if (gain > 32767u) return false;
        candidate.events[i] = {Read64(p), Read32(p + 8), Read32(p + 12),
                               static_cast<std::uint16_t>(gain)};
    }
    if (!ValidateSong(candidate)) return false;
    song = candidate;
    return true;
}
bool RenderPcm(const Song& song, PcmFormat format, std::uint64_t first_sample,
               std::int16_t* output, std::size_t count) noexcept {
    if (!ValidateSong(song) || !format.sample_rate ||
        format.sample_rate > kMaxSampleRate || (!output && count) ||
        count > (std::numeric_limits<std::uint64_t>::max)() - first_sample)
        return false;
    const auto period = std::uint64_t(format.sample_rate) * 1000u;
    struct Voice { std::uint64_t start, end; std::uint32_t freq; std::uint16_t gain; };
    std::array<Voice, kMaxEvents> voices{};
    for (std::uint32_t j = 0; j < song.count; ++j) {
        const auto& event = song.events[j];
        auto& voice = voices[j];
        if (!TickToSample(event.tick, format.sample_rate, voice.start) ||
            !TickToSample(event.tick + event.duration, format.sample_rate, voice.end))
            return false;
        voice.freq = event.frequency_millihz;
        voice.gain = event.gain;
    }
    for (std::size_t i = 0; i < count; ++i) {
        const auto sample = first_sample + i;
        std::int64_t mix = 0;
        for (std::uint32_t j = 0; j < song.count; ++j) {
            const auto& v = voices[j];
            if (sample < v.start || sample >= v.end) continue;
            const auto age = sample - v.start;
            // Triangle, fixed-point, with bounded linear attack/release.
            const auto phase = ((age % period) * v.freq) % period;
            const auto ramp = phase * 65536u / period;
            const auto triangle = ramp < 32768u ?
                static_cast<std::int64_t>(2u * ramp) - 32768 :
                98303 - static_cast<std::int64_t>(2u * ramp);
            const auto edge = format.sample_rate / 200u + 1u;
            auto envelope = age + 1u;
            if (envelope > v.end - sample) envelope = v.end - sample;
            if (envelope > edge) envelope = edge;
            mix += triangle * v.gain * static_cast<std::int64_t>(envelope) /
                   (32768ll * edge);
        }
        if (mix > 32767) mix = 32767;
        if (mix < -32768) mix = -32768;
        output[i] = static_cast<std::int16_t>(mix);
    }
    return true;
}
}
