// Measurement frontend for the production PackagePlayer. No renderer, queue,
// root signature, shader dispatch, resource allocator or swap chain lives here.
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "nw11_scene_harness.h"
#include "nw11_scene_generated.h"
#include "player/package_player.h"
#include "player/embedded_package.h"

namespace nw11 {
extern std::uint64_t peak_engine_committed_gpu_bytes;
extern std::uint64_t first_present_filetime;
namespace {
using nw::player::ExitCode;
using Clock = std::chrono::steady_clock;
constexpr int kExitOk = 0;
constexpr int kExitUnsupported = 3;
constexpr int kExitInitializationFailed = 4;
const std::array<std::uint32_t, 6>& contract_reference_ticks() {
    static const std::array<std::uint32_t, 6> ticks = {0u, 240000u, 480000u, 720000u, 959999u, 960000u};
    return ticks;
}
std::string JsonEscape(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 8);
    for (char character : value) {
        switch (character) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(character) < 0x20) {
                    char buffer[8] = {};
                    std::snprintf(
                        buffer, sizeof(buffer), "\\u%04x",
                        static_cast<unsigned>(static_cast<unsigned char>(character)));
                    out += buffer;
                } else {
                    out.push_back(character);
                }
        }
    }
    return out;
}

std::string ReadFile(const std::string& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
        throw std::runtime_error("cannot open file: " + path);
    }
    std::ostringstream buffer;
    buffer << stream.rdbuf();
    return buffer.str();
}

std::uint64_t FiletimeTicks(FILETIME value) {
    return (std::uint64_t(value.dwHighDateTime) << 32u) | value.dwLowDateTime;
}

std::uint64_t ProcessCreationFiletime() {
    FILETIME created{}, exited{}, kernel{}, user{};
    if (!GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user))
        throw std::runtime_error("cannot measure process creation time");
    return FiletimeTicks(created);
}

double QpcMilliseconds() {
    LARGE_INTEGER count{}, frequency{};
    if (!QueryPerformanceCounter(&count) || !QueryPerformanceFrequency(&frequency))
        throw std::runtime_error("cannot read presentation measurement clock");
    return 1000.0 * double(count.QuadPart) / double(frequency.QuadPart);
}

struct RunIdentity {
    std::string executable_hash;
    std::string package_hash;
    std::uint64_t process_creation_filetime = ProcessCreationFiletime();
    double warmup_start_qpc_ms = 0;
    double measurement_start_qpc_ms = 0;
    double measurement_end_qpc_ms = 0;
};

void WriteIdentity(std::ostream& json, const RendererOptions& options, const RunIdentity& run) {
    json << "  \"launch_id\": \"" << JsonEscape(options.launch_id) << "\",\n"
         << "  \"tested_commit\": \"" << JsonEscape(options.commit) << "\",\n"
         << "  \"executable_sha256\": \"" << run.executable_hash << "\",\n"
         << "  \"package_sha256\": \"" << run.package_hash << "\",\n"
         << "  \"process_id\": " << GetCurrentProcessId() << ",\n"
         << "  \"process_creation_filetime\": " << run.process_creation_filetime << ",\n";
}

void WriteCaptureManifest(const RendererOptions& options_, const RunIdentity& run) {
    if (options_.capture_manifest.empty()) {
        return;
    }
    std::filesystem::path manifest_path(options_.capture_manifest);
    if (!manifest_path.parent_path().empty()) {
        std::filesystem::create_directories(manifest_path.parent_path());
    }
    std::ostringstream json;
    json << "{\n";
    json << "  \"format\": \"nw11-capture-set\",\n";
    json << "  \"version\": 1,\n";
    WriteIdentity(json, options_, run);
    json << "  \"fixed_ticks\": [0, 240000, 480000, 720000, 959999, 960000],\n";
    json << "  \"images\": [\n";
    const auto ticks = contract_reference_ticks();
    for (std::size_t index = 0; index < ticks.size(); ++index) {
        const std::string name = std::to_string(ticks[index]) + ".rgba";
        json << "    {\"tick\": " << ticks[index]
             << ", \"path\": \"" << JsonEscape(name) << "\", \"width\": "
             << scene::kOutputWidth << ", \"height\": " << scene::kOutputHeight
             << ", \"format\": \"rgba32f\"}";
        json << (index + 1 < ticks.size() ? ",\n" : "\n");
    }
    json << "  ]\n}\n";
    std::ofstream stream(options_.capture_manifest, std::ios::binary);
    if (!stream) {
        throw std::runtime_error(
            "cannot write capture manifest: " + options_.capture_manifest);
    }
    const std::string text = json.str();
    stream.write(text.data(), static_cast<std::streamsize>(text.size()));
}

