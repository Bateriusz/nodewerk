#pragma once

// Minimal package-driven Windows 11 x64 D3D12 player.
//
// Public lifetime:
//   initialize(package) -> renderAtTick(tick)* -> resize(...)? -> shutdown()
//
// Initialization instructions execute exactly once.  Frame instructions execute
// in their emitted order on every renderAtTick call.  The player owns a small
// bounded set of frames in flight; after initialization, rendering performs no
// engine-owned heap or descriptor allocation except explicit resize/recreation
// paths.

#include "capability_requirements.h"
#include "exit_codes.h"
#include "package/nwp_format.h"
#include "player_instrumentation.h"
#include "resource_plan.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace nw::player {

struct PlayerConfig {
    const char* window_title = "Nodewerk NW-04 Player";
    std::uint32_t width = 1280u;
    std::uint32_t height = 720u;
    // Small bounded ring.  The implementation clamps this to 1..3.
    std::uint32_t frames_in_flight = 2u;
    bool create_window = true;
    bool visible_window = true;
    bool enable_validation = false;
    bool headless = false;
    // Diagnostic/CI only: allow the Microsoft Basic Render Driver (WARP)
    // adapter for native lifecycle and D3D12-validation execution.  Hardware
    // qualification never sets this flag.
    bool allow_software_adapter = false;
    // Diagnostic seam: force IDXGISwapChain3::Present to fail after the frame
    // command list has been submitted, exercising retirement-fence handling.
    bool simulate_presentation_failure = false;
    bool emit_benchmark_metrics = false;
    bool emit_readback_values = false;
    // Diagnostic/qualification seam: force the established non-aliased
    // committed realization even when the package has a proven alias plan.
    bool force_non_aliased_plan = false;
    bool wait_for_gpu_each_frame = false;
    // Generated manifests enable only the optional feature queries the linked
    // artifact actually requires.  The common artifact leaves these false so
    // unused OPTIONS1/OPTIONS4 queries are not part of its linked closure.
    bool query_optional_wave_ops = false;
    bool query_optional_int64_shader_ops = false;
    bool query_optional_fp16_shader_ops = false;
    // Zero means no explicit preflight memory budget.  A non-zero value is
    // checked after native allocation requirements are queried and before any
    // bulk resource creation.
    std::uint64_t memory_budget_bytes = 0u;
};

struct PlayerDiagnostic {
    std::string category;
    std::string code;
    std::string requirement;
    std::string required_value;
    std::string observed_value;
    std::string profile_id;
    std::string requirements_digest;
    std::string stage;
    std::string message;
};

struct PlayerAllocationStats {
    std::uint64_t engine_resource_bytes = 0u;
    // Cumulative bytes ever allocated for engine resources.  This intentionally
    // differs from engine_resource_bytes, which tracks live allocations and is
    // reduced when a resize/recreation path releases the old display texture.
    std::uint64_t engine_cumulative_resource_bytes = 0u;
    std::uint64_t engine_upload_bytes = 0u;
    std::uint64_t engine_descriptor_count = 0u;
    std::uint64_t engine_heap_count = 0u;
    std::uint64_t package_plan_bytes = 0u;
    std::uint64_t allocation_calls = 0u;
    std::uint64_t descriptor_allocation_calls = 0u;
    std::uint64_t resize_count = 0u;
    std::uint64_t upload_reuse_waits = 0u;
    std::uint64_t frames_rendered = 0u;
    std::uint64_t frames_presented = 0u;
    std::uint64_t uav_barrier_count = 0u;
    std::uint64_t alias_barrier_count = 0u;
    std::uint64_t present_failure_retirements = 0u;
    bool post_initialization_allocation_violation = false;
};

struct PlatformState;

class PackagePlayer {
public:
    PackagePlayer();
    explicit PackagePlayer(const PlayerConfig& config);
    ~PackagePlayer();

    PackagePlayer(const PackagePlayer&) = delete;
    PackagePlayer& operator=(const PackagePlayer&) = delete;

    // Primary NW-04 interfaces.
    ExitCode initialize(const package::Package& package);
    ExitCode initialize(const std::uint8_t* package_bytes,
                        std::size_t size_bytes);
    ExitCode initializeFromFile(const char* package_path);
    ExitCode renderAtTick(std::uint64_t tick);
    // Reads back the exact resource target of frame.present.  Diagnostic/
    // qualification only; returns the bytes written by the package shader.
    ExitCode readbackPresentedOutput(std::vector<std::uint8_t>* bytes);
    ExitCode resize(std::uint32_t width, std::uint32_t height);
    ExitCode shutdown();

    // Uppercase aliases keep the C++ surface easy to discover next to the
    // lower-case contract names above.
    ExitCode Initialize(const package::Package& pkg) {
        return initialize(pkg);
    }
    ExitCode RenderAtTick(std::uint64_t tick) { return renderAtTick(tick); }
    ExitCode Resize(std::uint32_t width, std::uint32_t height) {
        return resize(width, height);
    }
    ExitCode Shutdown() { return shutdown(); }

