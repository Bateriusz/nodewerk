#pragma once

// Minimal Win32 lifecycle for the NW-04 package player.  The platform layer
// owns only window creation, message pumping, minimization tracking, and
// deterministic teardown; it has no rendering or resource policy.

#include <cstdint>

namespace nw::platform {

struct WindowConfig {
    const char* title = "Nodewerk NW-04 Player";
    std::uint32_t width = 1280u;
    std::uint32_t height = 720u;
    bool visible = true;
    bool resizable = true;
};

class Win32Window {
public:
    Win32Window() = default;
    ~Win32Window();

    Win32Window(const Win32Window&) = delete;
    Win32Window& operator=(const Win32Window&) = delete;

    bool Create(const WindowConfig& config) noexcept;
    void Destroy() noexcept;

    // Returns false after a WM_DESTROY/WM_CLOSE request.
    bool PumpMessages() noexcept;

    bool IsMinimized() const noexcept;
    bool IsClosed() const noexcept;
    bool IsCreated() const noexcept;

    void GetClientSize(std::uint32_t* width,
                       std::uint32_t* height) const noexcept;
    void RequestClientSize(std::uint32_t width,
                           std::uint32_t height) noexcept;

    // HWND on Windows; opaque null/simulated handle elsewhere.
    void* NativeHandle() const noexcept;

    // Test-only host simulation state.  On Windows this still reflects real
    // WM_SIZE notifications; the setter exists so GPU-less tests can exercise
    // minimize/zero-sized behavior deterministically.
    void SetSimulatedMinimized(bool minimized) noexcept;

private:
    bool created_ = false;
    bool closed_ = false;
    bool minimized_ = false;
    std::uint32_t width_ = 0u;
    std::uint32_t height_ = 0u;
    void* native_handle_ = nullptr;
};

} // namespace nw::platform
