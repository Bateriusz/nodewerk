#!/usr/bin/env python3
"""Generate the NW-08 authoring-only PackagePlayer overlay.

The NW-07 release PackagePlayer sources are pinned inputs and must remain
byte-for-byte unchanged.  NW-08 needs an authoring hot-reload seam, so this
script deterministically derives a private build copy from those frozen inputs.
Every replacement is exact and fail-closed: if the frozen source shape changes,
configuration stops instead of silently producing a different runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one source marker, found {count}")
    return text.replace(old, new)


def generate(repo_root: Path, output_dir: Path) -> None:
    source_dir = repo_root / "src" / "player"
    header = (source_dir / "package_player.h").read_text(encoding="utf-8")
    cpp = (source_dir / "package_player.cpp").read_text(encoding="utf-8")

    header = replace_once(
        header,
        "#pragma once\n",
        """#pragma once

#ifndef NW08_AUTHORING_RUNTIME
#define NW08_AUTHORING_RUNTIME 0
#endif
""",
        "header macro",
    )
    header = replace_once(
        header,
        "struct PlatformState;\n",
        r"""struct PlatformState;

#if NW08_AUTHORING_RUNTIME
struct AuthoringAdapterIdentity {
    std::uint32_t vendor_id = 0u;
    std::uint32_t device_id = 0u;
    std::uint32_t subsystem_id = 0u;
    std::uint32_t luid_low = 0u;
    std::int32_t luid_high = 0;
    bool available = false;
    bool software = false;
};

struct AuthoringReloadResult {
    ExitCode exit_code = ExitCode::Success;
    bool requires_device_recreation = false;
    std::uint32_t reused_resources = 0u;
    std::uint32_t recreated_resources = 0u;
    std::uint32_t reused_root_signatures = 0u;
    std::uint32_t recreated_root_signatures = 0u;
    std::uint32_t reused_pipelines = 0u;
    std::uint32_t recreated_pipelines = 0u;
};

struct AuthoringPassTiming {
    std::string name;
    double milliseconds = 0.0;
};
#endif
""",
        "authoring declarations",
    )
    header = replace_once(
        header,
        "    ExitCode shutdown();\n",
        r"""    ExitCode shutdown();
#if NW08_AUTHORING_RUNTIME
    AuthoringReloadResult reloadForAuthoringFromFile(const char* package_path);
    AuthoringAdapterIdentity authoringAdapterIdentity() const noexcept;
    const AuthoringReloadResult& authoring_reload_stats() const noexcept {
        return authoring_reload_stats_;
    }
    const std::vector<AuthoringPassTiming>& authoring_pass_timings() const noexcept {
        return authoring_pass_timings_;
    }
#endif
""",
        "authoring public methods",
    )
    header = replace_once(
        header,
        "    ExitCode CheckMemoryBudget(const char* stage);\n",
        r"""    ExitCode CheckMemoryBudget(const char* stage);
#if NW08_AUTHORING_RUNTIME
    bool IsAuthoringRetainedResource(std::uint32_t resource_id) const noexcept;
#endif
""",
        "authoring private helper",
    )
    header = replace_once(
        header,
        "    std::vector<std::string> last_frame_order_;\n",
        r"""    std::vector<std::string> last_frame_order_;
#if NW08_AUTHORING_RUNTIME
    std::vector<std::uint32_t> authoring_retained_resource_ids_;
    std::vector<AuthoringPassTiming> authoring_pass_timings_;
    AuthoringReloadResult authoring_reload_stats_{};
    bool authoring_reload_in_progress_ = false;
#endif
""",
        "authoring private state",
    )

    cpp = replace_once(
        cpp,
        "#include <cmath>\n",
        "#include <chrono>\n#include <cmath>\n",
        "authoring timing chrono include",
    )

    methods = r"""
#if NW08_AUTHORING_RUNTIME
bool PackagePlayer::IsAuthoringRetainedResource(
    std::uint32_t resource_id) const noexcept {
    return std::find(authoring_retained_resource_ids_.begin(),
                     authoring_retained_resource_ids_.end(),
                     resource_id) != authoring_retained_resource_ids_.end();
}

