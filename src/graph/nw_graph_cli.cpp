#include "nw_graph.h"

#include <algorithm>
#include <cstdio>
#include <cstdint>
#include <fstream>
#include <initializer_list>
#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

namespace {

using namespace nw::graph;

std::string JsonString(std::string_view text) {
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
    out += "\"";
    return out;
}

std::string DiagnosticsJson(const std::vector<Diagnostic>& diagnostics) {
    std::string out = "[";
    for (std::size_t i = 0; i < diagnostics.size(); ++i) {
        if (i != 0) {
            out += ",";
        }
        out += DiagnosticToJson(diagnostics[i]);
    }
    out += "]";
    return out;
}

bool ReadFile(const std::string& path, std::string& text) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        return false;
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    text = buffer.str();
    return true;
}

bool ParseU64(const std::string& text, std::uint64_t& value) {
    if (text.empty()) {
        return false;
    }
    std::uint64_t result = 0;
    for (char c : text) {
        if (c < '0' || c > '9') {
            return false;
        }
        result = result * 10u + static_cast<std::uint64_t>(c - '0');
    }
    value = result;
    return true;
}

struct LoadedGraph {
    bool ok = false;
    GraphDocument document;
    ValidationResult validation;
    std::vector<Diagnostic> parse_diagnostics;

    const std::vector<Diagnostic>& diagnostics() const {
        return validation.diagnostics.empty() ? parse_diagnostics
                                              : validation.diagnostics;
    }
};

LoadedGraph LoadGraph(const std::string& path, bool migrate,
                      const OperatorRegistry& registry) {
    LoadedGraph loaded;
    std::string text;
    if (!ReadFile(path, text)) {
        Diagnostic diagnostic;
        diagnostic.code = DiagnosticCode::ParseError;
        diagnostic.message = "cannot read graph file '" + path + "'";
        loaded.parse_diagnostics.push_back(std::move(diagnostic));
        return loaded;
    }
    ParseResult parsed = ParseGraphText(text, registry, path);
    loaded.parse_diagnostics = parsed.diagnostics;
    if (!parsed.document) {
        return loaded;
    }
    loaded.document = *parsed.document;
    if (HasErrors(parsed.diagnostics)) {
        return loaded;
    }
    if (loaded.document.schema_version != kCurrentSchemaVersion) {
        if (!migrate) {
            loaded.validation = ValidateGraphDocument(loaded.document, registry);
            loaded.ok = false;
            return loaded;
        }
        MigrationResult migration =
            MigrateGraphDocument(loaded.document, registry);
        if (!migration.document) {
            loaded.parse_diagnostics = migration.diagnostics;
            return loaded;
        }
        loaded.document = *migration.document;
    }
    loaded.validation = ValidateGraphDocument(loaded.document, registry);
    loaded.ok = loaded.validation.ok();
    return loaded;
}

bool HasOption(const std::vector<std::string>& args, const std::string& name) {
    return std::find(args.begin(), args.end(), name) != args.end();
}

bool OptionValue(const std::vector<std::string>& args, const std::string& name,
                 std::string& value) {
    for (std::size_t i = 0; i + 1u < args.size(); ++i) {
        if (args[i] == name) {
            value = args[i + 1u];
            return true;
        }
    }
    return false;
}

std::string HexU64(std::uint64_t value) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string out = "0x";
    for (int i = 15; i >= 0; --i) {
        out.push_back(kHex[(value >> (static_cast<unsigned>(i) * 4u)) & 0x0fu]);
    }
    return out;
}

void PrintValidation(const LoadedGraph& loaded) {
    const GraphSnapshot* snapshot =
        loaded.validation.snapshot ? &*loaded.validation.snapshot : nullptr;
    std::cout << "{";
    std::cout << "\"valid\":" << (loaded.ok ? "true" : "false");
    std::cout << ",\"schema_version\":" << loaded.document.schema_version;
    std::cout << ",\"profile_id\":" << JsonString(loaded.document.profile.id);
    std::cout << ",\"requirements_digest\":"
              << JsonString(loaded.document.profile.requirements_digest);
    const std::string semantic_digest =
        snapshot ? snapshot->semantic_digest() : std::string();
    std::cout << ",\"semantic_digest\":"
              << JsonString(semantic_digest);
    std::cout << ",\"node_count\":" << loaded.document.nodes.size();
    std::cout << ",\"diagnostics\":" << DiagnosticsJson(loaded.diagnostics());
    std::cout << "}\n";
}

