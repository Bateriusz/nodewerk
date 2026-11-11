#include "win32_window.h"

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#endif

namespace nw::platform {

#if defined(_WIN32)
namespace {

constexpr const wchar_t* kWindowClassName = L"NodewerkNw04PlayerWindow";

LRESULT CALLBACK Nw04WindowProc(HWND hwnd, UINT message, WPARAM wparam,
                                LPARAM lparam) {
    switch (message) {
        case WM_SIZE: {
            (void)hwnd;
            (void)lparam;
            (void)wparam;
            return 0;
        }
        case WM_GETMINMAXINFO: {
            auto* info = reinterpret_cast<MINMAXINFO*>(lparam);
            if (info != nullptr) {
                info->ptMinTrackSize.x = 64;
                info->ptMinTrackSize.y = 64;
            }
            return 0;
        }
        case WM_CLOSE:
            DestroyWindow(hwnd);
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
        case WM_ERASEBKGND:
            return 1;
        default:
            return DefWindowProcW(hwnd, message, wparam, lparam);
    }
}

} // namespace
#endif

Win32Window::~Win32Window() { Destroy(); }

bool Win32Window::Create(const WindowConfig& config) noexcept {
    if (created_) {
        return true;
    }
    width_ = config.width;
    height_ = config.height;
#if defined(_WIN32)
    HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSEXW window_class{};
    window_class.cbSize = static_cast<UINT>(sizeof(window_class));
    window_class.style = static_cast<DWORD>(CS_HREDRAW | CS_VREDRAW | CS_OWNDC);
    window_class.lpfnWndProc = &Nw04WindowProc;
    window_class.hInstance = instance;
    window_class.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    window_class.hbrBackground = reinterpret_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
    window_class.lpszClassName = kWindowClassName;
    if (RegisterClassExW(&window_class) == 0u) {
        const DWORD error = GetLastError();
        if (error != static_cast<DWORD>(ERROR_CLASS_ALREADY_EXISTS)) {
            return false;
        }
    }
    RECT rect{0, 0, static_cast<LONG>(config.width),
              static_cast<LONG>(config.height)};
    const DWORD style = WS_OVERLAPPEDWINDOW;
    AdjustWindowRectEx(&rect, style, FALSE, 0u);
    const int window_width = static_cast<int>(rect.right - rect.left);
    const int window_height = static_cast<int>(rect.bottom - rect.top);
    HWND hwnd = CreateWindowExW(
        0u, kWindowClassName, L"Nodewerk NW-04 Player", style,
        CW_USEDEFAULT, CW_USEDEFAULT, window_width, window_height, nullptr,
        nullptr, instance, nullptr);
    if (hwnd == nullptr) {
        return false;
    }
    native_handle_ = hwnd;
    created_ = true;
    closed_ = false;
    minimized_ = false;
    if (config.visible) {
        ShowWindow(hwnd, SW_SHOW);
        UpdateWindow(hwnd);
    }
    return true;
#else
    // GPU-less host simulation: model the same lifecycle without a window.
    native_handle_ = nullptr;
    created_ = true;
    closed_ = false;
    minimized_ = false;
    return true;
#endif
}

void Win32Window::Destroy() noexcept {
#if defined(_WIN32)
    if (native_handle_ != nullptr) {
        HWND hwnd = static_cast<HWND>(native_handle_);
        if (IsWindow(hwnd) != FALSE) {
            DestroyWindow(hwnd);
        }
    }
#endif
    native_handle_ = nullptr;
    created_ = false;
    closed_ = true;
    minimized_ = false;
}

bool Win32Window::PumpMessages() noexcept {
    if (!created_ || closed_) {
        return false;
    }
#if defined(_WIN32)
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0u, 0u, PM_REMOVE) != FALSE) {
        if (message.message == static_cast<UINT>(WM_QUIT)) {
            closed_ = true;
            return false;
        }
        TranslateMessage(&message);
        DispatchMessageW(&message);
        if (message.message == static_cast<UINT>(WM_SIZE)) {
            minimized_ = (message.wParam ==
                          static_cast<WPARAM>(SIZE_MINIMIZED));
        }
    }
    if (native_handle_ != nullptr) {
        HWND hwnd = static_cast<HWND>(native_handle_);
        if (IsWindow(hwnd) == FALSE) {
            closed_ = true;
            return false;
        }
        RECT client{};
        if (GetClientRect(hwnd, &client) != FALSE) {
            width_ = static_cast<std::uint32_t>(client.right - client.left);
            height_ = static_cast<std::uint32_t>(client.bottom - client.top);
            if (IsIconic(hwnd) != FALSE) {
                minimized_ = true;
            }
        }
    }
#endif
    return !closed_;
}

bool Win32Window::IsMinimized() const noexcept {
    if (minimized_ || width_ == 0u || height_ == 0u) {
        return true;
    }
#if defined(_WIN32)
    if (native_handle_ != nullptr &&
        IsIconic(static_cast<HWND>(native_handle_)) != FALSE) {
        return true;
    }
#endif
    return false;
}

bool Win32Window::IsClosed() const noexcept { return closed_; }

bool Win32Window::IsCreated() const noexcept { return created_; }

void Win32Window::GetClientSize(std::uint32_t* width,
                                std::uint32_t* height) const noexcept {
    if (width != nullptr) *width = width_;
    if (height != nullptr) *height = height_;
}

void Win32Window::RequestClientSize(std::uint32_t width,
                                    std::uint32_t height) noexcept {
    width_ = width;
    height_ = height;
    minimized_ = (width == 0u || height == 0u);
#if defined(_WIN32)
    if (native_handle_ != nullptr) {
        HWND hwnd = static_cast<HWND>(native_handle_);
        RECT rect{0, 0, static_cast<LONG>(width), static_cast<LONG>(height)};
        const DWORD style = static_cast<DWORD>(GetWindowLongPtrW(hwnd, GWL_STYLE));
        AdjustWindowRectEx(&rect, style, FALSE, 0u);
        SetWindowPos(hwnd, nullptr, 0, 0,
                     static_cast<int>(rect.right - rect.left),
                     static_cast<int>(rect.bottom - rect.top),
                     static_cast<UINT>(SWP_NOMOVE | SWP_NOZORDER |
                                       SWP_NOACTIVATE));
    }
#endif
}

void* Win32Window::NativeHandle() const noexcept { return native_handle_; }

void Win32Window::SetSimulatedMinimized(bool minimized) noexcept {
    minimized_ = minimized;
}

} // namespace nw::platform