    bool initialized() const noexcept { return initialized_; }
    std::uint32_t initialization_count() const noexcept {
        return initialization_count_;
    }
    std::uint64_t shutdown_count() const noexcept { return shutdown_count_; }
    std::uint64_t frames_rendered() const noexcept { return frames_rendered_; }
    std::uint64_t last_tick() const noexcept { return last_tick_; }
    std::uint32_t last_timeline_parameter_id() const noexcept {
        return timeline_parameter_id_;
    }
    std::uint64_t last_timeline_value() const noexcept {
        return timeline_tick_;
    }
    std::uint32_t present_resource_id() const noexcept {
        return present_resource_id_;
    }
    std::uint64_t frames_presented() const noexcept {
        return frames_presented_;
    }
    const std::array<std::uint32_t,
                     package::kMaxRootSignatureDwordCost>&
    last_root_constants() const noexcept {
        return last_root_constants_;
    }
    std::uint32_t last_root_constant_count() const noexcept {
        return last_root_constant_count_;
    }
    std::uint32_t width() const noexcept { return width_; }
    std::uint32_t height() const noexcept { return height_; }
    bool minimized() const noexcept { return minimized_; }
    bool window_closed() const noexcept { return window_closed_; }
    bool using_aliased_plan() const noexcept { return using_aliased_plan_; }
    bool validation_enabled() const noexcept {
        return config_.enable_validation;
    }

    const package::Package& package() const noexcept { return package_; }
    const ResourcePlan& resource_plan() const noexcept { return plan_; }
    const QueriedAdapterCapabilities& queried_capabilities() const noexcept {
        return capabilities_;
    }
    const PlayerAllocationStats& allocation_stats() const noexcept {
        return stats_;
    }
    const std::vector<PlayerDiagnostic>& diagnostics() const noexcept {
        return diagnostics_;
    }
    ExitCode last_exit_code() const noexcept { return last_exit_; }
    const std::vector<std::string>& last_frame_instruction_order() const noexcept {
        return last_frame_order_;
    }
    const std::string& package_sha256() const noexcept {
        return package_sha256_;
    }

    std::string diagnostics_json() const;
    int Report(bool force_diagnostic_output = false) const noexcept;

    // Explicit test seams.  They never change runtime eligibility branches;
    // they replace the queried snapshot or allocator with a deterministic
    // host-side implementation.
    void SetQueriedCapabilitiesForTesting(
        const QueriedAdapterCapabilities& capabilities);
    void SetResourceAllocationQueryForTesting(
        ResourceAllocationQueryFn query, void* user_data);
    void SetForceDeviceRemovedForTesting(bool removed) noexcept;
    void SetForceRootSignatureCreationFailureForTesting(bool fail) noexcept;
    void SetForcePipelineCreationFailureForTesting(bool fail) noexcept;
    // Diagnostic-only seam that injects one tracked engine allocation into the
    // render path so the post-initialization allocation guard is exercised.
    void SetSimulatePostInitializationAllocationForTesting(bool allocate) noexcept;
    // Test seam that drives the real Win32 WM_SIZE/SW_MINIMIZE/SW_RESTORE
    // transitions on Windows; host simulation simply flips the tracked state.
    bool SetWindowMinimizedForTesting(bool minimized) noexcept;
    // Host-simulation planner checks.  They exercise the exact frame-plan
    // validation and timeline constant application without a GPU.
    bool BuildFramePlanForTesting(const package::Package& pkg);
    bool ApplyFramePlanForTesting(std::uint64_t tick);

    std::uint64_t upload_reuse_waits() const noexcept {
        return stats_.upload_reuse_waits;
    }

#if NW_PLAYER_DIAGNOSTICS
    void SetCaptureHook(CaptureHook hook, void* user_data) noexcept;
    const PlayerInstrumentation& instrumentation() const noexcept {
        return instrumentation_;
    }
#endif

private:
    ExitCode Fail(ExitCode code,
                  const char* category,
                  const char* diagnostic_code,
                  const char* requirement,
                  const std::string& required_value,
                  const std::string& observed_value,
                  const char* stage,
                  const std::string& message);
    ExitCode FailEvaluation(const PackageCapabilityEvaluation& evaluation,
                            const char* stage);
    ExitCode FailPlan(const ResourcePlanResult& plan_result);
    void AddFailureDiagnostics(
        const std::vector<CapabilityRequirementFailure>& failures,
        const char* category,
        const char* stage);
    void NoteEngineAllocation(std::uint64_t bytes,
                              std::uint64_t bytes_upload,
                              bool descriptor,
                              bool heap,
                              bool explicit_path);
    void RemoveEngineResourceAllocation(std::uint64_t bytes) noexcept;
    bool BuildFrameInstructionPlan();
    ExitCode RunInitializationInstructions();
    ExitCode ExecuteFrameInstructions(std::uint64_t tick);
    ExitCode OpenDeviceAndWindow();
    ExitCode CreateSwapChainAndFrames();
    ExitCode RecreateRenderTargets();
    void ReleasePlatform() noexcept;
    ExitCode WaitForAllFrames(const char* stage);
    bool CheckDeviceRemoved(const char* stage);
    bool CheckD3D12Validation(const char* stage);
    ExitCode CheckMemoryBudget(const char* stage);