std::string EvalStatsJson(const EvaluationResult& result) {
    std::ostringstream out;
    out << "{\"ok\":" << (result.ok ? "true" : "false");
    out << ",\"cache\":{\"hits\":" << result.stats.hits
        << ",\"misses\":" << result.stats.misses
        << ",\"regenerated\":" << result.stats.regenerated << "}";
    out << ",\"hits_by_node\":{";
    bool first = true;
    for (const auto& [node_id, hit] : result.cache_hit_by_node) {
        if (!first) out << ",";
        first = false;
        out << JsonString(node_id) << ":" << (hit ? "true" : "false");
    }
    out << "},\"diagnostics\":" << DiagnosticsJson(result.diagnostics) << "}";
    return out.str();
}

std::string EvalValuesJson(const GraphSnapshot& snapshot,
                           const EvaluationResult& result) {
    struct Row {
        std::string key;
        std::string json;
    };
    std::vector<Row> rows;
    for (const auto& [node_id, outputs] : result.values_by_node) {
        const NodeDefinition* node = snapshot.FindNode(node_id);
        if (node == nullptr) {
            continue;
        }
        const std::string hash = snapshot.StructuralHash(node_id);
        const std::string op =
            node->operator_id + "@" + node->operator_version.ToString();
        for (const auto& [port, value] : outputs) {
            std::ostringstream json;
            json << "{\"operator\":" << JsonString(op)
                 << ",\"structural_hash\":" << JsonString(hash)
                 << ",\"port\":" << JsonString(port)
                 << ",\"type\":" << JsonString(ToString(value.type))
                 << ",\"residency\":" << JsonString(ToString(value.residency))
                 << ",\"value\":" << JsonString(value.CanonicalText()) << "}";
            rows.push_back({hash + "." + port, json.str()});
        }
    }
    std::sort(rows.begin(), rows.end(),
              [](const Row& a, const Row& b) { return a.key < b.key; });
    std::string out = "[";
    for (std::size_t i = 0; i < rows.size(); ++i) {
        if (i != 0) out += ",";
        out += rows[i].json;
    }
    out += "]";
    return out;
}

std::string ResourceJson(const ResourceDescriptor& descriptor) {
    std::ostringstream out;
    out << "{\"kind\":" << JsonString(descriptor.kind)
        << ",\"max_width\":" << descriptor.max_width
        << ",\"max_height\":" << descriptor.max_height
        << ",\"max_depth\":" << descriptor.max_depth
        << ",\"max_bytes\":" << descriptor.max_bytes
        << ",\"max_vertices\":" << descriptor.max_vertices
        << ",\"max_indices\":" << descriptor.max_indices
        << ",\"format_class\":" << JsonString(descriptor.format_class)
        << ",\"bounded\":" << (descriptor.bounded ? "true" : "false") << "}";
    return out.str();
}

std::string PortJson(const PortSpec& port) {
    std::ostringstream out;
    out << "{\"id\":" << JsonString(port.id)
        << ",\"type\":" << JsonString(ToString(port.type))
        << ",\"residency\":" << JsonString(ToString(port.residency))
        << ",\"dimension\":" << port.dimension
        << ",\"resource\":";
    if (port.descriptor) {
        out << ResourceJson(*port.descriptor);
    } else {
        out << "null";
    }
    out << "}";
    return out.str();
}

