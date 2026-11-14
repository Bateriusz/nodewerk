#pragma once

// NW-06 audio-independent clock seam.
//
// This interface deliberately has no audio-library dependency.  A future audio
// implementation may provide its own clock while the player continues to use
// integer 48 kHz ticks.  The default monotonic provider uses QueryPerformance
// Counter on Windows and a caller-supplied tick source for host tests.

#include <cstdint>
#include <limits>

namespace nw::player {

inline constexpr std::uint32_t kTicksPerSecond = 48'000u;
inline constexpr std::uint64_t kMaxTicks =
    (std::numeric_limits<std::uint64_t>::max)();

class ClockProvider {
public:
    virtual ~ClockProvider() = default;
    virtual std::uint64_t now_ticks() const noexcept = 0;
    virtual std::uint32_t tick_rate() const noexcept = 0;
};

struct MonotonicTickSource {
    // Returns monotonic ticks at the provider's declared rate.
    using Function = std::uint64_t (*)(void* user_data);
    Function function = nullptr;
    void* user_data = nullptr;
};

class MonotonicClockProvider final : public ClockProvider {
public:
    explicit MonotonicClockProvider(
        MonotonicTickSource source = {},
        std::uint32_t rate = kTicksPerSecond) noexcept
        : source_(source), rate_(rate == 0u ? kTicksPerSecond : rate) {}

    std::uint64_t now_ticks() const noexcept override {
        if (source_.function == nullptr) return 0u;
        return source_.function(source_.user_data);
    }

    std::uint32_t tick_rate() const noexcept override { return rate_; }

private:
    MonotonicTickSource source_{};
    std::uint32_t rate_ = kTicksPerSecond;
};

inline bool CheckedTicksFromNanoseconds(std::uint64_t nanoseconds,
                                        std::uint32_t tick_rate,
                                        std::uint64_t* ticks) noexcept {
    if (ticks == nullptr || tick_rate == 0u) return false;
    const std::uint64_t seconds =
        nanoseconds / 1'000'000'000ull;
    const std::uint64_t remainder =
        nanoseconds % 1'000'000'000ull;
    if (seconds > kMaxTicks / tick_rate) return false;
    const std::uint64_t whole = seconds * tick_rate;
    const std::uint64_t fractional =
        (remainder * static_cast<std::uint64_t>(tick_rate)) /
        1'000'000'000ull;
    if (whole > kMaxTicks - fractional) return false;
    *ticks = whole + fractional;
    return true;
}

} // namespace nw::player
