#include "nw_compiler.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <map>
#include <set>
#include <sstream>
#include <system_error>
#include <utility>

namespace nw::compiler {

namespace {

constexpr char kPinnedDxcVersion[] = "1.8.2407";

bool IsShaderOperator(std::string_view id) {
    return id == "nw.shader.expr" || id == "nw.shader.vec4_const" ||
           id == "nw.shader.add" || id == "nw.shader.mul" ||
           id == "nw.shader.mix";
}

LoweringDiagnostic MakeDiagnostic(std::string code, std::string message,
                                  std::string node_id = {},
                                  std::string operator_id = {},
                                  std::string port_id = {},
                                  std::string category = "graph_validation",
                                  std::string section = {},
                                  std::uint64_t offset = 0,
                                  bool has_offset = false) {
    LoweringDiagnostic diagnostic;
    diagnostic.category = std::move(category);
    diagnostic.code = std::move(code);
    diagnostic.message = std::move(message);
    diagnostic.node_id = std::move(node_id);
    diagnostic.operator_id = std::move(operator_id);
    diagnostic.port_id = std::move(port_id);
    diagnostic.section = std::move(section);
    diagnostic.offset = offset;
    diagnostic.has_offset = has_offset;
    return diagnostic;
}

std::string ReplaceShaderTokens(std::string expression, std::string& error) {
    if (expression.find("texture2d") != std::string::npos ||
        expression.find("sample(") != std::string::npos) {
        error =
            "texture sampling is not emitted by the initial compute math/noise "
            "pass family";
        return {};
    }
    const std::pair<std::string_view, std::string_view> replacements[] = {
        {"vec2", "float2"},
        {"vec3", "float3"},
        {"vec4", "float4"},
        {"mix", "lerp"},
        {"time", "g_time"},
    };
    std::string replaced;
    replaced.reserve(expression.size() + 32u);
    std::size_t position = 0;
    while (position < expression.size()) {
        const unsigned char character =
            static_cast<unsigned char>(expression[position]);
        if (std::isalpha(character) || character == '_') {
            const std::size_t start = position++;
            while (position < expression.size()) {
                const unsigned char next =
                    static_cast<unsigned char>(expression[position]);
                if (!std::isalnum(next) && next != '_') break;
                ++position;
            }
            const std::string_view token(expression.data() + start,
                                         position - start);
            bool replaced_token = false;
            if (token == "const") {
                // NW-02 const(x) is a semantic identity; strip the operation
                // and leave its parenthesized argument in place.
                std::size_t lookahead = position;
                while (lookahead < expression.size() &&
                       std::isspace(static_cast<unsigned char>(
                           expression[lookahead]))) {
                    ++lookahead;
                }
                if (lookahead < expression.size() &&
                    expression[lookahead] == '(') {
                    position = lookahead;
                    replaced_token = true;
                }
            }
            for (const auto& [from, to] : replacements) {
                if (replaced_token) break;
                if (token == from) {
                    replaced.append(to.data(), to.size());
                    replaced_token = true;
                    break;
                }
            }
            if (!replaced_token) replaced.append(token.data(), token.size());
        } else {
            replaced.push_back(expression[position++]);
        }
    }
    return replaced;
}

std::vector<std::string> TransitiveDependencies(
    const graph::GraphSnapshot& snapshot, std::string_view root) {
    std::vector<std::string> result;
    std::set<std::string> seen;
    const std::function<void(std::string_view)> visit = [&](std::string_view id) {
        const std::string key(id);
        if (!seen.insert(key).second) return;
        const graph::NodeDefinition* node = snapshot.FindNode(key);
        if (node == nullptr) return;
        for (const auto& input : node->inputs) {
            visit(input.source_node);
        }
        result.push_back(key);
    };
    visit(root);
    std::sort(result.begin(), result.end());
    return result;
}

std::string EmitComputeHlsl(const std::string& expression,
                            const CompilerOptions& options,
                            std::vector<SourceMapEntry>& source_map,
                            const std::vector<std::string>& node_ids) {
    const std::uint32_t group_x = options.thread_group[0];
    const std::uint32_t group_y = options.thread_group[1];
    const std::uint32_t group_z = options.thread_group[2];
    const std::uint32_t width = group_x * options.dispatch_groups[0];
    const std::uint32_t height = group_y * options.dispatch_groups[1];
    std::ostringstream out;
    out << "// Generated by nodewerkc NW-03; fused ShaderExpr compute entry point.\n"
        << "// No source paths, editor layout, reflection, or debug metadata are emitted.\n"
        << "cbuffer NwParams : register(b0) { float4 g_params[4]; };\n"
        << "RWByteAddressBuffer nw_output : register(u0);\n\n"
        << "[numthreads(" << group_x << ", " << group_y << ", " << group_z
        << ")]\n"
        << "void " << options.entry_point
        << "(uint3 nw_dispatch_id : SV_DispatchThreadID) {\n"
        << "    float2 uv = (float2(nw_dispatch_id.xy) + 0.5) / float2("
        << width << ".0, " << height << ".0);\n"
        << "    float g_time = g_params[0].x;\n"
        << "    float4 nw_value = " << expression << ";\n"
        << "    uint nw_index = nw_dispatch_id.x + nw_dispatch_id.y * "
        << width << "u + nw_dispatch_id.z * " << width * height << "u;\n"
        << "    nw_output.Store4(nw_index * 16u, asuint(nw_value));\n"
        << "}\n";
    for (const auto& node_id : node_ids) {
        SourceMapEntry entry;
        entry.generated_line = 10u;
        entry.generated_column = 24u;
        entry.node_id = node_id;
        entry.expression_id = "nw_value";
        source_map.push_back(std::move(entry));
    }
    return out.str();
}

std::vector<std::uint8_t> ReadFileBytes(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) return {};
    const std::string text((std::istreambuf_iterator<char>(input)),
                           std::istreambuf_iterator<char>());
    std::vector<std::uint8_t> bytes(text.size());
    if (!bytes.empty()) {
        std::memcpy(bytes.data(), text.data(), text.size());
    }
    return bytes;
}

bool WriteFileBytes(const std::filesystem::path& path,
                    const std::vector<std::uint8_t>& bytes) {
    std::error_code error;
    std::filesystem::create_directories(path.parent_path(), error);
    std::ofstream output(path, std::ios::binary);
    if (!output) return false;
    output.write(reinterpret_cast<const char*>(bytes.data()),
                 static_cast<std::streamsize>(bytes.size()));
    return output.good();
}

std::string Quote(const std::string& value) { return "\"" + value + "\""; }

std::string DxcPrefix(const std::string& dxc) {
    if (dxc.size() > 3u && dxc.substr(dxc.size() - 3u) == ".py") {
#if defined(_WIN32)
        return std::string("python ");
#else
        return std::string("python3 ");
#endif
    }
    return {};
}

std::string QueryDxcVersion(const std::string& dxc) {
    const std::string command =
        DxcPrefix(dxc) + Quote(dxc) + " --version 2>&1";
#if defined(_WIN32)
    std::FILE* pipe = _popen(command.c_str(), "r");
#else
    std::FILE* pipe = popen(command.c_str(), "r");
#endif
    if (pipe == nullptr) return "unknown";
    std::string output;
    char buffer[512]{};
    while (std::fgets(buffer, sizeof(buffer), pipe) != nullptr) {
        output += buffer;
    }
#if defined(_WIN32)
    _pclose(pipe);
#else
    pclose(pipe);
#endif
    // Extract the first dotted numeric version.  This deliberately avoids
    // <regex> so the authoring frontend remains buildable with the same
    // minimal standard-library subset used by the concrete Windows gate.
    for (std::size_t i = 0; i < output.size(); ++i) {
        if (!std::isdigit(static_cast<unsigned char>(output[i]))) continue;
        if (i != 0u &&
            std::isdigit(static_cast<unsigned char>(output[i - 1u]))) {
            continue;
        }
        std::size_t position = i;
        int dot_count = 0;
        while (position < output.size()) {
            if (std::isdigit(static_cast<unsigned char>(output[position]))) {
                ++position;
                continue;
            }
            if (output[position] == '.' && dot_count < 3) {
                ++dot_count;
                ++position;
                continue;
            }
            break;
        }
        if (dot_count >= 2) {
            return output.substr(i, position - i);
        }
    }
    return output.empty() ? "unknown" : output.substr(0, output.find('\n'));
}

std::string CompilerFingerprint(const graph::GraphSnapshot& snapshot,
                                const std::vector<GeneratedShader>& shaders,
                                const std::string& dxc_identity,
                                const std::string& optimization) {
    std::ostringstream canonical;
    canonical << "nw03-compiler-fingerprint-v1\n"
              << snapshot.semantic_digest() << "\n"
              << dxc_identity << "\n"
              << optimization << "\n"
              << "compute.math_noise.v1\n";
    for (const auto& shader : shaders) {
        canonical << shader.SpecializationKey() << "\n";
    }
    return "sha256:v1:" +
           package::Sha256Hex(canonical.str());
}

} // namespace

