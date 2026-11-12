// NW-04 package-only compute + fullscreen-display fixture.
//
// The fixture passes one immutable `.nwp` v1 package to PackagePlayer, invokes
// initialization exactly once, then executes the package's ordered frame
// instructions at supplied ticks.  It never parses graphs, reflects shaders, or
// compiles shaders at runtime.

#include "package_player.h"
#include "package_loader.h"

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace {

struct FixtureArgs {
    const char* package_path = nullptr;
    std::uint32_t frames = 60u;
    std::uint64_t tick_start = 0u;
    std::uint64_t tick_step = 1u;
    std::uint32_t width = 1280u;
    std::uint32_t height = 720u;
    bool diagnostics = false;
    bool headless = false;
    bool allow_software_adapter = false;
    bool emit_benchmark_metrics = false;
    bool validation = false;
    bool resize_sequence = false;
    bool simulate_minimize = false;
    bool win32_minimize_restore = false;
    bool simulate_device_removal = false;
    bool simulate_allocation_failure = false;
    bool simulate_capability_failure = false;
    bool simulate_pipeline_failure = false;
    bool simulate_root_signature_failure = false;
    bool simulate_frame_allocation = false;
    bool simulate_present_failure = false;
    bool present_before_last_dispatch = false;
    bool planner_self_test = false;
    const char* planner_mutation = nullptr;
};

bool ParseU32(const char* text, std::uint32_t* value) {
    if (text == nullptr || value == nullptr) return false;
    char* end = nullptr;
    const unsigned long parsed = std::strtoul(text, &end, 10);
    if (end == text || *end != '\0' || parsed > 100000ul) return false;
    *value = static_cast<std::uint32_t>(parsed);
    return true;
}

bool ParseU64(const char* text, std::uint64_t* value) {
    if (text == nullptr || value == nullptr) return false;
    char* end = nullptr;
    const unsigned long long parsed = std::strtoull(text, &end, 10);
    if (end == text || *end != '\0') return false;
    *value = static_cast<std::uint64_t>(parsed);
    return true;
}

bool HasEnvironmentVariable(const char* name) {
#if defined(_WIN32)
    char* value = nullptr;
    std::size_t length = 0u;
    const int error = _dupenv_s(&value, &length, name);
    const bool present = error == 0 && value != nullptr;
    std::free(value);
    return present;
#else
    return std::getenv(name) != nullptr;
#endif
}

bool ParseArgs(int argc, char** argv, FixtureArgs* args) {
    for (int index = 1; index < argc; ++index) {
        const char* arg = argv[index];
        if (std::strcmp(arg, "--package") == 0) {
            if (++index >= argc) return false;
            args->package_path = argv[index];
        } else if (std::strcmp(arg, "--frames") == 0) {
            if (++index >= argc || !ParseU32(argv[index], &args->frames)) {
                return false;
            }
        } else if (std::strcmp(arg, "--tick-start") == 0) {
            if (++index >= argc || !ParseU64(argv[index], &args->tick_start)) {
                return false;
            }
        } else if (std::strcmp(arg, "--tick-step") == 0) {
            if (++index >= argc || !ParseU64(argv[index], &args->tick_step)) {
                return false;
            }
        } else if (std::strcmp(arg, "--width") == 0) {
            if (++index >= argc || !ParseU32(argv[index], &args->width)) {
                return false;
            }
        } else if (std::strcmp(arg, "--height") == 0) {
            if (++index >= argc || !ParseU32(argv[index], &args->height)) {
                return false;
            }
        } else if (std::strcmp(arg, "--diagnostics") == 0) {
            args->diagnostics = true;
        } else if (std::strcmp(arg, "--headless") == 0) {
            args->headless = true;
        } else if (std::strcmp(arg, "--allow-software-adapter") == 0) {
            args->allow_software_adapter = true;
        } else if (std::strcmp(arg, "--emit-benchmark-metrics") == 0) {
            args->emit_benchmark_metrics = true;
        } else if (std::strcmp(arg, "--validation") == 0) {
            args->validation = true;
        } else if (std::strcmp(arg, "--no-validation") == 0) {
            args->validation = false;
        } else if (std::strcmp(arg, "--resize-sequence") == 0) {
            args->resize_sequence = true;
        } else if (std::strcmp(arg, "--simulate-minimize") == 0) {
            args->simulate_minimize = true;
        } else if (std::strcmp(arg, "--win32-minimize-restore") == 0) {
            args->win32_minimize_restore = true;
        } else if (std::strcmp(arg, "--simulate-device-removal") == 0) {
            args->simulate_device_removal = true;
        } else if (std::strcmp(arg, "--simulate-allocation-failure") == 0) {
            args->simulate_allocation_failure = true;
        } else if (std::strcmp(arg, "--simulate-capability-failure") == 0) {
            args->simulate_capability_failure = true;
        } else if (std::strcmp(arg, "--simulate-pipeline-failure") == 0) {
            args->simulate_pipeline_failure = true;
        } else if (std::strcmp(arg, "--simulate-root-signature-failure") ==
                   0) {
            args->simulate_root_signature_failure = true;
        } else if (std::strcmp(arg, "--simulate-frame-allocation") == 0) {
            args->simulate_frame_allocation = true;
        } else if (std::strcmp(arg, "--simulate-present-failure") == 0) {
            args->simulate_present_failure = true;
        } else if (std::strcmp(arg, "--present-before-last-dispatch") == 0) {
            args->present_before_last_dispatch = true;
        } else if (std::strcmp(arg, "--planner-self-test") == 0) {
            args->planner_self_test = true;
        } else if (std::strcmp(arg, "--planner-mutation") == 0) {
            if (++index >= argc) return false;
            args->planner_mutation = argv[index];
        } else {
            return false;
        }
    }
    return args->package_path != nullptr;
}

void EmitFrameOrder(const nw::player::PackagePlayer& player) {
    std::printf("NW_PLAYER_FRAME_ORDER=");
    const auto& order = player.last_frame_instruction_order();
    for (std::size_t index = 0u; index < order.size(); ++index) {
        if (index != 0u) std::printf(",");
        std::printf("%s", order[index].c_str());
    }
    std::printf("\n");
}

} // namespace

