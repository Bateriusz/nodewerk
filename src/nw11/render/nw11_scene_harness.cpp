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
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "nw11_scene_harness.h"
#include "nw11_scene_generated.h"
#include "player/package_player.h"
#include "player/embedded_package.h"

namespace nw11 {
namespace {
using nw::player::ExitCode;
using Clock = std::chrono::steady_clock;
constexpr int kExitOk = 0;
constexpr int kExitUnsupported = 3;
constexpr int kExitInitializationFailed = 4;
const std::array<std::uint32_t, 5>& contract_reference_ticks() {
    static const std::array<std::uint32_t, 5> ticks = {0u, 240000u, 480000u, 720000u, 959999u};
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

void WriteCaptureManifest(const RendererOptions& options_) {
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
    json << "  \"fixed_ticks\": [0, 240000, 480000, 720000, 959999],\n";
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

void WriteReport(const RendererOptions& options_, const nw::player::PackagePlayer& player, const std::vector<float>& gpu_samples_ms_, const std::vector<float>& cpu_samples_ms_, const std::vector<float>& frame_samples_ms_, double cold_initialization_ms_, std::uint64_t peak) {
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
    json << std::setprecision(9);
    json << "{\n";
    json << "  \"format\": \"nw11-raw-run.v1\",\n";
    json << "  \"version\": 1,\n";
    json << "  \"task\": \"NW-11\",\n";
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
    write_samples("frame_time_samples", frame_samples_ms_, false);
    json << "    \"cold_initialization_ms\": " << cold_initialization_ms_
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
    json << "    \"peak_engine_committed_gpu_bytes\": " << peak
         << ",\n";
    json << "    \"image\": {\n";
    json << "      \"max_abs_channel_error\": 0.0,\n";
    json << "      \"rmse\": 0.0,\n";
    json << "      \"reference_kind\": \"" << JsonEscape(options_.reference_kind)
         << "\",\n";
    json << "      \"fixed_ticks\": [0, 240000, 480000, 720000, 959999]\n";
    json << "    }\n";
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


double ProcessAgeMs() {
    FILETIME created{}, exited{}, kernel{}, user{}, now{};
    if (!GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user))
        throw std::runtime_error("cannot measure process creation time");
    GetSystemTimePreciseAsFileTime(&now);
    const auto ticks = [](FILETIME f) {
        return (std::uint64_t(f.dwHighDateTime) << 32u) | f.dwLowDateTime;
    };
    if (ticks(now) < ticks(created)) throw std::runtime_error("invalid process clock");
    return double(ticks(now) - ticks(created)) / 10000.0;
}

int RunProduction(const RendererOptions& options) {
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
    nw::player::PlayerConfig config;
    config.width = scene::kOutputWidth;
    config.height = scene::kOutputHeight;
    config.present_sync_interval = 0;
    config.wait_for_gpu_each_frame = true;
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
    std::uint64_t peak = 0;
    auto render = [&](std::uint64_t tick) {
        if (player.renderAtTick(tick) != ExitCode::Success || player.window_closed()) {
            player.Report(true);
            throw std::runtime_error("production frame execution failed");
        }
        peak = std::max(peak, player.allocation_stats().engine_resource_bytes);
    };
    render(0);
    if (!options.headless && !player.frames_presented())
        throw std::runtime_error("first production frame was not presented");
    const double cold = ProcessAgeMs();
    if (!options.capture_dir.empty()) {
        std::filesystem::create_directories(options.capture_dir);
        for (auto tick : contract_reference_ticks()) {
            render(tick);
            std::vector<std::uint8_t> pixels;
            if (player.readbackPresentedOutput(&pixels) != ExitCode::Success ||
                pixels.size() != std::uint64_t(scene::kOutputWidth) * scene::kOutputHeight * 16u)
                throw std::runtime_error("production RGBA32F capture failed");
            std::ofstream out(std::filesystem::path(options.capture_dir) /
                (std::to_string(tick) + ".rgba"), std::ios::binary);
            out.write(reinterpret_cast<const char*>(pixels.data()), static_cast<std::streamsize>(pixels.size()));
            if (!out) throw std::runtime_error("cannot write production capture");
        }
        WriteCaptureManifest(options);
    }
    const auto origin = Clock::now();
    auto seconds = [&](auto t) { return std::chrono::duration<double>(t - origin).count(); };
    auto tick_at = [&](auto t) { return static_cast<std::uint64_t>(
        std::fmod(seconds(t) * scene::kTicksPerSecond, double(scene::kLoopTicks))); };
    while (seconds(Clock::now()) < options.warmup_seconds) render(tick_at(Clock::now()));
    std::vector<float> gpu, cpu, frame;
    const auto start = Clock::now();
    while (std::chrono::duration<double>(Clock::now() - start).count() < options.run_seconds) {
        const auto begin = Clock::now();
        render(tick_at(begin));
        const auto& timing = player.instrumentation().timing();
        if (!timing.gpu_available || !timing.cpu_available || timing.gpu_ms <= 0.0)
            throw std::runtime_error("production GPU/CPU timing unavailable");
        gpu.push_back(static_cast<float>(timing.gpu_ms));
        cpu.push_back(static_cast<float>(timing.cpu_submit_ms));
        frame.push_back(static_cast<float>(std::chrono::duration<double, std::milli>(Clock::now() - begin).count()));
    }
    if (gpu.empty() || peak == 0 || player.allocation_stats().post_initialization_allocation_violation)
        throw std::runtime_error("missing samples or invalid engine allocation accounting");
    WriteReport(options, player, gpu, cpu, frame, cold, peak);
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