std::string ParamSpecJson(const ParamSpec& param) {
    std::ostringstream out;
    out << "{\"id\":" << JsonString(param.id)
        << ",\"type\":" << JsonString(ToString(param.type))
        << ",\"required\":" << (param.required ? "true" : "false")
        << ",\"normalization\":" << JsonString(ToString(param.normalization))
        << ",\"has_range\":" << (param.has_range ? "true" : "false")
        << ",\"min\":" << param.min_value
        << ",\"max\":" << param.max_value
        << ",\"bounded_resource_dimension\":"
        << (param.bounded_resource_dimension ? "true" : "false")
        << ",\"enum_values\":[";
    for (std::size_t i = 0; i < param.enum_values.size(); ++i) {
        if (i != 0) out << ",";
        out << JsonString(param.enum_values[i]);
    }
    out << "]";
    if (param.default_value) {
        out << ",\"default\":" << JsonString(ParamCanonicalText(
            *param.default_value));
    } else {
        out << ",\"default\":null";
    }
    out << "}";
    return out.str();
}

std::string RegistryJson(const OperatorRegistry& registry) {
    std::vector<OperatorSchema> operators = registry.All();
    std::sort(operators.begin(), operators.end(),
              [](const OperatorSchema& a, const OperatorSchema& b) {
                  if (a.id != b.id) return a.id < b.id;
                  return a.version < b.version;
              });
    std::ostringstream out;
    out << "{\"operators\":[";
    for (std::size_t i = 0; i < operators.size(); ++i) {
        const OperatorSchema& schema = operators[i];
        if (i != 0) out << ",";
        out << "{\"id\":" << JsonString(schema.id)
            << ",\"version\":" << JsonString(schema.version.ToString())
            << ",\"kind\":" << JsonString(ToString(schema.kind))
            << ",\"summary\":" << JsonString(schema.summary)
            << ",\"frequency\":[";
        for (std::size_t f = 0; f < schema.frequency_policy.allowed.size();
             ++f) {
            if (f != 0) out << ",";
            out << JsonString(ToString(schema.frequency_policy.allowed[f]));
        }
        out << "],\"default_frequency\":"
            << JsonString(ToString(schema.frequency_policy.default_frequency))
            << ",\"seed_behavior\":"
            << JsonString(ToString(schema.default_seed_behavior))
            << ",\"allowed_seed_behaviors\":[";
        for (std::size_t s = 0; s < schema.allowed_seed_behaviors.size(); ++s) {
            if (s != 0) out << ",";
            out << JsonString(ToString(schema.allowed_seed_behaviors[s]));
        }
        out << "],\"inputs\":[";
        for (std::size_t p = 0; p < schema.inputs.size(); ++p) {
            if (p != 0) out << ",";
            out << PortJson(schema.inputs[p]);
        }
        out << "],\"outputs\":[";
        for (std::size_t p = 0; p < schema.outputs.size(); ++p) {
            if (p != 0) out << ",";
            out << PortJson(schema.outputs[p]);
        }
        out << "],\"params\":[";
        for (std::size_t p = 0; p < schema.params.size(); ++p) {
            if (p != 0) out << ",";
            out << ParamSpecJson(schema.params[p]);
        }
        out << "],\"resource\":";
        if (schema.resource_descriptor) {
            out << ResourceJson(*schema.resource_descriptor);
        } else {
            out << "null";
        }
        out << ",\"produces_resource\":"
            << (schema.produces_resource ? "true" : "false")
            << ",\"explicit_readback\":"
            << (schema.explicit_readback ? "true" : "false")
            << ",\"explicit_residency_conversion\":"
            << (schema.explicit_residency_conversion ? "true" : "false")
            << ",\"lowering_hook\":" << JsonString(schema.lowering_hook)
            << ",\"lowering_hook_version\":"
            << JsonString(schema.lowering_hook_version)
            << ",\"shader_operations\":[";
        for (std::size_t s = 0; s < schema.shader_operations.size(); ++s) {
            if (s != 0) out << ",";
            out << JsonString(schema.shader_operations[s]);
        }
        out << "],\"contract_digest\":"
            << JsonString(OperatorContractDigest(schema)) << "}";
    }
    out << "]}";
    return out.str();
}

