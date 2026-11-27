#include "audio/procedural_audio.h"
#include <cmath>
#include <limits>

namespace nw::audio {
namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr std::uint64_t kAuthoritativeTicksPerSecond = 48'000;

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
std::uint16_t Read16(const std::uint8_t* p) noexcept {
    return static_cast<std::uint16_t>(
        std::uint16_t(p[0]) | (std::uint16_t(p[1]) << 8));
}
std::uint32_t Read32(const std::uint8_t* p) noexcept {
    return std::uint32_t(p[0]) | (std::uint32_t(p[1]) << 8) |
        (std::uint32_t(p[2]) << 16) | (std::uint32_t(p[3]) << 24);
}
std::uint64_t Read64(const std::uint8_t* p) noexcept {
    return Read32(p) | (std::uint64_t(Read32(p + 4)) << 32);
}

double Waveform(std::uint32_t waveform, double phase, std::uint32_t seed,
                std::uint64_t sample_index) noexcept {
    switch (waveform) {
    case 0:
        return std::sin(2.0 * kPi * phase);
    case 1:
        return phase < 0.5 ? 1.0 : -1.0;
    case 2:
        return phase * 2.0 - 1.0;
    case 3:
        return 1.0 - 4.0 * std::fabs(phase - 0.5);
    case 4: {
        // Matches tools/nw13_audio.py's deterministic 32-bit noise stream.
        std::uint32_t value = static_cast<std::uint32_t>(sample_index) +
            seed * 0x9E3779B9u;
        value = (value ^ (value >> 16)) * 0x7FEB352Du;
        value = (value ^ (value >> 15)) * 0x846CA68Bu;
        value ^= value >> 16;
        return (double(value) / 4294967295.0) * 2.0 - 1.0;
    }
    default:
        return 0.0;
    }
}

std::uint64_t ScaleTicks(std::uint32_t ticks, std::uint32_t rate) noexcept {
    const std::uint64_t scaled =
        std::uint64_t(ticks) * std::uint64_t(rate) / kAuthoritativeTicksPerSecond;
    return scaled < 1u ? 1u : scaled;
}

double EnvelopeLevel(const Instrument& instrument, std::uint64_t age,
                     std::uint64_t total, std::uint32_t rate) noexcept {
    const auto attack = ScaleTicks(instrument.attack_ticks, rate);
    const auto decay = ScaleTicks(instrument.decay_ticks, rate);
    const auto release = ScaleTicks(instrument.release_ticks, rate);
    const double sustain = double(instrument.sustain_milli) / 1000.0;
    if (age < attack) return double(age) / double(attack);
    if (age < attack + decay) {
        const double ratio = double(age - attack) / double(decay);
        return 1.0 + (sustain - 1.0) * ratio;
    }
    const std::uint64_t remaining = total > age ? total - age : 0u;
    if (remaining < release) {
        return sustain * (double(remaining) / double(release));
    }
    return sustain;
}

double BusGain(const Song& song, std::uint8_t bus, std::uint64_t sample,
               std::uint32_t rate) noexcept {
    const Automation* track = nullptr;
    for (std::uint32_t i = 0; i < song.automation_count; ++i) {
        if (song.automation[i].bus == bus) {
            track = &song.automation[i];
            break;
        }
    }
    if (track == nullptr || track->count == 0u) return 1.0;
    const double tick = double(sample) *
        double(kAuthoritativeTicksPerSecond) / double(rate);
    if (tick <= double(track->keys[0].tick)) {
        return double(track->keys[0].value_milli) / 1000.0;
    }
    const auto last = track->count - 1u;
    if (tick >= double(track->keys[last].tick)) {
        return double(track->keys[last].value_milli) / 1000.0;
    }
    for (std::uint32_t i = 1; i <= last; ++i) {
        const auto& previous = track->keys[i - 1u];
        const auto& current = track->keys[i];
        if (tick <= double(current.tick)) {
            if (current.tick == previous.tick) {
                return double(current.value_milli) / 1000.0;
            }
            const double alpha = (tick - double(previous.tick)) /
                double(current.tick - previous.tick);
            const double previous_value = double(previous.value_milli) / 1000.0;
            const double current_value = double(current.value_milli) / 1000.0;
            return previous_value + (current_value - previous_value) * alpha;
        }
    }
    return double(track->keys[last].value_milli) / 1000.0;
}

std::int16_t ClampPcm(double value) noexcept {
    if (value > 32767.0) return 32767;
    if (value < -32768.0) return -32768;
    return static_cast<std::int16_t>(std::llround(value));
}

bool RenderV1(const Song& song, PcmFormat format, std::uint64_t first_sample,
              std::int16_t* output, std::size_t count) noexcept {
    const auto period = std::uint64_t(format.sample_rate) * 1000u;
    struct Voice {
        std::uint64_t start, end;
        std::uint32_t freq;
        std::uint16_t gain;
    };
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
        const auto value = static_cast<std::int16_t>(mix);
        if (format.channels == 2u) {
            output[2u * i] = value;
            output[2u * i + 1u] = value;
        } else {
            output[i] = value;
        }
    }
    return true;
}

}