AuthoringAdapterIdentity PackagePlayer::authoringAdapterIdentity() const noexcept {
    AuthoringAdapterIdentity identity{};
#if defined(_WIN32)
    const PlatformState* state = platform_.get();
    if (state == nullptr || state->adapter == nullptr) {
        return identity;
    }
    DXGI_ADAPTER_DESC1 desc{};
    if (FAILED(state->adapter->GetDesc1(&desc))) {
        return identity;
    }
    identity.vendor_id = desc.VendorId;
    identity.device_id = desc.DeviceId;
    identity.subsystem_id = desc.SubSysId;
    identity.luid_low = desc.AdapterLuid.LowPart;
    identity.luid_high = desc.AdapterLuid.HighPart;
    identity.available = true;
    identity.software = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0u;
#endif
    return identity;
}

AuthoringReloadResult PackagePlayer::reloadForAuthoringFromFile(
    const char* package_path) {
    AuthoringReloadResult result{};
    auto finish = [&](ExitCode code, bool recreate) {
        result.exit_code = code;
        result.requires_device_recreation = recreate;
        authoring_reload_stats_ = result;
        authoring_retained_resource_ids_.clear();
        authoring_reload_in_progress_ = false;
        return result;
    };
    if (package_path == nullptr || *package_path == '\0' || !initialized_ ||
        platform_ == nullptr) {
        return finish(ExitCode::InvalidArguments, true);
    }

    const PackageLoadResult loaded = LoadPackageFromFile(package_path);
    if (loaded.exit_code != ExitCode::Success) {
        return finish(loaded.exit_code, false);
    }

    auto resource_equal = [](const package::ResourceDescriptor& a,
                             const package::ResourceDescriptor& b) {
        return a.resource_id == b.resource_id && a.name == b.name &&
               a.kind == b.kind && a.format == b.format &&
               a.usage == b.usage && a.pass_family == b.pass_family &&
               a.binding_slot == b.binding_slot &&
               a.binding_space == b.binding_space && a.width == b.width &&
               a.height == b.height && a.depth == b.depth &&
               a.array_size == b.array_size && a.mip_levels == b.mip_levels &&
               a.size_bytes == b.size_bytes && a.flags == b.flags;
    };
    if (package_.resources.size() != loaded.package.resources.size()) {
        return finish(ExitCode::Success, true);
    }
    for (std::size_t index = 0u; index < package_.resources.size(); ++index) {
        if (!resource_equal(package_.resources[index],
                            loaded.package.resources[index])) {
            return finish(ExitCode::Success, true);
        }
    }

    const ExitCode wait = WaitForAllFrames("authoring_reload");
    if (wait != ExitCode::Success) {
        return finish(wait, true);
    }

    const PackageCapabilityEvaluation capability =
        EvaluatePackageCapabilities(loaded.package, capabilities_);
    if (!capability.ok()) {
        return finish(capability.exit_code, false);
    }

    const ResourceAllocationQueryFn query =
#if defined(_WIN32)
        allocation_query_ != nullptr ? allocation_query_ : QueryAdapterAllocationInfo;
    void* const query_user_data =
        allocation_query_ != nullptr ? allocation_user_data_ : platform_.get();
#else
        allocation_query_ != nullptr ? allocation_query_ : SimulateResourceAllocation;
    void* const query_user_data =
        allocation_query_ != nullptr ? allocation_user_data_ : nullptr;
#endif
    ResourcePlanResult plan_result =
        BuildNonAliasedResourcePlan(loaded.package, query, query_user_data);
    if (plan_result.exit_code != ExitCode::Success) {
        return finish(plan_result.exit_code, false);
    }

    const package::Package old_package = package_;
    const ResourcePlan old_plan = plan_;
    PlatformState* state = platform_.get();
    const bool capability_same =
        old_package.capability.profile_id == loaded.package.capability.profile_id &&
        old_package.capability.requirements_digest ==
            loaded.package.capability.requirements_digest;

    auto parameter_equal = [](const package::ParameterRecord& a,
                              const package::ParameterRecord& b) {
        return a.parameter_id == b.parameter_id && a.name == b.name &&
               a.type == b.type && a.phase == b.phase &&
               a.component_count == b.component_count && a.value == b.value &&
               a.timeline_start_frame == b.timeline_start_frame &&
               a.timeline_end_frame == b.timeline_end_frame &&
               a.timeline_step == b.timeline_step &&
               a.timeline_interpolation == b.timeline_interpolation;
    };
    auto all_parameters_equal = [&](const package::Package& a,
                                    const package::Package& b) {
        if (a.parameters.size() != b.parameters.size()) return false;
        for (std::size_t index = 0u; index < a.parameters.size(); ++index) {
            if (!parameter_equal(a.parameters[index], b.parameters[index])) {
                return false;
            }
        }
        return true;
    };
    auto uploaded = [](const package::Package& pkg, std::uint32_t id) {
        for (const auto& instruction : pkg.initialization) {
            if (instruction.opcode == "init.upload_resource" &&
                !instruction.operands.empty() &&
                instruction.operands[0].type ==
                    package::OperandType::ResourceRef &&
                instruction.operands[0].u32 == id) {
                return true;
            }
        }
        return false;
    };
    auto texture_seed_equal = [](const package::Package& a,
                                 const package::Package& b,
                                 std::uint32_t id) {
        std::uint32_t old_seed = 0u;
        std::uint32_t new_seed = 0u;
        const bool old_ok = TextureSeedForResource(a, id, &old_seed);
        const bool new_ok = TextureSeedForResource(b, id, &new_seed);
        return old_ok && new_ok && old_seed == new_seed;
    };
    auto root_equal = [](const package::RootSignatureRecord& a,
                         const package::RootSignatureRecord& b) {
        if (a.root_signature_id != b.root_signature_id ||
            a.family != b.family || a.convention != b.convention ||
            a.dword_cost != b.dword_cost || a.blob != b.blob ||
            a.bindings.size() != b.bindings.size()) {
            return false;
        }
        for (std::size_t i = 0u; i < a.bindings.size(); ++i) {
            const auto& x = a.bindings[i];
            const auto& y = b.bindings[i];
            if (x.name != y.name || x.usage != y.usage ||
                x.slot != y.slot || x.space != y.space ||
                x.root_parameter != y.root_parameter) {
                return false;
            }
        }
        return true;
    };
    auto shader_equal = [](const package::ShaderRecord& a,
                           const package::ShaderRecord& b) {
        return a.shader_id == b.shader_id &&
               a.entry_point == b.entry_point && a.target == b.target &&
               a.blob == b.blob &&
               a.root_signature_id == b.root_signature_id &&
               a.family == b.family &&
               a.thread_group_x == b.thread_group_x &&
               a.thread_group_y == b.thread_group_y &&
               a.thread_group_z == b.thread_group_z &&
               a.flags == b.flags;
    };
    auto find_root = [](const package::Package& pkg, std::uint32_t id)
        -> const package::RootSignatureRecord* {
        for (const auto& item : pkg.root_signatures) {
            if (item.root_signature_id == id) return &item;
        }
        return nullptr;
    };
    auto find_shader = [](const package::Package& pkg, std::uint32_t id)
        -> const package::ShaderRecord* {
        for (const auto& item : pkg.shaders) {
            if (item.shader_id == id) return &item;
        }
        return nullptr;
    };

    const bool compute_only_packages =
        std::all_of(old_package.shaders.begin(), old_package.shaders.end(),
                    [](const package::ShaderRecord& shader) {
                        return shader.target == "cs_6_0";
                    }) &&
        std::all_of(loaded.package.shaders.begin(), loaded.package.shaders.end(),
                    [](const package::ShaderRecord& shader) {
                        return shader.target == "cs_6_0";
                    });

    for (auto it = state->pipelines.begin(); it != state->pipelines.end();) {
        const auto* old_shader = find_shader(old_package, it->shader_id);
        const auto* new_shader = find_shader(loaded.package, it->shader_id);
        bool keep = compute_only_packages && capability_same &&
                    old_shader != nullptr && new_shader != nullptr &&
                    shader_equal(*old_shader, *new_shader);
        if (keep) {
            const auto* old_root =
                find_root(old_package, old_shader->root_signature_id);
            const auto* new_root =
                find_root(loaded.package, new_shader->root_signature_id);
            keep = old_root != nullptr && new_root != nullptr &&
                   root_equal(*old_root, *new_root);
        }
        if (keep) {
            ++result.reused_pipelines;
            ++it;
        } else {
            if (it->pipeline_state != nullptr) it->pipeline_state->Release();
            it = state->pipelines.erase(it);
        }
    }

    for (auto it = state->root_signatures.begin();
         it != state->root_signatures.end();) {
        const auto* old_root = find_root(old_package, it->root_signature_id);
        const auto* new_root =
            find_root(loaded.package, it->root_signature_id);
        const bool keep = capability_same && old_root != nullptr &&
                          new_root != nullptr &&
                          root_equal(*old_root, *new_root);
        if (keep) {
            ++result.reused_root_signatures;
            ++it;
        } else {
            if (it->root_signature != nullptr) it->root_signature->Release();
            it = state->root_signatures.erase(it);
        }
    }

    const bool parameters_unchanged =
        all_parameters_equal(old_package, loaded.package);
    authoring_retained_resource_ids_.clear();
    for (auto& slot : state->resources) {
        bool keep = capability_same;
        const bool is_uploaded =
            uploaded(old_package, slot.resource_id) ||
            uploaded(loaded.package, slot.resource_id);
        if (keep && is_uploaded) {
            if (slot.resource_id == 1u) {
                keep = parameters_unchanged;
            } else {
                keep = texture_seed_equal(old_package, loaded.package,
                                          slot.resource_id);
            }
        }
        if (keep) {
            authoring_retained_resource_ids_.push_back(slot.resource_id);
            ++result.reused_resources;
            continue;
        }
        const ResourcePlacement* old_placement = old_plan.Find(slot.resource_id);
        if (slot.upload != nullptr) {
            slot.upload->Release();
            slot.upload = nullptr;
        }
        if (slot.resource != nullptr) {
            slot.resource->Release();
            slot.resource = nullptr;
            if (old_placement != nullptr &&
                old_placement->descriptor.usage != "sampler") {
                RemoveEngineResourceAllocation(
                    old_placement->allocation_size_bytes);
            }
        }
        slot.state = D3D12_RESOURCE_STATE_COMMON;
    }

    package_ = loaded.package;
    package_sha256_ = loaded.package_sha256;
    plan_ = plan_result.plan;
    using_aliased_plan_ = false;
    stats_.package_plan_bytes = plan_.total_engine_bytes;
    diagnostics_.clear();
    last_exit_ = ExitCode::Success;
    frame_ops_.clear();
    last_frame_order_.clear();
    timeline_parameter_id_ = 0u;
    timeline_tick_ = 0u;
    timeline_override_active_ = false;
    present_resource_id_ = 0u;
    present_source_width_ = 0u;
    present_source_height_ = 0u;

    if (!BuildFrameInstructionPlan()) {
        return finish(last_exit_, true);
    }
    authoring_reload_in_progress_ = true;
    const ExitCode init = RunInitializationInstructions();
    authoring_reload_in_progress_ = false;
    if (init != ExitCode::Success) {
        return finish(init, true);
    }
    authoring_retained_resource_ids_.clear();

    result.recreated_resources =
        static_cast<std::uint32_t>(state->resources.size()) -
        result.reused_resources;
    result.recreated_root_signatures =
        static_cast<std::uint32_t>(state->root_signatures.size()) -
        result.reused_root_signatures;
    result.recreated_pipelines =
        static_cast<std::uint32_t>(state->pipelines.size()) -
        result.reused_pipelines;

    if (CheckD3D12Validation("authoring_reload")) {
        return finish(last_exit_, true);
    }
    const ExitCode budget = CheckMemoryBudget("authoring_reload");
    if (budget != ExitCode::Success) {
        return finish(budget, true);
    }
    last_exit_ = ExitCode::Success;
    authoring_reload_stats_ = result;
    return result;
}
#endif