std::string DocumentJson(const GraphDocument& document) {
    std::ostringstream out;
    out << "{\"schema_version\":" << document.schema_version
        << ",\"profile\":{\"id\":" << JsonString(document.profile.id)
        << ",\"requirements_digest\":"
        << JsonString(document.profile.requirements_digest) << "}"
        << ",\"compiler_settings\":{\"optimization\":"
        << JsonString(document.settings.optimization)
        << ",\"deterministic\":"
        << (document.settings.deterministic ? "true" : "false")
        << ",\"api\":" << JsonString(document.settings.api)
        << ",\"feature_level\":" << JsonString(document.settings.feature_level)
        << ",\"shader_model\":" << JsonString(document.settings.shader_model)
        << ",\"frequency_policy\":"
        << JsonString(document.settings.frequency_policy)
        << ",\"shader_blob_policy\":"
        << JsonString(document.settings.shader_blob_policy)
        << ",\"quantization_policy\":"
        << JsonString(document.settings.quantization.policy)
        << ",\"quantization_bits\":"
        << document.settings.quantization.bits
        << ",\"quantization_step\":"
        << document.settings.quantization.step << "},\"nodes\":[";
    for (std::size_t i = 0; i < document.nodes.size(); ++i) {
        const NodeDefinition& node = document.nodes[i];
        if (i != 0) out << ",";
        out << "{\"id\":" << JsonString(node.id)
            << ",\"operator_id\":" << JsonString(node.operator_id)
            << ",\"operator_version\":"
            << JsonString(node.operator_version.ToString())
            << ",\"frequency\":" << JsonString(ToString(node.frequency))
            << ",\"residency\":" << JsonString(ToString(node.residency))
            << ",\"seed\":{\"behavior\":"
            << JsonString(ToString(node.seed.behavior))
            << ",\"explicit\":" << (node.seed.has_explicit_seed ? "true" : "false")
            << ",\"value\":" << node.seed.explicit_seed
            << ",\"node_id\":" << JsonString(node.seed.node_id) << "}"
            << ",\"parameters\":{";
        bool first = true;
        for (const auto& [name, value] : node.params) {
            if (!first) out << ",";
            first = false;
            out << JsonString(name) << ":"
                << JsonString(ParamCanonicalText(value));
        }
        out << "},\"inputs\":[";
        for (std::size_t p = 0; p < node.inputs.size(); ++p) {
            if (p != 0) out << ",";
            out << "{\"port\":" << JsonString(node.inputs[p].port)
                << ",\"source_node\":"
                << JsonString(node.inputs[p].source_node)
                << ",\"source_port\":"
                << JsonString(node.inputs[p].source_port) << "}";
        }
        out << "]}";
    }
    out << "],\"editor_layout\":{";
    bool first = true;
    for (const auto& [node_id, layout] : document.layout.nodes) {
        if (!first) out << ",";
        first = false;
        out << JsonString(node_id) << ":{\"x\":" << layout.x
            << ",\"y\":" << layout.y
            << ",\"collapsed\":" << (layout.collapsed ? "true" : "false")
            << "}";
    }
    out << "},\"textual_schema\":\"nwgraph-v1\"}";
    return out.str();
}

int ValidateCommand(const std::vector<std::string>& args,
                    const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli validate <file> [--migrate]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    PrintValidation(loaded);
    return loaded.ok ? 0 : 1;
}

int DigestCommand(const std::vector<std::string>& args,
                  const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli digest <file> [--migrate]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"valid\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    std::cout << loaded.validation.snapshot->semantic_digest() << "\n";
    return 0;
}

int CanonicalCommand(const std::vector<std::string>& args,
                     const OperatorRegistry& registry, bool bytes) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli canonical <file> [--migrate] "
                     "[--out <path>]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"valid\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    if (!bytes) {
        std::cout << loaded.validation.snapshot->canonical_text();
        return 0;
    }
    std::string out_path;
    if (!OptionValue(args, "--out", out_path)) {
        std::cerr << "--out <path> is required for canonical-bytes\n";
        return 2;
    }
    std::ofstream output(out_path, std::ios::binary);
    if (!output) {
        std::cerr << "cannot write " << out_path << "\n";
        return 1;
    }
    const auto& data = loaded.validation.snapshot->canonical_bytes();
    output.write(reinterpret_cast<const char*>(data.data()),
                 static_cast<std::streamsize>(data.size()));
    return output ? 0 : 1;
}

