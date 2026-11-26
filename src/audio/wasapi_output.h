#pragma once
#include "audio/procedural_audio.h"
#include <atomic>
#include <windows.h>

namespace nw::audio {
// Control operations are serialized by the caller. The worker owns all COM
// and endpoint objects. The renderer observes only atomic clock snapshots.
class WasapiOutput final : public player::ClockProvider {
public:
    WasapiOutput() = default;
    ~WasapiOutput() override;
    WasapiOutput(const WasapiOutput&) = delete;
    WasapiOutput& operator=(const WasapiOutput&) = delete;
    bool start(const Song& song, std::uint64_t tick = 0);
    bool seek(std::uint64_t tick);
    void stop() noexcept;
    std::uint64_t now_ticks() const noexcept override { return tick_.load(); }
    std::uint32_t tick_rate() const noexcept override { return player::kTicksPerSecond; }
    bool failed() const noexcept { return failed_.load(); }
    std::uint64_t starvation_observations() const noexcept { return starvation_.load(); }
    std::uint64_t observed_underruns() const noexcept { return underruns_.load(); }
    std::uint64_t missing_samples_lower_bound() const noexcept { return missing_.load(); }
    bool synthesis_cpu_available() const noexcept { return cpu_available_.load(); }
    std::uint64_t clock_discontinuities() const noexcept { return discontinuities_.load(); }
    std::uint64_t synthesis_cpu_100ns() const noexcept { return cpu_.load(); }
private:
    static DWORD WINAPI ThreadEntry(void* context) noexcept;
    void run() noexcept;
    Song song_{};
    std::uint64_t origin_ = 0;
    HANDLE worker_ = nullptr;
    std::atomic<bool> stop_{false}, failed_{false}, ready_{false};
    std::atomic<std::uint64_t> tick_{0}, starvation_{0}, discontinuities_{0}, cpu_{0};
    std::atomic<std::uint64_t> underruns_{0}, missing_{0};
    std::atomic<bool> cpu_available_{true};
};
}