"""
    cpp = replace_once(
        cpp,
        "ExitCode PackagePlayer::initialize(const package::Package& pkg) {",
        methods + "ExitCode PackagePlayer::initialize(const package::Package& pkg) {",
        "authoring implementation",
    )
    cpp = replace_once(
        cpp,
        """    if (initialized_ && !explicit_path && !resize_in_progress_ &&
        !shutting_down_) {""",
        """    if (initialized_ && !explicit_path && !resize_in_progress_ &&
        !shutting_down_
#if NW08_AUTHORING_RUNTIME
        && !authoring_reload_in_progress_
#endif
    ) {""",
        "allocation accounting",
    )
    cpp = replace_once(
        cpp,
        r"""            if (record == nullptr) {
                return Fail(ExitCode::RootSignatureCreationFailed, "pipeline",
                            "root_signature_record_missing", "root_signature",
                            std::to_string(root_id), "missing",
                            "init.create_root_signature",
                            "package carries no root-signature record");
            }
            if (force_root_signature_creation_failure_) {""",
        r"""            if (record == nullptr) {
                return Fail(ExitCode::RootSignatureCreationFailed, "pipeline",
                            "root_signature_record_missing", "root_signature",
                            std::to_string(root_id), "missing",
                            "init.create_root_signature",
                            "package carries no root-signature record");
            }
#if NW08_AUTHORING_RUNTIME
            if (std::any_of(
                    state->root_signatures.begin(),
                    state->root_signatures.end(),
                    [root_id](const PlatformState::RootSlot& existing) {
                        return existing.root_signature_id == root_id &&
                               existing.root_signature != nullptr;
                    })) {
                continue;
            }
#endif
            if (force_root_signature_creation_failure_) {""",
        "root-signature reuse",
    )
    cpp = replace_once(
        cpp,
        r"""            if (shader == nullptr || root == nullptr) {
                return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                            "pipeline_dependency_missing", "pipeline",
                            std::to_string(shader_id),
                            shader == nullptr ? "shader missing"
                                              : "root signature missing",
                            "init.create_pipeline",
                            "compute pipeline dependencies are unavailable");
            }
            if (force_pipeline_creation_failure_) {""",
        r"""            if (shader == nullptr || root == nullptr) {
                return Fail(ExitCode::PipelineStateCreationFailed, "pipeline",
                            "pipeline_dependency_missing", "pipeline",
                            std::to_string(shader_id),
                            shader == nullptr ? "shader missing"
                                              : "root signature missing",
                            "init.create_pipeline",
                            "compute pipeline dependencies are unavailable");
            }
#if NW08_AUTHORING_RUNTIME
            if (std::any_of(
                    state->pipelines.begin(), state->pipelines.end(),
                    [shader_id](const PlatformState::PipelineSlot& existing) {
                        return existing.shader_id == shader_id &&
                               existing.pipeline_state != nullptr;
                    })) {
                continue;
            }
#endif
            if (force_pipeline_creation_failure_) {""",
        "pipeline reuse",
    )
    cpp = replace_once(
        cpp,
        r"""            const std::uint32_t resource_id = instruction.operands[0].u32;
            PlatformState::ResourceSlot* slot = nullptr;""",
        r"""            const std::uint32_t resource_id = instruction.operands[0].u32;
#if NW08_AUTHORING_RUNTIME
            if (IsAuthoringRetainedResource(resource_id)) {
                continue;
            }
#endif
            PlatformState::ResourceSlot* slot = nullptr;""",
        "upload reuse",
    )
    cpp = replace_once(
        cpp,
        r"""    std::vector<bool> alias_barrier_emitted(plan_.alias_barriers.size(), false);
    for (const auto& op : frame_ops_) {""",
        r"""    std::vector<bool> alias_barrier_emitted(plan_.alias_barriers.size(), false);
#if NW08_AUTHORING_RUNTIME
    authoring_pass_timings_.clear();
#endif
    for (const auto& op : frame_ops_) {
#if NW08_AUTHORING_RUNTIME
        const auto authoring_pass_begin = std::chrono::steady_clock::now();
#endif""",
        "authoring pass timing begin",
    )
    cpp = replace_once(
        cpp,
        r"""                            "frame operation is unavailable in this build");
        }
    }
    if (present_requested && state->swapchain != nullptr) {""",
        r"""                            "frame operation is unavailable in this build");
        }
#if NW08_AUTHORING_RUNTIME
        const auto authoring_pass_end = std::chrono::steady_clock::now();
        authoring_pass_timings_.push_back(AuthoringPassTiming{
            op.opcode,
            std::chrono::duration<double, std::milli>(
                authoring_pass_end - authoring_pass_begin).count(),
        });
#endif
    }
    if (present_requested && state->swapchain != nullptr) {""",
        "authoring pass timing end",
    )
    cpp = replace_once(
        cpp,
        r"""    frame_ops_.clear();
    last_frame_order_.clear();
    initialization_count_ = 0u;""",
        r"""    frame_ops_.clear();
    last_frame_order_.clear();
#if NW08_AUTHORING_RUNTIME
    authoring_retained_resource_ids_.clear();
    authoring_pass_timings_.clear();
    authoring_reload_stats_ = AuthoringReloadResult{};
    authoring_reload_in_progress_ = false;
#endif
    initialization_count_ = 0u;""",
        "authoring reset",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "package_player.h").write_text(header, encoding="utf-8", newline="\n")
    (output_dir / "package_player.cpp").write_text(cpp, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    generate(args.repo_root.resolve(), args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