int EvalCommand(const std::vector<std::string>& args,
                const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli eval <file> [--migrate] "
                     "[--frame N] [--seed N] [--workers N]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"ok\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    EvaluationContext context;
    std::string value;
    if (OptionValue(args, "--frame", value)) {
        ParseU64(value, context.frame_index);
    }
    if (OptionValue(args, "--seed", value)) {
        ParseU64(value, context.seed_base);
    }
    if (OptionValue(args, "--workers", value)) {
        std::uint64_t workers = 1;
        ParseU64(value, workers);
        context.worker_count = static_cast<unsigned>(workers);
    }
    GraphEvaluator evaluator(*loaded.validation.snapshot);
    EvaluationResult result = evaluator.Evaluate(context);
    std::cout << "{\"ok\":" << (result.ok ? "true" : "false")
              << ",\"semantic_digest\":"
              << JsonString(loaded.validation.snapshot->semantic_digest())
              << ",\"cache\":{\"hits\":" << result.stats.hits
              << ",\"misses\":" << result.stats.misses
              << ",\"regenerated\":" << result.stats.regenerated << "}"
              << ",\"values\":"
              << EvalValuesJson(*loaded.validation.snapshot, result)
              << ",\"diagnostics\":" << DiagnosticsJson(result.diagnostics)
              << "}\n";
    return result.ok ? 0 : 1;
}

int SeedCommand(const std::vector<std::string>& args,
                const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli seed <file> [--seed N]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"ok\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    EvaluationContext context;
    std::string value;
    if (OptionValue(args, "--seed", value)) {
        ParseU64(value, context.seed_base);
    }
    struct Row {
        std::string hash;
        std::string json;
    };
    std::vector<Row> rows;
    for (const auto& node : loaded.validation.snapshot->nodes()) {
        const std::string hash =
            loaded.validation.snapshot->StructuralHash(node.id);
        std::ostringstream json;
        json << "{\"operator\":"
             << JsonString(node.operator_id + "@" +
                           node.operator_version.ToString())
             << ",\"structural_hash\":" << JsonString(hash)
             << ",\"seed\":"
             << JsonString(HexU64(DeriveSeed(
                    *loaded.validation.snapshot, node, context)))
             << "}";
        rows.push_back({hash, json.str()});
    }
    std::sort(rows.begin(), rows.end(),
              [](const Row& a, const Row& b) { return a.hash < b.hash; });
    std::cout << "{\"ok\":true,\"seeds\":[";
    for (std::size_t i = 0; i < rows.size(); ++i) {
        if (i != 0) std::cout << ",";
        std::cout << rows[i].json;
    }
    std::cout << "]}\n";
    return 0;
}

int CacheCommand(const std::vector<std::string>& args,
                 const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli cache <file> [--invalidate <node>] "
                     "[--corrupt <node>]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"ok\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    EvaluationContext context;
    GraphEvaluator evaluator(*loaded.validation.snapshot);
    EvaluationResult initial = evaluator.Evaluate(context);
    EvaluationResult repeat = evaluator.Evaluate(context);
    std::cout << "{\"ok\":true,\"initial\":" << EvalStatsJson(initial)
              << ",\"repeat\":" << EvalStatsJson(repeat);
    std::string invalidate;
    if (OptionValue(args, "--invalidate", invalidate)) {
        evaluator.ApplyInputChange(invalidate);
        EvaluationResult after = evaluator.Evaluate(context);
        std::cout << ",\"after_invalidate\":" << EvalStatsJson(after);
    }
    std::string corrupt;
    if (OptionValue(args, "--corrupt", corrupt)) {
        evaluator.CorruptCacheEntry(corrupt);
        EvaluationResult after = evaluator.Evaluate(context);
        std::cout << ",\"after_corrupt\":" << EvalStatsJson(after);
    }
    std::cout << "}\n";
    return 0;
}

