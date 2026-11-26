#pragma once
#include <cstdint>

namespace nw::audio {
// Stream coordinates are absolute 48 kHz samples. Endpoint padding is an
// observation, not a count of glitches. A fresh clock position beyond the
// submitted frontier proves a gap in this coordinate system, but not the
// number of physical audible glitches. Do not rewind or replay elapsed audio.
enum class StreamAction { Continue, Recover, Discontinuity };
struct StreamProgress {
    std::uint64_t last = 0;
    std::uint64_t submitted_until = 0;
    std::uint64_t observed_underruns = 0;
    std::uint64_t missing_samples = 0;
    StreamAction observe(std::uint64_t position, std::uint32_t padding,
                         std::uint32_t capacity) noexcept {
        if (position < last || padding > capacity) return StreamAction::Discontinuity;
        last = position;
        if (position <= submitted_until) return StreamAction::Continue;
        // Nonempty queue contradicts a position beyond all submitted frames.
        if (padding != 0) return StreamAction::Discontinuity;
        ++observed_underruns;
        missing_samples += position - submitted_until;
        submitted_until = position;
        return StreamAction::Recover;
    }
};
}
