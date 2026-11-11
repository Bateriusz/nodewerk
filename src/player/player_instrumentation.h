#pragma once

// Development-only instrumentation.  Release linkage compiles the diagnostic
// surface out entirely (`NW_PLAYER_DIAGNOSTICS == 0`): no capture hooks, query
// heaps, timing readback, or diagnostic tooling are part of the release player.

#if defined(NW_PROFILE_DEV) && NW_PROFILE_DEV
#define NW_PLAYER_DIAGNOSTICS 1
#else
#define NW_PLAYER_DIAGNOSTICS 0
#endif

#include <cstdint>

namespace nw::player {

struct EngineMemoryAccounting {
    // Engine-owned accounting.  These are deliberately separate from the
    // driver/process totals below.
    std::uint64_t engine_resource_bytes = 0u;
    std::uint64_t engine_upload_bytes = 0u;
    std::uint64_t engine_descriptor_count = 0u;
    std::uint64_t engine_heap_count = 0u;

    // Driver / process totals, reported separately.
    std::uint64_t process_private_bytes = 0u;
    std::uint64_t gpu_local_usage_bytes = 0u;
};

struct PlayerTiming {
    double cpu_submit_ms = 0.0;
    double gpu_ms = 0.0;
    std::uint64_t tick = 0u;
    bool cpu_available = false;
    bool gpu_available = false;
};

// Queries a process total through the platform API.  Returns 0 when the host
// does not expose the measurement; zero is reported as unavailable, never as
// an engine allocation measurement.
std::uint64_t QueryProcessPrivateBytes() noexcept;

#if NW_PLAYER_DIAGNOSTICS

using CaptureHook = void (*)(std::uint64_t tick, void* user_data);

class PlayerInstrumentation {
public:
    void BeginFrame(std::uint64_t tick) noexcept;
    void EndFrame() noexcept;
    void SetEngineAccounting(const EngineMemoryAccounting& accounting) noexcept;
    void SetProcessTotals(std::uint64_t process_private_bytes,
                          std::uint64_t gpu_local_usage_bytes) noexcept;
    void SetCaptureHook(CaptureHook hook, void* user_data) noexcept;
    void Capture(std::uint64_t tick) noexcept;
    void SetGpuTiming(double gpu_ms) noexcept;

    const PlayerTiming& timing() const noexcept { return timing_; }
    const EngineMemoryAccounting& memory() const noexcept { return memory_; }

private:
    PlayerTiming timing_{};
    EngineMemoryAccounting memory_{};
    CaptureHook capture_hook_ = nullptr;
    void* capture_user_data_ = nullptr;
    double frame_begin_ms_ = 0.0;
};

#endif // NW_PLAYER_DIAGNOSTICS

} // namespace nw::player