int LastValidCommand(const std::vector<std::string>& args,
                     const OperatorRegistry& registry) {
    if (args.size() < 2u) {
        std::cerr << "usage: nw_graph_cli last-valid <valid-file> "
                     "<invalid-file>\n";
        return 2;
    }
    std::string valid_text;
    std::string invalid_text;
    if (!ReadFile(args[0], valid_text) || !ReadFile(args[1], invalid_text)) {
        std::cerr << "cannot read input files\n";
        return 1;
    }
    AuthoringSession session(registry);
    ValidationResult valid = session.ApplyEdit(valid_text, args[0]);
    const std::string before = session.LastValid()
                                   ? session.LastValid()->semantic_digest()
                                   : std::string();
    ValidationResult invalid = session.ApplyEdit(invalid_text, args[1]);
    const std::string after = session.LastValid()
                                  ? session.LastValid()->semantic_digest()
                                  : std::string();
    std::cout << "{\"ok\":true"
              << ",\"initial_valid\":" << (valid.ok() ? "true" : "false")
              << ",\"invalid_edit_rejected\":"
              << (!invalid.ok() ? "true" : "false")
              << ",\"initial_digest\":" << JsonString(before)
              << ",\"after_invalid_digest\":" << JsonString(after)
              << ",\"retained\":"
              << ((!before.empty() && before == after) ? "true" : "false")
              << ",\"diagnostics\":"
              << DiagnosticsJson(invalid.diagnostics) << "}\n";
    return valid.ok() && !invalid.ok() && !before.empty() && before == after
               ? 0
               : 1;
}

int ContractCommand(const std::vector<std::string>& args,
                    const OperatorRegistry& registry) {
    if (args.size() < 2u) {
        std::cerr << "usage: nw_graph_cli contract <operator-id> <version>\n";
        return 2;
    }
    const auto version = SemanticVersion::Parse(args[1]);
    const OperatorSchema* schema =
        version ? registry.Find(args[0], *version) : nullptr;
    if (schema == nullptr) {
        std::cerr << "unknown operator version\n";
        return 1;
    }
    std::cout << OperatorContractDigest(*schema) << "\n";
    return 0;
}

int AcceptCommand(const std::vector<std::string>& args,
                  const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli accept <file> [--migrate] "
                     "[--gpu-model ...] [--gpu-generation ...] "
                     "[--adapter ...] [--pci-vendor N] [--pci-device N]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"accepted\":false,\"profile_id\":\"\","
                     "\"requirements_digest\":\"\",\"semantic_digest\":\"\","
                     "\"gpu_available\":"
                  << (HasOption(args, "--gpu-present") ? "true" : "false")
                  << ",\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    ObservedHardwareMetadata observed;
    std::string value;
    OptionValue(args, "--gpu-model", observed.gpu_model);
    OptionValue(args, "--gpu-generation", observed.gpu_generation);
    OptionValue(args, "--adapter", observed.adapter_description);
    if (OptionValue(args, "--pci-vendor", value)) {
        std::uint64_t parsed = 0;
        ParseU64(value, parsed);
        observed.pci_vendor_id = static_cast<std::uint32_t>(parsed);
    }
    if (OptionValue(args, "--pci-device", value)) {
        std::uint64_t parsed = 0;
        ParseU64(value, parsed);
        observed.pci_device_id = static_cast<std::uint32_t>(parsed);
    }
    CompilerAcceptance acceptance = AcceptGraphForCompilation(
        loaded.document, observed, HasOption(args, "--gpu-present"), registry);
    std::cout << "{\"accepted\":"
              << (acceptance.accepted ? "true" : "false")
              << ",\"profile_id\":" << JsonString(acceptance.profile_id)
              << ",\"requirements_digest\":"
              << JsonString(acceptance.requirements_digest)
              << ",\"semantic_digest\":"
              << JsonString(acceptance.semantic_digest)
              << ",\"gpu_available\":"
              << (HasOption(args, "--gpu-present") ? "true" : "false")
              << ",\"diagnostics\":"
              << DiagnosticsJson(acceptance.diagnostics) << "}\n";
    return acceptance.accepted ? 0 : 1;
}