void WriteReport(const RendererOptions& options_, const nw::player::PackagePlayer& player, const std::vector<float>& gpu_samples_ms_, const std::vector<float>& cpu_samples_ms_, const RunIdentity& run, double startup_ms) {
    if (options_.benchmark_json.empty()) {
        throw std::runtime_error("--benchmark-json is required for a measured run");
    }
    std::string decision = "{}";
    if (!options_.capability_decision_path.empty()) {
        decision = ReadFile(options_.capability_decision_path);
        if (decision.empty()) {
            throw std::runtime_error("capability decision file is empty");
        }
    }
    std::ostringstream json;
    json << std::setprecision(17);
    json << "{\n";
    json << "  \"format\": \"nw11-raw-run.v1\",\n";
    json << "  \"version\": 1,\n";
    json << "  \"task\": \"NW-11\",\n";
    WriteIdentity(json, options_, run);
    json << "  \"timing\": {\"warmup_start_qpc_ms\": " << run.warmup_start_qpc_ms
         << ", \"measurement_start_qpc_ms\": " << run.measurement_start_qpc_ms
         << ", \"measurement_end_qpc_ms\": " << run.measurement_end_qpc_ms
         << "},\n";

    json << "  \"role\": \"" << JsonEscape(options_.role) << "\",\n";
    json << "  \"device_tier\": \"" << JsonEscape(options_.device_tier) << "\",\n";
    json << "  \"device_key\": \"" << JsonEscape(options_.device_key) << "\",\n";
    json << "  \"environment_key\": \"" << JsonEscape(options_.environment_key)
         << "\",\n";
    const auto& caps = player.queried_capabilities();
    json << "  \"adapter\": {\"adapter_name\": \"" << JsonEscape(caps.adapter_description)
         << "\", \"adapter_luid\": \"" << JsonEscape(options_.adapter_luid)
         << "\", \"vendor_id\": " << caps.vendor_id
         << ", \"device_id\": " << caps.device_id << "},\n";
    json << "  \"status\": \"eligible\",\n";
    json << "  \"artifact_path\": \"" << JsonEscape(options_.artifact_path)
         << "\",\n";
    json << "  \"capability_decision\": " << decision << ",\n";
    json << "  \"measurement\": {\n";
    auto write_samples = [&](const char* name, const std::vector<float>& values,
                             bool last) {
        json << "    \"" << name << "\": [";
        for (std::size_t index = 0; index < values.size(); ++index) {
            if (index) {
                json << ", ";
            }
            json << values[index];
        }
        json << "]" << (last ? "\n" : ",\n");
    };
    write_samples("gpu_time_samples", gpu_samples_ms_, false);
    write_samples("cpu_submit_samples", cpu_samples_ms_, false);
    json << "    \"presented_frames\": " << player.frames_presented() << ",\n";
    json << "    \"process_start_to_first_present_ms\": " << startup_ms
         << ",\n";
    json << "    \"distribution_bytes\": " << options_.distribution_bytes
         << ",\n";
    json << "    \"shader_count\": " << options_.shader_ids.size() << ",\n";
    json << "    \"shader_ids\": [";
    for (std::size_t index = 0; index < options_.shader_ids.size(); ++index) {
        if (index) {
            json << ", ";
        }
        json << "\"" << JsonEscape(options_.shader_ids[index]) << "\"";
    }
    json << "],\n";
    json << "    \"peak_engine_committed_gpu_bytes\": " << peak_engine_committed_gpu_bytes
         << "\n";
    json << "  },\n";
    json << "  \"shader_inventory\": [";
    for (std::size_t index = 0; index < options_.shader_ids.size(); ++index) {
        if (index) {
            json << ", ";
        }
        json << "\"" << JsonEscape(options_.shader_ids[index]) << "\"";
    }
    json << "],\n";
    json << "  \"raw_per_sample_results_preserved\": true,\n";
    json << "  \"operator_supplied_values\": false\n";
    json << "}\n";

    const std::filesystem::path path(options_.benchmark_json);
    if (!path.parent_path().empty()) {
        std::filesystem::create_directories(path.parent_path());
    }
    std::ofstream stream(options_.benchmark_json, std::ios::binary);
    if (!stream) {
        throw std::runtime_error(
            "cannot write benchmark report: " + options_.benchmark_json);
    }
    const std::string text = json.str();
    stream.write(text.data(), static_cast<std::streamsize>(text.size()));
}


