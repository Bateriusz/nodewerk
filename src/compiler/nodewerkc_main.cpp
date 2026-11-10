// NW-03 `nodewerkc` command-line frontend.
//
// Commands:
//   nodewerkc validate <graph.nwgraph>
//   nodewerkc compile-package <graph.nwgraph> <out.nwp> [--dxc <path>]
//   nodewerkc inspect <package.nwp>

#include "nw_compiler.h"
#include "nw_graph.h"
#include "nwp_format.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

namespace {

std::string JsonEscape(std::string_view text) {
    std::string out = "\"";
    for (char c : text) {
        switch (c) {
        case '\\': out += "\\\\"; break;
        case '"': out += "\\\""; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (static_cast<unsigned char>(c) < 0x20u) {
                char buffer[8]{};
                std::snprintf(buffer, sizeof(buffer), "\\u%04x",
                              static_cast<unsigned>(c));
                out += buffer;
            } else {
                out.push_back(c);
            }
            break;
        }
    }
    out.push_back('"');
    return out;
}

bool ReadTextFile(const std::string& path, std::string& text) {
    std::ifstream input(path, std::ios::binary);
    if (!input) return false;
    std::ostringstream buffer;
    buffer << input.rdbuf();
    text = buffer.str();
    return true;
}

std::string DiagnosticsJson(
    const std::vector<nw::graph::Diagnostic>& diagnostics) {
    std::string out = "[";
    for (std::size_t i = 0; i < diagnostics.size(); ++i) {
        if (i != 0) out += ",";
        out += nw::graph::DiagnosticToJson(diagnostics[i]);
    }
    out += "]";
    return out;
}

std::string PackageDiagnosticsJson(
    const std::vector<nw::package::Diagnostic>& diagnostics) {
    std::string out = "[";
    for (std::size_t i = 0; i < diagnostics.size(); ++i) {
        if (i != 0) out += ",";
        out += nw::package::DiagnosticToJson(diagnostics[i]);
    }
    out += "]";
    return out;
}

struct LoadedGraph {
    bool ok = false;
    nw::graph::GraphDocument document;
    nw::graph::ValidationResult validation;
    std::vector<nw::graph::Diagnostic> diagnostics;
};

LoadedGraph LoadGraph(const std::string& path,
                      const nw::graph::OperatorRegistry& registry) {
    LoadedGraph loaded;
    std::string text;
    if (!ReadTextFile(path, text)) {
        nw::graph::Diagnostic diagnostic;
        diagnostic.code = nw::graph::DiagnosticCode::ParseError;
        diagnostic.message = "cannot read graph '" + path + "'";
        loaded.diagnostics.push_back(std::move(diagnostic));
        return loaded;
    }
    nw::graph::ParseResult parsed =
        nw::graph::ParseGraphText(text, registry, path);
    loaded.diagnostics = parsed.diagnostics;
    if (!parsed.document || nw::graph::HasErrors(parsed.diagnostics)) {
        return loaded;
    }
    loaded.document = *parsed.document;
    loaded.validation =
        nw::graph::ValidateGraphDocument(loaded.document, registry);
    loaded.diagnostics = loaded.validation.diagnostics;
    loaded.ok = loaded.validation.ok();
    return loaded;
}

int ValidateCommand(const std::string& path) {
    const auto registry = nw::graph::OperatorRegistry::CreateDefault();
    LoadedGraph loaded = LoadGraph(path, registry);
    const auto* snapshot =
        loaded.validation.snapshot ? &*loaded.validation.snapshot : nullptr;
    std::cout << "{";
    std::cout << "\"ok\":" << (loaded.ok ? "true" : "false");
    std::cout << ",\"valid\":" << (loaded.ok ? "true" : "false");
    std::cout << ",\"profile_id\":"
              << JsonEscape(loaded.document.profile.id);
    std::cout << ",\"requirements_digest\":"
              << JsonEscape(loaded.document.profile.requirements_digest);
    std::cout << ",\"semantic_digest\":"
              << JsonEscape(snapshot ? snapshot->semantic_digest() : "");
    std::cout << ",\"node_count\":" << loaded.document.nodes.size();
    if (loaded.ok && snapshot != nullptr) {
        nw::compiler::LoweringResult lowering =
            nw::compiler::LowerGraphSnapshot(*snapshot);
        std::cout << ",\"lowering\":{\"specialization_count\":"
                  << lowering.shaders.size() << ",\"specializations\":[";
        for (std::size_t i = 0; i < lowering.shaders.size(); ++i) {
            if (i != 0) std::cout << ",";
            const auto& shader = lowering.shaders[i];
            std::cout << "{\"specialization_id\":"
                      << JsonEscape(shader.specialization_id)
                      << ",\"specialization_key\":"
                      << JsonEscape(shader.SpecializationKey())
                      << ",\"entry_point\":"
                      << JsonEscape(shader.entry_point)
                      << ",\"target\":" << JsonEscape(shader.target)
                      << ",\"hlsl_sha256\":"
                      << JsonEscape(shader.HlslSha256()) << "}";
        }
        std::cout << "],\"diagnostics\":[";
        for (std::size_t i = 0; i < lowering.diagnostics.size(); ++i) {
            if (i != 0) std::cout << ",";
            const auto& diagnostic = lowering.diagnostics[i];
            std::cout << "{\"category\":"
                      << JsonEscape(diagnostic.category)
                      << ",\"severity\":\"error\",\"code\":"
                      << JsonEscape(diagnostic.code)
                      << ",\"message\":" << JsonEscape(diagnostic.message)
                      << ",\"source\":{\"node_id\":"
                      << JsonEscape(diagnostic.node_id)
                      << ",\"operator_id\":"
                      << JsonEscape(diagnostic.operator_id)
                      << ",\"port_id\":" << JsonEscape(diagnostic.port_id)
                      << "}}";
        }
        std::cout << "]}";
    }
    std::cout << ",\"diagnostics\":" << DiagnosticsJson(loaded.diagnostics);
    std::cout << "}\n";
    return loaded.ok ? 0 : 1;
}

int InspectCommand(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        std::cerr << "{\"ok\":false,\"valid\":false,\"error\":"
                  << JsonEscape("cannot read package '" + path + "'")
                  << "}\n";
        return 1;
    }
    const std::string text((std::istreambuf_iterator<char>(input)),
                           std::istreambuf_iterator<char>());
    std::vector<std::uint8_t> data(text.size());
    if (!data.empty()) {
        std::memcpy(data.data(), text.data(), text.size());
    }
    nw::package::Package package;
    std::vector<nw::package::Diagnostic> diagnostics;
    const bool valid = nw::package::DecodePackage(
        data.data(), data.size(), package, diagnostics, false,
        nw::package::kMaxPackageBytes, nw::package::kMaxSectionBytes, true);
    const std::uint64_t checksum_offset =
        data.size() >= nw::package::kChecksumSize
            ? data.size() - nw::package::kChecksumSize
            : 0u;
    const std::string checksum_hex =
        checksum_offset != 0u
            ? nw::package::BytesToHex(
                  data.data() + checksum_offset, nw::package::kChecksumSize)
            : std::string();
    std::cout << nw::package::InspectJson(package, valid, diagnostics,
                                           data.size(), checksum_hex)
              << "\n";
    return valid ? 0 : 1;
}