int ProfileCommand(const std::vector<std::string>& args) {
    if (args.size() < 2u) {
        std::cerr << "usage: nw_graph_cli profile <id> <requirements-digest>\n";
        return 2;
    }
    CapabilityProfileIdentity identity{args[0], args[1]};
    std::vector<Diagnostic> diagnostics = ValidateCapabilityProfile(identity);
    const bool ok = diagnostics.empty();
    std::cout << "{\"valid\":" << (ok ? "true" : "false")
              << ",\"profile_id\":" << JsonString(identity.id)
              << ",\"requirements_digest\":"
              << JsonString(identity.requirements_digest)
              << ",\"diagnostics\":" << DiagnosticsJson(diagnostics)
              << "}\n";
    return ok ? 0 : 1;
}

int MigrateCommand(const std::vector<std::string>& args,
                   const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli migrate <file>\n";
        return 2;
    }
    std::string text;
    if (!ReadFile(args[0], text)) {
        std::cerr << "cannot read " << args[0] << "\n";
        return 1;
    }
    ParseResult parsed = ParseGraphText(text, registry, args[0]);
    if (!parsed.document || HasErrors(parsed.diagnostics)) {
        std::cout << "{\"migrated\":false,\"diagnostics\":"
                  << DiagnosticsJson(parsed.diagnostics) << "}\n";
        return 1;
    }
    MigrationResult migration =
        MigrateGraphDocument(*parsed.document, registry);
    if (!migration.document) {
        std::cout << "{\"migrated\":false,\"diagnostics\":"
                  << DiagnosticsJson(migration.diagnostics) << "}\n";
        return 1;
    }
    std::cout << EmitDocumentText(*migration.document);
    return 0;
}

int FieldDigestCommand() {
    const auto digests = GoldenFieldDigests();
    std::cout << "{";
    bool first = true;
    for (const auto& [name, digest] : digests) {
        if (!first) std::cout << ",";
        first = false;
        std::cout << JsonString(name) << ":" << JsonString(digest);
    }
    std::cout << "}\n";
    return 0;
}

int SelfTestCommand(const OperatorRegistry& registry) {
    struct Check {
        std::string name;
        bool passed;
    };
    std::vector<Check> checks;
    checks.push_back({"sha256_abc",
                      Sha256Hex("abc") ==
                          "ba7816bf8f01cfea414140de5dae2223"
                          "b00361a396177a9cb410ff61f20015ad"});
    const auto golden = GoldenFieldDigests();
    checks.push_back({"golden_field_cases", golden.size() >= 6u});

    const OperatorSchema* add = registry.Find("nw.math.add",
                                              SemanticVersion{1, 0, 0});
    checks.push_back({"registered_add", add != nullptr});
    if (add != nullptr) {
        OperatorSchema bumped = *add;
        bumped.version = SemanticVersion{1, 0, 1};
        checks.push_back({"operator_version_changes_contract",
                          OperatorContractDigest(*add) !=
                              OperatorContractDigest(bumped)});
        OperatorSchema mutated = *add;
        if (!mutated.outputs.empty()) {
            mutated.outputs[0].residency = Residency::Gpu;
        }
        checks.push_back({"operator_semantics_change_contract",
                          OperatorContractDigest(*add) !=
                              OperatorContractDigest(mutated)});
    }
    checks.push_back({"profile_identity",
                      ValidateCapabilityProfile(
                          CapabilityProfileIdentity{
                              std::string(kCommonProfileId.data(),
                                          kCommonProfileId.size()),
                              std::string(kCommonRequirementsDigest.data(),
                                          kCommonRequirementsDigest.size())})
                          .empty()});
    if (add != nullptr) {
        OperatorRegistry extended = OperatorRegistry::CreateDefault();
        OperatorSchema extension_operator = *add;
        extension_operator.id = "nw.extension.test_add";
        extension_operator.version = SemanticVersion{1, 0, 0};
        extension_operator.lowering_hook = "nw.extension.test";
        ExtensionRegistration extension;
        extension.extension_id = "nw.extension.test";
        extension.version = SemanticVersion{1, 0, 0};
        extension.operators = {extension_operator};
        extension.lowering_namespace = "nw.extension.test";
        std::vector<std::string> extension_diagnostics;
        const bool registered = RegisterAuthoringExtension(
            extended, extension, extension_diagnostics);
        checks.push_back(
            {"authoring_extension_registration",
             registered &&
                 extended.Find("nw.extension.test_add",
                               SemanticVersion{1, 0, 0}) != nullptr &&
                 extension_diagnostics.empty()});
    }
    bool all = true;
    std::cout << "{\"ok\":true,\"checks\":[";
    for (std::size_t i = 0; i < checks.size(); ++i) {
        if (i != 0) std::cout << ",";
        std::cout << "{\"name\":" << JsonString(checks[i].name)
                  << ",\"passed\":" << (checks[i].passed ? "true" : "false")
                  << "}";
        all = all && checks[i].passed;
    }
    std::cout << "],\"all_passed\":" << (all ? "true" : "false") << "}\n";
    return all ? 0 : 1;
}

