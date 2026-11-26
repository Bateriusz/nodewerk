#pragma once

#include <windows.h>
#include <dwmapi.h>

#include <array>
#include <cmath>
#include <cstdio>

namespace nw11 {

inline bool CaptureReleaseFrame(DWORD pid, const char* output, const char* reference,
                                double deadline_qpc_ms) {
    // The unmodified release player holds the requested tick on the desktop.
    // Capture its visible 1920x1080 SDR swap chain and normalize UNORM to RGBA32F.
    SetProcessDPIAware();
    const auto fail = [pid](const char* reason) {
        std::printf("{\"source\":\"dwm_composed_client\",\"process_id\":%lu,\"failure_reason\":\"%s\"}\n",
                    static_cast<unsigned long>(pid), reason);
        return false;
    };
    constexpr int width = 1920;
    constexpr int height = 1080;
    // A composed blank client area can precede the sole present. Use the
    // independently rendered SDR reference to recognize visible scene content.
    struct Anchor { int x, y; std::array<float, 4> rgba; };
    std::array<Anchor, 63> anchors{};
    FILE* expected = nullptr;
    if (fopen_s(&expected, reference, "rb") || !expected) return fail("reference_open_failed");
    size_t count = 0;
    bool loaded = true;
    for (int y = 1; y <= 7 && loaded; ++y) {
        for (int x = 1; x <= 9 && loaded; ++x) {
            auto& anchor = anchors[count++];
            anchor.x = x * width / 10;
            anchor.y = y * height / 8;
            const long offset = (anchor.y * width + anchor.x) * 4 * sizeof(float);
            loaded = std::fseek(expected, offset, SEEK_SET) == 0 &&
                     std::fread(anchor.rgba.data(), sizeof(float), 4, expected) == 4;
            for (float component : anchor.rgba) loaded = loaded && std::isfinite(component);
        }
    }
    std::fclose(expected);
    if (!loaded || !std::isfinite(deadline_qpc_ms)) return fail("invalid_reference_or_deadline");
    LARGE_INTEGER frequency{};
    if (!QueryPerformanceFrequency(&frequency)) return fail("qpc_unavailable");
    const auto now = [&frequency]() {
        LARGE_INTEGER value{};
        QueryPerformanceCounter(&value);
        return 1000.0 * double(value.QuadPart) / double(frequency.QuadPart);
    };
    HDC desktop = GetDC(nullptr);
    if (!desktop) return fail("desktop_dc_unavailable");
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
    bool okay = false;
    double captured_qpc_ms = 0;
    const char* failure_reason = "capture_resources_unavailable";
    unsigned foreground_samples = 0, geometry_samples = 0, pixel_samples = 0;
    DWORD last_foreground_pid = 0;
    RECT last_bounds{};
    POINT last_origin{};
    RECT last_monitor{};
    double best_anchor_error = -1;
    if (previous && previous != HGDI_ERROR && mapped) {
        failure_reason = "foreground_window_not_observed";
        while (now() <= deadline_qpc_ms) {
            const HWND window = GetForegroundWindow();
            DWORD window_pid = 0;
            GetWindowThreadProcessId(window, &window_pid);
            last_foreground_pid = window_pid;
            if (window_pid != pid) { Sleep(8); continue; }
            ++foreground_samples;
            RECT bounds{};
            POINT origin{};
            MONITORINFO monitor{};
            monitor.cbSize = sizeof(monitor);
            if (!GetClientRect(window, &bounds) || !ClientToScreen(window, &origin) ||
                !GetMonitorInfoW(MonitorFromWindow(window, MONITOR_DEFAULTTONULL), &monitor)) {
                failure_reason = "window_geometry_unavailable";
                Sleep(8); continue;
            }
            last_bounds = bounds;
            last_origin = origin;
            last_monitor = monitor.rcMonitor;
            if (bounds.right != width || bounds.bottom != height ||
                origin.x < monitor.rcMonitor.left || origin.y < monitor.rcMonitor.top ||
                origin.x + width > monitor.rcMonitor.right || origin.y + height > monitor.rcMonitor.bottom) {
                failure_reason = "native_client_not_fully_visible";
                Sleep(8); continue;
            }
            ++geometry_samples;
            if (FAILED(DwmFlush()) || GetForegroundWindow() != window ||
                !BitBlt(memory, 0, 0, width, height, desktop, origin.x, origin.y, SRCCOPY | CAPTUREBLT) ||
                !GdiFlush()) {
                failure_reason = "desktop_capture_unavailable";
                Sleep(8); continue;
            }
            ++pixel_samples;
            captured_qpc_ms = now();
            const auto* bgra = static_cast<const unsigned char*>(mapped);
            double anchor_error = 0;
            for (const auto& anchor : anchors) {
                const auto* pixel = bgra + (anchor.y * width + anchor.x) * 4;
                for (int channel = 0; channel < 3; ++channel) {
                    const double error = std::abs(float(pixel[2 - channel]) / 255.0f - anchor.rgba[channel]);
                    if (error > anchor_error) anchor_error = error;
                }
            }
            if (best_anchor_error < 0 || anchor_error < best_anchor_error) best_anchor_error = anchor_error;
            okay = captured_qpc_ms <= deadline_qpc_ms && anchor_error <= double(1e-4f);
            if (okay) break;
            failure_reason = captured_qpc_ms > deadline_qpc_ms ? "startup_deadline_exceeded" : "reference_anchors_mismatch";
            Sleep(8);
        }
    }
    if (okay) {
        failure_reason = "capture_write_failed";
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
    if (previous && previous != HGDI_ERROR) SelectObject(memory, previous);
    if (bitmap) DeleteObject(bitmap);
    if (memory) DeleteDC(memory);
    ReleaseDC(nullptr, desktop);
    if (okay) {
        std::printf("{\"source\":\"dwm_composed_client\",\"process_id\":%lu,\"observed_qpc_ms\":%.6f}\n",
                    static_cast<unsigned long>(pid), captured_qpc_ms);
    } else {
        std::printf("{\"source\":\"dwm_composed_client\",\"process_id\":%lu,"
                    "\"failure_reason\":\"%s\",\"foreground_pid\":%lu,"
                    "\"foreground_samples\":%u,\"geometry_samples\":%u,\"pixel_samples\":%u,"
                    "\"client_width\":%ld,\"client_height\":%ld,\"origin\":[%ld,%ld],"
                    "\"monitor\":[%ld,%ld,%ld,%ld],\"best_anchor_error\":%.9g,"
                    "\"deadline_qpc_ms\":%.6f,\"last_capture_qpc_ms\":%.6f}\n",
                    static_cast<unsigned long>(pid), failure_reason,
                    static_cast<unsigned long>(last_foreground_pid), foreground_samples, geometry_samples,
                    pixel_samples, last_bounds.right, last_bounds.bottom, last_origin.x, last_origin.y,
                    last_monitor.left, last_monitor.top, last_monitor.right, last_monitor.bottom,
                    best_anchor_error, deadline_qpc_ms, captured_qpc_ms);
    }
    return okay;
}

}  // namespace nw11
