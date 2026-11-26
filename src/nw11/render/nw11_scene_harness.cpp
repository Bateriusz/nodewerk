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
#include <utility>
#include <vector>
#include "nw11_scene_harness.h"
#include "nw11_scene_generated.h"
#include "player/package_player.h"
#include "player/validation_exit_codes.h"
#include "player/embedded_package.h"

namespace nw11 {
extern std::uint64_t peak_engine_committed_gpu_bytes;
extern std::uint64_t first_present_filetime;
namespace {
using nw::player::ExitCode;
using Clock = std::chrono::steady_clock;
constexpr int kExitOk = 0;
constexpr int kExitUnsupported = 3;

using nw::validation::HarnessExitCode;

class RendererFailure final : public std::runtime_error {
public:
    RendererFailure(int exit_code, std::string code, std::string message)
        : std::runtime_error(std::move(message)),
          exit_code_(exit_code),
          code_(std::move(code)) {}

    int exit_code() const noexcept { return exit_code_; }
    const std::string& code() const noexcept { return code_; }

private:
    int exit_code_;
    std::string code_;
};

[[noreturn]] void FailHarness(HarnessExitCode code, const char* name,
                              const std::string& message) {
    throw RendererFailure(static_cast<int>(code), name, message);
}

[[noreturn]] void FailPlayer(ExitCode status, const char* stage) {
    throw RendererFailure(
        static_cast<int>(status), ExitCodeName(status),
        std::string(stage) + " failed with PackagePlayer " + ExitCodeName(status));
}
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
        FailHarness(HarnessExitCode::EvidenceIoFailure, "input_read_failed", "cannot open file: " + path);
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
        FailHarness(HarnessExitCode::MeasurementUnavailable, "process_time_unavailable", "cannot measure process creation time");
    return FiletimeTicks(created);
}

double QpcMilliseconds() {
    LARGE_INTEGER count{}, frequency{};
    if (!QueryPerformanceCounter(&count) || !QueryPerformanceFrequency(&frequency))
        FailHarness(HarnessExitCode::MeasurementUnavailable, "clock_unavailable", "cannot read presentation measurement clock");
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
        FailHarness(HarnessExitCode::ArtifactIdentityUnavailable, "executable_identity_unavailable", "cannot identify running executable");
    std::ifstream image(std::filesystem::path(module), std::ios::binary);
    const std::string image_bytes{std::istreambuf_iterator<char>(image), {}};
    if (image_bytes.empty()) FailHarness(HarnessExitCode::ArtifactIdentityUnavailable, "executable_hash_unavailable", "cannot hash running executable");
    return nw::package::Sha256Hex(image_bytes);
}

int RunProduction(const RendererOptions& options) {
    if (options.fixed_quality != "nw07.quality.v1") {
        FailHarness(HarnessExitCode::InvalidMeasurementContract,
                    "invalid_fixed_quality",
                    "unexpected fixed-quality profile: " + options.fixed_quality);
    }
    if (options.runs != 1 || options.run_seconds == 0) {
        FailHarness(HarnessExitCode::InvalidMeasurementContract,
                    "invalid_measurement_process_contract",
                    "each measurement requires one fresh process and a non-zero run duration");
    }
    if (!options.allow_software_adapter &&
        (options.adapter_luid.empty() || options.headless)) {
        FailHarness(HarnessExitCode::InvalidMeasurementContract,
                    "physical_adapter_or_presentation_missing",
                    "physical validation requires a probe-selected adapter and presentation");
    }
    if (!SetEnvironmentVariableA(
            "NW11_ADAPTER_LUID",
            options.adapter_luid.empty() ? nullptr : options.adapter_luid.c_str())) {
        FailHarness(HarnessExitCode::InvalidMeasurementContract,
                    "adapter_selection_environment_failed",
                    "cannot select the probe-qualified adapter");
    }

    nw::player::EmbeddedPackage embedded;
    nw::player::EmbeddedLinkMetadata metadata;
    if (!nw::player::LoadEmbeddedPackage(&embedded) ||
        !nw::player::ParseEmbeddedLinkMetadata(embedded, &metadata)) {
        FailHarness(HarnessExitCode::ArtifactIdentityUnavailable,
                    "embedded_production_package_invalid",
                    "missing or invalid embedded NW-07 production package");
    }

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
    config.borderless_window = !options.headless;
    config.memory_budget_bytes = metadata.memory_budget_bytes;
    config.query_optional_wave_ops = metadata.wave_ops;
    config.query_optional_fp16_shader_ops = metadata.fp16_shader_ops;
    config.query_optional_int64_shader_ops = metadata.int64_shader_ops;

    nw::player::PackagePlayer player(config);
    const ExitCode status = player.initialize(embedded.bytes, embedded.size);
    if (status != ExitCode::Success) {
        player.Report(true);
        if (status == ExitCode::OptionalFeatureUnsupported) {
            std::fprintf(
                stderr,
                "[NW11-PRODUCTION-ERR][process_exit_code=%d]"
                "[player_exit_code=%d][code=%s] PackagePlayer initialization "
                "reported an unsupported optional feature\n",
                kExitUnsupported, static_cast<int>(status), ExitCodeName(status));
            return kExitUnsupported;
        }
        std::fprintf(
            stderr,
            "[NW11-PRODUCTION-ERR][exit_code=%d][code=%s] "
            "PackagePlayer initialization failed\n",
            static_cast<int>(status), ExitCodeName(status));
        return static_cast<int>(status);
    }

    if (player.present_source_width() != scene::kOutputWidth ||
        player.present_source_height() != scene::kOutputHeight) {
        FailHarness(
            HarnessExitCode::PresentationSourceGeometryMismatch,
            "presentation_source_geometry_mismatch",
            "production source geometry expected " +
                std::to_string(scene::kOutputWidth) + "x" +
                std::to_string(scene::kOutputHeight) + " but PackagePlayer bound " +
                std::to_string(player.present_source_width()) + "x" +
                std::to_string(player.present_source_height()));
    }

    // Win32 may initially choose a different client size for an overlapped
    // 1920x1080 window (work-area constraints, DPI, placement).  NW-11's
    // matched physical benchmark requires an exact presentation client size,
    // so correct it explicitly through the existing PackagePlayer resize path
    // rather than conflating client geometry with package source geometry.
    if (!options.headless &&
        (player.width() != scene::kOutputWidth ||
         player.height() != scene::kOutputHeight)) {
        const ExitCode resize =
            player.resize(scene::kOutputWidth, scene::kOutputHeight);
        if (resize != ExitCode::Success) {
            player.Report(true);
            FailPlayer(resize, "presentation resize");
        }
        if (player.width() != scene::kOutputWidth ||
            player.height() != scene::kOutputHeight) {
            FailHarness(
                HarnessExitCode::PresentationClientGeometryMismatch,
                "presentation_client_geometry_mismatch",
                "presentation client expected " +
                    std::to_string(scene::kOutputWidth) + "x" +
                    std::to_string(scene::kOutputHeight) + " but resize produced " +
                    std::to_string(player.width()) + "x" +
                    std::to_string(player.height()));
        }
    }

    auto render = [&](std::uint64_t tick) {
        const ExitCode frame_status = player.renderAtTick(tick);
        if (frame_status != ExitCode::Success) {
            player.Report(true);
            FailPlayer(frame_status, "production frame execution");
        }
        if (player.window_closed()) {
            FailHarness(HarnessExitCode::PresentationWindowClosed,
                        "presentation_window_closed",
                        "production presentation window closed during the measured run");
        }
        if (!options.headless &&
            (player.width() != scene::kOutputWidth ||
             player.height() != scene::kOutputHeight)) {
            FailHarness(
                HarnessExitCode::PresentationClientGeometryMismatch,
                "presentation_client_geometry_changed",
                "presentation client changed from required " +
                    std::to_string(scene::kOutputWidth) + "x" +
                    std::to_string(scene::kOutputHeight) + " to " +
                    std::to_string(player.width()) + "x" +
                    std::to_string(player.height()));
        }
    };

    render(0);
    if (!options.headless && !player.frames_presented()) {
        FailHarness(HarnessExitCode::MeasurementUnavailable,
                    "first_present_unavailable",
                    "first production frame was not presented");
    }
    if (!options.headless &&
        first_present_filetime < run.process_creation_filetime) {
        FailHarness(HarnessExitCode::MeasurementUnavailable,
                    "first_present_timestamp_invalid",
                    "first presentation timestamp predates process creation");
    }

    const double startup_ms = options.headless
                                  ? 0.0
                                  : double(first_present_filetime -
                                           run.process_creation_filetime) /
                                        10000.0;
    run.warmup_start_qpc_ms = QpcMilliseconds();
    const auto origin = Clock::now();
    auto seconds = [&](auto t) {
        return std::chrono::duration<double>(t - origin).count();
    };
    auto tick_at = [&](auto t) {
        return static_cast<std::uint64_t>(
            std::fmod(seconds(t) * scene::kTicksPerSecond,
                      double(scene::kLoopTicks)));
    };

    while (seconds(Clock::now()) < options.warmup_seconds) {
        render(tick_at(Clock::now()));
    }

    std::vector<float> gpu, cpu;
    run.measurement_start_qpc_ms = QpcMilliseconds();
    const auto start = Clock::now();
    while (std::chrono::duration<double>(Clock::now() - start).count() <
           options.run_seconds) {
        const auto begin = Clock::now();
        render(tick_at(begin));
        const auto& timing = player.instrumentation().timing();
        if (!timing.cpu_available) {
            FailHarness(HarnessExitCode::MeasurementUnavailable,
                        "cpu_submission_timing_unavailable",
                        "production CPU submission timing unavailable");
        }
        if (options.allow_software_adapter) {
            if (!timing.gpu_available || timing.gpu_ms <= 0.0) {
                FailHarness(HarnessExitCode::MeasurementUnavailable,
                            "gpu_timing_unavailable",
                            "production GPU timing unavailable in WARP smoke");
            }
            gpu.push_back(static_cast<float>(timing.gpu_ms));
        }
        cpu.push_back(static_cast<float>(timing.cpu_submit_ms));
    }
    run.measurement_end_qpc_ms = QpcMilliseconds();

    if (cpu.empty() || peak_engine_committed_gpu_bytes == 0 ||
        player.allocation_stats().post_initialization_allocation_violation) {
        FailHarness(HarnessExitCode::MeasurementUnavailable,
                    "measurement_accounting_unavailable",
                    "missing samples or invalid engine allocation accounting");
    }

    WriteReport(options, player, gpu, cpu, run, startup_ms);
    if (!options.capture_dir.empty()) {
        std::filesystem::create_directories(options.capture_dir);
        for (auto tick : contract_reference_ticks()) {
            render(tick);
            std::vector<std::uint8_t> pixels;
            const ExitCode capture =
                options.capture_display
                    ? player.readbackDisplayOutput(&pixels)
                    : player.readbackPresentedOutput(&pixels);
            if (capture != ExitCode::Success) {
                player.Report(true);
                FailPlayer(capture, "production capture");
            }

            const auto pixel_count =
                std::uint64_t(scene::kOutputWidth) * scene::kOutputHeight;
            if (options.capture_display) {
                if (pixels.size() != pixel_count * 4u) {
                    FailHarness(
                        HarnessExitCode::CaptureValidationFailed,
                        "display_capture_size_mismatch",
                        "production display capture size changed");
                }
                std::vector<float> rgba;
                rgba.reserve(pixels.size());
                for (auto channel : pixels) {
                    rgba.push_back(float(channel) / 255.0f);
                }
                const auto* bytes =
                    reinterpret_cast<const std::uint8_t*>(rgba.data());
                pixels.assign(bytes,
                              bytes + rgba.size() * sizeof(float));
            }
            if (pixels.size() != pixel_count * 16u) {
                FailHarness(HarnessExitCode::CaptureValidationFailed,
                            "rgba32f_capture_size_mismatch",
                            "production RGBA32F capture size changed");
            }

            std::ofstream out(
                std::filesystem::path(options.capture_dir) /
                    (std::to_string(tick) + ".rgba"),
                std::ios::binary);
            out.write(reinterpret_cast<const char*>(pixels.data()),
                      static_cast<std::streamsize>(pixels.size()));
            if (!out) {
                FailHarness(HarnessExitCode::EvidenceIoFailure,
                            "capture_write_failed",
                            "cannot write production capture");
            }
        }
        WriteCaptureManifest(options, run);
    }

    const ExitCode shutdown = player.shutdown();
    if (shutdown != ExitCode::Success) {
        player.Report(true);
        std::fprintf(
            stderr,
            "[NW11-PRODUCTION-ERR][exit_code=%d][code=%s] "
            "PackagePlayer shutdown failed\n",
            static_cast<int>(shutdown), ExitCodeName(shutdown));
        return static_cast<int>(shutdown);
    }
    return kExitOk;
}
} // namespace
int RunSceneRenderer(const RendererOptions& options) {
    try {
        if (options.self_check) {
            std::puts("{\"format\":\"nw11-production-player\",\"status\":\"self_check_ok\"}");
            return kExitOk;
        }
        return RunProduction(options);
    } catch (const RendererFailure& error) {
        std::fprintf(stderr,
                     "[NW11-PRODUCTION-ERR][exit_code=%d][code=%s] %s\n",
                     error.exit_code(), error.code().c_str(), error.what());
        return error.exit_code();
    } catch (const std::exception& error) {
        const int code = static_cast<int>(HarnessExitCode::UnhandledFailure);
        std::fprintf(stderr,
                     "[NW11-PRODUCTION-ERR][exit_code=%d]"
                     "[code=unhandled_harness_failure] %s\n",
                     code, error.what());
        return code;
    }
}
} // namespace nw11
