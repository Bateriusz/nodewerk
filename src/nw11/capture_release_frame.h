#pragma once

#include <windows.h>

#include <array>
#include <cstdio>
#include <vector>

namespace nw11 {

inline bool CaptureReleaseFrame(DWORD pid, const char* output) {
    // The unmodified release player holds the requested tick on the desktop.
    // Capture its visible 1920x1080 SDR swap chain and normalize UNORM to RGBA32F.
    SetProcessDPIAware();
    const HWND window = GetForegroundWindow();
    DWORD window_pid = 0;
    GetWindowThreadProcessId(window, &window_pid);
    RECT bounds{};
    constexpr int width = 1920;
    constexpr int height = 1080;
    if (window_pid != pid || !GetClientRect(window, &bounds) ||
        bounds.right != width || bounds.bottom != height) return false;
    POINT origin{};
    if (!ClientToScreen(window, &origin)) return false;
    MONITORINFO monitor{};
    monitor.cbSize = sizeof(monitor);
    if (!GetMonitorInfoW(MonitorFromWindow(window, MONITOR_DEFAULTTONULL), &monitor) ||
        origin.x < monitor.rcMonitor.left || origin.y < monitor.rcMonitor.top ||
        origin.x + width > monitor.rcMonitor.right || origin.y + height > monitor.rcMonitor.bottom)
        return false;
    HDC desktop = GetDC(nullptr);
    if (!desktop) return false;
    HDC memory = CreateCompatibleDC(desktop);
    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(info.bmiHeader);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;
    void* mapped = nullptr;
    HBITMAP bitmap = CreateDIBSection(desktop, &info, DIB_RGB_COLORS, &mapped, nullptr, 0);
    HGDIOBJ previous = memory && bitmap ? SelectObject(memory, bitmap) : nullptr;
    bool okay = previous && mapped && BitBlt(memory, 0, 0, width, height,
        desktop, origin.x, origin.y, SRCCOPY | CAPTUREBLT);
    if (okay) okay = GdiFlush() != FALSE;
    if (okay) {
        std::array<float, width * 4> row{};
        const auto* bgra = static_cast<const unsigned char*>(mapped);
        FILE* file = nullptr;
        okay = fopen_s(&file, output, "wb") == 0 && file;
        if (okay) {
            for (int y = 0; y < height && okay; ++y) {
                for (int x = 0; x < width; ++x, bgra += 4) {
                    row[x * 4] = float(bgra[2]) / 255.0f;
                    row[x * 4 + 1] = float(bgra[1]) / 255.0f;
                    row[x * 4 + 2] = float(bgra[0]) / 255.0f;
                    row[x * 4 + 3] = 1.0f;
                }
                okay = std::fwrite(row.data(), sizeof(float), row.size(), file) == row.size();
            }
            if (std::fclose(file) != 0) okay = false;
        }
    }
    if (previous) SelectObject(memory, previous);
    if (bitmap) DeleteObject(bitmap);
    if (memory) DeleteDC(memory);
    ReleaseDC(nullptr, desktop);
    return okay;
}

}  // namespace nw11
