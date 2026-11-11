#include "player_instrumentation.h"

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#endif

namespace nw::player {

std::uint64_t QueryProcessPrivateBytes() noexcept {
#if defined(_WIN32)
    using K32GetProcessMemoryInfoFn =
        BOOL(WINAPI*)(HANDLE, PPROCESS_MEMORY_COUNTERS, DWORD);
    HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
    if (kernel32 == nullptr) {
        return 0u;
    }
    auto get_process_memory_info = reinterpret_cast<K32GetProcessMemoryInfoFn>(
        reinterpret_cast<void*>(GetProcAddress(kernel32,
                                               "K32GetProcessMemoryInfo")));
    if (get_process_memory_info == nullptr) {
        return 0u;
    }
    PROCESS_MEMORY_COUNTERS_EX counters{};
    if (get_process_memory_info(
            GetCurrentProcess(),
            reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&counters),
            static_cast<DWORD>(sizeof(counters))) != FALSE) {
        return static_cast<std::uint64_t>(counters.PrivateUsage);
    }
#endif
    return 0u;
}

#if NW_PLAYER_DIAGNOSTICS
namespace {

double NowMs() noexcept {
#if defined(_WIN32)
    LARGE_INTEGER frequency{};
    LARGE_INTEGER counter{};
    if (QueryPerformanceFrequency(&frequency) == FALSE ||
        frequency.QuadPart == 0) {
        return 0.0;
    }
    QueryPerformanceCounter(&counter);
    return (1000.0 * static_cast<double>(counter.QuadPart)) /
           static_cast<double>(frequency.QuadPart);
#else
    return 0.0;
#endif
}

} // namespace

void PlayerInstrumentation::BeginFrame(std::uint64_t tick) noexcept {
    timing_.tick = tick;
    timing_.cpu_submit_ms = 0.0;
    timing_.gpu_ms = 0.0;
    timing_.cpu_available = false;
    timing_.gpu_available = false;
    frame_begin_ms_ = NowMs();
}

void PlayerInstrumentation::EndFrame() noexcept {
    const double end = NowMs();
    if (frame_begin_ms_ > 0.0 && end >= frame_begin_ms_) {
        timing_.cpu_submit_ms = end - frame_begin_ms_;
        timing_.cpu_available = true;
    }
}

void PlayerInstrumentation::SetEngineAccounting(
    const EngineMemoryAccounting& accounting) noexcept {
    memory_.engine_resource_bytes = accounting.engine_resource_bytes;
    memory_.engine_upload_bytes = accounting.engine_upload_bytes;
    memory_.engine_descriptor_count = accounting.engine_descriptor_count;
    memory_.engine_heap_count = accounting.engine_heap_count;
}

void PlayerInstrumentation::SetProcessTotals(
    std::uint64_t process_private_bytes,
    std::uint64_t gpu_local_usage_bytes) noexcept {
    memory_.process_private_bytes = process_private_bytes;
    memory_.gpu_local_usage_bytes = gpu_local_usage_bytes;
}

void PlayerInstrumentation::SetCaptureHook(CaptureHook hook,
                                           void* user_data) noexcept {
    capture_hook_ = hook;
    capture_user_data_ = user_data;
}

void PlayerInstrumentation::Capture(std::uint64_t tick) noexcept {
    if (capture_hook_ != nullptr) {
        capture_hook_(tick, capture_user_data_);
    }
}

void PlayerInstrumentation::SetGpuTiming(double gpu_ms) noexcept {
    if (gpu_ms >= 0.0) {
        timing_.gpu_ms = gpu_ms;
        timing_.gpu_available = true;
    }
}

#endif // NW_PLAYER_DIAGNOSTICS

} // namespace nw::player