std::string GeneratedShader::HlslSha256() const {
    return package::Sha256Hex(hlsl);
}

std::string GeneratedShader::SpecializationKey() const {
    std::ostringstream canonical;
    canonical << entry_point << "\n"
              << target << "\n"
              << family << "\n"
              << HlslSha256() << "\n"
              << thread_group[0] << "," << thread_group[1] << ","
              << thread_group[2] << "\n";
    return package::Sha256Hex(canonical.str());
}

LoweringResult LowerGraphSnapshot(const graph::GraphSnapshot& snapshot,
                                  const CompilerOptions& options) {
    LoweringResult result;
    graph::GraphEvaluator evaluator(snapshot);
    const graph::EvaluationResult evaluation =
        evaluator.Evaluate(graph::EvaluationContext{});
    if (!evaluation.ok) {
        for (const auto& diagnostic : evaluation.diagnostics) {
            LoweringDiagnostic lowered;
            lowered.code = std::string(graph::ToString(diagnostic.code));
            lowered.message = diagnostic.message;
            lowered.node_id = diagnostic.source.node_id;
            lowered.operator_id = diagnostic.source.operator_id;
            lowered.port_id = diagnostic.source.port_id;
            result.diagnostics.push_back(std::move(lowered));
        }
        return result;
    }

    std::map<std::string, std::vector<std::string>> shader_consumers;
    std::vector<std::string> gpu_roots;
    for (const auto& node : snapshot.nodes()) {
        for (const auto& input : node.inputs) {
            const graph::NodeDefinition* source =
                snapshot.FindNode(input.source_node);
            if (source == nullptr) continue;
            if (IsShaderOperator(source->operator_id)) {
                shader_consumers[source->id].push_back(node.id);
            }
            if (input.port == "shader" &&
                node.operator_id.rfind("nw.render.", 0) == 0 &&
                IsShaderOperator(source->operator_id)) {
                gpu_roots.push_back(source->id);
            }
        }
    }
    std::sort(gpu_roots.begin(), gpu_roots.end());
    gpu_roots.erase(std::unique(gpu_roots.begin(), gpu_roots.end()),
                    gpu_roots.end());

    std::vector<std::string> roots;
    if (!gpu_roots.empty()) {
        roots = gpu_roots;
    } else {
        for (const auto& node : snapshot.nodes()) {
            if (!IsShaderOperator(node.operator_id)) continue;
            bool consumed_by_shader = false;
            auto found = shader_consumers.find(node.id);
            if (found != shader_consumers.end()) {
                for (const auto& consumer_id : found->second) {
                    const graph::NodeDefinition* consumer =
                        snapshot.FindNode(consumer_id);
                    if (consumer != nullptr &&
                        IsShaderOperator(consumer->operator_id)) {
                        consumed_by_shader = true;
                        break;
                    }
                }
            }
            if (!consumed_by_shader) roots.push_back(node.id);
        }
    }
    if (roots.empty()) {
        result.diagnostics.push_back(MakeDiagnostic(
            "missing_implementation",
            "graph has no reachable ShaderExpr producer"));
        return result;
    }

    std::set<std::string> seen_keys;
    for (const auto& root_id : roots) {
        const graph::NodeDefinition* root = snapshot.FindNode(root_id);
        if (root == nullptr) continue;
        auto value_node = evaluation.values_by_node.find(root_id);
        if (value_node == evaluation.values_by_node.end()) {
            result.diagnostics.push_back(MakeDiagnostic(
                "evaluation_failure",
                "reachable shader node has no evaluated value", root_id,
                root->operator_id));
            continue;
        }
        auto value = value_node->second.find("out");
        if (value == value_node->second.end()) {
            result.diagnostics.push_back(MakeDiagnostic(
                "evaluation_failure", "shader node has no 'out' value", root_id,
                root->operator_id));
            continue;
        }
        std::string error;
        const std::string hlsl_expression =
            ReplaceShaderTokens(value->second.text, error);
        if (!error.empty()) {
            LoweringDiagnostic diagnostic = MakeDiagnostic(
                "invalid_expression", error, root_id, root->operator_id,
                "expression");
            result.diagnostics.push_back(std::move(diagnostic));
            continue;
        }
        GeneratedShader shader;
        shader.specialization_id = root_id;
        shader.entry_point = options.entry_point;
        shader.target = options.target;
        shader.family = "compute.math_noise";
        shader.thread_group = options.thread_group;
        shader.root_expression = value->second.text;
        shader.root_node_ids = TransitiveDependencies(snapshot, root_id);
        shader.hlsl = EmitComputeHlsl(hlsl_expression, options,
                                      shader.source_map,
                                      shader.root_node_ids);
        for (auto& entry : shader.source_map) {
            const graph::NodeDefinition* node = snapshot.FindNode(entry.node_id);
            if (node != nullptr) entry.operator_id = node->operator_id;
        }
        const std::string key = shader.SpecializationKey();
        if (!seen_keys.insert(key).second) continue;
        result.shaders.push_back(std::move(shader));
    }
    return result;
}