int CompilePackageCommand(const std::string& graph_path,
                          const std::string& output_path,
                          const std::string& dxc_path) {
    const auto registry = nw::graph::OperatorRegistry::CreateDefault();
    LoadedGraph loaded = LoadGraph(graph_path, registry);
    if (!loaded.ok) {
        std::cerr << "{\"ok\":false,\"valid\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics) << "}\n";
        return 1;
    }
    nw::compiler::CompileResult result = nw::compiler::CompileGraphPackage(
        *loaded.validation.snapshot, dxc_path);
    if (!result.ok) {
        std::cerr << "{\"ok\":false,\"valid\":false,\"diagnostics\":[";
        for (std::size_t i = 0; i < result.diagnostics.size(); ++i) {
            if (i != 0) std::cerr << ",";
            const auto& diagnostic = result.diagnostics[i];
            std::cerr << "{\"category\":"
                      << JsonEscape(diagnostic.category)
                      << ",\"severity\":\"error\",\"code\":"
                      << JsonEscape(diagnostic.code)
                      << ",\"message\":"
                      << JsonEscape(diagnostic.message)
                      << ",\"source\":{\"node_id\":"
                      << JsonEscape(diagnostic.node_id)
                      << ",\"operator_id\":"
                      << JsonEscape(diagnostic.operator_id)
                      << ",\"port_id\":" << JsonEscape(diagnostic.port_id)
                      << "}}";
        }
        std::cerr << "]}\n";
        return 1;
    }
    const std::filesystem::path destination(output_path);
    const std::filesystem::path temporary =
        destination.parent_path() / (destination.filename().string() + ".tmp");
    std::error_code error;
    std::filesystem::create_directories(destination.parent_path(), error);
    {
        std::ofstream output(temporary, std::ios::binary);
        output.write(reinterpret_cast<const char*>(result.package_bytes.data()),
                     static_cast<std::streamsize>(result.package_bytes.size()));
        if (!output.good()) {
            std::cerr << "{\"ok\":false,\"error\":\"cannot write package\"}\n";
            return 1;
        }
    }
#if defined(_WIN32)
    std::filesystem::remove(destination, error);
#endif
    std::filesystem::rename(temporary, destination, error);
    if (error) {
        std::filesystem::remove(temporary, error);
        std::cerr << "{\"ok\":false,\"error\":\"cannot publish package\"}\n";
        return 1;
    }
    std::cout << "{\"ok\":true,\"valid\":true,\"output\":"
              << JsonEscape(output_path)
              << ",\"package_sha256\":"
              << JsonEscape(nw::package::Sha256Hex(result.package_bytes))
              << ",\"compiler_fingerprint\":"
              << JsonEscape(result.compiler_fingerprint)
              << ",\"profile_fingerprint\":"
              << JsonEscape(result.profile_fingerprint) << "}\n";
    return 0;
}

void Usage() {
    std::cerr
        << "usage:\n"
        << "  nodewerkc validate <graph.nwgraph>\n"
        << "  nodewerkc compile-package <graph.nwgraph> <out.nwp> "
           "[--dxc <path>]\n"
        << "  nodewerkc inspect <package.nwp>\n";
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        Usage();
        return 2;
    }
    const std::string command = argv[1];
    if (command == "validate") {
        return ValidateCommand(argv[2]);
    }
    if (command == "inspect") {
        return InspectCommand(argv[2]);
    }
    if (command == "compile-package") {
        if (argc < 4) {
            Usage();
            return 2;
        }
        std::string dxc = "dxc";
        for (int index = 4; index + 1 < argc; ++index) {
            if (std::string(argv[index]) == "--dxc") {
                dxc = argv[index + 1];
            }
        }
        return CompilePackageCommand(argv[2], argv[3], dxc);
    }
    Usage();
    return 2;
}