int main(int argc, char** argv) {
    FixtureArgs args{};
    if (!ParseArgs(argc, argv, &args)) {
        std::fprintf(
            stderr,
            "usage: nw_package_player_fixture --package <file.nwp> "
            "[--frames N] [--tick-start N] [--tick-step N] [--width N] "
            "[--height N] [--diagnostics] [--headless] "
            "[--allow-software-adapter] "
            "[--emit-benchmark-metrics] [--resize-sequence] "
            "[--simulate-minimize] [--win32-minimize-restore] "
            "[--simulate-device-removal] "
            "[--simulate-allocation-failure] [--simulate-capability-failure] "
            "[--simulate-pipeline-failure] "
            "[--simulate-root-signature-failure] "
            "[--simulate-frame-allocation] "
            "[--simulate-present-failure] "
            "[--present-before-last-dispatch] "
            "[--planner-self-test] "
            "[--planner-mutation name] "
            "[--validation]\n");
        return static_cast<int>(nw::player::ExitCode::InvalidArguments);
    }

    nw::player::PlayerConfig config{};
    config.width = args.width;
    config.height = args.height;
    config.headless = args.headless;
    config.allow_software_adapter =
        args.allow_software_adapter ||
        HasEnvironmentVariable("NW04_ALLOW_SOFTWARE_ADAPTER");
    config.simulate_presentation_failure = args.simulate_present_failure;
    config.enable_validation = args.validation;
    config.emit_benchmark_metrics = args.emit_benchmark_metrics;
    config.wait_for_gpu_each_frame = args.emit_benchmark_metrics;
    config.frames_in_flight = 2u;

    nw::player::PackagePlayer player(config);
    if (args.simulate_capability_failure) {
        nw::player::QueriedAdapterCapabilities capabilities =
            nw::player::MakeBaselineQueriedCapabilities(
                nw::package::kMinWindowsBuild);
        capabilities.max_feature_level = 0xB000u;
        player.SetQueriedCapabilitiesForTesting(capabilities);
    }
    if (args.simulate_allocation_failure) {
        player.SetResourceAllocationQueryForTesting(
            [](const nw::package::ResourceDescriptor&, void*)
                -> nw::player::ResourceAllocationQuery {
                nw::player::ResourceAllocationQuery query{};
                query.detail = "simulated allocation-info failure";
                return query;
            },
            nullptr);
    }
    if (args.simulate_pipeline_failure) {
        player.SetForcePipelineCreationFailureForTesting(true);
    }
    if (args.simulate_root_signature_failure) {
        player.SetForceRootSignatureCreationFailureForTesting(true);
    }
    if (args.simulate_frame_allocation) {
        player.SetSimulatePostInitializationAllocationForTesting(true);
    }
    if (args.planner_self_test) {
        const nw::player::PackageLoadResult loaded =
            nw::player::LoadPackageFromFile(args.package_path);
        if (loaded.exit_code != nw::player::ExitCode::Success) {
            std::fprintf(stderr, "planner self-test package load failed\n");
            return static_cast<int>(loaded.exit_code);
        }
        nw::package::Package package = loaded.package;
        if (args.planner_mutation != nullptr) {
            bool mutated = false;
            if (std::strcmp(args.planner_mutation, "barrier-state") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.barrier" &&
                        instruction.operands.size() == 2u) {
                        instruction.operands[0].u32 = 99u;
                        mutated = true;
                        break;
                    }
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "barrier-usage") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.barrier" &&
                        instruction.operands.size() == 2u) {
                        instruction.operands[0].u32 = 4u;
                        instruction.operands[1].u32 = 1u;
                        mutated = true;
                        break;
                    }
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "descriptor-heap") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.set_descriptor_table" &&
                        instruction.operands.size() == 3u) {
                        instruction.operands[1].string_value =
                            "unexpected_heap";
                        mutated = true;
                        break;
                    }
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "descriptor-target") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.set_descriptor_table" &&
                        instruction.operands.size() == 3u) {
                        instruction.operands[2].u32 = 1u;
                        mutated = true;
                        break;
                    }
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "timeline-type") == 0) {
                if (!package.parameters.empty()) {
                    package.parameters.front().type = "double";
                    mutated = true;
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "root-constant-bound") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.set_root_constants" &&
                        instruction.operands.size() == 3u) {
                        instruction.operands[2].u32 = 5u;
                        mutated = true;
                        break;
                    }
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "copy-bounds") == 0) {
                package.frame.push_back(nw::package::Instruction{
                    "frame.copy_buffer",
                    {nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 1u, 0u, 0, false,
                         0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 2u, 0u, 0, false,
                         0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::U64, 0u, 1ull << 40, 0, false,
                         0.0, ""}}});
                mutated = true;
            } else if (std::strcmp(args.planner_mutation, "copy-self") == 0) {
                package.frame.push_back(nw::package::Instruction{
                    "frame.copy_buffer",
                    {nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 2u, 0u, 0, false,
                         0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 2u, 0u, 0, false,
                         0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::U64, 0u, 16u, 0, false, 0.0,
                         ""}}});
                mutated = true;
            } else if (std::strcmp(args.planner_mutation,
                                   "copy-texture") == 0) {
                nw::package::ResourceDescriptor texture{};
                texture.resource_id = 99u;
                texture.name = "planner_texture";
                texture.kind = "texture2d";
                texture.format = "DXGI_FORMAT_R8G8B8A8_UNORM";
                texture.usage = "uav";
                texture.pass_family = "compute.math_noise";
                texture.width = 4u;
                texture.height = 4u;
                texture.size_bytes = 64u;
                package.resources.push_back(texture);
                package.frame.push_back(nw::package::Instruction{
                    "frame.copy_buffer",
                    {nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 99u, 0u, 0,
                         false, 0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 2u, 0u, 0, false,
                         0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::U64, 0u, 16u, 0, false, 0.0,
                         ""}}});
                mutated = true;
            } else if (std::strcmp(args.planner_mutation,
                                   "copy-readback-source") == 0) {
                nw::package::ResourceDescriptor readback{};
                readback.resource_id = 98u;
                readback.name = "planner_readback";
                readback.kind = "buffer";
                readback.format = "DXGI_FORMAT_R32_UINT";
                readback.usage = "readback";
                readback.pass_family = "compute.math_noise";
                readback.size_bytes = 64u;
                package.resources.push_back(readback);
                package.frame.push_back(nw::package::Instruction{
                    "frame.copy_buffer",
                    {nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 98u, 0u, 0,
                         false, 0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::ResourceRef, 2u, 0u, 0, false,
                         0.0, ""},
                     nw::package::InstructionOperand{
                         nw::package::OperandType::U64, 0u, 16u, 0, false, 0.0,
                         ""}}});
                mutated = true;
            } else if (std::strcmp(args.planner_mutation,
                                   "present-target") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.present" &&
                        instruction.operands.size() == 1u) {
                        instruction.operands[0].u32 = 1u;
                        mutated = true;
                        break;
                    }
                }
            } else if (std::strcmp(args.planner_mutation,
                                   "dispatch-root") == 0) {
                for (auto& instruction : package.frame) {
                    if (instruction.opcode == "frame.dispatch" &&
                        instruction.operands.size() == 4u) {
                        instruction.operands[3].u32 = 99u;
                        mutated = true;
                        break;
                    }
                }
            }
            if (!mutated) {
                std::fprintf(stderr, "unknown planner mutation: %s\n",
                             args.planner_mutation);
                return static_cast<int>(
                    nw::player::ExitCode::InvalidArguments);
            }
        }
        if (!player.BuildFramePlanForTesting(package)) {
            std::printf("NW_PLAYER_PLANNER_REJECTED=1\n");
            std::fprintf(stderr, "planner self-test rejected the package: %s\n",
                         player.diagnostics_json().c_str());
            return player.Report(true);
        }
        for (std::uint32_t frame = 0u; frame < args.frames; ++frame) {
            const std::uint64_t tick = args.tick_start +
                                       static_cast<std::uint64_t>(frame) *
                                           args.tick_step;
            if (!player.ApplyFramePlanForTesting(tick)) {
                return player.Report(true);
            }
        }
        std::printf("NW_PLAYER_PLANNER_SELF_TEST=1\n");
        std::printf("NW_PLAYER_LAST_ROOT_CONSTANT_COUNT=%u\n",
                    player.last_root_constant_count());
        if (player.last_root_constant_count() > 0u) {
            std::printf("NW_PLAYER_LAST_ROOT_CONSTANT0=0x%08X\n",
                        player.last_root_constants()[0]);
        }
        std::printf("NW_PLAYER_LAST_TIMELINE_PARAMETER_ID=%u\n",
                    player.last_timeline_parameter_id());
        std::printf("NW_PLAYER_LAST_TIMELINE_VALUE=%llu\n",
                    static_cast<unsigned long long>(
                        player.last_timeline_value()));
        return 0;
    }
    nw::player::ExitCode code = nw::player::ExitCode::Success;
    if (args.present_before_last_dispatch) {
        // Native regression: a package dispatch must still observe its own
        // root signature/constants/descriptors after an intervening present.
        const nw::player::PackageLoadResult loaded =
            nw::player::LoadPackageFromFile(args.package_path);
        if (loaded.exit_code != nw::player::ExitCode::Success) {
            return static_cast<int>(loaded.exit_code);
        }
        nw::package::Package package = loaded.package;
        int present_index = -1;
        int last_dispatch_index = -1;
        for (std::size_t index = 0u; index < package.frame.size(); ++index) {
            if (package.frame[index].opcode == "frame.present") {
                present_index = static_cast<int>(index);
            } else if (package.frame[index].opcode == "frame.dispatch") {
                last_dispatch_index = static_cast<int>(index);
            }
        }
        if (present_index < 0 || last_dispatch_index < 0 ||
            last_dispatch_index < present_index) {
            std::fprintf(stderr,
                         "package cannot exercise present-before-dispatch\n");
            return static_cast<int>(
                nw::player::ExitCode::PackageValidationFailed);
        }
        const nw::package::Instruction present =
            package.frame[static_cast<std::size_t>(present_index)];
        package.frame.erase(package.frame.begin() + present_index);
        package.frame.insert(
            package.frame.begin() + last_dispatch_index, present);
        code = player.initialize(package);
    } else {
        code = player.initializeFromFile(args.package_path);
    }
    if (code != nw::player::ExitCode::Success) {
        std::printf("NW_PLAYER_PARTIAL_INITIALIZATION_CLEANUP=1\n");
        if (args.simulate_allocation_failure &&
            code == nw::player::ExitCode::ResourceAllocationFailed) {
            std::printf("NW_PLAYER_ALLOCATION_FAILURE_REJECTED=1\n");
        }
        if (args.simulate_capability_failure &&
            code == nw::player::ExitCode::CapabilityBaselineUnmet) {
            std::printf("NW_PLAYER_CAPABILITY_FAILURE_REJECTED=1\n");
        }
        if (args.simulate_pipeline_failure &&
            code == nw::player::ExitCode::PipelineStateCreationFailed) {
            std::printf("NW_PLAYER_PIPELINE_FAILURE_REJECTED=1\n");
        }
        if (args.simulate_root_signature_failure &&
            code == nw::player::ExitCode::RootSignatureCreationFailed) {
            std::printf("NW_PLAYER_ROOT_SIGNATURE_FAILURE_REJECTED=1\n");
        }
        return player.Report(true);
    }

    if (args.simulate_present_failure) {
        code = player.renderAtTick(args.tick_start);
        const auto& stats = player.allocation_stats();
        std::printf("NW_PLAYER_PRESENT_FAILURE_RETIRED=%d\n",
                    stats.present_failure_retirements > 0u ? 1 : 0);
        if (code != nw::player::ExitCode::PresentationFailed ||
            stats.present_failure_retirements == 0u) {
            std::fprintf(stderr,
                         "presentation failure did not preserve submitted-work "
                         "retirement\n");
            (void)player.shutdown();
            return player.Report(true);
        }
        const nw::player::ExitCode shutdown_code = player.shutdown();
        std::printf("NW_PLAYER_PRESENT_FAILURE_SHUTDOWN_CLEAN=%d\n",
                    shutdown_code == nw::player::ExitCode::Success ? 1 : 0);
        if (shutdown_code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
        return static_cast<int>(nw::player::ExitCode::PresentationFailed);
    }

    if (args.resize_sequence) {
        code = player.resize(args.width / 2u, args.height / 2u);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
        code = player.resize(args.width, args.height);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
    }
    if (args.simulate_minimize) {
        code = player.resize(0u, 0u);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
        // A minimized/zero-sized window must suspend presentation safely.
        code = player.renderAtTick(args.tick_start);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
        std::printf("NW_PLAYER_MINIMIZE_SUSPENDED=%d\n",
                    player.minimized() ? 1 : 0);
        code = player.resize(args.width, args.height);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
    }

    if (args.win32_minimize_restore) {
        // Exercise the real WM_SIZE/SW_MINIMIZE/SW_RESTORE transitions.  An
        // ordinary restore returns to the same client size, so this path must
        // resume rendering without requiring ResizeBuffers.
        code = player.renderAtTick(args.tick_start);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
        const std::uint64_t before_minimize = player.frames_rendered();
        if (!player.SetWindowMinimizedForTesting(true)) {
            std::fprintf(stderr, "could not simulate Win32 minimize\n");
            return static_cast<int>(nw::player::ExitCode::WindowCreationFailed);
        }
        code = player.renderAtTick(args.tick_start + 1u);
        if (code != nw::player::ExitCode::Success ||
            player.frames_rendered() != before_minimize) {
            std::fprintf(stderr, "minimized window did not suspend rendering\n");
            return player.Report(true);
        }
        if (!player.SetWindowMinimizedForTesting(false)) {
            std::fprintf(stderr, "could not simulate Win32 restore\n");
            return static_cast<int>(nw::player::ExitCode::WindowCreationFailed);
        }
        code = player.renderAtTick(args.tick_start + 2u);
        if (code != nw::player::ExitCode::Success ||
            player.frames_rendered() != before_minimize + 1u) {
            std::fprintf(stderr,
                         "restored window did not resume rendering at the "
                         "unchanged client size\n");
            return player.Report(true);
        }
        std::printf("NW_PLAYER_MINIMIZE_RESTORE_RENDERED=1\n");
    }

    if (args.simulate_device_removal) {
        player.SetForceDeviceRemovedForTesting(true);
        code = player.renderAtTick(args.tick_start);
        if (code != nw::player::ExitCode::DeviceRemoved) {
            std::fprintf(stderr,
                         "expected DeviceRemoved, observed exit code %d\n",
                         static_cast<int>(code));
            player.Report(true);
            (void)player.shutdown();
            return static_cast<int>(nw::player::ExitCode::DeviceRemoved);
        }
        (void)player.shutdown();
        std::printf("NW_PLAYER_DEVICE_REMOVAL_TERMINATED=1\n");
        return static_cast<int>(nw::player::ExitCode::DeviceRemoved);
    }

    for (std::uint32_t frame = 0u; frame < args.frames; ++frame) {
        const std::uint64_t tick =
            args.tick_start + static_cast<std::uint64_t>(frame) * args.tick_step;
        code = player.renderAtTick(tick);
        if (code != nw::player::ExitCode::Success) {
            return player.Report(true);
        }
    }

    const auto& stats = player.allocation_stats();
    std::printf("NW_PLAYER_PROFILE_ID=%s\n",
                player.package().capability.profile_id.c_str());
    std::printf("NW_PLAYER_REQUIREMENTS_DIGEST=%s\n",
                player.package().capability.requirements_digest.c_str());
    if (!player.package_sha256().empty()) {
        std::printf("NW_PLAYER_PACKAGE_SHA256=%s\n",
                    player.package_sha256().c_str());
    }
    std::printf("NW_PLAYER_INITIALIZATION_COUNT=%u\n",
                player.initialization_count());
    std::printf("NW_PLAYER_FRAMES=%llu\n",
                static_cast<unsigned long long>(player.frames_rendered()));
    std::printf("NW_PLAYER_PRESENTS=%llu\n",
                static_cast<unsigned long long>(player.frames_presented()));
    std::printf("NW_PLAYER_PRESENT_RESOURCE_ID=%u\n",
                player.present_resource_id());
    std::printf("NW_PLAYER_RESIZE_COUNT=%llu\n",
                static_cast<unsigned long long>(stats.resize_count));
    std::printf("NW_PLAYER_UPLOAD_REUSE_WAITS=%llu\n",
                static_cast<unsigned long long>(stats.upload_reuse_waits));
    std::printf("NW_PLAYER_UAV_BARRIERS=%llu\n",
                static_cast<unsigned long long>(stats.uav_barrier_count));
    std::printf("NW_PLAYER_LAST_TIMELINE_PARAMETER_ID=%u\n",
                player.last_timeline_parameter_id());
    std::printf("NW_PLAYER_LAST_TIMELINE_VALUE=%llu\n",
                static_cast<unsigned long long>(player.last_timeline_value()));
    std::printf("NW_PLAYER_LAST_ROOT_CONSTANT_COUNT=%u\n",
                player.last_root_constant_count());
    if (player.last_root_constant_count() > 0u) {
        std::printf("NW_PLAYER_LAST_ROOT_CONSTANT0=0x%08X\n",
                    player.last_root_constants()[0]);
    }
    std::printf("NW_PLAYER_NON_ALIASED_PLACEMENTS=%d\n",
                player.resource_plan().UsesOnlyNonAliasedPlacements() ? 1 : 0);
    std::printf("NW_PLAYER_ENGINE_RESOURCE_BYTES=%llu\n",
                static_cast<unsigned long long>(stats.engine_resource_bytes));
    std::printf("NW_PLAYER_ENGINE_CUMULATIVE_RESOURCE_BYTES=%llu\n",
                static_cast<unsigned long long>(
                    stats.engine_cumulative_resource_bytes));
    std::printf("NW_PLAYER_ENGINE_UPLOAD_BYTES=%llu\n",
                static_cast<unsigned long long>(stats.engine_upload_bytes));
    std::printf("NW_PLAYER_ENGINE_DESCRIPTOR_COUNT=%llu\n",
                static_cast<unsigned long long>(stats.engine_descriptor_count));
    std::printf("NW_PLAYER_POST_INIT_ALLOCATION_VIOLATION=%d\n",
                stats.post_initialization_allocation_violation ? 1 : 0);
    std::printf("NW_PLAYER_READBACK=not_performed\n");
    std::printf("NW_PLAYER_D3D12_VALIDATION_REQUESTED=%d\n",
                args.validation ? 1 : 0);
    if (!player.queried_capabilities().adapter_description.empty()) {
        std::printf("NW_PLAYER_ADAPTER_DESCRIPTION=%s\n",
                    player.queried_capabilities().adapter_description.c_str());
    }
    if (!player.queried_capabilities().adapter_generation.empty()) {
        std::printf("NW_PLAYER_ADAPTER_GENERATION=%s\n",
                    player.queried_capabilities().adapter_generation.c_str());
    }
    const auto& capabilities = player.queried_capabilities();
    std::printf("NW_PLAYER_ADAPTER_VENDOR_ID=%u\n", capabilities.vendor_id);
    std::printf("NW_PLAYER_ADAPTER_DEVICE_ID=%u\n", capabilities.device_id);
    std::printf("NW_PLAYER_DEDICATED_VIDEO_MEMORY_BYTES=%llu\n",
                static_cast<unsigned long long>(
                    capabilities.dedicated_video_memory_bytes));
    std::printf("NW_PLAYER_WINDOWS_BUILD=%u\n",
                capabilities.windows_build_number);
    std::printf("NW_PLAYER_MAX_FEATURE_LEVEL=0x%X\n",
                capabilities.max_feature_level);
    std::printf("NW_PLAYER_MAX_SHADER_MODEL=0x%X\n",
                capabilities.max_shader_model);
    std::printf("NW_PLAYER_RESOURCE_BINDING_TIER=%u\n",
                capabilities.resource_binding_tier);
    std::printf("NW_PLAYER_RESOURCE_HEAP_TIER=%u\n",
                capabilities.resource_heap_tier);
    std::printf("NW_PLAYER_MAX_CBV_SRV_UAV_DESCRIPTORS_PER_HEAP=%u\n",
                capabilities.max_cbv_srv_uav_descriptors_per_heap);
    std::printf("NW_PLAYER_MAX_SAMPLER_DESCRIPTORS_PER_HEAP=%u\n",
                capabilities.max_sampler_descriptors_per_heap);
    std::printf("NW_PLAYER_MAX_ROOT_SIGNATURE_DWORD_COST=%u\n",
                capabilities.max_root_signature_dword_cost);
    std::printf("NW_PLAYER_MAX_TEXTURE_2D_DIMENSION=%u\n",
                capabilities.max_texture_2d_dimension);
    std::printf("NW_PLAYER_MAX_COMPUTE_THREADS_PER_GROUP=%u\n",
                capabilities.max_compute_threads_per_group);
    std::printf("NW_PLAYER_MAX_BUFFER_BYTES=%llu\n",
                static_cast<unsigned long long>(capabilities.max_buffer_bytes));
    std::printf("NW_PLAYER_MAX_SWAP_CHAIN_BUFFERS=%u\n",
                capabilities.max_swap_chain_buffer_count);
    std::printf("NW_PLAYER_DRIVER_VERSION=%s\n",
                capabilities.driver_version.empty()
                    ? "unavailable"
                    : capabilities.driver_version.c_str());
    std::printf("NW_PLAYER_WDDM_VERSION=%s\n",
                capabilities.wddm_version_text.empty()
                    ? "unavailable"
                    : capabilities.wddm_version_text.c_str());
    std::printf("NW_PLAYER_TOOLCHAIN_IDENTITY=%s\n",
                player.package().capability.toolchain_identity.c_str());
    std::printf("NW_PLAYER_TOOLCHAIN_DXC_VERSION=%s\n",
                player.package().capability.toolchain_dxc_version.c_str());
    EmitFrameOrder(player);
    if (args.emit_benchmark_metrics) {
#if NW_PLAYER_DIAGNOSTICS
        const auto& timing = player.instrumentation().timing();
        if (timing.cpu_available) {
            std::printf("NW_PLAYER_CPU_SUBMIT_MS=%.3f\n", timing.cpu_submit_ms);
        }
        if (timing.gpu_available) {
            std::printf("NW_PLAYER_GPU_MS=%.3f\n", timing.gpu_ms);
        }
        const auto& memory = player.instrumentation().memory();
        std::printf("NW_PLAYER_ENGINE_ACCOUNTED_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        memory.engine_resource_bytes +
                        memory.engine_upload_bytes));
        std::printf("NW_PLAYER_ENGINE_CUMULATIVE_ACCOUNTED_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        memory.engine_cumulative_resource_bytes +
                        memory.engine_upload_bytes));
        std::printf("NW_PLAYER_DRIVER_PROCESS_PRIVATE_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        memory.process_private_bytes));
        std::printf("NW_PLAYER_DRIVER_GPU_LOCAL_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        memory.gpu_local_usage_bytes));
#else
        std::printf("NW_PLAYER_CPU_SUBMIT_MS_UNAVAILABLE=1\n");
        std::printf("NW_PLAYER_GPU_MS_UNAVAILABLE=1\n");
#endif
        std::printf("NW_PLAYER_ENGINE_COMMITTED_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        stats.engine_resource_bytes + stats.engine_upload_bytes));
        std::printf("NW_PLAYER_ENGINE_CUMULATIVE_COMMITTED_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        stats.engine_cumulative_resource_bytes +
                        stats.engine_upload_bytes));
        std::printf("NW_PLAYER_PROCESS_PRIVATE_BYTES=%llu\n",
                    static_cast<unsigned long long>(
                        nw::player::QueryProcessPrivateBytes()));
    }
    const nw::player::ExitCode shutdown_code = player.shutdown();
    if (shutdown_code != nw::player::ExitCode::Success) {
        return player.Report(true);
    }
    if (args.diagnostics) {
        std::printf("NW_PLAYER_DIAGNOSTICS=%s\n", player.diagnostics_json().c_str());
    }
    std::fflush(stdout);
    return 0;
}