double PcmStream::Advance(Voice& voice, const Event& event,
                          std::uint64_t sample) const noexcept {
    const auto age = sample - voice.start;
    const auto& instrument = song_->instruments[event.instrument];
    const double phase = double(age) * double(event.frequency_millihz) /
        1000.0 / double(format_.sample_rate);
    const double raw = Waveform(
        instrument.waveform, phase - std::floor(phase), instrument.seed, sample);
    if (age % window_ == 0u) {
        voice.filter = voice.warming_filter;
        voice.warming_filter = 0.0;
    }
    voice.filter += voice.alpha * (raw - voice.filter);
    voice.warming_filter += voice.alpha * (raw - voice.warming_filter);
    return voice.filter;
}

bool PcmStream::Start(const Song& song, PcmFormat format,
                      std::uint64_t first_sample) noexcept {
    song_ = nullptr;
    if (!ValidateSong(song) || !format.sample_rate || format.sample_rate > kMaxSampleRate ||
        (format.channels != 1u && format.channels != 2u)) return false;
    song_ = &song;
    format_ = format;
    cursor_ = first_sample;
    // A one-second warm-up attenuates even the minimum 20 Hz cutoff by
    // far more than the precision of PCM16. Window boundaries are anchored
    // to the note start, so changing callback sizes leaves samples identical.
    window_ = format.sample_rate;
    if (song.version == 1u) return true;
    for (std::uint32_t j = 0; j < song.count; ++j) {
        const auto& event = song.events[j];
        auto& voice = voices_[j];
        voice = {};
        if (!TickToSample(event.tick, format.sample_rate, voice.start) ||
            !TickToSample(event.tick + event.duration, format.sample_rate, voice.end)) {
            song_ = nullptr;
            return false;
        }
        const auto& instrument = song.instruments[event.instrument];
        const double angle = (double(instrument.pan_milli) / 1000.0 + 1.0) * kPi * 0.25;
        voice.left_gain = std::cos(angle);
        voice.right_gain = std::sin(angle);
        const double dt = 1.0 / double(format.sample_rate);
        const double rc = 1.0 / (2.0 * kPi * double(instrument.cutoff_hz));
        voice.alpha = dt / (rc + dt);
        if (voice.start >= first_sample || voice.end <= first_sample) continue;
        const auto block = (first_sample - voice.start) / window_;
        const auto begin = voice.start + (block == 0 ? 0 : (block - 1) * window_);
        for (auto sample = begin; sample < first_sample; ++sample)
            Advance(voice, event, sample);
    }
    return true;
}