std::string ExecutableSha256() {
    wchar_t module[32768]{};
    const DWORD length = GetModuleFileNameW(nullptr, module, static_cast<DWORD>(std::size(module)));
    if (length == 0 || length >= std::size(module))
        throw std::runtime_error("cannot identify running executable");
    std::ifstream image(std::filesystem::path(module), std::ios::binary);
    const std::string image_bytes{std::istreambuf_iterator<char>(image), {}};
    if (image_bytes.empty()) throw std::runtime_error("cannot hash running executable");
    return nw::package::Sha256Hex(image_bytes);
}

int RunProduction(const RendererOptions& options) {
    if (options.fixed_quality != "nw07.quality.v1")
        throw std::runtime_error("unexpected fixed-quality profile");
    if (options.runs != 1 || options.run_seconds == 0)
        throw std::runtime_error("each measurement requires one fresh process");
    if (!options.allow_software_adapter && (options.adapter_luid.empty() || options.headless))
        throw std::runtime_error("physical validation requires a probe-selected adapter and presentation");
    if (!SetEnvironmentVariableA("NW11_ADAPTER_LUID", options.adapter_luid.empty() ? nullptr : options.adapter_luid.c_str()))
        throw std::runtime_error("cannot select probe adapter");
    nw::player::EmbeddedPackage embedded;
    nw::player::EmbeddedLinkMetadata metadata;
    if (!nw::player::LoadEmbeddedPackage(&embedded) ||
        !nw::player::ParseEmbeddedLinkMetadata(embedded, &metadata))
        throw std::runtime_error("missing or invalid embedded NW-07 production package");
    RunIdentity run;
    if (!options.launch_id.empty()) {
        run.executable_hash = ExecutableSha256();
        run.package_hash = nw::package::Sha256Hex(embedded.bytes, embedded.size);
    }
    nw::player::PlayerConfig config;
    config.width = scene::kOutputWidth;
    config.height = scene::kOutputHeight;
    config.present_sync_interval = 0;
    // Hosted WARP smoke uses the player's synchronous timestamp readback.
    // Physical runs use external GPU/display telemetry and normal frame retirement.
    config.wait_for_gpu_each_frame = options.allow_software_adapter;
    config.allow_software_adapter = options.allow_software_adapter;
    config.headless = options.headless;
    config.create_window = !options.headless;
    config.memory_budget_bytes = metadata.memory_budget_bytes;
    config.query_optional_wave_ops = metadata.wave_ops;
    config.query_optional_fp16_shader_ops = metadata.fp16_shader_ops;
    config.query_optional_int64_shader_ops = metadata.int64_shader_ops;
    nw::player::PackagePlayer player(config);
    auto status = player.initialize(embedded.bytes, embedded.size);
    if (status != ExitCode::Success) {
        player.Report(true);
        return status == ExitCode::OptionalFeatureUnsupported ? kExitUnsupported : kExitInitializationFailed;
    }
    auto render = [&](std::uint64_t tick) {
        if (player.renderAtTick(tick) != ExitCode::Success || player.window_closed()) {
            player.Report(true);
            throw std::runtime_error("production frame execution failed");
        }
        if (player.width() != scene::kOutputWidth || player.height() != scene::kOutputHeight)
            throw std::runtime_error("production presentation dimensions changed");
    };
    render(0);
    if (!options.headless && !player.frames_presented())
        throw std::runtime_error("first production frame was not presented");
    if (!options.headless && first_present_filetime < run.process_creation_filetime)
        throw std::runtime_error("invalid first presentation timestamp");
    const double startup_ms = options.headless ? 0.0 :
        double(first_present_filetime - run.process_creation_filetime) / 10000.0;
    run.warmup_start_qpc_ms = QpcMilliseconds();
    const auto origin = Clock::now();
    auto seconds = [&](auto t) { return std::chrono::duration<double>(t - origin).count(); };
    auto tick_at = [&](auto t) { return static_cast<std::uint64_t>(
        std::fmod(seconds(t) * scene::kTicksPerSecond, double(scene::kLoopTicks))); };
    while (seconds(Clock::now()) < options.warmup_seconds) render(tick_at(Clock::now()));
    std::vector<float> gpu, cpu;
    run.measurement_start_qpc_ms = QpcMilliseconds();
    const auto start = Clock::now();
    while (std::chrono::duration<double>(Clock::now() - start).count() < options.run_seconds) {
        const auto begin = Clock::now();
        render(tick_at(begin));
        const auto& timing = player.instrumentation().timing();
        if (!timing.cpu_available)
            throw std::runtime_error("production CPU submission timing unavailable");
        if (options.allow_software_adapter) {
            if (!timing.gpu_available || timing.gpu_ms <= 0.0)
                throw std::runtime_error("production GPU timing unavailable in WARP smoke");
            gpu.push_back(static_cast<float>(timing.gpu_ms));
        }
        cpu.push_back(static_cast<float>(timing.cpu_submit_ms));
    }
    run.measurement_end_qpc_ms = QpcMilliseconds();
    if (cpu.empty() || peak_engine_committed_gpu_bytes == 0 || player.allocation_stats().post_initialization_allocation_violation)
        throw std::runtime_error("missing samples or invalid engine allocation accounting");
    WriteReport(options, player, gpu, cpu, run, startup_ms);
    if (!options.capture_dir.empty()) {
        std::filesystem::create_directories(options.capture_dir);
        for (auto tick : contract_reference_ticks()) {
            render(tick);
            std::vector<std::uint8_t> pixels;
            const auto capture = options.capture_display ? player.readbackDisplayOutput(&pixels)
                                                         : player.readbackPresentedOutput(&pixels);
            if (capture != ExitCode::Success)
                throw std::runtime_error("production capture failed");
            const auto pixel_count = std::uint64_t(scene::kOutputWidth) * scene::kOutputHeight;
            if (options.capture_display) {
                if (pixels.size() != pixel_count * 4u)
                    throw std::runtime_error("production display capture size changed");
                std::vector<float> rgba;
                rgba.reserve(pixels.size());
                for (auto channel : pixels) rgba.push_back(float(channel) / 255.0f);
                const auto* bytes = reinterpret_cast<const std::uint8_t*>(rgba.data());
                pixels.assign(bytes, bytes + rgba.size() * sizeof(float));
            }
            if (pixels.size() != pixel_count * 16u)
                throw std::runtime_error("production RGBA32F capture size changed");
            std::ofstream out(std::filesystem::path(options.capture_dir) /
                (std::to_string(tick) + ".rgba"), std::ios::binary);
            out.write(reinterpret_cast<const char*>(pixels.data()), static_cast<std::streamsize>(pixels.size()));
            if (!out) throw std::runtime_error("cannot write production capture");
        }
        WriteCaptureManifest(options, run);
    }
    return player.shutdown() == ExitCode::Success ? kExitOk : kExitInitializationFailed;
}
} // namespace
int RunSceneRenderer(const RendererOptions& options) {
    try {
        if (options.self_check) {
            std::puts("{\"format\":\"nw11-production-player\",\"status\":\"self_check_ok\"}");
            return kExitOk;
        }
        return RunProduction(options);
    } catch (const std::exception& error) {
        std::fprintf(stderr, "[NW11-PRODUCTION-ERR] %s\n", error.what());
        return kExitInitializationFailed;
    }
}
} // namespace nw11
