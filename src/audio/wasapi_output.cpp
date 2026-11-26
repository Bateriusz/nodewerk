#include "audio/wasapi_output.h"
#include <windows.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <limits>

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
std::uint64_t CpuTime() noexcept {
    FILETIME create{}, exit{}, kernel{}, user{};
    if (!GetThreadTimes(GetCurrentThread(), &create, &exit, &kernel, &user)) return 0;
    return ((std::uint64_t(kernel.dwHighDateTime) << 32) | kernel.dwLowDateTime) +
           ((std::uint64_t(user.dwHighDateTime) << 32) | user.dwLowDateTime);
}
}
WasapiOutput::~WasapiOutput() { stop(); }
bool WasapiOutput::start(const Song& song, std::uint64_t tick) {
    stop();
    if (!ValidateSong(song) || tick >= song.duration_ticks) return false;
    song_ = song;
    origin_ = tick;
    tick_.store(tick);
    failed_.store(false);
    ready_.store(false);
    stop_.store(false);
    starvation_.store(0);
    discontinuities_.store(0);
    cpu_.store(0);
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
    WAVEFORMATEX format{};
    format.wFormatTag = WAVE_FORMAT_PCM;
    format.nChannels = 1;
    format.nSamplesPerSec = player::kTicksPerSecond;
    format.wBitsPerSample = 16;
    format.nBlockAlign = 2;
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
    std::uint64_t cursor = origin_;
    auto fill = [&](UINT32 frames) noexcept {
        if (!frames) return true;
        BYTE* data = nullptr;
        if (FAILED(render->GetBuffer(frames, &data))) return false;
        const auto before = CpuTime();
        const bool ok = RenderPcm(song_, {}, cursor,
                                  reinterpret_cast<std::int16_t*>(data), frames);
        const auto after = CpuTime();
        if (before && after >= before) cpu_.fetch_add(after - before);
        const auto hr = render->ReleaseBuffer(frames, ok ? 0u : AUDCLNT_BUFFERFLAGS_SILENT);
        if (!ok || FAILED(hr)) return false;
        cursor += frames;
        return true;
    };
    if (!fill(capacity) || FAILED(client->Start())) { fail(); return; }
    ready_.store(true);
    std::uint64_t last = origin_;
    while (!stop_.load()) {
        const DWORD wait = WaitForSingleObject(event.value, 10);
        if (wait != WAIT_OBJECT_0 && wait != WAIT_TIMEOUT) { fail(); break; }
        UINT64 position = 0;
        std::uint64_t elapsed = 0;
        if (FAILED(clock->GetPosition(&position, nullptr))) { fail(); break; }
        if (position < base_position ||
            !SampleToTick(position - base_position, static_cast<std::uint32_t>(frequency), elapsed) ||
            elapsed > (std::numeric_limits<std::uint64_t>::max)() - origin_) {
            discontinuities_.fetch_add(1); fail(); break;
        }
        const auto tick = origin_ + elapsed;
        if (tick < last) { discontinuities_.fetch_add(1); fail(); break; }
        last = tick;
        tick_.store(tick);
        if (wait == WAIT_OBJECT_0) {
            UINT32 padding = 0;
            if (FAILED(client->GetCurrentPadding(&padding)) || padding > capacity) { fail(); break; }
            if (!padding) {
                starvation_.fetch_add(1);
                // If playback advanced beyond the submitted samples, do not
                // replay elapsed audio. Reconstruct from the actual clock.
                if (tick > cursor) cursor = tick;
            }
            if (!fill(capacity - padding)) { fail(); break; }
        }
    }
    if (FAILED(client->Stop())) fail();
}
}
