#pragma once
#include "audio/procedural_audio.h"
#include <atomic>
#include <string_view>
#include <windows.h>

namespace nw::audio {
// Only collected in an explicit desktop validation run, not on the worker.
struct PlaybackDisplay {
    HMONITOR monitor = nullptr;
    std::array<wchar_t, 128> device_path{};
    std::uint32_t refresh_numerator = 0;
    std::uint32_t refresh_denominator = 0;
    bool operator==(const PlaybackDisplay&) const = default;
};
PlaybackDisplay GetPlaybackDisplay(HWND window) noexcept;
bool IsWindowOnPlaybackMonitor(HWND window, HMONITOR monitor) noexcept;

// Control operations are serialized by the caller. The worker owns all COM
// and endpoint objects. The renderer observes only atomic clock snapshots.
class WasapiOutput final : public player::ClockProvider {
public:
    WasapiOutput() = default;
    ~WasapiOutput() override;
    WasapiOutput(const WasapiOutput&) = delete;
    WasapiOutput& operator=(const WasapiOutput&) = delete;
    bool start(const Song& song, std::uint64_t tick = 0, bool collect_evidence = false);
    // Controller may read this after stop(), which joins the device owner.
    std::wstring_view selected_endpoint_id() const noexcept { return endpoint_id_.data(); }
    bool seek(std::uint64_t tick);
    void stop() noexcept;
    std::uint64_t now_ticks() const noexcept override { return tick_.load(); }
    std::uint32_t tick_rate() const noexcept override { return player::kTicksPerSecond; }
    bool failed() const noexcept { return failed_.load(); }
    std::uint64_t starvation_observations() const noexcept { return starvation_.load(); }
    std::uint64_t observed_underruns() const noexcept { return underruns_.load(); }
    std::uint64_t missing_samples_lower_bound() const noexcept { return missing_.load(); }
    bool synthesis_cpu_available() const noexcept { return cpu_available_.load(); }
    bool worker_cpu_available() const noexcept { return worker_cpu_available_.load(); }
    std::uint64_t worker_cpu_100ns() const noexcept { return worker_cpu_.load(); }
    std::uint64_t clock_discontinuities() const noexcept { return discontinuities_.load(); }
    std::uint64_t synthesis_cpu_100ns() const noexcept { return cpu_.load(); }
private:
    static DWORD WINAPI ThreadEntry(void* context) noexcept;
    void run() noexcept;
    Song song_{};
    bool collect_evidence_ = false;
    std::array<wchar_t, 512> endpoint_id_{};
    std::uint64_t origin_ = 0;
    HANDLE worker_ = nullptr;
    std::atomic<bool> stop_{false}, failed_{false}, ready_{false};
    std::atomic<std::uint64_t> tick_{0}, starvation_{0}, discontinuities_{0}, cpu_{0};
    std::atomic<std::uint64_t> underruns_{0}, missing_{0};
    std::atomic<bool> cpu_available_{true}, worker_cpu_available_{false};
    std::atomic<std::uint64_t> worker_cpu_{0};
};
}