bool PcmStream::Render(std::int16_t* output, std::size_t count) noexcept {
    if (!song_ || (!output && count) ||
        count > (std::numeric_limits<std::uint64_t>::max)() - cursor_ ||
        count > (std::numeric_limits<std::size_t>::max)() / format_.channels) return false;
    const auto& song = *song_;
    if (song.version == 1u) {
        if (!RenderV1(song, format_, cursor_, output, count)) return false;
        cursor_ += count;
        return true;
    }
    for (std::size_t i = 0; i < count; ++i) {
        const std::uint64_t sample = cursor_ + i;
        double left = 0.0;
        double right = 0.0;
        for (std::uint32_t j = 0; j < song.count; ++j) {
            auto& voice = voices_[j];
            if (sample < voice.start || sample >= voice.end) continue;
            const auto& event = song.events[j];
            const auto& instrument = song.instruments[event.instrument];
            const double state = Advance(voice, event, sample);
            const double envelope = EnvelopeLevel(
                instrument, sample - voice.start, voice.end - voice.start, format_.sample_rate);
            const double bus = BusGain(song, event.bus, sample, format_.sample_rate);
            const double amplitude = state * envelope *
                (double(event.gain) / 1000.0) *
                (double(instrument.gain_milli) / 1000.0) * bus;
            left += amplitude * voice.left_gain;
            right += amplitude * voice.right_gain;
        }
        if (format_.channels == 2u) {
            output[2u * i] = ClampPcm(left * 32767.0);
            output[2u * i + 1u] = ClampPcm(right * 32767.0);
        } else {
            output[i] = ClampPcm((left + right) * 0.5 * 32767.0);
        }
    }
    cursor_ += count;
    return true;
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
    if (song.version == 1u) {
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
    if (song.version != 2u || !song.instrument_count ||
        song.instrument_count > kMaxInstruments ||
        song.automation_count > kMaxAutomationBuses) return false;
    for (std::uint32_t i = 0; i < song.instrument_count; ++i) {
        const auto& instrument = song.instruments[i];
        if (instrument.waveform > 4u || instrument.filter_type != 0u ||
            instrument.gain_milli > 1000u || instrument.pan_milli < -1000 ||
            instrument.pan_milli > 1000 || instrument.sustain_milli > 1000u ||
            instrument.attack_ticks > kMaxSongTicks ||
            instrument.decay_ticks > kMaxSongTicks ||
            instrument.release_ticks > kMaxSongTicks ||
            instrument.cutoff_hz < 20u || instrument.cutoff_hz > 96'000u)
            return false;
    }
    std::uint64_t previous = 0;
    for (std::uint32_t i = 0; i < song.count; ++i) {
        const auto& e = song.events[i];
        if (e.tick < previous || e.tick >= song.duration_ticks ||
            !e.duration || e.duration > song.duration_ticks - e.tick ||
            !e.frequency_millihz || e.frequency_millihz > 20'000'000 ||
            e.gain > 8000 ||
            static_cast<std::uint32_t>(e.instrument) >= song.instrument_count ||
            e.bus > 1) return false;
        previous = e.tick;
    }
    for (std::uint32_t i = 0; i < song.automation_count; ++i) {
        const auto& track = song.automation[i];
        if (track.bus > 1 || track.count > kMaxAutomationKeys) return false;
        std::uint64_t key_tick = 0;
        for (std::uint32_t k = 0; k < track.count; ++k) {
            if (track.keys[k].tick < key_tick ||
                track.keys[k].tick > song.duration_ticks ||
                track.keys[k].value_milli > 8000u) return false;
            key_tick = track.keys[k].tick;
        }
        for (std::uint32_t other = 0; other < i; ++other) {
            if (song.automation[other].bus == track.bus) return false;
        }
    }
    return true;
}
bool DecodeSong(const std::uint8_t* bytes, std::size_t size, Song& song) noexcept {
    if (!bytes || size < 20 || Read32(bytes) != 0x5341574eu) return false;
    const std::uint32_t version = Read32(bytes + 4);
    Song candidate{};
    if (version == 1u) {
        candidate.version = 1;
        candidate.duration_ticks = Read64(bytes + 8);
        candidate.count = Read32(bytes + 16);
        if (candidate.count > kMaxEvents || size != 20u + 20u * candidate.count)
            return false;
        for (std::uint32_t i = 0; i < candidate.count; ++i) {
            const auto* p = bytes + 20u + 20u * i;
            const auto gain = Read32(p + 16);
            if (gain > 32767u) return false;
            candidate.events[i] = {Read64(p), Read32(p + 8), Read32(p + 12),
                                   static_cast<std::uint16_t>(gain), 0, 0};
        }
        if (!ValidateSong(candidate)) return false;
        song = candidate;
        return true;
    }
    if (version != 2u || size < 28u) return false;
    candidate.version = 2;
    candidate.duration_ticks = Read64(bytes + 8);
    candidate.count = Read32(bytes + 16);
    candidate.instrument_count = Read32(bytes + 20);
    candidate.automation_count = Read32(bytes + 24);
    if (candidate.count > kMaxEvents ||
        candidate.instrument_count > kMaxInstruments ||
        !candidate.instrument_count ||
        candidate.automation_count > kMaxAutomationBuses)
        return false;
    std::size_t offset = 28u;
    for (std::uint32_t i = 0; i < candidate.instrument_count; ++i) {
        if (offset + 40u > size) return false;
        const auto* p = bytes + offset;
        auto& instrument = candidate.instruments[i];
        instrument.waveform = Read32(p);
        instrument.seed = Read32(p + 4);
        instrument.gain_milli = Read32(p + 8);
        instrument.pan_milli = static_cast<std::int32_t>(Read32(p + 12));
        instrument.attack_ticks = Read32(p + 16);
        instrument.decay_ticks = Read32(p + 20);
        instrument.sustain_milli = Read32(p + 24);
        instrument.release_ticks = Read32(p + 28);
        instrument.cutoff_hz = Read32(p + 32);
        instrument.filter_type = Read32(p + 36);
        offset += 40u;
    }
    for (std::uint32_t i = 0; i < candidate.count; ++i) {
        if (offset + 24u > size) return false;
        const auto* p = bytes + offset;
        if (p[23] != 0 || Read32(p + 16) > 8000u) return false;
        candidate.events[i] = {Read64(p), Read32(p + 8), Read32(p + 12),
                               static_cast<std::uint16_t>(Read32(p + 16)),
                               Read16(p + 20), p[22]};
        offset += 24u;
    }
    for (std::uint32_t i = 0; i < candidate.automation_count; ++i) {
        if (offset + 8u > size) return false;
        auto& track = candidate.automation[i];
        track.bus = bytes[offset];
        track.count = Read32(bytes + offset + 4);
        if (bytes[offset + 1u] != 0 || bytes[offset + 2u] != 0 ||
            bytes[offset + 3u] != 0 ||
            track.count > kMaxAutomationKeys)
            return false;
        offset += 8u;
        if (offset + 16u * track.count > size) return false;
        for (std::uint32_t k = 0; k < track.count; ++k) {
            const auto* p = bytes + offset + 16u * k;
            if (Read32(p + 12) != 0u) return false;
            track.keys[k] = {Read64(p), Read32(p + 8)};
        }
        offset += 16u * track.count;
    }
    if (offset != size) return false;
    if (!ValidateSong(candidate)) return false;
    song = candidate;
    return true;
}
bool RenderPcm(const Song& song, PcmFormat format, std::uint64_t first_sample,
               std::int16_t* output, std::size_t count) noexcept {
    PcmStream stream;
    return stream.Start(song, format, first_sample) && stream.Render(output, count);
}
}
