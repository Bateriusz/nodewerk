#include "audio/wasapi_output.h"
#include "audio/stream_progress.h"
#include <windows.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <limits>
#include <algorithm>
#include <cwchar>
#include <iterator>

namespace nw::audio {
namespace {
template<class T> struct Com {
    T* value = nullptr;
    ~Com() { if (value) value->Release(); }
    T* operator->() const noexcept { return value; }
};
struct Apartment {
    HRESULT status = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ~Apartment() { if (SUCCEEDED(status)) CoUninitialize(); }
};
struct EventHandle {
    HANDLE value = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    ~EventHandle() { if (value) CloseHandle(value); }
};
bool CpuTime(HANDLE thread, std::uint64_t& result) noexcept {
    FILETIME create{}, exit{}, kernel{}, user{};
    if (!GetThreadTimes(thread, &create, &exit, &kernel, &user)) return false;
    result = ((std::uint64_t(kernel.dwHighDateTime) << 32) | kernel.dwLowDateTime) +
             ((std::uint64_t(user.dwHighDateTime) << 32) | user.dwLowDateTime);
    return true;
}
}
bool IsWindowOnPlaybackMonitor(HWND window, HMONITOR expected) noexcept {
    if (!window || !expected || !IsWindowVisible(window) || IsIconic(window) ||
        MonitorFromWindow(window, MONITOR_DEFAULTTONULL) != expected) return false;
    MONITORINFO info{};
    info.cbSize = sizeof(info);
    RECT client{};
    if (!GetMonitorInfoW(expected, &info) || !GetClientRect(window, &client) ||
        client.right <= client.left || client.bottom <= client.top) return false;
    POINT first{client.left, client.top};
    POINT last{client.right, client.bottom};
    return ClientToScreen(window, &first) && ClientToScreen(window, &last) &&
           first.x >= info.rcMonitor.left && first.y >= info.rcMonitor.top &&
           last.x <= info.rcMonitor.right && last.y <= info.rcMonitor.bottom;
}

PlaybackDisplay GetPlaybackDisplay(HWND window) noexcept {
    if (!window || GetSystemMetrics(SM_REMOTESESSION)) return {};
    const auto monitor = MonitorFromWindow(window, MONITOR_DEFAULTTONULL);
    if (!IsWindowOnPlaybackMonitor(window, monitor)) return {};
    MONITORINFOEXW info{};
    info.cbSize = sizeof(info);
    if (!GetMonitorInfoW(monitor, reinterpret_cast<MONITORINFO*>(&info))) return {};

    // QDC_VIRTUAL_REFRESH_RATE_AWARE (Windows 11); reject dynamic refresh,
    // clones and indirect/virtual targets instead of choosing a plausible mode.
    constexpr UINT32 flags = QDC_ONLY_ACTIVE_PATHS | 0x40u;
    constexpr UINT32 dynamic_refresh = 0x10u; // DISPLAYCONFIG_PATH_BOOST_REFRESH_RATE
    std::array<DISPLAYCONFIG_PATH_INFO, 64> paths{};
    std::array<DISPLAYCONFIG_MODE_INFO, 192> modes{};
    UINT32 path_count = static_cast<UINT32>(paths.size());
    UINT32 mode_count = static_cast<UINT32>(modes.size());
    // QueryDisplayConfig accepts the refresh-awareness modifier and the input
    // capacities. Do not pass that modifier to GetDisplayConfigBufferSizes.
    const LONG query = QueryDisplayConfig(flags, &path_count, paths.data(),
                                          &mode_count, modes.data(), nullptr);
    if (query == ERROR_INSUFFICIENT_BUFFER) return {}; // report unavailable, not a partial path
    if (query != ERROR_SUCCESS || path_count == 0) return {};
    const DISPLAYCONFIG_PATH_INFO* selected = nullptr;
    for (UINT32 i = 0; i < path_count; ++i) {
        const auto& path = paths[i];
        DISPLAYCONFIG_SOURCE_DEVICE_NAME source{};
        source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
        source.header.size = sizeof(source);
        source.header.adapterId = path.sourceInfo.adapterId;
        source.header.id = path.sourceInfo.id;
        if (DisplayConfigGetDeviceInfo(&source.header) != ERROR_SUCCESS) return {};
        if (std::wcscmp(source.viewGdiDeviceName, info.szDevice) != 0) continue;
        if (selected) return {};
        selected = &path;
    }
    if (!selected || !selected->targetInfo.targetAvailable ||
        (selected->flags & dynamic_refresh)) return {};
    const auto& target_info = selected->targetInfo;
    const auto tech = target_info.outputTechnology;
    if (tech == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_OTHER ||
        tech == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_MIRACAST ||
        tech == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_WIRED ||
        tech == DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INDIRECT_VIRTUAL) return {};
    DISPLAYCONFIG_TARGET_DEVICE_NAME target{};
    target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME;
    target.header.size = sizeof(target);
    target.header.adapterId = target_info.adapterId;
    target.header.id = target_info.id;
    const auto rate = target_info.refreshRate;
    if (!rate.Numerator || !rate.Denominator ||
        DisplayConfigGetDeviceInfo(&target.header) != ERROR_SUCCESS ||
        !target.monitorDevicePath[0]) return {};
    PlaybackDisplay result{};
    result.monitor = monitor;
    result.refresh_numerator = rate.Numerator;
    result.refresh_denominator = rate.Denominator;
    static_assert(std::size(target.monitorDevicePath) == result.device_path.size());
    std::copy_n(target.monitorDevicePath, result.device_path.size(), result.device_path.data());
    if (result.device_path.back() != L'\0') return {};
    return result;
}

WasapiOutput::~WasapiOutput() { stop(); }
bool WasapiOutput::start(const Song& song, std::uint64_t tick, bool collect_evidence) {
    stop();
    if (!ValidateSong(song) || tick >= song.duration_ticks) return false;
    song_ = song;
    origin_ = tick;
    collect_evidence_ = collect_evidence;
    endpoint_id_[0] = L'\0';
    tick_.store(tick);
    failed_.store(false);
    ready_.store(false);
    stop_.store(false);
    starvation_.store(0);
    discontinuities_.store(0);
    cpu_.store(0);
    cpu_available_.store(true);
    worker_cpu_available_.store(false);
    worker_cpu_.store(0);
    underruns_.store(0);
    missing_.store(0);
    worker_ = CreateThread(nullptr, 0, &WasapiOutput::ThreadEntry, this, 0, nullptr);
    if (!worker_) { failed_.store(true); return false; }
    // Startup handshake only; never executed by the audio worker or callback.
    while (!ready_.load() && !failed_.load()) Sleep(1);
    if (failed_.load()) { stop(); return false; }
    return true;
}
bool WasapiOutput::seek(std::uint64_t tick) {
    const Song copy = song_;
    return start(copy, tick);
}
void WasapiOutput::stop() noexcept {
    stop_.store(true);
    if (worker_) {
        WaitForSingleObject(worker_, INFINITE);
        std::uint64_t total = 0;
        worker_cpu_available_.store(CpuTime(worker_, total));
        worker_cpu_.store(total);
        CloseHandle(worker_);
        worker_ = nullptr;
    }
}
DWORD WINAPI WasapiOutput::ThreadEntry(void* context) noexcept {
    static_cast<WasapiOutput*>(context)->run();
    return 0;
}
void WasapiOutput::run() noexcept {
    auto fail = [this]() noexcept { failed_.store(true); };
    Apartment apartment;
    if (FAILED(apartment.status)) { fail(); return; }
    Com<IMMDeviceEnumerator> enumerator;
    Com<IMMDevice> device;
    Com<IAudioClient> client;
    Com<IAudioRenderClient> render;
    Com<IAudioClock> clock;
    EventHandle event;
    if (!event.value ||
        FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                IID_PPV_ARGS(&enumerator.value))) ||
        FAILED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device.value)) ||
        FAILED(device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                                reinterpret_cast<void**>(&client.value)))) {
        fail(); return;
    }
    if (collect_evidence_) {
        LPWSTR id = nullptr;
        DWORD state = 0;
        if (SUCCEEDED(device->GetState(&state)) && state == DEVICE_STATE_ACTIVE &&
            SUCCEEDED(device->GetId(&id)) && id) {
            const auto length = std::wcslen(id);
            if (length < endpoint_id_.size())
                std::copy_n(id, length + 1u, endpoint_id_.data());
        }
        CoTaskMemFree(id);
    }
    const auto pcm = SongPcmFormat(song_);
    PcmStream stream;
    if (!stream.Start(song_, pcm, origin_)) { fail(); return; }
    WAVEFORMATEX format{};
    format.wFormatTag = WAVE_FORMAT_PCM;
    format.nChannels = static_cast<WORD>(pcm.channels);
    format.nSamplesPerSec = pcm.sample_rate;
    format.wBitsPerSample = 16;
    format.nBlockAlign = static_cast<WORD>(pcm.channels * sizeof(std::int16_t));
    format.nAvgBytesPerSec = format.nSamplesPerSec * format.nBlockAlign;
    // Shared mode allows endpoint conversion. The actual clock units/second
    // come from IAudioClock, not the requested stream sample rate.
    constexpr DWORD flags = AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    UINT32 capacity = 0;
    UINT64 frequency = 0, base_position = 0;
    if (FAILED(client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, &format, nullptr)) ||
        FAILED(client->SetEventHandle(event.value)) ||
        FAILED(client->GetBufferSize(&capacity)) || !capacity ||
        FAILED(client->GetService(IID_PPV_ARGS(&render.value))) ||
        FAILED(client->GetService(IID_PPV_ARGS(&clock.value))) ||
        FAILED(clock->GetFrequency(&frequency)) || !frequency ||
        frequency > (std::numeric_limits<std::uint32_t>::max)() ||
        FAILED(clock->GetPosition(&base_position, nullptr))) {
        fail(); return;
    }
    std::uint64_t epoch_origin = origin_;
    std::uint64_t cursor = origin_;
    StreamProgress progress{origin_, origin_};
    auto fill = [&](UINT32 frames) noexcept {
        if (!frames) return true;
        BYTE* data = nullptr;
        if (FAILED(render->GetBuffer(frames, &data))) return false;
        std::uint64_t before = 0, after = 0;
        const bool before_available = CpuTime(GetCurrentThread(), before);
        const bool ok = stream.Render(reinterpret_cast<std::int16_t*>(data), frames);
        const bool after_available = CpuTime(GetCurrentThread(), after);
        if (before_available && after_available && after >= before)
            cpu_.fetch_add(after - before);
        else cpu_available_.store(false);
        const auto hr = render->ReleaseBuffer(frames, ok ? 0u : AUDCLNT_BUFFERFLAGS_SILENT);
        if (!ok || FAILED(hr)) return false;
        cursor += frames;
        progress.submitted_until = cursor;
        return true;
    };
    if (!fill(capacity) || FAILED(client->Start())) { fail(); return; }
    ready_.store(true);
    auto clock_tick = [&](std::uint64_t& tick) noexcept {
        UINT64 position = 0;
        std::uint64_t elapsed = 0;
        if (FAILED(clock->GetPosition(&position, nullptr)) || position < base_position ||
            !SampleToTick(position - base_position, static_cast<std::uint32_t>(frequency), elapsed) ||
            elapsed > (std::numeric_limits<std::uint64_t>::max)() - epoch_origin)
            return false;
        tick = epoch_origin + elapsed;
        return true;
    };
    while (!stop_.load()) {
        const DWORD wait = WaitForSingleObject(event.value, 10);
        if (wait != WAIT_OBJECT_0 && wait != WAIT_TIMEOUT) { fail(); break; }
        // Read padding first and the clock immediately afterwards; no frames
        // are submitted between these observations. Sample even on timeout.
        UINT32 padding = 0;
        std::uint64_t tick = 0;
        if (FAILED(client->GetCurrentPadding(&padding)) || !clock_tick(tick)) {
            discontinuities_.fetch_add(1); fail(); break;
        }
        // No submissions occur between samples. The first padding sample
        // can legally become zero while the thread is waiting for the clock.
        if (progress.needs_padding_refresh(tick, padding, capacity) &&
            FAILED(client->GetCurrentPadding(&padding))) {
            discontinuities_.fetch_add(1); fail(); break;
        }
        if (!padding) starvation_.fetch_add(1);
        const auto action = progress.observe(tick, padding, capacity);
        if (action == StreamAction::Discontinuity) {
            discontinuities_.fetch_add(1); fail(); break;
        }
        tick_.store(tick);
        if (action == StreamAction::Recover) {
            // Stop the stream before choosing the new sample origin, so
            // elapsed audio cannot be submitted under the old clock epoch.
            if (FAILED(client->Stop()) || !clock_tick(tick) || tick < progress.last) {
                discontinuities_.fetch_add(1); fail(); break;
            }
            if (tick > progress.submitted_until)
                progress.missing_samples += tick - progress.submitted_until;
            underruns_.store(progress.observed_underruns);
            missing_.store(progress.missing_samples);
            discontinuities_.fetch_add(1);
            epoch_origin = tick;
            cursor = tick;
            progress.last = tick;
            progress.submitted_until = tick;
            tick_.store(tick);
            if (!stream.Start(song_, pcm, cursor) || FAILED(client->Reset()) ||
                FAILED(clock->GetPosition(&base_position, nullptr)) ||
                !fill(capacity) || FAILED(client->Start())) { fail(); break; }
        } else if (!fill(capacity - padding)) { fail(); break; }
    }
    if (FAILED(client->Stop())) fail();
}
}