    PlayerConfig config_{};
    ExitCode last_exit_ = ExitCode::Success;
    std::vector<PlayerDiagnostic> diagnostics_;
    package::Package package_{};
    std::string package_sha256_;
    QueriedAdapterCapabilities capabilities_{};
    bool capabilities_overridden_ = false;
    ResourcePlan plan_{};
    PlayerAllocationStats stats_{};
    std::uint32_t frames_in_flight_ = 2u;
    std::uint32_t initialization_count_ = 0u;
    std::uint64_t shutdown_count_ = 0u;
    std::uint64_t frames_rendered_ = 0u;
    std::uint64_t frames_presented_ = 0u;
    std::uint64_t last_tick_ = 0u;
    std::uint64_t next_fence_value_ = 1u;
    std::uint32_t width_ = 1280u;
    std::uint32_t height_ = 720u;
    bool initialized_ = false;
    bool minimized_ = false;
    bool suspended_ = false;
    bool window_closed_ = false;
    bool resize_in_progress_ = false;
    bool shutting_down_ = false;
    bool force_device_removed_ = false;
    bool force_root_signature_creation_failure_ = false;
    bool force_pipeline_creation_failure_ = false;
    bool simulate_post_initialization_allocation_ = false;
    bool using_aliased_plan_ = false;
    std::uint32_t timeline_parameter_id_ = 0u;
    std::uint64_t timeline_tick_ = 0u;
    bool timeline_override_active_ = false;
    // Resolved from the validated frame.present operand during planning.
    std::uint32_t present_resource_id_ = 0u;
    std::uint32_t present_source_width_ = 0u;
    std::uint32_t present_source_height_ = 0u;
    std::array<std::uint32_t, package::kMaxRootSignatureDwordCost>
        last_root_constants_{};
    std::uint32_t last_root_constant_count_ = 0u;

    ResourceAllocationQueryFn allocation_query_ = nullptr;
    void* allocation_user_data_ = nullptr;

    struct FrameOp {
        enum class Type : std::uint8_t {
            SetRootSignature,
            SetRootConstants,
            SetDescriptorTable,
            Barrier,
            Dispatch,
            Draw,
            CopyBuffer,
            SetTimeline,
            Present,
        };

        Type type = Type::Present;
        std::string opcode;
        std::uint32_t root_parameter = 0u;
        std::uint32_t resource_a = 0u;
        std::uint32_t resource_b = 0u;
        // Resource currently bound through the active descriptor table.  Used
        // to place alias-activation barriers immediately before the dispatch
        // that first uses the incoming resource.
        std::uint32_t descriptor_resource = 0u;
        std::uint32_t shader_id = 0u;
        std::uint32_t group_x = 1u;
        std::uint32_t group_y = 1u;
        std::uint32_t group_z = 1u;
        std::uint32_t parameter_id = 0u;
        std::uint32_t barrier_state = 0u;
        std::uint32_t constant_count = 0u;
        std::int32_t time_byte_offset = -1;
        std::uint64_t byte_count = 0u;
        std::uint64_t tick_value = 0u;
        std::string heap_name;
        std::array<std::uint32_t, package::kMaxRootSignatureDwordCost>
            constants{};
        // Per-parameter placement inside this root-constant block.  Populated
        // once while the frame plan is built so frame.set_timeline can update
        // the selected parameter without allocating or re-planning.
        std::array<std::uint32_t, package::kMaxRootSignatureDwordCost>
            parameter_ids{};
        std::array<std::int32_t, package::kMaxRootSignatureDwordCost>
            parameter_offsets{};
        std::array<std::uint8_t, package::kMaxRootSignatureDwordCost>
            parameter_tick_kinds{};
        std::array<std::uint32_t, package::kMaxRootSignatureDwordCost>
            parameter_sizes{};
        std::uint32_t parameter_count = 0u;
        std::uint32_t instruction_index = 0u;
    };
    std::vector<FrameOp> frame_ops_;
    std::vector<std::string> last_frame_order_;
    std::unique_ptr<PlatformState> platform_;

#if NW_PLAYER_DIAGNOSTICS
    PlayerInstrumentation instrumentation_{};
#endif
};

} // namespace nw::player
