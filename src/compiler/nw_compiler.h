#pragma once

// NW-03 compiler-side lowering and offline package compilation.
//
// This target consumes NW-02 immutable graph snapshots and ShaderExpr values.
// It contains no player runtime graph parser, plugin ABI, or graphics API
// abstraction.  HLSL is generated and compiled with a pinned offline DXC.

#include "nw_graph.h"
#include "nwp_format.h"

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace nw::compiler {

struct SourceMapEntry {
    std::uint32_t generated_line = 0;
    std::uint32_t generated_column = 0;
    std::string node_id;
    std::string operator_id;
    std::string port_id;
    std::string expression_id;
};

struct GeneratedShader {
    std::string specialization_id;
    std::string entry_point = "CSMain";
    std::string target = "cs_6_0";
    std::string family = "compute.math_noise";
    std::string hlsl;
    std::string root_expression;
    std::vector<std::string> root_node_ids;
    std::vector<SourceMapEntry> source_map;
    std::array<std::uint32_t, 3> thread_group = {64u, 1u, 1u};

    std::string HlslSha256() const;
    std::string SpecializationKey() const;
};

struct CompilerOptions {
    std::string optimization = "O2";
    std::string target = "cs_6_0";
    std::string entry_point = "CSMain";
    std::array<std::uint32_t, 3> thread_group = {64u, 1u, 1u};
    std::array<std::uint32_t, 3> dispatch_groups = {4u, 1u, 1u};
};

struct LoweringDiagnostic {
    std::string category = "graph_validation";
    std::string code;
    std::string message;
    std::string node_id;
    std::string operator_id;
    std::string port_id;
    std::string section;
    std::uint64_t offset = 0;
    bool has_offset = false;
    std::uint32_t line = 0;
    std::uint32_t column = 0;
};

struct LoweringResult {
    std::vector<GeneratedShader> shaders;
    std::vector<LoweringDiagnostic> diagnostics;

    bool ok() const { return diagnostics.empty() && !shaders.empty(); }
};

// Lower every reachable ShaderExpr entry point.  Unreachable ShaderExpr nodes
// are not emitted; identical complete generated entry points are deduplicated.
LoweringResult LowerGraphSnapshot(const graph::GraphSnapshot& snapshot,
                                  const CompilerOptions& options = {});

struct CompiledShader {
    GeneratedShader shader;
    std::vector<std::uint8_t> blob;
    std::string blob_sha256;
    std::string measured_dxc_version;
    bool cache_hit = false;
};

struct CompileResult {
    bool ok = false;
    std::vector<std::uint8_t> package_bytes;
    package::Package package;
    std::vector<CompiledShader> shaders;
    std::vector<LoweringDiagnostic> diagnostics;
    std::string compiler_fingerprint;
    std::string profile_fingerprint;
};

// Compile every generated specialization with real offline DXC, validate the
// DXIL payload, assemble a `.nwp` v1 package, and validate it before returning.
CompileResult CompileGraphPackage(
    const graph::GraphSnapshot& snapshot,
    const std::string& dxc_executable,
    const CompilerOptions& options = {});

} // namespace nw::compiler