int ExportJsonCommand(const std::vector<std::string>& args,
                      const OperatorRegistry& registry) {
    if (args.empty()) {
        std::cerr << "usage: nw_graph_cli export-json <file> [--migrate]\n";
        return 2;
    }
    LoadedGraph loaded =
        LoadGraph(args[0], HasOption(args, "--migrate"), registry);
    if (!loaded.ok) {
        std::cout << "{\"valid\":false,\"diagnostics\":"
                  << DiagnosticsJson(loaded.diagnostics()) << "}\n";
        return 1;
    }
    std::cout << DocumentJson(loaded.document) << "\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    const OperatorRegistry registry = OperatorRegistry::CreateDefault();
    if (argc < 2) {
        std::cerr << "usage: nw_graph_cli <command> [args...]\n";
        return 2;
    }
    const std::string command = argv[1];
    std::vector<std::string> args;
    for (int i = 2; i < argc; ++i) {
        args.emplace_back(argv[i]);
    }
    if (command == "validate") {
        return ValidateCommand(args, registry);
    }
    if (command == "digest") {
        return DigestCommand(args, registry);
    }
    if (command == "canonical") {
        return CanonicalCommand(args, registry, false);
    }
    if (command == "canonical-bytes") {
        return CanonicalCommand(args, registry, true);
    }
    if (command == "eval") {
        return EvalCommand(args, registry);
    }
    if (command == "seed") {
        return SeedCommand(args, registry);
    }
    if (command == "cache") {
        return CacheCommand(args, registry);
    }
    if (command == "last-valid") {
        return LastValidCommand(args, registry);
    }
    if (command == "registry") {
        std::cout << RegistryJson(registry) << "\n";
        return 0;
    }
    if (command == "profile") {
        return ProfileCommand(args);
    }
    if (command == "accept") {
        return AcceptCommand(args, registry);
    }
    if (command == "migrate") {
        return MigrateCommand(args, registry);
    }
    if (command == "contract") {
        return ContractCommand(args, registry);
    }
    if (command == "field-digest") {
        return FieldDigestCommand();
    }
    if (command == "selftest") {
        return SelfTestCommand(registry);
    }
    if (command == "export-json") {
        return ExportJsonCommand(args, registry);
    }
    if (command == "hash-text") {
        std::string text;
        for (std::size_t i = 0; i < args.size(); ++i) {
            if (i != 0) text.push_back(' ');
            text += args[i];
        }
        std::cout << Sha256Digest(text) << "\n";
        return 0;
    }
    if (command == "migrations") {
        std::cout << "{\"migrations\":[";
        const auto migrations = AvailableMigrations();
        for (std::size_t i = 0; i < migrations.size(); ++i) {
            if (i != 0) std::cout << ",";
            std::cout << "{\"id\":" << JsonString(migrations[i].id)
                      << ",\"from\":" << migrations[i].from
                      << ",\"to\":" << migrations[i].to
                      << ",\"description\":"
                      << JsonString(migrations[i].description) << "}";
        }
        std::cout << "]}\n";
        return 0;
    }
    std::cerr << "unknown command '" << command << "'\n";
    return 2;
}