CompileResult CompileGraphPackage(const graph::GraphSnapshot& snapshot,
                                  const std::string& dxc_executable,
                                  const CompilerOptions& options) {
    CompileResult result;
    LoweringResult lowering = LowerGraphSnapshot(snapshot, options);
    result.diagnostics = lowering.diagnostics;
    if (!lowering.ok()) return result;
    const std::string dxc_version = QueryDxcVersion(dxc_executable);
    if (dxc_version == "unknown" ||
        dxc_version.find(kPinnedDxcVersion) == std::string::npos) {
        result.diagnostics.push_back(MakeDiagnostic(
            "toolchain_version_mismatch",
            "DXC version '" + dxc_version + "' does not match pinned '" +
                kPinnedDxcVersion + "'",
            "", "", "", "package_validation"));
        return result;
    }
    const std::string dxc_identity = "dxc:" + dxc_version;
    const std::filesystem::path temporary =
        std::filesystem::temp_directory_path() /
        ("nw03-compile-" + package::Sha256Hex(dxc_identity +
                                              std::to_string(
                                                  std::chrono::steady_clock::now()
                                                      .time_since_epoch()
                                                      .count())));
    std::error_code filesystem_error;
    std::filesystem::create_directories(temporary, filesystem_error);
    for (std::size_t index = 0; index < lowering.shaders.size(); ++index) {
        const auto& shader = lowering.shaders[index];
        const auto hlsl_path = temporary / ("shader_" + std::to_string(index) + ".hlsl");
        const auto dxil_path = temporary / ("shader_" + std::to_string(index) + ".dxil");
        const auto log_path = temporary / ("shader_" + std::to_string(index) + ".log");
        std::vector<std::uint8_t> hlsl_bytes(shader.hlsl.size());
        if (!hlsl_bytes.empty()) {
            std::memcpy(hlsl_bytes.data(), shader.hlsl.data(),
                        shader.hlsl.size());
        }
        if (!WriteFileBytes(hlsl_path, hlsl_bytes)) {
            result.diagnostics.push_back(MakeDiagnostic(
                "shader_compilation_failed", "cannot write temporary HLSL",
                shader.source_map.empty() ? "" : shader.source_map.front().node_id,
                ""));
            return result;
        }
        std::ostringstream command;
        command << DxcPrefix(dxc_executable) << Quote(dxc_executable)
                << " -T " << shader.target << " -E " << shader.entry_point
                << " -HV 2021 -" << options.optimization
                << " -Qstrip_reflect -Qstrip_debug -Fo "
                << Quote(dxil_path.string()) << " "
                << Quote(hlsl_path.string()) << " > "
                << Quote(log_path.string()) << " 2>&1";
        const int exit_code = std::system(command.str().c_str());
        CompiledShader compiled;
        compiled.shader = shader;
        compiled.blob = ReadFileBytes(dxil_path);
        std::string reason;
        if (exit_code != 0 || compiled.blob.empty() ||
            !package::ValidateDxilPayload(compiled.blob, reason)) {
            std::ifstream log(log_path);
            std::ostringstream log_text;
            log_text << log.rdbuf();
            result.diagnostics.push_back(MakeDiagnostic(
                "shader_compilation_failed",
                "DXC failed for specialization '" + shader.specialization_id +
                    "': " + (reason.empty() ? log_text.str() : reason),
                shader.source_map.empty() ? "" : shader.source_map.front().node_id,
                ""));
            return result;
        }
        compiled.blob_sha256 = package::Sha256Hex(compiled.blob);
        compiled.measured_dxc_version = dxc_version;
        result.shaders.push_back(std::move(compiled));
    }
    std::filesystem::remove_all(temporary, filesystem_error);

    package::Package package;
    package.flags = 1u;
    package::RootBinding params{"params", "cbv", 0u, 0u, 0u};
    package::RootBinding output{"output", "uav", 0u, 0u, 1u};
    package::RootSignatureRecord root_signature;
    root_signature.root_signature_id = 1u;
    root_signature.family = "compute.math_noise";
    root_signature.convention = "compute.math_noise.v1";
    root_signature.dword_cost = 4u;
    root_signature.bindings = {params, output};
    root_signature.blob =
        package::MakeRootSignatureBlob(root_signature.family,
                                       root_signature.bindings);
    package.root_signatures.push_back(root_signature);

    package::ResourceDescriptor params_resource;
    params_resource.resource_id = 1u;
    params_resource.name = "params";
    params_resource.kind = "buffer";
    params_resource.format = "DXGI_FORMAT_R32G32B32A32_FLOAT";
    params_resource.usage = "cbv";
    params_resource.pass_family = "compute.math_noise";
    params_resource.size_bytes = 64u;
    package.resources.push_back(params_resource);
    package::ResourceDescriptor output_resource;
    output_resource.resource_id = 2u;
    output_resource.name = "output";
    output_resource.kind = "buffer";
    output_resource.format = "DXGI_FORMAT_R32_UINT";
    output_resource.usage = "uav";
    output_resource.pass_family = "compute.math_noise";
    output_resource.size_bytes = 256u * 16u;
    package.resources.push_back(output_resource);

    package::ParameterRecord time_parameter;
    time_parameter.parameter_id = 1u;
    time_parameter.name = "time";
    time_parameter.type = "float";
    time_parameter.phase = "frame";
    time_parameter.component_count = 1u;
    time_parameter.value = {0u, 0u, 0u, 0u};
    package.parameters.push_back(time_parameter);

    std::vector<GeneratedShader> sorted_shaders;
    for (const auto& shader : lowering.shaders) sorted_shaders.push_back(shader);
    std::sort(sorted_shaders.begin(), sorted_shaders.end(),
              [](const GeneratedShader& a, const GeneratedShader& b) {
                  return a.specialization_id < b.specialization_id;
              });
    for (std::size_t index = 0; index < sorted_shaders.size(); ++index) {
        const auto& shader = sorted_shaders[index];
        package::ShaderRecord record;
        record.shader_id = static_cast<std::uint32_t>(index + 1u);
        record.entry_point = shader.entry_point;
        record.target = shader.target;
        record.root_signature_id = 1u;
        record.family = shader.family;
        record.thread_group_x = shader.thread_group[0];
        record.thread_group_y = shader.thread_group[1];
        record.thread_group_z = shader.thread_group[2];
        for (const auto& compiled : result.shaders) {
            if (compiled.shader.SpecializationKey() == shader.SpecializationKey()) {
                record.blob = compiled.blob;
                break;
            }
        }
        package.shaders.push_back(std::move(record));
    }

    package::InstructionOperand resource_one;
    resource_one.type = package::OperandType::ResourceRef;
    resource_one.u32 = 1u;
    package::InstructionOperand resource_two;
    resource_two.type = package::OperandType::ResourceRef;
    resource_two.u32 = 2u;
    package::InstructionOperand root_one;
    root_one.type = package::OperandType::RootSignatureRef;
    root_one.u32 = 1u;
    package::InstructionOperand zero_u32;
    zero_u32.type = package::OperandType::U32;
    zero_u32.u32 = 0u;
    package::InstructionOperand one_u32;
    one_u32.type = package::OperandType::U32;
    one_u32.u32 = 1u;
    package::InstructionOperand four_u32;
    four_u32.type = package::OperandType::U32;
    four_u32.u32 = 4u;
    package::InstructionOperand dispatch_x;
    dispatch_x.type = package::OperandType::U32;
    dispatch_x.u32 = options.dispatch_groups[0];
    package::InstructionOperand dispatch_y;
    dispatch_y.type = package::OperandType::U32;
    dispatch_y.u32 = options.dispatch_groups[1];
    package::InstructionOperand dispatch_z;
    dispatch_z.type = package::OperandType::U32;
    dispatch_z.u32 = options.dispatch_groups[2];
    package::InstructionOperand descriptor_type;
    descriptor_type.type = package::OperandType::String;
    descriptor_type.string_value = "cbv_srv_uav";
    package.initialization = {
        {"init.create_resource", {resource_one}},
        {"init.create_resource", {resource_two}},
        {"init.create_root_signature", {root_one}},
        {"init.upload_resource", {resource_one}},
    };
    package.frame = {
        {"frame.set_root_signature", {root_one}},
        {"frame.set_root_constants", {zero_u32, resource_one, four_u32}},
        {"frame.set_descriptor_table",
         {one_u32, descriptor_type, resource_two}},
    };
    for (std::size_t index = 0; index < sorted_shaders.size(); ++index) {
        package::InstructionOperand shader_ref;
        shader_ref.type = package::OperandType::ShaderRef;
        shader_ref.u32 = static_cast<std::uint32_t>(index + 1u);
        package.initialization.push_back(
            {"init.create_pipeline", {shader_ref, root_one}});
        package.frame.push_back(
            {"frame.dispatch",
             {dispatch_x, dispatch_y, dispatch_z, shader_ref}});
    }
    std::set<std::string> emitted_formats;
    std::uint32_t descriptor_count = 0u;
    std::uint32_t sampler_count = 0u;
    std::uint64_t max_buffer_bytes = 0u;
    std::uint32_t max_texture_dimension = 0u;
    for (const auto& resource : package.resources) {
        emitted_formats.insert(resource.format);
        if (resource.usage == "sampler") {
            ++sampler_count;
        } else if (resource.usage == "srv" || resource.usage == "uav") {
            ++descriptor_count;
        }
        if (resource.kind == "buffer") {
            max_buffer_bytes =
                std::max(max_buffer_bytes, resource.size_bytes);
        }
        if (resource.kind == "texture2d") {
            max_texture_dimension =
                std::max(max_texture_dimension,
                         std::max(resource.width, resource.height));
        }
    }
    std::uint32_t max_threads = 0u;
    for (const auto& shader : package.shaders) {
        max_threads = std::max<std::uint32_t>(
            max_threads, shader.thread_group_x * shader.thread_group_y *
                             shader.thread_group_z);
    }
    package.capability.required_dxgi_formats.assign(emitted_formats.begin(),
                                                    emitted_formats.end());
    package.capability.max_cbv_srv_uav_descriptors_per_frame = descriptor_count;
    package.capability.max_sampler_descriptors_per_frame = sampler_count;
    package.capability.max_root_signature_dword_cost = root_signature.dword_cost;
    package.capability.limit_max_texture_2d_dimension = max_texture_dimension;
    package.capability.limit_max_compute_threads_per_group = max_threads;
    package.capability.limit_max_buffer_bytes = max_buffer_bytes;
    package.capability.limit_swap_chain_buffer_count = 0u;
    std::set<std::string> targets;
    std::set<std::string> init_opcodes;
    std::set<std::string> frame_opcodes;
    for (const auto& shader : package.shaders) targets.insert(shader.target);
    for (const auto& instruction : package.initialization)
        init_opcodes.insert(instruction.opcode);
    for (const auto& instruction : package.frame)
        frame_opcodes.insert(instruction.opcode);
    package.capability.emitted_shader_targets.assign(targets.begin(),
                                                     targets.end());
    package.capability.required_init_instructions.assign(init_opcodes.begin(),
                                                         init_opcodes.end());
    package.capability.required_frame_instructions.assign(frame_opcodes.begin(),
                                                          frame_opcodes.end());
    package.capability.toolchain_dxc_version = dxc_version;
    package.capability.toolchain_identity = dxc_identity;
    result.compiler_fingerprint =
        CompilerFingerprint(snapshot, lowering.shaders, dxc_identity,
                            options.optimization);
    package.capability.compiler_fingerprint = result.compiler_fingerprint;
    result.profile_fingerprint =
        package::ComputeProfileFingerprint(package.capability);
    package.capability.profile_fingerprint = result.profile_fingerprint;
    result.package = package;
    std::vector<package::Diagnostic> validation;
    result.package_bytes = package::EncodePackage(package, validation);
    if (result.package_bytes.empty()) {
        for (const auto& diagnostic : validation) {
            result.diagnostics.push_back(
                MakeDiagnostic(diagnostic.code, diagnostic.message, "", "",
                               "", "package_validation", diagnostic.section,
                               diagnostic.offset, diagnostic.has_offset));
        }
        return result;
    }
    const auto leakage = package::CheckReleaseLeakage(result.package_bytes);
    if (!leakage.empty()) {
        for (const auto& finding : leakage) {
            result.diagnostics.push_back(
                MakeDiagnostic("release_leakage", finding, "", "", "",
                               "package_validation"));
        }
        return result;
    }
    result.ok = true;
    return result;
}

} // namespace nw::compiler
